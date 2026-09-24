"""
Committee Member Guide — governance workflow diagram (MEMBER_WORKFLOW.md,
rendered at /admin/guide).

Locks in the September 2026 rewrite: the diagram must show the sequence
Committee Review → Committee Proposal → Board Approval / Rejection →
Community Vote, with the Board's green outcome being "Board Approved /
Staged for Community Vote" (never "Live to residents"), and the
explanatory text must mention the community vote.
"""
import os
import re
from datetime import datetime, timezone

import pytest
from unittest.mock import MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import admin_app
from admin_app import app

REPO = os.path.dirname(admin_app.__file__)


def _md() -> str:
    with open(os.path.join(REPO, "MEMBER_WORKFLOW.md"), encoding="utf-8") as f:
        return f.read()


def _diagram() -> str:
    m = re.search(r"```mermaid\n(.*?)```", _md(), re.S)
    assert m, "MEMBER_WORKFLOW.md has no mermaid diagram"
    return m.group(1)


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


# ── Unit: the diagram source ──────────────────────────────────────────────────

def test_diagram_has_three_lanes_in_governance_order():
    d = _diagram()
    committee = d.index('subgraph COMMITTEE')
    board = d.index('subgraph BOARD')
    community = d.index('subgraph COMMUNITY')
    assert committee < board < community
    assert "Committee" in d and "HOA Board" in d and "Community" in d


def test_diagram_committee_lane_covers_review_flag_discuss_propose():
    d = _diagram()
    lane = d[d.index("subgraph COMMITTEE"):d.index("subgraph BOARD")]
    for step in ("Review a clause", "Flag items", "Discuss in the", "Propose the revised language", "in the flag thread"):
        assert step in lane, f"committee lane missing '{step}'"
    # members never see the database edit form, so the diagram must not point at it
    assert "Submit for Approval" not in lane


def test_diagram_board_lane_approves_or_rejects():
    d = _diagram()
    lane = d[d.index("subgraph BOARD"):d.index("subgraph COMMUNITY")]
    assert "approves or rejects" in lane
    assert "Board reviews the" in lane
    assert "|Approved|" in lane and "|Rejected|" in lane
    assert "Board Approved" in lane and "Staged for Community Vote" in lane
    assert "Reason recorded on the <b>flag</b>" in lane
    assert "resubmit" in lane


def test_diagram_flow_is_committee_to_board_to_community():
    d = _diagram()
    assert re.search(r"^\s*E -->.*\bF\s*$", d, re.M), "proposal must flow to the Board"
    assert re.search(r"^\s*G -->.*\bV\s*$", d, re.M), "Board approval must flow to the Community Vote"
    assert "Community Vote" in d


def test_diagram_never_claims_board_approval_goes_live():
    low = _diagram().lower()
    for stale in ("live to residents", "~1 hour", "accuracy<br/>check", "accuracy check"):
        assert stale not in low, f"stale wording in diagram: {stale!r}"


def test_diagram_has_no_loop_back_edge():
    # Loop-backs make dagre/ELK push lanes sideways and cross the submission
    # arrow; the resubmit path is carried by the Rejected node's own label.
    assert "-.->" not in _diagram()


def test_explanatory_text_mentions_board_then_community_vote():
    md = _md()
    intro = md[md.index("## The big picture"):md.index("## Step 0")]
    low = intro.lower()
    assert "committee review → committee proposal → board approval / rejection → community vote" in low
    assert "final board approval happens after the committee's proposal is submitted" in low
    assert "advanced to the community for final voting" in low
    assert "revised and resubmitted" in low
    assert "does not ratify" not in low


def test_never_see_table_names_the_board():
    md = _md()
    row = next(l for l in md.splitlines() if l.startswith("| Approve / reject buttons"))
    assert "HOA Board" in row and "community vote" in row.lower()


# ── Integration: the rendered /admin/guide page ───────────────────────────────

def test_guide_page_renders_governance_diagram_for_members(client, mock_supabase):
    sess, load = member_session()
    with sess, load:
        resp = client.get("/admin/guide")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    m = re.search(r'<div class="mermaid">(.*?)</div>', html, re.S)
    assert m, "guide page lost its mermaid block"
    diagram = m.group(1)
    for needle in ("subgraph COMMITTEE", "subgraph BOARD", "subgraph COMMUNITY",
                   "HOA Board", "Board Approved", "Staged for Community Vote",
                   "Community Vote", "in the flag thread"):
        assert needle in diagram, f"rendered diagram missing {needle!r}"
    assert "Live to residents" not in html
    assert "&lt;br/&gt;" not in diagram            # html un-escaped for mermaid.js


def test_guide_page_uses_elk_renderer_and_lane_tints(client, mock_supabase):
    sess, load = member_session()
    with sess, load:
        resp = client.get("/admin/guide")
    html = resp.get_data(as_text=True)
    assert 'defaultRenderer: "elk"' in html
    for lane in ("COMMITTEE", "BOARD", "COMMUNITY"):
        assert f'flowchart-{lane}-' in html, f"lane tint CSS missing for {lane}"
