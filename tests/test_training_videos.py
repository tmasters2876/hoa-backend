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

MEDIA = "https://media.example/sign"


def _v(number, title, audience, pages, minutes=1, url=True, drive=None):
    return {"number": number, "title": title, "audience": audience, "minutes": minutes, "pages": pages,
            "url": f"/admin/training/{number}" if url else None, "drive_url": drive}


def _sign(number):
    return f"{MEDIA}/{number}.mp4?token=t", f"{MEDIA}/{number}.vtt?token=t"


VIDEOS = [
    _v("03", "The Revision Flags page", "member", ["admin_flags"], drive="https://drive.google.com/file/d/abc/view"),
    _v("13", "The Pending queue", "board", ["admin_pending"]),
    _v("14", "Administration for superusers", "superuser", ["admin_audit"], minutes=2),
    _v("06", "Not uploaded yet", "member", ["admin_users"], url=False),
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
    assert "Watch the video: The Revision Flags page" in html and 'href="/admin/training/03"' in html
    assert "drive.google.com" not in html  # the strip plays in the console, Drive is only an archive link on the player page
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


def test_player_page_streams_signed_urls_with_a_caption_track(client):
    sb = _chainable()
    with patch("admin_app.get_supabase_client", return_value=sb), patch("admin_app.load_training_videos", return_value=VIDEOS), \
         patch("admin_app._training_media_urls", side_effect=_sign) as sign:
        s, l = session_as("member")
        with s, l:
            html = client.get("/admin/training/03").get_data(as_text=True)
    sign.assert_called_once_with("03")
    assert f'src="{MEDIA}/03.mp4?token=t"' in html and f'<track id="train-cc" kind="captions" src="{MEDIA}/03.vtt?token=t"' in html
    assert 'id="cc-toggle"' in html and "Also in Google Drive" in html and "drive.google.com/file/d/abc/view" in html
    assert "The Pending queue" not in html  # the side list is role-filtered too


def test_player_page_is_404_for_a_video_the_role_may_not_see(client):
    sb = _chainable()
    with patch("admin_app.get_supabase_client", return_value=sb), patch("admin_app.load_training_videos", return_value=VIDEOS), \
         patch("admin_app._training_media_urls", side_effect=_sign) as sign:
        s, l = session_as("member")
        with s, l:
            assert client.get("/admin/training/13").status_code == 404
            assert client.get("/admin/training/99").status_code == 404
        assert sign.call_count == 0  # no link is ever minted for a video the role may not see
        s, l = session_as("board")
        with s, l:
            r = client.get("/admin/training/13")
    assert r.status_code == 200 and f"{MEDIA}/13.mp4?token=t" in r.get_data(as_text=True)


def test_player_page_degrades_when_the_bucket_is_unreachable(client):
    sb = _chainable()
    sb.storage.from_.return_value.create_signed_url.side_effect = RuntimeError("no bucket")
    with patch("admin_app.get_supabase_client", return_value=sb), patch("admin_app.load_training_videos", return_value=VIDEOS):
        s, l = session_as("member")
        with s, l:
            r = client.get("/admin/training/03")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "not available right now" in html and 'src="' not in html.split('id="train-video"')[1][:200]


def test_signed_urls_come_from_the_training_bucket():
    sb = MagicMock()
    sb.storage.from_.return_value.create_signed_url.side_effect = lambda path, secs: {"signedURL": f"https://x/{path}?s={secs}"}
    with patch("admin_app.get_supabase_client", return_value=sb):
        assert admin_app._training_media_urls("06") == ("https://x/06.mp4?s=3600", "https://x/06.vtt?s=3600")
    sb.storage.from_.assert_called_with("training")


def test_real_registry_links_every_video_to_its_player_page():
    for v in admin_app.load_training_videos():
        assert v["url"] == f"/admin/training/{v['number']}"
