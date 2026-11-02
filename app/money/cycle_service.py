"""F1 service layer for the money domain.

R1 wired up the bare bootstrap (open_initial_cycle, get_current_cycle,
payday config). R2 owns the lifecycle:

- ``CycleService.close_cycle`` — lock + summary + snapshot + commit
  in one transaction. ``MoneyEngine`` provides the read model
  ``CycleSummary`` inside the same session.
- ``CycleService.rollover_cycle`` — close any expired open cycle and
  open the next natural one. Pending obligations do NOT block the
  natural rollover.
- ``CycleService.ensure_current_cycle`` — catch-up walk that
  idempotently walks forward until the open cycle covers ``today``.
  Defensive cap of 12 steps.

The dependency arrow is one-way: ``CycleService`` consumes
``MoneyEngine`` (read) and the snapshot builder. ``MoneyEngine`` does
not import anything from this module, so the cycle service can change
without ever rippling into the read engine.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.money.cycle_resolution import (
    CycleWindow,
    cycle_window_for,
    successor_window_for,
)
from app.money.dto import CycleSummary
from app.money.engine import (
    CycleCatchUpLimitExceeded,
    CycleHasPendingError,
    MoneyEngine,
)
from app.money.fixed_obligations import (
    resolve_carry_over_for_close,
)
from app.money.models import FinancialCycle, UserSettings
from app.money.repository import (
    DEFAULT_PAYDAY_DAY,
    FinancialCycleRepository,
    UserSettingsRepository,
    cycle_currency_scope,
    normalize_currency,
)
from app.money.snapshot import (
    build_closing_snapshot,
)


class UserSettingsService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = UserSettingsRepository(session)

    def get_settings(self, user_id: int, *, currency: str = "ARS"):
        return self.repo.get_or_default(user_id, currency=currency)

    def set_payday_day(
        self,
        user_id: int,
        payday_day: int,
        *,
        timezone: Optional[str] = None,
        currency: str = "ARS",
    ) -> UserSettings:
        return self.repo.upsert_payday_day(
            user_id, payday_day, timezone=timezone, currency=currency
        )

    def set_payday_rule(
        self, user_id: int, pay_rule: str, *, weekday: Optional[int] = None,
        payday_day: Optional[int] = None, timezone: Optional[str] = None,
        currency: str = "ARS",
    ) -> UserSettings:
        return self.repo.upsert_payday_rule(
            user_id, pay_rule, weekday=weekday,
            payday_day=payday_day, timezone=timezone, currency=currency,
        )

    def set_first_weekday(
        self, user_id: int, weekday: int = 4, *, timezone: Optional[str] = None, currency: str = "ARS",
    ) -> UserSettings:
        return self.set_payday_rule(
            user_id, "first_weekday", weekday=weekday, timezone=timezone, currency=currency,
        )


class CycleRolloverRequired(RuntimeError):
    """Raised when the user has history but no open cycle for ``today``.

    R1 explicitly stops here. R2 will resolve this with the actual
    rollover logic.
    """

    def __init__(
        self,
        user_id: int,
        latest_cycle: FinancialCycle,
        today: date,
    ) -> None:
        # Snapshot the scalars we need so the message survives the
        # session rollback that propagates this exception out of the
        # service layer (otherwise the ORM entity is detached and the
        # __str__ raises DetachedInstanceError).
        self.user_id = user_id
        self.latest_cycle_index = latest_cycle.cycle_index
        self.latest_cycle_start = latest_cycle.start_date
        self.latest_cycle_end = latest_cycle.end_date
        self.latest_cycle = latest_cycle
        self.today = today
        super().__init__(
            f"User {user_id} has cycle #{self.latest_cycle_index} "
            f"({self.latest_cycle_start}..{self.latest_cycle_end}) "
            f"but no open cycle for today ({today}); rollover is required."
        )


@dataclass(frozen=True)
class CurrentCycleView:
    """Read-only projection of an open cycle for the UI layer.

    Mirrors ``CycleWindow`` + the row's bookkeeping fields without
    exposing the ORM entity.
    """

    cycle_id: int
    cycle_index: int
    start_date: date
    end_date: date
    payday_day: int
    status: str
    pay_rule: str = "fixed_day"
    payday_weekday: Optional[int] = None
    currency: str = "ARS"
    legacy_mixed_currency: bool = False


class CycleService:
    def __init__(
        self,
        session: Session,
        *,
        session_factory: Optional[Callable[[], Session]] = None,
    ) -> None:
        """``session_factory`` is only used to recover from concurrent
        open attempts (one IntegrityError, one fresh session to re-read).
        Tests that don't exercise concurrency can omit it.
        """
        self.session = session
        self.cycles = FinancialCycleRepository(session)
        self.settings = UserSettingsRepository(session)
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # settings helpers
    # ------------------------------------------------------------------

    def _payday_day(self, user_id: int, *, currency: str = "ARS") -> int:
        return self._payday_config(user_id, currency=currency)["payday_day"]

    def _payday_config(self, user_id: int, *, currency: str = "ARS", freeze: bool = False) -> dict:
        currency = normalize_currency(currency)
        row = (self.settings.materialize_currency(user_id, currency) if freeze
               else self.settings.get_or_default(user_id, currency=currency))
        return {
            "payday_day": row.payday_day,
            "pay_rule": row.pay_rule,
            "payday_weekday": row.payday_weekday,
        }

    # ------------------------------------------------------------------
    # read contract
    # ------------------------------------------------------------------

    def get_current_cycle(
        self, user_id: int, today: date, *, currency: str = "ARS"
    ) -> Optional[CurrentCycleView]:
        """Return the open cycle that contains ``today``.

        READ ONLY. No writes. If the cycle has expired, returns None.
        """
        currency = normalize_currency(currency)
        open_cycle = self.cycles.find_open_cycle(user_id, currency=currency)
        if open_cycle is None:
            return None
        window = CycleWindow(
            start_date=open_cycle.start_date,
            end_date=open_cycle.end_date,
            payday_day=open_cycle.payday_day,
            pay_rule=open_cycle.pay_rule,
            payday_weekday=open_cycle.payday_weekday,
        )
        if not window.contains(today):
            return None
        return CurrentCycleView(
            cycle_id=open_cycle.id,
            cycle_index=open_cycle.cycle_index,
            start_date=open_cycle.start_date,
            end_date=open_cycle.end_date,
            payday_day=open_cycle.payday_day,
            status=open_cycle.status,
            currency=open_cycle.currency,
            legacy_mixed_currency=open_cycle.legacy_mixed_currency,
            pay_rule=open_cycle.pay_rule,
            payday_weekday=open_cycle.payday_weekday,
        )

    # ------------------------------------------------------------------
    # open lifecycle
    # ------------------------------------------------------------------

    def open_initial_cycle(
        self, user_id: int, today: date, *, currency: str = "ARS"
    ) -> CurrentCycleView:
        """Open the very first cycle for ``user_id`` if none exists.

        Semantics (R1):

        A. If an open cycle contains ``today``, return it.
        B. If the user has NO cycles at all, compute the window from
           payday_day and insert one (cycle_index = 1).
        C. If the user has history but no current open cycle, raise
           ``CycleRolloverRequired``. R2 will close + reopen.

        Concurrent invocations are serialised by the partial unique
        index on ``status='open'``. The first writer wins; every
        other caller re-reads the canonical row on a fresh session
        so it doesn't depend on the rolled-back transaction's snapshot.
        """
        return self._open_initial_once(user_id, today, currency=normalize_currency(currency))

    def _open_initial_once(
        self, user_id: int, today: date, *, currency: str = "ARS"
    ) -> CurrentCycleView:
        existing = self.get_current_cycle(user_id, today, currency=currency)
        if existing is not None:
            return existing

        # Close any in-flight tx so the next read sees the latest
        # committed state — critical for the concurrent case.
        self._reset_session()

        if self.cycles.has_any_cycle(user_id, currency=currency):
            latest = self.cycles.find_latest_cycle(user_id, currency=currency)
            assert latest is not None
            current = self.get_current_cycle(user_id, today, currency=currency)
            if current is not None:
                return current
            raise CycleRolloverRequired(user_id, latest, today)

        window = cycle_window_for(today, **self._payday_config(user_id, currency=currency, freeze=True))
        opened_at = datetime.now(timezone.utc)

        try:
            row = self.cycles.insert_cycle(
                user_id=user_id,
                cycle_index=1,
                currency=currency,
                start_date=window.start_date,
                end_date=window.end_date,
                payday_day=window.payday_day,
                pay_rule=window.pay_rule,
                payday_weekday=window.payday_weekday,
                opened_at=opened_at,
            )
            self.session.commit()
            self.session.refresh(row)
        except IntegrityError:
            self.session.rollback()
            return self._recover_after_concurrent_open(user_id, today, currency=currency)

        return CurrentCycleView(
            cycle_id=row.id,
            cycle_index=row.cycle_index,
            start_date=row.start_date,
            end_date=row.end_date,
            payday_day=row.payday_day,
            status=row.status,
            currency=row.currency,
            legacy_mixed_currency=row.legacy_mixed_currency,
            pay_rule=row.pay_rule,
            payday_weekday=row.payday_weekday,
        )

    def _reset_session(self) -> None:
        """End any in-flight transaction so subsequent reads get a
        fresh PostgreSQL READ COMMITTED snapshot.
        """
        try:
            if self.session.in_transaction():
                self.session.rollback()
        except Exception:
            pass

    def _recover_after_concurrent_open(
        self, user_id: int, today: date, *, currency: str = "ARS"
    ) -> CurrentCycleView:
        """Re-read after a concurrent writer beat us.

        Uses a brand-new session when one is supplied; otherwise
        relies on the rolled-back transaction's auto-begin on the
        next read (which under READ COMMITTED picks up the winner's
        commit).
        """
        if self._session_factory is not None:
            fresh = self._session_factory()
            try:
                view = CycleService(
                    fresh, session_factory=self._session_factory
                ).get_current_cycle(user_id, today, currency=currency)
            finally:
                fresh.close()
            if view is None:
                raise RuntimeError(
                    "Concurrency recovery failed: no open cycle after "
                    "IntegrityError."
                )
            return view

        current = self.get_current_cycle(user_id, today, currency=currency)
        if current is None:
            raise RuntimeError(
                "Concurrency recovery failed: no open cycle after "
                "IntegrityError."
            )
        return current

    # ------------------------------------------------------------------
    # recovery
    # ------------------------------------------------------------------

    def _safe_get_current_cycle(
        self, user_id: int, today: date, *, currency: str = "ARS"
    ) -> Optional[CurrentCycleView]:
        """``get_current_cycle`` wrapped in a defensive rollback so a
        session that ended up in an aborted state (e.g., an earlier
        IntegrityError in another operation) doesn't poison the read.
        """
        try:
            return self.get_current_cycle(user_id, today, currency=currency)
        except Exception:
            self.session.rollback()
            return self.get_current_cycle(user_id, today, currency=currency)

    def _recover_after_concurrent_open(
        self, user_id: int, today: date, *, currency: str = "ARS"
    ) -> Optional[CurrentCycleView]:
        """Re-read on a fresh session when a concurrent winner beat us.

        PostgreSQL's READ COMMITTED snapshot inside the rolled-back
        transaction does not always surface data committed by the
        winner before the IntegrityError fired. Reading on a brand-new
        connection guarantees we see the canonical row.
        """
        if self._session_factory is None:
            # No factory supplied (typical path from web/telegram):
            # the rolled-back transaction in the same session will
            # auto-begin a new transaction on the next read.
            return self.get_current_cycle(user_id, today, currency=currency)

        fresh = self._session_factory()
        try:
            return CycleService(
                fresh, session_factory=self._session_factory
            ).get_current_cycle(user_id, today, currency=currency)
        finally:
            fresh.close()

    # ------------------------------------------------------------------
    # R2 lifecycle — close, rollover, ensure_current
    # ------------------------------------------------------------------
    #
    # These methods OWN the cycle lifecycle. They take ``SELECT FOR
    # UPDATE`` on the cycle row, delegate the read model to
    # ``MoneyEngine`` (same session, same transaction), build the
    # snapshot via ``build_closing_snapshot`` (pure), persist the
    # snapshot, change ``status`` + ``closed_at``, and commit. The
    # transaction boundary is intentionally here: a failure mid-close
    # rolls back every mutation in one atomic step so the cycle row
    # can never end up "closed but with no snapshot" or "open but with
    # a snapshot stamped on it".

    _CATCH_UP_LIMIT = 12

    def close_cycle(
        self,
        user_id: int,
        cycle_id: int,
        *,
        force: bool = False,
        closed_by: str = "manual",
    ) -> CycleSummary:
        """Close a cycle, snapshot it, and lock the row.

        Single transaction:

        1. ``SELECT … FOR UPDATE`` on the cycle row.
        2. ``MoneyEngine.get_cycle_summary`` returns the read model
           inside the same session — so the cycle stays locked while
           the read model is captured.
        3. If pending obligations remain and ``force`` is False,
           raise :class:`CycleHasPendingError`. The session is still
           usable; the caller decides whether to commit or rollback.
        4. Build the carried-over list via
           :func:`resolve_carry_over_for_close` and serialize the
           snapshot via the pure ``build_closing_snapshot`` helper.
        5. Persist the snapshot, set ``status='closed'``, stamp
           ``closed_at`` (UTC).
        6. Commit.

        If step 3 raises, steps 4-6 never run; the caller rolls back
        and the cycle remains open. If any other step raises, the
        outer ``with session_scope()`` block (or test commit) rolls
        back the whole transaction.
        """
        cycle = self._lock_cycle_for_update(user_id, cycle_id)
        if cycle.status == "closed":
            return MoneyEngine(self.session).get_cycle_summary(
                user_id, cycle_id
            )
        self.session.flush()

        # ``get_cycle_summary`` issues a fresh SELECT on the same row;
        # the cycle's FOR UPDATE lock is owned by this transaction, so
        # the re-fetched entity carries the in-progress snapshot data.
        summary = MoneyEngine(self.session).get_cycle_summary(
            user_id, cycle_id
        )

        if not _pending_is_zero(summary.pending_reserved_obligations):
            if not force:
                raise CycleHasPendingError(
                    _amounts_copy(summary.pending_reserved_obligations)
                )

        previous_carried_over = self._previous_carried_over(cycle)
        carried = resolve_carry_over_for_close(
            self.session,
            user_id,
            cycle,
            previous_carried_over=previous_carried_over,
        )

        closed_at = datetime.now(timezone.utc)
        snapshot = build_closing_snapshot(
            summary,
            carried,
            closed_at=closed_at,
            closed_by=closed_by,
        )
        cycle.closing_snapshot = snapshot
        cycle.status = "closed"
        cycle.closed_at = closed_at

        self.session.commit()
        self.session.refresh(cycle)
        return MoneyEngine(self.session).get_cycle_summary(
            user_id, cycle_id
        )

    def rollover_cycle(
        self, user_id: int, today: date, *, currency: str = "ARS"
    ) -> FinancialCycle:
        """Close any expired open cycle and return the resulting open
        cycle.

        - If the latest open cycle covers ``today`` → no-op return.
        - If it has expired → close with ``force=True``,
          ``closed_by='rollover'`` (pending obligations do NOT block
          the natural rollover) and open the next natural cycle.
        - If the user has no open cycle → defer to
          ``open_initial_cycle`` (the bootstrap path).

        Same transaction boundary as :meth:`close_cycle`. The
        catch-up cap is enforced inside :meth:`ensure_current_cycle`
        which is the public entry point for "walk forward".
        """
        return self._rollover_once(user_id, today, attempt=0, currency=normalize_currency(currency))

    def ensure_current_cycle(
        self, user_id: int, today: date, *, currency: str = "ARS"
    ) -> FinancialCycle:
        """Walk forward through any expired cycles until today is
        covered.

        Repeatedly calls :meth:`rollover_cycle` semantics. Defensive
        cap at ``_CATCH_UP_LIMIT`` (= 12) steps; exceeding the cap
        raises :class:`CycleCatchUpLimitExceeded` so a runaway catch-up
        never spins forever.
        """
        return self._rollover_once(user_id, today, attempt=0, currency=normalize_currency(currency))

    # ------------------------------------------------------------------
    # R2 internals
    # ------------------------------------------------------------------

    def _rollover_once(
        self, user_id: int, today: date, *, attempt: int, currency: str = "ARS"
    ) -> FinancialCycle:
        if attempt >= self._CATCH_UP_LIMIT:
            raise CycleCatchUpLimitExceeded(
                f"More than {self._CATCH_UP_LIMIT} cycles needed to "
                f"catch up to {today.isoformat()}; run again later."
            )

        open_cycle = self._latest_open_cycle(user_id, currency=currency)
        if open_cycle is None:
            latest = self._latest_cycle_any_status(user_id, currency=currency)
            if latest is None:
                view = self.open_initial_cycle(user_id, today, currency=currency)
                return self._require_cycle(user_id, view.cycle_id)
            if latest.status == "open":
                # A concurrent opener may commit between the two SELECTs.
                # Re-read its canonical row instead of treating it as closed.
                return self._rollover_once(user_id, today, attempt=attempt + 1, currency=currency)
            if today <= latest.end_date:
                raise LookupError(
                    "El ciclo actual ya está cerrado; el próximo comienza "
                    f"{(latest.end_date + timedelta(days=1)).isoformat()}."
                )
            # A manual close leaves no open row. The successor remains
            # contiguous, applying the currently configured payday without
            # changing the closed cycle or its frozen snapshot.
            next_day = latest.end_date + timedelta(days=1)
            window = successor_window_for(next_day, **self._payday_config(user_id, currency=currency, freeze=True))
            new_cycle = self._try_insert_open_cycle(
                user_id=user_id,
                cycle_index=self._next_cycle_index(user_id, currency=currency),
                currency=currency,
                start_date=window.start_date,
                end_date=window.end_date,
                payday_day=window.payday_day,
                pay_rule=window.pay_rule,
                payday_weekday=window.payday_weekday,
            )
            if new_cycle is not None:
                self.session.commit()
            else:
                self.session.rollback()
            return self._rollover_once(user_id, today, attempt=attempt + 1, currency=currency)

        if open_cycle.start_date <= today <= open_cycle.end_date:
            return open_cycle
        if today < open_cycle.start_date:
            raise LookupError(
                "El próximo ciclo comienza "
                f"{open_cycle.start_date.isoformat()}; todavía no está activo."
            )

        # Close the expired cycle. Pending is NOT a blocker here; the
        # snapshot captures whatever carries forward.
        self.close_cycle(
            user_id,
            open_cycle.id,
            force=True,
            closed_by="rollover",
        )

        # Same session — no extra commit needed before opening the
        # next cycle. We rely on the partial unique index to catch the
        # concurrent-writer case.
        just_closed = self._latest_cycle_any_status(user_id, currency=currency)
        assert just_closed is not None
        next_day = just_closed.end_date + timedelta(days=1)
        window = successor_window_for(next_day, **self._payday_config(user_id, currency=currency, freeze=True))
        next_cycle_index = self._next_cycle_index(user_id, currency=currency)
        new_cycle = self._try_insert_open_cycle(
            user_id=user_id,
            cycle_index=next_cycle_index,
            currency=currency,
            start_date=window.start_date,
            end_date=window.end_date,
            payday_day=window.payday_day,
            pay_rule=window.pay_rule,
            payday_weekday=window.payday_weekday,
        )
        if new_cycle is not None:
            self.session.commit()
        else:
            # Lost the race; re-read and return the winner.
            self.session.rollback()
            winner = self._latest_open_cycle(user_id, currency=currency)
            if winner is not None:
                return winner

        # Continue walking: this is also the catch-up path. The
        # defensive cap at ``_CATCH_UP_LIMIT`` keeps the loop bounded
        # in case of a clock-drift or payday-misconfiguration bug.
        return self._rollover_once(user_id, today, attempt=attempt + 1, currency=currency)

    def _lock_cycle_for_update(
        self, user_id: int, cycle_id: int
    ) -> FinancialCycle:
        stmt = (
            select(FinancialCycle)
            .where(
                FinancialCycle.telegram_user_id == user_id,
                FinancialCycle.id == cycle_id,
            )
            .with_for_update(of=FinancialCycle)
            .execution_options(populate_existing=True)
        )
        cycle = self.session.execute(stmt).scalar_one_or_none()
        if cycle is None:
            raise LookupError(
                f"Cycle {cycle_id} not found for user {user_id}"
            )
        return cycle

    def _require_cycle(
        self, user_id: int, cycle_id: int
    ) -> FinancialCycle:
        cycle = self.session.get(FinancialCycle, cycle_id)
        if cycle is None or cycle.telegram_user_id != user_id:
            raise LookupError(
                f"Cycle {cycle_id} not found for user {user_id}"
            )
        return cycle

    def _latest_open_cycle(
        self, user_id: int, *, currency: str = "ARS"
    ) -> Optional[FinancialCycle]:
        stmt = (
            select(FinancialCycle)
            .where(
                FinancialCycle.telegram_user_id == user_id,
                FinancialCycle.currency == normalize_currency(currency),
                FinancialCycle.status == "open",
            )
            .order_by(FinancialCycle.cycle_index.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def _latest_cycle_any_status(
        self, user_id: int, *, currency: str = "ARS"
    ) -> Optional[FinancialCycle]:
        stmt = (
            select(FinancialCycle)
            .where(FinancialCycle.telegram_user_id == user_id, cycle_currency_scope(currency))
            .order_by(FinancialCycle.cycle_index.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def _next_cycle_index(self, user_id: int, *, currency: str = "ARS") -> int:
        stmt = select(
            func.coalesce(func.max(FinancialCycle.cycle_index), 0)
        ).where(FinancialCycle.telegram_user_id == user_id, cycle_currency_scope(currency))
        return int(self.session.execute(stmt).scalar_one() or 0) + 1

    def _try_insert_open_cycle(
        self,
        *,
        user_id: int,
        cycle_index: int,
        start_date: date,
        end_date: date,
        payday_day: int,
        pay_rule: str = "fixed_day",
        payday_weekday: Optional[int] = None,
        currency: str = "ARS",
    ) -> Optional[FinancialCycle]:
        obj = FinancialCycle(
            telegram_user_id=user_id,
            currency=normalize_currency(currency),
            cycle_index=cycle_index,
            start_date=start_date,
            end_date=end_date,
            payday_day=payday_day,
            pay_rule=pay_rule,
            payday_weekday=payday_weekday,
            status="open",
            opened_at=datetime.now(timezone.utc),
        )
        self.session.add(obj)
        try:
            self.session.flush()
        except IntegrityError:
            self.session.rollback()
            return None
        return obj

    def _previous_carried_over(
        self, cycle: FinancialCycle
    ):
        """Pull the previous closed cycle's carried-over list."""
        stmt = (
            select(FinancialCycle)
            .where(
                FinancialCycle.telegram_user_id == cycle.telegram_user_id,
                cycle_currency_scope(cycle.currency),
                FinancialCycle.cycle_index < cycle.cycle_index,
            )
            .order_by(FinancialCycle.cycle_index.desc())
            .limit(1)
        )
        prev = self.session.execute(stmt).scalar_one_or_none()
        if prev is None or prev.closing_snapshot is None:
            return ()
        from app.money.snapshot import (
            deserialize_carried_over,
            ensure_snapshot_version,
        )

        ensure_snapshot_version(prev.closing_snapshot)
        return tuple(occ for occ in deserialize_carried_over(
            prev.closing_snapshot.get("carried_over_fixed", [])
        ) if occ.currency.upper() == cycle.currency)


def _pending_is_zero(mbc) -> bool:
    """``True`` when ``mbc`` has no currency with a non-zero amount."""
    return all(
        mbc.get(currency) == Decimal("0")
        for currency in mbc.currencies()
    )


def _amounts_copy(mbc) -> dict[str, Decimal]:
    """Copy the underlying amounts dict so callers can keep it after
    the summary is garbage-collected (the MoneyByCurrency class keeps
    its data in a private attribute that is otherwise inaccessible
    from outside the module).
    """
    return {cur: Decimal(mbc.get(cur)) for cur in mbc.currencies()}
