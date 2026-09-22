"""Compare ingested passage text against the rendered Wikisource page.

The ingest cleaner keeps only the ``ProperNoun`` template. Any other template
is deleted, so content carried by templates silently disappears from the
corpus. This script measures that loss so it is a reported number rather than
an invisible quality regression.
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


def rendered_plain_text(client: httpx.Client, page: str) -> str:
    response = client.get(
        "https://zh.wikisource.org/w/api.php",
        params={"action": "parse", "page": page, "prop": "text", "format": "json"},
    )
    response.raise_for_status()
    html = response.json().get("parse", {}).get("text", {}).get("*", "")
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)
    return re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", html))


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
    if not pages or not pages[0].get("revisions"):
        return ""
    return pages[0]["revisions"][0]["slots"]["main"]["content"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", nargs="*", default=["三國志/卷01", "三國志/卷32"])
    parser.add_argument("--output", default="artifacts/ingest_text_loss.json")
    args = parser.parse_args()

    rows = []
    with httpx.Client(timeout=90, headers={"User-Agent": "historybot-research/0.3 (text loss audit)"}) as client:
        for page in args.pages:
            raw = wikitext(client, page)
            if not raw:
                rows.append({"page": page, "error": "page_missing"})
                continue
            cleaned = clean_wikitext(raw)
            rendered = rendered_plain_text(client, page)
            passages = split_passages(cleaned, source_id="src_audit")
            rows.append(
                {
                    "page": page,
                    "wikitext_chars": len(raw),
                    "cleaned_chars": len(re.sub(r"\s+", "", cleaned)),
                    "rendered_chars": len(rendered),
                    "cleaned_passages": len(passages),
                    "missing_vs_rendered_chars": len(rendered) - len(re.sub(r"\s+", "", cleaned)),
                }
            )
    report = {"pages": rows}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
