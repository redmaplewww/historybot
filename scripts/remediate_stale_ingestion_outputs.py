"""Invalidate AI outputs whose quotes no longer match current passage text.

No historical row is deleted. The script creates a consistent SQLite backup,
then marks stale checkpoints for re-reading, demotes stale verified anchors,
and changes dead run states from ``running``/``reading`` to ``interrupted``.
Run without ``--apply`` to print the proposed changes only.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.audit_ingestion_quality import (
    contains_markup,
    parse_json,
    quotes_from_payload,
    reading_step_quote_mismatches,
)


def add_audit(payload: dict[str, Any], *, reason: str, checked_at: str) -> dict[str, Any]:
    updated = dict(payload)
    updated["quality_audit"] = {
        "status": "stale",
        "reason": reason,
        "checked_at": checked_at,
    }
    return updated


def parse_timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def find_changes(conn: sqlite3.Connection) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    passages = {
        row["id"]: parse_json(row["payload"], {})
        for row in conn.execute("SELECT id,payload FROM passages")
    }
    stale_evidence: list[tuple[str, str]] = []
    for row in conn.execute("SELECT id,claim_id,passage_id,payload FROM evidence"):
        payload = parse_json(row["payload"], {})
        quote = str(payload.get("quote") or "")
        text = str(passages.get(row["passage_id"], {}).get("text") or "")
        if quote and quote not in text:
            stale_evidence.append((row["id"], row["claim_id"]))

    stale_anchors = []
    for row in conn.execute("SELECT id,passage_id,quote FROM v2_evidence_anchors"):
        text = str(passages.get(row["passage_id"], {}).get("text") or "")
        if not row["quote"] or row["quote"] not in text:
            stale_anchors.append(row["id"])

    stale_steps: list[tuple[str, str]] = []
    stale_steps_by_run: Counter[str] = Counter()
    for row in conn.execute("SELECT id,run_id,input_ref,output FROM v2_reading_steps WHERE input_ref IS NOT NULL"):
        passage = passages.get(row["input_ref"])
        if not passage:
            continue
        output = parse_json(row["output"], {})
        if reading_step_quote_mismatches(output, passages, row["input_ref"]) or contains_markup(output):
            stale_steps.append((row["id"], row["run_id"]))
            stale_steps_by_run[row["run_id"]] += 1

    stale_drafts = []
    stale_drafts_by_run: Counter[str] = Counter()
    for row in conn.execute("SELECT id,run_id,payload FROM v2_knowledge_drafts"):
        payload = parse_json(row["payload"], {})
        passage_id = str(payload.get("passage_id") or "")
        text = str(passages.get(passage_id, {}).get("text") or "")
        bad_quotes = [quote for quote in quotes_from_payload(payload) if quote not in text]
        if bad_quotes or contains_markup(payload):
            stale_drafts.append((row["id"], row["run_id"]))
            stale_drafts_by_run[row["run_id"]] += 1

    cutoff = datetime.now(UTC) - timedelta(hours=6)
    stale_v2_runs = []
    for row in conn.execute("SELECT id,status,updated_at,payload FROM v2_reading_runs WHERE status IN ('reading','review_required')"):
        payload = parse_json(row["payload"], {})
        interruption = payload.get("interruption", {}) if isinstance(payload.get("interruption"), dict) else {}
        updated = parse_timestamp(row["updated_at"])
        if interruption.get("reason") == "stale_running_state" or (row["status"] == "reading" and updated < cutoff):
            stale_v2_runs.append(row["id"])
    stale_v3_runs = []
    for row in conn.execute("SELECT run_id,status,updated_at FROM v3_repair_runs WHERE status='running'"):
        updated = parse_timestamp(row["updated_at"])
        if updated < cutoff:
            stale_v3_runs.append(row["run_id"])
    invalid_v3_runs = []
    for row in conn.execute("SELECT run_id,payload FROM v3_repair_runs WHERE status='complete'"):
        payload = parse_json(row["payload"], {})
        expected = payload.get("windows")
        ledger_total = conn.execute(
            "SELECT COUNT(*) FROM v3_repair_steps WHERE run_id=? AND step_type='temporal_resolution'",
            (row["run_id"],),
        ).fetchone()[0]
        if isinstance(expected, int) and ledger_total != expected:
            invalid_v3_runs.append(row["run_id"])

    return {
        "stale_evidence": stale_evidence,
        "stale_claims": sorted({claim_id for _, claim_id in stale_evidence}),
        "stale_anchors": stale_anchors,
        "stale_steps": stale_steps,
        "stale_steps_by_run": dict(stale_steps_by_run),
        "stale_drafts": stale_drafts,
        "stale_drafts_by_run": dict(stale_drafts_by_run),
        "stale_v2_runs": stale_v2_runs,
        "stale_v3_runs": stale_v3_runs,
        "invalid_v3_runs": invalid_v3_runs,
    }


def backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(source)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(destination_conn)
    finally:
        destination_conn.close()
        source_conn.close()


def apply_changes(conn: sqlite3.Connection, changes: dict[str, Any], checked_at: str) -> None:
    conn.row_factory = sqlite3.Row
    with conn:
        for evidence_id, _ in changes["stale_evidence"]:
            row = conn.execute("SELECT payload FROM evidence WHERE id=?", (evidence_id,)).fetchone()
            payload = add_audit(parse_json(row["payload"], {}), reason="quote_not_in_current_passage", checked_at=checked_at)
            conn.execute("UPDATE evidence SET payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), evidence_id))
        for claim_id in changes["stale_claims"]:
            row = conn.execute("SELECT payload FROM claims WHERE id=?", (claim_id,)).fetchone()
            if not row:
                continue
            payload = parse_json(row["payload"], {})
            payload["status"] = "disputed"
            metadata = payload.get("metadata", {}) if isinstance(payload.get("metadata"), dict) else {}
            metadata["review_state"] = "needs_reread"
            payload["metadata"] = metadata
            payload = add_audit(payload, reason="supporting_quote_not_in_current_passage", checked_at=checked_at)
            conn.execute("UPDATE claims SET status='disputed',payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), claim_id))
        for anchor_id in changes["stale_anchors"]:
            row = conn.execute("SELECT payload FROM v2_evidence_anchors WHERE id=?", (anchor_id,)).fetchone()
            payload = add_audit(parse_json(row["payload"], {}), reason="quote_not_in_current_passage", checked_at=checked_at)
            conn.execute("UPDATE v2_evidence_anchors SET verified=0,payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), anchor_id))
        conn.executemany("UPDATE v2_reading_steps SET status='stale' WHERE id=?", [(step_id,) for step_id, _ in changes["stale_steps"]])
        for draft_id, _ in changes["stale_drafts"]:
            row = conn.execute("SELECT payload FROM v2_knowledge_drafts WHERE id=?", (draft_id,)).fetchone()
            payload = add_audit(parse_json(row["payload"], {}), reason="quote_or_markup_stale_after_passage_repair", checked_at=checked_at)
            conn.execute("UPDATE v2_knowledge_drafts SET status='review_required',payload=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(payload, ensure_ascii=False), draft_id))
        affected_runs = set(changes["stale_steps_by_run"]) | set(changes["stale_drafts_by_run"])
        for run_id in affected_runs:
            row = conn.execute("SELECT payload,status FROM v2_reading_runs WHERE id=?", (run_id,)).fetchone()
            if not row or row["status"] in {"failed", "interrupted"}:
                continue
            payload = parse_json(row["payload"], {})
            if isinstance(payload.get("interruption"), dict):
                continue
            payload["quality_audit"] = {
                "status": "review_required",
                "stale_steps": changes["stale_steps_by_run"].get(run_id, 0),
                "stale_drafts": changes["stale_drafts_by_run"].get(run_id, 0),
                "checked_at": checked_at,
            }
            conn.execute("UPDATE v2_reading_runs SET status='review_required',payload=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(payload, ensure_ascii=False), run_id))
        for run_id in changes["stale_v2_runs"]:
            row = conn.execute("SELECT payload FROM v2_reading_runs WHERE id=?", (run_id,)).fetchone()
            payload = parse_json(row["payload"], {})
            payload["interruption"] = {"reason": "stale_running_state", "marked_at": checked_at}
            conn.execute("UPDATE v2_reading_runs SET status='interrupted',payload=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(payload, ensure_ascii=False), run_id))
        for run_id in changes["stale_v3_runs"]:
            row = conn.execute("SELECT payload FROM v3_repair_runs WHERE run_id=?", (run_id,)).fetchone()
            payload = parse_json(row["payload"], {})
            payload["interruption"] = {"reason": "stale_running_state", "marked_at": checked_at}
            conn.execute("UPDATE v3_repair_runs SET status='interrupted',payload=?,updated_at=CURRENT_TIMESTAMP WHERE run_id=?", (json.dumps(payload, ensure_ascii=False), run_id))
        for run_id in changes["invalid_v3_runs"]:
            row = conn.execute("SELECT payload FROM v3_repair_runs WHERE run_id=?", (run_id,)).fetchone()
            payload = parse_json(row["payload"], {})
            payload["invalidation"] = {
                "reason": "checkpoint_ledger_incomplete_and_short_passage_context_missing",
                "marked_at": checked_at,
            }
            conn.execute("UPDATE v3_repair_runs SET status='invalidated',payload=?,updated_at=CURRENT_TIMESTAMP WHERE run_id=?", (json.dumps(payload, ensure_ascii=False), run_id))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/three_kingdoms_rebuilt_v2.db")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--output", default="artifacts/stale_ingestion_remediation.json")
    args = parser.parse_args()

    db_path = Path(args.db)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    changes = find_changes(conn)
    summary = {
        "stale_evidence": len(changes["stale_evidence"]),
        "stale_claims": len(changes["stale_claims"]),
        "stale_anchors": len(changes["stale_anchors"]),
        "stale_steps": len(changes["stale_steps"]),
        "stale_drafts": len(changes["stale_drafts"]),
        "stale_v2_runs": len(changes["stale_v2_runs"]),
        "stale_v3_runs": len(changes["stale_v3_runs"]),
        "invalid_v3_runs": len(changes["invalid_v3_runs"]),
    }
    checked_at = datetime.now(UTC).isoformat()
    backup_path = None
    if args.apply:
        conn.close()
        if not args.no_backup:
            backup_path = Path(args.backup) if args.backup else db_path.with_name(f"{db_path.stem}.pre_quality_remediation_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}{db_path.suffix}")
            backup_database(db_path, backup_path)
        conn = sqlite3.connect(db_path)
        apply_changes(conn, changes, checked_at)
    conn.close()

    report = {"database": str(db_path), "applied": args.apply, "backup": str(backup_path) if backup_path else None, "checked_at": checked_at, "summary": summary}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
