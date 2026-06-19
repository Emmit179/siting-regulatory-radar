from __future__ import annotations

import asyncio
import http.server
import os
import shutil
import socketserver
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import Settings
from .db import Database
from .exporter import export_site
from .pipeline import bootstrap as bootstrap_project
from .pipeline import export_state_archive, import_state_archive, run_update
from .platforms import detect_platform
from .scoring import recompute_snapshots
from .utils import canonical_url

app = typer.Typer(
    name="countywatch",
    help="Texas County Regulatory Radar: crawl, analyze, score, and publish official county records.",
    no_args_is_help=True,
)
console = Console()


def _settings() -> Settings:
    settings = Settings.load()
    settings.ensure_directories()
    return settings


def _resolve_county(db: Database, value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    row = db.one(
        "SELECT fips,name FROM counties WHERE fips=? OR lower(name)=lower(?) OR lower(name || ' county')=lower(?)",
        (value, value, value),
    )
    if not row:
        raise typer.BadParameter(f"Unknown Texas county: {value}")
    return str(row["fips"])


@app.command()
def bootstrap() -> None:
    """Create the database, seed all 254 counties, and generate an empty-but-valid dashboard."""
    settings = _settings()
    result = bootstrap_project(settings)
    console.print(
        Panel.fit(
            f"Seeded [bold]{result['counties']}[/bold] Texas counties and generated the dashboard.",
            title="Bootstrap complete",
        )
    )


@app.command()
def update(
    county: Annotated[
        str | None, typer.Option(help="County name or 5-digit FIPS; default is all counties.")
    ] = None,
    max_counties: Annotated[
        int, typer.Option(min=0, help="Limit counties for debugging; 0 means all.")
    ] = 0,
    force_directory: Annotated[
        bool, typer.Option(help="Refresh official-site directory records even when fresh.")
    ] = False,
    force_discovery: Annotated[
        bool, typer.Option(help="Re-run source discovery even when fresh.")
    ] = False,
    discover_only: Annotated[
        bool, typer.Option(help="Resolve sites and sources without downloading documents.")
    ] = False,
    no_llm: Annotated[
        bool, typer.Option(help="Use only the conservative local rules engine for this run.")
    ] = False,
) -> None:
    """Incrementally update official records, analyses, scores, and the static dashboard."""
    settings = _settings()
    db = Database(settings.database)
    try:
        db.bootstrap_counties(
            __import__("countywatch.pipeline", fromlist=["bundled_counties"]).bundled_counties()
        )
        county_fips = _resolve_county(db, county)
    finally:
        db.close()
    if no_llm:
        settings.llm_enabled = False
    console.print(f"[bold]Updating[/bold] {county or 'all 254 counties'} …")
    stats = asyncio.run(
        run_update(
            settings,
            county_fips=county_fips,
            max_counties=max_counties,
            force_directory=force_directory,
            force_discovery=force_discovery,
            discover_only=discover_only,
        )
    )
    table = Table(title="Update result")
    table.add_column("Metric")
    table.add_column("Count", justify="right")
    for key in (
        "counties_checked",
        "counties_partial",
        "counties_deferred",
        "sources_checked",
        "documents_seen",
        "documents_not_modified",
        "documents_skipped_cap",
        "revisions_created",
        "analyses_created",
        "analyses_upgraded",
        "signals_created",
        "llm_calls",
        "errors",
    ):
        table.add_row(key.replace("_", " ").title(), str(stats.get(key, 0)))
    console.print(table)
    if stats.get("error_samples"):
        console.print(
            "[yellow]Representative crawl errors (coverage remains visible in the dashboard):[/yellow]"
        )
        for error in stats["error_samples"][:8]:
            console.print(f"  • {error}")


@app.command("export")
def export_command() -> None:
    """Recompute decayed scores and regenerate dashboard JSON/CSV from cached state."""
    settings = _settings()
    db = Database(settings.database)
    try:
        recompute_snapshots(db)
        result = export_site(db, settings.site_dir)
    finally:
        db.close()
    console.print(
        f"Exported {result['counties']} counties and {result['signals']} signals to {settings.site_dir}"
    )


@app.command("add-source")
def add_source(
    county: Annotated[str, typer.Argument(help="County name or 5-digit FIPS")],
    url: Annotated[str, typer.Argument(help="Official meeting/minutes/listing URL")],
    source_type: Annotated[
        str, typer.Option(help="meetings, agendas, minutes, public_notices, ordinances, or video")
    ] = "meetings",
    title: Annotated[str, typer.Option(help="Readable source title")] = "Manual official source",
) -> None:
    """Add or correct a public source without editing code."""
    settings = _settings()
    url = canonical_url(url)
    if not url:
        raise typer.BadParameter("URL must use http or https")
    db = Database(settings.database)
    try:
        db.bootstrap_counties(
            __import__("countywatch.pipeline", fromlist=["bundled_counties"]).bundled_counties()
        )
        fips = _resolve_county(db, county)
        source_id = db.upsert_source(
            fips,
            url,
            title,
            source_type,
            detect_platform(url),
            100,
            "manual override",
            {"manually_added": True},
        )
    finally:
        db.close()
    console.print(f"Added source #{source_id}: {url}")


@app.command()
def inspect(county: Annotated[str, typer.Argument(help="County name or 5-digit FIPS")]) -> None:
    """Show cached coverage, sources, and signals for one county."""
    settings = _settings()
    db = Database(settings.database)
    try:
        fips = _resolve_county(db, county)
        info = db.one("SELECT * FROM counties WHERE fips=?", (fips,))
        sources = db.sources(fips)
        signals = db.query(
            "SELECT topic,stage,posture,risk_score,title,meeting_date,source_url FROM signals WHERE county_fips=? ORDER BY risk_score DESC",
            (fips,),
        )
    finally:
        db.close()
    console.print(
        Panel.fit(
            f"[bold]{info['name']} County[/bold]\nFIPS {fips}\nOfficial site: {info.get('official_url') or 'unresolved'}\nCoverage: {info.get('coverage_score', 0):.1f}",
            title="County",
        )
    )
    table = Table(title="Sources")
    for column in ("Type", "Platform", "Last success", "Failures", "URL"):
        table.add_column(column)
    for source in sources:
        table.add_row(
            source["source_type"],
            source["platform"],
            source.get("last_success") or "—",
            str(source["failure_count"]),
            source["url"],
        )
    console.print(table)
    signal_table = Table(title="Signals")
    for column in ("Risk", "Topic", "Stage", "Posture", "Date", "Title"):
        signal_table.add_column(column)
    for signal in signals:
        signal_table.add_row(
            str(signal["risk_score"]),
            signal["topic"],
            signal["stage"],
            signal["posture"],
            signal.get("meeting_date") or "—",
            signal["title"],
        )
    console.print(signal_table)


@app.command()
def doctor() -> None:
    """Check Windows/local prerequisites and configuration without making network requests."""
    settings = _settings()
    db = Database(settings.database)
    try:
        county_count = db.scalar("SELECT count(*) FROM counties", default=0)
    finally:
        db.close()
    checks: list[tuple[str, bool, str]] = []
    checks.append(("Python", sys.version_info >= (3, 11), sys.version.split()[0]))
    checks.append(("Database", settings.database.exists(), str(settings.database)))
    checks.append(("County seed", county_count == 254, f"{county_count}/254"))
    tesseract = shutil.which("tesseract") or next(
        (
            str(p)
            for p in [
                Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
                Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
                *(
                    [
                        Path(os.environ["LOCALAPPDATA"])
                        / "Programs"
                        / "Tesseract-OCR"
                        / "tesseract.exe"
                    ]
                    if os.getenv("LOCALAPPDATA")
                    else []
                ),
            ]
            if p.exists()
        ),
        None,
    )
    checks.append(
        (
            "Tesseract OCR",
            bool(tesseract),
            tesseract or "optional locally; installed in GitHub Actions",
        )
    )
    browser_cache = Path.home() / ".cache" / "ms-playwright"
    windows_cache = (
        Path(os.getenv("LOCALAPPDATA", "")) / "ms-playwright"
        if os.getenv("LOCALAPPDATA")
        else Path("__missing__")
    )
    browser_ok = browser_cache.exists() or windows_cache.exists()
    checks.append(
        (
            "Playwright browser",
            browser_ok,
            "installed" if browser_ok else "run: python -m playwright install chromium",
        )
    )
    providers = settings.configured_providers()
    checks.append(
        (
            "Free LLM provider",
            bool(providers),
            ", ".join(providers) if providers else "none; rules fallback will work",
        )
    )
    checks.append(
        ("Static site", (settings.site_dir / "index.html").exists(), str(settings.site_dir))
    )
    table = Table(title=f"Texas County Regulatory Radar {__version__}")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    for name, ok, detail in checks:
        table.add_row(name, "[green]OK[/green]" if ok else "[yellow]Attention[/yellow]", detail)
    console.print(table)
    critical = [
        name
        for name, ok, _ in checks
        if not ok and name in {"Python", "Database", "County seed", "Static site"}
    ]
    if critical:
        raise typer.Exit(1)


@app.command()
def serve(
    port: Annotated[int, typer.Option(min=1024, max=65535)] = 8765,
    no_open: Annotated[bool, typer.Option(help="Do not open the default browser.")] = False,
) -> None:
    """Serve the dashboard locally (required because browsers block JSON fetches from file://)."""
    settings = _settings()
    os.chdir(settings.site_dir)

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A003
            console.print(f"[dim]{format % args}[/dim]")

        def end_headers(self) -> None:
            self.send_header("Cache-Control", "no-cache")
            super().end_headers()

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", port), QuietHandler) as server:
        url = f"http://127.0.0.1:{port}/"
        console.print(
            Panel.fit(
                f"Dashboard: [link={url}]{url}[/link]\nPress Ctrl+C to stop.", title="Local server"
            )
        )
        if not no_open:
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


@app.command("state-pack")
def state_pack(
    output: Annotated[Path, typer.Argument()] = Path("var/countywatch-state.sqlite3.gz"),
) -> None:
    settings = _settings()
    result = export_state_archive(settings, output.resolve())
    console.print(f"Wrote {result}")


@app.command("state-unpack")
def state_unpack(archive: Annotated[Path, typer.Argument()]) -> None:
    settings = _settings()
    import_state_archive(settings, archive.resolve())
    console.print(f"Restored state from {archive}")


if __name__ == "__main__":
    app()
