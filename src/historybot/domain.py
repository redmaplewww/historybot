from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SourceKind(StrEnum):
    PRIMARY = "primary"
    CONTEMPORARY = "contemporary"
    COMPILATION = "compilation"
    MODERN_STUDY = "modern_study"
    FOLK = "folk"
    UNKNOWN = "unknown"


class ClaimStatus(StrEnum):
    EXTRACTED = "extracted"
    EVIDENCE_LINKED = "evidence_linked"
    MACHINE_REVIEWED = "machine_reviewed"
    HUMAN_REVIEWED = "human_reviewed"
    PROMOTED = "promoted"
    DISPUTED = "disputed"
    REFUTED = "refuted"
    SUPERSEDED = "superseded"


class Polarity(StrEnum):
    SUPPORTS = "supports"
    REFUTES = "refutes"
    PARTIAL = "partial"
    UNRELATED = "unrelated"


class Confidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score: float = Field(ge=0, le=1)
    source_reliability: float = Field(ge=0, le=1)
    extraction_quality: float = Field(ge=0, le=1)
    resolution_quality: float = Field(ge=0, le=1)
    corroboration: float = Field(ge=0, le=1)
    contradiction_penalty: float = Field(ge=0, le=1)
    rationale: str


class Source(BaseModel):
    id: str
    title: str
    author: str | None = None
    url: str | None = None
    kind: SourceKind = SourceKind.UNKNOWN
    composition_date: str | None = None
    edition: str | None = None
    access_date: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


class Passage(BaseModel):
    id: str
    source_id: str
    ordinal: int
    text: str
    locator: dict[str, Any] = Field(default_factory=dict)
    text_hash: str


class EntityMention(BaseModel):
    surface: str
    entity_type: str
    passage_id: str
    canonical_id: str | None = None
    confidence: float = Field(ge=0, le=1)


class EventCandidate(BaseModel):
    event_type: str
    label: str
    date_start: str | None = None
    date_end: str | None = None
    place: str | None = None
    participant_surfaces: list[str] = Field(default_factory=list)
    passage_id: str
    confidence: float = Field(ge=0, le=1)


class Claim(BaseModel):
    id: str
    subject: str
    predicate: str
    object: str
    statement: str
    status: ClaimStatus = ClaimStatus.EXTRACTED
    confidence: Confidence
    passage_ids: list[str] = Field(min_length=1)
    event_id: str | None = None
    time_from: str | None = None
    time_to: str | None = None
    time_precision: str | None = None
    time_expression: str | None = None
    inference_method: str = "explicit"
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidenceLink(BaseModel):
    id: str
    claim_id: str
    passage_id: str
    polarity: Polarity
    quote: str
    reasoning: str
    independent_source: bool = False
    confidence: float = Field(ge=0, le=1)


class Conflict(BaseModel):
    id: str
    claim_ids: list[str] = Field(min_length=2)
    description: str
    severity: str
    status: str = "open"
    evidence_ids: list[str] = Field(default_factory=list)


class ExtractedArtifacts(BaseModel):
    mentions: list[EntityMention] = Field(default_factory=list)
    events: list[EventCandidate] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)


class ResearchQuestion(BaseModel):
    text: str
    scope: str | None = None
    language: str = "zh"


class IngestionStatus(StrEnum):
    RECEIVED = "received"
    RECOGNIZED = "recognized"
    NORMALIZED = "normalized"
    ANNOTATED = "annotated"
    ASSESSED = "assessed"
    DRAFT_SAVED = "draft_saved"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    REVIEW_REQUIRED = "review_required"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FAILED = "failed"


class EvidenceKind(StrEnum):
    OBSERVED = "observed"
    NORMALIZED = "normalized"
    INFERRED = "inferred"


class IngestionJob(BaseModel):
    id: str
    status: IngestionStatus = IngestionStatus.RECEIVED
    input_type: str
    raw_asset_id: str
    template: str = "historical"
    options: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    current_artifact_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RawAsset(BaseModel):
    id: str
    name: str
    media_type: str
    content_hash: str
    size: int = Field(ge=0)
    uri: str | None = None
    content: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RecognitionSpan(BaseModel):
    text: str
    page: int | None = None
    block: int | None = None
    bbox: list[float] | None = None
    confidence: float = Field(ge=0, le=1)
    source: str = "text"


class RecognitionResult(BaseModel):
    id: str
    raw_asset_id: str
    text: str
    spans: list[RecognitionSpan] = Field(default_factory=list)
    language: str = "und"
    processor: str
    processor_version: str = "1"
    confidence: float = Field(ge=0, le=1)
    needs_review: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class NormalizedDocument(BaseModel):
    id: str
    recognition_id: str
    text: str
    paragraphs: list[dict[str, Any]] = Field(default_factory=list)
    tables: list[dict[str, Any]] = Field(default_factory=list)
    changes: list[dict[str, Any]] = Field(default_factory=list)
    processor: str = "normalizer"
    processor_version: str = "1"


class Annotation(BaseModel):
    id: str
    label: str
    value: str
    start: int
    end: int
    evidence_kind: EvidenceKind = EvidenceKind.OBSERVED
    confidence: float = Field(ge=0, le=1)
    source: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class AnnotationSet(BaseModel):
    id: str
    document_id: str
    annotations: list[Annotation] = Field(default_factory=list)
    processor: str = "rules"
    processor_version: str = "1"


class CompletenessAssessment(BaseModel):
    id: str
    annotation_set_id: str
    template: str
    required_dimensions: list[str]
    present_dimensions: list[str]
    missing_dimensions: list[str] = Field(default_factory=list)
    conflict_dimensions: list[str] = Field(default_factory=list)
    score: float = Field(ge=0, le=1)
    blocking: bool = False
    rationale: str = ""


class ClarificationQuestion(BaseModel):
    id: str
    job_id: str
    field: str
    question: str
    reason: str
    priority: int = Field(ge=1, le=5)
    blocking: bool = True
    candidates: list[str] = Field(default_factory=list)
    status: str = "open"
    answer: str | None = None
    source_annotation_ids: list[str] = Field(default_factory=list)


class ReviewTask(BaseModel):
    id: str
    job_id: str
    kind: str
    target_id: str
    reason: str
    status: str = "open"
    reviewer: str | None = None
    decision: str | None = None


class IngestionArtifact(BaseModel):
    id: str
    job_id: str
    kind: str
    input_hash: str
    processor: str
    processor_version: str
    payload: dict[str, Any]
    parent_id: str | None = None
