import json

from historybot.report import write_report
from historybot.store import KnowledgeStore


def test_report_preserves_persisted_claims_when_contradiction_times_out(tmp_path):
    db = tmp_path / "report.db"
    output = tmp_path / "report.json"
    store = KnowledgeStore(str(db))
    store.audit("task", "pipeline_failed", "task", {"error_type": "ReadTimeout", "message": ""})
    report = write_report(str(db), str(output))
    assert report["status"] == "failed"  # no claims means no false partial-success signal
    assert json.loads(output.read_text(encoding="utf-8"))["counts"]["claims"] == 0

