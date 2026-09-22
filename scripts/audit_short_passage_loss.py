"""Measure source text dropped by the passage-length floor.

``split_passages`` discards chunks shorter than 12 characters. Some of those
chunks are complete chronicle entries ("四年春，軍鄄城。" — the year boundary
for the following year), so the floor silently deletes real text. This reports
how much text and how many year-bearing entries are lost.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from historybot.ingest import clean_wikitext
from historybot.repair import YEAR_WORD_PATTERN, era_absolute_anchor
from historybot.store import KnowledgeStore

MIN_LENGTH = 12


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", nargs="*", default=["三國志/卷01", "三國志/卷32"])
    parser.add_argument("--db", default="data/three_kingdoms_rebuilt_v2.db")
    parser.add_argument("--output", default="artifacts/short_passage_loss.json")
    args = parser.parse_args()

    import httpx

    store = KnowledgeStore(args.db)
    rows = []
    with httpx.Client(timeout=90, headers={"User-Agent": "historybot-research/0.3"}) as client:
        for page in args.pages:
            response = client.get(
                "https://zh.wikisource.org/w/api.php",
                params={
                    "action": "query",
                    "format": "json",
                    "formatversion": "2",
                    "prop": "revisions",
                    "rvprop": "content",
                    "rvslots": "main",
                    "titles": page,
                },
            )
            response.raise_for_status()
            pages = response.json().get("query", {}).get("pages", [])
            raw = pages[0]["revisions"][0]["slots"]["main"]["content"] if pages else ""
            if not raw:
                rows.append({"page": page, "error": "page_missing"})
                continue
            cleaned = clean_wikitext(raw)
            chunks = [re.sub(r"\s+", " ", part).strip() for part in re.split(r"\n+|(?<=[。！？])", cleaned)]
            dropped = [chunk for chunk in chunks if chunk and len(chunk) < MIN_LENGTH]
            year_bearing = [chunk for chunk in dropped if YEAR_WORD_PATTERN.search(chunk) or era_absolute_anchor(chunk)]
            rows.append(
                {
                    "page": page,
                    "chunks": len([c for c in chunks if c]),
                    "dropped_chunks": len(dropped),
                    "dropped_chars": sum(len(chunk) for chunk in dropped),
                    "dropped_with_year": len(year_bearing),
                    "dropped_samples": dropped[:8],
                    "dropped_year_samples": year_bearing[:8],
                }
            )
    report = {"min_length": MIN_LENGTH, "pages": rows}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    main()
