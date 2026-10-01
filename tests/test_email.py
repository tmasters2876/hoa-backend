"""
Email (sql/005_email.sql, mailer.py):

- mailer backends: off (default) logs skipped; file writes to dev/mail; subject prefix.
- Forgot password by username OR email: same message either way, token stored
  hashed with a one-hour expiry, link emailed; unknown / inactive / no-email
  accounts get the same message and no email.
- Reset: bad or expired or used token refused; good token sets the hash, clears
  must_change_password, marks the token used.
- Notifications: pure routing per event; the actor gets a copy as a receipt; opt-out
  respected; each lifecycle route sends.
- Account email: any user sets their own; superuser sets others; members cannot.
"""
import hashlib
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import admin_app
import mailer
from admin_app import app, notification_recipients

FID = "11111111-1111-1111-1111-111111111111"
UID = "user-123"


def _chainable(rows=None):
    m = MagicMock()
    for method in ("from_", "select", "insert", "update", "delete", "eq", "neq", "is_", "not_", "or_",
                   "contains", "ilike", "gte", "lte", "order", "range", "limit", "rpc", "in_"):
        getattr(m, method).return_value = m
    m.execute.return_value = MagicMock(data=rows or [], count=len(rows or []))
    return m


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def sb():
    m = _chainable()
    with patch("admin_app.get_supabase_client", return_value=m):
        yield m


@pytest.fixture
def mail_on(monkeypatch):
    monkeypatch.setenv("MAIL_BACKEND", "file")
    monkeypatch.setenv("HOA_BASE_URL", "http://console.test")


@pytest.fixture
def sent():
    with patch("admin_app.mailer.send_email", return_value="sent") as m:
        yield m


def as_role(role, username="alice"):
    data = {"logged_in": True, "username": username, "user_id": UID, "role": role,
            "logged_in_at": datetime.now(timezone.utc).isoformat()}
    m = MagicMock()
    m.get.side_effect = lambda k, default=None: data.get(k, default)
    row = {"id": UID, "username": username, "is_active": True, "role": role, "must_change_password": False}
    return patch("admin_app.session", m), patch("admin_app._load_current_user", return_value=row)


def inserts(sb, table=None):
    out = []
    for c in sb.insert.call_args_list:
        out.append(c.args[0])
    return out


# ── mailer ───────────────────────────────────────────────────────────────────

def test_mailer_off_by_default_logs_skipped(monkeypatch):
    monkeypatch.delenv("MAIL_BACKEND", raising=False)
    with patch("mailer._log") as log:
        assert mailer.send_email("a@b.c", "Hi", "body", kind="x") == "skipped"
        assert log.call_args.args[4] == "skipped"
    assert not mailer.enabled()


def test_mailer_file_backend_writes_outbox(tmp_path, monkeypatch):
    monkeypatch.setenv("MAIL_BACKEND", "file")
    monkeypatch.setenv("HOA_ENV", "dev")
    monkeypatch.setattr(mailer, "FILE_OUTBOX", tmp_path)
    with patch("mailer._log"):
        assert mailer.send_email("a@b.c", "Reset your password", "line one\nline two", kind="password_reset", related_id="u1") == "sent"
    items = mailer.outbox()
    assert len(items) == 1
    assert items[0]["subject"] == "[DEV] [PLCA Console] Reset your password"
    assert items[0]["to"] == "a@b.c" and "line two" in items[0]["text"]
    assert mailer.outbox_message(items[0]["id"])["kind"] == "password_reset"
    assert mailer.outbox_message("../etc/passwd") is None
    assert mailer.clear_outbox() == 2 and mailer.outbox() == []


# ── forgot password ──────────────────────────────────────────────────────────

def _user(email="alice@example.com", active=True):
    return {"id": UID, "username": "alice", "email": email, "is_active": active}


def test_forgot_is_hidden_when_mail_is_off(client, sb, monkeypatch):
    monkeypatch.delenv("MAIL_BACKEND", raising=False)
    r = client.get("/forgot")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")


@pytest.mark.parametrize("identifier,col", [("alice", "username"), ("Alice@Example.com", "email")])
def test_forgot_by_username_or_email_sends_a_hashed_token_link(client, sb, mail_on, sent, identifier, col):
    with patch("admin_app._find_user_by_identifier", return_value=_user()) as find, patch("admin_app.flash") as fl, patch("admin_app.log_user_activity"):
        r = client.post("/forgot", data={"identifier": identifier})
    assert r.status_code == 302
    assert "reset link is on its way" in fl.call_args.args[0]
    row = inserts(sb)[-1]
    assert row["user_id"] == UID and len(row["token_hash"]) == 64 and row["expires_at"]
    to, subject, body = sent.call_args.args[:3]
    assert to == "alice@example.com" and "Reset your password" in subject
    link = [w for w in body.split() if w.startswith("http://console.test/reset/")][0]
    token = link.rsplit("/", 1)[1]
    assert hashlib.sha256(token.encode()).hexdigest() == row["token_hash"]   # only the hash is stored
    assert token not in str(row)


@pytest.mark.parametrize("found", [None, _user(email=None), _user(active=False)])
def test_forgot_unknown_or_unusable_account_says_the_same_and_sends_nothing(client, sb, mail_on, sent, found):
    with patch("admin_app._find_user_by_identifier", return_value=found), patch("admin_app.flash") as fl, patch("admin_app.log_user_activity"):
        r = client.post("/forgot", data={"identifier": "whoever"})
    assert r.status_code == 302
    assert "reset link is on its way" in fl.call_args.args[0]
    assert not sent.called and not sb.insert.called


def test_forgot_is_rate_limited(client, sb, mail_on, sent):
    with patch("admin_app._find_user_by_identifier", return_value=None), patch("admin_app.flash") as fl, patch("admin_app.log_user_activity"):
        for _ in range(5):
            client.post("/forgot", data={"identifier": "hammer"})
        r = client.post("/forgot", data={"identifier": "hammer"})
    assert r.status_code == 429 and "wait 15 minutes" in fl.call_args.args[0]


def test_find_user_by_identifier_picks_the_column(sb):
    with app.test_request_context():
        admin_app._find_user_by_identifier("Bob")
        assert sb.eq.call_args.args == ("username", "bob")
        admin_app._find_user_by_identifier("Bob@X.org")
        assert sb.eq.call_args.args == ("email", "bob@x.org")


# ── reset ────────────────────────────────────────────────────────────────────

def _reset_row(minutes=30, used=None):
    return {"id": "r1", "user_id": UID, "used_at": used,
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()}


def test_reset_rejects_bad_expired_and_used_tokens(client, sb):
    for row in ([], [_reset_row(minutes=-1)], [_reset_row(used="2026-01-01T00:00:00+00:00")]):
        sb.execute.return_value = MagicMock(data=row)
        with patch("admin_app.flash") as fl:
            r = client.get("/reset/sometoken")
        assert r.status_code == 400 and "invalid or has expired" in fl.call_args.args[0]
        with patch("admin_app.flash"):
            assert client.post("/reset/sometoken", data={"new_password": "longenough1", "confirm_password": "longenough1"}).status_code == 400
    assert not sb.update.called


def test_reset_with_good_token_sets_password_and_burns_token(client, sb):
    sb.execute.side_effect = [
        MagicMock(data=[_reset_row()]), MagicMock(data=[{"id": UID, "username": "alice", "is_active": True}]),  # _valid_reset
        MagicMock(data=[]), MagicMock(data=[]),                                                               # update user, update reset
        MagicMock(data=[]), MagicMock(data=[]),                                                               # activity, audit
    ]
    with patch("admin_app.flash") as fl, patch("admin_app.bcrypt.hashpw", return_value=b"$2b$12$hash"):
        r = client.post("/reset/goodtoken", data={"new_password": "longenough1", "confirm_password": "longenough1"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    assert "Password updated" in fl.call_args.args[0]
    ups = [c.args[0] for c in sb.update.call_args_list]
    assert ups[0] == {"password_hash": "$2b$12$hash", "must_change_password": False}
    assert "used_at" in ups[1]


def test_reset_validates_password_fields(client, sb):
    sb.execute.side_effect = lambda *a, **k: MagicMock(data=[_reset_row()]) if sb.execute.call_count % 2 else MagicMock(data=[{"id": UID, "username": "alice", "is_active": True}])
    with patch("admin_app.flash") as fl:
        r = client.post("/reset/goodtoken", data={"new_password": "short", "confirm_password": "short"})
    assert r.status_code == 400 and "at least 8" in fl.call_args.args[0]


# ── notification routing (pure) ──────────────────────────────────────────────

USERS = [
    {"username": "alice", "email": "a@x", "role": "member", "notify_flags": True},
    {"username": "bob", "email": "b@x", "role": "member", "notify_flags": True},
    {"username": "carol", "email": "c@x", "role": "board", "notify_flags": True},
    {"username": "dave", "email": "d@x", "role": "superuser", "notify_flags": False},
    {"username": "erin", "email": None, "role": "member", "notify_flags": True},
]


def names(rows):
    return sorted(u["username"] for u in rows)


def test_new_flag_goes_to_everyone_including_the_actor_but_not_opt_outs():
    assert names(notification_recipients("created", {"alice"}, USERS, "alice")) == ["alice", "bob", "carol"]


def test_comments_and_closures_go_to_the_thread_only():
    parts = {"alice", "bob"}
    assert names(notification_recipients("comment", parts, USERS, "bob")) == ["alice", "bob"]
    assert names(notification_recipients("closed", parts, USERS, "carol")) == ["alice", "bob", "carol"]   # the actor gets a receipt
    assert names(notification_recipients("reopened", {"alice"}, USERS, "alice")) == ["alice"]
    assert names(notification_recipients("reopened", {"alice"}, USERS, "dave")) == ["alice"]            # dave opted out


def test_board_steps_go_to_board_and_thread():
    assert names(notification_recipients("submitted", {"alice"}, USERS, "alice")) == ["alice", "carol"]   # dave opted out
    assert names(notification_recipients("recalled", {"alice", "bob"}, USERS, "carol")) == ["alice", "bob", "carol"]
    assert names(notification_recipients("decided", {"alice"}, USERS, "carol")) == ["alice", "carol"]


# ── lifecycle routes send ────────────────────────────────────────────────────

def _flag(status="in_review", **over):
    f = {"id": FID, "flag_type": "clause", "clause_id": "DECL_26_03", "flagged_by": "alice", "status": status,
         "proposal_text": "new words", "question_text": None}
    f.update(over)
    return f


def _users_rows():
    return [{"username": "alice", "email": "a@x", "role": "member", "is_active": True, "notify_flags": True},
            {"username": "bob", "email": "b@x", "role": "board", "is_active": True, "notify_flags": True}]


def test_comment_emails_the_other_participants_with_a_link(client, sb, mail_on, sent):
    sb.execute.side_effect = lambda *a, **k: MagicMock(data=[])
    with patch("admin_app._fetch_flag", return_value=_flag("open")), patch("admin_app.log_audit_event"), \
         patch("admin_app._flag_participants", return_value={"alice", "bob"}), \
         patch("admin_app.supabase") as sup:
        sup.return_value = _chainable(_users_rows())
        s, l = as_role("member", "bob")
        with s, l:
            client.post(f"/admin/flags/{FID}/comment", data={"comment": "Let's keep the caliper rule."})
    assert sorted(c.args[0] for c in sent.call_args_list) == ["a@x", "b@x"]   # alice (flagger) and bob (actor)
    to, subject, body = [c.args for c in sent.call_args_list if c.args[0] == "a@x"][0][:3]
    assert subject == "New comment: DECL_26_03"
    assert "bob commented on DECL_26_03" in body and "Status now: In Discussion" in body
    assert "Let's keep the caliper rule." in body and f"http://console.test/admin/flags/{FID}" in body
    assert sent.call_args.kwargs == {"kind": "flag_comment", "related_id": FID}


def test_submit_emails_the_board(client, sb, mail_on, sent):
    with patch("admin_app._fetch_flag", return_value=_flag("in_review")), patch("admin_app.log_audit_event"), \
         patch("admin_app._flag_participants", return_value={"alice"}), patch("admin_app._flag_system_comment"), \
         patch("admin_app.supabase") as sup:
        sup.return_value = _chainable(_users_rows())
        s, l = as_role("member", "alice")
        with s, l:
            client.post(f"/admin/flags/{FID}/submit")
    assert sorted(c.args[0] for c in sent.call_args_list) == ["a@x", "b@x"]
    assert sent.call_args.args[1] == "Sent to the Board: DECL_26_03"


def test_create_flag_emails_everyone_else(client, sb, mail_on, sent):
    with patch("admin_app.log_audit_event"), patch("admin_app._flag_participants", return_value={"alice"}), \
         patch("admin_app.supabase") as sup:
        chain = _chainable(_users_rows())
        chain.execute.side_effect = [MagicMock(data=[{"id": FID}]), MagicMock(data=_users_rows())]
        sup.return_value = chain
        s, l = as_role("member", "alice")
        with s, l:
            r = client.post("/admin/flags", data={"flag_type": "clause", "clause_id": "DECL_26_03", "flag_notes": "Trees."})
    assert r.get_json()["ok"]
    assert sorted(c.args[0] for c in sent.call_args_list) == ["a@x", "b@x"]
    assert sent.call_args.args[1] == "New revision flag: DECL_26_03" and "Trees." in sent.call_args.args[2]


def test_nothing_is_sent_when_mail_is_off(client, sb, sent, monkeypatch):
    monkeypatch.delenv("MAIL_BACKEND", raising=False)
    with patch("admin_app._fetch_flag", return_value=_flag("open")), patch("admin_app.log_audit_event"):
        s, l = as_role("member", "bob")
        with s, l:
            client.post(f"/admin/flags/{FID}/comment", data={"comment": "hi"})
    assert not sent.called


# ── account email ────────────────────────────────────────────────────────────

def test_user_sets_own_email_and_opt_out(client, sb):
    with patch("admin_app.log_audit_event") as audit, patch("admin_app.flash"):
        s, l = as_role("member")
        with s, l:
            r = client.post("/admin/users/me/email", data={"email": "Alice@Example.com"})
    assert r.status_code == 302
    assert sb.update.call_args.args[0] == {"email": "alice@example.com", "notify_flags": False}
    assert audit.call_args.kwargs["action"] == "user_email_set"


def test_bad_email_is_refused(client, sb):
    with patch("admin_app.flash") as fl:
        s, l = as_role("member")
        with s, l:
            client.post("/admin/users/me/email", data={"email": "not-an-address", "notify_flags": "1"})
    assert "does not look right" in fl.call_args.args[0] and not sb.update.called


def test_member_cannot_set_another_users_email(client, sb):
    with patch("admin_app.flash"):
        s, l = as_role("member")
        with s, l:
            r = client.post("/admin/users/other-id/set-email", data={"email": "x@y.z"})
    assert r.status_code == 302 and not sb.update.called


def test_superuser_sets_another_users_email(client, sb):
    sb.execute.return_value = MagicMock(data=[{"username": "bob"}])
    with patch("admin_app.log_audit_event"):
        s, l = as_role("superuser")
        with s, l:
            r = client.post("/admin/users/other-id/set-email", data={"email": "bob@x.org"})
    assert r.get_json() == {"ok": True, "message": "Email set for bob."}
    assert sb.update.call_args.args[0] == {"email": "bob@x.org"}


def test_dev_outbox_is_dev_only(client, sb, monkeypatch):
    monkeypatch.delenv("HOA_ENV", raising=False)
    with patch("admin_app.flash"):
        s, l = as_role("member")
        with s, l:
            assert client.get("/admin/dev/mail").status_code == 302
    monkeypatch.setenv("HOA_ENV", "dev"); monkeypatch.setenv("MAIL_BACKEND", "file")
    with patch("admin_app.render_template", return_value="ok") as rt, patch("admin_app.mailer.outbox", return_value=[]):
        s, l = as_role("member")
        with s, l:
            assert client.get("/admin/dev/mail").status_code == 200
    assert rt.call_args.args[0] == "admin_dev_mail.html"
