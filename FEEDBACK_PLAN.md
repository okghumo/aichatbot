# Bucky feedback loop — implementation plan

**Status:** plan only. No code written yet.
**Source:** the CRM team's `bucky-feedback-api.md` proposal, plus our decision to
keep editable prompts in SQLite instead of GitHub.

---

## Implementation status (branch `feat/ai-suggested-prompt-changes`)

**Built and tested (392 backend tests pass; the one failing test, `test_discount_ask_still_goes_to_the_normal_flow`, needs network and fails the same on `main`):**

| Phase | State | Where |
|---|---|---|
| 1. Receiver + `response_id` | Done | `app/feedback.py`, `app/crm_chat.py`, `app/db.py` |
| 2. Prompt store with 5-version / 45-day history | Done | `app/prompt_store.py`, `app/llm.py`, `data/locked/must_keep.json` |
| 3. Proposer + admin review/rollback | Done (proposer **off** by default) | `app/feedback_proposer.py`, `app/admin_prompts.py`, `data/locked/proposer_rules.md` |
| 4. Status callback to the CRM | **Not built** (needs the CRM's URL and secret) | see `FEEDBACK_API.md` section 8 |
| 5. Backups / retention | **Not built**: nightly copy of the SQLite file to S3 and a retention period for raw transcripts are still to do | |

**Fixed (2026-11-24): stale drafts no longer clobber each other.** Previously, two drafts queued
against the same prompt each held a frozen full-text snapshot computed when they were *written*.
Approving the first moved the prompt to v2; approving the second then silently overwrote it with a
draft computed against v1, discarding the first (already-approved) change. Each draft now also
records the live `prompts.version` it was written against (`base_version`). `approve_draft` checks
that version at approval time: if it still matches, it applies as before; if the prompt moved under
it, the draft is marked `stale`, its feedback item is reset to `received` so the proposer redrafts it
against the now-current prompt, and the approval call fails with a clear 422 instead of applying. No
auto-merge is attempted — a stale draft is simply redone against the latest prompt, never silently
applied on top of a conflicting change. `GET /api/admin/prompts/drafts` now also returns `stale: bool`
per draft so this is visible before anyone tries to approve it. See `app/prompt_store.approve_draft`.

**Not doing (per decision, 2026-11-24):** nightly SQLite backups to S3 (Phase 5) are explicitly
out of scope for now. Phase 4 (CRM status callback) stays on standby, not started.

**Changed (2026-11-24): `PROMPT_HISTORY_KEEP` raised from 2 to 5.** Old prompt versions are now
kept for 5 changes per prompt name (still capped at 45 days each) instead of 2. Pure config change
(`app/config.py`, `backend/.env.example`); the mechanism itself (section 3.1) is unchanged.

**Where the build differs from the plan above**
- Drafts are applied as a *targeted edit* (exact old text → new text, or append), not a full rewrite, so diffs stay small and the model cannot silently drop sections.
- "Golden chats" are **not** LLM-based. The gate is deterministic: allowlist of 3 prompts, size cap, may not shrink below 50%, `{name}` / `{desk_phone}` placeholders must survive, and the headings listed in `data/locked/must_keep.json` (safety, medical limits, handoff rule) must survive. A real golden-chat run against the model is a worthwhile next step.
- `response_log` stores the prompt versions but not the tools called (the streaming loop does not expose them).
- `price_overview.md` and `chat_playbook.md` are **not** editable (the playbook is not loaded by the code at all).
- The locked rules are the code (`llm.py` prompts, `flow_guard`, sanitizer) and `data/locked/`; the existing `llm.py` prompt order is unchanged, so replies are identical before and after a prompt edit.

**Server state (2026-11-18)**: the production EC2 runs the backend built **locally from this branch** (`docker build -t ghcr.io/okghumo/aichatbot-backend:latest ./backend`, then `docker compose -f docker-compose.prod.yml up -d --no-deps backend`), with `ADMIN_API_KEY` and `PROPOSER_ENABLED=false` added to `backend/.env`. The `bucky_db` volume was created on that first start.
- **Merge to `main` before any other `main` deploy.** CI builds and pushes only from `main`; its next deploy runs `docker compose pull` and would replace this local image with main's, which has no `/api/v2/feedback`. Until the branch is merged, either avoid pushing to `main` or rebuild this branch's image afterwards.
- The server checkout is on the feature branch: switch back (`git checkout main && git pull --ff-only`) once merged.
- Rollback: `git checkout main && git pull --ff-only`, then `docker compose -f docker-compose.prod.yml pull backend && docker compose -f docker-compose.prod.yml up -d backend`. The feedback data stays in the volume.
- The `ADMIN_API_KEY` used for the first start was a temporary one shown in chat: replace it with a fresh random value.

**Deploy notes**
- Both compose files now mount a `bucky_db` volume at `/var/lib/bucky` and set `DB_PATH`. **Back this volume up**: it holds feedback and prompt history.
- New env vars (see `backend/.env.example`): `DB_PATH`, `ADMIN_API_KEY` (admin endpoints return 503 until set), `PROPOSER_ENABLED` (default false), `PROPOSER_MODEL`, `PROMPT_HISTORY_KEEP`, `PROMPT_HISTORY_DAYS`.
- Single backend instance only (SQLite). If it becomes several, move `app/db.py` to Postgres/RDS.
- First start seeds version 1 of each prompt from `data/*.md`; if the database is unreadable the files are used.

**Admin API** (`Authorization: Bearer $ADMIN_API_KEY`):

| Call | Purpose |
|---|---|
| `GET /api/admin/prompts` | Live version of each prompt, plus its old versions (max 5) with `days_left` |
| `GET /api/admin/prompts/drafts?status=pending` | Proposed edits with a diff against the live prompt |
| `POST /api/admin/prompts/drafts/{id}/approve` `{"by": "name"}` | Make it live; the old prompt goes to history |
| `POST /api/admin/prompts/drafts/{id}/reject` `{"by": "name"}` | Discard; feedback becomes `wont_apply` |
| `POST /api/admin/prompts/{name}/rollback/{history_id}` `{"by": "name"}` | Swap an old version back in (undoable) |
| `POST /api/admin/feedback/propose` | Run the proposer once now |
| `GET /api/admin/feedback/stats` | Feedback counts by status, pending drafts |

---

## 1. What we are building (in one paragraph)

Agents in the CRM flag wrong Bucky replies and write what Bucky should have said.
A super admin approves it. Only approved items are POSTed to Bucky at
`POST /api/v2/feedback`. Bucky stores them, and a controlled, human-reviewed
process turns them into improvements of Bucky's **editable prompts**. Those
prompts live in a SQLite database with versions, so a change can go live (or be
rolled back) without a code deploy and without a GitHub token.

## 2. Our part vs the CRM's part

| CRM team builds | We (Bucky backend) build |
|---|---|
| Feedback button, categories, review on WhatsApp/CRM | `POST /api/v2/feedback` receiver (**required**) |
| Retries with backoff (2s / 5s / 10s, then hourly for a day) | Idempotency on `feedback_id` |
| Feedback log and statuses | `response_id` in the `/api/v2/chat` response (**ask 3**) |
| Receives our callback | Optional status callback `applied` / `wont_apply` (phase 2) |
| | Versioned prompt store + draft/approve flow (our own addition) |

## 3. End-to-end workflow

```
CRM (approved item)
   │ POST /api/v2/feedback  (Bearer CHAT_API_KEY)
   ▼
[1] feedback.py: auth → validate → dedupe on feedback_id → INSERT → 200 {received, ref}
   │                      (no LLM work inside the request)
   ▼
[2] feedback table: status = "received"
   │
   ▼  (background job, or manual trigger — never inside the request)
[3] Proposer: reads item + transcript + current active prompts
   │          asks a model for a SUGGESTED edit to ONE editable prompt
   ▼
[4] prompt_drafts: new row, status = "pending"   (NOT live)
   │
   ▼
[5] Human review (protected admin endpoint): see diff → approve / reject
   │
   ├─ reject → draft "rejected", feedback → "wont_apply", callback to CRM
   └─ approve → run golden checks → ONE TRANSACTION (see 5.1):
                 current live prompt → prompt_history (keep max 5, 45 days)
                 draft → live `prompts` row
                 → reload cache → feedback → "applied" → callback to CRM
```

Key rule: **the model only ever creates drafts. A human activates them.**

### 3.1 Prompt versioning rule (the "5 old prompts for 45 days" rule)

- `prompts` (main table) holds **only the live prompt**, one row per prompt name.
- `prompt_history` holds the **previous** prompts, **at most 5 per prompt name** (`PROMPT_HISTORY_KEEP`), each kept **at most 45 days**.
- When a change is approved for prompt `X`:
  1. If `prompt_history` already has 5 rows for `X` → **delete the oldest**.
  2. Copy the current live `X` into `prompt_history` (stamped with the time it was retired).
  3. Overwrite the live `X` row with the new content.
  4. Steps 1–3 happen in **one SQLite transaction**: either all happen or none, so we can never lose the live prompt or end up with 6 old copies.
- Example, prompt `whatsapp_persona`, history already holds v1 through v5, live is v6:
  - Change request → v7 arrives.
  - v1 (oldest) is deleted. v6 moves into history. History is now v2..v6. Live is v7.
- Rollback = **swap**: take a chosen history row, make it live, and move the current live prompt into history (the restored row leaves history first, so nothing is deleted by the swap). That way a rollback is itself undoable.
- A history row older than 45 days is deleted, even if the cap of 5 is not reached. So there may be fewer than 5 after a quiet period. The very first live prompt (the seed) has no history until the first change.

## 4. What can and cannot be changed

Enforced in **code**, not only in the prompt text.

| Zone | Contents | Who can change it |
|---|---|---|
| **Editable (SQLite)** | `knowledge_base`, `whatsapp_persona`, `whatsapp_style`, `chat_playbook` | The proposer creates drafts; a human activates |
| **Locked rules (git)** | New `backend/data/locked/*.md`: safety, handoff, "what you may not change" | Humans, through git only |
| **Code (git)** | `backend/app/*`, tests, `flow_guard`, `stream_sanitizer`, handoff, OTP, auth | Humans only |
| **Live data** | `offers.json`, `offer-template.json`, scraped data, `price_overview.md` | Humans/scrapers; **not** the proposer |

Enforcement points:
1. The proposer has **no file-write or shell tool**. It returns text; our code writes the row.
2. The target prompt name must be in a fixed allowlist (`EDITABLE_PROMPTS`). Anything else → rejected.
3. The locked prompt is appended **last** by `build_messages`, so an editable prompt cannot override it.
4. Size cap per prompt (for example 40 KB) and a "must not shrink by more than X%" check to catch wipe-outs.
5. Every activation goes through the golden-chat check (section 8).

## 5. Storage: SQLite

- File: `/data/bucky.db` inside the container, on a **new Docker volume** (`bucky_db`) in `docker-compose.yml`. Today `backend/data/` has no volume, so anything written there is lost on redeploy. The same applies to `session_summaries.json` — worth fixing separately.
- Use Python's built-in `sqlite3`, no new dependency. WAL mode on. All DB calls via `asyncio.to_thread` so the event loop is never blocked.
- One module, `app/db.py`, owns the connection. Nothing else imports `sqlite3`, so a later switch to Postgres/RDS touches one file.
- **Backups:** nightly copy of the `.db` file to S3 (cron on the EC2, using `sqlite3 .backup`). Not optional: it holds prompt history and training data.
- Single backend instance only. If it becomes several instances, move to RDS.

### Tables

```
feedback
  feedback_id   TEXT PRIMARY KEY          -- from the CRM, the idempotency key
  received_at   TEXT
  payload_json  TEXT                      -- full body as received (size-capped)
  category      TEXT
  session_id    TEXT, request_id TEXT, response_id TEXT
  status        TEXT   -- received | proposed | applied | wont_apply | failed
  ref           TEXT   -- our id returned to the CRM

prompts                                     -- MAIN table: the LIVE prompt only
  name          TEXT PRIMARY KEY          -- one of EDITABLE_PROMPTS
  version       INTEGER                   -- increases on every change
  content       TEXT
  source        TEXT   -- seed | feedback:<feedback_id> | rollback | manual
  updated_by    TEXT, updated_at TEXT

prompt_history                              -- OLD prompts: max 5 per name, 45 days
  id            INTEGER PRIMARY KEY
  name          TEXT                      -- same names as `prompts`
  version       INTEGER                   -- the version number it had when live
  content       TEXT
  source        TEXT
  retired_at    TEXT                      -- when it stopped being live; the 45 days count from here
  UNIQUE(name, version)
  -- the "max 5" and "45 days" rules are enforced in code, inside the
  -- same transaction as the swap (section 3.1), plus a daily purge.

prompt_drafts                               -- proposed changes waiting for a human
  id            INTEGER PRIMARY KEY
  name          TEXT, content TEXT, reason TEXT
  feedback_id   TEXT                      -- which feedback produced it
  status        TEXT   -- pending | approved | rejected
  created_at TEXT, decided_by TEXT, decided_at TEXT

response_log
  response_id   TEXT PRIMARY KEY
  session_id    TEXT, request_id TEXT, created_at TEXT
  prompt_versions_json TEXT               -- which versions were live
  tools_json    TEXT                      -- tools called
```

`response_log` is what makes `response_id` useful: given a flagged reply we can see
exactly which prompt versions produced it. (If that version has since aged out of
`prompt_history`, we still know its version number, just not the text.)

### 5.1 Retention rules

| Rule | Value | Where enforced |
|---|---|---|
| Live prompts per name | exactly 1 (`prompts` primary key) | schema |
| Old prompts per name | **max 5** | swap transaction: delete oldest first |
| Old prompt lifetime | **45 days** from `retired_at` | daily purge job + checked again on every swap |
| Rejected / approved drafts | keep 45 days for the audit trail, then delete | same daily purge |
| Feedback items | decide in Phase 5 (they hold customer chat) | Phase 5 |

Constants live in `config.py` (`PROMPT_HISTORY_KEEP=2`, `PROMPT_HISTORY_DAYS=45`) so
they can change without touching logic.

## 6. Phases

### Phase 0 — Decisions (before any code)
- [ ] Phone/name in feedback: masked (`91886******03`, no name) or raw? **Recommend masked** — it is training data.
- [ ] `category` required? **Recommend required** (it is a fixed enum from the CRM).
- [ ] Agree body shape, 500 msgs / 1 MB limit, and `response_id` (yes). Reply to the CRM team with these.
- [ ] Who approves prompt drafts (same super admins, or someone else)?

### Phase 1 — Receiver + `response_id` (required by the CRM)
Files: new `app/db.py`, new `app/feedback.py`; edit `app/main.py` (router), `app/crm_chat.py`, `app/config.py`, `docker-compose.yml`.

1. `db.py`: connect, create tables if missing, helper functions.
2. `feedback.py`: Pydantic models mirroring the CRM body. `category` is an enum of the 9 values. `expected_reply` required. Enforce body size and transcript length (reject over 1 MB / 500 msgs with 422).
3. Endpoint `POST /api/v2/feedback`: reuse `_require_key` from `crm_chat.py`.
   - `INSERT` with `feedback_id` as primary key. A conflict means duplicate → return `200 {received:true, duplicate:true, ref}`.
   - Because the primary key is the dedupe, no Redis lock is needed, so it works when Redis is off. If a concurrent insert races, the loser sees the conflict and returns the duplicate response. The `409` in the CRM contract is only for "still being stored"; with a single-statement insert it will effectively never occur — we keep it documented but unused.
   - Response codes: `200` ok/duplicate, `401` bad key (existing behaviour), `422` bad body with a clear `error` text (the CRM shows it), `5xx` on our own failure.
4. Logging: mask phone and session ids; never log the transcript body. Follow the existing `mask_session_id` pattern and `test_log_hygiene.py`.
5. `response_id`: add `response_id: str | None` to `ChatV2Response`, generate one per reply, write a `response_log` row. Additive field, so existing callers are unaffected.
6. Tests (`tests/test_feedback.py`): no/invalid key; valid insert; duplicate returns same `ref`; bad category 422; oversize 422; response contains `response_id`; log hygiene.
7. Update `CHAT_API.md` and add `FEEDBACK_API.md`. Run `graphify update .`.

**Done when:** the CRM can POST an item, retry it, and get a stable answer; items survive a backend restart; tests pass.

### Phase 2 — Prompt store (editable prompts in SQLite)
Files: `app/db.py`, new `app/prompt_store.py`; edit `app/llm.py`, `app/config.py`; new `backend/data/locked/`.

1. **Seed:** on startup, if a prompt has no row in `prompts`, insert the current `data/*.md` file as version 1, source `seed`. `prompt_history` starts empty.
2. **Loader:** `prompt_store.get(name)` returns the live content from an in-memory cache. `reload()` clears the cache. If the DB read fails, fall back to the file in git and log a warning — Bucky must never run without a prompt.
3. Replace the file reads in `llm.py` (`_base_prompt_cache`, `_whatsapp_style_cache`, around lines 118–131 and ~362) with `prompt_store.get(...)`. Keep the `{name}` / `{desk_phone}` substitutions as they are.
4. Move the safety/handoff/"you may not change" instructions into `data/locked/*.md`, always appended last. These stay in git and are read from files.
5. **Swap function** `prompt_store.apply(name, new_content, source, by)`: one transaction implementing section 3.1 (delete oldest history row if 2 exist → copy live into history → overwrite live, `version + 1`). Also `rollback(name, history_id)` (the swap in section 3.1) and `purge_expired()` (deletes history and decided drafts older than 45 days).
5a. Run `purge_expired()` at startup and then daily from a loop like `dashboard.idle_scan_loop`.
5b. Record which live versions were used on each reply (feeds `response_log.prompt_versions_json`).
6. Tests: seed creates v1 with empty history; loader returns the live version; DB failure falls back to file; **third change deletes the oldest history row and history stays at 2; history rows older than 45 days are purged; a rollback swaps live and history without deleting anything; a failure mid-swap leaves live and history unchanged (transaction);** locked text is always last in the built prompt; an edited prompt cannot remove it.

**Done when:** behaviour is identical to today with the DB as the source, and a manual `apply()` changes Bucky's next reply after `reload()`.

### Phase 3 — Proposer + review (turns feedback into drafts)
Files: new `app/feedback_proposer.py`, new `app/admin_prompts.py`; edit `app/main.py`.

1. **Proposer** (background loop like `dashboard.idle_scan_loop`, or a manual trigger first): for each `received` item, give a model the item, transcript, and the live content of the allowlisted prompts. Ask for: which prompt to change (from the allowlist) and the new full text, plus a one-line reason.
2. Validate the output (allowlist, size, shrink limits). Insert a `draft` version. Set the feedback status `proposed`. Use `litellm` like the rest of the app; low frequency, so cost is small.
3. **Admin endpoints**, protected by a **separate** `ADMIN_API_KEY` (not the CRM chat key):
   - `GET /admin/prompts/drafts` — list pending drafts with a diff against the live prompt; also list each prompt's history (up to 5 old versions with their `retired_at` and days left).
   - `POST /admin/prompts/{id}/approve` and `/reject`.
   - `POST /admin/prompts/{name}/rollback/{version}`.
4. Approve = run the golden check (section 8); if it passes, call `prompt_store.apply()` (section 3.1), mark the draft approved, `reload()`.
5. Tests: invalid proposer output is rejected; approve makes the draft live and moves the old live prompt into history; rollback restores a history prompt; reject leaves everything unchanged; admin endpoints need the admin key.

**Done when:** an approved feedback item produces a draft, a human can approve it, and Bucky's replies change; a rollback restores the old behaviour.

### Phase 4 — Callback to the CRM (optional)
1. Env: `CRM_FEEDBACK_WEBHOOK_URL`, `CRM_FEEDBACK_WEBHOOK_SECRET` (blank = off, like the summary webhook).
2. On apply/reject, POST `{feedback_id, status, note, at}` with `X-Bucky-Signature: sha256=<HMAC-SHA256 of the raw body>`. Note the CRM's format is a plain body signature, **not** the timestamped `<ts>.<body>` format that `dashboard._sign_webhook` uses, so write a small separate signer.
3. Retry with backoff and never block the review flow on failure.
4. Tests: signature value, retries, off-when-blank.

### Phase 5 — Operations
- Nightly SQLite backup to S3; test a restore once.
- Admin visibility: count of items by status (a simple `GET /admin/feedback/stats`).
- Retention: decide how long raw transcripts are kept (they contain customer chat).

## 7. Config added

| Env var | Purpose |
|---|---|
| `DB_PATH` | SQLite file, default `/data/bucky.db` |
| `ADMIN_API_KEY` | Protects `/admin/*`; blank = admin endpoints disabled |
| `FEEDBACK_MAX_BYTES` | Default 1,000,000 |
| `FEEDBACK_MAX_MESSAGES` | Default 500 |
| `PROPOSER_ENABLED` | Default false until Phase 3 is reviewed |
| `PROMPT_HISTORY_KEEP` | Old prompts kept per name, default 5 |
| `PROMPT_HISTORY_DAYS` | Days an old prompt is kept, default 45 |
| `CRM_FEEDBACK_WEBHOOK_URL` / `_SECRET` | Phase 4 callback; blank = off |

## 8. Safety checks

1. **Golden chats:** a small fixed set of test conversations (handoff, price quoting, safety reassurance, language mirroring, OTP). Before a draft goes active, run them against the new prompt and compare with expected rules (for example "must hand off for refund requests"). A failure blocks activation.
2. **Feedback is untrusted input.** A customer message inside a transcript, or the agent's `expected_reply`, could contain instructions aimed at the proposer model. The proposer prompt must say the content is data, not instructions, and the human diff review is the real defence.
3. **No auto-apply** in the first version, even for "tone" feedback.
4. **Rate and size limits** on `/api/v2/feedback`, since it is an authenticated machine endpoint.
5. **PII:** store the masked phone only (pending Phase 0), keep transcripts out of logs, and set a retention period.

## 9. Risks and open points

| Risk | Mitigation |
|---|---|
| SQLite file lost with the box | Volume + nightly S3 backup; the files in git act as the seed/fallback |
| Several backend instances later | Move `db.py` to RDS; it is the only module that touches the DB |
| A bad draft goes live | Human approval + golden checks + one-step rollback to any of the last 5 live prompts (45 days) |
| Prompt edits are no longer visible in git | Version rows with author and time; the admin diff view |
| Feedback conflicts with locked rules | Locked prompt is last and wins; proposer is told to ignore requests to change locked behaviour |
| Prompt caching cost | A new version changes the cached prompt once; expected and small |

## 10. Suggested build order

1. Phase 0 answers, and a reply to the CRM team (so the contract is frozen).
2. Phase 1 — unblocks the CRM team. Ship first.
3. Phase 2 — then Phase 3, in that order. Do not start Phase 3 before the golden checks exist.
4. Phase 4 and 5 once the loop works end to end.

## 11. Reply to send to the CRM team (draft)

- Body shape is workable as proposed.
- Please mask the phone (`91886******03`) and omit the name.
- 500 messages / 1 MB is fine.
- `category` is required.
- We will add `response_id` to `/api/v2/chat` in the same release.
- We will build the status callback later; signing will be `sha256=<HMAC of raw body>` as you proposed.
