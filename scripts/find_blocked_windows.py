"""Find which temporal windows a gateway's content policy rejects.

An OpenAI-compatible gateway can refuse a request with
``upstream_content_policy_blocked``. Which passage triggers it matters: real
source text is not something the pipeline may quietly skip, so the blocked
windows are listed explicitly for review.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from historybot.model_gateway import ModelGateway
from historybot.repair import RepairService
from historybot.repair_text import ERA_STARTS
from historybot.v2_schemas import TemporalResponse


async def probe(service: RepairService, tasks: list[dict], concurrency: int) -> list[dict]:
    semaphore = asyncio.Semaphore(concurrency)
    results: list[dict] = []

    async def one(task: dict) -> None:
        async with semaphore:
            passage_id = task["passages"][-1]["id"]
            system, user = service._temporal_prompt(
                task["passages"],
                task["targets"],
                anchor_context=task["anchor_context"],
                reading_summaries=task["reading_summaries"],
                era_table=task["era_table"],
            )
            entry = {"passage_id": passage_id, "ordinal": task.get("ordinal")}
            try:
                await service.gateway.structured(
                    task="historical_temporal_resolution",
                    system=system,
                    user=user,
                    schema=TemporalResponse,
                )
                entry["status"] = "ok"
            except Exception as error:  # noqa: BLE001 - probing every failure mode
                entry["status"] = "failed"
                entry["error"] = f"{type(error).__name__}: {str(error)[:240]}"
                entry["window_text"] = " | ".join(
                    passage.get("text", "")[:40] for passage in task["passages"]
                )
            results.append(entry)

    await asyncio.gather(*(one(task) for task in tasks))
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/three_kingdoms_rebuilt_v2.db")
    parser.add_argument("--source", default=None)
    parser.add_argument("--limit-windows", type=int, default=0)
    parser.add_argument("--limit-sources", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--context-size", type=int, default=4)
    parser.add_argument(
        "--era-table",
        choices=("anchor", "full", "off"),
        default="anchor",
        help="era-year reference table variant to send",
    )
    parser.add_argument("--output", default="artifacts/blocked_windows.json")
    args = parser.parse_args()

    service = RepairService(
        db_path=args.db,
        concurrency=args.concurrency,
        context_size=args.context_size,
        run_id="blocked_probe",
        era_table_mode=args.era_table,
    )
    passages, _, claims, events = service.load_v2()
    reading_steps = service._original_reading_steps()
    by_source: dict[str, list[dict]] = {}
    for passage in passages.values():
        by_source.setdefault(passage["source_id"], []).append(passage)
    source_ids = [args.source] if args.source else sorted(by_source)[: args.limit_sources]

    tasks: list[dict] = []
    for source_id in source_ids:
        ordered = sorted(by_source[source_id], key=lambda p: p["ordinal"])
        source_tasks = service._build_window_targets(ordered, claims, events, reading_steps)
        index = {p["id"]: position for position, p in enumerate(ordered)}
        for task in source_tasks:
            target_index = index[task["passages"][-1]["id"]]
            task["ordinal"] = ordered[target_index]["ordinal"]
            task["anchor_context"] = service._forward_anchor_context(ordered, target_index)
            summaries = []
            for prior in range(max(0, target_index - 20), target_index):
                units = reading_steps.get(ordered[prior]["id"], {}).get("output", {}).get("units", [])
                if units and units[0].get("reading_summary"):
                    summaries.append(f"[{ordered[prior]['id']}] {units[0]['reading_summary']}")
            task["reading_summaries"] = "\n".join(summaries[-20:])
            task["era_table"] = service._era_table()
        tasks.extend(source_tasks)
    if args.limit_windows:
        tasks = tasks[: args.limit_windows]

    print(f"probing {len(tasks)} windows across {len(source_ids)} sources", flush=True)
    results = asyncio.run(probe(service, tasks, args.concurrency))
    failed = [item for item in results if item["status"] == "failed"]
    report = {
        "windows_probed": len(results),
        "failed": len(failed),
        "failures": failed,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"windows={len(results)} failed={len(failed)}")
    for item in failed[:20]:
        print(f"  ord={item.get('ordinal')} {item['passage_id']}: {item['error'][:120]}")


if __name__ == "__main__":
    main()
