"""
THE LAW (Sept 2026 amendment): committee members revise the governing
documents, never the clause database.

- Every database-changing route (create/edit/delete/re-embed a clause) and
  My Submissions is board-and-up (`db_change_required`).
- Every database-maintenance surface in the templates is hidden from
  members (`can_edit_db`): edit forms, Add Clause, Search Test, stale /
  pending badges, pending & decided changes, change history, My Submissions
  in the sidebar. Board users still see all of it.
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


def _chainable(rows=None):
    m = MagicMock()
    for method in (
        "from_", "select", "insert", "update", "delete",
        "eq", "neq", "is_", "or_", "contains", "ilike",
        "gte", "lte", "order", "range", "limit", "rpc", "in_",
    ):
        getattr(m, method).return_value = m
    m.execute.return_value = MagicMock(data=rows or [], count=len(rows or []))
    return m


CLAUSE = {
    "id": "11111111-1111-1111-1111-111111111111", "clause_id": "DECL_27_08",
    "document": "Declaration.pdf", "page": 28, "citation": "Page 27, Section R",
    "clause_text": "Swimming, boating, fishing may be permitted.", "plain_summary": "Pond use at Board discretion.",
    "link": "https://drive.google.com/file/d/abc/view", "embedding": None, "match_source": "x",
    "tags": ["POND"], "created_at": "2026-01-01T00:00:00", "precedence_level": 2, "status": "pending",
}


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def mock_supabase():
    sb = _chainable([dict(CLAUSE)])
    with patch("admin_app.get_supabase_client", return_value=sb):
        yield sb


def session_as(role, username="testuser"):
    data = {"logged_in": True, "username": username, "user_id": "user-123", "role": role,
            "logged_in_at": datetime.now(timezone.utc).isoformat()}
    m = MagicMock()
    m.get.side_effect = lambda k, default=None: data.get(k, default)
    row = {"id": "user-123", "username": username, "is_active": True, "role": role, "must_change_password": False}
    return patch("admin_app.session", m), patch("admin_app._load_current_user", return_value=row)


def get(client, role, path):
    s, l = session_as(role)
    with s, l:
        return client.get(path)


# ── Route gating ──────────────────────────────────────────────────────────────

DB_ROUTES = [
    ("POST", "/admin/clauses"),
    ("POST", f"/admin/clauses/{CLAUSE['id']}/update"),
    ("POST", f"/admin/clauses/{CLAUSE['id']}/update-json"),
    ("POST", f"/admin/clauses/{CLAUSE['id']}/delete"),
    ("POST", f"/admin/clauses/{CLAUSE['id']}/regenerate-embedding"),
    ("GET", "/admin/my-submissions"),
]


@pytest.mark.parametrize("method,path", DB_ROUTES)
def test_member_cannot_reach_database_routes(client, mock_supabase, method, path):
    s, l = session_as("member")
    with s, l, patch("admin_app.flash") as mock_flash:
        resp = client.open(path, method=method, data={})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/admin")
    assert any("permission" in str(c.args[0]).lower() for c in mock_flash.call_args_list)
    assert not mock_supabase.insert.called and not mock_supabase.update.called and not mock_supabase.delete.called


@pytest.mark.parametrize("role", ["board", "superuser"])
def test_board_and_up_can_reach_my_submissions(client, mock_supabase, role):
    s, l = session_as(role)
    with s, l, patch("admin_app.render_template", return_value="ok") as mock_render:
        resp = client.get("/admin/my-submissions")
    assert resp.status_code == 200
    assert mock_render.call_args[0][0] == "admin_my_submissions.html"


def test_board_update_reaches_handler(client, mock_supabase):
    s, l = session_as("board")
    with s, l, patch("admin_app.submit_pending_change", return_value={"id": "p1", "verification": {}}) as spc, \
         patch("admin_app.log_audit_event"):
        resp = client.post(f"/admin/clauses/{CLAUSE['id']}/update",
                           data={"citation": "c", "page": "1", "link": "https://drive.google.com/file/d/x"})
    assert resp.status_code == 302
    assert spc.called


# ── Clauses page ──────────────────────────────────────────────────────────────

DB_SURFACES = [
    "Submit for Approval", "Add Clause", "Regenerate Embedding", "Delete Clause",
    "Search Test", "Stale embeddings", "Pending approval", "Pending Approval",
    "embedding ok", "embedding stale", "My Submissions", "Source Verification",
    "Approval Workflow", "Import CSV",
]


def test_member_dashboard_hides_every_database_surface(client, mock_supabase):
    html = get(client, "member", "/admin").get_data(as_text=True)
    for s in DB_SURFACES:
        assert s not in html, f"member dashboard leaks {s!r}"
    # …but keeps the reading tools
    for keep in ("clause-details read-only", "Revision Flags", "Revision Workflow", "Export CSV", "Community Vote", "🏳 Flag"):
        assert keep in html, f"member dashboard missing {keep!r}"
    assert "Swimming, boating, fishing" in html   # verbatim text is readable inline


def test_board_dashboard_keeps_database_surfaces(client, mock_supabase):
    html = get(client, "board", "/admin").get_data(as_text=True)
    for s in ("Submit for Approval", "Add Clause", "Regenerate Embedding", "Delete Clause",
              "Search Test", "Stale embeddings", "Pending approval", "My Submissions", "Approval Workflow"):
        assert s in html, f"board dashboard lost {s!r}"
    assert 'clause-details read-only' not in html   # board gets the edit expander, not the read-only one


def test_member_search_test_is_not_run(client, mock_supabase):
    with patch("admin_app.run_search_test") as rst:
        get(client, "member", "/admin?test_query=sheds")
        assert not rst.called
    with patch("admin_app.run_search_test", return_value={"question": "sheds", "vector": [], "keyword": [], "prefilter": None, "prefilter_enabled": True}) as rst:
        get(client, "board", "/admin?test_query=sheds")
        assert rst.called


# ── Clause page ───────────────────────────────────────────────────────────────

def test_member_clause_page_is_read_only(client, mock_supabase):
    html = get(client, "member", "/admin/clauses/DECL_27_08").get_data(as_text=True)
    for s in ("Edit This Clause", "Pending &amp; Decided Changes", "Change History", "Submit for Approval",
              "not visible to residents", "stale embedding"):
        assert s not in html, f"member clause page leaks {s!r}"
    assert "Revision Flags" in html
    assert "Swimming, boating, fishing" in html


def test_board_clause_page_keeps_maintenance_panels(client, mock_supabase):
    html = get(client, "board", "/admin/clauses/DECL_27_08").get_data(as_text=True)
    for s in ("Edit This Clause", "Pending &amp; Decided Changes", "Change History"):
        assert s in html


def test_member_clause_page_skips_history_and_pending_queries(client, mock_supabase):
    get(client, "member", "/admin/clauses/DECL_27_08")
    tables = [c.args[0] for c in mock_supabase.from_.call_args_list]
    assert "clause_audit_log" not in tables
    # pending_changes is still counted for the sidebar badge, but never
    # queried for this clause's own history
    eqs = [c.args for c in mock_supabase.eq.call_args_list]
    orders = [c.args[0] for c in mock_supabase.order.call_args_list]
    assert ("record_id", CLAUSE["id"]) not in eqs      # audit history lookup
    assert "submitted_at" not in orders                # pending-changes lookup


# ── Guide ─────────────────────────────────────────────────────────────────────

def test_guide_no_longer_tells_members_to_edit_the_database(client, mock_supabase):
    md = open(os.path.join(os.path.dirname(admin_app.__file__), "MEMBER_WORKFLOW.md"), encoding="utf-8").read()
    low = md.lower()
    for stale in ("submit for approval", "edit the fields", "click **submit"):
        assert stale not in low, f"guide still says {stale!r}"
    assert "submit to the board" in low and "recall" in low
    assert "never edit a clause" in low
    html = get(client, "member", "/admin/guide").get_data(as_text=True)
    assert "Submit for Approval" not in html
