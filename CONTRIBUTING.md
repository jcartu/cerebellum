# Contributing to CEREBELLUM

Thanks for considering a contribution. CEREBELLUM is alpha software run by its author against production workloads, so the bar for changes is high. Read this whole file before opening a PR.

## Ground rules

- **Every change ships with tests.** No exceptions. The test suite is at 81% coverage and we don't ship regressions.
- **Every public function has type hints.** mypy `strict = true` runs in CI on all first-party modules. There are no first-party modules in the override list.
- **Security-critical paths get property tests.** RateLimiter, DailyCostTracker, Cypher filter, SSRF validator, kill switch — these have Hypothesis property tests. New code in these areas needs property tests too.
- **No secrets in commits.** Pre-commit hooks check this. If you slip one through, we revert and force-push the rewritten history; you'll need to rebase.
- **No new dependencies without justification.** Adding a dep means writing the rationale in the PR description. "It's nice" is not a rationale.

## Development setup

```bash
git clone https://github.com/jcartu/cerebellum && cd cerebellum
make install            # creates .venv, installs the package + dev deps
make check              # lint + typecheck + test, must pass
```

Required tools (installed by `make install`): `ruff`, `mypy`, `pytest`, `hypothesis`.

## Branch and PR conventions

1. **Branch from `main`.** No feature branches off feature branches.
2. **Branch naming:** `feat/<short-name>`, `fix/<short-name>`, `docs/<short-name>`, `refactor/<short-name>`.
3. **Commits:** imperative mood, one logical change per commit. Sign-off is not required but appreciated.
4. **PR description must include:**
   - What problem this solves
   - How it's tested
   - Any new dependencies and why
   - Any security implications
5. **CI must be green.** `make check` locally; GitHub Actions in the PR.
6. **Coverage cannot drop more than 2 percentage points** without explicit reviewer sign-off in the PR thread.

## Scope of contributions we want

In rough order of priority:

1. **Test coverage on `episode_store.py`.** Currently the lowest-covered first-party module at 69%. Tests for the LLM-Cypher generation path are particularly welcome.
2. **Bug fixes** with reproducible test cases.
3. **Documentation improvements** — especially the deployment guide and the security threat model.
4. **New tool handlers** for the policy arbiter, with the constraint that any handler doing side effects must default to `forbidden_tools` (approval-only) until proven safe.
5. **Integration tests** with real NATS, KuzuDB, and a mock LLM endpoint.

## Scope of contributions we don't want (yet)

- New LLM features that require new prompts or model-specific tuning. The proposer prompt is stable for a reason.
- New persistence backends (Postgres, Redis, etc.). The current SQLite + KuzuDB choice is intentional.
- UI redesigns. The HTMX dashboard is intentionally minimal.
- "Inspired by" memory layer features that overlap with RASPUTIN, Mem0, Letta, or Zep. CEREBELLUM is the proposal/gate layer, not a memory store.

If you're not sure whether your idea fits, open an issue with the `proposal` label and we'll discuss before you write code.

## Reporting security issues

Don't open a public issue. Email the maintainer directly (see `SECURITY.md`). We'll acknowledge within 72 hours.

## Code style

- Run `make format` before committing. This runs `ruff format` and `ruff check --fix`.
- Line length: 110 characters (configured in `pyproject.toml`).
- Type-hint everything. `Any` is allowed only with a comment explaining why.
- Logging: use `logger = logging.getLogger(__name__)` at the top of each module. No print statements outside of CLI entry points.
- Error messages: include enough context to debug from the log alone. "Failed" is not an error message; "Failed to parse evidence_event_ids: expected list, got dict" is.

## What "ready to merge" looks like

- All CI checks green.
- One reviewer approval (the maintainer for now).
- Coverage holds or improves.
- `decisions.md` updated if this changes architectural assumptions.
- README/docs updated if this changes user-visible behavior.
