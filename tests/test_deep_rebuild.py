from countywatch.deep_rebuild import (
    DeepModelClient,
    Excerpt,
    _budget_display,
    _parse_duration_seconds,
    _parse_retry_after,
    _sanitize_event,
)
from countywatch.scoring import combine_scores, risk_for_signal


def _row(document_type: str = "minutes") -> dict:
    return {
        "revision_id": 101,
        "document_id": 55,
        "county_fips": "48295",
        "county_name": "Lipscomb",
        "title": "Commissioners Court Minutes",
        "document_type": document_type,
        "meeting_date": "2026-05-12",
        "canonical_url": "https://example.gov/minutes.pdf",
    }


def _raw(quote: str, **overrides) -> dict:
    value = {
        "passage_id": "P1",
        "topic": "solar",
        "signal_kind": "local_regulatory_process",
        "posture": "neutral",
        "stage": "study",
        "mechanisms": ["permitting"],
        "project_name": None,
        "title": "County reviews solar permitting questions",
        "summary": "The county reviewed questions about permitting for utility-scale solar facilities.",
        "evidence_quote": quote,
        "confidence": 0.91,
        "explicit_action": False,
        "action_outcome": "pending",
        "event_key": "solar permitting inquiry",
        "authority_caveat": "The record shows an inquiry, not an adopted restriction.",
    }
    value.update(overrides)
    return value


def _excerpt(text: str) -> Excerpt:
    return Excerpt(id="P1", start=0, end=len(text), text=text, topics=["solar"], score=90, reason="test")


def test_solar_radar_speed_sign_is_rejected():
    quote = "CONSIDER INSTALLING SOLAR RADAR SPEED LIMIT SIGNS"
    assert _sanitize_event(_raw(quote), [_excerpt(quote)], _row(), index=0) is None


def test_saas_data_center_is_rejected():
    quote = "Tyler SaaS Services will be provided via a third-party data center."
    raw = _raw(
        quote,
        topic="data_center",
        title="County software hosting contract",
        summary="The county software contract references third-party hosting services.",
        signal_kind="other_material",
        mechanisms=["other"],
    )
    assert _sanitize_event(raw, [_excerpt(quote)], _row(), index=0) is None


def test_quote_must_contain_target_and_action_itself():
    text = (
        "The county discussed a utility-scale solar facility.\n\n"
        "The court directed staff to draft an ordinance."
    )
    raw = _raw(
        "The court directed staff to draft an ordinance.",
        stage="staff_direction",
        mechanisms=["ordinance"],
    )
    assert _sanitize_event(raw, [_excerpt(text)], _row(), index=0) is None


def test_solar_permitting_inquiry_is_retained_without_inventing_adoption():
    quote = (
        "Dori gave the commissioners a copy of an email for solar permitting or zoning "
        "and asked whether a moratorium was in place."
    )
    event = _sanitize_event(_raw(quote), [_excerpt(quote)], _row(), index=0)
    assert event is not None
    assert event["topic"] == "solar"
    assert event["signal_kind"] == "local_regulatory_process"
    assert event["stage"] == "study"
    assert event["action_outcome"] == "pending"


def test_tax_abatement_is_forced_to_project_facilitation():
    quote = (
        "The Commissioners Court approved the Cazadores Solar LLC tax abatement agreement "
        "pursuant to Chapter 312 of the Texas Tax Code."
    )
    raw = _raw(
        quote,
        signal_kind="local_restriction",
        posture="restrictive",
        stage="adopted",
        mechanisms=["moratorium", "tax_incentive"],
        explicit_action=True,
        action_outcome="approved",
        title="Solar tax abatement agreement approved",
        summary="The court approved a Chapter 312 tax abatement agreement for the solar project.",
    )
    event = _sanitize_event(raw, [_excerpt(quote)], _row(), index=0)
    assert event is not None
    assert event["signal_kind"] == "project_facilitation"
    assert event["posture"] == "supportive"
    assert event["mechanisms"] == ["tax_incentive"]
    risk = risk_for_signal(
        stage=event["stage"],
        mechanisms=event["mechanisms"],
        posture=event["posture"],
        confidence=event["confidence"],
        activity_date="2026-05-12",
        signal_kind=event["signal_kind"],
        action_outcome=event["action_outcome"],
    )
    assert risk <= 12


def test_semantic_kind_controls_heatmap_risk():
    kwargs = dict(
        stage="adopted",
        mechanisms=["moratorium", "ordinance"],
        posture="restrictive",
        confidence=0.95,
        activity_date="2026-06-01",
        action_outcome="adopted",
    )
    local = risk_for_signal(**kwargs, signal_kind="local_restriction")
    advocacy = risk_for_signal(**kwargs, signal_kind="state_policy_advocacy")
    monitoring = risk_for_signal(**kwargs, signal_kind="project_monitoring")
    assert local >= 85
    assert advocacy <= 48
    assert monitoring <= 20


def test_multiple_events_raise_priority_without_duplicate_explosion():
    assert combine_scores([80]) == 80
    assert 80 < combine_scores([80, 80]) < 90
    assert combine_scores([80] * 6) < 95


def test_rate_limit_duration_parser():
    assert _parse_duration_seconds("7.5s") == 7.5
    assert _parse_duration_seconds("2m30s") == 150
    assert _parse_duration_seconds("1h2m3s") == 3723


def test_retry_after_parser_supports_groq_and_google_retry_info():
    import httpx

    request = httpx.Request("POST", "https://example.test")
    groq = httpx.Response(429, request=request, headers={"retry-after": "3.795"})
    assert _parse_retry_after(groq) == 3.795

    gemini = httpx.Response(
        429,
        request=request,
        json={
            "error": {
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": "17.25s",
                    }
                ]
            }
        },
    )
    assert _parse_retry_after(gemini) == 17.25


def test_unattended_rate_limit_waits_and_retries_same_request():
    import httpx

    request = httpx.Request("POST", "https://example.test")

    class FakeHttpClient:
        def __init__(self):
            self.responses = [
                httpx.Response(
                    429,
                    request=request,
                    headers={"retry-after": "3.5"},
                    json={"error": {"message": "tokens per minute"}},
                ),
                httpx.Response(200, request=request, json={"ok": True}),
            ]

        def post(self, *args, **kwargs):
            return self.responses.pop(0)

    client = object.__new__(DeepModelClient)
    client.client = FakeHttpClient()
    client.auto_wait = True
    client.rate_limit_buffer = 5.0
    client.max_rate_limit_wait = 0.0
    client.wait_heartbeat = 300.0
    client.max_transient_retries = 12
    client.rate_limit_waits = 0
    client.total_wait_seconds = 0.0
    waits = []
    client._sleep_with_heartbeat = lambda seconds, label: waits.append((seconds, label))
    client._log = lambda *args, **kwargs: None

    result = client._post(
        "groq",
        "https://example.test",
        headers={},
        payload={},
        purpose="test",
        prompt="test",
        model="test-model",
    )
    assert result == {"ok": True}
    assert client.rate_limit_waits == 1
    assert waits[0][0] == 8.5
    assert "retrying automatically" not in waits[0][1]


def test_unlimited_budget_display():
    assert _budget_display(7, -1) == "7/∞"


def test_checkpointed_review_and_atomic_cutover_round_trip(tmp_path, monkeypatch):
    import json

    from countywatch.config import Settings
    from countywatch.db import Database
    from countywatch.deep_rebuild import (
        cutover,
        ensure_deep_schema,
        process_county_jobs,
        process_document_jobs,
        seed_county_jobs,
        seed_jobs,
    )

    monkeypatch.setenv("COUNTYWATCH_ROOT", str(tmp_path))
    settings = Settings.load()
    settings.ensure_directories()
    db = Database(settings.database)
    db.bootstrap_counties(
        [
            {
                "fips": "48015",
                "name": "Austin",
                "slug": "austin",
                "directory_url": "https://example.gov/austin",
            }
        ]
    )
    db.update_county(
        "48015",
        official_url="https://example.gov",
        site_status="resolved",
        discovery_last_run="2026-06-18T00:00:00+00:00",
    )
    source_id = db.upsert_source(
        "48015",
        "https://example.gov/meetings",
        "Commissioners Court",
        "minutes",
        "generic",
        100,
        "test",
    )
    quote = "Discussion and action regarding moratorium/order regarding BESS and Data Centers."
    document_id, revision_id, _ = db.upsert_document_revision(
        county_fips="48015",
        source_id=source_id,
        url="https://example.gov/2026-06-08-minutes.pdf",
        title="June 8, 2026 Minutes",
        document_type="minutes",
        meeting_date="2026-06-08",
        published_at=None,
        etag=None,
        last_modified=None,
        mime_type="application/pdf",
        content_hash="content-1",
        text_hash="text-1",
        binary_path=None,
        text_path="",
        extract_method="test",
        page_count=1,
        word_count=len(quote.split()),
        extraction_metadata={},
        warnings=[],
        metadata={},
    )
    db.store_revision_text(revision_id, quote)
    ensure_deep_schema(db)
    seeded = seed_jobs(settings, db)
    assert seeded["candidates"] == 1

    result = {
        "document_relevant": True,
        "rejection_reason": "",
        "events": [
            {
                "passage_id": "P1",
                "topic": "data_center",
                "signal_kind": "local_restriction",
                "posture": "restrictive",
                "stage": "public_hearing",
                "mechanisms": ["moratorium", "ordinance"],
                "project_name": None,
                "title": "County considers data-center moratorium order",
                "summary": "The commissioners court listed discussion and action on a data-center moratorium order.",
                "evidence_quote": quote,
                "confidence": 0.96,
                "explicit_action": True,
                "action_outcome": "pending",
                "event_key": "data center moratorium order",
                "authority_caveat": "The minutes item does not establish that an order was adopted.",
            }
        ],
    }

    class FakeModels:
        groq_model = "openai/gpt-oss-120b"
        gemini_model = "gemini-3.1-flash-lite"
        gemini_key = "test"
        max_groq_calls = 20
        max_gemini_calls = 20
        groq_calls = 0
        gemini_calls = 0

        def groq(self, prompt, *, schema, schema_name, purpose):
            self.groq_calls += 1
            return result

        def gemini(self, prompt, *, schema, purpose):
            self.gemini_calls += 1
            return result

    models = FakeModels()
    assert process_document_jobs(settings, db, models, deadline=None) == 1
    assert db.scalar("SELECT status FROM deep_jobs WHERE revision_id=?", (revision_id,)) == "final"
    seeded_counties = seed_county_jobs(db)
    assert seeded_counties["counties"] == 1
    assert process_county_jobs(db, models, deadline=None) == 1

    published = cutover(settings, db)
    assert published["signals"] == 1
    signal = db.one("SELECT * FROM signals WHERE status='active'")
    assert signal is not None
    assert signal["engine"] == "deep_ensemble"
    metadata = json.loads(signal["metadata_json"])
    assert metadata["signal_kind"] == "local_restriction"
    assert metadata["action_outcome"] == "pending"
    exported = json.loads((settings.site_dir / "data" / "signals.json").read_text())
    assert exported["signals"][0]["signalKind"] == "local_restriction"
    dashboard = json.loads((settings.site_dir / "data" / "dashboard.json").read_text())
    assert dashboard["counties"][0]["assessment"]
    assert list((tmp_path / "var" / "backups").glob("*.sqlite3"))
    db.close()
