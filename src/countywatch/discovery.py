from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .db import Database
from .http import CrawlerClient
from .models import SourceCandidate
from .platforms import detect_platform
from .utils import canonical_url, normalize_space, parse_iso, same_site, utcnow

POSITIVE = {
    "commissioners court": 45,
    "commissioner court": 45,
    "agenda": 34,
    "minutes": 34,
    "meeting packet": 40,
    "public notice": 30,
    "public hearing": 28,
    "ordinance": 26,
    "resolution": 18,
    "agenda center": 42,
    "meeting calendar": 25,
    "meetings": 12,
    "county clerk": 8,
    "court video": 18,
}
NEGATIVE = {
    "employment": -35,
    "jobs": -30,
    "tax sale": -30,
    "property search": -28,
    "jury": -25,
    "elections": -15,
    "bid": -12,
    "rfp": -12,
    "parks": -8,
}
APPROVED_VENDOR_TOKENS = (
    "legistar.com", "granicus.com", "civicclerk.com", "primegov.com", "civicweb.net",
    "boarddocs.com", "iqm2.com", "swagit.com", "civicplus.com", "youtube.com", "youtu.be",
    "municode.com", "agendaquick.net",
)
GUESSED_PATHS = (
    "/commissioners-court",
    "/commissionerscourt",
    "/commissioners-court/agendas-minutes",
    "/agendas-minutes",
    "/agendas-and-minutes",
    "/meetings",
    "/AgendaCenter",
    "/public-notices",
    "/government/commissioners-court",
    "/departments/commissioners-court",
    "/calendar",
)
FILE_RE = re.compile(r"\.(pdf|docx?|rtf|txt|csv)(?:$|\?)", re.I)


def link_score(label: str, url: str) -> int:
    value = normalize_space(f"{label} {url}").lower()
    score = 0
    for term, weight in POSITIVE.items():
        if term in value:
            score += weight
    for term, weight in NEGATIVE.items():
        if term in value:
            score += weight
    if FILE_RE.search(url):
        score += 15
    if any(token in value for token in ("agenda", "minute")) and "commission" in value:
        score += 20
    if any(token in value for token in ("solar", "data center", "battery storage", "moratorium")):
        score += 20
    return score


def source_type(label: str, url: str) -> str:
    value = f"{label} {url}".lower()
    if FILE_RE.search(url):
        return "document_feed"
    if "minute" in value:
        return "minutes"
    if "agenda" in value or "packet" in value:
        return "agendas"
    if "public notice" in value or "public hearing" in value:
        return "public_notices"
    if "ordinance" in value or "resolution" in value or "municode" in value:
        return "ordinances"
    if "video" in value or "youtube" in value or "swagit" in value:
        return "video"
    if "calendar" in value:
        return "calendar"
    return "meetings"


def extract_links(html: bytes | str, base_url: str) -> list[tuple[str, str, int]]:
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    links: dict[str, tuple[str, int]] = {}
    for anchor in soup.find_all("a", href=True):
        url = canonical_url(urljoin(base_url, anchor["href"]))
        if not url:
            continue
        label = normalize_space(" ".join([
            anchor.get_text(" ", strip=True),
            anchor.get("title", ""),
            anchor.get("aria-label", ""),
        ]))
        score = link_score(label, url)
        previous = links.get(url)
        if previous is None or score > previous[1]:
            links[url] = (label[:300], score)
    return [(url, label, score) for url, (label, score) in links.items()]


def parse_sitemap(content: bytes, base_url: str) -> list[str]:
    soup = BeautifulSoup(content, "xml")
    urls = []
    for loc in soup.find_all("loc")[:10000]:
        url = canonical_url(urljoin(base_url, loc.get_text(strip=True)))
        if url:
            urls.append(url)
    return urls


def is_allowed_external(homepage: str, candidate: str) -> bool:
    if same_site(homepage, candidate):
        return True
    host = (urlsplit(candidate).hostname or "").lower()
    return any(token in host for token in APPROVED_VENDOR_TOKENS)


async def discover_county(
    db: Database,
    client: CrawlerClient,
    county: dict[str, object],
    *,
    force: bool = False,
    refresh_days: int = 30,
) -> dict[str, int]:
    fips = str(county["fips"])
    homepage = canonical_url(str(county.get("official_url") or ""))
    if not homepage:
        return {"pages": 0, "sources": 0, "errors": 0, "unresolved": 1}
    last = parse_iso(str(county.get("discovery_last_run") or ""))
    if not force and last and last > datetime.now(UTC) - timedelta(days=max(1, refresh_days)) and len(db.sources(fips)) > 1:
        return {"pages": 0, "sources": 0, "errors": 0, "skipped": 1}

    candidates: dict[str, SourceCandidate] = {}
    # Third tuple value marks speculative paths whose 404/timeout is expected and
    # should not be reported as a coverage failure.
    page_queue: list[tuple[str, int, bool]] = [(homepage, 0, False)]
    seen_pages: set[str] = set()
    pages_fetched = 0
    errors = 0

    def add_candidate(url: str, label: str, score: int, method: str, html_hint: str = "") -> None:
        url = canonical_url(url)
        if not url or not is_allowed_external(homepage, url):
            return
        if score < 18:
            return
        platform = detect_platform(url, html_hint)
        priority = min(100, max(1, score + (15 if platform != "generic" else 0)))
        candidate = SourceCandidate(
            url=url,
            title=label or f"{county['name']} County meetings",
            source_type=source_type(label, url),
            platform=platform,
            priority=priority,
            discovery_method=method,
            metadata={"discovery_score": score},
        )
        previous = candidates.get(url)
        if previous is None or candidate.priority > previous.priority:
            candidates[url] = candidate

    # Explicit paths are verified before being stored, never assumed to exist.
    split = urlsplit(homepage)
    origin = urlunsplit((split.scheme, split.netloc, "", "", ""))
    for path in GUESSED_PATHS:
        page_queue.append((origin + path, 1, True))

    try:
        sitemap_roots = await client.sitemap_urls(homepage)
        for sitemap in sitemap_roots[:4]:
            try:
                result = await client.fetch(sitemap, max_bytes=8_000_000)
                for url in parse_sitemap(result.content, sitemap):
                    score = link_score("", url)
                    if score >= 18:
                        add_candidate(url, "Sitemap meeting/public-records page", score, "sitemap")
                        if same_site(homepage, url) and not FILE_RE.search(url):
                            page_queue.append((url, 1, True))
            except Exception:
                continue
    except Exception:
        pass

    while page_queue and pages_fetched < 35:
        url, depth, soft_fail = page_queue.pop(0)
        url = canonical_url(url)
        if not url or url in seen_pages or not is_allowed_external(homepage, url):
            continue
        seen_pages.add(url)
        try:
            result = await client.fetch(url, allow_browser=True, max_bytes=10_000_000)
            ctype = result.content_type
            if ctype and "html" not in ctype and "xml" not in ctype and not FILE_RE.search(url):
                continue
            pages_fetched += 1
            html = result.content.decode("utf-8", errors="replace")
            page_platform = detect_platform(result.final_url, html)
            page_score = link_score("", result.final_url)
            if page_score >= 18 or page_platform != "generic" or FILE_RE.search(result.final_url):
                title_soup = BeautifulSoup(html, "lxml")
                title = normalize_space(title_soup.title.get_text(" ") if title_soup.title else "")
                add_candidate(result.final_url, title, max(page_score, 25 if page_platform != "generic" else page_score), "verified path", html)
            for link, label, score in extract_links(result.content, result.final_url):
                if not is_allowed_external(homepage, link):
                    continue
                if score >= 18:
                    add_candidate(link, label, score, "official-site link")
                if depth < 2 and score >= 25 and not FILE_RE.search(link) and same_site(homepage, link):
                    page_queue.append((link, depth + 1, False))
        except Exception:
            if not soft_fail:
                errors += 1

    # Always retain the official homepage as a low-priority change/discovery source.
    db.upsert_source(
        fips, homepage, f"{county['name']} County website", "homepage", "generic", 10,
        "Texas Counties Deliver directory", {},
    )
    for candidate in sorted(candidates.values(), key=lambda item: item.priority, reverse=True)[:18]:
        db.upsert_source(
            fips, candidate.url, candidate.title, candidate.source_type, candidate.platform,
            candidate.priority, candidate.discovery_method, candidate.metadata,
        )
    db.update_county(fips, discovery_last_run=utcnow(), last_error=None if candidates else county.get("last_error"))
    return {"pages": pages_fetched, "sources": len(candidates), "errors": errors}


async def discover_all(
    db: Database,
    client: CrawlerClient,
    *,
    county_fips: str | None = None,
    force: bool = False,
    limit: int = 0,
    refresh_days: int = 30,
) -> dict[str, int]:
    stats = {
        "counties": 0, "pages": 0, "sources": 0, "errors": 0,
        "skipped": 0, "unresolved": 0,
    }
    lock = asyncio.Lock()

    async def worker(county: dict[str, object]) -> None:
        result = await discover_county(
            db, client, county, force=force, refresh_days=refresh_days
        )
        async with lock:
            stats["counties"] += 1
            for key in ("pages", "sources", "errors", "skipped", "unresolved"):
                stats[key] += result.get(key, 0)

    await asyncio.gather(*(worker(c) for c in db.counties(county_fips, limit)))
    return stats
