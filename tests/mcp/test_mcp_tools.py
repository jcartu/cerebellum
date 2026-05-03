"""Tests for MCP tool handlers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cerebellum.mcp import tools as mcp_tools


@pytest.fixture(autouse=True)
def reset_tool_state():
    """Reset lazy-loaded singletons between tests."""
    mcp_tools._emitter = None
    mcp_tools._episode_store = None
    mcp_tools._arbiter = None
    yield
    mcp_tools._emitter = None
    mcp_tools._episode_store = None
    mcp_tools._arbiter = None


class TestRecentEvents:
    def test_returns_empty_list(self):
        with patch.object(mcp_tools, "_get_emitter", return_value=MagicMock(query=lambda **kw: [])):
            result = mcp_tools.recent_events()
            assert result == []

    def test_returns_events(self):
        mock_emitter = MagicMock()
        mock_emitter.query.return_value = [
            {"id": "e1", "timestamp": "2026-01-01T00:00:00Z", "type": "test", "payload": {}, "actor": "mcp", "context": {}}
        ]
        with patch.object(mcp_tools, "_get_emitter", return_value=mock_emitter):
            result = mcp_tools.recent_events()
            assert len(result) == 1
            assert result[0]["id"] == "e1"

    def test_passes_limit(self):
        mock_emitter = MagicMock()
        mock_emitter.query.return_value = []
        with patch.object(mcp_tools, "_get_emitter", return_value=mock_emitter):
            mcp_tools.recent_events(limit=10)
            mock_emitter.query.assert_called_once()
            assert mock_emitter.query.call_args.kwargs["limit"] == 10


class TestRecentEpisodes:
    def test_returns_empty_list(self):
        mock_store = MagicMock()
        mock_store.get_recent_episodes.return_value = []
        with patch.object(mcp_tools, "_get_episode_store", return_value=mock_store):
            result = mcp_tools.recent_episodes()
            assert result == []

    def test_returns_episodes(self):
        mock_store = MagicMock()
        mock_store.get_recent_episodes.return_value = [
            {"id": "ep1", "title": "Test", "summary": "Summary", "start_time": "", "end_time": "", "event_count": 5}
        ]
        with patch.object(mcp_tools, "_get_episode_store", return_value=mock_store):
            result = mcp_tools.recent_episodes()
            assert len(result) == 1
            assert result[0]["id"] == "ep1"


class TestSuccessorPatterns:
    def test_returns_edges(self):
        mock_store = MagicMock()
        mock_store.query_successor_edges.return_value = [
            {"source_type": "a", "target_type": "b", "support": 10, "confidence": 0.8, "lift": 2.0}
        ]
        with patch.object(mcp_tools, "_get_episode_store", return_value=mock_store):
            result = mcp_tools.successor_patterns(event_type="a")
            assert len(result) == 1


class TestPendingProposals:
    def test_returns_empty_when_no_arbiter(self):
        with patch.object(mcp_tools, "_get_arbiter", return_value=None):
            result = mcp_tools.pending_proposals()
            assert result == []

    def test_returns_pending(self):
        mock_arbiter = MagicMock()
        mock_arbiter.get_pending_proposals.return_value = [
            {"id": "p1", "title": "Test", "description": "Desc", "status": "pending", "confidence": 0.9, "decision": None, "timestamp": ""}
        ]
        with patch.object(mcp_tools, "_get_arbiter", return_value=mock_arbiter):
            result = mcp_tools.pending_proposals()
            assert len(result) == 1


class TestKillSwitchState:
    def test_returns_false_when_no_arbiter(self):
        with patch.object(mcp_tools, "_get_arbiter", return_value=None):
            result = mcp_tools.kill_switch_state()
            assert result == {"enabled": False, "since": None}

    def test_returns_active_state(self):
        mock_arbiter = MagicMock()
        mock_arbiter.is_kill_switch_active.return_value = True
        with patch.object(mcp_tools, "_get_arbiter", return_value=mock_arbiter):
            result = mcp_tools.kill_switch_state()
            assert result["enabled"] is True


class TestSystemMetrics:
    def test_returns_metrics(self):
        mock_emitter = MagicMock()
        mock_emitter.query.return_value = [{"id": "e1"}] * 5
        with patch.object(mcp_tools, "_get_emitter", return_value=mock_emitter), \
             patch.object(mcp_tools, "_get_arbiter", return_value=None):
            result = mcp_tools.system_metrics()
            assert "events_24h" in result
            assert result["events_24h"] == 5


class TestEntityLookup:
    def test_returns_entities(self):
        mock_store = MagicMock()
        mock_store.query_entity.return_value = [
            {"id": "ent1", "name": "Test", "type": "entity", "description": "Desc", "last_seen": ""}
        ]
        with patch.object(mcp_tools, "_get_episode_store", return_value=mock_store):
            result = mcp_tools.entity_lookup(name="Test")
            assert len(result) == 1


class TestEmitEvent:
    def test_emits_event(self):
        mock_emitter = MagicMock()
        mock_emitter.emit.return_value = "event-123"
        with patch.object(mcp_tools, "_get_emitter", return_value=mock_emitter):
            result = mcp_tools.emit_event(event_type="test.event", payload={"key": "val"})
            assert result["event_id"] == "event-123"
            assert "timestamp" in result

    def test_rejects_empty_type(self):
        with pytest.raises(ValueError, match="event_type"):
            mcp_tools.emit_event(event_type="", payload={})

    def test_rejects_non_dict_payload(self):
        with pytest.raises(ValueError, match="payload"):
            mcp_tools.emit_event(event_type="test", payload="not a dict")  # type: ignore

    def test_defaults_payload_to_empty_dict(self):
        mock_emitter = MagicMock()
        mock_emitter.emit.return_value = "event-123"
        with patch.object(mcp_tools, "_get_emitter", return_value=mock_emitter):
            result = mcp_tools.emit_event(event_type="test")
            assert result["event_id"] == "event-123"


class TestProposeAction:
    def test_rejects_missing_title(self):
        with pytest.raises(ValueError, match="title"):
            mcp_tools.propose_action(title="", description="d", plan="p")

    def test_rejects_missing_description(self):
        with pytest.raises(ValueError, match="description"):
            mcp_tools.propose_action(title="t", description="", plan="p")

    def test_rejects_missing_plan(self):
        with pytest.raises(ValueError, match="plan"):
            mcp_tools.propose_action(title="t", description="d", plan="")

    def test_returns_error_when_no_arbiter(self):
        with patch.object(mcp_tools, "_get_arbiter", return_value=None):
            result = mcp_tools.propose_action(title="t", description="d", plan="p")
            assert result["policy_decision"] == "discard"


class TestSetKillSwitch:
    def test_rejects_missing_reason(self):
        with pytest.raises(ValueError, match="reason"):
            mcp_tools.set_kill_switch(enabled=True, reason="")

    def test_returns_pending_approval(self):
        mock_arbiter = MagicMock()
        with patch.object(mcp_tools, "_get_arbiter", return_value=mock_arbiter):
            result = mcp_tools.set_kill_switch(enabled=True, reason="testing")
            assert result["status"] == "pending_approval"
            assert "approval_id" in result

    def test_returns_error_when_no_arbiter(self):
        with patch.object(mcp_tools, "_get_arbiter", return_value=None):
            result = mcp_tools.set_kill_switch(enabled=True, reason="testing")
            assert result["status"] == "rejected"


class TestSnoozeProposal:
    def test_rejects_missing_id(self):
        with pytest.raises(ValueError, match="proposal_id"):
            mcp_tools.snooze_proposal(proposal_id="", until="2026-01-01T00:00:00Z")

    def test_rejects_missing_until(self):
        with pytest.raises(ValueError, match="until"):
            mcp_tools.snooze_proposal(proposal_id="p1", until="")

    def test_rejects_invalid_timestamp(self):
        with pytest.raises(ValueError, match="ISO 8601"):
            mcp_tools.snooze_proposal(proposal_id="p1", until="not-a-date")

    def test_returns_snoozed(self):
        result = mcp_tools.snooze_proposal(proposal_id="p1", until="2026-01-01T00:00:00Z")
        assert result["status"] == "snoozed"
        assert result["proposal_id"] == "p1"
