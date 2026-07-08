"""
Tests for Priority Phase item 2b: the board-gated /admin/questions page.
"""
import os
from datetime import datetime, timezone

import pytest
from unittest.mock import MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

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


def login_session(role):
    session_data = {
        "logged_in": True,
        "username": "testuser",
        "user_id": "user-123",
        "role": role,
        "logged_in_at": datetime.now(timezone.utc).isoformat(),
    }
    m = MagicMock()
    m.get.side_effect = lambda k, default=None: session_data.get(k, default)
    row = {"id": "user-123", "username": "testuser", "is_active": True,
           "role": role, "must_change_password": False}
    return patch("admin_app.session", m), patch("admin_app._load_current_user", return_value=row)


@pytest.mark.parametrize("role,expected_allowed", [
    ("member", False),
    ("board", True),
    ("superuser", True),
])
def test_questions_page_gate_matrix(client, mock_supabase, role, expected_allowed):
    sess, load = login_session(role)
    with sess, load, \
         patch("admin_app.flash"), \
         patch("admin_app.render_template", return_value="ok") as mock_render:
        resp = client.get("/admin/questions")

    if expected_allowed:
        assert resp.status_code == 200
        assert mock_render.call_args[0][0] == "admin_questions.html"
    else:
        assert resp.status_code == 302
        assert not mock_render.called


def test_whimsy_hidden_by_default(client, mock_supabase):
    sess, load = login_session("board")
    with sess, load, patch("admin_app.render_template", return_value="ok"):
        client.get("/admin/questions")
    assert ("whimsy", False) in [c.args for c in mock_supabase.eq.call_args_list]


def test_include_whimsy_removes_filter(client, mock_supabase):
    sess, load = login_session("board")
    with sess, load, patch("admin_app.render_template", return_value="ok"):
        client.get("/admin/questions?include_whimsy=1")
    assert ("whimsy", False) not in [c.args for c in mock_supabase.eq.call_args_list]


def test_fallback_only_filter(client, mock_supabase):
    sess, load = login_session("board")
    with sess, load, patch("admin_app.render_template", return_value="ok"):
        client.get("/admin/questions?fallback_only=1")
    assert ("prefilter_used", False) in [c.args for c in mock_supabase.eq.call_args_list]


def test_search_strips_postgrest_metacharacters(client, mock_supabase):
    sess, load = login_session("board")
    with sess, load, patch("admin_app.render_template", return_value="ok"):
        client.get("/admin/questions?q=fence%25,(height)")
    pattern = mock_supabase.ilike.call_args[0][1]
    for ch in "%_,()":
        assert ch not in pattern.strip("%") or pattern.count("%") == 2  # only the wrapping wildcards
    assert pattern.startswith("%") and pattern.endswith("%")
