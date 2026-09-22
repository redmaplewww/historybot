from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model_gateway import ModelGateway
from .v2_schemas import TemporalResponse

from .repair_text import (
    ALIAS_RELATIONS,
    ENTITY_TYPE_MAP,
    IDENTITY_RELATIONS,
    PUNCTUATION_TRANSLATION,
    ERA_STARTS,
    SANGUO_SOURCE_PATTERN,
    TEMPORAL_SYSTEM,
    TEMPORAL_USER_TEMPLATE,
    TEMPORAL_USER_ANCHOR_TEMPLATE,
)
# Era names are matched against the known 年号 vocabulary rather than by
# grabbing up to four preceding characters. A greedy prefix absorbed ordinary
# words ("是歲中平六年" -> era "是歲中平"), so real anchors were silently
# dropped and every dependent date lost its base.
ANCHOR_PATTERN = re.compile(
    r"(?P<era>" + "|".join(sorted(ERA_STARTS, key=len, reverse=True)) + r")"
    r"(?P<year>元|[一二三四五六七八九十]{1,3})年"
)
# The chronicles drop the era name on later years of the same reign
# ("二年春", "三年春"). Those passages carry no anchor but are exactly the
# steps a target must be counted through, so they are surfaced separately.
YEAR_WORD_PATTERN = re.compile(r"(?:^|[\s。：：」」])(元|[一二三四五六七八九十]{1,3})年")
CONVERT_DIGITS = {"元": 1, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
DIGITS_TO_CHINESE = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九"}


def normalize_surface(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = text.translate(PUNCTUATION_TRANSLATION).casefold()
    return re.sub(r"\s+", " ", text)


def normalize_entity_type(value: object) -> str:
    raw = normalize_surface(value)
    key = raw.replace("_", "").replace("-", "").replace(" ", "").lower()
    if raw in ENTITY_TYPE_MAP:
        return ENTITY_TYPE_MAP[raw]
    if key in ENTITY_TYPE_MAP:
        return ENTITY_TYPE_MAP[key]
    if "person" in key:
        return "person"
    if "location" in key or "place" in key or key == "loc":
        return "place"
    if "official" in key or "office" in key or "title" in key or "\u5b98" in raw:
        return "office"
    if "group" in key or "org" in key:
        return "group"
    return "concept"


def deterministic_id(prefix: str, payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()[:20]}"


def era_anchor(text: str) -> tuple[str, int] | None:
    match = None
    for candidate in ANCHOR_PATTERN.finditer(text):
        match = candidate
    if not match:
        return None
    value = parse_era_year(match.group("year"))
    if value is None:
        return None
    return match.group("era"), value


def parse_era_year(expression: str) -> int | None:
    """Parse a 年号 year expression such as 元, 九, 十, 十七, 二十四.

    Year values stay in the 1-40 range used by the chronicle. The previous
    implementation accumulated digits from the right and read 二十四 as 16,
    which shifted every anchor it resolved.
    """
    if not expression:
        return None
    if expression == "\u5143":
        return 1
    if "\u5341" not in expression:
        total = 0
        for char in expression:
            numeral = CONVERT_DIGITS.get(char)
            if numeral is None or numeral == 10:
                return None
            total = total * 10 + numeral
        return total or None
    if expression.count("\u5341") != 1:
        return None
    head, _, tail = expression.partition("\u5341")
    if len(head) > 1 or len(tail) > 1:
        return None
    tens = CONVERT_DIGITS[head] if head else 1
    ones = CONVERT_DIGITS[tail] if tail else 0
    if tens == 10 or ones == 10:
        return None
    return tens * 10 + ones


def format_era_year(value: int) -> str:
    """Render a 1-40 year value the way the chronicles write it (元, 九, 二十四)."""
    if value == 1:
        return "\u5143"
    if value < 10:
        return DIGITS_TO_CHINESE[value]
    if value == 10:
        return "\u5341"
    if value < 20:
        return "\u5341" + DIGITS_TO_CHINESE[value - 10]
    tens, ones = divmod(value, 10)
    return DIGITS_TO_CHINESE[tens] + "\u5341" + (DIGITS_TO_CHINESE[ones] if ones else "")


def era_absolute_anchor(text: str) -> tuple[str, int, int] | None:
    anchor = era_anchor(text)
    if not anchor:
        return None
    era_name, era_year = anchor
    start = ERA_STARTS.get(era_name)
    if start is None:
        return None
    return era_name, era_year, start + era_year - 1


def date_string(ref: dict, endpoint: str) -> str | None:
    explicit = ref.get(endpoint)
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    year = ref.get("year")
    if endpoint == "time_to" and ref.get("to_year"):
        year = ref.get("to_year")
    if not year:
        return None
    month = ref.get("month")
    day = ref.get("day")
    if endpoint == "time_to" and ref.get("to_month"):
        month = ref.get("to_month")
    if day and not month:
        month = 1
    if not month:
        return str(int(year))
    if day:
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    return f"{int(year):04d}-{int(month):02d}"


@dataclass
class RepairService:
    db_path: str
    gateway: ModelGateway | None = None
    concurrency: int = 8
    context_size: int = 4
    era_table_mode: str = "anchor"
    run_id: str = "repair_v3_default"
    retry_failed: bool = False
    _conn: sqlite3.Connection | None = None
    _era_starts_cache: dict[str, int] | None = None

    def __post_init__(self) -> None:
        self.gateway = self.gateway or ModelGateway()
        self.conn = sqlite3.connect(self.db_path, timeout=60.0)
        self.conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.conn.executescript(
            """
CREATE TABLE IF NOT EXISTS v3_normalized_surfaces (
  surface_key TEXT PRIMARY KEY, surface TEXT NOT NULL, canonical_id TEXT NOT NULL,
  entity_type TEXT NOT NULL, method TEXT NOT NULL, confidence REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS v3_surface_links (
  surface_a TEXT NOT NULL, surface_b TEXT NOT NULL,
  relation TEXT NOT NULL, passage_id TEXT NOT NULL,
  PRIMARY KEY(surface_a, surface_b, relation, passage_id)
);
CREATE TABLE IF NOT EXISTS v3_entity_rollups (
  canonical_id TEXT PRIMARY KEY, canonical_surface TEXT NOT NULL,
  entity_type TEXT NOT NULL, member_keys TEXT NOT NULL, member_surfaces TEXT NOT NULL,
  confidence REAL NOT NULL, method TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v3_object_time_links (
  object_id TEXT PRIMARY KEY, object_kind TEXT NOT NULL, passage_id TEXT NOT NULL,
  claim_id TEXT, time_ref TEXT NOT NULL, status TEXT NOT NULL,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS v3_repair_steps (
  step_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
  step_type TEXT NOT NULL, input_ref TEXT, output TEXT NOT NULL, model TEXT,
  prompt_version TEXT, status TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS v3_repair_runs (
  run_id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_v3_object_time_object ON v3_object_time_links(object_kind, object_id);
CREATE INDEX IF NOT EXISTS idx_v3_repair_steps_run ON v3_repair_steps(run_id, step_type, status);
"""
        )
        self.conn.commit()

    def load_v2(self) -> tuple[dict, dict, dict, dict]:
        passages: dict[str, dict] = {}
        sql = f"""
          SELECT p.id, p.source_id, p.ordinal, p.payload, s.payload AS source_payload
          FROM passages p JOIN sources s ON s.id = p.source_id
          WHERE json_extract(s.payload, '$.title') LIKE '{SANGUO_SOURCE_PATTERN}'
        """
        for row in self.conn.execute(sql):
            passage = json.loads(row["payload"])
            source = json.loads(row["source_payload"])
            passage.update(source_id=row["source_id"], source_title=source.get("title", ""))
            passages[row["id"]] = passage
        entities: dict[str, dict] = {}
        for row in self.conn.execute("SELECT id,payload FROM entities WHERE id LIKE 'ent_v2_%'"):
            item = json.loads(row["payload"])
            item["id"] = row["id"]
            entities[row["id"]] = item
        claims: dict[str, dict] = {}
        for row in self.conn.execute("SELECT id,payload FROM claims WHERE id LIKE 'clm_v2_%'"):
            item = json.loads(row["payload"])
            item["id"] = row["id"]
            claims[row["id"]] = item
        events: dict[str, dict] = {}
        for row in self.conn.execute("SELECT id,payload FROM events WHERE id LIKE 'evt_v2_%'"):
            item = json.loads(row["payload"])
            item["id"] = row["id"]
            events[row["id"]] = item
        return passages, entities, claims, events

    def normalize_entities(self, *, persist: bool = True) -> dict[str, Any]:
        passages, entities, claims, events = self.load_v2()
        surface_records: dict[str, list[dict]] = defaultdict(list)
        for entity in entities.values():
            surface = str(entity.get("surface", "")).strip()
            if not surface:
                continue
            key = normalize_surface(surface)
            surface_records[key].append(
                {
                    "surface": surface,
                    "entity_type": normalize_entity_type(entity.get("entity_type")),
                    "source_entity_id": entity["id"],
                    "passage_id": entity.get("passage_id", ""),
                }
            )
        surface_keys = sorted(surface_records)
        index = {key: index for index, key in enumerate(surface_keys)}
        parent = list(range(len(surface_keys)))
        type_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for key, records in surface_records.items():
            for record in records:
                type_counts[key][record["entity_type"]] += 1

        def dominant_type(surface_key: str) -> str:
            return max(
                type_counts[surface_key],
                key=lambda entity_type: (type_counts[surface_key][entity_type], entity_type),
            )

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra == rb:
                return
            # Prefer the lexicographically smallest traditional surface as canonical seed.
            if surface_keys[rb] < surface_keys[ra]:
                ra, rb = rb, ra
            parent[rb] = ra

        for row in self.conn.execute("SELECT canonical_surface,alias_surface,relation,passage_id FROM entity_aliases"):
            a = normalize_surface(row["canonical_surface"])
            b = normalize_surface(row["alias_surface"])
            if a not in index or b not in index or a == b:
                continue
            if row["relation"] in IDENTITY_RELATIONS or row["relation"] in ALIAS_RELATIONS:
                union(index[a], index[b])
                self.conn.execute(
                    "INSERT OR IGNORE INTO v3_surface_links(surface_a,surface_b,relation,passage_id) VALUES(?,?,?,?)",
                    (surface_keys[index[a]], surface_keys[index[b]], row["relation"], row["passage_id"]),
                )
        surface_to_canonical = {}
        members: dict[str, list[str]] = defaultdict(list)
        for key in surface_keys:
            root = find(index[key])
            canonical = surface_keys[root]
            surface_to_canonical[key] = deterministic_id(
                "entnorm",
                {"canonical_surface": canonical, "entity_type": dominant_type(canonical)},
            )
            members[canonical].append(key)
        rollups = {}
        for canonical, keys in sorted(members.items()):
            member_type = dominant_type(canonical)
            member_surfaces = sorted({surface_records[key][0]["surface"] for key in keys})
            member_keys = sorted(set(keys))
            canonical_id = surface_to_canonical[canonical]
            rollups[canonical_id] = {
                "canonical_id": canonical_id,
                "canonical_surface": surface_records[canonical][0]["surface"],
                "entity_type": member_type,
                "member_keys": member_keys,
                "member_surfaces": member_surfaces,
                "confidence": 0.85,
                "method": "ai_identity_alias_union_traditional",
            }
        if persist:
            for key in surface_keys:
                self.conn.execute(
                    "INSERT OR REPLACE INTO v3_normalized_surfaces(surface_key,surface,canonical_id,entity_type,method,confidence) VALUES(?,?,?,?,?,?)",
                    (
                        key,
                        surface_records[key][0]["surface"],
                        surface_to_canonical[key],
                        dominant_type(key),
                        "nfkc_traditional_seed",
                        0.85,
                    ),
                )
            for item in rollups.values():
                self.conn.execute(
                    "INSERT OR REPLACE INTO v3_entity_rollups(canonical_id,canonical_surface,entity_type,member_keys,member_surfaces,confidence,method) VALUES(?,?,?,?,?,?,?)",
                    (item["canonical_id"], item["canonical_surface"], item["entity_type"], json.dumps(item["member_keys"], ensure_ascii=False), json.dumps(item["member_surfaces"], ensure_ascii=False), item["confidence"], item["method"]),
                )
            for entity_id, entity in entities.items():
                surface = str(entity.get("surface", "")).strip()
                key = normalize_surface(surface)
                if key not in surface_to_canonical:
                    continue
                entity["normalization"] = {
                    "surface_key": key,
                    "normalized_entity_id": surface_to_canonical[key],
                    "normalized_entity_type": dominant_type(key),
                    "method": "v3_surface_rollup",
                }
                self.conn.execute("UPDATE entities SET payload=? WHERE id=?", (json.dumps(entity, ensure_ascii=False), entity_id))
            self.conn.commit()
        return {
            "raw_entities": len(entities),
            "normalized_surfaces": len(surface_keys),
            "normalized_entities": len(rollups),
            "identity_links_used": sum(len(v) for v in members.values()) - len(members),
        }

    def _stored_step_id(self, step_id: str) -> str:
        """Scope a logical checkpoint ID to this repair run.

        ``v3_repair_steps.step_id`` is the table's global primary key. Earlier
        versions stored ``ckpt_time_<passage>`` directly, so starting another
        run replaced the first run's row. Prefixing with ``run_id`` preserves
        each ledger while ``_get_step`` still recognizes same-run legacy rows.
        """
        return f"{self.run_id}:{step_id}"

    def _save_step(self, step_id: str, *, step_type: str, input_ref: str, output: dict, model: str | None = None, prompt_version: str = "temporal-v3", status: str = "complete") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO v3_repair_steps(step_id,run_id,ordinal,step_type,input_ref,output,model,prompt_version,status) VALUES(?,?,?,?,?,?,?,?,?)",
            (self._stored_step_id(step_id), self.run_id, 0, step_type, input_ref, json.dumps(output, ensure_ascii=False), model, prompt_version, status),
        )
        self.conn.commit()

    def _get_step(self, step_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT output,model,prompt_version,status FROM v3_repair_steps WHERE step_id=? AND run_id=?",
            (self._stored_step_id(step_id), self.run_id),
        ).fetchone()
        if not row:
            # Backward compatibility for ledgers written before run-scoped IDs.
            # The run_id predicate prevents another run's legacy checkpoint
            # from being reused merely because it targets the same passage.
            row = self.conn.execute(
                "SELECT output,model,prompt_version,status FROM v3_repair_steps WHERE step_id=? AND run_id=?",
                (step_id, self.run_id),
            ).fetchone()
        if not row:
            return None
        try:
            return {"output": json.loads(row["output"]), "model": row["model"], "prompt_version": row["prompt_version"], "status": row["status"]}
        except Exception:
            return None

    def _temporal_prompt(
        self,
        window: list[dict],
        targets: list[dict],
        anchor_context: str = "",
        reading_summaries: str = "",
        era_table: str = "",
    ) -> tuple[str, str]:
        context_lines = []
        for passage in window:
            context_lines.append(f"[{passage['id']}] {passage.get('text', '').strip()}")
        targets_by_passage: dict[str, list[dict]] = defaultdict(list)
        for target in targets:
            targets_by_passage[target["passage_id"]].append(target)
        target_lines = []
        for passage_id in [p["id"] for p in window]:
            items = targets_by_passage.get(passage_id, [])
            if not items:
                continue
            payload = []
            for item in items:
                if item["kind"] == "claim":
                    payload.append({"claim_id": item["id"], "statement": f"{item.get('subject', '')} {item.get('predicate', '')} {item.get('object', '')}"})
                else:
                    payload.append({"event_id": item["id"], "label": item.get("label", ""), "event_type": item.get("event_type", "")})
            target_lines.append(json.dumps({"passage_id": passage_id, "items": payload}, ensure_ascii=False))
        source_context = "\n".join(context_lines)
        if anchor_context:
            source_context = anchor_context + "\n" + source_context
        target_context = "\n".join(target_lines)
        system = TEMPORAL_SYSTEM
        target_context = "\n".join(target_lines)
        if anchor_context:
            user = TEMPORAL_USER_ANCHOR_TEMPLATE.format(
                reading_summaries=reading_summaries,
                anchor_context=anchor_context,
                source_context=source_context,
                target_context=target_context,
                era_table=era_table,
            )
        else:
            user = TEMPORAL_USER_TEMPLATE.format(
                source_context=source_context,
                target_context=target_context,
            )
        return system, user

    def _build_window_targets(self, passages: list[dict], claims: dict[str, dict], events: dict[str, dict], reading_steps: dict[str, dict]) -> list[dict]:
        claims_by_passage: dict[str, list[dict]] = defaultdict(list)
        events_by_passage: dict[str, list[dict]] = defaultdict(list)
        for claim in claims.values():
            for passage_id in claim.get("passage_ids", []) or []:
                claims_by_passage[passage_id].append(claim)
        for event in events.values():
            if event.get("passage_id"):
                events_by_passage[event["passage_id"]].append(event)
        windows = []
        for index, passage in enumerate(passages):
            passage_id = passage["id"]
            targets = []
            for claim in claims_by_passage.get(passage_id, []):
                targets.append({"kind": "claim", "passage_id": passage_id, **claim})
            for event in events_by_passage.get(passage_id, []):
                targets.append({"kind": "event", "passage_id": passage_id, **event})
            if not targets:
                continue
            first = max(0, index - self.context_size)
            window = passages[first:index + 1]
            reading = reading_steps.get(passage_id, {}).get("output", {}).get("units", [{}])[0]
            windows.append({"passages": window, "targets": targets, "reading": reading})
        return windows

    def _forward_anchor_context(
        self,
        ordered: list[dict],
        target_index: int,
        *,
        max_backward: int = 200,
        anchor_span: int = 4,
    ) -> str:
        """Build the era-year context a target needs to resolve its own date.

        The chronicles omit the era name on later years (二年春, 三年春) and
        then say 是歲 for the closing year. An anchor alone is not enough: the
        intervening year-bearing passages are what let the model count forward
        from 初平元年 to 初平四年. They are therefore listed explicitly.
        """
        lower = max(0, target_index - max_backward)
        for index in range(target_index - 1, lower - 1, -1):
            anchor = era_absolute_anchor(str(ordered[index].get("text", "")))
            if not anchor:
                continue
            anchor_name, anchor_year, absolute_year = anchor
            first = max(0, index - anchor_span + 1)
            selected = ordered[first : index + 1]
            label = (
                f"前文锚点：{anchor_name}{format_era_year(anchor_year)}年，"
                f"对应公元约{absolute_year}年。"
            )
            between = [
                item
                for item in ordered[index + 1 : target_index]
                if YEAR_WORD_PATTERN.search(str(item.get("text", "")))
            ]
            sections = [
                label
                + "注意：锚点只说明该前文位置的纪年；目标是否承接该纪年必须由原文关系判断。",
                "锚点所在片段：",
                "\n".join(
                    f"[{item['id']}] {item.get('text', '').strip()}" for item in selected
                ),
            ]
            if between:
                sections.append(
                    "锚点与目标之间省略年号的纪年片段（若这些递增属于同一年号，"
                    "目标需要由此续推）："
                )
                sections.append(
                    "\n".join(
                        f"[{item['id']}] {item.get('text', '').strip()}" for item in between
                    )
                )
            return "\n".join(sections)
        return ""

    def _reading_steps_for_passages(self, passage_ids: list[str]) -> dict[str, dict]:
        result = {}
        for passage_id in passage_ids:
            cached = self._get_step(f"original_read_{passage_id}")
            if cached:
                result[passage_id] = cached
        return result

    def _original_reading_steps(self) -> dict[str, dict]:
        rows = self.conn.execute(
            "SELECT id,output FROM v2_reading_steps WHERE step_type='read' AND id LIKE 'ckpt_read_%'"
        ).fetchall()
        result = {}
        for row in rows:
            passage_id = row["id"].removeprefix("ckpt_read_")
            try:
                result[passage_id] = {"output": json.loads(row["output"])}
            except Exception:
                continue
        return result

    def _reference_sort_key(self, item: dict) -> str:
        return str(item.get("id", "")).removeprefix("clm_v2_").removeprefix("evt_v2_")

    def _era_table(self) -> str:
        """Render the era-year reference table used by the temporal prompt.

        ``anchor`` (default) lists only the era names that occur in the corpus,
        which keeps the prompt small and avoids handing the model a table it
        does not need. ``full`` lists every known 年号 and ``off`` omits the
        table entirely; both exist because an OpenAI-compatible gateway can
        reject otherwise valid prompts, and the table is the part under test.
        """
        if self.era_table_mode == "off":
            return ""
        eras = (
            sorted(ERA_STARTS.items())
            if self.era_table_mode == "full"
            else sorted(self._era_starts_in_corpus().items())
        )
        if not eras:
            return ""
        return "\n".join(f"{era}元年={start}" for era, start in eras)

    def _era_starts_in_corpus(self) -> dict[str, int]:
        """Era names that actually appear in the 三国志 passages of this database."""
        if self._era_starts_cache is None:
            found: dict[str, int] = {}
            for row in self.conn.execute(
                """
                SELECT json_extract(p.payload, '$.text') AS text
                FROM passages p JOIN sources s ON s.id = p.source_id
                WHERE json_extract(s.payload, '$.title') LIKE ?
                """,
                (SANGUO_SOURCE_PATTERN,),
            ):
                text = row["text"] or ""
                for era, start in ERA_STARTS.items():
                    if era in text:
                        found[era] = start
            self._era_starts_cache = found
        return self._era_starts_cache

    def _normalize_time_ref(self, reference: dict) -> dict:
        ref = dict(reference)
        precision = str(ref.get("time_precision") or "uncertain")
        if precision not in {"day", "month", "year", "range", "decade", "era_only", "uncertain"}:
            precision = "uncertain"
        ref["time_precision"] = precision
        ref["time_from"] = date_string(ref, "time_from")
        ref["time_to"] = date_string(ref, "time_to")
        if precision in {"era_only", "uncertain"}:
            ref["time_from"] = None
            ref["time_to"] = None
        if precision == "year" and ref.get("time_from") and not ref.get("time_to"):
            ref["time_to"] = ref["time_from"]
        if ref.get("month") and not ref.get("year"):
            ref["time_precision"] = "uncertain"
            ref["time_from"] = None
            ref["time_to"] = None
        return ref

    async def run_temporal_repair(self, *, limit_sources: int | None = None) -> dict[str, Any]:
        passages, entities, claims, events = self.load_v2()
        passage_ids = set(passages)
        claims = {k: v for k, v in claims.items() if set(v.get("passage_ids", []) or []).issubset(passage_ids)}
        events = {k: v for k, v in events.items() if v.get("passage_id") in passage_ids}
        by_source: dict[str, list[dict]] = defaultdict(list)
        for passage in passages.values():
            by_source[passage["source_id"]].append(passage)
        source_ids = sorted(by_source, key=lambda sid: next(p["source_title"] for p in by_source[sid]))
        if limit_sources is not None:
            source_ids = source_ids[:limit_sources]
        reading_steps = self._original_reading_steps()
        tasks: list[dict] = []
        for source_id in source_ids:
            ordered = sorted(by_source[source_id], key=lambda p: p["ordinal"])
            source_tasks = self._build_window_targets(ordered, claims, events, reading_steps)
            passage_index = {p["id"]: index for index, p in enumerate(ordered)}
            for task in source_tasks:
                target_index = passage_index[task["passages"][-1]["id"]]
                task["target_index"] = target_index
                task["anchor_context"] = self._forward_anchor_context(ordered, target_index)
                summary_parts = []
                for prior in range(max(0, target_index - 20), target_index):
                    prior_id = ordered[prior]["id"]
                    prior_reading = reading_steps.get(prior_id, {}).get("output", {}).get("units", [])
                    if prior_reading:
                        summary = str(prior_reading[0].get("reading_summary", "")).strip()
                        if summary:
                            summary_parts.append(f"[{prior_id}] {summary}")
                task["reading_summaries"] = "\n".join(summary_parts[-20:])
                task["era_table"] = self._era_table()
            tasks.extend(source_tasks)
        self.conn.execute(
            "INSERT OR REPLACE INTO v3_repair_runs(run_id,status,payload,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP)",
            (self.run_id, "running", json.dumps({"windows": len(tasks), "targets": sum(len(t["targets"]) for t in tasks)}, ensure_ascii=False)),
        )
        self.conn.commit()
        semaphore = asyncio.Semaphore(max(1, self.concurrency))
        completed = 0
        processed = 0
        failures: list[dict] = []

        async def process_window(window: dict) -> None:
            nonlocal completed, processed
            async with semaphore:
                first_passage = window["passages"][-1]
                passage_id = first_passage["id"]
                checkpoint_id = f"ckpt_time_{passage_id}"
                target_ids = {str(item["id"]) for item in window["targets"]}
                cached = self._get_step(checkpoint_id)
                if cached and target_ids.issubset({r.get("claim_id", r.get("event_id")) for r in cached["output"].get("references", [])}):
                    references = cached["output"].get("references", [])
                elif cached and cached.get("status") == "failed" and not self.retry_failed:
                    # A window that a gateway rejected is not silently retried
                    # on every resume; it stays recorded as failed until an
                    # explicit retry run asks for it.
                    processed += 1
                    return
                else:
                    system, user = self._temporal_prompt(
                        window["passages"],
                        window["targets"],
                        anchor_context=window.get("anchor_context", ""),
                        reading_summaries=window.get("reading_summaries", ""),
                        era_table=window.get("era_table", ""),
                    )
                    try:
                        result = await self.gateway.structured(task="historical_temporal_resolution", system=system, user=user, schema=TemporalResponse)
                    except Exception as error:  # noqa: BLE001 - one window must not abort the run
                        # A gateway can reject a window (upstream content policy)
                        # or return unparsable JSON. Recording the window as
                        # failed keeps it retryable and keeps the other windows
                        # moving; it never fabricates a time for the targets.
                        failures.append(
                            {
                                "passage_id": passage_id,
                                "ordinal": first_passage.get("ordinal"),
                                "error": f"{type(error).__name__}: {str(error)[:300]}",
                            }
                        )
                        self._save_step(
                            checkpoint_id,
                            step_type="temporal_resolution",
                            input_ref=passage_id,
                            output={"failed": f"{type(error).__name__}: {str(error)[:300]}"},
                            model=None,
                            prompt_version="temporal-v3",
                            status="failed",
                        )
                        processed += 1
                        return
                    references = [self._normalize_time_ref(item.model_dump(mode="json")) for item in result.value.references]
                    self._save_step(
                        checkpoint_id,
                        step_type="temporal_resolution",
                        input_ref=passage_id,
                        output={"references": references, "context_summary": result.value.context_summary, "usage": result.raw.get("usage", {})},
                        model=result.model,
                    )
                valid = {str(ref.get("claim_id") or ref.get("event_id") or ""): ref for ref in references}
                for target in window["targets"]:
                    ref = valid.get(target["id"])
                    if not ref:
                        continue
                    kind = target["kind"]
                    self.conn.execute(
                        "INSERT OR REPLACE INTO v3_object_time_links(object_id,object_kind,passage_id,claim_id,time_ref,status,updated_at) VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                        (target["id"], kind, passage_id, target["id"] if kind == "claim" else None, json.dumps(ref, ensure_ascii=False), "resolved" if ref.get("time_from") else str(ref.get("time_precision", "uncertain"))),
                    )
                self.conn.commit()
                completed += 1
                processed += 1
                if processed % 25 == 0:
                    print(
                        f"TEMPORAL {self.run_id}: {processed}/{len(tasks)} windows "
                        f"({completed} resolved, {len(failures)} failed)",
                        flush=True,
                    )

        await asyncio.gather(*(process_window(task) for task in tasks))
        materialization = self._materialize_temporal(claims, events)
        # Report from the persisted ledger rather than this run's in-memory
        # list: a resumed run reuses checkpoints and only appends newly failed
        # windows, so the in-memory count under-reports the real total.
        ledger = {
            row["status"]: row["n"]
            for row in self.conn.execute(
                "SELECT status, COUNT(*) n FROM v3_repair_steps "
                "WHERE run_id=? AND step_type='temporal_resolution' GROUP BY status",
                (self.run_id,),
            ).fetchall()
        }
        payload = {
            "windows": len(tasks),
            "completed": ledger.get("complete", completed),
            "failed_windows": ledger.get("failed", len(failures)),
            **materialization,
        }
        self.conn.execute(
            "INSERT OR REPLACE INTO v3_repair_runs(run_id,status,payload,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP)",
            (self.run_id, "complete", json.dumps(payload, ensure_ascii=False)),
        )
        self.conn.commit()
        return payload

    def _materialize_temporal(self, claims: dict[str, dict], events: dict[str, dict]) -> dict[str, Any]:
        rows = self.conn.execute("SELECT object_id,object_kind,passage_id,claim_id,time_ref,status FROM v3_object_time_links WHERE object_id IS NOT NULL").fetchall()
        best: dict[str, tuple[tuple[int, int, str], sqlite3.Row]] = {}
        for row in rows:
            ref = json.loads(row["time_ref"])
            confidence = max(0.0, min(1.0, float(ref.get("confidence", 0.0))))
            rank = (
                1 if ref.get("time_from") or ref.get("time_to") else 0,
                int(round(confidence * 1000)),
                str(row["object_id"]),
            )
            if row["object_id"] not in best or rank > best[row["object_id"]][0]:
                best[row["object_id"]] = rank, row
        claim_updates = event_updates = uncertain = 0
        precision_counts: dict[str, int] = defaultdict(int)
        for row in [value[1] for value in best.values()]:
            ref = json.loads(row["time_ref"])
            precision_counts[str(ref.get("time_precision", "uncertain"))] += 1
            if not ref.get("time_from") and not ref.get("time_to"):
                uncertain += 1
                continue
            if row["object_kind"] == "claim":
                claim = claims.get(row["object_id"])
                if not claim:
                    continue
                claim["time_from"] = ref.get("time_from")
                claim["time_to"] = ref.get("time_to")
                claim["time_expression"] = ref.get("time_expression") or claim.get("time_expression")
                claim["time_precision"] = ref.get("time_precision")
                claim["temporal"] = ref
                self.conn.execute("UPDATE claims SET payload=? WHERE id=?", (json.dumps(claim, ensure_ascii=False), row["object_id"]))
                relation_id = f"rel_{row['object_id']}"
                relation_row = self.conn.execute("SELECT payload FROM relations WHERE id=?", (relation_id,)).fetchone()
                if relation_row:
                    relation = json.loads(relation_row["payload"])
                    relation.update(valid_from=claim["time_from"], valid_to=claim["time_to"], time_precision=claim.get("time_precision"), time_expression=claim.get("time_expression"), temporal=ref)
                    # The relation table stores valid_from/valid_to as columns
                    # and keeps precision/expression inside the JSON payload, so
                    # only the columns that exist are written.
                    self.conn.execute(
                        "UPDATE relations SET valid_from=?,valid_to=?,payload=? WHERE id=?",
                        (
                            claim["time_from"],
                            claim["time_to"],
                            json.dumps(relation, ensure_ascii=False),
                            relation_id,
                        ),
                    )
                claim_updates += 1
            else:
                event = events.get(row["object_id"])
                if not event:
                    continue
                event["date_start"] = ref.get("time_from")
                event["date_end"] = ref.get("time_to")
                event["time_expression"] = ref.get("time_expression")
                event["time_precision"] = ref.get("time_precision")
                event["temporal"] = ref
                self.conn.execute("UPDATE events SET payload=? WHERE id=?", (json.dumps(event, ensure_ascii=False), row["object_id"]))
                if ref.get("time_from") or ref.get("time_to"):
                    self.conn.execute(
                        "INSERT OR REPLACE INTO timeline_entries(id,event_id,start_date,end_date,payload) VALUES(?,?,?,?,?)",
                        (f"tl_{row['object_id']}", row["object_id"], ref.get("time_from"), ref.get("time_to"), json.dumps({**event, "provenance": {"kind": "temporal_repair_v3", "passage_id": row["passage_id"]}}, ensure_ascii=False)),
                    )
                event_updates += 1
        self.conn.commit()
        return {
            "claims_updated": claim_updates,
            "events_updated": event_updates,
            "uncertain_or_era_only": uncertain,
            "precision_counts": dict(sorted(precision_counts.items())),
        }

    def report(self) -> dict[str, Any]:
        claims = self.conn.execute("SELECT COUNT(*) FROM claims WHERE id LIKE 'clm_v2_%'").fetchone()[0]
        events = self.conn.execute("SELECT COUNT(*) FROM events WHERE id LIKE 'evt_v2_%'").fetchone()[0]
        claim_time = self.conn.execute("SELECT COUNT(*) FROM claims WHERE id LIKE 'clm_v2_%' AND (json_extract(payload,'$.time_from') IS NOT NULL OR json_extract(payload,'$.time_to') IS NOT NULL)").fetchone()[0]
        event_time = self.conn.execute("SELECT COUNT(*) FROM events WHERE id LIKE 'evt_v2_%' AND (json_extract(payload,'$.date_start') IS NOT NULL OR json_extract(payload,'$.date_end') IS NOT NULL)").fetchone()[0]
        surfaces = self.conn.execute("SELECT COUNT(*) FROM v3_normalized_surfaces").fetchone()[0]
        rollups = self.conn.execute("SELECT COUNT(*) FROM v3_entity_rollups").fetchone()[0]
        return {
            "claims": claims,
            "claims_with_absolute_time": claim_time,
            "events": events,
            "events_with_absolute_time": event_time,
            "normalized_surfaces": surfaces,
            "normalized_entities": rollups,
        }


def _repair_usage(steps: list[dict]) -> dict[str, int]:
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for step in steps:
        usage = step.get("output", {}).get("usage", {}) or {}
        for key in totals:
            totals[key] += int(usage.get(key, 0))
    return totals


def run_repair_cli() -> dict[str, Any]:
    import argparse
    import asyncio
    from datetime import datetime

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/three_kingdoms_rebuilt_v2.db")
    parser.add_argument("--run-id", default="repair_v3_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    parser.add_argument("--normalize-only", action="store_true")
    parser.add_argument("--sources", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--context-size", type=int, default=4)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="re-ask windows a gateway previously rejected",
    )
    parser.add_argument(
        "--era-table",
        choices=("anchor", "full", "off"),
        default="anchor",
        help="era-year reference table: only corpus eras, all known eras, or none",
    )
    args = parser.parse_args()

    run_id = args.run_id
    service = RepairService(
        db_path=args.db,
        concurrency=args.concurrency,
        context_size=args.context_size,
        run_id=run_id,
        retry_failed=args.retry_failed,
        era_table_mode=args.era_table,
    )
    result = service.normalize_entities()
    report = {
        "run_id": run_id,
        "normalization": result,
        "usage": _repair_usage([]),
        "report": service.report(),
    }
    if not args.normalize_only:
        temporal = asyncio.run(
            service.run_temporal_repair(limit_sources=args.sources)
        )
        steps = service.conn.execute(
            "SELECT output FROM v3_repair_steps WHERE run_id=? AND step_type='temporal_resolution'",
            (run_id,),
        ).fetchall()
        usage = _repair_usage([{"output": json.loads(row[0])} for row in steps])
        report["temporal"] = temporal
        report["usage"] = usage
        report["report"] = service.report()
        if args.resume:
            report["resumed_checkpoints"] = service.conn.execute(
                "SELECT COUNT(*) FROM v3_repair_steps WHERE run_id=? AND step_type='temporal_resolution'",
                (run_id,),
            ).fetchone()[0]
    artifact_dir = Path("artifacts")
    artifact_dir.mkdir(exist_ok=True)
    artifact_path = artifact_dir / f"repair_v3_{run_id}.json"
    artifact_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return report


if __name__ == "__main__":
    run_repair_cli()
