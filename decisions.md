# CEREBELLUM Rebuild Log

This file is the persistent state of the rebuild. Every Sisyphus work session ends with an update here. A blank entry at end of a phase means the phase did not complete.

**Maintainer:** Sisyphus (OpenCode + Qwen3.5-27B FP8)
**Reviewer of last resort:** Claude Opus 4.7 via OpenRouter, capped at 8 calls/phase
**Plan source:** `CEREBELLUM_REBUILD_PLAN.md` at repo root

---

## Format

For each phase, fill out the section before merging to `main` and tagging `phase-N-complete`.

```
## Phase N — <name>
- **Started:** YYYY-MM-DD
- **Completed:** YYYY-MM-DD
- **Branch:** phase-N-name
- **Commit range:** <first sha>..<last sha>
- **Exit gate result:** PASS | FAIL (and why, if retried)

### What shipped
- bullet list of concrete deliverables

### What was deferred
- bullet list with target phase

### Surprises
- things that were harder, easier, or different than the plan assumed

### Decisions made without Opus
- one-liner per non-obvious choice + rationale (e.g. "chose pymining over hand-rolled PrefixSpan because the lib is 200 LOC and well-tested")

### Opus calls (max 8 per phase)
| # | Date | Question | Response summary | Action taken |
|---|------|----------|------------------|--------------|
| 1 | ...  | ...      | ...              | ...          |

### Metrics snapshot
- relevant numbers at end of phase (test coverage, token spend, real event-stream stats, etc.)
```

---

## Phase 0 — Bootstrap

- **Started:** 2026-05-02
- **Completed:** 2026-05-02
- **Branch:** phase-0-bootstrap
- **Commit range:** 3da5741..672ae0a
- **Exit gate result:** PASS (lint clean, tests pass, 12.66% coverage, mypy deferred to Phase 6)

### What shipped
- Package layout: `src/*.py` → `src/cerebellum/*.py` (proper hatchling package)
- `pyproject.toml` with deps, dev deps, ruff/mypy/pytest config
- `Makefile` with install/test/lint/typecheck/format/run targets
- `requirements.txt` generated via pip-compile
- GitHub Actions CI (ruff + mypy + pytest on Python 3.11/3.12)
- `.gitignore`, `.env.example`, `config.example.json`
- Phase exit gates: `scripts/gates/phase_0.sh` through `phase_6.sh`
- `scripts/check_coverage_delta.py` for coverage regression detection
- systemd service templates + `scripts/install_systemd.sh`
- `src/cerebellum/models.py` — single source of truth for model identifiers
- All imports updated to `cerebellum.*` namespace
- Hardcoded `/home/josh` paths fixed in `cron/*.json`, scripts, source
- Legacy files deleted: `cerebellum-plan.md`, root `__init__.py`, duplicate `cerebellum/` dir, old `.service` files

### What was deferred
- Mypy strict type checking (70 errors) → Phase 6 per plan
- Test coverage beyond 10% threshold → Phase 1+ (behavioral tests)

### Surprises
- Python 3.14 available (system default), pyproject.toml targets 3.11+
- `ruff --fix` removed some imports that were actually needed (F821 errors)
- `pyproject.toml` got corrupted by repeated edits — had to rebuild manually
- Gate script self-referenced `/home/josh` in grep — needed exclusion

### Decisions made without Opus
- Chose hatchling over setuptools (simpler, faster, modern)
- Coverage threshold 10% for Phase 0 (plan says "even if low")
- Mypy deferred to Phase 6 (plan explicitly allows this)
- `BASE_DIR` pattern using `Path(__file__).resolve().parent` instead of env var in scripts

### Opus calls
| # | Date | Question | Response summary | Action taken |
|---|------|----------|------------------|--------------|
| 0 | — | — | No Opus calls this phase | — |

### Metrics snapshot
- LOC delta: ~0 (restructure only, no new functionality)
- Test count: 1 (smoke test) → 1 (same)
- Coverage: 0% → 12.66%
- Opus token spend this phase: $0

---

## Phase 1 — Rebrand and honesty

- **Started:** 2026-05-02
- **Completed:** 2026-05-03
- **Branch:** phase-1-rebrand-and-honesty
- **Commit range:** 07917c5..2de9835
- **Exit gate result:** PASS

### What shipped
- Renamed all cognitive-anatomy classes to operational names (Hippocampus→EpisodeStore, PrefrontalCortex→Proposer, BasalGanglia→PolicyArbiter, CerebellumEventEmitter→EventBus)
- Renamed all files to match (episode_store.py, proposer.py, policy_arbiter.py, event_bus.py)
- KuzuDB schema migration: CausalEdge→SuccessorEdge (migrations/001_rename_successor_edge.py, idempotent)
- Rewrote README.md: honest framing, dropped "shadow cognition", dropped "causal links" claims, added Limitations section
- Added docs/architecture.md with dataflow diagram, no anatomy metaphors
- Fixed cost field semantics (generation_cost_usd + estimated_execution_cost_usd)
- Honesty pass on policy.yaml (removed browser.screenshot from allowlist, renamed browser.navigate→http.get)
- Behavioral tests: 10 tests for EventBus, 15 for EpisodeStore (69% coverage)
- Fixed "shadow cognition" string in LLM prompt

### What was deferred
- Mypy strict on proposer.py and policy_arbiter.py → Phase 6 per plan
- Full test coverage beyond 60% threshold → Phase 6

### Surprises
- KuzuDB 0.7.1 doesn't support `CREATE RELATION TABLE` — relationships stored via node properties instead
- KuzuDB read-only DBs crash on close with WAL — had to remove entirely
- KuzuDB doesn't support parameterized LIMIT — use f-string integer literals
- The migration script needed special handling for Kuzu's dynamic schema

### Decisions made without Opus
- Kept deprecated aliases (Hippocampus, etc.) for one phase of backward compatibility
- Chose Kuzu node properties over relation tables for SuccessorEdge (Kuzu limitation)
- Coverage threshold 60% for Phase 1 (plan target met)
- No Opus calls — work was clear from plan spec, no architecture ambiguities

### Opus calls
| # | Date | Question | Response summary | Action taken |
|---|------|----------|------------------|--------------|
| 0 | — | — | No Opus calls this phase | — |

### Metrics snapshot
- Coverage on `event_bus.py`: 60%
- Coverage on `episode_store.py`: 69%
- `git grep -i "shadow cognition\|causal"` count outside docs/: 0 (honest disclaimers in README only)
- Opus token spend this phase: $0
---

## Phase 2 — Hypothesis grounding

- **Started:**
- **Completed:**
- **Branch:** phase-2-grounding
- **Commit range:**
- **Exit gate result:**

### What shipped

### What was deferred

### Surprises

### Decisions made without Opus

### Opus calls

| # | Date | Question | Response summary | Action taken |
|---|------|----------|------------------|--------------|

### Metrics snapshot
- 2h integration: % of accepted proposals with valid evidence_event_ids:
- Verifier-vs-proposer disagreement rate:
- Coverage on `proposer.py` + `grounding.py`:
- Opus token spend this phase: $

---

## Phase 3 — Real successor-pattern mining

- **Started:**
- **Completed:**
- **Branch:** phase-3-real-causality
- **Commit range:**
- **Exit gate result:**

### What shipped

### What was deferred

### Surprises

### Decisions made without Opus

### Opus calls

| # | Date | Question | Response summary | Action taken |
|---|------|----------|------------------|--------------|

### Metrics snapshot
- Planted-pattern recovery (3/3 expected):
- False-positive rate on uniform random stream (0/1000 expected):
- Edges discovered on real event stream:
- Lift distribution (p25 / p50 / p75 / max):
- Coverage on `mining.py`:
- Opus token spend this phase: $

---

## Phase 4 — Real action surface

- **Started:**
- **Completed:**
- **Branch:** phase-4-action-surface
- **Commit range:**
- **Exit gate result:**

### What shipped

### What was deferred

### Surprises

### Decisions made without Opus

### Opus calls

| # | Date | Question | Response summary | Action taken |
|---|------|----------|------------------|--------------|

### Metrics snapshot
- RASPUTIN tools wired:
- Live test result (proposal → arbiter → rasputin.search → result):
- Coverage on action handlers:
- Opus token spend this phase: $

---

## Phase 5 — Feedback loop

- **Started:**
- **Completed:**
- **Branch:** phase-5-feedback-loop
- **Commit range:**
- **Exit gate result:**

### What shipped

### What was deferred

### Surprises

### Decisions made without Opus

### Opus calls

| # | Date | Question | Response summary | Action taken |
|---|------|----------|------------------|--------------|

### Metrics snapshot
- proposal_outcomes rows after baseline week:
- Approval rate by proposer model:
- Verifier-correctness rate:
- Mean confidence (approved) vs (rejected):
- Calibration status: uncalibrated | calibrated (and Platt coefficients)
- Opus token spend this phase: $

---

## Phase 6 — Test, harden, ship

- **Started:**
- **Completed:**
- **Branch:** phase-6-test-and-ship
- **Commit range:**
- **Exit gate result:**

### What shipped

### What was deferred

### Surprises

### Decisions made without Opus

### Opus calls

| # | Date | Question | Response summary | Action taken |
|---|------|----------|------------------|--------------|

### Metrics snapshot
- Global coverage:
- Per-module coverage (every module ≥ 80%, arbiter/dashboard ≥ 75%):
- Property test count:
- Fuzz iterations passed:
- Top 5 issues from final Opus audit (and disposition):
- Opus token spend this phase: $
- **Total Opus token spend across rebuild: $**

---

## Cross-cutting log

Anything that doesn't fit a phase. Format: `YYYY-MM-DD — note`.

- 

---

## Open questions parking lot

When a question comes up that's out of scope for the current phase but should not be lost, file it here with a target phase. Phase exit reviews check this list.

- 

---

## Known limitations (running list)

This list is what goes in the README's "Limitations" section in Phase 1 and gets updated every phase. Honesty pass: if a claim moves from "limitation" to "implemented," delete it here and add a test that proves the implementation.

- Hypothesis confidence is self-reported by the LLM; no calibration until Phase 5 reaches 100+ outcomes per model.
- Successor-edge mining is association, not causation. Lift filtering reduces false positives but does not establish causality.
- Reversal detection (was an auto-executed action later undone by the user?) is a stub until destructive tools come online.
- No mTLS on the NATS connection; single-host deployment assumed.
- Auto-execute action surface is intentionally narrow (read-only by default). Adding write tools requires a separate threat model review.
- 
