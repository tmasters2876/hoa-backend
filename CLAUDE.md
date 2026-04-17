# CLAUDE.md

## Project Summary

`hoa-backend` is a Flask API that answers homeowner questions about PLCA (Plantation Lakes Community Association) HOA rules. It loads all approved governing document clauses from Supabase into memory at startup and uses GPT-4o to answer questions by reading the full clause corpus directly.

This repo also includes a local-only admin interface in `admin_app.py` for maintaining the Supabase `clauses` table on a Mac without deploying anything.

This is the **production branch**. The dev/experimental branch lives at `../hoa-backend-dev/`.

---

## Runtime Architecture

### Web layer
- `app.py` creates the Flask app and enables global CORS.
- `POST /ask` reads JSON input and delegates all business logic to `answer_question()` in `ask_gpt.py`.
- `POST /log` forwards question/answer/ip data to a hardcoded Google Apps Script endpoint for logging.
- `admin_app.py` runs a separate local Flask app for clause search, inline edits, deletes, adds, embedding refresh, search testing, pending change approvals, revision flags, and GPT-powered admin search.

### AI / answer layer (`ask_gpt.py`)

**Architecture: Full-corpus in-memory reasoning**

`ask_gpt.py` no longer uses vector search or keyword retrieval. It loads ALL approved clauses from Supabase once at startup into `_clause_cache` and passes the entire corpus to GPT for every question.

**Flow:**
1. `get_all_clauses()` — paginates through `clauses` table (page size 1000), filters `status = "approved"`, caches result in `_clause_cache` (module-level global). Cache persists for the lifetime of the process.
2. `format_all_clauses_for_gpt(clauses)` — formats every clause as `[CLAUSE_ID|DOC_SHORT|CITATION]\nSUMMARY | FULL TEXT: ...` (summary + full clause text, capped at 400 chars). Uses `DOC_SHORT` map to abbreviate long PDF filenames (e.g. `CCR`, `BG2022`, `TXC209`). Sorted by `precedence_level` ascending (highest authority first).
3. GPT call — `gpt-4o` receives the full formatted corpus in the user prompt and is instructed to cite clauses using `[CLAUSE_ID]` bracket notation. Temperature 0.1.
4. Post-processing pipeline (in order):
   a. Normalize malformed brackets: `[WALLS_01|BG2022|Page 13]` → `[WALLS_01]`
   b. Capture `raw_cited_ids` from cleaned response (before link injection)
   c. Replace `[CLAUSE_ID]` with linked HTML citations using `DOC_SHORT_DISPLAY` map for human-readable document names (e.g. "CCRs, Article VI, Section 3")
5. Build `cited_clauses` list from `raw_cited_ids` using the `by_id` lookup. Cap Texas Property Code to 1 display result. If no cited clauses, keyword-score `all_clauses` to find a fallback set.
6. Return: if `output_format == "json"`, return dict with `answer`, `clauses`, `question`, `mode`, `format`. Otherwise return `final_answer` (inline linked citations are sufficient; clause cards appended only when fallback path fires).

**Key functions:**
- `get_all_clauses()` — loads and caches full approved clause corpus
- `format_all_clauses_for_gpt(clauses)` — formats corpus for GPT prompt
- `format_clauses_for_display(clauses)` — formats clause cards for UI display
- `check_instant_whimsy(question_lower)` — returns canned responses for creator/dragon/whimsy questions; bypasses retrieval entirely
- `answer_question(question, ...)` — main entry point; called by `app.py` and `admin_app.py`

**Document authority order (encoded in system prompt):**
1. Texas Property Code
2. CCRs & Declarations
3. CCR Amendments
4. Articles of Incorporation
5. Bylaws
6. Board Resolutions & Clarifying Resolutions
7. Specific Regulations (Solar, Flags, Rain Barrels, etc.)
8. 2022 Builders Guidelines

**CCR delegation exception:** The system prompt instructs GPT that when a CCR delegates to the Builders Guidelines ("per the Builder Guidelines" / "as approved by the ARC"), the Builders Guidelines rule is authoritative on that topic — not overridden by the CCR. GPT cites both documents in those cases.

**Citation grouping:** GPT is instructed to group related rules in one paragraph with a single citation at the end, not to cite the same document repeatedly per sentence.

### Special behavior
- `check_instant_whimsy()` returns canned responses for creator/developer questions and fantasy/dragon/wizard questions. Returns early, skips Supabase/GPT.

### Admin tool (`admin_app.py`)
- Intended for local use at `http://127.0.0.1:5051`.
- Runs on port `PORT` env var (default 5051).
- Three user tiers:
  - **Superusers** (`SUPERUSERS = {'tmasters', 'cmasters', 'admin'}`) — full access
  - **Approvers** (`is_approver=True` in `admin_users` table) — can close/reopen revision flags; no clause edit approval or user management rights
  - **Regular users** — browse, flag, comment only
- Capabilities: browse/search clauses, add/edit/delete clauses, embedding regeneration, pending changes workflow, bulk CSV import/delete, user management, audit log, CCR revision flags, admin GPT search.
- **Admin Search (`/admin/search`)** — calls `answer_question(output_format="json")` from `ask_gpt.py`; returns answer + cited clause IDs for the flag UI.

---

## API Endpoints

### `POST /ask`
- File: `app.py`
- Request JSON: `question`, `mode` (optional), `tags` (optional), `output_format` (optional, default `"markdown"`)
- Returns JSON if `output_format == "json"`, otherwise HTML/markdown text

### `POST /log`
- File: `app.py`
- Request JSON: `question`, `answer`, `ip`
- Sends payload to a hardcoded Google Apps Script URL

### Local admin endpoints
- `GET /admin` — clause browse/search dashboard
- `POST /admin/clauses` — add clause
- `POST /admin/clauses/<id>/update` — edit clause (form)
- `POST /admin/clauses/<id>/update-json` — edit clause (AJAX/JSON)
- `POST /admin/clauses/<id>/delete` — submit deletion
- `POST /admin/clauses/<id>/regenerate-embedding` — refresh embedding
- `GET /admin/import/template` — download CSV template
- `POST /admin/import` — bulk import clauses from CSV
- `GET /admin/bulk-delete/template` — download bulk delete template
- `POST /admin/bulk-delete` — bulk delete clauses by clause_id from CSV
- `GET /admin/pending` — list pending changes
- `POST /admin/pending/<id>/approve` — approve pending change
- `POST /admin/pending/<id>/reject` — reject pending change
- `GET /admin/audit` — audit log
- `GET /admin/users` — user management
- `POST /admin/users` — create user
- `POST /admin/users/<id>/toggle` — activate/deactivate user
- `POST /admin/users/<id>/toggle-approver` — grant/revoke Approver role
- `POST /admin/users/<id>/delete` — delete user
- `POST /admin/users/<id>/reset-password` — reset user password
- `POST /admin/users/change-password` — change own password
- `GET /admin/flags` — list revision flags
- `GET /admin/flags/<id>` — flag detail
- `POST /admin/flags` — create flag (clause or topic type)
- `POST /admin/flags/<id>/comment` — add comment to flag
- `POST /admin/flags/<id>/status` — update flag status
- `GET /admin/search` — admin GPT Q&A search with flag capability
- `GET /health` — health check

---

## Supabase Integration

### Environment variables
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

### Client creation
- In `ask_gpt.py`: created at import time with `create_client(...)`.
- In `admin_app.py`: via `get_supabase_client()` from `services.py` (LRU-cached).

### Tables used
- `clauses` — main clause store. Key fields: `id`, `clause_id`, `document`, `page`, `citation`, `clause_text`, `plain_summary`, `link`, `embedding`, `tags`, `created_at`, `precedence_level`, `status`
- `pending_changes` — staged clause edits awaiting approval
- `clause_audit_log` — all audit events
- `user_activity_log` — login/logout/session events
- `admin_users` — admin console users. Key fields: `id`, `username`, `password_hash`, `is_active`, `must_change_password`, `is_approver`
- `clause_flags` — CCR revision flags. Key fields: `id`, `flag_type` (clause/topic), `clause_id`, `flagged_by`, `flag_notes`, `status`, `resolution_notes`, `closed_by`, `question_text`, `answer_snapshot`, `cited_clause_ids` (text[]), `created_at`, `updated_at`
- `clause_flag_comments` — threaded comments on flags
- RPC: `match_clauses` — vector similarity search (used by admin_app.py embedding regeneration; no longer used by ask_gpt.py answer pipeline)

---

## File Structure

```text
hoa-backend/
├── .env.example
├── .gitignore
├── .replit
├── CLAUDE.md
├── admin_app.py
├── app.py
├── ask_gpt.py
├── generated-icon.png
├── pyproject.toml
├── render.yaml
├── requirements.txt
├── services.py
├── templates/
│   ├── admin_base.html
│   ├── admin_index.html
│   ├── admin_pending.html
│   ├── admin_audit.html
│   ├── admin_users.html
│   ├── admin_login.html
│   ├── admin_change_password.html
│   ├── admin_flags.html
│   ├── admin_flag_detail.html
│   └── admin_search.html
├── tests/
│   ├── __init__.py
│   └── test_approval.py
└── uv.lock
```

---

## Deployment / Environment

### Render
- `render.yaml` deploys as a Python web service.
- Build: `pip install -r requirements.txt`
- Start: `python app.py`

### Local env
- Copy `.env.example` → `.env` and fill: `OPENAI_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SECRET_KEY`

### Local run
```bash
cd hoa-backend
source venv/bin/activate
python app.py          # public API on port 8080
python admin_app.py    # admin tool on port 5051
```

---

## Important Observations

### Known issues / risks
- `ask_gpt.py` passes the FULL clause corpus to GPT on every request. If the corpus grows very large (thousands of clauses), token limits and latency will become a concern.
- `_clause_cache` is a module-level global — it persists for the lifetime of the process. Restarting the server clears it. On Render (free tier), the process restarts frequently.
- `app.py` assumes `request.get_json()` returns a dict — will raise if body is missing/invalid JSON.
- `POST /log` uses a hardcoded external Google Apps Script URL.
- The service role key is used directly in app code — treat as highly sensitive.
- `answer_question()` accepts `mode`, `structure_type`, `concern_level`, `tags` parameters but they are unused in the current full-corpus pipeline.
- The default generation model is `gpt-4o` (via `OPENAI_CHAT_MODEL` env override).
- `pyproject.toml` is stale — names the package `python-template` and declares no real dependencies.

### Code quality / maintenance notes
- `tests/test_approval.py` covers `submit_pending_change`, `_apply_pending_change`, and the `/admin/pending/<id>/approve` route with full mocking.
- `ask_gpt.py` and `admin_app.py` both create their own Supabase clients independently.
- The `DOC_SHORT` map (for GPT payload) and `DOC_SHORT_DISPLAY` map (for citation links) must be kept in sync when new documents are added to Supabase.

---

## Practical Notes For Future Sessions

- **answer pipeline**: all logic in `ask_gpt.py` → `answer_question()`. No vector search — full corpus loaded via `get_all_clauses()`.
- **citation post-processing order**: (1) normalize malformed brackets, (2) capture `raw_cited_ids`, (3) replace brackets with HTML anchor tags. Do not reorder these steps.
- **admin search**: `admin_app.py` `/admin/search` calls `answer_question(output_format="json")` — do not re-add the old `fetch_matching_clauses` import.
- **adding new documents**: update `DOC_SHORT` in `format_all_clauses_for_gpt` and `DOC_SHORT_DISPLAY` in `answer_question`.
- **weak answers**: check clause `plain_summary` and `clause_text` field quality in Supabase; the 400-char cap truncates long clauses.
- **deployment issues**: check `render.yaml`, `requirements.txt`, and the stale `pyproject.toml` mismatch.
- **admin_app debugging**: revision flag routes at the bottom of `admin_app.py`; `inject_superuser` context processor runs on every template render.
