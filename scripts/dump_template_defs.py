"""Dump the Wikisource template definitions that the ingest cleaner touches.

The cleaner must keep every template that renders visible historical content
and drop only presentational wrappers. This prints the raw template bodies so
the keep/drop decision is grounded in the actual definition.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from historybot.ingest import WIKISOURCE_TEMPLATE_NAMES


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="artifacts/wikisource_template_defs.json")
    args = parser.parse_args()

    definitions: dict[str, dict] = {}
    with httpx.Client(timeout=60, headers={"User-Agent": "historybot-research/0.3"}) as client:
        for name in WIKISOURCE_TEMPLATE_NAMES:
            response = client.get(
                "https://zh.wikisource.org/w/api.php",
                params={
                    "action": "query",
                    "format": "json",
                    "formatversion": "2",
                    "prop": "revisions",
                    "rvprop": "content",
                    "rvslots": "main",
                    "titles": f"Template:{name}",
                },
            )
            for attempt in range(4):
                if response.status_code != 429:
                    break
                time.sleep(2**attempt)
                response = client.get(
                    "https://zh.wikisource.org/w/api.php",
                    params={
                        "action": "query",
                        "format": "json",
                        "formatversion": "2",
                        "prop": "revisions",
                        "rvprop": "content",
                        "rvslots": "main",
                        "titles": f"Template:{name}",
                    },
                )
            response.raise_for_status()
            pages = response.json().get("query", {}).get("pages", [])
            page = pages[0] if pages else {}
            body = page.get("revisions", [{}])[0].get("slots", {}).get("main", {}).get("content")
            definitions[name] = {"exists": body is not None, "body": body}
            time.sleep(0.6)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(definitions, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, item in definitions.items():
        status = "FOUND" if item["exists"] else "missing"
        print(f"{status:8s} Template:{name} len={len(item['body'] or '')}")


if __name__ == "__main__":
    main()
