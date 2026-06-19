from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import dateparser
import orjson

TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def today_iso() -> str:
    return date.today().isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def parse_date(value: str | None, relative_base: datetime | None = None) -> str | None:
    if not value:
        return None
    value = normalize_space(value)
    if not value or len(value) > 180:
        return None
    parsed = dateparser.parse(
        value,
        settings={
            "PREFER_DATES_FROM": "past",
            "DATE_ORDER": "MDY",
            "RELATIVE_BASE": relative_base or datetime.now(),
            "RETURN_AS_TIMEZONE_AWARE": False,
            "STRICT_PARSING": False,
        },
    )
    if not parsed or parsed.year < 1990 or parsed.year > datetime.now().year + 2:
        return None
    return parsed.date().isoformat()


def slugify(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_text(value: str) -> str:
    value = value.replace("\u00ad", "").replace("\u00a0", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def stable_id(*parts: object, length: int = 24) -> str:
    raw = "\x1f".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:length]


def canonical_url(url: str, base: str | None = None) -> str:
    url = urljoin(base or "", url.strip())
    split = urlsplit(url)
    if split.scheme not in {"http", "https"}:
        return ""
    host = split.netloc.lower()
    if host.endswith(":80") and split.scheme == "http":
        host = host[:-3]
    if host.endswith(":443") and split.scheme == "https":
        host = host[:-4]
    params = [(k, v) for k, v in parse_qsl(split.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    path = re.sub(r"/{2,}", "/", split.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((split.scheme.lower(), host, path, urlencode(params, doseq=True), ""))


def same_site(a: str, b: str) -> bool:
    ah = urlsplit(a).hostname or ""
    bh = urlsplit(b).hostname or ""
    if ah == bh:
        return True
    a_parts = ah.lower().split(".")
    b_parts = bh.lower().split(".")
    return len(a_parts) >= 2 and len(b_parts) >= 2 and a_parts[-2:] == b_parts[-2:]


def extension_for(url: str, mime: str | None = None) -> str:
    suffix = Path(urlsplit(url).path).suffix.lower()
    known = {".pdf", ".docx", ".txt", ".csv", ".html", ".htm", ".rtf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
    if suffix in known:
        return suffix
    mime = (mime or "").split(";", 1)[0].strip().lower()
    return {
        "application/pdf": ".pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "text/plain": ".txt",
        "text/csv": ".csv",
        "text/html": ".html",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/tiff": ".tif",
    }.get(mime, ".bin")


def json_dumps(value: Any, pretty: bool = False) -> str:
    option = orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS if pretty else 0
    return orjson.dumps(value, option=option, default=str).decode()


def json_loads(value: str | bytes | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return orjson.loads(value)
    except orjson.JSONDecodeError:
        return default


def extract_json_object(value: str) -> dict[str, Any]:
    value = value.strip()
    value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.I | re.S)
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start = value.find("{")
    if start < 0:
        raise ValueError("Model response did not contain a JSON object")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(value)):
        char = value[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(value[start : index + 1])
    raise ValueError("Model response contained incomplete JSON")
