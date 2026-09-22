from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain import Claim, ClaimStatus, Confidence, EvidenceLink, Polarity
from .model_gateway import ModelGateway, ModelResult
from .store import KnowledgeStore
from .v2_adapters import WikiStore, build_memory_store
from .v2_schemas import ReadingResponse, VerificationResponse


@dataclass
class AIIngestionService:
    """AI-first historical reading pipeline with a publish gate."""

    store: KnowledgeStore
    gateway: ModelGateway | None = None
    wiki: WikiStore | None = None
    memories: Any | None = None

    def __post_init__(self) -> None:
        self.gateway = self.gateway or ModelGateway()
        self.wiki = self.wiki or WikiStore(Path(os.getenv("HISTORYBOT_WIKI_ROOT", "data/wiki")))
        self.memories = self.memories or build_memory_store(self.store.db)

    async def run(self, *, job_id: str | None, source_id: str, question: str = "") -> dict[str, Any]:
        passages = self.store.get_passages(source_id)
        if not passages:
            raise ValueError(f"source has no passages: {source_id}")
        run_id = "read_" + uuid.uuid4().hex[:20]
        self.store.save_v2_reading_run(run_id, job_id=job_id, status="reading", payload={"source_id": source_id, "question": question, "total_units": len(passages)})
        # Per-passage checkpoint IDs survive re-runs across different run_ids.
        reading_steps: dict[str, dict] = {}
        verification_steps: dict[str, dict] = {}
        failed_read: list[str] = []
        failed_verify: list[str] = []

        async def read_one(ordinal: int, passage) -> None:
            passage_id = passage.id
            checkpoint_id = f"ckpt_read_{passage_id}"
            cached = self.store.get_v2_step(checkpoint_id)
            if cached and cached.get("status") == "complete" and cached.get("output", {}).get("units"):
                reading_steps[passage_id] = cached
                return
            prompt = self._reading_prompt([passage], question)
            result = await self.gateway.structured(
                task="historical_reading",
                system="你是严谨的中国历史史料阅读员。只能根据提供的原文阅读，不能补写原文没有的事实。",
                user=prompt,
                schema=ReadingResponse,
            )
            units = result.value.model_dump(mode="json").get("units", [])
            for unit in units:
                unit["passage_id"] = passage_id
            output = {"units": units, "usage": result.raw.get("usage", {})}
            self.store.save_v2_step(
                checkpoint_id,
                run_id=run_id,
                ordinal=ordinal,
                step_type="read",
                output=output,
                input_ref=passage_id,
                model=result.model,
                prompt_version="historical-reading-v2",
            )
            reading_steps[passage_id] = {"id": checkpoint_id, "output": output}

        async def verify_one(ordinal: int, passage) -> None:
            passage_id = passage.id
            cached_reading = reading_steps.get(passage_id)
            if not cached_reading:
                return
            checkpoint_id = f"ckpt_verify_{passage_id}"
            cached = self.store.get_v2_step(checkpoint_id)
            if cached and cached.get("status") == "complete" and cached.get("output", {}).get("items"):
                verification_steps[passage_id] = cached
                return
            unit = (cached_reading.get("output", {}).get("units") or [{}])[0]
            candidate = json.dumps(unit, ensure_ascii=False)
            result = await self.gateway.structured(
                task="historical_reading_verification",
                system="你是独立史料复核员。逐项对照原文，报告遗漏、臆造、错引和段落错配。",
                user=f"原文：\n{passage.text}\n\n阅读结果：\n{candidate}\n\n请验证这段阅读结果是否准确对应原文。",
                schema=VerificationResponse,
            )
            payload = result.value.model_dump(mode="json")
            output = {**payload, "usage": result.raw.get("usage", {})}
            self.store.save_v2_step(
                checkpoint_id,
                run_id=run_id,
                ordinal=ordinal,
                step_type="verify",
                output=output,
                input_ref=passage_id,
                model=result.model,
                prompt_version="historical-verify-v2",
            )
            verification_steps[passage_id] = {"id": checkpoint_id, "output": output}

        semaphore = asyncio.Semaphore(max(1, int(os.getenv("HISTORYBOT_V2_CONCURRENCY", "1"))))
        async def bounded(fn, ordinal: int, passage):
            async with semaphore:
                try:
                    await fn(ordinal, passage)
                except Exception:
                    if fn is read_one:
                        failed_read.append(passage.id)
                    else:
                        failed_verify.append(passage.id)

        await asyncio.gather(*(bounded(read_one, i, p) for i, p in enumerate(passages)))
        await asyncio.gather(*(bounded(verify_one, i, p) for i, p in enumerate(passages)))

        readings = []
        for passage in passages:
            step = reading_steps.get(passage.id)
            if not step:
                continue
            for unit in step.get("output", {}).get("units", []):
                unit["passage_id"] = passage.id
                readings.append(unit)

        verification_by_passage = {
            passage_id: step.get("output", {})
            for passage_id, step in verification_steps.items()
        }

        summary = self._summary(readings)
        self.store.save_v2_step(f"{run_id}_synthesis", run_id=run_id, ordinal=len(passages), step_type="synthesis", output=summary, model=None, prompt_version="historical-reading-v2")
        readings = self._validate_readings(readings, passages)

        graph_counts = self._materialize_graph_v2(run_id, readings, verification_by_passage)
        self.store.save_v2_step(f"{run_id}_graph", run_id=run_id, ordinal=len(passages) + 1, step_type="graph_materialization", output=graph_counts, model=None, prompt_version="historical-graph-v2")
        drafts = self._save_drafts_v2(run_id, source_id, readings, verification_by_passage)
        verified_count = sum(
            1 for items in (output.get("items", []) for output in verification_by_passage.values())
            if any(item.get("supported") for item in items)
        )
        passed = (
            len(readings) == len(passages)
            and len(verification_by_passage) == len(passages)
            and not failed_read
            and not failed_verify
            and verified_count == len(passages)
        )
        status = "verified" if passed else "review_required"
        self.store.save_v2_reading_run(run_id, job_id=job_id, status=status, payload={
            "source_id": source_id, "question": question,
            "total_units": len(passages),
            "read_units": len(readings),
            "verify_units": len(verification_by_passage),
            "verified_units": verified_count,
            "failed_read": failed_read,
            "failed_verify": failed_verify,
            "draft_ids": drafts,
            "graph": graph_counts,
        })
        if passed:
            await self.publish(run_id, source_id=source_id, summary=summary)
        return {"run_id": run_id, "status": status, "read_units": len(readings), "total_units": len(passages), "verified_units": verified_count, "failed_read": failed_read, "failed_verify": failed_verify, "draft_ids": drafts, "graph": graph_counts}

    def _validate_readings(self, readings: list[dict], passages: list) -> list[dict]:
        """Reject model output that points outside the submitted source or invents quotes."""
        by_id = {passage.id: passage.text for passage in passages}
        valid = []
        seen_passages = set()
        for item in readings:
            passage_id = item.get("passage_id")
            if passage_id not in by_id or passage_id in seen_passages:
                continue
            quotes = [quote for quote in item.get("evidence_quotes", []) if quote and quote in by_id[passage_id]]
            if not quotes:
                continue
            item = dict(item)
            item["evidence_quotes"] = quotes
            item["mentions"] = [value for value in item.get("mentions", []) if value.get("passage_id") == passage_id]
            item["identity_links"] = [value for value in item.get("identity_links", []) if value.get("passage_id") == passage_id]
            item["events"] = [value for value in item.get("events", []) if value.get("passage_id") == passage_id]
            item["claims"] = [
                value
                for value in item.get("claims", [])
                if value.get("passage_ids") and set(value["passage_ids"]) == {passage_id}
            ]
            seen_passages.add(passage_id)
            valid.append(item)
        return valid

    def _reading_prompt(self, passages: list, question: str) -> str:
        body = "\n".join(f"[{p.id}] {p.text}" for p in passages)
        return f"研究问题：{question or '完整阅读并记录史料明确内容'}\n请逐段返回 units。每段必须使用真实 passage_id，给出原文明确事实、结构化人物 mentions、身份别名 identity_links、事件 events 和主张 claims；每个 claim 必须是原文明确表达的 subject/predicate/object，必须引用该段 passage_id，不能把推断写成事实。每个 claim 的 statement 要能被 evidence_quotes 原文直接支持。只有原文明示同一身份时才能给 identity_links。无法确定处放入 uncertainties，简短判断依据放入 reasoning_summary。\n史料：\n{body}"

    def _materialize_graph(self, run_id: str, readings: list[dict], verification: VerificationResponse) -> dict[str, int]:
        """Persist AI reading outputs into the evidence graph without inventing edges."""
        verified = {item.unit_id: item for item in verification.items}
        entity_ids: set[str] = set()
        alias_ids: set[str] = set()
        event_ids: set[str] = set()
        claim_ids: set[str] = set()
        evidence_count = 0
        for unit in readings:
            unit_id = unit.get("unit_id", "unknown")
            check = verified.get(unit_id)
            supported = bool(check and check.supported)
            unit_status = "verified" if supported else "disputed"
            for mention in unit.get("mentions", []):
                surface = str(mention.get("surface", "")).strip()
                entity_type = str(mention.get("entity_type", "concept"))
                if not surface:
                    continue
                entity_id = "ent_v2_" + hashlib.sha256(json.dumps({"surface": surface, "entity_type": entity_type}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
                entity_ids.add(entity_id)
                payload = {**mention, "id": entity_id, "surface": surface, "entity_type": entity_type, "confidence": float(mention.get("confidence", 0.5)), "resolution": {"status": unit_status, "method": "ai_reading_v2", "run_id": run_id, "unit_id": unit_id}}
                self.store.save_entity(entity_id, payload, run_id)
            for identity in unit.get("identity_links", []):
                canonical = str(identity.get("canonical_surface", "")).strip()
                alias = str(identity.get("alias_surface", "")).strip()
                if not canonical or not alias or canonical == alias:
                    continue
                alias_id = "alias_v2_" + hashlib.sha256(json.dumps({"canonical_surface": canonical, "alias_surface": alias, "relation": identity.get("relation"), "passage_id": identity.get("passage_id")}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
                alias_ids.add(alias_id)
                self.store.save_entity_alias(alias_id, {**identity, "id": alias_id, "canonical_surface": canonical, "alias_surface": alias, "confidence": float(identity.get("confidence", 0.5)), "status": unit_status, "run_id": run_id, "unit_id": unit_id}, run_id)
            unit_event_ids: list[str] = []
            unit_events = unit.get("events", [])
            for event in unit_events:
                event_payload = dict(event)
                participants = [str(item).strip() for item in event_payload.get("participant_surfaces", []) if str(item).strip()]
                event_payload["participant_surfaces"] = list(dict.fromkeys(participants))
                event_id = "evt_v2_" + hashlib.sha256(json.dumps({k: event_payload.get(k) for k in ("event_type", "label", "date_start", "date_end", "place", "participant_surfaces", "passage_id")}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
                event_ids.add(event_id)
                unit_event_ids.append(event_id)
                event_payload.update({"id": event_id, "status": unit_status, "run_id": run_id, "unit_id": unit_id})
                self.store.save_event(event_id, event_payload, run_id)
            for draft in unit.get("claims", []):
                passage_ids = list(dict.fromkeys(draft.get("passage_ids", []) or [unit.get("passage_id")]))
                if not passage_ids or not draft.get("subject") or not draft.get("predicate") or not draft.get("object"):
                    continue
                claim_id = "clm_v2_" + hashlib.sha256(json.dumps({"subject": draft["subject"], "predicate": draft["predicate"], "object": draft["object"], "statement": draft.get("statement", ""), "passage_ids": sorted(passage_ids)}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
                claim_ids.add(claim_id)
                score = max(0.0, min(1.0, float(draft.get("confidence", 0.5))))
                primary_event = unit_events[0] if unit_events else {}
                claim = Claim(id=claim_id, subject=draft["subject"], predicate=draft["predicate"], object=draft["object"], statement=draft.get("statement", ""), status=ClaimStatus.MACHINE_REVIEWED if supported else ClaimStatus.DISPUTED, confidence=Confidence(score=score, source_reliability=score, extraction_quality=score, resolution_quality=score, corroboration=0.0, contradiction_penalty=0.0, rationale=(check.rationale if check else "未完成独立复核")), passage_ids=passage_ids, event_id=unit_event_ids[0] if unit_event_ids else None, time_from=primary_event.get("date_start"), time_to=primary_event.get("date_end"), time_expression=primary_event.get("date_start") or primary_event.get("date_end"), inference_method="explicit", metadata={"run_id": run_id, "unit_id": unit_id, "ai_reading": True})
                self.store.save_claim(claim, run_id)
                quotes = [quote for quote in unit.get("evidence_quotes", []) if quote]
                if not quotes:
                    quotes = [""]
                for index, passage_id in enumerate(passage_ids):
                    quote = quotes[min(index, len(quotes) - 1)]
                    link_id = "ev_v2_" + hashlib.sha256(f"{claim_id}:{passage_id}".encode()).hexdigest()[:16]
                    link = EvidenceLink(id=link_id, claim_id=claim_id, passage_id=passage_id, polarity=Polarity.SUPPORTS if supported else Polarity.UNRELATED, quote=quote, reasoning=check.rationale if check else "未完成独立复核", independent_source=False, confidence=score)
                    self.store.save_evidence(link, run_id)
                    evidence_count += 1
        return {"entities": len(entity_ids), "aliases": len(alias_ids), "events": len(event_ids), "claims": len(claim_ids), "evidence": evidence_count}

    def _materialize_graph_v2(self, run_id: str, readings: list[dict], verification_by_passage: dict[str, dict]) -> dict[str, int]:
        """Persist AI reading outputs into the evidence graph, keyed by passage_id."""
        entity_ids: set[str] = set()
        alias_ids: set[str] = set()
        event_ids: set[str] = set()
        claim_ids: set[str] = set()
        evidence_count = 0
        for unit in readings:
            passage_id = unit.get("passage_id", "unknown")
            verify_output = verification_by_passage.get(passage_id, {})
            items = verify_output.get("items", [])
            supported = bool(items and items[0].get("supported"))
            unit_status = "verified" if supported else "disputed"
            rationale = (items[0].get("rationale", "") if items else "") or "未完成独立复核"
            for mention in unit.get("mentions", []):
                surface = str(mention.get("surface", "")).strip()
                entity_type = str(mention.get("entity_type", "concept"))
                if not surface:
                    continue
                entity_id = "ent_v2_" + hashlib.sha256(json.dumps({"surface": surface, "entity_type": entity_type}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
                entity_ids.add(entity_id)
                payload = {**mention, "id": entity_id, "surface": surface, "entity_type": entity_type, "confidence": float(mention.get("confidence", 0.5)), "resolution": {"status": unit_status, "method": "ai_reading_v2", "run_id": run_id, "passage_id": passage_id}}
                self.store.save_entity(entity_id, payload, run_id)
            for identity in unit.get("identity_links", []):
                canonical = str(identity.get("canonical_surface", "")).strip()
                alias = str(identity.get("alias_surface", "")).strip()
                if not canonical or not alias or canonical == alias:
                    continue
                alias_id = "alias_v2_" + hashlib.sha256(json.dumps({"canonical_surface": canonical, "alias_surface": alias, "relation": identity.get("relation"), "passage_id": identity.get("passage_id", passage_id)}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
                alias_ids.add(alias_id)
                self.store.save_entity_alias(alias_id, {**identity, "id": alias_id, "canonical_surface": canonical, "alias_surface": alias, "confidence": float(identity.get("confidence", 0.5)), "status": unit_status, "run_id": run_id, "passage_id": passage_id}, run_id)
            unit_event_ids: list[str] = []
            unit_events = unit.get("events", [])
            for event in unit_events:
                event_payload = dict(event)
                participants = [str(item).strip() for item in event_payload.get("participant_surfaces", []) if str(item).strip()]
                event_payload["participant_surfaces"] = list(dict.fromkeys(participants))
                event_payload.setdefault("passage_id", passage_id)
                event_id = "evt_v2_" + hashlib.sha256(json.dumps({k: event_payload.get(k) for k in ("event_type", "label", "date_start", "date_end", "place", "participant_surfaces", "passage_id")}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
                event_ids.add(event_id)
                unit_event_ids.append(event_id)
                event_payload.update({"id": event_id, "status": unit_status, "run_id": run_id, "passage_id": passage_id})
                self.store.save_event(event_id, event_payload, run_id)
            for draft in unit.get("claims", []):
                draft = dict(draft)
                draft.setdefault("passage_ids", [passage_id])
                if not draft.get("passage_ids"):
                    draft["passage_ids"] = [passage_id]
                draft.setdefault("passage_id", passage_id)
                passage_ids = list(dict.fromkeys(draft["passage_ids"]))
                if not draft.get("subject") or not draft.get("predicate") or not draft.get("object"):
                    continue
                claim_id = "clm_v2_" + hashlib.sha256(json.dumps({"subject": draft["subject"], "predicate": draft["predicate"], "object": draft["object"], "statement": draft.get("statement", ""), "passage_ids": sorted(passage_ids)}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
                claim_ids.add(claim_id)
                score = max(0.0, min(1.0, float(draft.get("confidence", 0.5))))
                primary_event = unit_events[0] if unit_events else {}
                claim = Claim(id=claim_id, subject=draft["subject"], predicate=draft["predicate"], object=draft["object"], statement=draft.get("statement", ""), status=ClaimStatus.MACHINE_REVIEWED if supported else ClaimStatus.DISPUTED, confidence=Confidence(score=score, source_reliability=score, extraction_quality=score, resolution_quality=score, corroboration=0.0, contradiction_penalty=0.0, rationale=rationale), passage_ids=passage_ids, event_id=unit_event_ids[0] if unit_event_ids else None, time_from=primary_event.get("date_start"), time_to=primary_event.get("date_end"), time_expression=primary_event.get("date_start") or primary_event.get("date_end"), inference_method="explicit", metadata={"run_id": run_id, "passage_id": passage_id, "ai_reading": True})
                self.store.save_claim(claim, run_id)
                quotes = [quote for quote in unit.get("evidence_quotes", []) if quote] or [""]
                for index, pid in enumerate(passage_ids):
                    quote = quotes[min(index, len(quotes) - 1)]
                    link_id = "ev_v2_" + hashlib.sha256(f"{claim_id}:{pid}".encode()).hexdigest()[:16]
                    link = EvidenceLink(id=link_id, claim_id=claim_id, passage_id=pid, polarity=Polarity.SUPPORTS if supported else Polarity.UNRELATED, quote=quote, reasoning=rationale, independent_source=False, confidence=score)
                    self.store.save_evidence(link, run_id)
                    evidence_count += 1
        return {"entities": len(entity_ids), "aliases": len(alias_ids), "events": len(event_ids), "claims": len(claim_ids), "evidence": evidence_count}

    def _summary(self, readings: list[dict]) -> dict[str, Any]:
        return {"document_summary": "；".join(item.get("reading_summary", "") for item in readings if item.get("reading_summary")), "unit_count": len(readings), "uncertainties": [x for item in readings for x in item.get("uncertainties", [])]}

    async def _verify(self, run_id: str, passages: list, readings: list[dict], question: str) -> ModelResult:
        source = "\n".join(f"[{p.id}] {p.text}" for p in passages)
        candidate = json.dumps(readings, ensure_ascii=False)
        result = await self.gateway.structured(task="historical_reading_verification", system="你是独立史料复核员。逐项对照原文，报告遗漏、臆造、错引和段落错配。", user=f"研究问题：{question}\n原文：\n{source}\n阅读结果：\n{candidate}", schema=VerificationResponse)
        return result

    def _save_drafts(self, run_id: str, source_id: str, readings: list[dict], verification: VerificationResponse) -> list[str]:
        verified = {item.unit_id: item for item in verification.items if item.supported}
        ids = []
        for item in readings:
            passage_id = item.get("passage_id", "unknown")
            draft_id = f"draft_{run_id}_{passage_id}"
            check = verified.get(item.get("unit_id"))
            payload = {"source_id": source_id, **item, "verification": check.model_dump(mode="json") if check else None}
            self.store.save_v2_draft(draft_id, run_id=run_id, kind="historical_reading", payload=payload, status="verified" if item.get("unit_id") in verified else "review_required")
            ids.append(draft_id)
            for index, quote in enumerate(item.get("evidence_quotes", [])):
                passage_id = item.get("passage_id")
                if passage_id:
                    self.store.save_v2_anchor(f"anchor_{draft_id}_{index}", draft_id=draft_id, passage_id=passage_id, quote=quote, locator={"passage_id": passage_id}, verified=item.get("unit_id") in verified, payload={"run_id": run_id})
        return ids

    def _save_drafts_v2(self, run_id: str, source_id: str, readings: list[dict], verification_by_passage: dict[str, dict]) -> list[str]:
        ids = []
        for item in readings:
            passage_id = item.get("passage_id", "unknown")
            draft_id = f"draft_{run_id}_{passage_id}"
            verify_output = verification_by_passage.get(passage_id, {})
            items = verify_output.get("items", [])
            supported = bool(items and items[0].get("supported"))
            payload = {"source_id": source_id, **item, "verification": items[0] if items else None}
            self.store.save_v2_draft(draft_id, run_id=run_id, kind="historical_reading", payload=payload, status="verified" if supported else "review_required")
            ids.append(draft_id)
            for index, quote in enumerate(item.get("evidence_quotes", [])):
                pid = item.get("passage_id")
                if pid:
                    self.store.save_v2_anchor(f"anchor_{draft_id}_{index}", draft_id=draft_id, passage_id=pid, quote=quote, locator={"passage_id": pid}, verified=supported, payload={"run_id": run_id})
        return ids

    async def publish(self, run_id: str, *, source_id: str, summary: dict[str, Any]) -> dict[str, Any]:
        source = self.store.get_source(source_id)
        if not source:
            raise KeyError(source_id)
        title = source.title.replace("/", "-")
        slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", title).strip("-").lower() or source_id
        content = f"---\ntitle: \"{source.title}\"\ntype: source\nsource_id: {source_id}\nreading_run: {run_id}\n---\n\n## AI 阅读摘要\n{summary['document_summary']}\n\n## 证据状态\n已通过独立 AI 复核；详细证据锚点见 SQLite 阅读记录。\n"
        path = f"sources/{slug}.md"
        self.wiki.save_draft(run_id, path, content, metadata={"source_id": source_id})
        published = self.wiki.publish(run_id, [path])
        memory_id = f"memory_{hashlib.sha256(f'{source_id}:{run_id}'.encode()).hexdigest()[:20]}"
        memory = self.memories.add(summary["document_summary"], memory_id=memory_id, metadata={"kind": "historical_knowledge", "source_id": source_id, "run_id": run_id, "wiki_path": path, "status": "active"})
        self.store.save_v2_sync_task(f"sync_{run_id}_wiki", run_id=run_id, target="wiki", object_id=path, payload=published, status="complete")
        self.store.save_v2_sync_task(f"sync_{run_id}_memory", run_id=run_id, target="memory", object_id=memory_id, payload=memory, status="complete")
        return {"wiki": published, "memory": memory}
