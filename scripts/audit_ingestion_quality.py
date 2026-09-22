"""Audit whether the ingestion database is usable as an evidence corpus.

The report deliberately separates extracted candidates from independently
reviewed material. Large row counts are not treated as proof of historical
reliability, and every quote check is performed against the current passage
text so text repairs cannot leave apparently verified stale outputs behind.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from historybot.repair import era_absolute_anchor

MARKUP_PATTERN = re.compile(r"\{\{|\}\}|\[\[|\]\]|<ref\b|-\{|\}-|__[A-Z0-9_]+__|={2,}")
BROAD_SURFACES = {
    "先主",
    "後主",
    "后主",
    "太祖",
    "帝",
    "王",
    "公",
    "君",
    "主",
    "天子",
    "皇帝",
    "將軍",
    "将军",
    "丞相",
    "其",
    "彼",
    "此",
    "某",
}


def parse_json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def add_sample(samples: list[dict[str, Any]], item: dict[str, Any], limit: int) -> None:
    if len(samples) < limit:
        samples.append(item)


def quotes_from_payload(payload: dict[str, Any]) -> list[str]:
    quotes: list[str] = []
    direct = payload.get("evidence_quotes", [])
    if isinstance(direct, list):
        quotes.extend(str(value).strip() for value in direct if str(value).strip())
    units = payload.get("units", [])
    if isinstance(units, list):
        for unit in units:
            if isinstance(unit, dict):
                values = unit.get("evidence_quotes", [])
                if isinstance(values, list):
                    quotes.extend(str(value).strip() for value in values if str(value).strip())
    return quotes


def contains_markup(value: Any) -> bool:
    """Search textual values without treating JSON punctuation as source text."""
    if isinstance(value, str):
        return bool(MARKUP_PATTERN.search(value))
    if isinstance(value, dict):
        return any(contains_markup(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_markup(item) for item in value)
    return False


def reading_step_quote_mismatches(
    output: dict[str, Any], passages: dict[str, dict[str, Any]], fallback_passage_id: str
) -> list[dict[str, str]]:
    """Check each reading unit against the passage ID carried by that unit."""
    mismatches: list[dict[str, str]] = []
    units = output.get("units", [])
    if isinstance(units, list) and units:
        for unit in units:
            if not isinstance(unit, dict):
                continue
            passage_id = str(unit.get("passage_id") or fallback_passage_id)
            text = str(passages.get(passage_id, {}).get("text") or "")
            values = unit.get("evidence_quotes", [])
            if not isinstance(values, list):
                continue
            for value in values:
                quote = str(value).strip()
                if quote and quote not in text:
                    mismatches.append({"passage_id": passage_id, "quote": quote})
        return mismatches
    text = str(passages.get(fallback_passage_id, {}).get("text") or "")
    for quote in quotes_from_payload(output):
        if quote not in text:
            mismatches.append({"passage_id": fallback_passage_id, "quote": quote})
    return mismatches


def date_key(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    match = re.fullmatch(r"(-?\d{1,4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", value.strip())
    if not match:
        return None
    year, month, day = (int(part) if part else default for part, default in zip(match.groups(), (0, 1, 1)))
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return None
    return year, month, day


def parse_timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def source_collection(payload: dict[str, Any]) -> str:
    metadata = payload.get("metadata", {}) if isinstance(payload.get("metadata"), dict) else {}
    collection = str(metadata.get("collection") or "").strip()
    if collection:
        return collection
    title = str(payload.get("title") or "未命名来源")
    return title.split("·", 1)[0].split("/", 1)[0]


def audit(db_path: Path, *, sample_limit: int = 20) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    sources = {
        row["id"]: parse_json(row["payload"], {})
        for row in conn.execute("SELECT id,payload FROM sources")
    }
    passages: dict[str, dict[str, Any]] = {}
    passage_sources: dict[str, str] = {}
    source_passages: dict[str, list[str]] = defaultdict(list)
    for row in conn.execute("SELECT id,source_id,ordinal,payload FROM passages"):
        payload = parse_json(row["payload"], {})
        payload.update(id=row["id"], source_id=row["source_id"], ordinal=row["ordinal"])
        passages[row["id"]] = payload
        passage_sources[row["id"]] = row["source_id"]
        source_passages[row["source_id"]].append(row["id"])

    passage_quality = Counter()
    passage_samples: list[dict[str, Any]] = []
    for passage_id, passage in passages.items():
        text = str(passage.get("text") or "")
        issues = []
        if not text.strip():
            issues.append("empty")
        elif len(text.strip()) < 12:
            issues.append("short")
        if MARKUP_PATTERN.search(text):
            issues.append("markup_like")
        for issue in issues:
            passage_quality[issue] += 1
        if issues:
            add_sample(
                passage_samples,
                {"passage_id": passage_id, "source_id": passage.get("source_id"), "ordinal": passage.get("ordinal"), "text": text, "issues": issues},
                sample_limit,
            )

    table_counts = {}
    for table in (
        "sources",
        "passages",
        "entities",
        "events",
        "claims",
        "evidence",
        "relations",
        "v2_reading_runs",
        "v2_reading_steps",
        "v2_knowledge_drafts",
        "v2_evidence_anchors",
        "v3_repair_runs",
        "v3_repair_steps",
        "v3_object_time_links",
    ):
        table_counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    per_source: dict[str, dict[str, Any]] = {}
    source_claim_sets: dict[str, set[str]] = defaultdict(set)
    for source_id, source in sources.items():
        per_source[source_id] = {
            "source_id": source_id,
            "title": source.get("title", ""),
            "collection": source_collection(source),
            "passages": len(source_passages.get(source_id, [])),
            "entities": 0,
            "events": 0,
            "claims": 0,
            "evidence": 0,
            "v2_read_steps": 0,
            "v2_verify_steps": 0,
            "v2_anchors": 0,
            "v2_verified_anchors": 0,
            "v2_stale_anchors": 0,
        }

    referential = Counter()
    quote_checks = Counter()
    quote_samples: list[dict[str, Any]] = []
    evidence_rows = list(conn.execute("SELECT id,claim_id,passage_id,payload FROM evidence"))
    claim_ids = {row[0] for row in conn.execute("SELECT id FROM claims")}
    for row in evidence_rows:
        passage = passages.get(row["passage_id"])
        source_id = passage_sources.get(row["passage_id"])
        if row["claim_id"] not in claim_ids:
            referential["evidence_missing_claim"] += 1
        if not passage:
            referential["evidence_missing_passage"] += 1
            continue
        if source_id not in sources:
            referential["evidence_missing_source"] += 1
        if source_id in per_source:
            per_source[source_id]["evidence"] += 1
            source_claim_sets[source_id].add(row["claim_id"])
        payload = parse_json(row["payload"], {})
        quality_audit = payload.get("quality_audit", {}) if isinstance(payload.get("quality_audit"), dict) else {}
        if quality_audit.get("status") == "stale":
            quote_checks["flagged_stale"] += 1
        quote = str(payload.get("quote") or "")
        quote_checks["total"] += 1
        if not quote:
            quote_checks["empty"] += 1
        elif quote in str(passage.get("text") or ""):
            quote_checks["exact_match"] += 1
        else:
            quote_checks["stale_or_invented"] += 1
            add_sample(
                quote_samples,
                {
                    "evidence_id": row["id"],
                    "claim_id": row["claim_id"],
                    "passage_id": row["passage_id"],
                    "quote": quote,
                    "passage_text": passage.get("text", ""),
                },
                sample_limit,
            )

    claim_status = Counter()
    claim_review_state = Counter()
    claim_duplicate_counter: Counter[str] = Counter()
    claim_duplicate_examples: dict[str, dict[str, Any]] = {}
    claims_without_evidence = 0
    evidence_claim_ids = {row["claim_id"] for row in evidence_rows}
    missing_claim_passages = 0
    temporal = Counter()
    temporal_samples: list[dict[str, Any]] = []
    for row in conn.execute("SELECT id,status,payload FROM claims"):
        payload = parse_json(row["payload"], {})
        claim_status[row["status"]] += 1
        metadata = payload.get("metadata", {}) if isinstance(payload.get("metadata"), dict) else {}
        claim_review_state[str(metadata.get("review_state") or "unspecified")] += 1
        if row["id"] not in evidence_claim_ids:
            claims_without_evidence += 1
        passage_ids = payload.get("passage_ids", []) or []
        for passage_id in passage_ids:
            if passage_id not in passages:
                missing_claim_passages += 1
        key_payload = {
            "subject": payload.get("subject"),
            "predicate": payload.get("predicate"),
            "object": payload.get("object"),
            "statement": payload.get("statement"),
            "passage_ids": sorted(passage_ids),
        }
        key = json.dumps(key_payload, ensure_ascii=False, sort_keys=True)
        claim_duplicate_counter[key] += 1
        claim_duplicate_examples.setdefault(key, {"claim_id": row["id"], **key_payload})
        start, end = payload.get("time_from"), payload.get("time_to")
        if start or end:
            temporal["claims_with_time"] += 1
        for endpoint, value in (("time_from", start), ("time_to", end)):
            if value and date_key(value) is None:
                temporal[f"claim_invalid_{endpoint}"] += 1
                add_sample(temporal_samples, {"kind": "claim", "id": row["id"], endpoint: value}, sample_limit)
        if date_key(start) and date_key(end) and date_key(start) > date_key(end):
            temporal["claim_reversed_range"] += 1

    entity_types = Counter()
    entity_resolution = Counter()
    entity_issues = Counter()
    entity_samples: list[dict[str, Any]] = []
    for row in conn.execute("SELECT id,payload FROM entities"):
        payload = parse_json(row["payload"], {})
        surface = str(payload.get("surface") or "").strip()
        entity_type = str(payload.get("entity_type") or "unspecified")
        resolution = payload.get("resolution", {}) if isinstance(payload.get("resolution"), dict) else {}
        entity_types[entity_type] += 1
        entity_resolution[str(resolution.get("status") or payload.get("status") or "unspecified")] += 1
        passage_id = payload.get("passage_id")
        if passage_id and passage_id not in passages:
            referential["entity_missing_passage"] += 1
        source_id = passage_sources.get(str(passage_id))
        if source_id in per_source:
            per_source[source_id]["entities"] += 1
        issues = []
        if not surface:
            issues.append("empty")
        if MARKUP_PATTERN.search(surface):
            issues.append("markup_like")
        if surface in BROAD_SURFACES:
            issues.append("context_dependent_or_broad")
        if len(surface) > 40:
            issues.append("overly_long")
        if surface and not re.search(r"[\w\u3400-\u9fff]", surface):
            issues.append("punctuation_only")
        for issue in issues:
            entity_issues[issue] += 1
        if issues:
            add_sample(entity_samples, {"entity_id": row["id"], "surface": surface, "entity_type": entity_type, "issues": issues}, sample_limit)

    event_status = Counter()
    event_completeness = Counter()
    event_duplicate_counter: Counter[str] = Counter()
    event_duplicate_examples: dict[str, dict[str, Any]] = {}
    for row in conn.execute("SELECT id,payload FROM events"):
        payload = parse_json(row["payload"], {})
        event_status[str(payload.get("status") or "unspecified")] += 1
        passage_id = str(payload.get("passage_id") or "")
        if passage_id and passage_id not in passages:
            referential["event_missing_passage"] += 1
        source_id = passage_sources.get(passage_id)
        if source_id in per_source:
            per_source[source_id]["events"] += 1
        participants = [str(value).strip() for value in payload.get("participant_surfaces", []) if str(value).strip()]
        if not participants:
            event_completeness["missing_participants"] += 1
        if not str(payload.get("place") or "").strip():
            event_completeness["missing_place"] += 1
        start, end = payload.get("date_start"), payload.get("date_end")
        if not start and not end:
            event_completeness["missing_date"] += 1
        else:
            temporal["events_with_time"] += 1
        for endpoint, value in (("date_start", start), ("date_end", end)):
            if value and date_key(value) is None:
                temporal[f"event_invalid_{endpoint}"] += 1
                add_sample(temporal_samples, {"kind": "event", "id": row["id"], endpoint: value}, sample_limit)
        if date_key(start) and date_key(end) and date_key(start) > date_key(end):
            temporal["event_reversed_range"] += 1
        key_payload = {
            "event_type": payload.get("event_type"),
            "label": payload.get("label"),
            "date_start": start,
            "date_end": end,
            "place": payload.get("place"),
            "participants": sorted(set(participants)),
            "passage_id": passage_id,
        }
        key = json.dumps(key_payload, ensure_ascii=False, sort_keys=True)
        event_duplicate_counter[key] += 1
        event_duplicate_examples.setdefault(key, {"event_id": row["id"], **key_payload})

    for source_id, ids in source_claim_sets.items():
        per_source[source_id]["claims"] = len(ids)
    for row in conn.execute("SELECT claim_id FROM relations WHERE claim_id IS NOT NULL"):
        if row["claim_id"] not in claim_ids:
            referential["relation_missing_claim"] += 1
    referential["claims_without_evidence"] = claims_without_evidence
    referential["claim_payload_missing_passage"] = missing_claim_passages

    v2_anchor_checks = Counter()
    v2_anchor_samples: list[dict[str, Any]] = []
    for row in conn.execute("SELECT id,draft_id,passage_id,quote,verified,payload FROM v2_evidence_anchors"):
        passage = passages.get(row["passage_id"])
        source_id = passage_sources.get(row["passage_id"])
        if source_id in per_source:
            per_source[source_id]["v2_anchors"] += 1
            per_source[source_id]["v2_verified_anchors"] += int(bool(row["verified"]))
        v2_anchor_checks["total"] += 1
        anchor_payload = parse_json(row["payload"], {})
        quality_audit = anchor_payload.get("quality_audit", {}) if isinstance(anchor_payload.get("quality_audit"), dict) else {}
        if quality_audit.get("status") == "stale":
            v2_anchor_checks["flagged_stale"] += 1
        if not passage:
            v2_anchor_checks["missing_passage"] += 1
            continue
        quote = str(row["quote"] or "")
        if quote and quote in str(passage.get("text") or ""):
            v2_anchor_checks["exact_match"] += 1
        else:
            v2_anchor_checks["stale_or_invented"] += 1
            if row["verified"]:
                v2_anchor_checks["verified_but_stale"] += 1
            if source_id in per_source:
                per_source[source_id]["v2_stale_anchors"] += 1
            add_sample(
                v2_anchor_samples,
                {"anchor_id": row["id"], "draft_id": row["draft_id"], "passage_id": row["passage_id"], "verified": row["verified"], "quote": quote, "passage_text": passage.get("text", "")},
                sample_limit,
            )

    stale_v2 = Counter()
    stale_v2_samples: list[dict[str, Any]] = []
    for row in conn.execute("SELECT id,step_type,input_ref,output,status FROM v2_reading_steps WHERE input_ref IS NOT NULL"):
        if row["step_type"] in {"read", "verify"}:
            source_id = passage_sources.get(row["input_ref"])
            if source_id in per_source:
                per_source[source_id][f"v2_{row['step_type']}_steps"] += 1
        passage = passages.get(row["input_ref"])
        if not passage:
            continue
        output = parse_json(row["output"], {})
        bad_quotes = reading_step_quote_mismatches(output, passages, row["input_ref"])
        has_markup = contains_markup(output)
        if bad_quotes or has_markup:
            stale_v2["reading_steps"] += 1
            stale_v2[f"reading_steps_{row['step_type']}"] += 1
            if bad_quotes:
                stale_v2["reading_steps_quote_mismatch"] += 1
            if has_markup:
                stale_v2["reading_steps_with_markup"] += 1
            if row["status"] == "stale":
                stale_v2["reading_steps_flagged_stale"] += 1
            add_sample(stale_v2_samples, {"kind": "reading_step", "id": row["id"], "passage_id": row["input_ref"], "status": row["status"], "bad_quotes": bad_quotes[:3]}, sample_limit)

    draft_status = Counter()
    for row in conn.execute("SELECT id,status,payload FROM v2_knowledge_drafts"):
        draft_status[row["status"]] += 1
        payload = parse_json(row["payload"], {})
        passage_id = str(payload.get("passage_id") or "")
        passage = passages.get(passage_id)
        if not passage:
            if passage_id:
                stale_v2["draft_missing_passage"] += 1
            continue
        quotes = quotes_from_payload(payload)
        bad_quotes = [quote for quote in quotes if quote not in str(passage.get("text") or "")]
        has_markup = contains_markup(payload)
        if bad_quotes or has_markup:
            stale_v2["knowledge_drafts"] += 1
            if bad_quotes:
                stale_v2["knowledge_drafts_quote_mismatch"] += 1
            if has_markup:
                stale_v2["knowledge_drafts_with_markup"] += 1
            quality_audit = payload.get("quality_audit", {}) if isinstance(payload.get("quality_audit"), dict) else {}
            if quality_audit.get("status") == "stale":
                stale_v2["knowledge_drafts_flagged_stale"] += 1
            add_sample(stale_v2_samples, {"kind": "knowledge_draft", "id": row["id"], "passage_id": passage_id, "status": row["status"], "bad_quotes": bad_quotes[:3]}, sample_limit)

    era_checks = Counter()
    era_samples: list[dict[str, Any]] = []
    for row in conn.execute("SELECT object_id,passage_id,time_ref FROM v3_object_time_links"):
        ref = parse_json(row["time_ref"], {})
        expression = str(ref.get("time_expression") or "")
        anchor = era_absolute_anchor(expression)
        if not anchor:
            continue
        era_checks["era_expressions"] += 1
        expected_year = anchor[2]
        actual = date_key(ref.get("time_from"))
        if actual and actual[0] == expected_year:
            era_checks["consistent"] += 1
        else:
            era_checks["inconsistent_or_unresolved"] += 1
            add_sample(era_samples, {"object_id": row["object_id"], "passage_id": row["passage_id"], "time_expression": expression, "expected_year": expected_year, "time_from": ref.get("time_from")}, sample_limit)

    now = datetime.now(UTC)
    stale_cutoff = now - timedelta(hours=6)
    run_audit: dict[str, Any] = {"v2_status": Counter(), "v3_runs": [], "stale_runs": []}
    for row in conn.execute("SELECT id,status,updated_at,payload FROM v2_reading_runs"):
        run_audit["v2_status"][row["status"]] += 1
        if row["status"] == "reading":
            updated = parse_timestamp(row["updated_at"])
            if updated < stale_cutoff:
                run_audit["stale_runs"].append({"kind": "v2", "run_id": row["id"], "status": row["status"], "updated_at": row["updated_at"]})
    for row in conn.execute("SELECT run_id,status,updated_at,payload FROM v3_repair_runs ORDER BY updated_at"):
        payload = parse_json(row["payload"], {})
        ledger = {
            item["status"]: item["n"]
            for item in conn.execute("SELECT status,COUNT(*) n FROM v3_repair_steps WHERE run_id=? GROUP BY status", (row["run_id"],))
        }
        item = {"run_id": row["run_id"], "status": row["status"], "updated_at": row["updated_at"], "payload": payload, "ledger": ledger, "ledger_total": sum(ledger.values())}
        expected = payload.get("windows")
        if isinstance(expected, int):
            item["window_ledger_delta"] = expected - item["ledger_total"]
        run_audit["v3_runs"].append(item)
        if row["status"] == "running":
            updated = parse_timestamp(row["updated_at"])
            if updated < stale_cutoff:
                run_audit["stale_runs"].append({"kind": "v3", "run_id": row["run_id"], "status": row["status"], "updated_at": row["updated_at"]})
    run_audit["v2_status"] = dict(run_audit["v2_status"])

    collections: dict[str, Counter[str]] = defaultdict(Counter)
    for item in per_source.values():
        collection = item["collection"]
        for key in ("passages", "entities", "events", "claims", "evidence", "v2_read_steps", "v2_verify_steps", "v2_anchors", "v2_verified_anchors", "v2_stale_anchors"):
            collections[collection][key] += int(item[key])
        collections[collection]["sources"] += 1

    claim_duplicate_groups = [(key, count) for key, count in claim_duplicate_counter.items() if count > 1]
    event_duplicate_groups = [(key, count) for key, count in event_duplicate_counter.items() if count > 1]
    duplicates = {
        "claims": {
            "groups": len(claim_duplicate_groups),
            "extra_rows": sum(count - 1 for _, count in claim_duplicate_groups),
            "samples": [{"count": count, **claim_duplicate_examples[key]} for key, count in claim_duplicate_groups[:sample_limit]],
        },
        "events": {
            "groups": len(event_duplicate_groups),
            "extra_rows": sum(count - 1 for _, count in event_duplicate_groups),
            "samples": [{"count": count, **event_duplicate_examples[key]} for key, count in event_duplicate_groups[:sample_limit]],
        },
    }

    short_loss = None
    short_loss_path = Path("artifacts/short_passage_loss.json")
    if short_loss_path.exists():
        short_loss = parse_json(short_loss_path.read_text(encoding="utf-8"), None)
    short_migration = None
    short_migration_path = Path("artifacts/short_passage_migration.json")
    if short_migration_path.exists():
        short_migration = parse_json(short_migration_path.read_text(encoding="utf-8"), None)

    report = {
        "generated_at": now.isoformat(),
        "database": str(db_path),
        "sqlite_integrity": integrity,
        "counts": table_counts,
        "coverage": {
            "collections": {key: dict(value) for key, value in sorted(collections.items())},
            "sources": sorted(per_source.values(), key=lambda item: (item["collection"], item["title"])),
        },
        "passage_quality": {**dict(passage_quality), "samples": passage_samples},
        "referential_integrity": dict(referential),
        "evidence_quote_containment": {**dict(quote_checks), "samples": quote_samples},
        "v2_anchor_quote_containment": {**dict(v2_anchor_checks), "samples": v2_anchor_samples},
        "stale_v2_outputs": {**dict(stale_v2), "samples": stale_v2_samples},
        "review_state": {
            "claim_status": dict(claim_status),
            "claim_review_state": dict(claim_review_state),
            "entity_resolution": dict(entity_resolution),
            "event_status": dict(event_status),
            "draft_status": dict(draft_status),
        },
        "entities": {"types": dict(entity_types.most_common()), "issues": dict(entity_issues), "samples": entity_samples},
        "events": {"completeness": dict(event_completeness)},
        "duplicates": duplicates,
        "temporal": {**dict(temporal), "era_consistency": dict(era_checks), "samples": temporal_samples, "era_samples": era_samples},
        "run_ledger": run_audit,
        "known_short_passage_loss_before_fix": short_loss,
        "short_passage_migration_audit": short_migration,
        "interpretation": {
            "candidate_layer": "extracted/disputed/review_required rows are candidates, not verified historical facts",
            "verified_layer": "machine_reviewed or verified labels still require quote containment and provenance checks",
            "current_blocker": "Do not run full V3 temporal repair until short passages are migrated and stale V2 outputs are invalidated",
        },
    }
    conn.close()
    return report


def pct(numerator: int, denominator: int) -> str:
    return f"{(100.0 * numerator / denominator):.2f}%" if denominator else "n/a"


def render_markdown(report: dict[str, Any]) -> str:
    counts = report["counts"]
    evidence = report["evidence_quote_containment"]
    anchors = report["v2_anchor_quote_containment"]
    stale = report["stale_v2_outputs"]
    passage_quality = report["passage_quality"]
    review = report["review_state"]
    ref = report["referential_integrity"]
    events = report["events"]["completeness"]
    migration = report.get("short_passage_migration_audit") or {}
    migration_totals = migration.get("totals", {}) if isinstance(migration, dict) else {}
    lines = [
        "# 史料入库合理性审查",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 数据库：`{report['database']}`",
        f"- SQLite 完整性：`{report['sqlite_integrity']}`",
        "",
        "## 结论",
        "",
        "当前库是规模较大的抽取候选库，不等同于已经史学核验的知识库。引用可回溯性总体存在，但文本修复后仍有旧版 AI 输出和证据锚点失效；短段落尚未迁移进现库，因此不应启动全量 V3 时间修复。",
        "",
        "## 规模与审查状态",
        "",
        f"- 来源 {counts['sources']}，段落 {counts['passages']}，主张 {counts['claims']}，证据 {counts['evidence']}，事件 {counts['events']}，实体 {counts['entities']}。",
        f"- 当前段落中空文本 {passage_quality.get('empty', 0)}，12 字以下短段 {passage_quality.get('short', 0)}，残留标记 {passage_quality.get('markup_like', 0)}。现有短段主要来自后续文本修复，不能覆盖旧分段阈值已经丢弃的原始短条目。",
        f"- 主张状态：`{json.dumps(review['claim_status'], ensure_ascii=False)}`。",
        f"- 草稿状态：`{json.dumps(review['draft_status'], ensure_ascii=False)}`。",
        f"- 实体解析状态：`{json.dumps(review['entity_resolution'], ensure_ascii=False)}`。",
        "",
        "## 证据与文本一致性",
        "",
        f"- 正式 evidence 精确命中 {evidence.get('exact_match', 0)}/{evidence.get('total', 0)}（{pct(evidence.get('exact_match', 0), evidence.get('total', 0))}）；失效或非原文引用 {evidence.get('stale_or_invented', 0)}，已标记 stale {evidence.get('flagged_stale', 0)}。",
        f"- V2 锚点精确命中 {anchors.get('exact_match', 0)}/{anchors.get('total', 0)}（{pct(anchors.get('exact_match', 0), anchors.get('total', 0))}）；失效 {anchors.get('stale_or_invented', 0)}，已降级 {anchors.get('flagged_stale', 0)}。",
        f"- 受文本修复影响的 reading steps {stale.get('reading_steps', 0)}（已标 stale {stale.get('reading_steps_flagged_stale', 0)}），knowledge drafts {stale.get('knowledge_drafts', 0)}（已降级 {stale.get('knowledge_drafts_flagged_stale', 0)}）。",
        "",
        "## 结构完整性",
        "",
        f"- 引用完整性计数：`{json.dumps(ref, ensure_ascii=False)}`。",
        f"- 事件缺参与者 {events.get('missing_participants', 0)}，缺地点 {events.get('missing_place', 0)}，缺日期 {events.get('missing_date', 0)}。这些不必然是错误，但属于查询能力边界。",
        f"- 重复主张额外行 {report['duplicates']['claims']['extra_rows']}；重复事件额外行 {report['duplicates']['events']['extra_rows']}。",
        f"- 65 卷重分段只读审计：缺失段落 {migration_totals.get('missing_rows', 'n/a')}，缺失字符 {migration_totals.get('missing_chars', 'n/a')}，含年/岁段落 {migration_totals.get('missing_year_like_rows', 'n/a')}，现存无法精确对齐段落 {migration_totals.get('unmatched_current_rows', 'n/a')}；可直接安全原位迁移的卷仅 {migration_totals.get('safe_sources', 'n/a')}/{migration_totals.get('sources', 'n/a')}。",
        "",
        "## 运行账本",
        "",
        f"- 陈旧运行 {len(report['run_ledger']['stale_runs'])} 个。",
    ]
    for run in report["run_ledger"]["v3_runs"]:
        lines.append(f"- `{run['run_id']}`：状态 `{run['status']}`，窗口账本 {run['ledger_total']}，记录窗口 {run.get('payload', {}).get('windows', 'n/a')}，差值 {run.get('window_ledger_delta', 'n/a')}。")
    lines.extend([
        "",
        "## 处置顺序",
        "",
        "1. 将短段落迁移入现库，并保持既有 passage_id 引用不失效。",
        "2. 把引用不再命中当前原文的 V2 step/draft/anchor 标记为 stale 或 review_required，重新阅读受影响段落。",
        "3. 清理陈旧 run 状态并以修复后的 run-scoped checkpoint 重新做小批量验证。",
        "4. 以上通过后，才启动 65 卷全量 V3 时间修复。",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/three_kingdoms_rebuilt_v2.db")
    parser.add_argument("--json-output", default="artifacts/ingestion_quality_audit.json")
    parser.add_argument("--markdown-output", default="artifacts/ingestion_quality_audit.md")
    parser.add_argument("--sample-limit", type=int, default=20)
    args = parser.parse_args()

    report = audit(Path(args.db), sample_limit=max(1, args.sample_limit))
    json_output = Path(args.json_output)
    markdown_output = Path(args.markdown_output)
    json_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_output.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"json": str(json_output), "markdown": str(markdown_output), "counts": report["counts"], "stale_runs": len(report["run_ledger"]["stale_runs"])}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
