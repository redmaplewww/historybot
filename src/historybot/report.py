from __future__ import annotations

import argparse
import json
from pathlib import Path

from .store import KnowledgeStore


def write_report(db: str, output: str | None = None) -> dict:
    store = KnowledgeStore(db)
    counts = store.counts()
    last = store.db.execute("SELECT action,payload FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    status = "complete"
    error = None
    if last and last[0] in {"pipeline_failed", "pipeline_pending"}:
        payload = json.loads(last[1])
        error = payload
        status = "pending_contradictions" if counts["claims"] > 0 and payload.get("error_type") in {"ReadTimeout", "ConnectTimeout", "PoolTimeout"} else "failed"
    report = {"status": status, "error": error, "counts": counts, "sources": [source.model_dump(mode="json") for source in store.get_sources()]}
    target = output or str(Path(db).with_suffix(".report.json"))
    Path(target).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    print(write_report(args.db, args.output))

