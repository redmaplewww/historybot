"""Dump one volume's passages in order with their detected era-year anchors.

Used to check whether an era-omitted statement (是歲) is inherited from the
right chronicle year by reading the surrounding sequence, not just the window.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from historybot.repair import ANCHOR_PATTERN, era_absolute_anchor
from historybot.store import KnowledgeStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/three_kingdoms_rebuilt_v2.db")
    parser.add_argument("--source", default="src_8d3056db6591b2ee")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int, default=500)
    parser.add_argument("--output", default="artifacts/volume_sequence.txt")
    args = parser.parse_args()

    store = KnowledgeStore(args.db)
    rows = store.db.execute(
        "SELECT ordinal, json_extract(payload,'$.text') t FROM passages WHERE source_id=? "
        "AND ordinal BETWEEN ? AND ? ORDER BY ordinal",
        (args.source, args.start, args.end),
    ).fetchall()
    lines = []
    for row in rows:
        text = row["t"] or ""
        anchor = era_absolute_anchor(text)
        has_year_word = "\u5e74" in text
        marker = ""
        if anchor:
            marker = f"  <<< ANCHOR {anchor}"
        elif has_year_word:
            marker = "  (year word, no anchor)"
        lines.append(f"ord={row['ordinal']:3d}{marker} :: {text}")
    Path(args.output).write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {len(lines)} passages to {args.output}")


if __name__ == "__main__":
    main()
