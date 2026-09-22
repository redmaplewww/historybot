from __future__ import annotations

import asyncio

from historybot.domain import IngestionStatus
from historybot.ingest import (
    clean_wikitext,
    split_passages,
    split_template_arguments,
    strip_language_variants,
    strip_markup_residue,
)
from historybot.ingestion import IngestionService
from historybot.store import KnowledgeStore


def test_ingestion_preserves_versions_and_questions(tmp_path):
    store = KnowledgeStore(str(tmp_path / "ingestion.db"))
    service = IngestionService(store)
    job = asyncio.run(service.submit("建安十三年，曹操在赤壁附近与刘备交战。", name="note.txt", media_type="text/plain"))
    completed = asyncio.run(service.run(job))

    assert completed.status in {IngestionStatus.AWAITING_CLARIFICATION, IngestionStatus.ACCEPTED, IngestionStatus.REVIEW_REQUIRED}
    assert store.db.execute("SELECT COUNT(*) FROM raw_assets").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM recognition_results").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM normalized_documents").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM annotations").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM ingestion_artifacts").fetchone()[0] >= 4
    assert store.get_questions(job.id)


def test_clean_wikitext_preserves_proper_noun_template_content():
    text = "{{ProperNoun|張遼}}字{{ProperNoun|文遠}}，{{ProperNoun|并州}}人也。{{cite|ignored}}"
    assert clean_wikitext(text) == "張遼字文遠，并州人也。"


def test_clean_wikitext_preserves_era_year_annotation():
    """The chronicle's absolute-year anchors live in the YL template.

    Dropping it made every 是歲 / 二年春 statement unresolvable, because the
    era-year text those statements inherit was deleted during ingestion.
    """
    text = "{{YL|興平元年|194年}}春，太祖自徐州還。"
    assert clean_wikitext(text) == "興平元年春，太祖自徐州還。"
    assert clean_wikitext("{{YL|黃初元年}}春正月。") == "黃初元年春正月。"


def test_clean_wikitext_drops_presentational_templates_only():
    text = "{{ProperNoun|曹操}}{{quote|曰}}{{gap}}{{cite|ignored}}討{{ProperNoun|董卓}}"
    cleaned = clean_wikitext(text)
    assert "曹操" in cleaned
    assert "董卓" in cleaned
    assert "ignored" not in cleaned
    assert "{{" not in cleaned


def test_split_template_arguments_ignores_nested_pipes():
    """A wiki link inside an argument must not be sliced in half."""
    parts = split_template_arguments("*|《[[英雄記|英雄記]]》曰：一|二")
    assert parts == ["*", "《[[英雄記|英雄記]]》曰：一", "二"]


def test_clean_wikitext_keeps_era_year_alongside_dropped_commentary():
    """裴松之注 stays dropped, but the era-year anchor must survive.

    Merging 裴注 into 陳壽's base text is a provenance decision that needs its
    own marking, so ``{{*|…}}`` is deliberately not restored here. The era-year
    annotation is different: it is the chronicle's own dating and the temporal
    repair cannot work without it.
    """
    text = "{{*|《[[英雄記|英雄記]]》曰：馥字文節。}}是歲{{YL|中平六年}}也。"
    cleaned = clean_wikitext(text)
    assert "中平六年" in cleaned
    assert "[[" not in cleaned
    assert "]]" not in cleaned


def test_clean_wikitext_resolves_language_variant_markup():
    """``-{才}-`` and ``-{zh-hans:后;zh-hant:後}-`` render as plain text."""
    assert clean_wikitext("王佐-{才}-也。") == "王佐才也。"
    assert clean_wikitext("-{zh-hans:后;zh-hant:後}-主") == "后主"
    assert strip_language_variants("-{甲|乙}-丙") == "乙丙"


def test_clean_wikitext_drops_behaviour_switches():
    """``__FORCETOC__`` renders as nothing and must not reach a passage."""
    assert clean_wikitext("__FORCETOC__太祖武皇帝") == "太祖武皇帝"
    assert "__" not in clean_wikitext("先主納以為妾。__FORCETOC__")


def test_strip_markup_residue_cleans_stored_passage_text():
    """Rows ingested before the cleaner was fixed still carry raw markup."""
    assert strip_markup_residue("王佐-{才}-也。") == "王佐才也。"
    assert strip_markup_residue("先主納以為妾。__FORCETOC__") == "先主納以為妾。"
    assert strip_markup_residue("太祖至陳留。") == "太祖至陳留。"


def test_strip_markup_residue_is_idempotent():
    text = "殤-{后}-及後主。__FORCETOC__"
    once = strip_markup_residue(text)
    assert strip_markup_residue(once) == once


def test_strip_markup_residue_keeps_section_heading_text():
    """Section headings name the biography; only the delimiters are markup."""
    assert strip_markup_residue("==張遼== 張遼字文遠。") == "張遼 張遼字文遠。"
    assert strip_markup_residue("===子 譚、熙、尚===") == "子 譚、熙、尚"
    assert strip_markup_residue("其分遼典邑各百戸。 ==樂進== 樂進字文謙。") == (
        "其分遼典邑各百戸。 樂進 樂進字文謙。"
    )


def test_clean_wikitext_keeps_heading_text_without_equals():
    cleaned = clean_wikitext("==張遼==\n\n張遼字文遠，雁門馬邑人也。")
    assert cleaned == "張遼\n\n張遼字文遠，雁門馬邑人也。"
    assert "==" not in cleaned
    """裴松之注 stays dropped, but the era-year anchor must survive.

    Merging 裴注 into 陳壽's base text is a provenance decision that needs its
    own marking, so ``{{*|…}}`` is deliberately not restored here. The era-year
    annotation is different: it is the chronicle's own dating and the temporal
    repair cannot work without it.
    """
    text = "{{*|《[[英雄記|英雄記]]》曰：馥字文節。}}是歲{{YL|中平六年}}也。"
    cleaned = clean_wikitext(text)
    assert "中平六年" in cleaned
    assert "[[" not in cleaned
    assert "]]" not in cleaned


def test_split_passages_preserves_short_chronicle_entries():
    passages = split_passages(
        "初平三年，太祖軍頓丘。\n四年春，軍鄄城。\n夏，太祖還軍。",
        source_id="src_test",
    )

    assert [passage.text for passage in passages] == [
        "初平三年，太祖軍頓丘。",
        "四年春，軍鄄城。",
        "夏，太祖還軍。",
    ]
    assert [passage.ordinal for passage in passages] == [0, 1, 2]


def test_ingestion_is_idempotent_by_content_hash(tmp_path):
    store = KnowledgeStore(str(tmp_path / "ingestion.db"))
    service = IngestionService(store)
    first = asyncio.run(service.submit(b"same", name="scan.bin", media_type="image/png"))
    second = asyncio.run(service.submit(b"same", name="scan.bin", media_type="image/png"))
    assert first.id == second.id
    assert store.db.execute("SELECT COUNT(*) FROM ingestion_jobs").fetchone()[0] == 1


def test_answer_promotes_user_confirmed_dimension(tmp_path):
    store = KnowledgeStore(str(tmp_path / "ingestion.db"))
    service = IngestionService(store)
    job = asyncio.run(service.submit("A short historical note.", name="note.txt", media_type="text/plain"))
    asyncio.run(service.run(job))
    question = store.get_questions(job.id)[0]
    updated = service.answer_question(question.id, "confirmed")
    saved = store.get_questions(job.id)[0]
    assert saved.status == "answered"
    assert updated.id == job.id
    assert store.get_annotations(job.id).annotations[-1].source == "user"


def test_ingestion_storage_status_reports_write_and_index_contract(tmp_path):
    store = KnowledgeStore(str(tmp_path / "ingestion.db"))
    service = IngestionService(store)
    job = asyncio.run(service.submit("建安十三年，曹操在赤壁附近交战。", name="note.txt", media_type="text/plain"))
    asyncio.run(service.run(job))

    status = store.storage_status(job.id)

    assert status["schema_ready"] is True
    assert status["write_ready"] is True
    assert status["counts"]["sources"] == 1
    assert status["counts"]["passages"] == 1
    assert status["index_usage"]["idx_passages_source"] is True
