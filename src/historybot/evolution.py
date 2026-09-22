from __future__ import annotations

import argparse
import hashlib
import json

from .policies import SOURCE_POLICY_VERSION
from .store import KnowledgeStore


def create_snapshot(db: str) -> dict:
    store = KnowledgeStore(db)
    claims = store.get_claims()
    status_counts: dict[str, int] = {}
    for claim in claims:
        status_counts[claim.status.value] = status_counts.get(claim.status.value, 0) + 1
    disputed = status_counts.get("disputed", 0)
    payload = {
        "claim_count": len(claims),
        "status_counts": status_counts,
        "disputed_rate": disputed / len(claims) if claims else 0,
        "source_policy_version": SOURCE_POLICY_VERSION,
        "feedback_count": store.db.execute("SELECT COUNT(*) FROM review_feedback").fetchone()[0],
        "next_actions": ["review disputed claims", "compare folk claims with independent compilation sources"] if disputed else ["collect independent sources"],
    }
    snapshot_id = "evo_" + hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    store.save_evolution_snapshot(snapshot_id, SOURCE_POLICY_VERSION, payload, "evolution")
    return {"id": snapshot_id, **payload}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    print(create_snapshot(args.db))

