from __future__ import annotations

from historybot.contextual_repair import (
    ContextClaim,
    ContextEvent,
    ContextualReadingResponse,
    build_context_packet,
    chronology_scaffold,
    resolve_dual_calendar,
    validate_reading,
)


def sample_passages():
    texts = [
        "太祖武皇帝，沛国谯人也。",
        "初平元年春正月，诸州郡起兵。",
        "二年春，军东郡。",
        "三年春，太祖军顿丘。",
        "四年春，军鄄城。",
        "夏，太祖还军定陶。",
        "是岁，孙策受袁术使渡江。",
        "興平元年春，太祖自徐州還。",
    ]
    return [
        {"id": f"pv_{index}", "ordinal": index, "text": text}
        for index, text in enumerate(texts)
    ]


def test_chronology_scaffold_carries_era_through_relative_years_and_same_year():
    rows = chronology_scaffold(sample_passages()[1:7])
    by_passage = {row["passage_id"]: row for row in rows}

    assert by_passage["pv_1"]["gregorian_year"] == 190
    assert by_passage["pv_2"]["gregorian_year"] == 191
    assert by_passage["pv_3"]["gregorian_year"] == 192
    assert by_passage["pv_4"]["gregorian_year"] == 193
    assert by_passage["pv_6"]["gregorian_year"] == 193
    assert by_passage["pv_6"]["relation"] == "same_year_candidate"


def test_context_packet_contains_continuous_text_and_next_anchor():
    passages = sample_passages()
    packet = build_context_packet(passages, 6, source_title="三国志·卷01")

    assert [item["ordinal"] for item in packet["continuous_passages"]] == list(range(1, 8))
    assert packet["previous_anchor_passage_id"] == "pv_1"
    assert packet["next_anchor_passage_id"] == "pv_7"
    assert packet["context_hash"]


def test_validate_reading_checks_quote_and_dual_calendar_consistency():
    passages = sample_passages()
    packet = build_context_packet(passages, 6, source_title="三国志·卷01")
    reading = ContextualReadingResponse(
        target_passage_id="pv_6",
        reading_summary="孙策当年受袁术命渡江。",
        claims=[
            ContextClaim(
                subject="孙策",
                predicate="受命",
                object="渡江",
                statement="孙策受袁术命渡江",
                evidence_quote="孙策受袁术使渡江",
                time_expression="是岁",
            )
        ],
        events=[
            ContextEvent(
                label="孙策受命渡江",
                event_type="军事行动",
                participant_surfaces=["孙策", "袁术"],
                evidence_quote="是岁，孙策受袁术使渡江。",
                time_expression="是岁",
                relation_type="same_year",
                anchor_passage_id="pv_4",
                era_name="初平",
                era_year=4,
                gregorian_year=193,
            )
        ],
    )

    result = validate_reading(reading, packet)

    assert result["valid"] is True
    assert result["temporal_checks"][0]["computed_gregorian_year"] == 193
    assert resolve_dual_calendar("初平", 4) == 193


def test_validate_reading_rejects_wrong_year_and_invented_quote():
    passages = sample_passages()
    packet = build_context_packet(passages, 6, source_title="三国志·卷01")
    reading = ContextualReadingResponse(
        target_passage_id="pv_6",
        reading_summary="错误候选",
        events=[
            ContextEvent(
                label="错误事件",
                event_type="军事行动",
                evidence_quote="原文没有这句话",
                era_name="初平",
                era_year=4,
                gregorian_year=192,
            )
        ],
    )

    result = validate_reading(reading, packet)

    assert result["valid"] is False
    assert any(error.startswith("quote_not_in_target") for error in result["errors"])
    assert any(error.startswith("dual_calendar_mismatch") for error in result["errors"])
