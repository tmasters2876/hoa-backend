"""
Tests for Priority Phase item #11 (backend half): the meta_out contract on
answer_question() and the resident_questions logging in app.py's /ask.

The one catastrophic failure mode — a logging error breaking resident
answers — is covered explicitly.
"""
import os
import pytest
from unittest.mock import MagicMock

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import ask_gpt
import app as app_module


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_clause(clause_id, precedence_level=5, plain_summary="", clause_text="", tags=None):
    return {
        "clause_id": clause_id,
        "document": "TestDoc",
        "page": 1,
        "citation": "Sec. 1",
        "clause_text": clause_text,
        "plain_summary": plain_summary,
        "link": "",
        "precedence_level": precedence_level,
        "tags": tags or [],
    }


def fake_gpt_response(content):
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=content))]
    return resp


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


# ── answer_question() meta_out contract ──────────────────────────────────────

def test_whimsy_sets_meta_and_skips_gpt(monkeypatch):
    # Any GPT/Supabase call would blow up — whimsy must short-circuit both.
    monkeypatch.setattr(ask_gpt, "get_all_clauses",
                        lambda: pytest.fail("whimsy must not fetch clauses"))
    meta = {}
    answer = ask_gpt.answer_question("who made you?", meta_out=meta)
    assert answer
    assert meta["whimsy"] is True
    assert meta["cited_ids"] == []
    assert meta["prefilter_used"] is None
    assert meta["prefilter_clause_count"] is None


def test_meta_captures_only_resolved_cited_ids(monkeypatch):
    clauses = [
        make_clause("FENCE_01", plain_summary="fence height rules"),
        make_clause("SHED_01", plain_summary="shed placement"),
    ]
    monkeypatch.setattr(ask_gpt, "get_all_clauses", lambda: clauses)
    monkeypatch.setattr(
        ask_gpt.client.chat.completions, "create",
        lambda **kw: fake_gpt_response(
            "Fences are limited [FENCE_01]. Also see [HALLUCINATED_99]."),
    )
    meta = {}
    ask_gpt.answer_question("can I build a fence?", meta_out=meta)
    assert meta["whimsy"] is False
    # Hallucinated ID resolves to no clause — excluded from the log.
    assert meta["cited_ids"] == ["FENCE_01"]


def test_meta_prefilter_fallback_logs_false(monkeypatch):
    # Two clauses can never clear min_results=15, so the prefilter falls
    # back to the full corpus and must be logged as prefilter_used=False.
    clauses = [make_clause("A_01"), make_clause("B_01")]
    monkeypatch.setattr(ask_gpt, "get_all_clauses", lambda: clauses)
    monkeypatch.setattr(ask_gpt.client.chat.completions, "create",
                        lambda **kw: fake_gpt_response("No rule found."))
    meta = {}
    ask_gpt.answer_question("something obscure", meta_out=meta)
    assert meta["prefilter_used"] is False
    assert meta["prefilter_clause_count"] == len(clauses)


def test_meta_prefilter_subset_logs_true(monkeypatch):
    # 20 clauses matching "fence" clear min_results=15; 5 unrelated ones
    # are filtered out — a genuine subset, logged as prefilter_used=True.
    matching = [make_clause(f"FENCE_{i:02d}", plain_summary="fence height rules and fence materials")
                for i in range(20)]
    unrelated = [make_clause(f"POOL_{i:02d}", plain_summary="swimming rules") for i in range(5)]
    clauses = matching + unrelated
    monkeypatch.setattr(ask_gpt, "get_all_clauses", lambda: clauses)
    monkeypatch.setattr(ask_gpt.client.chat.completions, "create",
                        lambda **kw: fake_gpt_response("See [FENCE_00]."))
    monkeypatch.setenv("ENABLE_CLAUSE_PREFILTER", "true")
    meta = {}
    ask_gpt.answer_question("what are the fence height rules?", meta_out=meta)
    assert meta["prefilter_used"] is True
    assert meta["prefilter_clause_count"] < len(clauses)


def test_answer_unchanged_when_meta_out_omitted(monkeypatch):
    # Callers that don't pass meta_out (e.g. admin_app's /admin/search)
    # must be completely unaffected.
    clauses = [make_clause("FENCE_01", plain_summary="fence rules")]
    monkeypatch.setattr(ask_gpt, "get_all_clauses", lambda: clauses)
    monkeypatch.setattr(ask_gpt.client.chat.completions, "create",
                        lambda **kw: fake_gpt_response("Yes [FENCE_01]."))
    answer = ask_gpt.answer_question("fences?")
    assert "FENCE" not in answer or answer  # runs without error, returns text
    assert isinstance(answer, str)


# ── /ask logging integration ─────────────────────────────────────────────────

def _fake_answer(question, mode, tags, output_format, meta_out=None):
    if meta_out is not None:
        meta_out.update({
            "whimsy": False,
            "cited_ids": ["FENCE_01"],
            "prefilter_used": True,
            "prefilter_clause_count": 12,
        })
    return "the answer"


def test_ask_inserts_log_row(monkeypatch, client):
    monkeypatch.setattr(app_module, "answer_question", _fake_answer)
    fake_sb = MagicMock()
    monkeypatch.setattr(app_module, "get_supabase_client", lambda: fake_sb)

    resp = client.post("/ask", json={"question": "how tall can my fence be?"})

    assert resp.status_code == 200
    assert resp.get_data(as_text=True) == "the answer"
    row = fake_sb.from_.return_value.insert.call_args[0][0]
    assert row["question"] == "how tall can my fence be?"
    assert row["answer"] == "the answer"
    assert row["cited_clause_ids"] == ["FENCE_01"]
    assert row["prefilter_used"] is True
    assert row["prefilter_clause_count"] == 12
    assert row["whimsy"] is False
    assert "ip" not in row  # owner decision: IP is never stored


def test_logging_failure_never_breaks_ask(monkeypatch, client):
    monkeypatch.setattr(app_module, "answer_question", _fake_answer)
    monkeypatch.setattr(
        app_module, "get_supabase_client",
        lambda: (_ for _ in ()).throw(RuntimeError("supabase down")))

    resp = client.post("/ask", json={"question": "fences?"})

    assert resp.status_code == 200
    assert resp.get_data(as_text=True) == "the answer"


def test_kill_switch_disables_logging(monkeypatch, client):
    monkeypatch.setattr(app_module, "answer_question", _fake_answer)
    monkeypatch.setenv("ENABLE_QUESTION_LOG", "false")
    fake_sb = MagicMock()
    monkeypatch.setattr(app_module, "get_supabase_client", lambda: fake_sb)

    resp = client.post("/ask", json={"question": "fences?"})

    assert resp.status_code == 200
    fake_sb.from_.assert_not_called()


def test_json_output_logs_answer_field(monkeypatch, client):
    def fake_json_answer(question, mode, tags, output_format, meta_out=None):
        if meta_out is not None:
            meta_out.update({"whimsy": False, "cited_ids": [],
                             "prefilter_used": False, "prefilter_clause_count": 2})
        return {"question": question, "answer": "json answer",
                "clauses": [], "mode": mode, "format": "json"}

    monkeypatch.setattr(app_module, "answer_question", fake_json_answer)
    fake_sb = MagicMock()
    monkeypatch.setattr(app_module, "get_supabase_client", lambda: fake_sb)

    resp = client.post("/ask", json={"question": "fences?", "output_format": "json"})

    assert resp.status_code == 200
    row = fake_sb.from_.return_value.insert.call_args[0][0]
    assert row["answer"] == "json answer"  # the text, not the whole dict
