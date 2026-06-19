from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from countywatch.config import Settings
from countywatch.db import Database
from countywatch.directory import refresh_directory
from countywatch.llm import LLMRouter
from countywatch.pipeline import UpdatePipeline, bundled_counties


class _DirectoryClient:
    def __init__(self) -> None:
        self.calls = 0

    async def fetch(self, url: str):
        self.calls += 1
        return type(
            "Result",
            (),
            {
                "content": b'<div>County Website <a href="https://anderson.example.gov">Visit</a></div>',
                "final_url": url,
            },
        )()


@pytest.mark.asyncio
async def test_directory_refresh_interval_is_configurable(tmp_path: Path):
    db = Database(tmp_path / "state.sqlite3")
    try:
        db.bootstrap_counties(bundled_counties())
        checked = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        db.update_county(
            "48001",
            official_url="https://anderson.example.gov/",
            site_status="resolved",
            site_last_checked=checked,
        )
        client = _DirectoryClient()
        result = await refresh_directory(
            db, client, county_fips="48001", refresh_days=30
        )
        assert result["skipped"] == 1
        assert client.calls == 0

        result = await refresh_directory(
            db, client, county_fips="48001", refresh_days=5
        )
        assert result["resolved"] == 1
        assert client.calls == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_gemini_key_is_sent_in_header_not_url(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("COUNTYWATCH_ROOT", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "private-test-key")
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "gemini")
    settings = Settings.load()
    db = Database(tmp_path / "state.sqlite3")
    router = LLMRouter(settings, db)
    captured = {}

    class _Response:
        def json(self):
            return {
                "candidates": [{"content": {"parts": [{"text": '{"signals": []}'}]}}],
                "usageMetadata": {},
            }

    async def fake_post(url: str, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        return _Response()

    monkeypatch.setattr(router, "_post_with_retry", fake_post)
    try:
        await router._gemini("test", "gemini-test")
        assert "private-test-key" not in captured["url"]
        assert captured["headers"]["x-goog-api-key"] == "private-test-key"
    finally:
        await router.close()
        db.close()


@pytest.mark.asyncio
async def test_nested_directory_and_discovery_errors_roll_up(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("COUNTYWATCH_ROOT", str(tmp_path))
    settings = Settings.load()
    settings.ensure_directories()
    db = Database(settings.database)
    run_id = db.start_run("discover")
    pipeline = UpdatePipeline(settings, db, run_id)

    async def fake_directory(*args, **kwargs):
        return {"checked": 1, "resolved": 0, "failed": 1, "skipped": 0}

    async def fake_discovery(*args, **kwargs):
        return {"counties": 1, "pages": 0, "sources": 0, "errors": 2, "skipped": 0}

    monkeypatch.setattr("countywatch.pipeline.refresh_directory", fake_directory)
    monkeypatch.setattr("countywatch.pipeline.discover_all", fake_discovery)
    monkeypatch.setattr("countywatch.pipeline.recompute_snapshots", lambda db: None)
    monkeypatch.setattr("countywatch.pipeline.export_site", lambda db, site: {})
    try:
        stats = await pipeline.run(max_counties=1, discover_only=True)
        assert stats["errors"] == 3
    finally:
        await pipeline.close()
        db.close()


@pytest.mark.asyncio
async def test_county_cap_is_applied_after_every_source_is_considered(tmp_path: Path, monkeypatch):
    from countywatch.models import DocumentCandidate

    monkeypatch.setenv("COUNTYWATCH_ROOT", str(tmp_path))
    settings = Settings.load()
    settings.max_documents_per_county = 2
    db = Database(settings.database)
    db.bootstrap_counties(bundled_counties())
    first_id = db.upsert_source(
        "48001", "https://example.gov/agendas", "Agendas", "agendas", "generic", 90, "test", {}
    )
    second_id = db.upsert_source(
        "48001", "https://example.gov/notices", "Notices", "public_notices", "generic", 70, "test", {}
    )
    pipeline = UpdatePipeline(settings, db, db.start_run("test", "48001"))
    source_calls: list[int] = []
    ingested: list[str] = []

    async def fake_crawl_source(county, source):
        source_calls.append(int(source["id"]))
        if int(source["id"]) == first_id:
            return [
                (source, DocumentCandidate(f"https://example.gov/a{i}.pdf", f"Agenda {i}", "agenda", date), None)
                for i, date in enumerate(("2026-06-10", "2026-06-09", "2026-06-08"), start=1)
            ]
        return [
            (
                source,
                DocumentCandidate(
                    "https://example.gov/notice.pdf", "Solar hearing notice", "public_notice", "2026-06-11"
                ),
                None,
            )
        ]

    async def fake_ingest(county, source, candidate, preload=None):
        ingested.append(candidate.url)

    monkeypatch.setattr(pipeline, "crawl_source", fake_crawl_source)
    monkeypatch.setattr(pipeline, "ingest_document", fake_ingest)
    try:
        county = db.one("SELECT * FROM counties WHERE fips='48001'")
        await pipeline.crawl_county(county)
        assert source_calls == [first_id, second_id]
        assert ingested == ["https://example.gov/notice.pdf", "https://example.gov/a1.pdf"]
        assert pipeline.stats["documents_skipped_cap"] == 2
    finally:
        await pipeline.close()
        db.close()


@pytest.mark.asyncio
async def test_http_body_limit_is_enforced_while_streaming(tmp_path: Path, monkeypatch):
    import httpx

    from countywatch.http import CrawlerClient, TooLarge

    monkeypatch.setenv("COUNTYWATCH_ROOT", str(tmp_path))
    settings = Settings.load()
    crawler = CrawlerClient(settings)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"x" * 2048, request=request)
    )
    client = httpx.AsyncClient(transport=transport)
    try:
        with pytest.raises(TooLarge):
            await crawler._request(client, "https://example.gov/large", {}, 1024)
    finally:
        await client.aclose()
        await crawler.close()


@pytest.mark.asyncio
async def test_llm_call_budget_is_atomic_across_concurrent_analyses(tmp_path: Path, monkeypatch):
    import asyncio

    from countywatch.llm import Completion, LLMError

    monkeypatch.setenv("COUNTYWATCH_ROOT", str(tmp_path))
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "groq")
    monkeypatch.setenv("COUNTYWATCH_LLM_MAX_CALLS_PER_RUN", "1")
    settings = Settings.load()
    db = Database(settings.database)
    router = LLMRouter(settings, db)

    async def fake_groq(prompt: str, model: str):
        await asyncio.sleep(0.02)
        return Completion('{"signals": []}', "groq", model)

    monkeypatch.setattr(router, "_groq", fake_groq)
    try:
        results = await asyncio.gather(
            router.complete("first", purpose="test"),
            router.complete("second", purpose="test"),
            return_exceptions=True,
        )
        assert router.calls == 1
        assert sum(isinstance(result, Completion) for result in results) == 1
        assert sum(isinstance(result, LLMError) for result in results) == 1
    finally:
        await router.close()
        db.close()


@pytest.mark.asyncio
async def test_recent_known_document_is_revalidated_after_listing_304(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("COUNTYWATCH_ROOT", str(tmp_path))
    settings = Settings.load()
    db = Database(settings.database)
    db.bootstrap_counties(bundled_counties())
    source_id = db.upsert_source(
        "48001", "https://example.gov/agendas", "Agendas", "agendas", "generic", 80, "test", {}
    )
    db.upsert_document_revision(
        county_fips="48001",
        source_id=source_id,
        url="https://example.gov/recent.pdf",
        title="Recent agenda",
        document_type="agenda",
        meeting_date=datetime.now(UTC).date().isoformat(),
        published_at=None,
        etag="etag-1",
        last_modified=None,
        mime_type="application/pdf",
        content_hash="8" * 64,
        text_hash="9" * 64,
        binary_path="var/documents/recent.pdf",
        text_path="var/text/recent.txt",
        extract_method="pdf",
        page_count=1,
        word_count=10,
        extraction_metadata={},
        warnings=[],
        metadata={},
    )
    pipeline = UpdatePipeline(settings, db, db.start_run("test", "48001"))
    source = db.sources("48001")[0]
    try:
        assert pipeline.known_document_refresh_candidates(source) == []
        stale = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        db.execute(
            "UPDATE documents SET last_seen_at=? WHERE canonical_url=?",
            (stale, "https://example.gov/recent.pdf"),
        )
        candidates = pipeline.known_document_refresh_candidates(source)
        assert [candidate.url for candidate in candidates] == [
            "https://example.gov/recent.pdf"
        ]
        assert candidates[0].metadata["known_document_refresh"] is True
    finally:
        await pipeline.close()
        db.close()
