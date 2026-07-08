"""
Tests for the second improvement batch:
  #18 clause permalink   #14 pending history   #6 my submissions
  #7 tag management      #3 CSV export
"""
import os
from datetime import datetime, timezone

import pytest
from unittest.mock import MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import admin_app
from admin_app import app


def _chainable():
    m = MagicMock()
    for method in (
        "from_", "select", "insert", "update", "delete",
        "eq", "neq", "is_", "or_", "contains", "ilike",
        "gte", "lte", "order", "range", "limit", "rpc", "in_",
    ):
        getattr(m, method).return_value = m
    m.execute.return_value = MagicMock(data=[], count=0)
    return m


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def mock_supabase():
    sb = _chainable()
    with patch("admin_app.get_supabase_client", return_value=sb):
        yield sb


def login_session(role, username="testuser", user_id="user-123"):
    session_data = {
        "logged_in": True,
        "username": username,
        "user_id": user_id,
        "role": role,
        "logged_in_at": datetime.now(timezone.utc).isoformat(),
    }
    m = MagicMock()
    m.get.side_effect = lambda k, default=None: session_data.get(k, default)
    row = {"id": user_id, "username": username, "is_active": True,
           "role": role, "must_change_password": False}
    return patch("admin_app.session", m), patch("admin_app._load_current_user", return_value=row)


CLAUSE = {
    "id": "11111111-2222-3333-4444-555555555555",
    "clause_id": "DECL_27_08",
    "document": "CCR.pdf", "page": 5, "citation": "Art. VI",
    "clause_text": "text", "plain_summary": "summary", "link": "",
    "embedding": [0.1], "match_source": "x", "tags": ["FENCE"],
    "created_at": "2026-01-01", "precedence_level": 2, "status": "approved",
}


# ── #18 clause permalink ──────────────────────────────────────────────────────

def test_permalink_uuid_redirects_to_clause_id(client, mock_supabase):
    sess, load = login_session("member")
    with sess, load, patch("admin_app.fetch_clause", return_value=dict(CLAUSE)):
        resp = client.get(f"/admin/clauses/{CLAUSE['id']}")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/admin/clauses/DECL_27_08")


def test_permalink_renders_by_clause_id(client, mock_supabase):
    mock_supabase.execute.return_value = MagicMock(data=[dict(CLAUSE)], count=1)
    sess, load = login_session("member")
    with sess, load, patch("admin_app.render_template", return_value="ok") as mock_render:
        resp = client.get("/admin/clauses/DECL_27_08")
    assert resp.status_code == 200
    assert mock_render.call_args[0][0] == "admin_clause_detail.html"
    assert mock_render.call_args[1]["clause"]["clause_id"] == "DECL_27_08"


def test_permalink_unknown_key_redirects_home(client, mock_supabase):
    sess, load = login_session("member")
    with sess, load, patch("admin_app.flash") as mock_flash:
        resp = client.get("/admin/clauses/NOPE_99")
    assert resp.status_code == 302
    assert any("not found" in str(c.args[0]) for c in mock_flash.call_args_list)


# ── #14 pending history ───────────────────────────────────────────────────────

def test_board_user_redirected_from_live_queue_to_history(client, mock_supabase):
    sess, load = login_session("board")
    with sess, load:
        resp = client.get("/admin/pending")
    assert resp.status_code == 302
    assert "status=history" in resp.headers["Location"]


def test_board_user_sees_history(client, mock_supabase):
    sess, load = login_session("board")
    with sess, load, patch("admin_app.render_template", return_value="ok") as mock_render:
        resp = client.get("/admin/pending?status=history")
    assert resp.status_code == 200
    assert mock_render.call_args[1]["view"] == "history"
    assert ("status", "pending") in [c.args for c in mock_supabase.neq.call_args_list]


def test_member_blocked_from_pending_entirely(client, mock_supabase):
    sess, load = login_session("member")
    with sess, load, patch("admin_app.flash"):
        resp = client.get("/admin/pending?status=history")
    assert resp.status_code == 302
    assert "status=history" not in resp.headers["Location"]  # bounced to admin home


def test_superuser_live_queue_unchanged(client, mock_supabase):
    sess, load = login_session("superuser")
    with sess, load, patch("admin_app.render_template", return_value="ok") as mock_render:
        resp = client.get("/admin/pending")
    assert resp.status_code == 200
    assert mock_render.call_args[1]["view"] == "pending"
    assert ("status", "pending") in [c.args for c in mock_supabase.eq.call_args_list]


# ── #6 my submissions ─────────────────────────────────────────────────────────

def test_my_submissions_scoped_to_current_user(client, mock_supabase):
    sess, load = login_session("member", username="clee")
    with sess, load, patch("admin_app.render_template", return_value="ok") as mock_render:
        resp = client.get("/admin/my-submissions")
    assert resp.status_code == 200
    assert ("submitted_by", "clee") in [c.args for c in mock_supabase.eq.call_args_list]
    assert mock_render.call_args[0][0] == "admin_my_submissions.html"


# ── #7 tag management ─────────────────────────────────────────────────────────

def test_tags_page_superuser_only(client, mock_supabase):
    sess, load = login_session("board")
    with sess, load, patch("admin_app.flash"), \
         patch("admin_app.render_template", return_value="ok") as mock_render:
        resp = client.get("/admin/tags")
    assert resp.status_code == 302
    assert not mock_render.called


def test_rename_uppercases_and_replaces(client, mock_supabase):
    rows = [
        {"id": "c1", "clause_id": "A_01", "tags": ["Fence", "ARC"]},
        {"id": "c2", "clause_id": "A_02", "tags": ["POOL"]},
    ]
    sess, load = login_session("superuser")
    with sess, load, \
         patch("admin_app._fetch_all_clause_tags", return_value=rows), \
         patch("admin_app.log_audit_event"), \
         patch("admin_app.flash") as mock_flash:
        client.post("/admin/tags/rename", data={"old_tag": "Fence", "new_tag": "fence"})
    payload = mock_supabase.update.call_args[0][0]
    assert payload["tags"] == ["FENCE", "ARC"]          # uppercased, order kept
    assert mock_supabase.update.call_count == 1          # c2 untouched
    assert any("1 clause" in str(c.args[0]) for c in mock_flash.call_args_list)


def test_rename_into_existing_tag_merges_dedupes(client, mock_supabase):
    rows = [{"id": "c1", "clause_id": "A_01", "tags": ["Fence", "FENCE"]}]
    sess, load = login_session("superuser")
    with sess, load, \
         patch("admin_app._fetch_all_clause_tags", return_value=rows), \
         patch("admin_app.log_audit_event"), patch("admin_app.flash"):
        client.post("/admin/tags/rename", data={"old_tag": "Fence", "new_tag": "FENCE"})
    payload = mock_supabase.update.call_args[0][0]
    assert payload["tags"] == ["FENCE"]                  # merged, single instance


def test_delete_tag_removes_everywhere(client, mock_supabase):
    rows = [{"id": "c1", "clause_id": "A_01", "tags": ["JUNK", "FENCE"]}]
    sess, load = login_session("superuser")
    with sess, load, \
         patch("admin_app._fetch_all_clause_tags", return_value=rows), \
         patch("admin_app.log_audit_event"), patch("admin_app.flash"):
        client.post("/admin/tags/delete", data={"old_tag": "JUNK"})
    payload = mock_supabase.update.call_args[0][0]
    assert payload["tags"] == ["FENCE"]


# ── #3 CSV export ─────────────────────────────────────────────────────────────

def test_export_returns_csv_with_template_columns(client, mock_supabase):
    mock_supabase.execute.return_value = MagicMock(data=[dict(CLAUSE)], count=1)
    sess, load = login_session("member")
    with sess, load, patch("admin_app.log_user_activity"):
        resp = client.get("/admin/export")
    assert resp.status_code == 200
    assert resp.mimetype == "text/csv"
    body = resp.get_data(as_text=True).splitlines()
    header = body[0].split(",")
    assert header[:2] == ["id", "status"]
    assert header[2:] == admin_app.TEMPLATE_HEADERS
    assert "DECL_27_08" in body[1]
    assert "FENCE" in body[1]           # tags serialized, not a Python list
    assert "embedding" not in body[0]


def test_export_applies_tag_filter(client, mock_supabase):
    mock_supabase.execute.return_value = MagicMock(data=[], count=0)
    sess, load = login_session("member")
    with sess, load, patch("admin_app.log_user_activity"):
        client.get("/admin/export?tag=FENCE")
    assert ("tags", ["FENCE"]) in [c.args for c in mock_supabase.contains.call_args_list]
