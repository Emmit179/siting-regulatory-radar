from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from .db import Database
from .models import ValidatedSignal
from .utils import json_loads, parse_iso, today_iso

STAGE_WEIGHT = {
    "mention": 10,
    "study": 25,
    "staff_direction": 42,
    "drafting": 58,
    "public_notice": 64,
    "public_hearing": 70,
    "introduction": 78,
    "adopted": 94,
    "enforcement": 98,
    "rescinded": 18,
}
MECHANISM_BONUS = {
    "moratorium": 16,
    "prohibition": 14,
    "zoning": 7,
    "ordinance": 7,
    "permitting": 6,
    "setbacks": 5,
    "fire_safety": 4,
    "noise": 3,
    "water": 4,
    "roads": 3,
    "decommissioning": 3,
    "tax_incentive": -8,
    "development_agreement": -4,
    "other": 0,
}
POSTURE_FACTOR = {
    "restrictive": 1.0,
    "mixed": 0.80,
    "neutral": 0.48,
    "supportive": 0.16,
    "unknown": 0.42,
}

# A project appearing in county records is not itself regulatory risk. Deep-review
# signals therefore carry a semantic kind that determines how much of the stage /
# mechanism score is allowed to reach the heatmap.
SIGNAL_KIND_FACTOR = {
    "local_restriction": 1.00,
    "local_regulatory_process": 0.82,
    "state_policy_advocacy": 0.48,
    "public_opposition": 0.38,
    "existing_regulation": 0.42,
    "project_monitoring": 0.20,
    "project_facilitation": 0.10,
    "other_material": 0.25,
}
SIGNAL_KIND_CAP = {
    "local_restriction": 100.0,
    "local_regulatory_process": 78.0,
    "state_policy_advocacy": 48.0,
    "public_opposition": 38.0,
    "existing_regulation": 46.0,
    "project_monitoring": 20.0,
    "project_facilitation": 12.0,
    "other_material": 28.0,
}
OUTCOME_FACTOR = {
    "none": 0.86,
    "proposed": 0.96,
    "pending": 1.00,
    "approved": 1.04,
    "denied": 0.50,
    "adopted": 1.08,
    "enforced": 1.10,
    "rescinded": 0.28,
    "unknown": 0.92,
}


def _age_days(value: str | None) -> int:
    parsed = parse_iso(value)
    if parsed is None and value:
        try:
            parsed = datetime.fromisoformat(value).replace(tzinfo=UTC)
        except ValueError:
            parsed = None
    if parsed is None:
        return 0
    return max(0, (datetime.now(UTC) - parsed).days)


def risk_for_signal(
    *,
    stage: str,
    mechanisms: list[str],
    posture: str,
    confidence: float,
    activity_date: str | None,
    signal_kind: str | None = None,
    action_outcome: str | None = None,
) -> float:
    base = STAGE_WEIGHT.get(stage, 10) + sum(
        MECHANISM_BONUS.get(mechanism, 0) for mechanism in set(mechanisms)
    )
    base = max(0.0, min(100.0, float(base)))
    base *= POSTURE_FACTOR.get(posture, 0.42)
    base *= 0.64 + 0.36 * max(0.0, min(1.0, confidence))

    kind_cap: float | None = None
    if signal_kind:
        base *= SIGNAL_KIND_FACTOR.get(signal_kind, 0.25)
        kind_cap = SIGNAL_KIND_CAP.get(signal_kind, 28.0)
        base = min(base, kind_cap)
    if action_outcome:
        base *= OUTCOME_FACTOR.get(action_outcome, 0.92)
    if kind_cap is not None:
        base = min(base, kind_cap)

    age = _age_days(activity_date)
    if stage in {"adopted", "enforcement"}:
        decay = max(0.72, math.exp(-age / 1000))
    elif stage == "rescinded" or action_outcome == "rescinded":
        decay = math.exp(-age / 120)
    else:
        decay = max(0.18, math.exp(-age / 300))
    return round(max(0.0, min(100.0, base * decay)), 1)


def sentiment_for_signal(posture: str, confidence: float, stage: str) -> float:
    value = {
        "restrictive": 100,
        "mixed": 35,
        "neutral": 0,
        "supportive": -85,
        "unknown": 0,
    }.get(posture, 0)
    if stage == "rescinded":
        value = -65
    return round(value * max(0.25, confidence), 1)


def apply_signal_scores(signals: list[ValidatedSignal], activity_date: str | None) -> None:
    for signal in signals:
        signal.risk_score = risk_for_signal(
            stage=signal.stage, mechanisms=signal.mechanisms, posture=signal.posture,
            confidence=signal.confidence, activity_date=activity_date,
        )
        signal.sentiment = sentiment_for_signal(signal.posture, signal.confidence, signal.stage)


def coverage_for_county(db: Database, county: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    fips = county["fips"]
    sources = db.query("SELECT * FROM sources WHERE county_fips=? AND enabled=1", (fips,))
    documents = db.query(
        """SELECT status,meeting_date,first_seen_at,current_revision_id FROM documents
           WHERE county_fips=?""",
        (fips,),
    )
    score = 0.0
    if county.get("official_url"):
        score += 20
    if county.get("discovery_last_run"):
        score += 10
    substantive = [s for s in sources if s["source_type"] != "homepage"]
    score += min(25, len(substantive) * 5)
    recent_success = [
        s for s in sources
        if s.get("last_success") and _age_days(s.get("last_success")) <= 45
    ]
    if sources:
        score += 25 * (len(recent_success) / len(sources))
    extracted = [d for d in documents if d.get("current_revision_id")]
    if extracted:
        score += min(20, 6 + math.log2(len(extracted) + 1) * 3)
    source_failures = sum(1 for s in sources if int(s.get("failure_count") or 0) >= 3)
    score -= min(20, source_failures * 3)
    score = round(max(0, min(100, score)), 1)
    details = {
        "official_site_resolved": bool(county.get("official_url")),
        "sources": len(sources),
        "substantive_sources": len(substantive),
        "recently_successful_sources": len(recent_success),
        "documents": len(documents),
        "extracted_documents": len(extracted),
        "failing_sources": source_failures,
    }
    return score, details


def combine_scores(values: list[float]) -> float:
    values = sorted(
        (max(0.0, min(100.0, value)) for value in values if value > 0),
        reverse=True,
    )[:6]
    if not values:
        return 0.0
    strongest = values[0]
    # Additional independent, deduplicated threads raise priority, but they cannot
    # make repeated agenda/minutes references explode toward 100.
    residual = 1.0
    for value in values[1:]:
        residual *= 1.0 - (value / 100.0) * 0.24
    uplift = (100.0 - strongest) * (1.0 - residual)
    return round(min(100.0, strongest + uplift), 1)


def recompute_snapshots(db: Database) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for county in db.counties():
        rows = db.query(
            """
            SELECT s.*, d.title AS document_title, d.document_type
            FROM signals s JOIN documents d ON d.id=s.document_id
            WHERE s.county_fips=? AND s.status='active'
            ORDER BY coalesce(s.meeting_date,s.first_seen_at) DESC, s.risk_score DESC
            """,
            (county["fips"],),
        )
        # Recalculate time decay on every export, not only when the signal was created.
        for row in rows:
            mechanisms = json_loads(row.get("mechanisms_json"), [])
            metadata = json_loads(row.get("metadata_json"), {})
            row["mechanisms"] = mechanisms
            row["metadata"] = metadata
            row["risk_score"] = risk_for_signal(
                stage=row["stage"],
                mechanisms=mechanisms,
                posture=row["posture"],
                confidence=float(row["confidence"]),
                activity_date=row.get("meeting_date") or row.get("first_seen_at"),
                signal_kind=metadata.get("signal_kind"),
                action_outcome=metadata.get("action_outcome"),
            )
            row["sentiment"] = sentiment_for_signal(
                row["posture"], float(row["confidence"]), row["stage"]
            )
        by_topic = {topic: [] for topic in ("solar", "data_center", "bess", "wind", "general_land_use")}
        for row in rows:
            by_topic.setdefault(row["topic"], []).append(float(row["risk_score"]))
        topic_scores = {topic: combine_scores(values) for topic, values in by_topic.items()}
        primary = [topic_scores["solar"], topic_scores["data_center"], topic_scores["bess"], topic_scores["wind"]]
        overall = max(primary + [topic_scores["general_land_use"] * 0.30]) if rows else 0.0
        if sum(score >= 35 for score in primary) >= 2:
            overall = min(100, overall + 5)
        coverage, coverage_details = coverage_for_county(db, county)
        if coverage < 35 and not rows:
            status = "unknown"
        elif overall >= 78:
            status = "critical"
        elif overall >= 58:
            status = "high"
        elif overall >= 32:
            status = "elevated"
        elif overall > 0:
            status = "watch"
        else:
            status = "no_current_signal"
        weighted = [(float(r["sentiment"]), max(1.0, float(r["risk_score"]))) for r in rows[:20]]
        sentiment = round(sum(v * w for v, w in weighted) / sum(w for _, w in weighted), 1) if weighted else 0.0
        confidences = [float(r["confidence"]) for r in rows[:10]]
        signal_conf = sum(confidences) / len(confidences) * 100 if confidences else 0
        confidence = round(min(100, signal_conf * 0.75 + coverage * 0.25), 1) if rows else round(coverage * 0.45, 1)
        latest = max((r.get("meeting_date") or r.get("first_seen_at") or "" for r in rows), default="") or None
        record = {
            "county_fips": county["fips"],
            "snapshot_date": today_iso(),
            "overall_risk": round(overall, 1),
            "solar_risk": topic_scores["solar"],
            "data_center_risk": topic_scores["data_center"],
            "bess_risk": topic_scores["bess"],
            "wind_risk": topic_scores["wind"],
            "sentiment": sentiment,
            "confidence": confidence,
            "coverage": coverage,
            "risk_status": status,
            "active_signal_count": len(rows),
            "latest_activity": latest,
            "details": {"coverage": coverage_details, "general_land_use_risk": topic_scores["general_land_use"]},
        }
        db.update_county(county["fips"], coverage_score=coverage)
        db.save_snapshot(record)
        snapshots.append(record)
    return snapshots
