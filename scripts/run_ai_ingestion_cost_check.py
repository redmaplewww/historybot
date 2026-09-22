from __future__ import annotations

import argparse
import asyncio
import sqlite3
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from historybot.ai_ingestion import AIIngestionService
from historybot.store import KnowledgeStore
from historybot.v2_adapters import SQLiteMemoryStore, WikiStore


def usage_from_steps(rows: list[dict]) -> dict[str, int]:
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for row in rows:
        usage = row.get("output", {}).get("usage", {}) or {}
        totals["prompt_tokens"] += int(usage.get("prompt_tokens", 0))
        totals["completion_tokens"] += int(usage.get("completion_tokens", 0))
        totals["total_tokens"] += int(usage.get("total_tokens", 0))
    return totals


def run_report(db_path: str, source_id: str) -> dict:
    """Record one complete AI reading run; reuse it later for incremental runs."""
    store = KnowledgeStore(db_path)
    service = AIIngestionService(store, wiki=WikiStore(Path("data/wiki")), memories=SQLiteMemoryStore(store.db))
    result = asyncio.run(service.run(job_id=None, source_id=source_id, question="complete reading and evidence graph"))
    steps = store.get_v2_steps(result["run_id"])
    usage = usage_from_steps(steps)
    return {"run": result, "usage": usage}


def volume_status(connection: sqlite3.Connection, source_id: str) -> dict:
    runs = connection.execute(
        "SELECT id,status,payload FROM v2_reading_runs WHERE id LIKE 'read_%' ORDER BY rowid"
    ).fetchall()
    latest = None
    for run_id, status, payload in runs:
        data = json.loads(payload) if payload else {}
        if data.get("source_id") == source_id:
            latest = {"run_id": run_id, "status": status, "data": data}
    return latest or {"run_id": None, "status": "not_started", "data": {}}


def checkpoint_status(db_path: str, source_id: str) -> dict:
    """Count per-passage checkpoints to estimate how much of a volume is already done."""
    connection = sqlite3.connect(db_path)
    read_count = connection.execute(
        "SELECT COUNT(*) FROM v2_reading_steps WHERE id LIKE 'ckpt_read_%' AND input_ref IN "
        "(SELECT id FROM passages WHERE source_id=?)",
        (source_id,),
    ).fetchone()[0]
    verify_count = connection.execute(
        "SELECT COUNT(*) FROM v2_reading_steps WHERE id LIKE 'ckpt_verify_%' AND input_ref IN "
        "(SELECT id FROM passages WHERE source_id=?)",
        (source_id,),
    ).fetchone()[0]
    connection.close()
    return {"read_checkpoints": read_count, "verify_checkpoints": verify_count}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/three_kingdoms_rebuilt_v2.db")
    parser.add_argument("--source-id")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--replay-steps", action="store_true")
    args = parser.parse_args()

    if args.all:
        connection = sqlite3.connect(args.db)
        rows = connection.execute(
            "SELECT s.id, json_extract(s.payload,'$.title'), COUNT(p.id) "
            "FROM sources s JOIN passages p ON p.source_id=s.id "
            "WHERE json_extract(s.payload,'$.title') LIKE '三国志%' GROUP BY s.id ORDER BY s.id"
        ).fetchall()
        connection.close()

        from datetime import datetime

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = Path(f"artifacts/ai_ingestion_cost_check_all_{stamp}.json")
        report_path.parent.mkdir(exist_ok=True)
        reports: list[dict] = []
        for source_id, title, unit_count in rows:
            status = volume_status(sqlite3.connect(args.db), source_id)
            if status["status"] == "verified":
                steps = get_steps(args.db, status["run_id"])
                usage = usage_from_steps(steps)
                reports.append({
                    "source_id": source_id,
                    "title": title,
                    "status": "verified",
                    "usage": usage,
                    "run_id": status["run_id"],
                    "skipped": True,
                })
                print(f"SKIP {title} already verified {usage}", flush=True)
                continue
            if status["status"] == "review_required":
                checkpoints = checkpoint_status(args.db, source_id)
                # With per-passage checkpoints, re-running a review_required volume
                # only redoes failed passages instead of the whole volume.
                if checkpoints["read_checkpoints"] > 0 and checkpoints["verify_checkpoints"] > 0:
                    print(f"RESUME {title} review_required {checkpoints}", flush=True)
                else:
                    steps = get_steps(args.db, status["run_id"])
                    usage = usage_from_steps(steps)
                    reports.append({
                        "source_id": source_id,
                        "title": title,
                        "status": "review_required",
                        "usage": usage,
                        "run_id": status["run_id"],
                        "skipped": True,
                    })
                    print(f"SKIP {title} review_required {usage}", flush=True)
                    continue
            start = time.monotonic()
            try:
                report = run_report(args.db, source_id)
                report["title"] = title
                report["elapsed_seconds"] = round(time.monotonic() - start, 1)
                reports.append(report)
                print(f"COMPLETE {title} {report['run']['status']} {report['usage']}", flush=True)
            except Exception as error:
                reports.append({
                    "source_id": source_id,
                    "title": title,
                    "error": f"{type(error).__name__}: {error}",
                    "elapsed_seconds": round(time.monotonic() - start, 1),
                })
                print(f"FAILED {title} {type(error).__name__}: {error}", flush=True)
            report_path.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
        report_path.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"REPORT {report_path}", flush=True)
        return

    if args.source_id:
        report = run_report(args.db, args.source_id)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    if args.replay_steps:
        connection = sqlite3.connect(args.db)
        runs = connection.execute("SELECT id FROM v2_reading_runs WHERE id LIKE 'read_%'").fetchall()
        for (run_id,) in runs:
            steps = get_steps(args.db, run_id)
            usage = usage_from_steps(steps)
            print(f"{run_id} {usage}")
        return

    parser.error("--source-id, --all, or --replay-steps is required")


def get_steps(db_path: str, run_id: str | None) -> list[dict]:
    if not run_id:
        return []
    connection = sqlite3.connect(db_path)
    rows = connection.execute(
        "SELECT id,ordinal,step_type,input_ref,output,model,prompt_version,status "
        "FROM v2_reading_steps WHERE run_id=? ORDER BY ordinal",
        (run_id,),
    ).fetchall()
    result = []
    for row in rows:
        try:
            output = json.loads(row[4])
        except Exception:
            output = {}
        result.append({
            "id": row[0], "ordinal": row[1], "step_type": row[2], "input_ref": row[3],
            "output": output, "model": row[5], "prompt_version": row[6], "status": row[7],
        })
    return result


if __name__ == "__main__":
    main()
