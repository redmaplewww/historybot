from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .model_gateway import ModelGateway
from .repair import era_absolute_anchor, parse_era_year
from .repair_text import ERA_STARTS

RELATIVE_YEAR_PATTERN = re.compile(r"^(?P<year>元|[一二三四五六七八九十]{1,3})年")


class ContextMention(BaseModel):
    surface: str
    entity_type: str
    canonical_surface: str = ""
    confidence: float = Field(default=0.0, ge=0, le=1)
    context_basis: str = ""


class ContextClaim(BaseModel):
    subject: str
    predicate: str
    object: str
    statement: str
    evidence_quote: str
    time_expression: str = ""
    context_basis: str = ""
    confidence: float = Field(default=0.0, ge=0, le=1)


class ContextEvent(BaseModel):
    label: str
    event_type: str
    participant_surfaces: list[str] = Field(default_factory=list)
    place: str = ""
    evidence_quote: str
    time_expression: str = ""
    relation_type: Literal[
        "explicit",
        "inherited",
        "same_year",
        "next_year",
        "previous_year",
        "retrospective",
        "range",
        "unknown",
    ] = "unknown"
    anchor_passage_id: str = ""
    era_name: str | None = None
    era_year: int | None = None
    gregorian_year: int | None = None
    context_basis: str = ""
    confidence: float = Field(default=0.0, ge=0, le=1)


class ContextualReadingResponse(BaseModel):
    target_passage_id: str
    reading_summary: str
    mentions: list[ContextMention] = Field(default_factory=list)
    claims: list[ContextClaim] = Field(default_factory=list)
    events: list[ContextEvent] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


class ContextualVerificationResponse(BaseModel):
    target_passage_id: str
    quote_supported: bool
    entity_resolution_supported: bool
    temporal_resolution_supported: bool
    omission_check_passed: bool
    overall_supported: bool
    corrections: list[str] = Field(default_factory=list)
    rationale: str


def deterministic_id(prefix: str, payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()[:20]}"


def resolve_dual_calendar(era_name: str | None, era_year: int | None) -> int | None:
    if not era_name or not era_year or era_year < 1:
        return None
    start = ERA_STARTS.get(era_name)
    return start + era_year - 1 if start is not None else None


def chronology_scaffold(passages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a deterministic candidate chronology for AI adjudication.

    Explicit era anchors establish state. A leading ``二年/三年`` advances the
    active era, while ``是岁`` inherits the current state. This is only a
    candidate scaffold: the model must still decide whether the narrative
    actually inherits or retrospectively refers elsewhere.
    """
    active_era: str | None = None
    active_era_year: int | None = None
    active_gregorian: int | None = None
    active_anchor: str | None = None
    rows: list[dict[str, Any]] = []
    for passage in passages:
        text = str(passage.get("text") or "")
        explicit = era_absolute_anchor(text)
        relation = None
        if explicit:
            active_era, active_era_year, active_gregorian = explicit
            active_anchor = str(passage["id"])
            relation = "explicit"
        else:
            relative = RELATIVE_YEAR_PATTERN.match(text)
            if relative and active_era:
                parsed = parse_era_year(relative.group("year"))
                if parsed is not None:
                    active_era_year = parsed
                    active_gregorian = resolve_dual_calendar(active_era, parsed)
                    relation = "inherited_year_number"
            elif text.startswith(("是歲", "是岁")) and active_era:
                relation = "same_year_candidate"
        if relation:
            rows.append(
                {
                    "passage_id": passage["id"],
                    "ordinal": passage["ordinal"],
                    "time_expression": text[:20],
                    "relation": relation,
                    "anchor_passage_id": active_anchor,
                    "era_name": active_era,
                    "era_year": active_era_year,
                    "gregorian_year": active_gregorian,
                }
            )
    return rows


def build_context_packet(
    passages: list[dict[str, Any]],
    target_index: int,
    *,
    source_title: str,
    lookahead: int = 3,
) -> dict[str, Any]:
    previous_anchor_index = 0
    for index in range(target_index, -1, -1):
        if era_absolute_anchor(str(passages[index].get("text") or "")):
            previous_anchor_index = index
            break
    next_anchor_index = None
    for index in range(target_index + 1, len(passages)):
        if era_absolute_anchor(str(passages[index].get("text") or "")):
            next_anchor_index = index
            break
    end = min(len(passages), target_index + lookahead + 1)
    continuous = passages[previous_anchor_index:end]
    next_anchor = passages[next_anchor_index] if next_anchor_index is not None else None
    payload = {
        "source_title": source_title,
        "target_passage_id": passages[target_index]["id"],
        "target_ordinal": passages[target_index]["ordinal"],
        "section_context": passages[: min(3, len(passages))],
        "previous_anchor_passage_id": passages[previous_anchor_index]["id"],
        "next_anchor_passage_id": next_anchor["id"] if next_anchor else None,
        "continuous_passages": continuous,
        "next_anchor": next_anchor,
        "chronology_scaffold": chronology_scaffold(continuous),
    }
    payload["context_hash"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    return payload


def validate_reading(
    reading: ContextualReadingResponse,
    packet: dict[str, Any],
) -> dict[str, Any]:
    target = next(
        item
        for item in packet["continuous_passages"]
        if item["id"] == packet["target_passage_id"]
    )
    target_text = str(target["text"])
    errors: list[str] = []
    if reading.target_passage_id != packet["target_passage_id"]:
        errors.append("target_passage_id_mismatch")
    for item in [*reading.claims, *reading.events]:
        if not item.evidence_quote or item.evidence_quote not in target_text:
            errors.append(f"quote_not_in_target:{item.evidence_quote}")
    temporal_checks = []
    for event in reading.events:
        computed = resolve_dual_calendar(event.era_name, event.era_year)
        consistent = computed is None or event.gregorian_year is None or computed == event.gregorian_year
        if not consistent:
            errors.append(
                f"dual_calendar_mismatch:{event.era_name}{event.era_year}={computed},"
                f"model={event.gregorian_year}"
            )
        temporal_checks.append(
            {
                "event": event.label,
                "computed_gregorian_year": computed,
                "model_gregorian_year": event.gregorian_year,
                "consistent": consistent,
            }
        )
    return {"valid": not errors, "errors": errors, "temporal_checks": temporal_checks}


def render_context(packet: dict[str, Any]) -> str:
    section = "\n".join(
        f"[{item['id']}|ordinal={item['ordinal']}] {item['text']}"
        for item in packet.get("section_context", [])
    )
    passages = "\n".join(
        f"[{item['id']}|ordinal={item['ordinal']}] {item['text']}"
        for item in packet["continuous_passages"]
    )
    next_anchor = ""
    if packet.get("next_anchor"):
        item = packet["next_anchor"]
        next_anchor = f"\n后向边界锚点：[{item['id']}|ordinal={item['ordinal']}] {item['text']}"
    scaffold = json.dumps(packet["chronology_scaffold"], ensure_ascii=False, indent=2)
    return (
        f"来源：{packet['source_title']}\n"
        f"卷首/篇章上下文：\n{section}\n"
        f"目标 passage：{packet['target_passage_id']}\n"
        f"从前一绝对年号锚点到目标后的连续原文：\n{passages}{next_anchor}\n\n"
        f"确定性纪年候选骨架（只供核对，必须由原文关系判断是否承接）：\n{scaffold}"
    )


@dataclass
class ContextualCanaryService:
    db_path: str
    gateway: ModelGateway | None = None

    def __post_init__(self) -> None:
        self.gateway = self.gateway or ModelGateway()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.conn.executescript(
            """
CREATE TABLE IF NOT EXISTS corpus_versions (
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_title TEXT NOT NULL,
  cleaner_version TEXT NOT NULL, splitter_version TEXT NOT NULL,
  passage_count INTEGER NOT NULL, payload TEXT NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS passage_versions (
  id TEXT PRIMARY KEY, corpus_version_id TEXT NOT NULL, source_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL, text TEXT NOT NULL, text_hash TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS passage_mappings (
  old_passage_id TEXT NOT NULL, new_passage_id TEXT NOT NULL,
  method TEXT NOT NULL, confidence REAL NOT NULL,
  PRIMARY KEY(old_passage_id,new_passage_id)
);
CREATE TABLE IF NOT EXISTS context_packets (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, target_passage_id TEXT NOT NULL,
  context_hash TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS canary_runs (
  id TEXT PRIMARY KEY, corpus_version_id TEXT NOT NULL, status TEXT NOT NULL,
  payload TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS canary_steps (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, target_passage_id TEXT NOT NULL,
  step_type TEXT NOT NULL, status TEXT NOT NULL, model TEXT,
  output TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS canonical_entities (
  id TEXT PRIMARY KEY, canonical_surface TEXT NOT NULL, entity_type TEXT NOT NULL,
  status TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entity_mentions (
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL, passage_id TEXT NOT NULL,
  surface TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS canary_claims (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, passage_id TEXT NOT NULL,
  status TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS canary_events (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, passage_id TEXT NOT NULL,
  status TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS temporal_assertions (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, object_id TEXT NOT NULL,
  object_kind TEXT NOT NULL, passage_id TEXT NOT NULL,
  anchor_passage_id TEXT, era_name TEXT, era_year INTEGER,
  gregorian_from TEXT, gregorian_to TEXT, precision TEXT NOT NULL,
  relation_type TEXT NOT NULL, context_hash TEXT NOT NULL,
  verification_status TEXT NOT NULL, payload TEXT NOT NULL
);
"""
        )
        self.conn.commit()

    async def read(self, packet: dict[str, Any]) -> tuple[ContextualReadingResponse, Any]:
        context = render_context(packet)
        target = next(
            item
            for item in packet["continuous_passages"]
            if item["id"] == packet["target_passage_id"]
        )
        system = (
            "你是严谨的中国古代史料连续阅读员。必须利用前后文解析省略主语、称谓和时间指代，"
            "但 evidence_quote 只能逐字引用目标 passage。不能用外部历史知识补写原文。"
            "对时间同时给出年号和公元年；无法确定必须标 unknown。"
        )
        user = (
            f"{context}\n\n目标原文：\n[{target['id']}] {target['text']}\n\n"
            "请完整抽取目标原文的人物、主张和事件。特别判断省略主语以及‘是岁/某年’"
            "承接哪个前文锚点。context_basis 必须列出使用的 passage_id。"
        )
        result = await self.gateway.structured(
            task="historical_contextual_canary_reading",
            system=system,
            user=user,
            schema=ContextualReadingResponse,
        )
        return result.value, result

    async def verify(
        self,
        packet: dict[str, Any],
        reading: ContextualReadingResponse,
        deterministic_validation: dict[str, Any],
    ) -> tuple[ContextualVerificationResponse, Any]:
        system = (
            "你是独立史料复核员。只按给出的连续原文复核候选结果。"
            "逐项检查引文、上下文补出的主语、时间承接、公元与年号一致性，以及是否漏读。"
        )
        user = (
            f"{render_context(packet)}\n\n候选阅读：\n"
            f"{reading.model_dump_json(indent=2)}\n\n确定性检查：\n"
            f"{json.dumps(deterministic_validation, ensure_ascii=False, indent=2)}"
        )
        result = await self.gateway.structured(
            task="historical_contextual_canary_verification",
            system=system,
            user=user,
            schema=ContextualVerificationResponse,
        )
        return result.value, result

    def persist_verified(
        self,
        *,
        run_id: str,
        corpus_version_id: str,
        packet: dict[str, Any],
        reading: ContextualReadingResponse,
        verification: ContextualVerificationResponse,
        reading_model: str,
        verification_model: str,
        deterministic_validation: dict[str, Any],
    ) -> dict[str, int]:
        target_id = packet["target_passage_id"]
        verified = bool(verification.overall_supported and deterministic_validation["valid"])
        status = "verified" if verified else "review_required"
        self.conn.execute(
            "INSERT OR REPLACE INTO context_packets(id,run_id,target_passage_id,context_hash,payload) VALUES(?,?,?,?,?)",
            (
                deterministic_id("ctx", {"run": run_id, "target": target_id}),
                run_id,
                target_id,
                packet["context_hash"],
                json.dumps(packet, ensure_ascii=False),
            ),
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO canary_steps(id,run_id,target_passage_id,step_type,status,model,output) VALUES(?,?,?,?,?,?,?)",
            (
                deterministic_id("step", {"run": run_id, "target": target_id, "type": "read"}),
                run_id,
                target_id,
                "contextual_read",
                status,
                reading_model,
                reading.model_dump_json(),
            ),
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO canary_steps(id,run_id,target_passage_id,step_type,status,model,output) VALUES(?,?,?,?,?,?,?)",
            (
                deterministic_id("step", {"run": run_id, "target": target_id, "type": "verify"}),
                run_id,
                target_id,
                "contextual_verify",
                status,
                verification_model,
                verification.model_dump_json(),
            ),
        )
        entity_count = claim_count = event_count = temporal_count = 0
        for mention in reading.mentions:
            canonical = mention.canonical_surface.strip() or mention.surface.strip()
            entity_id = deterministic_id(
                "cent", {"surface": canonical, "entity_type": mention.entity_type}
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO canonical_entities(id,canonical_surface,entity_type,status,payload) VALUES(?,?,?,?,?)",
                (
                    entity_id,
                    canonical,
                    mention.entity_type,
                    status,
                    mention.model_dump_json(),
                ),
            )
            mention_id = deterministic_id(
                "ment", {"entity": entity_id, "passage": target_id, "surface": mention.surface}
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO entity_mentions(id,entity_id,passage_id,surface,payload) VALUES(?,?,?,?,?)",
                (mention_id, entity_id, target_id, mention.surface, mention.model_dump_json()),
            )
            entity_count += 1
        for claim in reading.claims:
            claim_id = deterministic_id(
                "cclaim",
                {
                    "target": target_id,
                    "subject": claim.subject,
                    "predicate": claim.predicate,
                    "object": claim.object,
                    "quote": claim.evidence_quote,
                },
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO canary_claims(id,run_id,passage_id,status,payload) VALUES(?,?,?,?,?)",
                (claim_id, run_id, target_id, status, claim.model_dump_json()),
            )
            claim_count += 1
        for event in reading.events:
            event_id = deterministic_id(
                "cevent", {"target": target_id, "label": event.label, "quote": event.evidence_quote}
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO canary_events(id,run_id,passage_id,status,payload) VALUES(?,?,?,?,?)",
                (event_id, run_id, target_id, status, event.model_dump_json()),
            )
            event_count += 1
            computed_year = resolve_dual_calendar(event.era_name, event.era_year)
            temporal_id = deterministic_id(
                "ctime", {"event": event_id, "context": packet["context_hash"]}
            )
            self.conn.execute(
                """
                INSERT OR REPLACE INTO temporal_assertions(
                  id,run_id,object_id,object_kind,passage_id,anchor_passage_id,
                  era_name,era_year,gregorian_from,gregorian_to,precision,
                  relation_type,context_hash,verification_status,payload
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    temporal_id,
                    run_id,
                    event_id,
                    "event",
                    target_id,
                    event.anchor_passage_id or None,
                    event.era_name,
                    event.era_year,
                    str(computed_year) if computed_year else None,
                    str(computed_year) if computed_year else None,
                    "year" if computed_year else "uncertain",
                    event.relation_type,
                    packet["context_hash"],
                    status,
                    json.dumps(
                        {
                            **event.model_dump(mode="json"),
                            "computed_gregorian_year": computed_year,
                            "corpus_version_id": corpus_version_id,
                            "verification": verification.model_dump(mode="json"),
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            temporal_count += 1
        self.conn.commit()
        return {
            "entities": entity_count,
            "claims": claim_count,
            "events": event_count,
            "temporal_assertions": temporal_count,
            "verified": int(verified),
        }
