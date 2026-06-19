from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .db import Database
from .http import CrawlerClient
from .utils import canonical_url, normalize_space, parse_iso, utcnow

DIRECTORY_HOSTS = {"texascountiesdeliver.org", "www.texascountiesdeliver.org"}


def parse_county_profile(html: bytes, profile_url: str) -> tuple[str | None, str | None]:
    soup = BeautifulSoup(html, "lxml")
    official: str | None = None
    seat: str | None = None
    text = normalize_space(soup.get_text(" "))
    seat_match = re.search(r"County Seat\s+([A-Za-z .'-]+?)(?:\s+Established|\s+Population|$)", text, re.I)
    if seat_match:
        seat = normalize_space(seat_match.group(1))[:100]
    for anchor in soup.find_all("a", href=True):
        href = canonical_url(urljoin(profile_url, anchor["href"]))
        if not href:
            continue
        host = (urlsplit(href).hostname or "").lower()
        context = normalize_space(" ".join([
            anchor.get_text(" ", strip=True),
            anchor.parent.get_text(" ", strip=True) if anchor.parent else "",
        ])).lower()
        if host not in DIRECTORY_HOSTS and ("county website" in context or anchor.get_text(strip=True).lower() == "visit"):
            official = href
            break
    if not official:
        candidates: list[str] = []
        for anchor in soup.find_all("a", href=True):
            href = canonical_url(urljoin(profile_url, anchor["href"]))
            host = (urlsplit(href).hostname or "").lower() if href else ""
            if href and host not in DIRECTORY_HOSTS and host.endswith((".tx.us", ".gov")):
                candidates.append(href)
        if candidates:
            official = candidates[0]
    return official, seat


async def refresh_directory(
    db: Database,
    client: CrawlerClient,
    *,
    force: bool = False,
    county_fips: str | None = None,
    limit: int = 0,
    refresh_days: int = 45,
) -> dict[str, int]:
    counties = db.counties(county_fips, limit)
    cutoff = datetime.now(UTC) - timedelta(days=max(1, refresh_days))
    stats = {"checked": 0, "resolved": 0, "failed": 0, "skipped": 0}
    lock = asyncio.Lock()

    async def worker(county: dict[str, object]) -> None:
        last = parse_iso(str(county.get("site_last_checked") or ""))
        if not force and county.get("official_url") and last and last > cutoff:
            async with lock:
                stats["skipped"] += 1
            return
        try:
            result = await client.fetch(str(county["directory_url"]))
            official, seat = parse_county_profile(result.content, result.final_url)
            if not official:
                raise ValueError("No official county website found on directory profile")
            db.update_county(
                str(county["fips"]),
                official_url=official,
                seat=seat,
                site_status="resolved",
                site_last_checked=utcnow(),
                failure_count=0,
                last_error=None,
            )
            db.upsert_source(
                str(county["fips"]), official, f"{county['name']} County website",
                "homepage", "generic", 10, "Texas Counties Deliver directory",
                {"directory_url": county["directory_url"]},
            )
            async with lock:
                stats["resolved"] += 1
        except Exception as exc:
            db.update_county(
                str(county["fips"]),
                site_status="directory_error",
                site_last_checked=utcnow(),
                failure_count=int(county.get("failure_count") or 0) + 1,
                last_error=str(exc)[:1000],
            )
            async with lock:
                stats["failed"] += 1
        finally:
            async with lock:
                stats["checked"] += 1

    await asyncio.gather(*(worker(c) for c in counties))
    return stats
