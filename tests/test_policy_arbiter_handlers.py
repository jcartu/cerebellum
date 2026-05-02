"""Tests for Phase 4: http_client and new policy_arbiter handlers."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from cerebellum.http_client import _is_blocked_ip, safe_get, safe_post
from cerebellum.policy_arbiter import TOOL_COST_ESTIMATES

# ---------------------------------------------------------------------------
# http_client tests
# ---------------------------------------------------------------------------


class TestIsBlockedIp:
    def test_blocks_loopback_ipv4(self) -> None:
        assert _is_blocked_ip("127.0.0.1") is True

    def test_blocks_private_10_x(self) -> None:
        assert _is_blocked_ip("10.0.0.1") is True

    def test_blocks_private_192_168_x(self) -> None:
        assert _is_blocked_ip("192.168.1.1") is True

    def test_blocks_private_172_16_x(self) -> None:
        assert _is_blocked_ip("172.16.0.1") is True

    def test_blocks_link_local(self) -> None:
        assert _is_blocked_ip("169.254.169.254") is True

    def test_blocks_loopback_ipv6(self) -> None:
        assert _is_blocked_ip("::1") is True

    def test_blocks_unique_local_ipv6(self) -> None:
        assert _is_blocked_ip("fc00::1") is True

    def test_allows_public_ip(self) -> None:
        assert _is_blocked_ip("8.8.8.8") is False

    def test_allows_hostname(self) -> None:
        assert _is_blocked_ip("example.com") is False


class TestSafeGet:
    @patch("cerebellum.http_client.httpx.Client")
    def test_get_success(self, mock_client_cls: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        with patch("cerebellum.http_client._is_blocked_ip", return_value=False):
            result = safe_get("http://example.com/data")
        mock_client.get.assert_called_once()
        assert result == mock_response

    @patch("cerebellum.http_client._is_blocked_ip", return_value=True)
    def test_get_blocks_private_ip(self, _: MagicMock) -> None:
        try:
            safe_get("http://192.168.1.1/data")
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "Blocked IP" in str(e)


class TestSafePost:
    @patch("cerebellum.http_client.httpx.Client")
    def test_post_success(self, mock_client_cls: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        with patch("cerebellum.http_client._is_blocked_ip", return_value=False):
            result = safe_post("http://example.com/api", json={"key": "value"})
        mock_client.post.assert_called_once()
        assert result == mock_response

    @patch("cerebellum.http_client._is_blocked_ip", return_value=True)
    def test_post_blocks_private_ip(self, _: MagicMock) -> None:
        try:
            safe_post("http://10.0.0.1/api", json={})
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "Blocked IP" in str(e)


# ---------------------------------------------------------------------------
# TOOL_COST_ESTIMATES tests
# ---------------------------------------------------------------------------


class TestToolCostEstimates:
    def test_has_http_get(self) -> None:
        assert "http.get" in TOOL_COST_ESTIMATES

    def test_has_web_search(self) -> None:
        assert "web.search" in TOOL_COST_ESTIMATES

    def test_has_file_read(self) -> None:
        assert "file.read" in TOOL_COST_ESTIMATES

    def test_has_memory_query(self) -> None:
        assert "memory.query" in TOOL_COST_ESTIMATES

    def test_has_model_call(self) -> None:
        assert "model.call" in TOOL_COST_ESTIMATES

    def test_has_notification_send(self) -> None:
        assert "notification.send" in TOOL_COST_ESTIMATES

    def test_has_notification_summarize(self) -> None:
        assert "notification.summarize" in TOOL_COST_ESTIMATES

    def test_has_proposal_snooze(self) -> None:
        assert "proposal.snooze" in TOOL_COST_ESTIMATES

    def test_has_rasputin_search(self) -> None:
        assert "rasputin.search" in TOOL_COST_ESTIMATES

    def test_has_rasputin_recent_facts(self) -> None:
        assert "rasputin.recent_facts" in TOOL_COST_ESTIMATES

    def test_has_rasputin_entity_lookup(self) -> None:
        assert "rasputin.entity_lookup" in TOOL_COST_ESTIMATES

    def test_has_rasputin_episode_summary(self) -> None:
        assert "rasputin.episode_summary" in TOOL_COST_ESTIMATES

    def test_has_rasputin_commit_fact(self) -> None:
        assert "rasputin.commit_fact" in TOOL_COST_ESTIMATES

    def test_has_rasputin_reflect(self) -> None:
        assert "rasputin.reflect" in TOOL_COST_ESTIMATES

    def test_costs_are_non_negative(self) -> None:
        for tool, cost in TOOL_COST_ESTIMATES.items():
            assert cost >= 0.0, f"{tool} has negative cost"

    def test_has_all_fourteen_tools(self) -> None:
        assert len(TOOL_COST_ESTIMATES) == 14
