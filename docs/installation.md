# Installation

This guide covers a full CEREBELLUM deployment: installing dependencies, setting up NATS, configuring secrets, optional Telegram integration, and starting the systemd services.

For a quick local test, see the [README](../README.md#quick-start). This document is for production deployment.

## System requirements

- **OS:** Linux (Ubuntu 22.04+ / Arch / similar). macOS works for development; production use is Linux-first.
- **Python:** 3.11 or 3.12.
- **Disk:** 1 GB minimum for code + small databases. Plan 5–10 GB if you ingest high-volume event streams.
- **Memory:** 1 GB for Observatory + 2 GB for Cortex/Arbiter. The systemd units cap at these levels.
- **CPU:** Any modern x86_64 or ARM64. CEREBELLUM is I/O bound, not CPU bound.

## 1. Install NATS with JetStream

CEREBELLUM uses NATS JetStream as its event bus. JetStream is required (regular NATS without JetStream won't work because we need durable subscriptions).

### Quick install (single host, dev or low-volume prod)

```bash
# Linux x86_64
curl -L https://github.com/nats-io/nats-server/releases/download/v2.10.20/nats-server-v2.10.20-linux-amd64.tar.gz \
  | tar xz && sudo mv nats-server-v2.10.20-linux-amd64/nats-server /usr/local/bin/
```

Create a config file at `/etc/nats/nats-server.conf`:

```
server_name: cerebellum-host
listen: 127.0.0.1:4222

jetstream {
  store_dir: /var/lib/nats/jetstream
  max_memory_store: 256MB
  max_file_store: 4GB
}

# Token-based auth. Replace with your own token (also in cerebellum's .env).
authorization {
  token: "REPLACE_WITH_YOUR_NATS_TOKEN"
}
```

Make sure the directory exists and is writable:

```bash
sudo mkdir -p /var/lib/nats/jetstream
sudo chown nats:nats /var/lib/nats/jetstream  # if running as the nats user
```

Start it:

```bash
sudo systemctl enable --now nats-server
# Or for a quick test: nats-server -c /etc/nats/nats-server.conf
```

### Production hardening (multi-host or untrusted network)

For any deployment beyond a single trusted host, you should also enable TLS. Generate certs with `mkcert` or your CA, then add to the NATS config:

```
tls {
  cert_file: "/etc/nats/server-cert.pem"
  key_file: "/etc/nats/server-key.pem"
  ca_file: "/etc/nats/ca.pem"
  verify_and_map: true
  timeout: 5
}
```

Update CEREBELLUM's `config.json` accordingly (see [Configuration](#5-configure)).

## 2. Install CEREBELLUM

```bash
git clone https://github.com/jcartu/cerebellum
cd cerebellum
make install
```

This creates `.venv/` and installs the package in editable mode along with dev dependencies. Verify:

```bash
make check
# → ruff lint clean, mypy strict clean, 700+ tests pass
```

## 3. Optional: set up the Telegram bot

If you want approval-via-Telegram (recommended), create a bot:

1. Talk to [@BotFather](https://t.me/BotFather) on Telegram. Use `/newbot`, follow prompts. Save the bot token.
2. Send your bot a message. Then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your chat ID.
3. Generate a webhook secret:
   ```bash
   openssl rand -hex 32
   ```
4. Set the webhook (replace placeholders):
   ```bash
   curl -X POST "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
     -d "url=https://your-public-host/telegram/webhook" \
     -d "secret_token=<WEBHOOK_SECRET>"
   ```

If you're running CEREBELLUM behind a reverse proxy, point the webhook URL at the proxy and forward to `127.0.0.1:18790/telegram/webhook`. CEREBELLUM binds to loopback by default for security.

## 4. Configure secrets

Create your `.env`:

```bash
cp .env.example .env
chmod 0600 .env
$EDITOR .env
```

Fill in:

```
# Required
OPENROUTER_API_KEY=sk-or-v1-...
DASHBOARD_TOKEN=$(openssl rand -hex 32)
CEREBELLUM_NATS_TOKEN=<the token from your nats-server.conf>

# Required if using Telegram
TELEGRAM_BOT_TOKEN=<from BotFather>
TELEGRAM_CHAT_ID=<your numeric chat id>
TELEGRAM_WEBHOOK_SECRET=<the secret you generated>
TELEGRAM_ALLOWED_USER_IDS=<your numeric telegram user id>

# Optional
BRAVE_SEARCH_API_KEY=<for the web.search tool>
CEREBELLUM_BASE_DIR=  # defaults to project dir
```

## 5. Configure runtime

```bash
cp config.example.json config.json
$EDITOR config.json
```

Settings worth reviewing:

- **`models`** — list of OpenRouter model slugs to use, in priority order (the proposer falls through on failure).
- **`generation_interval_minutes`** — how often the proposer runs. Default 5. For low-volume agents, 15 is fine.
- **`dashboard.port`** — default 18790. Change if it conflicts with another service.
- **`hippocampus.openrouter_model`** — the model used for LLM-generated Cypher in the episode store. Default `gpt-4o`. For cost, `gpt-4o-mini` works.

For NATS TLS, add to `config.json`:

```json
{
  "nats": {
    "host": "your-nats-host",
    "port": 4222,
    "tls": true,
    "tls_ca": "/path/to/ca.pem",
    "tls_cert": "/path/to/client-cert.pem",
    "tls_key": "/path/to/client-key.pem"
  }
}
```

You can also set TLS via env: `CEREBELLUM_NATS_TLS_CA`, `CEREBELLUM_NATS_TLS_CERT`, `CEREBELLUM_NATS_TLS_KEY`.

## 6. Configure policy

```bash
$EDITOR policy.yaml
```

Start strict and loosen over time. A good starting point:

```yaml
global:
  enabled: true
  kill_switch_command: "/cerebellum-halt"
  max_actions_per_hour: 5         # start low
  max_llm_cost_per_day_usd: 2.0   # start low

forbidden_tools:
  - shell.exec
  - file.delete
  - rasputin.commit_fact
  - rasputin.reflect

auto_execute:
  min_confidence: 0.90            # very high bar
  max_cost_usd: 0.10
  allowed_tools:
    - rasputin.search             # read-only
    - rasputin.recent_facts       # read-only
    - rasputin.entity_lookup      # read-only
    - notification.summarize      # idempotent

stage_notify:
  min_confidence: 0.65
  max_cost_usd: 0.50
  telegram:
    timeout_minutes: 60
```

After a week of operation, look at the Telegram queue. If you're approving most stage_notify proposals, lower `auto_execute.min_confidence` slightly. If you're rejecting most of them, raise the `stage_notify.min_confidence` floor.

## 7. Install systemd units

```bash
make systemd-install
```

This runs `scripts/install_systemd.sh`, which substitutes `__USER__`, `__CEREBELLUM_BASE_DIR__`, `__PYTHON__` in the template files and installs them to `~/.config/systemd/user/`.

Enable and start:

```bash
systemctl --user enable --now cerebellum-observatory cerebellum-cortex
systemctl --user status cerebellum-observatory cerebellum-cortex
```

For services that should survive logout (recommended for production):

```bash
sudo loginctl enable-linger $USER
```

## 8. Verify

```bash
# Health check
curl -H "Authorization: Bearer $DASHBOARD_TOKEN" http://127.0.0.1:18790/healthz
# → {"status":"ok"}

# Dashboard
open http://127.0.0.1:18790/
# (browser will need the bearer token; use a browser extension or curl for testing)

# Logs
journalctl --user -u cerebellum-observatory -f
journalctl --user -u cerebellum-cortex -f
```

After ~5 minutes, you should see the proposer's first cycle in the cortex logs. If your agent is emitting events to NATS (subject prefix `cerebellum.events.*`), they'll appear in the dashboard timeline.

## 9. Connect your agent

Your agent needs to publish JSON events to NATS on subjects matching the configured pattern. A minimal example in Python:

```python
import json
import nats
import asyncio
from datetime import datetime, timezone

async def emit(event_type: str, payload: dict, actor: str = "my-agent"):
    nc = await nats.connect("nats://localhost:4222", token="YOUR_NATS_TOKEN")
    js = nc.jetstream()
    event = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": event_type,
        "payload": payload,
        "actor": actor,
        "context": {},
    }
    await js.publish(
        f"cerebellum.events.{event_type}",
        json.dumps(event).encode(),
    )
    await nc.drain()

# Example
asyncio.run(emit("model.call", {"model": "claude-opus-4-7", "tokens_in": 1500}))
```

For agents in other languages, the wire format is the same: a JSON object with `id`, `timestamp` (ISO-8601 UTC), `type`, `payload`, `actor`, and optional `context`. Subject is `cerebellum.events.<type>`.

## 10. Day-2 operations

- **Approving / rejecting from Telegram.** When a proposal is staged, you'll get a Telegram message with inline buttons. Tap one. The arbiter records the decision and proceeds.
- **Kill switch.** Send `/cerebellum-halt` to your bot, or POST to the dashboard's kill-switch endpoint. The flag file flips and all auto-execute is suspended. Resume from the dashboard.
- **Budget exhaustion.** When the daily LLM budget is exhausted, the proposer pauses for the rest of the UTC day. You'll see a `cerebellum.budget_exhausted` event. Increase `max_llm_cost_per_day_usd` if this happens regularly.
- **Tuning the policy.** Re-read your `policy.yaml` weekly for the first month. The right values depend on your agent's behavior and your tolerance for false positives.

## Troubleshooting

| Symptom | Likely cause | Fix |
| :--- | :--- | :--- |
| `nats: connection refused` | NATS not running, or wrong port/token | `systemctl status nats-server`, check `.env` |
| Dashboard returns 401 | Token mismatch | Verify `DASHBOARD_TOKEN` in `.env` matches what you're sending |
| No proposals appearing | No events being ingested, or LLM calls failing | Check NATS subject pattern; tail cortex logs |
| Telegram callbacks fail | Webhook not registered, or wrong secret | Run the `setWebhook` curl command from step 3 |
| Everything is discarded | Policy too strict | Lower `min_confidence` thresholds in `policy.yaml` |
| `kill_switch.flag` exists | System was halted | `rm` the file, or use the dashboard "resume" button |
| Coverage XML written everywhere | pytest is being run from the wrong dir | Run from repo root, or use `make test` |
| `mypy` errors after editing | New code missing type hints | Add hints, or cite the offending lines and ask Opus / a reviewer |

## Hardening checklist

Before you trust CEREBELLUM with consequential actions:

- [ ] `.env` is `chmod 0600`, not in git, not in any backup.
- [ ] `DASHBOARD_TOKEN` is at least 32 hex chars and rotated quarterly.
- [ ] Dashboard binds to loopback (or behind a reverse proxy with auth).
- [ ] NATS uses TLS if reachable from any network you don't fully trust.
- [ ] Telegram webhook secret is set and the bot's user ID allowlist is configured.
- [ ] `policy.yaml` `forbidden_tools` includes everything that could write or delete.
- [ ] Daily budget cap is set to a number you'd be willing to lose if things go sideways.
- [ ] You've tested the kill switch (toggle, verify auto-execute pauses).
- [ ] You've reviewed the first week's `arbiter_decisions.jsonl` to verify the policy is doing what you think.
