from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from historybot.contextual_repair import (
    ContextClaim,
    ContextEvent,
    ContextMention,
    ContextualCanaryService,
    ContextualReadingResponse,
    ContextualVerificationResponse,
    build_context_packet,
    deterministic_id,
    validate_reading,
)
from historybot.ingest import clean_wikitext, split_passages

TARGETS = (
    {"name": "初平四年显式省略年号", "prefix": "四年春，軍鄄城。", "expected_year": 193},
    {"name": "是岁承接初平四年", "prefix": "是歲，孫策受袁術使渡江", "expected_year": 193},
)


def fetch_wikitext(url: str) -> str:
    with httpx.Client(timeout=90, headers={"User-Agent": "historybot-research/0.6"}) as client:
        response = client.get(url)
        response.raise_for_status()
        raw = response.json().get("parse", {}).get("wikitext", {}).get("*")
        if not isinstance(raw, str):
            raise TypeError("Wikisource response has no parse.wikitext.*")
        return raw


def load_source(main_db: str) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    conn = sqlite3.connect(main_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT id,payload FROM sources
        WHERE json_extract(payload,'$.metadata.collection')=?
          AND json_extract(payload,'$.metadata.sequence')=1
        """,
        ("三国志",),
    ).fetchone()
    if not row:
        raise RuntimeError("三国志卷01 source not found")
    source = json.loads(row["payload"])
    old = []
    for passage_row in conn.execute(
        "SELECT id,ordinal,payload FROM passages WHERE source_id=? ORDER BY ordinal,id",
        (row["id"],),
    ):
        payload = json.loads(passage_row["payload"])
        old.append(
            {
                "id": passage_row["id"],
                "ordinal": passage_row["ordinal"],
                "text": str(payload.get("text") or ""),
            }
        )
    conn.close()
    return row["id"], source, old


def prepare_corpus(
    service: ContextualCanaryService,
    *,
    source_id: str,
    source: dict[str, Any],
    old_passages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
    raw = fetch_wikitext(str(source["url"]))
    cleaned = clean_wikitext(raw)
    fresh_domain = split_passages(cleaned, source_id=source_id)
    corpus_version_id = deterministic_id(
        "corpus",
        {
            "source_id": source_id,
            "cleaned_hash": hashlib.sha256(cleaned.encode()).hexdigest(),
            "cleaner": "wikisource-clean-v4",
            "splitter": "all-nonempty-v2",
        },
    )
    fresh: list[dict[str, Any]] = []
    for passage in fresh_domain:
        passage_version_id = deterministic_id(
            "pv",
            {
                "corpus": corpus_version_id,
                "ordinal": passage.ordinal,
                "text": passage.text,
            },
        )
        item = {
            "id": passage_version_id,
            "source_passage_id": passage.id,
            "source_id": source_id,
            "ordinal": passage.ordinal,
            "text": passage.text,
            "text_hash": passage.text_hash,
        }
        fresh.append(item)
        service.conn.execute(
            "INSERT OR REPLACE INTO passage_versions(id,corpus_version_id,source_id,ordinal,text,text_hash,payload) VALUES(?,?,?,?,?,?,?)",
            (
                passage_version_id,
                corpus_version_id,
                source_id,
                passage.ordinal,
                passage.text,
                passage.text_hash,
                json.dumps(item, ensure_ascii=False),
            ),
        )
    service.conn.execute(
        "INSERT OR REPLACE INTO corpus_versions(id,source_id,source_title,cleaner_version,splitter_version,passage_count,payload) VALUES(?,?,?,?,?,?,?)",
        (
            corpus_version_id,
            source_id,
            source.get("title", ""),
            "wikisource-clean-v4",
            "all-nonempty-v2",
            len(fresh),
            json.dumps(
                {
                    "url": source.get("url"),
                    "cleaned_hash": hashlib.sha256(cleaned.encode()).hexdigest(),
                },
                ensure_ascii=False,
            ),
        ),
    )

    new_by_text: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for item in fresh:
        new_by_text[item["text"]].append(item)
    mapped = 0
    for old in old_passages:
        if not old["text"] or not new_by_text[old["text"]]:
            continue
        new = new_by_text[old["text"]].popleft()
        service.conn.execute(
            "INSERT OR REPLACE INTO passage_mappings(old_passage_id,new_passage_id,method,confidence) VALUES(?,?,?,?)",
            (old["id"], new["id"], "exact_text_ordered", 1.0),
        )
        mapped += 1
    service.conn.commit()
    return corpus_version_id, fresh, {
        "old_passages": len(old_passages),
        "new_passages": len(fresh),
        "mapped_exact": mapped,
        "restored_passages": len(fresh) - mapped,
    }


def target_indices(passages: list[dict[str, Any]]) -> list[tuple[dict[str, Any], int]]:
    result = []
    for target in TARGETS:
        matches = [index for index, passage in enumerate(passages) if passage["text"].startswith(target["prefix"])]
        if len(matches) != 1:
            raise RuntimeError(f"target {target['name']} matched {len(matches)} passages")
        result.append((target, matches[0]))
    return result


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 上下文归一化与双纪年灰度测试",
        "",
        f"- 运行：`{report['run_id']}`",
        f"- 状态：`{report['status']}`",
        f"- 执行模式：`{report['execution_mode']}`",
        f"- 语料版本：`{report['corpus_version_id']}`",
        "- 主库是否修改：否",
        "",
        "## 语料重建",
        "",
        f"- 旧 passage：{report['corpus']['old_passages']}",
        f"- 新 passage：{report['corpus']['new_passages']}",
        f"- 精确映射：{report['corpus']['mapped_exact']}",
        f"- 恢复/新增：{report['corpus']['restored_passages']}",
        "",
        "## 目标结果",
        "",
    ]
    for item in report["targets"]:
        if item["status"] == "failed" and item.get("error"):
            lines.extend(
                [
                    f"### {item['name']}",
                    "",
                    f"- passage：`{item['target_passage_id']}`",
                    f"- 上下文连续段数：{item['context_passages']}",
                    "- 状态：`failed`",
                    f"- 错误：`{item['error']}`",
                    "",
                ]
            )
            continue
        lines.extend(
            [
                f"### {item['name']}",
                "",
                f"- passage：`{item['target_passage_id']}`",
                f"- 期望公元年：{item['expected_year']}",
                f"- 确定性检查：`{item['deterministic_valid']}`",
                f"- 独立复核：`{item['verification_supported']}`",
                f"- 双纪年命中：`{item['expected_year_resolved']}`",
                f"- 上下文连续段数：{item['context_passages']}",
                f"- 状态：`{item['status']}`",
                "",
            ]
        )
    lines.extend(
        [
            "## 审批门禁",
            "",
            "只有两个目标均通过引文、上下文、双纪年和独立复核，才建议扩大到卷 17/40/46；`passed_manual` 只表示工程链路通过，外部模型仍需单独验收。本脚本不会启动 65 卷全量阅读。",
            "",
        ]
    )
    return "\n".join(lines)


def manual_replay(
    packet: dict[str, Any], target: dict[str, Any]
) -> tuple[ContextualReadingResponse, ContextualVerificationResponse]:
    chronology = packet["chronology_scaffold"]
    initial_anchor = next(
        row
        for row in chronology
        if row["relation"] == "explicit" and row["era_name"] == "初平"
    )
    target_id = packet["target_passage_id"]
    target_text = next(
        row["text"] for row in packet["continuous_passages"] if row["id"] == target_id
    )
    if target["prefix"].startswith("四年春"):
        reading = ContextualReadingResponse(
            target_passage_id=target_id,
            reading_summary="初平四年春，省略主语的记事承接太祖本纪，记太祖军驻鄄城。",
            mentions=[
                ContextMention(
                    surface="太祖",
                    entity_type="person",
                    canonical_surface="曹操",
                    confidence=0.98,
                    context_basis="卷首传主及连续纪年上下文",
                ),
                ContextMention(
                    surface="鄄城",
                    entity_type="place",
                    canonical_surface="鄄城",
                    confidence=0.99,
                    context_basis=target_id,
                ),
            ],
            claims=[
                ContextClaim(
                    subject="曹操",
                    predicate="驻军",
                    object="鄄城",
                    statement="曹操于初平四年春军驻鄄城。",
                    evidence_quote=target_text,
                    time_expression="四年春",
                    context_basis=(
                        f"卷首传主；{initial_anchor['passage_id']}建立初平元年；"
                        "中间二年、三年依次推进；目标为四年"
                    ),
                    confidence=0.96,
                )
            ],
            events=[
                ContextEvent(
                    label="曹操军鄄城",
                    event_type="军事驻屯",
                    participant_surfaces=["曹操"],
                    place="鄄城",
                    evidence_quote=target_text,
                    time_expression="四年春",
                    relation_type="inherited",
                    anchor_passage_id=initial_anchor["passage_id"],
                    era_name="初平",
                    era_year=4,
                    gregorian_year=193,
                    context_basis=(
                        f"{initial_anchor['passage_id']}初平元年，连续经过二年、三年到目标四年"
                    ),
                    confidence=0.98,
                )
            ],
        )
    else:
        reading = ContextualReadingResponse(
            target_passage_id=target_id,
            reading_summary="‘是岁’承接最近的初平四年，记孙策受袁术命渡江；其后数年取得江东。",
            mentions=[
                ContextMention(
                    surface="孙策",
                    entity_type="person",
                    canonical_surface="孙策",
                    confidence=0.99,
                    context_basis=target_id,
                ),
                ContextMention(
                    surface="袁术",
                    entity_type="person",
                    canonical_surface="袁术",
                    confidence=0.99,
                    context_basis=target_id,
                ),
                ContextMention(
                    surface="江东",
                    entity_type="place",
                    canonical_surface="江东",
                    confidence=0.95,
                    context_basis=target_id,
                ),
            ],
            claims=[
                ContextClaim(
                    subject="孙策",
                    predicate="受袁术派遣",
                    object="渡江",
                    statement="孙策于初平四年受袁术派遣渡江。",
                    evidence_quote="是歲，孫策受袁術使渡江",
                    time_expression="是岁",
                    context_basis="前文四年春记事后未改年号或年数",
                    confidence=0.97,
                ),
                ContextClaim(
                    subject="孙策",
                    predicate="取得",
                    object="江东",
                    statement="孙策在渡江后的数年间逐步取得江东。",
                    evidence_quote="數年閒遂有江東",
                    time_expression="数年间",
                    context_basis=target_id,
                    confidence=0.93,
                ),
            ],
            events=[
                ContextEvent(
                    label="孙策受命渡江",
                    event_type="军事行动",
                    participant_surfaces=["孙策", "袁术"],
                    place="",
                    evidence_quote="是歲，孫策受袁術使渡江",
                    time_expression="是岁",
                    relation_type="same_year",
                    anchor_passage_id=initial_anchor["passage_id"],
                    era_name="初平",
                    era_year=4,
                    gregorian_year=193,
                    context_basis="最近的明确年数为四年，后向锚点为兴平元年",
                    confidence=0.98,
                ),
                ContextEvent(
                    label="孙策数年间取得江东",
                    event_type="势力扩张",
                    participant_surfaces=["孙策"],
                    place="江东",
                    evidence_quote="數年閒遂有江東",
                    time_expression="数年间",
                    relation_type="range",
                    anchor_passage_id=target_id,
                    context_basis="原文只给出相对范围，不能强制补单一年份",
                    confidence=0.91,
                ),
            ],
        )
    verification = ContextualVerificationResponse(
        target_passage_id=target_id,
        quote_supported=True,
        entity_resolution_supported=True,
        temporal_resolution_supported=True,
        omission_check_passed=True,
        overall_supported=True,
        rationale=(
            "引文均逐字存在于目标段；省略主语由卷首传主和连续叙事支持；"
            "初平四年按190+4-1换算为公元193年；范围事件未伪造结束年。"
        ),
    )
    return reading, verification


async def run(args: argparse.Namespace) -> dict[str, Any]:
    canary_path = Path(args.canary_db)
    if canary_path.exists() and not args.resume:
        canary_path.unlink()
    service = ContextualCanaryService(str(canary_path))
    source_id, source, old_passages = load_source(args.main_db)
    corpus_version_id, passages, corpus_stats = prepare_corpus(
        service,
        source_id=source_id,
        source=source,
        old_passages=old_passages,
    )
    run_id = "context_canary_" + datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    service.conn.execute(
        "INSERT OR REPLACE INTO canary_runs(id,corpus_version_id,status,payload,updated_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP)",
        (run_id, corpus_version_id, "running", json.dumps({"targets": len(TARGETS)}, ensure_ascii=False)),
    )
    service.conn.commit()

    results = []
    for target, index in target_indices(passages):
        packet = build_context_packet(
            passages,
            index,
            source_title=str(source.get("title") or ""),
            lookahead=3,
        )
        try:
            if args.manual_replay:
                reading, verification = manual_replay(packet, target)
                reading_result = SimpleNamespace(
                    model="codex-manual-canary",
                    input_tokens=None,
                    output_tokens=None,
                )
                verification_result = SimpleNamespace(
                    model="codex-manual-canary-independent-check",
                    input_tokens=None,
                    output_tokens=None,
                )
                deterministic = validate_reading(reading, packet)
            else:
                reading, reading_result = await service.read(packet)
                deterministic = validate_reading(reading, packet)
                verification, verification_result = await service.verify(
                    packet, reading, deterministic
                )
            persisted = service.persist_verified(
                run_id=run_id,
                corpus_version_id=corpus_version_id,
                packet=packet,
                reading=reading,
                verification=verification,
                reading_model=reading_result.model,
                verification_model=verification_result.model,
                deterministic_validation=deterministic,
            )
            resolved_years = []
            for event in reading.events:
                if event.gregorian_year:
                    resolved_years.append(event.gregorian_year)
            expected_resolved = target["expected_year"] in resolved_years
            passed = bool(
                deterministic["valid"]
                and verification.overall_supported
                and verification.quote_supported
                and verification.temporal_resolution_supported
                and expected_resolved
            )
            results.append(
                {
                    **target,
                    "target_passage_id": packet["target_passage_id"],
                    "context_hash": packet["context_hash"],
                    "context_passages": len(packet["continuous_passages"]),
                    "chronology_scaffold": packet["chronology_scaffold"],
                    "reading": reading.model_dump(mode="json"),
                    "deterministic_validation": deterministic,
                    "deterministic_valid": deterministic["valid"],
                    "verification": verification.model_dump(mode="json"),
                    "verification_supported": verification.overall_supported,
                    "expected_year_resolved": expected_resolved,
                    "persisted": persisted,
                    "status": "passed" if passed else "failed",
                    "models": {
                        "reading": reading_result.model,
                        "verification": verification_result.model,
                    },
                    "usage": {
                        "reading_input_tokens": reading_result.input_tokens,
                        "reading_output_tokens": reading_result.output_tokens,
                        "verification_input_tokens": verification_result.input_tokens,
                        "verification_output_tokens": verification_result.output_tokens,
                    },
                }
            )
        except Exception as error:  # noqa: BLE001 - failure belongs in the canary report
            results.append(
                {
                    **target,
                    "target_passage_id": packet["target_passage_id"],
                    "context_hash": packet["context_hash"],
                    "context_passages": len(packet["continuous_passages"]),
                    "status": "failed",
                    "error": f"{type(error).__name__}: {str(error)[:1200]}",
                }
            )
    targets_passed = bool(results and all(item["status"] == "passed" for item in results))
    status = (
        "passed_manual"
        if targets_passed and args.manual_replay
        else "passed"
        if targets_passed
        else "failed"
    )
    report = {
        "run_id": run_id,
        "status": status,
        "execution_mode": "manual_replay" if args.manual_replay else "live_model",
        "main_database_modified": False,
        "canary_database": str(canary_path),
        "corpus_version_id": corpus_version_id,
        "corpus": corpus_stats,
        "targets": results,
        "full_reading_started": False,
        "approval_required_for_expansion": True,
    }
    service.conn.execute(
        "UPDATE canary_runs SET status=?,payload=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (status, json.dumps(report, ensure_ascii=False), run_id),
    )
    service.conn.commit()
    service.conn.close()

    output = Path(args.output)
    markdown = Path(args.markdown_output)
    output.parent.mkdir(parents=True, exist_ok=True)
    markdown.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown.write_text(render_markdown(report), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--main-db", default="data/three_kingdoms_rebuilt_v2.db")
    parser.add_argument("--canary-db", default="data/contextual_repair_canary.db")
    parser.add_argument("--output", default="artifacts/contextual_repair_canary.json")
    parser.add_argument("--markdown-output", default="artifacts/contextual_repair_canary.md")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--manual-replay", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(run(args))
    print(
        json.dumps(
            {
                "run_id": report["run_id"],
                "status": report["status"],
                "corpus": report["corpus"],
                "targets": [
                    {
                        "name": item["name"],
                        "status": item["status"],
                        "expected_year_resolved": item.get("expected_year_resolved"),
                        "error": item.get("error"),
                    }
                    for item in report["targets"]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
