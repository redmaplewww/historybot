from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any

from .store import KnowledgeStore
from .text_normalization import normalize_surface

REJECTED_TYPES = {"地点", "时间", "官职", "爵位", "姓氏", "政治空间", "组织", "事件", "族群"}
GENERIC_SURFACES = {
    "天下", "世人", "莫能審", "未明确说明的对象", "未明确说明的事项",
    "先主", "后主", "后", "皇后", "皇太后", "太后", "夫人", "山阳公",
    "明帝", "明皇帝", "文帝", "武帝", "少帝", "献帝", "天子", "陛下",
    "丞相", "太守", "将军", "刺史", "司空", "司徒", "太尉", "校尉",
    "皇叔", "使君", "主上", "世子", "二嫂", "二兄", "二弟", "二子",
}
CANONICAL_TITLE_SUFFIXES = (
    "公", "帝", "后", "王", "侯", "夫人", "太后", "皇后", "皇叔", "使君",
    "丞相", "相国", "司空", "司徒", "太尉", "太守", "刺史", "将军", "大将军",
    "校尉", "中郎将", "令", "尉", "君", "主", "太子", "公主", "单于", "可汗",
    "传", "紀", "记", "表", "书",
)
IDENTITY_PREDICATES = {"字", "讳", "姓名", "名"}
# These relations are accepted only when the alias points to one unique,
# plausible canonical name.  "简称" is common in source passages (e.g. 操
# for 曹操), but still remains conservative because ambiguous aliases are
# kept in review_required instead of being globally merged.
IDENTITY_RELATIONS = {"字", "号", "谥号", "曾用名", "本名", "讳", "姓名", "名", "简称"}
HAN_NAME_RE = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff]{2,6}$")
HAN_SURFACE_RE = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff]{1,6}$")


def _normalized_type(value: str) -> str:
    return normalize_surface(value or "")


def is_name_entity_type(entity_type: str) -> bool:
    """Whether a model label provides evidence for a person's proper name."""

    entity_type = _normalized_type(entity_type)
    if entity_type in {"人物", "人名", "姓名", "复姓", "人物姓名及字"}:
        return True
    if "人物姓名" in entity_type or "人名" in entity_type:
        return True
    return "人物" in entity_type and not any(
        marker in entity_type for marker in ("称谓", "别名", "别称", "字", "官职", "自称", "群体")
    )


def is_alias_entity_type(entity_type: str) -> bool:
    entity_type = _normalized_type(entity_type)
    return any(marker in entity_type for marker in ("字", "号", "谥号", "别名", "别称", "称谓"))


def canonical_name_is_plausible(value: str) -> bool:
    value = normalize_surface(value)
    return (
        bool(value)
        and bool(HAN_NAME_RE.fullmatch(value))
        and value not in GENERIC_SURFACES
        and not value.endswith("某")
        and not value.endswith(CANONICAL_TITLE_SUFFIXES)
    )


def build_identity_index(
    entities: list[dict],
    identity_rows: list[dict],
    claims: list[dict] | None = None,
) -> dict[str, Any]:
    """Build a conservative, evidence-backed identity index for projections."""

    alias_candidates: dict[str, set[str]] = defaultdict(set)
    canonical_surfaces: set[str] = set()
    for entity in entities:
        surface = normalize_surface(entity.get("surface", ""))
        if is_name_entity_type(entity.get("entity_type", "")) and canonical_name_is_plausible(surface):
            canonical_surfaces.add(surface)

    for identity in identity_rows:
        relation = normalize_surface(identity.get("relation", ""))
        canonical = normalize_surface(identity.get("canonical_surface", ""))
        alias = normalize_surface(identity.get("alias_surface", ""))
        if (
            relation in IDENTITY_RELATIONS
            and canonical
            and alias
            and canonical != alias
            and canonical_name_is_plausible(canonical)
            and HAN_SURFACE_RE.fullmatch(alias)
        ):
            alias_candidates[alias].add(canonical)
            canonical_surfaces.add(canonical)

    for claim in claims or []:
        predicate = normalize_surface(claim.get("predicate", ""))
        subject = normalize_surface(claim.get("subject", ""))
        object_ = normalize_surface(claim.get("object", ""))
        if (
            predicate in IDENTITY_PREDICATES
            and canonical_name_is_plausible(subject)
            and HAN_SURFACE_RE.fullmatch(object_ or "")
        ):
            alias_candidates[object_].add(subject)
            canonical_surfaces.add(subject)

    direct_aliases = {
        alias: next(iter(candidates))
        for alias, candidates in alias_candidates.items()
        if len(candidates) == 1
    }
    ambiguous_aliases = {
        alias for alias, candidates in alias_candidates.items() if len(candidates) > 1
    }

    # Historical texts often use surname + an already-established courtesy name:
    # 刘玄德, 曹孟德, 关云长. This is safe only when the tail alias is unique and
    # its canonical surname agrees with the prefix.
    all_surfaces = {
        normalize_surface(entity.get("surface", ""))
        for entity in entities
        if canonical_name_is_plausible(entity.get("surface", ""))
    }
    alias_map = dict(direct_aliases)
    for surface in all_surfaces:
        if surface in alias_map:
            continue
        candidates = {
            target
            for prefix_length in (1, 2)
            if len(surface) > prefix_length
            for target in [direct_aliases.get(surface[prefix_length:])]
            if target and target != surface and target.startswith(surface[:prefix_length])
        }
        if len(candidates) == 1:
            alias_map[surface] = next(iter(candidates))

    # Also collapse honorific/place-prefixed spellings when the unprefixed
    # proper name is itself present in the corpus (例如“东平刘桢”“大将军何进”).
    canonical_targets = canonical_surfaces | set(direct_aliases.values())
    for surface in all_surfaces:
        if surface in alias_map:
            continue
        suffixes = {
            target
            for target in canonical_targets
            if target != surface and surface.endswith(target) and len(surface) > len(target)
        }
        if len(suffixes) == 1:
            alias_map[surface] = next(iter(suffixes))

    return {
        "alias_map": alias_map,
        "ambiguous_aliases": ambiguous_aliases,
        "canonical_surfaces": canonical_surfaces,
    }


def derive_identity_links(store: KnowledgeStore, entities: list[dict]) -> int:
    """Persist links explicitly stated in source passages, such as 刘备字玄德."""

    by_passage: dict[str, set[str]] = defaultdict(set)
    for entity in entities:
        surface = normalize_surface(entity.get("surface", ""))
        if is_name_entity_type(entity.get("entity_type", "")) and canonical_name_is_plausible(surface):
            by_passage[entity.get("passage_id", "")].add(surface)

    markers = ("字", "本字", "号", "號", "谥号", "諡號", "讳", "諱", "名")
    marker_pattern = "|".join(re.escape(marker) for marker in markers)
    inserted = 0
    for row in store.db.execute("SELECT id, payload FROM passages"):
        passage_id = row[0]
        text = normalize_surface(json.loads(row[1]).get("text", ""))
        candidates = sorted(by_passage.get(passage_id, ()), key=len, reverse=True)
        for canonical in candidates:
            match = re.search(
                rf"{re.escape(canonical)}(?:[，,、:：\s]*)?(?:{marker_pattern})"
                rf"(?:曰|为|作|是)?([\u3400-\u4dbf\u4e00-\u9fff]{{1,6}})",
                text,
            )
            if not match:
                continue
            alias = normalize_surface(match.group(1))
            if not alias or alias == canonical or not HAN_SURFACE_RE.fullmatch(alias):
                continue
            relation = "字" if any(marker in match.group(0) for marker in ("字", "本字")) else "号"
            alias_id = "derived_alias_" + hashlib.sha256(
                f"{canonical}|{alias}|{relation}|{passage_id}".encode()
            ).hexdigest()[:24]
            payload = {
                "id": alias_id,
                "canonical_surface": canonical,
                "alias_surface": alias,
                "relation": relation,
                "passage_id": passage_id,
                "confidence": 0.97,
                "derived_by": "explicit_identity_pattern_v1",
            }
            cursor = store.db.execute(
                "INSERT OR IGNORE INTO entity_aliases(id,canonical_surface,alias_surface,relation,passage_id,confidence,payload) VALUES(?,?,?,?,?,?,?)",
                (alias_id, canonical, alias, relation, passage_id, payload["confidence"], json.dumps(payload, ensure_ascii=False)),
            )
            inserted += cursor.rowcount
    if inserted:
        store.db.commit()
    return inserted


def resolve_entities(db: str, *, derive_links: bool = True) -> dict:
    store = KnowledgeStore(db)
    entities = [json.loads(row[0]) for row in store.db.execute("SELECT payload FROM entities ORDER BY id")]
    claims = [json.loads(row[0]) for row in store.db.execute("SELECT payload FROM claims ORDER BY id")]
    if derive_links:
        derive_identity_links(store, entities)
    identity_rows = [json.loads(row[0]) for row in store.db.execute("SELECT payload FROM entity_aliases ORDER BY id")]
    identity_index = build_identity_index(entities, identity_rows, claims)
    aliases = identity_index["alias_map"]
    ambiguous_aliases = identity_index["ambiguous_aliases"]
    claim_subjects = {normalize_surface(claim.get("subject", "")) for claim in claims}
    claim_objects = {normalize_surface(claim.get("object", "")) for claim in claims}
    generic_surfaces = {normalize_surface(item) for item in GENERIC_SURFACES}
    accepted = alias = rejected = review = 0
    for entity in entities:
        original_surface = entity.get("surface", "").strip()
        surface = normalize_surface(original_surface)
        entity_type = entity.get("entity_type", "")
        is_person_type = is_name_entity_type(entity_type)
        is_alias_type = is_alias_entity_type(entity_type) or surface in aliases
        has_claim_context = surface in claim_subjects or surface in claim_objects
        reason = "模型标注为人物且有证据段落"
        status = "accepted" if is_person_type and canonical_name_is_plausible(surface) else "rejected"
        if entity.get("metadata", {}).get("source") == "external_registry":
            status = "review_required"
            reason = "外部人物索引候选，已在本地原文出现，等待模型或人工审查"
        if surface in ambiguous_aliases:
            status = "review_required"
            reason = "身份别名存在多个合格本名候选，不能全局合并"
        elif is_alias_type and (len(surface) >= 2 or surface in aliases):
            status = "alias" if surface in aliases else "review_required"
            reason = "身份关系表明确指向同一人物" if surface in aliases else (
                "人物称谓在主张或事件中有证据上下文" if has_claim_context else "人物称谓候选，等待上下文审核"
            )
        if _normalized_type(entity_type) in REJECTED_TYPES or surface in generic_surfaces:
            status, reason = "rejected", "实体类型或表面形式明确不是人物"
        if status == "accepted":
            accepted += 1
        elif status == "alias":
            alias += 1
        elif status == "rejected":
            rejected += 1
        else:
            review += 1
        entity.update(
            {
                "resolution": {
                    "status": status,
                    "reason": reason,
                    "canonical_surface": aliases.get(surface, surface if status == "accepted" else None),
                    "normalized_surface": surface,
                    "original_surface": original_surface,
                }
            }
        )
        store.db.execute("UPDATE entities SET payload=? WHERE id=?", (json.dumps(entity, ensure_ascii=False), entity["id"]))
    store.db.commit()
    store.audit("entity_resolution", "entity_resolution_completed", "entities", {"accepted": accepted, "alias": alias, "rejected": rejected})
    return {"accepted": accepted, "alias": alias, "rejected": rejected, "review_required": review, "total": len(entities)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    print(resolve_entities(args.db))
