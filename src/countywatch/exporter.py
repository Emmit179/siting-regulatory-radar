from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Any

from .db import Database
from .scoring import risk_for_signal, sentiment_for_signal
from .utils import json_dumps, json_loads, utcnow


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json_dumps(value), encoding="utf-8")
    temp.replace(path)


def _safe_csv_cell(value: Any) -> Any:
    """Prevent spreadsheet formula execution when a CSV is opened interactively."""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(
            {field: _safe_csv_cell(row.get(field)) for field in fields}
            for row in rows
        )


def _table_exists(db: Database, table: str) -> bool:
    return bool(
        db.scalar(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
            default=0,
        )
    )


def export_site(db: Database, site_dir: Path) -> dict[str, int]:
    data_dir = site_dir / "data"
    exports_dir = site_dir / "exports"
    data_dir.mkdir(parents=True, exist_ok=True)
    exports_dir.mkdir(parents=True, exist_ok=True)

    latest_snapshot_date = db.scalar("SELECT max(snapshot_date) FROM county_snapshots")
    snapshots = {
        row["county_fips"]: row
        for row in db.query("SELECT * FROM county_snapshots WHERE snapshot_date=?", (latest_snapshot_date,))
    } if latest_snapshot_date else {}

    sources_by_county: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in db.query("SELECT * FROM sources WHERE enabled=1 ORDER BY priority DESC,id"):
        sources_by_county[row["county_fips"]].append({
            "title": row["title"],
            "url": row["url"],
            "type": row["source_type"],
            "platform": row["platform"],
            "lastSuccess": row["last_success"],
            "lastChecked": row["last_checked"],
            "failureCount": row["failure_count"],
            "lastError": row["last_error"],
        })

    doc_stats = {
        row["county_fips"]: row
        for row in db.query(
            """SELECT county_fips,count(*) AS documents,
               sum(CASE WHEN current_revision_id IS NOT NULL THEN 1 ELSE 0 END) AS extracted,
               sum(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,
               max(coalesce(meeting_date,first_seen_at)) AS latest_document
               FROM documents GROUP BY county_fips"""
        )
    }

    deep_county_results: dict[str, dict[str, Any]] = {}
    deep_cutover_at: str | None = None
    deep_version: str | None = None
    if _table_exists(db, "deep_county_jobs"):
        for row in db.query(
            "SELECT county_fips,result_json FROM deep_county_jobs WHERE status='final'"
        ):
            deep_county_results[str(row["county_fips"])] = json_loads(
                row.get("result_json"), {}
            )
    if _table_exists(db, "deep_meta"):
        deep_cutover_at = db.scalar(
            "SELECT value FROM deep_meta WHERE key='last_cutover_at'",
            default=None,
        )
        deep_version = db.scalar(
            "SELECT value FROM deep_meta WHERE key='methodology_version'",
            default=None,
        )

    histories: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in db.query(
        """SELECT county_fips,snapshot_date,overall_risk,solar_risk,data_center_risk,bess_risk,
           wind_risk,sentiment,coverage FROM county_snapshots
           WHERE snapshot_date >= date('now','-180 days') ORDER BY snapshot_date"""
    ):
        histories[row["county_fips"]].append({
            "date": row["snapshot_date"],
            "risk": row["overall_risk"],
            "solar": row["solar_risk"],
            "dataCenter": row["data_center_risk"],
            "bess": row["bess_risk"],
            "wind": row["wind_risk"],
            "sentiment": row["sentiment"],
            "coverage": row["coverage"],
        })

    counties: list[dict[str, Any]] = []
    csv_counties: list[dict[str, Any]] = []
    for county in db.counties():
        snapshot = snapshots.get(county["fips"], {})
        docs = doc_stats.get(county["fips"], {})
        history = histories.get(county["fips"], [])
        previous = history[-2]["risk"] if len(history) > 1 else snapshot.get("overall_risk", 0)
        record = {
            "fips": county["fips"],
            "name": county["name"],
            "seat": county.get("seat"),
            "officialUrl": county.get("official_url"),
            "directoryUrl": county.get("directory_url"),
            "siteStatus": county.get("site_status"),
            "risk": snapshot.get("overall_risk", 0),
            "solar": snapshot.get("solar_risk", 0),
            "dataCenter": snapshot.get("data_center_risk", 0),
            "bess": snapshot.get("bess_risk", 0),
            "wind": snapshot.get("wind_risk", 0),
            "sentiment": snapshot.get("sentiment", 0),
            "confidence": snapshot.get("confidence", 0),
            "coverage": snapshot.get("coverage", county.get("coverage_score", 0)),
            "status": snapshot.get("risk_status", "unknown"),
            "signalCount": snapshot.get("active_signal_count", 0),
            "latestActivity": snapshot.get("latest_activity"),
            "documents": docs.get("documents", 0) or 0,
            "extractedDocuments": docs.get("extracted", 0) or 0,
            "documentErrors": docs.get("errors", 0) or 0,
            "latestDocument": docs.get("latest_document"),
            "sourceCount": len(sources_by_county.get(county["fips"], [])),
            "sourceFailures": sum(1 for s in sources_by_county.get(county["fips"], []) if s["failureCount"] >= 3),
            "dailyChange": round(float(snapshot.get("overall_risk", 0)) - float(previous or 0), 1),
            "assessment": str(
                deep_county_results.get(county["fips"], {}).get("assessment") or ""
            ),
        }
        counties.append(record)
        csv_counties.append({
            "county_fips": record["fips"],
            "county": record["name"],
            "risk_status": record["status"],
            "overall_risk": record["risk"],
            "solar_risk": record["solar"],
            "data_center_risk": record["dataCenter"],
            "bess_risk": record["bess"],
            "wind_risk": record["wind"],
            "sentiment": record["sentiment"],
            "confidence": record["confidence"],
            "coverage": record["coverage"],
            "active_signals": record["signalCount"],
            "latest_activity": record["latestActivity"],
            "official_url": record["officialUrl"],
        })

    signal_rows = db.query(
        """
        SELECT s.*, c.name AS county_name, d.title AS document_title,
               d.document_type, d.published_at
        FROM signals s
        JOIN counties c ON c.fips=s.county_fips
        JOIN documents d ON d.id=s.document_id
        WHERE s.status='active'
        ORDER BY coalesce(s.meeting_date,s.first_seen_at) DESC, s.risk_score DESC
        LIMIT 5000
        """
    )
    signals: list[dict[str, Any]] = []
    csv_signals: list[dict[str, Any]] = []
    for row in signal_rows:
        mechanisms = json_loads(row["mechanisms_json"], [])
        metadata = json_loads(row.get("metadata_json"), {})
        activity_date = row.get("meeting_date") or row.get("first_seen_at")
        current_risk = risk_for_signal(
            stage=row["stage"],
            mechanisms=mechanisms,
            posture=row["posture"],
            confidence=float(row["confidence"]),
            activity_date=activity_date,
            signal_kind=metadata.get("signal_kind"),
            action_outcome=metadata.get("action_outcome"),
        )
        current_sentiment = sentiment_for_signal(
            row["posture"], float(row["confidence"]), row["stage"]
        )
        record = {
            "id": row["id"],
            "countyFips": row["county_fips"],
            "county": row["county_name"],
            "topic": row["topic"],
            "posture": row["posture"],
            "stage": row["stage"],
            "mechanisms": mechanisms,
            "title": row["title"],
            "summary": row["summary"],
            "quote": row["evidence_quote"],
            "risk": current_risk,
            "sentiment": current_sentiment,
            "confidence": round(float(row["confidence"]) * 100, 1),
            "explicitAction": bool(row["explicit_action"]),
            "authorityCaveat": row["authority_caveat"],
            "engine": row["engine"],
            "provider": row["provider"],
            "model": row["model"],
            "meetingDate": row["meeting_date"],
            "firstSeen": row["first_seen_at"],
            "sourceUrl": row["source_url"],
            "documentTitle": row["document_title"],
            "documentType": row["document_type"],
            "signalKind": metadata.get("signal_kind"),
            "projectName": metadata.get("project_name"),
            "actionOutcome": metadata.get("action_outcome"),
            "supportingSourceCount": int(metadata.get("supporting_source_count") or 0),
            "supportingSources": metadata.get("supporting_sources") or [],
            "reviewMethod": metadata.get("review_method"),
        }
        signals.append(record)
        csv_signals.append({
            "signal_id": record["id"],
            "county_fips": record["countyFips"],
            "county": record["county"],
            "topic": record["topic"],
            "posture": record["posture"],
            "stage": record["stage"],
            "mechanisms": ";".join(mechanisms),
            "risk_score": record["risk"],
            "confidence_percent": record["confidence"],
            "meeting_date": record["meetingDate"],
            "title": record["title"],
            "evidence_quote": record["quote"],
            "source_url": record["sourceUrl"],
            "engine": record["engine"],
            "model": record["model"],
            "signal_kind": record["signalKind"],
            "project_name": record["projectName"],
            "action_outcome": record["actionOutcome"],
            "supporting_source_count": record["supportingSourceCount"],
        })

    status_counts = defaultdict(int)
    for county in counties:
        status_counts[county["status"]] += 1
    covered = sum(1 for county in counties if county["coverage"] >= 35)
    stats = {
        "countyCount": len(counties),
        "coveredCount": covered,
        "activeSignals": len(signals),
        "highRiskCount": sum(1 for county in counties if county["status"] in {"high", "critical"}),
        "resolvedSites": sum(1 for county in counties if county["officialUrl"]),
        "documents": sum(int(county["documents"]) for county in counties),
        "statuses": dict(status_counts),
    }
    dashboard = {
        "generatedAt": utcnow(),
        "snapshotDate": latest_snapshot_date,
        "stats": stats,
        "counties": counties,
        "methodologyVersion": deep_version or (
            "tx-county-deep-v2.2.0" if deep_cutover_at else "1.0.0"
        ),
        "deepRebuildAt": deep_cutover_at,
        "disclaimer": (
            "Early indicators are extracted from official public records. Scores are research triage, not legal conclusions, "
            "proof of intent, or a determination that a county possesses authority to enact a measure."
        ),
    }
    coverage = {
        "generatedAt": dashboard["generatedAt"],
        "counties": [
            {
                "fips": county["fips"],
                "name": county["name"],
                "coverage": county["coverage"],
                "siteStatus": county["siteStatus"],
                "sources": sources_by_county.get(county["fips"], []),
                "documents": county["documents"],
                "extractedDocuments": county["extractedDocuments"],
                "documentErrors": county["documentErrors"],
            }
            for county in counties
        ],
    }
    meta = {
        "generatedAt": dashboard["generatedAt"],
        "snapshotDate": latest_snapshot_date,
        "counts": stats,
        "methodologyVersion": dashboard["methodologyVersion"],
        "deepRebuildAt": deep_cutover_at,
        "dataFiles": ["dashboard.json", "signals.json", "coverage.json", "history.json", "map.json"],
    }

    _write_json(data_dir / "dashboard.json", dashboard)
    _write_json(data_dir / "signals.json", {"generatedAt": dashboard["generatedAt"], "signals": signals})
    _write_json(data_dir / "coverage.json", coverage)
    _write_json(data_dir / "history.json", {"generatedAt": dashboard["generatedAt"], "history": histories})
    _write_json(data_dir / "meta.json", meta)
    _write_csv(
        exports_dir / "county-risk.csv", csv_counties,
        [
            "county_fips", "county", "risk_status", "overall_risk", "solar_risk",
            "data_center_risk", "bess_risk", "wind_risk", "sentiment", "confidence",
            "coverage", "active_signals", "latest_activity", "official_url",
        ],
    )
    _write_csv(
        exports_dir / "signals.csv", csv_signals,
        [
            "signal_id", "county_fips", "county", "topic", "posture", "stage",
            "mechanisms", "risk_score", "confidence_percent", "meeting_date", "title",
            "evidence_quote", "source_url", "engine", "model", "signal_kind",
            "project_name", "action_outcome", "supporting_source_count",
        ],
    )
    return {"counties": len(counties), "signals": len(signals), "sources": sum(len(v) for v in sources_by_county.values())}
