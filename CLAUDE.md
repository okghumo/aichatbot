# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

"Bucky" — a reusable FastAPI + litellm chatbot backend with a Vite/React/TS
frontend. One backend codebase is repurposed for different bots (currently:
bucketlistt's travel/activity concierge) purely by swapping the
`SYSTEM_PROMPT_FILE` knowledge base and env config — no code branching.
Backend also does Weaviate-based RAG, live MCP tool calls, file attachments,
session-summary webhooks, and SMTP alerting. See [README.md](README.md),
[ARCHITECTURE.md](ARCHITECTURE.md), and [FUNCTIONALITY.md](FUNCTIONALITY.md)
for the full picture; [ATTACHMENTS_API.md](ATTACHMENTS_API.md),
[SUMMARY_WEBHOOK_API.md](SUMMARY_WEBHOOK_API.md), and
[backend/SMTP_ALERTS.md](backend/SMTP_ALERTS.md) document individual
subsystems in depth.

## Graphify (codebase knowledge graph)

This project has a knowledge graph at `graphify-out/` with god nodes,
community structure, and cross-file relationships.

- For codebase questions, first run `graphify query "<question>"` when
  `graphify-out/graph.json` exists. Use `graphify path "<A>" "<B>"` for
  relationships and `graphify explain "<concept>"` for focused concepts.
  These return a scoped subgraph, usually much smaller than
  `GRAPH_REPORT.md` or raw grep output.
- If `graphify-out/wiki/index.md` exists, use it for broad navigation
  instead of raw source browsing.
- Read `graphify-out/GRAPH_REPORT.md` only for broad architecture review or
  when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current
  (AST-only, no API cost).

## Commands

### Backend (run from `backend/`)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"        # installs pytest, pylint, httpx, etc.
cp .env.example .env           # then set OPENAI_API_KEY (or provider key)

uvicorn app.main:app --reload  # dev server, http://localhost:8000

pytest                         # full suite
pytest tests/test_llm.py                       # one file
pytest tests/test_llm.py::test_name -v         # one test

pylint app                     # lint; config in pyproject.toml [tool.pylint.*]
```

Tests mock `litellm.acompletion` (no network calls, no real API key needed —
`conftest.py` sets a dummy `OPENAI_API_KEY`) and use `FakeRedis` in place of
a real Redis. Must run from `backend/` so the relative `SYSTEM_PROMPT_FILE`
path resolves.

### Frontend (run from `frontend/`)

```bash
npm install
npm run dev      # vite dev server, http://localhost:5173
npm run build    # tsc -b && vite build -> dist/
npm run test     # vitest run
npm run test -- src/App.test.tsx   # one file
npm run lint     # eslint .
```

### Full stack

`docker-compose up --build` runs Weaviate + backend + frontend together
(env from `backend/.env` / `frontend/.env`).

## Architecture

Thin FastAPI routes, thick service layer (`app/llm.py` is the core: system
prompt loading, the tool-calling loop, SSE streaming). The two endpoints
that matter are `POST /api/chat` (SSE) and `POST /api/chat/attachments`.
litellm is the only LLM integration point — `LLM_MODEL` in `.env` picks the
provider/model; `app/llm.py` never changes when switching providers.

Backend module map (`backend/app/`):
- `main.py` — FastAPI app, routes, wires the middleware stack.
- `llm.py` — system-prompt injection, tool-calling loop (resolve tools
  non-streaming, then stream the final answer), SSE frame building,
  `is_protected_turn()` gate.
- `flow_guard.py` — `is_protected_turn()`: marks messages that must always
  reach the full unrestricted pipeline (imported by `llm.py`).
- `stream_sanitizer.py` — `StreamSanitizer.feed()/.flush()`: scrubs the
  outgoing token stream before it reaches the client.
- `retriever.py` — Weaviate RAG retrieval, loaded lazily, falls back to the
  flat markdown knowledge-base prompt when the vector DB is unpopulated.
- `mcp_client.py` — remote MCP server client (`bucketlistt` catalog API);
  `_run_tool_loop()` / `_execute_tool()` drive tool execution, with
  result trimming/compaction for tag-search and closure lookups.
- `cache.py` — Redis-backed MCP result cache; no-ops if `REDIS_URL` unset.
- `session_store.py` — Redis-backed session state (`SESSION_TTL_SECONDS`).
- `token_store.py` — pending-phone / OTP token storage for verified-phone
  capture into session summaries.
- `dashboard.py` — employee dashboard API: idle-session scan loop, forces a
  structured summary out of the model via tool use, HMAC-SHA256-signs and
  pushes it to `SUMMARY_WEBHOOK_URL` (Stripe-style `<timestamp>.<body>`).
- `attachments.py` — upload pipeline: content-type sniff, malware scan via
  clamd (fail-closed if clamd is unreachable), then extraction (`.docx` via
  `python-docx` + `defusedxml`, hardened against XXE).
- `notifier.py` — SMTP critical-alert sending (see `SMTP_ALERTS.md`).
- `rate_limit.py` — per-IP sliding-window `RateLimitMiddleware`
  (`ponytail`-tagged in-memory implementation — see the file for the
  documented upgrade path).
- `db.py` — SQLite (feedback inbox, editable prompts + history, reply log); only module that touches the DB.
- `prompt_store.py` — the 3 editable prompts (knowledge base, WhatsApp persona/style) live here, not read
  from files: live row + at most 2 old versions for 45 days, rollback, drafts. Falls back to `data/*.md`.
- `feedback.py` — `POST /api/v2/feedback` (CRM feedback, idempotent on `feedback_id`); see `FEEDBACK_API.md`.
- `feedback_proposer.py` / `admin_prompts.py` — feedback → *draft* prompt edit (never live) and the
  `ADMIN_API_KEY`-guarded endpoints to approve/reject/roll back. Rules in `data/locked/` are not editable via the store.
- `tools.py` — native tool functions (e.g. lead capture) + their schemas.
- `config.py` / `schemas.py` — `Settings` (env-driven, only the LLM key has
  no default) and Pydantic request/response models.

Frontend (`frontend/src/`): `App.tsx` is the chat shell; `api/` parses the
SSE stream; `components/` holds Header/Sidebar/MessageContent/LeadModal
etc. It only depends on the `/api/chat` SSE contract and `/api/health`, so
it works against any backend instance speaking that contract.

## Repurposing pattern

Switching use case (e.g. customer bot → internal policy bot) only requires:
write a new knowledge-base markdown file, point `SYSTEM_PROMPT_FILE` at it,
restart. No route or service code changes. This only stays viable while the
KB fits comfortably in the model's context; past that, extend
`retriever.py`'s RAG path instead of growing the flat-file prompt.
