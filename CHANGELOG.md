# Changelog

All notable changes to CEREBELLUM are documented here. The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows phase-based versioning during alpha.

## [Unreleased]

### Planned

- Real Cypher tokenizer (Phase 7) replacing the current regex-based string-literal stripping.
- Mutual TLS for NATS (Phase 7) — currently server verification only.
- Telegram webhook IP allowlist + replay protection beyond the existing `update_id` deduplication.
- ≥90% coverage on security-critical paths.
- SAST + dependency audit + secret scanning in CI.

---

## Phase 6 Redo (Final) — 2026-05-03

**Test, harden, ship.** Final hardening pass with property tests, fuzzing, and a full Opus 4.7 audit.

### Added

- 14 Hypothesis-based property tests covering RateLimiter, DailyCostTracker, Cypher filter, and SSRF validator invariants. Reproducible seeds recorded in `decisions.md`.
- 10,000-iteration Telegram webhook fuzzer (`tests/test_telegram_fuzzer.py`) covering oversized fields, missing fields, type confusion, Unicode tricks, replay attacks, and recursive payloads.
- 27 Cypher filter regression tests, including the previously-failing `MATCH (n) WHERE n.name = "DROP table" RETURN n` case.
- CALL whitelist tightening: only `db.schema`, `db.show_tables`, and `db.show_connections` are accepted.
- NATS TLS configuration via `nats.tls`, `nats.tls_cert`, `nats.tls_key`, `nats.tls_ca` (config keys) or matching `CEREBELLUM_NATS_TLS_*` environment variables.
- `SECURITY.md` section documenting NATS TLS modes (server verification, mTLS).
- 14-check exit gate script (`scripts/gates/phase_6.sh`).
- Final Opus 4.7 audit recorded in `decisions.md` with CONDITIONAL PASS verdict.

### Changed

- `mypy strict = true` now applies to all first-party modules. The `[[tool.mypy.overrides]]` section contains only the third-party `kuzu` and `nats` import-stub block.
- Cypher safety filter strips string literals (both `'...'` and `"..."`) before applying the keyword regex, eliminating false positives on legitimate queries with keywords inside string values.
- Test isolation: dashboard tests now use isolated databases per test, preventing cross-test contamination.
- `test_validate_file_path_outside_root` no longer depends on `Path.home()` and runs deterministically regardless of whether the test runner is root.

### Fixed

- `_emitter` bug in policy arbiter handler tests.
- Dashboard test reliability with FastAPI TestClient route binding under module reload.

### Metrics

- Tests: 365 → 615 (+250).
- Global coverage: 72% → 81%.
- `policy_arbiter.py` coverage: 73% → 80%.
- `dashboard.py` coverage: 61% → 77%.
- `mypy --strict` errors: deferred → 0.
- Opus token spend (final audit): ~$1.

---

## Phase 5 — 2026-05-04

**Feedback loop.** Closed-loop outcome tracking and confidence calibration.

### Added

- `feedback_loop.py` (95% coverage) with `FeedbackStore`, `ProposalOutcome`, `CalibrationMetrics` classes.
- `proposal_outcomes` and `calibration_snapshots` SQLite tables.
- Platt scaling for confidence calibration (activated after ≥100 outcomes per proposer model).
- Expected Calibration Error (ECE) computation.
- `/api/metrics/weekly` endpoint and dashboard panel.
- `scripts/weekly_calibration.py` cron job.

### Changed

- Arbiter records every decision (approve, reject, snooze, expire, auto_execute, discard) to `proposal_outcomes`.
- Reversal detection placeholder added (currently returns `False` for all auto-execute tools, since the surface is read-only by design).

---

## Phase 4 — 2026-05-04

**Real action surface.** Connected the policy arbiter to RASPUTIN's MCP server.

### Added

- RASPUTIN MCP tool handlers in `policy_arbiter.py`:
  - `rasputin.search` (auto-executable)
  - `rasputin.recent_facts` (auto-executable)
  - `rasputin.entity_lookup` (auto-executable)
  - `rasputin.episode_summary` (auto-executable)
  - `rasputin.commit_fact` (approval required)
  - `rasputin.reflect` (approval required)
- `notification.summarize` tool: summarizes the last hour of events and posts to Telegram.
- `proposal.snooze` tool: marks a proposal as "remind me later."
- Per-tool execution-cost estimator. `Proposal.estimated_execution_cost_usd` is now non-None for all paths.
- `http_client.py` wrapper module (httpx-based; SSRF-pinned variants).

### Changed

- Removed `browser.screenshot` (was a deferred stub) and renamed `browser.navigate` → `http.get` (handler does an HTTP GET, not navigation).
- `policy.yaml` updated to reflect the new tool surface.

---

## Phase 3 — 2026-05-04

**Real successor-pattern mining.** Replaced co-occurrence counting with proper sequential pattern mining.

### Added

- `mining.py` (91% coverage, 22 tests) with hand-rolled PrefixSpan, lift scoring, shuffle baselines.
- Entity-aware pattern mining: associations are at the `(entity, event_type)` level, not just `event_type`.
- `lift` column on `SuccessorEdge`. Edges with `lift < 1.5` are rejected as noise.
- Weekly shuffle baseline (`SuccessorEdge.shuffle_baseline_lift`); edges where `actual_lift / baseline < 2.0` are flagged `low_confidence`.
- `relevant_patterns` payload key in proposer prompt: top 10 SuccessorEdges matching a recent event are surfaced.

### Removed

- `_event_types_change_significantly` heuristic (no theoretical basis; was silently killing valid cross-domain associations).

### Metrics

- Planted-pattern recovery: 3/3 with lift > 2.0.
- False-positive rate on uniform-random 1000-event stream: 0.

---

## Phase 2 — 2026-05-04

**Hypothesis grounding.** Every proposal must cite specific event IDs, and a second model verifies before storage.

### Added

- `grounding.py` (81% coverage) with `GroundingVerifier` class.
- `evidence_event_ids` and `causal_argument` fields on `Hypothesis`. Both required.
- Verifier pass with `gpt-4o-mini` (cheap-model second-pass).
- Evidence-subset duplicate detection: a new proposal whose `evidence_event_ids` is a subset of an active proposal's evidence is flagged duplicate.
- 50/day proposal cap with 12-hour pause when reached.
- `verifier_metrics` SQLite table + daily aggregate.

---

## Phase 1 — 2026-05-03

**Rebrand and honesty pass.** Removed cognitive-anatomy naming and brought the README in line with what the code actually does.

### Changed

- `Hippocampus` → `EpisodeStore`
- `PrefrontalCortex` → `Proposer`
- `BasalGanglia` → `PolicyArbiter`
- `CerebellumEventEmitter` → `EventBus`
- `CausalEdge` → `SuccessorEdge`
- LLM prompt persona changed from "shadow cognition layer" to "proactive ops assistant."
- README rewritten to remove "causal inference" claims and add a clear "Limitations" section.

### Added

- `001_rename_successor_edge.py` migration (idempotent).
- `docs/architecture.md` with operational naming.
- `Hypothesis.generation_cost_usd` separated from `estimated_execution_cost_usd`.

### Removed

- `cerebellum-plan.md` (superseded by per-phase decisions log).
- Duplicate root-level `__init__.py` and stub `cerebellum/observatory_main.py`.

---

## Phase 0 — 2026-05-02

**Bootstrap.** Made the repo installable, testable, and CI-green for a fresh clone.

### Added

- `pyproject.toml` (Hatchling build system, ruff + mypy + pytest config).
- `Makefile` with `install`, `lint`, `typecheck`, `test`, `check`, `format`, per-phase exit gate targets.
- `requirements.txt` generated from `pyproject.toml`.
- Templated systemd units (`services/*.service.template` + `scripts/install_systemd.sh`).
- `.github/workflows/ci.yml` running on Python 3.11 and 3.12, with coverage-drop gate on PRs.
- `decisions.md` skeleton.
- `cerebellum/models.py` constants module for model identifiers.

### Changed

- Package layout consolidated to `src/cerebellum/`. All scripts updated.
- Model identifier `claude-opus-4.7` (invalid OpenRouter slug) → `claude-opus-4-7`.
- All hardcoded `/home/josh` paths replaced with `${CEREBELLUM_BASE_DIR}` env var.

### Removed

- Pre-rebuild `pylint.yml` workflow.
- Hardcoded user/path assumptions across systemd units and source.

---

## Pre-Phase 0

The pre-rebuild repository (commits before `c28c5c0`) is preserved in git history but should not be referenced for current behavior. See `decisions.md` for the rebuild plan and per-phase exit gate results.
