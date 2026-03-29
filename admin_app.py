import csv
import functools
import io
import math
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import bcrypt
import pdfplumber
import requests as http_requests
from rapidfuzz import fuzz

from flask import Flask, Response, flash, jsonify, redirect, render_template, request, session, url_for
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

SUPERUSERS = {'tmasters', 'cmasters'}


# ── Auth ──────────────────────────────────────────────────────────────────────

def lookup_and_verify_user(username: str, password: str) -> dict | None:
    result = (
        supabase()
        .from_("admin_users")
        .select("id,username,password_hash,is_active,must_change_password")
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
        if session.get("must_change_password"):
            # Allow only the change-password page and logout
            allowed = {url_for("force_change_password"), url_for("force_change_password_post"), url_for("logout")}
            if request.path not in allowed:
                flash("Please set your password before continuing.", "error")
                return redirect(url_for("force_change_password"))
        return f(*args, **kwargs)
    return wrapper


def superuser_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        if session.get("username") not in SUPERUSERS:
            flash("You do not have permission to manage users.", "error")
            return redirect(url_for("admin_home"))
        return f(*args, **kwargs)
    return wrapper


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


def browse_clauses(keyword: str, tag: str, document: str, page: int) -> tuple[list[dict], int]:
    query = supabase().from_("clauses").select(CLAUSE_COLUMNS, count="exact")
    if keyword:
        query = query.or_(build_keyword_filter(keyword))
    if tag:
        query = query.contains("tags", [tag])
    if document:
        query = query.eq("document", document)

    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE - 1
    result = query.order("created_at", desc=True).range(start, end).execute()
    return result.data or [], result.count or 0


def run_search_test(question: str, limit: int = 8) -> dict:
    results = {"question": question, "vector": [], "keyword": []}
    if not question.strip():
        return results

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
    }


def index_redirect():
    return redirect(url_for("admin_home"))


@app.context_processor
def inject_superuser():
    pending_count = 0
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
    return {
        "is_superuser": session.get("username") in SUPERUSERS,
        "pending_count": pending_count,
    }


# ── Audit log ─────────────────────────────────────────────────────────────────

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
        payload = {k: v for k, v in proposed.items()}
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
    user = lookup_and_verify_user(username, password)
    if user:
        session.permanent = True
        session["logged_in"] = True
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        if user.get("must_change_password"):
            session["must_change_password"] = True
            flash("Welcome! Please set your own password before continuing.", "success")
            return redirect(url_for("force_change_password"))
        return redirect(url_for("admin_home"))
    flash("Invalid username or password.", "error")
    return render_template("admin_login.html"), 401


@app.post("/logout")
def logout():
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
    try:
        clauses, total_count = browse_clauses(
            keyword=filters["q"],
            tag=filters["tag"],
            document=filters["document"],
            page=filters["page"],
        )
    except Exception:
        flash("Search filter could not be applied — showing all clauses.", "error")
        clauses, total_count = browse_clauses(keyword="", tag=filters["tag"], document=filters["document"], page=1)
    documents, tags = get_filter_options()
    search_test = run_search_test(filters["test_query"]) if filters["test_query"] else None

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
    )


@app.post("/admin/clauses")
@login_required
def create_clause():
    errors = _validate_source_fields(request.form)
    if errors:
        for msg in errors:
            flash(msg, "error")
        return redirect(url_for("admin_home"))

    self_approve = bool(request.form.get("self_approve")) and session.get("username") in SUPERUSERS

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
    errors = _validate_source_fields(request.form)
    if errors:
        for msg in errors:
            flash(msg, "error")
        return redirect(url_for("admin_home"))

    existing = fetch_clause(clause_id)
    if not existing:
        flash(f"Clause {clause_id} was not found.", "error")
        return redirect(url_for("admin_home"))

    self_approve = bool(request.form.get("self_approve")) and session.get("username") in SUPERUSERS

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
    return redirect(url_for("admin_home"))


@app.post("/admin/clauses/<clause_id>/update-json")
@login_required
def update_clause_json(clause_id: str):
    errors = _validate_source_fields(request.form)
    if errors:
        return jsonify({"ok": False, "message": " ".join(errors)})

    existing = fetch_clause(clause_id)
    if not existing:
        return jsonify({"ok": False, "message": f"Clause {clause_id} not found."}), 404

    self_approve = bool(request.form.get("self_approve")) and session.get("username") in SUPERUSERS

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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
    result = (
        supabase()
        .from_("admin_users")
        .select("id,username,is_active,created_at,must_change_password")
        .order("created_at")
        .execute()
    )
    return render_template(
        "admin_users.html",
        users=result.data or [],
        current_user_id=session.get("user_id"),
        is_superuser=session.get("username") in SUPERUSERS,
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
        supabase().from_("admin_users").insert({
            "username": username,
            "password_hash": password_hash,
            "must_change_password": True,
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
@superuser_required
def admin_pending():
    action_filter = request.args.get("action_filter", "").strip()
    who_filter = request.args.get("who", "").strip()
    page = max(1, to_int_or_none(request.args.get("page")) or 1)

    query = supabase().from_("pending_changes").select("*", count="exact").eq("status", "pending")
    if action_filter:
        query = query.eq("action", action_filter)
    if who_filter:
        query = query.eq("submitted_by", who_filter)

    start = (page - 1) * PENDING_PAGE_SIZE
    end = start + PENDING_PAGE_SIZE - 1
    result = query.order("submitted_at", desc=False).range(start, end).execute()

    entries = result.data or []
    total_count = result.count or 0
    total_pages = max(1, math.ceil(total_count / PENDING_PAGE_SIZE))

    # For each entry fetch the current live clause so we can show context
    for entry in entries:
        clause_uuid = entry.get("clause_id")
        entry["current_clause"] = fetch_clause(clause_uuid) if clause_uuid else None

    pending_filters = {"action_filter": action_filter, "who": who_filter}
    query_args = {k: v for k, v in pending_filters.items() if v}

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5051))
    app.run(debug=False, host="0.0.0.0", port=port)
