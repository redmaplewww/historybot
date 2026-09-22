from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

import httpx

from .agent import HistoryResearchAgent
from .domain import SourceKind
from .ingest import ingest_url
from .store import KnowledgeStore

TRIPLE_SOURCES = [
    ("https://zh.wikisource.org/w/api.php?action=parse&page=三國志%2F卷01&prop=wikitext&format=json", "三国志·魏书武帝纪", "陈寿", SourceKind.COMPILATION),
    ("https://zh.wikisource.org/w/api.php?action=parse&page=三國志%2F卷32&prop=wikitext&format=json", "三国志·蜀书先主传", "陈寿", SourceKind.COMPILATION),
    ("https://zh.wikisource.org/w/api.php?action=parse&page=三國演義%2F第043回&prop=wikitext&format=json", "三国演义·第四十三回", "罗贯中", SourceKind.FOLK),
    ("https://zh.wikisource.org/w/api.php?action=parse&page=三國演義%2F第044回&prop=wikitext&format=json", "三国演义·第四十四回", "罗贯中", SourceKind.FOLK),
]


async def main(db: str, question: str, max_passages: int = 0, source_id: str | None = None) -> None:
    task_id = str(uuid.uuid4())
    store = KnowledgeStore(db)
    agent = HistoryResearchAgent(store=store)
    for url, title, author, kind in TRIPLE_SOURCES:
        try:
            await ingest_url(url, title=title, author=author, kind=kind, store=store, task_id=task_id)
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            print(f"source failed: {url}: {exc}")
    try:
        if source_id:
            result = await agent.analyze_source(source_id, question=question, max_passages=max_passages, run_id=task_id)
        else:
            result = await agent.analyze_all(question=question, per_source=max_passages, run_id=task_id)
        report = {"task_id": task_id, "status": result.get("status"), "conflicts": result.get("conflicts", 0), "counts": store.counts(), "sources": [source.model_dump(mode="json") for source in store.get_sources()]}
        Path(db).with_suffix(".report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(report)
    except httpx.TimeoutException as exc:
        counts = store.counts()
        status = "pending_contradictions" if counts["claims"] > 0 else "failed"
        store.audit(task_id, "pipeline_pending" if status == "pending_contradictions" else "pipeline_failed", task_id, {"error_type": type(exc).__name__, "message": str(exc)})
        report = {"task_id": task_id, "status": status, "error_type": type(exc).__name__, "error": str(exc), "counts": counts, "sources": [source.model_dump(mode="json") for source in store.get_sources()]}
        Path(db).with_suffix(".report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(report)
    except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        store.audit(task_id, "pipeline_failed", task_id, {"error_type": type(exc).__name__, "message": str(exc)})
        report = {"task_id": task_id, "status": "failed", "error_type": type(exc).__name__, "error": str(exc), "counts": store.counts(), "sources": [source.model_dump(mode="json") for source in store.get_sources()]}
        Path(db).with_suffix(".report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/historybot.db")
    parser.add_argument("--question", default="刘备、曹操、孙权在赤壁之战前后的关系与事件链是什么？三国演义中哪些叙述需要正史证据校验？")
    parser.add_argument("--max-passages", type=int, default=0, help="0 means all ingested passages")
    parser.add_argument("--source-id", default=None, help="limit analysis to one already-ingested source id")
    args = parser.parse_args()
    asyncio.run(main(args.db, args.question, args.max_passages, args.source_id))
