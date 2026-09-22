import asyncio
import sqlite3

from historybot.domain import Passage
from historybot.graph import build_graph
from historybot.model_gateway import ModelResult
from historybot.schemas import ExtractionResponse
from historybot.store import KnowledgeStore


class RecordingGateway:
    def __init__(self):
        self.tasks = []

    async def structured(self, *, system, user, schema, task):
        self.tasks.append(task)
        if schema is ExtractionResponse:
            return ModelResult(ExtractionResponse(), "test-provider", "test-model", 0, 0, {})
        raise AssertionError(f"unexpected schema in empty extraction contract: {schema}")


def test_extraction_is_batched_without_hardcoded_historical_answers(monkeypatch):
    monkeypatch.setenv("HISTORYBOT_PASSAGE_BATCH_SIZE", "2")
    store = KnowledgeStore(":memory:")
    gateway = RecordingGateway()
    passages = [Passage(id=f"p{i}", source_id="s", ordinal=i, text=f"独立测试段落 {i}", text_hash=str(i)) for i in range(5)]
    graph = build_graph(store=store, gateway=gateway)
    result = asyncio.run(graph.ainvoke({"task_id": "contract", "question": "测试问题", "passage_text": [{"id": p.id, "source_id": p.source_id, "text": p.text} for p in passages], "passage_ids": [p.id for p in passages]}))
    assert result["status"] == "complete"
    assert gateway.tasks == ["evidence_extraction"] * 3
    assert store.counts()["claims"] == 0


def test_relation_schema_has_provenance_columns_and_is_empty_for_empty_extraction():
    store = KnowledgeStore(":memory:")
    columns = {row[1] for row in store.db.execute("PRAGMA table_info(relations)").fetchall()}
    assert {"claim_id", "valid_from", "valid_to", "inference_method", "confidence"} <= columns
    assert store.db.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 0


def test_legacy_relations_table_is_migrated_in_place(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE relations (id TEXT PRIMARY KEY, subject TEXT NOT NULL, predicate TEXT NOT NULL, object TEXT NOT NULL, payload TEXT NOT NULL)")
    connection.commit()
    connection.close()
    store = KnowledgeStore(str(path))
    columns = {row[1] for row in store.db.execute("PRAGMA table_info(relations)").fetchall()}
    assert {"claim_id", "valid_from", "valid_to", "inference_method", "confidence"} <= columns
