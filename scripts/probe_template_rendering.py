"""Render representative Wikisource templates to learn what they display.

The ingest cleaner must reproduce the rendered text, not the raw wikitext.
This asks the MediaWiki parser to render sample template usages so the
keep/replace decision comes from observed output rather than a guess.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import httpx

SAMPLES = {
    "YL": "{{YL|\u8208\u5e73\u5143\u5e74|194\u5e74}}\u6625",
    "star": "{{*|\u300a\u82f1\u96c4\u8a18\u300b\u66f0\uff1a\u99a5\u5b57\u6587\u7bc0\u3002}}",
    "ProperNoun": "{{ProperNoun|\u66f9\u64cd}}",
    "quote": "{{quote|\u5b5f\u5b50\u66f0}}",
    "gap": "\u524d{{gap}}\u5f8c",
    "\u53e6": "{{另|\u4e00\u4f5c\u67d0}}",
    "langconv_simple": "\u738b\u4f50-{\u624d}-\u4e5f\u3002",
    "langconv_variant": "-{zh-hans:\u540e;zh-hant:\u5f8c}-\u4e3b",
    "langconv_pipe": "-{\u7532|\u4e59}-\u4e19",
    "forcetoc": "__FORCETOC__\u592a\u7956\u6b66\u7687\u5e1d",
}


def render(client: httpx.Client, wikitext: str) -> str:
    response = client.post(
        "https://zh.wikisource.org/w/api.php",
        data={"action": "parse", "format": "json", "contentmodel": "wikitext", "text": wikitext},
    )
    response.raise_for_status()
    html = response.json().get("parse", {}).get("text", {}).get("*", "")
    return re.sub(r"<[^>]+>", "", html).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="artifacts/template_rendering.json")
    args = parser.parse_args()

    rows = {}
    with httpx.Client(timeout=60, headers={"User-Agent": "historybot-research/0.3 (rendering probe)"}) as client:
        for name, sample in SAMPLES.items():
            try:
                rows[name] = {"sample": sample, "rendered": render(client, sample)}
            except Exception as error:  # noqa: BLE001 - probe must report any failure
                rows[name] = {"sample": sample, "error": f"{type(error).__name__}: {error}"}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, row in rows.items():
        print(f"{name:12s} -> {row.get('rendered', row.get('error'))!r}")


if __name__ == "__main__":
    main()
