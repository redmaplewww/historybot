from historybot.evolution import create_snapshot
from historybot.store import KnowledgeStore


def test_evolution_snapshot_is_auditable(tmp_path):
    db = tmp_path / "evolution.db"
    snapshot = create_snapshot(str(db))
    store = KnowledgeStore(str(db))
    assert snapshot["source_policy_version"]
    assert store.counts()["evolution_snapshots"] == 1
    assert snapshot["claim_count"] == 0

