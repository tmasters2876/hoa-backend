"""
Tests for submit_pending_change, _apply_pending_change, and the
/admin/pending/<change_id>/approve route in admin_app.py.
"""
import os
import pytest
from unittest.mock import MagicMock, patch

# Must be set before admin_app (and services.py) are imported, because both
# read environment variables at module level.
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from admin_app import app, submit_pending_change, _apply_pending_change


# ── Helpers ───────────────────────────────────────────────────────────────────

def _chainable():
    """Return a MagicMock where every Supabase-style method returns itself,
    enabling arbitrary method chaining.  execute() returns an empty result
    by default; override with m.execute.return_value or .side_effect."""
    m = MagicMock()
    for method in (
        "from_", "select", "insert", "update", "delete",
        "eq", "neq", "is_", "or_", "contains", "ilike",
        "gte", "lte", "order", "range", "limit", "rpc",
    ):
        getattr(m, method).return_value = m
    m.execute.return_value = MagicMock(data=[], count=0)
    return m


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def mock_supabase():
    """Patch get_supabase_client (the name imported into admin_app) so every
    call to supabase() inside admin_app returns our chainable mock."""
    sb = _chainable()
    with patch("admin_app.get_supabase_client", return_value=sb):
        yield sb


@pytest.fixture
def mock_session():
    """Patch admin_app.session with a MagicMock pre-configured as a logged-in
    superuser (tmasters).  Tests that need a different user can override
    m.get.side_effect inside the test."""
    session_data = {
        "logged_in": True,
        "username": "tmasters",
        "user_id": "user-123",
        "logged_in_at": "2026-04-01T10:00:00+00:00",
    }
    m = MagicMock()
    m.get.side_effect = lambda k, default=None: session_data.get(k, default)
    with patch("admin_app.session", m):
        yield m


# ── submit_pending_change ─────────────────────────────────────────────────────

class TestSubmitPendingChange:

    def test_create_action_calls_verification(self, mock_supabase, mock_session):
        mock_supabase.execute.return_value = MagicMock(data=[{"id": "chg-1"}])
        proposed = {
            "clause_text": "No structure without ARC approval.",
            "link": "https://drive.google.com/file/d/abc123",
            "page": 5,
            "citation": "Art. VI, Sec. 3",
        }
        with patch("admin_app.verify_clause_against_source") as mock_verify:
            mock_verify.return_value = {"status": "verified", "score": 90, "reason": "Found"}
            result = submit_pending_change("clause-uuid-1", "create", proposed, None)

        mock_verify.assert_called_once_with(
            "No structure without ARC approval.",
            "https://drive.google.com/file/d/abc123",
            5,
        )
        assert result is not None
        assert result["id"] == "chg-1"
        assert result["verification"]["status"] == "verified"
        assert result["verification"]["score"] == 90
        assert "verified_at" in result["verification"]

    def test_edit_action_calls_verification(self, mock_supabase, mock_session):
        mock_supabase.execute.return_value = MagicMock(data=[{"id": "chg-2"}])
        proposed = {
            "clause_text": "Edited clause text.",
            "link": "https://drive.google.com/file/d/xyz789",
            "page": 3,
            "citation": "Art. I",
        }
        with patch("admin_app.verify_clause_against_source") as mock_verify:
            mock_verify.return_value = {"status": "partial", "score": 65, "reason": "Partial match"}
            result = submit_pending_change(
                "clause-uuid-2", "edit", proposed, {"clause_text": "Old text"}
            )

        mock_verify.assert_called_once()
        assert result is not None
        assert result["verification"]["status"] == "partial"

    def test_delete_action_skips_verification(self, mock_supabase, mock_session):
        mock_supabase.execute.return_value = MagicMock(data=[{"id": "chg-3"}])
        with patch("admin_app.verify_clause_against_source") as mock_verify:
            result = submit_pending_change(
                "clause-uuid-3", "delete", None, {"clause_text": "Old text"}
            )

        mock_verify.assert_not_called()
        assert result is not None
        assert result["verification"] is None

    def test_create_missing_fields_skips_verification(self, mock_supabase, mock_session):
        """create with no link/page should skip PDF check and record 'skipped'."""
        mock_supabase.execute.return_value = MagicMock(data=[{"id": "chg-4"}])
        proposed = {"clause_text": "Some text"}  # missing link and page
        with patch("admin_app.verify_clause_against_source") as mock_verify:
            result = submit_pending_change("clause-uuid-4", "create", proposed, None)

        mock_verify.assert_not_called()
        assert result["verification"]["status"] == "skipped"
        assert result["verification"]["reason"] == "Missing text, link, or page"

    def test_verification_stored_in_proposed_changes(self, mock_supabase, mock_session):
        """_verification must be embedded into proposed_changes before insert."""
        mock_supabase.execute.return_value = MagicMock(data=[{"id": "chg-5"}])
        proposed = {
            "clause_text": "Some clause.",
            "link": "https://drive.google.com/file/d/abc",
            "page": 2,
            "citation": "Art. II",
        }
        with patch("admin_app.verify_clause_against_source") as mock_verify:
            mock_verify.return_value = {"status": "verified", "score": 85, "reason": "OK"}
            submit_pending_change("clause-uuid-5", "create", proposed, None)

        insert_payload = mock_supabase.insert.call_args[0][0]
        assert "_verification" in insert_payload["proposed_changes"]
        assert insert_payload["proposed_changes"]["_verification"]["status"] == "verified"

    def test_returns_none_on_supabase_failure(self, mock_supabase, mock_session):
        mock_supabase.execute.side_effect = Exception("DB connection error")
        with patch("admin_app.verify_clause_against_source"):
            result = submit_pending_change(
                "clause-uuid-6", "create", {"clause_text": "text"}, None
            )
        assert result is None


# ── _apply_pending_change ─────────────────────────────────────────────────────

class TestApplyPendingChange:

    def test_create_sets_status_approved(self, mock_supabase):
        change = {"clause_id": "c-uuid-1", "action": "create", "proposed_changes": {}}
        _apply_pending_change(change)

        mock_supabase.update.assert_called_once_with({"status": "approved"})
        mock_supabase.eq.assert_called_with("id", "c-uuid-1")

    def test_edit_applies_proposed_fields(self, mock_supabase):
        change = {
            "clause_id": "c-uuid-2",
            "action": "edit",
            "proposed_changes": {
                "clause_text": "Updated clause.",
                "citation": "Art. V, Sec. 1",
            },
        }
        _apply_pending_change(change)

        payload = mock_supabase.update.call_args[0][0]
        assert payload["clause_text"] == "Updated clause."
        assert payload["citation"] == "Art. V, Sec. 1"
        assert payload["status"] == "approved"
        mock_supabase.eq.assert_called_with("id", "c-uuid-2")

    def test_edit_excludes_private_keys(self, mock_supabase):
        """Keys prefixed with '_' (e.g. _verification) must be stripped before update."""
        change = {
            "clause_id": "c-uuid-3",
            "action": "edit",
            "proposed_changes": {
                "clause_text": "New text",
                "_verification": {"status": "verified", "score": 90},
                "_internal": "ignored",
            },
        }
        _apply_pending_change(change)

        payload = mock_supabase.update.call_args[0][0]
        assert "_verification" not in payload
        assert "_internal" not in payload
        assert "clause_text" in payload

    def test_delete_removes_clause(self, mock_supabase):
        change = {"clause_id": "c-uuid-4", "action": "delete", "proposed_changes": None}
        _apply_pending_change(change)

        mock_supabase.delete.assert_called_once()
        mock_supabase.eq.assert_called_with("id", "c-uuid-4")


# ── /admin/pending/<change_id>/approve ───────────────────────────────────────

class TestApprovePendingRoute:

    # Shared base change dict; individual tests override fields as needed.
    _base = {
        "id": "chg-10",
        "clause_id": "c-uuid-10",
        "submitted_by": "other_admin",
        "action": "edit",
        "proposed_changes": {"clause_id": "ART1-1", "clause_text": "Some rule."},
    }

    def test_not_found_returns_redirect_with_error(
        self, client, mock_supabase, mock_session
    ):
        mock_supabase.execute.return_value = MagicMock(data=[], count=0)
        with patch("admin_app.log_audit_event"), \
             patch("admin_app.flash") as mock_flash:
            response = client.post("/admin/pending/nonexistent/approve")

        assert response.status_code == 302
        assert "/admin/pending" in response.headers["Location"]
        mock_flash.assert_called_once_with(
            "Pending change not found or already processed.", "error"
        )

    def test_self_approve_delete_is_blocked(
        self, client, mock_supabase, mock_session
    ):
        change = {**self._base, "submitted_by": "tmasters", "action": "delete"}
        mock_supabase.execute.return_value = MagicMock(data=[change], count=1)
        with patch("admin_app.log_audit_event"), \
             patch("admin_app.flash") as mock_flash, \
             patch("admin_app._apply_pending_change") as mock_apply:
            response = client.post("/admin/pending/chg-10/approve")

        assert response.status_code == 302
        assert "/admin/pending" in response.headers["Location"]
        mock_apply.assert_not_called()
        mock_flash.assert_called_once_with(
            "Deletions cannot be self-approved — a different superuser must approve.",
            "error",
        )

    def test_self_approve_edit_is_allowed(
        self, client, mock_supabase, mock_session
    ):
        change = {**self._base, "submitted_by": "tmasters", "action": "edit"}
        # _apply_pending_change is patched, so only two execute() calls happen:
        # 1. fetch the pending change  2. update its status
        mock_supabase.execute.side_effect = [
            MagicMock(data=[change], count=1),
            MagicMock(data=[], count=0),
        ]
        with patch("admin_app.log_audit_event") as mock_log, \
             patch("admin_app.flash") as mock_flash, \
             patch("admin_app._apply_pending_change") as mock_apply:
            response = client.post("/admin/pending/chg-10/approve")

        assert response.status_code == 302
        assert "/admin/pending" in response.headers["Location"]
        mock_apply.assert_called_once_with(change)
        mock_log.assert_called_once_with(
            action="change_approved",
            clause_id="ART1-1",
            record_id="c-uuid-10",
            notes="self-approved",
        )
        flash_message = mock_flash.call_args[0][0]
        assert "self-approved" in flash_message

    def test_approve_by_different_user_succeeds(
        self, client, mock_supabase, mock_session
    ):
        change = {**self._base, "submitted_by": "other_admin", "action": "create"}
        mock_supabase.execute.side_effect = [
            MagicMock(data=[change], count=1),
            MagicMock(data=[], count=0),
        ]
        with patch("admin_app.log_audit_event") as mock_log, \
             patch("admin_app.flash") as mock_flash, \
             patch("admin_app._apply_pending_change") as mock_apply:
            response = client.post("/admin/pending/chg-10/approve")

        assert response.status_code == 302
        assert "/admin/pending" in response.headers["Location"]
        mock_apply.assert_called_once_with(change)
        mock_log.assert_called_once_with(
            action="change_approved",
            clause_id="ART1-1",
            record_id="c-uuid-10",
            notes=None,
        )
        # Pending change row must be stamped approved by the reviewer
        update_payload = mock_supabase.update.call_args[0][0]
        assert update_payload["status"] == "approved"
        assert update_payload["reviewed_by"] == "tmasters"
        # Flash must not claim self-approval
        flash_message = mock_flash.call_args[0][0]
        assert "self-approved" not in flash_message
