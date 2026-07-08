"""
Tests for Priority Phase items 3-5:
  #5  build_field_diff (pending page diff)
  #1  clause cache TTL
  #19 _score_clauses refactor + filter_relevant_clauses_debug + search test
"""
import os
import pytest
from unittest.mock import MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import ask_gpt
from admin_app import build_field_diff, run_search_test


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


# ── #5 build_field_diff ───────────────────────────────────────────────────────

def test_diff_edit_shows_only_changed_fields():
    change = {
        "action": "edit",
        "original_values": {"citation": "Art. I", "page": 3, "clause_text": "Old."},
        "proposed_changes": {"citation": "Art. II", "page": 3, "clause_text": "Old."},
    }
    diffs = build_field_diff(change)
    assert [d["field"] for d in diffs] == ["citation"]
    assert diffs[0]["old"] == "Art. I" and diffs[0]["new"] == "Art. II"


def test_diff_skips_underscore_metadata():
    change = {
        "action": "edit",
        "original_values": {"citation": "A"},
        "proposed_changes": {"citation": "B", "_verification": {"status": "verified"}},
    }
    assert [d["field"] for d in build_field_diff(change)] == ["citation"]


def test_diff_none_and_empty_string_compare_equal():
    change = {
        "action": "edit",
        "original_values": {"link": None, "citation": "A"},
        "proposed_changes": {"link": "", "citation": "A"},
    }
    assert build_field_diff(change) == []


def test_diff_tags_compare_as_lists_display_joined():
    change = {
        "action": "edit",
        "original_values": {"tags": ["FENCE", "ARC"]},
        "proposed_changes": {"tags": ["FENCE", "ARC", "WALL"]},
    }
    diffs = build_field_diff(change)
    assert len(diffs) == 1
    assert diffs[0]["old"] == "FENCE, ARC"
    assert diffs[0]["new"] == "FENCE, ARC, WALL"

    unchanged = {
        "action": "edit",
        "original_values": {"tags": ["FENCE"]},
        "proposed_changes": {"tags": ["FENCE"]},
    }
    assert build_field_diff(unchanged) == []


def test_diff_create_lists_all_proposed_fields_as_new():
    change = {
        "action": "create",
        "original_values": None,
        "proposed_changes": {"citation": "Art. I", "clause_text": "Text.",
                             "link": "", "_verification": {}},
    }
    diffs = build_field_diff(change)
    assert {d["field"] for d in diffs} == {"citation", "clause_text"}  # empty + _meta skipped
    assert all(d["old"] is None for d in diffs)


def test_diff_delete_returns_empty():
    change = {"action": "delete", "original_values": {"citation": "A"}, "proposed_changes": None}
    assert build_field_diff(change) == []


# ── #1 clause cache TTL ───────────────────────────────────────────────────────

@pytest.fixture
def fresh_cache(monkeypatch):
    """Reset ask_gpt cache globals and patch its supabase client."""
    sb = MagicMock()
    for meth in ("from_", "select", "eq", "range"):
        getattr(sb, meth).return_value = sb
    sb.execute.return_value = MagicMock(data=[make_clause("A_01")])
    monkeypatch.setattr(ask_gpt, "supabase", sb)
    monkeypatch.setattr(ask_gpt, "_clause_cache", None)
    monkeypatch.setattr(ask_gpt, "_cache_loaded_at", 0.0)
    return sb


def test_cache_hit_within_ttl(fresh_cache, monkeypatch):
    monkeypatch.setattr(ask_gpt.time, "time", lambda: 1000.0)
    ask_gpt.get_all_clauses()
    ask_gpt.get_all_clauses()
    assert fresh_cache.execute.call_count == 1  # second call served from cache


def test_cache_expires_after_ttl(fresh_cache, monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(ask_gpt.time, "time", lambda: now["t"])
    ask_gpt.get_all_clauses()
    now["t"] += ask_gpt.CACHE_TTL_SECONDS + 1
    ask_gpt.get_all_clauses()
    assert fresh_cache.execute.call_count == 2  # TTL elapsed -> refetched


def test_invalidate_forces_refetch(fresh_cache, monkeypatch):
    monkeypatch.setattr(ask_gpt.time, "time", lambda: 1000.0)
    ask_gpt.get_all_clauses()
    ask_gpt.invalidate_clause_cache()
    ask_gpt.get_all_clauses()
    assert fresh_cache.execute.call_count == 2


# ── #19 debug filter + search test ───────────────────────────────────────────

def _fence_corpus():
    matching = [make_clause(f"FENCE_{i:02d}", precedence_level=(i % 9) + 1,
                            plain_summary="fence height rules and fence materials")
                for i in range(20)]
    unrelated = [make_clause(f"POOL_{i:02d}", plain_summary="swimming rules") for i in range(5)]
    return matching + unrelated


def test_debug_matches_production_selection():
    corpus = _fence_corpus()
    question = "what are the fence height rules?"
    prod = ask_gpt.filter_relevant_clauses(question, corpus)
    debug = ask_gpt.filter_relevant_clauses_debug(question, corpus)
    assert debug["fallback_triggered"] is False
    assert [c["clause_id"] for _, c in debug["matched"]] == [c["clause_id"] for c in prod]


def test_debug_fallback_matches_production_fallback():
    corpus = [make_clause("A_01"), make_clause("B_01")]
    question = "something entirely unrelated"
    prod = ask_gpt.filter_relevant_clauses(question, corpus)
    debug = ask_gpt.filter_relevant_clauses_debug(question, corpus)
    assert prod == corpus  # production sends full corpus
    assert debug["fallback_triggered"] is True
    assert debug["corpus_size"] == 2


def test_run_search_test_includes_prefilter_preview(monkeypatch):
    corpus = _fence_corpus()
    monkeypatch.setattr(ask_gpt, "get_all_clauses", lambda: corpus)

    sb = MagicMock()
    for meth in ("from_", "select", "or_", "limit", "rpc"):
        getattr(sb, meth).return_value = sb
    sb.execute.return_value = MagicMock(data=[])
    with patch("admin_app.get_supabase_client", return_value=sb), \
         patch("admin_app.generate_embedding", return_value=[0.0] * 1536):
        results = run_search_test("what are the fence height rules?")

    pf = results["prefilter"]
    assert pf is not None
    assert pf["fallback_triggered"] is False
    assert pf["matched_count"] == 20
    assert len(pf["top"]) <= 25
    assert {"score", "clause_id", "document", "citation", "summary", "tags"} <= set(pf["top"][0])
    assert results["prefilter_enabled"] is True


def test_run_search_test_survives_prefilter_failure(monkeypatch):
    # Preview blowing up must not take down the legacy searches.
    monkeypatch.setattr(ask_gpt, "get_all_clauses",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    sb = MagicMock()
    for meth in ("from_", "select", "or_", "limit", "rpc"):
        getattr(sb, meth).return_value = sb
    sb.execute.return_value = MagicMock(data=[])
    with patch("admin_app.get_supabase_client", return_value=sb), \
         patch("admin_app.generate_embedding", return_value=[0.0] * 1536):
        results = run_search_test("fences?")
    assert results["prefilter"] is None
    assert results["vector"] == [] and results["keyword"] == []
