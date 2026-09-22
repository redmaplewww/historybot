from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable

from .entity_resolution import build_identity_index, canonical_name_is_plausible
from .store import KnowledgeStore
from .text_normalization import normalize_surface

NEGATION_MARKERS = ("不", "未", "无", "莫", "非", "拒", "否认", "未曾")


def _payloads(store: KnowledgeStore, table: str) -> list[dict]:
    return [json.loads(row[0]) for row in store.db.execute(f"SELECT payload FROM {table} ORDER BY id")]


def _canonicalizer(store: KnowledgeStore, claims: list[dict], entities: list[dict], aliases: list[dict]):
    index = build_identity_index(entities, aliases, claims)
    alias_map = index["alias_map"]

    def canonical(value: str) -> str:
        current = normalize_surface(value or "")
        seen: set[str] = set()
        while current in alias_map and current not in seen:
            seen.add(current)
            current = alias_map[current]
        return current

    return canonical, index


def _candidate_id(dimension: str, left_ids: Iterable[str], right_ids: Iterable[str]) -> str:
    key = json.dumps({"dimension": dimension, "left": sorted(left_ids), "right": sorted(right_ids)}, ensure_ascii=False, sort_keys=True)
    return "cc_" + hashlib.sha256(key.encode()).hexdigest()[:20]


def _candidate(dimension: str, left_ids: list[str], right_ids: list[str], description: str, reason: str, *, source_ids: list[str] | None = None) -> dict:
    return {
        "id": _candidate_id(dimension, left_ids, right_ids),
        "status": "pending",
        "dimension": dimension,
        "left_ids": sorted(left_ids),
        "right_ids": sorted(right_ids),
        "description": description,
        "reason": reason,
        "source_ids": sorted(set(source_ids or [])),
        "reviewer": None,
    }


def generate_candidates(store: KnowledgeStore) -> list[dict]:
    """Generate evidence-linked conflict candidates without asserting a conflict."""

    claims = _payloads(store, "claims")
    events = _payloads(store, "events")
    entities = _payloads(store, "entities")
    aliases = _payloads(store, "entity_aliases")
    passages = {item["id"]: item for item in _payloads(store, "passages")}
    canonical, index = _canonicalizer(store, claims, entities, aliases)
    person_surfaces = set(index["canonical_surfaces"]) | set(index["alias_map"].values())
    candidates: dict[str, dict] = {}

    def sources_for_passages(passage_ids: Iterable[str]) -> list[str]:
        return [passages[pid]["source_id"] for pid in passage_ids if pid in passages]

    # Person/relation candidates: one normalized relation has incompatible objects.
    relation_groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for claim in claims:
        relation_groups[(canonical(claim.get("subject", "")), normalize_surface(claim.get("predicate", "")))].append(claim)
    for (subject, predicate), items in relation_groups.items():
        by_object: dict[str, list[dict]] = defaultdict(list)
        for item in items:
            by_object[canonical(item.get("object", ""))].append(item)
        objects = [value for value in by_object if value]
        if len(objects) < 2:
            continue
        left, right = objects[0], objects[1]
        if subject not in person_surfaces and not canonical_name_is_plausible(subject):
            continue
        left_claims = by_object[left]
        right_claims = by_object[right]
        candidate = _candidate(
            "person",
            [item["id"] for item in left_claims],
            [item["id"] for item in right_claims],
            f"同一人物 {subject} 的关系“{predicate}”出现不同对象：{left} / {right}",
            "相同主语与关系的对象不一致，需要结合时间、语境和来源独立性复核",
            source_ids=sources_for_passages([pid for item in left_claims + right_claims for pid in item.get("passage_ids", [])]),
        )
        candidates[candidate["id"]] = candidate

    # Person/relation candidates: explicit negative and positive statements overlap.
    for index_left, left in enumerate(claims):
        left_text = normalize_surface(left.get("statement", ""))
        if not any(marker in left_text for marker in NEGATION_MARKERS):
            continue
        for right in claims[index_left + 1 :]:
            if canonical(left.get("subject", "")) != canonical(right.get("subject", "")):
                continue
            right_text = normalize_surface(right.get("statement", ""))
            if any(marker in right_text for marker in NEGATION_MARKERS):
                continue
            left_tokens = {token for token in re.findall(r"[\u3400-\u9fff]{2,}", left_text)}
            right_tokens = {token for token in re.findall(r"[\u3400-\u9fff]{2,}", right_text)}
            if not (left_tokens & right_tokens):
                continue
            candidate = _candidate(
                "person",
                [left["id"]],
                [right["id"]],
                f"人物 {canonical(left.get('subject', ''))} 的正负表述可能冲突",
                "两条主张共享人物和关键语句，但一条含否定标记；需核对是否为不同时间或语境",
                source_ids=sources_for_passages(left.get("passage_ids", []) + right.get("passage_ids", [])),
            )
            candidates[candidate["id"]] = candidate

    # Time/place candidates: same event label with conflicting non-empty values.
    event_groups: dict[str, list[dict]] = defaultdict(list)
    for event in events:
        event_groups[normalize_surface(event.get("label", ""))].append(event)
    for label, items in event_groups.items():
        if len(items) < 2:
            continue
        for field, dimension, title in (("date_start", "time", "时间"), ("place", "place", "地点")):
            values: dict[str, list[dict]] = defaultdict(list)
            for item in items:
                value = normalize_surface(str(item.get(field) or ""))
                if value:
                    values[value].append(item)
            if len(values) < 2:
                continue
            value_items = list(values.items())
            left_value, left_events = value_items[0]
            right_value, right_events = value_items[1]
            left_participants = {canonical(value) for value in left_events[0].get("participant_surfaces", [])}
            right_participants = {canonical(value) for value in right_events[0].get("participant_surfaces", [])}
            if left_participants and right_participants and not (left_participants & right_participants):
                continue
            candidate = _candidate(
                dimension,
                [item["id"] for item in left_events],
                [item["id"] for item in right_events],
                f"事件“{label}”的{title}字段出现不同值：{left_value} / {right_value}",
                f"同名事件的{title}不一致，需要核对是否为同一事件、异名地点或不同纪年表达",
                source_ids=sources_for_passages([item.get("passage_id", "") for item in left_events + right_events]),
            )
            candidates[candidate["id"]] = candidate
    return sorted(candidates.values(), key=lambda item: item["id"])


def run_deterministic_review(db: str, task_id: str = "conflict_review") -> dict:
    store = KnowledgeStore(db)
    candidates = generate_candidates(store)
    for candidate in candidates:
        store.save_conflict_candidate(candidate, task_id)
    candidate_counts = {
        dimension: sum(item["dimension"] == dimension for item in candidates)
        for dimension in ("person", "place", "time")
    }
    scanned = {
        table: store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("claims", "events", "entities", "passages")
    }
    dimensions_scanned = ["person", "place", "time"]
    store.audit(
        task_id,
        "conflict_review_candidates_generated",
        task_id,
        {
            "candidate_count": len(candidates),
            "candidate_counts": candidate_counts,
            "dimensions_scanned": dimensions_scanned,
            "scanned": scanned,
        },
    )
    return {
        "status": "pending_model_review",
        "task_id": task_id,
        "candidates": len(candidates),
        "candidate_counts": candidate_counts,
        "dimensions_scanned": dimensions_scanned,
        "scanned": scanned,
        "counts": store.counts(),
    }
