"""
THE LAW — Final Approval clause enforcement (README.md).

Member-facing surfaces must describe the approval step ONLY as "final
approval": never who approves, how many, by what mechanism, and never
reviewer identities on decided items. These tests fail if that ambiguity
ever regresses.
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

# Terms that must never appear in anything a member sees. Case-insensitive.
FORBIDDEN_FOR_MEMBERS = [
    "superuser", "two-person", "two person", "second admin",
    "president", "board role",
]


def _leaks(html: str) -> list[str]:
    low = html.lower()
    return [t for t in FORBIDDEN_FOR_MEMBERS if t in low]


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


def member_session():
    session_data = {
        "logged_in": True,
        "username": "volunteer",
        "user_id": "user-123",
        "role": "member",
        "logged_in_at": datetime.now(timezone.utc).isoformat(),
    }
    m = MagicMock()
    m.get.side_effect = lambda k, default=None: session_data.get(k, default)
    row = {"id": "user-123", "username": "volunteer", "is_active": True,
           "role": "member", "must_change_password": False}
    return patch("admin_app.session", m), patch("admin_app._load_current_user", return_value=row)


DECIDED = {
    "id": "p1", "action": "edit", "submitted_by": "volunteer",
    "submitted_at": "2026-07-02T10:00:00", "status": "rejected",
    "reviewed_by": "tmasters", "reviewed_at": "2026-07-03T10:00:00",
    "review_notes": "wrong article", "clause_id": "u-1",
    "proposed_changes": {"citation": "A"}, "original_values": {"citation": "B"},
}


def test_member_workflow_md_obeys_final_approval_clause():
    text = open(os.path.join(os.path.dirname(admin_app.__file__), "MEMBER_WORKFLOW.md"),
                encoding="utf-8").read()
    assert not _leaks(text), f"MEMBER_WORKFLOW.md leaks: {_leaks(text)}"
    assert "final approval" in text.lower()


def test_guide_page_obeys_final_approval_clause(client, mock_supabase):
    sess, load = member_session()
    with sess, load:
        resp = client.get("/admin/guide")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert not _leaks(html), f"/admin/guide leaks: {_leaks(html)}"


def test_dashboard_help_panel_obeys_clause_for_members(client, mock_supabase):
    sess, load = member_session()
    with sess, load:
        resp = client.get("/admin")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert not _leaks(html), f"member dashboard leaks: {_leaks(html)}"
    assert "final approval" in html.lower()


def test_my_submissions_hides_reviewer_identity(client, mock_supabase):
    mock_supabase.execute.return_value = MagicMock(data=[dict(DECIDED)], count=1)
    sess, load = member_session()
    with sess, load:
        resp = client.get("/admin/my-submissions")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "tmasters" not in html            # who decided: hidden
    assert "wrong article" in html           # why: always shown
    assert not _leaks(html)
