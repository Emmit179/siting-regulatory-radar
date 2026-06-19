from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Topic(StrEnum):
    SOLAR = "solar"
    DATA_CENTER = "data_center"
    BESS = "bess"
    WIND = "wind"
    GENERAL_LAND_USE = "general_land_use"


class Posture(StrEnum):
    RESTRICTIVE = "restrictive"
    SUPPORTIVE = "supportive"
    NEUTRAL = "neutral"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class Stage(StrEnum):
    MENTION = "mention"
    STUDY = "study"
    STAFF_DIRECTION = "staff_direction"
    DRAFTING = "drafting"
    PUBLIC_NOTICE = "public_notice"
    PUBLIC_HEARING = "public_hearing"
    INTRODUCTION = "introduction"
    ADOPTED = "adopted"
    ENFORCEMENT = "enforcement"
    RESCINDED = "rescinded"


class Mechanism(StrEnum):
    MORATORIUM = "moratorium"
    PROHIBITION = "prohibition"
    ZONING = "zoning"
    ORDINANCE = "ordinance"
    PERMITTING = "permitting"
    SETBACKS = "setbacks"
    FIRE_SAFETY = "fire_safety"
    NOISE = "noise"
    WATER = "water"
    ROADS = "roads"
    DECOMMISSIONING = "decommissioning"
    TAX_INCENTIVE = "tax_incentive"
    DEVELOPMENT_AGREEMENT = "development_agreement"
    OTHER = "other"


@dataclass(slots=True)
class FetchResult:
    requested_url: str
    final_url: str
    status_code: int
    headers: dict[str, str]
    content: bytes
    from_browser: bool = False
    not_modified: bool = False

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";", 1)[0].lower()


@dataclass(slots=True)
class SourceCandidate:
    url: str
    title: str
    source_type: str
    platform: str
    priority: int
    discovery_method: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DocumentCandidate:
    url: str
    title: str
    document_type: str
    meeting_date: str | None = None
    published_at: str | None = None
    parent_url: str | None = None
    platform: str = "generic"
    requires_browser: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExtractionResult:
    text: str
    method: str
    page_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Passage:
    id: str
    start: int
    end: int
    text: str
    topics: list[str]
    score: float
    matched_terms: list[str]


class LLMSignal(BaseModel):
    passage_id: str
    topic: Topic
    posture: Posture
    stage: Stage
    mechanisms: list[Mechanism] = Field(default_factory=list, max_length=8)
    title: str = Field(min_length=4, max_length=180)
    summary: str = Field(min_length=8, max_length=800)
    evidence_quote: str = Field(min_length=8, max_length=900)
    confidence: float = Field(ge=0.0, le=1.0)
    explicit_action: bool = False
    authority_caveat: str | None = Field(default=None, max_length=400)

    @field_validator("mechanisms")
    @classmethod
    def unique_mechanisms(cls, value: list[Mechanism]) -> list[Mechanism]:
        return list(dict.fromkeys(value))


class LLMDocumentResult(BaseModel):
    document_relevant: bool
    signals: list[LLMSignal] = Field(default_factory=list, max_length=12)
    document_summary: str = Field(default="", max_length=1000)


@dataclass(slots=True)
class ValidatedSignal:
    topic: str
    posture: str
    stage: str
    mechanisms: list[str]
    title: str
    summary: str
    evidence_quote: str
    evidence_start: int
    evidence_end: int
    confidence: float
    explicit_action: bool
    authority_caveat: str | None
    passage_id: str
    engine: str
    provider: str
    model: str
    risk_score: float = 0.0
    sentiment: float = 0.0
