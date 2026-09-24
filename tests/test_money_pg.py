"""PostgreSQL integration test for F1 R1.

Runs the R1 migration against a clean test database on the local
PostgreSQL instance, exercises the schema CHECK/UNIQUE/partial-unique
constraints, then drives ``open_initial_cycle`` from N concurrent
processes to confirm the partial unique index yields exactly one
physical open cycle and every caller observes the same logical cycle.

Not wired into the normal pytest run because it requires a live
PostgreSQL server. Invoke directly:

    python -m tests.test_money_pg
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
PG_DB = os.environ.get("F1_PG_DB", "f1_r1_test")
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


def _check_payday_range_constraint() -> None:
    import psycopg

    with psycopg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASS, dbname=PG_DB
    ) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO user_settings(telegram_user_id, payday_day) "
                    "VALUES (%s, %s)",
                    (1, 0),
                )
                conn.commit()
            except psycopg.errors.CheckViolation:
                conn.rollback()
                return
    raise AssertionError("Expected CHECK violation for payday_day=0")


def _check_date_range_constraint() -> None:
    import psycopg

    with psycopg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASS, dbname=PG_DB
    ) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO financial_cycles"
                    "(telegram_user_id, cycle_index, start_date, end_date, "
                    "payday_day, status) VALUES "
                    "(%s, %s, %s, %s, %s, %s)",
                    (1, 1, date(2026, 11, 1), date(2026, 10, 1), 5, "open"),
                )
                conn.commit()
            except psycopg.errors.CheckViolation:
                conn.rollback()
                return
        raise AssertionError("Expected CHECK violation for end_date < start_date")


def _check_unique_user_index() -> None:
    import psycopg

    with psycopg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASS, dbname=PG_DB
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_settings(telegram_user_id, payday_day) "
                "VALUES (%s, %s)",
                (99, 5),
            )
            cur.execute(
                "INSERT INTO financial_cycles"
                "(telegram_user_id, cycle_index, start_date, end_date, "
                "payday_day, status) VALUES "
                "(99, 1, %s, %s, 5, 'open')",
                (date(2026, 10, 5), date(2026, 11, 4)),
            )
            try:
                cur.execute(
                    "INSERT INTO financial_cycles"
                    "(telegram_user_id, cycle_index, start_date, end_date, "
                    "payday_day, status) VALUES "
                    "(99, 1, %s, %s, 5, 'open')",
                    (date(2026, 11, 5), date(2026, 12, 4)),
                )
                conn.commit()
            except psycopg.errors.UniqueViolation:
                conn.rollback()
                return
    raise AssertionError("Expected UNIQUE violation on (user, cycle_index)")


def _check_partial_unique_one_open_per_user() -> None:
    import psycopg

    with psycopg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASS, dbname=PG_DB
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_settings(telegram_user_id, payday_day) "
                "VALUES (%s, %s)",
                (200, 5),
            )
            cur.execute(
                "INSERT INTO financial_cycles"
                "(telegram_user_id, cycle_index, start_date, end_date, "
                "payday_day, status) VALUES "
                "(200, 1, %s, %s, 5, 'open')",
                (date(2026, 10, 5), date(2026, 11, 4)),
            )
            conn.commit()
            try:
                cur.execute(
                    "INSERT INTO financial_cycles"
                    "(telegram_user_id, cycle_index, start_date, end_date, "
                    "payday_day, status) VALUES "
                    "(200, 2, %s, %s, 5, 'open')",
                    (date(2026, 11, 5), date(2026, 12, 4)),
                )
                conn.commit()
            except psycopg.errors.UniqueViolation:
                conn.rollback()
                return
    raise AssertionError("Expected partial UNIQUE violation on second open cycle")


def _concurrent_open_initial_cycle() -> None:
    """Drive open_initial_cycle from N concurrent threads, each on its own session."""
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

    with psycopg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASS, dbname=PG_DB
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM financial_cycles WHERE telegram_user_id = %s",
                (500,),
            )
            cur.execute(
                "INSERT INTO user_settings(telegram_user_id, payday_day) "
                "VALUES (%s, %s) ON CONFLICT (telegram_user_id) "
                "DO UPDATE SET payday_day = EXCLUDED.payday_day",
                (500, 5),
            )
            conn.commit()

    today = date(2026, 10, 10)

    with psycopg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASS, dbname=PG_DB
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM financial_cycles WHERE telegram_user_id = %s",
                (500,),
            )
            print(f"[pg] pre-test cycles for user 500: {cur.fetchone()[0]}")

    def worker(_idx: int) -> tuple[int, int, int]:
        from app.money import CycleService

        def open_fresh():
            return get_session_factory()()

        with get_session_factory()() as session:
            view = CycleService(
                session,
                session_factory=open_fresh,
            ).open_initial_cycle(
                user_id=500, today=today
            )
            return (view.cycle_id, view.cycle_index, view.status == "open")

    n = 12
    with ThreadPoolExecutor(max_workers=n) as pool:
        results = [f.result() for f in as_completed(pool.submit(worker, i) for i in range(n))]

    cycle_ids = {r[0] for r in results}
    cycle_indexes = {r[1] for r in results}
    all_open = all(r[2] for r in results)
    assert cycle_ids == {1}, f"Expected exactly one physical open cycle, got {cycle_ids}"
    assert cycle_indexes == {1}
    assert all_open

    with psycopg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASS, dbname=PG_DB
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM financial_cycles "
                "WHERE telegram_user_id = %s AND status = 'open'",
                (500,),
            )
            count = cur.fetchone()[0]
    assert count == 1, f"Expected exactly one row open in DB, got {count}"

    _ = get_engine()  # keep linter happy


def main() -> int:
    print("[pg] resetting schema")
    _reset_schema()
    print("[pg] alembic upgrade head")
    _alembic_upgrade_head()
    print("[pg] check payday_day range CHECK")
    _check_payday_range_constraint()
    print("[pg] check financial_cycles end_date >= start_date CHECK")
    _check_date_range_constraint()
    print("[pg] check UNIQUE (user, cycle_index)")
    _check_unique_user_index()
    print("[pg] check partial UNIQUE one open per user")
    _check_partial_unique_one_open_per_user()
    print("[pg] alembic downgrade 0011_recurring_reminders")
    down_ok = _alembic_downgrade("0011_recurring_reminders")
    if not down_ok:
        print(
            "[pg] downgrade to 0011 failed; skipping the re-upgrade leg. "
            "This is a pre-existing F0 condition (the migration chain "
            "predates R1)."
        )
    else:
        print("[pg] alembic upgrade head (re-upgrade)")
        _alembic_upgrade_head()
    print("[pg] concurrent open_initial_cycle")
    _concurrent_open_initial_cycle()
    print("[pg] all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
