from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .domain import IngestionJob, IngestionStatus
from .graph import build_graph, development_checkpointer
from .ingestion import IngestionService
from .model_gateway import ModelGateway
from .store import KnowledgeStore


@dataclass
class HistoryResearchAgent:
    """Application boundary for ingestion, LangGraph orchestration, and persistence.

    The graph receives only passages that have already been committed by the
    ingestion service. Run metadata is persisted independently of extracted
    facts so a failed model call is observable and resumable by an outer worker.
    """

    store: KnowledgeStore
    ingestion: IngestionService | None = None
    gateway: ModelGateway | None = None
    checkpointer_factory: Callable[[], Any] = development_checkpointer

    def __post_init__(self) -> None:
        if self.ingestion is None:
            self.ingestion = IngestionService(self.store)
        if self.gateway is None:
            self.gateway = ModelGateway()

    async def ingest_and_research(
        self,
        source: str | bytes | Path,
        *,
        question: str,
        name: str | None = None,
        media_type: str | None = None,
        template: str = "historical",
        options: dict[str, Any] | None = None,
        max_passages: int = 0,
    ) -> dict[str, Any]:
        """Ingest one source, then analyze its committed passages if eligible."""
        assert self.ingestion is not None
        run_id = f"run_{uuid.uuid4().hex}"
        self.store.save_agent_run(
            run_id,
            status="received",
            question=question,
            payload={"stage": "ingestion"},
        )
        try:
            job = await self.ingestion.submit(
                source,
                name=name,
                media_type=media_type,
                template=template,
                options=options,
            )
            job = await self.ingestion.run(job)
            source_model = self.store.get_source_for_ingestion_job(job.id)
            if job.status is not IngestionStatus.ACCEPTED or source_model is None:
                payload = {
                    "run_id": run_id,
                    "status": "awaiting_ingestion_review",
                    "ingestion_job": job.model_dump(mode="json"),
                    "questions": [item.model_dump(mode="json") for item in self.store.get_questions(job.id, status="open")],
                }
                self.store.save_agent_run(
                    run_id,
                    status=payload["status"],
                    question=question,
                    ingestion_job_id=job.id,
                    payload=payload,
                )
                return payload
            return await self.analyze_source(
                source_model.id,
                question=question,
                max_passages=max_passages,
                run_id=run_id,
                ingestion_job=job,
            )
        except Exception as error:
            self.store.save_agent_run(
                run_id,
                status="failed",
                question=question,
                payload={"stage": "ingestion"},
                error=f"{type(error).__name__}: {error}",
            )
            raise

    async def analyze_source(
        self,
        source_id: str,
        *,
        question: str,
        max_passages: int = 0,
        run_id: str | None = None,
        ingestion_job: IngestionJob | None = None,
    ) -> dict[str, Any]:
        """Run the graph against a source already present in the database."""
        source = self.store.get_source(source_id)
        if source is None:
            raise KeyError(f"unknown source: {source_id}")
        passages = self.store.get_passages(source_id)
        if max_passages > 0:
            passages = passages[:max_passages]
        if not passages:
            raise ValueError(f"source has no committed passages: {source_id}")
        return await self.analyze_passages(
            passages,
            question=question,
            run_id=run_id,
            source_id=source_id,
            ingestion_job=ingestion_job,
            source_title=source.title,
        )

    async def analyze_passages(
        self,
        passages: list,
        *,
        question: str,
        run_id: str | None = None,
        source_id: str | None = None,
        ingestion_job: IngestionJob | None = None,
        source_title: str | None = None,
    ) -> dict[str, Any]:
        """Analyze a caller-selected set of passages that already exists in storage."""
        persisted_ids = {passage.id for passage in self.store.get_passages()}
        if not passages or any(passage.id not in persisted_ids for passage in passages):
            raise ValueError("agent input must contain only committed passages")

        run_id = run_id or f"run_{uuid.uuid4().hex}"
        self.store.save_agent_run(
            run_id,
            status="running",
            question=question,
            ingestion_job_id=ingestion_job.id if ingestion_job else None,
            source_id=source_id,
            passage_count=len(passages),
            payload={"stage": "graph", "source_title": source_title},
        )
        state = {
            "task_id": run_id,
            "question": question,
            "passage_text": [
                {"id": passage.id, "source_id": passage.source_id, "text": passage.text}
                for passage in passages
            ],
            "passage_ids": [passage.id for passage in passages],
        }
        try:
            graph = build_graph(
                store=self.store,
                gateway=self.gateway or ModelGateway(),
                checkpointer=self.checkpointer_factory(),
            )
            result = await graph.ainvoke(
                state,
                config={"configurable": {"thread_id": run_id}},
            )
            payload = {
                "run_id": run_id,
                "status": result.get("status", "complete"),
                "source_id": source_id,
                "passage_count": len(passages),
                "conflicts": len(result.get("conflicts", [])),
                "counts": self.store.counts(),
            }
            self.store.save_agent_run(
                run_id,
                status=payload["status"],
                question=question,
                ingestion_job_id=ingestion_job.id if ingestion_job else None,
                source_id=source_id,
                passage_count=len(passages),
                payload=payload,
            )
            return payload
        except (TimeoutError, httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError) as error:
            self.store.save_agent_run(
                run_id,
                status="failed",
                question=question,
                ingestion_job_id=ingestion_job.id if ingestion_job else None,
                source_id=source_id,
                passage_count=len(passages),
                payload={"stage": "graph"},
                error=f"{type(error).__name__}: {error}",
            )
            raise

    async def analyze_all(
        self, *, question: str, per_source: int = 0, run_id: str | None = None
    ) -> dict[str, Any]:
        """Analyze persisted sources through the same boundary used by ingestion."""
        run_id = run_id or f"run_{uuid.uuid4().hex}"
        source_ids = [source.id for source in self.store.get_sources()]
        if not source_ids:
            raise ValueError("database has no sources")
        selected = []
        for source_id in source_ids:
            passages = self.store.get_passages(source_id)
            if per_source <= 0 or per_source >= len(passages):
                selected.extend(passages)
                continue
            indexes = (
                {
                    round(index * (len(passages) - 1) / (per_source - 1))
                    for index in range(per_source)
                }
                if per_source > 1
                else {len(passages) // 2}
            )
            selected.extend(passages[index] for index in sorted(indexes))
        result = await self.analyze_passages(selected, question=question, run_id=run_id)
        return {**result, "sources": len(source_ids), "sample_per_source": per_source}
