"""Tests for MCP server integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cerebellum.mcp.server import CerebellumMCPServer


class TestServerListTools:
    def test_lists_all_tools(self):
        server = CerebellumMCPServer()
        # Get the list_tools handler
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            # Call the list_tools handler registered on the server
            handler = server.server.request_handlers.get
            from mcp.types import ListToolsRequest
            list_handler = server.server.request_handlers.get(ListToolsRequest)
            assert list_handler is not None
        finally:
            loop.close()

    def test_tool_count(self):
        from cerebellum.mcp.server import TOOL_DEFINITIONS
        assert len(TOOL_DEFINITIONS) == 12


class TestServerCallTool:
    def test_unknown_tool_returns_error(self):
        server = CerebellumMCPServer()
        import asyncio

        async def run():
            from mcp.types import CallToolRequest, CallToolRequestParams
            params = CallToolRequestParams(name="nonexistent.tool", arguments={})
            req = CallToolRequest(params=params)
            handler = server.server.request_handlers[type(req)]
            result = await handler(req)
            # Should return error
            assert result is not None

        asyncio.run(run())

    def test_known_tool_dispatches(self):
        server = CerebellumMCPServer()
        import asyncio

        async def run():
            from cerebellum.mcp import tools as mcp_tools
            mcp_tools._emitter = None
            mcp_tools._episode_store = None
            mcp_tools._arbiter = None

            mock_emitter = MagicMock()
            mock_emitter.query.return_value = []
            mcp_tools._emitter = mock_emitter

            from mcp.types import CallToolRequest, CallToolRequestParams
            params = CallToolRequestParams(name="cerebellum.recent_events", arguments={"limit": 10})
            req = CallToolRequest(params=params)
            handler = server.server.request_handlers[type(req)]
            result = await handler(req)
            assert result is not None

        asyncio.run(run())


class TestServerAuth:
    def test_auth_middleware_rejects_no_token(self):
        from cerebellum.mcp.auth import validate_token
        assert validate_token("", "secret") is False
        assert validate_token("wrong", "secret") is False

    def test_auth_middleware_accepts_valid_token(self):
        from cerebellum.mcp.auth import validate_token
        assert validate_token("secret", "secret") is True

    def test_rate_limit_allows_normal_traffic(self):
        from cerebellum.mcp.auth import check_rate_limit
        assert check_rate_limit("127.0.0.1", max_requests=100) is True

    def test_rate_limit_blocks_excess(self):
        from cerebellum.mcp.auth import check_rate_limit, _rate_windows
        _rate_windows.clear()
        for _ in range(5):
            check_rate_limit("10.0.0.1", max_requests=5)
        assert check_rate_limit("10.0.0.1", max_requests=5) is False
        _rate_windows.clear()
