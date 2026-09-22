from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .domain import EventCandidate, EntityMention
from .schemas import ClaimDraft, IdentityLink


class ReadingUnit(BaseModel):
    unit_id: str
    passage_id: str
    reading_summary: str
    explicit_facts: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    mentions: list[EntityMention] = Field(default_factory=list)
    identity_links: list[IdentityLink] = Field(default_factory=list)
    events: list[EventCandidate] = Field(default_factory=list)
    claims: list[ClaimDraft] = Field(default_factory=list)
    reasoning_summary: str
    evidence_quotes: list[str] = Field(default_factory=list)


class ReadingResponse(BaseModel):
    units: list[ReadingUnit] = Field(default_factory=list)
    document_summary: str
    unresolved_questions: list[str] = Field(default_factory=list)


class VerificationItem(BaseModel):
    unit_id: str
    passage_id: str = ""
    supported: bool
    missing: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    rationale: str


class VerificationResponse(BaseModel):
    items: list[VerificationItem] = Field(default_factory=list)
    overall_supported: bool
    summary: str

class TimeReference(BaseModel):
    claim_id: str = ""
    event_id: str = ""
    time_expression: str = ""
    time_from: str | None = None
    time_to: str | None = None
    time_precision: Literal[
        "day", "month", "year", "range", "decade", "era_only", "uncertain"
    ] = "uncertain"
    era_name: str | None = None
    era_year: int | None = None
    year: int | None = None
    month: int | None = None
    day: int | None = None
    context_basis: str = ""
    confidence: float = Field(default=0.0, ge=0, le=1)


class TemporalResponse(BaseModel):
    references: list[TimeReference] = Field(default_factory=list)
    context_summary: str
