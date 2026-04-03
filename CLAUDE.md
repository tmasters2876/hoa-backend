# CLAUDE.md

## Project Summary

`hoa-backend` is a small Flask API that answers homeowner questions about HOA rules. It accepts a natural-language question, finds relevant clauses in Supabase, sends the matched clauses to OpenAI, and returns either markdown/HTML-ish text or JSON.

This repo also includes a local-only admin interface in `admin_app.py` for maintaining the Supabase `clauses` table on a Mac without deploying anything.

This is the **production branch**. The dev/experimental branch lives at `../hoa-backend-dev/`.

## Runtime Architecture

### Web layer
- `app.py` creates the Flask app and enables global CORS.
- `POST /ask` reads JSON input and delegates all business logic to `answer_question()` in `ask_gpt.py`.
- `POST /log` forwards question/answer/ip data to a hardcoded Google Apps Script endpoint for logging.
- `admin_app.py` runs a separate local Flask app for clause search, inline edits, deletes, adds, embedding refresh, search testing, pending change approvals, revision flags, and GPT-powered admin search.

### AI / retrieval layer
- `ask_gpt.py` loads env vars with `load_dotenv()`.
- It initializes:
  - `OpenAI` using `OPENAI_API_KEY`
  - Supabase using `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`
- Retrieval flow (`fetch_matching_clauses`):
  1. Generate an embedding for the user question using `text-embedding-ada-002`.
  2. Call Supabase RPC `match_clauses` with that embedding.
  3. If fewer than 5 vector matches are returned, run keyword fallback queries.
  4. Score and dedupe candidates; keep only those above a 0.5 threshold, up to 5.
  5. **Document diversity enforcement**: if all top results are from `"Texas Property Code"`, replace the lowest-scoring entry with the highest-scoring non-TX-Code clause found in the scored list.
  6. If nothing remains after threshold, take top 2 regardless.
  7. Format the clauses into HTML snippets and send them to OpenAI `gpt-4o` for final response generation.

### Special behavior
- `check_instant_whimsy()` returns canned responses for creator/developer questions, feedback/complaint questions, and fantasy/dragon/wizard style questions.
- If whimsy triggers, the app returns early and skips Supabase/OpenAI retrieval.

### Admin tool behavior
- `admin_app.py` is intended for local use at `http://127.0.0.1:5051`.
- Runs on port `PORT` env var (default 5051).
- Three user tiers:
  - **Superusers** (`SUPERUSERS = {'tmasters', 'cmasters', 'admin'}`) — full access
  - **Approvers** (`is_approver=True` in `admin_users` table) — can close/reopen revision flags; no clause edit approval or user management rights
  - **Regular users** — browse, flag, comment only
- It supports:
  - Browse/search by keyword, tag, and document
  - Add clauses manually (with optional self-approve for superusers)
  - Inline editing of clause fields (AJAX, with optional self-approve)
  - Delete clauses (submit for approval)
  - Mark embeddings stale / regenerate via OpenAI
  - Pending changes workflow (submit → approve/reject by second superuser)
  - Source verification via PDF fuzzy-match against Google Drive links
  - Bulk CSV import and bulk CSV delete (superusers only)
  - User management (create, activate/deactivate, delete, reset password, grant/revoke Approver role)
  - Audit log (all clause changes, approvals, user actions)
  - **CCR Revision Flags** — flag individual clauses or full GPT Q&A topics for revision committee review; threaded comments; status workflow (open → in_review → closed)
  - **Admin Search** (`/admin/search`) — GPT-powered Q&A using the same retrieval logic as the resident chatbot; cited clause cards with Flag buttons; "Flag this Topic" for topic-level flags

## API Endpoints

### `POST /ask`
- File: `app.py`
- Request JSON: `question`, `mode` (optional), `tags` (optional), `output_format` (optional, default `"markdown"`)
- Returns JSON if `output_format == "json"`, otherwise `text/markdown`

### `POST /log`
- File: `app.py`
- Request JSON: `question`, `answer`, `ip`
- Sends payload to a hardcoded Google Apps Script URL; returns `{status: "logged", code: <status>}`

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

## Supabase Integration

### Environment variables
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

### Client creation
- In `ask_gpt.py`: created at import time with `create_client(...)`.
- In `admin_app.py`: via `get_supabase_client()` from `services.py` (LRU-cached; requires env vars set before import).

### Tables used
- `clauses` — main clause store. Key fields: `id`, `clause_id`, `document`, `page`, `citation`, `clause_text`, `plain_summary`, `link`, `embedding`, `match_source`, `tags`, `created_at`, `precedence_level`, `status`
- `pending_changes` — staged clause edits awaiting approval
- `clause_audit_log` — all audit events
- `user_activity_log` — login/logout/session events
- `admin_users` — admin console users. Key fields: `id`, `username`, `password_hash`, `is_active`, `must_change_password`, `is_approver`
- `clause_flags` — CCR revision flags. Key fields: `id`, `flag_type` (clause/topic), `clause_id` (text, references `clauses.clause_id`), `flagged_by`, `flag_notes`, `status`, `resolution_notes`, `closed_by`, `question_text`, `answer_snapshot`, `cited_clause_ids` (text[]), `created_at`, `updated_at`
- `clause_flag_comments` — threaded comments on flags. Key fields: `id`, `flag_id`, `author`, `comment`, `created_at`
- RPC: `match_clauses` — vector similarity search

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

## Deployment / Environment

### Render
- `render.yaml` deploys as a Python web service.
- Build: `pip install -r requirements.txt`
- Start: `python app.py`

### Local env
- Copy `.env.example` → `.env` and fill: `OPENAI_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SECRET_KEY`

### Local admin run
- `python admin_app.py` — defaults to port 5051

## Important Observations

### Known issues / risks
- `app.py` assumes `request.get_json()` returns a dict — will raise if body is missing/invalid JSON.
- `POST /log` uses a hardcoded external Google Apps Script URL.
- The service role key is used directly in app code — treat as highly sensitive.
- `answer_question()` accepts `mode`, `structure_type`, `concern_level` but `/ask` only passes `mode`.
- `mode` is effectively unused in the answer pipeline.
- The default embedding model is `text-embedding-ada-002` (via `OPENAI_EMBEDDING_MODEL` env override).
- The default generation model is `gpt-4o` (via `OPENAI_CHAT_MODEL` env override).
- `pyproject.toml` is stale — names the package `python-template` and declares no real dependencies.
- `.replit` references `main.py` but entry file is `app.py` — stale for Replit usage.
- `test_summary_query.py` is a manual print script, not an automated test.
- `_is_approver()` in `admin_app.py` makes a Supabase query on every request via `inject_superuser` — acceptable for local-only use at low concurrency.

### Code quality / maintenance notes
- `tests/test_approval.py` covers `submit_pending_change`, `_apply_pending_change`, and the `/admin/pending/<id>/approve` route with full mocking.
- Keyword fallback deduplication logic vs vector-only filter in `answer_question()` may still discard keyword results — verify behavior if search quality regresses.
- `ask_gpt.py` and `admin_app.py` both create their own Supabase clients independently.

## Practical Notes For Future Sessions

- Start debugging HTTP-level behavior in `app.py`.
- Start debugging retrieval quality, prompt construction, and OpenAI/Supabase issues in `ask_gpt.py`.
- Start debugging clause maintenance workflows in `admin_app.py`.
- Revision flag routes all live at the bottom of `admin_app.py` above `if __name__ == "__main__"`.
- `inject_superuser` context processor runs on every request that renders a template — it queries both `pending_changes` and `clause_flags` counts plus `_is_approver()`.
- If answers are weak, inspect: Supabase RPC `match_clauses`, clause schema/contents, vector-vs-fallback scoring, document diversity enforcement.
- If deployment fails, check `render.yaml`, `requirements.txt`, and the mismatch with `pyproject.toml`.
