from historybot.export import export_snapshot


def test_graph_snapshot_has_all_dimensions(tmp_path):
    db = tmp_path / "snapshot.db"
    output = tmp_path / "graph.json"
    result = export_snapshot(str(db), str(output))
    assert set(result) == {"sources", "entities", "events", "relations", "timeline", "places", "claims"}
    assert output.exists()

