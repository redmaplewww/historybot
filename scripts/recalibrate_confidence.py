from __future__ import annotations

import argparse
import json

from historybot.policies import preliminary_claim_confidence
from historybot.store import KnowledgeStore


def recalibrate(db: str) -> dict[str, int]:
    """Re-score pending extracted claims and evidence using source baselines."""

    store = KnowledgeStore(db)
    source_assessments = {
        source.id: source.metadata.get("assessment", {})
        for source in store.get_sources()
    }
    passage_sources = {
        row[0]: row[1]
        for row in store.db.execute("SELECT id, source_id FROM passages")
    }
    changed_claims = 0
    changed_evidence = 0
    for row in store.db.execute("SELECT id, payload, status FROM claims ORDER BY id").fetchall():
        claim_id, raw_payload, status = row
        claim = json.loads(raw_payload)
        if status != "extracted" or claim.get("metadata", {}).get("review_state") != "pending":
            continue
        passage_ids = claim.get("passage_ids", [])
        assessments = [
            source_assessments.get(passage_sources.get(passage_id, ""), {})
            for passage_id in passage_ids
        ]
        old_confidence = claim.get("confidence", {})
        recalibrated = preliminary_claim_confidence(
            float(old_confidence.get("extraction_quality", old_confidence.get("score", 0.0))),
            assessments,
        )
        if (
            old_confidence.get("score") == recalibrated["score"]
            and old_confidence.get("source_reliability") == recalibrated["source_reliability"]
        ):
            continue
        claim["confidence"] = {
            **old_confidence,
            "score": recalibrated["score"],
            "source_reliability": recalibrated["source_reliability"],
            "rationale": recalibrated["rationale"],
        }
        serialized = json.dumps(claim, ensure_ascii=False)
        store.db.execute("UPDATE claims SET payload=? WHERE id=?", (serialized, claim_id))
        store.db.execute(
            "UPDATE relations SET confidence=?, payload=json_set(payload, '$.confidence', ?) WHERE claim_id=?",
            (recalibrated["score"], recalibrated["score"], claim_id),
        )
        changed_claims += 1
        for evidence_row in store.db.execute("SELECT id, payload FROM evidence WHERE claim_id=?", (claim_id,)).fetchall():
            evidence_id, evidence_raw = evidence_row
            evidence = json.loads(evidence_raw)
            evidence["confidence"] = recalibrated["score"]
            store.db.execute(
                "UPDATE evidence SET payload=? WHERE id=?",
                (json.dumps(evidence, ensure_ascii=False), evidence_id),
            )
            changed_evidence += 1
    store.db.commit()
    store.audit(
        "confidence_recalibration",
        "pending_claims_recalibrated",
        "claims",
        {"claims": changed_claims, "evidence": changed_evidence, "policy": "source-baseline-v1"},
    )
    return {"claims": changed_claims, "evidence": changed_evidence}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    print(recalibrate(args.db))
