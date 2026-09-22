"""Restore and qualify a HistoryBot database export bundle.

The command never modifies the archive or the current application database. It
creates a new SQLite file, applies the current ``KnowledgeStore`` migrations,
then validates row counts, provenance references, integrity and indexes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from historybot.store import KnowledgeStore

INDEXES = (
    "idx_passages_source",
    "idx_entities_surface",
    "idx_entities_type",
    "idx_events_dates",
    "idx_events_place",
    "idx_relations_subject_predicate",
    "idx_relations_object",
    "idx_timeline_dates",
    "idx_places_place",
    "idx_claims_status",
    "idx_claims_subject_predicate",
    "idx_claims_object",
    "idx_evidence_claim",
    "idx_evidence_passage",
    "idx_relations_claim",
    "idx_relations_valid_time",
    "idx_entity_aliases_alias",
)


def _sha256_stream(handle) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _bundle_root(names: list[str]) -> str:
    roots = {name.split("/", 1)[0] for name in names if "/" in name}
    if len(roots) != 1:
        raise ValueError("export bundle must contain one top-level directory")
    return next(iter(roots))


def _load_bundle(archive: Path, destination: Path) -> dict[str, Any]:
    with zipfile.ZipFile(archive) as bundle:
        root = _bundle_root(bundle.namelist())
        manifest_path = f"{root}/manifest.json"
        manifest = json.loads(bundle.read(manifest_path).decode("utf-8"))
        files = manifest.get("files", {})
        for relative, expected in files.items():
            member = f"{root}/{relative}"
            with bundle.open(member) as handle:
                actual = _sha256_stream(handle)
            if actual != expected.get("sha256"):
                raise ValueError(f"checksum mismatch for {relative}: {actual} != {expected.get('sha256')}")
        sqlite_member = f"{root}/historybot.sqlite3"
        with bundle.open(sqlite_member) as handle, destination.open("wb") as output:
            shutil.copyfileobj(handle, output, length=1024 * 1024)
    return manifest


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}


def _orphan_counts(connection: sqlite3.Connection) -> dict[str, int]:
    checks = {
        "passages_without_source": "SELECT COUNT(*) FROM passages p LEFT JOIN sources s ON s.id=p.source_id WHERE s.id IS NULL",
        "aliases_without_passage": "SELECT COUNT(*) FROM entity_aliases a LEFT JOIN passages p ON p.id=a.passage_id WHERE p.id IS NULL",
        "evidence_without_claim": "SELECT COUNT(*) FROM evidence e LEFT JOIN claims c ON c.id=e.claim_id WHERE c.id IS NULL",
        "evidence_without_passage": "SELECT COUNT(*) FROM evidence e LEFT JOIN passages p ON p.id=e.passage_id WHERE p.id IS NULL",
        "timeline_without_event": "SELECT COUNT(*) FROM timeline_entries t LEFT JOIN events e ON e.id=t.event_id WHERE e.id IS NULL",
        "places_without_event": "SELECT COUNT(*) FROM place_assertions p LEFT JOIN events e ON e.id=p.event_id WHERE e.id IS NULL",
        "relations_without_claim": "SELECT COUNT(*) FROM relations r LEFT JOIN claims c ON c.id=r.claim_id WHERE r.claim_id IS NOT NULL AND c.id IS NULL",
        "extraction_without_passage": "SELECT COUNT(*) FROM extraction_passages x LEFT JOIN passages p ON p.id=x.passage_id WHERE p.id IS NULL",
        "people_extraction_without_passage": "SELECT COUNT(*) FROM people_extraction_passages x LEFT JOIN passages p ON p.id=x.passage_id WHERE p.id IS NULL",
    }
    return {name: connection.execute(sql).fetchone()[0] for name, sql in checks.items()}


def _json_reference_orphans(connection: sqlite3.Connection) -> dict[str, int]:
    passage_ids = {row[0] for row in connection.execute("SELECT id FROM passages")}
    orphan_claim_passages = 0
    for (payload,) in connection.execute("SELECT payload FROM claims"):
        values = json.loads(payload).get("passage_ids", [])
        orphan_claim_passages += sum(item not in passage_ids for item in values)
    return {"claim_passage_ids_missing": orphan_claim_passages}


def qualify(database: Path, manifest: dict[str, Any], archive: Path) -> dict[str, Any]:
    store = KnowledgeStore(str(database))
    try:
        connection = store.db
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        connection.execute("REINDEX")
        connection.commit()
        counts = _table_counts(connection)
        orphans = {**_orphan_counts(connection), **_json_reference_orphans(connection)}
        storage = store.storage_status()
        expected = {name: item["rows"] for name, item in manifest.get("tables", {}).items()}
        count_match = all(counts.get(name) == value for name, value in expected.items())
        qualified = integrity == "ok" and count_match and not any(orphans.values()) and storage["schema_ready"]
        return {
            "qualified": qualified,
            "archive": str(archive.resolve()),
            "database": str(database.resolve()),
            "integrity_check": integrity,
            "count_match": count_match,
            "counts": counts,
            "expected_counts": expected,
            "orphans": orphans,
            "indexes": storage["indexes"],
            "schema_ready": storage["schema_ready"],
            "source_snapshot_created_at": manifest.get("created_at_utc"),
        }
    finally:
        store.db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path, default=Path("data/historybot_qualified_20260913.db"))
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    archive = args.archive.resolve()
    output = args.output.resolve()
    if not archive.is_file():
        raise FileNotFoundError(archive)
    if output.exists() and not args.force:
        raise FileExistsError(f"output exists; use --force to replace: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    with tempfile.TemporaryDirectory(prefix="historybot_restore_") as temporary:
        snapshot = Path(temporary) / "snapshot.sqlite3"
        manifest = _load_bundle(archive, snapshot)
        shutil.copyfile(snapshot, output)
    result = qualify(output, manifest, archive)
    report = args.report or output.with_suffix(".qualification.json")
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["qualified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
