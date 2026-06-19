from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path


def scalar(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> int:
    value = conn.execute(sql, params).fetchone()[0]
    return int(value or 0)


def connect_read_only(database: Path) -> sqlite3.Connection:
    uri = f"{database.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def render(database: Path) -> None:
    with connect_read_only(database) as conn:
        run = conn.execute(
            """
            SELECT id, started_at, status, mode, county_filter
            FROM crawl_runs
            WHERE status='running'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        if run is None:
            latest = conn.execute(
                """
                SELECT id, started_at, finished_at, status, mode,
                       sources_checked, documents_seen, revisions_created,
                       analyses_created, signals_created, errors
                FROM crawl_runs
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            print("No update is marked as running in the database.")
            if latest:
                print()
                print(f"Latest run:       #{latest['id']} ({latest['status']})")
                print(f"Started:          {latest['started_at']}")
                print(f"Finished:         {latest['finished_at'] or 'not recorded'}")
                print(f"Sources checked:  {latest['sources_checked']}")
                print(f"Documents seen:   {latest['documents_seen']}")
                print(f"New revisions:    {latest['revisions_created']}")
                print(f"Analyses:         {latest['analyses_created']}")
                print(f"Signals:          {latest['signals_created']}")
                print(f"Errors:           {latest['errors']}")
            return

        started = str(run["started_at"])
        total_counties = 1 if run["county_filter"] else scalar(conn, "SELECT count(*) FROM counties")

        directory_refreshed = scalar(
            conn,
            "SELECT count(*) FROM counties WHERE site_last_checked >= ?",
            (started,),
        )
        discovery_refreshed = scalar(
            conn,
            "SELECT count(*) FROM counties WHERE discovery_last_run >= ?",
            (started,),
        )
        counties_completed = scalar(
            conn,
            "SELECT count(*) FROM counties WHERE last_crawl_at >= ?",
            (started,),
        )
        counties_touched = scalar(
            conn,
            """
            SELECT count(*) FROM (
                SELECT county_fips FROM sources WHERE last_checked >= ?
                UNION
                SELECT county_fips FROM documents WHERE last_seen_at >= ?
            )
            """,
            (started, started),
        )
        sources_checked = scalar(
            conn,
            "SELECT count(*) FROM sources WHERE last_checked >= ?",
            (started,),
        )
        documents_touched = scalar(
            conn,
            "SELECT count(*) FROM documents WHERE last_seen_at >= ?",
            (started,),
        )
        revisions_created = scalar(
            conn,
            "SELECT count(*) FROM revisions WHERE created_at >= ?",
            (started,),
        )
        pages_extracted = scalar(
            conn,
            "SELECT coalesce(sum(page_count), 0) FROM revisions WHERE created_at >= ?",
            (started,),
        )
        words_extracted = scalar(
            conn,
            "SELECT coalesce(sum(word_count), 0) FROM revisions WHERE created_at >= ?",
            (started,),
        )
        analyses = scalar(
            conn,
            "SELECT count(*) FROM analyses WHERE created_at >= ?",
            (started,),
        )
        signals = scalar(
            conn,
            "SELECT count(*) FROM signals WHERE first_seen_at >= ?",
            (started,),
        )
        llm_calls = scalar(
            conn,
            "SELECT count(*) FROM llm_usage WHERE run_id = ?",
            (int(run["id"]),),
        )
        llm_successes = scalar(
            conn,
            "SELECT count(*) FROM llm_usage WHERE run_id = ? AND status='ok'",
            (int(run["id"]),),
        )
        source_failures = scalar(
            conn,
            """
            SELECT count(*) FROM sources
            WHERE last_checked >= ? AND last_error IS NOT NULL
            """,
            (started,),
        )
        document_failures = scalar(
            conn,
            """
            SELECT count(*) FROM documents
            WHERE last_seen_at >= ? AND status='error'
            """,
            (started,),
        )

        recent = conn.execute(
            """
            SELECT name, last_crawl_at
            FROM counties
            WHERE last_crawl_at >= ?
            ORDER BY last_crawl_at DESC, name
            LIMIT 8
            """,
            (started,),
        ).fetchall()

        percent = (100.0 * counties_completed / total_counties) if total_counties else 0.0
        print("Texas County Regulatory Radar — live database progress")
        print("=" * 58)
        print(f"Run:                 #{run['id']} ({run['mode']})")
        print(f"Started (UTC):       {started}")
        print()
        print(f"Directory refreshed: {directory_refreshed:>5} county records")
        print(f"Discovery refreshed: {discovery_refreshed:>5} county records")
        print(f"Crawl touched:       {counties_touched:>5} counties")
        print(
            f"Crawl completed:     {counties_completed:>5} / {total_counties} "
            f"({percent:5.1f}%)"
        )
        print()
        print(f"Sources checked:     {sources_checked:>8,}")
        print(f"Documents touched:   {documents_touched:>8,}")
        print(f"New/changed files:   {revisions_created:>8,}")
        print(f"Pages extracted:     {pages_extracted:>8,}")
        print(f"Words extracted:     {words_extracted:>8,}")
        print(f"Analyses written:    {analyses:>8,}")
        print(f"Signals found:       {signals:>8,}")
        print(f"LLM calls:           {llm_calls:>8,} ({llm_successes:,} successful)")
        print(f"Visible failures:    {source_failures + document_failures:>8,}")

        if recent:
            print()
            print("Most recently completed counties:")
            for row in recent:
                print(f"  {row['name']:<24} {row['last_crawl_at']}")

        print()
        print("Refreshes automatically. Press Ctrl+C to close this watcher only.")
        print("The update window can remain running.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only live progress watcher for CountyWatch")
    parser.add_argument("--once", action="store_true", help="Print once instead of refreshing")
    parser.add_argument("--interval", type=float, default=10.0, help="Refresh interval in seconds")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    configured = os.getenv("COUNTYWATCH_DB")
    database = Path(configured).expanduser() if configured else root / "var" / "countywatch.sqlite3"
    if not database.is_absolute():
        database = (root / database).resolve()

    if not database.exists():
        print(f"Database not found: {database}")
        print("Place these two watcher files in the project folder beside update-now.bat.")
        return 1

    try:
        while True:
            if not args.once and sys.stdout.isatty():
                os.system("cls" if os.name == "nt" else "clear")
            try:
                render(database)
            except sqlite3.Error as exc:
                print(f"Database is temporarily busy: {exc}")
                print("The watcher will retry; the updater is not affected.")
            if args.once:
                break
            time.sleep(max(2.0, args.interval))
    except KeyboardInterrupt:
        print("\nWatcher closed. The updater was not stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
