from __future__ import annotations

import asyncio
import hashlib
import json
import os
from typing import Any, TypedDict

import httpx
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from .domain import Claim, ClaimStatus, Confidence, Conflict, EvidenceLink, Polarity
from .model_gateway import ModelGateway
from .schemas import ClaimDraft, ContradictionResponse, ExtractionResponse, ReviewResponse
from .store import KnowledgeStore


class PipelineState(TypedDict, total=False):
    task_id: str
    question: str
    passage_ids: list[str]
    passage_text: list[dict[str, str]]
    extraction: dict[str, Any]
    reviews: list[dict[str, Any]]
    conflicts: list[dict[str, Any]]
    status: str
    errors: list[str]


def normalize_polarity(value: str) -> Polarity:
    normalized = value.strip().lower()
    aliases = {"supports": Polarity.SUPPORTS, "supported": Polarity.SUPPORTS, "supports the claim": Polarity.SUPPORTS, "positive": Polarity.SUPPORTS, "支持": Polarity.SUPPORTS, "refutes": Polarity.REFUTES, "refuted": Polarity.REFUTES, "contradicts": Polarity.REFUTES, "negative": Polarity.REFUTES, "反驳": Polarity.REFUTES, "反对": Polarity.REFUTES, "partial": Polarity.PARTIAL, "partially supports": Polarity.PARTIAL, "mixed": Polarity.PARTIAL, "部分支持": Polarity.PARTIAL, "unrelated": Polarity.UNRELATED, "无关": Polarity.UNRELATED}
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported evidence polarity from model: {value!r}") from exc


def build_graph(*, store: KnowledgeStore, gateway: ModelGateway, checkpointer=None):
    async def extract(state: PipelineState):
        batch_size = int(os.getenv("HISTORYBOT_PASSAGE_BATCH_SIZE", "12"))
        concurrency = max(1, int(os.getenv("HISTORYBOT_LLM_CONCURRENCY", "1")))
        all_payload = {"mentions": [], "identity_links": [], "events": [], "claims": []}
        last_result = None
        semaphore = asyncio.Semaphore(concurrency)

        async def extract_batch(batch):
            parts = "\n".join(f"[{item['id']}] {item['text']}" for item in batch)
            async with semaphore:
                return await gateway.structured(
                    task="evidence_extraction",
                    system="你是严谨的历史史料阅读员。只能抽取文本明确表达的内容；不能根据常识补全。每个主张必须引用 passage_id。",
                    user=f"研究问题：{state['question']}\n史料段落：\n{parts}", schema=ExtractionResponse)

        batches = [state["passage_text"][offset:offset + batch_size] for offset in range(0, len(state["passage_text"]), batch_size)]
        results = await asyncio.gather(*(extract_batch(batch) for batch in batches))
        for result in results:
            payload = result.value.model_dump(mode="json")
            last_result = result
            for key, values in all_payload.items():
                values.extend(payload[key])
        payload = all_payload
        for mention in payload["mentions"]:
            entity_id = "ent_" + hashlib.sha256(json.dumps({"surface": mention["surface"], "entity_type": mention["entity_type"], "passage_id": mention["passage_id"]}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
            store.save_entity(entity_id, {**mention, "id": entity_id}, state["task_id"])
        for identity in payload.get("identity_links", []):
            alias_id = "alias_" + hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
            store.save_entity_alias(alias_id, {**identity, "id": alias_id}, state["task_id"])
        for event in payload["events"]:
            event_id = "evt_" + hashlib.sha256(json.dumps(event, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
            store.save_event(event_id, {**event, "id": event_id}, state["task_id"])
        for claim in payload["claims"]:
            if not claim["passage_ids"]: raise ValueError("claim without passage evidence")
            draft = ClaimDraft.model_validate(claim)
            claim_id = "clm_" + hashlib.sha256(json.dumps({"subject": draft.subject, "predicate": draft.predicate, "object": draft.object, "statement": draft.statement, "passage_ids": sorted(draft.passage_ids)}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
            claim.clear()
            claim.update(Claim(id=claim_id, subject=draft.subject, predicate=draft.predicate, object=draft.object, statement=draft.statement, passage_ids=draft.passage_ids, confidence=Confidence(score=draft.confidence, source_reliability=draft.confidence, extraction_quality=draft.confidence, resolution_quality=draft.confidence, corroboration=0, contradiction_penalty=0, rationale="候选主张，待证据审查"), metadata={"model_claim_id": draft.id}).model_dump(mode="json"))
        if last_result is not None:
            store.audit(state["task_id"], "llm_extraction", state["task_id"], {"provider": last_result.provider, "model": last_result.model, "batch_size": batch_size})
        return {"extraction": payload, "status": "extracted"}

    async def review(state: PipelineState):
        reviews = []
        concurrency = max(1, int(os.getenv("HISTORYBOT_LLM_CONCURRENCY", "1")))
        semaphore = asyncio.Semaphore(concurrency)
        passages = {item["id"]: item["text"] for item in state["passage_text"]}
        passage_sources = {item["id"]: item.get("source_id") for item in state["passage_text"]}
        sources = {source.id: source for source in store.get_sources()}

        async def review_claim(claim):
            evidence = "\n".join(passages.get(pid, "") for pid in claim["passage_ids"])
            source_assessments = [sources[passage_sources[pid]].metadata.get("assessment", {}) for pid in claim["passage_ids"] if passage_sources.get(pid) in sources]
            async with semaphore:
                result = await gateway.structured(
                    task="evidence_review",
                    system="你是史料审查员。判断主张是否被原文支持；如果原文没有足够信息，必须给出 false。",
                    user=json.dumps({"claim": claim, "evidence": evidence, "source_assessments": source_assessments}, ensure_ascii=False), schema=ReviewResponse)
            review_payload = result.value.model_dump(mode="json")
            claim_obj = Claim.model_validate(claim)
            source_reliability = min((item.get("baseline_score", 0.25) for item in source_assessments), default=0.25)
            folk_only = bool(source_assessments) and all(item.get("requires_corroboration", True) and item.get("baseline_score", 0) < 0.5 for item in source_assessments)
            independent_sources = len({passage_sources.get(pid) for pid in claim_obj.passage_ids if passage_sources.get(pid)}) > 1
            claim_obj.status = ClaimStatus.MACHINE_REVIEWED if review_payload["evidence_sufficient"] and not folk_only else ClaimStatus.DISPUTED
            corroboration = 1.0 if independent_sources else 0.0
            score = min(review_payload["revised_confidence"], source_reliability) * (0.85 + 0.15 * corroboration)
            claim_obj.confidence = Confidence(score=score, source_reliability=source_reliability, extraction_quality=claim_obj.confidence.extraction_quality, resolution_quality=claim_obj.confidence.resolution_quality, corroboration=corroboration, contradiction_penalty=claim_obj.confidence.contradiction_penalty, rationale=review_payload["rationale"] + ("；来源属于野史/文学传统，必须寻找独立史料互证" if folk_only else ""))
            return review_payload, claim_obj, passages, independent_sources, score

        reviewed = await asyncio.gather(*(review_claim(claim) for claim in state["extraction"]["claims"]))
        for review_payload, claim_obj, claim_passages, independent_sources, score in reviewed:
            reviews.append(review_payload)
            store.save_claim(claim_obj, state["task_id"])
            for pid in claim_obj.passage_ids:
                link = EvidenceLink(id="ev_" + hashlib.sha256(f"{claim_obj.id}:{pid}".encode()).hexdigest()[:16], claim_id=claim_obj.id, passage_id=pid, polarity=normalize_polarity(review_payload["polarity"]), quote=claim_passages.get(pid, ""), reasoning=review_payload["rationale"], independent_source=independent_sources, confidence=score)
                store.save_evidence(link, state["task_id"])
        return {"reviews": reviews, "status": "reviewed"}

    async def contradictions(state: PipelineState):
        claims = state["extraction"]["claims"]
        if len(claims) < 2:
            return {"conflicts": [], "status": "complete"}
        try:
            result = await gateway.structured(task="contradiction_detection", system="你是历史证伪审查员，只报告文本中可解释的矛盾，不要臆造。", user=json.dumps({"claims": claims}, ensure_ascii=False), schema=ContradictionResponse)
        except httpx.ReadTimeout as exc:
            store.audit(state["task_id"], "contradiction_pending", state["task_id"], {"error_type": type(exc).__name__, "claim_count": len(claims)})
            return {"conflicts": [], "status": "pending_contradictions", "errors": ["contradiction_detection_timeout"]}
        conflicts = []
        for item in result.value.conflicts:
            payload = item.model_dump(mode="json")
            conflict_id = "conf_" + hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
            conflict = Conflict(id=conflict_id, **payload)
            store.save_conflict(conflict, state["task_id"])
            conflicts.append(conflict.model_dump(mode="json"))
        return {"conflicts": conflicts, "status": "complete"}

    builder = StateGraph(PipelineState)
    builder.add_node("extract", extract)
    builder.add_node("review", review)
    builder.add_node("contradictions", contradictions)
    builder.add_edge(START, "extract")
    builder.add_edge("extract", "review")
    builder.add_edge("review", "contradictions")
    builder.add_edge("contradictions", END)
    return builder.compile(checkpointer=checkpointer)


def development_checkpointer():
    """Use only for local runs; production should inject a durable checkpointer."""
    if os.getenv("HISTORYBOT_ENV", "development") == "production":
        raise RuntimeError("production requires an injected durable LangGraph checkpointer")
    return MemorySaver()
