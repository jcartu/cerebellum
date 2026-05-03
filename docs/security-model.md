# Security Model

This document describes CEREBELLUM's threat model, the controls that mitigate each category of risk, and the audits the codebase has gone through. For installation-level security advice (what to configure, how to harden), see [`installation.md`](installation.md). For the vulnerability disclosure policy, see [`SECURITY.md`](../SECURITY.md).

## Trust boundaries

CEREBELLUM runs on a host you control. Trust boundaries:

| Inside (trusted) | Outside (not trusted) |
| :--- | :--- |
| The host where CEREBELLUM runs | Other hosts on your network |
| The systemd service user | Other users on the same host |
| Local files in `${CEREBELLUM_BASE_DIR}` | Files outside the base directory |
| Loopback HTTP traffic | Any inbound traffic from non-loopback |
| NATS messages signed by the configured token | Events from any NATS server you didn't configure |
| OpenRouter / Telegram TLS endpoints | Any URL the LLM proposes |

The core assumption: **the LLM is not trusted.** Anything the LLM produces — proposed actions, Cypher queries, URLs to fetch, file paths to read — is treated as untrusted input that goes through validation before it touches the system.

## Threat categories and mitigations

### 1. Prompt injection / LLM-driven misuse

**Threat:** An attacker (or accidentally-poisoned event stream) causes the LLM to generate a proposal that, if executed, harms the system. Examples: a proposed action that exfiltrates secrets, deletes files, or makes excessive API calls.

**Mitigations:**

- **Tool allowlist.** Only tools listed in `auto_execute.allowed_tools` run without approval. Anything else routes through Telegram.
- **Forbidden tools.** Tools in `forbidden_tools` never run, regardless of approval. Default forbidden list includes destructive operations.
- **Grounding requirement.** Every proposal must cite specific `evidence_event_ids` from recent events. Proposals with empty or invalid evidence are discarded.
- **Verifier pass.** A second (cheap) model independently checks that the proposal is supported by the cited evidence.
- **Budget caps.** Daily LLM spend cap, sliding-window action rate limit. Even a runaway proposer can't burn unbounded resources.
- **Confidence threshold.** Auto-execute requires high confidence; staging requires medium. Below those, proposals are discarded.

### 2. SSRF / outbound traffic abuse

**Threat:** The LLM proposes (or directly generates) a URL pointing at internal services (RFC1918, loopback, link-local, cloud metadata IPs like `169.254.169.254`). If unvalidated, the request leaks internal state or hits internal admin endpoints.

**Mitigations:**

- **DNS pre-resolution.** Every outbound URL has its hostname resolved once, before the connection.
- **IP allowlist.** Resolved IPs are checked against a private/loopback/link-local/multicast/ULA/metadata blocklist. Failing IPs are rejected.
- **Connection pinning.** The HTTPS connection is pinned to the resolved IP — DNS-rebind attacks during the connection lifetime fail.
- **No redirects.** The HTTPS handler explicitly refuses redirects, so a server cannot bounce the connection to an internal IP after validation.
- **Response caps.** Every response has a byte limit (default 5 MB) to prevent memory exhaustion.

### 3. Path traversal / file system abuse

**Threat:** The LLM proposes reading a file path that, after resolution, points at sensitive system files (`/etc/shadow`, `~/.ssh/id_rsa`, etc.).

**Mitigations:**

- **Symlink resolution.** All file paths are `realpath`-resolved before access.
- **Root allowlist.** After resolution, the path must start with `${CEREBELLUM_BASE_DIR}` or another explicitly configured allowlist root.
- **Forbidden prefix list.** Even within an allowlist root, paths matching `/etc/`, `/root/`, `/proc/`, `/sys/`, `/boot/`, `/var/log/` are hard-denied.
- **Pattern allowlist for file extensions.** Only `.txt`, `.md`, `.json`, `.yaml`, `.log` are readable by default. Configurable.

### 4. Cypher injection

**Threat:** The LLM generates a Cypher query that, when executed against KuzuDB, modifies or deletes data. The proposer can call `episode_store.query` with LLM-generated Cypher.

**Mitigations:**

- **Prefix allowlist.** Only queries starting with `MATCH`, `OPTIONAL MATCH`, `WITH`, `UNWIND`, `RETURN`, or `CALL` are accepted.
- **Keyword blocklist.** Any query containing `CREATE`, `DELETE`, `DETACH`, `SET`, `REMOVE`, `MERGE`, `DROP`, `ALTER` as identifiers (not inside string literals) is rejected.
- **String literal handling.** String literals (both `'...'` and `"..."`) are stripped before keyword checking, so legitimate queries like `WHERE n.name = "DROP table"` are accepted.
- **Multi-statement rejection.** Queries containing `;` are rejected outright.
- **CALL whitelist.** When `CALL` is used, the procedure must be in the read-only allowlist (`db.schema`, `db.show_tables`, `db.show_connections`).

**Known limitation:** the keyword filter uses regex on the post-strip query, which is not a real Cypher tokenizer. Edge cases (escaped quotes inside literals, unusual whitespace, multi-line strings) are covered by 27 regression tests but a real tokenizer is planned (Phase 7).

### 5. Telegram webhook abuse

**Threat:** An attacker hits the public Telegram webhook endpoint with crafted payloads to trigger actions, replay old approvals, or exhaust resources.

**Mitigations:**

- **HMAC secret.** Every webhook request must include the configured `secret_token` header. Mismatched requests get 403.
- **User-ID allowlist.** Even with a valid secret, only updates from users in `TELEGRAM_ALLOWED_USER_IDS` are processed.
- **Replay protection.** Every `update_id` is recorded in SQLite; duplicates are rejected.
- **Constant-time comparison.** Token comparisons use `hmac.compare_digest` to avoid timing side channels.
- **Rate limiting.** Per-IP rate limit on the webhook endpoint.
- **Fuzz tested.** 10,000 iterations of malformed payloads run on every release; no unhandled exceptions, no auth bypass observed.

**Known limitation:** there is no IP allowlist beyond the user-ID check. An attacker who steals the webhook secret can submit forged payloads from any IP. IP allowlisting against Telegram's published IP ranges is planned (Phase 7).

### 6. Kill switch / emergency stop

**Threat:** The system is misbehaving and the operator needs to halt all auto-execute immediately.

**Mitigations:**

- **File-backed flag.** A flag file under `${CEREBELLUM_BASE_DIR}` controls the system's enabled state.
- **Cross-process consistency.** The flag is read with `flock` on every check, so updates are visible immediately to all components.
- **Multiple toggles.** Telegram command, dashboard button, manual `touch kill_switch.flag` all work.
- **Fail-closed.** If the flag file can't be read (filesystem error), the system fails closed (treats it as "halted").

### 7. Budget exhaustion / runaway costs

**Threat:** A proposer loop misfires and burns through OpenRouter credits or hits API rate limits hard.

**Mitigations:**

- **Daily LLM cost cap.** `max_llm_cost_per_day_usd` in `policy.yaml`. Resets at UTC midnight. Tracked persistently across restarts.
- **Per-action rate limit.** Sliding-window cap on actions per hour.
- **Per-tool execution-cost estimate.** Each tool has an estimated cost; the arbiter aggregates over plan steps and rejects auto-execute if estimated cost exceeds `auto_execute.max_cost_usd`.
- **Daily proposal cap.** The proposer itself caps at 50 proposals/day; after that, it pauses for 12 hours.
- **Verifier on a cheap model.** The grounding verifier uses `gpt-4o-mini` by default, not the proposer model.

### 8. State file corruption

**Threat:** A crash mid-write corrupts `kill_switch.flag`, `arbiter_decisions.jsonl`, or `pending_approvals.json`. On restart, the system gets confused state.

**Mitigations:**

- **Atomic writes.** All state writes use temp file + fsync + rename. Either the new content is fully written or the old content is preserved.
- **SQLite WAL.** Event store, hypotheses, feedback all use SQLite WAL mode for crash safety.
- **Schema migrations.** KuzuDB schema changes go through idempotent migration scripts (`src/cerebellum/migrations/*.py`).

### 9. Data leakage via secrets in logs / metadata

**Threat:** An exception traceback, a debug log line, or a Telegram message includes API keys, tokens, or sensitive content from events.

**Mitigations:**

- **Sanitization in proposals.** The proposer's hypothesis sanitizer redacts known secret patterns (`sk-or-v1-*`, `Bearer *`, `password=*`) before storing or sending to Telegram.
- **No secrets in logs.** API responses are not logged at INFO level; tracebacks are sanitized through Python's `logging` framework with a custom filter.
- **EnvironmentFile permissions.** systemd reads `.env` with `chmod 0600`; only the service user can read it.

**Known limitation:** the sanitizer is regex-based and does not catch all possible secret formats. Operators should treat logs as sensitive and restrict access.

### 10. Supply chain

**Threat:** A compromised dependency ships a malicious update that affects CEREBELLUM users.

**Mitigations:**

- **Pinned minor versions.** `pyproject.toml` pins each dependency to a minor-version range. Major versions cannot be auto-installed.
- **`requirements.txt` checked into repo.** Generated from `pyproject.toml`; the exact version set used in CI is reproducible.
- **No `pip install` from URLs or git.** All deps come from PyPI.

**Planned (Phase 7):** SAST scanning, dependency audit (`pip-audit`), secret scanning in CI.

## Audit history

CEREBELLUM has been audited at three checkpoints:

1. **Phase 1 audit (post-rebrand).** Internal review by the maintainer. Found and fixed: `shadow cognition` in LLM prompt, naming inconsistencies.
2. **Phase 6 redo audit.** Internal review by the maintainer. Found and fixed: 14 `mypy --strict` errors that masked real type confusions, Cypher false-positive in `WHERE n.name = "DROP table"`, urllib callers needing migration to the safe-pinned wrapper.
3. **Phase 6 final Opus 4.7 audit.** External review via Anthropic Claude Opus 4.7 in audit mode, prompted as: "Audit this the way you'd audit MemPalace." Verdict: CONDITIONAL PASS. Findings:
    - **HIGH:** NATS TLS server-verify-only is acceptable for single-host but mTLS is required for production multi-host. Deferred to Phase 7.
    - **HIGH:** Cypher filter regex+strip is sufficient for current vectors but a real tokenizer would close edge cases. Deferred to Phase 7.
    - **HIGH:** CALL whitelist appropriate, but `db.show_connections` is admin-diagnostic; consider server-level allowlist. Deferred to Phase 7.
    - **HIGH:** Telegram auth uses `hmac.compare_digest`; IP allowlist + replay protection beyond `update_id` dedup deferred.
    - **MEDIUM:** Coverage 81% global meets gate; ≥90% on security-critical paths deferred.
    - **MEDIUM:** mypy strict applies to all 18 first-party modules with no selective relaxation. Confirmed.
    - **LOW:** Dependency audit, SAST, secret scan deferred to Phase 7.
    - **LOW:** LLM output treated as untrusted input throughout the pipeline. Confirmed.

## Reporting issues

See [`SECURITY.md`](../SECURITY.md). Don't open public issues for vulnerabilities.
