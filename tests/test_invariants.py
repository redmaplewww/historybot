from historybot.corpus import FOLK_COMPARISON_CHAPTERS, SAN_GUO_ZHI_VOLUMES, complete_corpus
from historybot.domain import Claim, ClaimStatus, Confidence, Passage, Source, SourceKind
from historybot.ingest import split_passages
from historybot.policies import assess_source, preliminary_claim_confidence
from historybot.run_existing import balanced_passages
from historybot.store import KnowledgeStore


def test_passage_ids_are_stable_and_nonempty():
    first = split_passages("刘备起兵。曹操南下。", source_id="s")
    second = split_passages("刘备起兵。曹操南下。", source_id="s")
    assert [item.id for item in first] == [item.id for item in second]
    assert all(item.text_hash for item in first)


def test_claim_requires_evidence_passage():
    claim = Claim(id="c", subject="刘备", predicate="参与", object="赤壁之战", statement="刘备参与赤壁之战", passage_ids=["p"], confidence=Confidence(score=.5, source_reliability=.5, extraction_quality=.5, resolution_quality=.5, corroboration=.5, contradiction_penalty=0, rationale="test"))
    assert claim.status == ClaimStatus.EXTRACTED


def test_source_assessment_is_explicit_and_varies_by_genre():
    compilation = assess_source(SourceKind.COMPILATION)
    folk = assess_source(SourceKind.FOLK)
    assert compilation["policy_version"]
    assert compilation["baseline_score"] > folk["baseline_score"]
    assert folk["requires_corroboration"] is True


def test_preliminary_claim_confidence_uses_weakest_bound_source():
    result = preliminary_claim_confidence(
        0.98,
        [assess_source(SourceKind.COMPILATION), assess_source(SourceKind.FOLK)],
    )
    assert result["score"] == 0.35
    assert result["source_reliability"] == 0.35


def test_complete_corpus_catalog_is_explicit_and_stable():
    catalog = complete_corpus()
    assert len(SAN_GUO_ZHI_VOLUMES) == 65
    assert len(FOLK_COMPARISON_CHAPTERS) == 16
    assert len(catalog) == 81
    assert len({item.url for item in catalog}) == len(catalog)
    assert sum(item.kind.value == "compilation" for item in catalog) == 65
    assert sum(item.kind.value == "folk" for item in catalog) == 16


def test_balanced_passage_selection_is_bounded_and_source_aware(tmp_path):
    store = KnowledgeStore(str(tmp_path / "sample.db"))
    source = Source(id="s", title="source", kind=SourceKind.COMPILATION)
    store.save_source(source)
    store.save_passages(
        [
            Passage(id=f"p{i}", source_id=source.id, ordinal=i, text=f"passage {i} long enough", text_hash=str(i))
            for i in range(5)
        ]
    )
    selected = balanced_passages(store, 2)
    assert [item.id for item in selected] == ["p0", "p4"]
    assert [item.id for item in balanced_passages(store, 1)] == ["p2"]
