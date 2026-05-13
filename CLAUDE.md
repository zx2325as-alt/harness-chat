# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development commands

### Full stack
- Start both services: `./start_all.sh`
- Stop both services: `./stop_all.sh`
- Windows launcher: `start_all.bat`

`start_all.sh` starts:
- backend API on `http://127.0.0.1:8000`
- frontend dev server on `http://127.0.0.1:6008`
- logs under `logs/backend.log` and `logs/frontend.log`

### Backend
Run from `backend/` unless noted otherwise.

- Install dependencies: `pip install -r requirements.txt`
- Start dev server: `uvicorn app:app --reload --port 8000`
- Alternate start: `python -m uvicorn app:app --reload --port 8000`
- Smoke check: `python scripts/smoke_check.py`

There is no established pytest/lint configuration in the repo root or backend at the moment. Prefer the smoke check plus targeted manual verification of the affected API path.

### Frontend
Run from `frontend/`.

- Install dependencies: `npm install`
- Start dev server: `npm run serve`
- Production build: `npm run build`

There is no frontend lint or automated test script in `frontend/package.json` currently.

## Architecture overview

This is a FastAPI + Vue 3 monorepo for a chat-style “Harness Chat” application.

### Backend shape
- API entrypoint: `backend/app.py`
- Core harness orchestration: `backend/harness.py`
- Shared stream/sync runtime wrapper: `backend/runtime_executor.py`
- Runtime DAG implementation: `backend/runtime/`
- Runtime config overlay helpers: `backend/config_runtime.py`
- Search abstraction: `backend/search_service.py`

`backend/app.py` constructs the FastAPI app, loads `config.yaml`, overlays `config.runtime.yaml`, initializes Redis if configured, creates `DualTrackHarness`, and exposes both synchronous and SSE chat endpoints plus config/document APIs.

The backend is now organized around a unified runtime stream rather than separate fully independent execution tracks. `backend/runtime_executor.py` makes synchronous REST responses and streaming responses share the same underlying event stream. The main execution path is the adaptive DAG runtime in `backend/runtime/dag_stream.py`.

The DAG runtime is conceptually:
- analyze intent/complexity
- plan execution
- run a DAG of search / draft / quality / finalize stages
- stream step events and answer chunks back to the client

The `backend/runtime/` package is split by concern:
- `orchestrator/`: runtime planning, scheduling, budgets, escalation
- `nodes/`: executable DAG nodes such as search, draft, critic, verify, repair, finalize, agent
- `quality/`: verification / sufficiency / hallucination / repair logic
- `parallel/`: parallel search and speculative execution helpers
- `streaming/`: progressive streaming behavior
- `state/` and `cache/`: execution state, evidence, semantic memory, caches
- `models/`: router / critic / verifier / runtime-intent data models

### API surface
Key routes live in `backend/app.py`:
- `POST /api/chat`
- `POST /api/chat/stream`
- `GET /api/config`
- `PUT /api/config/runtime`
- `POST /api/documents/parse`
- `POST /api/documents/parse_folder`
- `POST /api/feedback`
- `DELETE /api/session/{session_id}/history`
- `GET /api/health`

### Configuration model
Backend behavior is heavily config-driven:
- base config: `backend/config.yaml`
- runtime overrides persisted separately: `backend/config.runtime.yaml`

When changing runtime behavior, check whether the change belongs in the base config schema, the runtime patch schema, or both. The frontend config screen writes runtime overrides through `PUT /api/config/runtime`, so config changes often require keeping API serialization and UI expectations aligned.

### Frontend shape
- App bootstrap: `frontend/src/main.js`
- Top-level UI/state container: `frontend/src/App.vue`
- Shared chat helpers and SSE parsing: `frontend/src/chatShared.js`
- Session persistence and quota fallback: `frontend/src/sessionPersistence.js`
- Config screen: `frontend/src/components/ConfigView.vue`
- Step/run visualization: `frontend/src/components/StepDisplay.vue`

The frontend is a single-page Vue 3 app with two main views:
- chat UI
- runtime configuration UI

`App.vue` owns the main session state, active run state, request lifecycle, and step panel visibility. It sends chat requests to the backend, consumes SSE frames for `/api/chat/stream`, and stores conversation/session state in IndexedDB with localStorage fallback.

Frontend development uses Vue CLI dev server on port `6008`. `frontend/vue.config.js` proxies `/api` to `http://127.0.0.1:8000`, so frontend code should generally use same-origin `/api` paths instead of hardcoding backend hosts.

### Persistence and streaming details
- Client sessions are persisted locally in the browser, primarily via IndexedDB.
- Server-side conversation history can also be used via session IDs and Redis-backed state.
- The UI exposes detailed execution steps; backend SSE step payloads are therefore part of the product contract, not just debugging output.

## Working notes for future changes
- If you change the backend streaming event format, also inspect the frontend SSE parsing and step rendering path in `frontend/src/chatShared.js`, `frontend/src/App.vue`, and `frontend/src/components/StepDisplay.vue`.
- If you change runtime configuration fields, verify all of: `backend/config.yaml`, `backend/config.runtime.yaml`, `backend/config_runtime.py`, `backend/app.py`, and `frontend/src/components/ConfigView.vue`.
- If you change chat execution flow, verify both sync and streaming endpoints because they share the same runtime event source but have different response assembly.
- The repo currently contains checked-in runtime/logical evolution artifacts and some generated Python cache directories under `backend/runtime/__pycache__`; avoid treating those as architectural sources.
