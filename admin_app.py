import csv
import functools
import io
import math
import os
from datetime import timedelta
from urllib.parse import urlencode

import bcrypt

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
    "embedding,match_source,tags,created_at,precedence_level"
)
PAGE_SIZE = 25

SUPERUSERS = {'tmasters', 'cmasters'}


# ── Auth ──────────────────────────────────────────────────────────────────────

def lookup_and_verify_user(username: str, password: str) -> dict | None:
    result = (
        supabase()
        .from_("admin_users")
        .select("id,username,password_hash,is_active")
        .eq("username", username)
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
        return redirect(url_for("admin_home"))
    flash("Invalid username or password.", "error")
    return render_template("admin_login.html"), 401


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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
    }
    supabase().from_("clauses").insert(payload).execute()
    flash("Clause added. Embedding is marked stale until you regenerate it.", "success")
    return redirect(url_for("admin_home"))


@app.post("/admin/clauses/<clause_id>/update")
@login_required
def update_clause(clause_id: str):
    existing = fetch_clause(clause_id)
    if not existing:
        flash(f"Clause {clause_id} was not found.", "error")
        return redirect(url_for("admin_home"))

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

    supabase().from_("clauses").update(payload).eq("id", clause_id).execute()

    if text_changed:
        flash("Clause updated. Text changed, so the embedding was marked stale.", "success")
    else:
        flash("Clause metadata updated.", "success")
    return redirect(url_for("admin_home"))


@app.post("/admin/clauses/<clause_id>/update-json")
@login_required
def update_clause_json(clause_id: str):
    existing = fetch_clause(clause_id)
    if not existing:
        return jsonify({"ok": False, "message": f"Clause {clause_id} not found."}), 404

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

    supabase().from_("clauses").update(payload).eq("id", clause_id).execute()

    embedding_stale = text_changed or not existing.get("embedding")
    message = "Saved. Text changed — embedding is now stale." if text_changed else "Metadata saved."
    return jsonify({"ok": True, "message": message, "embedding_stale": embedding_stale})


@app.post("/admin/clauses/<clause_id>/delete")
@login_required
def delete_clause(clause_id: str):
    supabase().from_("clauses").delete().eq("id", clause_id).execute()
    flash(f"Deleted clause {clause_id}.", "success")
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
            "link": (row.get("link") or "").strip() or None,
            "tags": parse_tags(row.get("tags") or ""),
            "precedence_level": precedence_level,
            "match_source": (row.get("match_source") or "").strip() or "CSV Import",
            "embedding": None,
        })

    for err in row_errors:
        flash(err, "error")

    if rows_ok:
        supabase().from_("clauses").insert(rows_ok).execute()
        n = len(rows_ok)
        flash(
            f"Imported {n} clause{'s' if n != 1 else ''}. "
            "Embeddings are stale — regenerate as needed.",
            "success",
        )
    elif not row_errors:
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
        flash(f"Deleted {deleted_count} {noun}: {ids_preview}.", "success")

    return redirect(url_for("admin_home"))


# ── User management routes ────────────────────────────────────────────────────

@app.get("/admin/users")
@login_required
def admin_users():
    result = (
        supabase()
        .from_("admin_users")
        .select("id,username,is_active,created_at")
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
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    if not username or not password:
        flash("Username and password are both required.", "error")
        return redirect(url_for("admin_users"))
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    try:
        supabase().from_("admin_users").insert({
            "username": username,
            "password_hash": password_hash,
        }).execute()
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

    password_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt(rounds=12)).decode()
    supabase().from_("admin_users").update({"password_hash": password_hash}).eq("id", user_id).execute()
    return jsonify({"ok": True, "message": "Password reset successfully."})


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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5051))
    app.run(debug=False, host="0.0.0.0", port=port)
