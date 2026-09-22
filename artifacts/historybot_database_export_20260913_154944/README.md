# HistoryBot database export

This bundle is a consistent snapshot of the HistoryBot SQLite database.

## Restore

- Direct restore: use `historybot.sqlite3` as the application database.
- SQL rebuild: run `sqlite3 restored.sqlite3 < database.sql`.
- Data migration: import the UTF-8 JSON Lines files under `tables/` using the
  column definitions recorded in `manifest.json` and `schema.sql`.

`manifest.json` contains row counts and SHA-256 checksums for verification.
Binary SQLite values in JSONL are represented as `{"$binary_hex": "..."}`.
