"""Regression tests for the V3 temporal repair helpers.

These cover the two defects that made the chronicle's dates unusable: an
ingest cleaner that deleted the era-year annotation, and an anchor parser that
misread multi-character years.
"""

from __future__ import annotations

import pytest

from historybot.ingest import (
    LEGACY_CONTENT_TEMPLATES,
    clean_wikitext,
    inserted_spans,
    raw_to_stored_offset,
    split_passages,
    split_passages_with_spans,
)
from historybot.repair import RepairService, era_absolute_anchor, era_anchor, parse_era_year


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("元", 1),
        ("一", 1),
        ("九", 9),
        ("十", 10),
        ("十一", 11),
        ("十七", 17),
        ("二十", 20),
        ("二十四", 24),
        ("三十", 30),
    ],
)
def test_parse_era_year_reads_chinese_numerals(expression, expected):
    assert parse_era_year(expression) == expected


def test_parse_era_year_rejects_non_year_expressions():
    assert parse_era_year("") is None
    assert parse_era_year("廿") is None
    assert parse_era_year("一百") is None


def test_era_anchor_does_not_absorb_preceding_words():
    """A greedy prefix read 是歲中平六年 as era 是歲中平 and lost the anchor."""
    assert era_anchor("是歲中平六年也。") == ("中平", 6)
    assert era_absolute_anchor("是歲中平六年也。") == ("中平", 6, 189)


def test_era_anchor_ignores_ordinary_year_wording():
    """關中平 and 年二十 are not era-year anchors."""
    assert era_anchor("關中平。") is None
    assert era_absolute_anchor("關中平。") is None


def test_era_anchor_resolves_absolute_years():
    assert era_absolute_anchor("初平元年春正月") == ("初平", 1, 190)
    assert era_absolute_anchor("興平元年春") == ("興平", 1, 194)
    assert era_absolute_anchor("建安元年春正月") == ("建安", 1, 196)
    assert era_absolute_anchor("建安二十四年") == ("建安", 24, 219)


def test_inserted_spans_reports_only_additions():
    old = "春，太祖自徐州還。"
    new = "興平元年春，太祖自徐州還。"
    assert inserted_spans(old, new) == [(0, "興平元年")]


def test_inserted_spans_rejects_rewrites():
    with pytest.raises(ValueError):
        inserted_spans("曹操至陳留", "曹操到達陳留")


def test_split_passages_with_spans_matches_plain_split():
    text = "太祖至陳留，散家財，合義兵，將以誅卓。\n\n是歲，孫策受袁術使渡江。"
    plain = [passage.text for passage in split_passages(text, source_id="src_test")]
    spans = [chunk for chunk, _, _ in split_passages_with_spans(text)]
    assert plain == spans


def test_raw_to_stored_offset_handles_collapsed_whitespace():
    raw = "興平元年春，太祖自徐州還。\n\n初，太祖父嵩"
    stored = "興平元年春，太祖自徐州還。 初，太祖父嵩"
    offset = raw.index("初，太祖父嵩")
    assert stored[raw_to_stored_offset(raw, offset) :].startswith("初，太祖父嵩")


def test_legacy_cleaner_drops_era_year_that_current_cleaner_keeps():
    """The old mapping is what produced the stored rows, so it must differ."""
    text = "{{YL|興平元年|194年}}春，太祖自徐州還。"
    assert clean_wikitext(text) == "興平元年春，太祖自徐州還。"
    assert clean_wikitext(text, templates=LEGACY_CONTENT_TEMPLATES) == "春，太祖自徐州還。"


def test_v3_checkpoints_are_scoped_to_repair_run(tmp_path):
    db_path = str(tmp_path / "repair.db")
    first = RepairService(db_path=db_path, run_id="run_a")
    second = RepairService(db_path=db_path, run_id="run_b")

    first._save_step(
        "ckpt_time_passage",
        step_type="temporal_resolution",
        input_ref="passage",
        output={"references": [{"claim_id": "claim_a"}]},
    )
    second._save_step(
        "ckpt_time_passage",
        step_type="temporal_resolution",
        input_ref="passage",
        output={"references": [{"claim_id": "claim_b"}]},
    )

    assert first._get_step("ckpt_time_passage")["output"]["references"][0]["claim_id"] == "claim_a"
    assert second._get_step("ckpt_time_passage")["output"]["references"][0]["claim_id"] == "claim_b"
    rows = first.conn.execute(
        "SELECT step_id,run_id FROM v3_repair_steps ORDER BY run_id"
    ).fetchall()
    assert [(row["step_id"], row["run_id"]) for row in rows] == [
        ("run_a:ckpt_time_passage", "run_a"),
        ("run_b:ckpt_time_passage", "run_b"),
    ]
