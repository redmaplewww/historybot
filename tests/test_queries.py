from historybot.queries import person_view, place_view, timeline_view
from historybot.store import KnowledgeStore


def test_dimension_queries_are_empty_but_well_formed_before_extraction():
    store = KnowledgeStore(":memory:")
    assert person_view(store, "任意人物") == {"name": "任意人物", "mentions": [], "claims": []}
    assert timeline_view(store) == []
    assert place_view(store, "任意地点") == []

