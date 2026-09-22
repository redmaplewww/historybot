"""Compare the temporal anchor detector against the anchors really present.

The detector drives which prior era-year a passage inherits. A detector that
misses anchors silently degrades every downstream date, so this reports how
many anchors exist in the corpus and how many the detector currently finds.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from historybot.repair import era_absolute_anchor
from historybot.repair_text import ERA_STARTS
from historybot.store import KnowledgeStore


def ground_truth_anchor(text: str) -> tuple[str, int, int] | None:
    """Find the last known era name directly followed by a year in ``text``.

    This is written independently of the production regex on purpose: it
    matches the era vocabulary explicitly, so a detector that greps arbitrary
    characters cannot agree with it by accident.
    """
    best = None
    for match in re.finditer(r"(元|[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]{1,3})\u5e74", text):
        for era in sorted(ERA_STARTS, key=len, reverse=True):
            start = match.start() - len(era)
            if start >= 0 and text[start : match.start()] == era:
                era_year = 1 if match.group(1) == "\u5143" else None
                if era_year is None:
                    era_year = convert_year(match.group(1))
                if era_year is not None:
                    best = (era, era_year, ERA_STARTS[era] + era_year - 1)
                break
    return best


def convert_year(expression: str) -> int | None:
    """Parse a 1-3 character Chinese numeral in the 年号 year range (1-40)."""
    digits = {"\u4e00": 1, "\u4e8c": 2, "\u4e09": 3, "\u56db": 4, "\u4e94": 5,
              "\u516d": 6, "\u4e03": 7, "\u516b": 8, "\u4e5d": 9}
    ten = "\u5341"
    if not expression or any(char not in digits and char != ten for char in expression):
        return None
    if ten not in expression:
        total = 0
        for char in expression:
            total = total * 10 + digits[char]
        return total
    if expression.count(ten) != 1:
        return None
    head, tail = expression.split(ten)
    tens = digits[head] if head else 1
    ones = digits[tail] if tail else 0
    if head and len(head) != 1:
        return None
    if tail and len(tail) != 1:
        return None
    return tens * 10 + ones


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/three_kingdoms_rebuilt_v2.db")
    parser.add_argument("--output", default="artifacts/era_anchor_detection.json")
    parser.add_argument("--show", type=int, default=12)
    args = parser.parse_args()

    store = KnowledgeStore(args.db)
    rows = store.db.execute(
        """
        SELECT p.ordinal, json_extract(p.payload, '$.text') AS text, s.payload AS source_payload
        FROM passages p JOIN sources s ON s.id = p.source_id
        WHERE json_extract(s.payload, '$.title') LIKE '三国志·卷%'
        ORDER BY p.source_id, p.ordinal
        """
    ).fetchall()

    detected = missed = 0
    found, missed_samples = [], []
    for row in rows:
        text = row["text"] or ""
        truth = ground_truth_anchor(text)
        if not truth:
            continue
        current = era_absolute_anchor(text)
        if current and current[2] == truth[2]:
            detected += 1
            if len(found) < args.show:
                found.append({"text": text[:70], "era": truth[0], "year": truth[2]})
        else:
            missed += 1
            if len(missed_samples) < args.show:
                missed_samples.append(
                    {
                        "ordinal": row["ordinal"],
                        "text": text[:70],
                        "expected": truth,
                        "detected": current,
                    }
                )

    report = {
        "passages_scanned": len(rows),
        "anchors_present": detected + missed,
        "detected": detected,
        "missed": missed,
        "detection_rate": round(detected / (detected + missed), 4) if detected + missed else None,
        "missed_samples": missed_samples,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "missed_samples"}, ensure_ascii=False, indent=2))
    for item in missed_samples:
        print(f"  ord={item['ordinal']:4d} expected={item['expected']} detected={item['detected']} :: {item['text']}")


if __name__ == "__main__":
    main()
