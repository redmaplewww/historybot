"""Check that a text repair keeps passage identity aligned.

``repair_wikisource_passages`` recomputes passages from freshly cleaned
wikitext. If the cleaner changes the text, chunking can shift and passage IDs
could be paired with the wrong text. This reports, per source, whether the
chunk count is unchanged and which passages differ, so a repair cannot silently
misalign the corpus.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import asyncio
import importlib.util

from historybot.ingest import clean_wikitext, split_passages
from historybot.store import KnowledgeStore
from historybot.corpus import complete_corpus


def _load_repair_module():
    path = Path(__file__).resolve().parent / "repair_wikisource_passages.py"
    spec = importlib.util.spec_from_file_location("repair_wikisource_passages", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/three_kingdoms_rebuilt_v2.db")
    parser.add_argument("--limit", type=int, default=65)
    parser.add_argument("--show", type=int, default=3, help="sources to show diffs for")
    parser.add_argument("--output", default="artifacts/passage_realignment_audit.json")
    args = parser.parse_args()

    import hashlib

    sources = complete_corpus()[: args.limit]
    fetch_sources = _load_repair_module().fetch_sources
    pages = asyncio.run(fetch_sources(sources))
    store = KnowledgeStore(args.db)

    rows = []
    misaligned = []
    for source in sources:
        source_id = "src_" + hashlib.sha256(source.url.encode()).hexdigest()[:16]
        raw = pages.get(source.page)
        if raw is None:
            continue
        corrected = split_passages(clean_wikitext(raw), source_id=source_id)
        current = store.db.execute(
            "SELECT id, ordinal, payload FROM passages WHERE source_id=? ORDER BY ordinal",
            (source_id,),
        ).fetchall()
        count_match = len(current) == len(corrected)
        differing = []
        if count_match:
            for row, new in zip(current, corrected, strict=True):
                old_text = json.loads(row[2]).get("text", "")
                if old_text != new.text:
                    differing.append({"ordinal": row[1], "old": old_text, "new": new.text})
        entry = {
            "title": source.title,
            "source_id": source_id,
            "current_passages": len(current),
            "recomputed_passages": len(corrected),
            "count_match": count_match,
            "differing": len(differing),
        }
        rows.append(entry)
        if not count_match:
            misaligned.append(entry)
        if differing and len([r for r in rows if r.get("sample")]) < args.show:
            entry["sample"] = differing[:4]

    report = {
        "sources": len(rows),
        "sources_with_count_change": len(misaligned),
        "count_change_titles": [r["title"] for r in misaligned],
        "total_differing_passages": sum(r["differing"] for r in rows),
        "details": rows,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"sources={report['sources']} count_changes={len(misaligned)} differing={report['total_differing_passages']}")
    for r in rows:
        if r.get("sample"):
            print("=" * 80)
            print(f"{r['title']} differing={r['differing']}")
            for item in r["sample"]:
                print(f"  ord={item['ordinal']}")
                print(f"    old={item['old'][:90]!r}")
                print(f"    new={item['new'][:90]!r}")


if __name__ == "__main__":
    main()
