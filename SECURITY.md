# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability, please open a GitHub issue tagged
`security` or contact the maintainer directly. Do **not** disclose
vulnerabilities in public before a fix is available.

## Threat Model

This bot is designed as a **personal**, single-user expense tracker. The
security model assumes:

- One Telegram user (`TELEGRAM_ALLOWED_USER_ID`) is the only legitimate operator.
- The database and HTTP API are reachable only from `127.0.0.1` on the host.
- The AI inference (Ollama, faster-whisper) runs locally; no expense data
  leaves the host.

## Hardening Recommendations (Production)

If you adapt this bot for a multi-user or public deployment, the following
items in `docker-compose.yml` must be revisited:

| Item | Current (dev) | Recommended (prod) |
|------|---------------|---------------------|
| Postgres port | `127.0.0.1:5432:5432` | not exposed; use unix socket or private network |
| Postgres password | `expenses` (in compose) | strong random password via secret store |
| API port | `127.0.0.1:8000:8000` | reverse proxy with TLS |
| Bot token | required | rotate if leaked (`/revoke` via @BotFather) |
| Data backups | none | encrypted off-host backup of `postgres_data` |

## Audit History

See `SECURITY_AUDIT.md` for the most recent full audit.
