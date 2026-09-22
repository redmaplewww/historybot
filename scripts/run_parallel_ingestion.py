from __future__ import annotations

import argparse
import asyncio
import sqlite3
import json
import os
import sys
import time
from pathlib import Path
from multiprocessing import Process, Queue

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


def run_one_volume(db_path: str, source_id: str, title: str, result_queue: Queue) -> None:
    """Run one volume in a separate process; checkpoint reuse skips done passages."""
    try:
        start = time.monotonic()
        store = KnowledgeStore(db_path)
        service = AIIngestionService(store, wiki=WikiStore(Path("data/wiki")), memories=SQLiteMemoryStore(store.db))
        result = asyncio.run(service.run(job_id=None, source_id=source_id, question="complete reading and evidence graph"))
        steps = store.get_v2_steps(result["run_id"])
        usage = usage_from_steps(steps)
        elapsed = round(time.monotonic() - start, 1)
        report = {"title": title, "source_id": source_id, "status": result["status"], "usage": usage, "elapsed_seconds": elapsed, "run_id": result["run_id"], "graph": result.get("graph")}
        result_queue.put(("complete", report))
    except Exception as error:
        result_queue.put(("failed", {"title": title, "source_id": source_id, "error": f"{type(error).__name__}: {error}"}))


def get_sanguo_volumes(db_path: str) -> list[tuple[str, str, int]]:
    connection = sqlite3.connect(db_path)
    rows = connection.execute("SELECT id, payload FROM sources").fetchall()
    connection.close()
    volumes = []
    for source_id, payload in rows:
        try:
            data = json.loads(payload)
            title = data.get("title", "")
            if title.startswith("\u4e09\u56fd\u5fd7"):
                passage_count = 0
                conn = sqlite3.connect(db_path)
                passage_count = conn.execute("SELECT COUNT(*) FROM passages WHERE source_id=?", (source_id,)).fetchone()[0]
                conn.close()
                volumes.append((source_id, title, passage_count))
        except Exception:
            pass
    volumes.sort(key=lambda x: x[2])
    return volumes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/three_kingdoms_rebuilt_v2.db")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    volumes = get_sanguo_volumes(args.db)
    print(f"Total 三国志 volumes: {len(volumes)}, workers: {args.workers}", flush=True)

    result_queue: Queue = Queue()
    active_processes: list[tuple[Process, str]] = []
    reports: list[dict] = []
    from datetime import datetime
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = Path(args.report or f"artifacts/ai_ingestion_parallel_{stamp}.json")
    report_path.parent.mkdir(exist_ok=True)

    volume_index = 0
    while volume_index < len(volumes) or active_processes:
        # Start new workers when capacity allows
        while volume_index < len(volumes) and len(active_processes) < args.workers:
            source_id, title, unit_count = volumes[volume_index]
            process = Process(target=run_one_volume, args=(args.db, source_id, title, result_queue))
            process.start()
            active_processes.append((process, title))
            print(f"START {title} ({unit_count} passages)", flush=True)
            volume_index += 1

        # Collect completed results
        if not result_queue.empty():
            kind, report = result_queue.get()
            if kind == "complete":
                print(f"DONE {report['title']} {report['status']} {report['usage']} {report['elapsed_seconds']}s", flush=True)
            else:
                print(f"FAILED {report['title']} {report.get('error', '')[:120]}", flush=True)
            reports.append(report)
            report_path.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
            active_processes = [(p, t) for p, t in active_processes if p.is_alive() or t != report["title"]]

        # Remove dead processes that did not report
        alive = []
        for proc, title in active_processes:
            if proc.is_alive():
                alive.append((proc, title))
            else:
                print(f"EXITED {title} without report (likely crashed)", flush=True)
        active_processes = alive
        time.sleep(1)

    # Drain remaining results
    while not result_queue.empty():
        kind, report = result_queue.get()
        reports.append(report)
    report_path.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"ALL DONE report={report_path}", flush=True)


if __name__ == "__main__":
    main()
