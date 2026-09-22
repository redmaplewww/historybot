from typing import Literal

from pydantic import BaseModel, Field

from .domain import EntityMention, EventCandidate


class IdentityLink(BaseModel):
    canonical_surface: str
    alias_surface: str
    relation: str = Field(min_length=1, max_length=20)
    passage_id: str
    confidence: float = Field(ge=0, le=1)


class ClaimDraft(BaseModel):
    id: str
    subject: str
    predicate: str
    object: str
    statement: str
    passage_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)


class ExtractionResponse(BaseModel):
    mentions: list[EntityMention] = Field(default_factory=list)
    identity_links: list[IdentityLink] = Field(default_factory=list)
    events: list[EventCandidate] = Field(default_factory=list)
    claims: list[ClaimDraft] = Field(default_factory=list)


class PeopleExtractionResponse(BaseModel):
    mentions: list[EntityMention] = Field(default_factory=list)
    identity_links: list[IdentityLink] = Field(default_factory=list)


class ReviewResponse(BaseModel):
    claim_id: str
    evidence_sufficient: bool
    polarity: Literal["supports", "refutes", "partial", "unrelated"]
    concerns: list[str] = Field(default_factory=list)
    revised_confidence: float = Field(ge=0, le=1)
    rationale: str


class ConflictDraft(BaseModel):
    claim_ids: list[str] = Field(min_length=2)
    description: str
    severity: str = "medium"


class ContradictionResponse(BaseModel):
    conflicts: list[ConflictDraft] = Field(default_factory=list)
