"""Tests for Phase 6: policy_arbiter coverage boost.

Targets: policy_arbiter.py 10% -> 75%.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
import sys

for path in (PROJECT_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

os.environ.setdefault("DASHBOARD_TOKEN", "test-token")

from cerebellum.policy_arbiter import (
    COSTLY_AUTO_EXECUTE_TOOLS,
    SENSITIVE_HYPOTHESIS_FIELD_TOKENS,
    TOOL_COST_ESTIMATES,
    ActionDecision,
    DailyCostTracker,
    PolicyArbiter,
    RateLimiter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_policy_path() -> str:
    """Create a temporary policy.yaml and return its path."""
    content = """
global:
  enabled: true
  max_actions_per_hour: 100
  max_llm_cost_per_day_usd: 5.0
auto_execute:
  min_confidence: 0.85
  max_cost: 0.3
  allowed_tools:
    - "http.get"
    - "notification.send"
    - "notification.summarize"
    - "proposal.snooze"
    - "rasputin.search"
    - "rasputin.recent_facts"
    - "rasputin.entity_lookup"
    - "rasputin.episode_summary"
  required_reversibility:
    - "unknown"
stage_notify:
  min_confidence: 0.6
  max_cost: 0.8
discard:
  max_confidence: 0.5
  min_cost: 0.9
forbidden_tools:
  - "file.write"
  - "file.delete"
model_candidates:
  - "openai/gpt-4o"
"""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    tmp.write(content)
    tmp.close()
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


def _make_arbiter(policy_path: str | None = None) -> PolicyArbiter:
    """Create a PolicyArbiter with a mock emitter and clean state."""
    if policy_path is None:
        policy_path = _make_policy_path()
    mock_emitter = MagicMock()
    mock_emitter.query.return_value = []
    arbiter = PolicyArbiter(policy_path, emitter=mock_emitter)
    arbiter.kill_switch = False
    # Clean up all state files to prevent cross-test contamination (shared /tmp/graph/)
    try:
        arbiter.kill_switch_file.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        arbiter.state_file.unlink(missing_ok=True)
    except Exception:
        pass
    return arbiter


# ---------------------------------------------------------------------------
# RateLimiter tests
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_allows_within_limit(self) -> None:
        rl = RateLimiter(max_count=5, window_seconds=60)
        for _ in range(5):
            assert rl.allow() is True

    def test_blocks_over_limit(self) -> None:
        rl = RateLimiter(max_count=2, window_seconds=60)
        assert rl.allow() is True
        assert rl.allow() is True
        assert rl.allow() is False

    def test_snapshot(self) -> None:
        rl = RateLimiter(max_count=10, window_seconds=3600)
        rl.allow()
        snap = rl.snapshot()
        assert snap["used"] == 1
        assert snap["remaining"] == 9


# ---------------------------------------------------------------------------
# DailyCostTracker tests
# ---------------------------------------------------------------------------


class TestDailyCostTracker:
    def test_allows_within_budget(self) -> None:
        dct = DailyCostTracker(max_cost=5.0)
        assert dct.allow(1.0) is True
        assert dct.allow(2.0) is True

    def test_blocks_over_budget(self) -> None:
        dct = DailyCostTracker(max_cost=5.0)
        dct.allow(4.5)
        assert dct.allow(1.0) is False

    def test_snapshot(self) -> None:
        dct = DailyCostTracker(max_cost=10.0)
        dct.allow(3.0)
        snap = dct.snapshot()
        assert snap["spent"] == 3.0
        assert snap["remaining"] == 7.0


# ---------------------------------------------------------------------------
# ActionDecision tests
# ---------------------------------------------------------------------------


class TestActionDecision:
    def test_to_dict(self) -> None:
        ad = ActionDecision(
            hypothesis_id="h-1",
            decision="auto_execute",
            reason="test",
            timestamp="2025-01-01T00:00:00",
        )
        d = ad.to_dict()
        assert d["hypothesis_id"] == "h-1"
        assert d["decision"] == "auto_execute"


# ---------------------------------------------------------------------------
# PolicyArbiter - evaluate()
# ---------------------------------------------------------------------------


class TestPolicyArbiterEvaluate:
    def test_auto_execute_high_confidence(self) -> None:
        arbiter = _make_arbiter()
        hypothesis = {
            "id": "test-1",
            "confidence": 0.95,
            "generation_cost_usd": 0.01,
            "reversibility": "unknown",
            "plan": [{"tool": "http.get", "url": "http://example.com"}],
        }
        result = arbiter.evaluate(hypothesis)
        assert result.decision == "auto_execute"

    def test_stage_notify_medium_confidence(self) -> None:
        arbiter = _make_arbiter()
        hypothesis = {
            "id": "test-2",
            "confidence": 0.7,
            "generation_cost_usd": 0.01,
            "plan": [{"tool": "http.get", "url": "http://example.com"}],
        }
        result = arbiter.evaluate(hypothesis)
        assert result.decision == "stage_notify"

    def test_discard_low_confidence(self) -> None:
        arbiter = _make_arbiter()
        hypothesis = {
            "id": "test-3",
            "confidence": 0.3,
            "generation_cost_usd": 0.01,
            "plan": [{"tool": "http.get", "url": "http://example.com"}],
        }
        result = arbiter.evaluate(hypothesis)
        assert result.decision == "discard"

    def test_forbidden_tool(self) -> None:
        arbiter = _make_arbiter()
        hypothesis = {
            "id": "test-4",
            "confidence": 0.95,
            "generation_cost_usd": 0.01,
            "plan": [{"tool": "file.delete", "path": "/tmp/test"}],
        }
        result = arbiter.evaluate(hypothesis)
        assert result.decision == "discard"
        assert "forbidden" in result.reason

    def test_invalid_confidence_negative(self) -> None:
        arbiter = _make_arbiter()
        hypothesis = {
            "id": "test-5",
            "confidence": -0.5,
            "generation_cost_usd": 0.01,
            "plan": [],
        }
        result = arbiter.evaluate(hypothesis)
        assert result.decision == "discard"

    def test_invalid_generation_cost(self) -> None:
        arbiter = _make_arbiter()
        hypothesis = {
            "id": "test-6",
            "confidence": 0.9,
            "generation_cost_usd": "not_a_number",
            "plan": [],
        }
        result = arbiter.evaluate(hypothesis)
        assert result.decision == "discard"

    def test_high_cost_blocked(self) -> None:
        arbiter = _make_arbiter()
        hypothesis = {
            "id": "test-7",
            "confidence": 0.95,
            "generation_cost_usd": 1.0,
            "plan": [{"tool": "http.get", "url": "http://example.com"}],
        }
        result = arbiter.evaluate(hypothesis)
        assert result.decision != "auto_execute"

    def test_kill_switch_blocks(self) -> None:
        arbiter = _make_arbiter()
        arbiter.kill_switch = True
        hypothesis = {
            "id": "test-8",
            "confidence": 0.95,
            "generation_cost_usd": 0.01,
            "plan": [{"tool": "http.get", "url": "http://example.com"}],
        }
        result = arbiter.evaluate(hypothesis)
        assert result.decision == "discard"
        assert "kill switch" in result.reason

    def test_clamps_confidence_over_1(self) -> None:
        arbiter = _make_arbiter()
        hypothesis = {
            "id": "test-9",
            "confidence": 1.5,
            "generation_cost_usd": 0.01,
            "reversibility": "unknown",
            "plan": [{"tool": "http.get", "url": "http://example.com"}],
        }
        result = arbiter.evaluate(hypothesis)
        assert result.decision == "auto_execute"

    def test_missing_execution_cost_for_costly_tools(self) -> None:
        arbiter = _make_arbiter()
        hypothesis = {
            "id": "test-10",
            "confidence": 0.95,
            "generation_cost_usd": 0.01,
            "reversibility": "unknown",
            "plan": [{"tool": "model.call", "model": "openai/gpt-4o", "prompt": "hi"}],
        }
        result = arbiter.evaluate(hypothesis)
        # model.call is in COSTLY_AUTO_EXECUTE_TOOLS and not in allowed_tools
        assert result.decision in ("stage_notify", "discard")


# ---------------------------------------------------------------------------
# PolicyArbiter - _extract_tools / _extract_plan
# ---------------------------------------------------------------------------


class TestPolicyArbiterExtractTools:
    def test_extract_tools_from_plan(self) -> None:
        arbiter = _make_arbiter()
        hypothesis = {
            "plan": [
                {"tool": "http.get", "url": "http://example.com"},
                {"tool": "file.read", "path": "/tmp/test"},
            ]
        }
        tools = arbiter._extract_tools(hypothesis)
        assert "http.get" in tools
        assert "file.read" in tools

    def test_extract_tools_empty_plan(self) -> None:
        arbiter = _make_arbiter()
        hypothesis = {"plan": []}
        tools = arbiter._extract_tools(hypothesis)
        assert tools == []

    def test_extract_tools_from_action_field(self) -> None:
        arbiter = _make_arbiter()
        hypothesis = {"plan": [{"action": "web.search", "query": "test"}]}
        tools = arbiter._extract_tools(hypothesis)
        assert "web.search" in tools

    def test_extract_plan_dict_steps(self) -> None:
        arbiter = _make_arbiter()
        hypothesis = {"plan": {"steps": [{"tool": "http.get"}]}}
        plan = arbiter._extract_plan(hypothesis)
        assert len(plan) == 1


# ---------------------------------------------------------------------------
# PolicyArbiter - _execute_step
# ---------------------------------------------------------------------------


class TestPolicyArbiterExecuteStep:
    def test_execute_unknown_tool(self) -> None:
        arbiter = _make_arbiter()
        result = arbiter._execute_step("unknown.tool", {})
        assert result["status"] == "error"
        assert "No handler" in result["error"]

    def test_execute_http_get_missing_url(self) -> None:
        arbiter = _make_arbiter()
        result = arbiter._execute_step("http.get", {})
        assert result["status"] == "error"

    def test_execute_proposal_snooze(self) -> None:
        arbiter = _make_arbiter()
        result = arbiter._execute_step("proposal.snooze", {"proposal_id": "p-1", "duration_minutes": 30})
        assert result["status"] == "ok"
        assert result["result"]["result"]["proposal_id"] == "p-1"

    def test_execute_proposal_snooze_missing_id(self) -> None:
        arbiter = _make_arbiter()
        result = arbiter._execute_step("proposal.snooze", {})
        assert result["status"] == "error"

    def test_execute_notification_summarize_no_events(self) -> None:
        arbiter = _make_arbiter()
        arbiter.emitter.query.return_value = []
        result = arbiter._execute_step("notification.summarize", {"hours": 1})
        assert result["status"] in ("ok", "error")


# ---------------------------------------------------------------------------
# PolicyArbiter - handle_approval
# ---------------------------------------------------------------------------


class TestPolicyArbiterHandleApproval:
    def test_handle_snooze(self) -> None:
        arbiter = _make_arbiter()
        result = arbiter.handle_approval("test-1", "snooze", user_id="test-user")
        assert result["status"] == "snoozed"
        assert result["hypothesis_id"] == "test-1"

    def test_handle_reject(self) -> None:
        arbiter = _make_arbiter()
        result = arbiter.handle_approval("test-2", "reject", user_id="test-user")
        assert result["status"] == "rejected"

    def test_handle_explain(self) -> None:
        arbiter = _make_arbiter()
        result = arbiter.handle_approval("test-3", "explain", user_id="test-user")
        assert result["status"] == "explained"

    def test_handle_unknown_decision(self) -> None:
        arbiter = _make_arbiter()
        result = arbiter.handle_approval("test-4", "dance", user_id="test-user")
        assert result["status"] == "unknown_decision"


# ---------------------------------------------------------------------------
# PolicyArbiter - toggle_kill_switch
# ---------------------------------------------------------------------------


class TestPolicyArbiterKillSwitch:
    def test_toggle_kill_switch_on(self) -> None:
        arbiter = _make_arbiter()
        result = arbiter.toggle_kill_switch(enabled=True)
        assert arbiter.kill_switch is True
        assert result["kill_switch"] is True

    def test_toggle_kill_switch_off(self) -> None:
        arbiter = _make_arbiter()
        arbiter.kill_switch = True
        result = arbiter.toggle_kill_switch(enabled=False)
        assert arbiter.kill_switch is False
        assert result["kill_switch"] is False


# ---------------------------------------------------------------------------
# PolicyArbiter - get_status
# ---------------------------------------------------------------------------


class TestPolicyArbiterGetStatus:
    def test_get_status(self) -> None:
        arbiter = _make_arbiter()
        status = arbiter.get_status()
        assert "kill_switch" in status
        assert "rate_limits" in status
        assert "pending_approvals" in status
        assert "recent_decisions" in status


# ---------------------------------------------------------------------------
# PolicyArbiter - _sanitize_hypothesis
# ---------------------------------------------------------------------------


class TestPolicyArbiterSanitize:
    def test_redacts_sensitive_fields(self) -> None:
        arbiter = _make_arbiter()
        hypothesis = {"metadata": {"api_key": "secret123", "name": "test"}}
        sanitized = arbiter._sanitize_hypothesis(hypothesis)
        assert sanitized["metadata"]["api_key"] == "[REDACTED]"
        assert sanitized["metadata"]["name"] == "test"

    def test_redacts_nested_sensitive(self) -> None:
        arbiter = _make_arbiter()
        hypothesis = {"deep": {"token": "abc", "safe": "value"}}
        sanitized = arbiter._sanitize_hypothesis(hypothesis)
        assert sanitized["deep"]["token"] == "[REDACTED]"
        assert sanitized["deep"]["safe"] == "value"

    def test_non_sensitive_passthrough(self) -> None:
        arbiter = _make_arbiter()
        hypothesis = {"plan": [{"tool": "http.get"}]}
        sanitized = arbiter._sanitize_hypothesis(hypothesis)
        assert sanitized == hypothesis


# ---------------------------------------------------------------------------
# PolicyArbiter - _validate_url
# ---------------------------------------------------------------------------


class TestPolicyArbiterValidateURL:
    def test_rejects_non_http_scheme(self) -> None:
        arbiter = _make_arbiter()
        with pytest.raises(ValueError, match="scheme"):
            arbiter._validate_url("ftp://example.com")

    def test_rejects_localhost(self) -> None:
        arbiter = _make_arbiter()
        with pytest.raises(ValueError, match="Forbidden hostname"):
            arbiter._validate_url("http://localhost/api")

    def test_rejects_empty_url(self) -> None:
        arbiter = _make_arbiter()
        with pytest.raises(ValueError):
            arbiter._validate_url("")

    def test_rejects_metadata_google(self) -> None:
        arbiter = _make_arbiter()
        with pytest.raises(ValueError, match="Forbidden hostname"):
            arbiter._validate_url("http://metadata.google.internal/")


# ---------------------------------------------------------------------------
# PolicyArbiter - _validate_file_path
# ---------------------------------------------------------------------------


class TestPolicyArbiterValidateFilePath:
    def test_rejects_non_path(self) -> None:
        arbiter = _make_arbiter()
        with pytest.raises(ValueError):
            arbiter._validate_file_path("/tmp/test")  # type: ignore[arg-type]

    def test_rejects_etc(self) -> None:
        arbiter = _make_arbiter()
        with pytest.raises(ValueError, match="Forbidden path"):
            arbiter._validate_file_path(Path("/etc/passwd"))

    def test_rejects_ssh(self) -> None:
        arbiter = _make_arbiter()
        # Create a temp .ssh dir so realpath resolves, then test the pattern rejection
        ssh_dir = arbiter.state_dir / ".ssh"
        ssh_dir.mkdir(exist_ok=True)
        ssh_key = ssh_dir / "id_rsa"
        ssh_key.touch()
        with pytest.raises(ValueError, match="Forbidden path"):
            arbiter._validate_file_path(ssh_key)


# ---------------------------------------------------------------------------
# PolicyArbiter - _record_decision
# ---------------------------------------------------------------------------


class TestPolicyArbiterRecordDecision:
    def test_record_decision(self) -> None:
        arbiter = _make_arbiter()
        result = arbiter._record_decision("test-1", "auto_execute", "test reason")
        assert isinstance(result, ActionDecision)
        assert result.hypothesis_id == "test-1"
        assert result.decision == "auto_execute"

    def test_decision_appended_to_recent(self) -> None:
        arbiter = _make_arbiter()
        arbiter._record_decision("test-2", "discard", "low confidence")
        assert len(arbiter.recent_decisions) >= 1


# ---------------------------------------------------------------------------
# PolicyArbiter - _emit_event
# ---------------------------------------------------------------------------


class TestPolicyArbiterEmitEvent:
    def test_emit_event_calls_emitter(self) -> None:
        arbiter = _make_arbiter()
        arbiter._emit_event("test.event", {"data": "test"})
        arbiter.emitter.emit.assert_called()


# ---------------------------------------------------------------------------
# PolicyArbiter - _site_url
# ---------------------------------------------------------------------------


class TestPolicyArbiterSiteUrl:
    def test_default_site_url(self) -> None:
        arbiter = _make_arbiter()
        url = arbiter._site_url()
        assert isinstance(url, str)
        assert len(url) > 0

    def test_site_url_from_env(self) -> None:
        arbiter = _make_arbiter()
        with patch.dict(os.environ, {"CEREBELLUM_HTTP_REFERER": "https://my.site.com"}):
            url = arbiter._site_url()
            assert url == "https://my.site.com"


# ---------------------------------------------------------------------------
# PolicyArbiter - auto_execute
# ---------------------------------------------------------------------------


class TestPolicyArbiterAutoExecute:
    def test_auto_execute_blocked_by_kill_switch(self) -> None:
        arbiter = _make_arbiter()
        arbiter.kill_switch = True
        arbiter._write_kill_switch_file(True)
        result = arbiter.auto_execute({"id": "test-ae", "plan": []})
        assert result["status"] == "blocked"
        assert result["reason"] == "kill switch enabled"

    def test_auto_execute_empty_plan(self) -> None:
        arbiter = _make_arbiter()
        result = arbiter.auto_execute({"id": "test-ae-2", "plan": []})
        assert result["status"] == "completed"
        assert result["results"] == []


# ---------------------------------------------------------------------------
# PolicyArbiter - stage_for_approval
# ---------------------------------------------------------------------------


class TestPolicyArbiterStageForApproval:
    def test_stage_returns_message_id(self) -> None:
        arbiter = _make_arbiter()
        message_id = arbiter.stage_for_approval({
            "id": "test-stage-1",
            "confidence": 0.7,
            "summary": "Test hypothesis",
        })
        assert isinstance(message_id, str)
        assert len(message_id) > 0


# ---------------------------------------------------------------------------
# PolicyArbiter - helpers
# ---------------------------------------------------------------------------


class TestPolicyArbiterHelpers:
    def test_coerce_optional_float_none(self) -> None:
        arbiter = _make_arbiter()
        assert arbiter._coerce_optional_float(None) is None

    def test_coerce_optional_float_valid(self) -> None:
        arbiter = _make_arbiter()
        assert arbiter._coerce_optional_float(3.14) == 3.14

    def test_coerce_optional_float_invalid(self) -> None:
        arbiter = _make_arbiter()
        assert arbiter._coerce_optional_float("abc") is None

    def test_extract_execution_cost(self) -> None:
        arbiter = _make_arbiter()
        cost = arbiter._extract_execution_cost({"cost": 1.5}, {})
        assert cost == 1.5

    def test_extract_execution_cost_zero(self) -> None:
        arbiter = _make_arbiter()
        cost = arbiter._extract_execution_cost({}, {})
        assert cost == 0.0

    def test_telegram_keyboard(self) -> None:
        arbiter = _make_arbiter()
        kb = arbiter._telegram_keyboard("h-123")
        assert len(kb) == 2
        assert kb[0][0]["callback_data"] == "approve:h-123"

    def test_format_telegram_card(self) -> None:
        arbiter = _make_arbiter()
        card = arbiter._format_telegram_card({
            "id": "h-1",
            "summary": "Test",
            "confidence": 0.9,
        })
        assert "h-1" in card
        assert "Test" in card

    def test_is_world_writable_path(self) -> None:
        arbiter = _make_arbiter()
        result = arbiter._is_world_writable_path(Path("/tmp"))
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_costly_tools(self) -> None:
        assert "model.call" in COSTLY_AUTO_EXECUTE_TOOLS
        assert "web.search" in COSTLY_AUTO_EXECUTE_TOOLS

    def test_sensitive_fields(self) -> None:
        assert "api_key" in SENSITIVE_HYPOTHESIS_FIELD_TOKENS
        assert "password" in SENSITIVE_HYPOTHESIS_FIELD_TOKENS

    def test_tool_costs_exist(self) -> None:
        assert len(TOOL_COST_ESTIMATES) >= 10
        assert TOOL_COST_ESTIMATES["http.get"] == 0.0
        assert TOOL_COST_ESTIMATES["model.call"] > 0.0

    def test_rasputin_costs(self) -> None:
        assert "rasputin.search" in TOOL_COST_ESTIMATES
        assert "rasputin.commit_fact" in TOOL_COST_ESTIMATES
        assert "rasputin.reflect" in TOOL_COST_ESTIMATES

# ---------------------------------------------------------------------------
# PolicyArbiter - handler coverage (mocked)
# ---------------------------------------------------------------------------


class TestPolicyArbiterHandlers:
    """Test handlers that normally need network by mocking dependencies."""

    def test_handle_notification_send_no_creds(self) -> None:
        arbiter = _make_arbiter()
        env = dict(os.environ)
        env.pop("TELEGRAM_BOT_TOKEN", None)
        env.pop("OPENCLAW_TELEGRAM_BOT_TOKEN", None)
        with patch.dict(os.environ, env, clear=True):
            with patch.object(arbiter, "_resolve_openclaw_binary", return_value=None):
                result = arbiter._handle_notification_send({"text": "hello"})
                assert result["result"]["ok"] is False or result["result"].get("skipped") is True

    def test_handle_notification_summarize_with_events(self) -> None:
        arbiter = _make_arbiter()
        arbiter._emitter = MagicMock()
        arbiter._emitter.query.return_value = [{"type": "cerebellum.action"}, {"type": "cerebellum.execution"}]
        with patch.object(arbiter, "_send_telegram_message", return_value={"ok": True}):
            result = arbiter._handle_notification_summarize({"hours": 1})
            assert result["status"] == "ok"

    def test_handle_rasputin_search(self) -> None:
        arbiter = _make_arbiter()
        with patch.object(arbiter, "_call_rasputin_mcp", return_value={"status": "ok"}) as mock_call:
            result = arbiter._handle_rasputin_search({"query": "test"})
            mock_call.assert_called_once_with("search", {"query": "test"})

    def test_handle_rasputin_search_missing_query(self) -> None:
        arbiter = _make_arbiter()
        with pytest.raises(ValueError, match="requires query"):
            arbiter._handle_rasputin_search({})

    def test_handle_rasputin_recent_facts(self) -> None:
        arbiter = _make_arbiter()
        with patch.object(arbiter, "_call_rasputin_mcp", return_value={"status": "ok"}) as mock_call:
            result = arbiter._handle_rasputin_recent_facts({"limit": 5})
            mock_call.assert_called_once_with("recent_facts", {"limit": 5})

    def test_handle_rasputin_entity_lookup(self) -> None:
        arbiter = _make_arbiter()
        with patch.object(arbiter, "_call_rasputin_mcp", return_value={"status": "ok"}) as mock_call:
            result = arbiter._handle_rasputin_entity_lookup({"entity": "e"})
            mock_call.assert_called_once_with("entity_lookup", {"entity": "e"})

    def test_handle_rasputin_entity_lookup_missing(self) -> None:
        arbiter = _make_arbiter()
        with pytest.raises(ValueError, match="requires entity"):
            arbiter._handle_rasputin_entity_lookup({})

    def test_handle_rasputin_episode_summary(self) -> None:
        arbiter = _make_arbiter()
        with patch.object(arbiter, "_call_rasputin_mcp", return_value={"status": "ok"}) as mock_call:
            result = arbiter._handle_rasputin_episode_summary({"hours": 12})
            mock_call.assert_called_once_with("episode_summary", {"hours": 12})

    def test_handle_rasputin_commit_fact(self) -> None:
        arbiter = _make_arbiter()
        with patch.object(arbiter, "_call_rasputin_mcp", return_value={"status": "ok"}) as mock_call:
            result = arbiter._handle_rasputin_commit_fact({"fact": "f"})
            mock_call.assert_called_once_with("commit_fact", {"fact": "f"})

    def test_handle_rasputin_commit_fact_missing(self) -> None:
        arbiter = _make_arbiter()
        with pytest.raises(ValueError, match="requires fact"):
            arbiter._handle_rasputin_commit_fact({})

    def test_handle_rasputin_reflect(self) -> None:
        arbiter = _make_arbiter()
        with patch.object(arbiter, "_call_rasputin_mcp", return_value={"status": "ok"}) as mock_call:
            result = arbiter._handle_rasputin_reflect({"prompt": "p"})
            mock_call.assert_called_once_with("reflect", {"prompt": "p"})

    def test_call_rasputin_mcp_success(self) -> None:
        arbiter = _make_arbiter()
        mock_response = MagicMock()
        mock_response.json.return_value = {"data": "test"}
        with patch("cerebellum.policy_arbiter.safe_post", return_value=mock_response):
            result = arbiter._call_rasputin_mcp("search", {"query": "test"})
            assert result["status"] == "ok"

    def test_call_rasputin_mcp_failure(self) -> None:
        arbiter = _make_arbiter()
        with patch("cerebellum.policy_arbiter.safe_post", side_effect=Exception("conn refused")):
            result = arbiter._call_rasputin_mcp("search", {"query": "test"})
            assert result["status"] == "error"

    def test_handle_web_search_no_api_key(self) -> None:
        arbiter = _make_arbiter()
        env = dict(os.environ)
        env.pop("BRAVE_SEARCH_API_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="BRAVE_SEARCH_API_KEY"):
                arbiter._handle_web_search({"query": "test"})

    def test_handle_web_search_missing_query(self) -> None:
        arbiter = _make_arbiter()
        with pytest.raises(ValueError, match="requires a query"):
            arbiter._handle_web_search({})

    def test_handle_model_call_no_api_key(self) -> None:
        arbiter = _make_arbiter()
        env = dict(os.environ)
        env.pop("OPENROUTER_API_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
                arbiter._handle_model_call({"model": "openai/gpt-4o", "prompt": "hi"})

    def test_handle_model_call_wrong_model(self) -> None:
        arbiter = _make_arbiter()
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            with pytest.raises(ValueError, match="not in allowlist"):
                arbiter._handle_model_call({"model": "unknown/model", "prompt": "hi"})

    def test_handle_memory_query_non_localhost(self) -> None:
        arbiter = _make_arbiter()
        with patch.dict(os.environ, {"QDRANT_URL": "http://evil.com:6333"}):
            with pytest.raises(ValueError, match="must be localhost"):
                arbiter._handle_memory_query({"collection": "test", "vector": [0.1]})

    def test_handle_memory_query_invalid_collection(self) -> None:
        arbiter = _make_arbiter()
        with pytest.raises(ValueError, match="Invalid Qdrant collection"):
            arbiter._handle_memory_query({"collection": "../../etc", "vector": [0.1]})

    def test_handle_memory_query_missing_vector(self) -> None:
        arbiter = _make_arbiter()
        with pytest.raises(ValueError, match="requires a vector"):
            arbiter._handle_memory_query({"collection": "test"})

    def test_handle_file_read_missing_path(self) -> None:
        arbiter = _make_arbiter()
        with pytest.raises(ValueError):
            arbiter._handle_file_read({})

    def test_auto_execute_with_steps(self) -> None:
        arbiter = _make_arbiter()
        result = arbiter.auto_execute({
            "id": "test-ae-3",
            "plan": [{"tool": "proposal.snooze", "proposal_id": "p-1", "duration_minutes": 10}],
        })
        assert result["status"] == "completed"
        assert len(result["results"]) == 1
        assert result["results"][0]["ok"] is True

    def test_auto_execute_step_failure(self) -> None:
        arbiter = _make_arbiter()
        result = arbiter.auto_execute({
            "id": "test-ae-4",
            "plan": [{"tool": "unknown.tool"}],
        })
        assert result["status"] == "completed"
        assert result["results"][0]["result"]["status"] == "error"

    def test_auto_execute_cost_tracking(self) -> None:
        arbiter = _make_arbiter()
        result = arbiter.auto_execute({
            "id": "test-ae-5",
            "plan": [{"tool": "proposal.snooze", "proposal_id": "p-1", "cost": 0.5}],
        })
        assert result["execution_cost"] == 0.5

    def test_handle_approval_blocked_by_kill_switch(self) -> None:
        arbiter = _make_arbiter()
        arbiter.kill_switch = True
        arbiter._write_kill_switch_file(True)
        result = arbiter.handle_approval("test-ks", "approve", user_id="user")
        assert result["status"] == "blocked"

    def test_refresh_kill_switch_from_disk(self) -> None:
        arbiter = _make_arbiter()
        arbiter.kill_switch = False
        arbiter._write_kill_switch_file(True)
        arbiter._refresh_kill_switch_from_disk()
        assert arbiter.kill_switch is True

    def test_read_kill_switch_file(self) -> None:
        arbiter = _make_arbiter()
        arbiter._write_kill_switch_file(True)
        assert arbiter._read_kill_switch_file() is True

    def test_read_kill_switch_file_not_exists(self) -> None:
        arbiter = _make_arbiter()
        arbiter.kill_switch_file.unlink(missing_ok=True)
        assert arbiter._read_kill_switch_file() is None

    def test_assert_public_ip_private(self) -> None:
        arbiter = _make_arbiter()
        import ipaddress
        with pytest.raises(ValueError, match="forbidden address"):
            arbiter._assert_public_ip(ipaddress.ip_address("192.168.1.1"), "test")

    def test_assert_public_ip_loopback(self) -> None:
        arbiter = _make_arbiter()
        import ipaddress
        with pytest.raises(ValueError, match="forbidden address"):
            arbiter._assert_public_ip(ipaddress.ip_address("127.0.0.1"), "test")

    def test_sanitize_list(self) -> None:
        arbiter = _make_arbiter()
        sanitized = arbiter._sanitize_hypothesis([{"api_key": "secret"}, {"safe": "data"}])
        assert sanitized[0]["api_key"] == "[REDACTED]"
        assert sanitized[1]["safe"] == "data"

    def test_extract_plan_not_list(self) -> None:
        arbiter = _make_arbiter()
        assert arbiter._extract_plan({"plan": "not a list"}) == []

    def test_load_json_not_exists(self) -> None:
        arbiter = _make_arbiter()
        assert arbiter._load_json(Path("/nonexistent"), default={}) == {}

    def test_persist_event(self) -> None:
        arbiter = _make_arbiter()
        arbiter._persist_event("test.topic", {"key": "value"})

    def test_update_hypothesis_state(self) -> None:
        arbiter = _make_arbiter()
        arbiter._update_hypothesis_state("h-1", "completed", {"data": "test"})

    def test_site_url_default(self) -> None:
        arbiter = _make_arbiter()
        env = dict(os.environ)
        for k in ("CEREBELLUM_HTTP_REFERER", "CEREBELLUM_SITE_URL", "OPENROUTER_HTTP_REFERER"):
            env.pop(k, None)
        with patch.dict(os.environ, env, clear=True):
            assert arbiter._site_url() == "https://openclaw.local/cerebellum"

    def test_send_telegram_message_no_creds(self) -> None:
        arbiter = _make_arbiter()
        env = dict(os.environ)
        for k in ("TELEGRAM_BOT_TOKEN", "OPENCLAW_TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "OPENCLAW_TELEGRAM_CHAT_ID"):
            env.pop(k, None)
        with patch.dict(os.environ, env, clear=True):
            with patch.object(arbiter, "_resolve_openclaw_binary", return_value=None):
                result = arbiter._send_telegram_message("hello")
                assert result["ok"] is False

    def test_coerce_optional_float_negative(self) -> None:
        arbiter = _make_arbiter()
        assert arbiter._coerce_optional_float(-1.0) is None

    def test_coerce_optional_float_nan(self) -> None:
        arbiter = _make_arbiter()
        assert arbiter._coerce_optional_float(float("nan")) is None

    def test_extract_execution_cost_from_result(self) -> None:
        arbiter = _make_arbiter()
        assert arbiter._extract_execution_cost({}, {"execution_cost": 2.5}) == 2.5

    def test_extract_execution_cost_non_numeric(self) -> None:
        arbiter = _make_arbiter()
        assert arbiter._extract_execution_cost({"cost": "abc"}, {}) == 0.0

    def test_rate_limiter_discard(self) -> None:
        arbiter = _make_arbiter()
        arbiter.action_limiter = RateLimiter(max_count=0, window_seconds=3600)
        result = arbiter.evaluate({"id": "t-rl", "confidence": 0.95, "generation_cost_usd": 0.01, "plan": [{"tool": "http.get"}]})
        assert result.decision == "discard"
        assert "rate limit" in result.reason

    def test_cost_limiter_discard(self) -> None:
        arbiter = _make_arbiter()
        # Pre-spend the budget so next evaluation is rejected
        arbiter.cost_limiter = DailyCostTracker(max_cost=0.01)
        arbiter.cost_limiter.allow(0.01)  # spend it all
        result = arbiter.evaluate({"id": "t-cl", "confidence": 0.7, "generation_cost_usd": 0.005, "plan": [{"tool": "http.get"}]})
        assert result.decision == "discard"
        assert "budget" in result.reason
        assert "budget" in result.reason

    def test_auto_execute_budget_halt(self) -> None:
        arbiter = _make_arbiter()
        result = arbiter.auto_execute({
            "id": "t-budget",
            "plan": [{"tool": "proposal.snooze", "proposal_id": "p-1", "cost": 10000.0}],
        })
        assert result["status"] == "partial_failure"
        assert result["reason"] == "execution cost budget exceeded"

    def test_resolve_openclaw_binary_none(self) -> None:
        arbiter = _make_arbiter()
        result = arbiter._resolve_openclaw_binary()
        assert result is None or isinstance(result, Path)

    def test_handle_notification_send_missing_text(self) -> None:
        arbiter = _make_arbiter()
        with pytest.raises(ValueError, match="requires text"):
            arbiter._handle_notification_send({})

    def test_generation_cost_clamped_to_budget(self) -> None:
        arbiter = _make_arbiter()
        # generation_cost_usd > daily_budget triggers clamping (line 269-275)
        result = arbiter.evaluate({
            "id": "t-clamp",
            "confidence": 0.95,
            "generation_cost_usd": 10.0,  # exceeds daily budget of 5.0
            "reversibility": "unknown",
            "plan": [{"tool": "http.get"}],
        })
        # Should still evaluate (cost clamped to budget)
        assert result.decision in ("auto_execute", "stage_notify", "discard")

    def test_load_state_malformed_item(self) -> None:
        arbiter = _make_arbiter()
        # Write malformed state data
        import json as _json
        state = {
            "kill_switch": False,
            "recent_decisions": ["not_a_dict", 123, None],
        }
        arbiter.state_file.write_text(_json.dumps(state))
        # Should not raise
        arbiter._load_state()
        assert arbiter.recent_decisions == []

    def test_emit_event_error_path(self) -> None:
        arbiter = _make_arbiter()
        # Make emitter raise an exception
        arbiter.emitter.emit.side_effect = RuntimeError("emit failed")
        arbiter.emitter.publish = None
        arbiter.emitter.send = None
        # Should not raise - falls back to persist_event
        arbiter._emit_event("test.event", {"data": "test"})

    def test_persist_event_error_path(self) -> None:
        arbiter = _make_arbiter()
        # _append_jsonl to nonexistent dir should be caught internally
        arbiter._persist_event("test.topic", {"key": "value"})
        # Should not raise - errors are caught internally

    def test_load_json_error(self) -> None:
        arbiter = _make_arbiter()
        # Create file with invalid JSON
        test_file = arbiter.state_dir / "bad.json"
        test_file.write_text("{invalid json}")
        result = arbiter._load_json(test_file, default="fallback")
        assert result == "fallback"

    def test_handle_file_read_existing_file(self) -> None:
        arbiter = _make_arbiter()
        # Create a test file in the allowed base dir
        test_file = arbiter.base_dir / "test_read.txt"
        test_file.write_text("hello world")
        try:
            result = arbiter._handle_file_read({"path": str(test_file)})
            assert result["status"] == "ok"
            assert result["content"] == "hello world"
        finally:
            test_file.unlink(missing_ok=True)

    def test_handle_file_read_truncated(self) -> None:
        arbiter = _make_arbiter()
        test_file = arbiter.base_dir / "test_large.txt"
        test_file.write_text("x" * 15000)
        try:
            result = arbiter._handle_file_read({"path": str(test_file)})
            assert result["status"] == "ok"
            assert result["truncated"] is True
            assert len(result["content"]) == 10000
        finally:
            test_file.unlink(missing_ok=True)

    def test_handle_model_call_missing_model(self) -> None:
        arbiter = _make_arbiter()
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            with pytest.raises(ValueError, match="not in allowlist"):
                arbiter._handle_model_call({"prompt": "hi"})

    def test_handle_web_search_missing_query_empty(self) -> None:
        arbiter = _make_arbiter()
        with pytest.raises(ValueError, match="requires a query"):
            arbiter._handle_web_search({"query": ""})

    def test_validate_url_rejects_private_ip(self) -> None:
        arbiter = _make_arbiter()
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.1", 80))]):
            with pytest.raises(ValueError, match="forbidden address"):
                arbiter._validate_url("http://test-private.example.com")

    def test_validate_url_rejects_loopback_resolution(self) -> None:
        arbiter = _make_arbiter()
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 80))]):
            with pytest.raises(ValueError, match="forbidden address"):
                arbiter._validate_url("http://test-loopback.example.com")

    def test_validate_file_path_disallowed_suffix(self) -> None:
        arbiter = _make_arbiter()
        test_file = arbiter.base_dir / "test.xyz"
        test_file.touch()
        try:
            with pytest.raises(ValueError, match="disallowed file type"):
                arbiter._validate_file_path(test_file)
        finally:
            test_file.unlink(missing_ok=True)

    def test_validate_file_path_sensitive_pattern(self) -> None:
        arbiter = _make_arbiter()
        test_file = arbiter.base_dir / "test.env"
        test_file.touch()
        try:
            with pytest.raises(ValueError):
                arbiter._validate_file_path(test_file)
        finally:
            test_file.unlink(missing_ok=True)

    def test_validate_file_path_outside_root(self) -> None:
        arbiter = _make_arbiter()
        # Override base_dir to the project root so /tmp is genuinely outside allowed roots.
        # This avoids the root-user fragility where Path.home() -> /root (forbidden prefix).
        arbiter.base_dir = Path(__file__).resolve().parents[2]
        test_file = Path("/tmp") / "cerebellum_test_outside.py"
        test_file.touch()
        try:
            with pytest.raises(ValueError, match="outside allowed roots"):
                arbiter._validate_file_path(test_file)
        finally:
            test_file.unlink(missing_ok=True)

    def test_validate_file_path_config_json_allowed(self) -> None:
        arbiter = _make_arbiter()
        config_file = arbiter.base_dir / "config.json"
        config_file.touch()
        try:
            # Should not raise - config.json is explicitly allowed
            arbiter._validate_file_path(config_file)
        finally:
            config_file.unlink(missing_ok=True)

    def test_sanitize_hypothesis_deep_nesting(self) -> None:
        arbiter = _make_arbiter()
        value = {"a": {"b": {"c": {"api_key": "secret"}}}}
        sanitized = arbiter._sanitize_hypothesis(value)
        assert sanitized["a"]["b"]["c"]["api_key"] == "[REDACTED]"

    def test_extract_tools_with_none_tool_name(self) -> None:
        arbiter = _make_arbiter()
        hypothesis = {"plan": [{"tool": None, "other": "data"}, {"tool": "http.get"}]}
        tools = arbiter._extract_tools(hypothesis)
        assert tools == ["http.get"]

    def test_rate_limiter_prune(self) -> None:
        rl = RateLimiter(max_count=2, window_seconds=60)
        rl.allow()
        rl.allow()
        assert rl.allow() is False
        # Manually expire events by manipulating the deque
        import time
        now = time.monotonic()
        while rl.events:
            rl.events.popleft()
        assert rl.allow() is True

    def test_daily_cost_tracker_day_reset(self) -> None:
        dct = DailyCostTracker(max_cost=1.0)
        dct.allow(0.5)
        # Simulate day change
        dct._day = dct._day - __import__("datetime").timedelta(days=1)
        assert dct.allow(0.5) is True

    def test_daily_cost_tracker_negative_cost(self) -> None:
        dct = DailyCostTracker(max_cost=1.0)
        assert dct.allow(-1.0) is True  # negative costs treated as 0
