from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from .domain import (
    AnnotationSet,
    Claim,
    ClarificationQuestion,
    CompletenessAssessment,
    Conflict,
    EvidenceLink,
    IngestionArtifact,
    IngestionJob,
    NormalizedDocument,
    Passage,
    RawAsset,
    RecognitionResult,
    ReviewTask,
    Source,
)


class KnowledgeStore:
    """SQLite implementation of the repository boundary; PostgreSQL can implement the same API."""

    def __init__(self, path: str = "historybot.db"):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS passages (id TEXT PRIMARY KEY, source_id TEXT NOT NULL, ordinal INTEGER NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS entities (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS entity_aliases (id TEXT PRIMARY KEY, canonical_surface TEXT NOT NULL, alias_surface TEXT NOT NULL, relation TEXT NOT NULL, passage_id TEXT NOT NULL, confidence REAL NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS relations (id TEXT PRIMARY KEY, subject TEXT NOT NULL, predicate TEXT NOT NULL, object TEXT NOT NULL, claim_id TEXT, valid_from TEXT, valid_to TEXT, inference_method TEXT NOT NULL DEFAULT 'explicit', confidence REAL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS timeline_entries (id TEXT PRIMARY KEY, event_id TEXT NOT NULL, start_date TEXT, end_date TEXT, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS place_assertions (id TEXT PRIMARY KEY, event_id TEXT NOT NULL, place TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS claims (id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, claim_id TEXT NOT NULL, passage_id TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS conflicts (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS conflict_candidates (id TEXT PRIMARY KEY, status TEXT NOT NULL, dimension TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS review_feedback (id TEXT PRIMARY KEY, claim_id TEXT NOT NULL, label TEXT NOT NULL, reason TEXT NOT NULL, reviewer TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS evolution_snapshots (id TEXT PRIMARY KEY, policy_version TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, action TEXT NOT NULL, object_id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS ingestion_jobs (id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS raw_assets (id TEXT PRIMARY KEY, content_hash TEXT NOT NULL UNIQUE, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ingestion_artifacts (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, kind TEXT NOT NULL, input_hash TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS recognition_results (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS normalized_documents (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS annotations (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS completeness_assessments (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS clarification_questions (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS review_tasks (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS processing_errors (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, stage TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS agent_runs (id TEXT PRIMARY KEY, status TEXT NOT NULL, question TEXT NOT NULL, ingestion_job_id TEXT, source_id TEXT, passage_count INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL, error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS people_extraction_passages (passage_id TEXT PRIMARY KEY, status TEXT NOT NULL, batch_id TEXT NOT NULL, error TEXT);
        CREATE TABLE IF NOT EXISTS extraction_passages (passage_id TEXT PRIMARY KEY, status TEXT NOT NULL, batch_id TEXT NOT NULL, error TEXT);
        CREATE TABLE IF NOT EXISTS v2_reading_runs (id TEXT PRIMARY KEY, job_id TEXT, status TEXT NOT NULL, pipeline_version TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS v2_reading_steps (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, ordinal INTEGER NOT NULL, step_type TEXT NOT NULL, input_ref TEXT, output TEXT NOT NULL, model TEXT, prompt_version TEXT, status TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS v2_knowledge_drafts (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS v2_evidence_anchors (id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, passage_id TEXT NOT NULL, quote TEXT NOT NULL, locator TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS v2_sync_tasks (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, target TEXT NOT NULL, object_id TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS v2_memories (id TEXT PRIMARY KEY, data TEXT NOT NULL, metadata TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS v2_memory_history (id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS idx_passages_source ON passages(source_id);
        CREATE INDEX IF NOT EXISTS idx_entity_aliases_alias ON entity_aliases(alias_surface);
        CREATE INDEX IF NOT EXISTS idx_entities_surface ON entities(json_extract(payload, '$.surface'));
        CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(json_extract(payload, '$.entity_type'));
        CREATE INDEX IF NOT EXISTS idx_events_dates ON events(json_extract(payload, '$.date_start'), json_extract(payload, '$.date_end'));
        CREATE INDEX IF NOT EXISTS idx_events_place ON events(json_extract(payload, '$.place'));
        CREATE INDEX IF NOT EXISTS idx_relations_subject_predicate ON relations(subject, predicate);
        CREATE INDEX IF NOT EXISTS idx_relations_object ON relations(object);
        CREATE INDEX IF NOT EXISTS idx_timeline_dates ON timeline_entries(start_date, end_date);
        CREATE INDEX IF NOT EXISTS idx_places_place ON place_assertions(place);
        CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
        CREATE INDEX IF NOT EXISTS idx_claims_subject_predicate ON claims(json_extract(payload, '$.subject'), json_extract(payload, '$.predicate'));
        CREATE INDEX IF NOT EXISTS idx_claims_object ON claims(json_extract(payload, '$.object'));
        CREATE INDEX IF NOT EXISTS idx_evidence_claim ON evidence(claim_id);
        CREATE INDEX IF NOT EXISTS idx_evidence_passage ON evidence(passage_id);
        CREATE INDEX IF NOT EXISTS idx_conflict_candidates_status ON conflict_candidates(status);
        CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status);
        CREATE INDEX IF NOT EXISTS idx_agent_runs_ingestion_job ON agent_runs(ingestion_job_id);
        """)
        relation_columns = {row[1] for row in self.db.execute("PRAGMA table_info(relations)").fetchall()}
        for name, definition in {
            "claim_id": "TEXT",
            "valid_from": "TEXT",
            "valid_to": "TEXT",
            "inference_method": "TEXT NOT NULL DEFAULT 'explicit'",
            "confidence": "REAL",
        }.items():
            if name not in relation_columns:
                self.db.execute(f"ALTER TABLE relations ADD COLUMN {name} {definition}")
        # Backfill structured relation columns for databases created before the
        # provenance schema. The JSON payload remains the source of truth for
        # legacy rows, while new writes populate both representations.
        legacy_rows = self.db.execute("SELECT id, payload FROM relations WHERE claim_id IS NULL OR confidence IS NULL").fetchall()
        for row in legacy_rows:
            payload = json.loads(row[1])
            claim_id = payload.get("claim_id")
            claim_row = self.db.execute("SELECT payload FROM claims WHERE id=?", (claim_id,)).fetchone() if claim_id else None
            claim_payload = json.loads(claim_row[0]) if claim_row else {}
            self.db.execute(
                "UPDATE relations SET claim_id=COALESCE(claim_id,?), valid_from=COALESCE(valid_from,?), valid_to=COALESCE(valid_to,?), inference_method=COALESCE(inference_method,?), confidence=COALESCE(confidence,?) WHERE id=?",
                (claim_id, payload.get("valid_from", claim_payload.get("time_from")), payload.get("valid_to", claim_payload.get("time_to")), payload.get("inference_method", claim_payload.get("inference_method", "explicit")), payload.get("confidence", claim_payload.get("confidence", {}).get("score")), row[0]),
            )
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_relations_claim ON relations(claim_id)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_relations_valid_time ON relations(valid_from, valid_to)")
        self.db.commit()

    def _put(self, table: str, key: str, payload: dict, *, status: str | None = None) -> None:
        if table == "sources": sql = "INSERT OR IGNORE INTO sources(id,payload) VALUES(?,?)"
        elif table == "passages": sql = "INSERT OR IGNORE INTO passages(id,source_id,ordinal,payload) VALUES(?,?,?,?)"
        elif table == "entities": sql = "INSERT OR IGNORE INTO entities(id,payload) VALUES(?,?)"
        elif table == "events": sql = "INSERT OR IGNORE INTO events(id,payload) VALUES(?,?)"
        elif table == "claims": sql = "INSERT OR REPLACE INTO claims(id,status,payload,version) VALUES(?,?,?,COALESCE((SELECT version+1 FROM claims WHERE id=?),1))"
        elif table == "evidence": sql = "INSERT OR IGNORE INTO evidence(id,claim_id,passage_id,payload) VALUES(?,?,?,?)"
        elif table == "conflicts": sql = "INSERT OR REPLACE INTO conflicts(id,payload) VALUES(?,?)"
        else: raise ValueError(table)
        data = json.dumps(payload, ensure_ascii=False)
        if table in ("sources", "entities", "events"): args = (key, data)
        elif table == "passages": args = (key, payload["source_id"], payload["ordinal"], data)
        elif table == "claims": args = (key, status or payload["status"], data, key)
        elif table == "evidence": args = (key, payload["claim_id"], payload["passage_id"], data)
        else: args = (key, data)
        self.db.execute(sql, args)
        self.db.commit()

    def save_source(self, source: Source, task_id: str = "system") -> None:
        self._put("sources", source.id, source.model_dump(mode="json")); self.audit(task_id, "source_saved", source.id, {})

    def save_passages(self, passages: Iterable[Passage], task_id: str = "system") -> None:
        for passage in passages: self._put("passages", passage.id, passage.model_dump(mode="json")); self.audit(task_id, "passage_saved", passage.id, {})

    def save_claim(self, claim: Claim, task_id: str = "system") -> None:
        self._put("claims", claim.id, claim.model_dump(mode="json"), status=claim.status.value); self.audit(task_id, "claim_saved", claim.id, {"status": claim.status.value})
        relation_id = f"rel_{claim.id}"
        relation_payload = {"claim_id": claim.id, "subject": claim.subject, "predicate": claim.predicate, "object": claim.object, "passage_ids": claim.passage_ids, "event_id": claim.event_id, "valid_from": claim.time_from, "valid_to": claim.time_to, "time_precision": claim.time_precision, "time_expression": claim.time_expression, "inference_method": claim.inference_method, "confidence": claim.confidence.score, "provenance": {"kind": "claim", "claim_id": claim.id, "passage_ids": claim.passage_ids}}
        self.db.execute("INSERT OR REPLACE INTO relations(id,subject,predicate,object,claim_id,valid_from,valid_to,inference_method,confidence,payload) VALUES(?,?,?,?,?,?,?,?,?,?)", (relation_id, claim.subject, claim.predicate, claim.object, claim.id, claim.time_from, claim.time_to, claim.inference_method, claim.confidence.score, json.dumps(relation_payload, ensure_ascii=False)))
        self.db.commit()

    def save_evidence(self, link: EvidenceLink, task_id: str = "system") -> None:
        self._put("evidence", link.id, link.model_dump(mode="json")); self.audit(task_id, "evidence_saved", link.id, {})

    def save_conflict(self, conflict: Conflict, task_id: str = "system") -> None:
        self._put("conflicts", conflict.id, conflict.model_dump(mode="json")); self.audit(task_id, "conflict_saved", conflict.id, {})

    def save_conflict_candidate(self, candidate: dict, task_id: str = "system") -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO conflict_candidates(id,status,dimension,payload,updated_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP)",
            (candidate["id"], candidate.get("status", "pending"), candidate["dimension"], json.dumps(candidate, ensure_ascii=False)),
        )
        self.db.commit()
        self.audit(task_id, "conflict_candidate_saved", candidate["id"], {"dimension": candidate["dimension"], "status": candidate.get("status", "pending")})

    def get_conflict_candidates(self, status: str | None = None) -> list[dict]:
        sql = "SELECT payload FROM conflict_candidates"
        params: tuple[str, ...] = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id"
        return [json.loads(row[0]) for row in self.db.execute(sql, params).fetchall()]

    def save_feedback(self, feedback_id: str, claim_id: str, label: str, reason: str, reviewer: str, task_id: str = "system") -> None:
        payload = {"id": feedback_id, "claim_id": claim_id, "label": label, "reason": reason, "reviewer": reviewer}
        self.db.execute("INSERT OR REPLACE INTO review_feedback(id,claim_id,label,reason,reviewer,payload) VALUES(?,?,?,?,?,?)", (feedback_id, claim_id, label, reason, reviewer, json.dumps(payload, ensure_ascii=False)))
        self.db.commit(); self.audit(task_id, "feedback_saved", feedback_id, {"claim_id": claim_id, "label": label})

    def save_evolution_snapshot(self, snapshot_id: str, policy_version: str, payload: dict, task_id: str = "system") -> None:
        self.db.execute("INSERT OR REPLACE INTO evolution_snapshots(id,policy_version,payload) VALUES(?,?,?)", (snapshot_id, policy_version, json.dumps(payload, ensure_ascii=False)))
        self.db.commit(); self.audit(task_id, "evolution_snapshot_saved", snapshot_id, {"policy_version": policy_version})

    def save_entity(self, entity_id: str, payload: dict, task_id: str = "system") -> None:
        self._put("entities", entity_id, payload); self.audit(task_id, "entity_candidate_saved", entity_id, {})

    def save_entity_alias(self, alias_id: str, payload: dict, task_id: str = "system") -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO entity_aliases(id,canonical_surface,alias_surface,relation,passage_id,confidence,payload) VALUES(?,?,?,?,?,?,?)",
            (alias_id, payload["canonical_surface"], payload["alias_surface"], payload["relation"], payload["passage_id"], payload["confidence"], json.dumps(payload, ensure_ascii=False)),
        )
        self.db.commit()
        self.audit(task_id, "entity_alias_saved", alias_id, {})

    def save_event(self, event_id: str, payload: dict, task_id: str = "system") -> None:
        payload = self._enrich_event_time(payload)
        self._put("events", event_id, payload)
        # Preserve the exact source passage on every derived dimensional record.
        passage_id = payload.get("passage_id")
        provenance = {"kind": "event", "event_id": event_id, "passage_ids": [passage_id] if passage_id else []}
        if payload.get("date_start") or payload.get("date_end"):
            timeline_payload = {**payload, "provenance": provenance}
            self.db.execute("INSERT OR REPLACE INTO timeline_entries(id,event_id,start_date,end_date,payload) VALUES(?,?,?,?,?)", (f"tl_{event_id}", event_id, payload.get("date_start"), payload.get("date_end"), json.dumps(timeline_payload, ensure_ascii=False)))
            self.db.commit()
        if payload.get("place"):
            place_payload = {**payload, "provenance": provenance}
            self.db.execute("INSERT OR REPLACE INTO place_assertions(id,event_id,place,payload) VALUES(?,?,?,?)", (f"place_{event_id}", event_id, payload["place"], json.dumps(place_payload, ensure_ascii=False)))
            self.db.commit()
        self.audit(task_id, "event_candidate_saved", event_id, {})

    @staticmethod
    def _enrich_event_time(payload: dict) -> dict:
        """Add deterministic precision/sort metadata without inventing years."""
        start = payload.get("date_start")
        end = payload.get("date_end")
        value = start or end
        if not value:
            precision = "unknown"
            sort_key = "9:"
        elif re.fullmatch(r"\d{1,4}", str(value)):
            precision = "year" if len(str(value)) == 4 else "partial_numeric"
            sort_key = f"0:{int(value):04d}"
        elif re.search(r"年", str(value)):
            precision = "year_or_era"
            sort_key = f"1:{value}"
        elif re.search(r"月|正月|闰月", str(value)):
            precision = "month_without_year"
            sort_key = f"2:{value}"
        else:
            precision = "text"
            sort_key = f"3:{value}"
        return {**payload, "date_precision": payload.get("date_precision", precision), "date_sort_key": payload.get("date_sort_key", sort_key), "date_expression": payload.get("date_expression", value)}

    def get_passages(self, source_id: str | None = None) -> list[Passage]:
        rows = self.db.execute("SELECT payload FROM passages WHERE source_id=? ORDER BY ordinal", (source_id,)).fetchall() if source_id else self.db.execute("SELECT payload FROM passages ORDER BY source_id,ordinal").fetchall()
        return [Passage.model_validate_json(row[0]) for row in rows]

    def get_sources(self) -> list[Source]:
        return [Source.model_validate_json(row[0]) for row in self.db.execute("SELECT payload FROM sources ORDER BY id").fetchall()]

    def get_source(self, source_id: str) -> Source | None:
        row = self.db.execute("SELECT payload FROM sources WHERE id=?", (source_id,)).fetchone()
        return Source.model_validate_json(row[0]) if row else None

    def get_source_for_ingestion_job(self, job_id: str) -> Source | None:
        """Resolve the source created by the controlled ingestion pipeline."""
        row = self.db.execute(
            "SELECT payload FROM sources WHERE json_extract(payload, '$.metadata.ingestion_job_id')=? ORDER BY id LIMIT 1",
            (job_id,),
        ).fetchone()
        return Source.model_validate_json(row[0]) if row else None

    def get_claims(self) -> list[Claim]:
        return [Claim.model_validate_json(row[0]) for row in self.db.execute("SELECT payload FROM claims ORDER BY id").fetchall()]

    def audit(self, task_id: str, action: str, object_id: str, payload: dict) -> None:
        self.db.execute("INSERT INTO audit_log(task_id,action,object_id,payload) VALUES(?,?,?,?)", (task_id, action, object_id, json.dumps(payload, ensure_ascii=False))); self.db.commit()

    def save_ingestion_job(self, job: IngestionJob) -> None:
        self.db.execute("INSERT OR REPLACE INTO ingestion_jobs(id,status,payload,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP)", (job.id, job.status.value, job.model_dump_json()))
        self.db.commit()

    def get_ingestion_job(self, job_id: str) -> IngestionJob | None:
        row = self.db.execute("SELECT payload FROM ingestion_jobs WHERE id=?", (job_id,)).fetchone()
        return IngestionJob.model_validate_json(row[0]) if row else None

    def save_agent_run(
        self,
        run_id: str,
        *,
        status: str,
        question: str,
        payload: dict,
        ingestion_job_id: str | None = None,
        source_id: str | None = None,
        passage_count: int = 0,
        error: str | None = None,
    ) -> None:
        """Persist the orchestration boundary separately from extracted facts."""
        self.db.execute(
            """
            INSERT OR REPLACE INTO agent_runs
                (id,status,question,ingestion_job_id,source_id,passage_count,payload,error,updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                run_id,
                status,
                question,
                ingestion_job_id,
                source_id,
                passage_count,
                json.dumps(payload, ensure_ascii=False),
                error,
            ),
        )
        self.db.commit()
        self.audit(run_id, "agent_run_saved", run_id, {"status": status})

    def get_agent_run(self, run_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT id,status,question,ingestion_job_id,source_id,passage_count,payload,error,created_at,updated_at FROM agent_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def get_agent_runs(self, *, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            "SELECT id,status,question,ingestion_job_id,source_id,passage_count,payload,error,created_at,updated_at FROM agent_runs ORDER BY updated_at DESC, id DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def save_raw_asset(self, asset: RawAsset) -> None:
        self.db.execute("INSERT OR IGNORE INTO raw_assets(id,content_hash,payload) VALUES(?,?,?)", (asset.id, asset.content_hash, asset.model_dump_json()))
        self.db.commit()

    def get_raw_asset(self, asset_id: str) -> RawAsset | None:
        row = self.db.execute("SELECT payload FROM raw_assets WHERE id=?", (asset_id,)).fetchone()
        return RawAsset.model_validate_json(row[0]) if row else None

    def save_processing_error(self, error_id: str, job_id: str, stage: str, payload: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO processing_errors(id,job_id,stage,payload) VALUES(?,?,?,?)",
            (error_id, job_id, stage, json.dumps(payload, ensure_ascii=False)),
        )
        self.db.commit()

    def save_ingestion_artifact(self, artifact: IngestionArtifact) -> None:
        self.db.execute("INSERT OR REPLACE INTO ingestion_artifacts(id,job_id,kind,input_hash,payload) VALUES(?,?,?,?,?)", (artifact.id, artifact.job_id, artifact.kind, artifact.input_hash, artifact.model_dump_json()))
        self.db.commit()

    def save_v2_reading_run(self, run_id: str, *, job_id: str | None, status: str, payload: dict, pipeline_version: str = "v2") -> None:
        self.db.execute("INSERT OR REPLACE INTO v2_reading_runs(id,job_id,status,pipeline_version,payload,updated_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)", (run_id, job_id, status, pipeline_version, json.dumps(payload, ensure_ascii=False)))
        self.db.commit()

    def save_v2_step(self, step_id: str, *, run_id: str, ordinal: int, step_type: str, output: dict, input_ref: str | None = None, model: str | None = None, prompt_version: str | None = None, status: str = "complete") -> None:
        self.db.execute("INSERT OR REPLACE INTO v2_reading_steps(id,run_id,ordinal,step_type,input_ref,output,model,prompt_version,status) VALUES(?,?,?,?,?,?,?,?,?)", (step_id, run_id, ordinal, step_type, input_ref, json.dumps(output, ensure_ascii=False), model, prompt_version, status))
        self.db.commit()

    def save_v2_draft(self, draft_id: str, *, run_id: str, kind: str, payload: dict, status: str = "draft") -> None:
        self.db.execute("INSERT OR REPLACE INTO v2_knowledge_drafts(id,run_id,kind,payload,status,updated_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)", (draft_id, run_id, kind, json.dumps(payload, ensure_ascii=False), status))
        self.db.commit()

    def save_v2_anchor(self, anchor_id: str, *, draft_id: str, passage_id: str, quote: str, locator: dict, verified: bool, payload: dict | None = None) -> None:
        self.db.execute("INSERT OR REPLACE INTO v2_evidence_anchors(id,draft_id,passage_id,quote,locator,verified,payload) VALUES(?,?,?,?,?,?,?)", (anchor_id, draft_id, passage_id, quote, json.dumps(locator, ensure_ascii=False), int(verified), json.dumps(payload or {}, ensure_ascii=False)))
        self.db.commit()

    def save_v2_sync_task(self, task_id: str, *, run_id: str, target: str, object_id: str, payload: dict, status: str = "pending") -> None:
        self.db.execute("INSERT OR IGNORE INTO v2_sync_tasks(id,run_id,target,object_id,status,payload) VALUES(?,?,?,?,?,?)", (task_id, run_id, target, object_id, status, json.dumps(payload, ensure_ascii=False)))
        self.db.commit()

    def get_v2_steps(self, run_id: str) -> list[dict]:
        return [{"id": r[0], "ordinal": r[1], "step_type": r[2], "input_ref": r[3], "output": json.loads(r[4]), "model": r[5], "prompt_version": r[6], "status": r[7]} for r in self.db.execute("SELECT id,ordinal,step_type,input_ref,output,model,prompt_version,status FROM v2_reading_steps WHERE run_id=? ORDER BY ordinal", (run_id,)).fetchall()]

    def get_v2_step(self, step_id: str) -> dict | None:
        row = self.db.execute("SELECT id,ordinal,step_type,input_ref,output,model,prompt_version,status FROM v2_reading_steps WHERE id=?", (step_id,)).fetchone()
        if not row:
            return None
        try:
            output = json.loads(row[4])
        except Exception:
            output = {}
        return {"id": row[0], "ordinal": row[1], "step_type": row[2], "input_ref": row[3], "output": output, "model": row[5], "prompt_version": row[6], "status": row[7]}

    def get_v2_drafts(self, run_id: str) -> list[dict]:
        return [{"id": r[0], "kind": r[1], "payload": json.loads(r[2]), "status": r[3]} for r in self.db.execute("SELECT id,kind,payload,status FROM v2_knowledge_drafts WHERE run_id=? ORDER BY id", (run_id,)).fetchall()]

    def save_recognition(self, job_id: str, result: RecognitionResult) -> None:
        self.db.execute("INSERT OR REPLACE INTO recognition_results(id,job_id,payload) VALUES(?,?,?)", (result.id, job_id, result.model_dump_json())); self.db.commit()

    def save_normalized(self, job_id: str, document: NormalizedDocument) -> None:
        self.db.execute("INSERT OR REPLACE INTO normalized_documents(id,job_id,payload) VALUES(?,?,?)", (document.id, job_id, document.model_dump_json())); self.db.commit()

    def save_annotations(self, job_id: str, annotations: AnnotationSet) -> None:
        self.db.execute("INSERT OR REPLACE INTO annotations(id,job_id,payload) VALUES(?,?,?)", (annotations.id, job_id, annotations.model_dump_json())); self.db.commit()

    def save_assessment(self, job_id: str, assessment: CompletenessAssessment) -> None:
        self.db.execute("INSERT OR REPLACE INTO completeness_assessments(id,job_id,payload) VALUES(?,?,?)", (assessment.id, job_id, assessment.model_dump_json())); self.db.commit()

    def save_question(self, question: ClarificationQuestion) -> None:
        self.db.execute("INSERT OR REPLACE INTO clarification_questions(id,job_id,status,payload) VALUES(?,?,?,?)", (question.id, question.job_id, question.status, question.model_dump_json())); self.db.commit()

    def get_questions(self, job_id: str, status: str | None = None) -> list[ClarificationQuestion]:
        sql = "SELECT payload FROM clarification_questions WHERE job_id=?" + (" AND status=?" if status else "") + " ORDER BY id"
        args = (job_id, status) if status else (job_id,)
        return [ClarificationQuestion.model_validate_json(row[0]) for row in self.db.execute(sql, args).fetchall()]

    def get_annotations(self, job_id: str) -> AnnotationSet | None:
        row = self.db.execute("SELECT payload FROM annotations WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        return AnnotationSet.model_validate_json(row[0]) if row else None

    def storage_status(self, job_id: str | None = None) -> dict:
        """Return a database contract check for ingestion writes and indexes.

        The query is intentionally read-only so the API can expose it after a
        write without changing the database. Counts are scoped to a job where
        the schema carries a job id; source/passages are resolved through the
        deterministic source id created by ``IngestionService``.
        """
        tables = (
            "raw_assets",
            "ingestion_jobs",
            "recognition_results",
            "normalized_documents",
            "annotations",
            "completeness_assessments",
            "ingestion_artifacts",
            "clarification_questions",
            "review_tasks",
            "sources",
            "passages",
        )
        counts: dict[str, int] = {}
        job_tables = {
            "recognition_results",
            "normalized_documents",
            "annotations",
            "completeness_assessments",
            "ingestion_artifacts",
            "clarification_questions",
            "review_tasks",
        }
        source_id = f"src_{job_id[4:]}" if job_id and job_id.startswith("job_") else None
        for table in tables:
            if job_id in (None, ""):
                counts[table] = self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            elif table == "ingestion_jobs":
                counts[table] = self.db.execute("SELECT COUNT(*) FROM ingestion_jobs WHERE id=?", (job_id,)).fetchone()[0]
            elif table in job_tables:
                counts[table] = self.db.execute(f"SELECT COUNT(*) FROM {table} WHERE job_id=?", (job_id,)).fetchone()[0]
            elif table == "raw_assets":
                job = self.get_ingestion_job(job_id)
                raw_asset_id = job.raw_asset_id if job else None
                counts[table] = self.db.execute("SELECT COUNT(*) FROM raw_assets WHERE id=?", (raw_asset_id,)).fetchone()[0]
            elif table == "sources":
                counts[table] = self.db.execute("SELECT COUNT(*) FROM sources WHERE id=?", (source_id,)).fetchone()[0]
            else:
                counts[table] = self.db.execute("SELECT COUNT(*) FROM passages WHERE source_id=?", (source_id,)).fetchone()[0]

        expected_indexes = (
            "idx_passages_source",
            "idx_entities_surface",
            "idx_entities_type",
            "idx_events_dates",
            "idx_events_place",
            "idx_relations_subject_predicate",
            "idx_relations_object",
            "idx_timeline_dates",
            "idx_places_place",
            "idx_claims_status",
            "idx_claims_subject_predicate",
            "idx_claims_object",
            "idx_evidence_claim",
            "idx_evidence_passage",
            "idx_relations_claim",
            "idx_relations_valid_time",
            "idx_entity_aliases_alias",
        )
        present_indexes = {
            row[0]
            for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        indexes = {name: name in present_indexes for name in expected_indexes}
        index_usage: dict[str, bool] = {}
        if source_id:
            plan = self.db.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM passages WHERE source_id=? ORDER BY ordinal",
                (source_id,),
            ).fetchall()
            index_usage["idx_passages_source"] = any("idx_passages_source" in row[3] for row in plan)
        return {
            "database": str(self.path),
            "job_id": job_id,
            "counts": counts,
            "indexes": indexes,
            "index_usage": index_usage,
            "schema_ready": all(indexes.values()),
            "write_ready": all(counts[name] > 0 for name in ("ingestion_jobs", "recognition_results", "normalized_documents", "annotations")) if job_id else True,
        }

    def save_review_task(self, task: ReviewTask) -> None:
        self.db.execute("INSERT OR REPLACE INTO review_tasks(id,job_id,status,payload) VALUES(?,?,?,?)", (task.id, task.job_id, task.status, task.model_dump_json())); self.db.commit()

    def counts(self) -> dict[str, int]:
        return {table: self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("sources", "passages", "entities", "entity_aliases", "events", "relations", "timeline_entries", "place_assertions", "claims", "evidence", "conflicts", "conflict_candidates", "review_feedback", "evolution_snapshots", "audit_log")}

    def graph_snapshot(self) -> dict:
        def payloads(table: str) -> list[dict]:
            return [json.loads(row[0]) for row in self.db.execute(f"SELECT payload FROM {table} ORDER BY id").fetchall()]

        claims = [item.model_dump(mode="json") for item in self.get_claims()]
        claim_by_id = {item["id"]: item for item in claims}
        relations = payloads("relations")
        # Backfill provenance for databases created before the explicit provenance field.
        for relation in relations:
            claim = claim_by_id.get(relation.get("claim_id"))
            if claim:
                relation.setdefault("passage_ids", claim.get("passage_ids", []))
                relation.setdefault("event_id", claim.get("event_id"))
                relation.setdefault("provenance", {"kind": "claim", "claim_id": claim["id"], "passage_ids": claim.get("passage_ids", [])})

        return {
            "sources": [item.model_dump(mode="json") for item in self.get_sources()],
            "entities": payloads("entities"),
            "events": payloads("events"),
            "relations": relations,
            "timeline": payloads("timeline_entries"),
            "places": payloads("place_assertions"),
            "claims": claims,
        }
