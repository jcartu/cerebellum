"""Tests for Phase 6: policy_arbiter coverage boost.

Targets: policy_arbiter.py 10% -> 75%.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
import sys

for path in (PROJECT_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

os.environ.setdefault("DASHBOARD_TOKEN", "test-token")

from cerebellum.policy_arbiter import (  # noqa: E402
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
    return tmp.name


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
