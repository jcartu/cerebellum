# Configuration Reference

CEREBELLUM is configured by three files. This page is the complete reference for each.

## `.env` — secrets only

| Variable | Required | Description |
| :--- | :--- | :--- |
| `OPENROUTER_API_KEY` | Yes | OpenRouter API key for all LLM calls. |
| `DASHBOARD_TOKEN` | Yes | Bearer token for dashboard auth. Generate with `openssl rand -hex 32`. |
| `CEREBELLUM_NATS_TOKEN` | Yes | Token configured in `nats-server.conf`. |
| `CEREBELLUM_BASE_DIR` | No | Override the base directory for state files. Defaults to project root. |
| `TELEGRAM_BOT_TOKEN` | If using Telegram | Bot token from @BotFather. |
| `TELEGRAM_CHAT_ID` | If using Telegram | Numeric chat ID for staged proposals. |
| `TELEGRAM_WEBHOOK_SECRET` | If using Telegram | Secret token for webhook auth. Generate with `openssl rand -hex 32`. |
| `TELEGRAM_ALLOWED_USER_IDS` | If using Telegram | Comma-separated numeric user IDs allowed to approve. |
| `BRAVE_SEARCH_API_KEY` | If using `web.search` | Brave Search API key. |
| `CEREBELLUM_NATS_TLS_CA` | If using NATS TLS | Path to CA cert (PEM). |
| `CEREBELLUM_NATS_TLS_CERT` | If using mTLS | Path to client cert (PEM). |
| `CEREBELLUM_NATS_TLS_KEY` | If using mTLS | Path to client private key (PEM). |

`.env` should be `chmod 0600` and never committed to git. The `.env.example` file shows the expected shape with placeholder values.

## `config.json` — runtime settings

```json
{
  "nats": {
    "host": "localhost",
    "port": 4222,
    "jetstream_domain": "",
    "tls": false,
    "tls_ca": "",
    "tls_cert": "",
    "tls_key": ""
  },
  "sqlite": {
    "events_db": "events.db"
  },
  "event_types": [
    "cron.start", "cron.end", "cron.error",
    "telegram.inbound", "telegram.outbound",
    "browser.action", "model.call",
    "memory.write", "file.edit", "gpu.state",
    "cerebellum.hypothesis", "cerebellum.action", "cerebellum.approval"
  ],
  "dashboard": {
    "port": 18790,
    "host": "127.0.0.1"
  },
  "arbiter_loop": {
    "sleep_seconds": 300,
    "sleep_jitter_fraction": 0.1
  },
  "hippocampus": {
    "openrouter_url": "https://openrouter.ai/api/v1/chat/completions",
    "openrouter_model": "openai/gpt-4o"
  },
  "models": ["openai/gpt-4o", "anthropic/claude-opus-4-7"],
  "openrouter_base_url": "https://openrouter.ai/api/v1",
  "generation_interval_minutes": 5,
  "app_name": "CEREBELLUM",
  "site_url": "https://localhost/cerebellum"
}
```

### Field reference

| Field | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `nats.host` | string | `"localhost"` | NATS server hostname. |
| `nats.port` | int | `4222` | NATS server port. |
| `nats.jetstream_domain` | string | `""` | Optional JetStream domain for multi-cluster setups. |
| `nats.tls` | bool | `false` | Enable TLS for the NATS connection. |
| `nats.tls_ca` | string | `""` | Path to CA cert for server verification. |
| `nats.tls_cert` | string | `""` | Path to client cert for mTLS. |
| `nats.tls_key` | string | `""` | Path to client private key for mTLS. |
| `sqlite.events_db` | string | `"events.db"` | Path to events database (relative to base dir). |
| `event_types` | array | (see above) | Whitelist of event types accepted from NATS. |
| `dashboard.port` | int | `18790` | Dashboard HTTP port. |
| `dashboard.host` | string | `"127.0.0.1"` | Dashboard bind address. **Don't change to `0.0.0.0` without a reverse proxy.** |
| `arbiter_loop.sleep_seconds` | int | `300` | Base sleep between arbiter cycles. |
| `arbiter_loop.sleep_jitter_fraction` | float | `0.1` | Random jitter as a fraction of sleep_seconds. |
| `hippocampus.openrouter_model` | string | `"openai/gpt-4o"` | Model for LLM-generated Cypher in episode store. |
| `models` | array | (see above) | Proposer model fallback list, in priority order. |
| `generation_interval_minutes` | int | `5` | How often the proposer runs. |

## `policy.yaml` — execution rulebook

```yaml
global:
  enabled: true
  kill_switch_command: "/cerebellum-halt"
  max_actions_per_hour: 10
  max_llm_cost_per_day_usd: 5.0

forbidden_tools:
  - shell.exec
  - file.delete
  - rasputin.commit_fact
  - rasputin.reflect

auto_execute:
  min_confidence: 0.85
  max_cost_usd: 0.30
  allowed_tools:
    - rasputin.search
    - rasputin.recent_facts
    - rasputin.entity_lookup
    - rasputin.episode_summary
    - http.get
    - notification.summarize
    - memory.query

stage_notify:
  min_confidence: 0.60
  max_cost_usd: 0.80
  telegram:
    timeout_minutes: 60
    callback_url: "" # auto-derived from dashboard config
```

### Field reference

#### `global`

| Field | Type | Description |
| :--- | :--- | :--- |
| `enabled` | bool | Master switch. If `false`, all proposals are discarded. |
| `kill_switch_command` | string | Telegram command that toggles the kill switch. |
| `max_actions_per_hour` | int | Sliding-window cap on auto-executed actions. |
| `max_llm_cost_per_day_usd` | float | Daily LLM spend cap. Resets at UTC midnight. |

#### `forbidden_tools`

A list of tool names that **never** execute, regardless of approval. Use this for anything destructive or expensive.

#### `auto_execute`

A proposal that meets all of these criteria runs without human approval:

- `confidence >= min_confidence` (after Platt calibration if available)
- `estimated_execution_cost_usd <= max_cost_usd`
- All tools in `tools_required` are in `allowed_tools` and none are in `forbidden_tools`
- Reversibility: `reversibility == "reversible"` or `"idempotent"`

#### `stage_notify`

A proposal that doesn't qualify for `auto_execute` but meets `stage_notify` criteria gets sent to Telegram for approval. If approved within `telegram.timeout_minutes`, it executes (with the same tool checks). If rejected or timed out, it's discarded.

A proposal that doesn't qualify for either is discarded immediately.

## Tool reference

The arbiter dispatches to handlers based on `tools_required` in the proposal. Available handlers:

| Tool | Side effects | Default policy | Notes |
| :--- | :--- | :--- | :--- |
| `rasputin.search` | Read | auto | Search RASPUTIN memory. |
| `rasputin.recent_facts` | Read | auto | List recent commits to RASPUTIN. |
| `rasputin.entity_lookup` | Read | auto | Resolve an entity in RASPUTIN. |
| `rasputin.episode_summary` | Read | auto | Get RASPUTIN's view of recent activity. |
| `rasputin.commit_fact` | Write | forbidden | Commit a fact to RASPUTIN. Always requires approval. |
| `rasputin.reflect` | Write | forbidden | Trigger a reflection cycle. Always requires approval. |
| `http.get` | Read (external) | auto | HTTP GET via SSRF-pinned client. |
| `web.search` | Read (external) | stage | Brave Search query. |
| `memory.query` | Read | auto | Qdrant vector search (if configured). |
| `notification.send` | Send | stage | Send arbitrary Telegram message. |
| `notification.summarize` | Send | auto | Summarize last hour of events to Telegram. Idempotent. |
| `proposal.snooze` | State | auto | Mark a proposal as "remind me later." |
| `model.call` | Read | stage | Make an additional LLM call. |
| `file.read` | Read | auto | Read a file in `${CEREBELLUM_BASE_DIR}` allowlist. |

To override the default for any tool, list it in `auto_execute.allowed_tools`, `forbidden_tools`, or neither (which defaults to `stage_notify` if it meets `stage_notify` thresholds).

## Environment variables that affect runtime

Beyond the secrets in `.env`, a few env vars control behavior:

| Variable | Default | Description |
| :--- | :--- | :--- |
| `CEREBELLUM_BASE_DIR` | (project dir) | Where state files live. |
| `CEREBELLUM_LOG_LEVEL` | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `CEREBELLUM_DRY_RUN` | `0` | If `1`, the arbiter logs decisions but does not execute. |

Set these in `.env` or pass them in the systemd `Environment=` directive.
