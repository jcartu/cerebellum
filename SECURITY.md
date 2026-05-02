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
