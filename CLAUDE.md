# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development commands

### Full stack
- Start both services: `./start_all.sh` (Windows: `start_all.bat`)
- Stop both services: `./stop_all.sh`
- Logs land in `logs/backend.log` and `logs/frontend.log`

### Backend (run from `backend/`)
- Install: `pip install -r requirements.txt`
- Start: `uvicorn app:app --reload --port 8000`
- Smoke check: `python scripts/smoke_check.py`

There is no pytest/lint configuration. Use the smoke check plus targeted manual verification of affected API paths.

### Frontend (run from `frontend/`)
- Install: `npm install`
- Dev server (port 6008, proxies `/api` → `localhost:8000`): `npm run serve`
- Production build: `npm run build`

There is no frontend lint or automated test script.

## Architecture overview

FastAPI + Vue 3 monorepo. The backend implements an **adaptive self-correcting async DAG runtime** (`adaptive_dag_v3`). Both `/api/chat` (sync) and `/api/chat/stream` (SSE) share the same underlying `harness.run_stream()` event source — the sync endpoint simply aggregates the stream into a `ChatResponse`. Any change to streaming format affects both endpoints.

### Request lifecycle

```
POST /api/chat[/stream]
  → app.py: validate, resolve session, call runtime_executor
  → runtime_executor.py: calls harness.run_stream()
  → harness.py: complexity analysis → RuntimeIntent → RuntimeHarness.run()
  → runtime/dag_stream.py: Intent → Planner → DAG construction
  → runtime/nodes/dag_phases.py: parallel search / draft / critic / repair / finalize
  → event stream: trace | step | chunk | content_reset | model_end | error
```

### SSE event types
Events are dicts with an `event` key, parsed in `frontend/src/chatShared.js` and rendered by `App.vue` / `StepDisplay.vue`:
- `trace`: Protocol metadata, phase, trace ID
- `step`: Execution step with name, status, meta, latency
- `chunk`: Progressive text content for rendering
- `content_reset`: Clear render buffer (new answer phase starting)
- `model_end`: Model call completion with latency/token counts
- `error`: Failure details

### Runtime intent scoring (`runtime/models/runtime_intent.py`)
The analyzer outputs continuous scores (not mutually exclusive tracks):
- `reasoning_score`, `search_score`, `risk_score` (0–1)
- `latency_budget`: low (5s) / medium (20s) / high (45s)
- `quality_requirement`: low / medium / high
- `realtime_requirement`, `tool_requirement`: booleans

These scores drive DAG parallelism levels dynamically — there are no fixed fast/refine/agent tracks.

### DAG execution phases (`runtime/nodes/dag_phases.py`, 38KB)
1. **Intake**: Complexity analysis + runtime planning
2. **Search**: Parallel web/doc searches (query count from config)
3. **Draft**: Parallel drafts with hedged delay
4. **Quality**: Parallel critics → repair loop (max 2 rounds) if issues found
5. **Finalize**: Markdown formatting + polish

Critic facets (in `runtime/quality/critic_engine.py`): coverage, logic, evidence, hallucination, policy. Each outputs structured JSON with optional `needs_search` hints.

Quality pipeline has three layers configured in `config.yaml`:
- Layer 1 (draft): Wide coverage gap detection
- Layer 2 (review): Error correction; delimited by `<<<FINAL_ANSWER>>>...<<<END_FINAL_ANSWER>>>`
- Layer 3 (polish): Final refinement

### Two-layer configuration system
- **`backend/config.yaml`**: Full schema — model registry, search settings, quality pipeline, API keys
- **`backend/config.runtime.yaml`**: Runtime overrides persisted via `PUT /api/config/runtime`; deep-merged on top of base config

When changing runtime behavior, determine whether the change belongs in the base schema, the runtime patch schema, or both. The frontend config screen writes runtime overrides through the API, so serialization and UI expectations must stay aligned.

**Secret handling**: `config_runtime.py` detects secret keys by prefix/suffix (`api_key*`, `*_secret`, `*_token`, `*_api_key`). API responses redact secrets; client patches are sanitized to prevent accidental overwrites. Add new secret keys to the `SECRET_KEY_*` sets in `config_runtime.py`.

Key config sections:
| Section | Controls |
|---|---|
| `harness.dag_runtime` | Parallel search count, critic count, max repair rounds |
| `harness.task_model_templates` | Model pools per task type (reasoning / generation / code / conversation) with draft/review/polish tiers |
| `harness.search` | Tavily/DuckDuckGo, depth, result count, cache TTL, authority ranking |
| `harness.documents` | BM25 (55%) + embedding (45%) weights, chunk size (7000 chars), reranking (BGE-v2-m3) |
| `harness.quality_pipeline` | Per-layer temperatures and instructions |
| `harness.runtime_orchestrator` | Budget manager, unified critic, metrics storage |
| `models.*` | Per-model OpenAI-compatible config: base_url, api_key, timeout |

### Search service (`backend/search_service.py`)
- Primary: Tavily; fallback: DuckDuckGo
- Session caching with 30-min default TTL
- Speculative markers: keywords (weather, news, stock, etc.) trigger implicit search
- Authority ranking blends official domain hints with BM25/embedding scores

### Document processing
- Hybrid retrieval: BM25 (55%) + embedding (45%), embedding model `text-embedding-3-large`
- Chunking: 7000 chars, 900-char overlap
- Reranking: BGE v2 M3 (top-8 from top-12)
- Limits: 100 PDF pages, 2500 sheet rows

### State tracking (`runtime/state/`)
- `ExecutionState`: Goals, resolved/unresolved list, risk scores, phases
- `EvidenceGraph`: Search result references with source tracking for quality auditing
- `SemanticMemory`: Turn-local caching to avoid redundant searches

### Observability
- Product metrics emitted as JSONL via `emit_product_metric()`
- Metrics SQLite: `data/runtime_metrics.sqlite`
- Tracked: thumb_down_rate, retry_rate, followup_rate, latency_breakdown, escalation_rate

### Frontend (`frontend/src/`)
- `App.vue`: Owns session state, active run state, SSE consumption, step panel visibility
- `chatShared.js`: SSE event parsing, session/run/message ID generation
- `sessionPersistence.js`: IndexedDB primary, localStorage fallback
- `ConfigView.vue`: Writes runtime overrides through `PUT /api/config/runtime`
- `StepDisplay.vue`: Right sidebar — step visualization, resizable, syncs with live run
- Rendering: `marked` (Markdown), KaTeX (math), DOMPurify (sanitization)

### Session persistence
- Client sessions: IndexedDB (primary) → localStorage (fallback), UUID-based
- Server-side: optional Redis-backed history via session IDs

## Cross-cutting change rules

- **Streaming format change** → inspect `chatShared.js`, `App.vue`, `StepDisplay.vue`
- **Runtime config field change** → update all of: `config.yaml`, `config.runtime.yaml`, `config_runtime.py`, `app.py`, `ConfigView.vue`
- **Chat execution flow change** → verify both sync and streaming endpoints
- **New secret key** → add to `SECRET_KEY_*` sets in `config_runtime.py`
