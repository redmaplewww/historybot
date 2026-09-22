from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol


class MemoryStore(Protocol):
    def add(self, text: str, *, memory_id: str, metadata: dict[str, Any]) -> dict[str, Any]: ...
    def search(self, query: str, *, limit: int = 10, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...
    def get(self, memory_id: str) -> dict[str, Any] | None: ...
    def update(self, memory_id: str, text: str, *, metadata: dict[str, Any] | None = None) -> dict[str, Any]: ...
    def invalidate(self, memory_id: str, *, reason: str) -> None: ...
    def history(self, memory_id: str) -> list[dict[str, Any]]: ...


@dataclass
class SQLiteMemoryStore:
    """Deterministic local adapter used when Mem0 is unavailable and for tests."""

    db: Any

    def add(self, text: str, *, memory_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        payload = {"id": memory_id, "data": text, "metadata": metadata, "status": "active", "created_at": now, "updated_at": now}
        self.db.execute("INSERT OR IGNORE INTO v2_memories(id,data,metadata,status,created_at,updated_at) VALUES(?,?,?,?,?,?)", (memory_id, text, json.dumps(metadata, ensure_ascii=False), "active", now, now))
        self.db.execute("INSERT INTO v2_memory_history(memory_id,action,payload) VALUES(?,?,?)", (memory_id, "add", json.dumps(payload, ensure_ascii=False)))
        self.db.commit()
        return payload

    def search(self, query: str, *, limit: int = 10, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT id,data,metadata,status,created_at,updated_at FROM v2_memories WHERE status='active' AND data LIKE ? ORDER BY updated_at DESC LIMIT ?", (f"%{query}%", max(1, min(limit, 100)))).fetchall()
        result = []
        for row in rows:
            metadata = json.loads(row[2])
            if filters and any(metadata.get(k) != v for k, v in filters.items()):
                continue
            result.append({"id": row[0], "memory": row[1], "metadata": metadata, "score": 1.0, "status": row[3], "created_at": row[4], "updated_at": row[5]})
        return result

    def get(self, memory_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT id,data,metadata,status,created_at,updated_at FROM v2_memories WHERE id=?", (memory_id,)).fetchone()
        if not row:
            return None
        return {"id": row[0], "memory": row[1], "metadata": json.loads(row[2]), "status": row[3], "created_at": row[4], "updated_at": row[5]}

    def update(self, memory_id: str, text: str, *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        current = self.get(memory_id)
        if current is None:
            raise KeyError(memory_id)
        merged = {**current["metadata"], **(metadata or {})}
        now = datetime.now(UTC).isoformat()
        self.db.execute("UPDATE v2_memories SET data=?,metadata=?,updated_at=? WHERE id=?", (text, json.dumps(merged, ensure_ascii=False), now, memory_id))
        self.db.execute("INSERT INTO v2_memory_history(memory_id,action,payload) VALUES(?,?,?)", (memory_id, "update", json.dumps({"text": text, "metadata": merged}, ensure_ascii=False)))
        self.db.commit()
        return self.get(memory_id) or {}

    def invalidate(self, memory_id: str, *, reason: str) -> None:
        self.db.execute("UPDATE v2_memories SET status='invalidated',updated_at=? WHERE id=?", (datetime.now(UTC).isoformat(), memory_id))
        self.db.execute("INSERT INTO v2_memory_history(memory_id,action,payload) VALUES(?,?,?)", (memory_id, "invalidate", json.dumps({"reason": reason}, ensure_ascii=False)))
        self.db.commit()

    def history(self, memory_id: str) -> list[dict[str, Any]]:
        return [{"action": r[0], "payload": json.loads(r[1]), "created_at": r[2]} for r in self.db.execute("SELECT action,payload,created_at FROM v2_memory_history WHERE memory_id=? ORDER BY id", (memory_id,)).fetchall()]


class Mem0MemoryStore:
    """Thin adapter around self-hosted Mem0; evidence metadata stays in SQLite."""

    def __init__(self, *, db_path: str = "data/mem0"):
        try:
            from mem0 import Memory
        except ImportError as exc:
            raise RuntimeError("mem0ai is not installed") from exc
        from mem0.configs.base import MemoryConfig

        model = os.getenv("HISTORYBOT_MEMORY_MODEL", "gpt-4o-mini")
        config = MemoryConfig(
            vector_store={"provider": "qdrant", "config": {"path": db_path, "collection_name": "historybot_memories"}},
            llm={"provider": "openai", "config": {"model": model}},
            embedder={"provider": "huggingface", "config": {"model": "BAAI/bge-small-zh-v1.5"}},
            history_db_path=str(Path(db_path).with_suffix(".history.db")),
        )
        self.memory = Memory(config)

    def add(self, text: str, *, memory_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
        result = self.memory.add(text, agent_id="historybot", run_id=memory_id, metadata={**metadata, "memory_id": memory_id}, infer=False)
        return {"id": memory_id, "memory": text, "metadata": metadata, "backend": "mem0", "result": result}

    def search(self, query: str, *, limit: int = 10, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self.memory.search(query, top_k=limit, filters={"agent_id": "historybot", **(filters or {})})

    def get(self, memory_id: str) -> dict[str, Any] | None:
        try:
            return self.memory.get(memory_id)
        except Exception:
            return None

    def update(self, memory_id: str, text: str, *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.memory.update(memory_id, text=text, metadata=metadata)

    def invalidate(self, memory_id: str, *, reason: str) -> None:
        self.memory.update(memory_id, metadata={"status": "invalidated", "invalidated_reason": reason})

    def history(self, memory_id: str) -> list[dict[str, Any]]:
        return self.memory.history(memory_id)


def build_memory_store(db: Any, *, path: str | None = None) -> MemoryStore:
    if os.getenv("HISTORYBOT_MEMORY_BACKEND", "sqlite") == "mem0":
        return Mem0MemoryStore(db_path=path or os.getenv("HISTORYBOT_MEM0_PATH", "data/mem0"))
    return SQLiteMemoryStore(db)


@dataclass
class WikiStore:
    root: Path

    def __post_init__(self) -> None:
        self.draft_root = self.root / ".drafts"
        for folder in (self.root, self.draft_root, self.root / "sources", self.root / "entities", self.root / "events", self.root / "concepts"):
            folder.mkdir(parents=True, exist_ok=True)

    def read(self, path: str) -> str | None:
        target = self.root / path
        return target.read_text(encoding="utf-8") if target.exists() else None

    def search(self, query: str, *, limit: int = 20) -> list[dict[str, str]]:
        rows = []
        for path in self.root.rglob("*.md"):
            if self.draft_root in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if query.casefold() in text.casefold() or query.casefold() in path.stem.casefold():
                rows.append({"path": str(path.relative_to(self.root)).replace("\\", "/"), "text": text})
        return rows[:limit]

    def save_draft(self, run_id: str, path: str, content: str, *, metadata: dict[str, Any]) -> dict[str, Any]:
        safe = Path(path)
        if safe.is_absolute() or ".." in safe.parts or safe.suffix.lower() != ".md":
            raise ValueError("wiki path must be a relative markdown path")
        draft_path = self.draft_root / run_id / safe
        draft_path.parent.mkdir(parents=True, exist_ok=True)
        draft_path.write_text(content, encoding="utf-8")
        return {"run_id": run_id, "path": path, "draft_path": str(draft_path), "metadata": metadata}

    def publish(self, run_id: str, paths: list[str]) -> dict[str, Any]:
        published = []
        for path in paths:
            source = self.draft_root / run_id / path
            if not source.exists():
                raise FileNotFoundError(source)
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            published.append(path)
        log = self.root / "log.md"
        with log.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## [{datetime.now(UTC).date().isoformat()}] publish | {run_id}\n\nPublished: {', '.join(published)}\n")
        return {"run_id": run_id, "paths": published, "published_at": datetime.now(UTC).isoformat()}
