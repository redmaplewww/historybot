# HistoryBot

HistoryBot is an evidence-first historical research pipeline orchestrated by LangGraph. It stores source versions and passage-level provenance before allowing a model to propose entities, events, claims, or interpretations.

## Run

```powershell
uv sync --extra dev --extra ocr --extra vision
uv run pytest
uv run ruff check src tests
```

The real research run requires a valid provider credential. The gateway currently supports Anthropic Messages API configuration:

```powershell
$env:HISTORYBOT_LLM_PROVIDER = "anthropic"
$env:ANTHROPIC_BASE_URL = "https://api.anthropic.com"
$env:ANTHROPIC_AUTH_TOKEN = "..."
$env:HISTORYBOT_MODEL = "claude-3-5-sonnet-latest"
uv run python -m historybot.run_research --db data/three_kingdoms.db
```

For an OpenAI-compatible gateway, use the same provider-neutral interface:

```powershell
$env:HISTORYBOT_LLM_PROVIDER = "openai_compatible"
$env:HISTORYBOT_BASE_URL = "https://your-gateway.example"
$env:HISTORYBOT_API_KEY = "..."
$env:HISTORYBOT_MODEL = "gpt-5.6-terra"
uv run python -m historybot.run_research --db data/three_kingdoms_final_live.db --max-passages 0
```

The runner downloads public Wikisource MediaWiki API pages for specific chapters of `三国志` and `三国演义`, records 1,071 passage-level records in the current live snapshot, and writes a JSON report next to the database. A provider error is recorded explicitly; it never creates placeholder claims.

Once a live model run has produced candidates, export the dimensional graph view with:

```powershell
uv run python -m historybot.export --db data/three_kingdoms_final.db --output data/three_kingdoms_graph.json
```

The export contains separate `entities`, `events`, `relations`, `timeline`, `places`, and `claims` collections, plus source provenance. Empty collections are an honest signal that the upstream extraction stage did not produce data.

The SQLite schema builds indexes for entity surface/type, event dates/places, relation subject/predicate/object, claim endpoints/status, and evidence passage ids. Every derived relation, timeline entry, place assertion, and claim carries `passage_ids` (and therefore resolves to `passages.source_id` and the original `sources` record). Query the graph dimensions directly:

```python
from historybot.queries import person_timeline, person_relations, conflict_view
timeline = person_timeline(store, "人物名")
relations = person_relations(store, "人物名")
conflicts = conflict_view(store, name="人物名")
```

`conflict_view` expands each conflict into the competing claims and their source passage ids, so a credibility disagreement can be checked against the original wording. The current 三国 sample validation is recorded in `data/three_kingdoms_graph_validation.json`.

## Architecture rules

- LangGraph owns orchestration and resumable state; domain services own validation and persistence.
- Local runs use an explicit in-memory checkpointer; production refuses to start without an injected durable checkpointer (PostgreSQL adapter).
- Nodes return typed Pydantic objects and may only write idempotent, versioned artifacts.
- A claim cannot be promoted without passage evidence. Conflicts create a new state and never delete prior evidence.
- Source reliability is a versioned policy signal, not a conclusion. Folk/literary sources require independent corroboration.
- Models are selected through `ModelGateway`; no node imports a vendor SDK or depends on a fixed model response.
- Tests assert invariants, schemas, idempotency, and recovery. They do not encode historical answers or weaken thresholds to make a test pass.

The historical workspace uses a clean rebuilt snapshot rather than the earlier trial database. The rebuild rejects decoded text containing Unicode replacement characters, so damaged source pages cannot silently become evidence. Entity mentions are stored per passage, while `entity_aliases` stores explicit identity statements such as `字`; the identity index normalizes traditional/simplified surfaces, derives only source-grounded `字`/`号`/`本名` links, and merges compound spellings such as surname-plus-courtesy-name only when the courtesy name has a unique canonical target. Ambiguous aliases remain review nodes and unresolved titles are excluded from the person view.

To ingest the complete 65-volume `三国志` corpus and the bounded literary comparison set:

```powershell
uv run python -m historybot.bulk_ingest --db data/three_kingdoms_complete.db --concurrency 4
```

The command is idempotent at source and passage level, writes an adjacent `.ingest.json`
manifest, and records failures instead of creating placeholder text. The 16 literary chapters
are deliberately labelled `folk`; their claims remain disputed until independent compilation
evidence supports them.

Wikisource uses `ProperNoun` templates for many names. If a snapshot was ingested before those
templates were preserved, repair its passage text in place before rerunning model extraction:

```powershell
uv run python scripts/repair_wikisource_passages.py --db data/three_kingdoms_rebuilt_v2.db
```

The repair aligns corrected text to existing passage IDs, records every changed passage in the
audit log, and invalidates only the affected extraction-ledger rows. It never invents entities or
silently changes passage counts.

For resumable model extraction against a clean corpus snapshot:

```powershell
$env:HISTORYBOT_LLM_PROVIDER = "openai_compatible"
$env:HISTORYBOT_BASE_URL = "https://your-gateway.example"
$env:HISTORYBOT_API_KEY = "..."
$env:HISTORYBOT_MODEL = "gpt-5.6-terra"
$env:HISTORYBOT_REASONING_EFFORT = "none"
$env:HISTORYBOT_HTTP_TIMEOUT = "360"
uv run python -m historybot.bulk_extract --db data/three_kingdoms_rebuilt_v2.db --batch-size 100 --concurrency 2 --retries 0 --people-only
```

To run both phases unattended, use the resumable supervisor (it starts the evidence phase only
after the people ledger reaches full passage coverage):

```powershell
uv run python scripts/run_full_extraction.py --db data/three_kingdoms_rebuilt_v2.db --batch-size 100 --concurrency 2 --retries 0
```

The people-only phase persists person mentions and explicit identity links first. A later run
without `--people-only` adds events and evidence-bound claim candidates to the separate extraction
ledger. Failed batches remain failed and are safe to retry. The web projection exposes the ledger
coverage and includes its version in `overview.data_version`; the frontend polls that version every
15 seconds, so graph, timeline, and place views refresh after each committed batch.

Identity matching keeps the original source surface for evidence, but indexes a simplified-Chinese
normal form so traditional and simplified spellings share one node. An alias is merged only when its
normalized candidate canonical name is unique and the relation is an explicit identity relation
(`字`, `号`, `谥号`, `曾用名`, `本名`, `讳`, `姓名`, or `名`). Ambiguous aliases remain visible as
reviewable surfaces instead of being globally assigned to the last person encountered. Completion
is tracked per passage in separate `people_extraction_passages` and `extraction_passages` ledgers, so
people extraction cannot accidentally mark event/claim extraction complete, and repeated batches are
not counted as new corpus coverage.

## Research workspace

The browser workspace reads the verified SQLite snapshot and exposes person relations,
event co-participation, timelines, places, claims, evidence quotations, and source ratings.
The current workspace service is configured to `data/three_kingdoms_rebuilt_v2.db`, containing
81 sources and 17,033 passages. Full-corpus model extraction is resumable through
`historybot.bulk_extract`; its `people_extraction_batches` ledger records completed and failed
passage batches so a gateway timeout never creates placeholder entities.

The normalized identity invariants can be checked with:

```powershell
uv run python scripts/audit_people_index.py --db data/three_kingdoms_rebuilt_v2.db
```

An external biography index may be used only as a review queue, never as automatic historical
truth. `scripts/import_person_registry.py` requires an exact normalized occurrence in a local
passage, stores the registry commit and passage provenance, and leaves every imported candidate
at `review_required` until the model or a human confirms it.

```powershell
cd frontend
npm install
npm run build
cd ..
uv run uvicorn historybot.web:app --host 127.0.0.1 --port 8000
```

Set `HISTORYBOT_DB` to inspect another compatible snapshot. Workspace projections derive all
displayed counts, graph edges, confidence values, and citations from persisted records; the
ingestion endpoints are the controlled write path for new source material.

## Unified ingestion

The ingestion service accepts text, HTML/URLs, PDFs, and binary image inputs through one versioned pipeline. It preserves the raw asset, recognition output, normalized document, annotations, completeness assessment, questions, review tasks, and audit artifacts. Image and handwriting input uses a pluggable OCR provider; the default provider intentionally returns a review task instead of inventing text.

```powershell
uv run python -m historybot.ingest_cli --db data/ingestion.db submit .\note.txt --run
uv run python -m historybot.ingest_cli --db data/ingestion.db questions job_<id>
uv run python -m historybot.ingest_cli --db data/ingestion.db answer job_<id> q_<id> --value "用户确认内容"
uv run python -m historybot.ingest_cli --db data/ingestion.db submit scan.png --ocr-provider rapidocr --run
uv run python -m historybot.ingest_cli --db data/ingestion.db submit handwriting.png --ocr-provider trocr-handwritten --run
```

The same service is exposed by `POST /api/ingestion`, `GET /api/ingestion/{job_id}`, and `POST /api/ingestion/{job_id}/questions/{question_id}`. A field is not considered complete merely because it has a value: it must have an observed or user-confirmed evidence span. Inferred values remain reviewable and cannot silently become historical claims. Re-submitting identical content is idempotent by content hash, and every processing version is retained.

The ingestion service shares the research database selected by `HISTORYBOT_DB` (default:
`data/three_kingdoms_rebuilt_v2.db`). After a job completes, `GET /api/ingestion/{job_id}`
includes a `storage` summary. The dedicated `GET /api/ingestion/{job_id}/storage` endpoint
reports the persisted raw asset, recognition, normalized document, annotations, questions,
source and passage counts, confirms the database indexes, and checks that a source-scoped
passage query uses `idx_passages_source`. This makes the write contract observable without
exposing database credentials or requiring direct SQL from the caller.

To restore and qualify a database export without changing the current snapshot:

```powershell
uv run python scripts/restore_database_export.py .\artifacts\historybot_database_export_20260913_154944.zip --output .\data\historybot_qualified_20260913.db
$env:HISTORYBOT_DB = "data/historybot_qualified_20260913.db"
```

The restore command verifies the bundle checksums, applies current schema migrations, checks
SQLite integrity and all provenance references, rebuilds indexes, and writes a
`.qualification.json` report beside the database. The resulting database can be selected by
the API or workspace through `HISTORYBOT_DB`.

## AI-first historical ingestion (V2)

V2 makes AI reading and independent verification a release gate. Existing readers and OCR only produce source material and reading aids; they do not create trusted historical facts.

```text
POST /api/v2/ingestion/{source_id}/read
GET  /api/v2/reading/{run_id}
GET  /api/v2/memories?q=赤壁
GET  /api/v2/wiki/search?q=曹操
```

Each run stores immutable reading steps, evidence anchors, verification output and knowledge drafts in SQLite. Verified reading units also materialize structured mentions, identity links, events, claims, evidence and relation projections into the existing evidence graph. Every claim edge resolves through `claim_id -> evidence -> passage_id -> source`; model references outside the current passage are rejected. Only complete passage coverage with exact evidence quotes and successful independent verification publishes a source page under `data/wiki/` and writes a long-term memory. Draft pages remain under `data/wiki/.drafts/<run_id>/`.

The reproducible graph smoke test uses 18 real passages from `三国志·卷01`:

```powershell
.venv/Scripts/python.exe -X utf8 scripts/run_ai_graph_live_test.py
```

It writes `artifacts/ai_graph_live_report.json`, `artifacts/ai_graph_live_projection.json`, and a PNG projection. The replay is explicitly labelled `codex-manual-replay`; it validates storage and provenance contracts without claiming that the unavailable external model completed the run.

The default memory backend is SQLite. Set `HISTORYBOT_MEMORY_BACKEND=mem0`, install `historybot[memory]`, and set `HISTORYBOT_MEM0_PATH` to use the self-hosted Mem0/Qdrant adapter. Mem0 receives reviewed text with `infer=False`; SQLite remains authoritative for source provenance and review state.

The adapter code is in `src/historybot/v2_adapters.py`, the workflow in `src/historybot/ai_ingestion.py`, and contracts in `src/historybot/v2_schemas.py`.

## Agent and ingestion boundary

`historybot.agent.HistoryResearchAgent` is the application entry point for new research runs. It submits material to `IngestionService`, waits for the completeness/review gate, reads only committed `sources` and `passages` through `KnowledgeStore`, and then invokes the LangGraph extraction/review/contradiction graph. Material whose ingestion status is `awaiting_clarification` or `review_required` is returned with its questions; model extraction does not start for it.

Each run is recorded in the `agent_runs` table with its source, passage count, status, result counts, and error details. The graph remains provider-neutral through `ModelGateway`, and the Agent depends on `KnowledgeStore` methods rather than constructing SQL or synthetic passages. HTTP entry points are `POST /api/research`, `POST /api/research/sources/{source_id}`, `GET /api/research/runs`, and `GET /api/research/runs/{run_id}`.

Local model comparison is reproducible with `uv run python scripts/evaluate_ocr.py --image scan.png`. The `ocr` extra installs RapidOCR/ONNX Runtime; the `vision` extra installs TrOCR's Transformers/PyTorch runtime. TrOCR is line-level visual OCR, so page layout detection remains a separate stage and all TrOCR output is sent to review by default.
