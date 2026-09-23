"""
DEV mode (dev/README.md): HOA_ENV=dev marks the local sandbox.

- Every page shows a DEV badge; production (HOA_ENV unset) shows none.
- Startup refuses a non-local SUPABASE_URL in DEV mode, so the sandbox can
  never be pointed at production by a stale .env.
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
from admin_app import app, assert_dev_supabase_is_local, is_dev_mode


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
        "logged_in": True, "username": "volunteer", "user_id": "user-123",
        "role": "member", "logged_in_at": datetime.now(timezone.utc).isoformat(),
    }
    m = MagicMock()
    m.get.side_effect = lambda k, default=None: session_data.get(k, default)
    row = {"id": "user-123", "username": "volunteer", "is_active": True,
           "role": "member", "must_change_password": False}
    return patch("admin_app.session", m), patch("admin_app._load_current_user", return_value=row)


# ── is_dev_mode ───────────────────────────────────────────────────────────────

def test_dev_mode_off_by_default(monkeypatch):
    monkeypatch.delenv("HOA_ENV", raising=False)
    assert is_dev_mode() is False


@pytest.mark.parametrize("value", ["dev", "DEV", " dev "])
def test_dev_mode_on(monkeypatch, value):
    monkeypatch.setenv("HOA_ENV", value)
    assert is_dev_mode() is True


@pytest.mark.parametrize("value", ["prod", "production", "development", ""])
def test_dev_mode_other_values_are_not_dev(monkeypatch, value):
    monkeypatch.setenv("HOA_ENV", value)
    assert is_dev_mode() is False


# ── localhost guard ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:54321", "http://localhost:54321", "http://0.0.0.0:54321",
])
def test_guard_accepts_local_urls_in_dev(monkeypatch, url):
    monkeypatch.setenv("HOA_ENV", "dev")
    assert_dev_supabase_is_local(url)  # no raise


@pytest.mark.parametrize("url", [
    "https://xzqmunxbmoiuinyampsl.supabase.co", "https://anything.supabase.co", "",
])
def test_guard_refuses_remote_urls_in_dev(monkeypatch, url):
    monkeypatch.setenv("HOA_ENV", "dev")
    with pytest.raises(RuntimeError, match="Refusing to start"):
        assert_dev_supabase_is_local(url)


def test_guard_reads_env_when_no_url_given(monkeypatch):
    monkeypatch.setenv("HOA_ENV", "dev")
    monkeypatch.setenv("SUPABASE_URL", "https://prod.supabase.co")
    with pytest.raises(RuntimeError):
        assert_dev_supabase_is_local()
    monkeypatch.setenv("SUPABASE_URL", "http://127.0.0.1:54321")
    assert_dev_supabase_is_local()


def test_guard_is_inert_outside_dev(monkeypatch):
    monkeypatch.delenv("HOA_ENV", raising=False)
    assert_dev_supabase_is_local("https://prod.supabase.co")  # production path untouched


# ── badge rendering ───────────────────────────────────────────────────────────

def test_badge_shown_in_dev(client, mock_supabase, monkeypatch):
    monkeypatch.setenv("HOA_ENV", "dev")
    sess, load = member_session()
    with sess, load:
        resp = client.get("/admin")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "DEV · local sandbox" in html
    assert "<title>[DEV] PLCA Admin Console</title>" in html


def test_badge_hidden_in_production(client, mock_supabase, monkeypatch):
    monkeypatch.delenv("HOA_ENV", raising=False)
    sess, load = member_session()
    with sess, load:
        resp = client.get("/admin")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "local sandbox" not in html
    assert "<title>PLCA Admin Console</title>" in html


def test_login_page_marks_sandbox_in_dev(client, mock_supabase, monkeypatch):
    monkeypatch.setenv("HOA_ENV", "dev")
    html = client.get("/login").get_data(as_text=True)
    assert "DEV · local sandbox" in html
    monkeypatch.delenv("HOA_ENV", raising=False)
    html = client.get("/login").get_data(as_text=True)
    assert "local sandbox" not in html
