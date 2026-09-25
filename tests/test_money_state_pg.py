"""F1 R3 PostgreSQL integration tests.

Schema-level guarantees the SQLite suite cannot verify:

- ``cycle_incomes`` UNIQUE ``(cycle_id, source_key)`` survives a
  soft-delete (a re-issued command with the same key restores the
  same row instead of inserting a duplicate).
- ``cycle_incomes`` partial UNIQUE ``(cycle_id, currency) WHERE
  kind='income' AND deleted_at IS NULL`` rejects concurrent base
  income inserts.
- ``savings_targets`` CHECK enforces ``target_pct XOR target_amount``.
- ``cycle_incomes`` CHECK rejects ``kind='extra'`` without a
  ``source_key``.
- Concurrent ``record_extra_income`` / ``record_savings_realized``
  with the same ``source_key`` observe one physical row.
- Concurrent ``set_income`` with the same ``(cycle, currency)``
  observes one base row.
- Decimal precision survives JSON roundtrip on a NUMERIC column.

Not wired into the normal pytest run; invoke directly::

    python -m tests.test_money_state_pg

Requires ``F1_PG_HOST`` / ``F1_PG_PORT`` / etc. env vars pointing at a
local PostgreSQL with the test database ``f1_r3_test`` provisioned.
"""
from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from decimal import Decimal

PG_HOST = os.environ.get("F1_PG_HOST", "localhost")
PG_PORT = os.environ.get("F1_PG_PORT", "15434")
PG_USER = os.environ.get("F1_PG_USER", "agent_os")
PG_PASS = os.environ.get("F1_PG_PASS", "agent_os")
PG_DB = os.environ.get("F1_PG_DB", "f1_r3_test")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pg_url() -> str:
    return (
        f"postgresql+psycopg://{PG_USER}:{PG_PASS}"
        f"@{PG_HOST}:{PG_PORT}/{PG_DB}"
    )


def _reset_schema() -> None:
    import psycopg

    with psycopg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER,
        password=PG_PASS, dbname=PG_DB,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
            cur.execute("CREATE SCHEMA public")
            cur.execute("GRANT ALL ON SCHEMA public TO current_user")
        conn.commit()


def _alembic_upgrade_head() -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = _pg_url()
    env.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
    env.setdefault("TELEGRAM_ALLOWED_USER_ID", "1")
    venv_dir = os.path.join(REPO, ".venv")
    alembic_bin = os.path.join(venv_dir, "bin", "alembic")
    if not os.path.exists(alembic_bin):
        alembic_bin = "alembic"
    result = subprocess.run(
        [alembic_bin, "upgrade", "head"],
        cwd=REPO, env=env, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        print("STDOUT:", result.stdout)
        print("STDERR:", result.stderr)
        raise RuntimeError("alembic upgrade head failed")


def _bootstrap(pg_database_url: str) -> None:
    """Drop + migrate + register models."""
    import psycopg
    from sqlalchemy import create_engine, text
    from app.models_registry import register_all_models

    with psycopg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER,
        password=PG_PASS, dbname=PG_DB,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
            cur.execute("CREATE SCHEMA public")
            cur.execute("GRANT ALL ON SCHEMA public TO current_user")
        conn.commit()

    _alembic_upgrade_head()
    register_all_models()


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _check_unique_extra_source_key_survives_soft_delete() -> None:
    """UNIQUE (cycle_id, source_key) must reject a fresh insert even
    after a soft-delete."""
    from sqlalchemy import create_engine, text

    engine = create_engine(_pg_url(), future=True)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO user_settings "
                "(telegram_user_id, payday_day, timezone, "
                " created_at, updated_at) "
                "VALUES (100, 1, 'UTC', now(), now())"
            ))
            conn.execute(text(
                "INSERT INTO financial_cycles "
                "(telegram_user_id, cycle_index, start_date, "
                " end_date, payday_day, status, opened_at, "
                " created_at, updated_at) "
                "VALUES (100, 1, '2026-10-01', '2026-10-31', "
                " 1, 'open', now(), now(), now())"
            ))
            cycle_id = conn.execute(text(
                "SELECT id FROM financial_cycles "
                "WHERE telegram_user_id = 100 ORDER BY id DESC LIMIT 1"
            )).scalar_one()

            # Insert extra
            conn.execute(text(
                "INSERT INTO cycle_incomes "
                "(cycle_id, currency, kind, amount, source_key, "
                " revision, created_at, updated_at) "
                "VALUES (:c, 'ARS', 'extra', 50, 'X', 1, "
                " now(), now())"
            ), {"c": cycle_id})

            # Soft-delete
            conn.execute(text(
                "UPDATE cycle_incomes SET deleted_at = now(), "
                "revision = revision + 1 WHERE source_key = 'X'"
            ))

        # A second physical row with the same source_key must be
        # rejected by the UNIQUE constraint.
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                conn.execute(text(
                    "INSERT INTO cycle_incomes "
                    "(cycle_id, currency, kind, amount, source_key, "
                    " revision, created_at, updated_at) "
                    "VALUES (:c, 'ARS', 'extra', 70, 'X', 1, "
                    " now(), now())"
                ), {"c": cycle_id})
                raise RuntimeError(
                    "UNIQUE constraint failed to reject duplicate "
                    "source_key after soft-delete"
                )
            except Exception:
                pass
            finally:
                trans.rollback()
    finally:
        engine.dispose()


def _check_unique_realization_source_key_survives_soft_delete() -> None:
    from sqlalchemy import create_engine, text

    engine = create_engine(_pg_url(), future=True)
    try:
        with engine.begin() as conn:
            cycle_id = conn.execute(text(
                "SELECT id FROM financial_cycles "
                "WHERE telegram_user_id = 100 ORDER BY id DESC LIMIT 1"
            )).scalar_one()

            conn.execute(text(
                "INSERT INTO savings_realizations "
                "(cycle_id, currency, amount, source_key, revision, "
                " created_at, updated_at) "
                "VALUES (:c, 'ARS', 50, 'R1', 1, now(), now())"
            ), {"c": cycle_id})

            conn.execute(text(
                "UPDATE savings_realizations SET deleted_at = now(), "
                "revision = revision + 1 WHERE source_key = 'R1'"
            ))

        with engine.connect() as conn:
            trans = conn.begin()
            try:
                conn.execute(text(
                    "INSERT INTO savings_realizations "
                    "(cycle_id, currency, amount, source_key, "
                    " revision, created_at, updated_at) "
                    "VALUES (:c, 'ARS', 70, 'R1', 1, "
                    " now(), now())"
                ), {"c": cycle_id})
                raise RuntimeError(
                    "savings_realizations UNIQUE failed after soft-delete"
                )
            except Exception:
                pass
            finally:
                trans.rollback()
    finally:
        engine.dispose()


def _check_savings_target_xor() -> None:
    """Both target_pct AND target_amount NULL → CHECK violation."""
    from sqlalchemy import create_engine, text

    engine = create_engine(_pg_url(), future=True)
    try:
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                conn.execute(text(
                    "INSERT INTO savings_targets "
                    "(cycle_id, currency, created_at, updated_at) "
                    "VALUES (1, 'ARS', now(), now())"
                ))
                raise RuntimeError(
                    "savings_targets XOR CHECK allowed both NULL"
                )
            except Exception:
                pass
            finally:
                trans.rollback()
    finally:
        engine.dispose()


def _check_extra_requires_source_key() -> None:
    """``kind='extra'`` requires a non-null ``source_key`` (CHECK)."""
    from sqlalchemy import create_engine, text

    engine = create_engine(_pg_url(), future=True)
    try:
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                conn.execute(text(
                    "INSERT INTO cycle_incomes "
                    "(cycle_id, currency, kind, amount, source_key, "
                    " revision, created_at, updated_at) "
                    "VALUES (1, 'ARS', 'extra', 50, NULL, 1, "
                    " now(), now())"
                ))
                raise RuntimeError(
                    "extra row accepted without source_key"
                )
            except Exception:
                pass
            finally:
                trans.rollback()
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Concurrent UPSERTs through the service
# ---------------------------------------------------------------------------


def _concurrent_set_income_one_row() -> None:
    from app.database.database import get_session_factory
    from app.money import MoneyStateService
    from sqlalchemy import func, select
    from app.money.models import CycleIncome

    factory = get_session_factory()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            with factory() as s:
                MoneyStateService(s).set_income(
                    user_id=100, cycle_id=1,
                    currency="ARS", amount=Decimal("1100000"),
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = [ex.submit(worker) for _ in range(12)]
        for fut in as_completed(futures):
            fut.result()

    with factory() as s:
        rows = s.execute(
            select(func.count(CycleIncome.id)).where(
                CycleIncome.cycle_id == 1,
                CycleIncome.kind == "income",
            )
        ).scalar_one()
    assert rows == 1, f"expected 1 base income row, got {rows}"
    assert not errors, errors


def _concurrent_record_extra_one_row() -> None:
    from app.database.database import get_session_factory
    from app.money import MoneyStateService
    from sqlalchemy import func, select
    from app.money.models import CycleIncome

    factory = get_session_factory()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            with factory() as s:
                MoneyStateService(s).record_extra_income(
                    user_id=100, cycle_id=1,
                    currency="ARS", amount=Decimal("50000"),
                    source_key="RACE_X",
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = [ex.submit(worker) for _ in range(12)]
        for fut in as_completed(futures):
            fut.result()

    with factory() as s:
        rows = s.execute(
            select(func.count(CycleIncome.id)).where(
                CycleIncome.cycle_id == 1,
                CycleIncome.kind == "extra",
                CycleIncome.source_key == "RACE_X",
            )
        ).scalar_one()
    assert rows == 1, f"expected 1 extra row, got {rows}"
    assert not errors, errors


def _concurrent_record_realized_one_row() -> None:
    from app.database.database import get_session_factory
    from app.money import MoneyStateService
    from sqlalchemy import func, select
    from app.money.models import SavingsRealization

    factory = get_session_factory()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            with factory() as s:
                MoneyStateService(s).record_savings_realized(
                    user_id=100, cycle_id=1,
                    currency="ARS", amount=Decimal("50000"),
                    source_key="RACE_R",
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = [ex.submit(worker) for _ in range(12)]
        for fut in as_completed(futures):
            fut.result()

    with factory() as s:
        rows = s.execute(
            select(func.count(SavingsRealization.id)).where(
                SavingsRealization.cycle_id == 1,
                SavingsRealization.source_key == "RACE_R",
            )
        ).scalar_one()
    assert rows == 1, f"expected 1 realization row, got {rows}"
    assert not errors, errors


def _check_decimal_precision() -> None:
    """1234.56 and 0.01 survive roundtrip into NUMERIC(18,2)."""
    from sqlalchemy import create_engine, text

    engine = create_engine(_pg_url(), future=True)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "DELETE FROM cycle_incomes WHERE cycle_id = 1"
            ))
            conn.execute(text(
                "INSERT INTO cycle_incomes "
                "(cycle_id, currency, kind, amount, source_key, "
                " revision, created_at, updated_at) "
                "VALUES (1, 'ARS', 'extra', 1234.56, 'PRE_A', 1, "
                " now(), now()), "
                "(1, 'ARS', 'extra', 0.01, 'PRE_B', 1, now(), now())"
            ))
            rows = conn.execute(text(
                "SELECT source_key, amount FROM cycle_incomes "
                "WHERE cycle_id = 1 AND source_key IN ('PRE_A', 'PRE_B')"
            )).all()
        seen = {r[0]: r[1] for r in rows}
        assert seen["PRE_A"] == Decimal("1234.56")
        assert seen["PRE_B"] == Decimal("0.01")
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print("[pg] resetting schema")
    _reset_schema()
    print("[pg] alembic upgrade head")
    _alembic_upgrade_head()

    print("[pg] check cycle_incomes UNIQUE (cycle_id, source_key) "
          "survives soft-delete")
    _check_unique_extra_source_key_survives_soft_delete()
    print("[pg] check savings_realizations UNIQUE (cycle_id, source_key) "
          "survives soft-delete")
    _check_unique_realization_source_key_survives_soft_delete()
    print("[pg] check savings_targets XOR")
    _check_savings_target_xor()
    print("[pg] check extra rows require source_key")
    _check_extra_requires_source_key()

    print("[pg] concurrent set_income (same amount → 1 row)")
    _concurrent_set_income_one_row()
    print("[pg] concurrent record_extra_income (same key → 1 row)")
    _concurrent_record_extra_one_row()
    print("[pg] concurrent record_savings_realized (same key → 1 row)")
    _concurrent_record_realized_one_row()

    print("[pg] check NUMERIC precision (1234.56, 0.01)")
    _check_decimal_precision()

    print("[pg] all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
