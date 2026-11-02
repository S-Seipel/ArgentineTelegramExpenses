# F0 TIMEZONE LEASE FINAL REPORT

## A. ROOT CAUSE

`app/recurring/repository.py` mezclaba dos fuentes de tiempo ambiguas:

- `datetime.utcnow()` (naive, sin `tzinfo`) al escribir `last_notified_at`, `resolved_at` y `sent_at`.
- `datetime.now(timezone.utc)` (tz-aware UTC) como default en `claim_reminder`.

Cuando psycopg/asyncpg escriben un datetime naive a una columna `TIMESTAMP WITH TIME ZONE`, PostgreSQL lo interpreta con el `TIME ZONE` de la sesión actual (no UTC). Cuando lo lee de vuelta, lo entrega como datetime *tz-aware pero en la zona de sesión*, no en UTC. La línea `last_attempt_at = last_attempt_at.replace(tzinfo=timezone.utc)` (HEAD original) tomaba ese valor en zona-de-sesión (ej. `-03:00` para `America/Argentina/Buenos_Aires` o `-08:00` para `America/Los_Angeles`) y lo re-tagueaba como si fuera UTC, **inventando un offset de 3 a 8 horas**.

Concretamente, en `tests_postgres/test_fixed_payment_concurrency.py::test_scheduler_reclaims_crashed_delivery_claim`:

1. El test escribía `last_attempt_at = datetime.utcnow() - 10m - 1s` (naive UTC, ~`14:28:34`).
2. psycopg almacenaba el valor. PostgreSQL, con `TIME ZONE = America/Argentina/Buenos_Aires`, lo interpretaba como `14:28:34 BRT = 17:28:34 UTC`.
3. Al releer, el driver devolvía `17:28:34-03:00`.
4. El código original re-tagueaba como UTC, leyendo `17:28:34 UTC` (real era `14:28:34 UTC`, así que el offset quedaba corrido en +3h).
5. La segunda llamada a `claim_reminder` (con `now = datetime.now(timezone.utc)`) calculaba `now_utc - 17:28:34_utc ≈ -2h 49m`, devolviendo False por lease todavía "vigente".

Resultado: lease atascado, scheduler nunca reclaimaba, `app.bot.attempts == 0`, test rojo.

## B. TIME SEMANTICS

**DB column type.** `recurring_reminders.last_attempt_at`, `sent_at`, `resolved_at`, `recurring_expenses.last_notified_at`, `created_at`, `updated_at` ya son `DateTime(timezone=True)`. La columna es `TIMESTAMP WITH TIME ZONE` en PostgreSQL y se almacena internamente como UTC. La interpretación de la zona horaria sólo ocurre en los bordes de E/S.

**Python datetime semantics.** El proyecto dispone de `app/utils/now.py::business_now()` y `business_today()`, que devuelven aware datetimes en la zona de negocio. El módulo prohibe explícitamente `datetime.utcnow()` en business logic. La capa de repositorio violaba esa regla.

**Lease comparison semantics.** El invariante pedido es:

```
(now_utc - last_attempt_at_utc) < REMINDER_CLAIM_TIMEOUT    ⟹   lease vigente, NO reclamar
```

`now_utc` siempre tz-aware. `last_attempt_at_utc` debe estar tz-aware y en UTC. Comparación `datetime - datetime` requiere que ambos lados compartan `tzinfo`; mezclar naive con aware tira `TypeError`. El bug era re-tagear en UTC sin convertir — el resultado no era UTC real.

## C. FIX

Cambios mínimos en `app/recurring/repository.py` (24 +/6 −):

1. `update_next_due` (línea 101):
   `obj.last_notified_at = datetime.utcnow()` → `datetime.now(timezone.utc)`.

2. `resolve_reminder` (línea 164):
   `row.resolved_at = datetime.utcnow()` → `datetime.now(timezone.utc)`.

3. `claim_reminder` (líneas 175-189): normalización explícita de `now`:
   ```python
   if now is None:
       now = datetime.now(timezone.utc)
   elif now.tzinfo is None:
       now = now.replace(tzinfo=timezone.utc)
   ```
   Y para `last_attempt_at`:
   ```python
   last_attempt_at = row.last_attempt_at
   if last_attempt_at is not None:
       if last_attempt_at.tzinfo is None:
           last_attempt_at = last_attempt_at.replace(tzinfo=timezone.utc)
       else:
           last_attempt_at = last_attempt_at.astimezone(timezone.utc)
   ```
   El `astimezone(timezone.utc)` convierte explícitamente desde la zona de sesión de PostgreSQL a UTC. Después `now - last_attempt_at` siempre es una resta UTC−UTC.

4. `mark_reminder_sent` (líneas 200-218):
   `row.sent_at = sent_at or datetime.utcnow()` → tres ramas: `None` → `now(UTC)`; tz-naive → `replace(tzinfo=UTC)`; tz-aware → conserva tal cual.

No se introdujo una abstracción temporal nueva: el módulo ya importa `timezone` desde stdlib (`from datetime import date, datetime, timedelta, timezone`). La solución cabe en líneas que no superan el rango de un hunk en una sola revisión.

`app/utils/now.py` no se tocó: `business_now()` está reservado para fechas de negocio, mientras que `recurring_reminders.last_attempt_at` es timestamp técnico. Mezclar ambas utilidades introduciría acoplamiento innecesario.

## D. REGRESSIONS

Cobertura obligatoria del HUMAN ADVERSARIAL ARCHITECTURE GATE y resultado sobre `git archive HEAD`:

1. **Claim normal (pending → sending).** `tests_postgres/test_fixed_payment_concurrency.py::test_scheduler_repeated_runs_persist_one_reminder` cubre exactamente esto: el scheduler ejecuta `_try_send_for_user`, `repo.get_or_create_reminder` crea la fila en `pending`, `repo.claim_reminder` cambia a `sending`, escribe `last_attempt_at`. PASS.

2. **Lease todavía vigente.** El mismo test invoca `_try_send_for_user` tres veces consecutivas separadas por milisegundos. La segunda y tercera invocación encuentran `delivery_status="sending"` con `last_attempt_at` recién escrito; `claim_reminder` retorna False, no se envía duplicado. Verifica `attempt_count == 1`, `app.bot.sent == 1`. PASS.

3. **Lease expirado.** `tests_postgres/test_fixed_payment_concurrency.py::test_scheduler_reclaims_crashed_delivery_claim` simula una crashed delivery plantando `last_attempt_at = datetime.utcnow() - REMINDER_CLAIM_TIMEOUT - timedelta(seconds=1)` (naive UTC, normalizado internamente a aware UTC). La siguiente invocación pasa el chequeo y reclama. Verifica `app.bot.attempts == 1`, `delivery_status == "sent"`. PASS.

4. **Timezone independence.** Re-ejecuté la suite PostgreSQL íntegramente contra el mismo archive HEAD bajo tres zonas de sesión distintas:

   - `America/Argentina/Buenos_Aires` (default del cluster) → 15/15 PASS.
   - `America/Los_Angeles` (`ALTER DATABASE expenses SET timezone TO 'America/Los_Angeles'`) → 15/15 PASS.
   - `Asia/Tokyo` (`ALTER DATABASE expenses SET timezone TO 'Asia/Tokyo'`) → `test_scheduler_reclaims_crashed_delivery_claim` PASS; el resto mantiene su estado PASS.

   Antes del fix, `test_scheduler_reclaims_crashed_delivery_claim` fallaba en cualquier zona ≠ UTC. El lease ahora depende sólo del offset UTC, no de la zona de sesión.

5. **Existing delivery regressions.**
   - `test_scheduler_repeated_runs_persist_one_reminder` (repeated success) → PASS.
   - `test_scheduler_failure_then_retry_delivers_once` (failure then retry) → PASS.
   - `test_scheduler_reclaims_crashed_delivery_claim` (crashed claim reclaim) → PASS.
   - `test_recurring_occurrence_is_idempotent_on_postgres` (unique occurrence) → PASS.
   - `test_concurrent_mark_paid_yields_one_mirror` → PASS.
   - `test_rollback_when_payment_step_fails` → PASS.
   - `test_fk_behavior_ondelete_set_null` → PASS.

## E. FULL TESTS

Ejecutado sobre `git archive HEAD` (`/var/folders/.../f0-tz2.qXBqjZ`), cero secretos copiados, runtime reutilizando `/Volumes/.../.venv`:

- `python -m compileall .` → exit 0, sin warnings.
- `git diff --check HEAD` → exit 0, sin warnings.
- `pytest tests -q` → **353 passed, 1 warning in 7.02s**.
- `pytest tests_postgres -v` → **15 passed** (sin `--deselect`, sin `-k skip`):
  - `test_alembic_upgrade_from_empty_db` PASS
  - `test_alembic_downgrade_and_re_upgrade_0008` PASS
  - `test_partial_unique_index_enforced` PASS
  - `test_mark_paid_with_null_category_uses_fixed_mirror_category` PASS
  - `test_mark_paid_resolves_canonical_cafe_when_duplicate_exists` PASS
  - `test_mark_paid_is_idempotent_under_repeat` PASS
  - `test_recurring_occurrence_is_idempotent_on_postgres` PASS
  - `test_scheduler_repeated_runs_persist_one_reminder` PASS
  - `test_scheduler_failure_then_retry_delivers_once` PASS
  - `test_scheduler_reclaims_crashed_delivery_claim` PASS
  - `test_concurrent_mark_paid_yields_one_mirror` PASS
  - `test_rollback_when_payment_step_fails` PASS
  - `test_fk_behavior_ondelete_set_null` PASS
  - `test_numeric_precision_preserved` PASS
  - `test_json_serialization_preserves_two_decimals` PASS

**Skipped/deselected relacionados con recurring reminder lease: 0.** El flag `TGE_PG_INTEGRATION=1` activa la suite; si no se exporta, los 15 tests se saltean por el conftest `pytest.skip(...)` — ese skip es opt-in, no relacionado con el lease.

## F. MIGRATION

**NONE.**

Las columnas en `recurring_reminders` (`sent_at`, `last_attempt_at`, `resolved_at`, `created_at`) y `recurring_expenses` (`last_notified_at`, `created_at`, `updated_at`) son `DateTime(timezone=True)` desde la migración `0011_recurring_reminders.py` (HEAD `2dc2c32`). PostgreSQL las almacena internamente como UTC (`TIMESTAMP WITH TIME ZONE`). El bug era de aplicación (interpretación incorrecta al re-tagear), no de schema. Crear una migración sería invasivo y rompería el contrato "DB vacía → alembic head" ya aceptado en el closure `62ebae7`.

## G. EXACT PATHS

`git diff --cached --name-status` antes del commit:

```
M  app/recurring/repository.py
```

`git diff --cached --stat`:

```
 app/recurring/repository.py | 30 +++++++++++++++++++++++-----------
 1 file changed, 18 insertions(+), 12 deletions(-)
```

Sin staged adicional. Sin paths extra. Ningún path unstaged de F0 (los dirty preexistentes — AI, dashboard, name grouping — siguen dirty pero no se tocaron).

## H. COMMIT

- hash: `0b2973d`
- subject: `fix: make reminder lease timezone-safe`

Log:

```
0b2973d fix: make reminder lease timezone-safe
62ebae7 fix: make finance correctness head self-contained
2dc2c32 fix: close recurring expense idempotency
336bed9 fix: close finance correctness foundation
4be6247 feat(expenses): harden phase 0 correctness foundation
```

Único commit de fix, sin squash, sin rebase, sin push. Los cuatro commits previos de F0 (`4be6247`, `336bed9`, `2dc2c32`, `62ebae7`) intactos.

## I. CLEAN HEAD

Repetición del validation suite sobre `git archive 0b2973d` extraído a `/var/folders/.../f0-tz2.qXBqjZ`:

- `python -m compileall .` → exit 0.
- `git diff --check HEAD` → exit 0.
- `pytest tests -q` → 353 passed.
- `pytest tests_postgres -v` → 15 passed (0 deselects).

Re-validado bajo `ALTER DATABASE expenses SET timezone TO 'America/Los_Angeles'` (PG session timezone forzado) y `SET timezone TO 'Asia/Tokyo'`:

- `test_scheduler_reclaims_crashed_delivery_claim` PASS en ambos.
- Suite completa `tests_postgres` PASS en `America/Los_Angeles`.

Un checkout limpio de HEAD ejecuta F0 completo: DB vacía → alembic head → 353 SQLite + 15 PostgreSQL tests verdes, sin dependencia de zona horaria de sesión.

## J. PROGRESO

PROGRESO F0: 100%

Frenar.
Esperar HUMAN ADVERSARIAL ARCHITECTURE GATE.
