from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .domain import Claim, EvidenceLink
from .store import KnowledgeStore


def page_key(url: str | None) -> str:
    if not url:
        return ""
    return unquote(parse_qs(urlparse(url).query).get("page", [url])[0])


def _payloads(store: KnowledgeStore, table: str) -> list[dict]:
    return [json.loads(row[0]) for row in store.db.execute(f"SELECT payload FROM {table} ORDER BY id")]


def _claim_id(claim: Claim) -> str:
    payload = {
        "subject": claim.subject,
        "predicate": claim.predicate,
        "object": claim.object,
        "statement": claim.statement,
        "passage_ids": sorted(claim.passage_ids),
    }
    return "clm_" + hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]


def merge_artifacts(source_db: str, target_db: str) -> dict:
    source = KnowledgeStore(source_db)
    target = KnowledgeStore(target_db)
    target_sources = {page_key(item.url): item for item in target.get_sources()}
    source_sources = source.get_sources()
    source_to_target: dict[str, str] = {}
    for item in source_sources:
        target_source = target_sources.get(page_key(item.url))
        if not target_source:
            raise ValueError(f"source page missing from target: {page_key(item.url)}")
        source_to_target[item.id] = target_source.id

    passage_maps: dict[tuple[str, str], str] = {}
    for source_item in source_sources:
        old_passages = source.get_passages(source_item.id)
        new_passages = target.get_passages(source_to_target[source_item.id])
        by_hash = {passage.text_hash: passage for passage in new_passages}
        for passage in old_passages:
            target_passage = by_hash.get(passage.text_hash)
            if not target_passage:
                raise ValueError(f"passage hash missing: {passage.id}")
            passage_maps[(source_item.id, passage.id)] = target_passage.id

    event_maps: dict[str, str] = {}
    for event in _payloads(source, "events"):
        event_source_id = source.db.execute("SELECT source_id FROM passages WHERE id=?", (event["passage_id"],)).fetchone()[0]
        mapped = {**event, "passage_id": passage_maps[(event_source_id, event["passage_id"])]}
        event_id = "evt_" + hashlib.sha256(json.dumps(mapped, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
        target.save_event(event_id, {**mapped, "id": event_id}, "merge")
        event_maps[event["id"]] = event_id

    for entity in _payloads(source, "entities"):
        entity_source_id = source.db.execute("SELECT source_id FROM passages WHERE id=?", (entity["passage_id"],)).fetchone()[0]
        target.save_entity(entity["id"], {**entity, "passage_id": passage_maps[(entity_source_id, entity["passage_id"])]}, "merge")

    claim_maps: dict[str, str] = {}
    old_claims = source.get_claims()
    for old_claim in old_claims:
        mapped_passages = []
        for passage_id in old_claim.passage_ids:
            passage_source_id = source.db.execute("SELECT source_id FROM passages WHERE id=?", (passage_id,)).fetchone()[0]
            mapped_passages.append(passage_maps[(passage_source_id, passage_id)])
        if len(mapped_passages) != len(old_claim.passage_ids):
            raise ValueError(f"claim passage mapping incomplete: {old_claim.id}")
        new_claim = old_claim.model_copy(
            update={
                "id": _claim_id(old_claim.model_copy(update={"passage_ids": mapped_passages})),
                "passage_ids": mapped_passages,
                "event_id": event_maps.get(old_claim.event_id, old_claim.event_id),
            }
        )
        target.save_claim(new_claim, "merge")
        claim_maps[old_claim.id] = new_claim.id

    evidence_count = 0
    for link_payload in _payloads(source, "evidence"):
        old_link = EvidenceLink.model_validate(link_payload)
        old_passage = source.db.execute("SELECT source_id FROM passages WHERE id=?", (old_link.passage_id,)).fetchone()
        mapped_passage = passage_maps[(old_passage[0], old_link.passage_id)]
        new_claim_id = claim_maps[old_link.claim_id]
        new_id = "ev_" + hashlib.sha256(f"{new_claim_id}:{mapped_passage}".encode()).hexdigest()[:16]
        target.save_evidence(old_link.model_copy(update={"id": new_id, "claim_id": new_claim_id, "passage_id": mapped_passage}), "merge")
        evidence_count += 1

    report = {
        "source_db": str(Path(source_db)),
        "target_db": str(Path(target_db)),
        "sources_mapped": len(source_to_target),
        "events_mapped": len(event_maps),
        "claims_mapped": len(claim_maps),
        "evidence_mapped": evidence_count,
        "counts": target.counts(),
    }
    Path(target_db).with_suffix(".merge.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Map previously reviewed artifacts onto a normalized full corpus")
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--target-db", required=True)
    args = parser.parse_args()
    print(json.dumps(merge_artifacts(args.source_db, args.target_db), ensure_ascii=False, indent=2))
