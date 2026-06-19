from __future__ import annotations

import re
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from .http import CrawlerClient
from .models import DocumentCandidate
from .utils import canonical_url, normalize_space, parse_date

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

FILE_EXTENSIONS = {".pdf", ".docx", ".doc", ".rtf", ".txt", ".csv", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
DOCUMENT_WORDS = re.compile(
    r"\b(agenda|minutes?|packet|backup|supporting documents?|ordinance|resolution|public notice|"
    r"notice of (?:meeting|hearing)|commissioners? court|workshop|special meeting|regular meeting|"
    r"transcript|meeting materials?)\b",
    re.I,
)
MEETING_PAGE_WORDS = re.compile(r"\b(meeting detail|view meeting|commissioners? court|agenda center|meetings?)\b", re.I)
IGNORE_WORDS = re.compile(r"\b(job|employment|bid|rfp|tax sale|jury|election results?|property search|inmate)\b", re.I)
DATE_PATTERNS = [
    re.compile(r"\b(0?[1-9]|1[0-2])[/-]([0-2]?\d|3[01])[/-](20\d{2})\b"),
    re.compile(r"\b(20\d{2})[/-](0?[1-9]|1[0-2])[/-]([0-2]?\d|3[01])\b"),
    re.compile(r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
               r"Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+"
               r"\d{1,2}(?:st|nd|rd|th)?,?\s+20\d{2}\b", re.I),
]


def detect_platform(url: str, html: str = "") -> str:
    combined = f"{url}\n{html[:150_000]}".lower()
    host = (urlsplit(url).hostname or "").lower()
    if "legistar.com" in host or "granicus" in combined and "legistar" in combined:
        return "legistar"
    if "agendacenter" in combined or "civicplus" in combined:
        return "civicplus"
    if "civicclerk" in combined:
        return "civicclerk"
    if "primegov" in combined:
        return "primegov"
    if "civicweb" in combined:
        return "civicweb"
    if "boarddocs" in combined:
        return "boarddocs"
    if "iqm2" in combined:
        return "iqm2"
    if "swagit" in combined:
        return "swagit"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "municode" in combined:
        return "municode"
    return "generic"


def infer_document_type(label: str, url: str) -> str:
    label_value = label.lower()
    url_value = url.lower()
    # URL and anchor-label signals are more reliable than surrounding row text,
    # which often contains both an Agenda and a Minutes link.
    if "/viewfile/agenda/" in url_value or "agenda_file" in url_value or label_value.strip().startswith("agenda"):
        return "agenda"
    if "/viewfile/minutes/" in url_value or "minutes_file" in url_value or label_value.strip().startswith("minute"):
        return "minutes"
    value = f"{label_value} {url_value}"
    if "minute" in value:
        return "minutes"
    if "packet" in value or "backup" in value or "supporting" in value:
        return "packet"
    if "agenda" in value:
        return "agenda"
    if "ordinance" in value:
        return "ordinance"
    if "resolution" in value:
        return "resolution"
    if "public notice" in value or "notice of hearing" in value:
        return "public_notice"
    if "transcript" in value or "caption" in value:
        return "transcript"
    if "youtube" in value or "video" in value or "swagit" in value:
        return "video"
    return "meeting_document"


def infer_meeting_date(value: str) -> str | None:
    for pattern in DATE_PATTERNS:
        match = pattern.search(value)
        if match:
            parsed = parse_date(match.group(0))
            if parsed:
                return parsed
    return None


def _context(anchor) -> str:
    pieces = [anchor.get_text(" ", strip=True), anchor.get("title", ""), anchor.get("aria-label", "")]
    parent = anchor.parent
    for _ in range(2):
        if parent is None:
            break
        pieces.append(parent.get_text(" ", strip=True)[:800])
        parent = parent.parent
    return normalize_space(" ".join(pieces))


def _is_file_url(url: str) -> bool:
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in FILE_EXTENSIONS:
        return True
    lowered = url.lower()
    return any(token in lowered for token in (
        "/viewfile/agenda/", "/viewfile/minutes/", "downloadfile", "documentcenter/view/",
        "?file=", "attachment", "agenda_file", "minutes_file",
    ))


def parse_listing(html: bytes | str, base_url: str, platform: str | None = None) -> tuple[list[DocumentCandidate], list[DocumentCandidate]]:
    """Return document links and meeting/detail links from a rendered or static listing page."""
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    platform = platform or detect_platform(base_url, html)
    documents: dict[str, DocumentCandidate] = {}
    detail_pages: dict[str, DocumentCandidate] = {}

    for anchor in soup.find_all("a", href=True):
        url = canonical_url(urljoin(base_url, anchor["href"]))
        if not url:
            continue
        context = _context(anchor)
        if not context:
            context = urlsplit(url).path
        if IGNORE_WORDS.search(context) and not DOCUMENT_WORDS.search(context):
            continue
        meeting_date = infer_meeting_date(context + " " + url)
        title = normalize_space(anchor.get_text(" ", strip=True) or anchor.get("title", "") or context)[:300]
        if _is_file_url(url) and (DOCUMENT_WORDS.search(context + " " + url) or Path(urlsplit(url).path).suffix.lower() in FILE_EXTENSIONS):
            documents[url] = DocumentCandidate(
                url=url,
                title=title,
                document_type=infer_document_type(title, url),
                meeting_date=meeting_date,
                parent_url=base_url,
                platform=platform,
                metadata={"link_context": context[:1200]},
            )
            continue
        lowered = f"{context} {url}".lower()
        is_detail = (
            MEETING_PAGE_WORDS.search(context) is not None
            or any(token in lowered for token in (
                "meetingdetail", "meeting.aspx", "/meetings/", "/event/", "/calendar/event/",
                "agendacenter/viewfile", "agendaonline", "meeting?id=", "eventid=",
            ))
        )
        if is_detail and url != canonical_url(base_url):
            detail_pages[url] = DocumentCandidate(
                url=url,
                title=title,
                document_type="meeting_page",
                meeting_date=meeting_date,
                parent_url=base_url,
                platform=platform,
                requires_browser=platform in {"civicclerk", "primegov", "civicweb", "boarddocs", "iqm2", "swagit"},
                metadata={"link_context": context[:1200]},
            )

    # Some meeting pages contain the relevant agenda/minute text directly with no downloadable file.
    page_text = normalize_space(soup.get_text(" "))
    if len(page_text) > 120 and DOCUMENT_WORDS.search(page_text[:5000]) and any(
        term in page_text.lower() for term in ("commissioners court", "agenda item", "regular meeting", "public hearing")
    ):
        documents.setdefault(
            canonical_url(base_url),
            DocumentCandidate(
                url=canonical_url(base_url),
                title=normalize_space(soup.title.get_text(" ") if soup.title else "Meeting page")[:300],
                document_type=infer_document_type(page_text[:1000], base_url),
                meeting_date=infer_meeting_date(page_text[:3000]),
                parent_url=base_url,
                platform=platform,
                metadata={"inline_document": True},
            ),
        )
    return list(documents.values()), list(detail_pages.values())


async def legistar_candidates(client: CrawlerClient, source_url: str, lookback_days: int) -> list[DocumentCandidate]:
    """Read public Legistar event metadata through the documented public OData endpoint."""
    split = urlsplit(source_url)
    host_parts = (split.hostname or "").split(".")
    if not host_parts or host_parts[0] in {"www", "webapi"}:
        path_match = re.search(r"/([A-Za-z0-9_-]+)/", split.path)
        if not path_match:
            return []
        client_name = path_match.group(1)
    else:
        client_name = host_parts[0]
    endpoint = (
        f"https://webapi.legistar.com/v1/{quote(client_name)}/Events"
        "?$top=250&$orderby=EventDate%20desc"
    )
    result = await client.fetch(endpoint, max_bytes=8_000_000)
    import orjson

    payload = orjson.loads(result.content)
    if not isinstance(payload, list):
        return []
    cutoff = (datetime.now(UTC) - timedelta(days=lookback_days)).date()
    candidates: dict[str, DocumentCandidate] = {}
    for event in payload:
        if not isinstance(event, dict):
            continue
        event_date = str(event.get("EventDate") or "")[:10] or None
        if event_date:
            try:
                if datetime.fromisoformat(event_date).date() < cutoff:
                    continue
            except ValueError:
                pass
        body = normalize_space(str(event.get("EventBodyName") or "Commissioners Court"))
        event_id = event.get("EventId")
        for key, dtype in (("EventAgendaFile", "agenda"), ("EventMinutesFile", "minutes")):
            url = canonical_url(str(event.get(key) or ""))
            if url:
                candidates[url] = DocumentCandidate(
                    url=url,
                    title=f"{body} {dtype.title()} {event_date or ''}".strip(),
                    document_type=dtype,
                    meeting_date=event_date,
                    parent_url=source_url,
                    platform="legistar",
                    metadata={"event_id": event_id},
                )
        event_url = canonical_url(str(event.get("EventInSiteURL") or ""))
        if event_url:
            candidates[event_url] = DocumentCandidate(
                url=event_url,
                title=f"{body} meeting {event_date or ''}".strip(),
                document_type="meeting_page",
                meeting_date=event_date,
                parent_url=source_url,
                platform="legistar",
                metadata={"event_id": event_id, "inline_document": True},
            )
    return list(candidates.values())
