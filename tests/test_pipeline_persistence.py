import asyncio

from historybot.domain import Passage, Source, SourceKind
from historybot.graph import build_graph
from historybot.model_gateway import ModelResult
from historybot.policies import assess_source
from historybot.schemas import ClaimDraft, ContradictionResponse, ExtractionResponse, ReviewResponse
from historybot.store import KnowledgeStore


class SyntheticGateway:
    async def structured(self, *, schema, **kwargs):
        if schema is ExtractionResponse:
            value = ExtractionResponse(
                mentions=[{"surface": "人物甲", "entity_type": "person", "passage_id": "p1", "confidence": 0.9}],
                events=[{"event_type": "meeting", "label": "一次会见", "date_start": "0208", "date_end": "0208", "place": "地点甲", "participant_surfaces": ["人物甲"], "passage_id": "p1", "confidence": 0.8}],
                claims=[ClaimDraft(id="c1", subject="人物甲", predicate="参与", object="一次会见", statement="人物甲参与一次会见", passage_ids=["p1", "p2"], confidence=0.8)],
            )
        elif schema is ReviewResponse:
            value = ReviewResponse(claim_id="c1", evidence_sufficient=True, polarity="supports", revised_confidence=0.9, rationale="两个来源段落均明确提及")
        elif schema is ContradictionResponse:
            value = ContradictionResponse()
        else:
            raise AssertionError(schema)
        return ModelResult(value, "synthetic-contract", "synthetic", 0, 0, {})


def test_model_artifacts_persist_across_graph_dimensions():
    store = KnowledgeStore(":memory:")
    store.save_source(Source(id="s1", title="来源一", kind=SourceKind.COMPILATION, metadata={"assessment": assess_source(SourceKind.COMPILATION)}))
    store.save_source(Source(id="s2", title="来源二", kind=SourceKind.COMPILATION, metadata={"assessment": assess_source(SourceKind.COMPILATION)}))
    store.save_passages([Passage(id="p1", source_id="s1", ordinal=1, text="人物甲参与一次会见。", text_hash="1"), Passage(id="p2", source_id="s2", ordinal=1, text="人物甲参与一次会见。", text_hash="2")])
    graph = build_graph(store=store, gateway=SyntheticGateway())
    result = asyncio.run(graph.ainvoke({"task_id": "persist", "question": "关系", "passage_text": [{"id": "p1", "source_id": "s1", "text": "人物甲参与一次会见。"}, {"id": "p2", "source_id": "s2", "text": "人物甲参与一次会见。"}], "passage_ids": ["p1", "p2"]}))
    counts = store.counts()
    assert result["status"] == "complete"
    assert counts["entities"] == 1
    assert counts["events"] == counts["timeline_entries"] == counts["place_assertions"] == 1
    event = store.db.execute("SELECT payload FROM events").fetchone()[0]
    assert '"date_precision": "year"' in event
    assert '"date_sort_key": "0:0208"' in event
    assert counts["claims"] == counts["relations"] == 1
    assert counts["evidence"] == 2
    assert store.get_claims()[0].confidence.corroboration == 1
    relation = store.db.execute("SELECT claim_id, inference_method, confidence FROM relations").fetchone()
    assert relation[0] == store.get_claims()[0].id
    assert relation[1] == "explicit"
    assert relation[2] == store.get_claims()[0].confidence.score
