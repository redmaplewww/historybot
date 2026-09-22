from __future__ import annotations

import json

from historybot.bulk_extract import _completed_passages, _ensure_status_table
from historybot.store import KnowledgeStore


def test_legacy_batch_ledger_is_expanded_to_passage_ids(tmp_path) -> None:
    store = KnowledgeStore(str(tmp_path / "extract.db"))
    _ensure_status_table(store)
    assert store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='extraction_passages'"
    ).fetchone()
    store.db.execute(
        "INSERT INTO people_extraction_batches(id,status,passage_ids,payload) VALUES(?,?,?,?)",
        ("legacy_batch", "complete", json.dumps(["p_a", "p_b"]), "{}"),
    )
    store.db.commit()

    assert _completed_passages(store, "people_extraction_batches", "people_extraction_passages") == {"p_a", "p_b"}
    rows = [tuple(row) for row in store.db.execute(
        "SELECT passage_id,status,batch_id FROM people_extraction_passages ORDER BY passage_id"
    ).fetchall()]
    assert rows == [("p_a", "complete", "legacy"), ("p_b", "complete", "legacy")]
