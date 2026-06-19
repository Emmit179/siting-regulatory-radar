from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _float(value: str | None, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


def project_root() -> Path:
    override = os.getenv("COUNTYWATCH_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd().resolve()


@dataclass(slots=True)
class Settings:
    root: Path
    database: Path
    site_dir: Path
    cache_dir: Path
    document_dir: Path
    text_dir: Path
    user_agent: str
    contact_email: str
    concurrency: int
    per_host_delay: float
    request_timeout: float
    max_document_bytes: int
    max_documents_per_source: int
    max_documents_per_county: int
    initial_lookback_days: int
    max_run_minutes: int
    discovery_refresh_days: int
    directory_refresh_days: int
    enable_browser: bool
    allow_insecure_tls: bool
    enable_ocr: bool
    max_ocr_pages: int
    enable_youtube: bool
    llm_enabled: bool
    llm_provider_order: tuple[str, ...]
    llm_max_calls: int
    verify_high_risk: bool
    groq_api_key: str | None
    groq_model: str
    groq_verify_model: str
    gemini_api_key: str | None
    gemini_model: str
    gemini_verify_model: str
    openrouter_api_key: str | None
    openrouter_model: str
    github_repository: str | None

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Settings":
        root = project_root()
        load_dotenv(env_file or root / ".env", override=False)
        database = Path(os.getenv("COUNTYWATCH_DB", root / "var" / "countywatch.sqlite3"))
        site_dir = Path(os.getenv("COUNTYWATCH_SITE_DIR", root / "site"))
        cache_dir = Path(os.getenv("COUNTYWATCH_CACHE_DIR", root / "var" / "cache"))
        document_dir = Path(os.getenv("COUNTYWATCH_DOCUMENT_DIR", root / "var" / "documents"))
        text_dir = Path(os.getenv("COUNTYWATCH_TEXT_DIR", root / "var" / "text"))
        order = tuple(
            x.strip().lower()
            for x in os.getenv("LLM_PROVIDER_ORDER", "groq,gemini,openrouter").split(",")
            if x.strip()
        )
        return cls(
            root=root,
            database=database.expanduser().resolve(),
            site_dir=site_dir.expanduser().resolve(),
            cache_dir=cache_dir.expanduser().resolve(),
            document_dir=document_dir.expanduser().resolve(),
            text_dir=text_dir.expanduser().resolve(),
            user_agent=os.getenv(
                "COUNTYWATCH_USER_AGENT",
                "TexasCountyRegulatoryRadar/1.0 (+public-records-research; respectful crawler)",
            ),
            contact_email=os.getenv("COUNTYWATCH_CONTACT_EMAIL", ""),
            concurrency=max(1, _int(os.getenv("COUNTYWATCH_CONCURRENCY"), 8)),
            per_host_delay=max(0.2, _float(os.getenv("COUNTYWATCH_HOST_DELAY_SECONDS"), 1.2)),
            request_timeout=max(10, _float(os.getenv("COUNTYWATCH_REQUEST_TIMEOUT_SECONDS"), 35)),
            max_document_bytes=max(1_000_000, _int(os.getenv("COUNTYWATCH_MAX_DOCUMENT_BYTES"), 40_000_000)),
            max_documents_per_source=max(5, _int(os.getenv("COUNTYWATCH_MAX_DOCUMENTS_PER_SOURCE"), 80)),
            max_documents_per_county=max(10, _int(os.getenv("COUNTYWATCH_MAX_DOCUMENTS_PER_COUNTY"), 160)),
            initial_lookback_days=max(30, _int(os.getenv("COUNTYWATCH_INITIAL_LOOKBACK_DAYS"), 730)),
            max_run_minutes=max(0, _int(os.getenv("COUNTYWATCH_MAX_RUN_MINUTES"), 0)),
            discovery_refresh_days=max(1, _int(os.getenv("COUNTYWATCH_DISCOVERY_REFRESH_DAYS"), 30)),
            directory_refresh_days=max(1, _int(os.getenv("COUNTYWATCH_DIRECTORY_REFRESH_DAYS"), 45)),
            enable_browser=_bool(os.getenv("COUNTYWATCH_BROWSER_FALLBACK"), True),
            allow_insecure_tls=_bool(os.getenv("COUNTYWATCH_ALLOW_INSECURE_TLS"), False),
            enable_ocr=_bool(os.getenv("COUNTYWATCH_OCR"), True),
            max_ocr_pages=max(1, _int(os.getenv("COUNTYWATCH_MAX_OCR_PAGES"), 80)),
            enable_youtube=_bool(os.getenv("COUNTYWATCH_YOUTUBE_TRANSCRIPTS"), True),
            llm_enabled=_bool(os.getenv("COUNTYWATCH_LLM_ENABLED"), True),
            llm_provider_order=order,
            llm_max_calls=max(0, _int(os.getenv("COUNTYWATCH_LLM_MAX_CALLS_PER_RUN"), 450)),
            verify_high_risk=_bool(os.getenv("COUNTYWATCH_VERIFY_HIGH_RISK"), True),
            groq_api_key=os.getenv("GROQ_API_KEY") or None,
            groq_model=os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
            groq_verify_model=os.getenv("GROQ_VERIFY_MODEL", "openai/gpt-oss-120b"),
            gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite"),
            gemini_verify_model=os.getenv("GEMINI_VERIFY_MODEL", "gemini-2.5-flash"),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY") or None,
            openrouter_model=os.getenv("OPENROUTER_MODEL", "openrouter/free"),
            github_repository=os.getenv("GITHUB_REPOSITORY") or None,
        )

    def ensure_directories(self) -> None:
        for path in (
            self.database.parent,
            self.site_dir,
            self.cache_dir,
            self.document_dir,
            self.text_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def configured_providers(self) -> list[str]:
        keys = {
            "groq": self.groq_api_key,
            "gemini": self.gemini_api_key,
            "openrouter": self.openrouter_api_key,
        }
        return [provider for provider in self.llm_provider_order if keys.get(provider)]

    def redacted(self) -> dict[str, object]:
        return {
            "database": str(self.database),
            "site_dir": str(self.site_dir),
            "concurrency": self.concurrency,
            "per_host_delay": self.per_host_delay,
            "max_run_minutes": self.max_run_minutes,
            "browser": self.enable_browser,
            "allow_insecure_tls": self.allow_insecure_tls,
            "ocr": self.enable_ocr,
            "llm_enabled": self.llm_enabled,
            "providers": self.configured_providers(),
        }
