# Roadmap

Proposed improvements for the bot, organized by **value vs effort**. Pick
from the top of the list when picking up new work.

Financial phases reviewed: 2026-11-02. The feature ideas below retain their
original 2026-09-09 prioritization and are not a completion checklist.

## Financial phases

| Phase | Scope | State |
|---|---|---|
| F0 | Expense identity, exact money, fixed-payment mirrors, reminders, dates and migrations | Closed |
| F1 R1 | Payday configuration and cycle bootstrap | Closed |
| F1 R2 | Read-only MoneyEngine, cycle closing, rollover and snapshots | Closed |
| F1 R3 | Income, extras and savings mutations | Closed |
| F1 R4 | Repair, Telegram/API/dashboard integration, immutable history and concurrency checks | Running locally |
| F1.1 | Archive previous monthly income in `monthly_budget_legacy`, preserving records and rejecting legacy writes | Running locally; introduced in migration 0016 |
| F2 | Cycle commands and dashboard are the primary financial interface; `/liberado` aliases `/disponible` | Running locally |
| F3 | Independent payday settings, cycles, rollover and money isolation per currency | Running locally on migration 0017 |
| F1.5 | Opt-in recurring commitments with stable due dates across reminder snoozes | Running locally; introduced in migration 0014 |
| Payday calendar | Fixed monthly day or the first selected weekday; first Friday is supported explicitly | Implemented and validated on migration 0015; rollout recorded separately |
| Local backups | Manual copies and daily Buenos Aires schedule, checksums, isolated restore and seven-copy automatic retention | Installed locally; the daily job and its first verified copy passed |

The existing local installation was updated on 2026-11-02. Telegram polling,
the dashboard, and the configured text provider were checked after startup.
Whisper's cached model runs as the non-root app user; silent audio is rejected
without inventing a transcription. The existing environment configuration
was preserved. Original database records and currency totals were unchanged.

Release validation includes the normal suite on the Python 3.12 runtime
image, PostgreSQL migration and concurrency tests, runtime dependency
auditing, browser checks and a rehearsal on an isolated restored backup.
Exact results are recorded per release rather than freezing a test count
in this roadmap.

F1.5 forecasts the cycle and retains one overdue occurrence per recurring
template; it does not accumulate forgotten monthly installments as debt.
Closed snapshots remain immutable. Ordinary reminders reserve no money.

F1.1/F2/F3 extend F1; they are not additional R blocks. Migration 0016
renames the old monthly table without copying its amounts into cycle
income. Its compatibility API remains read-only. Migration 0017 introduces
currency calendars and independent cycle identities. Each currency has its
own current cycle, closing snapshot, successor and pending obligations.
Existing closed mixed-currency snapshots remain unchanged and readable.
There is no currency conversion or combined ARS/USD total.

The first-Friday calendar in ARS remains configured. An explicit calendar
change affects only that currency's successors, preserving open and closed
cycle dates. Saving a calendar does not create a cycle or enter income.

---

## Tier 1 — High value, medium effort

These change how the bot is used day-to-day. Worth doing before the
fancys.

### 1.1 Monthly budgets with alerts
**Value:** prevents overspending before it happens.
**Effort:** ~3h.

- `/presupuesto comida 50000` saves a per-category monthly limit.
- After each expense, check if the user exceeded 80% or 100% of any
  category budget. Notify in the same turn with a friendly warning.
- New table `budgets` (category_id, monthly_limit, currency, valid_from).
- Job in the same asyncio loop as the recurring reminder scheduler.
- Commands: `/presupuesto`, `/presupuesto_del`, `/presupuestos`.

### 1.2 Receipt / ticket vision
**Value:** unique feature, lets you snap a paper ticket instead of typing.
**Effort:** ~4-6h (plus model downloads). Plus vision LLMs hallucinate
numeric values; verified that even with `llava:7b` + `qwen3:4b` 2-pass
the amount extraction is unreliable for receipts with many small line
items. Deferred — fix the data path before adding the surface.

### 1.3 Dashboard web (`/dashboard`) ✅ done
**Value:** visualization that beats scrolling through chat.
**Effort:** ~3-4h.

---

## Tier 2 — Quick wins (≤2h each)

Small, contained changes that compound into a much better experience.

### 2.1 Search by text
**Value:** finds old expenses without scrolling `/gastos` endlessly.
**Effort:** ~1h.

- `/buscar starbucks` or natural `gastos en starbucks`.
- `ILIKE %starbucks%` filter on `expenses.name`.
- Returns compact list with IDs for follow-up edit/delete.

### 2.2 Month-over-month comparison
**Value:** instant insight without thinking.
**Effort:** ~1h.

- "gasté 30% más en comida que el mes pasado" type answers.
- Sum this month + last month per category, compute diff %, render with
  ↑↓ emoji.

### 2.3 Custom date range queries
**Value:** the parser already supports relative dates; expose more.
**Effort:** ~1-2h.

- Extend `parse_relative_date` to handle "del 5 al 15 de marzo".
- New `QuerySpec.period` values: `range_start` + `range_end`.
- Voice: "gastos del 1 al 10 de agosto".

### 2.4 Snooze variants
**Value:** current `/posponer` is binary. Granular control helps.
**Effort:** ~1h.

- Replace "posponer" with a small menu: "mañana", "en 3 días",
  "el viernes que viene".
- Recognized by the reminder reply dispatcher in `handlers.py`.

### 2.5 Undo last action
**Value:** safety net for accidental delete/edit.
**Effort:** ~2h.

- `/undo` reverts the most recent destructive action.
- Track last 10 actions in memory (or a small `audit_log` table).

---

## Tier 3 — Experimental / fancys

Cool but each carries meaningful trade-offs.

### 3.1 Multi-user support
**Value:** makes the bot shareable with family / partner.
**Effort:** ~4-6h.

- Replace `TELEGRAM_ALLOWED_USER_ID` with a `users` table.
- Per-user settings (timezone, allowed categories, budget).
- Migration of existing single-user data is mostly free (just attach
  user_id to all rows).

### 3.2 Voice replies from the bot
**Value:** hands-free confirmation (e.g., while driving).
**Effort:** ~2-3h.

- Use `edge-tts` (free, Microsoft, no API key) to generate audio.
- Send audio file as reply instead of (or alongside) text.
- Adds ~50MB of dependencies.

### 3.3 Geolocation of expenses
**Value:** nice-to-have for travel tracking.
**Effort:** ~3h.

- Telegram sends location when shared.
- Store lat/long alongside expense.
- Visual map optional (Leaflet + OSM tiles).

### 3.4 ML classifier for categorization
**Value:** could be faster than the LLM for big batches.
**Effort:** ~6h (training + serving).

- Train a scikit-learn classifier on historical `expenses.name →
  category` pairs.
- Use it as a fast-path before falling back to LLM.
- Diminishing returns given the LLM is already <10s now.

### 3.5 Spending prediction
**Value:** projection of month-end total.
**Effort:** ~2h.

- Linear extrapolation: `(spent_so_far / day_of_month) * days_in_month`.
- Surface in the daily reminder scheduler.

---

## Tier 4 — Polish

Things to clean up while we have time.

### 4.1 Fix `@botname` in group chats
**Value:** enables multi-chat use cases.
**Effort:** ~10min.

- `command = parts[0].lower().split("@", 1)[0]` in
  `app/bot/handlers.py:42`.
- Without this, commands silently fail in groups.

### 4.2 Backup automation — implemented
**Value:** protects the DB against accidental wipe.
**Effort:** ~1h.

- Host-side custom-format `pg_dump` backups in the user's private
  Application Support directory, validated with `pg_restore --list` and SHA-256.
- Daily 03:00 Buenos Aires cutoff with login/wake catch-up; a lightweight
  LaunchAgent checks every 15 minutes while the script prevents duplicates.
- Retain the latest seven valid automatic copies; manual copies remain
  outside retention. Restore drills use a disposable database, never an
  automatic replacement of the live database. See README for commands.

### 4.3 Metric export (Prometheus)
**Value:** observability if you ever scale past 1 user.
**Effort:** ~2h.

- `/metrics` endpoint with counters for: messages processed,
  AI latency histogram, errors by type, recurring reminders sent.

### 4.4 Translation to English / Portuguese
**Value:** widens audience if published.
**Effort:** ~4h.

- All hardcoded strings are in Spanish (lunfardo-aware).
- Extract to a `translations.py` module.
- Locale flag per user.

---

## Decisions deferred

- **Switching the LLM**: `qwen3:4b` is now well-tuned for this use case
  (3-10s responses after the perf commit). No urgent reason to change.
- **Cloud AI fallback**: explicitly out of scope (local-first is a core
  promise of the bot).
- **Payment integrations** (Mercado Pago scraping, etc.): out of scope;
  scope creep risk.
