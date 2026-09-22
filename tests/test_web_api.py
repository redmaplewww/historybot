from __future__ import annotations

from fastapi.testclient import TestClient

from historybot.domain import Claim, Confidence, EvidenceLink, Passage, Polarity, Source
from historybot.entity_resolution import canonical_name_is_plausible, resolve_entities
from historybot.store import KnowledgeStore
from historybot.web import create_app


def test_workspace_links_graph_claims_to_passage_evidence(tmp_path) -> None:
    db_path = tmp_path / "research.db"
    store = KnowledgeStore(str(db_path))
    source = Source(id="src_test", title="Synthetic source", author="Researcher")
    passage = Passage(
        id="p_test",
        source_id=source.id,
        ordinal=0,
        text="A met B.",
        text_hash="hash",
    )
    claim = Claim(
        id="clm_test",
        subject="Person A",
        predicate="met",
        object="Person B",
        statement="Person A met Person B.",
        confidence=Confidence(
            score=0.7,
            source_reliability=0.8,
            extraction_quality=0.9,
            resolution_quality=0.9,
            corroboration=0.2,
            contradiction_penalty=0,
            rationale="Direct textual support.",
        ),
        passage_ids=[passage.id],
    )
    evidence = EvidenceLink(
        id="ev_test",
        claim_id=claim.id,
        passage_id=passage.id,
        polarity=Polarity.SUPPORTS,
        quote=passage.text,
        reasoning="The sentence states the relation.",
        confidence=0.7,
    )
    store.save_source(source)
    store.save_passages([passage])
    store.save_claim(claim)
    store.save_evidence(evidence)

    client = TestClient(create_app(db_path=db_path, static_dir=tmp_path / "missing"))
    response = client.get("/api/workspace")

    assert response.status_code == 200
    payload = response.json()
    assert payload["overview"]["evidence_coverage"] == 1
    assert payload["overview"]["data_version"].startswith("audit:")
    assert payload["graph"]["edges"][0]["claim_id"] == claim.id
    assert payload["claims"][0]["evidence"][0]["source"]["id"] == source.id
    assert payload["claims"][0]["evidence"][0]["quote"] == passage.text


def test_node_endpoint_returns_connected_evidence(tmp_path) -> None:
    db_path = tmp_path / "research.db"
    store = KnowledgeStore(str(db_path))
    source = Source(id="src_test", title="Synthetic source")
    passage = Passage(
        id="p_test", source_id=source.id, ordinal=0, text="A advised B.", text_hash="hash"
    )
    claim = Claim(
        id="clm_test",
        subject="Person A",
        predicate="advised",
        object="Person B",
        statement="Person A advised Person B.",
        confidence=Confidence(
            score=0.6,
            source_reliability=0.6,
            extraction_quality=1,
            resolution_quality=1,
            corroboration=0,
            contradiction_penalty=0,
            rationale="Direct support.",
        ),
        passage_ids=[passage.id],
    )
    store.save_source(source)
    store.save_passages([passage])
    store.save_claim(claim)
    store.save_evidence(
        EvidenceLink(
            id="ev_test",
            claim_id=claim.id,
            passage_id=passage.id,
            polarity=Polarity.SUPPORTS,
            quote=passage.text,
            reasoning="Direct support.",
            confidence=0.6,
        )
    )

    client = TestClient(create_app(db_path=db_path, static_dir=tmp_path / "missing"))
    response = client.get("/api/nodes/Person%20A")

    assert response.status_code == 200
    assert response.json()["claims"][0]["evidence"][0]["passage_id"] == passage.id


def test_identity_alias_is_one_person_node(tmp_path) -> None:
    db_path = tmp_path / "identity.db"
    store = KnowledgeStore(str(db_path))
    source = Source(id="src_identity", title="Identity source")
    passage = Passage(id="p_identity", source_id=source.id, ordinal=0, text="刘永字公寿。", text_hash="identity")
    store.save_source(source)
    store.save_passages([passage])
    store.save_entity("ent_person", {"id": "ent_person", "surface": "刘永", "entity_type": "人物姓名", "passage_id": passage.id, "confidence": 0.99, "resolution": {"status": "accepted"}})
    store.save_entity("ent_alias", {"id": "ent_alias", "surface": "公寿", "entity_type": "人物称谓", "passage_id": passage.id, "confidence": 0.99, "resolution": {"status": "alias", "canonical_surface": "刘永"}})
    store.save_entity_alias("alias_identity", {"id": "alias_identity", "canonical_surface": "刘永", "alias_surface": "公寿", "relation": "字", "passage_id": passage.id, "confidence": 1.0})

    client = TestClient(create_app(db_path=db_path, static_dir=tmp_path / "missing"))
    graph = client.get("/api/workspace").json()["graph"]
    people = [node for node in graph["nodes"] if node["kind"] == "person"]
    assert [node["label"] for node in people] == ["刘永"]
    assert people[0]["aliases"] == ["公寿"]


def test_short_alias_is_one_person_node_and_event_target(tmp_path) -> None:
    db_path = tmp_path / "short_alias.db"
    store = KnowledgeStore(str(db_path))
    source = Source(id="src_short_alias", title="Short alias source")
    passage = Passage(id="p_short_alias", source_id=source.id, ordinal=0, text="曹操，简称操。操与刘备交战。", text_hash="short-alias")
    store.save_source(source)
    store.save_passages([passage])
    store.save_entity("ent_cao", {"id": "ent_cao", "surface": "曹操", "entity_type": "人物", "passage_id": passage.id, "confidence": 0.99, "resolution": {"status": "accepted"}})
    store.save_entity("ent_short", {"id": "ent_short", "surface": "操", "entity_type": "人物简称", "passage_id": passage.id, "confidence": 0.99, "resolution": {"status": "alias"}})
    store.save_entity_alias("alias_short", {"id": "alias_short", "canonical_surface": "曹操", "alias_surface": "操", "relation": "简称", "passage_id": passage.id, "confidence": 0.99})
    store.save_event("evt_short", {"id": "evt_short", "event_type": "交战", "participant_surfaces": ["操", "刘备"], "passage_id": passage.id, "confidence": 0.99})

    client = TestClient(create_app(db_path=db_path, static_dir=tmp_path / "missing"))
    graph = client.get("/api/workspace").json()["graph"]
    people = [node for node in graph["nodes"] if node["kind"] == "person"]
    assert [node["label"] for node in people] == ["曹操"]
    assert "操" in next(node for node in people if node["label"] == "曹操")["aliases"]
    assert not any(node["label"] == "操" for node in graph["nodes"])
    assert any(edge["source"] == "曹操" for edge in graph["edges"] if edge["event_id"] == "evt_short")


def test_traditional_and_simplified_identity_surfaces_merge(tmp_path) -> None:
    db_path = tmp_path / "variants.db"
    store = KnowledgeStore(str(db_path))
    source = Source(id="src_variants", title="Variant source")
    passage = Passage(id="p_variants", source_id=source.id, ordinal=0, text="劉備字玄德。", text_hash="variants")
    store.save_source(source)
    store.save_passages([passage])
    store.save_entity("ent_traditional", {"id": "ent_traditional", "surface": "劉備", "entity_type": "人物姓名", "passage_id": passage.id, "confidence": 0.99, "resolution": {"status": "accepted"}})
    store.save_entity("ent_simplified", {"id": "ent_simplified", "surface": "刘备", "entity_type": "人物姓名", "passage_id": passage.id, "confidence": 0.99, "resolution": {"status": "accepted"}})
    store.save_entity("ent_alias", {"id": "ent_alias", "surface": "玄德", "entity_type": "人物称谓", "passage_id": passage.id, "confidence": 0.99, "resolution": {"status": "alias", "canonical_surface": "劉備"}})
    store.save_entity_alias("alias_variant", {"id": "alias_variant", "canonical_surface": "劉備", "alias_surface": "玄德", "relation": "字", "passage_id": passage.id, "confidence": 1.0})

    client = TestClient(create_app(db_path=db_path, static_dir=tmp_path / "missing"))
    people = [node for node in client.get("/api/workspace").json()["graph"]["nodes"] if node["kind"] == "person"]
    assert [node["label"] for node in people] == ["刘备"]
    assert set(people[0]["aliases"]) == {"劉備", "玄德"}
    assert client.get("/api/nodes/%E5%8A%89%E5%82%99").json()["node"]["label"] == "刘备"


def test_compound_courtesy_name_merges_into_canonical_person(tmp_path) -> None:
    db_path = tmp_path / "compound_identity.db"
    store = KnowledgeStore(str(db_path))
    source = Source(id="src_compound", title="Compound identity source")
    passage = Passage(id="p_compound", source_id=source.id, ordinal=0, text="刘玄德与关云长会见。", text_hash="compound")
    store.save_source(source)
    store.save_passages([passage])
    store.save_entity("ent_bei", {"id": "ent_bei", "surface": "刘备", "entity_type": "人物", "passage_id": passage.id, "confidence": 0.99, "resolution": {"status": "accepted"}})
    store.save_entity("ent_xuande", {"id": "ent_xuande", "surface": "玄德", "entity_type": "人物字", "passage_id": passage.id, "confidence": 0.99, "resolution": {"status": "alias"}})
    store.save_entity("ent_compound", {"id": "ent_compound", "surface": "刘玄德", "entity_type": "人物", "passage_id": passage.id, "confidence": 0.99, "resolution": {"status": "accepted"}})
    store.save_entity_alias("alias_xuande", {"id": "alias_xuande", "canonical_surface": "刘备", "alias_surface": "玄德", "relation": "字", "passage_id": passage.id, "confidence": 1.0})

    client = TestClient(create_app(db_path=db_path, static_dir=tmp_path / "missing"))
    people = [node for node in client.get("/api/workspace").json()["graph"]["nodes"] if node["kind"] == "person"]
    assert [node["label"] for node in people] == ["刘备"]
    assert set(people[0]["aliases"]) == {"玄德", "刘玄德"}
    assert client.get("/api/nodes/玄德").json()["node"]["label"] == "刘备"
    queried = client.get("/api/graph", params={"q": "玄德"}).json()
    assert any(node["label"] == "刘备" for node in queried["nodes"])


def test_explicit_text_identity_is_indexed_without_model_link(tmp_path) -> None:
    db_path = tmp_path / "derived_identity.db"
    store = KnowledgeStore(str(db_path))
    source = Source(id="src_derived", title="Derived identity source")
    passage = Passage(id="p_derived", source_id=source.id, ordinal=0, text="刘永，字公寿。", text_hash="derived")
    store.save_source(source)
    store.save_passages([passage])
    store.save_entity("ent_person", {"id": "ent_person", "surface": "刘永", "entity_type": "人物", "passage_id": passage.id, "confidence": 0.99, "resolution": {"status": "accepted"}})
    store.save_entity("ent_alias", {"id": "ent_alias", "surface": "公寿", "entity_type": "人物字", "passage_id": passage.id, "confidence": 0.99, "resolution": {"status": "review_required"}})
    resolve_entities(str(db_path))

    client = TestClient(create_app(db_path=db_path, static_dir=tmp_path / "missing"))
    graph = client.get("/api/workspace").json()["graph"]
    people = [node for node in graph["nodes"] if node["kind"] == "person"]
    assert [node["label"] for node in people] == ["刘永"]
    assert people[0]["aliases"] == ["公寿"]


def test_ambiguous_alias_is_not_globally_merged(tmp_path) -> None:
    db_path = tmp_path / "ambiguous.db"
    store = KnowledgeStore(str(db_path))
    source = Source(id="src_ambiguous", title="Ambiguous source")
    passage = Passage(id="p_ambiguous", source_id=source.id, ordinal=0, text="子敬。", text_hash="ambiguous")
    store.save_source(source)
    store.save_passages([passage])
    for entity_id, surface in (("ent_a", "鲁肃"), ("ent_b", "张昭"), ("ent_alias", "子敬")):
        store.save_entity(entity_id, {"id": entity_id, "surface": surface, "entity_type": "人物姓名", "passage_id": passage.id, "confidence": 0.99, "resolution": {"status": "accepted"}})
    store.save_entity_alias("alias_a", {"id": "alias_a", "canonical_surface": "鲁肃", "alias_surface": "子敬", "relation": "字", "passage_id": passage.id, "confidence": 1.0})
    store.save_entity_alias("alias_b", {"id": "alias_b", "canonical_surface": "张昭", "alias_surface": "子敬", "relation": "字", "passage_id": passage.id, "confidence": 1.0})

    client = TestClient(create_app(db_path=db_path, static_dir=tmp_path / "missing"))
    labels = {node["label"] for node in client.get("/api/workspace").json()["graph"]["nodes"] if node["kind"] == "person"}
    assert labels == {"子敬", "张昭", "鲁肃"}


def test_title_cannot_be_canonical_person_name() -> None:
    assert canonical_name_is_plausible("刘永")
    assert not canonical_name_is_plausible("丁")
    assert not canonical_name_is_plausible("山阳公")
    assert not canonical_name_is_plausible("皇后")


def test_people_ledger_changes_workspace_version(tmp_path) -> None:
    db_path = tmp_path / "ledger.db"
    store = KnowledgeStore(str(db_path))
    source = Source(id="src_ledger", title="Ledger source")
    passage = Passage(id="p_ledger", source_id=source.id, ordinal=0, text="刘永。", text_hash="ledger")
    store.save_source(source)
    store.save_passages([passage])
    store.db.execute(
        "CREATE TABLE people_extraction_batches (id TEXT PRIMARY KEY, status TEXT NOT NULL, passage_ids TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, error TEXT, payload TEXT NOT NULL)"
    )
    store.db.execute(
        "INSERT INTO people_extraction_batches(id,status,passage_ids,payload) VALUES(?,?,?,?)",
        ("batch_ledger", "complete", '["p_ledger"]', "{}"),
    )
    store.db.commit()

    client = TestClient(create_app(db_path=db_path, static_dir=tmp_path / "missing"))
    payload = client.get("/api/workspace").json()
    assert payload["overview"]["extraction"]["completed_passages"] == 1
    assert payload["overview"]["extraction"]["coverage"] == 1
    assert payload["overview"]["extraction"]["people"]["completed_passages"] == 1
    assert payload["overview"]["extraction"]["evidence"]["completed_passages"] == 0
    assert ":people:" in payload["overview"]["data_version"]


def test_ingestion_api_submits_and_answers_question(tmp_path) -> None:
    db_path = tmp_path / "ingestion.db"
    client = TestClient(create_app(db_path=db_path, static_dir=tmp_path / "missing"))
    response = client.post(
        "/api/ingestion",
        json={"source": "A historical note.", "name": "note.txt", "media_type": "text/plain"},
    )
    assert response.status_code == 200
    job = response.json()
    status = client.get(f"/api/ingestion/{job['id']}")
    assert status.status_code == 200
    question = status.json()["questions"][0]
    answered = client.post(
        f"/api/ingestion/{job['id']}/questions/{question['id']}",
        json={"answer": "confirmed"},
    )
    assert answered.status_code == 200
    assert any(item["status"] == "answered" for item in answered.json()["questions"])


def test_ingestion_api_exposes_database_storage_status(tmp_path) -> None:
    db_path = tmp_path / "ingestion.db"
    client = TestClient(create_app(db_path=db_path, static_dir=tmp_path / "missing"))
    response = client.post(
        "/api/ingestion",
        json={"source": "建安十三年，曹操在赤壁附近交战。", "name": "note.txt", "media_type": "text/plain"},
    )
    assert response.status_code == 200
    job_id = response.json()["id"]

    status = client.get(f"/api/ingestion/{job_id}/storage")
    assert status.status_code == 200
    payload = status.json()
    assert payload["write_ready"] is True
    assert payload["schema_ready"] is True
    assert payload["index_usage"]["idx_passages_source"] is True
    assert client.get(f"/api/ingestion/{job_id}").json()["storage"]["job_id"] == job_id


def test_workspace_summary_can_skip_heavy_dimensions(tmp_path) -> None:
    db_path = tmp_path / "summary.db"
    store = KnowledgeStore(str(db_path))
    source = Source(id="src_summary", title="Summary source")
    passage = Passage(id="p_summary", source_id=source.id, ordinal=0, text="A note.", text_hash="summary")
    store.save_source(source)
    store.save_passages([passage])

    client = TestClient(create_app(db_path=db_path, static_dir=tmp_path / "missing"))
    response = client.get("/api/workspace", params={"include_graph": "false", "include_dimensions": "false"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["graph"] == {"nodes": [], "edges": []}
    assert payload["timeline"] == []
    assert payload["overview"]["counts"]["passages"] == 1


def test_graph_limit_and_dimensions_endpoint(tmp_path) -> None:
    db_path = tmp_path / "dimensions.db"
    store = KnowledgeStore(str(db_path))
    source = Source(id="src_dimensions", title="Dimensions source")
    passage = Passage(id="p_dimensions", source_id=source.id, ordinal=0, text="A note.", text_hash="dimensions")
    store.save_source(source)
    store.save_passages([passage])

    client = TestClient(create_app(db_path=db_path, static_dir=tmp_path / "missing"))
    graph = client.get("/api/graph", params={"limit": 1})
    dimensions = client.get("/api/dimensions")

    assert graph.status_code == 200
    assert len(graph.json()["nodes"]) <= 1
    assert dimensions.status_code == 200
    assert set(dimensions.json()) == {"timeline", "places", "claims", "sources"}
