# Security Policy

## Hardening Assumptions

- Services run locally under a dedicated unprivileged user.
- Secrets are provided explicitly in `.env`; `systemd` does not expand `${VAR}` placeholders in `EnvironmentFile` entries.
- Dashboard and Telegram integrations are expected to stay behind network and token-based access controls.
- Outbound HTTP calls are limited to non-redirecting helpers and existing SSRF guards in the codebase.
- Local state files, SQLite databases, and graph storage are trusted only on a single-host deployment.

## Operational Expectations

- Replace all placeholder secrets before enabling services.
- Restrict filesystem permissions on the project directory, `.env`, databases, and graph files.
- Expose the dashboard only through a trusted reverse proxy or localhost binding.
- Keep Python dependencies and service definitions updated with security fixes.
- Review logs regularly for repeated auth failures, webhook abuse, or unexpected outbound requests.

## NATS TLS Configuration

NATS JetStream connections support TLS encryption and client certificate authentication.

### Environment Variables

| Variable | Description |
|---|---|
| `CEREBELLUM_NATS_TLS_CERT` | Path to client certificate file (PEM) |
| `CEREBELLUM_NATS_TLS_KEY` | Path to client private key file (PEM) |
| `CEREBELLUM_NATS_TLS_CA` | Path to CA certificate file (PEM) |

### Config Keys

Alternatively, set via `nats` config dict:

| Key | Description |
|---|---|
| `nats.tls` | `true` to enable TLS (defaults to `false`) |
| `nats.tls_cert` | Client certificate path |
| `nats.tls_key` | Client private key path |
| `nats.tls_ca` | CA certificate path |

### Modes

- **Server verification only** — Set `nats.tls = true` with `tls_ca`. The client verifies the server's certificate against the CA.
- **Mutual TLS (mTLS)** — Set `nats.tls = true` with `tls_ca`, `tls_cert`, and `tls_key`. Both server and client verify each other's certificates.

### Requirements

- `CEREBELLUM_NATS_TOKEN` remains mandatory regardless of TLS mode.
- TLS is negotiated before authentication; both layers are required for production deployments.
- When TLS is enabled, the server URL scheme changes from `nats://` to `tls://`.
