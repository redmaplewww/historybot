from __future__ import annotations

import argparse
import json
from pathlib import Path

from .store import KnowledgeStore


def export_snapshot(db: str, output: str) -> dict:
    snapshot = KnowledgeStore(db).graph_snapshot()
    Path(output).write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return snapshot


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = export_snapshot(args.db, args.output)
    print({key: len(value) for key, value in result.items()})
