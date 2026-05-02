from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest

from cerebellum.proposer import Hypothesis, Proposer


def _full_proposal_dict(overrides: dict | None = None) -> dict:
    base = {
        "title": "Test hypothesis",
        "description": "A test description",
        "confidence": 0.7,
        "utility": 0.8,
        "cost": 0.05,
        "reversibility": "partial",
        "plan": ["step 1", "step 2"],
        "tools_required": ["http.get"],
        "context_summary": "Test context",
        "evidence_event_ids": ["event-1", "event-2"],
        "causal_argument": "Event-1 caused event-2 which implies action.",
        "metadata": {"source": "test"},
    }
    if overrides:
        base.update(overrides)
    return base


@pytest.fixture
def proposer(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"models": ["openai/gpt-4o"]}))
    p = Proposer(str(config_path))
    try:
        yield p
    finally:
        p._checkpoint_stop.set()
        if p._sqlite:
            p._sqlite.close()


def test_hypothesis_has_grounding_fields():
    h = Hypothesis(
        id="h1",
        timestamp=datetime.now(UTC).isoformat(),
        title="T",
        description="D",
        confidence=0.5,
        utility=0.5,
        generation_cost_usd=0.01,
        estimated_execution_cost_usd=None,
        reversibility="partial",
        plan=["p"],
        tools_required=["t"],
        context_summary="C",
        state="proposed",
        metadata={},
        evidence_event_ids=["e1"],
        causal_argument="Because e1 happened.",
    )
    assert h.evidence_event_ids == ["e1"]
    assert h.causal_argument == "Because e1 happened."
    d = h.to_dict()
    assert "evidence_event_ids" in d
    assert "causal_argument" in d


def test_proposer_rejects_ungrounded_proposals(proposer):
    """Proposals without evidence_event_ids or causal_argument are rejected."""
    # Simulate by checking _coerce_hypothesis + the rejection logic
    item = _full_proposal_dict({"evidence_event_ids": [], "causal_argument": ""})
    h = proposer._coerce_hypothesis(item, [], [])
    assert h is not None
    # The rejection happens in generate_hypotheses, not coerce
    assert not h.evidence_event_ids
    assert not h.causal_argument.strip()


def test_proposer_rejects_ungrounded_in_generate(proposer, caplog):
    """generate_hypotheses rejects ungrounded proposals."""
    # Mock _call_llm to return an ungrounded proposal
    ungrounded = _full_proposal_dict({"evidence_event_ids": [], "causal_argument": ""})
    with mock.patch.object(proposer, "_call_llm", return_value=([ungrounded], {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "model": ""})), caplog.at_level("INFO"):
        result = proposer.generate_hypotheses()
    assert result == []
    assert "ungrounded" in caplog.text


def test_proposer_accepts_grounded_proposals(proposer):
    """generate_hypotheses stores grounded proposals."""
    grounded = _full_proposal_dict()
    with mock.patch.object(proposer, "_call_llm", return_value=([grounded], {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "model": "openai/gpt-4o"})):
        result = proposer.generate_hypotheses()
    assert len(result) == 1
    assert result[0].evidence_event_ids == ["event-1", "event-2"]
    assert result[0].causal_argument == "Event-1 caused event-2 which implies action."


def test_proposer_duplicate_detection_by_title(proposer):
    """Exact title match is a duplicate."""
    h = Hypothesis(
        id="h-new",
        timestamp=datetime.now(UTC).isoformat(),
        title="Existing Title",
        description="D",
        confidence=0.5,
        utility=0.5,
        generation_cost_usd=0.01,
        estimated_execution_cost_usd=None,
        reversibility="partial",
        plan=["p"],
        tools_required=["t"],
        context_summary="C",
        state="proposed",
        metadata={},
        evidence_event_ids=["e1"],
        causal_argument="Arg",
    )
    existing = [{"title": "Existing Title", "evidence_event_ids": ["e2"]}]
    assert proposer._is_duplicate(h, existing) is True


def test_proposer_duplicate_detection_by_evidence_subset(proposer):
    """Same evidence subset + similar action (Jaccard > 0.8) is a duplicate."""
    h = Hypothesis(
        id="h-new",
        timestamp=datetime.now(UTC).isoformat(),
        title="Fix API timeout",
        description="D",
        confidence=0.5,
        utility=0.5,
        generation_cost_usd=0.01,
        estimated_execution_cost_usd=None,
        reversibility="partial",
        plan=["p"],
        tools_required=["t"],
        context_summary="C",
        state="proposed",
        metadata={},
        evidence_event_ids=["e1"],
        causal_argument="Arg",
    )
    existing = [{"title": "Fix API timeout error", "evidence_event_ids": ["e1", "e2"]}]
    assert proposer._is_duplicate(h, existing) is True


def test_proposer_not_duplicate_different_evidence(proposer):
    """Different evidence + different title is not a duplicate."""
    h = Hypothesis(
        id="h-new",
        timestamp=datetime.now(UTC).isoformat(),
        title="Deploy new service",
        description="D",
        confidence=0.5,
        utility=0.5,
        generation_cost_usd=0.01,
        estimated_execution_cost_usd=None,
        reversibility="partial",
        plan=["p"],
        tools_required=["t"],
        context_summary="C",
        state="proposed",
        metadata={},
        evidence_event_ids=["e10", "e11"],
        causal_argument="Arg",
    )
    existing = [{"title": "Fix API timeout", "evidence_event_ids": ["e1", "e2"]}]
    assert proposer._is_duplicate(h, existing) is False


def test_proposer_daily_cap_blocks_generation(proposer):
    """After 50 proposals, generation is blocked."""
    proposer._daily_proposal_count = 50
    proposer._daily_proposal_count_day = datetime.now(UTC).date().isoformat()
    result = proposer.generate_hypotheses()
    assert result == []


def test_proposer_pause_after_cap(proposer):
    """After cap triggered, proposer is paused for 12 hours."""
    proposer._daily_proposal_count = 50
    proposer._daily_proposal_count_day = datetime.now(UTC).date().isoformat()
    proposer._last_cap_triggered = None
    proposer._trigger_proposal_cap_if_needed()
    assert proposer._last_cap_triggered is not None
    assert proposer._can_generate_hypotheses() is False


def test_proposer_resume_after_pause(proposer):
    """After 12h pause, proposer can generate again (new day resets count)."""
    proposer._daily_proposal_count = 50
    proposer._daily_proposal_count_day = datetime.now(UTC).date().isoformat()
    proposer._last_cap_triggered = datetime.now(UTC) - timedelta(hours=13)
    # New day resets count to 0, which clears the cap
    proposer._daily_proposal_count_day = "1900-01-01"
    assert proposer._can_generate_hypotheses() is True


def test_proposer_store_and_retrieve(proposer):
    """Store a hypothesis and retrieve it."""
    h = Hypothesis(
        id="store-test-1",
        timestamp=datetime.now(UTC).isoformat(),
        title="Stored",
        description="D",
        confidence=0.5,
        utility=0.5,
        generation_cost_usd=0.01,
        estimated_execution_cost_usd=None,
        reversibility="partial",
        plan=["p"],
        tools_required=["t"],
        context_summary="C",
        state="proposed",
        metadata={},
        evidence_event_ids=["e1"],
        causal_argument="Arg",
    )
    assert proposer._store_hypothesis(h) is True
    retrieved = proposer.get_hypothesis("store-test-1")
    assert retrieved is not None
    assert retrieved["title"] == "Stored"
    assert retrieved["evidence_event_ids"] == ["e1"]
    assert retrieved["causal_argument"] == "Arg"


def test_proposer_update_state(proposer):
    """Update hypothesis state tracks transitions."""
    h = Hypothesis(
        id="state-test-1",
        timestamp=datetime.now(UTC).isoformat(),
        title="State",
        description="D",
        confidence=0.5,
        utility=0.5,
        generation_cost_usd=0.01,
        estimated_execution_cost_usd=None,
        reversibility="partial",
        plan=["p"],
        tools_required=["t"],
        context_summary="C",
        state="proposed",
        metadata={},
        evidence_event_ids=[],
        causal_argument="",
    )
    proposer._store_hypothesis(h)
    assert proposer.update_hypothesis_state("state-test-1", "staged", "test reason") is True
    retrieved = proposer.get_hypothesis("state-test-1")
    assert retrieved["state"] == "staged"
    transitions = retrieved["metadata"]["state_transitions"]
    assert len(transitions) == 1
    assert transitions[0]["from"] == "proposed"
    assert transitions[0]["to"] == "staged"


def test_proposer_invalid_state_rejected(proposer):
    assert proposer.update_hypothesis_state("nonexistent", "invalid_state") is False


def test_proposer_expire_old_hypotheses(proposer):
    """Old proposed hypotheses are expired."""
    old_ts = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    h = Hypothesis(
        id="expire-test-1",
        timestamp=old_ts,
        title="Old",
        description="D",
        confidence=0.5,
        utility=0.5,
        generation_cost_usd=0.01,
        estimated_execution_cost_usd=None,
        reversibility="partial",
        plan=["p"],
        tools_required=["t"],
        context_summary="C",
        state="proposed",
        metadata={},
        evidence_event_ids=[],
        causal_argument="",
    )
    proposer._store_hypothesis(h)
    expired = proposer.expire_old_hypotheses(max_age_hours=24)
    assert expired == 1
    retrieved = proposer.get_hypothesis("expire-test-1")
    assert retrieved["state"] == "expired"


def test_proposer_get_stats(proposer):
    stats = proposer.get_hypothesis_stats()
    assert "counts_by_state" in stats
    assert "total" in stats
    assert "avg_confidence" in stats


def test_proposer_parse_hypothesis_response_valid(proposer):
    """Parse valid LLM JSON response."""
    response = json.dumps([_full_proposal_dict()])
    result = proposer._parse_hypothesis_response(response, "openai/gpt-4o")
    assert result is not None
    assert len(result) == 1
    assert result[0]["evidence_event_ids"] == ["event-1", "event-2"]


def test_proposer_parse_hypothesis_response_invalid_json(proposer):
    result = proposer._parse_hypothesis_response("not json", "model")
    assert result is None


def test_proposer_parse_hypothesis_response_missing_keys(proposer):
    partial = {"title": "T", "evidence_event_ids": [], "causal_argument": ""}
    result = proposer._parse_hypothesis_response(json.dumps([partial]), "model")
    assert result is None


def test_proposer_clean_json_content_strips_fences(proposer):
    content = "```json\n[1,2,3]\n```"
    cleaned = proposer._clean_json_content(content)
    assert cleaned == "[1,2,3]"


def test_proposer_derive_cost(proposer):
    cost = proposer._derive_cost(1000, 500, model="openai/gpt-4o")
    assert 0.0 <= cost <= 1.0


def test_proposer_derive_cost_unknown_model(proposer):
    cost = proposer._derive_cost(1000, 500, model="unknown/model")
    assert cost > 0


def test_proposer_clamp_float(proposer):
    assert proposer._clamp_float(1.5) == 1.0
    assert proposer._clamp_float(-0.5) == 0.0
    assert proposer._clamp_float(0.5) == 0.5
    assert proposer._clamp_float("invalid") == 0.0


def test_proposer_jaccard(proposer):
    a = {"fix", "api", "timeout"}
    b = {"fix", "api", "error"}
    j = proposer._jaccard(a, b)
    assert 0.0 < j < 1.0
    assert proposer._jaccard(set(), {"x"}) == 0.0


def test_proposer_tokenizer(proposer):
    tokens = proposer._tokenize_for_dedup("Fix API timeout!")
    assert "fix" in tokens
    assert "api" in tokens
    assert "timeout" in tokens


def test_proposer_coerce_hypothesis_minimal(proposer):
    """Coerce a minimal proposal with grounding fields."""
    item = _full_proposal_dict()
    h = proposer._coerce_hypothesis(item, [], [])
    assert h is not None
    assert h.evidence_event_ids == ["event-1", "event-2"]
    assert h.causal_argument == "Event-1 caused event-2 which implies action."


def test_proposer_coerce_hypothesis_deduplicates_evidence(proposer):
    """Evidence IDs are deduplicated and capped at 5."""
    item = _full_proposal_dict({"evidence_event_ids": ["e1", "e1", "e2", "e3", "e4", "e5", "e6"]})
    h = proposer._coerce_hypothesis(item, [], [])
    assert h is not None
    assert len(h.evidence_event_ids) <= 5
    assert "e1" in h.evidence_event_ids


def test_proposer_build_prompt_includes_grounding_rules(proposer):
    prompt = proposer._build_prompt(episodes=[], events=[], existing=[])
    assert "evidence_event_ids" in prompt
    assert "causal_argument" in prompt


def test_proposer_get_active_hypotheses_includes_grounding(proposer):
    h = Hypothesis(
        id="active-test-1",
        timestamp=datetime.now(UTC).isoformat(),
        title="Active",
        description="D",
        confidence=0.5,
        utility=0.5,
        generation_cost_usd=0.01,
        estimated_execution_cost_usd=None,
        reversibility="partial",
        plan=["p"],
        tools_required=["t"],
        context_summary="C",
        state="proposed",
        metadata={},
        evidence_event_ids=["e1"],
        causal_argument="Arg",
    )
    proposer._store_hypothesis(h)
    active = proposer.get_active_hypotheses(state="proposed")
    assert len(active) >= 1
    found = [a for a in active if a["id"] == "active-test-1"]
    assert len(found) == 1
    assert found[0]["evidence_event_ids"] == ["e1"]
    assert found[0]["causal_argument"] == "Arg"


def test_proposer_no_api_key_disables_llm(proposer):
    """Without API key, _call_llm returns empty."""
    result, usage = proposer._call_llm("test prompt")
    assert result == []
    assert usage["total_tokens"] == 0


def test_proposer_extract_usage(proposer):
    """Extract usage from mock response."""
    class MockUsage:
        prompt_tokens = 100
        completion_tokens = 50
        total_tokens = 150

    class MockResponse:
        usage = MockUsage()
        model = "openai/gpt-4o"

    usage = proposer._extract_usage(MockResponse(), model="openai/gpt-4o")
    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 50
    assert usage["total_tokens"] == 150


def test_proposer_extract_message_content_string(proposer):
    class MockMsg:
        content = "hello"

    class MockChoice:
        message = MockMsg()

    class MockResponse:
        choices = [MockChoice()]

    content = proposer._extract_message_content(MockResponse())
    assert content == "hello"


def test_proposer_extract_message_content_list(proposer):
    class MockMsg:
        content = [{"type": "text", "text": "hello"}, {"type": "text", "text": " world"}]

    class MockChoice:
        message = MockMsg()

    class MockResponse:
        choices = [MockChoice()]

    content = proposer._extract_message_content(MockResponse())
    assert content == "hello world"


def test_proposer_redact_openrouter_error(proposer):
    # Proposer has no API key (deleted in fixture), so no redaction
    msg = proposer._redact_openrouter_error(Exception("some error"))
    assert "some error" in msg


def test_proposer_remaining_slots(proposer):
    proposer._daily_proposal_count = 0
    proposer._daily_proposal_count_day = datetime.now(UTC).date().isoformat()
    assert proposer._remaining_daily_proposal_slots() == 50

    proposer._daily_proposal_count = 48
    assert proposer._remaining_daily_proposal_slots() == 2

    proposer._daily_proposal_count = 50
    assert proposer._remaining_daily_proposal_slots() == 0


def test_proposer_record_generated_proposals_triggers_cap(proposer):
    proposer._daily_proposal_count = 49
    proposer._daily_proposal_count_day = datetime.now(UTC).date().isoformat()
    proposer._last_cap_triggered = None
    proposer._record_generated_proposals(1)
    assert proposer._last_cap_triggered is not None

def test_proposer_load_config_missing_file(proposer, tmp_path):
    """_load_config returns empty dict when config file missing."""
    p2 = Proposer.__new__(Proposer)
    p2.config_path = tmp_path / "nonexistent.json"
    result = p2._load_config()
    assert result == {}

def test_proposer_load_config_invalid_json(proposer, tmp_path):
    """_load_config returns empty dict on parse error."""
    bad = tmp_path / "bad.json"
    bad.write_text("{invalid json}")
    p2 = Proposer.__new__(Proposer)
    p2.config_path = bad
    result = p2._load_config()
    assert result == {}

def test_proposer_build_client_no_openai(proposer):
    """_build_client returns None when openai not installed."""
    with (mock.patch.object(proposer, "api_key", "fake-key"),
         mock.patch("cerebellum.proposer.OpenAI", None)):
        result = proposer._build_client()
        assert result is None

def test_proposer_get_connection_uninitialized():
    """_get_connection raises RuntimeError before init."""
    p = Proposer.__new__(Proposer)
    p._sqlite = None
    with pytest.raises(RuntimeError, match="not initialized"):
        p._get_connection()

def test_proposer_build_prompt_exception_fallback(proposer):
    """_build_prompt returns safe fallback on exception."""
    bad = type("Bad", (), {"__iter__": lambda s: 1/0})()
    prompt = proposer._build_prompt(episodes=[bad], events=[], existing=[])
    assert "[]" in prompt

def test_proposer_extract_usage_malformed(proposer):
    """_extract_usage handles malformed usage gracefully."""
    class BadUsage:
        prompt_tokens = "not_a_number"
        completion_tokens = None
        total_tokens = None

    class MockResp:
        usage = BadUsage()
        model = "test-model"

    usage = proposer._extract_usage(MockResp(), model="test-model")
    assert usage["prompt_tokens"] == 0
    assert usage["model"] == "test-model"

def test_proposer_parse_response_dict_with_hypotheses(proposer):
    """Parse dict response with hypotheses key."""
    resp = json.dumps({"hypotheses": [_full_proposal_dict()]})
    result = proposer._parse_hypothesis_response(resp, "model")
    assert result is not None
    assert len(result) == 1

def test_proposer_parse_response_dict_missing_key(proposer):
    """Parse dict response missing hypotheses key returns None."""
    resp = json.dumps({"other": "data"})
    result = proposer._parse_hypothesis_response(resp, "model")
    assert result is None

def test_proposer_parse_response_non_list_non_dict(proposer):
    """Parse string payload returns None."""
    resp = json.dumps("just a string")
    result = proposer._parse_hypothesis_response(resp, "model")
    assert result is None

def test_proposer_parse_response_non_dict_item(proposer):
    """Parse list with non-dict item returns None."""
    resp = json.dumps([1, 2, 3])
    result = proposer._parse_hypothesis_response(resp, "model")
    assert result is None

def test_proposer_parse_response_plan_not_list(proposer):
    """Reject hypothesis with plan as string."""
    item = _full_proposal_dict({"plan": "not a list"})
    resp = json.dumps([item])
    result = proposer._parse_hypothesis_response(resp, "model")
    assert result is None

def test_proposer_parse_response_tools_not_list(proposer):
    """Reject hypothesis with tools_required as string."""
    item = _full_proposal_dict({"tools_required": "not a list"})
    resp = json.dumps([item])
    result = proposer._parse_hypothesis_response(resp, "model")
    assert result is None

def test_proposer_parse_response_evidence_not_list(proposer):
    """Reject hypothesis with evidence_event_ids as string."""
    item = _full_proposal_dict({"evidence_event_ids": "not a list"})
    resp = json.dumps([item])
    result = proposer._parse_hypothesis_response(resp, "model")
    assert result is None

def test_proposer_parse_response_causal_not_string(proposer):
    """Reject hypothesis with causal_argument as list."""
    item = _full_proposal_dict({"causal_argument": [1, 2]})
    resp = json.dumps([item])
    result = proposer._parse_hypothesis_response(resp, "model")
    assert result is None

def test_proposer_parse_response_metadata_not_dict(proposer):
    """Reject hypothesis with metadata as list."""
    item = _full_proposal_dict({"metadata": [1, 2]})
    resp = json.dumps([item])
    result = proposer._parse_hypothesis_response(resp, "model")
    assert result is None

def test_proposer_extract_message_content_list_objects(proposer):
    """Extract content from list of objects with text attr."""
    class TextObj:
        text = "hello"

    class MockMsg:
        content = [TextObj()]

    class MockChoice:
        message = MockMsg()

    class MockResponse:
        choices = [MockChoice()]

    content = proposer._extract_message_content(MockResponse())
    assert content == "hello"

def test_proposer_extract_message_content_fallback_string(proposer):
    """Extract content falls back to str() for non-str non-list."""
    class MockMsg:
        content = 123

    class MockChoice:
        message = MockMsg()

    class MockResponse:
        choices = [MockChoice()]

    content = proposer._extract_message_content(MockResponse())
    assert content == "123"

def test_proposer_extract_message_content_type_error(proposer):
    """Extract content raises ValueError on TypeError."""
    class BadMessage:
        def __getattr__(self, name):
            raise TypeError("no attr")

    class MockChoice:
        message = BadMessage()

    class MockResponse:
        choices = [MockChoice()]

    with pytest.raises(ValueError, match="Unable to extract"):
        proposer._extract_message_content(MockResponse())

def test_proposer_clean_json_content_partial_fences(proposer):
    """Clean JSON handles partial fence (no closing)."""
    content = "```json\n[1,2,3]"
    cleaned = proposer._clean_json_content(content)
    assert "[1,2,3]" in cleaned

def test_proposer_get_active_no_state(proposer):
    """get_active_hypotheses without state filter."""
    h = Hypothesis(
        id="no-state-1",
        timestamp=datetime.now(UTC).isoformat(),
        title="T",
        description="D",
        confidence=0.5,
        utility=0.5,
        generation_cost_usd=0.01,
        estimated_execution_cost_usd=None,
        reversibility="partial",
        plan=["p"],
        tools_required=["t"],
        context_summary="C",
        state="proposed",
        metadata={},
        evidence_event_ids=[],
        causal_argument="",
    )
    proposer._store_hypothesis(h)
    all_h = proposer.get_active_hypotheses()
    assert len(all_h) >= 1

def test_proposer_get_hypothesis_not_found(proposer):
    """get_hypothesis returns None for unknown ID."""
    result = proposer.get_hypothesis("nonexistent-id")
    assert result is None

def test_proposer_update_state_nonexistent(proposer):
    """update_hypothesis_state returns False for unknown ID."""
    result = proposer.update_hypothesis_state("nonexistent", "staged")
    assert result is False

def test_proposer_expire_no_old(proposer):
    """expire_old_hypotheses returns 0 when nothing to expire."""
    expired = proposer.expire_old_hypotheses()
    assert expired == 0

def test_proposer_normalize_records(proposer):
    """_normalize_records handles various input types."""
    assert proposer._normalize_records(None) == []
    assert proposer._normalize_records({"episodes": [{"a": 1}]}) == [{"a": 1}]
    assert proposer._normalize_records({"events": [{"b": 2}]}) == [{"b": 2}]
    assert proposer._normalize_records({"c": 3}) == [{"c": 3}]
    assert proposer._normalize_records([{"d": 4}]) == [{"d": 4}]
    assert proposer._normalize_records("scalar") == [{"value": "scalar"}]

def test_proposer_json_safe_with_to_dict(proposer):
    """_json_safe uses to_dict() method when available."""
    class HasToDict:
        def to_dict(self):
            return {"x": 1}
    result = proposer._json_safe(HasToDict())
    assert result == {"x": 1}

def test_proposer_json_safe_with_dict_attr(proposer):
    """_json_safe falls back to __dict__ when no to_dict."""
    class HasDict:
        def __init__(self):
            self.x = 1
    result = proposer._json_safe(HasDict())
    assert result == {"x": 1}

def test_proposer_coerce_json_value_datetime(proposer):
    """_coerce_json_value converts datetime to ISO string."""
    dt = datetime(2025, 1, 1, 12, 0, 0)
    result = proposer._coerce_json_value(dt)
    assert result == "2025-01-01T12:00:00"

def test_proposer_coerce_json_value_list(proposer):
    """_coerce_json_value recurses into lists."""
    result = proposer._coerce_json_value([1, "a", None])
    assert result == [1, "a", None]

def test_proposer_coerce_json_value_dict(proposer):
    """_coerce_json_value recurses into dicts."""
    result = proposer._coerce_json_value({"a": 1, "b": "x"})
    assert result == {"a": 1, "b": "x"}

def test_proposer_coerce_json_value_fallback(proposer):
    """_coerce_json_value falls back to str() for unknown types."""
    result = proposer._coerce_json_value({1, 2})
    assert isinstance(result, str)

def test_proposer_fallback_context_summary(proposer):
    """_fallback_context_summary returns formatted string."""
    summary = proposer._fallback_context_summary([1, 2], [3])
    assert "2 recent episodes" in summary
    assert "1 recent events" in summary

def test_proposer_generate_returns_empty_on_cap(proposer):
    """generate_hypotheses returns [] when cap reached."""
    proposer._daily_proposal_count = 50
    proposer._daily_proposal_count_day = datetime.now(UTC).date().isoformat()
    result = proposer.generate_hypotheses()
    assert result == []

def test_proposer_coerce_returns_none_on_invalid(proposer):
    """_coerce_hypothesis returns None for invalid payload."""
    h = proposer._coerce_hypothesis("not a dict", [], [])
    assert h is None

def test_proposer_row_to_dict_handles_missing_cost(proposer):
    """_row_to_dict handles legacy rows with cost column."""
    class FakeRow(dict):
        pass
    row = FakeRow({
        "id": "r1", "timestamp": "2025-01-01", "title": "T",
        "description": "D", "confidence": 0.5, "utility": 0.5,
        "cost": 0.03, "generation_cost_usd": None,
        "estimated_execution_cost_usd": None,
        "reversibility": "partial", "plan": "[]",
        "tools_required": "[]", "context_summary": "C",
        "evidence_event_ids": "[]", "causal_argument": "",
        "state": "proposed", "metadata": "{}",
    })
    result = proposer._row_to_dict(row)
    assert result["generation_cost_usd"] == 0.03

def test_proposer_parse_json_column_malformed(proposer):
    """_parse_json_column returns fallback on corrupt JSON."""
    result = proposer._parse_json_column("{bad}", [])
    assert result == []

def test_proposer_emit_event_no_emitter(proposer):
    """_emit_event is a no-op when emitter is None."""
    proposer._emit_event("test.event", {"key": "val"})
    # Should not raise

def test_proposer_emit_event_with_emitter(proposer):
    """_emit_event calls emitter.emit when available."""
    mock_emitter = mock.Mock()
    proposer.emitter = mock_emitter
    proposer._emit_event("test.event", {"key": "val"})
    mock_emitter.emit.assert_called_once()

def test_proposer_emit_event_fallback_publish(proposer):
    """_emit_event falls back to publish() if emit() missing."""
    mock_emitter = mock.Mock(spec=["publish"])
    proposer.emitter = mock_emitter
    proposer._emit_event("test.event", {"key": "val"})
    mock_emitter.publish.assert_called_once()

def test_proposer_schema_migration_adds_columns(proposer):
    """_ensure_hypothesis_schema adds missing columns."""
    with proposer._db_lock:
        conn = proposer._get_connection()
    # Columns already exist from __init__, so this just verifies no error
    proposer._ensure_hypothesis_schema(conn)
    # No exception means success

def test_proposer_record_zero_count_no_op(proposer):
    """_record_generated_proposals(0) is a no-op."""
    proposer._daily_proposal_count = 10
    proposer._record_generated_proposals(0)
    assert proposer._daily_proposal_count == 10

def test_proposer_cap_blocks_generation(proposer):
    """_can_generate_hypotheses blocks when at cap & no pause expiry."""
    proposer._daily_proposal_count = 50
    proposer._daily_proposal_count_day = datetime.now(UTC).date().isoformat()
    proposer._last_cap_triggered = datetime.now(UTC)
    assert proposer._can_generate_hypotheses() is False

def test_proposer_duplicate_by_exact_title(proposer):
    """Duplicate detection catches exact title match."""
    h = Hypothesis(
        id="h-dup",
        timestamp=datetime.now(UTC).isoformat(),
        title="Exact Match",
        description="D",
        confidence=0.5,
        utility=0.5,
        generation_cost_usd=0.01,
        estimated_execution_cost_usd=None,
        reversibility="partial",
        plan=["p"],
        tools_required=["t"],
        context_summary="C",
        state="proposed",
        metadata={},
        evidence_event_ids=[],
        causal_argument="",
    )
    existing = [{"title": "Exact Match", "evidence_event_ids": []}]
    assert proposer._is_duplicate(h, existing) is True

def test_proposer_not_duplicate_different(proposer):
    """Non-duplicate when titles and evidence differ."""
    h = Hypothesis(
        id="h-unique",
        timestamp=datetime.now(UTC).isoformat(),
        title="Completely Different",
        description="D",
        confidence=0.5,
        utility=0.5,
        generation_cost_usd=0.01,
        estimated_execution_cost_usd=None,
        reversibility="partial",
        plan=["p"],
        tools_required=["t"],
        context_summary="C",
        state="proposed",
        metadata={},
        evidence_event_ids=["unique-e"],
        causal_argument="",
    )
    existing = [{"title": "Something Else", "evidence_event_ids": ["other-e"]}]
    assert proposer._is_duplicate(h, existing) is False

def test_proposer_get_stats_empty_db(proposer):
    """get_hypothesis_stats works on empty DB."""
    stats = proposer.get_hypothesis_stats()
    assert stats["total"] == 0
    assert stats["counts_by_state"] == {}
    assert stats["avg_confidence"] == 0.0

def test_proposer_generate_exception_returns_empty(proposer):
    """generate_hypotheses catches exceptions and returns []."""
    with mock.patch.object(proposer, "_get_recent_episodes", side_effect=RuntimeError("boom")):
        result = proposer.generate_hypotheses()
        assert result == []

def test_proposer_extract_usage_type_error(proposer):
    """_extract_usage handles TypeError in usage extraction."""
    class BadInt:
        def __int__(self):
            raise TypeError("bad int")

    class BadUsage:
        prompt_tokens = BadInt()
        completion_tokens = 0
        total_tokens = 0

    class MockResp:
        usage = BadUsage()
        model = "test"

    usage = proposer._extract_usage(MockResp(), model="test")
    assert usage["prompt_tokens"] == 0

def test_proposer_jaccard_identical_sets(proposer):
    """Jaccard of identical sets is 1.0."""
    s = {"a", "b", "c"}
    assert proposer._jaccard(s, s) == 1.0

def test_proposer_jaccard_disjoint_sets(proposer):
    """Jaccard of disjoint sets is 0.0."""
    a = {"a", "b"}
    b = {"c", "d"}
    assert proposer._jaccard(a, b) == 0.0

def test_proposer_clamp_float_edge_cases(proposer):
    """_clamp_float handles NaN and Infinity."""
    assert proposer._clamp_float(float("inf")) == 1.0
    assert proposer._clamp_float(float("-inf")) == 0.0
    assert proposer._clamp_float(float("nan")) >= 0.0

def test_proposer_derive_cost_zero_tokens(proposer):
    """_derive_cost returns 0.0 for zero tokens."""
    cost = proposer._derive_cost(0, 0)
    assert cost == 0.0

def test_proposer_build_client_with_key(proposer, monkeypatch):
    """_build_client succeeds with valid API key."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    p2 = Proposer.__new__(Proposer)
    p2.api_key = "test-key"
    p2.site_url = "https://test.local"
    p2.app_name = "Test"
    p2.openrouter_base_url = "https://openrouter.ai/api/v1"
    client = p2._build_client()
    assert client is not None

def test_proposer_load_config_success(proposer, tmp_path):
    """_load_config returns parsed JSON on success."""
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"models": ["test"]}))
    p2 = Proposer.__new__(Proposer)
    p2.config_path = cfg
    result = p2._load_config()
    assert result == {"models": ["test"]}

def test_proposer_redact_keeps_key(proposer, monkeypatch):
    """_redact_openrouter_error redacts API key from message."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret-key-123")
    p2 = Proposer.__new__(Proposer)
    p2.api_key = "secret-key-123"
    msg = p2._redact_openrouter_error(Exception("error with secret-key-123 inside"))
    assert "secret-key-123" not in msg
    assert "sk-or-v1-***" in msg

def test_proposer_update_state_successive_transitions(proposer):
    """Multiple state transitions are tracked in metadata."""
    h = Hypothesis(
        id="multi-trans-1",
        timestamp=datetime.now(UTC).isoformat(),
        title="T",
        description="D",
        confidence=0.5,
        utility=0.5,
        generation_cost_usd=0.01,
        estimated_execution_cost_usd=None,
        reversibility="partial",
        plan=["p"],
        tools_required=["t"],
        context_summary="C",
        state="proposed",
        metadata={},
        evidence_event_ids=[],
        causal_argument="",
    )
    proposer._store_hypothesis(h)
    proposer.update_hypothesis_state("multi-trans-1", "staged", "reason1")
    proposer.update_hypothesis_state("multi-trans-1", "executed", "reason2")
    retrieved = proposer.get_hypothesis("multi-trans-1")
    assert retrieved["state"] == "executed"
    transitions = retrieved["metadata"]["state_transitions"]
    assert len(transitions) == 2
    assert transitions[0]["to"] == "staged"
    assert transitions[1]["to"] == "executed"

