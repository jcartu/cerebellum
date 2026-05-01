# CEREBELLUM — Shadow Cognition Layer for RASPUTIN

> A continuously-running layer that observes everything RASPUTIN does, builds causal models, and proactively acts on opportunities before you ask.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        CEREBELLUM                                   │
├─────────────┬──────────────┬─────────────────┬──────────────────────┤
│ Observatory │ Hippocampus  │ Prefrontal      │ Basal Ganglia        │
│ (Event      │ (Causal      │ Cortex          │ (Action Arbiter)     │
│  Stream)    │  Memory)     │ (Hypotheses)    │                      │
├─────────────┼──────────────┼─────────────────┼──────────────────────┤
│ NATS        │ KuzuDB       │ GPT-5.5/Opus    │ Policy YAML          │
│ SQLite WAL  │ Qdrant       │ (via OpenRouter)│ Telegram approvals   │
│ Event SDK   │ Episodes     │ 5min cadence    │ Auto-execute path    │
└─────────────┴──────────────┴─────────────────┴──────────────────────┘
                              │
                    ┌─────────┴─────────┐
                    │     The Dojo      │
                    │  (Self-Improve)   │
                    │  LoRA on 120B     │
                    └───────────────────┘
```

## Infrastructure

| Resource | Assignment |
|----------|-----------|
| GPU 0 (RTX PRO 6000, 96GB) | vLLM 120B (port 8001), Dojo training |
| GPU 1 (RTX PRO 6000, 96GB) | vLLM 20B (port 8002) |
| GPU 2 (RTX 5090, 32GB) | Qwen 35B (Ollama port 11436), Hippocampus clustering |
| CPU | NATS, KuzuDB, Observatory, Prefrontal Cortex coordinator |

## File Layout

```
/home/josh/.openclaw/cerebellum/
├── config.json                    # Main config
├── policy.yaml                    # Action policy
├── events.db                      # SQLite WAL event store
├── graph/                         # KuzuDB embedded graph
├── hypotheses.db                  # Hypothesis lifecycle DB
├── feedback.db                    # Dojo training data
├── src/
│   ├── __init__.py
│   ├── events.py                  # Event SDK + Observatory
│   ├── hippocampus.py             # Causal memory engine
│   ├── cortex.py                  # Hypothesis generator
│   ├── arbiter.py                 # Action arbiter
│   ├── dojo.py                    # Self-improvement loop
│   ├── observatory_main.py        # Observatory service entry
│   ├── instruments/
│   │   ├── cron_instrument.py     # Cron job instrumentation
│   │   ├── telegram_instrument.py # Telegram message hook
│   │   └── browser_instrument.py  # Browser action hook
│   └── ui/
│       ├── dashboard.py           # FastAPI dashboard
│       └── cortex_routes.py       # Hypothesis API routes
├── scripts/
│   ├── cluster_episodes.py        # 15min episode clustering
│   ├── mine_causal_edges.py       # Weekly causal mining
│   ├── generate_hypotheses.py     # 5min hypothesis generation
│   └── arbiter_loop.py            # Main arbiter loop
├── services/
│   ├── cerebellum-observatory.service
│   └── cerebellum-cortex.service
└── cron/
    ├── hippocampus-cluster.json
    ├── causal-miner.json
    └── cortex-generate.json
```

## Phase 1: The Observatory ✅

- NATS JetStream in Docker (persistent storage)
- Event SDK: `emit(type, payload, actor, context)` → NATS + SQLite WAL
- Cron instrumentation wrapper
- Real-time dashboard (FastAPI + HTMX, port 18790)

## Phase 2: The Hippocampus ✅

- KuzuDB embedded graph (Event, Episode, Entity, CausalEdge nodes)
- Episodic clusterer (15min cron, Qwen 35B on 5090)
- Causal edge miner (weekly, min support 5)
- Natural language query interface

## Phase 3: Prefrontal Cortex ✅

- Hypothesis generator (5min cadence + event-triggered)
- GPT-5.5 via OpenRouter (fallback: Opus 4.7)
- Structured JSON: confidence, utility, cost, reversibility, plan
- SQLite lifecycle: proposed → staged → executed → rejected → expired
- Live feed API for dashboard

## Phase 4: Basal Ganglia ✅

- Policy-based action decisions (YAML config)
- Three tiers: auto-execute, stage & notify (Telegram), discard
- Rate limiting, budget caps, kill-switch
- systemd user services

## Phase 5: The Dojo (Planned)

- Feedback collection from approval/rejection history
- Nightly LoRA training on local 120B (DPO on preference pairs)
- A/B shadow evaluation before promotion

## Success Criteria

- [ ] Phase 1: Every system action flows through single event stream
- [ ] Phase 2: Can query "what did I work on last Thursday" and get causally-linked answer
- [ ] Phase 3: Live feed of plausible hypotheses in dashboard
- [ ] Phase 4: System autonomously does 3+ useful things/day, zero wrong actions
- [ ] Phase 5: Week-over-week approval rate trends upward

## Services

| Service | Port | Status |
|---------|------|--------|
| cerebellum-observatory | 18790 (dashboard), 4222 (NATS) | Enabled |
| cerebellum-cortex | - | Enabled |
