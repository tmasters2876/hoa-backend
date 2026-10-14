"""Training video links (training/videos.yaml): per-page Watch strip, Help section,
Guide index. Board-audience videos never reach members; unlinked videos are silent."""
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import admin_app
from admin_app import app, training_videos_for

VIDEOS = [
    {"number": "03", "title": "The Revision Flags page", "audience": "member", "minutes": 1, "pages": ["admin_flags"], "url": "https://drive.google.com/file/d/abc/view"},
    {"number": "13", "title": "The Pending queue", "audience": "board", "minutes": 1, "pages": ["admin_pending"], "url": "https://drive.google.com/file/d/def/view"},
    {"number": "14", "title": "Administration for superusers", "audience": "superuser", "minutes": 2, "pages": ["admin_audit"], "url": "https://drive.google.com/file/d/ghi/view"},
    {"number": "06", "title": "Not uploaded yet", "audience": "member", "minutes": 1, "pages": ["admin_users"], "url": None},
]


def _chainable(rows=None):
    m = MagicMock()
    for method in ("from_", "select", "insert", "update", "delete", "eq", "neq", "is_", "or_", "contains", "ilike",
                   "gte", "lte", "order", "range", "limit", "rpc", "in_"):
        getattr(m, method).return_value = m
    m.execute.return_value = MagicMock(data=rows or [], count=len(rows or []))
    return m


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def session_as(role):
    data = {"logged_in": True, "username": "u", "user_id": "x", "role": role, "logged_in_at": datetime.now(timezone.utc).isoformat()}
    m = MagicMock(); m.get.side_effect = lambda k, d=None: data.get(k, d)
    return patch("admin_app.session", m), patch("admin_app._load_current_user", return_value={"id": "x", "username": "u", "is_active": True, "role": role, "must_change_password": False})


def test_role_gating_and_unlinked_videos():
    with patch("admin_app.load_training_videos", return_value=VIDEOS):
        m = training_videos_for("member", "admin_flags")
        assert [v["number"] for v in m["all"]] == ["03"] and [v["number"] for v in m["here"]] == ["03"]
        b = training_videos_for("board", "admin_pending")
        assert [v["number"] for v in b["all"]] == ["03", "13"] and b["here"][0]["number"] == "13"
        s = training_videos_for("superuser", "admin_audit")
        assert [v["number"] for v in s["all"]] == ["03", "13", "14"]


def test_watch_strip_on_the_flags_page_for_a_member(client):
    sb = _chainable()
    with patch("admin_app.get_supabase_client", return_value=sb), patch("admin_app.load_training_videos", return_value=VIDEOS):
        s, l = session_as("member")
        with s, l:
            html = client.get("/admin/flags").get_data(as_text=True)
    assert "Watch the video: The Revision Flags page" in html and "drive.google.com/file/d/abc/view" in html
    assert "The Pending queue" not in html and "Administration for superusers" not in html


def test_help_panel_lists_only_what_the_role_may_see(client):
    sb = _chainable()
    with patch("admin_app.get_supabase_client", return_value=sb), patch("admin_app.load_training_videos", return_value=VIDEOS):
        s, l = session_as("member")
        with s, l:
            html = client.get("/admin").get_data(as_text=True)
    assert "Training videos" in html and "03 · The Revision Flags page" in html
    assert "13 · The Pending queue" not in html and "Not uploaded yet" not in html


def test_real_registry_loads_and_every_page_exists():
    vids = admin_app.load_training_videos()
    assert len(vids) == 12
    endpoints = {r.endpoint for r in app.url_map.iter_rules()}
    for v in vids:
        for pg in v["pages"]:
            assert pg in endpoints, f"{v['number']}: unknown endpoint {pg}"
