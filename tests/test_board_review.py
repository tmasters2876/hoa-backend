"""
Board review loop (sql/003_board_review.sql, Sept 2026):

  open → in_review → awaiting_board → closed_changed | closed_no_change | closed_deferred

- Any committee member saves the proposal, submits it, recalls it (until the
  Board decides), and reopens it after a rejection/deferral.
- Only board-and-up can decide; rejection requires a reason.
- Every lifecycle step writes an append-only system comment into the thread.
- The Board Decisions page is board-and-up.
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
from admin_app import app, FLAG_STATUS_LABELS, FLAG_ACTIVE_STATUSES, FLAG_DECISIONS

FID = "22222222-2222-2222-2222-222222222222"


def _flag(status="in_review", **over):
    f = {
        "id": FID, "flag_type": "clause", "clause_id": "DECL_27_08", "flagged_by": "alice",
        "flag_notes": "Pond liability is one-sided", "status": status, "resolution_notes": None,
        "closed_by": None, "question_text": None, "answer_snapshot": None, "cited_clause_ids": None,
        "created_at": "2026-09-01T10:00:00", "updated_at": "2026-09-02T10:00:00",
        "proposal_current": "The Association shall not be responsible…",
        "proposal_text": "The Association shall post pond rules at each access point.",
        "proposal_source": "new drafting", "proposal_updated_by": "alice",
        "proposal_updated_at": "2026-09-02T10:00:00", "submitted_by": None, "submitted_at": None,
        "decided_at": None,
    }
    f.update(over)
    return f


def _chainable():
    m = MagicMock()
    for method in ("from_", "select", "insert", "update", "delete", "eq", "neq", "is_", "or_", "contains",
                   "ilike", "gte", "lte", "order", "range", "limit", "rpc", "in_"):
        getattr(m, method).return_value = m
    m.execute.return_value = MagicMock(data=[], count=0)
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


def as_role(role, username="bob"):
    data = {"logged_in": True, "username": username, "user_id": "u1", "role": role,
            "logged_in_at": datetime.now(timezone.utc).isoformat()}
    m = MagicMock()
    m.get.side_effect = lambda k, d=None: data.get(k, d)
    row = {"id": "u1", "username": username, "is_active": True, "role": role, "must_change_password": False}
    return patch("admin_app.session", m), patch("admin_app._load_current_user", return_value=row)


def updates(sb):
    return [c.args[0] for c in sb.update.call_args_list]


def system_comments(sb):
    return [c.args[0]["comment"] for c in sb.insert.call_args_list
            if isinstance(c.args[0], dict) and "comment" in c.args[0]]


def flash_msgs(mock_flash):
    return " ".join(str(c.args[0]) for c in mock_flash.call_args_list).lower()


# ── vocabulary ────────────────────────────────────────────────────────────────

def test_status_vocabulary():
    assert set(FLAG_STATUS_LABELS) == {"open", "in_review", "awaiting_board",
                                       "closed_changed", "closed_no_change", "closed_deferred"}
    assert FLAG_STATUS_LABELS["closed_changed"] == "Board Approved"
    assert FLAG_STATUS_LABELS["closed_no_change"] == "Board Rejected"
    assert "awaiting_board" in FLAG_ACTIVE_STATUSES
    assert FLAG_DECISIONS == {"approved": "closed_changed", "rejected": "closed_no_change", "deferred": "closed_deferred"}


# ── proposal ──────────────────────────────────────────────────────────────────

def test_member_saves_proposal_and_open_flag_moves_to_discussion(client, sb):
    with patch("admin_app._fetch_flag", return_value=_flag("open")), patch("admin_app.log_audit_event"):
        s, l = as_role("member")
        with s, l:
            resp = client.post(f"/admin/flags/{FID}/proposal", data={
                "proposal_current": "old", "proposal_text": "new", "proposal_source": "TPC 202"})
    assert resp.status_code == 302 and "#proposal" in resp.headers["Location"]
    u = updates(sb)[-1]
    assert u["proposal_text"] == "new" and u["proposal_updated_by"] == "bob" and u["status"] == "in_review"


def test_proposal_requires_text(client, sb):
    with patch("admin_app._fetch_flag", return_value=_flag("open")), patch("admin_app.flash") as fl:
        s, l = as_role("member")
        with s, l:
            client.post(f"/admin/flags/{FID}/proposal", data={"proposal_text": "  "})
    assert not sb.update.called and "required" in flash_msgs(fl)


def test_proposal_locked_while_awaiting_board(client, sb):
    with patch("admin_app._fetch_flag", return_value=_flag("awaiting_board")), patch("admin_app.flash") as fl:
        s, l = as_role("member")
        with s, l:
            client.post(f"/admin/flags/{FID}/proposal", data={"proposal_text": "edit attempt"})
    assert not sb.update.called and "locked" in flash_msgs(fl)


# ── submit / recall ───────────────────────────────────────────────────────────

def test_any_member_can_submit_to_board(client, sb):
    with patch("admin_app._fetch_flag", return_value=_flag("in_review")), patch("admin_app.log_audit_event"):
        s, l = as_role("member", username="carol")
        with s, l:
            resp = client.post(f"/admin/flags/{FID}/submit")
    assert resp.status_code == 302
    u = updates(sb)[-1]
    assert u["status"] == "awaiting_board" and u["submitted_by"] == "carol" and u["submitted_at"]
    assert any("Submitted to the Board" in c for c in system_comments(sb))


def test_submit_requires_a_saved_proposal(client, sb):
    with patch("admin_app._fetch_flag", return_value=_flag("in_review", proposal_text=None)), patch("admin_app.flash") as fl:
        s, l = as_role("member")
        with s, l:
            client.post(f"/admin/flags/{FID}/submit")
    assert not sb.update.called and "record the proposed language" in flash_msgs(fl)


@pytest.mark.parametrize("status", ["awaiting_board", "closed_changed", "closed_no_change"])
def test_submit_refused_unless_with_committee(client, sb, status):
    with patch("admin_app._fetch_flag", return_value=_flag(status)), patch("admin_app.flash") as fl:
        s, l = as_role("member")
        with s, l:
            client.post(f"/admin/flags/{FID}/submit")
    assert not sb.update.called and "cannot be submitted" in flash_msgs(fl)


def test_any_member_can_recall_before_decision(client, sb):
    with patch("admin_app._fetch_flag", return_value=_flag("awaiting_board", submitted_by="carol")), patch("admin_app.log_audit_event"):
        s, l = as_role("member", username="dave")   # not the submitter
        with s, l:
            resp = client.post(f"/admin/flags/{FID}/recall", data={"reason": "typo in section 2"})
    assert resp.status_code == 302
    u = updates(sb)[-1]
    assert u["status"] == "in_review" and u["submitted_by"] is None and u["submitted_at"] is None
    assert any("Recalled from the Board" in c and "typo in section 2" in c for c in system_comments(sb))


@pytest.mark.parametrize("status", ["in_review", "closed_changed", "closed_no_change"])
def test_recall_only_from_awaiting_board(client, sb, status):
    with patch("admin_app._fetch_flag", return_value=_flag(status)), patch("admin_app.flash") as fl:
        s, l = as_role("member")
        with s, l:
            client.post(f"/admin/flags/{FID}/recall")
    assert not sb.update.called and "only a flag awaiting the board" in flash_msgs(fl)


# ── decide ────────────────────────────────────────────────────────────────────

def test_member_cannot_decide(client, sb):
    with patch("admin_app._fetch_flag", return_value=_flag("awaiting_board")), patch("admin_app.flash") as fl:
        s, l = as_role("member")
        with s, l:
            resp = client.post(f"/admin/flags/{FID}/decide", data={"decision": "approved"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/admin")
    assert not sb.update.called and "permission" in flash_msgs(fl)


@pytest.mark.parametrize("role", ["board", "superuser"])
def test_board_approves(client, sb, role):
    with patch("admin_app._fetch_flag", return_value=_flag("awaiting_board")), patch("admin_app.log_audit_event"):
        s, l = as_role(role, username="pres")
        with s, l:
            resp = client.post(f"/admin/flags/{FID}/decide", data={"decision": "approved", "next": "/admin/board"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/admin/board")
    u = updates(sb)[-1]
    assert u["status"] == "closed_changed" and u["closed_by"] == "pres" and u["decided_at"]
    assert any("Board Approved" in c for c in system_comments(sb))


def test_board_reject_requires_reason(client, sb):
    with patch("admin_app._fetch_flag", return_value=_flag("awaiting_board")), patch("admin_app.flash") as fl:
        s, l = as_role("board")
        with s, l:
            client.post(f"/admin/flags/{FID}/decide", data={"decision": "rejected", "decision_notes": ""})
    assert not sb.update.called and "reason is required" in flash_msgs(fl)


def test_board_rejects_with_reason_recorded_on_flag_and_thread(client, sb):
    with patch("admin_app._fetch_flag", return_value=_flag("awaiting_board")), patch("admin_app.log_audit_event"):
        s, l = as_role("board")
        with s, l:
            client.post(f"/admin/flags/{FID}/decide", data={"decision": "rejected", "decision_notes": "conflicts with TPC 202.010"})
    u = updates(sb)[-1]
    assert u["status"] == "closed_no_change" and u["resolution_notes"] == "conflicts with TPC 202.010"
    assert any("Board Rejected" in c and "202.010" in c for c in system_comments(sb))


def test_board_defers(client, sb):
    with patch("admin_app._fetch_flag", return_value=_flag("awaiting_board")), patch("admin_app.log_audit_event"):
        s, l = as_role("board")
        with s, l:
            client.post(f"/admin/flags/{FID}/decide", data={"decision": "deferred"})
    assert updates(sb)[-1]["status"] == "closed_deferred"


def test_decide_only_when_awaiting(client, sb):
    with patch("admin_app._fetch_flag", return_value=_flag("in_review")), patch("admin_app.flash") as fl:
        s, l = as_role("board")
        with s, l:
            client.post(f"/admin/flags/{FID}/decide", data={"decision": "approved"})
    assert not sb.update.called and "only a flag awaiting the board" in flash_msgs(fl)


def test_invalid_decision_rejected(client, sb):
    with patch("admin_app._fetch_flag", return_value=_flag("awaiting_board")), patch("admin_app.flash") as fl:
        s, l = as_role("board")
        with s, l:
            client.post(f"/admin/flags/{FID}/decide", data={"decision": "maybe"})
    assert not sb.update.called and "invalid decision" in flash_msgs(fl)


# ── reopen ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", ["closed_no_change", "closed_deferred"])
def test_member_reopens_rejected_or_deferred(client, sb, status):
    with patch("admin_app._fetch_flag", return_value=_flag(status, closed_by="pres", resolution_notes="no")), patch("admin_app.log_audit_event"):
        s, l = as_role("member")
        with s, l:
            resp = client.post(f"/admin/flags/{FID}/reopen")
    assert resp.status_code == 302 and "#proposal" in resp.headers["Location"]
    u = updates(sb)[-1]
    assert u["status"] == "in_review" and u["submitted_by"] is None
    assert any("Reopened" in c for c in system_comments(sb))


@pytest.mark.parametrize("status", ["closed_changed", "awaiting_board", "in_review"])
def test_reopen_refused_otherwise(client, sb, status):
    with patch("admin_app._fetch_flag", return_value=_flag(status)), patch("admin_app.flash") as fl:
        s, l = as_role("member")
        with s, l:
            client.post(f"/admin/flags/{FID}/reopen")
    assert not sb.update.called and "only a rejected or deferred" in flash_msgs(fl)


# ── legacy status route no longer closes flags ────────────────────────────────

@pytest.mark.parametrize("status", ["closed_changed", "closed_no_change", "closed_deferred", "awaiting_board"])
def test_status_route_cannot_close_or_submit(client, sb, status):
    with patch("admin_app._fetch_flag", return_value=_flag("in_review")), patch("admin_app.flash") as fl:
        s, l = as_role("superuser")
        with s, l:
            client.post(f"/admin/flags/{FID}/status", data={"status": status})
    assert not sb.update.called and "board decisions page" in flash_msgs(fl)


def test_status_route_toggles_committee_states(client, sb):
    with patch("admin_app._fetch_flag", return_value=_flag("open")), patch("admin_app.log_audit_event"):
        s, l = as_role("member")
        with s, l:
            client.post(f"/admin/flags/{FID}/status", data={"status": "in_review"})
    assert updates(sb)[-1]["status"] == "in_review"


# ── Board Decisions page ──────────────────────────────────────────────────────

def test_board_page_gated(client, sb):
    s, l = as_role("member")
    with s, l, patch("admin_app.flash"):
        resp = client.get("/admin/board")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/admin")


def test_board_page_renders_queue_with_proposal(client, sb):
    sb.execute.return_value = MagicMock(data=[_flag("awaiting_board", submitted_by="carol", submitted_at="2026-09-03T09:00:00")], count=1)
    s, l = as_role("board")
    with s, l:
        html = client.get("/admin/board").get_data(as_text=True)
    for needle in ("Board Decisions", "DECL_27_08", "post pond rules", "submitted by", "carol",
                   'name="decision" value="approved"', 'name="decision" value="rejected"', 'name="decision" value="deferred"'):
        assert needle in html, needle
    assert ("status", "awaiting_board") in [c.args for c in sb.eq.call_args_list]


def test_board_page_history_tab(client, sb):
    sb.execute.return_value = MagicMock(data=[_flag("closed_no_change", closed_by="pres", resolution_notes="conflicts", decided_at="2026-09-04T09:00:00")], count=1)
    s, l = as_role("board")
    with s, l:
        html = client.get("/admin/board?view=history").get_data(as_text=True)
    assert "Board Rejected" in html and "conflicts" in html
    assert 'name="decision"' not in html


def test_sidebar_board_badge_for_board_only(client, sb):
    sb.execute.return_value = MagicMock(data=[], count=3)
    s, l = as_role("board")
    with s, l:
        html = client.get("/admin/flags").get_data(as_text=True)
    assert "Board Decisions" in html
    s, l = as_role("member")
    with s, l:
        html = client.get("/admin/flags").get_data(as_text=True)
    assert "Board Decisions" not in html


# ── flag page rendering ───────────────────────────────────────────────────────

def _render_flag(client, sb, role, flag):
    sb.execute.return_value = MagicMock(data=[], count=0)
    with patch("admin_app._fetch_flag", return_value=flag):
        s, l = as_role(role)
        with s, l:
            return client.get(f"/admin/flags/{FID}").get_data(as_text=True)


def test_flag_page_member_editable_with_submit(client, sb):
    html = _render_flag(client, sb, "member", _flag("in_review"))
    assert 'name="proposal_text"' in html and "Submit to the Board" in html
    assert "Recall from Board" not in html and 'name="decision"' not in html
    assert "2 · Committee proposal" in html


def test_flag_page_awaiting_shows_recall_to_member_and_decision_to_board(client, sb):
    member = _render_flag(client, sb, "member", _flag("awaiting_board", submitted_by="carol"))
    assert "Recall from Board" in member and 'name="proposal_text"' not in member and 'name="decision"' not in member
    board = _render_flag(client, sb, "board", _flag("awaiting_board", submitted_by="carol"))
    assert 'name="decision" value="approved"' in board and "Recall from Board" in board


def test_flag_page_rejected_shows_reason_and_reopen(client, sb):
    html = _render_flag(client, sb, "member", _flag("closed_no_change", closed_by="pres", resolution_notes="conflicts with state law", decided_at="2026-09-04T09:00:00"))
    assert "Board Rejected" in html and "conflicts with state law" in html and "Reopen for revision" in html
    assert "pres" not in html                       # reviewer identity hidden from members
    assert 'name="proposal_text"' not in html        # locked until reopened


def test_flag_page_approved_shows_community_vote_step(client, sb):
    html = _render_flag(client, sb, "member", _flag("closed_changed", decided_at="2026-09-04T09:00:00"))
    assert "Board Approved" in html and "5 · Community vote" in html and "Reopen for revision" not in html


# ── clause page lists its flags by TEXT clause_id (regression) ────────────────

def test_clause_page_queries_flags_by_text_clause_id(client, sb):
    clause = {"id": "11111111-1111-1111-1111-111111111111", "clause_id": "DECL_27_08", "document": "D", "page": 1,
              "citation": "c", "clause_text": "t", "plain_summary": "s", "link": None, "embedding": None,
              "match_source": None, "tags": [], "created_at": "2026-01-01", "precedence_level": 2, "status": "approved"}
    sb.execute.return_value = MagicMock(data=[clause], count=1)
    s, l = as_role("member")
    with s, l:
        client.get("/admin/clauses/DECL_27_08")
    eqs = [c.args for c in sb.eq.call_args_list]
    assert ("clause_id", "DECL_27_08") in eqs
    assert ("clause_id", clause["id"]) not in eqs


# ── guide ─────────────────────────────────────────────────────────────────────

def test_guide_documents_submit_recall_and_board(client, sb):
    md = open(os.path.join(os.path.dirname(admin_app.__file__), "MEMBER_WORKFLOW.md"), encoding="utf-8").read()
    for needle in ("Submit to the Board", "Recall from Board", "Awaiting Board", "Board Rejected", "Reopen for revision"):
        assert needle in md, needle
