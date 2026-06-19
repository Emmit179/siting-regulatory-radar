from __future__ import annotations

import asyncio
import time
import urllib.robotparser
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from .config import Settings
from .models import FetchResult
from .utils import canonical_url


class FetchError(RuntimeError):
    pass


class RobotsDenied(FetchError):
    pass


class TooLarge(FetchError):
    pass


@dataclass(slots=True)
class RobotsEntry:
    parser: urllib.robotparser.RobotFileParser
    fetched_at: float


class CrawlerClient:
    """Respectful async HTTP client with robots.txt, per-host pacing, and browser fallback."""

    def __init__(self, settings: Settings):
        self.settings = settings
        ua = settings.user_agent
        if settings.contact_email and "mailto:" not in ua:
            ua = f"{ua} (mailto:{settings.contact_email})"
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/pdf,application/xml,text/plain,*/*;q=0.7",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        }
        timeout = httpx.Timeout(settings.request_timeout, connect=min(20.0, settings.request_timeout))
        limits = httpx.Limits(max_connections=settings.concurrency * 2, max_keepalive_connections=settings.concurrency)
        self.client = httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
            http2=True,
            limits=limits,
        )
        self.insecure_client = httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
            http2=False,
            verify=False,
            limits=limits,
        )
        self._global = asyncio.Semaphore(settings.concurrency)
        self._host_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_request: dict[str, float] = defaultdict(float)
        self._robots: dict[str, RobotsEntry] = {}
        self._browser = None
        self._playwright = None

    async def __aenter__(self) -> "CrawlerClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        await self.client.aclose()
        await self.insecure_client.aclose()

    async def _pace(self, host: str) -> None:
        async with self._host_locks[host]:
            elapsed = time.monotonic() - self._last_request[host]
            delay = self.settings.per_host_delay - elapsed
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request[host] = time.monotonic()

    async def _allowed(self, url: str) -> bool:
        split = urlsplit(url)
        origin = urlunsplit((split.scheme, split.netloc, "", "", ""))
        entry = self._robots.get(origin)
        if entry and time.monotonic() - entry.fetched_at < 86400:
            return entry.parser.can_fetch(self.settings.user_agent, url)
        parser = urllib.robotparser.RobotFileParser()
        robots_url = origin + "/robots.txt"
        parser.set_url(robots_url)
        try:
            result = await self._raw_fetch(robots_url, max_bytes=1_000_000, check_robots=False)
            if 200 <= result.status_code < 300:
                parser.parse(result.content.decode("utf-8", errors="replace").splitlines())
            else:
                parser.parse([])
        except Exception:
            parser.parse([])
        self._robots[origin] = RobotsEntry(parser=parser, fetched_at=time.monotonic())
        return parser.can_fetch(self.settings.user_agent, url)

    @retry(
        retry=retry_if_exception_type(
            (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=12),
        reraise=True,
    )
    async def _request(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        max_bytes: int,
    ) -> tuple[httpx.Response, bytes]:
        """Stream a response so the decoded body never exceeds the configured cap."""
        request = client.build_request("GET", url, headers=headers)
        response = await client.send(request, stream=True, follow_redirects=True)
        try:
            content_length = response.headers.get("content-length")
            if content_length and content_length.isdigit() and int(content_length) > max_bytes:
                raise TooLarge(f"Content-Length {content_length} exceeds {max_bytes} bytes")
            if response.status_code == 304:
                return response, b""
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > max_bytes:
                    raise TooLarge(f"Response exceeded {max_bytes} bytes")
            return response, bytes(content)
        finally:
            await response.aclose()

    async def _raw_fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        max_bytes: int | None = None,
        check_robots: bool = True,
    ) -> FetchResult:
        url = canonical_url(url)
        if not url:
            raise FetchError("Unsupported or malformed URL")
        if check_robots and not await self._allowed(url):
            raise RobotsDenied(f"robots.txt does not allow crawling {url}")
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        split = urlsplit(url)
        await self._pace(split.netloc.lower())
        max_bytes = max_bytes or self.settings.max_document_bytes
        async with self._global:
            insecure = False
            try:
                response, content = await self._request(self.client, url, headers, max_bytes)
            except httpx.ConnectError as exc:
                tls_error = (
                    "CERTIFICATE_VERIFY_FAILED" in str(exc).upper()
                    or "SSL" in str(exc).upper()
                )
                if not tls_error or not self.settings.allow_insecure_tls:
                    raise
                response, content = await self._request(
                    self.insecure_client, url, headers, max_bytes
                )
                insecure = True
        if response.status_code == 304:
            return FetchResult(url, str(response.url), 304, dict(response.headers), b"", not_modified=True)
        result_headers = {k.lower(): v for k, v in response.headers.items()}
        if insecure:
            result_headers["x-countywatch-insecure-tls"] = "true"
        return FetchResult(
            requested_url=url,
            final_url=canonical_url(str(response.url)),
            status_code=response.status_code,
            headers=result_headers,
            content=content,
        )

    async def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        max_bytes: int | None = None,
        allow_browser: bool = False,
    ) -> FetchResult:
        result = await self._raw_fetch(
            url,
            etag=etag,
            last_modified=last_modified,
            max_bytes=max_bytes,
            check_robots=True,
        )
        if result.not_modified:
            return result
        if result.status_code >= 400:
            raise FetchError(f"HTTP {result.status_code} for {url}")
        ctype = result.content_type
        sparse_html = ctype in {"text/html", "application/xhtml+xml", ""} and len(result.content) < 900
        blocked = b"enable javascript" in result.content.lower() or b"access denied" in result.content.lower()
        if allow_browser and self.settings.enable_browser and (sparse_html or blocked):
            return await self.fetch_browser(result.final_url or url, max_bytes=max_bytes)
        return result

    async def fetch_browser(
        self, url: str, *, max_bytes: int | None = None
    ) -> FetchResult:
        if not self.settings.enable_browser:
            raise FetchError("Browser fallback is disabled")
        if not await self._allowed(url):
            raise RobotsDenied(f"robots.txt does not allow browser crawling {url}")
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise FetchError("Playwright is not installed") from exc
        if self._browser is None:
            self._playwright = await async_playwright().start()
            try:
                self._browser = await self._playwright.chromium.launch(headless=True)
            except Exception as exc:
                raise FetchError("Playwright Chromium is not installed; run: playwright install chromium") from exc
        context = await self._browser.new_context(
            user_agent=self.settings.user_agent,
            locale="en-US",
            java_script_enabled=True,
        )
        page = await context.new_page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=int(self.settings.request_timeout * 1000))
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            html = await page.content()
            content = html.encode("utf-8")
            limit = max_bytes or self.settings.max_document_bytes
            if len(content) > limit:
                raise TooLarge(f"Rendered page exceeded {limit} bytes")
            final_url = canonical_url(page.url)
            status = response.status if response else 200
            if status >= 400:
                raise FetchError(f"Browser HTTP {status} for {url}")
            headers = (
                {k.lower(): v for k, v in (await response.all_headers()).items()}
                if response
                else {}
            )
            headers["content-type"] = "text/html; charset=utf-8"
            return FetchResult(
                url, final_url, status, headers, content, from_browser=True
            )
        finally:
            await context.close()

    async def sitemap_urls(self, homepage: str) -> list[str]:
        split = urlsplit(homepage)
        origin = urlunsplit((split.scheme, split.netloc, "", "", ""))
        urls = [origin + "/sitemap.xml", origin + "/sitemap_index.xml"]
        entry = self._robots.get(origin)
        if entry:
            for candidate in getattr(entry.parser, "site_maps", lambda: [])() or []:
                urls.append(candidate)
        return list(dict.fromkeys(urls))
