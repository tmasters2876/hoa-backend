# CLAUDE.md

## Project Summary

`hoa-backend` is a small Flask API that answers homeowner questions about HOA rules. It accepts a natural-language question, finds relevant clauses in Supabase, sends the matched clauses to OpenAI, and returns either markdown/HTML-ish text or JSON.

This repo also includes a local-only admin interface in `admin_app.py` for maintaining the Supabase `clauses` table on a Mac without deploying anything.

This repo appears to be the simple production branch. There is no README in the repo as of this audit, so the source files are the primary documentation.

## Runtime Architecture

### Web layer
- `app.py` creates the Flask app and enables global CORS.
- `POST /ask` reads JSON input and delegates all business logic to `answer_question()` in `ask_gpt.py`.
- `POST /log` forwards question/answer/ip data to a hardcoded Google Apps Script endpoint for logging.
- `admin_app.py` runs a separate local Flask app for clause search, inline edits, deletes, adds, embedding refresh, and search testing.

### AI / retrieval layer
- `ask_gpt.py` loads env vars with `load_dotenv()`.
- It initializes:
  - shared client helpers from `services.py`
  - `OpenAI` using `OPENAI_API_KEY`
  - Supabase using `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`
- Retrieval flow:
  1. Generate an embedding for the user question using `text-embedding-ada-002`.
  2. Call Supabase RPC `match_clauses` with that embedding.
  3. If fewer than 5 vector matches are returned, run a fallback `ilike("plain_summary", f"%{question}%")` query on the `clauses` table.
  4. Keep only unique clauses whose `match_source` is `"Vector Match"`.
  5. If nothing remains, fetch a soft fallback set by querying `clauses.tags` for `["shed", "structure", "placement", "approval"]`.
  6. Format the clauses into HTML snippets and send them to OpenAI `gpt-4o` for final response generation.

### Special behavior
- `check_instant_whimsy()` returns canned responses for:
  - creator/developer questions
  - feedback/complaint questions
  - fantasy/dragon/wizard style questions
- If whimsy triggers, the app returns early and skips Supabase/OpenAI retrieval.

### Admin tool behavior
- `admin_app.py` is intended for local use at `http://127.0.0.1:5050`.
- It supports:
  - browse/search by keyword, tag, and document
  - add clauses manually
  - inline editing of `clause_text`, `plain_summary`, tags, citation, page, link, and `precedence_level`
  - delete clauses
  - mark embeddings stale by setting `embedding = null` when text changes
  - regenerate embeddings through OpenAI using the current OpenAI embedding model
  - test semantic search by calling the existing `match_clauses` RPC and showing keyword fallback results alongside it

## API Endpoints

### `POST /ask`
- File: `app.py`
- Request JSON:
  - `question` string
  - `mode` optional, default `"default"`
  - `tags` optional list, default `[]`
  - `output_format` optional, default `"markdown"`
- Behavior:
  - Calls `answer_question(question, mode, tags, output_format)`
  - Returns JSON if `output_format == "json"`
  - Otherwise returns a text response with content type `text/markdown`

### `POST /log`
- File: `app.py`
- Request JSON:
  - `question`
  - `answer`
  - `ip`
- Behavior:
  - Sends payload to a hardcoded Google Apps Script URL using `requests.post`
  - Returns `{status: "logged", code: <status>}` on success

### Local admin endpoints
- `GET /admin`
- `POST /admin/clauses`
- `POST /admin/clauses/<id>/update`
- `POST /admin/clauses/<id>/delete`
- `POST /admin/clauses/<id>/regenerate-embedding`
- `GET /health`

## Supabase Integration

Supabase is a core dependency in this repo.

### Environment variables
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

### Client creation
- In `ask_gpt.py`, the code creates a Supabase client at import time using `create_client(...)`.

### Supabase usage
- RPC:
  - `supabase.rpc("match_clauses", {...}).execute()`
  - Expected to return semantic/vector matches from a database-side function.
- Table queries:
  - `supabase.from_("clauses").select("*").ilike("plain_summary", f"%{question}%")`
  - Optional filters on `tags`, `structure_type`, and `concern_level`
  - `supabase.from_("clauses").select("*").contains("tags", general_tags).limit(5)`

### Expected schema assumptions
The code assumes a `clauses` table and/or RPC results containing fields such as:
- `id`
- `clause_id`
- `plain_summary`
- `summary`
- `citation`
- `link`
- `document`
- `precedence_level`
- `tags`
- `structure_type`
- `concern_level`

It also assumes the existence of an RPC function named `match_clauses`.

## File Structure

Top-level files present during this audit:

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
│   └── admin_index.html
├── test_summary_query.py
└── uv.lock
```

Notes:
- `.git/` exists but is omitted from the main tree above because it is repo metadata, not app code.
- There is no `README.md` in this repository.
- There is no `tests/` directory or formal test suite.

## Deployment / Environment

### Render
- `render.yaml` deploys the app as a Python web service.
- Build command: `pip install -r requirements.txt`
- Start command: `python app.py`

### Local env sample
- `.env.example` contains:
  - `OPENAI_API_KEY`
  - `SUPABASE_URL`
  - `SUPABASE_SERVICE_ROLE_KEY`

### Local admin run command
- `python admin_app.py`
- Defaults to `127.0.0.1:5050`

## Important Observations

### Likely broken or risky
- `app.py` assumes `request.get_json()` returns a dict. If the body is missing or invalid JSON, `data.get(...)` will raise because `data` can be `None`.
- `POST /log` uses a hardcoded external Google Apps Script URL. That is brittle and not configurable.
- The service role key is used directly in app code. That key has elevated privileges and should be treated as highly sensitive.
- `answer_question()` accepts `mode`, `structure_type`, and `concern_level`, but `/ask` only passes `mode` and never forwards `structure_type` or `concern_level`.
- `mode` is effectively unused in the answer pipeline.
- `fetch_matching_clauses()` appends fallback keyword matches, but `answer_question()` later keeps only entries whose `match_source` is `"Vector Match"`. That means keyword fallback results are effectively discarded unless no vector results exist and the soft fallback runs. This looks like a logic bug.
- The default embedding model is still `text-embedding-ada-002` unless overridden via `OPENAI_EMBEDDING_MODEL`, which is an older model choice and may be a maintenance risk.
- The default generation model is `gpt-4o` unless overridden via `OPENAI_CHAT_MODEL`.
- `pyproject.toml` is stale/inaccurate. It names the package `python-template` and declares no dependencies, while the real dependencies live in `requirements.txt`.
- `uv.lock` is effectively empty/minimal and does not reflect the actual dependency set.
- `.replit` references `main.py`, but this repo’s entry file is `app.py`. That config is stale/broken for Replit usage.
- `test_summary_query.py` is not a real automated test; it is a manual script that prints a query result.
- `test_summary_query.py` queries `summary`, while the main fallback query in `ask_gpt.py` uses `plain_summary`. That may indicate a schema mismatch or drift.
- `generated-icon.png` is present but unrelated to runtime behavior.
- `admin_app.py` uses a local Flask secret key constant. That is acceptable for local-only use, but should move to env if the tool ever becomes shared.

### Code quality / maintenance opportunities
- Add a real `README.md` explaining setup, API contract, Supabase schema, and deployment.
- Add input validation for `/ask` and `/log`.
- Move hardcoded URLs and model names into environment variables.
- Add timeouts and error handling around outbound logging requests.
- Add structured logging instead of returning raw exception strings to clients.
- Replace the current fallback merge logic so keyword matches can actually contribute to results.
- Add automated tests for:
  - whimsy paths
  - Supabase fallback behavior
  - `/ask` JSON vs markdown responses
  - invalid request bodies
- Consider separating retrieval, prompt formatting, and response rendering into smaller modules.

## Known Entry Points

- App server entry: `python app.py`
- Admin tool entry: `python admin_app.py`
- Main request handler: `app.py` -> `/ask`
- Core business logic: `ask_gpt.py` -> `answer_question()`
- Shared service wiring: `services.py`
- Manual DB probe: `test_summary_query.py`

## Practical Notes For Future Sessions

- Start debugging in `app.py` for HTTP-level behavior.
- Start debugging in `ask_gpt.py` for retrieval quality, prompt construction, and OpenAI/Supabase issues.
- Start debugging in `admin_app.py` for clause maintenance workflows.
- If answers are weak, inspect:
  - Supabase RPC `match_clauses`
  - clause schema/contents
  - the vector-vs-fallback deduplication logic
- If deployment fails, check `render.yaml`, `requirements.txt`, and the mismatch with `pyproject.toml` / `.replit`.
