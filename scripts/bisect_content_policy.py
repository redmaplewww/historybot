"""Bisect which part of a temporal prompt a gateway's safety filter rejects.

The gateway answers ``upstream_content_policy_blocked`` for reasons it does not
explain. Since the prompt is assembled from named parts (era table, anchor
context, reading summaries, source window, targets), asking for them one at a
time shows which part carries the trigger instead of leaving a blind retry.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from historybot.model_gateway import ModelGateway
from historybot.repair import RepairService
from historybot.repair_text import ERA_STARTS
from historybot.v2_schemas import TemporalResponse


async def ask(gateway: ModelGateway, label: str, user: str) -> dict:
    try:
        await gateway.structured(
            task="historical_temporal_resolution",
            system="\u4f60\u662f\u53f2\u6599\u65f6\u95f4\u8003\u636e\u5458\u3002",
            user=user,
            schema=TemporalResponse,
        )
        return {"label": label, "status": "ok"}
    except Exception as error:  # noqa: BLE001 - reporting every failure mode
        text = str(error)
        kind = "policy" if "upstream_content_policy_blocked" in text else type(error).__name__
        return {"label": label, "status": kind, "detail": text[:200]}


async def run(args) -> list[dict]:
    service = RepairService(
        db_path=args.db, concurrency=1, context_size=args.context_size, run_id="bisect"
    )
    passages, _, claims, events = service.load_v2()
    reading_steps = service._original_reading_steps()
    ordered = sorted(
        (p for p in passages.values() if p["source_id"] == args.source),
        key=lambda p: p["ordinal"],
    )
    tasks = service._build_window_targets(ordered, claims, events, reading_steps)
    index = {p["id"]: position for position, p in enumerate(ordered)}
    task = None
    for candidate in tasks:
        target_index = index[candidate["passages"][-1]["id"]]
        if ordered[target_index]["ordinal"] == args.ordinal:
            task = candidate
            task["ordinal"] = args.ordinal
            task["anchor_context"] = service._forward_anchor_context(ordered, target_index)
            summaries = []
            for prior in range(max(0, target_index - 20), target_index):
                units = reading_steps.get(ordered[prior]["id"], {}).get("output", {}).get("units", [])
                if units and units[0].get("reading_summary"):
                    summaries.append(f"[{ordered[prior]['id']}] {units[0]['reading_summary']}")
            task["reading_summaries"] = "\n".join(summaries[-20:])
            task["era_table"] = "\n".join(f"{e}\u5143\u5e74={s}" for e, s in sorted(ERA_STARTS.items()))
            break
    if task is None:
        raise SystemExit(f"no window for ordinal {args.ordinal}")

    _, full_user = service._temporal_prompt(
        task["passages"],
        task["targets"],
        anchor_context=task["anchor_context"],
        reading_summaries=task["reading_summaries"],
        era_table=task["era_table"],
    )
    source_context = "\n".join(
        f"[{p['id']}] {p.get('text','').strip()}" for p in task["passages"]
    )
    targets = "\n".join(
        json.dumps(
            {"passage_id": t["passage_id"], "items": [{"id": t["id"]}]}, ensure_ascii=False
        )
        for t in task["targets"][:1]
    )
    gateway = ModelGateway()
    probes = [
        ("A full prompt", full_user),
        ("B source window only", source_context),
        ("C anchor context", task["anchor_context"] or "(empty)"),
        ("D reading summaries", task["reading_summaries"] or "(empty)"),
        ("E era table", task["era_table"]),
        ("F one target", targets),
        ("G minimal probe", "\u8bf7\u8fd4\u56de references \u7a7a\u6570\u7ec4\u3002"),
    ]
    results = []
    for label, payload in probes:
        results.append(await ask(gateway, label, payload))
        await asyncio.sleep(0.4)
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/three_kingdoms_rebuilt_v2.db")
    parser.add_argument("--source", required=True)
    parser.add_argument("--ordinal", type=int, required=True)
    parser.add_argument("--context-size", type=int, default=4)
    parser.add_argument("--output", default="artifacts/content_policy_bisect.json")
    args = parser.parse_args()
    results = asyncio.run(run(args))
    Path(args.output).write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for item in results:
        print(f"{item['label']:26s} -> {item['status']}")


if __name__ == "__main__":
    main()
