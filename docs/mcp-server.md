# MCP Server

CEREBELLUM exposes an MCP (Model Context Protocol) server that allows AI assistants to query and interact with the system through standardized tool calls.

## Transports

### stdio (Default)
Used by Claude Desktop, Claude Code, and other MCP clients.

```bash
cerebellum-mcp
```

### SSE
HTTP-based transport for remote clients.

```bash
cerebellum-mcp --transport sse --host 0.0.0.0 --port 8765
```

Requires `CEREBELLUM_MCP_TOKEN` environment variable for Bearer token authentication.

## Tools

### Read-Only Tools

| Tool | Description |
|------|-------------|
| `cerebellum.recent_events` | Query recent events from the event bus |
| `cerebellum.recent_episodes` | Query recent episodes from the episode store |
| `cerebellum.successor_patterns` | Query successor patterns for an event type |
| `cerebellum.pending_proposals` | List proposals awaiting approval |
| `cerebellum.recent_proposals` | Query recent proposals from the arbiter |
| `cerebellum.kill_switch_state` | Check the current kill switch state |
| `cerebellum.system_metrics` | Get system health metrics |
| `cerebellum.entity_lookup` | Look up an entity in the knowledge graph |

### Write Tools

| Tool | Description |
|------|-------------|
| `cerebellum.emit_event` | Emit an event directly into the event bus |
| `cerebellum.propose_action` | Submit a proposal for action through the policy arbiter |
| `cerebellum.set_kill_switch` | Request to toggle the kill switch (requires approval) |
| `cerebellum.snooze_proposal` | Snooze a proposal until a specified time |

## Security

### Kill Switch Protection
The `set_kill_switch` tool NEVER directly toggles the kill switch. It always returns `pending_approval` and requires confirmation via Telegram or the dashboard. This is enforced by setting `confidence=0.0` which forces the policy arbiter to stage the proposal for notification.

### Authentication
- SSE transport requires a Bearer token (`CEREBELLUM_MCP_TOKEN`)
- Token comparison uses constant-time comparison to prevent timing attacks
- Rate limiting: 60 requests per minute per IP address

### Input Validation
- All write tools validate required fields before execution
- Event types must be non-empty strings
- Payloads must be dictionaries
- Timestamps must be valid ISO 8601 format

## Configuration

| Environment Variable | Description |
|---------------------|-------------|
| `CEREBELLUM_MCP_TOKEN` | Bearer token for SSE transport authentication |
| `CEREBELLUM_CONFIG` | Path to config.json (default: project root) |
| `CEREBELLUM_POLICY` | Path to policy.yaml (default: project root) |

## Example Usage

### Query Recent Events
```json
{
  "tool": "cerebellum.recent_events",
  "arguments": {
    "limit": 10,
    "since": "2026-05-01T00:00:00Z"
  }
}
```

### Submit a Proposal
```json
{
  "tool": "cerebellum.propose_action",
  "arguments": {
    "title": "Deploy new handler",
    "description": "Deploy the updated policy handler to production",
    "plan": "1. Run tests\n2. Deploy to staging\n3. Verify metrics\n4. Deploy to production",
    "confidence": 0.8
  }
}
```

### Check Kill Switch State
```json
{
  "tool": "cerebellum.kill_switch_state",
  "arguments": {}
}
```

## Testing

Run MCP tests:
```bash
pytest tests/mcp/ -v
```

Coverage target: 70% for MCP module (currently 77% for tools.py, 94% for auth.py).
