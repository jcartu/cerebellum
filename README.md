<div align="center">

![CEREBELLUM](logo.png)

# CEREBELLUM

**A proactive ops layer for autonomous agents.**

CEREBELLUM watches what your agent does, learns the patterns, and proposes the next move — with a human-in-the-loop kill switch on every action that matters.

[![Status](https://img.shields.io/badge/status-alpha-orange)](#status)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-615%20passing-brightgreen)](#testing)
[![Coverage](https://img.shields.io/badge/coverage-81%25-brightgreen)](#testing)
[![mypy](https://img.shields.io/badge/mypy-strict-blue)](https://mypy-lang.org)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

![CEREBELLUM Banner](banner.png)

</div>

---

## Table of contents

- [What this is](#what-this-is)
- [Who this is for](#who-this-is-for)
- [Where it sits in your stack](#where-it-sits-in-your-stack)
- [How it relates to other memory systems](#how-it-relates-to-other-memory-systems)
- [Why this exists](#why-this-exists)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Security](#security)
- [Status and roadmap](#status-and-roadmap)
- [Testing](#testing)
- [Documentation](#documentation)
- [License](#license)

---

## What this is

CEREBELLUM is a sidecar service that runs alongside an autonomous AI agent. It does four things:

1. **Records every event** the agent emits (and any system events you forward to it) into a durable, queryable store.
2. **Mines successor patterns** — what tends to happen after what — using PrefixSpan with lift filtering. This is sequential pattern mining, not causal inference.
3. **Proposes next actions** by feeding recent context, current episodes, and relevant patterns to an LLM. Every proposal must cite specific event IDs (grounding), and a second model verifies the proposal before it's stored.
4. **Arbitrates execution** through a YAML policy with three lanes: auto-execute (read-only, low-cost, high-confidence actions), stage-and-approve (anything with side effects routes through Telegram for your approval), and discard (low-confidence or budget-exhausted proposals).

Everything is observable. Everything is reversible by design (or refuses to auto-execute). A file-backed kill switch halts the system in milliseconds, cross-process.

What CEREBELLUM is **not**: a memory store for your agent's conversations or facts. That's a separate concern — see [Where it sits in your stack](#where-it-sits-in-your-stack).

---

## Who this is for

CEREBELLUM is for one specific kind of operator:

- You run **one or more long-lived autonomous agents** that emit events into your system. Examples: a Claude Code instance running as a coding agent, a research agent, a customer support bot, a DevOps automation.
- You want **proactive behavior** — the agent (or something next to it) noticing things and acting — but you do **not** want unsupervised autonomy. Every consequential action goes through you.
- You're comfortable running a **systemd service or container** on a host you control. CEREBELLUM is single-host first; multi-host is possible but not the default.
- You have **NATS JetStream** available (or are willing to run it). NATS is the event bus.
- You think about your agent's reliability the way you'd think about a production database. You want kill switches, audit logs, budget caps, and an approval queue.

If you're building chatbots, fine-tuning models, or running batch jobs, CEREBELLUM is overkill. If you're operating an agent that you check on once a day and want to know "what would my agent suggest doing next, given everything it's seen, and can I approve or reject those suggestions on my phone," CEREBELLUM is exactly what you want.

---

## Where it sits in your stack

CEREBELLUM is the **action proposal and gate** layer. It sits **between** your agent's memory layer and your agent's tools.

```
   ┌──────────────────────────────────────────────────────────────────┐
   │                         YOUR AGENT                               │
   │                                                                  │
   │   ┌─────────────────┐   ┌─────────────────┐   ┌──────────────┐  │
   │   │ CONVERSATIONAL  │   │  SEMANTIC /     │   │   TOOLS /    │  │
   │   │     MEMORY      │   │  EPISODIC       │   │   ACTIONS    │  │
   │   │                 │   │  MEMORY         │   │              │  │
   │   │  Letta /        │   │  RASPUTIN /     │   │  MCP servers │  │
   │   │  Mem0 /         │   │  Mem0 /         │   │  function    │  │
   │   │  Zep            │   │  Graphiti       │   │  calling     │  │
   │   └─────────────────┘   └─────────────────┘   └──────────────┘  │
   │                                                                  │
   └──────────────┬───────────────────────────────────────────────────┘
                  │ events (NATS)
                  ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │                         CEREBELLUM                               │
   │                                                                  │
   │  Records ─→ Episodes ─→ Patterns ─→ Proposals ─→ Policy ─→ ?     │
   │                                                                  │
   │                                                  │               │
   │                       auto-execute  ◄────────────┤               │
   │                       stage to you  ◄────────────┤               │
   │                       discard       ◄────────────┘               │
   │                                                                  │
   └──────────────────────────────────────────────────────────────────┘
                  │
                  ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │                   YOU (Telegram, dashboard)                      │
   │                  approve / reject / kill-switch                  │
   └──────────────────────────────────────────────────────────────────┘
```

Your agent already knows things. CEREBELLUM watches what your agent does and asks "given the pattern, should we do X next?" — then either does it (read-only, cheap, high-confidence) or asks you (everything else).

---

## How it relates to other memory systems

CEREBELLUM is **not** a replacement for any of these. It's a complement. Here's how each one differs in scope:

| System | Primary scope | What it stores | Where CEREBELLUM fits |
| :--- | :--- | :--- | :--- |
| [**RASPUTIN**](https://github.com/jcartu/rasputin-memory) | Long-term agent memory | Facts, entities, episodes from conversations and tool use | RASPUTIN is the agent's **memory**. CEREBELLUM is the agent's **reflexes** — it queries RASPUTIN as a tool but doesn't store the same kind of data. They run alongside. |
| [**Letta**](https://github.com/letta-ai/letta) (formerly MemGPT) | Conversational agent runtime with memory hierarchy | In-context, archival, recall memory tiers | Letta is an agent **framework**. CEREBELLUM observes a Letta agent's events and proposes actions on top of it. |
| [**Mem0**](https://github.com/mem0ai/mem0) | LLM-app memory layer | Facts and preferences extracted from conversations | Mem0 stores **what the user said and prefers**. CEREBELLUM stores **what the system did and what tends to follow**. Different axes; both can run together. |
| [**Zep**](https://github.com/getzep/zep) | Long-term agent memory with temporal knowledge graph | Sessions, facts, entities, relationships over time | Zep is the **historical record** the agent reasons over. CEREBELLUM is the **proactive proposal layer** that reasons over operational events. |
| [**Graphiti**](https://github.com/getzep/graphiti) | Temporal knowledge graphs for agents | Facts with bi-temporal validity | Graphiti is graph-shaped memory of the world. CEREBELLUM is sequence-shaped memory of the agent's operational behavior. |
| [**Hindsight**](https://github.com/hindsightagent) | Memory benchmark + reference architectures | Memory representations for LoCoMo / LongMemEval | Hindsight is a **benchmark and architecture reference**. CEREBELLUM is a **deployed system** with a different purpose entirely. |

**Recommended pairing.** If you're running an autonomous agent in production, the strongest stack is: an agent framework (Letta, Claude Code, your own) + a memory layer (RASPUTIN, Mem0, or Zep depending on whether you care more about facts vs sessions vs graphs) + CEREBELLUM as the proactive proposal/gate layer.

CEREBELLUM was built to sit alongside [RASPUTIN Memory](https://github.com/jcartu/rasputin-memory), but its event interface is generic — anything that publishes JSON events to NATS can drive it.

---

## Why this exists

Three problems with autonomous agents in production:

**1. Agents don't notice their own patterns.**
A model gets called, fails, gets called again with a slight prompt variation, fails differently. A deployment finishes, and 90 seconds later the same alert fires that fired the last three times. An agent's own memory is optimized for *its task* (answering questions, completing actions), not for *its operational behavior*. CEREBELLUM watches the operational behavior.

**2. Fully autonomous agents are too risky.**
"Let the agent do whatever it wants" is the AGI fantasy version. The production version is "let the agent suggest, let me approve." But most agent frameworks don't have a structured approval queue. CEREBELLUM has one: every proposal that has side effects gets staged with a clear summary, an "approve / reject / snooze" Telegram inline keyboard, a kill switch, and a budget cap.

**3. There's no good "what should the agent do next" layer.**
Agents take actions inside conversations or in response to triggers. They don't typically have a process that, every five minutes, reads the recent activity and asks "given everything, is there something useful we should do now?" CEREBELLUM is that process, with grounding and verification baked in to keep proposals from being LLM hallucinations.

The result: an operator who can supervise an autonomous agent the way a senior engineer supervises a junior — by reviewing a queue of proposed actions, approving the good ones, rejecting the rest, and never being surprised by what the system is doing.

---

## Architecture

```
                          YOUR AGENT (any framework)
                                 │
                                 │  publishes events
                                 ▼
                       ┌─────────────────────┐
                       │      EVENT BUS      │  NATS JetStream subscriber
                       │     event_bus.py    │  → events.db (SQLite WAL)
                       └──────────┬──────────┘
                                  │
                                  ▼
                       ┌─────────────────────┐
                       │    EPISODE STORE    │  Time-clustered episodes
                       │   episode_store.py  │  → KuzuDB graph
                       │                     │  → entities + edges
                       └──────────┬──────────┘
                                  │
                                  ▼
                       ┌─────────────────────┐
                       │       MINING        │  PrefixSpan + lift filter
                       │      mining.py      │  on (entity, event_type) pairs
                       │                     │  → SuccessorEdge nodes
                       └──────────┬──────────┘
                                  │
                                  ▼
                       ┌─────────────────────┐
                       │      PROPOSER       │  Grounded LLM proposals
                       │     proposer.py     │  with evidence_event_ids
                       │                     │  + cheap-model verifier
                       └──────────┬──────────┘
                                  │
                                  ▼
                       ┌─────────────────────┐
                       │   POLICY ARBITER    │  YAML rules + kill switch
                       │  policy_arbiter.py  │  + budget caps
                       └────┬─────┬──────┬───┘
                            │     │      │
                  auto      │     │      │  discard
                  execute  ◄┘     │      └►
                                  │
                                  ▼
                       ┌─────────────────────┐
                       │      TELEGRAM       │  Approval queue
                       │   ui/dashboard.py   │  + dashboard
                       └──────────┬──────────┘
                                  │
                                  ▼
                       ┌─────────────────────┐
                       │   FEEDBACK LOOP     │  Outcomes → calibration
                       │  feedback_loop.py   │  Platt scaling on confidence
                       └─────────────────────┘
```

### Components

| Component | Role | Persistent state |
| :--- | :--- | :--- |
| **EventBus** | NATS JetStream subscriber + SQLite WAL writer | `events.db` |
| **EpisodeStore** | Time-clustered episodes, KuzuDB graph, entity resolution | `graph/` (KuzuDB) |
| **Mining** | PrefixSpan with lift filtering, shuffle baselines | (in episode store) |
| **Proposer** | Grounded LLM proposals with evidence requirements | `hypotheses.db` |
| **GroundingVerifier** | Cheap-model second-pass verification of proposals | (in-memory) |
| **PolicyArbiter** | YAML policy, kill switch, budget caps, action dispatch | `arbiter_decisions.jsonl`, `kill_switch.flag` |
| **FeedbackLoop** | Outcome tracking, Platt-scaled calibration | `feedback.db` |
| **Dashboard** | FastAPI + HTMX, Telegram webhook | (in-memory + state files) |

For full architectural detail, see [`docs/architecture.md`](docs/architecture.md).

---

## Quick start

### Prerequisites

- Python 3.11 or 3.12
- A NATS server with JetStream enabled (see [docs/installation.md](docs/installation.md) for setup)
- An OpenRouter API key (or any OpenAI-compatible endpoint)
- Optional: a Telegram bot for approvals

### 1. Install

```bash
git clone https://github.com/jcartu/cerebellum && cd cerebellum
make install            # creates .venv, installs the package + dev deps
```

### 2. Configure

```bash
cp config.example.json config.json
cp .env.example .env
chmod 0600 .env
# edit both files; .env holds secrets, config.json holds runtime settings
```

### 3. Run

For local testing:

```bash
make run-observatory    # event bus + dashboard
# in another terminal:
make run-arbiter        # proposer + policy arbiter loop
```

For production deployment, use the systemd installer:

```bash
make systemd-install
systemctl --user enable --now cerebellum-observatory cerebellum-cortex
```

### 4. Verify

```bash
curl -H "Authorization: Bearer $DASHBOARD_TOKEN" http://127.0.0.1:18790/healthz
# → {"status":"ok"}
```

Open the dashboard at `http://127.0.0.1:18790/` (with the bearer token) to see the live event stream.

For a complete walkthrough including NATS setup, Telegram bot configuration, and TLS hardening, see [`docs/installation.md`](docs/installation.md).

---

## Configuration

CEREBELLUM is configured by three files:

- **`.env`** — secrets only (API keys, tokens). Never committed. See `.env.example`.
- **`config.json`** — runtime settings (NATS host, model selection, intervals). See `config.example.json`.
- **`policy.yaml`** — the rulebook for what auto-executes, what gets staged, what gets discarded.

A minimal `policy.yaml`:

```yaml
global:
  enabled: true
  kill_switch_command: "/cerebellum-halt"
  max_actions_per_hour: 10
  max_llm_cost_per_day_usd: 5.0

forbidden_tools:
  - shell.exec
  - file.delete
  - rasputin.commit_fact   # writes require approval
  - rasputin.reflect       # potentially destructive

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

stage_notify:
  min_confidence: 0.60
  max_cost_usd: 0.80
  telegram:
    timeout_minutes: 60
```

For the full configuration reference, see [`docs/configuration.md`](docs/configuration.md).

---

## Security

CEREBELLUM is paranoid by default. Threat-model summary:

- **SSRF protection.** Every outbound URL is resolved once, the IP checked against an allowlist (rejecting RFC1918, loopback, link-local, multicast, IPv6 ULA, and cloud metadata addresses), and the connection pinned to the validated IP. Redirects are refused.
- **Path traversal defense.** All file reads resolve symlinks and check against an explicit root allowlist. Forbidden prefixes (`/etc`, `/root`, `/proc`, `/sys`, `/boot`, `/var/log`) are hard-denied.
- **Tool allowlist.** Only tools listed in `auto_execute.allowed_tools` run without approval. Anything in `forbidden_tools` never runs.
- **Kill switch.** A `flock`-protected file flag halts the system instantly, cross-process. Toggle from Telegram or the dashboard.
- **Budget caps.** Sliding-window action rate limit, daily LLM spend cap, per-tool execution-cost estimates.
- **Telegram authentication.** HMAC secret tokens, user-ID allowlists, and SQLite-backed `update_id` deduplication to prevent webhook replay.
- **Dashboard authentication.** Bearer tokens required for all routes except `/healthz`. Binds to loopback by default.
- **Cypher safety filter.** LLM-generated graph queries are rejected if they contain blocked keywords as identifiers (string literals are stripped before checking, so `WHERE n.name = "DROP table"` is correctly accepted). `CALL` queries are restricted to a known-read-only procedure whitelist.
- **NATS TLS.** Optional mutual TLS for all event bus traffic. Server verification or client cert auth via config or environment variables.
- **Atomic writes.** State files use temp-file + fsync + rename to prevent corruption.
- **Response caps.** Every outbound HTTP response has a byte limit to prevent memory exhaustion.

For the full security threat model and audit history, see [`docs/security-model.md`](docs/security-model.md) and [`SECURITY.md`](SECURITY.md).

---

## Status and roadmap

**Current state:** alpha. The system runs cleanly, has 615 tests at 81% coverage, mypy strict on all first-party modules, property tests for safety-critical invariants, and a 10,000-iteration Telegram fuzzer. It is being run by the author against a production agent.

**Known limitations** (in scope for future work):

- Successor patterns are sequential associations with lift filtering, not causal inference. Patterns surface "B tends to follow A" — they don't prove "A causes B."
- Confidence calibration via Platt scaling activates only after ≥100 outcomes per proposer model. Until then, raw confidence is used.
- Reversal detection (was an auto-executed action later undone?) is a stub. Currently the auto-execute surface is read-only by design, so there's nothing to detect; this becomes relevant when destructive tools are added.
- Single-host deployment is assumed. Multi-host requires NATS TLS + cert pinning (config flags exist but the deployment guide is not written yet).

**Roadmap:**

- **Phase 7 (planned):** real Cypher tokenizer (replace regex-based string-literal stripping), mutual TLS for NATS, Telegram webhook IP allowlist + replay protection beyond the existing `update_id` dedup, ≥90% coverage on security-critical paths, SAST + dependency audit + secret scanning in CI.
- **Phase 8+:** integration with multiple memory backends, learnable verifier prompts, per-user policy profiles, multi-host deployment guide.

See [`CHANGELOG.md`](CHANGELOG.md) for what shipped in each phase.

---

## Testing

```bash
make test               # 615 unit + property tests, ~2 min
make test-integration   # requires running NATS + KuzuDB
make typecheck          # mypy strict
make lint               # ruff
make check              # all of the above
```

The test suite includes:

- **Unit tests:** behavioral coverage of every module, ≥80% global, ≥75% on `policy_arbiter.py` and `dashboard.py`.
- **Property tests** (Hypothesis): RateLimiter invariants, DailyCostTracker arithmetic, Cypher filter consistency, SSRF validator coverage of every private/loopback/link-local/metadata IP class.
- **Fuzz tests:** 10,000-iteration Telegram webhook fuzzer with reproducible seeds, exercising oversized fields, missing fields, type confusion, Unicode tricks, replay attacks, and recursive payloads.

CI runs on Python 3.11 and 3.12. Pull requests that drop coverage by more than 2 percentage points are blocked.

---

## Documentation

| Document | Contents |
| :--- | :--- |
| [`docs/architecture.md`](docs/architecture.md) | Detailed component dataflow, schemas, lifecycle |
| [`docs/installation.md`](docs/installation.md) | NATS setup, Telegram bot setup, TLS hardening |
| [`docs/configuration.md`](docs/configuration.md) | Full reference for `config.json`, `policy.yaml`, environment variables |
| [`docs/security-model.md`](docs/security-model.md) | Threat model, audit history, hardening assumptions |
| [`docs/comparisons.md`](docs/comparisons.md) | Detailed comparison with Letta, Mem0, Zep, Graphiti, RASPUTIN |
| [`SECURITY.md`](SECURITY.md) | How to report vulnerabilities |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | How to contribute |
| [`CHANGELOG.md`](CHANGELOG.md) | Per-phase change log |
| [`decisions.md`](decisions.md) | Engineering decision log across the rebuild |

---

## License

Apache License 2.0. See [`LICENSE`](LICENSE).

---

<div align="center">

*Memory without inference is a logbook.*<br/>
*Inference without memory is a stranger at your door every morning.*<br/>
**CEREBELLUM is neither.**

</div>
