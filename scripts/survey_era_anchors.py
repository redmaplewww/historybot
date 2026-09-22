"""List the era-year anchors a Wikisource volume actually contains.

After the cleaner preserves the ``YL`` era-year template, this shows each
anchor's position relative to its passage so the context window can be judged
against real data instead of assumptions.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from historybot.ingest import clean_wikitext, split_passages


def wikitext(client: httpx.Client, page: str) -> str:
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
    return pages[0]["revisions"][0]["slots"]["main"]["content"] if pages and pages[0].get("revisions") else ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--page", default="三國志/卷01")
    parser.add_argument("--output", default="artifacts/era_anchor_survey.json")
    args = parser.parse_args()

    with httpx.Client(timeout=90, headers={"User-Agent": "historybot-research/0.3 (anchor survey)"}) as client:
        raw = wikitext(client, args.page)
    raw_anchors = [
        {"call": m.group(1)}
        for m in re.finditer(r"\{\{YL((?:\|[^|}]*)*)\}\}", raw)
    ]
    cleaned = clean_wikitext(raw)
    passages = split_passages(cleaned, source_id="src_survey")
    anchors = []
    for index, passage in enumerate(passages):
        for match in re.finditer(r"([\u4e00-\u9fff]{1,4})(元|[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]{1,3})年", passage.text):
            anchors.append({"ordinal": index, "era": match.group(1), "year": match.group(2), "text": passage.text[:60]})
    report = {
        "page": args.page,
        "raw_yl_templates": len(raw_anchors),
        "raw_yl_sample": raw_anchors[:12],
        "cleaned_passages": len(passages),
        "anchor_passages": len(anchors),
        "anchors": anchors[:60],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "anchors"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
