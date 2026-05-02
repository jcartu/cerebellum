<div align="center">

![CEREBELLUM](logo.png)

# 🧠 CEREBELLUM

### *A shadow cognition layer that watches your AI agent, learns its patterns, and proposes the next best action.*

[![Status](https://img.shields.io/badge/status-alpha-orange)]()
[![Python](https://img.shields.io/badge/python-3.11+-blue)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-unreleased-red)]()
[![NATS](https://img.shields.io/badge/NATS-JetStream-green)](https://nats.io)

![CEREBELLUM Banner](banner.png)

</div>

---

> *"An agent without a cerebellum is a brain without reflexes. It thinks, but it cannot learn what usually follows what."*

CEREBELLUM sits **beside** your agent, not inside it. It records every event, stitches them into episodes, finds causal links, and suggests what to do next. You set the rules. High-risk actions wait for your approval. Safe ones happen automatically.

---

## ✨ Why this exists

AI agents don't remember their own behavior well. They don't notice that a browser crash at 3 AM usually follows a specific service error. They don't see when a user request leads to three failed tool calls. They can't schedule their own follow-up work.

CEREBELLUM fixes this by running as a separate process that:

1. 📥 **Ingests** every event via NATS JetStream and SQLite
2. 🧵 **Clusters** events into episodes to find related entities — files, services, people
3. 🔗 **Mines** causal patterns to see what events tend to precede others
4. 💡 **Hypothesizes** actionable next steps using an LLM grounded in real context
5. ⚖️ **Arbitrates** every plan against your YAML policy
6. 🏃 **Executes** approved plans in a hardened sandbox

The result is an advisor that learns your system's rhythms. It does the obvious work for you and asks for permission on everything else.

---

## 🏗️ Architecture

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

### Components at a glance

| Component | Role | Persistent State |
| :--- | :--- | :--- |
| 👁️ **Observatory** | Event ingest and NATS relay | `events.db` |
| 🧠 **Hippocampus** | Episodic and causal memory | `hippocampus.kuzu` |
| 💭 **Prefrontal Cortex** | Hypothesis generation | `hypotheses.db` |
| ⚖️ **Basal Ganglia** | Policy-gated action arbiter | `pending_approvals.json` |

---

## 🚀 Quick start

Deploy CEREBELLUM in about five minutes.

### 1️⃣ Clone and install

```bash
git clone https://github.com/jcartu/cerebellum ~/.openclaw/cerebellum
cd ~/.openclaw/cerebellum
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2️⃣ Configure secrets

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

### 3️⃣ Install services

```bash
sudo cp services/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cerebellum-observatory cerebellum-cortex
```

### 4️⃣ Verify

```bash
curl -H "Authorization: Bearer $DASHBOARD_TOKEN" http://127.0.0.1:18790/healthz
```

> **Prerequisites** — NATS server with JetStream, Python 3.11+, KuzuDB, and an OpenRouter account.

---

## ⚙️ Configuration

### `config.json` — read by every component

```json
{
  "nats": { "host": "localhost", "port": 4222, "jetstream_domain": "" },
  "sqlite": { "events_db": "/path/to/events.db" },
  "dashboard": { "port": 18790 },
  "models": ["openai/gpt-4o", "anthropic/claude-3.5-sonnet"],
  "generation_interval_minutes": 5
}
```

### `policy.yaml` — the rulebook for the Basal Ganglia

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

## 🔒 Security features

CEREBELLUM is **paranoid by default**. If an agent can act on its own, it must fail closed.

- 🛡️ **SSRF Protection** — Every outbound URL is resolved once and checked against a public IP allowlist. Connections are pinned to the validated IP. Redirects are refused.
- 📁 **Path Traversal Defense** — All file reads resolve symlinks and check against a root allowlist. Forbidden paths like `/etc` or `.env` are hard-denied.
- ✅ **Tool Allowlist** — Only tools in your `auto_execute` list run without approval. Forbidden tools never run.
- 🛑 **Kill Switch** — A file-backed, cross-process lock stops everything instantly. Toggle it from Telegram or the dashboard.
- 💰 **Budget Caps** — Sliding window for action rates, daily spend cap for LLMs.
- 🔑 **Telegram Auth** — Secret tokens and user-ID allowlists. SQLite tracks update IDs to prevent replay attacks.
- 🔐 **Dashboard Auth** — Bearer tokens required. Binds to loopback by default.
- 📏 **Response Caps** — Every HTTP response has a byte limit to prevent memory exhaustion.
- 💾 **Atomic Writes** — State files use temp files and fsync to prevent corruption.

---

## 🌐 API endpoints

All routes except `healthz` require `Authorization: Bearer $DASHBOARD_TOKEN`.

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

## 📦 Event schema

Events follow a standard shape across SQLite and NATS:

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

### Common event types

| Type | Meaning |
| :--- | :--- |
| `cerebellum.hypothesis` | A new proposal |
| `cerebellum.action` | A decision made by the arbiter |
| `cerebellum.execution` | Result of an automated task |
| `cerebellum.approval.staged` | Waiting for your input on Telegram |
| `cerebellum.kill_switch` | System was halted or resumed |

---

## 🔧 Troubleshooting

| Symptom | Likely cause | Fix |
| :--- | :--- | :--- |
| NATS connection error | Server down or wrong token | Check `systemctl status nats-server` and your token |
| Dashboard returns 401 | Token mismatch | Verify `DASHBOARD_TOKEN` in your `.env` |
| No hypotheses appearing | API key or quota issue | Check `journalctl -u cerebellum-cortex -f` |
| Telegram buttons fail | Webhook not set | Run the `setWebhook` curl command manually |
| Everything is discarded | Policy too strict | Lower `min_confidence` in `policy.yaml` |
| Kill switch stuck | Flag file exists | Delete or update `kill_switch.flag` |

---

## 📜 License and contributing

> ⚠️ **CEREBELLUM is unreleased software.** It handles sensitive credentials and can modify your system. Deploy it only on hosts you control.

Contributions are welcome. Please read the forthcoming `CONTRIBUTING.md` before opening a pull request.

---

<div align="center">

*"Memory without inference is a logbook.*
*Inference without memory is a stranger at your door every morning.*
**CEREBELLUM is neither."**

</div>
