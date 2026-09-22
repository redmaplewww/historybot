from __future__ import annotations

import argparse
import asyncio
import json
import os

from historybot.bulk_extract import extract_corpus
from historybot.entity_resolution import resolve_entities
from historybot.store import KnowledgeStore


async def run(db: str, batch_size: int, concurrency: int, retries: int) -> None:
    phases = (("people", True), ("evidence", False))
    for name, people_only in phases:
        print(json.dumps({"phase": name, "status": "started"}, ensure_ascii=False), flush=True)
        result = await extract_corpus(
            db,
            batch_size=batch_size,
            concurrency=concurrency,
            retries=retries,
            people_only=people_only,
        )
        # Rebuild source-grounded identity links at phase boundaries so the
        # read model can merge name/courtesy-name variants without scanning all
        # passages for every individual batch.
        resolve_entities(db, derive_links=True)
        ledger_table = "people_extraction_passages" if people_only else "extraction_passages"
        store = KnowledgeStore(db)
        completed = store.db.execute(
            f"SELECT COUNT(*) FROM {ledger_table} WHERE status='complete'"
        ).fetchone()[0]
        total = store.db.execute("SELECT COUNT(*) FROM passages").fetchone()[0]
        store.db.close()
        if result["failed_batches"] or completed < total:
            raise RuntimeError(
                f"{name} phase incomplete: {completed}/{total} passages, "
                f"{result['failed_batches']} failed batches"
            )
        print(json.dumps({"phase": name, "status": "finished", **result}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run resumable people then evidence extraction")
    parser.add_argument("--db", required=True)
    parser.add_argument("--batch-size", type=int, default=int(os.getenv("HISTORYBOT_EXTRACT_BATCH_SIZE", "100")))
    parser.add_argument("--concurrency", type=int, default=int(os.getenv("HISTORYBOT_LLM_CONCURRENCY", "2")))
    parser.add_argument("--retries", type=int, default=int(os.getenv("HISTORYBOT_EXTRACT_RETRIES", "0")))
    args = parser.parse_args()
    asyncio.run(run(args.db, args.batch_size, args.concurrency, args.retries))
