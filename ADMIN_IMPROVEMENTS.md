# ADMIN_IMPROVEMENTS.md — Admin Console Improvement Plan

**Created:** July 2026
**Source:** Full admin console review (both CLAUDE.md files, `admin_app.py` all 24 routes, `app.py`, `ask_gpt.py`, `services.py`, all 10 templates)
**Status:** Planning document — nothing here is implemented yet.

This document describes improvements to the PLCA HOA admin console and public chatbot backend. It is written so a developer who has **not** read the codebase can execute any item. The **Priority Phase** (in order: #11 → Roles Rebuild → #5 → #1 → #19) should be implemented first. The **Roles Rebuild** is a new item (July 2026 owner decision) that absorbs #9 and #10 and replaces the ad-hoc superuser-set + `is_approver` model with a three-tier `role` column. The remaining items are grouped by audience as **Future Phases** — treat those as a **menu, not a commitment**: for a volunteer-run HOA tool, the Priority Phase plus (eventually) #12 may be everything this project truly needs. Some items (e.g. #8 document registry) solve problems that occur roughly once a year and can reasonably stay code edits forever.

**Owner decisions locked in (July 2026):**
- #11: **drop IP entirely** (not stored, not hashed); questions page visible to **board and up**; whimsy answers logged with a `whimsy` flag.
- Roles: three hierarchical tiers — `superuser` > `board` > `member`; current approvers map 1:1 to `board`.
- #1: **TTL only — Part B (cross-service refresh button) is skipped.**

**Failsafe protocol for the Priority Phase (do not deviate):**
1. All work happens on the `priority-phase` branch — `main` auto-deploys both prod services, so nothing merges until verified. Tag `pre-priority-phase` marks the rollback point; Render's dashboard rollback is the second escape hatch.
2. Every item ships with pytest coverage (extend `tests/` — mocked Supabase/OpenAI, offline, sub-second). `pytest -q` green is the merge gate.
3. Risky runtime behavior gets an env-var kill switch mirroring `ENABLE_CLAUSE_PREFILTER` (e.g. `ENABLE_QUESTION_LOG`).
4. Schema changes are additive-only, created in Supabase **before** the code that reads them deploys; SQL lives in `sql/` in this repo.
5. Merge one item at a time — five small revertable deploys, not one big one.

> **Line-reference caveat:** the `admin_app.py:NNNN`-style anchors below were accurate when this doc was written and will start drifting as soon as the Priority Phase adds code. When implementing a Future Phase item later, re-verify anchors by grepping for the named function — don't trust the line numbers.

---

## Codebase Orientation (read this first)

Two Render services deploy from this single repo on every push to `main`:

| Service | Entry point | Purpose | Dev environment? |
|---|---|---|---|
| **hoa-backend** | `python app.py` (port 5000) | Public API. Carrd-embedded chatbot calls `POST /ask` | Yes — test in `../hoa-backend-dev/` first |
| **hoa-admin** | `python admin_app.py` (`PORT` env) | Admin console at hoa-admin.onrender.com | **No — changes go straight to prod** |

Key files:

| File | Role |
|---|---|
| `app.py` (56 lines) | Public Flask API: `POST /ask` (line 10) and `POST /log` (line 34, forwards to a hardcoded Google Apps Script URL at line 45) |
| `ask_gpt.py` (359 lines) | Answer pipeline: `get_all_clauses()` → `filter_relevant_clauses()` → `format_all_clauses_for_gpt()` → GPT-4o → citation post-processing. Module-level `_clause_cache` at line 16 |
| `admin_app.py` (~1947 lines) | Entire admin console. `SUPERUSERS` set at line 88. Decorators `login_required` (line 135) and `superuser_required` (line 162) |
| `services.py` | Shared Supabase + OpenAI client factories (`supabase()`, `generate_embedding()`) |
| `templates/` | 10 Jinja2 templates: `admin_base.html`, `admin_index.html`, `admin_login.html`, `admin_users.html`, `admin_pending.html`, `admin_audit.html`, `admin_search.html`, `admin_flags.html`, `admin_flag_detail.html`, `admin_change_password.html` |

Supabase tables: `clauses` (~712 rows; only `status='approved'` reaches residents), `admin_users`, `pending_changes`, `clause_audit_log` (append-only, never delete), `user_activity_log`, `clause_flags`, `clause_flag_comments`. One RPC: `match_clauses` (vector similarity — used only by the admin Search Test today, **not** by the resident pipeline).

Conventions that apply to every item below:

- New admin routes use `@app.get(...)`/`@app.post(...)` and must be wrapped in `@login_required` (superuser-only routes add `@superuser_required`).
- Permission/UI visibility derives from the `SUPERUSERS` set (exposed to templates via the `inject_superuser` context processor at `admin_app.py:331`) — never hardcode names in templates.
- Postgres array literals in the Supabase SQL editor use curly braces: `'{"TAG1","TAG2"}'`.
- Always `SELECT` before destructive SQL; `clause_audit_log` is append-only.
- Local dev venv: `/Users/thomasmasters/Projects/.venv/bin/python3` (admin on port 5051, backend on 5000).
- Changes to `app.py`/`ask_gpt.py` must be validated against the Carrd chatbot before pushing (test in `hoa-backend-dev` first).

---

# PRIORITY PHASE

Implement in this order: **#11 (backend logging) → Roles Rebuild → #11 (admin page) → #5 → #1 → #19**.

#11 is split: the logging half ships first because its data accrues with calendar time; its admin page ships after the Roles Rebuild because the page is gated `board`-and-up. The Roles Rebuild absorbs #9 (superusers into the DB) and #10 (per-request enforcement) — all three touch the same auth code path and deploy as one lockout-safe unit.

---

## 1. (#11) Resident Question Log inside the Admin Console

**Priority: 1 of 5 — highest impact in the entire review. Unlocks #12 and #13.** This is also the only item whose value compounds with calendar time — every week it isn't deployed is a week of resident-question data the board never gets back. Ship it first.

### What it does
Resident questions currently vanish into a hardcoded Google Apps Script URL (`app.py:45`) that only the Carrd front-end posts to — the board has no visibility into what residents actually ask, what the bot answered, or which clauses were cited. This item logs every `/ask` interaction (question, answer, cited clause IDs, whether the prefilter fell back to full corpus, timestamp, IP) to a new Supabase table and adds a browsable, searchable "Resident Questions" page to the admin console. It converts the chatbot from a black box into the board's primary signal for where the governing documents need work.

### Files / tables affected
| Target | Change |
|---|---|
| Supabase | New table `resident_questions` |
| `ask_gpt.py` | Return citation metadata from `answer_question()` (or log directly) |
| `app.py` | Log from the `/ask` handler; optionally retire `/log` |
| `admin_app.py` | New route `GET /admin/questions` |
| `templates/` | New `admin_questions.html`; nav link in `admin_base.html` |

### Implementation notes
1. **New table** (create in Supabase SQL editor):
   ```sql
   CREATE TABLE resident_questions (
     id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
     created_at timestamptz NOT NULL DEFAULT now(),
     question text NOT NULL,
     answer text,                      -- final HTML answer sent to the resident
     cited_clause_ids text[],          -- e.g. '{"DECL_27_08","BG_WALLS_01"}'
     prefilter_used boolean,           -- false = fell back to full corpus
     prefilter_clause_count int,       -- size of the filtered set sent to GPT
     whimsy boolean NOT NULL DEFAULT false,  -- canned whimsy answer, no GPT call
     mode text,
     output_format text
   );
   -- No IP column — owner decision: IP is not stored, not even hashed.
   ```
2. **Instrument the pipeline, log from `app.py`.** The cleanest seam: `answer_question()` in `ask_gpt.py` already computes everything needed — `raw_cited_ids` (line 298), `relevant_clauses` vs `all_clauses` (lines 184–188). Two options:
   - **Option A (recommended):** add an optional `meta_out: dict` parameter (or return a second value when a new `return_meta=True` flag is set) that surfaces `{"cited_ids": [...], "prefilter_used": bool, "prefilter_count": int}`. Then `app.py`'s `/ask` handler inserts the row after computing the result. Keeps Supabase writes out of `ask_gpt.py`'s hot path logic.
   - **Option B:** insert directly inside `answer_question()` just before each `return`. Simpler but touches three return paths (json, self-contained, fallback-appended) plus the whimsy short-circuit.
   - Either way: **wrap the insert in try/except and never let logging failure break `/ask`** — the resident answer must go out even if Supabase logging errors. `prefilter_used` is `True` only when `ENABLE_CLAUSE_PREFILTER` is on **and** `filter_relevant_clauses()` returned a subset (i.e. `len(relevant_clauses) < len(all_clauses)`); the fallback-to-full-corpus case (fewer than `min_results=15` clauses scored ≥ `min_score=2`, `ask_gpt.py:151–152`) logs `False`.
   - Whimsy answers (`check_instant_whimsy`, line 157): **log them, flagged** — add a `whimsy boolean DEFAULT false` column and set it on these rows (with empty `cited_clause_ids`). Volume of "who made you"-type questions is real signal that residents are poking at the bot, and excluding them becomes an analytics-time filter (#12) instead of an irreversible data-loss decision.
3. **Privacy — decisions made (July 2026).** HOA questions can be sensitive ("what's the lien process," "can I report my neighbor for X"), so:
   - **IP: dropped entirely** — not stored, not hashed. No `ip` column exists.
   - **Retention:** 24 months; enforce with a manual quarterly `DELETE FROM resident_questions WHERE created_at < now() - interval '24 months'` until it ever matters enough to automate.
   - **Visibility: board and up** — the questions page (and #12 analytics later) is gated `@role_required("board")` from the Roles Rebuild. The page therefore ships **after** the Roles Rebuild; the logging half ships first.
3. **Admin page.** New route in `admin_app.py`:
   ```python
   @app.get("/admin/questions")
   @login_required
   def admin_questions():
   ```
   Paginate exactly like `admin_audit()` (line 1422) — it's the closest existing pattern (date-ordered, `range()` pagination, simple filters). Filters worth having on day one: free-text search on `question` (reuse `sanitize_keyword`/`build_keyword_filter` at lines 242–247, adapted to a single-column `ilike`), a "full-corpus fallback only" checkbox (`prefilter_used = false`), and a "no citations" checkbox (`cited_clause_ids` empty). Render cited IDs as plain text now; they become links once #18 (clause permalink) exists.
4. **Template.** `admin_questions.html` extends `admin_base.html`; add a "Resident Questions" nav item in `admin_base.html`, visible only to `board`-and-up roles (hide it for `member` — same role check as the route).
5. **Retiring `/log`:** leave `app.py`'s `/log` route in place until the Carrd embed is updated — Carrd posts question/answer/ip to it separately. Best end state: `/ask` logs server-side (this item), then the Carrd JS `/log` call and route are removed. Note the Apps Script URL is hardcoded, not an env var.
6. **Deploy order:** create the table first, then push. Test the whole flow in `hoa-backend-dev` before touching prod `app.py`/`ask_gpt.py` (CLAUDE.md rule).

### Dependencies
None. **Unlocks #12 (analytics) and #13 (flag-from-question).** #18 improves it (clickable cited-clause links).

---

## 2. (Roles Rebuild) Three-Tier Role Model — absorbs #9 and #10

**Priority: 2 of 5 — owner-requested rebuild (July 2026). Security fix + governance fix in one deploy.**

### What it does
Today's model is ad-hoc: a hardcoded `SUPERUSERS` set at `admin_app.py:88` (changing it requires a code deploy), an `is_approver` boolean toggled from the Users page, and "everyone else." Additionally, deactivating a user only blocks *future logins* — an existing session stays valid up to 8 hours (`login_required` never re-checks `is_active`).

This replaces all of it with a single hierarchical `role` column on `admin_users`, enforced per-request:

| Role | Maps from today | Can do |
|---|---|---|
| `superuser` | hardcoded set (`tmasters`, `cmasters`, `admin`) | everything: users, roles, audit log, approvals, self-approve (with warning) |
| `board` | `is_approver = true` users (1:1 — owner confirmed) | flags workflow, resident questions (#11), analytics (#12), pending history |
| `member` | everyone else | browse clauses, submit changes, own password |

Each tier includes everything below it. Deactivation and demotion take effect on the user's **next request**, not in 8 hours (this is #10, folded in).

### Files / tables affected
| Target | Change |
|---|---|
| Supabase `admin_users` | New `role text NOT NULL DEFAULT 'member'` column + CHECK constraint; backfill from `SUPERUSERS` + `is_approver` |
| `admin_app.py` | New `_get_role()`/`role_required(min_role)` decorator; `login_required` re-checks `is_active` + loads role per-request; replace every `SUPERUSERS` read and `_is_approver()` call; `toggle_approver` → `set_role` route |
| `templates/admin_users.html` | Role dropdown replaces the approver toggle |
| Templates consuming `is_superuser`/approver flags | Switch to role-rank checks via updated context processor |

### Implementation notes
1. **SQL first (lockout-safe sequence — this is the #9 discipline):**
   ```sql
   ALTER TABLE admin_users ADD COLUMN role text NOT NULL DEFAULT 'member'
     CHECK (role IN ('superuser','board','member'));
   UPDATE admin_users SET role = 'superuser' WHERE username IN ('tmasters','cmasters','admin');
   UPDATE admin_users SET role = 'board' WHERE is_approver = true AND role = 'member';
   SELECT username, role, is_approver, is_active FROM admin_users ORDER BY role;  -- VERIFY before deploying code
   ```
   Keep `is_approver` in place (ignored by code) for one release as a rollback aid; drop it later.
2. **Rank-based check, one implementation:** `ROLE_RANK = {"member": 0, "board": 1, "superuser": 2}` and a `role_required(min_role)` decorator; `superuser_required` becomes `role_required("superuser")` (keep the old name as an alias so existing routes don't all churn in one diff).
3. **Per-request enforcement (#10):** inside `login_required`, fetch the user row by `session["user_id"]` once per request — reject and clear the session if the row is missing or `is_active` is false; stash the fresh role on `flask.g` so `role_required` and the template context processor read one lookup, not three. One Supabase query per request is fine at ~10 admins; add a ~60s module-level cache only if it ever matters.
4. **Grep for every consumer:** `SUPERUSERS` (decorator at line 162, `inject_superuser` context processor at line 331, approve/reject/delete guards near line 1579) and `_is_approver()` (line 91, flag routes). Preserve two rules exactly: deletes require a **second** superuser (no self-approval on deletes), and self-approval of non-deletes warns.
5. **Role management UI:** replace the approver toggle in `admin_users.html` with a role dropdown, superuser-only, with two guards: cannot change your own role, cannot demote the last superuser. Audit-log every change.
6. **Tests before merge:** decorator matrix (member/board/superuser × member-page/board-page/superuser-page), deactivated-user-bounced-mid-session, last-superuser guard, backfill mapping. The mocked-Supabase pattern in `tests/test_approval.py` covers all of it offline.
7. **Deploy sequence (hoa-admin has no dev):** run + verify SQL → deploy code → **immediately log in as a superuser and confirm access** before walking away. Rollback = revert commit; the column is additive and harmless to old code.
8. **Update both CLAUDE.md files** afterward — they document `SUPERUSERS` as a code-level set.

### Dependencies
None. **Blocks #11's admin page** (gated `board`-and-up). Subsumes #9 and #10 entirely.

---

## 3. (#5) Field-Level Diff View on the Pending Page

**Priority: 3 of 5.**

### What it does
`pending_changes` stores both `original_values` and `proposed_changes` as jsonb, but `templates/admin_pending.html` shows reviewers the raw JSON blobs. A reviewer approving an edit can't easily see *what changed*. This item renders a field-by-field old → new comparison for each pending change so the two-person approval workflow becomes an actual review instead of a rubber stamp.

### Files / tables affected
| Target | Change |
|---|---|
| `admin_app.py` | Compute per-field diffs in `admin_pending()` (line 1530) |
| `templates/admin_pending.html` | Replace raw JSON display with a diff table |

No schema changes — the data already exists.

### Implementation notes
1. **Compute the diff in Python, not Jinja.** In `admin_pending()`, for each change row build:
   ```python
   def build_field_diff(change):
       original = change.get("original_values") or {}
       proposed = change.get("proposed_changes") or {}
       diffs = []
       for field in sorted(set(original) | set(proposed)):
           if field.startswith("_"):        # skip _verification metadata
               continue
           old, new = original.get(field), proposed.get(field)
           if old != new:
               diffs.append({"field": field, "old": old, "new": new})
       return diffs
   ```
   Attach as `change["field_diffs"]` before passing to the template.
2. **Gotchas in the data:**
   - `proposed_changes` contains an injected `_verification` dict (added by `submit_pending_change()` at `admin_app.py:487`) — skip underscore-prefixed keys, exactly as `_apply_pending_change()` does at line 514. But **do show** the verification status separately (score/status is useful to the reviewer); it may already be rendered.
   - `tags` is a list — compare as lists but render as comma-joined strings (there's an existing `serialize_tags()` helper at line 184).
   - `action='add'` rows have no meaningful `original_values` (new clause) — render all proposed fields as "new"; `action='delete'` rows have no `proposed_changes` — render the original values with a "will be deleted" banner. Check how these are stored today by selecting a few rows before coding.
   - Long text fields (`clause_text` can be thousands of chars): render old/new in side-by-side `<pre>` blocks with `max-height` + scroll, or a `<details>` expander. A word-level inline diff (Python stdlib `difflib.ndiff`/`HtmlDiff`) is a nice-to-have, not required — field-level old/new alone is the win.
3. **Template:** in `admin_pending.html`, replace (or collapse behind a `<details>`) the raw jsonb dump with a table: Field | Current value | Proposed value. Keep the raw JSON available in an expander for debugging.
4. **Testing reality check:** hoa-admin has no dev environment. Test locally (`/Users/thomasmasters/Projects/.venv/bin/python3 admin_app.py`, port 5051) against real Supabase data by submitting a throwaway edit as a non-approving user, verifying the diff renders, then rejecting it.

### Dependencies
None. Pairs naturally with #14 (pending history uses the same diff rendering — build `build_field_diff()` as a reusable helper).

---

## 4. (#1) Clause Cache TTL + Invalidation Button

**Priority: 4 of 5. This is really two features — ship the TTL first; the button is optional polish.**

### What it does
`ask_gpt.py` loads all approved clauses once into a module-level `_clause_cache` (line 16) that lives for the life of the process. When an admin approves a clause change, **residents keep getting answers from the stale corpus until the hoa-backend Render service restarts** — today that means waiting for the next deploy or manually restarting the service.

**Part A (ship first, possibly alone): a cache TTL.** ~6 lines in `ask_gpt.py`, zero new infrastructure, fixes ~90% of the problem on its own. Note also that on Render's free tier hoa-backend spins down when idle and every cold start rebuilds the cache anyway — staleness only bites during sustained-traffic windows, which further weakens the case for anything fancier. Consider a 1-hour TTL rather than 6 (a full corpus refetch is ~712 rows — cheap).

**Part B — SKIPPED (owner decision, July 2026): the admin-triggered refresh button is not being built.** The notes below are retained only in case the TTL lag ever proves annoying in practice. Part B would require a shared secret, two Render env vars, a new backend endpoint, and cross-service HTTP with cold-start timeout handling.

### Files / tables affected
| Target | Change |
|---|---|
| `ask_gpt.py` | Add `invalidate_clause_cache()`; add TTL to `get_all_clauses()` |
| `app.py` | New secret-protected `POST /internal/refresh-cache` endpoint |
| `admin_app.py` | New `POST /admin/refresh-corpus` route that calls the backend endpoint |
| `templates/admin_index.html` | Button on the dashboard |
| Render env vars | New shared secret `CACHE_REFRESH_SECRET` on **both** services; `BACKEND_BASE_URL` on hoa-admin |

### Implementation notes
1. **Cross-service, not in-process — this is the key design constraint for Part B.** The cache lives in the **hoa-backend** process; the button lives in **hoa-admin**, a different Render service. Calling a Python function won't work — hoa-admin must make an HTTP call to hoa-backend.
2. **`ask_gpt.py` (Part A — the TTL):**
   ```python
   import time
   _clause_cache = None
   _cache_loaded_at = 0.0
   CACHE_TTL_SECONDS = int(os.getenv("CLAUSE_CACHE_TTL", "21600"))  # 6h safety net

   def invalidate_clause_cache():
       global _clause_cache
       _clause_cache = None

   def get_all_clauses():
       global _clause_cache, _cache_loaded_at
       if _clause_cache is not None and (time.time() - _cache_loaded_at) < CACHE_TTL_SECONDS:
           return _clause_cache
       ...  # existing pagination loop unchanged
       _cache_loaded_at = time.time()
   ```
   The TTL alone fixes 90% of the problem even if the button is never built. Everything below is Part B.
3. **`app.py`:** add the endpoint (shared-secret auth — do NOT leave it open; it's a cheap DoS vector since a refresh re-fetches ~712 rows):
   ```python
   @app.route("/internal/refresh-cache", methods=["POST"])
   def refresh_cache():
       secret = os.getenv("CACHE_REFRESH_SECRET")
       if not secret or request.headers.get("X-Refresh-Secret") != secret:
           return jsonify({"error": "unauthorized"}), 403
       from ask_gpt import invalidate_clause_cache, get_all_clauses
       invalidate_clause_cache()
       clauses = get_all_clauses()   # eager reload so the next resident isn't slow
       return jsonify({"status": "refreshed", "clause_count": len(clauses)})
   ```
4. **`admin_app.py`:** `POST /admin/refresh-corpus`, `@login_required` (any admin may press it — approving a change already implies corpus impact):
   - `requests.post(f"{os.getenv('BACKEND_BASE_URL')}/internal/refresh-cache", headers={"X-Refresh-Secret": os.getenv("CACHE_REFRESH_SECRET")}, timeout=30)`
   - Flash success with the returned `clause_count`, or a clear error on failure/timeout (Render free tier cold starts can be slow — the timeout matters).
   - Log via existing `log_audit_event()` (line 383) and `log_user_activity()` (line 371).
5. **UX placement:** button on `admin_index.html` dashboard; also flash a reminder ("Corpus refresh needed for this to reach residents — click Refresh") after `approve_pending()` (line 1579) succeeds. Auto-triggering the refresh from `approve_pending()` is a reasonable v2; keep v1 manual so failures are visible.
6. **Env setup (before pushing code):** generate one random secret (`python3 -c "import secrets; print(secrets.token_hex(32))"`), set `CACHE_REFRESH_SECRET` on both Render services, set `BACKEND_BASE_URL` on hoa-admin to the hoa-backend public URL, and add both to local `.env`. If the env var is unset the endpoint fails closed (403).
7. Note both services auto-deploy on push, so the two halves ship atomically from one commit.

### Dependencies
None. Independent of all other items, but every clause-editing item (#2, #5, #7, #8) benefits from it existing.

---

## 5. (#19) Fix the Misleading "Search Test" Panel

**Priority: 5 of 5.**

### What it does
The dashboard's Search Test panel (`run_search_test()` at `admin_app.py:288`) runs a vector search (`match_clauses` RPC) plus a keyword `ilike` search, and the help text claims it uses "the same logic as the production chatbot." That was true under the old vector-search architecture, but production now uses `filter_relevant_clauses()` full-corpus prompting — so admins are debugging a pipeline that doesn't exist. This item replaces (or augments) the panel with a **prefilter preview**: for a test question, show exactly which clauses `filter_relevant_clauses()` selects, with per-clause scores, the total count, and whether the full-corpus fallback triggered. Admins can then tune `min_results`/`min_score` against reality.

### Files / tables affected
| Target | Change |
|---|---|
| `ask_gpt.py` | Optionally add a `debug`/scores-returning variant of `filter_relevant_clauses()` |
| `admin_app.py` | Rewrite `run_search_test()` (line 288); it's rendered from `admin_home()` (line 650) via `current_filters()`'s `test_query` param (line 322) |
| `templates/admin_index.html` | Rewrite the Search Test panel markup and its help text |

### Implementation notes
1. **Get scores out of the filter.** `filter_relevant_clauses()` (`ask_gpt.py:114`) computes `(score, clause)` pairs internally but returns only clauses. Two options:
   - **Option A (rejected — do not do this):** add a `filter_relevant_clauses_debug()` that duplicates the scoring loop, keeping the production function untouched. Sounds safe, but it plants a landmine: the entire point of this panel is letting admins tune the prefilter, and the moment someone tunes the real function the debug copy silently diverges — the panel starts lying again, which is the exact bug this item exists to fix.
   - **Option B (recommended):** refactor the scoring loop into a shared `_score_clauses()` helper used by both `filter_relevant_clauses()` (unchanged public signature and behavior) and a thin debug wrapper returning `{"matched": [(score, clause), ...], "fallback_triggered": bool, "matched_count": int, "min_results": ..., "min_score": ...}`. This touches the resident path, so: the existing unit tests in `tests/test_ask_gpt.py` cover `filter_relevant_clauses()` and must still pass, and the change goes through `hoa-backend-dev` first with a handful of known Q&As re-verified (standard CLAUDE.md rule). One shared loop means the panel can never drift from production.
2. **`admin_app.py`:** hoa-admin does **not** currently import from `ask_gpt.py` for search (`admin_search()` at line 1925 calls `answer_question()`; do not re-add old `fetch_matching_clauses` imports). Import the debug function and rewrite `run_search_test()` to:
   - Fetch approved clauses. Reuse `get_all_clauses()` — note this also warms `ask_gpt`'s cache inside the *admin* process (harmless, separate process from hoa-backend) — or do a direct paginated query; either is fine, but must include `tags`, `plain_summary`, `clause_text`, `precedence_level`.
   - Run the debug filter with production defaults, and (nice-to-have) accept `min_results`/`min_score` overrides as query params so admins can experiment from the UI without a deploy.
3. **Panel rendering** (`admin_index.html`): show prominently — **"Prefilter selected N of ~712 clauses"** or **"FALLBACK: full corpus sent (only N clauses scored ≥ 2; threshold is 15)"** — then a table of the top ~25: score | clause_id | document | citation | matched tags. Also surface whether `ENABLE_CLAUSE_PREFILTER` is even on (read the env var; it defaults true but can be disabled in Render).
4. **Fix the lie in the help text.** Whatever else ships, the "same logic as the production chatbot" copy must be corrected to describe what the panel actually runs.
5. **Keep or drop the old searches?** The vector search still has a legitimate admin use (finding near-duplicate clauses; the `match_clauses` RPC is otherwise only used by embedding regeneration). Recommended: keep vector + keyword results in a collapsed "Legacy search (not used by the resident bot)" section, honestly labeled, with the prefilter preview as the primary result.
6. **GPT is never called** by this panel — it previews the corpus-selection step only. Say so in the help text (the full end-to-end test is `/admin/search`, which does call GPT and costs tokens).

### Dependencies
None, but informed by #11/#12 data: once the question log shows which real questions fall back to full corpus, this panel is where admins reproduce and tune them.

---

# FUTURE PHASES

The remaining 14 items, grouped by primary audience. Within each group, items are ordered roughly by effort-to-impact. **Treat this as a menu, not a plan** — implement an item when its pain actually shows up, not because it's listed here.

---

## Phase: For HOA Admins (day-to-day operators)

### (#2) Bulk "Regenerate All Stale Embeddings" Action

**What it does.** The dashboard already counts stale embeddings and `browse_clauses()` (line 271) supports a `stale=true` filter (`clause_has_stale_embedding()` at line 190 defines staleness), but regeneration is one clause at a time via `POST /admin/clauses/<clause_id>/regenerate-embedding` (line 913). After a bulk import, an admin clicks dozens of times. This adds a single "Regenerate all stale" button that loops over every stale clause and regenerates each embedding, reporting successes/failures.

**Files/tables:** `admin_app.py` (new `POST /admin/regenerate-stale-embeddings` route reusing the logic in `regenerate_clause_embedding()`; there is also existing standalone logic in `fix_missing_embeddings.py` at the workspace level to crib from), `templates/admin_index.html` (button next to the stale count).

**Implementation notes:**
- Query all stale clauses (same predicate the stale filter uses), then loop: `generate_embedding(clause_text)` → update row. Embeddings only depend on `clause_text` (CLAUDE.md: tag/summary changes don't require regeneration).
- OpenAI rate limits: process serially with a small `time.sleep(0.2)` between calls; ~tens of clauses is fine synchronously. If the stale set could exceed ~100, cap per click ("Regenerated 100, 43 remaining — click again") rather than adding background-job infrastructure to a Flask/Render-free-tier stack. Watch Render's request timeout (~30s per default gunicorn worker isn't in play here since admin runs `python admin_app.py`, but keep batches modest anyway).
- Audit: one `log_audit_event()` summary entry (count + failures), not one per clause.
- Superuser-only is unnecessary; `@login_required` suffices (it's non-destructive).

**Dependencies:** none.

---

### (#3) Clause Export (CSV)

**What it does.** Import exists (`POST /admin/import`, line 968, with a downloadable template at line 942) but there's no export. A "Download CSV" button that exports the *currently filtered* browse view gives admins backups, offline committee review packets, and a round-trip path for bulk edits without opening the Supabase dashboard.

**Files/tables:** `admin_app.py` (new `GET /admin/export` route), `templates/admin_index.html` (button beside the browse filters).

**Implementation notes:**
- Accept the same query params as the browse view (`q`, `tag`, `document`, `stale` — see `current_filters()` at line 316) and reuse `browse_clauses()`'s query-building, but **without pagination** — loop `range()` pages of 1000 like `get_all_clauses()` does.
- Column order should match the import template exactly (see `import_template()` at line 942) so export → edit → import round-trips. Serialize `tags` with the existing `serialize_tags()` helper. Include `clause_id`, `status`, `precedence_level`, and the row `id` (uuid) so re-imports can match rows.
- Stream with `Response(generate(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=clauses_export.csv"})` and Python's `csv` module (handles embedded commas/quotes/newlines in `clause_text`).
- Exclude the `embedding` column (1536 floats — useless in CSV and bloats the file).

**Dependencies:** none. Complements #17 (flag report export) — share any CSV helper you write.

---

### (#4) Per-Clause Change History on the Clause Card

**What it does.** Every clause field change is already recorded in `clause_audit_log` keyed by `record_id` (see `_log_clause_field_changes()` at line 405 and `log_audit_event()` at line 383), but the only view is the global, superuser-only `/admin/audit` page (line 1422). This surfaces an expandable "History" section on each clause showing who changed which field, when, old → new.

**Files/tables:** `admin_app.py`, `templates/admin_index.html` (clause cards live in the browse view).

**Implementation notes:**
- Don't fetch history for all 25 clauses on every browse page load. Add a lazy endpoint: `GET /admin/clauses/<clause_id>/history` returning rendered HTML or JSON, fetched when the user expands the History `<details>` (a few lines of vanilla JS `fetch`).
- Query: `clause_audit_log` where `record_id = <clause uuid>` ordered by timestamp desc, limit ~50. Inspect a few real rows first to confirm which columns hold field name/old/new (the logging writer is `_log_clause_field_changes()` — read it to see the exact shape).
- Visibility: history is read-only and valuable to regular users; `@login_required` only, even though the global audit page stays superuser-only. If that's a policy concern, redact the acting username for non-superusers.
- If #18 (clause permalink page) is built, this History section is a natural tab there — build the lazy endpoint either way; both UIs can call it.

**Dependencies:** none. Subsumed/enhanced by #18.

---

### (#6) "My Submissions" View for Non-Superusers

**What it does.** Regular users can submit changes (routed through `submit_pending_change()`, line 474) but `/admin/pending` (line 1530) is superuser-only — a submitter has no way to see whether their change was approved or rejected, or read the rejection reason already stored in `review_notes`. This adds a "My submissions" page listing the current user's pending/approved/rejected changes with status and reviewer notes.

**Files/tables:** `admin_app.py` (new `GET /admin/my-submissions`), new template `admin_my_submissions.html`, nav link in `admin_base.html`.

**Implementation notes:**
- Query `pending_changes` where `submitted_by = session["username"]`, all statuses, ordered by creation desc. `@login_required` only — the whole point is non-superuser access. Do not reuse the `/admin/pending` route with a filter; the pending page exposes approve/reject actions that must stay superuser-only.
- Show: date, action (add/edit/delete), clause identifier, status badge, reviewer (`reviewed_by`), and `review_notes` on rejected rows. Reuse the field-diff helper from #5 in a read-only expander so submitters can see exactly what they proposed.
- Confirm the exact `pending_changes` column names for review metadata by selecting a row (`reviewed_by`, `review_notes`, and whatever timestamp the approve/reject handlers at lines 1579/1624 write).

**Dependencies:** none required; reuses #5's diff helper if built first. Related to #14 (same data, board-wide view).

---

### (#7) Tag Management Page (rename / merge)

**What it does.** The pending "mixed-case tag audit" is currently manual SQL. This adds a page listing every distinct tag with its clause count, and a rename action that updates the tag across all clauses atomically — which also handles merges (rename `Fence` → `FENCE` merges into the existing `FENCE`). Fixes tag drift permanently and makes the audit a UI task.

**Files/tables:** `admin_app.py` (new `GET /admin/tags`, `POST /admin/tags/rename`), new template `admin_tags.html`, nav link. Touches the `clauses.tags` `text[]` column.

**Implementation notes:**
- **Listing:** `get_filter_options()` (line 216) already aggregates distinct tags for the browse filter dropdown — read it and reuse/extend its approach to also produce counts (in Python: fetch all `tags` arrays, `collections.Counter`). ~712 rows; don't over-engineer with SQL aggregation.
- **Rename:** fetch all clauses whose `tags` array contains the old tag (PostgREST: `.contains("tags", [old_tag])` — note tag matching is **case-sensitive** in Postgres arrays, which is exactly the drift being fixed), then per clause compute the new array in Python (replace old with new, dedupe, preserve order) and update. Per-row updates are fine at this scale and let you write one audit-log entry per touched clause via `_log_clause_field_changes()`.
- Make it superuser-only (`@superuser_required`) — it's a bulk mutation of approved clauses that bypasses the pending-changes workflow. State that bypass explicitly in the UI ("renames apply immediately, no two-person approval").
- Tag changes do **not** require embedding regeneration (CLAUDE.md) but **do** require a corpus cache refresh (#1) to reach residents — flash that reminder, since `filter_relevant_clauses()` weights tag matches 3×.
- Optional: a "delete tag everywhere" action, same mechanics with a confirmation step.

**Dependencies:** none hard; #1 (cache refresh) strongly complements it.

---

### (#8) Document Registry (replace hardcoded `DOC_SHORT` maps)

**What it does.** Adding a new governing document today requires editing two parallel dicts in `ask_gpt.py` — `DOC_SHORT` (line 48, token-saving abbreviations for the GPT prompt) and `DOC_SHORT_DISPLAY` (line 244, friendly names for citation links) — and redeploying. A small `documents` table (full name, short name, display name, default precedence) editable in the admin console makes onboarding a document a data change.

**Files/tables:** new Supabase table `documents`; `ask_gpt.py` (both maps become table-backed lookups); `admin_app.py` + new template `admin_documents.html` (CRUD page, superuser-only).

**Implementation notes:**
- Table: `documents (id uuid pk, document_name text unique, short_name text, display_name text, precedence_level int, link text)`. **Seed it from the two existing dicts** — write a one-off seed script or SQL INSERT built by copying `ask_gpt.py:48–73` and `244–269`; the dict keys must match `clauses.document` values exactly (they're exact-match lookups today).
- `ask_gpt.py`: load the registry once alongside the clause cache (piggyback on `get_all_clauses()`'s caching, and invalidate together via #1's `invalidate_clause_cache()`). **Keep the hardcoded dicts as fallback** if the table fetch fails or a document is missing a row — resident answers must never break because the registry is incomplete. Current fallbacks: `doc[:20]` in the prompt formatter, full `doc` name in display; preserve those as the final fallback.
- Admin CRUD page: superuser-only; on save, remind about cache refresh (#1).
- Bonus once table-backed: the clause add/edit forms can offer a document dropdown sourced from the registry instead of free text, killing filename typos at the source.
- This touches `ask_gpt.py`'s resident path — **test in hoa-backend-dev first**, verify a handful of known Q&As still cite with friendly names.

**Dependencies:** #1 (cache invalidation) should exist first so registry edits propagate without redeploys — otherwise this item's "no redeploy" goal is only half true.

---

### (#9) Move `SUPERUSERS` into the DB / (#10) Enforce `is_active` Per-Request

**Both absorbed into the Priority Phase "Roles Rebuild" item (2 of 5) — see above.** The three-tier `role` column supersedes #9's `is_superuser` boolean design, and per-request enforcement is folded into the same deploy.

---

## Phase: For Board Members

### (#12) Q&A Analytics Dashboard (built on #11)

**What it does.** Once resident questions are logged (#11), this page answers the board's real questions: What are residents asking most? Which questions fell back to full corpus (no relevance signal — likely uncovered topics)? Which answers cited nothing? Which clauses are *never* cited (candidates for consolidation)? It turns the question log into a prioritized worklist for the document revision committee.

**Files/tables:** reads `resident_questions` (+ `clauses` for never-cited analysis); `admin_app.py` (new `GET /admin/analytics`), new template `admin_analytics.html`, nav link.

**Implementation notes:**
- Compute in Python: fetch the last N days of `resident_questions` (paginated `range()` loop; add a date-range filter, default 90 days) and aggregate in-process. Volumes are tiny (a community HOA chatbot); no SQL views or materialized anything needed.
- Panels for v1:
  1. **Volume over time** — questions/week (plain HTML table or a simple inline SVG bar chart; no JS chart library).
  2. **Top topics** — tokenize questions with the same convention as `filter_relevant_clauses()` (`re.findall(r'[a-z]+', q.lower())`, len > 3, minus a small stopword list) and count; alternatively count matches against the known tag vocabulary (#7's tag list) for cleaner buckets.
  3. **Full-corpus fallback list** — `prefilter_used = false` rows: these are the questions the prefilter couldn't match, i.e. likely document gaps. Link each to its log entry.
  4. **Zero-citation answers** — empty `cited_clause_ids`: GPT couldn't ground the answer.
  5. **Citation frequency** — count each cited clause ID across all rows; show top 20 and, joined against approved `clauses`, the list of never-cited clause IDs.
- Every list should deep-link back to the question log page (#11) with filters applied; cited clauses link to #18 permalinks once those exist.
- Read-only, `@login_required`.

**Dependencies:** **hard dependency on #11** (needs the table and several weeks of data to be useful). #18 enhances linking.

---

### (#13) "Flag This Topic" from Real Resident Questions

**What it does.** The CCR-revision flag workflow already supports topic flags carrying a question, an answer snapshot, and cited clause IDs (`create_flag()`, line 1822, writing to `clause_flags` with `flag_type='topic'` and `cited_clause_ids text[]`). But today an admin must re-type a resident question into the flag form. This adds a one-click "Flag this topic" button on each row of the resident question log (#11) that pre-populates a flag directly from the logged question, answer, and citations — connecting real resident confusion to the committee's queue.

**Files/tables:** `admin_app.py` (extend `create_flag()` or add a sibling route that accepts a `resident_question_id`), `templates/admin_questions.html` (button per row), optionally a `source_question_id uuid` column on `clause_flags` for provenance.

**Implementation notes:**
- **Read `create_flag()` (line 1822) first** and reuse its insert shape exactly — flag creation, status lifecycle (open → in_review → closed), and the comments thread all already work; this item is only a new entry point.
- Flow: button posts `resident_question_id` to a new `POST /admin/questions/<id>/flag` route → fetch the log row → render the *existing* flag-creation form pre-filled (question text, answer snapshot, `cited_clause_ids`) for the admin to confirm/edit → submit through the normal `create_flag` path. Pre-fill + confirm beats silent one-click creation: the admin can trim the answer snapshot or adjust cited IDs.
- Permission: match whatever `create_flag` requires today (flags are managed by Approvers; check the decorator/role guard on line 1822's route before assuming).
- Add `source_question_id` to `clause_flags` (nullable) so the flag detail page (#15/#18 era) can link back to the originating resident question. One `ALTER TABLE`, nullable, no backfill needed.
- Nice-to-have: on the question log, badge questions that already have a flag (join on `source_question_id`) to prevent duplicate flags for the same recurring question.

**Dependencies:** **hard dependency on #11.** Enhanced by #15 (flag↔clause linking).

---

### (#14) Pending-Changes History View

**What it does.** `admin_pending()` (line 1530) hard-filters `status='pending'`, so once a change is approved or rejected it disappears from the UI — even though reviewer, timestamp, and rejection notes are all stored. This adds a "History" tab showing decided changes, giving the board a governance record ("who approved what, when, and why was that rejected") without SQL.

**Files/tables:** `admin_app.py` (extend `admin_pending()` with a `status` query param, or add `GET /admin/pending/history`), `templates/admin_pending.html` (tab bar + decided-row rendering).

**Implementation notes:**
- Simplest shape: `?status=history` on the existing route → query `pending_changes` where `status != 'pending'` (confirm the exact decided-status values by selecting distinct `status` first — likely `approved`/`rejected`), ordered by review timestamp desc, paginated like the audit page.
- Render each decided row with: action, clause, submitter, reviewer, decision timestamp, `review_notes`, and the same field-diff expander from #5 (read-only). **Hide the approve/reject buttons** on history rows — template must branch on status.
- Keep it superuser-only initially (same as the pending page); if the board wants wider visibility later, it's a one-decorator change.
- Optional filter: by submitter or reviewer (dropdown from `admin_users`).

**Dependencies:** soft dependency on #5 (reuses `build_field_diff()`); trivial without it (show raw values), much better with it. Related to #6 (per-user slice of the same data).

---

## Phase: For the Document Review Committee (flags workflow)

### (#15) Link Flags to Clauses and to the Fix

**What it does.** The flag detail page (`admin_flag_detail()`, line 1770) shows a clause snapshot but offers no "edit this clause" link, and topic flags list `cited_clause_ids` as plain text. Deliberation and action are disconnected: a committee member reading a flag can't jump to the clause, and a closed flag doesn't record what change (if any) resulted. This links flags → clause browse/edit, and optionally records the resulting `pending_changes.id` on the flag when it's closed as "Changed."

**Files/tables:** `admin_app.py` (`admin_flag_detail`, `update_flag_status` at line 1887), `templates/admin_flag_detail.html`, `templates/admin_flags.html`; optional `resolution_change_id uuid` column on `clause_flags`.

**Implementation notes:**
- **Clause links:** for each ID in `cited_clause_ids` (these are text `clause_id` values like `DECL_27_08`, not uuids — resolve via a `clauses` lookup on the `clause_id` column, the same resolution `_get_open_flag_clause_ids()` at line 1669 implies), link to the browse view pre-filtered (`/admin?q=<clause_id>`) — or to the #18 permalink if that exists. Handle dangling IDs (clause deleted since flagging) with a "no longer exists" badge rather than a broken link; the snapshot already stored on the flag covers this case.
- **Closing the loop:** add nullable `resolution_change_id` to `clause_flags`. In `update_flag_status()` (line 1887), when status is set to closed, offer an optional dropdown of recent `pending_changes` rows touching the flag's clauses (match on clause uuid, last ~90 days) to attach. Render the linked change (with #5's diff) on the flag detail page. Keep it optional — many flags close as "no change needed."
- Reverse direction (nice-to-have): on the pending page, badge changes whose clause has an open flag — `_get_open_flag_clause_ids()` already computes exactly this set for another consumer; reuse it.

**Dependencies:** none hard. Much better with #18 (permalinks) and #5 (diff rendering).

---

### (#16) Flag Assignment and Email Notification

**What it does.** Flags have no owner and no push channel — committee members only discover new flags or new comments by logging in and looking. This adds an `assigned_to` field on flags plus email notifications (immediate or daily digest) on flag creation, assignment, and new comments, keeping a volunteer committee engaged without requiring habit-forming logins.

**Files/tables:** `clause_flags` (new `assigned_to text` column), `admin_users` (new `email text` column — **the table has no email today**; usernames are not addresses), `admin_app.py` (assignment UI + send hooks in `create_flag`/`add_flag_comment` at line 1866/`update_flag_status`), `templates/admin_flag_detail.html` + `admin_flags.html`, new env vars.

**Implementation notes:**
- **Assignment first, email second** — assignment alone (a dropdown of users with `is_approver=true`, an "Assigned to me" filter on `/admin/flags`, audit-logged) is a small, self-contained ship that delivers half the value with zero infrastructure.
- **Email transport:** no email exists anywhere in this repo. Options: (a) SMTP via Python stdlib `smtplib` + Gmail app password — zero new dependencies, fine at this volume; (b) a transactional API (Resend/SendGrid free tier) — nicer deliverability, one new dependency. Either way: env vars (`SMTP_HOST/PORT/USER/PASSWORD/FROM_ADDR` or `EMAIL_API_KEY`) on the **hoa-admin** service only, and **send in a try/except that never blocks the request** — a mail failure must not break flag creation. Render free tier has no background workers, so send synchronously and keep it to one recipient list per event.
- Events for v1: flag created (notify all approvers), flag assigned (notify assignee), comment added (notify assignee + flag creator, minus the commenter). A daily digest is strictly better for volunteers but requires a scheduler (Render cron job hitting a secret-protected endpoint, same pattern as #1's endpoint) — defer to v2.
- Collect emails via the Users page (superuser edits) and/or the change-password page (self-service). Validate loosely; these are ~10 known people.
- Add an unsubscribe/notification-preference boolean per user before anyone asks.

**Dependencies:** none hard. If #9 is done, "all approvers" queries stay the same (`is_approver` is already a column).

---

### (#17) Exportable Flag Report for Board Meetings

**What it does.** Each meeting cycle, the committee needs a summary of open flags — description, status, affected clauses, discussion notes — and today someone assembles it by hand from the flags pages. This adds a one-click export: a printable HTML report (and/or CSV) of flags filtered by status, including comment threads and affected-clause citations, that becomes the meeting agenda.

**Files/tables:** `admin_app.py` (new `GET /admin/flags/report`), new template `admin_flags_report.html` (print-styled), reads `clause_flags` + `clause_flag_comments` + `clauses`.

**Implementation notes:**
- **Printable HTML beats CSV here** — comment threads don't tabulate. Render a clean report page: one section per flag (title/type/status/created/assigned), the clause snapshot or cited-clause list with citations resolved via a `clauses` lookup, then the comment thread chronologically. Add a `@media print` stylesheet (hide nav, page-break between flags) and a "Print / Save as PDF" note — the browser's print-to-PDF is the PDF export; don't add a PDF library.
- Filters: status multi-select (default: open + in_review), optional date range. Reuse the flag-fetching logic from `admin_flags()` (line 1687) — read it first; it likely already joins comment counts.
- Comments: fetch all `clause_flag_comments` for the selected flags in one query (`.in_("flag_id", [...])`) and group in Python — don't query per flag.
- Also offer `?format=csv` (one row per flag, comments concatenated) reusing #3's CSV streaming helper if it exists — some board secretaries live in Excel.
- `@login_required` is fine (read-only).

**Dependencies:** none. Shares CSV plumbing with #3; better clause links with #18.

---

### (#18) Single-Clause Permalink Page (`/admin/clauses/<id>`)

**What it does.** Flags, audit entries, pending changes, and (post-#11) resident-question citations all reference clauses, but the only way to *reach* a clause is the paginated browse list with filters. This adds a canonical page per clause — full text, summary, tags, citation, link, precedence, edit form, change history, open flags, pending changes — that becomes the hub every other feature links to.

**Files/tables:** `admin_app.py` (new `GET /admin/clauses/<clause_id>` route), new template `admin_clause_detail.html`; link-ification passes over `admin_flags.html`, `admin_flag_detail.html`, `admin_audit.html`, `admin_pending.html`, `admin_questions.html` (#11), `admin_index.html` (clause cards link to their permalink).

**Implementation notes:**
- **Route key decision:** the codebase has two identifiers — the `id` uuid (PK, used by `pending_changes.clause_id` and audit `record_id`) and the human `clause_id` text (e.g. `DECL_27_08`, used by GPT citations and `clause_flags.cited_clause_ids`). Accept **both** in one route: try uuid parse → lookup by `id`; else lookup by `clause_id` (there's an existing `fetch_clause()` helper at line 258 — read it to see which key it uses and extend rather than duplicate). Redirect the non-canonical form to the canonical URL (pick `clause_id` as canonical — it's human-readable and stable).
- Page sections, all from existing data:
  1. **Clause card** — every column except `embedding`; show `status` prominently (this page will surface pending/deleted clauses that browse hides).
  2. **Edit** — reuse the exact form + post target that browse cards use today (`POST /admin/clauses/<clause_id>/update`, line 766) so there's one edit code path.
  3. **History** — #4's lazy history endpoint, or inline if #4 isn't built.
  4. **Open flags** — `clause_flags` where `cited_clause_ids` contains this `clause_id` (`.contains(...)`) or the flag's clause uuid matches.
  5. **Pending changes** — `pending_changes` where `clause_id = <uuid>`, any status, with #5's diff if available.
- This item is mostly *integration*: after the page exists, do a sweep making every clause reference in every template a link. That sweep is what makes #4, #11, #12, #15, and #17 feel finished.
- `@login_required`; edit section visibility follows the same rules as the browse-page edit form.

**Dependencies:** none hard — build any time. Highest leverage *after* #11/#15/#17 exist, since it's the hub they link into. Reuses #4 (history) and #5 (diffs) if present.

---

## Suggested Sequencing & Dependency Summary

| Order | Item | Depends on | Unlocks / enhances |
|---|---|---|---|
| 1 | #11 Resident question log (backend logging half) | — | #12, #13 (hard); #18, #19 (soft) |
| 2 | Roles Rebuild (absorbs #9 + #10) | — | #11 admin page gate; security fix |
| 2b | #11 admin page (`board`-and-up) | Roles Rebuild | — |
| 3 | #5 Pending diff view | — | #6, #14, #15, #18 (reuse diff helper) |
| 4 | #1 Cache TTL (Part B skipped) | — | #7, #8 (propagation without redeploy) |
| 5 | #19 Honest search test (Option B) | — | tuning loop with #11/#12 data |
| — | #2 bulk embeddings, #3 CSV export, #4 clause history, #6 my submissions | — | independent, small |
| — | #7 tag management | best after #1 | — |
| — | #8 document registry | best after #1 | — |
| — | #12 analytics | **#11** + accumulated data | — |
| — | #13 flag-from-question | **#11** | better with #15 |
| — | #14 pending history | best after #5 | — |
| — | #15 flag↔clause links | — | better with #18, #5 |
| — | #16 flag assignment/email | — | assignment first, email v2 |
| — | #17 flag report | — | shares CSV helper with #3 |
| — | #18 clause permalink | — | integration hub; do after #11/#15 for max payoff |

**Standing reminders for every item:**
- hoa-admin has **no dev environment** — test locally against real Supabase, then deploy deliberately.
- hoa-backend changes (`app.py`, `ask_gpt.py`) go through `../hoa-backend-dev/` first and must not break the Carrd chatbot.
- Both services deploy from one repo on push — a single commit ships to both.
- New tables/columns: create in Supabase **before** pushing code that reads them.
- Log meaningful admin actions through the existing `log_audit_event()` / `log_user_activity()` helpers.
