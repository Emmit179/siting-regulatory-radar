from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import Settings
from .db import Database
from .scoring import risk_for_signal, sentiment_for_signal
from .utils import json_dumps, json_loads, normalize_space, utcnow

console = Console()
CLEANUP_VERSION = "countywatch-final-cleanup-v1"

# These are intentionally narrow. They describe records whose own caveat and quote
# identify another county's action as background, not the host county's action.
_FOREIGN_CAVEAT_PATTERNS = (
    re.compile(r"\bbelongs to\s+(?P<foreign>[A-Z][A-Za-z .'-]+?)\s+County\b", re.I),
    re.compile(r"\bexternal\s+(?P<foreign>[A-Z][A-Za-z .'-]+?)\s+County\b", re.I),
    re.compile(r"\bproject is in\s+(?P<foreign>[A-Z][A-Za-z .'-]+?)\s+County\b.*?\bdoes not establish\b", re.I | re.S),
    re.compile(r"\bwells?\b.*?\bin\s+(?P<foreign>[A-Z][A-Za-z .'-]+?)\s+County\b.*?\bnot\s+(?:a|an)\b", re.I | re.S),
)

_STRONG_FOREIGN_HOST_NEGATION = re.compile(
    r"\b(?:not\s+(?:itself\s+)?(?:a|an)|not\s+the\s+operative|not\s+(?:a|an)\s+[^.]{0,80}?"
    r"Commissioners\s+Court\s+action)\b",
    re.I,
)

_LOCAL_MORATORIUM_PATTERN = re.compile(
    r"\b(?:an\s+order\s+of|order\s+of|commissioners\s+court\s+of)\b.{0,220}?"
    r"\bdeclaring\b.{0,120}?\bmoratorium\b|"
    r"\bdeclaring\s+a\s+temporary\s+moratorium\b",
    re.I | re.S,
)

_STATE_REQUEST_PATTERN = re.compile(
    r"\b(?:request|urge|petition|ask)\b.{0,180}?\b(?:governor|legislature|state\s+of\s+texas|"
    r"state\s+officials?|state\s+agencies)\b",
    re.I | re.S,
)

_LOW_VALUE_CONTEXT_PATTERN = re.compile(
    r"\b(?:scholarship(?:s)?|museum(?:'s)?\s+windmill|wind\s+turbine\s+contributions?\s+payment)\b",
    re.I,
)

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_MONTH_PATTERN = "|".join(sorted(_MONTHS, key=len, reverse=True))

_EPHEMERAL_QUERY_KEYS = {
    "sig",
    "se",
    "st",
    "sp",
    "sv",
    "skoid",
    "sktid",
    "skt",
    "ske",
    "sks",
    "skv",
    "x-amz-signature",
    "x-amz-credential",
    "x-amz-date",
    "x-amz-expires",
}

_ACTION_MARKERS = (
    "moratorium",
    "tax abatement",
    "reinvestment zone",
    "road use",
    "development agreement",
    "public hearing",
    "permit",
    "resolution",
    "petition",
    "application",
    "non-compliance",
    "noncompliance",
    "fire safety",
    "water",
    "amendment",
    "renewal",
    "extension",
)

_DISTINCT_ACTION_MARKERS = re.compile(
    r"\b(?:amended|restated|first\s+amendment|second\s+amendment|renewal|extension|"
    r"reinvestment\s+zone|road\s+use|fire\s+safety|tax\s+abatement)\b",
    re.I,
)


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", normalize_space(str(value or "")).lower()).strip()


def _norm_url(value: Any) -> str:
    url = normalize_space(str(value or ""))
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    query = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in {"feature", "utm_source", "utm_medium", "utm_campaign"}
    ]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "&".join(f"{k}={v}" for k, v in query), ""))


def _is_ephemeral_url(value: Any) -> bool:
    url = normalize_space(str(value or ""))
    if not url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = parts.netloc.lower()
    keys = {key.lower() for key, _ in parse_qsl(parts.query, keep_blank_values=True)}
    return host.endswith(".blob.core.windows.net") or bool(keys & _EPHEMERAL_QUERY_KEYS)


def _valid_date(year: int, month: int, day: int) -> str | None:
    if year < 1990 or year > 2100:
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _expand_year(year: int) -> int:
    if year >= 100:
        return year
    return 2000 + year if year <= 79 else 1900 + year


def _extract_dates(value: Any) -> list[str]:
    """Extract only complete calendar dates; never invent a day for month/year text."""
    text = unquote(str(value or ""))
    if not text:
        return []
    found: list[tuple[int, str]] = []

    def add(position: int, year: int, month: int, day: int) -> None:
        parsed = _valid_date(year, month, day)
        if parsed and parsed not in {item[1] for item in found}:
            found.append((position, parsed))

    # Compact YYYYMMDD filenames are the strongest and most common county pattern.
    for match in re.finditer(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])([0-2]\d|3[01])(?!\d)", text):
        add(match.start(), int(match.group(1)), int(match.group(2)), int(match.group(3)))

    for match in re.finditer(
        r"(?<!\d)(20\d{2})[._/\-](0?[1-9]|1[0-2])[._/\-]([0-2]?\d|3[01])(?!\d)",
        text,
    ):
        add(match.start(), int(match.group(1)), int(match.group(2)), int(match.group(3)))

    for match in re.finditer(
        r"(?<!\d)(0?[1-9]|1[0-2])[._/\-]([0-2]?\d|3[01])[._/\-](\d{2}|20\d{2})(?!\d)",
        text,
    ):
        add(
            match.start(),
            _expand_year(int(match.group(3))),
            int(match.group(1)),
            int(match.group(2)),
        )

    # Seven-digit MDDYYYY filenames, e.g. agenda_items_5272025.pdf.
    for match in re.finditer(r"(?<!\d)(\d{7})(?!\d)", text):
        raw = match.group(1)
        candidates = [
            (int(raw[0]), int(raw[1:3]), int(raw[3:])),
            (int(raw[:2]), int(raw[2:3]), int(raw[3:])),
        ]
        for month, day, year in candidates:
            parsed = _valid_date(year, month, day)
            if parsed:
                found.append((match.start(), parsed))
                break

    for match in re.finditer(
        rf"\b({_MONTH_PATTERN})\.?\s+([0-2]?\d|3[01])(?:st|nd|rd|th)?(?:\s*,\s*|\s+)(20\d{{2}}|\d{{2}})\b",
        text,
        re.I,
    ):
        add(
            match.start(),
            _expand_year(int(match.group(3))),
            _MONTHS[match.group(1).lower().rstrip(".")],
            int(match.group(2)),
        )

    for match in re.finditer(
        rf"\b([0-2]?\d|3[01])\s+({_MONTH_PATTERN})\.?(?:\s*,\s*|\s+)(20\d{{2}}|\d{{2}})\b",
        text,
        re.I,
    ):
        add(
            match.start(),
            _expand_year(int(match.group(3))),
            _MONTHS[match.group(2).lower().rstrip(".")],
            int(match.group(1)),
        )

    found.sort(key=lambda item: item[0])
    return [value for _, value in found]


def _date_only(value: Any) -> str | None:
    text = normalize_space(str(value or ""))
    if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return None
    return None


def _source_type_rank(source: dict[str, Any]) -> int:
    title = _norm(source.get("title"))
    document_type = _norm(source.get("documentType") or source.get("document_type"))
    if "minutes" in title or document_type == "minutes":
        return 60
    if any(word in title for word in ("ordinance", "resolution", "order", "agreement")):
        return 55
    if document_type in {"ordinance", "resolution"}:
        return 55
    if "public hearing" in title or document_type == "public notice":
        return 35
    if "agenda packet" in title:
        return 22
    if "agenda" in title or document_type == "agenda":
        return 18
    if document_type == "meeting page":
        return 15
    return 10


def _quote_match_score(source_quote: Any, evidence_quote: Any) -> int:
    source = _norm(source_quote)
    evidence = _norm(evidence_quote)
    if not source or not evidence:
        return 0
    if source == evidence:
        return 100
    if source in evidence or evidence in source:
        shorter = min(len(source), len(evidence))
        longer = max(len(source), len(evidence))
        return 80 if shorter / max(1, longer) >= 0.65 else 55
    source_words = set(source.split())
    evidence_words = set(evidence.split())
    overlap = len(source_words & evidence_words) / max(1, len(source_words | evidence_words))
    return int(overlap * 60)


def _dedupe_sources(sources: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    by_url: dict[str, int] = {}
    for raw in sources:
        if not isinstance(raw, dict):
            continue
        source = dict(raw)
        url = normalize_space(str(source.get("url") or ""))
        if not url:
            continue
        key = _norm_url(url)
        if key in by_url:
            existing = output[by_url[key]]
            if _quote_match_score(source.get("quote"), existing.get("quote")) > 90 and len(str(source.get("quote") or "")) > len(str(existing.get("quote") or "")):
                existing["quote"] = source.get("quote")
            if not existing.get("meetingDate") and source.get("meetingDate"):
                existing["meetingDate"] = source.get("meetingDate")
            if len(str(source.get("title") or "")) > len(str(existing.get("title") or "")):
                existing["title"] = source.get("title")
            continue
        by_url[key] = len(output)
        output.append(source)
    return output


def _align_and_choose_source(signal: dict[str, Any]) -> tuple[bool, bool]:
    metadata = signal.setdefault("metadata", {})
    raw_sources = list(metadata.get("supporting_sources") or [])
    current_url = normalize_space(str(signal.get("source_url") or ""))
    evidence_quote = str(signal.get("evidence_quote") or "")
    if current_url and not any(_norm_url(source.get("url")) == _norm_url(current_url) for source in raw_sources if isinstance(source, dict)):
        raw_sources.insert(
            0,
            {
                "url": current_url,
                "title": signal.get("title"),
                "documentType": "meeting_document",
                "meetingDate": _date_only(signal.get("meeting_date")),
                "quote": evidence_quote,
            },
        )
    sources = _dedupe_sources(raw_sources)
    if not sources:
        metadata["supporting_sources"] = []
        metadata["supporting_source_count"] = 0
        return False, False

    event_date = _date_only(signal.get("meeting_date"))

    def rank(source: dict[str, Any]) -> tuple[int, int, int, int, str]:
        stable = 0 if _is_ephemeral_url(source.get("url")) else 100
        quote_score = _quote_match_score(source.get("quote"), evidence_quote)
        type_score = _source_type_rank(source)
        date_score = 12 if event_date and _date_only(source.get("meetingDate")) == event_date else 0
        return (stable, quote_score, type_score, date_score, str(source.get("url") or ""))

    current_source = next(
        (source for source in sources if _norm_url(source.get("url")) == _norm_url(current_url)),
        None,
    )
    # Preserve an already durable canonical link. Re-ranking is only needed when the
    # current URL is signed/expiring or absent from the source ledger.
    chosen = (
        current_source
        if current_source is not None and current_url and not _is_ephemeral_url(current_url)
        else max(sources, key=rank)
    )
    old_url = current_url
    new_url = normalize_space(str(chosen.get("url") or current_url))
    url_changed = bool(new_url and new_url != old_url)
    if url_changed:
        metadata["cleanup_previous_source_url"] = old_url
    signal["source_url"] = new_url

    # The canonical evidence quote must be visibly attached to at least one supporting
    # source. When the same document contains several separate agenda items, URL-only
    # deduplication used to retain the wrong item's quote.
    quote_aligned = False
    chosen_key = _norm_url(new_url)
    for source in sources:
        if _norm_url(source.get("url")) == chosen_key:
            if _norm(source.get("quote")) != _norm(evidence_quote):
                source["quote"] = evidence_quote
                quote_aligned = True
            break

    sources.sort(key=rank, reverse=True)
    metadata["supporting_sources"] = sources[:20]
    metadata["supporting_source_count"] = len(metadata["supporting_sources"])
    return url_changed, quote_aligned


def _is_compilation_source(source: dict[str, Any]) -> bool:
    title = _norm(source.get("title"))
    return bool(
        re.search(r"\b(?:all\s+minutes|minutes\s+for\s+20\d{2}|20\d{2}\s+minutes|annual\s+minutes)\b", title)
    )


def _action_dates(value: Any) -> list[str]:
    text = str(value or "")
    if not text:
        return []
    output: list[str] = []
    action = re.compile(
        r"\b(?:approved?|adopted?|passed|effective|scheduled\s+for|will\s+be\s+held|"
        r"meeting\s+on|hearing\s+on|motion\s+carried|resolved\s+this)\b",
        re.I,
    )
    for chunk in re.split(r"[\n\r]+|(?<=[.;])\s+", text):
        if action.search(chunk):
            for parsed in _extract_dates(chunk):
                if parsed not in output:
                    output.append(parsed)
    return output


def _resolve_signal_date(signal: dict[str, Any]) -> tuple[str | None, bool, str | None]:
    """Choose an event date conservatively.

    Existing date-only values are preserved unless a source filename/title is clearly
    inconsistent by more than four months, or an expiring canonical URL was replaced by
    a direct minutes/resolution source carrying the exact quote. Crawl timestamps are
    never exposed as meeting dates.
    """
    metadata = signal.setdefault("metadata", {})
    sources = list(metadata.get("supporting_sources") or [])
    evidence_quote = signal.get("evidence_quote")
    canonical_key = _norm_url(signal.get("source_url"))
    canonical = next(
        (source for source in sources if _norm_url(source.get("url")) == canonical_key),
        sources[0] if sources else {},
    )
    existing = _date_only(signal.get("meeting_date"))

    canonical_title_dates = _extract_dates(canonical.get("title"))
    canonical_url_dates = _extract_dates(canonical.get("url"))
    canonical_meeting = _date_only(canonical.get("meetingDate"))
    exact_quote = _quote_match_score(canonical.get("quote"), evidence_quote) >= 80

    if existing:
        # A portal sometimes takes an unrelated date from inside an application. A full
        # date embedded in the actual source title/filename is safer when the conflict is
        # extreme and the source is not an annual compilation.
        explicit = (canonical_title_dates or canonical_url_dates)
        if explicit and not _is_compilation_source(canonical):
            candidate = explicit[0]
            distance = abs((date.fromisoformat(existing) - date.fromisoformat(candidate)).days)
            if distance > 120:
                return candidate, candidate != signal.get("meeting_date"), (
                    "source title" if canonical_title_dates else "source URL"
                )

        # When an expiring blob was replaced with a durable minutes/resolution endpoint,
        # use that exact source's meeting date rather than a later packet that happened to
        # reproduce the same minutes.
        previous_url = str(metadata.get("cleanup_previous_source_url") or "")
        if (
            previous_url
            and _is_ephemeral_url(previous_url)
            and canonical_meeting
            and exact_quote
            and not _is_compilation_source(canonical)
        ):
            return canonical_meeting, canonical_meeting != signal.get("meeting_date"), "durable canonical source date"
        return existing, existing != signal.get("meeting_date"), "existing date"

    # Timestamp/null fallback: use source metadata, not the crawl time. Prefer a full
    # date in the source title, then a declared source meeting date, then filename.
    if canonical_title_dates:
        chosen = canonical_title_dates[0]
        return chosen, chosen != signal.get("meeting_date"), "source title"
    if canonical_meeting and not _is_compilation_source(canonical):
        return canonical_meeting, canonical_meeting != signal.get("meeting_date"), "source meeting date"
    if canonical_url_dates and not _is_compilation_source(canonical):
        chosen = canonical_url_dates[0]
        return chosen, chosen != signal.get("meeting_date"), "source URL"

    action_dates = _action_dates(evidence_quote)
    if action_dates:
        chosen = action_dates[0]
        return chosen, chosen != signal.get("meeting_date"), "action date in evidence quote"

    old = signal.get("meeting_date")
    return None, old is not None, "removed crawl timestamp" if old else None


def _foreign_context_reason(signal: dict[str, Any], county_names: set[str]) -> str | None:
    host = normalize_space(str(signal.get("county_name") or ""))
    caveat = normalize_space(str(signal.get("authority_caveat") or ""))
    quote = normalize_space(str(signal.get("evidence_quote") or ""))
    title = normalize_space(str(signal.get("title") or ""))
    combined = f"{title} {quote} {caveat}"
    host_norm = _norm(host)

    foreign_names = [
        name
        for name in sorted(county_names, key=len, reverse=True)
        if _norm(name) != host_norm
        and not host_norm.endswith(" " + _norm(name))
        and not host_norm.startswith(_norm(name) + " ")
        and re.search(rf"\b{re.escape(name)}\s+County\b", combined, re.I)
    ]
    if not foreign_names:
        return None

    for pattern in _FOREIGN_CAVEAT_PATTERNS:
        match = pattern.search(caveat)
        if match:
            foreign = normalize_space(match.group("foreign"))
            if _norm(foreign) != host_norm and any(_norm(foreign) == _norm(name) for name in foreign_names):
                return f"external {foreign} County action"

    if _STRONG_FOREIGN_HOST_NEGATION.search(caveat) and re.search(
        rf"\b(?:not|no)\b.{0,100}?\b{re.escape(host)}\s+County\b", caveat, re.I
    ):
        return f"external {foreign_names[0]} County context"

    if re.search(r"\b(?:included|used)\s+as\s+supporting\s+material\b", title + " " + caveat, re.I):
        return f"external {foreign_names[0]} County supporting material"

    # A facially foreign order can be removed when the host county is not the actor in
    # the quote. This catches copied Hill County orders in Limestone packets without
    # discarding a host county's own discussion of a neighboring policy.
    for foreign in foreign_names:
        if re.search(
            rf"\b(?:an\s+order\s+of\s+the\s+commissioners\s+court\s+of|"
            rf"commissioners\s+court\s+of|{re.escape(foreign)}\s+County,?\s+Texas)\b",
            quote,
            re.I,
        ) and not re.search(
            rf"\b(?:{re.escape(host)}\s+County\s+(?:adopted|approved|ordered|moved)|"
            rf"commissioners\s+court\s+of\s+{re.escape(host)}\s+County)\b",
            quote,
            re.I,
        ):
            return f"facially external {foreign} County instrument"
    return None


def _is_local_moratorium(signal: dict[str, Any]) -> bool:
    county = normalize_space(str(signal.get("county_name") or ""))
    quote = normalize_space(str(signal.get("evidence_quote") or ""))
    if not county or "moratorium" not in quote.lower():
        return False
    if not re.search(rf"\b{re.escape(county)}\s+County\b", quote, re.I):
        return False
    if _STATE_REQUEST_PATTERN.search(quote) and not _LOCAL_MORATORIUM_PATTERN.search(quote):
        return False
    local_scope = re.search(
        rf"\b(?:within\s+(?:the\s+)?(?:borders|unincorporated\s+areas)\s+of|within)\b.{0,100}?"
        rf"\b{re.escape(county)}\s+County\b",
        quote,
        re.I | re.S,
    )
    return bool(_LOCAL_MORATORIUM_PATTERN.search(quote) and local_scope)


def _correct_local_moratorium(signal: dict[str, Any]) -> bool:
    metadata = signal.setdefault("metadata", {})
    current_kind = str(metadata.get("signal_kind") or "")
    if current_kind == "local_restriction" or not _is_local_moratorium(signal):
        return False
    signal["posture"] = "restrictive"
    signal["stage"] = "adopted"
    mechanisms = list(dict.fromkeys([*(signal.get("mechanisms") or []), "moratorium", "prohibition"]))
    signal["mechanisms"] = mechanisms
    signal["explicit_action"] = True
    signal["authority_caveat"] = (
        "The quoted county order facially adopts a local moratorium. This classification does not "
        "opine on the county's legal authority, enforceability, duration, amendment, or judicial review."
    )
    metadata["signal_kind"] = "local_restriction"
    metadata["action_outcome"] = "adopted"
    metadata["cleanup_correction"] = "facial_local_moratorium"
    return True


def _is_low_value_context(signal: dict[str, Any]) -> bool:
    metadata = signal.get("metadata", {})
    kind = str(metadata.get("signal_kind") or "")
    if kind not in {"other_material", "project_monitoring"}:
        return False
    text = " ".join(
        str(signal.get(key) or "") for key in ("title", "summary", "evidence_quote")
    )
    return bool(_LOW_VALUE_CONTEXT_PATTERN.search(text))


def _action_fingerprint(signal: dict[str, Any]) -> tuple[str, ...]:
    text = _norm(
        " ".join(
            str(signal.get(key) or "") for key in ("title", "summary", "evidence_quote")
        )
    )
    return tuple(marker for marker in _ACTION_MARKERS if marker in text)


def _source_url_set(signal: dict[str, Any]) -> set[str]:
    metadata = signal.get("metadata", {})
    values = {_norm_url(signal.get("source_url"))}
    values.update(
        _norm_url(source.get("url"))
        for source in metadata.get("supporting_sources", [])
        if isinstance(source, dict)
    )
    return {value for value in values if value}


def _token_jaccard(left: Any, right: Any) -> float:
    a = set(_norm(left).split())
    b = set(_norm(right).split())
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _can_merge(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left.get("county_fips") != right.get("county_fips") or left.get("topic") != right.get("topic"):
        return False
    left_meta = left.get("metadata", {})
    right_meta = right.get("metadata", {})
    if (
        left_meta.get("signal_kind") != right_meta.get("signal_kind")
        or left.get("stage") != right.get("stage")
        or left_meta.get("action_outcome") != right_meta.get("action_outcome")
    ):
        return False
    left_project = _norm(left_meta.get("project_name"))
    right_project = _norm(right_meta.get("project_name"))
    if not left_project or not right_project or left_project != right_project:
        return False
    if not (_source_url_set(left) & _source_url_set(right)):
        return False
    if _action_fingerprint(left) != _action_fingerprint(right):
        return False
    left_distinct = set(_DISTINCT_ACTION_MARKERS.findall(str(left.get("title") or "")))
    right_distinct = set(_DISTINCT_ACTION_MARKERS.findall(str(right.get("title") or "")))
    if left_distinct != right_distinct:
        return False
    return (
        _token_jaccard(left.get("evidence_quote"), right.get("evidence_quote")) >= 0.94
        and _token_jaccard(left.get("title"), right.get("title")) >= 0.82
    )


def _merge_pair(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    # Prefer the stronger direct source, but preserve every unique supporting source and
    # every member event so the consolidation remains auditable.
    preferred = max(
        (left, right),
        key=lambda signal: (
            float(signal.get("confidence") or 0),
            len(str(signal.get("evidence_quote") or "")),
            str(signal.get("meeting_date") or ""),
        ),
    )
    other = right if preferred is left else left
    merged = dict(preferred)
    metadata = dict(preferred.get("metadata", {}))
    other_metadata = other.get("metadata", {})
    member_ids = list(
        dict.fromkeys(
            [
                *(metadata.get("member_ids") or []),
                *(other_metadata.get("member_ids") or []),
            ]
        )
    )
    metadata["member_ids"] = member_ids
    metadata["supporting_sources"] = _dedupe_sources(
        [
            *(metadata.get("supporting_sources") or []),
            *(other_metadata.get("supporting_sources") or []),
        ]
    )[:20]
    metadata["supporting_source_count"] = len(metadata["supporting_sources"])
    metadata["cleanup_merged_signal_ids"] = list(
        dict.fromkeys(
            [
                *(metadata.get("cleanup_merged_signal_ids") or []),
                left.get("id"),
                right.get("id"),
            ]
        )
    )
    merged["metadata"] = metadata
    merged["id"] = hashlib.sha256(
        ("final-cleanup|" + str(merged.get("county_fips")) + "|" + "|".join(sorted(member_ids))).encode("utf-8")
    ).hexdigest()[:24]
    return merged


def _merge_conservative_duplicates(signals: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    output: list[dict[str, Any]] = []
    merges: list[dict[str, Any]] = []
    for signal in signals:
        for index, existing in enumerate(output):
            if _can_merge(existing, signal):
                merged = _merge_pair(existing, signal)
                output[index] = merged
                merges.append(
                    {
                        "county": signal.get("county_name"),
                        "kept": merged.get("id"),
                        "members": [existing.get("id"), signal.get("id")],
                        "title": merged.get("title"),
                    }
                )
                break
        else:
            output.append(signal)
    return output, merges


def _recompute_signal_score(signal: dict[str, Any]) -> None:
    metadata = signal.setdefault("metadata", {})
    confidence = max(0.0, min(1.0, float(signal.get("confidence") or 0)))
    signal["risk_score"] = risk_for_signal(
        stage=str(signal.get("stage") or "mention"),
        mechanisms=list(signal.get("mechanisms") or ["other"]),
        posture=str(signal.get("posture") or "unknown"),
        confidence=confidence,
        activity_date=signal.get("meeting_date"),
        signal_kind=str(metadata.get("signal_kind") or "other_material"),
        action_outcome=str(metadata.get("action_outcome") or "unknown"),
    )
    signal["sentiment"] = sentiment_for_signal(
        str(signal.get("posture") or "unknown"),
        confidence,
        str(signal.get("stage") or "mention"),
    )


def _assessment_for(signals: list[dict[str, Any]], suppressed_foreign: int = 0) -> str:
    if not signals:
        base = "No target-specific event remains after deterministic jurisdiction and relevance cleanup."
        if suppressed_foreign:
            base += " External-county material was retained in source history but excluded from this county's risk signal set."
        return base
    restrictive = [
        signal
        for signal in signals
        if signal.get("metadata", {}).get("signal_kind") in {"local_restriction", "local_regulatory_process"}
    ]
    adopted = [
        signal
        for signal in restrictive
        if signal.get("stage") in {"adopted", "enforcement"}
        or signal.get("metadata", {}).get("action_outcome") in {"adopted", "enforced"}
    ]
    topics = sorted({str(signal.get("topic")) for signal in signals})
    topic_text = ", ".join(topic.replace("_", " ") for topic in topics)
    thread_word = "thread" if len(signals) == 1 else "threads"
    verb = "remains" if len(signals) == 1 else "remain"
    parts = [
        f"{len(signals)} validated target-specific {thread_word} {verb} across {topic_text}."
    ]
    if adopted:
        parts.append("At least one adopted or enforced local restrictive action is supported by a direct county record.")
    elif restrictive:
        parts.append("Local regulatory activity is present, but the retained records remain at inquiry, study, notice, hearing, drafting, or other pre-adoption stages.")
    else:
        parts.append("No local restrictive action is validated in the retained evidence.")
    if any(signal.get("metadata", {}).get("signal_kind") == "state_policy_advocacy" for signal in signals):
        parts.append("State-policy advocacy is kept separate from county-imposed regulation.")
    if any(signal.get("metadata", {}).get("signal_kind") == "project_facilitation" for signal in signals):
        parts.append("Project-facilitation records do not inflate restrictive risk.")
    if suppressed_foreign:
        parts.append("External-county instruments were excluded from this county's risk calculation.")
    return " ".join(parts)[:1200]


def _sync_county_jobs(
    db: Database,
    cleaned_signals: list[dict[str, Any]],
    changed_counties: set[str],
    suppressed_foreign_by_county: dict[str, int],
) -> None:
    if not changed_counties:
        return
    if not db.scalar(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='deep_county_jobs'",
        default=None,
    ):
        return
    by_county: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for signal in cleaned_signals:
        by_county[str(signal.get("county_fips"))].append(signal)

    for county_fips in sorted(changed_counties):
        row = db.one(
            "SELECT result_json FROM deep_county_jobs WHERE county_fips=? AND status='final'",
            (county_fips,),
        )
        if not row:
            continue
        county_signals = by_county.get(county_fips, [])
        clusters: list[dict[str, Any]] = []
        for signal in county_signals:
            metadata = signal.get("metadata", {})
            member_ids = [str(value) for value in metadata.get("member_ids", []) if str(value)]
            if not member_ids:
                continue
            canonical = str(metadata.get("canonical_member_id") or member_ids[0])
            if canonical not in member_ids:
                canonical = member_ids[0]
            clusters.append(
                {
                    "member_ids": member_ids,
                    "canonical_member_id": canonical,
                    "topic": signal.get("topic"),
                    "signal_kind": metadata.get("signal_kind"),
                    "posture": signal.get("posture"),
                    "stage": signal.get("stage"),
                    "mechanisms": signal.get("mechanisms") or ["other"],
                    "project_name": metadata.get("project_name"),
                    "title": signal.get("title"),
                    "summary": signal.get("summary"),
                    "confidence": signal.get("confidence"),
                    "explicit_action": bool(signal.get("explicit_action")),
                    "action_outcome": metadata.get("action_outcome"),
                    "authority_caveat": signal.get("authority_caveat"),
                }
            )
        result = {
            "assessment": _assessment_for(
                county_signals,
                suppressed_foreign=suppressed_foreign_by_county.get(county_fips, 0),
            ),
            "clusters": clusters,
            "cleanup_version": CLEANUP_VERSION,
        }
        db.execute(
            "UPDATE deep_county_jobs SET result_json=?,updated_at=? WHERE county_fips=?",
            (json_dumps(result), utcnow(), county_fips),
        )


def _write_report(db: Database, report: dict[str, Any]) -> Path | None:
    # Find the project root through the database path. The normal location is var/
    # countywatch.sqlite3; a custom DB still receives a sibling cleanup report.
    try:
        base = Path(db.path).resolve().parent / "final_cleanup"
    except Exception:
        return None
    base.mkdir(parents=True, exist_ok=True)
    latest = base / "latest-report.json"
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    latest.write_text(payload, encoding="utf-8")
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    archive = base / f"cleanup-{stamp}.json"
    archive.write_text(payload, encoding="utf-8")
    archives = sorted(base.glob("cleanup-*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for stale in archives[10:]:
        try:
            stale.unlink()
        except OSError:
            pass
    return latest


def cleanup_consolidated_signals(
    db: Database,
    signals: list[dict[str, Any]],
    county_results: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Apply deterministic final QA before active signals are written and exported."""
    county_rows = db.query("SELECT fips,name FROM counties")
    county_names = {normalize_space(str(row["name"])) for row in county_rows}
    name_by_fips = {str(row["fips"]): normalize_space(str(row["name"])) for row in county_rows}

    report: dict[str, Any] = {
        "cleanup_version": CLEANUP_VERSION,
        "generated_at": utcnow(),
        "input_signals": len(signals),
        "foreign_context_suppressed": [],
        "local_moratoria_corrected": [],
        "dates_changed": [],
        "durable_urls_selected": [],
        "supporting_quotes_aligned": [],
        "low_value_context_hidden": [],
        "duplicates_merged": [],
    }
    changed_counties: set[str] = set()
    suppressed_foreign_by_county: dict[str, int] = defaultdict(int)
    cleaned: list[dict[str, Any]] = []

    for original in signals:
        signal = dict(original)
        signal["metadata"] = dict(original.get("metadata", {}))
        signal["mechanisms"] = list(original.get("mechanisms") or [])
        county_fips = str(signal.get("county_fips") or "")
        county_name = name_by_fips.get(county_fips, "")
        signal["county_name"] = county_name

        foreign_reason = _foreign_context_reason(signal, county_names)
        if foreign_reason:
            report["foreign_context_suppressed"].append(
                {
                    "id": signal.get("id"),
                    "county": county_name,
                    "title": signal.get("title"),
                    "reason": foreign_reason,
                }
            )
            changed_counties.add(county_fips)
            suppressed_foreign_by_county[county_fips] += 1
            continue

        if _is_low_value_context(signal):
            report["low_value_context_hidden"].append(
                {
                    "id": signal.get("id"),
                    "county": county_name,
                    "title": signal.get("title"),
                }
            )
            changed_counties.add(county_fips)
            continue

        if _correct_local_moratorium(signal):
            report["local_moratoria_corrected"].append(
                {
                    "id": signal.get("id"),
                    "county": county_name,
                    "title": signal.get("title"),
                }
            )
            changed_counties.add(county_fips)

        url_changed, quote_aligned = _align_and_choose_source(signal)
        if url_changed:
            report["durable_urls_selected"].append(
                {
                    "id": signal.get("id"),
                    "county": county_name,
                    "url": signal.get("source_url"),
                }
            )
        if quote_aligned:
            report["supporting_quotes_aligned"].append(
                {
                    "id": signal.get("id"),
                    "county": county_name,
                    "title": signal.get("title"),
                }
            )

        resolved_date, date_changed, date_reason = _resolve_signal_date(signal)
        if date_changed:
            report["dates_changed"].append(
                {
                    "id": signal.get("id"),
                    "county": county_name,
                    "old": original.get("meeting_date"),
                    "new": resolved_date,
                    "reason": date_reason,
                }
            )
        signal["meeting_date"] = resolved_date
        signal["metadata"]["cleanup_version"] = CLEANUP_VERSION
        signal["metadata"]["date_source"] = date_reason
        _recompute_signal_score(signal)
        cleaned.append(signal)

    cleaned, merges = _merge_conservative_duplicates(cleaned)
    report["duplicates_merged"] = merges
    for merge in merges:
        county = str(merge.get("county") or "")
        for fips, name in name_by_fips.items():
            if name == county:
                changed_counties.add(fips)
                break

    # Source/date fixes do not change the assessment. Jurisdiction, classification,
    # suppression, and merges do, so only those counties receive a deterministic rewrite.
    _sync_county_jobs(db, cleaned, changed_counties, suppressed_foreign_by_county)

    for signal in cleaned:
        signal.pop("county_name", None)

    report["output_signals"] = len(cleaned)
    report["counts"] = {
        key: len(value)
        for key, value in report.items()
        if isinstance(value, list)
    }
    report_path = _write_report(db, report)
    if report_path:
        report["report_path"] = str(report_path)
    return cleaned, county_results


def republish(settings: Settings) -> int:
    # Importing here avoids a circular import when deep_rebuild calls the cleanup
    # function above during its own cutover.
    from .deep_rebuild import (
        acquire_run_lock,
        cutover,
        ensure_deep_schema,
        release_run_lock,
    )

    settings.ensure_directories()
    lock = acquire_run_lock(settings)
    db = Database(settings.database)
    try:
        ensure_deep_schema(db)
        pending_docs = int(db.scalar("SELECT count(*) FROM deep_jobs WHERE status<>'final'", default=0) or 0)
        pending_counties = int(
            db.scalar("SELECT count(*) FROM deep_county_jobs WHERE status<>'final'", default=0) or 0
        )
        if pending_docs or pending_counties:
            console.print(
                Panel.fit(
                    f"The cleanup cannot republish yet: {pending_docs} document jobs and "
                    f"{pending_counties} county jobs are not final.",
                    title="Final cleanup stopped safely",
                    border_style="yellow",
                )
            )
            return 1
        result = cutover(settings, db)
        report_path = settings.database.parent / "final_cleanup" / "latest-report.json"
        report = json_loads(report_path.read_text(encoding="utf-8") if report_path.exists() else "{}", {})
        counts = report.get("counts", {})
        table = Table(title="Final deterministic cleanup")
        table.add_column("Fix")
        table.add_column("Count", justify="right")
        table.add_row("External-county context removed from risk", str(counts.get("foreign_context_suppressed", 0)))
        table.add_row("Local moratoria reclassified", str(counts.get("local_moratoria_corrected", 0)))
        table.add_row("Dates corrected/cleared", str(counts.get("dates_changed", 0)))
        table.add_row("Durable canonical URLs selected", str(counts.get("durable_urls_selected", 0)))
        table.add_row("Supporting quotes realigned", str(counts.get("supporting_quotes_aligned", 0)))
        table.add_row("Low-value context hidden", str(counts.get("low_value_context_hidden", 0)))
        table.add_row("Conservative duplicate merges", str(counts.get("duplicates_merged", 0)))
        table.add_row("Published signals", str(result.get("signals", 0)))
        console.print(table)
        console.print(
            Panel.fit(
                f"Dashboard republished atomically.\nDatabase backup: {result.get('backup')}\n"
                f"Cleanup report: {report_path}",
                title="Final cleanup complete",
                border_style="green",
            )
        )
        return 0
    except Exception as exc:
        console.print(Panel.fit(str(exc), title="Final cleanup error", border_style="red"))
        return 1
    finally:
        db.close()
        release_run_lock(lock)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic final QA for CountyWatch signals.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("republish", help="Rebuild active signals from saved checkpoints, clean them, and publish.")
    args = parser.parse_args(argv)
    command = args.command or "republish"
    settings = Settings.load()
    if command == "republish":
        return republish(settings)
    parser.error(f"Unknown command: {command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
