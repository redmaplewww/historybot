from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections.abc import Iterable

from .domain import Claim, ClaimStatus, Confidence, EvidenceLink, Polarity
from .entity_resolution import resolve_entities
from .model_gateway import ModelGateway
from .policies import preliminary_claim_confidence
from .schemas import ExtractionResponse, PeopleExtractionResponse
from .store import KnowledgeStore


def _ensure_status_table(store: KnowledgeStore) -> None:
    store.db.execute(
        "CREATE TABLE IF NOT EXISTS extraction_batches (id TEXT PRIMARY KEY, status TEXT NOT NULL, passage_ids TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, error TEXT, payload TEXT NOT NULL)"
    )
    store.db.execute(
        "CREATE TABLE IF NOT EXISTS people_extraction_batches (id TEXT PRIMARY KEY, status TEXT NOT NULL, passage_ids TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, error TEXT, payload TEXT NOT NULL)"
    )
    store.db.execute(
        "CREATE TABLE IF NOT EXISTS people_extraction_passages (passage_id TEXT PRIMARY KEY, status TEXT NOT NULL, batch_id TEXT NOT NULL, error TEXT)"
    )
    store.db.execute(
        "CREATE TABLE IF NOT EXISTS extraction_passages (passage_id TEXT PRIMARY KEY, status TEXT NOT NULL, batch_id TEXT NOT NULL, error TEXT)"
    )
    store.db.commit()


def _completed_passages(store: KnowledgeStore, batch_table: str, passage_table: str) -> set[str]:
    """Read and backfill passage-level completion for legacy batch ledgers."""

    completed = {
        row[0]
        for row in store.db.execute(
            f"SELECT passage_id FROM {passage_table} WHERE status='complete'"
        )
    }
    legacy_rows = store.db.execute(
        f"SELECT passage_ids FROM {batch_table} WHERE status='complete'"
    ).fetchall()
    for row in legacy_rows:
        passage_ids = json.loads(row[0])
        completed.update(passage_ids)
        store.db.executemany(
            f"INSERT OR IGNORE INTO {passage_table}(passage_id,status,batch_id,error) VALUES(?,?,?,?)",
            [(passage_id, "complete", "legacy", None) for passage_id in passage_ids],
        )
    store.db.commit()
    return completed


def _chunks(items: list[dict], size: int) -> Iterable[list[dict]]:
    for offset in range(0, len(items), size):
        yield items[offset : offset + size]


def _batch_id(batch: list[dict]) -> str:
    return "batch_" + hashlib.sha256(",".join(item["id"] for item in batch).encode()).hexdigest()[:20]


def _prompt(batch: list[dict], people_only: bool = False) -> str:
    passages = "\n".join(f"[{item['id']}] {item['text']}" for item in batch)
    return (
        "只抽取下列史料原文明确出现的信息，不得根据常识、小说印象或上下文之外的知识补全。\n"
        "1. mentions：人物姓名、复姓、字、号、谥号、明确的人物称谓；地点、官职、组织、朝代、抽象概念不要标为人物。\n"
        "2. identity_links：只有原文明确表达‘某人字某’、‘某人号某’、‘某人谥某’、‘某人本名某’时填写；canonical_surface 是本名，alias_surface 是字/号/谥号/曾用名。\n"
        "3. events：只填原文明确发生的事件，participant_surfaces 只能填原文出现的人物表面名。\n"
        + ("本轮只返回 mentions 和 identity_links，events、claims 必须为空。\n" if people_only else "4. claims：只填可由一个或多个 passage_id 直接引用支持的主张。每项都必须使用输入中的真实 passage_id。\n")
        + "返回严格 JSON，不要 markdown。\n\n史料段落：\n" + passages
    )


async def _call(gateway: ModelGateway, batch: list[dict], retries: int, people_only: bool = False) -> ExtractionResponse | PeopleExtractionResponse:
    last_error: Exception | None = None
    for _ in range(retries + 1):
        try:
            result = await gateway.structured(
                task="full_corpus_extraction",
                system="你是证据优先的中国古代史料阅读员。输出必须服从 JSON Schema，不能制造实体或事件。",
                user=_prompt(batch, people_only),
                schema=PeopleExtractionResponse if people_only else ExtractionResponse,
            )
            return result.value
        except Exception as error:  # noqa: BLE001 - a failed batch is persisted and retried
            last_error = error
    assert last_error is not None
    raise last_error


def _save_result(store: KnowledgeStore, task_id: str, result: ExtractionResponse | PeopleExtractionResponse, passage_text: dict[str, str]) -> dict[str, int]:
    allowed_passages = {row[0] for row in store.db.execute("SELECT id FROM passages")}
    passage_sources = {
        row[0]: row[1]
        for row in store.db.execute("SELECT id, source_id FROM passages")
    }
    source_assessments = {
        source.id: source.metadata.get("assessment", {})
        for source in store.get_sources()
    }
    mentions = 0
    aliases = 0
    events = 0
    claims = 0
    for mention in result.mentions:
        if mention.passage_id not in allowed_passages or not mention.surface.strip():
            continue
        payload = {**mention.model_dump(mode="json"), "id": ""}
        entity_id = "ent_" + hashlib.sha256(
            json.dumps(
                {"surface": mention.surface, "entity_type": mention.entity_type, "passage_id": mention.passage_id},
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()[:20]
        payload["id"] = entity_id
        store.save_entity(entity_id, payload, task_id)
        mentions += 1
    for identity in result.identity_links:
        if identity.passage_id not in allowed_passages:
            continue
        payload = {**identity.model_dump(mode="json"), "id": ""}
        alias_id = "alias_" + hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()[:20]
        payload["id"] = alias_id
        store.save_entity_alias(alias_id, payload, task_id)
        aliases += 1
    for event in getattr(result, "events", []):
        if event.passage_id not in allowed_passages:
            continue
        payload = {**event.model_dump(mode="json"), "id": ""}
        event_id = "evt_" + hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()[:20]
        payload["id"] = event_id
        store.save_event(event_id, payload, task_id)
        events += 1
    for draft in getattr(result, "claims", []):
        passage_ids = [item for item in draft.passage_ids if item in allowed_passages]
        if not passage_ids:
            continue
        assessments = [
            source_assessments.get(passage_sources.get(passage_id, ""), {})
            for passage_id in passage_ids
        ]
        confidence = preliminary_claim_confidence(draft.confidence, assessments)
        claim_id = "clm_" + hashlib.sha256(
            json.dumps(
                {"subject": draft.subject, "predicate": draft.predicate, "object": draft.object, "statement": draft.statement, "passage_ids": sorted(passage_ids)},
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()[:20]
        claim = Claim(
            id=claim_id,
            subject=draft.subject,
            predicate=draft.predicate,
            object=draft.object,
            statement=draft.statement,
            status=ClaimStatus.EXTRACTED,
            confidence=Confidence(
                score=confidence["score"],
                source_reliability=confidence["source_reliability"],
                extraction_quality=draft.confidence,
                resolution_quality=0.25,
                corroboration=0,
                contradiction_penalty=0,
                rationale=confidence["rationale"],
            ),
            passage_ids=passage_ids,
            metadata={"model_claim_id": draft.id, "review_state": "pending"},
        )
        store.save_claim(claim, task_id)
        for passage_id in passage_ids:
            evidence_id = "ev_" + hashlib.sha256(f"{claim_id}:{passage_id}".encode()).hexdigest()[:16]
            store.save_evidence(
                EvidenceLink(
                    id=evidence_id,
                    claim_id=claim_id,
                    passage_id=passage_id,
                    polarity=Polarity.SUPPORTS,
                    quote=passage_text.get(passage_id, ""),
                    reasoning="候选主张的原文绑定，尚未完成审查",
                    confidence=confidence["score"],
                ),
                task_id,
            )
        claims += 1
    return {"mentions": mentions, "aliases": aliases, "events": events, "claims": claims}


async def extract_corpus(
    db: str,
    *,
    batch_size: int = 80,
    concurrency: int = 4,
    max_passages: int = 0,
    retries: int = 2,
    people_only: bool = False,
) -> dict:
    store = KnowledgeStore(db)
    _ensure_status_table(store)
    batch_table = "people_extraction_batches" if people_only else "extraction_batches"
    passage_table = "people_extraction_passages" if people_only else "extraction_passages"
    rows = store.db.execute("SELECT payload FROM passages ORDER BY source_id, ordinal").fetchall()
    passages = [json.loads(row[0]) for row in rows]
    if max_passages:
        passages = passages[:max_passages]
    done = _completed_passages(store, batch_table, passage_table)
    pending = [item for item in passages if item["id"] not in done]
    batches = list(_chunks(pending, batch_size))
    task_id = "full_extract_" + hashlib.sha256(db.encode()).hexdigest()[:12]
    semaphore = asyncio.Semaphore(max(1, concurrency))
    gateway = ModelGateway()

    async def run_batch(batch: list[dict]) -> tuple[list[dict], ExtractionResponse | None, Exception | None]:
        async with semaphore:
            try:
                result = await _call(gateway, batch, retries, people_only)
                return batch, result, None
            except Exception as error:  # noqa: BLE001 - reported in the batch ledger
                return batch, None, error

    totals = {"mentions": 0, "aliases": 0, "events": 0, "claims_seen": 0, "complete_batches": 0, "failed_batches": 0}
    for offset in range(0, len(batches), max(1, concurrency)):
        window = batches[offset : offset + max(1, concurrency)]
        results = await asyncio.gather(*(run_batch(batch) for batch in window))
        for batch, result, error in results:
            batch_id = _batch_id(batch)
            passage_ids = [item["id"] for item in batch]
            if error is not None:
                store.db.execute(
                    f"INSERT OR REPLACE INTO {batch_table}(id,status,passage_ids,attempt,error,payload) VALUES(?,?,?,?,?,?)",
                    (batch_id, "failed", json.dumps(passage_ids), retries + 1, repr(error), json.dumps({})),
                )
                store.db.commit()
                totals["failed_batches"] += 1
                continue
            assert result is not None
            saved = _save_result(store, task_id, result, {item["id"]: item["text"] for item in batch})
            store.db.execute(
                f"INSERT OR REPLACE INTO {batch_table}(id,status,passage_ids,attempt,error,payload) VALUES(?,?,?,?,?,?)",
                (batch_id, "complete", json.dumps(passage_ids), 1, None, json.dumps(saved, ensure_ascii=False)),
            )
            store.db.executemany(
                f"INSERT OR REPLACE INTO {passage_table}(passage_id,status,batch_id,error) VALUES(?,?,?,?)",
                [(passage_id, "complete", batch_id, None) for passage_id in passage_ids],
            )
            store.db.commit()
            totals["mentions"] += saved["mentions"]
            totals["aliases"] += saved["aliases"]
            totals["events"] += saved["events"]
            totals["claims_seen"] += saved["claims"]
            totals["complete_batches"] += 1
        # Batch-time resolution is intentionally cheap; the full source-grounded
        # identity derivation runs once at the phase boundary.
        resolve_entities(db, derive_links=False)
        print(json.dumps({"progress": min(offset + len(window), len(batches)), "total_batches": len(batches), **totals}, ensure_ascii=False), flush=True)
    return {"db": db, "passages": len(passages), "pending_batches": len(batches), **totals}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resumable full-corpus entity and event extraction")
    parser.add_argument("--db", required=True)
    parser.add_argument("--batch-size", type=int, default=int(os.getenv("HISTORYBOT_EXTRACT_BATCH_SIZE", "80")))
    parser.add_argument("--concurrency", type=int, default=int(os.getenv("HISTORYBOT_LLM_CONCURRENCY", "4")))
    parser.add_argument("--max-passages", type=int, default=0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--people-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(extract_corpus(args.db, batch_size=args.batch_size, concurrency=args.concurrency, max_passages=args.max_passages, retries=args.retries, people_only=args.people_only)), ensure_ascii=False, indent=2))
