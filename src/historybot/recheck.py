from __future__ import annotations

import argparse
import asyncio
import hashlib
import json

from .domain import Conflict
from .model_gateway import ModelGateway
from .schemas import ContradictionResponse
from .store import KnowledgeStore


async def recheck(db: str, max_claims: int = 4) -> dict:
    store = KnowledgeStore(db)
    claims = store.get_claims()[:max_claims]
    result = await ModelGateway().structured(
        task="contradiction_detection",
        system="你是历史证伪审查员，只报告给定主张之间有明确证据的矛盾，不要臆造。没有矛盾就返回空列表。",
        user=json.dumps({"claims": [claim.model_dump(mode="json") for claim in claims]}, ensure_ascii=False),
        schema=ContradictionResponse,
    )
    saved = []
    for item in result.value.conflicts:
        payload = item.model_dump(mode="json")
        conflict_id = "conf_" + hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
        conflict = Conflict(id=conflict_id, **payload)
        store.save_conflict(conflict, "recheck")
        saved.append(conflict.model_dump(mode="json"))
    store.audit("recheck", "contradiction_completed", "recheck", {"claim_count": len(claims), "conflict_count": len(saved), "model": result.model})
    return {"status": "complete", "checked_claims": len(claims), "conflicts": saved, "counts": store.counts()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--max-claims", type=int, default=4)
    args = parser.parse_args()
    print(asyncio.run(recheck(args.db, args.max_claims)))

