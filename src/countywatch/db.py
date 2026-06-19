from __future__ import annotations

import sqlite3
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from .utils import json_dumps, json_loads, utcnow

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS counties (
    fips TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    directory_url TEXT,
    official_url TEXT,
    seat TEXT,
    site_status TEXT NOT NULL DEFAULT 'unresolved',
    site_last_checked TEXT,
    discovery_last_run TEXT,
    last_crawl_at TEXT,
    coverage_score REAL NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    county_fips TEXT NOT NULL REFERENCES counties(fips) ON DELETE CASCADE,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'generic',
    priority INTEGER NOT NULL DEFAULT 0,
    discovery_method TEXT NOT NULL DEFAULT 'automatic',
    enabled INTEGER NOT NULL DEFAULT 1,
    etag TEXT,
    last_modified TEXT,
    last_checked TEXT,
    last_success TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_county ON sources(county_fips, enabled, priority DESC);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    county_fips TEXT NOT NULL REFERENCES counties(fips) ON DELETE CASCADE,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    canonical_url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    document_type TEXT NOT NULL DEFAULT 'unknown',
    meeting_date TEXT,
    published_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    etag TEXT,
    last_modified TEXT,
    current_content_hash TEXT,
    current_revision_id INTEGER,
    mime_type TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_documents_county_date ON documents(county_fips, meeting_date DESC, first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_id);

CREATE TABLE IF NOT EXISTS revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    binary_path TEXT,
    text_path TEXT NOT NULL,
    text_content_zlib BLOB,
    extract_method TEXT NOT NULL,
    page_count INTEGER NOT NULL DEFAULT 0,
    word_count INTEGER NOT NULL DEFAULT 0,
    extraction_metadata_json TEXT NOT NULL DEFAULT '{}',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE(document_id, content_hash, text_hash)
);
CREATE INDEX IF NOT EXISTS idx_revisions_document ON revisions(document_id, created_at DESC);

CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id INTEGER NOT NULL REFERENCES revisions(id) ON DELETE CASCADE,
    prompt_version TEXT NOT NULL,
    engine TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    passage_count INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(revision_id, prompt_version)
);
CREATE INDEX IF NOT EXISTS idx_analyses_revision ON analyses(revision_id, prompt_version);

CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    analysis_id INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    revision_id INTEGER NOT NULL REFERENCES revisions(id) ON DELETE CASCADE,
    county_fips TEXT NOT NULL REFERENCES counties(fips) ON DELETE CASCADE,
    topic TEXT NOT NULL,
    posture TEXT NOT NULL,
    stage TEXT NOT NULL,
    mechanisms_json TEXT NOT NULL DEFAULT '[]',
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    evidence_quote TEXT NOT NULL,
    evidence_start INTEGER NOT NULL,
    evidence_end INTEGER NOT NULL,
    passage_id TEXT,
    risk_score REAL NOT NULL,
    sentiment REAL NOT NULL,
    confidence REAL NOT NULL,
    explicit_action INTEGER NOT NULL DEFAULT 0,
    authority_caveat TEXT,
    engine TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    meeting_date TEXT,
    source_url TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_signals_county_topic ON signals(county_fips, topic, risk_score DESC);
CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(meeting_date DESC, first_seen_at DESC);

CREATE TABLE IF NOT EXISTS county_snapshots (
    county_fips TEXT NOT NULL REFERENCES counties(fips) ON DELETE CASCADE,
    snapshot_date TEXT NOT NULL,
    overall_risk REAL NOT NULL,
    solar_risk REAL NOT NULL,
    data_center_risk REAL NOT NULL,
    bess_risk REAL NOT NULL,
    wind_risk REAL NOT NULL,
    sentiment REAL NOT NULL,
    confidence REAL NOT NULL,
    coverage REAL NOT NULL,
    risk_status TEXT NOT NULL,
    active_signal_count INTEGER NOT NULL,
    latest_activity TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY (county_fips, snapshot_date)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_date ON county_snapshots(snapshot_date DESC);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    county_filter TEXT,
    sources_checked INTEGER NOT NULL DEFAULT 0,
    documents_seen INTEGER NOT NULL DEFAULT 0,
    revisions_created INTEGER NOT NULL DEFAULT 0,
    analyses_created INTEGER NOT NULL DEFAULT 0,
    signals_created INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    stats_json TEXT NOT NULL DEFAULT '{}',
    error TEXT
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES crawl_runs(id) ON DELETE SET NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    purpose TEXT NOT NULL,
    request_chars INTEGER NOT NULL DEFAULT 0,
    response_chars INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER,
    output_tokens INTEGER,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    def migrate(self) -> None:
        self.conn.executescript(SCHEMA)
        revision_columns = {
            str(row["name"]) for row in self.conn.execute("PRAGMA table_info(revisions)").fetchall()
        }
        if "text_content_zlib" not in revision_columns:
            self.conn.execute("ALTER TABLE revisions ADD COLUMN text_content_zlib BLOB")
        county_columns = {
            str(row["name"]) for row in self.conn.execute("PRAGMA table_info(counties)").fetchall()
        }
        if "last_crawl_at" not in county_columns:
            self.conn.execute("ALTER TABLE counties ADD COLUMN last_crawl_at TEXT")
        self.conn.execute(
            "INSERT INTO schema_meta(key,value) VALUES('schema_version','3') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return row[0] if row else default

    def bootstrap_counties(self, counties: list[dict[str, str]]) -> int:
        now = utcnow()
        with self.transaction() as conn:
            for county in counties:
                conn.execute(
                    """
                    INSERT INTO counties(fips,name,slug,directory_url,created_at,updated_at)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(fips) DO UPDATE SET
                        name=excluded.name, slug=excluded.slug,
                        directory_url=excluded.directory_url, updated_at=excluded.updated_at
                    """,
                    (
                        county["fips"],
                        county["name"],
                        county["slug"],
                        county.get("directory_url"),
                        now,
                        now,
                    ),
                )
        return len(counties)

    def counties(self, fips: str | None = None, limit: int = 0) -> list[dict[str, Any]]:
        sql = "SELECT * FROM counties"
        params: list[Any] = []
        if fips:
            sql += " WHERE fips=?"
            params.append(fips)
        sql += " ORDER BY name"
        if limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        return self.query(sql, params)


    def counties_for_crawl(
        self, fips: str | None = None, limit: int = 0
    ) -> list[dict[str, Any]]:
        """Return never-crawled counties first, then the stalest completed county."""
        sql = "SELECT * FROM counties"
        params: list[Any] = []
        if fips:
            sql += " WHERE fips=?"
            params.append(fips)
        sql += " ORDER BY (last_crawl_at IS NOT NULL), last_crawl_at, name"
        if limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        return self.query(sql, params)

    def update_county(self, fips: str, **fields: Any) -> None:
        allowed = {
            "official_url",
            "seat",
            "site_status",
            "site_last_checked",
            "discovery_last_run",
            "last_crawl_at",
            "coverage_score",
            "failure_count",
            "last_error",
        }
        values = {k: v for k, v in fields.items() if k in allowed}
        if not values:
            return
        values["updated_at"] = utcnow()
        columns = ", ".join(f"{key}=?" for key in values)
        self.execute(f"UPDATE counties SET {columns} WHERE fips=?", [*values.values(), fips])

    def upsert_source(
        self,
        county_fips: str,
        url: str,
        title: str,
        source_type: str,
        platform: str,
        priority: int,
        discovery_method: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        now = utcnow()
        self.execute(
            """
            INSERT INTO sources(
                county_fips,url,title,source_type,platform,priority,discovery_method,
                metadata_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(url) DO UPDATE SET
                county_fips=excluded.county_fips,
                title=CASE WHEN length(excluded.title)>0 THEN excluded.title ELSE sources.title END,
                source_type=excluded.source_type, platform=excluded.platform,
                priority=max(sources.priority,excluded.priority), enabled=1,
                metadata_json=excluded.metadata_json, updated_at=excluded.updated_at
            """,
            (
                county_fips,
                url,
                title,
                source_type,
                platform,
                priority,
                discovery_method,
                json_dumps(metadata or {}),
                now,
                now,
            ),
        )
        return int(self.scalar("SELECT id FROM sources WHERE url=?", (url,)))

    def sources(
        self, county_fips: str | None = None, enabled_only: bool = True
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if county_fips:
            clauses.append("county_fips=?")
            params.append(county_fips)
        if enabled_only:
            clauses.append("enabled=1")
        sql = "SELECT * FROM sources"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY priority DESC, id"
        rows = self.query(sql, params)
        for row in rows:
            row["metadata"] = json_loads(row.pop("metadata_json", "{}"), {})
        return rows

    def mark_source_success(self, source_id: int, headers: dict[str, str] | None = None) -> None:
        now = utcnow()
        headers = headers or {}
        self.execute(
            """
            UPDATE sources SET etag=coalesce(?,etag), last_modified=coalesce(?,last_modified),
                last_checked=?, last_success=?, failure_count=0, last_error=NULL, updated_at=?
            WHERE id=?
            """,
            (headers.get("etag"), headers.get("last-modified"), now, now, now, source_id),
        )

    def mark_source_failure(self, source_id: int, error: str) -> None:
        now = utcnow()
        self.execute(
            """UPDATE sources SET last_checked=?, failure_count=failure_count+1,
               last_error=?, updated_at=? WHERE id=?""",
            (now, error[:1000], now, source_id),
        )

    def document_by_url(self, url: str) -> dict[str, Any] | None:
        row = self.one("SELECT * FROM documents WHERE canonical_url=?", (url,))
        if row:
            row["metadata"] = json_loads(row.pop("metadata_json", "{}"), {})
        return row

    def touch_document_not_modified(self, document_id: int) -> None:
        self.execute(
            "UPDATE documents SET last_seen_at=?, status='ok' WHERE id=?", (utcnow(), document_id)
        )

    def upsert_document_revision(
        self,
        *,
        county_fips: str,
        source_id: int | None,
        url: str,
        title: str,
        document_type: str,
        meeting_date: str | None,
        published_at: str | None,
        etag: str | None,
        last_modified: str | None,
        mime_type: str,
        content_hash: str,
        text_hash: str,
        binary_path: str | None,
        text_path: str,
        extract_method: str,
        page_count: int,
        word_count: int,
        extraction_metadata: dict[str, Any],
        warnings: list[str],
        metadata: dict[str, Any],
    ) -> tuple[int, int, bool]:
        now = utcnow()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO documents(
                    county_fips,source_id,canonical_url,title,document_type,meeting_date,
                    published_at,first_seen_at,last_seen_at,etag,last_modified,current_content_hash,
                    mime_type,status,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'ok',?)
                ON CONFLICT(canonical_url) DO UPDATE SET
                    source_id=coalesce(excluded.source_id,documents.source_id),
                    title=CASE WHEN length(excluded.title)>0 THEN excluded.title ELSE documents.title END,
                    document_type=excluded.document_type,
                    meeting_date=coalesce(excluded.meeting_date,documents.meeting_date),
                    published_at=coalesce(excluded.published_at,documents.published_at),
                    last_seen_at=excluded.last_seen_at, etag=coalesce(excluded.etag,documents.etag),
                    last_modified=coalesce(excluded.last_modified,documents.last_modified),
                    current_content_hash=excluded.current_content_hash, mime_type=excluded.mime_type,
                    status='ok', failure_count=0, last_error=NULL, metadata_json=excluded.metadata_json
                """,
                (
                    county_fips,
                    source_id,
                    url,
                    title,
                    document_type,
                    meeting_date,
                    published_at,
                    now,
                    now,
                    etag,
                    last_modified,
                    content_hash,
                    mime_type,
                    json_dumps(metadata),
                ),
            )
            document_id = int(
                conn.execute("SELECT id FROM documents WHERE canonical_url=?", (url,)).fetchone()[0]
            )
            existing = conn.execute(
                "SELECT id FROM revisions WHERE document_id=? AND content_hash=? AND text_hash=?",
                (document_id, content_hash, text_hash),
            ).fetchone()
            created = existing is None
            if existing:
                revision_id = int(existing[0])
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO revisions(
                        document_id,content_hash,text_hash,binary_path,text_path,extract_method,
                        page_count,word_count,extraction_metadata_json,warnings_json,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        document_id,
                        content_hash,
                        text_hash,
                        binary_path,
                        text_path,
                        extract_method,
                        page_count,
                        word_count,
                        json_dumps(extraction_metadata),
                        json_dumps(warnings),
                        now,
                    ),
                )
                revision_id = int(cursor.lastrowid)
            conn.execute(
                "UPDATE documents SET current_revision_id=?, current_content_hash=? WHERE id=?",
                (revision_id, content_hash, document_id),
            )
        return document_id, revision_id, created

    def store_revision_text(self, revision_id: int, text: str) -> None:
        """Persist relevant extracted text compactly so cloud runs can upgrade cached rule results."""
        payload = zlib.compress(text.encode("utf-8"), level=9)
        self.execute(
            "UPDATE revisions SET text_content_zlib=? WHERE id=? AND text_content_zlib IS NULL",
            (sqlite3.Binary(payload), revision_id),
        )

    def revision_text(self, revision_id: int) -> str | None:
        row = self.one("SELECT text_content_zlib FROM revisions WHERE id=?", (revision_id,))
        if not row or row.get("text_content_zlib") is None:
            return None
        try:
            return zlib.decompress(row["text_content_zlib"]).decode("utf-8")
        except (zlib.error, UnicodeDecodeError):
            return None

    def rule_analysis_backlog(
        self, prompt_version: str, county_fips: str | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        params: list[Any] = [prompt_version]
        county_clause = ""
        if county_fips:
            county_clause = " AND d.county_fips=?"
            params.append(county_fips)
        params.append(max(1, limit))
        return self.query(
            f"""
            SELECT a.id AS analysis_id,a.engine,r.id AS revision_id,d.id AS document_id,
                   d.county_fips,d.title,d.document_type,d.meeting_date,d.canonical_url,
                   c.name AS county_name
            FROM analyses a
            JOIN revisions r ON r.id=a.revision_id
            JOIN documents d ON d.id=r.document_id AND d.current_revision_id=r.id
            JOIN counties c ON c.fips=d.county_fips
            WHERE a.prompt_version=? AND a.engine='rules'
              AND r.text_content_zlib IS NOT NULL{county_clause}
            ORDER BY coalesce(d.meeting_date,d.first_seen_at) DESC,a.created_at ASC
            LIMIT ?
            """,
            params,
        )

    def mark_document_failure(
        self,
        *,
        county_fips: str,
        source_id: int | None,
        url: str,
        title: str,
        document_type: str,
        error: str,
    ) -> None:
        now = utcnow()
        self.execute(
            """
            INSERT INTO documents(
                county_fips,source_id,canonical_url,title,document_type,first_seen_at,last_seen_at,
                status,failure_count,last_error
            ) VALUES(?,?,?,?,?,?,?,'error',1,?)
            ON CONFLICT(canonical_url) DO UPDATE SET
                last_seen_at=excluded.last_seen_at,status='error',
                failure_count=documents.failure_count+1,last_error=excluded.last_error
            """,
            (county_fips, source_id, url, title, document_type, now, now, error[:1000]),
        )

    def analysis(self, revision_id: int, prompt_version: str) -> dict[str, Any] | None:
        row = self.one(
            "SELECT * FROM analyses WHERE revision_id=? AND prompt_version=?",
            (revision_id, prompt_version),
        )
        if row:
            row["raw"] = json_loads(row.pop("raw_json", "{}"), {})
        return row

    def save_analysis(
        self,
        *,
        revision_id: int,
        prompt_version: str,
        engine: str,
        provider: str,
        model: str,
        status: str,
        passage_count: int,
        raw: dict[str, Any],
        error: str | None = None,
    ) -> int:
        now = utcnow()
        self.execute(
            """
            INSERT INTO analyses(
                revision_id,prompt_version,engine,provider,model,status,passage_count,
                raw_json,error,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(revision_id,prompt_version) DO UPDATE SET
                engine=excluded.engine,provider=excluded.provider,model=excluded.model,
                status=excluded.status,passage_count=excluded.passage_count,
                raw_json=excluded.raw_json,error=excluded.error,created_at=excluded.created_at
            """,
            (
                revision_id,
                prompt_version,
                engine,
                provider,
                model,
                status,
                passage_count,
                json_dumps(raw),
                error,
                now,
            ),
        )
        return int(
            self.scalar(
                "SELECT id FROM analyses WHERE revision_id=? AND prompt_version=?",
                (revision_id, prompt_version),
            )
        )

    def replace_signals(
        self,
        *,
        analysis_id: int,
        document_id: int,
        revision_id: int,
        county_fips: str,
        meeting_date: str | None,
        source_url: str,
        signals: list[dict[str, Any]],
    ) -> int:
        now = utcnow()
        with self.transaction() as conn:
            # Only signals from the document's current revision may remain active.
            # Amended agendas/minutes therefore supersede evidence from older versions,
            # including the case where the new version contains no relevant passage.
            conn.execute(
                "UPDATE signals SET status='superseded' "
                "WHERE document_id=? AND revision_id<>? AND status='active'",
                (document_id, revision_id),
            )
            conn.execute("DELETE FROM signals WHERE analysis_id=?", (analysis_id,))
            for signal in signals:
                conn.execute(
                    """
                    INSERT INTO signals(
                        id,analysis_id,document_id,revision_id,county_fips,topic,posture,stage,
                        mechanisms_json,title,summary,evidence_quote,evidence_start,evidence_end,
                        passage_id,risk_score,sentiment,confidence,explicit_action,authority_caveat,
                        engine,provider,model,meeting_date,source_url,first_seen_at,status,metadata_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?)
                    """,
                    (
                        signal["id"],
                        analysis_id,
                        document_id,
                        revision_id,
                        county_fips,
                        signal["topic"],
                        signal["posture"],
                        signal["stage"],
                        json_dumps(signal["mechanisms"]),
                        signal["title"],
                        signal["summary"],
                        signal["evidence_quote"],
                        signal["evidence_start"],
                        signal["evidence_end"],
                        signal.get("passage_id"),
                        signal["risk_score"],
                        signal["sentiment"],
                        signal["confidence"],
                        int(signal.get("explicit_action", False)),
                        signal.get("authority_caveat"),
                        signal["engine"],
                        signal["provider"],
                        signal["model"],
                        meeting_date,
                        source_url,
                        now,
                        json_dumps(signal.get("metadata", {})),
                    ),
                )
        return len(signals)

    def add_llm_usage(
        self,
        run_id: int | None,
        provider: str,
        model: str,
        purpose: str,
        request_chars: int,
        response_chars: int,
        status: str,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        error: str | None = None,
    ) -> None:
        self.execute(
            """
            INSERT INTO llm_usage(
                run_id,provider,model,purpose,request_chars,response_chars,input_tokens,
                output_tokens,status,error,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                provider,
                model,
                purpose,
                request_chars,
                response_chars,
                input_tokens,
                output_tokens,
                status,
                error,
                utcnow(),
            ),
        )

    def start_run(self, mode: str, county_filter: str | None = None) -> int:
        cursor = self.execute(
            "INSERT INTO crawl_runs(started_at,status,mode,county_filter) VALUES(?,'running',?,?)",
            (utcnow(), mode, county_filter),
        )
        return int(cursor.lastrowid)

    def finish_run(
        self, run_id: int, status: str, stats: dict[str, Any], error: str | None = None
    ) -> None:
        self.execute(
            """
            UPDATE crawl_runs SET finished_at=?,status=?,sources_checked=?,documents_seen=?,
                revisions_created=?,analyses_created=?,signals_created=?,errors=?,stats_json=?,error=?
            WHERE id=?
            """,
            (
                utcnow(),
                status,
                stats.get("sources_checked", 0),
                stats.get("documents_seen", 0),
                stats.get("revisions_created", 0),
                stats.get("analyses_created", 0),
                stats.get("signals_created", 0),
                stats.get("errors", 0),
                json_dumps(stats),
                error,
                run_id,
            ),
        )

    def save_snapshot(self, record: dict[str, Any]) -> None:
        self.execute(
            """
            INSERT INTO county_snapshots(
                county_fips,snapshot_date,overall_risk,solar_risk,data_center_risk,bess_risk,
                wind_risk,sentiment,confidence,coverage,risk_status,active_signal_count,
                latest_activity,details_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(county_fips,snapshot_date) DO UPDATE SET
                overall_risk=excluded.overall_risk,solar_risk=excluded.solar_risk,
                data_center_risk=excluded.data_center_risk,bess_risk=excluded.bess_risk,
                wind_risk=excluded.wind_risk,sentiment=excluded.sentiment,
                confidence=excluded.confidence,coverage=excluded.coverage,
                risk_status=excluded.risk_status,active_signal_count=excluded.active_signal_count,
                latest_activity=excluded.latest_activity,details_json=excluded.details_json,
                created_at=excluded.created_at
            """,
            (
                record["county_fips"],
                record["snapshot_date"],
                record["overall_risk"],
                record["solar_risk"],
                record["data_center_risk"],
                record["bess_risk"],
                record["wind_risk"],
                record["sentiment"],
                record["confidence"],
                record["coverage"],
                record["risk_status"],
                record["active_signal_count"],
                record.get("latest_activity"),
                json_dumps(record.get("details", {})),
                utcnow(),
            ),
        )
