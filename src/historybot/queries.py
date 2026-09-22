from __future__ import annotations

import json

from .store import KnowledgeStore


def _rows(store: KnowledgeStore, sql: str, params: tuple = ()) -> list[dict]:
    return [json.loads(row[0]) for row in store.db.execute(sql, params).fetchall()]


def person_view(store: KnowledgeStore, name: str) -> dict:
    mentions = _rows(store, "SELECT payload FROM entities WHERE json_extract(payload, '$.surface') = ?", (name,))
    claims = _rows(store, "SELECT payload FROM claims WHERE json_extract(payload, '$.subject') = ? OR json_extract(payload, '$.object') = ?", (name, name))
    return {"name": name, "mentions": mentions, "claims": claims}


def timeline_view(store: KnowledgeStore, start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    clauses: list[str] = []
    params: list[str] = []
    if start_date:
        clauses.append("(end_date IS NULL OR end_date >= ?)")
        params.append(start_date)
    if end_date:
        clauses.append("(start_date IS NULL OR start_date <= ?)")
        params.append(end_date)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return _rows(store, f"SELECT payload FROM timeline_entries{where} ORDER BY COALESCE(json_extract(payload, '$.date_sort_key'), COALESCE(start_date, end_date)), id", tuple(params))


def place_view(store: KnowledgeStore, place: str) -> list[dict]:
    return _rows(store, "SELECT payload FROM place_assertions WHERE place = ? ORDER BY id", (place,))


def person_timeline(store: KnowledgeStore, name: str) -> list[dict]:
    """Return events linked to a person, ordered by normalized date, with evidence ids."""
    rows = store.db.execute("""
        SELECT e.payload FROM events e
        WHERE EXISTS (
          SELECT 1 FROM json_each(COALESCE(json_extract(e.payload, '$.participant_surfaces'), '[]'))
          WHERE value = ?
        ) OR EXISTS (
          SELECT 1 FROM claims c
          WHERE (json_extract(c.payload, '$.subject') = ? OR json_extract(c.payload, '$.object') = ?)
            AND json_extract(c.payload, '$.event_id') = e.id
        )
        ORDER BY COALESCE(json_extract(e.payload, '$.date_sort_key'), COALESCE(json_extract(e.payload, '$.date_start'), json_extract(e.payload, '$.date_end'))), e.id
    """, (name, name, name)).fetchall()
    return [json.loads(row[0]) for row in rows]


def person_relations(store: KnowledgeStore, name: str) -> list[dict]:
    """Return graph edges touching a person, retaining claim and passage provenance."""
    return _rows(store, """
        SELECT payload FROM relations
        WHERE subject = ? OR object = ?
        ORDER BY predicate, id
    """, (name, name))


def conflict_view(store: KnowledgeStore, *, name: str | None = None) -> list[dict]:
    """Return conflicts enriched with the competing claims and source passage ids."""
    conflicts = _rows(store, "SELECT payload FROM conflicts ORDER BY id")
    claims = {item["id"]: item for item in _rows(store, "SELECT payload FROM claims")}
    result = []
    for conflict in conflicts:
        linked = [claims[cid] for cid in conflict.get("claim_ids", []) if cid in claims]
        if name and not any(c.get("subject") == name or c.get("object") == name for c in linked):
            continue
        result.append({**conflict, "claims": linked, "passage_ids": sorted({pid for c in linked for pid in c.get("passage_ids", [])})})
    return result
