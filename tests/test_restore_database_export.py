from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

from historybot.ingestion import IngestionService
from historybot.store import KnowledgeStore
from scripts.restore_database_export import qualify


def test_qualified_restore_reports_counts_references_and_indexes(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "qualified.db"
    store = KnowledgeStore(str(source))
    service = IngestionService(store)
    job = asyncio.run(service.submit("建安十三年。", name="note.txt", media_type="text/plain"))
    asyncio.run(service.run(job))
    store.db.close()

    # The restore validator is independent of the archive reader; this keeps
    # the test small while exercising the same qualification contract.
    with zipfile.ZipFile(tmp_path / "bundle.zip", "w") as bundle:
        bundle.writestr("bundle/manifest.json", json.dumps({"tables": {}, "files": {}}))
        bundle.write(source, "bundle/historybot.sqlite3")
    output.write_bytes(source.read_bytes())
    result = qualify(output, {"tables": {}, "created_at_utc": "test"}, tmp_path / "bundle.zip")

    assert result["qualified"] is True
    assert result["integrity_check"] == "ok"
    assert result["orphans"] == {name: 0 for name in result["orphans"]}
    assert result["schema_ready"] is True
