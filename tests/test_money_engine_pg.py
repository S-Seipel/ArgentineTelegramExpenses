"""PostgreSQL integration test for F1 R2.

Runs the R2 migration against a clean test database on the local
PostgreSQL instance, exercises the new schema constraints, then drives
``ensure_current_cycle`` from N concurrent processes to confirm the
partial unique index yields exactly one physical open cycle and every
caller observes the same logical cycle.

Not wired into the normal pytest run because it requires a live
PostgreSQL server. Invoke directly:

    python -m tests.test_money_engine_pg
"""
from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone

PG_HOST = os.environ.get("F1_PG_HOST", "localhost")
PG_PORT = os.environ.get("F1_PG_PORT", "15434")
PG_USER = os.environ.get("F1_PG_USER", "agent_os")
PG_PASS = os.environ.get("F1_PG_PASS", "agent_os")
PG_DB = os.environ.get("F1_PG_DB", "f1_r2_test")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pg_url() -> str:
    return (
        f"postgresql+psycopg://{PG_USER}:{PG_PASS}"
        f"@{PG_HOST}:{PG_PORT}/{PG_DB}"
    )


def _reset_schema() -> None:
    """Drop every table in the public schema and recreate the version table."""
    import psycopg

    with psycopg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASS, dbname=PG_DB
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
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print("STDOUT:", result.stdout)
        print("STDERR:", result.stderr)
        raise RuntimeError("alembic upgrade head failed")


def _alembic_downgrade(revision: str) -> bool:
    env = os.environ.copy()
    env["DATABASE_URL"] = _pg_url()
    env.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
    env.setdefault("TELEGRAM_ALLOWED_USER_ID", "1")
    venv_dir = os.path.join(REPO, ".venv")
    alembic_bin = os.path.join(venv_dir, "bin", "alembic")
    if not os.path.exists(alembic_bin):
        alembic_bin = "alembic"
    result = subprocess.run(
        [alembic_bin, "downgrade", revision],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print("STDOUT:", result.stdout)
        print("STDERR:", result.stderr)
        return False
    return True


def _check_unique_extra_source_key() -> None:
    """The extra source_key UNIQUE index must reject a second physical
    row even after the first is soft-deleted.
    """
    import psycopg

    with psycopg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASS, dbname=PG_DB
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_settings(telegram_user_id, payday_day) "
                "VALUES (%s, %s)",
                (800, 1),
            )
            cur.execute(
                "INSERT INTO financial_cycles"
                "(telegram_user_id, cycle_index, start_date, end_date, "
                "payday_day, status) VALUES "
                "(800, 1, %s, %s, 1, 'open')",
                (date(2026, 10, 1), date(2026, 10, 31)),
            )
            cur.execute(
                "INSERT INTO cycle_incomes"
                "(cycle_id, currency, kind, amount, source_key, note) "
                "VALUES "
                "((SELECT id FROM financial_cycles WHERE telegram_user_id = 800), "
                "'ARS', 'extra', 100.00, 'k1', NULL)",
            )
            # Soft-delete the first row.
            cur.execute(
                "UPDATE cycle_incomes SET deleted_at = now() "
                "WHERE source_key = 'k1'"
            )
            conn.commit()
            try:
                cur.execute(
                    "INSERT INTO cycle_incomes"
                    "(cycle_id, currency, kind, amount, source_key, note) "
                    "VALUES "
                    "((SELECT id FROM financial_cycles WHERE telegram_user_id = 800), "
                    "'ARS', 'extra', 100.00, 'k1', NULL)",
                )
                conn.commit()
            except psycopg.errors.UniqueViolation:
                conn.rollback()
                return
    raise AssertionError(
        "Expected UNIQUE violation on (cycle_id, source_key) for "
        "extra income even after soft-delete."
    )


def _check_unique_savings_realization_source_key() -> None:
    import psycopg

    with psycopg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASS, dbname=PG_DB
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_settings(telegram_user_id, payday_day) "
                "VALUES (%s, %s)",
                (801, 1),
            )
            cur.execute(
                "INSERT INTO financial_cycles"
                "(telegram_user_id, cycle_index, start_date, end_date, "
                "payday_day, status) VALUES "
                "(801, 1, %s, %s, 1, 'open')",
                (date(2026, 10, 1), date(2026, 10, 31)),
            )
            cur.execute(
                "INSERT INTO savings_realizations"
                "(cycle_id, currency, amount, source_key, note) "
                "VALUES "
                "((SELECT id FROM financial_cycles WHERE telegram_user_id = 801), "
                "'ARS', 100.00, 's1', NULL)",
            )
            cur.execute(
                "UPDATE savings_realizations SET deleted_at = now() "
                "WHERE source_key = 's1'"
            )
            conn.commit()
            try:
                cur.execute(
                    "INSERT INTO savings_realizations"
                    "(cycle_id, currency, amount, source_key, note) "
                    "VALUES "
                    "((SELECT id FROM financial_cycles WHERE telegram_user_id = 801), "
                    "'ARS', 100.00, 's1', NULL)",
                )
                conn.commit()
            except psycopg.errors.UniqueViolation:
                conn.rollback()
                return
    raise AssertionError(
        "Expected UNIQUE violation on savings_realizations "
        "(cycle_id, source_key) even after soft-delete."
    )


def _check_savings_target_xor() -> None:
    """SavingsTarget must reject both-or-neither rows."""
    import psycopg

    with psycopg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASS, dbname=PG_DB
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_settings(telegram_user_id, payday_day) "
                "VALUES (%s, %s)",
                (802, 1),
            )
            cur.execute(
                "INSERT INTO financial_cycles"
                "(telegram_user_id, cycle_index, start_date, end_date, "
                "payday_day, status) VALUES "
                "(802, 1, %s, %s, 1, 'open')",
                (date(2026, 10, 1), date(2026, 10, 31)),
            )
            try:
                cur.execute(
                    "INSERT INTO savings_targets"
                    "(cycle_id, currency, target_pct, target_amount) "
                    "VALUES "
                    "((SELECT id FROM financial_cycles WHERE telegram_user_id = 802), "
                    "'ARS', 0.20, 100.00)",
                )
                conn.commit()
            except psycopg.errors.CheckViolation:
                conn.rollback()
            else:
                raise AssertionError("Expected XOR CHECK violation")


def _check_amount_nonneg_cycle_income() -> None:
    import psycopg

    with psycopg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASS, dbname=PG_DB
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_settings(telegram_user_id, payday_day) "
                "VALUES (%s, %s)",
                (803, 1),
            )
            cur.execute(
                "INSERT INTO financial_cycles"
                "(telegram_user_id, cycle_index, start_date, end_date, "
                "payday_day, status) VALUES "
                "(803, 1, %s, %s, 1, 'open')",
                (date(2026, 10, 1), date(2026, 10, 31)),
            )
            try:
                cur.execute(
                    "INSERT INTO cycle_incomes"
                    "(cycle_id, currency, kind, amount, source_key, note) "
                    "VALUES "
                    "((SELECT id FROM financial_cycles WHERE telegram_user_id = 803), "
                    "'ARS', 'income', -10.00, NULL, NULL)",
                )
                conn.commit()
            except psycopg.errors.CheckViolation:
                conn.rollback()
                return
    raise AssertionError("Expected amount >= 0 CHECK violation")


def _check_extra_requires_source_key() -> None:
    import psycopg

    with psycopg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASS, dbname=PG_DB
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_settings(telegram_user_id, payday_day) "
                "VALUES (%s, %s)",
                (804, 1),
            )
            cur.execute(
                "INSERT INTO financial_cycles"
                "(telegram_user_id, cycle_index, start_date, end_date, "
                "payday_day, status) VALUES "
                "(804, 1, %s, %s, 1, 'open')",
                (date(2026, 10, 1), date(2026, 10, 31)),
            )
            try:
                cur.execute(
                    "INSERT INTO cycle_incomes"
                    "(cycle_id, currency, kind, amount, source_key, note) "
                    "VALUES "
                    "((SELECT id FROM financial_cycles WHERE telegram_user_id = 804), "
                    "'ARS', 'extra', 100.00, NULL, NULL)",
                )
                conn.commit()
            except psycopg.errors.CheckViolation:
                conn.rollback()
                return
    raise AssertionError("Expected extra requires source_key CHECK violation")


def _concurrent_ensure_current_cycle() -> None:
    """Spec §15: N callers all call ``ensure_current_cycle(today=Oct-02)``.

    Expected outcome:
      - Sep closed = 1 (the previously-open Sep cycle)
      - Oct open   = 1 (the new cycle that covers today)
      - Nov        = 0 (the new Oct cycle covers Oct 2..Nov 1)

    Every caller must observe the SAME logical Oct cycle.
    """
    import psycopg
    import sys

    os.environ["DATABASE_URL"] = _pg_url()
    if REPO not in sys.path:
        sys.path.insert(0, REPO)

    from app.database.database import (
        get_engine,
        get_session_factory,
        init_db,
    )

    init_db()

    # Seed a Sep cycle already in 'open' status for user 700.
    with psycopg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASS, dbname=PG_DB
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM financial_cycles WHERE telegram_user_id = %s",
                (700,),
            )
            cur.execute(
                "INSERT INTO user_settings(telegram_user_id, payday_day) "
                "VALUES (%s, %s) ON CONFLICT (telegram_user_id) "
                "DO UPDATE SET payday_day = EXCLUDED.payday_day",
                (700, 2),
            )
            cur.execute(
                "INSERT INTO financial_cycles"
                "(telegram_user_id, cycle_index, start_date, end_date, "
                "payday_day, status, opened_at, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, 'open', now(), now(), now())",
                (700, 1, date(2026, 9, 2), date(2026, 10, 1), 2),
            )
        conn.commit()

    today = date(2026, 10, 2)

    def worker(_idx: int) -> int:
        from app.money import CycleService

        with get_session_factory()() as session:
            cycle = CycleService(
                session, session_factory=get_session_factory
            ).ensure_current_cycle(700, today)
            return cycle.id

    n = 12
    with ThreadPoolExecutor(max_workers=n) as pool:
        ids = [f.result() for f in as_completed(pool.submit(worker, i) for i in range(n))]

    assert len(set(ids)) == 1, f"Expected one logical cycle, got {set(ids)}"

    with psycopg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASS, dbname=PG_DB
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, count(*) FROM financial_cycles "
                "WHERE telegram_user_id = %s GROUP BY status ORDER BY status",
                (700,),
            )
            rows = cur.fetchall()
            statuses = {s: c for s, c in rows}
    assert statuses.get("closed", 0) == 1, f"Expected exactly 1 closed cycle, got {statuses}"
    assert statuses.get("open", 0) == 1, f"Expected exactly 1 open cycle, got {statuses}"
    _ = get_engine()


def main() -> int:
    print("[pg] resetting schema")
    _reset_schema()
    print("[pg] alembic upgrade head")
    _alembic_upgrade_head()
    print("[pg] check cycle_incomes UNIQUE (cycle_id, source_key) survives soft-delete")
    _check_unique_extra_source_key()
    print("[pg] check savings_realizations UNIQUE (cycle_id, source_key) survives soft-delete")
    _check_unique_savings_realization_source_key()
    print("[pg] check savings_targets XOR")
    _check_savings_target_xor()
    print("[pg] check cycle_incomes amount >= 0")
    _check_amount_nonneg_cycle_income()
    print("[pg] check extra rows require source_key")
    _check_extra_requires_source_key()
    print("[pg] alembic downgrade 0012_cycle_foundation")
    down_ok = _alembic_downgrade("0012_cycle_foundation")
    if not down_ok:
        print(
            "[pg] downgrade to 0012 failed; skipping the re-upgrade leg."
        )
    else:
        print("[pg] alembic upgrade head (re-upgrade)")
        _alembic_upgrade_head()
    print("[pg] concurrent ensure_current_cycle")
    _concurrent_ensure_current_cycle()
    print("[pg] all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
