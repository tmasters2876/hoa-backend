import csv
import functools
import io
import math
import os
import re
import threading
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

import bcrypt
import pdfplumber
import requests as http_requests
from rapidfuzz import fuzz

from flask import Flask, Response, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from services import (
    OPENAI_EMBEDDING_MODEL,
    build_clause_embedding_input,
    generate_embedding,
    get_supabase_client,
)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.environ["SECRET_KEY"]
app.permanent_session_lifetime = timedelta(hours=8)



@app.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response

TEMPLATE_HEADERS = [
    "clause_id", "document", "page", "citation",
    "clause_text", "plain_summary", "link", "tags",
    "precedence_level", "match_source",
]
IMPORTABLE_COLUMNS = set(TEMPLATE_HEADERS)

CLAUSE_COLUMNS = (
    "id,clause_id,document,page,citation,clause_text,plain_summary,link,"
    "embedding,match_source,tags,created_at,precedence_level,status"
)
PAGE_SIZE = 25
AUDIT_PAGE_SIZE = 50
PENDING_PAGE_SIZE = 25
ACTIVITY_PAGE_SIZE = 25
FLAG_PAGE_SIZE = 25
QUESTIONS_PAGE_SIZE = 25

# ── Brute force protection ────────────────────────────────────────────────────
_login_attempts: dict[str, list] = {}
_login_attempts_lock = threading.Lock()
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 900  # 15 minutes

def _login_attempt_key(username: str) -> str:
    ip = request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()
    return f"{username.lower()}::{ip}"

def _is_login_locked(key: str) -> bool:
    with _login_attempts_lock:
        attempts = _login_attempts.get(key, [])
        now = datetime.now(timezone.utc).timestamp()
        attempts = [t for t in attempts if now - t < LOGIN_LOCKOUT_SECONDS]
        _login_attempts[key] = attempts
        return len(attempts) >= LOGIN_MAX_ATTEMPTS

def _record_login_failure(key: str):
    with _login_attempts_lock:
        now = datetime.now(timezone.utc).timestamp()
        attempts = _login_attempts.get(key, [])
        attempts = [t for t in attempts if now - t < LOGIN_LOCKOUT_SECONDS]
        attempts.append(now)
        _login_attempts[key] = attempts
        print(f"[brute-force] key={key!r} attempt_count={len(attempts)}", flush=True)

def _clear_login_attempts(key: str):
    with _login_attempts_lock:
        _login_attempts.pop(key, None)


# ── Roles ─────────────────────────────────────────────────────────────────────
# Three hierarchical tiers; each includes everything below it. Source of truth
# is the admin_users.role column (sql/002_roles.sql). LEGACY_SUPERUSERS exists
# only as a last-resort fallback when the DB is unreachable mid-session.

ROLE_RANK = {"member": 0, "board": 1, "superuser": 2}
LEGACY_SUPERUSERS = {'tmasters', 'cmasters', 'admin'}


def _role_rank(role) -> int:
    return ROLE_RANK.get((role or "member").strip().lower(), 0)


def _current_role() -> str:
    return getattr(g, "role", None) or "member"


def _is_superuser_request() -> bool:
    return _role_rank(_current_role()) >= ROLE_RANK["superuser"]


def _load_current_user():
    """Fetch the session user's row fresh from admin_users — the seam that
    makes deactivation/demotion take effect on the next request instead of
    at next login. Returns the row, or None if the user no longer exists.
    Raises on Supabase errors (login_required decides the fallback)."""
    user_id = session.get("user_id")
    if not user_id:
        return None
    result = (
        supabase()
        .from_("admin_users")
        .select("id,username,is_active,role,must_change_password")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


# ── Auth ──────────────────────────────────────────────────────────────────────

def lookup_and_verify_user(username: str, password: str) -> dict | None:
    result = (
        supabase()
        .from_("admin_users")
        .select("id,username,password_hash,is_active,must_change_password,role")
        .eq("username", username.lower())
        .limit(1)
        .execute()
    )
    rows = result.data or []
    if not rows:
        return None
    user = rows[0]
    if not user.get("is_active"):
        return None
    try:
        if bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
            return user
    except Exception:
        pass
    return None


def login_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        # Enforce 8-hour session limit server-side and log expiry
        logged_in_at = session.get("logged_in_at")
        if logged_in_at:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(logged_in_at)
                if age > app.permanent_session_lifetime:
                    log_user_activity(session.get("username"), "session_expired")
                    session.clear()
                    flash("Your session has expired. Please sign in again.", "error")
                    return redirect(url_for("login"))
            except Exception:
                pass
        if session.get("must_change_password"):
            # Allow only the change-password page and logout
            allowed = {url_for("force_change_password"), url_for("force_change_password_post"), url_for("logout")}
            if request.path not in allowed:
                flash("Please set your password before continuing.", "error")
                return redirect(url_for("force_change_password"))
        # Per-request account check: deactivation, deletion, and role changes
        # take effect on the user's NEXT request, not at next login.
        try:
            row = _load_current_user()
            if row is None or not row.get("is_active"):
                log_user_activity(session.get("username"), "session_revoked")
                session.clear()
                flash("Your account is no longer active. Please contact an administrator.", "error")
                return redirect(url_for("login"))
            g.role = (row.get("role") or "member").strip().lower()
            session["role"] = g.role  # keep the fallback snapshot fresh
        except Exception as e:
            # Transient Supabase failure: fall back to the at-login snapshot
            # rather than locking every admin out while the DB hiccups.
            print(f"[auth] WARNING: per-request user check failed, using session snapshot: {e}")
            g.role = session.get("role") or (
                "superuser" if session.get("username") in LEGACY_SUPERUSERS else "member"
            )
        return f(*args, **kwargs)
    return wrapper


def role_required(min_role: str):
    """Gate a route to `min_role` and above (member < board < superuser).
    Includes login_required, which sets g.role fresh each request."""
    min_rank = _role_rank(min_role)

    def decorator(f):
        @functools.wraps(f)
        def check(*args, **kwargs):
            if _role_rank(_current_role()) < min_rank:
                flash("You do not have permission to access that page.", "error")
                return redirect(url_for("admin_home"))
            return f(*args, **kwargs)
        return login_required(check)
    return decorator


def superuser_required(f):
    """Kept as an alias so existing routes don't churn."""
    return role_required("superuser")(f)


# ── Helpers ───────────────────────────────────────────────────────────────────

def supabase():
    return get_supabase_client()


def parse_tags(raw: str) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def serialize_tags(tags) -> str:
    if isinstance(tags, list):
        return ", ".join(str(tag) for tag in tags if str(tag).strip())
    return ""


def clause_has_stale_embedding(clause: dict) -> bool:
    return not clause.get("embedding")


def to_int_or_none(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_field_diff(change: dict) -> list[dict]:
    """Per-field old→new diff for a pending_changes row, computed in Python
    so templates just render. Reusable by the pending page, and later by
    #6 (my submissions) and #14 (pending history).

    - underscore-prefixed keys (_verification etc.) are skipped, matching
      _apply_pending_change
    - None and "" compare equal (form round-trips must not read as changes)
    - lists (tags) compare as lists, display comma-joined
    - action='create': every proposed field renders as new
    - action='delete': empty (the template's delete banner covers it)
    """
    def norm(v):
        if v is None:
            return ""
        if isinstance(v, list):
            return [str(x).strip() for x in v]
        return str(v).strip()

    def display(v):
        if v is None or v == "":
            return None
        if isinstance(v, list):
            return serialize_tags(v)
        return str(v)

    action = change.get("action")
    original = change.get("original_values") or {}
    proposed = change.get("proposed_changes") or {}

    if action == "delete":
        return []

    diffs = []
    if action == "create":
        for field in sorted(proposed):
            if field.startswith("_"):
                continue
            if norm(proposed.get(field)) != "":
                diffs.append({"field": field, "old": None, "new": display(proposed.get(field))})
        return diffs

    for field in sorted(set(original) | set(proposed)):
        if field.startswith("_"):
            continue
        old, new = original.get(field), proposed.get(field)
        if norm(old) != norm(new):
            diffs.append({"field": field, "old": display(old), "new": display(new)})
    return diffs


def _validate_source_fields(form) -> list[str]:
    """Returns a list of error messages. Empty list means valid."""
    errors = []
    if not form.get("citation", "").strip():
        errors.append("Citation is required.")
    if not str(form.get("page", "")).strip():
        errors.append("Page number is required.")
    link = form.get("link", "").strip()
    if not link or not link.startswith("https://drive.google.com/"):
        errors.append("Source link is required — must be a Google Drive URL.")
    return errors


def get_filter_options() -> tuple[list[str], list[str]]:
    result = (
        supabase()
        .from_("clauses")
        .select("document,tags")
        .limit(1000)
        .execute()
    )
    documents = sorted(
        {
            row.get("document", "").strip()
            for row in (result.data or [])
            if isinstance(row.get("document"), str) and row.get("document").strip()
        }
    )
    tags = sorted(
        {
            str(tag).strip()
            for row in (result.data or [])
            for tag in (row.get("tags") or [])
            if str(tag).strip()
        }
    )
    return documents, tags


def sanitize_keyword(keyword: str) -> str:
    """Strip characters that break Supabase .or_() filter syntax."""
    return keyword.translate(str.maketrans("", "", ",()``"))


def build_keyword_filter(keyword: str) -> str:
    pattern = f"%{sanitize_keyword(keyword).strip()}%"
    return (
        f"clause_id.ilike.{pattern},"
        f"citation.ilike.{pattern},"
        f"document.ilike.{pattern},"
        f"plain_summary.ilike.{pattern},"
        f"clause_text.ilike.{pattern}"
    )


def fetch_clause(clause_id: str) -> dict | None:
    result = (
        supabase()
        .from_("clauses")
        .select(CLAUSE_COLUMNS)
        .eq("id", clause_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def browse_clauses(keyword: str, tag: str, document: str, page: int, stale: bool = False) -> tuple[list[dict], int]:
    query = supabase().from_("clauses").select(CLAUSE_COLUMNS, count="exact")
    if keyword:
        query = query.or_(build_keyword_filter(keyword))
    if tag:
        query = query.contains("tags", [tag])
    if document:
        query = query.eq("document", document)
    if stale:
        query = query.is_("embedding", "null")

    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE - 1
    result = query.order("created_at", desc=True).range(start, end).execute()
    return result.data or [], result.count or 0


def run_search_test(question: str, limit: int = 8) -> dict:
    """Primary result: a PREFILTER PREVIEW running the exact production
    scoring loop (ask_gpt._score_clauses via filter_relevant_clauses_debug),
    so admins tune the pipeline residents actually get. The old vector +
    keyword searches are kept as clearly-labeled legacy tools — the resident
    bot uses neither. GPT is never called by this panel."""
    results = {
        "question": question,
        "vector": [],
        "keyword": [],
        "prefilter": None,
        "prefilter_enabled": os.getenv("ENABLE_CLAUSE_PREFILTER", "true").lower() != "false",
    }
    if not question.strip():
        return results

    try:
        from ask_gpt import filter_relevant_clauses_debug, get_all_clauses
        debug = filter_relevant_clauses_debug(question, get_all_clauses())
        debug["top"] = [
            {
                "score": s,
                "clause_id": c.get("clause_id"),
                "document": c.get("document"),
                "citation": c.get("citation"),
                "summary": (c.get("plain_summary") or "")[:160],
                "tags": serialize_tags(c.get("tags")),
            }
            for s, c in debug.pop("matched")[:25]
        ]
        results["prefilter"] = debug
    except Exception as e:
        print(f"[search-test] prefilter preview failed: {e}")

    query_embedding = generate_embedding(question)
    vector_result = supabase().rpc(
        "match_clauses",
        {
            "query_embedding": query_embedding,
            "match_threshold": 0.6,
            "match_count": limit,
        },
    ).execute()
    keyword_result = (
        supabase()
        .from_("clauses")
        .select(CLAUSE_COLUMNS)
        .or_(build_keyword_filter(question))
        .limit(limit)
        .execute()
    )

    results["vector"] = vector_result.data or []
    results["keyword"] = keyword_result.data or []
    return results


def current_filters() -> dict:
    return {
        "q": request.args.get("q", "").strip(),
        "tag": request.args.get("tag", "").strip(),
        "document": request.args.get("document", "").strip(),
        "page": max(1, to_int_or_none(request.args.get("page")) or 1),
        "test_query": request.args.get("test_query", "").strip(),
        "stale": request.args.get("stale", "").strip(),
    }


def index_redirect():
    return redirect(url_for("admin_home"))


@app.context_processor
def inject_superuser():
    pending_count = 0
    open_flags_count = 0
    if session.get("logged_in"):
        try:
            r = (
                supabase()
                .from_("pending_changes")
                .select("id", count="exact")
                .eq("status", "pending")
                .execute()
            )
            pending_count = r.count or 0
        except Exception:
            pass
        try:
            r2 = (
                supabase()
                .from_("clause_flags")
                .select("id", count="exact")
                .eq("status", "open")
                .execute()
            )
            open_flags_count = r2.count or 0
        except Exception:
            pass
    role = _current_role() if session.get("logged_in") else None
    rank = _role_rank(role) if role else -1
    return {
        "is_superuser": rank >= ROLE_RANK["superuser"],
        "is_approver": rank == ROLE_RANK["board"],  # board only, matching old semantics
        "role": role,
        "pending_count": pending_count,
        "open_flags_count": open_flags_count,
    }


# ── Audit log ─────────────────────────────────────────────────────────────────

def log_user_activity(username: str, action: str):
    try:
        supabase().from_("user_activity_log").insert({
            "username": username,
            "action": action,
            "ip_address": request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip(),
            "user_agent": request.user_agent.string,
        }).execute()
    except Exception as e:
        print(f"[activity] WARNING: failed to write activity log: {e}")


def log_audit_event(action, clause_id=None, record_id=None,
                    field_changed=None, old_value=None, new_value=None, notes=None):
    try:
        supabase().from_("clause_audit_log").insert({
            "action": action,
            "clause_id": clause_id,
            "record_id": str(record_id) if record_id is not None else None,
            "changed_by": session.get("username"),
            "field_changed": field_changed,
            "old_value": str(old_value)[:2000] if old_value is not None else None,
            "new_value": str(new_value)[:2000] if new_value is not None else None,
            "notes": notes,
        }).execute()
    except Exception as e:
        print(f"[audit] WARNING: failed to write audit log: {e}")


_CLAUSE_TRACKED_FIELDS = [
    "clause_id", "document", "page", "citation",
    "clause_text", "plain_summary", "link", "tags", "precedence_level",
]

def _log_clause_field_changes(record_id, existing, new_payload):
    """Log one audit row per changed clause field."""
    text_clause_id = (new_payload.get("clause_id") or existing.get("clause_id"))
    for field in _CLAUSE_TRACKED_FIELDS:
        old = existing.get(field)
        new = new_payload.get(field)
        # Normalise for comparison: treat None and "" as equivalent
        old_str = str(old) if old is not None else ""
        new_str = str(new) if new is not None else ""
        if old_str != new_str:
            log_audit_event(
                action="updated",
                clause_id=text_clause_id,
                record_id=record_id,
                field_changed=field,
                old_value=old_str or None,
                new_value=new_str or None,
            )


def extract_google_drive_id(url: str) -> str | None:
    """Return the file ID from any common Google Drive URL format."""
    if not url:
        return None
    m = re.search(r'/file/d/([^/?#]+)', url)
    if m:
        return m.group(1)
    m = re.search(r'[?&]id=([^&#]+)', url)
    if m:
        return m.group(1)
    return None


def download_pdf_page_text(drive_url: str, page_number: int) -> str | None:
    """Download a single PDF page from Google Drive and return its text."""
    try:
        file_id = extract_google_drive_id(drive_url)
        if not file_id:
            return None
        download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
        response = http_requests.get(download_url, timeout=10)
        if response.status_code != 200:
            return None
        with pdfplumber.open(io.BytesIO(response.content)) as pdf:
            idx = page_number - 1
            if idx < 0 or idx >= len(pdf.pages):
                return None
            return pdf.pages[idx].extract_text() or ""
    except Exception:
        return None


def verify_clause_against_source(clause_text: str, link: str, page: int) -> dict:
    """Fuzzy-match clause_text against the stated PDF page. Always returns a dict."""
    if not clause_text:
        return {"status": "skipped", "score": 0, "reason": "No clause text to verify"}
    page_text = download_pdf_page_text(link, page)
    if page_text is None:
        return {"status": "skipped", "score": 0, "reason": "Could not access PDF"}
    if not page_text.strip():
        return {"status": "skipped", "score": 0, "reason": "PDF page returned no text"}
    score = fuzz.partial_ratio(clause_text.lower(), page_text.lower())
    if score >= 80:
        return {"status": "verified", "score": score, "reason": f"Text found on page {page}"}
    if score >= 50:
        return {"status": "partial", "score": score, "reason": f"Partial match on page {page}"}
    return {"status": "not_found", "score": score, "reason": f"Text not found on page {page}"}


def submit_pending_change(clause_uuid, action: str, proposed_changes=None, original_values=None) -> dict | None:
    """Insert a pending_changes row. Returns {"id": ..., "verification": ...} or None on failure."""
    verification = None
    if action in ("create", "edit") and proposed_changes:
        clause_text = (proposed_changes.get("clause_text") or "").strip()
        link = (proposed_changes.get("link") or "").strip()
        page = proposed_changes.get("page")
        if clause_text and link and page:
            verification = verify_clause_against_source(clause_text, link, int(page))
        else:
            verification = {"status": "skipped", "score": 0, "reason": "Missing text, link, or page"}
        verification["verified_at"] = datetime.now(timezone.utc).isoformat()
        proposed_changes = dict(proposed_changes)
        proposed_changes["_verification"] = verification

    try:
        result = supabase().from_("pending_changes").insert({
            "clause_id": str(clause_uuid) if clause_uuid is not None else None,
            "submitted_by": session.get("username"),
            "action": action,
            "proposed_changes": proposed_changes,
            "original_values": original_values,
            "status": "pending",
        }).execute()
        rows = result.data or []
        change_id = rows[0].get("id") if rows else None
        return {"id": change_id, "verification": verification}
    except Exception as e:
        print(f"[pending] WARNING: failed to write pending_changes: {e}")
        return None


def _apply_pending_change(change: dict):
    """Apply an approved pending change to the clauses table."""
    clause_uuid = change.get("clause_id")
    action = change.get("action")
    proposed = change.get("proposed_changes") or {}
    if action == "create":
        supabase().from_("clauses").update({"status": "approved"}).eq("id", clause_uuid).execute()
    elif action == "edit":
        payload = {k: v for k, v in proposed.items() if not k.startswith('_')}
        payload["status"] = "approved"
        supabase().from_("clauses").update(payload).eq("id", clause_uuid).execute()
    elif action == "delete":
        supabase().from_("clauses").delete().eq("id", clause_uuid).execute()


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.get("/login")
def login():
    if session.get("logged_in"):
        return redirect(url_for("admin_home"))
    return render_template("admin_login.html")


@app.post("/login")
def login_post():
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    key = _login_attempt_key(username)

    if _is_login_locked(key):
        log_user_activity(username.strip().lower() or username, "login_blocked")
        flash("Too many failed attempts. Please wait 15 minutes before trying again.", "error")
        return render_template("admin_login.html"), 429

    user = lookup_and_verify_user(username, password)
    if user:
        _clear_login_attempts(key)
        session.clear() 
        session.permanent = True
        session["logged_in"] = True
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["role"] = (user.get("role") or "member").strip().lower()
        session["logged_in_at"] = datetime.now(timezone.utc).isoformat()
        log_user_activity(user["username"], "login_success")
        if user.get("must_change_password"):
            session["must_change_password"] = True
            flash("Welcome! Please set your own password before continuing.", "success")
            return redirect(url_for("force_change_password"))
        return redirect(url_for("admin_home"))

    _record_login_failure(key)
    log_user_activity(username.strip().lower() or username, "login_failed")
    flash("Invalid username or password.", "error")
    return render_template("admin_login.html"), 401


@app.post("/logout")
def logout():
    log_user_activity(session.get("username"), "logout")
    session.clear()
    return redirect(url_for("login"))


@app.get("/admin/change-password")
def force_change_password():
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    if not session.get("must_change_password"):
        return redirect(url_for("admin_home"))
    return render_template("admin_change_password.html")


@app.post("/admin/change-password")
def force_change_password_post():
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    if not session.get("must_change_password"):
        return redirect(url_for("admin_home"))

    user_id = session.get("user_id")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not new_password or not confirm_password:
        flash("Both password fields are required.", "error")
        return render_template("admin_change_password.html"), 400
    if len(new_password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return render_template("admin_change_password.html"), 400
    if new_password != confirm_password:
        flash("Passwords do not match.", "error")
        return render_template("admin_change_password.html"), 400

    result = (
        supabase()
        .from_("admin_users")
        .select("password_hash,username")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    if not rows:
        flash("User not found.", "error")
        return render_template("admin_change_password.html"), 400

    stored_hash = rows[0]["password_hash"]
    try:
        if bcrypt.checkpw(new_password.encode(), stored_hash.encode()):
            flash("New password cannot be the same as your temporary password.", "error")
            return render_template("admin_change_password.html"), 400
    except Exception:
        pass

    password_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt(rounds=12)).decode()
    supabase().from_("admin_users").update({
        "password_hash": password_hash,
        "must_change_password": False,
    }).eq("id", user_id).execute()

    log_audit_event(action="password_changed_first_login", new_value=rows[0]["username"])
    session.pop("must_change_password", None)
    flash("Password updated successfully. Welcome!", "success")
    return redirect(url_for("admin_home"))


# ── Public routes ─────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"ok": True}


# ── Protected routes ──────────────────────────────────────────────────────────

@app.get("/")
@login_required
def root():
    return index_redirect()


@app.get("/admin")
@login_required
def admin_home():
    filters = current_filters()
    stale_filter = bool(filters["stale"])
    try:
        clauses, total_count = browse_clauses(
            keyword=filters["q"],
            tag=filters["tag"],
            document=filters["document"],
            page=filters["page"],
            stale=stale_filter,
        )
    except Exception:
        flash("Search filter could not be applied — showing all clauses.", "error")
        clauses, total_count = browse_clauses(keyword="", tag=filters["tag"], document=filters["document"], page=1)
    documents, tags = get_filter_options()
    search_test = run_search_test(filters["test_query"]) if filters["test_query"] else None

    # Count clauses with stale embeddings for the dashboard banner
    try:
        stale_result = supabase().from_("clauses").select("id", count="exact").is_("embedding", "null").execute()
        stale_count = stale_result.count or 0
    except Exception:
        stale_count = 0

    total_pages = max(1, math.ceil(total_count / PAGE_SIZE)) if total_count else 1
    query_args = {k: v for k, v in filters.items() if k != "page" and v}
    prev_url = None
    next_url = None
    if filters["page"] > 1:
        prev_url = url_for("admin_home") + "?" + urlencode({**query_args, "page": filters["page"] - 1})
    if filters["page"] < total_pages:
        next_url = url_for("admin_home") + "?" + urlencode({**query_args, "page": filters["page"] + 1})

    for clause in clauses:
        clause["tags_csv"] = serialize_tags(clause.get("tags"))
        clause["embedding_stale"] = clause_has_stale_embedding(clause)

    if search_test:
        for row in search_test["vector"]:
            row["tags_csv"] = serialize_tags(row.get("tags"))
        for row in search_test["keyword"]:
            row["tags_csv"] = serialize_tags(row.get("tags"))

    flagged_clause_ids = _get_open_flag_clause_ids()

    return render_template(
        "admin_index.html",
        clauses=clauses,
        documents=documents,
        tags=tags,
        filters=filters,
        total_count=total_count,
        total_pages=total_pages,
        prev_url=prev_url,
        next_url=next_url,
        search_test=search_test,
        embedding_model=OPENAI_EMBEDDING_MODEL,
        stale_count=stale_count,
        flagged_clause_ids=flagged_clause_ids,
    )


@app.post("/admin/clauses")
@login_required
def create_clause():
    errors = _validate_source_fields(request.form)
    if errors:
        for msg in errors:
            flash(msg, "error")
        return redirect(url_for("admin_home"))

    self_approve = bool(request.form.get("self_approve")) and _is_superuser_request()

    payload = {
        "clause_id": request.form.get("clause_id", "").strip() or None,
        "document": request.form.get("document", "").strip() or None,
        "page": to_int_or_none(request.form.get("page")),
        "citation": request.form.get("citation", "").strip() or None,
        "clause_text": request.form.get("clause_text", "").strip() or None,
        "plain_summary": request.form.get("plain_summary", "").strip() or None,
        "link": request.form.get("link", "").strip() or None,
        "tags": parse_tags(request.form.get("tags", "")),
        "precedence_level": to_int_or_none(request.form.get("precedence_level")),
        "match_source": "Admin Added",
        "embedding": None,
        "status": "approved" if self_approve else "pending",
    }
    result = supabase().from_("clauses").insert(payload).execute()
    inserted_id = (result.data or [{}])[0].get("id")

    proposed_for_log = {k: v for k, v in payload.items() if k not in ("embedding", "status")}
    pending_result = submit_pending_change(
        clause_uuid=inserted_id,
        action="create",
        proposed_changes=proposed_for_log,
        original_values=None,
    )
    verif = (pending_result or {}).get("verification") or {}
    v_status = verif.get("status", "skipped")
    v_score = verif.get("score", 0)
    verif_note = f"source: {v_status} ({v_score}%)" if v_status != "skipped" else "source: skipped"
    log_audit_event(
        action="change_submitted",
        clause_id=payload.get("clause_id"),
        record_id=inserted_id,
        notes=f"create; {verif_note}" + (" (self-approved)" if self_approve else ""),
    )
    if self_approve:
        flash("Clause added and self-approved. Embedding is marked stale until you regenerate it.", "success")
    else:
        flash("Clause submitted for approval — a second admin must approve it before it appears in search.", "success")
    return redirect(url_for("admin_home"))


@app.post("/admin/clauses/<clause_id>/update")
@login_required
def update_clause(clause_id: str):
    # form may carry a same-app "next" path (e.g. the clause permalink page)
    nxt = request.form.get("next", "")
    back = redirect(nxt) if nxt.startswith("/admin") else redirect(url_for("admin_home"))

    errors = _validate_source_fields(request.form)
    if errors:
        for msg in errors:
            flash(msg, "error")
        return back

    existing = fetch_clause(clause_id)
    if not existing:
        flash(f"Clause {clause_id} was not found.", "error")
        return back

    self_approve = bool(request.form.get("self_approve")) and _is_superuser_request()

    new_clause_text = request.form.get("clause_text", "").strip()
    new_plain_summary = request.form.get("plain_summary", "").strip()
    text_changed = (
        (existing.get("clause_text") or "").strip() != new_clause_text
        or (existing.get("plain_summary") or "").strip() != new_plain_summary
    )

    payload = {
        "clause_id": request.form.get("clause_id", "").strip() or None,
        "document": request.form.get("document", "").strip() or None,
        "page": to_int_or_none(request.form.get("page")),
        "citation": request.form.get("citation", "").strip() or None,
        "clause_text": new_clause_text or None,
        "plain_summary": new_plain_summary or None,
        "link": request.form.get("link", "").strip() or None,
        "tags": parse_tags(request.form.get("tags", "")),
        "precedence_level": to_int_or_none(request.form.get("precedence_level")),
    }
    if text_changed:
        payload["embedding"] = None
        payload["match_source"] = "Admin Edited (Embedding Stale)"

    original_for_log = {k: existing.get(k) for k in payload}

    if self_approve:
        supabase().from_("clauses").update(payload).eq("id", clause_id).execute()
        _log_clause_field_changes(clause_id, existing, payload)
        log_audit_event(action="change_approved", clause_id=payload.get("clause_id") or existing.get("clause_id"),
                        record_id=clause_id, notes="edit self-approved")
        flash("Edit self-approved and applied." + (" Embedding marked stale." if text_changed else ""), "success")
    else:
        pending_result = submit_pending_change(
            clause_uuid=clause_id,
            action="edit",
            proposed_changes=payload,
            original_values=original_for_log,
        )
        verif = (pending_result or {}).get("verification") or {}
        v_status = verif.get("status", "skipped")
        v_score = verif.get("score", 0)
        verif_note = f"source: {v_status} ({v_score}%)" if v_status != "skipped" else "source: skipped"
        log_audit_event(action="change_submitted", clause_id=payload.get("clause_id") or existing.get("clause_id"),
                        record_id=clause_id, notes=f"edit; {verif_note}")
        flash("Edit submitted for approval — the live clause is unchanged until a second admin approves.", "success")
    return back


@app.post("/admin/clauses/<clause_id>/update-json")
@login_required
def update_clause_json(clause_id: str):
    errors = _validate_source_fields(request.form)
    if errors:
        return jsonify({"ok": False, "message": " ".join(errors)})

    existing = fetch_clause(clause_id)
    if not existing:
        return jsonify({"ok": False, "message": f"Clause {clause_id} not found."}), 404

    self_approve = bool(request.form.get("self_approve")) and _is_superuser_request()

    new_clause_text = request.form.get("clause_text", "").strip()
    new_plain_summary = request.form.get("plain_summary", "").strip()
    text_changed = (
        (existing.get("clause_text") or "").strip() != new_clause_text
        or (existing.get("plain_summary") or "").strip() != new_plain_summary
    )

    payload = {
        "clause_id": request.form.get("clause_id", "").strip() or None,
        "document": request.form.get("document", "").strip() or None,
        "page": to_int_or_none(request.form.get("page")),
        "citation": request.form.get("citation", "").strip() or None,
        "clause_text": new_clause_text or None,
        "plain_summary": new_plain_summary or None,
        "link": request.form.get("link", "").strip() or None,
        "tags": parse_tags(request.form.get("tags", "")),
        "precedence_level": to_int_or_none(request.form.get("precedence_level")),
    }
    if text_changed:
        payload["embedding"] = None
        payload["match_source"] = "Admin Edited (Embedding Stale)"

    original_for_log = {k: existing.get(k) for k in payload}

    if self_approve:
        supabase().from_("clauses").update(payload).eq("id", clause_id).execute()
        _log_clause_field_changes(clause_id, existing, payload)
        log_audit_event(action="change_approved", clause_id=payload.get("clause_id") or existing.get("clause_id"),
                        record_id=clause_id, notes="edit self-approved")
        embedding_stale = text_changed or not existing.get("embedding")
        msg = "⚠ Self-approved and applied." + (" Embedding now stale." if text_changed else "")
        return jsonify({"ok": True, "message": msg, "embedding_stale": embedding_stale, "pending": False})
    else:
        pending_result = submit_pending_change(
            clause_uuid=clause_id,
            action="edit",
            proposed_changes=payload,
            original_values=original_for_log,
        )
        verif = (pending_result or {}).get("verification") or {}
        v_status = verif.get("status", "skipped")
        v_score = verif.get("score", 0)
        verif_note = f"source: {v_status} ({v_score}%)" if v_status != "skipped" else "source: skipped"
        log_audit_event(action="change_submitted", clause_id=payload.get("clause_id") or existing.get("clause_id"),
                        record_id=clause_id, notes=f"edit; {verif_note}")
        return jsonify({"ok": True, "message": "Edit submitted for approval.", "embedding_stale": False, "pending": True})


@app.post("/admin/clauses/<clause_id>/delete")
@login_required
def delete_clause(clause_id: str):
    existing = fetch_clause(clause_id)
    if not existing:
        flash(f"Clause {clause_id} was not found.", "error")
        return redirect(url_for("admin_home"))
    submit_pending_change(
        clause_uuid=clause_id,
        action="delete",
        proposed_changes=None,
        original_values={k: existing.get(k) for k in _CLAUSE_TRACKED_FIELDS},
    )
    log_audit_event(
        action="change_submitted",
        clause_id=existing.get("clause_id"),
        record_id=clause_id,
        notes="delete",
    )
    flash("Deletion submitted for approval — a second superuser must approve before the clause is removed.", "success")
    return redirect(url_for("admin_home"))


@app.post("/admin/clauses/<clause_id>/regenerate-embedding")
@login_required
def regenerate_clause_embedding(clause_id: str):
    clause = fetch_clause(clause_id)
    if not clause:
        flash(f"Clause {clause_id} was not found.", "error")
        return redirect(url_for("admin_home"))

    embedding_input = build_clause_embedding_input(clause)
    if not embedding_input.strip():
        flash("Cannot generate an embedding for an empty clause.", "error")
        return redirect(url_for("admin_home"))

    embedding = generate_embedding(embedding_input)
    supabase().from_("clauses").update(
        {
            "embedding": embedding,
            "match_source": "Admin Refreshed Embedding",
        }
    ).eq("id", clause_id).execute()
    log_audit_event(
        action="embedding_regenerated",
        clause_id=clause.get("clause_id"),
        record_id=clause_id,
    )
    flash(f"Regenerated embedding for clause {clause_id}.", "success")
    return redirect(url_for("admin_home"))


@app.get("/admin/import/template")
@superuser_required
def import_template():
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=TEMPLATE_HEADERS)
    writer.writeheader()
    writer.writerow({
        "clause_id": "ART6-12",
        "document": "Declaration",
        "page": "14",
        "citation": "Art. VI, Sec. 3",
        "clause_text": "No structure shall be erected, placed, or altered on any lot until construction plans have been approved by the ARC.",
        "plain_summary": "Residents may not build or alter any structure without prior ARC approval.",
        "link": "",
        "tags": "arc, structure, approval",
        "precedence_level": "2",
        "match_source": "CSV Import",
    })
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=clauses_template.csv"},
    )


@app.post("/admin/import")
@superuser_required
def import_clauses():
    file = request.files.get("csv_file")
    if not file or not file.filename:
        flash("No file selected.", "error")
        return redirect(url_for("admin_home"))
    if not file.filename.lower().endswith(".csv"):
        flash("File must be a .csv.", "error")
        return redirect(url_for("admin_home"))

    try:
        text = file.stream.read().decode("utf-8-sig")  # strips BOM if present
    except Exception:
        flash("Could not read file. Make sure it is UTF-8 encoded.", "error")
        return redirect(url_for("admin_home"))

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        flash("CSV appears to be empty.", "error")
        return redirect(url_for("admin_home"))

    headers = {h.strip() for h in reader.fieldnames}

    unknown = headers - IMPORTABLE_COLUMNS
    if unknown:
        flash(
            f"Unknown column(s): {', '.join(sorted(unknown))}. "
            f"Allowed columns: {', '.join(TEMPLATE_HEADERS)}.",
            "error",
        )
        return redirect(url_for("admin_home"))

    if not headers & {"clause_text", "plain_summary"}:
        flash("CSV must contain at least a 'clause_text' or 'plain_summary' column.", "error")
        return redirect(url_for("admin_home"))

    rows_ok = []
    row_errors = []
    source_skip_count = 0

    for i, row in enumerate(reader, start=2):
        errors = []

        page_raw = (row.get("page") or "").strip()
        page = None
        if page_raw:
            try:
                page = int(page_raw)
                if page < 1:
                    errors.append("'page' must be a positive integer")
            except ValueError:
                errors.append(f"'page' value '{page_raw}' is not an integer")

        prec_raw = (row.get("precedence_level") or "").strip()
        precedence_level = None
        if prec_raw:
            try:
                precedence_level = int(prec_raw)
                if precedence_level < 0:
                    errors.append("'precedence_level' must be a non-negative integer")
            except ValueError:
                errors.append(f"'precedence_level' value '{prec_raw}' is not an integer")

        clause_text = (row.get("clause_text") or "").strip()
        plain_summary = (row.get("plain_summary") or "").strip()
        if not clause_text and not plain_summary:
            errors.append("at least one of 'clause_text' or 'plain_summary' must be non-empty")

        # Source field validation
        missing_source = []
        if not (row.get("citation") or "").strip():
            missing_source.append("citation")
        if not page_raw:
            missing_source.append("page")
        link_val = (row.get("link") or "").strip()
        if not link_val or not link_val.startswith("https://drive.google.com/"):
            missing_source.append("link")
        if missing_source:
            source_skip_count += 1
            missing_str = ", ".join(missing_source)
            row_errors.append(f"Row {i}: skipped — missing source fields: {missing_str}")
            log_audit_event(
                action="csv_import_skipped",
                notes=f"row {i}: missing {missing_str}",
            )
            continue

        if errors:
            row_errors.append(f"Row {i}: {'; '.join(errors)}")
            continue

        rows_ok.append({
            "clause_id": (row.get("clause_id") or "").strip() or None,
            "document": (row.get("document") or "").strip() or None,
            "page": page,
            "citation": (row.get("citation") or "").strip() or None,
            "clause_text": clause_text or None,
            "plain_summary": plain_summary or None,
            "link": link_val or None,
            "tags": parse_tags(row.get("tags") or ""),
            "precedence_level": precedence_level,
            "match_source": (row.get("match_source") or "").strip() or "CSV Import",
            "embedding": None,
        })

    for err in row_errors:
        flash(err, "error")

    if source_skip_count:
        flash(
            f"{source_skip_count} row{'s' if source_skip_count != 1 else ''} skipped — "
            "missing citation, page, or source link.",
            "error",
        )

    if rows_ok:
        supabase().from_("clauses").insert(rows_ok).execute()
        n = len(rows_ok)
        log_audit_event(
            action="csv_import",
            notes=f"{n} clause{'s' if n != 1 else ''} imported",
        )
        flash(
            f"Imported {n} clause{'s' if n != 1 else ''}. "
            "Embeddings are stale — regenerate as needed.",
            "success",
        )
    elif not row_errors and not source_skip_count:
        flash("CSV had no data rows.", "error")

    return redirect(url_for("admin_home"))


@app.get("/admin/bulk-delete/template")
@superuser_required
def bulk_delete_template():
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=TEMPLATE_HEADERS)
    writer.writeheader()
    writer.writerow({
        "clause_id": "ART6-12",
        "document": "Declaration",
        "page": "14",
        "citation": "Art. VI, Sec. 3",
        "clause_text": "No structure shall be erected, placed, or altered on any lot until construction plans have been approved by the ARC.",
        "plain_summary": "Residents may not build or alter any structure without prior ARC approval.",
        "link": "",
        "tags": "arc, structure, approval",
        "precedence_level": "2",
        "match_source": "CSV Import",
    })
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=bulk_delete_template.csv"},
    )


@app.post("/admin/bulk-delete")
@superuser_required
def bulk_delete_clauses():
    file = request.files.get("delete_csv_file")
    if not file or not file.filename:
        flash("No file selected.", "error")
        return redirect(url_for("admin_home"))
    if not file.filename.lower().endswith(".csv"):
        flash("File must be a .csv.", "error")
        return redirect(url_for("admin_home"))

    try:
        text = file.stream.read().decode("utf-8-sig")
    except Exception:
        flash("Could not read file. Make sure it is UTF-8 encoded.", "error")
        return redirect(url_for("admin_home"))

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        flash("CSV appears to be empty.", "error")
        return redirect(url_for("admin_home"))

    headers = {h.strip() for h in reader.fieldnames}

    unknown = headers - IMPORTABLE_COLUMNS
    if unknown:
        flash(
            f"Unknown column(s): {', '.join(sorted(unknown))}. "
            f"Allowed columns: {', '.join(TEMPLATE_HEADERS)}.",
            "error",
        )
        return redirect(url_for("admin_home"))

    if "clause_id" not in headers:
        flash("CSV must contain a 'clause_id' column — this is used to identify rows for deletion.", "error")
        return redirect(url_for("admin_home"))

    ids_to_delete = []
    row_errors = []

    for i, row in enumerate(reader, start=2):
        clause_id = (row.get("clause_id") or "").strip()
        if not clause_id:
            row_errors.append(f"Row {i}: 'clause_id' is empty — skipped")
            continue
        ids_to_delete.append(clause_id)

    for err in row_errors:
        flash(err, "error")

    if not ids_to_delete:
        flash("No valid clause_id values found — nothing deleted.", "error")
        return redirect(url_for("admin_home"))

    deleted_count = 0
    delete_errors = []
    for clause_id in ids_to_delete:
        try:
            supabase().from_("clauses").delete().eq("clause_id", clause_id).execute()
            deleted_count += 1
        except Exception as e:
            delete_errors.append(f"clause_id '{clause_id}': {e}")

    for err in delete_errors:
        flash(f"Delete failed — {err}", "error")

    if deleted_count:
        noun = "clause" if deleted_count == 1 else "clauses"
        ids_preview = ", ".join(ids_to_delete[:10])
        if len(ids_to_delete) > 10:
            ids_preview += f" … and {len(ids_to_delete) - 10} more"
        log_audit_event(
            action="csv_bulk_delete",
            notes=f"{deleted_count} {noun} deleted: {ids_preview}",
        )
        flash(f"Deleted {deleted_count} {noun}: {ids_preview}.", "success")

    return redirect(url_for("admin_home"))


# ── User management routes ────────────────────────────────────────────────────

@app.get("/admin/users")
@login_required
def admin_users():
    is_superuser = _is_superuser_request()

    users = []
    last_logins = {}
    if is_superuser:
        result = (
            supabase()
            .from_("admin_users")
            .select("id,username,is_active,created_at,must_change_password,role")
            .order("created_at")
            .execute()
        )
        users = result.data or []

        try:
            login_rows = (
                supabase()
                .from_("user_activity_log")
                .select("username,occurred_at")
                .eq("action", "login_success")
                .order("occurred_at", desc=True)
                .limit(500)
                .execute()
            ).data or []
            seen = set()
            for row in login_rows:
                uname = row["username"]
                if uname not in seen:
                    seen.add(uname)
                    ts = row["occurred_at"] or ""
                    last_logins[uname] = ts[:16].replace('T', ' ') if ts else ""
        except Exception as e:
            print(f"[activity] WARNING: failed to fetch last logins: {e}")

    # Activity feed (superusers only)
    activity_log = []
    activity_page = max(1, to_int_or_none(request.args.get("activity_page")) or 1)
    activity_total_pages = 1
    if is_superuser:
        try:
            act_start = (activity_page - 1) * ACTIVITY_PAGE_SIZE
            act_end = act_start + ACTIVITY_PAGE_SIZE - 1
            act_result = (
                supabase()
                .from_("user_activity_log")
                .select("username,action,ip_address,occurred_at", count="exact")
                .order("occurred_at", desc=True)
                .range(act_start, act_end)
                .execute()
            )
            activity_log = act_result.data or []
            activity_total_pages = max(1, math.ceil((act_result.count or 0) / ACTIVITY_PAGE_SIZE))
        except Exception as e:
            print(f"[activity] WARNING: failed to fetch activity log: {e}")

    for user in users:
        user["role"] = (user.get("role") or "member").strip().lower()

    return render_template(
        "admin_users.html",
        users=users,
        current_user_id=session.get("user_id"),
        is_superuser=is_superuser,
        last_logins=last_logins,
        activity_log=activity_log,
        activity_page=activity_page,
        activity_total_pages=activity_total_pages,
    )


@app.post("/admin/users")
@superuser_required
def create_user():
    username = request.form.get("username", "").strip().lower()
    password = request.form.get("password", "").strip()
    if not username or not password:
        flash("Username and password are both required.", "error")
        return redirect(url_for("admin_users"))
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    try:
        role = (request.form.get("role") or "member").strip().lower()
        if role not in ROLE_RANK:
            role = "member"
        supabase().from_("admin_users").insert({
            "username": username,
            "password_hash": password_hash,
            "must_change_password": True,
            "role": role,
            # legacy mirror kept in sync as a rollback aid (see sql/002_roles.sql)
            "is_approver": role == "board",
        }).execute()
        log_audit_event(action="user_added", new_value=username)
        flash(f"User '{username}' created.", "success")
    except Exception as e:
        if "unique" in str(e).lower():
            flash(f"Username '{username}' already exists.", "error")
        else:
            flash(f"Could not create user: {e}", "error")
    return redirect(url_for("admin_users"))


@app.post("/admin/users/<user_id>/toggle")
@superuser_required
def toggle_user(user_id: str):
    if user_id == session.get("user_id"):
        flash("You cannot deactivate your own account.", "error")
        return redirect(url_for("admin_users"))
    result = (
        supabase()
        .from_("admin_users")
        .select("username,is_active")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    if not rows:
        flash("User not found.", "error")
        return redirect(url_for("admin_users"))
    user = rows[0]
    new_status = not user["is_active"]
    supabase().from_("admin_users").update({"is_active": new_status}).eq("id", user_id).execute()
    verb = "activated" if new_status else "deactivated"
    log_audit_event(
        action=f"user_{verb}",
        new_value=user["username"],
    )
    flash(f"User '{user['username']}' {verb}.", "success")
    return redirect(url_for("admin_users"))


@app.post("/admin/users/<user_id>/delete")
@superuser_required
def delete_user(user_id: str):
    if user_id == session.get("user_id"):
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("admin_users"))
    result = (
        supabase()
        .from_("admin_users")
        .select("username")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    if not rows:
        flash("User not found.", "error")
        return redirect(url_for("admin_users"))
    username = rows[0]["username"]
    supabase().from_("admin_users").delete().eq("id", user_id).execute()
    log_audit_event(action="user_deleted", new_value=username)
    flash(f"User '{username}' deleted.", "success")
    return redirect(url_for("admin_users"))


@app.post("/admin/users/<user_id>/set-role")
@superuser_required
def set_role(user_id: str):
    new_role = (request.form.get("role") or "").strip().lower()
    if new_role not in ROLE_RANK:
        flash("Invalid role.", "error")
        return redirect(url_for("admin_users"))
    if user_id == session.get("user_id"):
        flash("You cannot change your own role.", "error")
        return redirect(url_for("admin_users"))
    result = (
        supabase()
        .from_("admin_users")
        .select("username,role")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    if not rows:
        flash("User not found.", "error")
        return redirect(url_for("admin_users"))
    user = rows[0]
    old_role = (user.get("role") or "member").strip().lower()
    if old_role == new_role:
        flash(f"'{user['username']}' is already {new_role}.", "success")
        return redirect(url_for("admin_users"))
    if old_role == "superuser":
        remaining = (
            supabase()
            .from_("admin_users")
            .select("id")
            .eq("role", "superuser")
            .eq("is_active", True)
            .execute()
        ).data or []
        if len(remaining) <= 1:
            flash("Cannot demote the last superuser.", "error")
            return redirect(url_for("admin_users"))
    supabase().from_("admin_users").update({
        "role": new_role,
        # legacy mirror kept in sync as a rollback aid (see sql/002_roles.sql)
        "is_approver": new_role == "board",
    }).eq("id", user_id).execute()
    log_audit_event(action="role_updated", new_value=f"{user['username']}: {old_role} → {new_role}")
    flash(f"Role for '{user['username']}' changed to {new_role}. Takes effect on their next request.", "success")
    return redirect(url_for("admin_users"))


@app.post("/admin/users/<user_id>/reset-password")
@superuser_required
def reset_user_password(user_id: str):
    if user_id == session.get("user_id"):
        return jsonify({"ok": False, "message": "Use the manual Supabase method to reset your own password."})

    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not new_password or not confirm_password:
        return jsonify({"ok": False, "message": "Both password fields are required."})
    if len(new_password) < 8:
        return jsonify({"ok": False, "message": "Password must be at least 8 characters."})
    if new_password != confirm_password:
        return jsonify({"ok": False, "message": "Passwords do not match."})

    result = supabase().from_("admin_users").select("id").eq("id", user_id).limit(1).execute()
    if not (result.data or []):
        return jsonify({"ok": False, "message": "User not found."})

    target = supabase().from_("admin_users").select("username").eq("id", user_id).limit(1).execute()
    target_username = (target.data or [{}])[0].get("username")
    password_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt(rounds=12)).decode()
    supabase().from_("admin_users").update({
        "password_hash": password_hash,
        "must_change_password": True,
    }).eq("id", user_id).execute()
    log_audit_event(action="password_reset", new_value=target_username)
    return jsonify({"ok": True, "message": "Password reset successfully. User will be prompted to set a new password on next login."})


@app.get("/admin/audit")
@superuser_required
def admin_audit():
    who = request.args.get("who", "").strip()
    action_filter = request.args.get("action_filter", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    page = max(1, to_int_or_none(request.args.get("page")) or 1)

    query = supabase().from_("clause_audit_log").select("*", count="exact")
    if who:
        query = query.eq("changed_by", who)
    if action_filter:
        query = query.eq("action", action_filter)
    if date_from:
        query = query.gte("changed_at", date_from)
    if date_to:
        # include the full end date by going to end of day
        query = query.lte("changed_at", date_to + "T23:59:59Z")

    start = (page - 1) * AUDIT_PAGE_SIZE
    end = start + AUDIT_PAGE_SIZE - 1
    result = query.order("changed_at", desc=True).range(start, end).execute()

    entries = result.data or []
    total_count = result.count or 0
    total_pages = max(1, math.ceil(total_count / AUDIT_PAGE_SIZE))

    # Truncate old/new value for display
    for entry in entries:
        for field in ("old_value", "new_value"):
            val = entry.get(field) or ""
            entry[f"{field}_display"] = val[:80] + ("…" if len(val) > 80 else "")

    audit_filters = {"who": who, "action_filter": action_filter, "date_from": date_from, "date_to": date_to}
    query_args = {k: v for k, v in audit_filters.items() if v}

    prev_url = None
    next_url = None
    if page > 1:
        prev_url = url_for("admin_audit") + "?" + urlencode({**query_args, "page": page - 1})
    if page < total_pages:
        next_url = url_for("admin_audit") + "?" + urlencode({**query_args, "page": page + 1})

    known_actions = [
        "created", "updated", "deleted", "embedding_regenerated",
        "csv_import", "csv_bulk_delete",
        "user_added", "user_activated", "user_deactivated", "user_deleted", "password_reset",
        "change_submitted", "change_approved", "change_rejected",
    ]

    return render_template(
        "admin_audit.html",
        entries=entries,
        total_count=total_count,
        total_pages=total_pages,
        page=page,
        prev_url=prev_url,
        next_url=next_url,
        filters=audit_filters,
        known_actions=known_actions,
    )


@app.post("/admin/users/change-password")
@login_required
def change_own_password():
    user_id = session.get("user_id")
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not current_password or not new_password or not confirm_password:
        return jsonify({"ok": False, "message": "All three fields are required."})
    if len(new_password) < 8:
        return jsonify({"ok": False, "message": "New password must be at least 8 characters."})
    if new_password != confirm_password:
        return jsonify({"ok": False, "message": "New passwords do not match."})

    result = (
        supabase()
        .from_("admin_users")
        .select("password_hash")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    if not rows:
        return jsonify({"ok": False, "message": "User not found."})

    stored_hash = rows[0]["password_hash"]
    try:
        if not bcrypt.checkpw(current_password.encode(), stored_hash.encode()):
            return jsonify({"ok": False, "message": "Current password is incorrect."})
    except Exception:
        return jsonify({"ok": False, "message": "Could not verify current password."})

    if bcrypt.checkpw(new_password.encode(), stored_hash.encode()):
        return jsonify({"ok": False, "message": "New password must be different from the current password."})

    password_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt(rounds=12)).decode()
    supabase().from_("admin_users").update({"password_hash": password_hash}).eq("id", user_id).execute()
    return jsonify({"ok": True, "message": "Password changed successfully."})


# ── Pending changes routes ─────────────────────────────────────────────────────

@app.get("/admin/pending")
@role_required("board")
def admin_pending():
    """Two tabs: the live queue (?status=pending, superuser-only — it has
    approve/reject actions) and History (?status=history, board-and-up —
    decided changes with reviewer, timestamp, and rejection notes)."""
    view = request.args.get("status", "pending").strip()
    if view != "history" and not _is_superuser_request():
        return redirect(url_for("admin_pending", status="history"))

    action_filter = request.args.get("action_filter", "").strip()
    who_filter = request.args.get("who", "").strip()
    page = max(1, to_int_or_none(request.args.get("page")) or 1)

    query = supabase().from_("pending_changes").select("*", count="exact")
    if view == "history":
        query = query.neq("status", "pending")
    else:
        query = query.eq("status", "pending")
    if action_filter:
        query = query.eq("action", action_filter)
    if who_filter:
        query = query.eq("submitted_by", who_filter)

    start = (page - 1) * PENDING_PAGE_SIZE
    end = start + PENDING_PAGE_SIZE - 1
    order_col = "reviewed_at" if view == "history" else "submitted_at"
    result = query.order(order_col, desc=(view == "history")).range(start, end).execute()

    entries = result.data or []
    total_count = result.count or 0
    total_pages = max(1, math.ceil(total_count / PENDING_PAGE_SIZE))

    # For each entry fetch the current live clause so we can show context
    for entry in entries:
        clause_uuid = entry.get("clause_id")
        entry["current_clause"] = fetch_clause(clause_uuid) if clause_uuid else None
        entry["field_diffs"] = build_field_diff(entry)

    pending_filters = {"action_filter": action_filter, "who": who_filter}
    query_args = {k: v for k, v in pending_filters.items() if v}
    if view == "history":
        query_args["status"] = "history"

    prev_url = None
    next_url = None
    if page > 1:
        prev_url = url_for("admin_pending") + "?" + urlencode({**query_args, "page": page - 1})
    if page < total_pages:
        next_url = url_for("admin_pending") + "?" + urlencode({**query_args, "page": page + 1})

    return render_template(
        "admin_pending.html",
        entries=entries,
        total_count=total_count,
        total_pages=total_pages,
        page=page,
        prev_url=prev_url,
        next_url=next_url,
        filters=pending_filters,
        view=view,
        current_username=session.get("username"),
    )


@app.post("/admin/pending/<change_id>/approve")
@superuser_required
def approve_pending(change_id: str):
    result = (
        supabase()
        .from_("pending_changes")
        .select("*")
        .eq("id", change_id)
        .eq("status", "pending")
        .limit(1)
        .execute()
    )
    rows = result.data or []
    if not rows:
        flash("Pending change not found or already processed.", "error")
        return redirect(url_for("admin_pending"))

    change = rows[0]
    reviewer = session.get("username")
    is_self = change["submitted_by"] == reviewer

    if is_self and change["action"] == "delete":
        flash("Deletions cannot be self-approved — a different superuser must approve.", "error")
        return redirect(url_for("admin_pending"))

    _apply_pending_change(change)

    supabase().from_("pending_changes").update({
        "status": "approved",
        "reviewed_by": reviewer,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", change_id).execute()

    clause_id_text = (change.get("proposed_changes") or {}).get("clause_id") or None
    notes = "self-approved" if is_self else None
    log_audit_event(
        action="change_approved",
        clause_id=clause_id_text,
        record_id=change.get("clause_id"),
        notes=notes,
    )
    flash("Change approved and applied." + (" (self-approved)" if is_self else ""), "success")
    return redirect(url_for("admin_pending"))


@app.post("/admin/pending/<change_id>/reject")
@superuser_required
def reject_pending(change_id: str):
    review_notes = request.form.get("review_notes", "").strip()
    if not review_notes:
        flash("A rejection reason is required.", "error")
        return redirect(url_for("admin_pending"))

    result = (
        supabase()
        .from_("pending_changes")
        .select("*")
        .eq("id", change_id)
        .eq("status", "pending")
        .limit(1)
        .execute()
    )
    rows = result.data or []
    if not rows:
        flash("Pending change not found or already processed.", "error")
        return redirect(url_for("admin_pending"))

    change = rows[0]

    if change["action"] == "create":
        supabase().from_("clauses").delete().eq("id", change["clause_id"]).execute()

    supabase().from_("pending_changes").update({
        "status": "rejected",
        "reviewed_by": session.get("username"),
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "review_notes": review_notes,
    }).eq("id", change_id).execute()

    clause_id_text = (change.get("proposed_changes") or {}).get("clause_id") or None
    log_audit_event(
        action="change_rejected",
        clause_id=clause_id_text,
        record_id=change.get("clause_id"),
        notes=review_notes,
    )
    flash("Change rejected.", "success")
    return redirect(url_for("admin_pending"))


# ── Clause CSV export (#3) ────────────────────────────────────────────────────

EXPORT_HEADERS = ["id", "status"] + TEMPLATE_HEADERS


@app.get("/admin/export")
@login_required
def export_clauses():
    """CSV of the currently filtered browse view (same q/tag/document/stale
    params), unpaginated. Column order = import template + id/status so
    export → edit → re-import round-trips. The embedding column is excluded
    (1536 floats of noise)."""
    keyword = request.args.get("q", "").strip()
    tag = request.args.get("tag", "").strip()
    document = request.args.get("document", "").strip()
    stale = request.args.get("stale", "").strip() == "true"

    rows, page_size, offset = [], 1000, 0
    while True:
        query = supabase().from_("clauses").select(CLAUSE_COLUMNS)
        if keyword:
            query = query.or_(build_keyword_filter(keyword))
        if tag:
            query = query.contains("tags", [tag])
        if document:
            query = query.eq("document", document)
        if stale:
            query = query.is_("embedding", "null")
        batch = (
            query.order("created_at", desc=True)
            .range(offset, offset + page_size - 1).execute()
        ).data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=EXPORT_HEADERS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({**{k: row.get(k) for k in EXPORT_HEADERS if k != "tags"},
                         "tags": serialize_tags(row.get("tags"))})

    log_user_activity(session.get("username"), "csv_export")
    filename = f"clauses_export{'_' + tag if tag else ''}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Tag Management (#7) ───────────────────────────────────────────────────────

def _fetch_all_clause_tags() -> list[dict]:
    """id, clause_id, tags for every clause row (all statuses), paginated."""
    rows, page_size, offset = [], 1000, 0
    while True:
        batch = (
            supabase().from_("clauses").select("id,clause_id,tags")
            .range(offset, offset + page_size - 1).execute()
        ).data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


@app.get("/admin/tags")
@superuser_required
def admin_tags():
    from collections import Counter
    counts = Counter()
    for row in _fetch_all_clause_tags():
        for t in row.get("tags") or []:
            t = (t or "").strip()
            if t:
                counts[t] += 1
    tags = sorted(counts.items(), key=lambda kv: kv[0].lower())
    mixed_case = [t for t, _ in tags if t != t.upper()]
    return render_template("admin_tags.html", tags=tags, mixed_case_count=len(mixed_case))


def _retag_clauses(old_tag: str, new_tag: str | None) -> int:
    """Replace old_tag with new_tag (or remove it when new_tag is None)
    across every clause, deduping while preserving order. Audit-logs each
    touched clause. Returns the number of clauses updated."""
    touched = 0
    for row in _fetch_all_clause_tags():
        tags = [t for t in (row.get("tags") or [])]
        if old_tag not in tags:
            continue
        new_tags = []
        for t in tags:
            replacement = new_tag if t == old_tag else t
            if replacement and replacement not in new_tags:
                new_tags.append(replacement)
        supabase().from_("clauses").update({"tags": new_tags}).eq("id", row["id"]).execute()
        log_audit_event(
            action="updated",
            clause_id=row.get("clause_id"),
            record_id=row["id"],
            field_changed="tags",
            old_value=serialize_tags(tags),
            new_value=serialize_tags(new_tags),
            notes="tag management",
        )
        touched += 1
    return touched


@app.post("/admin/tags/rename")
@superuser_required
def rename_tag():
    old_tag = (request.form.get("old_tag") or "").strip()
    # UPPERCASE is the corpus convention (owner decision, July 2026)
    new_tag = (request.form.get("new_tag") or "").strip().upper()
    if not old_tag or not new_tag:
        flash("Both the existing tag and the new name are required.", "error")
        return redirect(url_for("admin_tags"))
    if old_tag == new_tag:
        flash("New name is identical — nothing to do.", "success")
        return redirect(url_for("admin_tags"))
    touched = _retag_clauses(old_tag, new_tag)
    log_audit_event(action="tag_renamed", new_value=f"{old_tag} → {new_tag} ({touched} clauses)")
    flash(
        f"Renamed '{old_tag}' → '{new_tag}' across {touched} clause{'s' if touched != 1 else ''}. "
        "Applied immediately (no two-person approval); reaches residents within the cache TTL (~1h).",
        "success",
    )
    return redirect(url_for("admin_tags"))


@app.post("/admin/tags/delete")
@superuser_required
def delete_tag():
    old_tag = (request.form.get("old_tag") or "").strip()
    if not old_tag:
        flash("No tag given.", "error")
        return redirect(url_for("admin_tags"))
    touched = _retag_clauses(old_tag, None)
    log_audit_event(action="tag_deleted", new_value=f"{old_tag} ({touched} clauses)")
    flash(
        f"Removed '{old_tag}' from {touched} clause{'s' if touched != 1 else ''}. "
        "Applied immediately; reaches residents within the cache TTL (~1h).",
        "success",
    )
    return redirect(url_for("admin_tags"))


# ── My Submissions (#6) ───────────────────────────────────────────────────────

@app.get("/admin/my-submissions")
@login_required
def my_submissions():
    """Every user can see their own pending/approved/rejected changes with
    reviewer notes. Deliberately NOT the /admin/pending route — that page
    carries approve/reject actions that stay superuser-only."""
    page = max(1, to_int_or_none(request.args.get("page")) or 1)

    query = (
        supabase().from_("pending_changes").select("*", count="exact")
        .eq("submitted_by", session.get("username"))
    )
    start = (page - 1) * PENDING_PAGE_SIZE
    end = start + PENDING_PAGE_SIZE - 1
    result = query.order("submitted_at", desc=True).range(start, end).execute()

    entries = result.data or []
    total_count = result.count or 0
    total_pages = max(1, math.ceil(total_count / PENDING_PAGE_SIZE))
    for entry in entries:
        entry["field_diffs"] = build_field_diff(entry)

    prev_url = url_for("my_submissions", page=page - 1) if page > 1 else None
    next_url = url_for("my_submissions", page=page + 1) if page < total_pages else None

    return render_template(
        "admin_my_submissions.html",
        entries=entries,
        total_count=total_count,
        total_pages=total_pages,
        page=page,
        prev_url=prev_url,
        next_url=next_url,
    )


# ── Clause permalink (#18) ────────────────────────────────────────────────────

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


@app.get("/admin/clauses/<key>")
@login_required
def clause_detail(key: str):
    """Canonical page per clause. Accepts either the row uuid or the human
    clause_id (e.g. DECL_27_08); uuid URLs redirect to the clause_id form."""
    clause = None
    if _UUID_RE.fullmatch(key):
        clause = fetch_clause(key)
        if clause and clause.get("clause_id") and clause["clause_id"] != key:
            return redirect(url_for("clause_detail", key=clause["clause_id"]))
    if clause is None:
        rows = (
            supabase().from_("clauses").select(CLAUSE_COLUMNS)
            .eq("clause_id", key).limit(1).execute()
        ).data or []
        clause = rows[0] if rows else None
    if clause is None:
        flash(f"Clause '{key}' was not found.", "error")
        return redirect(url_for("admin_home"))

    clause["tags_csv"] = serialize_tags(clause.get("tags"))
    clause["embedding_stale"] = clause_has_stale_embedding(clause)
    uuid_id = str(clause["id"])
    text_id = clause.get("clause_id")

    history = []
    try:
        history = (
            supabase().from_("clause_audit_log").select("*")
            .eq("record_id", uuid_id)
            .order("changed_at", desc=True).limit(50).execute()
        ).data or []
    except Exception as e:
        print(f"[clause-detail] history fetch failed: {e}")

    flags = []
    try:
        direct = (
            supabase().from_("clause_flags").select("*")
            .eq("clause_id", uuid_id).execute()
        ).data or []
        topical = []
        if text_id:
            topical = (
                supabase().from_("clause_flags").select("*")
                .contains("cited_clause_ids", [text_id]).execute()
            ).data or []
        seen = set()
        for f in direct + topical:
            if f["id"] not in seen:
                seen.add(f["id"])
                flags.append(f)
        flags.sort(key=lambda f: f.get("created_at") or "", reverse=True)
    except Exception as e:
        print(f"[clause-detail] flags fetch failed: {e}")

    pending = []
    try:
        pending = (
            supabase().from_("pending_changes").select("*")
            .eq("clause_id", uuid_id)
            .order("submitted_at", desc=True).limit(20).execute()
        ).data or []
        for p in pending:
            p["field_diffs"] = build_field_diff(p)
    except Exception as e:
        print(f"[clause-detail] pending fetch failed: {e}")

    return render_template(
        "admin_clause_detail.html",
        clause=clause,
        history=history,
        flags=flags,
        pending=pending,
    )


# ── Resident Questions (#11) ─────────────────────────────────────────────────

@app.get("/admin/questions")
@role_required("board")
def admin_questions():
    q = request.args.get("q", "").strip()
    fallback_only = request.args.get("fallback_only") == "1"
    no_citations = request.args.get("no_citations") == "1"
    include_whimsy = request.args.get("include_whimsy") == "1"
    page = max(1, to_int_or_none(request.args.get("page")) or 1)

    query = supabase().from_("resident_questions").select("*", count="exact")
    if q:
        # strip PostgREST filter metacharacters before ilike
        sanitized = re.sub(r"[%_,()]", " ", q).strip()
        if sanitized:
            query = query.ilike("question", f"%{sanitized}%")
    if fallback_only:
        query = query.eq("prefilter_used", False)
    if no_citations:
        query = query.or_("cited_clause_ids.is.null,cited_clause_ids.eq.{}")
    if not include_whimsy:
        query = query.eq("whimsy", False)

    start = (page - 1) * QUESTIONS_PAGE_SIZE
    end = start + QUESTIONS_PAGE_SIZE - 1
    result = query.order("created_at", desc=True).range(start, end).execute()
    rows = result.data or []
    total_count = result.count or 0
    total_pages = max(1, math.ceil(total_count / QUESTIONS_PAGE_SIZE))

    filters = {
        "q": q,
        "fallback_only": "1" if fallback_only else "",
        "no_citations": "1" if no_citations else "",
        "include_whimsy": "1" if include_whimsy else "",
    }
    query_args = {k: v for k, v in filters.items() if v}
    prev_url = None
    next_url = None
    if page > 1:
        prev_url = url_for("admin_questions") + "?" + urlencode({**query_args, "page": page - 1})
    if page < total_pages:
        next_url = url_for("admin_questions") + "?" + urlencode({**query_args, "page": page + 1})

    return render_template(
        "admin_questions.html",
        rows=rows,
        total_count=total_count,
        page=page,
        total_pages=total_pages,
        prev_url=prev_url,
        next_url=next_url,
        filters=filters,
    )


# ── CCR Revision Flags ────────────────────────────────────────────────────────

def _get_open_flag_clause_ids() -> set:
    """Return set of clause_ids that have an open or in_review flag."""
    try:
        result = (
            supabase()
            .from_("clause_flags")
            .select("clause_id")
            .in_("status", ["open", "in_review"])
            .eq("flag_type", "clause")
            .execute()
        )
        return {row["clause_id"] for row in (result.data or []) if row.get("clause_id")}
    except Exception:
        return set()


@app.get("/admin/flags")
@login_required
def admin_flags():
    status_filter = request.args.get("status", "open").strip()
    flag_type_filter = request.args.get("flag_type", "").strip()
    page = max(1, to_int_or_none(request.args.get("page")) or 1)

    query = supabase().from_("clause_flags").select("*", count="exact")
    if status_filter and status_filter != "all":
        query = query.eq("status", status_filter)
    if flag_type_filter:
        query = query.eq("flag_type", flag_type_filter)

    start = (page - 1) * FLAG_PAGE_SIZE
    end = start + FLAG_PAGE_SIZE - 1
    result = query.order("created_at", desc=True).range(start, end).execute()
    flags = result.data or []
    total_count = result.count or 0
    total_pages = max(1, math.ceil(total_count / FLAG_PAGE_SIZE))

    # Get comment counts for each flag
    flag_ids = [f["id"] for f in flags]
    comment_counts = {}
    if flag_ids:
        try:
            cr = (
                supabase()
                .from_("clause_flag_comments")
                .select("flag_id")
                .in_("flag_id", flag_ids)
                .execute()
            )
            for row in (cr.data or []):
                fid = row["flag_id"]
                comment_counts[fid] = comment_counts.get(fid, 0) + 1
        except Exception:
            pass

    for f in flags:
        f["comment_count"] = comment_counts.get(f["id"], 0)

    # Status counts for stat tiles
    status_counts = {}
    try:
        sc_result = (
            supabase()
            .from_("clause_flags")
            .select("status")
            .execute()
        )
        for row in (sc_result.data or []):
            s = row.get("status", "")
            status_counts[s] = status_counts.get(s, 0) + 1
    except Exception:
        pass

    query_args = {}
    if status_filter:
        query_args["status"] = status_filter
    if flag_type_filter:
        query_args["flag_type"] = flag_type_filter

    prev_url = None
    next_url = None
    if page > 1:
        prev_url = url_for("admin_flags") + "?" + urlencode({**query_args, "page": page - 1})
    if page < total_pages:
        next_url = url_for("admin_flags") + "?" + urlencode({**query_args, "page": page + 1})

    return render_template(
        "admin_flags.html",
        flags=flags,
        total_count=total_count,
        total_pages=total_pages,
        page=page,
        status_filter=status_filter,
        flag_type_filter=flag_type_filter,
        status_counts=status_counts,
        prev_url=prev_url,
        next_url=next_url,
    )


@app.get("/admin/flags/<flag_id>")
@login_required
def admin_flag_detail(flag_id: str):
    result = (
        supabase()
        .from_("clause_flags")
        .select("*")
        .eq("id", flag_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    if not rows:
        flash("Flag not found.", "error")
        return redirect(url_for("admin_flags"))
    flag = rows[0]

    # Load full clause if clause-type flag
    clause = None
    if flag.get("flag_type") == "clause" and flag.get("clause_id"):
        try:
            cr = (
                supabase()
                .from_("clauses")
                .select(CLAUSE_COLUMNS)
                .eq("clause_id", flag["clause_id"])
                .limit(1)
                .execute()
            )
            clause = (cr.data or [None])[0]
        except Exception:
            pass

    # Load comments
    comments_result = (
        supabase()
        .from_("clause_flag_comments")
        .select("*")
        .eq("flag_id", flag_id)
        .order("created_at", desc=False)
        .execute()
    )
    comments = comments_result.data or []

    return render_template(
        "admin_flag_detail.html",
        flag=flag,
        clause=clause,
        comments=comments,
    )


@app.post("/admin/flags")
@login_required
def create_flag():
    """Create a new clause-type or topic-type flag."""
    flag_type = request.form.get("flag_type", "clause").strip()
    flag_notes = request.form.get("flag_notes", "").strip()

    if not flag_notes:
        return jsonify({"ok": False, "message": "Notes are required when flagging."})

    payload = {
        "flag_type": flag_type,
        "flagged_by": session.get("username"),
        "flag_notes": flag_notes,
        "status": "open",
    }

    if flag_type == "clause":
        clause_id = request.form.get("clause_id", "").strip()
        if not clause_id:
            return jsonify({"ok": False, "message": "clause_id is required for clause flags."})
        payload["clause_id"] = clause_id

    elif flag_type == "topic":
        payload["question_text"] = request.form.get("question_text", "").strip()
        payload["answer_snapshot"] = request.form.get("answer_snapshot", "").strip()
        cited_raw = request.form.get("cited_clause_ids", "")
        if cited_raw:
            payload["cited_clause_ids"] = [c.strip() for c in cited_raw.split(",") if c.strip()]

    try:
        result = supabase().from_("clause_flags").insert(payload).execute()
        new_flag = (result.data or [{}])[0]
        log_audit_event(
            action="flag_created",
            clause_id=payload.get("clause_id"),
            notes=f"flag_type={flag_type}; {flag_notes[:100]}",
        )
        return jsonify({"ok": True, "message": "Flagged for revision.", "flag_id": new_flag.get("id")})
    except Exception as e:
        print(f"[flags] ERROR creating flag: {e}")
        return jsonify({"ok": False, "message": "Failed to create flag."})


@app.post("/admin/flags/<flag_id>/comment")
@login_required
def add_flag_comment(flag_id: str):
    comment = request.form.get("comment", "").strip()
    if not comment:
        flash("Comment cannot be empty.", "error")
        return redirect(url_for("admin_flag_detail", flag_id=flag_id))

    supabase().from_("clause_flag_comments").insert({
        "flag_id": flag_id,
        "author": session.get("username"),
        "comment": comment,
    }).execute()

    supabase().from_("clause_flags").update(
        {"updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", flag_id).execute()

    return redirect(url_for("admin_flag_detail", flag_id=flag_id) + "#comments")


@app.post("/admin/flags/<flag_id>/status")
@login_required
def update_flag_status(flag_id: str):
    """Update flag status. Only superusers can close a flag."""
    new_status = request.form.get("status", "").strip()
    resolution_notes = request.form.get("resolution_notes", "").strip()

    valid_statuses = ["open", "in_review", "closed_no_change", "closed_changed", "closed_deferred"]
    if new_status not in valid_statuses:
        flash("Invalid status.", "error")
        return redirect(url_for("admin_flag_detail", flag_id=flag_id))

    closing_statuses = {"closed_no_change", "closed_changed", "closed_deferred"}
    username = session.get("username")
    can_close = _role_rank(_current_role()) >= ROLE_RANK["board"]
    if new_status in closing_statuses and not can_close:
        flash("You do not have permission to close flags.", "error")
        return redirect(url_for("admin_flag_detail", flag_id=flag_id))

    update_payload = {
        "status": new_status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if new_status in closing_statuses:
        update_payload["closed_by"] = session.get("username")
        update_payload["resolution_notes"] = resolution_notes or None

    supabase().from_("clause_flags").update(update_payload).eq("id", flag_id).execute()

    log_audit_event(
        action="flag_status_updated",
        notes=f"flag_id={flag_id}; new_status={new_status}",
    )

    flash(f"Flag status updated to {new_status.replace('_', ' ')}.", "success")
    return redirect(url_for("admin_flag_detail", flag_id=flag_id))


@app.get("/admin/search")
@login_required
def admin_search():
    question = request.args.get("q", "").strip()
    result = None
    if question:
        from ask_gpt import answer_question
        raw = answer_question(question, output_format="json")
        clauses = raw.get("clauses", [])
        cited_clause_ids = [c.get("clause_id") for c in clauses if c.get("clause_id")]
        result = {
            "question": question,
            "answer": raw.get("answer", ""),
            "clauses": clauses,
            "cited_clause_ids": cited_clause_ids,
        }

    flagged_clause_ids = _get_open_flag_clause_ids()
    return render_template("admin_search.html", question=question, result=result,
                           flagged_clause_ids=flagged_clause_ids)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5051))
    app.run(debug=False, host="0.0.0.0", port=port, threaded=False)
