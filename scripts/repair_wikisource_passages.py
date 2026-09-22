from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from difflib import SequenceMatcher
from itertools import islice
from pathlib import Path

import httpx

from historybot.corpus import CorpusSource, complete_corpus
from historybot.ingest import (
    LEGACY_CONTENT_TEMPLATES,
    clean_wikitext,
    inserted_spans,
    raw_to_stored_offset,
    split_passages_with_spans,
    strip_markup_residue,
)
from historybot.store import KnowledgeStore


def chunks(items: tuple[CorpusSource, ...], size: int):
    iterator = iter(items)
    while batch := tuple(islice(iterator, size)):
        yield batch


def map_boundary(opcodes: list[tuple], text_length: int, position: int, right_edge: bool = False) -> int:
    for tag, old_start, old_end, new_start, new_end in opcodes:
        if old_start <= position <= old_end:
            if tag == "equal":
                return new_start + position - old_start
            return new_end if right_edge else new_start
    return text_length if right_edge else 0


def build_replacement_texts(current_rows: list, legacy_text: str, corrected_text: str) -> list[str]:
    """Insert the newly preserved template text into each stored passage.

    Passage IDs are already referenced by claims and evidence, and the row set
    must not move, so this never re-chunks: it locates each insertion in the
    legacy document, maps it into the passage that contains it, and rewrites
    that passage's text as old text plus insertion. The mapping is verified
    against the stored text, so a bad offset raises instead of splicing an
    anchor into the middle of a word.
    """
    insertions = inserted_spans(legacy_text, corrected_text)
    stored_texts = [json.loads(row[2]).get("text", "") for row in current_rows]
    spans = split_passages_with_spans(legacy_text)
    if [text for text, _, _ in spans] != stored_texts:
        raise ValueError(
            "legacy cleaning does not reproduce the stored passages; "
            "refusing to repair because offsets would not align"
        )
    if not insertions:
        return stored_texts

    grouped: list[list[tuple[int, str]]] = [[] for _ in current_rows]
    for offset, inserted in insertions:
        # Spans are contiguous, so a passage's end equals the next one's start.
        # Treating the end as exclusive keeps an anchor that opens a passage
        # (for example the 初平元年 prefix before 春正月) with that passage
        # instead of appending it to the previous one.
        target = next(
            (
                index
                for index, (_, start, end) in enumerate(spans)
                if start <= offset < end or (index == len(spans) - 1 and offset == end)
            ),
            len(current_rows) - 1,
        )
        _, span_start, _ = spans[target]
        local = raw_to_stored_offset(legacy_text[span_start:offset], offset - span_start)
        grouped[target].append((local, inserted))

    replacements = []
    for text, edits in zip(stored_texts, grouped, strict=True):
        for local, inserted in sorted(edits, reverse=True):
            text = text[:local] + inserted + text[local:]
        replacements.append(text)
    return replacements


async def fetch_batch(
    client: httpx.AsyncClient, batch: tuple[CorpusSource, ...], retries: int = 4
) -> dict[str, str]:
    params = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "prop": "revisions",
        "rvprop": "content",
        "rvslots": "main",
        "titles": "|".join(source.page for source in batch),
    }
    for attempt in range(retries + 1):
        response = await client.get("https://zh.wikisource.org/w/api.php", params=params)
        if response.status_code != 429 or attempt == retries:
            response.raise_for_status()
            break
        await asyncio.sleep(2**attempt)
    pages = response.json().get("query", {}).get("pages", [])
    return {
        page["title"]: page["revisions"][0]["slots"]["main"]["content"]
        for page in pages
        if page.get("revisions")
    }


async def fetch_sources(sources: tuple[CorpusSource, ...]) -> dict[str, str]:
    headers = {"User-Agent": "historybot-research/0.3 (local corpus repair)"}
    pages: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=90, headers=headers) as client:
        for batch in chunks(sources, 10):
            pages.update(await fetch_batch(client, batch))
            await asyncio.sleep(1.0)
    return pages


def repair(
    db_path: str,
    sources: tuple[CorpusSource, ...],
    pages: dict[str, str],
    *,
    dry_run: bool = False,
) -> dict:
    store = KnowledgeStore(db_path)
    report = {
        "db": db_path,
        "started_at": datetime.now(UTC).isoformat(),
        "dry_run": dry_run,
        "sources": len(sources),
        "updated_sources": 0,
        "updated_passages": 0,
        "skipped_sources": [],
        "insertion_skipped_sources": [],
        "proper_noun_templates": 0,
        "era_year_templates": 0,
        "changed_sources": [],
        "invalidated_people_passages": 0,
    }
    task_id = "wikisource_text_repair_" + hashlib.sha256(db_path.encode()).hexdigest()[:12]
    for source in sources:
        source_id = "src_" + hashlib.sha256(source.url.encode()).hexdigest()[:16]
        raw = pages.get(source.page)
        if raw is None:
            report["skipped_sources"].append({"source": source.title, "reason": "page_missing"})
            continue
        report["proper_noun_templates"] += raw.count("{{ProperNoun|")
        report["era_year_templates"] += raw.count("{{YL|")
        # Recompute the text that produced the stored rows, then the corrected
        # text. The difference between them is only what a newly preserved
        # template renders, which is what gets inserted into the passages.
        legacy_cleaned = clean_wikitext(raw, templates=LEGACY_CONTENT_TEMPLATES)
        cleaned = clean_wikitext(raw)
        current_rows = store.db.execute(
            "SELECT id, ordinal, payload FROM passages WHERE source_id=? ORDER BY ordinal", (source_id,)
        ).fetchall()
        if not current_rows:
            report["skipped_sources"].append({"source": source.title, "reason": "source_not_in_db"})
            continue
        stored_texts = [json.loads(row[2]).get("text", "") for row in current_rows]
        # Phase 1: restore text that a newly preserved template renders. This
        # needs the stored rows to be reproducible from the legacy cleaning, so
        # an insertion cannot land at the wrong offset. Rows already repaired,
        # or produced by an older cleaner, fail that check and are left alone.
        replacement_texts = stored_texts
        if [text for text, _, _ in split_passages_with_spans(legacy_cleaned)] == stored_texts:
            replacement_texts = build_replacement_texts(current_rows, legacy_cleaned, cleaned)
        else:
            report["insertion_skipped_sources"].append(
                {"source": source.title, "reason": "stored rows are not reproducible from the legacy cleaning"}
            )
        # Phase 2: strip markup residue. This is per passage and idempotent, so
        # it runs on every row regardless of phase 1's outcome.
        replacement_texts = [strip_markup_residue(text) for text in replacement_texts]
        changed = 0
        changed_ids: list[str] = []
        for current, replacement_text in zip(current_rows, replacement_texts, strict=True):
            payload = json.loads(current[2])
            # Markup residue is stripped per passage: it renders as nothing, so
            # removing it cannot move a boundary or empty a passage.
            replacement_text = strip_markup_residue(replacement_text)
            if payload.get("text") == replacement_text:
                continue
            if dry_run:
                changed += 1
                continue
            payload["text"] = replacement_text
            payload["text_hash"] = hashlib.sha256(replacement_text.encode()).hexdigest()
            store.db.execute("UPDATE passages SET payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), current[0]))
            store.audit(task_id, "passage_text_repaired", current[0], {"source_id": source_id})
            changed_ids.append(current[0])
            changed += 1
        if not dry_run:
            store.db.commit()
            _invalidate_passage_ledgers(store, changed_ids)
        report["invalidated_people_passages"] += len(changed_ids)
        report["updated_sources"] += int(changed > 0)
        report["updated_passages"] += changed
        if changed:
            report["changed_sources"].append({"source": source.title, "changed_passages": changed})
    store.db.close()
    report["finished_at"] = datetime.now(UTC).isoformat()
    return report


def _invalidate_passage_ledgers(store: KnowledgeStore, passage_ids: list[str]) -> None:
    """Remove changed passages from resumable ledgers without touching stored evidence."""

    if not passage_ids:
        return
    changed = set(passage_ids)
    for passage_table, batch_table in (
        ("people_extraction_passages", "people_extraction_batches"),
        ("extraction_passages", "extraction_batches"),
    ):
        store.db.executemany(f"DELETE FROM {passage_table} WHERE passage_id=?", [(item,) for item in changed])
        for row in store.db.execute(f"SELECT id, passage_ids FROM {batch_table}").fetchall():
            ids = [item for item in json.loads(row[1]) if item not in changed]
            if len(ids) == len(json.loads(row[1])):
                continue
            if ids:
                store.db.execute(f"UPDATE {batch_table} SET passage_ids=? WHERE id=?", (json.dumps(ids), row[0]))
            else:
                store.db.execute(f"DELETE FROM {batch_table} WHERE id=?", (row[0],))
        store.db.commit()


async def main(db_path: str, *, dry_run: bool = False, historical_only: bool = True) -> dict:
    corpus = complete_corpus()
    sources = corpus[:65] if historical_only else corpus
    pages = await fetch_sources(sources)
    return repair(db_path, sources, pages, dry_run=dry_run)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Restore Wikisource proper-name template content in-place")
    parser.add_argument("--db", required=True)
    parser.add_argument("--dry-run", action="store_true", help="report the diff without writing")
    parser.add_argument(
        "--include-literary",
        action="store_true",
        help="also repair the bounded 三国演义 comparison chapters",
    )
    args = parser.parse_args()
    report = asyncio.run(
        main(args.db, dry_run=args.dry_run, historical_only=not args.include_literary)
    )
    suffix = ".passage_repair_dry_run.json" if args.dry_run else ".passage_repair.json"
    output = Path(args.db).with_suffix(suffix)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
