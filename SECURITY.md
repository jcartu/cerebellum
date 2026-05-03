# Security Policy

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

If you discover a security issue, email the maintainer directly:

- **Email:** josh@cartu.com
- **PGP key:** (optional, link to public key here if you have one)

You can expect:

- Acknowledgement within 72 hours.
- A timeline for the fix within 7 days.
- Credit in the changelog (if you want it; we respect anonymous reports).

If you don't hear back within 72 hours, follow up. We may have missed it.

## Scope

We're interested in reports about:

- Authentication/authorization bypasses (dashboard, Telegram webhook, NATS).
- SSRF, path traversal, Cypher injection, command injection.
- Leaks of `.env` contents, OpenRouter keys, or other secrets through logs or error responses.
- Replay attacks against the Telegram webhook.
- Kill switch bypasses (anything that lets auto-execute happen when the flag is set).
- Memory-exhaustion or DOS vectors.
- Supply chain issues with our pinned dependencies.

We're **not** interested in reports about:

- Issues that require pre-existing local access to the host running CEREBELLUM (the trust boundary is the host; we assume the host is already trusted).
- Configuration mistakes that aren't actually exploitable (e.g., "if you set `forbidden_tools: []` and run with `min_confidence: 0`, things go badly").
- Theoretical attacks against an LLM proposer that don't lead to a concrete bypass of the policy arbiter, kill switch, or other controls.

## Security model overview

CEREBELLUM is designed around a few core assumptions:

- **The LLM is not trusted.** All LLM-produced content (proposals, Cypher, URLs, file paths) goes through validation before it touches the system.
- **The host is trusted.** Anyone with shell access to the host can read state files and kill the service. We don't try to defend against that.
- **The network outside the host is hostile.** All inbound traffic requires authentication. All outbound traffic goes through SSRF-pinned, redirect-refusing wrappers.
- **The Telegram webhook secret is secret.** If it leaks, an attacker can submit forged updates. Rotate it if you suspect compromise.

For the full threat model, see [`docs/security-model.md`](docs/security-model.md).

## Hardening assumptions

- Services run locally under a dedicated unprivileged user (the systemd unit templates default to your invoking user, but you should create a dedicated user for production).
- Secrets are provided in `.env` with `chmod 0600`. systemd does not expand `${VAR}` placeholders in `EnvironmentFile` entries — values must be literal.
- Dashboard binds to loopback by default. If you expose it publicly, put a reverse proxy in front of it and require an additional auth layer.
- Outbound HTTP from CEREBELLUM uses non-redirecting helpers and SSRF-pinned IPs.
- SQLite databases and graph storage are trusted only on a single-host deployment.

## Operational expectations

- Replace all placeholder secrets in `.env.example` before enabling services.
- Restrict filesystem permissions on the project directory, `.env`, databases, and graph files (`chmod 0600` on `.env`, `0700` on the base directory).
- Expose the dashboard only through a trusted reverse proxy or localhost binding.
- Keep Python dependencies updated. `pyproject.toml` pins minor versions but you should refresh quarterly.
- Review logs (`journalctl --user -u cerebellum-*`) regularly for repeated auth failures, webhook abuse, or unexpected outbound requests.

## NATS TLS configuration

NATS JetStream connections support TLS encryption and client certificate authentication.

### Environment variables

| Variable | Description |
| :--- | :--- |
| `CEREBELLUM_NATS_TLS_CERT` | Path to client certificate file (PEM) |
| `CEREBELLUM_NATS_TLS_KEY` | Path to client private key file (PEM) |
| `CEREBELLUM_NATS_TLS_CA` | Path to CA certificate file (PEM) |

### Config keys

Alternatively, set via the `nats` config dict in `config.json`:

| Key | Description |
| :--- | :--- |
| `nats.tls` | `true` to enable TLS (defaults to `false`) |
| `nats.tls_cert` | Client certificate path |
| `nats.tls_key` | Client private key path |
| `nats.tls_ca` | CA certificate path |

### Modes

- **Server verification only** — set `nats.tls = true` with `tls_ca`. The client verifies the server's certificate against the CA.
- **Mutual TLS (mTLS)** — set `nats.tls = true` with `tls_ca`, `tls_cert`, and `tls_key`. Both server and client verify each other's certificates.

### Requirements

- `CEREBELLUM_NATS_TOKEN` remains mandatory regardless of TLS mode (TLS handles encryption; the token handles authentication).
- TLS is negotiated before authentication; both layers are required for production deployments.
- When TLS is enabled, the server URL scheme changes from `nats://` to `tls://` automatically.

## Audit history

CEREBELLUM has been audited at three checkpoints across the alpha rebuild. The full findings and dispositions are documented in [`decisions.md`](decisions.md). See [`docs/security-model.md`](docs/security-model.md) for the complete threat model and audit log.
