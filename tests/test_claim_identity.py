import asyncio

from historybot.domain import Passage, Source, SourceKind
from historybot.graph import build_graph
from historybot.model_gateway import ModelResult
from historybot.policies import assess_source
from historybot.schemas import ClaimDraft, ContradictionResponse, ExtractionResponse, ReviewResponse
from historybot.store import KnowledgeStore


class ReusingIdGateway:
    async def structured(self, *, schema, **kwargs):
        if schema is ExtractionResponse:
            value = ExtractionResponse(claims=[ClaimDraft(id="c1", subject="甲", predicate="行动", object="乙", statement="甲行动于乙", passage_ids=["p1"])])
        elif schema is ReviewResponse:
            value = ReviewResponse(claim_id="c1", evidence_sufficient=True, polarity="supports", revised_confidence=0.8, rationale="文本支持")
        else:
            value = ContradictionResponse()
        return ModelResult(value, "synthetic", "synthetic", 0, 0, {})


def test_model_local_ids_do_not_overwrite_claims():
    store = KnowledgeStore(":memory:")
    store.save_source(Source(id="s", title="来源", kind=SourceKind.COMPILATION, metadata={"assessment": assess_source(SourceKind.COMPILATION)}))
    store.save_passages([Passage(id="p1", source_id="s", ordinal=1, text="甲行动于乙。", text_hash="p1")])
    graph = build_graph(store=store, gateway=ReusingIdGateway())
    state = {"task_id": "identity", "question": "问题", "passage_text": [{"id": "p1", "source_id": "s", "text": "甲行动于乙。"}], "passage_ids": ["p1"]}
    asyncio.run(graph.ainvoke(state))
    asyncio.run(graph.ainvoke(state))
    assert store.counts()["claims"] == 1
    assert store.get_claims()[0].metadata["model_claim_id"] == "c1"

