"""Insert chronicle chunks previously lost to the 12-character length floor.

Existing passage IDs are preserved because claims and evidence reference them.
The migration aligns current non-empty passage text to freshly cleaned source
text in order, updates ordinals, and inserts only unmatched short chunks. It
refuses to write if any current non-empty passage cannot be matched exactly or
if an unmatched source chunk is 12 characters or longer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from historybot.ingest import clean_wikitext, split_passages, stable_id


def parse_json(value: Any, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def is_sanguozhi(source: dict[str, Any]) -> bool:
    metadata = source.get("metadata", {}) if isinstance(source.get("metadata"), dict) else {}
    collection = str(metadata.get("collection") or "")
    title = str(source.get("title") or "")
    return collection in {"三國志", "三国志"} or "三國志" in title or "三国志" in title


def fetch_wikitext(client: httpx.Client, url: str, *, retries: int = 6) -> str:
    for attempt in range(retries):
        response = client.get(url)
        if response.status_code != 429:
            response.raise_for_status()
            payload = response.json()
            parsed = payload.get("parse", {})
            raw = parsed.get("wikitext", {}).get("*")
            if not isinstance(raw, str):
                raise ValueError("source URL did not return parse.wikitext.*")
            return raw
        retry_after = response.headers.get("Retry-After")
        delay = float(retry_after) if retry_after and retry_after.isdigit() else min(30.0, 2.0 ** attempt)
        time.sleep(delay)
    raise RuntimeError(f"source remained rate limited after {retries} attempts: {url}")


def align_source(
    source_id: str,
    current_rows: list[sqlite3.Row],
    cleaned_text: str,
) -> dict[str, Any]:
    current_nonempty = []
    empty_rows = []
    for row in current_rows:
        payload = parse_json(row["payload"], {})
        text = str(payload.get("text") or "").strip()
        item = {"id": row["id"], "ordinal": row["ordinal"], "payload": payload, "text": text}
        if text:
            current_nonempty.append(item)
        else:
            empty_rows.append(item)

    fresh = split_passages(cleaned_text, source_id=source_id)
    current_texts = [item["text"] for item in current_nonempty]
    fresh_texts = [passage.text for passage in fresh]
    matcher = SequenceMatcher(None, current_texts, fresh_texts, autojunk=False)
    current_to_fresh: dict[int, int] = {}
    matched_fresh = set()
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            current_index = block.a + offset
            fresh_index = block.b + offset
            current_to_fresh[current_index] = fresh_index
            matched_fresh.add(fresh_index)

    unmatched_current = [current_nonempty[index] for index in range(len(current_nonempty)) if index not in current_to_fresh]
    missing = [fresh[index] for index in range(len(fresh)) if index not in matched_fresh]
    missing_short = [passage for passage in missing if len(passage.text) < 12]
    missing_long = [passage for passage in missing if len(passage.text) >= 12]
    safe = not unmatched_current and not missing_long
    return {
        "source_id": source_id,
        "safe": safe,
        "current_rows": len(current_rows),
        "current_nonempty": len(current_nonempty),
        "empty_rows": len(empty_rows),
        "fresh_rows": len(fresh),
        "matched_rows": len(current_to_fresh),
        "missing_rows": len(missing),
        "missing_short_rows": len(missing_short),
        "missing_long_rows": len(missing_long),
        "unmatched_current_rows": len(unmatched_current),
        "missing_chars": sum(len(passage.text) for passage in missing),
        "missing_year_like_rows": sum(1 for passage in missing if "年" in passage.text or "歲" in passage.text or "岁" in passage.text),
        "missing_samples": [{"ordinal": passage.ordinal, "text": passage.text} for passage in missing[:12]],
        "unmatched_current_samples": [{"id": item["id"], "ordinal": item["ordinal"], "text": item["text"]} for item in unmatched_current[:12]],
        "_current_nonempty": current_nonempty,
        "_empty_rows": empty_rows,
        "_fresh": fresh,
        "_current_to_fresh": current_to_fresh,
        "_matched_fresh": matched_fresh,
    }


def apply_source(conn: sqlite3.Connection, alignment: dict[str, Any], migrated_at: str) -> int:
    for current_index, fresh_index in alignment["_current_to_fresh"].items():
        current = alignment["_current_nonempty"][current_index]
        payload = dict(current["payload"])
        payload["ordinal"] = fresh_index
        locator = payload.get("locator", {}) if isinstance(payload.get("locator"), dict) else {}
        locator["ordinal"] = fresh_index
        payload["locator"] = locator
        conn.execute(
            "UPDATE passages SET ordinal=?,payload=? WHERE id=?",
            (fresh_index, json.dumps(payload, ensure_ascii=False), current["id"]),
        )
    for empty_index, current in enumerate(alignment["_empty_rows"]):
        payload = dict(current["payload"])
        payload["quality_status"] = "empty_after_markup_cleanup"
        payload["quality_audit"] = {"status": "excluded_from_alignment", "checked_at": migrated_at}
        # Keep tombstones after the real sequence without colliding with source ordinals.
        ordinal = len(alignment["_fresh"]) + empty_index
        payload["ordinal"] = ordinal
        locator = payload.get("locator", {}) if isinstance(payload.get("locator"), dict) else {}
        locator["ordinal"] = ordinal
        payload["locator"] = locator
        conn.execute("UPDATE passages SET ordinal=?,payload=? WHERE id=?", (ordinal, json.dumps(payload, ensure_ascii=False), current["id"]))

    inserted = 0
    for fresh_index, passage in enumerate(alignment["_fresh"]):
        if fresh_index in alignment["_matched_fresh"]:
            continue
        passage_id = stable_id("p", f"{alignment['source_id']}:{fresh_index}:{passage.text}")
        payload = passage.model_dump(mode="json")
        payload["id"] = passage_id
        payload["ordinal"] = fresh_index
        payload["locator"] = {"ordinal": fresh_index}
        payload["text_hash"] = hashlib.sha256(passage.text.encode()).hexdigest()
        payload["quality_status"] = "pending_extraction_after_short_passage_migration"
        payload["quality_audit"] = {"status": "newly_restored", "checked_at": migrated_at, "reason": "previous_length_floor"}
        conn.execute(
            "INSERT INTO passages(id,source_id,ordinal,payload) VALUES(?,?,?,?)",
            (passage_id, alignment["source_id"], fresh_index, json.dumps(payload, ensure_ascii=False)),
        )
        inserted += 1
    return inserted


def public_alignment(alignment: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in alignment.items() if not key.startswith("_")}


def backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(source)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(destination_conn)
    finally:
        destination_conn.close()
        source_conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/three_kingdoms_rebuilt_v2.db")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--delay", type=float, default=0.75)
    parser.add_argument("--output", default="artifacts/short_passage_migration.json")
    args = parser.parse_args()

    db_path = Path(args.db)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    selected = []
    for row in conn.execute("SELECT id,payload FROM sources ORDER BY json_extract(payload,'$.metadata.sequence'),id"):
        payload = parse_json(row["payload"], {})
        if is_sanguozhi(payload):
            selected.append((row["id"], payload))
    if args.limit is not None:
        selected = selected[: args.limit]

    alignments = []
    with httpx.Client(timeout=90, headers={"User-Agent": "historybot-research/0.4"}) as client:
        for index, (source_id, source) in enumerate(selected, start=1):
            rows = conn.execute("SELECT id,ordinal,payload FROM passages WHERE source_id=? ORDER BY ordinal,id", (source_id,)).fetchall()
            raw = fetch_wikitext(client, str(source.get("url") or ""))
            alignment = align_source(source_id, rows, clean_wikitext(raw))
            alignment["title"] = source.get("title", "")
            alignments.append(alignment)
            print(f"ALIGN {index}/{len(selected)} {source_id}: safe={alignment['safe']} missing={alignment['missing_rows']} unmatched={alignment['unmatched_current_rows']}", flush=True)
            if args.delay > 0 and index < len(selected):
                time.sleep(args.delay)

    safe = all(item["safe"] for item in alignments)
    totals = {
        "sources": len(alignments),
        "safe_sources": sum(1 for item in alignments if item["safe"]),
        "missing_rows": sum(item["missing_rows"] for item in alignments),
        "missing_chars": sum(item["missing_chars"] for item in alignments),
        "missing_year_like_rows": sum(item["missing_year_like_rows"] for item in alignments),
        "empty_rows": sum(item["empty_rows"] for item in alignments),
        "unmatched_current_rows": sum(item["unmatched_current_rows"] for item in alignments),
        "missing_long_rows": sum(item["missing_long_rows"] for item in alignments),
    }
    backup_path = None
    inserted = 0
    migrated_at = datetime.now(UTC).isoformat()
    if args.apply:
        if not safe:
            raise SystemExit("migration refused: at least one source failed exact monotonic alignment")
        conn.close()
        backup_path = Path(args.backup) if args.backup else db_path.with_name(f"{db_path.stem}.pre_short_passage_migration_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}{db_path.suffix}")
        backup_database(db_path, backup_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        with conn:
            for alignment in alignments:
                inserted += apply_source(conn, alignment, migrated_at)
            affected_sources = [item["source_id"] for item in alignments if item["missing_rows"]]
            for source_id in affected_sources:
                passage_ids = [row[0] for row in conn.execute("SELECT id FROM passages WHERE source_id=?", (source_id,))]
                if not passage_ids:
                    continue
                placeholders = ",".join("?" for _ in passage_ids)
                conn.execute(f"UPDATE v3_object_time_links SET status='stale_context_after_passage_migration',updated_at=CURRENT_TIMESTAMP WHERE passage_id IN ({placeholders})", passage_ids)
            conn.execute("UPDATE v3_repair_runs SET status='invalidated',updated_at=CURRENT_TIMESTAMP WHERE status='complete'")
    conn.close()

    report = {
        "database": str(db_path),
        "applied": args.apply,
        "safe": safe,
        "backup": str(backup_path) if backup_path else None,
        "inserted": inserted,
        "totals": totals,
        "sources": [public_alignment(item) for item in alignments],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("applied", "safe", "backup", "inserted", "totals")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
