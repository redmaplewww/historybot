from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export a consistent SQLite snapshot and portable rebuild files."
    )
    parser.add_argument("database", type=Path, help="SQLite database to export")
    parser.add_argument("output", type=Path, help="Directory to create")
    parser.add_argument(
        "--zip",
        action="store_true",
        dest="make_zip",
        help="Also create a ZIP archive next to the output directory",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"$binary_hex": value.hex()}
    return value


def write_jsonl(
    connection: sqlite3.Connection, table: str, columns: list[str], path: Path
) -> int:
    quoted_table = table.replace('"', '""')
    cursor = connection.execute(f'SELECT * FROM "{quoted_table}"')
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for values in cursor:
            row = {name: json_value(value) for name, value in zip(columns, values)}
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
            count += 1
    return count


def write_lines(path: Path, lines: Iterable[str]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for line in lines:
            handle.write(line)
            handle.write("\n")


def main() -> int:
    args = parse_args()
    source_path = args.database.resolve()
    output_path = args.output.resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"Database does not exist: {source_path}")
    if output_path.exists():
        raise FileExistsError(f"Output already exists: {output_path}")

    output_path.mkdir(parents=True)
    tables_path = output_path / "tables"
    tables_path.mkdir()
    snapshot_path = output_path / "historybot.sqlite3"

    source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
    snapshot = sqlite3.connect(snapshot_path)
    try:
        source.backup(snapshot)
    finally:
        snapshot.close()
        source.close()

    connection = sqlite3.connect(snapshot_path)
    try:
        integrity_rows = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        if integrity_rows != ["ok"]:
            raise RuntimeError(f"Snapshot integrity check failed: {integrity_rows}")

        objects = connection.execute(
            """
            SELECT type, name, sql
            FROM sqlite_master
            WHERE type IN ('table', 'view', 'index', 'trigger')
              AND name NOT LIKE 'sqlite_%'
            ORDER BY CASE type
                WHEN 'table' THEN 1 WHEN 'view' THEN 2
                WHEN 'index' THEN 3 ELSE 4 END, name
            """
        ).fetchall()
        table_names = [name for object_type, name, _ in objects if object_type == "table"]

        schema_path = output_path / "schema.sql"
        schema_lines = [
            f"{sql.rstrip(';')};"
            for _, _, sql in objects
            if sql is not None
        ]
        write_lines(schema_path, schema_lines)

        dump_path = output_path / "database.sql"
        write_lines(dump_path, connection.iterdump())

        table_manifest: dict[str, dict[str, Any]] = {}
        for table in table_names:
            quoted_table = table.replace('"', '""')
            columns = [
                row[1]
                for row in connection.execute(f'PRAGMA table_info("{quoted_table}")')
            ]
            jsonl_path = tables_path / f"{table}.jsonl"
            row_count = write_jsonl(connection, table, columns, jsonl_path)
            table_manifest[table] = {
                "rows": row_count,
                "columns": columns,
                "file": str(jsonl_path.relative_to(output_path)).replace("\\", "/"),
            }

        readme_path = output_path / "README.md"
        readme_path.write_text(
            """# HistoryBot database export

This bundle is a consistent snapshot of the HistoryBot SQLite database.

## Restore

- Direct restore: use `historybot.sqlite3` as the application database.
- SQL rebuild: run `sqlite3 restored.sqlite3 < database.sql`.
- Data migration: import the UTF-8 JSON Lines files under `tables/` using the
  column definitions recorded in `manifest.json` and `schema.sql`.

`manifest.json` contains row counts and SHA-256 checksums for verification.
Binary SQLite values in JSONL are represented as `{\"$binary_hex\": \"...\"}`.
""",
            encoding="utf-8",
            newline="\n",
        )
    finally:
        connection.close()

    exported_files = sorted(
        path for path in output_path.rglob("*") if path.is_file()
    )
    manifest = {
        "format_version": 1,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "source_database": str(source_path),
        "sqlite_version": sqlite3.sqlite_version,
        "integrity_check": "ok",
        "tables": table_manifest,
        "files": {
            str(path.relative_to(output_path)).replace("\\", "/"): {
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in exported_files
        },
    }
    manifest_path = output_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    if args.make_zip:
        archive_path = Path(
            shutil.make_archive(str(output_path), "zip", output_path.parent, output_path.name)
        )
        print(f"archive={archive_path}")
        print(f"archive_sha256={sha256_file(archive_path)}")

    print(f"export={output_path}")
    print(f"tables={len(table_manifest)}")
    print(f"rows={sum(item['rows'] for item in table_manifest.values())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
