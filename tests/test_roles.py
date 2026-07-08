"""
Tests for the Roles Rebuild (Priority Phase item 2): three-tier role model
(superuser > board > member), per-request is_active/role enforcement, and
the set-role route guards.
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
from admin_app import app, ROLE_RANK, _role_rank


# ── Helpers ───────────────────────────────────────────────────────────────────

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


def make_user_row(role="member", *, is_active=True, user_id="user-123", username="testuser"):
    return {
        "id": user_id,
        "username": username,
        "is_active": is_active,
        "role": role,
        "must_change_password": False,
    }


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


def login_session(role="member", username="testuser", user_id="user-123"):
    """Patch session as a logged-in user of the given role."""
    # Fresh timestamp — login_required's 8h expiry check runs on every
    # gated route, including superuser routes (via role_required).
    session_data = {
        "logged_in": True,
        "username": username,
        "user_id": user_id,
        "role": role,
        "logged_in_at": datetime.now(timezone.utc).isoformat(),
    }
    m = MagicMock()
    m.get.side_effect = lambda k, default=None: session_data.get(k, default)
    return patch("admin_app.session", m)


# ── Rank helper ───────────────────────────────────────────────────────────────

def test_role_rank_ordering():
    assert _role_rank("member") < _role_rank("board") < _role_rank("superuser")


def test_role_rank_unknown_and_missing_default_to_member():
    assert _role_rank(None) == ROLE_RANK["member"]
    assert _role_rank("") == ROLE_RANK["member"]
    assert _role_rank("weird") == ROLE_RANK["member"]
    assert _role_rank("  SUPERUSER ") == ROLE_RANK["superuser"]  # normalized


# ── Decorator matrix: superuser_required = role_required("superuser") ─────────
# POST /admin/users (create_user) is superuser-gated; use it as the probe.

@pytest.mark.parametrize("role,expected_allowed", [
    ("member", False),
    ("board", False),
    ("superuser", True),
])
def test_superuser_route_matrix(client, mock_supabase, role, expected_allowed):
    row = make_user_row(role)
    with login_session(role), \
         patch("admin_app._load_current_user", return_value=row), \
         patch("admin_app.log_audit_event"), \
         patch("admin_app.flash") as mock_flash:
        resp = client.post("/admin/users", data={"username": "nu", "password": "pw12345678"})

    assert resp.status_code == 302
    if expected_allowed:
        assert mock_supabase.insert.called  # reached the handler
    else:
        assert not mock_supabase.insert.called
        assert "/admin" in resp.headers["Location"]
        assert any("permission" in str(c.args[0]).lower()
                   for c in mock_flash.call_args_list)


def test_login_required_route_allows_member(client, mock_supabase):
    # /admin/flags is login_required only — members can view it.
    row = make_user_row("member")
    with login_session("member"), \
         patch("admin_app._load_current_user", return_value=row), \
         patch("admin_app.render_template", return_value="ok") as mock_render:
        resp = client.get("/admin/flags")
    assert resp.status_code == 200
    assert mock_render.called


def test_not_logged_in_redirects_to_login(client, mock_supabase):
    resp = client.get("/admin/flags")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


# ── Per-request enforcement (the #10 security fix) ────────────────────────────

def test_deactivated_user_bounced_mid_session(client, mock_supabase):
    row = make_user_row("member", is_active=False)
    with login_session("member"), \
         patch("admin_app._load_current_user", return_value=row), \
         patch("admin_app.log_user_activity") as mock_activity, \
         patch("admin_app.flash"):
        resp = client.get("/admin/flags")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    mock_activity.assert_called_once_with("testuser", "session_revoked")


def test_deleted_user_bounced_mid_session(client, mock_supabase):
    with login_session("member"), \
         patch("admin_app._load_current_user", return_value=None), \
         patch("admin_app.log_user_activity"), \
         patch("admin_app.flash"):
        resp = client.get("/admin/flags")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_demotion_applies_on_next_request(client, mock_supabase):
    # Session says superuser, but the DB row now says member: the fresh DB
    # role must win and the superuser-gated route must reject.
    row = make_user_row("member")
    with login_session("superuser"), \
         patch("admin_app._load_current_user", return_value=row), \
         patch("admin_app.flash"):
        resp = client.post("/admin/users", data={"username": "nu", "password": "pw12345678"})
    assert resp.status_code == 302
    assert not mock_supabase.insert.called


def test_db_error_falls_back_to_session_snapshot(client, mock_supabase):
    # Transient Supabase failure must not lock admins out mid-session.
    with login_session("superuser"), \
         patch("admin_app._load_current_user", side_effect=Exception("db down")), \
         patch("admin_app.log_audit_event"), \
         patch("admin_app.flash"):
        resp = client.post("/admin/users", data={"username": "nu", "password": "pw12345678"})
    assert resp.status_code == 302
    assert mock_supabase.insert.called  # session snapshot (superuser) honored


# ── set_role guards ───────────────────────────────────────────────────────────

def _super_session():
    return login_session("superuser", username="tmasters", user_id="super-1")


def _super_row():
    return make_user_row("superuser", user_id="super-1", username="tmasters")


def test_set_role_rejects_invalid_role(client, mock_supabase):
    with _super_session(), \
         patch("admin_app._load_current_user", return_value=_super_row()), \
         patch("admin_app.flash") as mock_flash:
        client.post("/admin/users/other-1/set-role", data={"role": "godmode"})
    assert not mock_supabase.update.called
    mock_flash.assert_any_call("Invalid role.", "error")


def test_set_role_blocks_own_role_change(client, mock_supabase):
    with _super_session(), \
         patch("admin_app._load_current_user", return_value=_super_row()), \
         patch("admin_app.flash") as mock_flash:
        client.post("/admin/users/super-1/set-role", data={"role": "member"})
    assert not mock_supabase.update.called
    mock_flash.assert_any_call("You cannot change your own role.", "error")


def test_set_role_blocks_demoting_last_superuser(client, mock_supabase):
    # Target lookup returns a superuser; the remaining-superusers query
    # returns only that one row.
    mock_supabase.execute.side_effect = [
        MagicMock(data=[{"username": "cmasters", "role": "superuser"}]),
        MagicMock(data=[{"id": "other-1"}]),  # only one active superuser left
    ]
    with _super_session(), \
         patch("admin_app._load_current_user", return_value=_super_row()), \
         patch("admin_app.flash") as mock_flash:
        client.post("/admin/users/other-1/set-role", data={"role": "member"})
    assert not mock_supabase.update.called
    mock_flash.assert_any_call("Cannot demote the last superuser.", "error")


def test_set_role_success_updates_role_and_mirror(client, mock_supabase):
    mock_supabase.execute.side_effect = [
        MagicMock(data=[{"username": "bob", "role": "member"}]),  # target lookup
        MagicMock(data=[]),  # update
    ]
    with _super_session(), \
         patch("admin_app._load_current_user", return_value=_super_row()), \
         patch("admin_app.log_audit_event") as mock_audit, \
         patch("admin_app.flash"):
        client.post("/admin/users/other-1/set-role", data={"role": "board"})
    payload = mock_supabase.update.call_args[0][0]
    assert payload["role"] == "board"
    assert payload["is_approver"] is True  # legacy mirror
    mock_audit.assert_called_once()


def test_create_user_stores_role(client, mock_supabase):
    with _super_session(), \
         patch("admin_app._load_current_user", return_value=_super_row()), \
         patch("admin_app.log_audit_event"), \
         patch("admin_app.flash"):
        client.post("/admin/users", data={
            "username": "newbie", "password": "pw12345678", "role": "board"})
    payload = mock_supabase.insert.call_args[0][0]
    assert payload["role"] == "board"
    assert payload["is_approver"] is True
