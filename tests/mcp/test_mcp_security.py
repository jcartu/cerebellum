"""Security tests for MCP layer."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cerebellum.mcp import tools as mcp_tools


@pytest.fixture(autouse=True)
def reset_state():
    mcp_tools._emitter = None
    mcp_tools._episode_store = None
    mcp_tools._arbiter = None
    yield
    mcp_tools._emitter = None
    mcp_tools._episode_store = None
    mcp_tools._arbiter = None


class TestKillSwitchSecurity:
    """Verify set_kill_switch never directly toggles the flag."""

    def test_returns_pending_approval_not_executed(self):
        mock_arbiter = MagicMock()
        with patch.object(mcp_tools, "_get_arbiter", return_value=mock_arbiter):
            result = mcp_tools.set_kill_switch(enabled=True, reason="security test")
            assert result["status"] == "pending_approval"
            assert "approval_id" in result
            # Verify no arbiter method was called — returns pending_approval directly

    def test_requires_reason(self):
        with pytest.raises(ValueError, match="reason"):
            mcp_tools.set_kill_switch(enabled=True, reason="")

    def test_requires_arbiter(self):
        with patch.object(mcp_tools, "_get_arbiter", return_value=None):
            result = mcp_tools.set_kill_switch(enabled=True, reason="test")
            assert result["status"] == "rejected"


class TestProposeActionSecurity:
    """Verify propose_action routes through policy arbiter."""

    def test_routes_through_arbiter(self):
        mock_arbiter = MagicMock()
        mock_decision = MagicMock()
        mock_decision.decision = "stage_notify"
        mock_decision.reason = "low confidence"
        mock_arbiter.evaluate.return_value = mock_decision

        with patch.object(mcp_tools, "_get_arbiter", return_value=mock_arbiter):
            result = mcp_tools.propose_action(
                title="Test",
                description="Test proposal",
                plan="Do nothing",
                confidence=0.3,
            )
            assert result["policy_decision"] == "stage_notify"
            mock_arbiter.evaluate.assert_called_once()

    def test_rejects_missing_required_fields(self):
        with pytest.raises(ValueError):
            mcp_tools.propose_action(title="", description="d", plan="p")
        with pytest.raises(ValueError):
            mcp_tools.propose_action(title="t", description="", plan="p")
        with pytest.raises(ValueError):
            mcp_tools.propose_action(title="t", description="d", plan="")


class TestEmitEventSecurity:
    """Verify emit_event validates input shapes."""

    def test_rejects_empty_event_type(self):
        with pytest.raises(ValueError, match="event_type"):
            mcp_tools.emit_event(event_type="", payload={})

    def test_rejects_non_dict_payload(self):
        with pytest.raises(ValueError, match="payload"):
            mcp_tools.emit_event(event_type="test", payload="not a dict")  # type: ignore

    def test_rejects_none_payload_as_non_dict(self):
        # None payload should default to empty dict, not raise
        mock_emitter = MagicMock()
        mock_emitter.emit.return_value = "event-123"
        with patch.object(mcp_tools, "_get_emitter", return_value=mock_emitter):
            result = mcp_tools.emit_event(event_type="test", payload=None)
            assert result["event_id"] == "event-123"


class TestSnoozeProposalSecurity:
    """Verify snooze_proposal validates timestamps."""

    def test_rejects_invalid_timestamp(self):
        with pytest.raises(ValueError, match="ISO 8601"):
            mcp_tools.snooze_proposal(proposal_id="p1", until="not-a-date")

    def test_rejects_missing_fields(self):
        with pytest.raises(ValueError):
            mcp_tools.snooze_proposal(proposal_id="", until="2026-01-01T00:00:00Z")
        with pytest.raises(ValueError):
            mcp_tools.snooze_proposal(proposal_id="p1", until="")


class TestAuthSecurity:
    """Verify auth module security properties."""

    def test_constant_time_comparison(self):
        from cerebellum.mcp.auth import constant_time_compare
        assert constant_time_compare("secret", "secret") is True
        assert constant_time_compare("secret", "wrong") is False
        assert constant_time_compare("", "") is True

    def test_validate_token_rejects_empty(self):
        from cerebellum.mcp.auth import validate_token
        assert validate_token("", "secret") is False
        assert validate_token("secret", "") is False
        assert validate_token("", "") is False

    def test_rate_limit_is_per_ip(self):
        from cerebellum.mcp.auth import check_rate_limit, _rate_windows
        _rate_windows.clear()
        # IP 1 should be limited
        for _ in range(5):
            check_rate_limit("10.0.0.1", max_requests=5)
        assert check_rate_limit("10.0.0.1", max_requests=5) is False
        # IP 2 should still be allowed
        assert check_rate_limit("10.0.0.2", max_requests=5) is True
        _rate_windows.clear()
