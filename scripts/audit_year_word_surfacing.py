"""Check that era-omitted year increments are surfaced to the temporal model.

The chronicles write 二年春 / 三年春 / 四年春 without repeating the era name.
Every one of those passages is a step the model must count through, so a
missing step produces a wrong year. This reports gaps in the surfaced chain.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from historybot.repair import YEAR_WORD_PATTERN, era_absolute_anchor
from historybot.store import KnowledgeStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/three_kingdoms_rebuilt_v2.db")
    parser.add_argument("--source", default="src_8d3056db6591b2ee")
    parser.add_argument("--output", default="artifacts/year_word_surfacing.json")
    args = parser.parse_args()

    store = KnowledgeStore(args.db)
    rows = store.db.execute(
        "SELECT ordinal, json_extract(payload,'$.text') t FROM passages "
        "WHERE source_id=? ORDER BY ordinal",
        (args.source,),
    ).fetchall()

    chain = []
    for row in rows:
        text = row["t"] or ""
        anchor = era_absolute_anchor(text)
        matches = [m.group(0) for m in YEAR_WORD_PATTERN.finditer(text)]
        if anchor:
            chain.append(
                {
                    "ordinal": row["ordinal"],
                    "kind": "anchor",
                    "anchor": anchor,
                    "text": text[:60],
                }
            )
        elif matches:
            chain.append(
                {
                    "ordinal": row["ordinal"],
                    "kind": "year_word",
                    "matches": matches,
                    "text": text[:60],
                }
            )
        elif "\u5e74" in text:
            chain.append(
                {"ordinal": row["ordinal"], "kind": "year_char_unmatched", "text": text[:60]}
            )

    report = {"source": args.source, "passages": len(rows), "chain": chain}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for item in chain:
        print(f"{item['kind']:20s} ord={item['ordinal']:4d} {item.get('matches', item.get('anchor', ''))} :: {item['text']}")


if __name__ == "__main__":
    main()
