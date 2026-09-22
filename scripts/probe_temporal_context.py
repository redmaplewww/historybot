"""Probe the V3 temporal context builder without calling a model.

The repair prompt depends on three context sources: the local passage window,
the nearest prior era-year anchor, and prior AI reading summaries. This script
prints what each target actually receives so an empty context source is a
visible defect rather than a silent quality loss.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from historybot.repair import RepairService
from historybot.repair_text import ERA_STARTS


def build_tasks(service: RepairService, source_id: str) -> tuple[list[dict], dict[str, int]]:
    passages, entities, claims, events = service.load_v2()
    # Load the reading ledger once: it is ~30k rows of JSON and reloading it per
    # prior passage turns this probe quadratic.
    reading_steps = service._original_reading_steps()
    own_passages = [p for p in passages.values() if p["source_id"] == source_id]
    if not own_passages:
        raise SystemExit(f"no passages for source {source_id}")
    ordered = sorted(own_passages, key=lambda p: p["ordinal"])
    tasks = service._build_window_targets(ordered, claims, events, reading_steps)
    index = {p["id"]: position for position, p in enumerate(ordered)}
    for task in tasks:
        target_index = index[task["passages"][-1]["id"]]
        task["target_index"] = target_index
        task["anchor_context"] = service._forward_anchor_context(ordered, target_index)
        summary_parts = []
        for prior in range(max(0, target_index - 20), target_index):
            units = reading_steps.get(ordered[prior]["id"], {}).get("output", {}).get("units", [])
            if units and units[0].get("reading_summary"):
                summary_parts.append(f"[{ordered[prior]['id']}] {units[0]['reading_summary']}")
        task["reading_summaries"] = "\n".join(summary_parts[-20:])
        task["era_table"] = "\n".join(
            f"{era}元年={start}" for era, start in sorted(ERA_STARTS.items())
        )
    rows = {task["passages"][-1]["id"]: task["passages"][-1]["ordinal"] for task in tasks}
    return tasks, rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/three_kingdoms_rebuilt_v2.db")
    parser.add_argument("--source", default="src_8d3056db6591b2ee", help="source_id to inspect")
    parser.add_argument("--context-size", type=int, default=4)
    parser.add_argument("--only-ordinal", type=int, default=None)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    service = RepairService(db_path=args.db, concurrency=1, context_size=args.context_size, run_id="probe")
    tasks, ordinal_by_passage = build_tasks(service, args.source)
    print(f"windows={len(tasks)} context_size={args.context_size} source={args.source}")

    selected = tasks
    if args.only_ordinal is not None:
        selected = [t for t in tasks if ordinal_by_passage.get(t["passages"][-1]["id"]) == args.only_ordinal]
    else:
        selected = tasks[: args.limit]

    for task in selected:
        passage_id = task["passages"][-1]["id"]
        system, user = service._temporal_prompt(
            task["passages"],
            task["targets"],
            anchor_context=task["anchor_context"],
            reading_summaries=task["reading_summaries"],
            era_table=task["era_table"],
        )
        anchor = task["anchor_context"].splitlines()[0] if task["anchor_context"] else "(none)"
        print("-" * 90)
        print(f"target passage_id={passage_id} ordinal={ordinal_by_passage.get(passage_id)} targets={len(task['targets'])}")
        print(f"  window_passages={len(task['passages'])} anchor={'yes' if task['anchor_context'] else 'NO'}")
        print(f"  anchor_label={anchor}")
        print(f"  summary_lines={len(task['reading_summaries'].splitlines())} era_table_lines={len(task['era_table'].splitlines())}")
        print(f"  prompt_chars={len(user)} system_chars={len(system)}")
        print(f"  uses_anchor_template={'YES' if '前文锚点' in user else 'NO'}")
        print(f"  era_table_has_placeholder={'YES' if '??' in user else 'no'}")


if __name__ == "__main__":
    main()
