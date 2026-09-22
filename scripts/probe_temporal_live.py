"""Run the temporal prompt against a live gateway for specific passages.

After the era-year anchors were restored, this checks whether the model can
actually resolve an era-omitted statement (是歲 / 二年春) by using the anchor
and reading-summary context, which is what the earlier prompt could not do.
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


def build_tasks(service: RepairService, source_id: str) -> list[dict]:
    passages, _, claims, events = service.load_v2()
    reading_steps = service._original_reading_steps()
    ordered = sorted(
        (p for p in passages.values() if p["source_id"] == source_id),
        key=lambda p: p["ordinal"],
    )
    tasks = service._build_window_targets(ordered, claims, events, reading_steps)
    index = {p["id"]: position for position, p in enumerate(ordered)}
    for task in tasks:
        target_index = index[task["passages"][-1]["id"]]
        task["target_index"] = target_index
        task["anchor_context"] = service._forward_anchor_context(ordered, target_index)
        summaries = []
        for prior in range(max(0, target_index - 20), target_index):
            units = reading_steps.get(ordered[prior]["id"], {}).get("output", {}).get("units", [])
            if units and units[0].get("reading_summary"):
                summaries.append(f"[{ordered[prior]['id']}] {units[0]['reading_summary']}")
        task["reading_summaries"] = "\n".join(summaries[-20:])
        task["era_table"] = "\n".join(
            f"{era}元年={start}" for era, start in sorted(ERA_STARTS.items())
        )
        task["ordinal"] = ordered[target_index]["ordinal"]
        task["text"] = ordered[target_index]["text"]
    return tasks


async def run(args) -> dict:
    os.environ.setdefault("HISTORYBOT_LLM_PROVIDER", "openai_compatible")
    os.environ.setdefault("HISTORYBOT_BASE_URL", "https://workbuddy2api.henryai.top")
    os.environ.setdefault("HISTORYBOT_MODEL", "deepseek-v4.1-flash")
    service = RepairService(
        db_path=args.db,
        concurrency=1,
        context_size=args.context_size,
        run_id="live_probe",
    )
    tasks = build_tasks(service, args.source)
    wanted = set(args.ordinal or [])
    selected = [t for t in tasks if not wanted or t["ordinal"] in wanted]
    results = []
    for task in selected[: args.limit]:
        system, user = service._temporal_prompt(
            task["passages"],
            task["targets"],
            anchor_context=task["anchor_context"],
            reading_summaries=task["reading_summaries"],
            era_table=task["era_table"],
        )
        entry = {
            "ordinal": task["ordinal"],
            "text": task["text"],
            "has_anchor": bool(task["anchor_context"]),
            "anchor_first_line": task["anchor_context"].splitlines()[0] if task["anchor_context"] else "",
            "target_count": len(task["targets"]),
        }
        try:
            result = await service.gateway.structured(
                task="historical_temporal_resolution",
                system=system,
                user=user,
                schema=TemporalResponse,
            )
            entry["context_summary"] = result.value.context_summary
            entry["references"] = [
                {
                    "id": item.claim_id or item.event_id,
                    "statement": next(
                        (
                            t.get("subject", "") + " " + t.get("predicate", "") + " " + t.get("object", "")
                            for t in task["targets"]
                            if t["id"] == (item.claim_id or item.event_id) and t["kind"] == "claim"
                        ),
                        "",
                    ).strip(),
                    "time_expression": item.time_expression,
                    "year": item.year,
                    "era_name": item.era_name,
                    "era_year": item.era_year,
                    "time_from": item.time_from,
                    "time_precision": item.time_precision,
                    "context_basis": item.context_basis,
                }
                for item in result.value.references
            ]
        except Exception as error:  # noqa: BLE001 - probe must report any failure
            entry["error"] = f"{type(error).__name__}: {error}"
        results.append(entry)
        print(json.dumps(entry, ensure_ascii=False, indent=2), flush=True)
    return {"results": results}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/three_kingdoms_rebuilt_v2.db")
    parser.add_argument("--source", default="src_8d3056db6591b2ee")
    parser.add_argument("--ordinal", type=int, nargs="*", default=None)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--context-size", type=int, default=4)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    report = asyncio.run(run(args))
    if args.output:
        Path(args.output).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
