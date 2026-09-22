from __future__ import annotations

import argparse
import json
import sqlite3

from historybot.entity_resolution import build_identity_index, is_name_entity_type
from historybot.text_normalization import normalize_surface
from historybot.web import ResearchView


def audit(db: str) -> dict[str, object]:
    connection = sqlite3.connect(db)
    entities = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM entities")]
    aliases = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM entity_aliases")]
    completed = {
        row[0]
        for row in connection.execute(
            "SELECT passage_id FROM people_extraction_passages WHERE status='complete'"
        )
    }
    total_passages = connection.execute("SELECT COUNT(*) FROM passages").fetchone()[0]
    candidates = {
        normalize_surface(entity["surface"])
        for entity in entities
        if entity.get("resolution", {}).get("status") != "rejected"
        and is_name_entity_type(entity.get("entity_type", ""))
    }
    alias_candidates: dict[str, set[str]] = {}
    for item in aliases:
        relation = normalize_surface(item.get("relation", ""))
        if relation not in {"字", "号", "谥号", "曾用名", "本名", "讳", "姓名", "名"}:
            continue
        alias = normalize_surface(item.get("alias_surface", ""))
        alias_candidates.setdefault(alias, set()).add(normalize_surface(item.get("canonical_surface", "")))
    identity_index = build_identity_index(entities, aliases, [])
    graph = ResearchView(db).workspace()["graph"]
    people = [node for node in graph["nodes"] if node["kind"] == "person"]
    person_labels = {node["label"] for node in people}
    unique_alias_map = {
        alias: next(iter(values))
        for alias, values in alias_candidates.items()
        if len(values) == 1
    }
    unmerged_unique_aliases = sum(
        alias in person_labels and canonical in person_labels and alias != canonical
        for alias, canonical in unique_alias_map.items()
    )
    unresolved_compound_surfaces = sum(
        surface in person_labels and surface not in identity_index["alias_map"]
        for surface in candidates
        if any(
            target.startswith(surface[:prefix_length])
            for prefix_length in (1, 2)
            if len(surface) > prefix_length
            for target in [identity_index["alias_map"].get(surface[prefix_length:])]
            if target
        )
    )
    return {
        "passages": total_passages,
        "completed_people_passages": len(completed),
        "people_coverage": len(completed) / total_passages if total_passages else 0,
        "accepted_unique_name_surfaces": len(candidates),
        "person_nodes": len(people),
        "normalized_duplicate_person_labels": len(people) - len({normalize_surface(node["label"]) for node in people}),
        "self_loop_edges": sum(edge["source"] == edge["target"] for edge in graph["edges"]),
        "ambiguous_identity_aliases": sum(len(values) > 1 for values in alias_candidates.values()),
        "unmerged_unique_identity_aliases": unmerged_unique_aliases,
        "unresolved_compound_surfaces": unresolved_compound_surfaces,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit normalized person graph invariants")
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.db), ensure_ascii=False, indent=2))
