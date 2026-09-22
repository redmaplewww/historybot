from historybot.conflict_review import generate_candidates
from historybot.domain import Claim, Confidence, Passage, Source
from historybot.store import KnowledgeStore


def _claim(claim_id: str, subject: str, predicate: str, object_: str, passage_id: str, statement: str) -> Claim:
    return Claim(
        id=claim_id,
        subject=subject,
        predicate=predicate,
        object=object_,
        statement=statement,
        passage_ids=[passage_id],
        confidence=Confidence(
            score=0.6,
            source_reliability=0.6,
            extraction_quality=0.9,
            resolution_quality=0.8,
            corroboration=0,
            contradiction_penalty=0,
            rationale="test",
        ),
    )


def test_conflict_review_generates_person_place_and_time_candidates(tmp_path):
    store = KnowledgeStore(str(tmp_path / "conflicts.db"))
    source = Source(id="src", title="source")
    passages = [
        Passage(id="p1", source_id=source.id, ordinal=1, text="曹操任命甲。", text_hash="p1"),
        Passage(id="p2", source_id=source.id, ordinal=2, text="曹操任命乙。", text_hash="p2"),
    ]
    store.save_source(source)
    store.save_passages(passages)
    store.save_entity("e_cao", {"id": "e_cao", "surface": "曹操", "entity_type": "人物", "passage_id": "p1", "confidence": 0.99})
    store.save_claim(_claim("c1", "曹操", "任命", "甲", "p1", "曹操任命甲"))
    store.save_claim(_claim("c2", "曹操", "任命", "乙", "p2", "曹操任命乙"))
    store.save_event("ev1", {"id": "ev1", "label": "交战", "event_type": "战斗", "date_start": "建安十年", "place": "许昌", "participant_surfaces": ["曹操"], "passage_id": "p1", "confidence": 0.9})
    store.save_event("ev2", {"id": "ev2", "label": "交战", "event_type": "战斗", "date_start": "建安十一年", "place": "邺城", "participant_surfaces": ["曹操"], "passage_id": "p2", "confidence": 0.9})

    candidates = generate_candidates(store)
    dimensions = {item["dimension"] for item in candidates}
    assert {"person", "place", "time"} <= dimensions
    assert all(item["status"] == "pending" for item in candidates)
    assert all(item["left_ids"] and item["right_ids"] for item in candidates)


def test_conflict_candidates_are_persisted_separately_from_confirmed_conflicts(tmp_path):
    store = KnowledgeStore(str(tmp_path / "candidate_store.db"))
    candidate = {"id": "cc_test", "status": "pending", "dimension": "time", "left_ids": ["ev1"], "right_ids": ["ev2"], "description": "d", "reason": "r"}
    store.save_conflict_candidate(candidate, "test")
    assert store.get_conflict_candidates("pending") == [candidate]
    assert store.db.execute("SELECT COUNT(*) FROM conflicts").fetchone()[0] == 0
