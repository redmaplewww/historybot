from __future__ import annotations

import asyncio
from pathlib import Path

from historybot.ai_ingestion import AIIngestionService
from historybot.domain import Passage, Source, SourceKind
from historybot.store import KnowledgeStore
from historybot.v2_adapters import SQLiteMemoryStore, WikiStore
from historybot.v2_schemas import ReadingResponse, VerificationResponse


class FakeGateway:
    def __init__(self):
        self.calls = []

    async def structured(self, *, task, system, user, schema):
        self.calls.append(task)
        if schema is ReadingResponse:
            ids = [line.split("]", 1)[0][1:] for line in user.splitlines() if line.startswith("[p_")]
            value = ReadingResponse(
                units=[{
                    "unit_id": f"u_{i}",
                    "passage_id": pid,
                    "reading_summary": "原文明确记载",
                    "reasoning_summary": "直接引文",
                    "evidence_quotes": ["曹操至赤壁。"],
                    "mentions": [{"surface": "曹操", "entity_type": "人物", "passage_id": pid, "confidence": 0.99}],
                    "identity_links": [{"canonical_surface": "曹操", "alias_surface": "太祖", "relation": "简称", "passage_id": pid, "confidence": 0.99}],
                    "events": [{"event_type": "抵达", "label": "曹操至赤壁", "place": "赤壁", "participant_surfaces": ["曹操"], "passage_id": pid, "confidence": 0.98}],
                    "claims": [{"id": "model_claim_1", "subject": "曹操", "predicate": "到达", "object": "赤壁", "statement": "曹操到达赤壁", "passage_ids": [pid], "confidence": 0.98}],
                } for i, pid in enumerate(ids)],
                document_summary="史料摘要",
            )
        else:
            value = VerificationResponse(items=[{"unit_id": "u_0", "supported": True, "rationale": "引文可核验"}], overall_supported=True, summary="通过")
        return type("Result", (), {"model": "fake", "value": value, "raw": {}})()


def test_ai_reading_persists_steps_drafts_wiki_and_memory(tmp_path: Path):
    store = KnowledgeStore(str(tmp_path / "db.sqlite"))
    source = Source(id="src_test", title="测试史料", kind=SourceKind.PRIMARY)
    store.save_source(source)
    store.save_passages([Passage(id="p_1", source_id=source.id, ordinal=0, text="曹操至赤壁。", text_hash="x")])
    gateway = FakeGateway()
    service = AIIngestionService(store, gateway=gateway, wiki=WikiStore(tmp_path / "wiki"), memories=SQLiteMemoryStore(store.db))
    result = asyncio.run(service.run(job_id=None, source_id=source.id))
    assert result["status"] == "verified"
    assert gateway.calls == ["historical_reading", "historical_reading_verification"]
    assert len(store.get_v2_steps(result["run_id"])) == 4
    assert result["graph"] == {"entities": 1, "aliases": 1, "events": 1, "claims": 1, "evidence": 1}
    assert store.db.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM entity_aliases").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1
    assert store.db.execute("SELECT passage_id FROM evidence").fetchone()[0] == "p_1"
    claim_payload = store.db.execute("SELECT payload FROM claims").fetchone()[0]
    assert '"event_id": "evt_v2_' in claim_payload
    assert (tmp_path / "wiki" / "sources" / "测试史料.md").exists()
    assert store.db.execute("SELECT COUNT(*) FROM v2_memories").fetchone()[0] == 1


def test_ai_reading_does_not_reuse_stale_checkpoints(tmp_path: Path):
    store = KnowledgeStore(str(tmp_path / "db.sqlite"))
    source = Source(id="src_test", title="测试史料", kind=SourceKind.PRIMARY)
    store.save_source(source)
    store.save_passages([Passage(id="p_1", source_id=source.id, ordinal=0, text="曹操至赤壁。", text_hash="x")])
    store.save_v2_step(
        "ckpt_read_p_1",
        run_id="old_run",
        ordinal=0,
        step_type="read",
        input_ref="p_1",
        output={"units": [{"unit_id": "old", "passage_id": "p_1", "evidence_quotes": ["旧引文"]}]},
        status="stale",
    )
    store.save_v2_step(
        "ckpt_verify_p_1",
        run_id="old_run",
        ordinal=0,
        step_type="verify",
        input_ref="p_1",
        output={"items": [{"unit_id": "old", "supported": True, "rationale": "旧复核"}]},
        status="stale",
    )

    gateway = FakeGateway()
    service = AIIngestionService(
        store,
        gateway=gateway,
        wiki=WikiStore(tmp_path / "wiki"),
        memories=SQLiteMemoryStore(store.db),
    )
    result = asyncio.run(service.run(job_id=None, source_id=source.id))

    assert result["status"] == "verified"
    assert gateway.calls == ["historical_reading", "historical_reading_verification"]
    assert store.get_v2_step("ckpt_read_p_1")["status"] == "complete"
    assert store.get_v2_step("ckpt_verify_p_1")["status"] == "complete"
