from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from historybot.entity_resolution import canonical_name_is_plausible, resolve_entities
from historybot.store import KnowledgeStore
from historybot.text_normalization import normalize_surface

NAME_PATTERN = re.compile(r"\bname\s*:\s*['\"]([^'\"]+)")


def load_registry_names(root: Path) -> list[tuple[str, str]]:
    names: dict[str, str] = {}
    for path in root.rglob("*.js"):
        text = path.read_text(encoding="utf-8")
        match = NAME_PATTERN.search(text)
        if match:
            name = normalize_surface(match.group(1).strip())
            if canonical_name_is_plausible(name):
                names.setdefault(name, str(path.relative_to(root)))
    return sorted(names.items(), key=lambda item: (-len(item[0]), item[0]))


def import_candidates(db_path: str, registry_root: Path, registry_commit: str) -> dict[str, int]:
    store = KnowledgeStore(db_path)
    passages = [
        (row[0], normalize_surface(json.loads(row[1]).get("text", "")))
        for row in store.db.execute("SELECT id, payload FROM passages ORDER BY id")
    ]
    existing = {
        normalize_surface(json.loads(row[0]).get("surface", ""))
        for row in store.db.execute("SELECT payload FROM entities")
    }
    names = load_registry_names(registry_root)
    inserted = 0
    matched = 0
    for name, registry_file in names:
        if name in existing:
            continue
        passage_id = next((pid for pid, text in passages if name in text), None)
        if not passage_id:
            continue
        matched += 1
        entity_id = "ent_registry_" + hashlib.sha256(f"{name}|{passage_id}".encode()).hexdigest()[:20]
        payload = {
            "id": entity_id,
            "surface": name,
            "entity_type": "人物候选",
            "passage_id": passage_id,
            "canonical_id": None,
            "confidence": 0.55,
            "metadata": {
                "source": "external_registry",
                "registry": "Woomessi/Storiap",
                "registry_file": registry_file,
                "registry_commit": registry_commit,
                "evidence_rule": "exact_normalized_surface_match",
            },
            "resolution": {
                "status": "review_required",
                "reason": "外部人物索引候选，已在本地原文出现，等待模型或人工审查",
            },
        }
        store.save_entity(entity_id, payload, "external_registry_import")
        existing.add(name)
        inserted += 1
    store.db.close()
    resolve_entities(db_path, derive_links=True)
    return {"registry_names": len(names), "matched_names": matched, "inserted_candidates": inserted}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import source-grounded external person candidates")
    parser.add_argument("--db", required=True)
    parser.add_argument("--registry-root", required=True, type=Path)
    parser.add_argument("--registry-commit", required=True)
    args = parser.parse_args()
    print(json.dumps(import_candidates(args.db, args.registry_root, args.registry_commit), ensure_ascii=False, indent=2))
