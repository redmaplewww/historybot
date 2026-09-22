from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path

import httpx

from .corpus import CorpusSource, complete_corpus
from .domain import Source
from .ingest import clean_wikitext, split_passages, stable_id
from .policies import assess_source
from .store import KnowledgeStore


def _chunks(items: list[CorpusSource], size: int) -> list[list[CorpusSource]]:
    iterator = iter(items)
    return [list(chunk) for chunk in iter(lambda: list(islice(iterator, size)), [])]


async def _fetch_batch(sources: list[CorpusSource], client: httpx.AsyncClient) -> dict[str, tuple[str, str]]:
    response = await client.get(
        "https://zh.wikisource.org/w/api.php",
        params={
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
            "titles": "|".join(source.page for source in sources),
        },
    )
    response.raise_for_status()
    pages = response.json().get("query", {}).get("pages", [])
    return {
        page["title"]: (page["title"], page["revisions"][0]["slots"]["main"]["content"])
        for page in pages
        if page.get("revisions")
    }


async def _fetch_one(source: CorpusSource, pages: dict[str, tuple[str, str]]) -> tuple[CorpusSource, str, str] | Exception:
    try:
        fetched_title, wikitext = pages[source.page]
        text = clean_wikitext(wikitext)
        if "\ufffd" in text:
            raise ValueError(f"replacement character found in decoded source: {source.page}")
        return source, fetched_title, text
    except (KeyError, ValueError, TypeError) as error:
        return error


async def ingest_corpus(
    db: str,
    *,
    concurrency: int = 2,
    sources: tuple[CorpusSource, ...] | None = None,
) -> dict:
    task_id = str(uuid.uuid4())
    catalog = sources or complete_corpus()
    store = KnowledgeStore(db)
    existing_ids = {source.id for source in store.get_sources()}
    pending = [source for source in catalog if stable_id("src", source.url) not in existing_ids]
    pages: dict[str, tuple[str, str]] = {}
    failed: list[dict] = []
    headers = {"User-Agent": "historybot-research/0.2 (local corpus import)"}
    async with httpx.AsyncClient(timeout=90, headers=headers) as client:
        batches = _chunks(pending, 20)
        for index, batch in enumerate(batches, start=1):
            try:
                pages.update(await _fetch_batch(batch, client))
            except httpx.HTTPError as error:
                failed.extend(
                    {"page": source.page, "error_type": type(error).__name__, "error": str(error)}
                    for source in batch
                )
            if index < len(batches):
                await asyncio.sleep(max(0.2, 0.6 / max(1, concurrency)))
    results = await asyncio.gather(*(_fetch_one(source, pages) for source in pending))
    ingested: list[dict] = []
    for result in results:
        if isinstance(result, Exception):
            failed.append({"error_type": type(result).__name__, "error": str(result)})
            continue
        source_spec, fetched_title, text = result
        source = Source(
            id=stable_id("src", source_spec.url),
            title=source_spec.title or fetched_title,
            author=source_spec.author,
            url=source_spec.url,
            kind=source_spec.kind,
            access_date=datetime.now(UTC),
            metadata={
                "assessment": assess_source(source_spec.kind),
                "collection": source_spec.collection,
                "sequence": source_spec.sequence,
                "fetched_title": fetched_title,
            },
        )
        passages = split_passages(text, source_id=source.id)
        store.save_source(source, task_id)
        store.save_passages(passages, task_id)
        ingested.append(
            {
                "source_id": source.id,
                "title": source.title,
                "kind": source.kind.value,
                "characters": len(text),
                "passages": len(passages),
            }
        )

    report = {
        "task_id": task_id,
        "status": "complete" if not failed else "partial",
        "catalog_size": len(catalog),
        "already_present": len(catalog) - len(pending),
        "ingested_sources": len(ingested),
        "failed_sources": failed,
        "ingested": ingested,
        "counts": store.counts(),
    }
    report_path = Path(db).with_suffix(".ingest.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest the complete Sanguozhi corpus plus literary comparison sources")
    parser.add_argument("--db", default="data/three_kingdoms_complete.db")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(ingest_corpus(args.db, concurrency=args.concurrency)), ensure_ascii=False, indent=2))
