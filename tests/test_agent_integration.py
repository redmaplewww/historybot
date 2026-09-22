from __future__ import annotations

import asyncio

from historybot.agent import HistoryResearchAgent
from historybot.domain import Passage, Source, SourceKind
from historybot.ingestion import IngestionService
from historybot.model_gateway import ModelResult
from historybot.policies import assess_source
from historybot.schemas import ContradictionResponse, ExtractionResponse, ReviewResponse
from historybot.store import KnowledgeStore


class EmptyGateway:
    calls: list[str]

    def __init__(self) -> None:
        self.calls = []

    async def structured(self, *, schema, task, **kwargs):
        self.calls.append(task)
        if schema is ExtractionResponse:
            value = ExtractionResponse()
        elif schema is ReviewResponse:
            value = ReviewResponse(
                claim_id="unused",
                evidence_sufficient=False,
                polarity="unrelated",
                revised_confidence=0,
                rationale="没有候选主张",
            )
        else:
            value = ContradictionResponse()
        return ModelResult(value, "test", "test", 0, 0, {})


def test_agent_analyzes_only_persisted_source_and_records_run(tmp_path):
    store = KnowledgeStore(str(tmp_path / "agent.db"))
    source = Source(
        id="source_1",
        title="测试史料",
        kind=SourceKind.COMPILATION,
        metadata={"assessment": assess_source(SourceKind.COMPILATION)},
    )
    passage = Passage(
        id="passage_1",
        source_id=source.id,
        ordinal=0,
        text="甲没有提出可抽取的主张。",
        text_hash="hash",
    )
    store.save_source(source)
    store.save_passages([passage])
    gateway = EmptyGateway()
    agent = HistoryResearchAgent(store=store, gateway=gateway)

    result = asyncio.run(agent.analyze_source(source.id, question="只抽取原文"))

    assert result["status"] == "complete"
    assert result["passage_count"] == 1
    assert gateway.calls == ["evidence_extraction"]
    run = store.get_agent_run(result["run_id"])
    assert run is not None
    assert run["status"] == "complete"
    assert run["source_id"] == source.id


def test_agent_stops_when_ingestion_requires_clarification(tmp_path):
    store = KnowledgeStore(str(tmp_path / "agent_gate.db"))
    gateway = EmptyGateway()
    agent = HistoryResearchAgent(
        store=store,
        ingestion=IngestionService(store),
        gateway=gateway,
    )

    result = asyncio.run(
        agent.ingest_and_research(
            "一段没有出处的材料。",
            name="note.txt",
            media_type="text/plain",
            question="它描述了什么事件？",
        )
    )

    assert result["status"] == "awaiting_ingestion_review"
    assert gateway.calls == []
    run = store.get_agent_run(result["run_id"])
    assert run is not None
    assert run["status"] == "awaiting_ingestion_review"
