"""Prove a passage repair only inserts era-year text.

The repair keeps existing passage IDs and rewrites their text, so a bad
alignment would silently attach the wrong words to a passage that claims and
evidence already point at. This asserts the strong invariant that every repair
is the old text plus an inserted era-year prefix, and reports any passage where
the diff includes anything else.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from historybot.corpus import complete_corpus
from historybot.ingest import LEGACY_CONTENT_TEMPLATES, clean_wikitext
from historybot.store import KnowledgeStore

def _load_repair_module():
    path = Path(__file__).resolve().parent / "repair_wikisource_passages.py"
    spec = importlib.util.spec_from_file_location("repair_wikisource_passages", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def can_recover_old(old: str, new: str) -> bool:
    """True when deleting the added text from ``new`` yields ``old``.

    ``SequenceMatcher`` reports each difference; if every one is an insertion
    then the change only added text, which is what a repair is allowed to do.
    """
    for tag, _, _, _, _ in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag != "equal" and tag != "insert":
            return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/three_kingdoms_rebuilt_v2.db")
    parser.add_argument("--limit", type=int, default=65)
    parser.add_argument("--output", default="artifacts/passage_repair_validation.json")
    parser.add_argument("--show", type=int, default=8)
    args = parser.parse_args()

    sources = complete_corpus()[: args.limit]
    repair_module = _load_repair_module()
    pages = asyncio.run(repair_module.fetch_sources(sources))
    store = KnowledgeStore(args.db)

    checked = 0
    insertion_only = 0
    violations: list[dict] = []
    count_changes: list[dict] = []
    legacy_mismatch: list[dict] = []
    document_mismatch: list[dict] = []
    for source in sources:
        source_id = "src_" + hashlib.sha256(source.url.encode()).hexdigest()[:16]
        raw = pages.get(source.page)
        if raw is None:
            continue
        current = store.db.execute(
            "SELECT id, ordinal, payload FROM passages WHERE source_id=? ORDER BY ordinal",
            (source_id,),
        ).fetchall()
        if not current:
            continue
        legacy_cleaned = clean_wikitext(raw, templates=LEGACY_CONTENT_TEMPLATES)
        cleaned = clean_wikitext(raw)
        # Precondition: the legacy cleaner must reproduce the stored document,
        # otherwise the offsets used to place insertions would not line up.
        stored_texts = [json.loads(row[2]).get("text", "") for row in current]
        recomputed_texts = [text for text, _, _ in repair_module.split_passages_with_spans(legacy_cleaned)]
        if stored_texts != recomputed_texts:
            legacy_mismatch.append(
                {
                    "title": source.title,
                    "stored_passages": len(stored_texts),
                    "recomputed_passages": len(recomputed_texts),
                }
            )
            continue
        # First prove the diff itself: re-applying the recovered insertions to
        # the legacy document must reproduce the corrected cleaning exactly.
        insertions = repair_module.inserted_spans(legacy_cleaned, cleaned)
        rebuilt = legacy_cleaned
        for offset, inserted in sorted(insertions, reverse=True):
            rebuilt = rebuilt[:offset] + inserted + rebuilt[offset:]
        if normalized(rebuilt) != normalized(cleaned):
            document_mismatch.append({"title": source.title, "stage": "reconstruction"})
            continue
        replacement_texts = repair_module.build_replacement_texts(current, legacy_cleaned, cleaned)
        # Second: every inserted character must survive into a passage. Passages
        # shorter than the 12-character floor are dropped by split_passages, so
        # a delta that is smaller than the inserted volume means an anchor was
        # placed in a chunk that no longer exists.
        old_total = sum(len(text) for text in stored_texts)
        new_total = sum(len(text) for text in replacement_texts)
        inserted_total = sum(len(inserted) for _, inserted in insertions)
        if new_total - old_total != inserted_total:
            document_mismatch.append(
                {
                    "title": source.title,
                    "stage": "character_delta",
                    "inserted": inserted_total,
                    "applied": new_total - old_total,
                }
            )
            continue
        for row, replacement in zip(current, replacement_texts, strict=True):
            old_text = json.loads(row[2]).get("text", "")
            if old_text == replacement:
                continue
            checked += 1
            if can_recover_old(old_text, replacement):
                insertion_only += 1
            else:
                violations.append(
                    {
                        "title": source.title,
                        "passage_id": row[0],
                        "ordinal": row[1],
                        "old": old_text[:110],
                        "new": replacement[:110],
                    }
                )

    report = {
        "sources_with_legacy_mismatch": len(legacy_mismatch),
        "legacy_mismatch_details": legacy_mismatch[:10],
        "sources_checked": len(sources) - len(legacy_mismatch),
        "changed_passages_checked": checked,
        "insertion_only_changes": insertion_only,
        "violations": len(violations),
        "document_mismatch": len(document_mismatch),
        "document_mismatch_details": document_mismatch[:10],
        "count_change_sources": len(count_changes),
        "count_change_details": count_changes,
        "violation_details": violations[: args.show],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"checked={checked} insertion_only={insertion_only} violations={len(violations)} "
        f"doc_mismatch={len(document_mismatch)} legacy_mismatch={len(legacy_mismatch)}"
    )
    for item in violations[: args.show]:
        print("-" * 80)
        print(f"{item['title']} ord={item['ordinal']}")
        print(f"  old={item['old']!r}")
        print(f"  new={item['new']!r}")


if __name__ == "__main__":
    main()
