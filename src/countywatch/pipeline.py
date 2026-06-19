from __future__ import annotations

import asyncio
import gzip
import json
import time
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .analyzer import PROMPT_VERSION, analyze
from .config import Settings
from .db import Database
from .directory import refresh_directory
from .discovery import discover_all
from .exporter import export_site
from .extract import extract_document, extract_youtube, youtube_video_id
from .http import CrawlerClient
from .llm import LLMRouter
from .models import DocumentCandidate, FetchResult
from .platforms import infer_meeting_date, legistar_candidates, parse_listing
from .prefilter import extract_passages
from .scoring import apply_signal_scores, recompute_snapshots
from .utils import (
    canonical_url,
    extension_for,
    sha256_bytes,
    parse_iso,
    sha256_text,
    stable_id,
    utcnow,
)


def bundled_counties() -> list[dict[str, str]]:
    path = files("countywatch.data").joinpath("counties.json")
    return json.loads(path.read_text(encoding="utf-8"))


def bootstrap(settings: Settings, db: Database | None = None) -> dict[str, int]:
    settings.ensure_directories()
    own = db is None
    db = db or Database(settings.database)
    try:
        count = db.bootstrap_counties(bundled_counties())
        recompute_snapshots(db)
        exported = export_site(db, settings.site_dir)
        return {"counties": count, **exported}
    finally:
        if own:
            db.close()


def _candidate_recent(candidate: DocumentCandidate, lookback_days: int) -> bool:
    if not candidate.meeting_date:
        return True
    try:
        return date.fromisoformat(candidate.meeting_date) >= date.today() - timedelta(
            days=lookback_days
        )
    except ValueError:
        return True


def _candidate_sort_key(candidate: DocumentCandidate) -> tuple[bool, str, int, str]:
    type_rank = {
        "agenda": 0,
        "packet": 1,
        "minutes": 2,
        "public_notice": 3,
        "ordinance": 4,
        "meeting_page": 5,
    }
    return (
        candidate.meeting_date is not None,
        candidate.meeting_date or "",
        -type_rank.get(candidate.document_type, 8),
        candidate.title,
    )


def _sort_candidates(candidates: list[DocumentCandidate]) -> list[DocumentCandidate]:
    return sorted(candidates, key=_candidate_sort_key, reverse=True)


class UpdatePipeline:
    def __init__(self, settings: Settings, db: Database, run_id: int):
        self.settings = settings
        self.db = db
        self.run_id = run_id
        self.stats: dict[str, Any] = {
            "directory": {},
            "discovery": {},
            "counties_checked": 0,
            "counties_deferred": 0,
            "counties_partial": 0,
            "sources_checked": 0,
            "documents_seen": 0,
            "revisions_created": 0,
            "analyses_created": 0,
            "analyses_upgraded": 0,
            "signals_created": 0,
            "documents_skipped_old": 0,
            "documents_skipped_cap": 0,
            "documents_not_modified": 0,
            "errors": 0,
            "error_samples": [],
        }
        self.client = CrawlerClient(settings)
        self.router = LLMRouter(settings, db, run_id)
        self._analysis_sem = asyncio.Semaphore(max(1, min(3, settings.concurrency)))
        self._deadline = (
            time.monotonic() + settings.max_run_minutes * 60
            if settings.max_run_minutes > 0
            else None
        )

    async def close(self) -> None:
        await self.router.close()
        await self.client.close()

    def _error(self, context: str, exc: Exception) -> None:
        self.stats["errors"] += 1
        if len(self.stats["error_samples"]) < 30:
            self.stats["error_samples"].append(f"{context}: {exc}")

    def deadline_reached(self, reserve_seconds: float = 0) -> bool:
        return self._deadline is not None and time.monotonic() >= self._deadline - reserve_seconds

    async def analyze_revision(
        self,
        *,
        county: dict[str, Any],
        document_id: int,
        revision_id: int,
        title: str,
        document_type: str,
        meeting_date: str | None,
        source_url: str,
        text: str,
        passages: list[Any] | None = None,
        upgrade: bool = False,
    ) -> str:
        passages = (
            passages if passages is not None else await asyncio.to_thread(extract_passages, text)
        )
        if passages:
            # Only potentially relevant records are embedded in durable cloud state.
            # This lets later free-tier runs upgrade rule results without redownloading files.
            self.db.store_revision_text(revision_id, text)
        async with self._analysis_sem:
            signals, raw, engine, provider, model = await analyze(
                self.settings,
                self.router,
                full_text=text,
                passages=passages,
                county_name=county["name"],
                title=title,
                document_type=document_type,
                meeting_date=meeting_date,
                source_url=source_url,
            )
        apply_signal_scores(signals, meeting_date)
        analysis_id = self.db.save_analysis(
            revision_id=revision_id,
            prompt_version=PROMPT_VERSION,
            engine=engine,
            provider=provider,
            model=model,
            status="ok",
            passage_count=len(passages),
            raw={
                "result": raw,
                "passages": [
                    {
                        "id": passage.id,
                        "start": passage.start,
                        "end": passage.end,
                        "topics": passage.topics,
                        "score": passage.score,
                        "matched_terms": passage.matched_terms,
                    }
                    for passage in passages
                ],
            },
        )
        signal_records = []
        for signal in signals:
            record = asdict(signal)
            record["id"] = stable_id(
                county["fips"],
                document_id,
                revision_id,
                signal.topic,
                signal.stage,
                signal.evidence_start,
                signal.evidence_quote,
            )
            signal_records.append(record)
        count = self.db.replace_signals(
            analysis_id=analysis_id,
            document_id=document_id,
            revision_id=revision_id,
            county_fips=county["fips"],
            meeting_date=meeting_date,
            source_url=source_url,
            signals=signal_records,
        )
        if upgrade:
            if engine == "llm":
                self.stats["analyses_upgraded"] += 1
        else:
            self.stats["analyses_created"] += 1
        self.stats["signals_created"] += count
        return engine

    async def upgrade_rule_backlog(self, county_fips: str | None = None) -> int:
        """Use today's free-model capacity to upgrade previously grounded rule results."""
        if not self.router.available():
            return 0
        limit = max(1, self.settings.llm_max_calls * 2)
        upgraded = 0
        for row in self.db.rule_analysis_backlog(PROMPT_VERSION, county_fips, limit):
            if not self.router.available():
                break
            text = self.db.revision_text(int(row["revision_id"]))
            if not text:
                continue
            before = self.router.calls
            try:
                engine = await self.analyze_revision(
                    county={"fips": row["county_fips"], "name": row["county_name"]},
                    document_id=int(row["document_id"]),
                    revision_id=int(row["revision_id"]),
                    title=row["title"],
                    document_type=row["document_type"],
                    meeting_date=row.get("meeting_date"),
                    source_url=row["canonical_url"],
                    text=text,
                    upgrade=True,
                )
                if engine == "llm":
                    upgraded += 1
            except Exception as exc:
                self._error(f"analysis backlog / {row['canonical_url']}", exc)
            if self.router.calls == before:
                break
        return upgraded

    async def ingest_document(
        self,
        county: dict[str, Any],
        source: dict[str, Any],
        candidate: DocumentCandidate,
        preloaded: FetchResult | None = None,
    ) -> None:
        url = canonical_url(candidate.url)
        if not url:
            return
        self.stats["documents_seen"] += 1
        existing = self.db.document_by_url(url)
        try:
            if youtube_video_id(url):
                extraction = await asyncio.to_thread(extract_youtube, url, self.settings)
                data = extraction.text.encode("utf-8")
                content_hash = sha256_bytes(data)
                mime_type = "text/plain"
                result_headers: dict[str, str] = {}
                binary_path: str | None = None
            else:
                if preloaded is not None:
                    result = preloaded
                else:
                    result = await self.client.fetch(
                        url,
                        etag=existing.get("etag") if existing else None,
                        last_modified=existing.get("last_modified") if existing else None,
                        allow_browser=candidate.requires_browser
                        or candidate.document_type == "meeting_page",
                    )
                if result.not_modified:
                    if existing:
                        self.db.touch_document_not_modified(int(existing["id"]))
                    self.stats["documents_not_modified"] += 1
                    return
                data = result.content
                content_hash = sha256_bytes(data)
                mime_type = result.content_type or "application/octet-stream"
                result_headers = result.headers
                extraction = await asyncio.to_thread(
                    extract_document, data, mime_type, url, self.settings
                )
                ext = extension_for(url, mime_type)
                binary = self.settings.document_dir / county["fips"] / f"{content_hash}{ext}"
                binary.parent.mkdir(parents=True, exist_ok=True)
                if not binary.exists():
                    binary.write_bytes(data)
                try:
                    binary_path = str(binary.relative_to(self.settings.root))
                except ValueError:
                    binary_path = str(binary)

            text = extraction.text.strip()
            text_hash = sha256_text(text)
            text_path_obj = self.settings.text_dir / f"{text_hash}.txt"
            if not text_path_obj.exists():
                text_path_obj.write_text(text, encoding="utf-8")
            try:
                text_path = str(text_path_obj.relative_to(self.settings.root))
            except ValueError:
                text_path = str(text_path_obj)
            meeting_date = candidate.meeting_date or infer_meeting_date(candidate.title)
            if not meeting_date and text:
                meeting_date = infer_meeting_date(text[:5000])
            resolved_title = (
                candidate.title
                or extraction.metadata.get("page_title")
                or urlsplit(url).path.rsplit("/", 1)[-1]
                or "County meeting record"
            )
            document_id, revision_id, created = self.db.upsert_document_revision(
                county_fips=county["fips"],
                source_id=int(source["id"]),
                url=url,
                title=resolved_title,
                document_type=candidate.document_type,
                meeting_date=meeting_date,
                published_at=candidate.published_at,
                etag=result_headers.get("etag"),
                last_modified=result_headers.get("last-modified"),
                mime_type=mime_type,
                content_hash=content_hash,
                text_hash=text_hash,
                binary_path=binary_path,
                text_path=text_path,
                extract_method=extraction.method,
                page_count=extraction.page_count,
                word_count=len(text.split()),
                extraction_metadata=extraction.metadata,
                warnings=extraction.warnings,
                metadata={
                    **candidate.metadata,
                    "parent_url": candidate.parent_url,
                    "platform": candidate.platform,
                },
            )
            if created:
                self.stats["revisions_created"] += 1
            cached = self.db.analysis(revision_id, PROMPT_VERSION)
            # Rule-only results remain immediately useful but can be upgraded by an LLM later.
            if cached and not (cached.get("engine") == "rules" and self.router.available()):
                return
            await self.analyze_revision(
                county=county,
                document_id=document_id,
                revision_id=revision_id,
                title=resolved_title,
                document_type=candidate.document_type,
                meeting_date=meeting_date,
                source_url=url,
                text=text,
                upgrade=bool(cached),
            )
        except Exception as exc:
            self.db.mark_document_failure(
                county_fips=county["fips"],
                source_id=int(source["id"]),
                url=url,
                title=candidate.title,
                document_type=candidate.document_type,
                error=str(exc),
            )
            self._error(f"{county['name']} / {url}", exc)

    def known_document_refresh_candidates(
        self, source: dict[str, Any]
    ) -> list[DocumentCandidate]:
        """Periodically revalidate recent linked files even when a listing is unchanged."""
        rows = self.db.query(
            """
            SELECT canonical_url,title,document_type,meeting_date,published_at,
                   first_seen_at,last_seen_at,status
            FROM documents
            WHERE source_id=?
            ORDER BY coalesce(meeting_date,first_seen_at) DESC
            LIMIT 60
            """,
            (int(source["id"]),),
        )
        now = datetime.now(UTC)
        lookback = min(self.settings.initial_lookback_days, 365)
        candidates: list[DocumentCandidate] = []
        for row in rows:
            reference: datetime | None = None
            if row.get("meeting_date"):
                try:
                    reference = datetime.combine(
                        date.fromisoformat(row["meeting_date"]),
                        datetime.min.time(),
                        tzinfo=UTC,
                    )
                except ValueError:
                    reference = None
            reference = reference or parse_iso(row.get("first_seen_at"))
            age_days = max(0, (now - reference).days) if reference else 0
            if age_days > lookback and row.get("status") != "error":
                continue
            if row.get("status") == "error" or age_days <= 45:
                refresh_after = timedelta(hours=20)
            elif age_days <= 180:
                refresh_after = timedelta(days=6)
            else:
                refresh_after = timedelta(days=29)
            last_seen = parse_iso(row.get("last_seen_at"))
            if last_seen and now - last_seen < refresh_after:
                continue
            candidates.append(
                DocumentCandidate(
                    url=row["canonical_url"],
                    title=row["title"],
                    document_type=row["document_type"],
                    meeting_date=row.get("meeting_date"),
                    published_at=row.get("published_at"),
                    parent_url=source["url"],
                    platform=source["platform"],
                    requires_browser=row["document_type"] == "meeting_page",
                    metadata={"known_document_refresh": True},
                )
            )
            if len(candidates) >= min(20, self.settings.max_documents_per_source):
                break
        return candidates

    async def crawl_source(
        self,
        county: dict[str, Any],
        source: dict[str, Any],
    ) -> list[tuple[dict[str, Any], DocumentCandidate, FetchResult | None]]:
        """Collect a bounded recent candidate set while recording source health."""
        self.stats["sources_checked"] += 1
        candidates: list[DocumentCandidate] = []
        details: list[DocumentCandidate] = []
        source_result: FetchResult | None = None
        try:
            if source["platform"] == "legistar":
                candidates = await legistar_candidates(
                    self.client, source["url"], self.settings.initial_lookback_days
                )
                self.db.mark_source_success(int(source["id"]))
            else:
                source_result = await self.client.fetch(
                    source["url"],
                    etag=source.get("etag"),
                    last_modified=source.get("last_modified"),
                    allow_browser=source["platform"]
                    in {"civicclerk", "primegov", "civicweb", "boarddocs", "iqm2", "swagit"},
                    max_bytes=12_000_000,
                )
                self.db.mark_source_success(int(source["id"]), source_result.headers)
                if source_result.not_modified:
                    candidates = self.known_document_refresh_candidates(source)
                elif source["source_type"] == "document_feed" or source_result.content_type not in {
                    "text/html",
                    "application/xhtml+xml",
                    "",
                }:
                    candidates = [
                        DocumentCandidate(
                            url=source["url"],
                            title=source["title"],
                            document_type="meeting_document",
                            platform=source["platform"],
                            metadata={"direct_source": True},
                        )
                    ]
                else:
                    candidates, details = parse_listing(
                        source_result.content, source_result.final_url, source["platform"]
                    )
                    candidates.extend(self.known_document_refresh_candidates(source))
        except Exception as exc:
            self.db.mark_source_failure(int(source["id"]), str(exc))
            self._error(f"{county['name']} source {source['url']}", exc)
            return []

        # Follow recent meeting/detail pages to reach attachments hidden one level down.
        detail_limit = min(45, self.settings.max_documents_per_source)
        for detail in _sort_candidates(details)[:detail_limit]:
            if self.deadline_reached(30):
                break
            if not _candidate_recent(detail, self.settings.initial_lookback_days):
                continue
            try:
                result = await self.client.fetch(
                    detail.url,
                    etag=None,
                    last_modified=None,
                    allow_browser=detail.requires_browser,
                    max_bytes=12_000_000,
                )
                nested, _ = parse_listing(result.content, result.final_url, detail.platform)
                for child in nested:
                    if not child.meeting_date:
                        child.meeting_date = detail.meeting_date
                    child.metadata["meeting_page"] = detail.url
                    candidates.append(child)
                if detail.metadata.get("inline_document") or not nested:
                    candidates.append(detail)
            except Exception as exc:
                self._error(f"{county['name']} detail {detail.url}", exc)

        collected: list[tuple[dict[str, Any], DocumentCandidate, FetchResult | None]] = []
        seen_urls: set[str] = set()
        for candidate in _sort_candidates(candidates):
            url = canonical_url(candidate.url)
            if not url or url in seen_urls:
                continue
            if not _candidate_recent(candidate, self.settings.initial_lookback_days):
                self.stats["documents_skipped_old"] += 1
                continue
            seen_urls.add(url)
            preload = (
                source_result
                if source_result and url == canonical_url(source["url"])
                else None
            )
            collected.append((source, candidate, preload))
            if len(collected) >= self.settings.max_documents_per_source:
                break
        return collected

    async def crawl_county(self, county: dict[str, Any]) -> None:
        sources = self.db.sources(county["fips"])
        collected: list[tuple[dict[str, Any], DocumentCandidate, FetchResult | None]] = []
        for source in sources:
            if self.deadline_reached(30):
                break
            collected.extend(await self.crawl_source(county, source))

        # Rank all sources together so one large agenda archive cannot starve minutes,
        # notices, ordinances, or a second meeting platform from being considered.
        collected.sort(
            key=lambda item: (
                *_candidate_sort_key(item[1])[:3],
                int(item[0].get("priority") or 0),
                item[1].title,
            ),
            reverse=True,
        )
        ranked_unique: list[
            tuple[dict[str, Any], DocumentCandidate, FetchResult | None]
        ] = []
        seen_urls: set[str] = set()
        for entry in collected:
            url = canonical_url(entry[1].url)
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            ranked_unique.append(entry)
        selected = ranked_unique[: self.settings.max_documents_per_county]
        self.stats["documents_skipped_cap"] += max(0, len(ranked_unique) - len(selected))

        for source, candidate, preload in selected:
            if self.deadline_reached(30):
                break
            await self.ingest_document(county, source, candidate, preload)

        if self.deadline_reached(30):
            self.stats["counties_partial"] += 1
        else:
            self.db.update_county(county["fips"], last_crawl_at=utcnow())
        self.stats["counties_checked"] += 1

    async def run(
        self,
        *,
        county_fips: str | None = None,
        max_counties: int = 0,
        force_directory: bool = False,
        force_discovery: bool = False,
        discover_only: bool = False,
    ) -> dict[str, Any]:
        self.db.bootstrap_counties(bundled_counties())
        self.stats["directory"] = await refresh_directory(
            self.db,
            self.client,
            force=force_directory,
            county_fips=county_fips,
            limit=max_counties,
            refresh_days=self.settings.directory_refresh_days,
        )
        directory_failed = int(self.stats["directory"].get("failed", 0))
        self.stats["errors"] += directory_failed
        if directory_failed and len(self.stats["error_samples"]) < 30:
            self.stats["error_samples"].append(
                f"Official-site directory resolution failed for {directory_failed} county/counties"
            )
        self.stats["discovery"] = await discover_all(
            self.db,
            self.client,
            county_fips=county_fips,
            force=force_discovery,
            limit=max_counties,
            refresh_days=self.settings.discovery_refresh_days,
        )
        discovery_errors = int(self.stats["discovery"].get("errors", 0))
        self.stats["errors"] += discovery_errors
        if discovery_errors and len(self.stats["error_samples"]) < 30:
            self.stats["error_samples"].append(
                f"Source discovery encountered {discovery_errors} non-speculative page failure(s)"
            )
        if not discover_only:
            self.stats["backlog_upgraded_before_crawl"] = await self.upgrade_rule_backlog(
                county_fips
            )
            counties = self.db.counties_for_crawl(county_fips, max_counties)
            # Never-crawled/stalest counties run first. The optional time budget lets
            # cloud jobs finish cleanly and persist progress instead of being hard-killed.
            worker_sem = asyncio.Semaphore(self.settings.concurrency)

            async def worker(county: dict[str, Any]) -> None:
                async with worker_sem:
                    if self.deadline_reached(30):
                        self.stats["counties_deferred"] += 1
                        return
                    await self.crawl_county(county)

            await asyncio.gather(*(worker(county) for county in counties))
            self.stats["backlog_upgraded_after_crawl"] = await self.upgrade_rule_backlog(
                county_fips
            )
        recompute_snapshots(self.db)
        self.stats["export"] = export_site(self.db, self.settings.site_dir)
        self.stats["llm_calls"] = self.router.calls
        return self.stats


async def run_update(
    settings: Settings,
    *,
    county_fips: str | None = None,
    max_counties: int = 0,
    force_directory: bool = False,
    force_discovery: bool = False,
    discover_only: bool = False,
) -> dict[str, Any]:
    settings.ensure_directories()
    db = Database(settings.database)
    run_id = db.start_run("discover" if discover_only else "update", county_fips)
    pipeline = UpdatePipeline(settings, db, run_id)
    try:
        stats = await pipeline.run(
            county_fips=county_fips,
            max_counties=max_counties,
            force_directory=force_directory,
            force_discovery=force_discovery,
            discover_only=discover_only,
        )
        incomplete = (
            stats["errors"] > 0
            or stats.get("counties_deferred", 0) > 0
            or stats.get("counties_partial", 0) > 0
        )
        db.finish_run(run_id, "partial" if incomplete else "success", stats)
        return stats
    except Exception as exc:
        pipeline._error("fatal", exc)
        db.finish_run(run_id, "failed", pipeline.stats, str(exc))
        raise
    finally:
        await pipeline.close()
        db.close()


def export_state_archive(settings: Settings, output: Path) -> Path:
    """Create a compressed SQLite state artifact for GitHub Actions persistence."""
    output.parent.mkdir(parents=True, exist_ok=True)
    db = Database(settings.database)
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()
    with settings.database.open("rb") as source, gzip.open(output, "wb", compresslevel=9) as target:
        while chunk := source.read(1024 * 1024):
            target.write(chunk)
    return output


def import_state_archive(settings: Settings, archive: Path) -> None:
    if not archive.exists():
        return
    settings.database.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(archive, "rb") as source, settings.database.open("wb") as target:
        while chunk := source.read(1024 * 1024):
            target.write(chunk)
