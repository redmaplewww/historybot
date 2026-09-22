"""Survey which MediaWiki templates the ingest cleaner currently discards.

``clean_wikitext`` keeps only ``ProperNoun`` and deletes every other template.
This script counts template usage in the public Wikisource wikitext so we can
see whether any deleted template carried historical content, such as the
era-year annotations used by the 三国志 chronicles.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from historybot.corpus import complete_corpus


def fetch_pages(sources, *, user_agent: str, batch_size: int = 10) -> dict[str, str]:
    pages: dict[str, str] = {}
    headers = {"User-Agent": user_agent}
    with httpx.Client(timeout=90, headers=headers) as client:
        for start in range(0, len(sources), batch_size):
            batch = sources[start : start + batch_size]
            params = {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "prop": "revisions",
                "rvprop": "content",
                "rvslots": "main",
                "titles": "|".join(source.page for source in batch),
            }
            for attempt in range(4):
                response = client.get("https://zh.wikisource.org/w/api.php", params=params)
                if response.status_code != 429 or attempt == 3:
                    response.raise_for_status()
                    break
                time.sleep(2**attempt)
            for page in response.json().get("query", {}).get("pages", []):
                if page.get("revisions"):
                    pages[page["title"]] = page["revisions"][0]["slots"]["main"]["content"]
            time.sleep(0.4)
    return pages


def template_names(wikitext: str) -> collections.Counter:
    return collections.Counter(
        match.group(1).strip() for match in re.finditer(r"\{\{([^|}\n]{1,40})", wikitext)
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=65)
    parser.add_argument("--output", default="artifacts/wikisource_template_survey.json")
    args = parser.parse_args()

    sources = complete_corpus()[: args.limit]
    pages = fetch_pages(sources, user_agent="historybot-research/0.3 (template survey)")
    totals: collections.Counter = collections.Counter()
    rows = []
    for source in sources:
        raw = pages.get(source.page, "")
        names = template_names(raw)
        totals.update(names)
        rows.append(
            {
                "page": source.page,
                "title": source.title,
                "chars": len(raw),
                "templates": dict(names.most_common()),
            }
        )
    report = {
        "volumes": len(rows),
        "template_totals": dict(totals.most_common(40)),
        "kept_by_cleaner": ["ProperNoun"],
        "volumes_without_yl": [row["title"] for row in rows if "YL" not in row["templates"]],
        "details": rows,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"volumes={report['volumes']} wrote {output}")
    print("top templates:", json.dumps(report["template_totals"], ensure_ascii=False))
    print("volumes missing YL:", len(report["volumes_without_yl"]))


if __name__ == "__main__":
    main()
