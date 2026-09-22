from __future__ import annotations

import base64
import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..domain import (
    Annotation,
    AnnotationSet,
    ClarificationQuestion,
    EvidenceKind,
    IngestionArtifact,
    IngestionJob,
    IngestionStatus,
    NormalizedDocument,
    RawAsset,
    ReviewTask,
    Source,
    SourceKind,
)
from ..ingest import split_passages
from ..store import KnowledgeStore
from .cleaning import normalize_document
from .completeness import CompletenessChecker
from .providers import (
    HtmlReader,
    InputReader,
    OfficeReader,
    PdfReader,
    PlainTextReader,
    RapidOCRProvider,
    TextOCRProvider,
    TrOCRProvider,
    UrlReader,
    default_ocr_provider,
    detect_media_type,
)


class IngestionService:
    """Coordinates ingestion stages while preserving every intermediate artifact."""

    def __init__(self, store: KnowledgeStore, *, readers: list[InputReader] | None = None, ocr: Any | None = None, checker: CompletenessChecker | None = None):
        self.store = store
        self.readers = readers or [UrlReader(), HtmlReader(), OfficeReader(), PdfReader(), PlainTextReader()]
        self.ocr = ocr or default_ocr_provider()
        self.checker = checker or CompletenessChecker()

    async def submit(self, source: str | bytes | Path, *, name: str | None = None, media_type: str | None = None, template: str = "historical", options: dict[str, Any] | None = None) -> IngestionJob:
        uri = str(source) if isinstance(source, (str, Path)) else None
        if isinstance(source, Path) or (isinstance(source, str) and not source.startswith(("http://", "https://")) and Path(source).exists()):
            path = Path(source)
            content = path.read_bytes()
            asset_name = name or path.name
        elif isinstance(source, str):
            content = source.encode("utf-8")
            asset_name = name or "input.txt"
        elif isinstance(source, bytes):
            content = source
            asset_name = name or "input.bin"
        else:
            content = b""
            asset_name = name or str(source)
        detected = media_type or detect_media_type(asset_name, content)
        content_hash = hashlib.sha256(content if content else asset_name.encode()).hexdigest()
        asset_id = f"asset_{content_hash[:20]}"
        metadata = {"template": template}
        if content and not detected.startswith("text/"):
            metadata["content_b64"] = base64.b64encode(content).decode("ascii")
        asset = RawAsset(id=asset_id, name=asset_name, media_type=detected, content_hash=content_hash, size=len(content), uri=uri, content=content.decode("utf-8", errors="replace") if content and detected.startswith("text/") else None, metadata=metadata)
        self.store.save_raw_asset(asset)
        job_id = f"job_{content_hash[:20]}"
        existing = self.store.get_ingestion_job(job_id)
        if existing:
            return existing
        job = IngestionJob(id=job_id, input_type=detected, raw_asset_id=asset_id, template=template, options=options or {})
        self.store.save_ingestion_job(job)
        return job

    async def run(self, job_id: str | IngestionJob, *, until_stage: IngestionStatus | None = None) -> IngestionJob:
        job = job_id if isinstance(job_id, IngestionJob) else self.store.get_ingestion_job(job_id)
        if not job:
            raise KeyError(f"unknown ingestion job: {job_id}")
        if until_stage is None and job.status in {
            IngestionStatus.ACCEPTED,
            IngestionStatus.REVIEW_REQUIRED,
            IngestionStatus.AWAITING_CLARIFICATION,
        } and self.store.get_source_for_ingestion_job(job.id):
            return job
        asset = self.store.get_raw_asset(job.raw_asset_id)
        if asset is None:
            raise RuntimeError("raw asset is missing")
        try:
            raw = await self._recognize(asset, job)
            self.store.save_recognition(job.id, raw)
            self._artifact(job, "recognition", raw.model_dump(mode="json"), raw.id)
            job = self._advance(job, IngestionStatus.RECOGNIZED, raw.id)
            if until_stage == IngestionStatus.RECOGNIZED:
                return job
            normalized = normalize_document(raw)
            self.store.save_normalized(job.id, normalized)
            self._artifact(job, "normalized", normalized.model_dump(mode="json"), normalized.id)
            job = self._advance(job, IngestionStatus.NORMALIZED, normalized.id)
            if until_stage == IngestionStatus.NORMALIZED:
                return job
            annotations = self._annotate(normalized)
            self.store.save_annotations(job.id, annotations)
            self._artifact(job, "annotations", annotations.model_dump(mode="json"), annotations.id)
            job = self._advance(job, IngestionStatus.ANNOTATED, annotations.id)
            assessment = self.checker.assess(annotations, job.template)
            self.store.save_assessment(job.id, assessment)
            self._artifact(job, "assessment", assessment.model_dump(mode="json"), assessment.id)
            job = self._advance(job, IngestionStatus.ASSESSED, assessment.id)
            self._save_source_passages(job, asset, normalized)
            if raw.needs_review:
                self.store.save_review_task(ReviewTask(id=f"review_{job.id}_recognition", job_id=job.id, kind="recognition", target_id=raw.id, reason="识别置信度不足或未识别到文本"))
            for question in self._questions(job, annotations, assessment):
                self.store.save_question(question)
            job = self._advance(job, IngestionStatus.REVIEW_REQUIRED if raw.needs_review else (IngestionStatus.AWAITING_CLARIFICATION if assessment.blocking else IngestionStatus.ACCEPTED), assessment.id)
            return job
        except Exception as exc:
            job.error = str(exc)
            job.status = IngestionStatus.FAILED
            job.updated_at = datetime.now(UTC)
            self.store.save_ingestion_job(job)
            self.store.save_processing_error(
                f"error_{job.id}",
                job.id,
                job.status.value,
                {"error_type": type(exc).__name__, "message": str(exc)},
            )
            raise

    def answer_question(self, question_id: str, answer: str, *, reviewer: str = "user") -> IngestionJob:
        row = self.store.db.execute("SELECT payload FROM clarification_questions WHERE id=?", (question_id,)).fetchone()
        if not row:
            raise KeyError(f"unknown clarification question: {question_id}")
        question = ClarificationQuestion.model_validate_json(row[0])
        question.answer = answer
        question.status = "answered"
        question.reason = f"{question.reason}；由 {reviewer} 确认"
        self.store.save_question(question)
        annotations = self.store.get_annotations(question.job_id)
        if annotations:
            annotations.annotations.append(Annotation(id=f"ann_user_{question.id}", label=question.field, value=answer, start=0, end=0, evidence_kind=EvidenceKind.NORMALIZED, confidence=1.0, source="user", metadata={"question_id": question.id}))
            self.store.save_annotations(question.job_id, annotations)
            assessment = self.checker.assess(annotations, self.store.get_ingestion_job(question.job_id).template)
            self.store.save_assessment(question.job_id, assessment)
        job = self.store.get_ingestion_job(question.job_id)
        if not job:
            raise KeyError(question.job_id)
        open_questions = self.store.get_questions(job.id, status="open")
        job.status = IngestionStatus.AWAITING_CLARIFICATION if open_questions else IngestionStatus.ACCEPTED
        job.updated_at = datetime.now(UTC)
        self.store.save_ingestion_job(job)
        return job

    async def _recognize(self, asset: RawAsset, job: IngestionJob):
        content = (asset.content or "").encode("utf-8") if asset.content is not None else base64.b64decode(asset.metadata.get("content_b64", ""))
        for reader in self.readers:
            if reader.supports(asset.media_type, asset.name if not asset.uri else asset.uri):
                return await reader.read(content, name=asset.uri or asset.name, media_type=asset.media_type, asset_id=asset.id)
        provider_name = job.options.get("ocr_provider")
        provider = self.ocr
        if provider_name == "rapidocr":
            provider = RapidOCRProvider()
        elif provider_name == "trocr-printed":
            provider = TrOCRProvider("microsoft/trocr-small-printed")
        elif provider_name == "trocr-handwritten":
            provider = TrOCRProvider("microsoft/trocr-small-handwritten")
        elif provider_name == "none":
            provider = TextOCRProvider()
        elif provider_name not in (None, ""):
            raise ValueError(f"unknown ocr_provider: {provider_name}")
        return await provider.recognize(content, name=asset.name, media_type=asset.media_type, asset_id=asset.id)

    def _annotate(self, document: NormalizedDocument) -> AnnotationSet:
        text = document.text
        annotations: list[Annotation] = []
        def add(label: str, value: str, start: int, end: int, confidence: float = 0.8, kind: EvidenceKind = EvidenceKind.OBSERVED, **metadata: Any):
            annotations.append(Annotation(id=f"ann_{hashlib.sha256(f'{label}:{start}:{end}:{value}'.encode()).hexdigest()[:16]}", label=label, value=value, start=start, end=end, confidence=confidence, evidence_kind=kind, source="rules", metadata=metadata))
        if text:
            add("evidence", text[:500], 0, min(len(text), 500), 1.0)
        for match in re.finditer(r"(?:公元前|公元)?\d{1,4}(?:年|年间|年初|年末)|建安(?:年间|元年)?", text):
            add("time", match.group(), match.start(), match.end(), 0.9)
        for match in re.finditer(r"(?:[北京上海南京洛阳成都许昌襄阳赤壁长安]市?|郡|县|州)", text):
            add("place", match.group(), match.start(), match.end(), 0.65)
        # Names are deliberately conservative; uncertain candidates become reviewable annotations.
        for match in re.finditer(r"(?<![\u4e00-\u9fff])[\u4e00-\u9fff]{2,4}(?![\u4e00-\u9fff])", text):
            value = match.group()
            if value not in {"历史资料", "相关内容", "公元前", "建安年间"}:
                add("person", value, match.start(), match.end(), 0.55, metadata={"candidate": True})
        if text:
            add("event", "待确认事件", 0, 0, 0.2, EvidenceKind.INFERRED, reason="文本未能可靠抽取事件类型")
        return AnnotationSet(id=f"annset_{document.id[5:]}", document_id=document.id, annotations=annotations)

    def _questions(self, job: IngestionJob, annotations: AnnotationSet, assessment) -> list[ClarificationQuestion]:
        questions: list[ClarificationQuestion] = []
        for field in assessment.missing_dimensions:
            prompts = {"source": "请补充该材料的明确出处或档案编号。", "author": "请确认作者、编者或记录者。", "source_type": "请确认来源类型（正史、原始记录、研究、文学/民间等）。", "time": "请确认材料涉及的时间或时间范围。", "place": "请确认涉及的地点及其历史名称。", "person": "请确认材料中的人物/组织名称。", "event": "请确认材料描述的事件或主题。", "evidence": "请指出支持该信息的原文段落或页码。"}
            questions.append(ClarificationQuestion(id=f"q_{job.id}_{field}", job_id=job.id, field=field, question=prompts.get(field, f"请补充字段：{field}"), reason=assessment.rationale, priority=1 if field in {"source", "evidence", "event"} else 2, blocking=True))
        for annotation in annotations.annotations:
            if annotation.evidence_kind is EvidenceKind.INFERRED:
                questions.append(ClarificationQuestion(id=f"q_{job.id}_inferred_{annotation.label}", job_id=job.id, field=annotation.label, question=f"系统无法从原文确认‘{annotation.label}’，请提供明确内容或确认保留为空。", reason="该值是推断结果，不能直接作为事实", priority=1, blocking=True, source_annotation_ids=[annotation.id]))
        if assessment.conflict_dimensions:
            for field in assessment.conflict_dimensions:
                questions.append(ClarificationQuestion(id=f"q_{job.id}_conflict_{field}", job_id=job.id, field=field, question=f"字段‘{field}’存在冲突，请确认采用哪个版本。", reason="同一字段有互相矛盾的识别结果", priority=1, blocking=True))
        return questions

    def _save_source_passages(self, job: IngestionJob, asset: RawAsset, document: NormalizedDocument) -> None:
        source = Source(id=f"src_{job.id[4:]}", title=asset.name, url=asset.uri, kind=SourceKind.UNKNOWN, access_date=datetime.now(UTC), metadata={"ingestion_job_id": job.id, "completeness_pending": True})
        self.store.save_source(source, job.id)
        self.store.save_passages(split_passages(document.text, source_id=source.id), job.id)

    def _artifact(self, job: IngestionJob, kind: str, payload: dict, parent_id: str | None) -> None:
        serialized = str(payload).encode()
        artifact = IngestionArtifact(id=f"artifact_{job.id}_{kind}", job_id=job.id, kind=kind, input_hash=hashlib.sha256(serialized).hexdigest(), processor=f"ingestion.{kind}", processor_version="1", payload=payload, parent_id=parent_id)
        self.store.save_ingestion_artifact(artifact)

    def _advance(self, job: IngestionJob, status: IngestionStatus, artifact_id: str | None) -> IngestionJob:
        job.status = status
        job.current_artifact_id = artifact_id
        job.updated_at = datetime.now(UTC)
        self.store.save_ingestion_job(job)
        return job
