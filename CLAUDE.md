# CLAUDE.md — PLCA HOA Backend

Last updated: April 2026

## Project Overview

This repo powers two production services for Plantation Lakes Community Association (PLCA), an HOA spanning Waller and Grimes Counties, Texas.

- **hoa-backend** — public-facing Flask API answering resident questions via a Carrd-embedded chatbot
- **hoa-admin** — protected admin console (Flask + Jinja2) for board members and staff to manage the clause database, review pending changes, manage users, and run the CCR revision flag workflow

**hoa-admin has no dev environment.** Changes go directly to production. Changes to hoa-backend are always tested on the dev service first (`../hoa-backend-dev/`), then applied independently to prod.

---

## Repository & Deployment

- **GitHub repo**: tmasters2876/hoa-backend (single repo, two Render services)
- **Branch**: main — push to GitHub triggers auto-deploy on Render
- **hoa-backend Render service**: public API, start command `python app.py`, port 5000
- **hoa-admin Render service**: admin console, start command `python admin_app.py`, port assigned dynamically via `PORT` env var, URL: hoa-admin.onrender.com
- **Local dev**:
  - Admin: port 5051
  - Backend: port 5000
  - Always use full venv path: `/Users/thomasmasters/Projects/.venv/bin/python3`
  - Never use system `python3` (aliased to Homebrew)

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.13 |
| Framework | Flask |
| Database | Supabase (PostgreSQL + pgvector) |
| Embeddings | OpenAI text-embedding-ada-002 |
| Chat model | GPT-4o (via ask_gpt.py) |
| Auth | bcrypt (rounds=12) + Flask sessions |
| PDF verification | pdfplumber + rapidfuzz |
| Hosting | Render (two services) |
| Version control | GitHub |

---

## Key Files

```
hoa-backend/
├── app.py               — public Flask API (Carrd calls this)
├── admin_app.py         — admin console (~1963 lines)
├── ask_gpt.py           — HOA answer logic (full-corpus GPT-4o approach)
├── services.py          — shared Supabase + OpenAI clients
├── requirements.txt
├── render.yaml          — Render deployment config
├── CLAUDE.md
└── templates/
    ├── admin_base.html
    ├── admin_index.html
    ├── admin_login.html
    ├── admin_users.html
    ├── admin_pending.html
    ├── admin_audit.html
    ├── admin_search.html
    ├── admin_flags.html
    ├── admin_flag_detail.html
    └── admin_change_password.html
```

---

## Architecture

### hoa-backend (public API)

`POST /ask` accepts `{ question, mode?, tags?, output_format? }` and delegates to `answer_question()` in `ask_gpt.py`. Returns JSON if `output_format == "json"`, otherwise HTML/markdown text.

`POST /log` forwards question/answer/ip to a hardcoded Google Apps Script URL for logging (not configurable via env var).

### ask_gpt.py — Full-Corpus Approach (with relevance pre-filter)

**Vector search was removed** from the resident-facing path in favor of full-corpus prompting; **a lightweight keyword/tag relevance pre-filter was added** (April 2026) to cut prompt size without reintroducing embeddings.

1. `get_all_clauses()` — fetches all `status='approved'` clauses (including `tags`) from Supabase using paginated `range()` calls (page size 1000). Cached in `_clause_cache` (module-level global) for the lifetime of the process.
2. `filter_relevant_clauses(question, all_clauses, tags=None, min_results=15, min_score=2)` — scores each clause by keyword overlap with the question plus a higher-weighted match against the clause's `tags` array (and any explicit `tags` the caller passes, currently inert from Carrd). Returns the relevant subset sorted by score desc / precedence asc. **Safety net**: if fewer than `min_results` clauses clear `min_score`, returns the complete unfiltered corpus — this is what keeps vague ("Tell me about the HOA") or likely-uncovered (e.g. "chickens") questions from losing context. Validated against the live corpus: narrow questions (fences, solar panels, paint) cut the prompt 79–98%. Gated behind `ENABLE_CLAUSE_PREFILTER` (default `true`; set `false` in Render env vars for an instant disable, no redeploy).
3. `format_all_clauses_for_gpt()` — sorts clauses by `precedence_level` (lower = higher authority), applies `DOC_SHORT` abbreviation map to save tokens, truncates each clause to 400 chars, formats as `[clause_id|doc_short|citation]\nsummary | FULL TEXT: clause_text`. Receives the **filtered** subset from step 2, not necessarily the full corpus.
4. `answer_question()` — sends the (possibly filtered) formatted corpus + system prompt to `gpt-4o` at temperature 0.1. GPT cites clauses using `[CLAUSE_ID]` bracket notation.
5. Post-processing (order matters — do not change) **operates on the FULL, unfiltered corpus** (`all_clauses`, not the filtered subset) — this is deliberate: it's what keeps `replace_bracketed_id()` able to resolve any clause GPT might cite, rather than silently dropping citations for clauses outside the filtered set:
   - Normalize malformed brackets: `[WALLS_01|BG2022|Page 13]` → `[WALLS_01]`
   - Capture `raw_cited_ids` from cleaned response ← **must be here, before link injection**
   - Replace `[CLAUSE_ID]` with HTML `<a>` links using `DOC_SHORT_DISPLAY` map for friendly document names
   - Cap Texas Property Code to 1 display result; keyword-scored fallback if no cited clauses
6. `check_instant_whimsy()` — intercepts creator/developer and fantasy keyword questions, returns canned response, skips GPT entirely.

Unit tests for `filter_relevant_clauses()` live in `tests/test_ask_gpt.py` (pure function, no Supabase mocking needed).

**CCR delegation rule in system prompt**: When a CCR delegates to Builders Guidelines ("per the Builder Guidelines" / "as approved by the ARC"), Builders Guidelines governs that topic. GPT cites both documents.

**Citation grouping**: GPT groups related rules in one paragraph with a single citation at the end — not one citation per sentence.

**Admin search**: `admin_app.py` `/admin/search` calls `answer_question(output_format="json")` — do not re-add old `fetch_matching_clauses` imports.

### hoa-admin (admin console)

All routes protected by `@login_required`. Session expires 8 hours. `SECRET_KEY` required in env (no default). `ProxyFix` middleware captures real IPs via `X-Forwarded-For`.

---

## Supabase Tables

### clauses (~712 rows as of April 2026)

| Column | Type | Notes |
|---|---|---|
| id | uuid | PK |
| clause_id | text | e.g. `DECL_27_08`, `BG_WALLS_01` |
| document | text | Full PDF filename or statute name |
| page | int | Source page number |
| citation | text | e.g. "Article VI, Section B" |
| clause_text | text | Verbatim source text |
| plain_summary | text | Plain-English summary |
| link | text | Google Drive URL or statutes.capitol.texas.gov |
| embedding | vector | text-embedding-ada-002, 1536 dims |
| tags | text[] | Postgres array e.g. `'{"FENCE","ARC"}'` |
| precedence_level | int | 1–9, lower = higher authority |
| status | text | 'approved' \| 'pending' \| 'deleted' |

**Only `status='approved'` clauses reach residents.**

### Document Hierarchy (precedence_level)

| Level | Document family |
|---|---|
| 1 | Texas Property Code |
| 2 | Declaration of CC&Rs (original + amendments) |
| 3 | Supplemental Amendments |
| 4 | Articles of Incorporation |
| 5 | Bylaws |
| 6 | Enforcement Resolutions |
| 7 | Clarifying Resolutions |
| 8 | Other Resolutions (fines, solar/flags, window coverings) |
| 9 | Builders Guidelines |

### Other tables
- `admin_users` — `id`, `username`, `password_hash` (bcrypt rounds=12), `is_active`, `must_change_password`, `is_approver`
- `pending_changes` — two-person approval workflow; fields: `clause_id`, `submitted_by`, `action` ('edit'|'add'|'delete'), `proposed_changes` (jsonb), `original_values` (jsonb), `status`, `reviewed_by`
- `clause_audit_log` — append-only, every clause action recorded, no deletes ever
- `user_activity_log` — login/logout/action tracking with real IPs
- `clause_flags` — CCR revision flags; `flag_type` (clause|topic), `cited_clause_ids` (text[]), status lifecycle (open → in_review → closed)
- `clause_flag_comments` — threaded append-only comments on flags
- RPC `match_clauses` — vector similarity search (used by embedding regeneration in admin; not used by answer pipeline)

---

## Authentication & User Roles

1. **Regular user** — browse clauses, change own password
2. **Approver** (`is_approver=True`) — board members who can manage CCR revision flags; granted/revoked by superusers only
3. **Superuser** (in `SUPERUSERS` set in `admin_app.py` ~line 88) — full access: add/delete/deactivate users, reset passwords, audit log, self-approve pending changes (with warning), grant/revoke Approver role

**Current superusers**: `{'tmasters', 'cmasters', 'admin'}` — adding a new superuser is a one-line change in `admin_app.py`.

**Permission checks and UI visibility must derive from the `SUPERUSERS` set, not hardcoded name lists in templates.**

Brute-force protection: 5 failed attempts = 15-minute lockout (keyed on username + IP). Deletions require a second superuser — no self-approval on deletes.

---

## Environment Variables

### hoa-backend
```
OPENAI_API_KEY
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
OPENAI_CHAT_MODEL          (default: gpt-4o)
OPENAI_EMBEDDING_MODEL     (default: text-embedding-ada-002)
ENABLE_CLAUSE_PREFILTER    (default: true; set "false" to disable the relevance pre-filter instantly)
```

### hoa-admin
```
OPENAI_API_KEY
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
SECRET_KEY                 (Flask session encryption — required, no default)
OPENAI_EMBEDDING_MODEL     (default: text-embedding-ada-002)
```

---

## Data & SQL Patterns

**Postgres array syntax** — always use curly-brace syntax in Supabase SQL editor:
```sql
'{"tag1","tag2"}'   -- CORRECT
["tag1","tag2"]     -- WRONG — throws 22P02 malformed array literal
```

**Embedding regeneration** — only needed when `clause_text` changes. Tag-only or `plain_summary`-only changes do NOT require regeneration.

**match_source column** — administrative ingestion record only. `ask_gpt.py` does not use it. No need to update for search behavior changes.

**Google Drive links** — Drive URLs use file IDs, not filenames. Fixing a `document` name typo does not require any Drive-side change.

**Texas statutory links** — use `statutes.capitol.texas.gov` as canonical `link` for Texas Property Code clauses.

**Before any destructive SQL** — always `SELECT` first. The `clause_audit_log` has no delete — preserve it.

**DOC_SHORT maps** — `DOC_SHORT` (in `format_all_clauses_for_gpt`) and `DOC_SHORT_DISPLAY` (in `answer_question`) must both be updated when new documents are added to Supabase.

---

## Git Workflow

```
main branch → push to GitHub → Render auto-deploys both services
```

After Claude Code sessions, always verify changes were committed AND pushed:
```bash
git add .
git commit -m "description"
git push origin main
```

Push rejected (non-fast-forward):
```bash
git pull origin main --rebase
git push origin main
```

---

## Common Fixes

### Login fails locally
```bash
# Check SECRET_KEY is in .env
lsof -i :5051   # kill old processes
/Users/thomasmasters/Projects/.venv/bin/python3 admin_app.py
```

### bcrypt not found
```bash
/Users/thomasmasters/Projects/.venv/bin/pip install bcrypt
```

### Password reset
```bash
cat > /tmp/gen_hash.py << 'EOF'
import bcrypt
password = b'NEW_PASSWORD'
print(bcrypt.hashpw(password, bcrypt.gensalt(rounds=12)).decode())
EOF
/Users/thomasmasters/Projects/.venv/bin/python3 /tmp/gen_hash.py
```
Then in Supabase SQL editor:
```sql
UPDATE admin_users SET password_hash = '$2b$12$...' WHERE username = 'username';
```
Then: `rm /tmp/gen_hash.py`

---

## Pending / On-Hold Work

| Item | Status | Notes |
|---|---|---|
| Mixed-case tag audit | Pending | Remaining document families not yet audited |
| UTC timestamp cleanup | Pending | Remove broken central_time filter; display clean UTC with label |
| Help panel update | Pending | Update collapsible help in admin_index.html for all new features |
| MFA/TOTP | On hold | Use pyotp + qrcode; make optional not mandatory |

---

## Notes for Claude Sessions

- Always read relevant source files before making changes
- Never modify `app.py` or `ask_gpt.py` without confirming it won't break Carrd search
- Test hoa-backend changes locally before pushing; hoa-admin has no dev — changes go direct to prod
- Both services deploy from the same repo on push
- Always `SELECT` before any `INSERT` to check for existing rows
- Mac is Apple Silicon (arm64) running macOS
- `SUPERUSERS` set in `admin_app.py` is the single source of truth for superuser permissions — do not hardcode names in templates
- Token concern (partially addressed by the `filter_relevant_clauses()` pre-filter): the corpus is still fetched/cached in full, and vague/broad questions still fall back to the complete corpus by design — if it keeps growing, revisit `min_results`/`min_score` in `ask_gpt.py` or reconsider the admin-side embeddings/`match_clauses` RPC for the resident-facing pipeline too
