from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

import httpx

from .agent import HistoryResearchAgent
from .store import KnowledgeStore


def balanced_passages(store: KnowledgeStore, per_source: int) -> list:
    """Choose evenly spaced passages per source without fabricating text or claims."""
    selected = []
    for source in store.get_sources():
        passages = store.get_passages(source.id)
        if not passages:
            continue
        count = min(max(1, per_source), len(passages))
        if count == 1:
            indexes = {len(passages) // 2}
        else:
            indexes = {
                round(index * (len(passages) - 1) / (count - 1)) for index in range(count)
            }
        selected.extend(passages[index] for index in sorted(indexes))
    return selected


async def analyze_existing(db: str, *, per_source: int = 2, question: str) -> dict:
    store = KnowledgeStore(db)
    agent = HistoryResearchAgent(store=store)
    task_id = str(uuid.uuid4())
    store.audit(task_id, "analysis_requested", task_id, {"per_source": per_source})
    try:
        result = await agent.analyze_all(question=question, per_source=per_source, run_id=task_id)
        report = {
            "task_id": task_id,
            "status": result["status"],
            "sample": {"per_source": per_source, "sources": result["sources"]},
            "conflicts": sum(item.get("conflicts", 0) for item in result["results"]),
            "counts": store.counts(),
            "sources": [source.model_dump(mode="json") for source in store.get_sources()],
        }
    except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError) as error:
        store.audit(task_id, "pipeline_failed", task_id, {"error_type": type(error).__name__, "message": str(error)})
        report = {
            "task_id": task_id,
            "status": "failed",
            "error_type": type(error).__name__,
            "error": str(error),
            "sample": {"per_source": per_source, "sources": len(store.get_sources())},
            "counts": store.counts(),
        }
    Path(db).with_suffix(".report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run evidence extraction on a balanced sample of an ingested corpus")
    parser.add_argument("--db", required=True)
    parser.add_argument("--per-source", type=int, default=2)
    parser.add_argument(
        "--question",
        default="比较正史编纂与文学传统对人物关系、事件链和地点的记载，只抽取段落明确表达的内容，并保留证据出处。",
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(analyze_existing(args.db, per_source=args.per_source, question=args.question)), ensure_ascii=False, indent=2))
