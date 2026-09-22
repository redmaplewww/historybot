from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from collections import Counter, defaultdict
from contextlib import contextmanager
from itertools import combinations
from pathlib import Path
from threading import RLock
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles

from .agent import HistoryResearchAgent
from .entity_resolution import (
    build_identity_index,
    canonical_name_is_plausible,
    is_name_entity_type,
)
from .ingestion import IngestionService
from .ai_ingestion import AIIngestionService
from .model_gateway import ModelGateway
from .store import KnowledgeStore
from .text_normalization import normalize_surface

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "data" / "three_kingdoms_rebuilt_v2.db"
DEFAULT_STATIC = PROJECT_ROOT / "frontend" / "dist"
GRAPH_PROJECTION_VERSION = "identity-alias-v2"


class ResearchView:
    """Read-only projections for the research workspace."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._graph_cache: dict[int, dict[str, Any]] = {}
        self._workspace_cache: dict[int, dict[str, Any]] = {}
        self._summary_cache: dict[int, dict[str, Any]] = {}
        self._database_signature = None
        self._cache_lock = RLock()
        self._graph_cache_path = self.db_path.with_suffix(".graph_projection.json")

    @contextmanager
    def _connect(self):
        if not self.db_path.exists():
            raise FileNotFoundError(self.db_path)
        connection = sqlite3.connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN")
            yield connection
        finally:
            connection.close()

    def _version(self, connection: sqlite3.Connection) -> int:
        signature = []
        for path in (self.db_path, Path(f"{self.db_path}-wal")):
            try:
                stat = path.stat()
                signature.append((stat.st_mtime_ns, stat.st_size))
            except FileNotFoundError:
                signature.append(None)
        with self._cache_lock:
            if signature != self._database_signature:
                self._graph_cache.clear()
                self._workspace_cache.clear()
                self._summary_cache.clear()
                self._database_signature = signature
        return int(connection.execute("SELECT COALESCE(MAX(id), 0) FROM audit_log").fetchone()[0])

    @staticmethod
    def _payloads(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
        rows = connection.execute(f"SELECT payload FROM {table} ORDER BY id").fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def _graph_projection(self, connection: sqlite3.Connection, version: int) -> dict[str, Any]:
        cached = self._graph_cache.get(version)
        if cached is not None:
            return cached
        try:
            sidecar = json.loads(self._graph_cache_path.read_text(encoding="utf-8"))
            saved_signature = sidecar.get("database_signature")
            current_signature = [list(item) if item is not None else None for item in self._database_signature]
            if (
                saved_signature == current_signature
                and sidecar.get("version") == version
                and sidecar.get("projection_version") == GRAPH_PROJECTION_VERSION
            ):
                graph = sidecar["graph"]
                self._graph_cache = {version: graph}
                return graph
        except (FileNotFoundError, OSError, json.JSONDecodeError, KeyError, TypeError):
            pass
        entities = self._payloads(connection, "entities")
        aliases = self._payloads(connection, "entity_aliases")
        events = self._payloads(connection, "events")
        claims = self._payloads(connection, "claims")
        graph = self._build_graph(entities, events, claims, aliases)
        self._graph_cache = {version: graph}
        try:
            self._graph_cache_path.write_text(
                json.dumps(
                    {
                        "database_signature": self._database_signature,
                        "version": version,
                        "projection_version": GRAPH_PROJECTION_VERSION,
                        "graph": graph,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
                newline="\n",
            )
        except OSError:
            pass
        return graph

    def _summary(self, connection: sqlite3.Connection, version: int) -> dict[str, Any]:
        cached = self._summary_cache.get(version)
        if cached is not None:
            return cached
        total_passages = connection.execute("SELECT COUNT(*) FROM passages").fetchone()[0]
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "sources", "passages", "entities", "entity_aliases", "events", "relations",
                "timeline_entries", "place_assertions", "claims", "evidence", "conflicts",
                "conflict_candidates",
            )
        }
        graph = self._graph_projection(connection, version)
        counts["person_nodes"] = sum(node["kind"] == "person" for node in graph["nodes"])
        claims = self._payloads(connection, "claims")
        sources = self._payloads(connection, "sources")
        evolution = self._payloads(connection, "evolution_snapshots")
        evidence_count = connection.execute("SELECT COUNT(DISTINCT claim_id) FROM evidence").fetchone()[0]
        status_counts = Counter(claim["status"] for claim in claims)
        kind_counts = Counter(source["kind"] for source in sources)
        resolution_rows = connection.execute(
            "SELECT COALESCE(json_extract(payload, '$.resolution.status'), 'unreviewed'), COUNT(*) FROM entities GROUP BY 1"
        ).fetchall()
        extraction = self._extraction_status(connection, total_passages)
        confidence_values = [claim["confidence"]["score"] for claim in claims]
        result = {
            "counts": counts,
            "status_counts": dict(status_counts),
            "source_kind_counts": dict(kind_counts),
            "average_confidence": sum(confidence_values) / len(confidence_values) if confidence_values else 0,
            "evidence_coverage": evidence_count / len(claims) if claims else 0,
            "latest_evolution": evolution[-1] if evolution else None,
            "entity_resolution": dict(resolution_rows),
            "entity_aliases": counts["entity_aliases"],
            "data_version": (
                f"audit:{version}:people:{extraction['people'].get('ledger_version', 0)}"
                f":people_complete:{extraction['people'].get('completed_passages', 0)}"
                f":people_failed:{extraction['people'].get('failed_batches', 0)}"
                f":evidence:{extraction['evidence'].get('ledger_version', 0)}"
                f":evidence_complete:{extraction['evidence'].get('completed_passages', 0)}"
                f":evidence_failed:{extraction['evidence'].get('failed_batches', 0)}"
            ),
            "extraction": extraction,
        }
        self._summary_cache = {version: result}
        return result

    def workspace(self, *, include_graph: bool = True, include_dimensions: bool = True) -> dict[str, Any]:
        with self._connect() as connection:
            version = self._version(connection)
            if not include_dimensions:
                summary = self._summary(connection, version)
                return {
                    "overview": summary,
                    "graph": self._graph_projection(connection, version) if include_graph else {"nodes": [], "edges": []},
                    "timeline": [], "places": [], "claims": [], "sources": [], "conflicts": [],
                    "conflict_candidates": self._payloads(connection, "conflict_candidates"),
                }
            cached = self._workspace_cache.get(version)
            if cached is not None:
                return cached if include_graph else {**cached, "graph": {"nodes": [], "edges": []}}

        with self._connect() as connection:
            sources = self._payloads(connection, "sources")
            passages = self._payloads(connection, "passages")
            entities = self._payloads(connection, "entities")
            aliases = self._payloads(connection, "entity_aliases")
            events = self._payloads(connection, "events")
            claims = self._payloads(connection, "claims")
            evidence = self._payloads(connection, "evidence")
            conflicts = self._payloads(connection, "conflicts")
            conflict_candidates = self._payloads(connection, "conflict_candidates")
            evolution = self._payloads(connection, "evolution_snapshots")
            extraction = self._extraction_status(connection, len(passages))
            audit_version = self._version(connection)
            counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "sources",
                    "passages",
                    "entities",
                    "entity_aliases",
                    "events",
                    "relations",
                    "timeline_entries",
                    "place_assertions",
                    "claims",
                    "evidence",
                    "conflicts",
                    "conflict_candidates",
                )
            }

        source_by_id = {item["id"]: item for item in sources}
        passage_by_id = {item["id"]: item for item in passages}
        evidence_by_claim: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for link in evidence:
            passage = passage_by_id.get(link["passage_id"], {})
            source = source_by_id.get(passage.get("source_id"), {})
            evidence_by_claim[link["claim_id"]].append(
                {
                    **link,
                    "passage_text": passage.get("text"),
                    "locator": passage.get("locator", {}),
                    "source": {
                        "id": source.get("id"),
                        "title": source.get("title", "未知来源"),
                        "author": source.get("author"),
                        "kind": source.get("kind", "unknown"),
                        "url": source.get("url"),
                        "assessment": source.get("metadata", {}).get("assessment", {}),
                    },
                }
            )

        enriched_claims = [
            {**claim, "evidence": evidence_by_claim.get(claim["id"], [])} for claim in claims
        ]
        graph = self._build_graph(entities, events, enriched_claims, aliases)
        counts["person_nodes"] = sum(node["kind"] == "person" for node in graph["nodes"])
        timeline = self._build_timeline(events, enriched_claims, passage_by_id, source_by_id)
        places = self._build_places(events, timeline)
        source_views = self._build_sources(sources, passages, enriched_claims)

        status_counts = Counter(claim["status"] for claim in claims)
        kind_counts = Counter(source["kind"] for source in sources)
        resolution_counts = Counter(
            entity.get("resolution", {}).get("status", "unreviewed")
            for entity in entities
        )
        confidence_values = [claim["confidence"]["score"] for claim in claims]
        overview = {
            "counts": counts,
            "status_counts": dict(status_counts),
            "source_kind_counts": dict(kind_counts),
            "average_confidence": (
                sum(confidence_values) / len(confidence_values) if confidence_values else 0
            ),
            "evidence_coverage": (
                sum(bool(evidence_by_claim.get(claim["id"])) for claim in claims) / len(claims)
                if claims
                else 0
            ),
            "latest_evolution": evolution[-1] if evolution else None,
            "entity_resolution": dict(resolution_counts),
            "entity_aliases": len(aliases),
            "data_version": (
                f"audit:{audit_version}:people:{extraction['people'].get('ledger_version', 0)}"
                f":people_complete:{extraction['people'].get('completed_passages', 0)}"
                f":people_failed:{extraction['people'].get('failed_batches', 0)}"
                f":evidence:{extraction['evidence'].get('ledger_version', 0)}"
                f":evidence_complete:{extraction['evidence'].get('completed_passages', 0)}"
                f":evidence_failed:{extraction['evidence'].get('failed_batches', 0)}"
            ),
            "extraction": extraction,
        }
        result = {
            "overview": overview,
            "graph": graph,
            "timeline": timeline,
            "places": places,
            "claims": enriched_claims,
            "sources": source_views,
            "conflicts": conflicts,
            "conflict_candidates": conflict_candidates,
        }
        self._workspace_cache = {audit_version: result}
        return result if include_graph else {**result, "graph": {"nodes": [], "edges": []}}

    def dimensions(self) -> dict[str, Any]:
        """Build non-graph dimensions using only event/evidence-linked passages."""
        with self._connect() as connection:
            sources = self._payloads(connection, "sources")
            events = self._payloads(connection, "events")
            claims = self._payloads(connection, "claims")
            evidence = self._payloads(connection, "evidence")
            passage_ids = {item.get("passage_id") for item in events if item.get("passage_id")}
            passage_ids.update(item.get("passage_id") for item in evidence if item.get("passage_id"))
            passages = {
                row["id"]: json.loads(row["payload"])
                for row in connection.execute(
                    "SELECT id,payload FROM passages WHERE id IN ({})".format(",".join("?" for _ in passage_ids)),
                    tuple(passage_ids),
                ).fetchall()
            } if passage_ids else {}
            source_by_id = {item["id"]: item for item in sources}
            evidence_by_claim: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for link in evidence:
                passage = passages.get(link["passage_id"], {})
                source = source_by_id.get(passage.get("source_id"), {})
                evidence_by_claim[link["claim_id"]].append({
                    **link,
                    "passage_text": passage.get("text"),
                    "locator": passage.get("locator", {}),
                    "source": {"id": source.get("id"), "title": source.get("title", "未知来源"), "author": source.get("author"), "kind": source.get("kind", "unknown"), "url": source.get("url"), "assessment": source.get("metadata", {}).get("assessment", {})},
                })
            enriched_claims = [{**claim, "evidence": evidence_by_claim.get(claim["id"], [])} for claim in claims]
            timeline = self._build_timeline(events, enriched_claims, passages, source_by_id)
            places = self._build_places(events, timeline)
            source_views = self._build_sources(sources, list(passages.values()), enriched_claims)
            passage_counts = dict(connection.execute("SELECT source_id,COUNT(*) FROM passages GROUP BY source_id"))
            for source_view in source_views:
                source_view["passage_count"] = passage_counts.get(source_view["id"], 0)
            return {"timeline": timeline, "places": places, "claims": enriched_claims, "sources": source_views}

    @staticmethod
    def _extraction_status(connection: sqlite3.Connection, total_passages: int) -> dict[str, Any]:
        people = ResearchView._ledger_status(
            connection, "people_extraction_batches", "people_extraction_passages", total_passages
        )
        evidence = ResearchView._ledger_status(
            connection, "extraction_batches", "extraction_passages", total_passages
        )
        if people["completed_passages"] < total_passages:
            active_scope = "people"
        elif evidence["completed_passages"] < total_passages:
            active_scope = "evidence"
        else:
            active_scope = "complete"
        return {
            **people,
            "active_scope": active_scope,
            "people": people,
            "evidence": evidence,
        }

    @staticmethod
    def _ledger_status(
        connection: sqlite3.Connection,
        batch_table: str,
        passage_table: str,
        total_passages: int,
    ) -> dict[str, Any]:
        try:
            rows = connection.execute(
                f"SELECT status, passage_ids FROM {batch_table}"
            ).fetchall()
        except sqlite3.OperationalError:
            return {
                "scope": "people" if batch_table.startswith("people_") else "evidence",
                "total_passages": total_passages,
                "completed_passages": 0,
                "failed_batches": 0,
                "ledger_version": 0,
            }
        try:
            passage_rows = connection.execute(
                f"SELECT passage_id FROM {passage_table} WHERE status='complete'"
            ).fetchall()
        except sqlite3.OperationalError:
            passage_rows = []
        completed = {row[0] for row in passage_rows}
        if not completed:
            completed = {
                passage_id
                for row in rows
                if row[0] == "complete"
                for passage_id in json.loads(row[1])
            }
        return {
            "scope": "people" if batch_table.startswith("people_") else "evidence",
            "total_passages": total_passages,
            "completed_passages": len(completed),
            "failed_batches": sum(
                row[0] == "failed"
                and any(passage_id not in completed for passage_id in json.loads(row[1]))
                for row in rows
            ),
            "coverage": len(completed) / total_passages if total_passages else 0,
            "ledger_version": max(
                connection.execute(
                    f"SELECT COALESCE(MAX(rowid), 0) FROM {batch_table}"
                ).fetchone()[0],
                connection.execute(
                    f"SELECT COALESCE(MAX(rowid), 0) FROM {passage_table}"
                ).fetchone()[0]
                if passage_rows
                else 0,
            ),
        }

    @staticmethod
    def _build_graph(
        entities: list[dict[str, Any]],
        events: list[dict[str, Any]],
        claims: list[dict[str, Any]],
        aliases: list[dict[str, Any]] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        identity_index = build_identity_index(entities, aliases, claims)
        alias_map = identity_index["alias_map"]

        def canonical_surface(value: str) -> str:
            current = normalize_surface(value)
            seen: set[str] = set()
            while current in alias_map and current not in seen:
                seen.add(current)
                current = alias_map[current]
            return current

        mentions: dict[str, list[dict[str, Any]]] = defaultdict(list)
        person_candidates: set[str] = set()
        for entity in entities:
            resolution_status = entity.get("resolution", {}).get("status")
            if resolution_status == "rejected":
                continue
            original_surface = entity["surface"].strip()
            surface = canonical_surface(original_surface)
            mentions[surface].append({**entity, "surface": surface, "original_surface": original_surface})
            entity_type = entity.get("entity_type", "")
            if (
                canonical_name_is_plausible(surface)
                and is_name_entity_type(entity_type)
                and resolution_status != "rejected"
            ):
                person_candidates.add(surface)
        person_candidates.update(identity_index["canonical_surfaces"])
        person_candidates.update(alias_map.values())

        node_claims: dict[str, list[str]] = defaultdict(list)
        node_events: dict[str, list[str]] = defaultdict(list)
        labels: set[str] = set(mentions)
        edges: list[dict[str, Any]] = []

        for claim in claims:
            subject = canonical_surface(claim["subject"])
            object_ = canonical_surface(claim["object"])
            if subject == object_ and normalize_surface(claim.get("predicate", "")) in {"字", "号", "谥号", "曾用名", "本名", "讳", "姓名", "名"}:
                continue
            labels.update((subject, object_))
            node_claims[subject].append(claim["id"])
            node_claims[object_].append(claim["id"])
            edges.append(
                {
                    "id": f"claim:{claim['id']}",
                    "source": subject,
                    "target": object_,
                    "label": claim["predicate"],
                    "edge_type": "claim",
                    "claim_id": claim["id"],
                    "status": claim["status"],
                    "confidence": claim["confidence"]["score"],
                }
            )

        for event in events:
            participants = list(dict.fromkeys(canonical_surface(item) for item in event.get("participant_surfaces", [])))
            labels.update(participants)
            for participant in participants:
                node_events[participant].append(event["id"])
            for source, target in combinations(participants, 2):
                edges.append(
                    {
                        "id": f"event:{event['id']}:{source}:{target}",
                        "source": source,
                        "target": target,
                        "label": event["event_type"],
                        "edge_type": "event",
                        "event_id": event["id"],
                        "status": "event",
                        "confidence": event["confidence"],
                    }
                )

        nodes = []
        for label in sorted(labels):
            entity_mentions = mentions.get(label, [])
            entity_types = sorted({item["entity_type"] for item in entity_mentions})
            if label in person_candidates:
                kind = "person"
            elif any("地点" in item or "空间" in item for item in entity_types):
                kind = "place"
            elif any("时间" in item for item in entity_types):
                kind = "time"
            elif any("官职" in item or "爵位" in item for item in entity_types):
                kind = "office"
            else:
                kind = "concept"
            confidences = [item["confidence"] for item in entity_mentions]
            nodes.append(
                {
                    "id": label,
                    "label": label,
                    "kind": kind,
                    "entity_types": entity_types,
                    "aliases": sorted(
                        {
                            item.get("original_surface", item["surface"])
                            for item in mentions.get(label, [])
                            if item.get("original_surface", item["surface"]) != label
                        }
                        | {alias for alias, canonical in alias_map.items() if canonical == label}
                    ),
                    "mention_count": len(entity_mentions),
                    "claim_ids": node_claims.get(label, []),
                    "event_ids": node_events.get(label, []),
                    "confidence": sum(confidences) / len(confidences) if confidences else None,
                }
            )
        return {"nodes": nodes, "edges": edges}

    @staticmethod
    def _build_timeline(
        events: list[dict[str, Any]],
        claims: list[dict[str, Any]],
        passages: dict[str, dict[str, Any]],
        sources: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        claims_by_event: dict[str, list[str]] = defaultdict(list)
        for claim in claims:
            if claim.get("event_id"):
                claims_by_event[claim["event_id"]].append(claim["id"])

        timeline = []
        for event in events:
            passage = passages.get(event.get("passage_id"), {})
            source = sources.get(passage.get("source_id"), {})
            timeline.append(
                {
                    **event,
                    "claim_ids": claims_by_event.get(event["id"], []),
                    "source": {
                        "id": source.get("id"),
                        "title": source.get("title", "未知来源"),
                        "kind": source.get("kind", "unknown"),
                    },
                    "locator": passage.get("locator", {}),
                }
            )
        return sorted(timeline, key=lambda item: (item.get("date_sort_key") is None, item.get("date_sort_key") or "9:", item["id"]))

    @staticmethod
    def _build_places(
        events: list[dict[str, Any]], timeline: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        timeline_by_id = {item["id"]: item for item in timeline}
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            if event.get("place"):
                grouped[event["place"]].append(timeline_by_id[event["id"]])
        return [
            {
                "name": place,
                "event_count": len(items),
                "participants": sorted(
                    {participant for item in items for participant in item["participant_surfaces"]}
                ),
                "events": items,
            }
            for place, items in sorted(grouped.items())
        ]

    @staticmethod
    def _build_sources(
        sources: list[dict[str, Any]],
        passages: list[dict[str, Any]],
        claims: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        passage_counts = Counter(item["source_id"] for item in passages)
        claim_counts: Counter[str] = Counter()
        disputed_counts: Counter[str] = Counter()
        for claim in claims:
            source_ids = {
                link["source"]["id"] for link in claim["evidence"] if link["source"]["id"]
            }
            for source_id in source_ids:
                claim_counts[source_id] += 1
                if claim["status"] == "disputed":
                    disputed_counts[source_id] += 1
        return [
            {
                **source,
                "passage_count": passage_counts[source["id"]],
                "claim_count": claim_counts[source["id"]],
                "disputed_count": disputed_counts[source["id"]],
            }
            for source in sources
        ]


def create_app(
    db_path: str | Path | None = None, static_dir: str | Path | None = None
) -> FastAPI:
    selected_db = Path(db_path or os.getenv("HISTORYBOT_DB", DEFAULT_DB))
    selected_static = Path(static_dir or os.getenv("HISTORYBOT_STATIC", DEFAULT_STATIC))
    view = ResearchView(selected_db)
    ingestion_store = KnowledgeStore(str(selected_db))
    ingestion_service = IngestionService(ingestion_store)
    ai_ingestion_service = AIIngestionService(ingestion_store)
    research_agent = HistoryResearchAgent(
        store=ingestion_store,
        ingestion=ingestion_service,
        gateway=ModelGateway(),
    )
    application = FastAPI(title="史证图谱 API", version="0.1.0")

    def load_workspace() -> dict[str, Any]:
        try:
            return view.workspace()
        except FileNotFoundError as error:
            raise HTTPException(status_code=503, detail=f"知识库不存在: {error}") from error

    @application.get("/api/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "database": selected_db.name, "exists": selected_db.exists()}

    @application.post("/api/ingestion")
    def submit_ingestion(payload: dict[str, Any]) -> dict[str, Any]:
        source = payload.get("source")
        if not isinstance(source, str) or not source.strip():
            raise HTTPException(status_code=400, detail="source is required")
        job = asyncio.run(ingestion_service.submit(source, name=payload.get("name"), media_type=payload.get("media_type"), template=payload.get("template", "historical"), options=payload.get("options")))
        if payload.get("run", True):
            job = asyncio.run(ingestion_service.run(job))
        return job.model_dump(mode="json")

    @application.post("/api/v2/ingestion/{source_id}/read")
    def ai_read_ingestion(source_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        source = ingestion_store.get_source(source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        options = payload or {}
        try:
            return asyncio.run(ai_ingestion_service.run(job_id=options.get("job_id"), source_id=source_id, question=str(options.get("question", ""))))
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @application.post("/api/v2/ingestion")
    def submit_ai_ingestion(payload: dict[str, Any]) -> dict[str, Any]:
        source = payload.get("source")
        if not isinstance(source, (str, bytes)) or not source:
            raise HTTPException(status_code=400, detail="source is required")
        try:
            job = asyncio.run(ingestion_service.submit(source, name=payload.get("name"), media_type=payload.get("media_type"), template=payload.get("template", "historical"), options=payload.get("options")))
            job = asyncio.run(ingestion_service.run(job))
            source_model = ingestion_store.get_source_for_ingestion_job(job.id)
            if source_model is None:
                return {"job": job.model_dump(mode="json"), "status": "awaiting_source"}
            result = asyncio.run(ai_ingestion_service.run(job_id=job.id, source_id=source_model.id, question=str(payload.get("question", ""))))
            return {"job": job.model_dump(mode="json"), "reading": result}
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @application.get("/api/v2/reading/{run_id}")
    def ai_reading_status(run_id: str) -> dict[str, Any]:
        row = ingestion_store.db.execute("SELECT id,job_id,status,pipeline_version,payload,created_at,updated_at FROM v2_reading_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="reading run not found")
        return {"id": row[0], "job_id": row[1], "status": row[2], "pipeline_version": row[3], "payload": json.loads(row[4]), "steps": ingestion_store.get_v2_steps(run_id), "drafts": ingestion_store.get_v2_drafts(run_id), "created_at": row[5], "updated_at": row[6]}

    @application.get("/api/v2/memories")
    def search_memories(q: str, limit: int = Query(default=10, ge=1, le=100)) -> dict[str, Any]:
        return {"results": ai_ingestion_service.memories.search(q, limit=limit)}

    @application.get("/api/v2/memories/{memory_id}")
    def get_memory(memory_id: str) -> dict[str, Any]:
        item = ai_ingestion_service.memories.get(memory_id)
        if item is None:
            raise HTTPException(status_code=404, detail="memory not found")
        return item

    @application.get("/api/v2/memories/{memory_id}/history")
    def memory_history(memory_id: str) -> dict[str, Any]:
        return {"memory_id": memory_id, "history": ai_ingestion_service.memories.history(memory_id)}

    @application.post("/api/v2/memories/{memory_id}/invalidate")
    def invalidate_memory(memory_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        reason = str((payload or {}).get("reason", "evidence invalidated"))
        try:
            ai_ingestion_service.memories.invalidate(memory_id, reason=reason)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return {"memory_id": memory_id, "status": "invalidated", "reason": reason}

    @application.get("/api/v2/wiki/search")
    def search_wiki(q: str, limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        return {"results": ai_ingestion_service.wiki.search(q, limit=limit)}

    @application.get("/api/v2/wiki/{path:path}")
    def read_wiki(path: str) -> dict[str, Any]:
        content = ai_ingestion_service.wiki.read(path)
        if content is None:
            raise HTTPException(status_code=404, detail="wiki page not found")
        return {"path": path, "content": content}

    @application.get("/api/ingestion/{job_id}")
    def ingestion_status(job_id: str) -> dict[str, Any]:
        job = ingestion_store.get_ingestion_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="ingestion job not found")
        return {
            "job": job.model_dump(mode="json"),
            "questions": [item.model_dump(mode="json") for item in ingestion_store.get_questions(job_id)],
            "storage": ingestion_store.storage_status(job_id),
        }

    @application.get("/api/ingestion/{job_id}/storage")
    def ingestion_storage(job_id: str) -> dict[str, Any]:
        if not ingestion_store.get_ingestion_job(job_id):
            raise HTTPException(status_code=404, detail="ingestion job not found")
        return ingestion_store.storage_status(job_id)

    @application.post("/api/research")
    def research(payload: dict[str, Any]) -> dict[str, Any]:
        source = payload.get("source")
        question = payload.get("question")
        if not isinstance(source, (str, bytes)) or not source:
            raise HTTPException(status_code=400, detail="source is required")
        if not isinstance(question, str) or not question.strip():
            raise HTTPException(status_code=400, detail="question is required")
        try:
            return asyncio.run(
                research_agent.ingest_and_research(
                    source,
                    question=question.strip(),
                    name=payload.get("name"),
                    media_type=payload.get("media_type"),
                    template=payload.get("template", "historical"),
                    options=payload.get("options"),
                    max_passages=int(payload.get("max_passages", 0)),
                )
            )
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @application.post("/api/research/sources/{source_id}")
    def research_source(source_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        question = payload.get("question")
        if not isinstance(question, str) or not question.strip():
            raise HTTPException(status_code=400, detail="question is required")
        try:
            return asyncio.run(
                research_agent.analyze_source(
                    source_id,
                    question=question.strip(),
                    max_passages=int(payload.get("max_passages", 0)),
                )
            )
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @application.get("/api/research/runs")
    def research_runs(limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
        return {"runs": ingestion_store.get_agent_runs(limit=limit)}

    @application.get("/api/research/runs/{run_id}")
    def research_run(run_id: str) -> dict[str, Any]:
        run = ingestion_store.get_agent_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="research run not found")
        return run

    @application.post("/api/ingestion/{job_id}/questions/{question_id}")
    def answer_ingestion_question(job_id: str, question_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload.get("answer"), str) or not payload["answer"].strip():
            raise HTTPException(status_code=400, detail="answer is required")
        try:
            job = ingestion_service.answer_question(question_id, payload["answer"])
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        if job.id != job_id:
            raise HTTPException(status_code=409, detail="question does not belong to job")
        return {"job": job.model_dump(mode="json"), "questions": [item.model_dump(mode="json") for item in ingestion_store.get_questions(job_id)]}

    @application.get("/api/workspace")
    def workspace(
        include_graph: bool = Query(default=True),
        include_dimensions: bool = Query(default=True),
    ) -> dict[str, Any]:
        return view.workspace(include_graph=include_graph, include_dimensions=include_dimensions)

    @application.get("/api/dimensions")
    def dimensions() -> dict[str, Any]:
        return view.dimensions()

    @application.get("/api/conflict-review/candidates")
    def conflict_review_candidates(status: str | None = Query(default=None)) -> dict[str, Any]:
        with view._connect() as connection:
            candidates = view._payloads(connection, "conflict_candidates")
        if status:
            candidates = [candidate for candidate in candidates if candidate.get("status") == status]
        return {"candidates": candidates, "count": len(candidates)}

    @application.get("/api/graph")
    def graph(
        q: str | None = Query(default=None, max_length=100),
        kind: str | None = Query(default=None),
        status: str | None = Query(default=None),
        limit: int = Query(default=120, ge=1, le=5000),
    ) -> dict[str, Any]:
        with view._connect() as connection:
            data = view._graph_projection(connection, view._version(connection))
        nodes = data["nodes"]
        edges = data["edges"]
        if kind:
            nodes = [node for node in nodes if node["kind"] == kind]
        matched: set[str] | None = None
        if q:
            needle = normalize_surface(q).casefold()
            matched = {
                node["id"]
                for node in nodes
                if needle in node["label"].casefold()
                or any(needle in alias.casefold() for alias in node.get("aliases", []))
            }
            matched.update(
                edge["source"]
                for edge in edges
                if needle in edge["label"].casefold()
            )
            matched.update(
                edge["target"]
                for edge in edges
                if needle in edge["label"].casefold()
            )
            nodes = [node for node in nodes if node["id"] in matched]
        node_ids = {node["id"] for node in nodes}
        edges = [
            edge
            for edge in edges
            if edge["source"] in node_ids
            and edge["target"] in node_ids
            and (not status or edge["status"] == status)
        ]
        connected = {edge["source"] for edge in edges} | {edge["target"] for edge in edges}
        if matched is not None:
            return {"nodes": [node for node in nodes if node["id"] in matched], "edges": edges}
        ranked = sorted(
            (node for node in nodes if node["id"] in connected),
            key=lambda node: (-sum(node["id"] in (edge["source"], edge["target"]) for edge in edges), node["label"]),
        )[:limit]
        node_ids = {node["id"] for node in ranked}
        return {
            "nodes": ranked,
            "edges": [edge for edge in edges if edge["source"] in node_ids and edge["target"] in node_ids],
        }

    @application.get("/api/nodes/{name}")
    def node(name: str) -> dict[str, Any]:
        with view._connect() as connection:
            graph_data = view._graph_projection(connection, view._version(connection))
        dimensions_data = view.dimensions()
        data = {"graph": graph_data, "claims": dimensions_data["claims"], "timeline": dimensions_data["timeline"]}
        normalized_name = normalize_surface(name)
        selected = next(
            (
                item
                for item in data["graph"]["nodes"]
                if item["id"] == normalized_name
                or normalized_name in {normalize_surface(alias) for alias in item.get("aliases", [])}
            ),
            None,
        )
        if not selected:
            raise HTTPException(status_code=404, detail="节点不存在")
        normalized_name = selected["id"]
        edges = [
            edge
            for edge in data["graph"]["edges"]
            if normalized_name in (edge["source"], edge["target"])
        ]
        claim_ids = {edge.get("claim_id") for edge in edges if edge.get("claim_id")}
        event_ids = {edge.get("event_id") for edge in edges if edge.get("event_id")}
        return {
            "node": selected,
            "nodes": [
                item
                for item in data["graph"]["nodes"]
                if item["id"] == normalized_name
                or item["id"] in {edge["source"] for edge in edges}
                or item["id"] in {edge["target"] for edge in edges}
            ],
            "edges": edges,
            "claims": [item for item in data["claims"] if item["id"] in claim_ids],
            "events": [item for item in data["timeline"] if item["id"] in event_ids],
        }

    if selected_static.exists():
        application.mount("/", StaticFiles(directory=selected_static, html=True), name="frontend")

    return application


app = create_app()
