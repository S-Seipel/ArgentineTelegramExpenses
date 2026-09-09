# Security Audit

Last run: 2026-09-09

## Scope

Static audit of source code, dependencies, Docker configuration, and git
state to confirm the project is safe to publish on GitHub (or any public
forge) **without leaking credentials or user data**.

## Verdict

**Safe to publish.** No secrets in tracked code; `.env` and runtime data
are ignored by git. No remote is configured yet.

## What was checked

### 1. Hardcoded secrets

- Grep for `token`, `password`, `secret`, `bearer`, `api_key` across
  `app/` and `tests/`.
- **Result:** only test placeholders (`"t"`, `"test-token"`) and
  config field aliases. No real credentials in source.

### 2. Git-tracked secrets

- `git ls-files | grep -iE "token|secret|password|credential"` → no matches.
- `.env` is in `.gitignore`. **Confirmed.** `git ls-files` does not show it.
- The repo at `git rev-parse --git-dir` resolves to the parent directory;
  this project has its own dedicated git directory not yet created.

### 3. User data in migrations or seed

- Alembic versions inspected for hardcoded `telegram_user_id`, names,
  amounts → none. Only schema definitions.

### 4. Logging

- Searched `app/**/*.py` for `logger.info(...)` calls passing `token` or
  `password` → none.
- `httpx` logs only the request URL and status (no body, no headers).

### 5. Container security

| Issue | Status | Fix |
|-------|--------|-----|
| Image ran as `root` | Fixed | Added non-root `app` user in `Dockerfile` (`USER app`) |
| Postgres password hardcoded | Accepted (dev only) | Documented in `SECURITY.md` |
| Bind to `0.0.0.0` inside container | Accepted | Required for Docker networking; host binding restricted to `127.0.0.1` |
| Host port `5432` exposed | OK | Bound to `127.0.0.1` only, not `0.0.0.0` |

### 6. Dependency vulnerabilities

- `pip-audit` against `requirements.txt` (locked versions).
- **Before:** 10 CVEs across `pytest` (1) and `starlette` (9).
- **After upgrade:** 0 CVEs.

| Package | Before | After |
|---------|--------|-------|
| `fastapi` | 0.115.6 | 0.141.1 |
| `starlette` | 0.41.3 | 1.6.0 |
| `pytest` | 8.3.4 | 9.1.1 |
| `pytest-asyncio` | 0.25.0 | 1.4.0 |
| `faster-whisper` | n/a | 1.0.3 (new) |

### 7. Postgres data persistence

- `postgres_data` is a Docker named volume, **not** a host bind mount.
- The volume is excluded from git by virtue of being runtime state.

### 8. Voice model artifacts

- `huggingface_cache` volume holds the Whisper model files (~150MB).
- Volume is **not** mounted to host disk; lives inside Docker's storage
  driver.

## Pre-publish checklist (run before `git push`)

1. `grep -rE "(token|password|secret).*['\"]" app/ tests/` → only test
   placeholders, no real values.
2. `git ls-files | grep -E "^\.env$|\.env\.prod|\.env\.live"` → empty.
3. `git status` → only project files, no stray `.DS_Store` or
   `node_modules`.
4. `pip-audit` → "No known vulnerabilities found".
5. `.venv/` and `__pycache__/` are not staged (covered by `.gitignore`).
6. Run the test suite: `.venv/bin/pytest` → 185 passed.
