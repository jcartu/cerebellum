# Comparisons

CEREBELLUM is often confused with memory systems for AI agents. It is not a memory system. It's a proactive proposal and gate layer that uses memory systems as inputs.

This page is for operators evaluating whether CEREBELLUM fits their stack alongside, or instead of, a tool they already use.

## TL;DR matrix

| | What it stores | What it does | Where CEREBELLUM differs |
| :--- | :--- | :--- | :--- |
| **CEREBELLUM** | Operational events, episodes, successor patterns, proposed actions | Proposes next actions; gates execution | (this is the baseline) |
| **RASPUTIN** | Long-term agent facts, entities, episodes from conversations | Stores and retrieves agent memory | RASPUTIN is memory; CEREBELLUM is reflexes. They run together. |
| **Letta (MemGPT)** | In-context, archival, recall memory tiers | Provides an agent runtime with memory hierarchy | Letta is the agent. CEREBELLUM watches the agent. |
| **Mem0** | Facts and preferences extracted from conversations | LLM-app memory layer | Mem0 stores user-stated preferences; CEREBELLUM stores system-observed sequences. |
| **Zep** | Sessions, facts, entities, relationships over time | Long-term memory + temporal graph | Zep is the historical record; CEREBELLUM is the proactive proposal layer. |
| **Graphiti** | Bi-temporally valid facts in a graph | Temporal knowledge graph for agents | Graphiti is graph-shaped world memory; CEREBELLUM is sequence-shaped behavior memory. |
| **Hindsight** | Memory representations for benchmarks | Reference architecture for LoCoMo / LongMemEval | Hindsight is a benchmark + reference; CEREBELLUM is a deployed system with a different goal. |
| **LangChain Memory / LlamaIndex Memory** | Conversation buffers, summaries, vector stores | Per-conversation memory primitives | These are memory primitives inside an agent; CEREBELLUM operates outside the agent. |

## Detailed comparisons

### CEREBELLUM vs RASPUTIN

[RASPUTIN](https://github.com/jcartu/rasputin-memory) is a self-hosted, long-term agent memory backend, benchmarked against LoCoMo and LongMemEval. It stores facts, entities, episodes, and supports semantic search, BM25, and graph traversal.

CEREBELLUM and RASPUTIN are designed to run side-by-side:

- **RASPUTIN answers:** "what do I know about this conversation, this entity, this past episode?"
- **CEREBELLUM answers:** "given recent events, what should we do next, and is it safe to do automatically?"

CEREBELLUM treats RASPUTIN as a tool (via MCP). The proposer can call `rasputin.search` to ground a proposal in stored memory; the policy arbiter can route a `rasputin.commit_fact` action through the approval queue.

If you only run one, run RASPUTIN — agent memory is the bigger problem. If you run both, you have an agent with persistent memory **and** a sidecar that watches its operational behavior and proposes next steps.

### CEREBELLUM vs Letta (formerly MemGPT)

[Letta](https://github.com/letta-ai/letta) is an agent framework with a memory hierarchy: in-context memory, archival memory (vector store), recall memory (per-session conversation history). You build agents *inside* Letta by configuring its memory blocks and tool calls.

CEREBELLUM doesn't compete with Letta — it complements it. A Letta agent emits events (you instrument the framework, or you have it publish to NATS as part of its tool execution). CEREBELLUM consumes those events, mines patterns, and proposes actions for *you* to approve.

You'd use both if: you want a sophisticated memory hierarchy inside the agent (Letta) **and** an external supervisory layer that proposes actions and gates execution (CEREBELLUM).

### CEREBELLUM vs Mem0

[Mem0](https://github.com/mem0ai/mem0) is a memory layer for LLM apps. It extracts facts and preferences from conversations and stores them with conflict resolution. The pitch is: drop-in memory for chatbots and assistants.

The orthogonality is clean:

- Mem0 stores **what the user said and prefers** ("user is allergic to peanuts", "user works at TechCorp").
- CEREBELLUM stores **what the system did and what tends to follow** ("after a deploy.start, deploy.error follows within 90s with lift 4.2").

You can run both. Mem0 keeps your agent personalized; CEREBELLUM watches your agent's behavior for proposals.

### CEREBELLUM vs Zep / Graphiti

[Zep](https://github.com/getzep/zep) is a long-term memory service with a temporal knowledge graph (Graphiti is the underlying graph engine, also developed by the Zep team). Zep is positioned as the historical record an agent reasons over: messages, facts, entities, relationships, all temporally indexed.

Where these differ from CEREBELLUM:

- **Shape:** Zep/Graphiti store *facts about the world* in a graph. CEREBELLUM stores *operational events* in a SQLite WAL and clusters them into episodes; the graph (KuzuDB) is for entity references and successor edges, not for fact storage.
- **Use case:** Zep helps the agent remember things across sessions. CEREBELLUM helps you supervise the agent's behavior and propose actions.
- **Time model:** Zep emphasizes bi-temporal validity (when a fact was true, when it was learned). CEREBELLUM emphasizes sequence (what events tend to follow what other events).

If you're running an agent with long-running sessions and need temporal fact tracking, Zep is the right tool. If you also want a process that watches the agent's tool calls and notices patterns, run CEREBELLUM alongside.

### CEREBELLUM vs Hindsight

[Hindsight](https://github.com/hindsightagent) is a memory benchmark + reference architectures for evaluating long-term memory systems on tasks like LoCoMo, LongMemEval, and FRAMES. Its top-line architecture (4-path retrieval with RRF fusion) was the inspiration for several improvements in RASPUTIN.

Hindsight is **not** a deployed system you run — it's a benchmark + reference designs. It's a different category from CEREBELLUM entirely. You'd use Hindsight to evaluate RASPUTIN (or any memory backend); you'd use CEREBELLUM to operate an agent.

### CEREBELLUM vs LangChain / LlamaIndex memory primitives

Both LangChain and LlamaIndex have memory primitives — `ConversationBufferMemory`, `VectorStoreRetrieverMemory`, summarization memories, etc. These are *components inside an agent* that handle per-conversation memory.

CEREBELLUM operates *outside* the agent. It doesn't care which framework you used to build the agent or what memory primitives are inside it. It cares about events the agent emits.

If your agent is built on LangChain or LlamaIndex and you want a supervisory layer, CEREBELLUM is additive.

## When to use CEREBELLUM

- You run a long-lived autonomous agent and want a structured proposal queue with human-in-the-loop approval.
- You want to notice patterns in your agent's behavior over time without writing custom analytics.
- You want a kill switch and budget caps that span the whole agent (not per-tool).
- You're comfortable running NATS + a Python service alongside your agent.

## When **not** to use CEREBELLUM

- You're building a chatbot. CEREBELLUM is overkill.
- Your agent is short-lived (minutes per session). The pattern miner needs days of events to be useful.
- You want fully autonomous behavior with no approval loop. CEREBELLUM is built around the assumption that side-effecting actions go through you.
- You're looking for an agent's primary memory layer. Use RASPUTIN, Mem0, Zep, or Letta — those are memory systems. CEREBELLUM is not.

## Recommended stacks

For a single-operator, autonomous-agent setup:

```
┌──────────────────────────────┐
│        Your agent            │
│   (Claude Code, Letta,       │
│    custom)                   │
│                              │
│   memory layer:              │
│      RASPUTIN                │
│                              │
│   tool layer:                │
│      MCP servers             │
│      function calls          │
└────────────┬─────────────────┘
             │ events (NATS)
             ▼
┌──────────────────────────────┐
│        CEREBELLUM            │
│                              │
│  Records, mines patterns,    │
│  proposes actions, gates     │
│  execution.                  │
└────────────┬─────────────────┘
             │
             ▼
       ┌─────────┐
       │   You   │
       └─────────┘
```

For a multi-agent or production setup with personalization:

```
┌──────────────────────────────┐
│        Your agent            │
│                              │
│   conversational memory:     │
│      Letta or Zep            │
│                              │
│   user preferences:          │
│      Mem0                    │
│                              │
│   long-term facts:           │
│      RASPUTIN                │
│                              │
│   tools:                     │
│      MCP / function calls    │
└────────────┬─────────────────┘
             │ events (NATS)
             ▼
┌──────────────────────────────┐
│        CEREBELLUM            │
└────────────┬─────────────────┘
             │
             ▼
       ┌─────────┐
       │   You   │
       └─────────┘
```

In both cases, CEREBELLUM is the smallest box but the only one with the kill switch.
