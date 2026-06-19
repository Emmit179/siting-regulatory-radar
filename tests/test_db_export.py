import json

from countywatch.db import Database
from countywatch.exporter import export_site
from countywatch.pipeline import bundled_counties
from countywatch.scoring import recompute_snapshots


def test_revision_dedup_and_full_county_export(tmp_path):
    db = Database(tmp_path / "state.sqlite3")
    try:
        assert db.bootstrap_counties(bundled_counties()) == 254
        source_id = db.upsert_source(
            "48001",
            "https://example.gov/agendas",
            "Agendas",
            "agendas",
            "generic",
            50,
            "test",
            {},
        )
        args = dict(
            county_fips="48001",
            source_id=source_id,
            url="https://example.gov/a.pdf",
            title="Agenda",
            document_type="agenda",
            meeting_date="2026-06-01",
            published_at=None,
            etag="x",
            last_modified=None,
            mime_type="application/pdf",
            content_hash="a" * 64,
            text_hash="b" * 64,
            binary_path="var/documents/a.pdf",
            text_path="var/text/b.txt",
            extract_method="pdf",
            page_count=1,
            word_count=10,
            extraction_metadata={},
            warnings=[],
            metadata={},
        )
        document_id, revision_id, created = db.upsert_document_revision(**args)
        assert created is True
        document_id2, revision_id2, created2 = db.upsert_document_revision(**args)
        assert (document_id2, revision_id2, created2) == (document_id, revision_id, False)
        recompute_snapshots(db)
        site = tmp_path / "site"
        result = export_site(db, site)
        assert result["counties"] == 254
        payload = json.loads((site / "data" / "dashboard.json").read_text())
        assert len(payload["counties"]) == 254
        anderson = next(c for c in payload["counties"] if c["fips"] == "48001")
        assert anderson["status"] == "unknown"
    finally:
        db.close()


def test_relevant_text_is_compressed_and_available_for_rule_backlog(tmp_path):
    db = Database(tmp_path / "state.sqlite3")
    try:
        db.bootstrap_counties(bundled_counties())
        source_id = db.upsert_source(
            "48001",
            "https://example.gov/meetings",
            "Meetings",
            "meetings",
            "generic",
            50,
            "test",
            {},
        )
        document_id, revision_id, _ = db.upsert_document_revision(
            county_fips="48001",
            source_id=source_id,
            url="https://example.gov/meeting-1",
            title="Commissioners Court Agenda",
            document_type="agenda",
            meeting_date="2026-06-01",
            published_at=None,
            etag=None,
            last_modified=None,
            mime_type="text/html",
            content_hash="c" * 64,
            text_hash="d" * 64,
            binary_path=None,
            text_path="var/text/d.txt",
            extract_method="html",
            page_count=1,
            word_count=12,
            extraction_metadata={},
            warnings=[],
            metadata={},
        )
        text = "The Commissioners Court directed counsel to draft a solar moratorium ordinance."
        db.store_revision_text(revision_id, text)
        db.save_analysis(
            revision_id=revision_id,
            prompt_version="test-prompt",
            engine="rules",
            provider="local",
            model="rules-v1",
            status="ok",
            passage_count=1,
            raw={},
        )
        assert db.revision_text(revision_id) == text
        backlog = db.rule_analysis_backlog("test-prompt", "48001")
        assert len(backlog) == 1
        assert backlog[0]["document_id"] == document_id
        assert backlog[0]["county_name"] == "Anderson"
    finally:
        db.close()


def test_new_document_revision_supersedes_old_signals(tmp_path):
    db = Database(tmp_path / "state.sqlite3")
    try:
        db.bootstrap_counties(bundled_counties())
        source_id = db.upsert_source(
            "48001", "https://example.gov/meetings", "Meetings", "meetings", "generic", 50, "test", {}
        )
        base = dict(
            county_fips="48001",
            source_id=source_id,
            url="https://example.gov/agenda.pdf",
            title="Agenda",
            document_type="agenda",
            meeting_date="2026-06-01",
            published_at=None,
            etag=None,
            last_modified=None,
            mime_type="application/pdf",
            binary_path="var/documents/a.pdf",
            text_path="var/text/a.txt",
            extract_method="pdf",
            page_count=1,
            word_count=20,
            extraction_metadata={},
            warnings=[],
            metadata={},
        )
        document_id, first_revision, _ = db.upsert_document_revision(
            **base, content_hash="1" * 64, text_hash="2" * 64
        )
        first_analysis = db.save_analysis(
            revision_id=first_revision,
            prompt_version="test",
            engine="rules",
            provider="local",
            model="rules-v1",
            status="ok",
            passage_count=1,
            raw={},
        )
        signal = {
            "id": "old-signal",
            "topic": "solar",
            "posture": "restrictive",
            "stage": "drafting",
            "mechanisms": ["moratorium"],
            "title": "Old signal",
            "summary": "Old summary",
            "evidence_quote": "draft a solar moratorium",
            "evidence_start": 0,
            "evidence_end": 24,
            "passage_id": "p1",
            "risk_score": 60.0,
            "sentiment": 60.0,
            "confidence": 0.7,
            "explicit_action": True,
            "authority_caveat": None,
            "engine": "rules",
            "provider": "local",
            "model": "rules-v1",
        }
        db.replace_signals(
            analysis_id=first_analysis,
            document_id=document_id,
            revision_id=first_revision,
            county_fips="48001",
            meeting_date="2026-06-01",
            source_url=base["url"],
            signals=[signal],
        )

        document_id_2, second_revision, _ = db.upsert_document_revision(
            **base, content_hash="3" * 64, text_hash="4" * 64
        )
        assert document_id_2 == document_id
        second_analysis = db.save_analysis(
            revision_id=second_revision,
            prompt_version="test",
            engine="rules",
            provider="local",
            model="rules-v1",
            status="ok",
            passage_count=0,
            raw={},
        )
        db.replace_signals(
            analysis_id=second_analysis,
            document_id=document_id,
            revision_id=second_revision,
            county_fips="48001",
            meeting_date="2026-06-01",
            source_url=base["url"],
            signals=[],
        )
        assert db.scalar("SELECT count(*) FROM signals WHERE status='active'", default=0) == 0
        assert db.scalar(
            "SELECT count(*) FROM signals WHERE status='superseded'", default=0
        ) == 1
    finally:
        db.close()


def test_signal_export_recalculates_decay_and_neutralizes_csv_formulas(tmp_path):
    import csv

    from countywatch.exporter import _write_csv
    from countywatch.scoring import risk_for_signal

    csv_path = tmp_path / "safe.csv"
    _write_csv(
        csv_path,
        [{"title": "=HYPERLINK(\"https://evil.invalid\")", "note": "ordinary"}],
        ["title", "note"],
    )
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        exported = next(csv.DictReader(handle))
    assert exported["title"].startswith("'=")
    assert exported["note"] == "ordinary"

    db = Database(tmp_path / "decay.sqlite3")
    try:
        db.bootstrap_counties(bundled_counties())
        source_id = db.upsert_source(
            "48001", "https://example.gov/meetings", "Meetings", "meetings", "generic", 50, "test", {}
        )
        document_id, revision_id, _ = db.upsert_document_revision(
            county_fips="48001",
            source_id=source_id,
            url="https://example.gov/old-agenda.pdf",
            title="Old agenda",
            document_type="agenda",
            meeting_date="2020-01-01",
            published_at=None,
            etag=None,
            last_modified=None,
            mime_type="application/pdf",
            content_hash="e" * 64,
            text_hash="f" * 64,
            binary_path="var/documents/old.pdf",
            text_path="var/text/old.txt",
            extract_method="pdf",
            page_count=1,
            word_count=20,
            extraction_metadata={},
            warnings=[],
            metadata={},
        )
        analysis_id = db.save_analysis(
            revision_id=revision_id,
            prompt_version="test",
            engine="rules",
            provider="local",
            model="rules-v1",
            status="ok",
            passage_count=1,
            raw={},
        )
        db.replace_signals(
            analysis_id=analysis_id,
            document_id=document_id,
            revision_id=revision_id,
            county_fips="48001",
            meeting_date="2020-01-01",
            source_url="https://example.gov/old-agenda.pdf",
            signals=[{
                "id": "decayed-signal",
                "topic": "solar",
                "posture": "restrictive",
                "stage": "drafting",
                "mechanisms": ["moratorium"],
                "title": "Old drafting signal",
                "summary": "Old summary",
                "evidence_quote": "draft a solar moratorium",
                "evidence_start": 0,
                "evidence_end": 24,
                "passage_id": "p1",
                "risk_score": 99.0,
                "sentiment": 99.0,
                "confidence": 1.0,
                "explicit_action": True,
                "authority_caveat": None,
                "engine": "rules",
                "provider": "local",
                "model": "rules-v1",
            }],
        )
        recompute_snapshots(db)
        site = tmp_path / "decay-site"
        export_site(db, site)
        payload = json.loads((site / "data" / "signals.json").read_text())
        signal = next(item for item in payload["signals"] if item["id"] == "decayed-signal")
        expected = risk_for_signal(
            stage="drafting",
            mechanisms=["moratorium"],
            posture="restrictive",
            confidence=1.0,
            activity_date="2020-01-01",
        )
        assert signal["risk"] == expected
        assert signal["risk"] < 99.0
    finally:
        db.close()
