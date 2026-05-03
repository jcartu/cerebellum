"""CEREBELLUM MCP Server — Model Context Protocol integration.

Supports both stdio (for Claude Desktop, Claude Code) and SSE transports.

Usage:
    cerebellum-mcp --transport stdio
    cerebellum-mcp --transport sse --port 8765
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from mcp.server import Server
from mcp.types import CallToolResult, TextContent, Tool

from cerebellum.mcp import tools as mcp_tools

logger = logging.getLogger(__name__)

# Tool definitions — maps name to (handler, description, input_schema)
TOOL_DEFINITIONS: dict[str, tuple[Any, str, dict[str, Any]]] = {
    "cerebellum.recent_events": (
        mcp_tools.recent_events,
        "Query recent events from the CEREBELLUM event bus.",
        {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "ISO 8601 timestamp. Events after this time."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50, "description": "Max events."},
            },
        },
    ),
    "cerebellum.recent_episodes": (
        mcp_tools.recent_episodes,
        "Query recent episodes from the CEREBELLUM episode store.",
        {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "ISO 8601 timestamp."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "Max episodes."},
            },
        },
    ),
    "cerebellum.successor_patterns": (
        mcp_tools.successor_patterns,
        "Query successor patterns for a given event type.",
        {
            "type": "object",
            "properties": {
                "event_type": {"type": "string", "description": "Source event type."},
                "min_lift": {"type": "number", "default": 1.5, "description": "Minimum lift threshold."},
            },
            "required": ["event_type"],
        },
    ),
    "cerebellum.pending_proposals": (
        mcp_tools.pending_proposals,
        "List proposals awaiting approval.",
        {"type": "object", "properties": {}},
    ),
    "cerebellum.recent_proposals": (
        mcp_tools.recent_proposals,
        "Query recent proposals from the policy arbiter.",
        {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "ISO 8601 timestamp."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "Max proposals."},
            },
        },
    ),
    "cerebellum.kill_switch_state": (
        mcp_tools.kill_switch_state,
        "Check the current kill switch state.",
        {"type": "object", "properties": {}},
    ),
    "cerebellum.system_metrics": (
        mcp_tools.system_metrics,
        "Get system health metrics (events, proposals, approval rate).",
        {"type": "object", "properties": {}},
    ),
    "cerebellum.entity_lookup": (
        mcp_tools.entity_lookup,
        "Look up an entity in the CEREBELLUM knowledge graph.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Entity name to look up."},
            },
            "required": ["name"],
        },
    ),
    "cerebellum.emit_event": (
        mcp_tools.emit_event,
        "Emit an event directly into the CEREBELLUM event bus.",
        {
            "type": "object",
            "properties": {
                "event_type": {"type": "string", "description": "Type of event."},
                "payload": {"type": "object", "description": "Event payload dictionary."},
                "actor": {"type": "string", "default": "mcp", "description": "Actor identifier."},
            },
            "required": ["event_type"],
        },
    ),
    "cerebellum.propose_action": (
        mcp_tools.propose_action,
        "Submit a proposal for action through the policy arbiter.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short title."},
                "description": {"type": "string", "description": "Detailed description."},
                "plan": {"type": "string", "description": "Step-by-step execution plan."},
                "evidence_event_ids": {"type": "array", "items": {"type": "string"}, "description": "Related event IDs."},
                "tools_required": {"type": "array", "items": {"type": "string"}, "description": "Tools needed."},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5},
                "estimated_cost_usd": {"type": "number", "minimum": 0, "default": 0.0},
            },
            "required": ["title", "description", "plan"],
        },
    ),
    "cerebellum.set_kill_switch": (
        mcp_tools.set_kill_switch,
        "Request to toggle the kill switch (requires Telegram/dashboard approval).",
        {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "description": "True to enable, false to disable."},
                "reason": {"type": "string", "description": "Reason for the toggle request."},
            },
            "required": ["enabled", "reason"],
        },
    ),
    "cerebellum.snooze_proposal": (
        mcp_tools.snooze_proposal,
        "Snooze a proposal until a specified time.",
        {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string", "description": "ID of the proposal."},
                "until": {"type": "string", "description": "ISO 8601 timestamp to snooze until."},
            },
            "required": ["proposal_id", "until"],
        },
    ),
}


class CerebellumMCPServer:
    """MCP server that wraps CEREBELLUM core operations."""

    def __init__(self) -> None:
        self.server = Server(
            name="cerebellum",
            version="0.1.0",
            instructions=(
                "CEREBELLUM MCP server. Provides tools to interact with the "
                "CEREBELLUM proactive ops layer for autonomous agents. "
                "Read-only tools query events, episodes, patterns, and proposals. "
                "Write tools emit events, propose actions, and manage the kill switch."
            ),
        )
        self._register_handlers()

    def _register_handlers(self) -> None:
        """Register all MCP tool handlers."""

        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            return [
                Tool(
                    name=name,
                    description=desc,
                    inputSchema=schema,
                )
                for name, (_, desc, schema) in TOOL_DEFINITIONS.items()
            ]

        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any] | None = None) -> CallToolResult:
            handler_info = TOOL_DEFINITIONS.get(name)
            if handler_info is None:
                return CallToolResult(
                    content=[TextContent(type="text", text=f"Unknown tool: {name}")],
                    isError=True,
                )

            handler, _, _ = handler_info
            args = arguments or {}

            try:
                result = handler(**args)
                if isinstance(result, list):
                    text = json.dumps(result, indent=2, default=str)
                elif isinstance(result, dict):
                    text = json.dumps(result, indent=2, default=str)
                else:
                    text = str(result)
                return CallToolResult(content=[TextContent(type="text", text=text)])
            except Exception as exc:
                logger.exception("Tool %s failed: %s", name, exc)
                return CallToolResult(
                    content=[TextContent(type="text", text=f"Error: {exc}")],
                    isError=True,
                )

    async def run_stdio(self) -> None:
        """Run the server over stdio transport."""
        from mcp.server.stdio import stdio_server

        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                self.server.create_initialization_options(),
            )

    async def run_sse(self, port: int = 8765, bind: str = "127.0.0.1") -> None:
        """Run the server over SSE transport with auth."""
        from cerebellum.mcp.auth import check_rate_limit, get_mcp_token, validate_token
        from fastapi import FastAPI, HTTPException, Request
        from sse_starlette.sse import EventSourceResponse

        app = FastAPI(title="CEREBELLUM MCP Server (SSE)")
        expected_token = get_mcp_token()

        @app.middleware("http")
        async def auth_middleware(request: Request, call_next: Any) -> Any:
            if request.url.path == "/health":
                return await call_next(request)

            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                raise HTTPException(status_code=401, detail="Missing bearer token")
            provided_token = auth_header[7:]

            if expected_token and not validate_token(provided_token, expected_token):
                raise HTTPException(status_code=401, detail="Invalid token")

            client_ip = request.headers.get("X-Forwarded-For", request.client.host)
            if not check_rate_limit(client_ip):
                raise HTTPException(status_code=429, detail="Rate limit exceeded")

            return await call_next(request)

        @app.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok", "server": "cerebellum-mcp"}

        @app.get("/sse")
        async def sse_endpoint() -> EventSourceResponse:
            async def event_generator():
                yield {"event": "open", "data": "connected"}

            return EventSourceResponse(event_generator())

        import uvicorn

        logger.info("Starting MCP SSE server on %s:%d", bind, port)
        uvicorn.run(app, host=bind, port=port, log_level="info")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="CEREBELLUM MCP Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cerebellum-mcp --transport stdio
  cerebellum-mcp --transport sse --port 8765
        """,
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport type (default: stdio)",
    )
    parser.add_argument("--port", type=int, default=8765, help="Port for SSE transport (default: 8765)")
    parser.add_argument("--bind", type=str, default="127.0.0.1", help="Bind address for SSE (default: 127.0.0.1)")
    return parser.parse_args()


def main() -> int:
    """Main entry point for the MCP server."""
    args = parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    server = CerebellumMCPServer()

    import asyncio

    if args.transport == "stdio":
        logger.info("Starting MCP server with stdio transport")
        asyncio.run(server.run_stdio())
    else:
        logger.info("Starting MCP server with SSE transport on port %d", args.port)
        asyncio.run(server.run_sse(port=args.port, bind=args.bind))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
