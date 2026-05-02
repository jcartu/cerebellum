<div align="center">

![CEREBELLUM](logo.png)

</div>

---

# 🧠 CEREBELLUM

**A shadow cognition layer that watches your AI agent, learns its patterns, and proposes the next best action.**

CEREBELLUM sits beside your agent instead of inside it. It records every event, stitches them into episodes, finds causal links, and suggests what to do next. You set the rules. High risk actions wait for your approval. Safe ones happen automatically.

> "An agent without a cerebellum is a brain without reflexes. It thinks, but it cannot learn what usually follows what."

---

## Why this exists

AI agents don't remember their own behavior well. They don't notice that a browser crash at 3am usually follows a specific service error. They don't see when a user request leads to three failed tool calls. They can't schedule their own follow up work.

CEREBELLUM fixes this by running as a separate process that:

1. **Ingests** every event via NATS JetStream and SQLite.
2. **Clusters** events into episodes to find related entities like files, services, and people.
3. **Mines** causal patterns to see what events tend to precede others.
4. **Hypothesizes** actionable next steps using an LLM grounded in real context.
5. **Arbitrates** every plan against your YAML policy.
6. **Executes** approved plans in a hardened sandbox.

The result is an advisor that learns your system's rhythms. It does the obvious work for you and asks for permission on everything else.

---

## Architecture

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                           YOUR AI AGENT                                 │
│                    (emits events via NATS / HTTP)                       │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │ cerebellum.events.*
                                ▼
        ┌───────────────────────────────────────────────────┐
        │          🧠 CEREBELLUM (4 components)             │
        │                                                   │
        │  ┌─────────────┐      ┌──────────────────┐        │
        │  │ OBSERVATORY │─────▶│   EVENT STORE    │        │
        │  │  (NATS sub) │      │ SQLite WAL + JS  │        │
        │  └─────────────┘      └────────┬─────────┘        │
        │                                │                  │
        │                                ▼                  │
        │                      ┌───────────────────┐        │
        │                      │   HIPPOCAMPUS     │        │
        │                      │  KuzuDB graph +   │        │
        │                      │  episodes +       │        │
        │                      │  causal edges     │        │
        │                      └────────┬──────────┘        │
        │                                │                  │
        │                                ▼                  │
        │                      ┌───────────────────┐        │
        │                      │ PREFRONTAL CORTEX │        │
        │                      │  LLM hypothesis   │        │
        │                      │  generator (5min) │        │
        │                      └────────┬──────────┘        │
        │                                │                  │
        │                                ▼                  │
        │                      ┌───────────────────┐        │
        │                      │  BASAL GANGLIA    │        │
        │                      │  Policy arbiter   │        │
        │                      │  + kill switch    │        │
        │                      └────┬────┬────┬────┘        │
        │                           │    │    │             │
        │                 auto_exec │    │    │ discard     │
        │                           ▼    ▼                  │
        │                      ┌─────────────┐              │
        │                      │  TELEGRAM   │◀── approve   │
        │                      │  (you)      │    reject    │
        │                      └─────────────┘    snooze    │
        └───────────────────────────────────────────────────┘
```

| Component | Role | State |
| :--- | :--- | :--- |
| **Observatory** | Event ingest and NATS relay | `events.db` |
| **Hippocampus** | Episodic and causal memory | `hippocampus.kuzu` |
| **Prefrontal Cortex** | Hypothesis generation | `hypotheses.db` |
| **Basal Ganglia** | Policy gated action arbiter | `pending_approvals.json` |

---

## Quick start

Deploy CEREBELLUM in about five minutes.

### 1. Clone and install
```bash
git clone https://github.com/you/cerebellum ~/.openclaw/cerebellum
cd ~/.openclaw/cerebellum
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure secrets
Create a `.env` file with your keys. Use literal values without shell expansion.
```bash
cat > .env <<'EOF'
OPENROUTER_API_KEY=sk-or-v1-...
TELEGRAM_BOT_TOKEN=1234567:ABC...
TELEGRAM_CHAT_ID=-100123...
TELEGRAM_WEBHOOK_SECRET=$(openssl rand -hex 32)
TELEGRAM_ALLOWED_USER_IDS=12345678
DASHBOARD_TOKEN=$(openssl rand -hex 32)
CEREBELLUM_NATS_TOKEN=$(openssl rand -hex 32)
BRAVE_SEARCH_API_KEY=BSA...
EOF
chmod 0600 .env
```

### 3. Install services
```bash
sudo cp services/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cerebellum-observatory cerebellum-cortex
```

### 4. Verify
```bash
curl -H "Authorization: Bearer $DASHBOARD_TOKEN" http://127.0.0.1:18790/healthz
```

**Prerequisites**: NATS server with JetStream, Python 3.11+, KuzuDB, and an OpenRouter account.

---

## Configuration

### `config.json`
This file is read by every component.
```json
{
  "nats": { "host": "localhost", "port": 4222, "jetstream_domain": "" },
  "sqlite": { "events_db": "/path/to/events.db" },
  "dashboard": { "port": 18790 },
  "models": ["openai/gpt-4o", "anthropic/claude-3.5-sonnet"],
  "generation_interval_minutes": 5
}
```

### `policy.yaml`
The rulebook for the Basal Ganglia.
```yaml
global:
  enabled: true
  kill_switch_command: "/cerebellum-halt"
  max_actions_per_hour: 10
  max_llm_cost_per_day_usd: 5.0

forbidden_tools: ["shell.exec", "file.delete"]

auto_execute:
  min_confidence: 0.85
  max_cost: 0.3
  allowed_tools: ["memory.query", "web.search"]

stage_notify:
  min_confidence: 0.6
  max_cost: 0.8
  telegram:
    timeout_minutes: 60
```

---

## Security features

CEREBELLUM is paranoid by default. If an agent can act on its own, it must fail closed.

*   **SSRF Protection**: Every outbound URL is resolved once and checked against a public IP allowlist. Connections are pinned to the validated IP. Redirects are refused.
*   **Path Traversal**: All file reads resolve symlinks and check against a root allowlist. Forbidden paths like `/etc` or `.env` are hard denied.
*   **Tool Allowlist**: Only tools in your `auto_execute` list run without approval. Forbidden tools never run.
*   **Kill Switch**: A file backed, cross process lock stops everything instantly. You can toggle it from Telegram or the dashboard.
*   **Budget Caps**: We use a sliding window for action rates and a daily spend cap for LLMs.
*   **Telegram Auth**: We use secret tokens and user ID allowlists. SQLite tracks update IDs to prevent replay attacks.
*   **Dashboard Auth**: Bearer tokens are required. The dashboard binds to loopback by default.
*   **Response Caps**: Every HTTP response has a byte limit to prevent memory exhaustion.
*   **Atomic Writes**: State files are written using temp files and fsync to prevent corruption.

---

## API endpoints

All routes except healthz require `Authorization: Bearer $DASHBOARD_TOKEN`.

| Method | Path | Description |
| :--- | :--- | :--- |
| `GET` | `/` | HTMX dashboard |
| `GET` | `/healthz` | Liveness check (no auth) |
| `GET` | `/api/events` | Query events with `since` and `limit` |
| `GET` | `/api/events/stream` | Live event tail via SSE |
| `GET` | `/api/stats` | 24h event histogram |
| `GET` | `/timeline` | HTML event cards |
| `POST` | `/telegram/webhook` | Telegram callbacks |

---

## Event schema

Events follow a standard shape in SQLite and NATS.
```json
{
  "id": "uuid4",
  "timestamp": "2024-11-20T04:12:33.456+00:00",
  "type": "cerebellum.hypothesis",
  "payload": { "title": "Fix OOM", "action": "..." },
  "actor": "cerebellum.cortex",
  "context": { "source": "phase3" }
}
```

**Common types**:
*   `cerebellum.hypothesis`: A new proposal.
*   `cerebellum.action`: A decision made by the arbiter.
*   `cerebellum.execution`: Result of an automated task.
*   `cerebellum.approval.staged`: Waiting for your input on Telegram.
*   `cerebellum.kill_switch`: The system was halted or resumed.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
| :--- | :--- | :--- |
| NATS connection error | Server down or token wrong | Check `systemctl status nats-server` and your token. |
| Dashboard returns 401 | Token mismatch | Verify `DASHBOARD_TOKEN` in your `.env` file. |
| No hypotheses appearing | API key or quota issue | Check `journalctl -u cerebellum-cortex -f` for errors. |
| Telegram buttons fail | Webhook not set | Run the `setWebhook` curl command manually. |
| Everything is discarded | Policy is too strict | Lower the `min_confidence` in your `policy.yaml`. |
| Kill switch stuck | Flag file exists | Delete or update the `kill_switch.flag` file. |

---

## License and contributing

CEREBELLUM is unreleased software. It handles sensitive credentials and can modify your system. Only deploy it on hosts you control.

We welcome contributions. Please read the forthcoming `CONTRIBUTING.md` before submitting a pull request.

*"Memory without inference is a logbook. Inference without memory is a stranger at your door every morning. CEREBELLUM is neither."*
