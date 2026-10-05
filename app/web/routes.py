"""FastAPI router exposing the dashboard UI and JSON endpoints.

All endpoints scope data to the single configured ``telegram_allowed_user_id``
since the bot is single-user. Bind to ``127.0.0.1`` only (see docker-compose).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config.settings import Settings, get_settings
from app.database.database import session_scope
from app.money import MoneyEngine
from app.utils.dates import today_in_tz
from app.web.queries import (
    budget_status,
    comparison,
    daily_trend,
    fixed_expenses_current_month,
    month_summary_dashboard,
    period_count,
    period_total_by_currency,
    projection,
    recent_expenses,
    recurring_upcoming,
    resolve_period,
    resolve_month,
    summary_for_period,
)

router = APIRouter()


def _current_user_id(settings: Settings = Depends(get_settings)) -> int:
    return settings.telegram_allowed_user_id


def _today(settings: Settings = Depends(get_settings)) -> date:
    return today_in_tz(settings.timezone)


@router.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/dashboard")


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard_html() -> str:
    return DASHBOARD_HTML


@router.get("/dashboard/mensual", response_class=HTMLResponse, include_in_schema=False)
async def monthly_dashboard_html() -> str:
    return DASHBOARD_HTML


@router.get("/api/summary")
async def api_summary(
    period: str = Query("month"),
    custom_start: Optional[date] = None,
    custom_end: Optional[date] = None,
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    window = resolve_period(
        period, today, custom_start=custom_start, custom_end=custom_end
    )
    with session_scope() as s:
        return summary_for_period(s, user_id, window)


@router.get("/api/monthly-summary")
async def api_monthly_summary(
    month: Optional[str] = Query(None),
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    try:
        window = resolve_month(month, today)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    previous_start = window.start.replace(day=1)
    if previous_start.month == 1:
        previous_key = f"{previous_start.year - 1}-12"
    else:
        previous_key = f"{previous_start.year}-{previous_start.month - 1:02d}"
    previous = resolve_month(previous_key, today)

    with session_scope() as s:
        summary = summary_for_period(s, user_id, window)
        trend = daily_trend(
            s,
            user_id,
            end=window.end,
            days=window.days_in_period,
        )
        comparison_result = comparison(s, user_id, window, previous)
        budget_rows = budget_status(s, user_id, window)
        items = recent_expenses(
            s,
            user_id,
            500,
            start=window.start,
            end=window.end,
        )
        total_by_currency = period_total_by_currency(
            s, user_id, start=window.start, end=window.end
        )
        count = period_count(s, user_id, start=window.start, end=window.end)

    single_total = (
        next(iter(total_by_currency.values()))
        if len(total_by_currency) == 1
        else None
    )
    return {
        "month": window.start.strftime("%Y-%m"),
        "current_month": today.strftime("%Y-%m"),
        "summary": summary,
        "trend": trend,
        "comparison": comparison_result,
        "budgets": {"items": budget_rows},
        "expenses": {
            "period_label": window.label,
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "count": count,
            "total": (
                float(single_total.quantize(Decimal("0.01")))
                if single_total is not None
                else None
            ),
            "total_by_currency": {
                key: float(value.quantize(Decimal("0.01")))
                for key, value in total_by_currency.items()
            },
            "items": items,
        },
    }


@router.get("/api/trend")
async def api_trend(
    period: str = Query("month"),
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    window = resolve_period(period, today)
    period_to_days = {
        "today": 1,
        "week": 7,
        "month": 30,
        "last_month": 30,
        "year": 365,
    }
    days = period_to_days.get(period, 30)
    with session_scope() as s:
        return daily_trend(s, user_id, end=window.end, days=days)


@router.get("/api/recent")
async def api_recent(
    limit: int = Query(10, ge=1, le=100),
    user_id: int = Depends(_current_user_id),
):
    with session_scope() as s:
        return {"items": recent_expenses(s, user_id, limit)}


@router.get("/api/expenses")
async def api_expenses(
    period: str = Query("month"),
    custom_start: Optional[date] = None,
    custom_end: Optional[date] = None,
    category: Optional[str] = Query(None, max_length=64),
    limit: int = Query(500, ge=1, le=2000),
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    """Return expenses for the period (default: current month).

    The ``items`` list is bounded by ``limit`` for transport size, but
    ``count`` and ``total`` describe the FULL period so the dashboard
    total is independent of how many rows the client fetched.

    Currency correctness: the legacy scalar ``total`` is only emitted
    when a single currency is present. With multiple currencies it's
    ``null`` and ``total_by_currency`` carries the per-currency map.
    """
    window = resolve_period(
        period, today, custom_start=custom_start, custom_end=custom_end
    )
    with session_scope() as s:
        items = recent_expenses(
            s,
            user_id,
            limit,
            start=window.start,
            end=window.end,
            category=category,
        )
        total_by_currency = period_total_by_currency(
            s,
            user_id,
            start=window.start,
            end=window.end,
            category=category,
        )
        count = period_count(
            s,
            user_id,
            start=window.start,
            end=window.end,
            category=category,
        )
    single_total = (
        next(iter(total_by_currency.values()))
        if len(total_by_currency) == 1
        else None
    )
    return {
        "period_label": window.label,
        "start": window.start.isoformat(),
        "end": window.end.isoformat(),
        "count": count,
        "total": (
            float(single_total.quantize(Decimal("0.01")))
            if single_total is not None
            else None
        ),
        "total_by_currency": {
            k: float(v.quantize(Decimal("0.01")))
            for k, v in total_by_currency.items()
        },
        "items": items,
    }


@router.get("/api/budgets")
async def api_budgets(
    period: str = Query("month"),
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    window = resolve_period(period, today)
    with session_scope() as s:
        return {"items": budget_status(s, user_id, window)}


@router.get("/api/comparison")
async def api_comparison(
    period: str = Query("month"),
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    prev_period = {
        "today": "yesterday",
        "week": "last_week",
        "month": "last_month",
        "year": "last_year",
    }.get(period, "last_month")
    current = resolve_period(period, today)
    previous = resolve_period(prev_period, today)
    with session_scope() as s:
        return comparison(s, user_id, current, previous)


@router.get("/api/projection")
async def api_projection(
    period: str = Query("month"),
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    if period == "today":
        return {"applies": False, "reason": "today"}
    prev_period = {
        "week": "last_week",
        "month": "last_month",
        "year": "last_year",
    }.get(period, "last_month")
    current = resolve_period(period, today)
    previous = resolve_period(prev_period, today)
    with session_scope() as s:
        result = projection(s, user_id, current, previous)
    result["applies"] = True
    return result


@router.get("/api/recurring/upcoming")
async def api_recurring_upcoming(
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    with session_scope() as s:
        return {"items": recurring_upcoming(s, user_id, today)}


@router.get("/api/fixed-expenses")
async def api_fixed_expenses(
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    with session_scope() as s:
        bills = fixed_expenses_current_month(s, user_id, today)
    return {"items": bills}


@router.post("/api/fixed-expenses")
async def api_create_fixed_expense(
    payload: dict,
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    """Create a new fixed expense from the dashboard."""
    from app.fixed_expenses.service import (
        FixedExpenseDraft,
        FixedExpenseService,
        FixedExpenseValidationError,
        current_month_year,
    )
    from app.fixed_expenses.repository import FixedExpenseRepository

    try:
        amount = Decimal(str(payload.get("expected_amount", "0")))
    except (InvalidOperation, TypeError):
        raise HTTPException(status_code=400, detail="Monto inválido.")
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Falta el nombre.")
    method = (payload.get("payment_method") or "").strip() or None
    try:
        due_day = int(payload.get("due_day_of_month", 1))
    except (ValueError, TypeError):
        due_day = 1
    currency = (payload.get("currency") or "ARS").upper()
    draft = FixedExpenseDraft(
        name=name,
        expected_amount=amount,
        currency=currency,
        payment_method=method,
        due_day_of_month=due_day,
        category=payload.get("category"),
    )
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        service = FixedExpenseService(repo)
        try:
            obj = service.add(user_id, draft)
        except FixedExpenseValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return {
        "id": obj.id,
        "name": obj.name,
        "expected_amount": float(obj.expected_amount),
        "currency": obj.currency,
        "payment_method": obj.payment_method,
        "due_day_of_month": obj.due_day_of_month,
    }


@router.post("/api/fixed-expenses/{fixed_id}/pay")
async def api_mark_paid(
    fixed_id: int,
    payload: dict,
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    """Mark a fixed expense as paid for the current month."""
    from app.fixed_expenses.service import (
        FixedExpenseService,
        current_month_year,
    )
    from app.fixed_expenses.repository import FixedExpenseRepository

    actual = payload.get("actual_amount")
    amount_dec = None
    if actual is not None and actual != "":
        try:
            amount_dec = Decimal(str(actual))
            if amount_dec <= 0:
                raise ValueError
        except (InvalidOperation, ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Monto real inválido.")
    my = current_month_year(today)
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        service = FixedExpenseService(repo)
        obj, _ = service.mark_paid(user_id, fixed_id, my, actual_amount=amount_dec)
        if obj is None:
            raise HTTPException(status_code=404, detail="Gasto fijo no encontrado.")
    return {"ok": True, "id": fixed_id, "month_year": my}


@router.post("/api/fixed-expenses/{fixed_id}/unpay")
async def api_unmark_paid(
    fixed_id: int,
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    from app.fixed_expenses.service import (
        FixedExpenseService,
        current_month_year,
    )
    from app.fixed_expenses.repository import FixedExpenseRepository

    my = current_month_year(today)
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        service = FixedExpenseService(repo)
        ok = service.unmark(user_id, fixed_id, my)
        if not ok:
            raise HTTPException(
                status_code=404, detail="Gasto fijo o pago no encontrado."
            )
    return {"ok": True, "id": fixed_id}


@router.post("/api/fixed-expenses/{fixed_id}/skip")
async def api_skip(
    fixed_id: int,
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    from app.fixed_expenses.service import (
        FixedExpenseService,
        current_month_year,
    )
    from app.fixed_expenses.repository import FixedExpenseRepository

    my = current_month_year(today)
    with session_scope() as s:
        repo = FixedExpenseRepository(s)
        service = FixedExpenseService(repo)
        obj, _ = service.mark_skipped(user_id, fixed_id, my)
        if obj is None:
            raise HTTPException(status_code=404, detail="Gasto fijo no encontrado.")
    return {"ok": True, "id": fixed_id}


@router.get("/api/month-summary")
async def api_month_summary(
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    with session_scope() as s:
        return month_summary_dashboard(s, user_id, today)


@router.get("/api/cycle/current")
async def api_cycle_current(
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    """Return the open financial cycle that contains today, or null."""
    from app.money import CycleService

    with session_scope() as s:
        view = CycleService(s).get_current_cycle(user_id, today)
    if view is None:
        return {"cycle": None}
    return {
        "cycle": {
            "id": view.cycle_id,
            "cycle_index": view.cycle_index,
            "start_date": view.start_date.isoformat(),
            "end_date": view.end_date.isoformat(),
            "payday_day": view.payday_day,
            "status": view.status,
        }
    }


@router.post("/api/cycle/open")
async def api_cycle_open(
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    """Open the initial cycle for the user.

    Returns the cycle view on success, raises 409 when the user already
    has history that requires rollover (R2 territory).
    """
    from app.money import CycleRolloverRequired, CycleService

    try:
        with session_scope() as s:
            view = CycleService(s).open_initial_cycle(user_id, today)
    except CycleRolloverRequired as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "cycle_rollover_required",
                "message": (
                    "El usuario ya tiene un ciclo cerrado. R2 implementa"
                    " el rollover."
                ),
                "latest_cycle_index": exc.latest_cycle_index,
            },
        ) from exc
    return {
        "cycle": {
            "id": view.cycle_id,
            "cycle_index": view.cycle_index,
            "start_date": view.start_date.isoformat(),
            "end_date": view.end_date.isoformat(),
            "payday_day": view.payday_day,
            "status": view.status,
        }
    }


# ---------------------------------------------------------------------------
# R2 — cycle-summary read + lifecycle endpoints (close / ensure)
# ---------------------------------------------------------------------------


def _summary_to_payload(
    summary, *, with_cycle: bool = False
) -> dict:
    """Render a ``CycleSummary`` as JSON-safe primitives.

    Monetary fields are emitted as ``dict[str, str]`` (Decimal strings)
    so JSON consumers can re-parse them losslessly. ``daily_safe_to_spend``
    is ``None`` when the cycle is closed or ``remaining_days <= 0``.
    """
    payload = {
        "cycle_id": summary.cycle_id,
        "cycle_index": summary.cycle_index,
        "status": summary.status,
        "start_date": summary.start_date.isoformat(),
        "end_date": summary.end_date.isoformat(),
        "payday_day": summary.payday_day,
        "snapshot_version": summary.snapshot_version,
        "money": {
            "income": summary.base_income.to_dict(),
            "extra": summary.extra_income.to_dict(),
            "gross_income": summary.gross_income.to_dict(),
            "savings_target": summary.savings_target.to_dict(),
            "savings_reserved": summary.savings_reserved.to_dict(),
            "savings_realized": summary.savings_realized.to_dict(),
            "savings_committed": summary.savings_committed.to_dict(),
            "realized_expenses": summary.realized_expenses.to_dict(),
            "pending_fixed_obligations": (
                summary.pending_fixed_obligations.to_dict()
            ),
            "pending_recurring_obligations": (
                summary.pending_recurring_obligations.to_dict()
            ),
            "pending_reserved_obligations": (
                summary.pending_reserved_obligations.to_dict()
            ),
            "available_to_spend": summary.available_to_spend.to_dict(),
            "daily_safe_to_spend": (
                summary.daily_safe_to_spend.to_dict()
                if summary.daily_safe_to_spend is not None
                else None
            ),
            "warnings": list(summary.warnings),
        },
        "remaining_days": summary.remaining_days,
        "carried_over_fixed": [
            {
                "fixed_id": occ.fixed_id,
                "name": occ.name,
                "expected_amount": str(occ.expected_amount),
                "currency": occ.currency,
                "due_date": occ.due_date.isoformat(),
                "original_month_year": occ.original_month_year,
            }
            for occ in summary.carried_over_fixed
        ],
    }
    if with_cycle:
        payload["cycle"] = {
            "id": summary.cycle_id,
            "cycle_index": summary.cycle_index,
            "start_date": summary.start_date.isoformat(),
            "end_date": summary.end_date.isoformat(),
            "payday_day": summary.payday_day,
            "status": summary.status,
        }
    return payload


@router.get("/api/cycle-summary")
async def api_cycle_summary(
    user_id: int = Depends(_current_user_id),
    cycle_id: int | None = Query(default=None),
    today: date = Depends(_today),
):
    """Return the full R2 cycle summary for the current (or given) cycle.

    When ``cycle_id`` is omitted the cycle service walks the rollover
    chain if the open cycle is expired; the read model itself comes
    from ``MoneyEngine`` inside the same session.
    """
    from app.money import (
        CycleCatchUpLimitExceeded,
        CycleService,
        MoneyEngine,
    )

    with session_scope() as s:
        cycle_service = CycleService(s)
        engine = MoneyEngine(s)
        if cycle_id is None:
            try:
                cycle = cycle_service.ensure_current_cycle(user_id, today)
            except CycleCatchUpLimitExceeded as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "cycle_catch_up_limit_exceeded",
                        "message": str(exc),
                    },
                ) from exc
            cycle_id = cycle.id
        summary = engine.get_cycle_summary(user_id, cycle_id, today=today)
        return _summary_to_payload(summary)


@router.post("/api/cycle/{cycle_id}/close")
async def api_cycle_close(
    cycle_id: int,
    user_id: int = Depends(_current_user_id),
    force: bool = Query(default=False),
):
    """Close a cycle. ``force=true`` allows closing with pending obligations."""
    from app.money import (
        CycleHasPendingError,
        CycleService,
        MoneyEngine,
    )

    with session_scope() as s:
        cycle_service = CycleService(s)
        engine = MoneyEngine(s)
        try:
            summary = cycle_service.close_cycle(
                user_id, cycle_id, force=force, closed_by="manual"
            )
        except CycleHasPendingError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "cycle_has_pending_obligations",
                    "pending_by_currency": {
                        k: str(v)
                        for k, v in exc.pending_by_currency.items()
                    },
                },
            ) from exc
        return _summary_to_payload(summary)


@router.post("/api/cycle/ensure")
async def api_cycle_ensure(
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    """Run ``ensure_current_cycle(today)``. Idempotent."""
    from app.money import (
        CycleCatchUpLimitExceeded,
        CycleService,
    )

    with session_scope() as s:
        cycle_service = CycleService(s)
        try:
            cycle = cycle_service.ensure_current_cycle(user_id, today)
        except CycleCatchUpLimitExceeded as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "cycle_catch_up_limit_exceeded",
                    "message": str(exc),
                },
            ) from exc
    return {
        "cycle": {
            "id": cycle.id,
            "cycle_index": cycle.cycle_index,
            "start_date": cycle.start_date.isoformat(),
            "end_date": cycle.end_date.isoformat(),
            "payday_day": cycle.payday_day,
            "status": cycle.status,
        }
    }


# ---------------------------------------------------------------------------
# R3 — income / savings mutations
# ---------------------------------------------------------------------------


def _resolve_open_cycle(
    user_id: int, cycle_id: int | None, today: date
) -> FinancialCycle:
    """Return the open cycle a mutation targets.

    When ``cycle_id`` is omitted, ``CycleService.ensure_current_cycle``
    walks the rollover chain and returns the open cycle for ``today``.
    """
    from app.money import (
        CycleCatchUpLimitExceeded,
        CycleService,
        FinancialCycle,
        MoneyStateError,
    )

    with session_scope() as s:
        cycle_service = CycleService(s)
        if cycle_id is None:
            try:
                return cycle_service.ensure_current_cycle(user_id, today)
            except CycleCatchUpLimitExceeded as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "cycle_catch_up_limit_exceeded",
                        "message": str(exc),
                    },
                ) from exc
        cycle = s.get(FinancialCycle, cycle_id)
        if cycle is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "cycle_not_found",
                    "message": f"Cycle {cycle_id} not found",
                },
            )
        if cycle.telegram_user_id != user_id:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "cycle_not_found",
                    "message": f"Cycle {cycle_id} not found",
                },
            )
        if cycle.status != "open":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "cycle_closed",
                    "message": (
                        f"Cycle {cycle_id} is closed; historical state "
                        "is immutable"
                    ),
                },
            )
        return cycle


def _parse_amount(raw) -> Decimal:
    """Parse a request body amount. JSON gives us a string already; the
    service does its own coercion but we want a clean 400 here.
    """
    if raw is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_amount", "message": "amount required"},
        )
    try:
        if isinstance(raw, Decimal):
            return raw
        return Decimal(str(raw))
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_amount",
                "message": f"amount must be a Decimal-compatible value: {exc}",
            },
        ) from exc


def _state_error_to_http(exc: Exception) -> HTTPException:
    from app.money import (
        CycleClosedError,
        CycleNotFoundError,
        CycleOwnershipMismatchError,
        ExtraIncomeNotFoundError,
        InvalidAmountError,
        InvalidSourceKeyError,
        InvalidTargetValueError,
        MoneySourceCurrencyConflictError,
        SavingsRealizationNotFoundError,
        TargetXorViolationError,
    )

    if isinstance(exc, CycleClosedError):
        return HTTPException(
            status_code=409,
            detail={"code": "cycle_closed", "message": str(exc)},
        )
    if isinstance(exc, MoneySourceCurrencyConflictError):
        return HTTPException(
            status_code=409,
            detail={
                "code": "source_currency_conflict",
                "message": "La moneda de esta operación no se puede cambiar.",
            },
        )
    if isinstance(exc, (CycleNotFoundError, CycleOwnershipMismatchError)):
        return HTTPException(
            status_code=404,
            detail={"code": "cycle_not_found", "message": str(exc)},
        )
    if isinstance(exc, (ExtraIncomeNotFoundError, SavingsRealizationNotFoundError)):
        return HTTPException(
            status_code=404,
            detail={"code": "entity_not_found", "message": str(exc)},
        )
    if isinstance(exc, (InvalidAmountError, InvalidSourceKeyError,
                         InvalidTargetValueError, TargetXorViolationError)):
        return HTTPException(
            status_code=400,
            detail={"code": "invalid_argument", "message": str(exc)},
        )
    raise HTTPException(
        status_code=500,
        detail={"code": "internal_error", "message": str(exc)},
    )


def _income_to_payload(row) -> dict:
    return {
        "id": row.id,
        "cycle_id": row.cycle_id,
        "currency": row.currency,
        "kind": row.kind,
        "amount": str(row.amount),
        "source_key": row.source_key,
        "note": row.note,
        "revision": row.revision,
        "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _target_to_payload(row) -> dict:
    return {
        "cycle_id": row.cycle_id,
        "currency": row.currency,
        "target_pct": str(row.target_pct) if row.target_pct is not None else None,
        "target_amount": str(row.target_amount) if row.target_amount is not None else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _reserved_to_payload(row) -> dict:
    return {
        "cycle_id": row.cycle_id,
        "currency": row.currency,
        "amount": str(row.amount),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _realization_to_payload(row) -> dict:
    return {
        "id": row.id,
        "cycle_id": row.cycle_id,
        "currency": row.currency,
        "amount": str(row.amount),
        "source_key": row.source_key,
        "note": row.note,
        "revision": row.revision,
        "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.post("/api/cycles/income")
async def api_cycle_set_income(
    payload: dict,
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    """Upsert the base income for the open cycle + currency."""
    from app.money import MoneyStateService

    cycle_id = payload.get("cycle_id")
    cycle = _resolve_open_cycle(
        user_id,
        int(cycle_id) if cycle_id is not None else None,
        today,
    )
    amount = _parse_amount(payload.get("amount"))
    currency = str(payload.get("currency") or "ARS")
    try:
        with session_scope() as s:
            row = MoneyStateService(s).set_income(
                user_id=user_id,
                cycle_id=cycle.id,
                currency=currency,
                amount=amount,
            )
            summary = MoneyEngine(s).get_cycle_summary(user_id, cycle.id, today=today)
            return {
                "income": _income_to_payload(row),
                "summary": _summary_to_payload(summary),
            }
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, HTTPException):
            raise
        raise _state_error_to_http(exc) from exc


@router.post("/api/cycles/extra")
async def api_cycle_record_extra(
    payload: dict,
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    """Upsert an extra income identified by ``source_key``."""
    from app.money import MoneyStateService

    cycle_id = payload.get("cycle_id")
    cycle = _resolve_open_cycle(
        user_id,
        int(cycle_id) if cycle_id is not None else None,
        today,
    )
    amount = _parse_amount(payload.get("amount"))
    currency = str(payload.get("currency") or "ARS")
    source_key = payload.get("source_key")
    note = payload.get("note")
    try:
        with session_scope() as s:
            row = MoneyStateService(s).record_extra_income(
                user_id=user_id,
                cycle_id=cycle.id,
                currency=currency,
                amount=amount,
                source_key=str(source_key) if source_key is not None else "",
                note=note,
            )
            summary = MoneyEngine(s).get_cycle_summary(user_id, cycle.id, today=today)
            return {
                "extra": _income_to_payload(row),
                "summary": _summary_to_payload(summary),
            }
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, HTTPException):
            raise
        raise _state_error_to_http(exc) from exc


@router.patch("/api/cycles/extra/{extra_id}")
async def api_cycle_update_extra(
    extra_id: int,
    payload: dict,
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    from app.money import MoneyStateService

    amount = payload.get("amount")
    amount_decimal = _parse_amount(amount) if amount is not None else None
    note = payload.get("note")  # None means "leave as-is", sentinel handled below
    try:
        with session_scope() as s:
            row = MoneyStateService(s).update_extra_income(
                user_id=user_id,
                extra_id=extra_id,
                amount=amount_decimal,
                note=note,
            )
            summary = MoneyEngine(s).get_cycle_summary(
                user_id, row.cycle_id, today=today
            )
            return {
                "extra": _income_to_payload(row),
                "summary": _summary_to_payload(summary),
            }
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, HTTPException):
            raise
        raise _state_error_to_http(exc) from exc


@router.delete("/api/cycles/extra/{extra_id}")
async def api_cycle_soft_delete_extra(
    extra_id: int,
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    from app.money import MoneyStateService

    try:
        with session_scope() as s:
            row = MoneyStateService(s).soft_delete_extra_income(
                user_id=user_id, extra_id=extra_id
            )
            summary = MoneyEngine(s).get_cycle_summary(
                user_id, row.cycle_id, today=today
            )
            return {
                "extra": _income_to_payload(row),
                "summary": _summary_to_payload(summary),
            }
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, HTTPException):
            raise
        raise _state_error_to_http(exc) from exc


@router.post("/api/cycles/savings/target")
async def api_cycle_set_savings_target(
    payload: dict,
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    from app.money import MoneyStateService

    cycle_id = payload.get("cycle_id")
    cycle = _resolve_open_cycle(
        user_id,
        int(cycle_id) if cycle_id is not None else None,
        today,
    )
    currency = str(payload.get("currency") or "ARS")
    target_pct = payload.get("target_pct")
    target_amount = payload.get("target_amount")
    pct_decimal = _parse_amount(target_pct) if target_pct is not None else None
    amt_decimal = _parse_amount(target_amount) if target_amount is not None else None
    try:
        with session_scope() as s:
            row = MoneyStateService(s).set_savings_target(
                user_id=user_id,
                cycle_id=cycle.id,
                currency=currency,
                target_pct=pct_decimal,
                target_amount=amt_decimal,
            )
            summary = MoneyEngine(s).get_cycle_summary(user_id, cycle.id, today=today)
            return {
                "target": _target_to_payload(row),
                "summary": _summary_to_payload(summary),
            }
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, HTTPException):
            raise
        raise _state_error_to_http(exc) from exc


@router.post("/api/cycles/savings/reserved")
async def api_cycle_set_savings_reserved(
    payload: dict,
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    from app.money import MoneyStateService

    cycle_id = payload.get("cycle_id")
    cycle = _resolve_open_cycle(
        user_id,
        int(cycle_id) if cycle_id is not None else None,
        today,
    )
    amount = _parse_amount(payload.get("amount"))
    currency = str(payload.get("currency") or "ARS")
    try:
        with session_scope() as s:
            row = MoneyStateService(s).set_savings_reserved(
                user_id=user_id,
                cycle_id=cycle.id,
                currency=currency,
                amount=amount,
            )
            summary = MoneyEngine(s).get_cycle_summary(user_id, cycle.id, today=today)
            return {
                "reserved": _reserved_to_payload(row),
                "summary": _summary_to_payload(summary),
            }
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, HTTPException):
            raise
        raise _state_error_to_http(exc) from exc


@router.post("/api/cycles/savings/release")
async def api_cycle_release_savings(
    payload: dict,
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    from app.money import MoneyStateService

    cycle_id = payload.get("cycle_id")
    cycle = _resolve_open_cycle(
        user_id,
        int(cycle_id) if cycle_id is not None else None,
        today,
    )
    amount = _parse_amount(payload.get("amount"))
    currency = str(payload.get("currency") or "ARS")
    try:
        with session_scope() as s:
            row = MoneyStateService(s).release_savings(
                user_id=user_id,
                cycle_id=cycle.id,
                currency=currency,
                amount=amount,
            )
            summary = MoneyEngine(s).get_cycle_summary(user_id, cycle.id, today=today)
            return {
                "reserved": _reserved_to_payload(row),
                "summary": _summary_to_payload(summary),
            }
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, HTTPException):
            raise
        raise _state_error_to_http(exc) from exc


@router.post("/api/cycles/savings/realized")
async def api_cycle_record_savings_realized(
    payload: dict,
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    from app.money import MoneyStateService

    cycle_id = payload.get("cycle_id")
    cycle = _resolve_open_cycle(
        user_id,
        int(cycle_id) if cycle_id is not None else None,
        today,
    )
    amount = _parse_amount(payload.get("amount"))
    currency = str(payload.get("currency") or "ARS")
    source_key = payload.get("source_key")
    note = payload.get("note")
    try:
        with session_scope() as s:
            row = MoneyStateService(s).record_savings_realized(
                user_id=user_id,
                cycle_id=cycle.id,
                currency=currency,
                amount=amount,
                source_key=str(source_key) if source_key is not None else "",
                note=note,
            )
            summary = MoneyEngine(s).get_cycle_summary(user_id, cycle.id, today=today)
            return {
                "realization": _realization_to_payload(row),
                "summary": _summary_to_payload(summary),
            }
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, HTTPException):
            raise
        raise _state_error_to_http(exc) from exc


@router.patch("/api/cycles/savings/realized/{realization_id}")
async def api_cycle_update_savings_realized(
    realization_id: int,
    payload: dict,
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    from app.money import MoneyStateService

    amount = payload.get("amount")
    amount_decimal = _parse_amount(amount) if amount is not None else None
    note = payload.get("note")
    try:
        with session_scope() as s:
            row = MoneyStateService(s).update_savings_realized(
                user_id=user_id,
                realization_id=realization_id,
                amount=amount_decimal,
                note=note,
            )
            summary = MoneyEngine(s).get_cycle_summary(
                user_id, row.cycle_id, today=today
            )
            return {
                "realization": _realization_to_payload(row),
                "summary": _summary_to_payload(summary),
            }
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, HTTPException):
            raise
        raise _state_error_to_http(exc) from exc


@router.delete("/api/cycles/savings/realized/{realization_id}")
async def api_cycle_soft_delete_savings_realized(
    realization_id: int,
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    from app.money import MoneyStateService

    try:
        with session_scope() as s:
            row = MoneyStateService(s).soft_delete_savings_realized(
                user_id=user_id, realization_id=realization_id
            )
            summary = MoneyEngine(s).get_cycle_summary(
                user_id, row.cycle_id, today=today
            )
            return {
                "realization": _realization_to_payload(row),
                "summary": _summary_to_payload(summary),
            }
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, HTTPException):
            raise
        raise _state_error_to_http(exc) from exc


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tus gastos</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
  <style>
    /* ===========================================================
       Base
       =========================================================== */
    :root {
      --bg-1: #0a0f1e;
      --bg-2: #0f172a;
      --text: #f1f5f9;
      --muted: #94a3b8;
      --accent: #34d399;
      --accent-2: #60a5fa;
      --warn: #fbbf24;
      --bad: #f87171;
      --glass: rgba(255, 255, 255, 0.04);
      --glass-border: rgba(255, 255, 255, 0.09);
      --glass-shadow: 0 8px 32px rgba(0, 0, 0, 0.35);
      --glass-shadow-hover: 0 16px 48px rgba(0, 0, 0, 0.5);
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body { min-height: 100vh; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display",
                   "SF Pro Text", "Segoe UI", Roboto, system-ui, sans-serif;
      color: var(--text);
      background:
        radial-gradient(ellipse at 0% 0%, rgba(52, 211, 153, 0.08) 0%, transparent 50%),
        radial-gradient(ellipse at 100% 100%, rgba(96, 165, 250, 0.08) 0%, transparent 50%),
        linear-gradient(180deg, var(--bg-1) 0%, var(--bg-2) 100%);
      background-attachment: fixed;
      padding: 32px 40px 64px;
      overflow-x: hidden;
      -webkit-font-smoothing: antialiased;
      letter-spacing: -0.011em;
    }

    /* Floating gradient orbs in the background */
    body::before, body::after {
      content: '';
      position: fixed;
      border-radius: 50%;
      filter: blur(120px);
      pointer-events: none;
      z-index: 0;
      opacity: 0.5;
    }
    body::before {
      top: -10%;
      left: -5%;
      width: 480px; height: 480px;
      background: radial-gradient(circle, #34d399 0%, transparent 70%);
      animation: orbDrift 22s ease-in-out infinite;
    }
    body::after {
      bottom: -15%;
      right: -8%;
      width: 540px; height: 540px;
      background: radial-gradient(circle, #60a5fa 0%, transparent 70%);
      animation: orbDrift 26s ease-in-out infinite reverse;
    }
    @keyframes orbDrift {
      0%, 100% { transform: translate(0, 0) scale(1); }
      33%      { transform: translate(40%, 30%) scale(1.05); }
      66%      { transform: translate(-30%, 40%) scale(0.95); }
    }

    main { position: relative; z-index: 1; max-width: 1400px; margin: 0 auto; }

    /* ===========================================================
       Header
       =========================================================== */
    header { margin-bottom: 32px; }
    h1 {
      font-size: 32px;
      font-weight: 700;
      letter-spacing: -0.025em;
      margin-bottom: 6px;
      line-height: 1.15;
    }
    h1 .emoji { filter: saturate(110%); }
    .subtitle {
      color: var(--muted);
      font-size: 14px;
      font-weight: 400;
      letter-spacing: 0.005em;
      transition: opacity 0.3s ease;
    }
    .subtitle.refreshing { opacity: 0.4; }

    /* ===========================================================
       Toolbar (period selector)
       =========================================================== */
    .toolbar {
      display: flex;
      gap: 8px;
      margin-bottom: 28px;
      flex-wrap: wrap;
    }
    .toolbar button {
      background: var(--glass);
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      color: var(--text);
      border: 1px solid var(--glass-border);
      padding: 9px 18px;
      border-radius: 999px;
      cursor: pointer;
      font-size: 13px;
      font-weight: 500;
      letter-spacing: 0.005em;
      transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
    }
    .toolbar button:hover {
      background: rgba(255, 255, 255, 0.08);
      border-color: rgba(255, 255, 255, 0.18);
      transform: translateY(-1px);
    }
    .toolbar button.active {
      background: linear-gradient(135deg, #34d399 0%, #60a5fa 100%);
      color: #0a0f1e;
      border-color: transparent;
      font-weight: 600;
      box-shadow: 0 8px 24px rgba(52, 211, 153, 0.25);
    }

    /* ===========================================================
       Grid + Panels (glassmorphism)
       =========================================================== */
    .grid {
      display: grid;
      gap: 18px;
      margin-bottom: 18px;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    }
    .row {
      display: grid;
      gap: 18px;
      margin-bottom: 18px;
      grid-template-columns: 1fr 1fr;
    }
    @media (max-width: 800px) { .row { grid-template-columns: 1fr; } }

    .panel {
      background: var(--glass);
      backdrop-filter: blur(20px) saturate(180%);
      -webkit-backdrop-filter: blur(20px) saturate(180%);
      padding: 22px 24px;
      border-radius: 20px;
      border: 1px solid var(--glass-border);
      box-shadow: var(--glass-shadow),
                  inset 0 1px 0 rgba(255, 255, 255, 0.06);
      transition:
        transform 0.3s cubic-bezier(0.16, 1, 0.3, 1),
        box-shadow 0.3s cubic-bezier(0.16, 1, 0.3, 1),
        border-color 0.3s ease;
      animation: panelEnter 0.7s cubic-bezier(0.16, 1, 0.3, 1) backwards;
      position: relative;
      overflow: hidden;
    }
    #fixed-panel { margin-bottom: 18px; }
    @keyframes panelEnter {
      from { opacity: 0; transform: translateY(12px) scale(0.985); }
      to   { opacity: 1; transform: translateY(0) scale(1); }
    }
    /* Stagger delays */
    .panel:nth-of-type(1)  { animation-delay: 0.04s; }
    .panel:nth-of-type(2)  { animation-delay: 0.10s; }
    .panel:nth-of-type(3)  { animation-delay: 0.16s; }
    .panel:nth-of-type(4)  { animation-delay: 0.22s; }
    .panel:nth-of-type(5)  { animation-delay: 0.28s; }
    .panel:nth-of-type(6)  { animation-delay: 0.34s; }

    .panel:hover {
      transform: translateY(-3px);
      border-color: rgba(255, 255, 255, 0.16);
      box-shadow: var(--glass-shadow-hover),
                  inset 0 1px 0 rgba(255, 255, 255, 0.10);
    }

    /* Refresh fade-out / fade-in */
    .panel.fading { animation: panelFade 0.25s ease forwards; }
    @keyframes panelFade {
      to { opacity: 0.35; }
    }

    /* ===========================================================
       Typography inside panels
       =========================================================== */
    .panel h2 {
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.10em;
      font-weight: 600;
      margin-bottom: 12px;
    }
    .big {
      font-size: 40px;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
      letter-spacing: -0.025em;
      line-height: 1.1;
      background: linear-gradient(180deg, #ffffff 0%, #b6c4d6 100%);
      -webkit-background-clip: text;
      background-clip: text;
      -webkit-text-fill-color: transparent;
      color: transparent;
    }
    .pill {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 10px;
      font-weight: 700;
      margin-left: 8px;
      letter-spacing: 0.06em;
    }
    .pill.ok   { background: rgba(52, 211, 153, 0.18); color: #34d399; }
    .pill.warn { background: rgba(251, 191, 36, 0.18); color: #fbbf24; }
    .pill.bad  { background: rgba(248, 113, 113, 0.18); color: #f87171; }

    /* ===========================================================
       Charts
       =========================================================== */
    .chart-wrap { position: relative; height: 360px; }
    .chart-wrap.tall { height: 420px; }

    /* ===========================================================
       Budgets
       =========================================================== */
    .budget-bar {
      background: rgba(255, 255, 255, 0.06);
      border-radius: 6px;
      height: 8px;
      overflow: hidden;
      margin-top: 8px;
    }
    .budget-fill {
      height: 100%;
      border-radius: 6px;
      transition: width 0.8s cubic-bezier(0.16, 1, 0.3, 1),
                  background 0.3s ease;
      box-shadow: 0 0 12px currentColor;
      opacity: 0.85;
    }
    .budget-row {
      padding: 16px 0;
      border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    }
    .budget-row:last-child { border: none; }
    .budget-row > div > div + div { margin-top: 4px; }
    .budget-row > div > .fx-actions { margin-top: 10px; }

    /* ===========================================================
       Comparison table + Recent table
       =========================================================== */
    table { width: 100%; border-collapse: collapse; }
    th, td {
      padding: 8px 6px;
      text-align: left;
      font-size: 13px;
      font-variant-numeric: tabular-nums;
    }
    th {
      color: var(--muted);
      font-weight: 500;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    }
    td.amount { text-align: right; }
    .delta-up   { color: var(--bad); font-weight: 500; }
    .delta-down { color: var(--accent); font-weight: 500; }
    .delta-flat { color: var(--muted); }

    /* ===========================================================
       Inputs and small buttons (used by gastos fijos form)
       =========================================================== */
    .fx-input {
      width: 100%;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid rgba(255, 255, 255, 0.10);
      color: var(--text);
      padding: 8px 10px;
      border-radius: 8px;
      font-size: 13px;
      font-family: inherit;
      outline: none;
      transition: border-color 0.2s ease, background 0.2s ease;
    }
    .fx-input:focus {
      border-color: rgba(96, 165, 250, 0.5);
      background: rgba(255, 255, 255, 0.06);
    }
    select.fx-input { cursor: pointer; }

    .fx-btn {
      background: rgba(255, 255, 255, 0.04);
      backdrop-filter: blur(10px);
      -webkit-backdrop-filter: blur(10px);
      color: var(--text);
      border: 1px solid rgba(255, 255, 255, 0.10);
      padding: 8px 14px;
      border-radius: 8px;
      cursor: pointer;
      font-size: 12px;
      font-weight: 500;
      transition: all 0.2s ease;
    }
    .fx-btn:hover {
      background: rgba(255, 255, 255, 0.08);
      border-color: rgba(255, 255, 255, 0.18);
      transform: translateY(-1px);
    }
    .fx-btn-primary {
      background: linear-gradient(135deg, #34d399 0%, #60a5fa 100%);
      color: #0a0f1e;
      border-color: transparent;
      font-weight: 600;
    }
    .fx-btn-primary:hover {
      box-shadow: 0 8px 20px rgba(52, 211, 153, 0.30);
    }
    .fx-btn-tiny {
      padding: 4px 8px;
      font-size: 11px;
      border-radius: 6px;
    }
    .fx-actions {
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      align-items: center;
      margin-top: 10px;
    }
    .fx-actions > * + * { margin-left: 0; }  /* gap handles this */
    .fx-actions > input[type="number"] {
      min-width: 130px;
      flex: 0 0 auto;
    }
    .fx-skip-btn {
      background: rgba(255, 255, 255, 0.03);
      border-color: rgba(255, 255, 255, 0.06);
      color: var(--muted);
    }

    .category-modal[hidden] { display: none; }
    .category-modal {
      position: fixed;
      inset: 0;
      z-index: 20;
      display: grid;
      place-items: center;
      padding: 20px;
    }
    .category-modal-backdrop {
      position: absolute;
      inset: 0;
      background: rgba(2, 6, 23, 0.72);
      backdrop-filter: blur(5px);
    }
    .category-modal-card {
      position: relative;
      width: min(720px, 100%);
      max-height: min(720px, 90vh);
      overflow: auto;
      padding: 24px;
      border: 1px solid var(--glass-border);
      border-radius: 20px;
      background: #111a2d;
      box-shadow: var(--glass-shadow-hover);
    }
    .category-modal-head {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
      margin-bottom: 18px;
    }
    .category-modal-close {
      border: 0;
      background: transparent;
      color: var(--muted);
      font-size: 24px;
      cursor: pointer;
      line-height: 1;
    }

    /* ===========================================================
       Background chart colors (Apple-inspired palette)
       =========================================================== */

  </style>
</head>
<body>
  <main>
    <header>
      <h1 id="page-title"><span class="emoji">💸</span> Argentine Telegram Expenses</h1>
      <div class="subtitle" id="subtitle">Cargando…</div>
    </header>

    <div class="toolbar" id="period-bar">
      <button data-p="month" class="active">Mes</button>
      <button data-p="week">Semana</button>
      <button data-p="today">Hoy</button>
      <button data-p="last_month">Mes pasado</button>
      <button data-p="year">Año</button>
      <a href="/dashboard/mensual" style="margin-left:auto; color:var(--accent); text-decoration:none; font-size:12px; align-self:center">Resumen mensual →</a>
    </div>

    <div class="toolbar" id="month-nav" style="display:none; justify-content:center; gap:16px; align-items:center">
      <button id="prev-month" aria-label="Mes anterior">←</button>
      <strong id="month-nav-label"></strong>
      <button id="next-month" aria-label="Mes siguiente">→</button>
      <a href="/dashboard" style="margin-left:10px; color:var(--accent); text-decoration:none; font-size:12px">Vista general</a>
    </div>

    <div class="grid" id="kpis"></div>

    <div class="row">
       <div class="panel" id="recurring-panel">
        <h2 id="trend-title">Tendencia</h2>
        <div class="chart-wrap"><canvas id="trend-chart"></canvas></div>
      </div>
      <div class="panel">
        <h2 id="category-title">Por categoría</h2>
        <div class="chart-wrap tall"><canvas id="category-chart"></canvas></div>
      </div>
    </div>

    <div class="row">
      <div class="panel">
        <h2 id="budgets-title">Presupuestos</h2>
        <div id="budgets-list"></div>
      </div>
      <div class="panel">
        <h2>Próximos recurrentes</h2>
        <div id="recurring-list"></div>
      </div>
    </div>

    <div class="panel" id="fixed-panel">
      <div style="display:flex; justify-content:space-between; align-items:flex-end; margin-bottom:14px; flex-wrap:wrap; gap:10px">
        <div>
          <h2 id="fixed-title" style="margin-bottom:2px">Gastos fijos</h2>
          <div class="subtitle" id="fixed-subtitle">Cargando…</div>
        </div>
        <div style="display:flex; gap:18px; align-items:flex-end; flex-wrap:wrap">
          <div style="text-align:right">
            <div style="font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.06em">Ingreso</div>
            <div class="big" id="fixed-income" style="font-size:22px">—</div>
          </div>
          <div style="text-align:right">
            <div style="font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.06em">Extra</div>
            <div class="big" id="fixed-extra" style="font-size:22px">—</div>
          </div>
          <div style="text-align:right">
            <div style="font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.06em">Pagado</div>
            <div class="big" id="fixed-paid" style="font-size:22px">—</div>
          </div>
          <div style="text-align:right">
            <div style="font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.06em">Liberado</div>
            <div class="big" id="fixed-liberado" style="font-size:24px">—</div>
          </div>
        </div>
      </div>
      <div id="fixed-list"></div>

      <div id="fixed-add-row" style="margin-top:14px; display:none">
        <div class="panel" style="padding:16px">
          <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:flex-end">
            <div style="flex:2; min-width:160px">
              <label class="subtitle" style="display:block; margin-bottom:4px">Nombre</label>
              <input id="fx-name" type="text" placeholder="CASA" class="fx-input">
            </div>
            <div style="flex:1; min-width:120px">
              <label class="subtitle" style="display:block; margin-bottom:4px">Monto esperado</label>
              <input id="fx-amount" type="number" placeholder="90000" class="fx-input">
            </div>
            <div style="flex:1; min-width:90px">
              <label class="subtitle" style="display:block; margin-bottom:4px">Día del mes</label>
              <input id="fx-day" type="number" min="1" max="31" placeholder="10" class="fx-input">
            </div>
            <div style="flex:1; min-width:140px">
              <label class="subtitle" style="display:block; margin-bottom:4px">Método</label>
              <select id="fx-method" class="fx-input">
                <option value="">(sin método)</option>
                <option value="TRANSFERENCIA">Transferencia</option>
                <option value="EFECTIVO">Efectivo</option>
                <option value="DEBITO">Débito automático</option>
                <option value="TARJETA">Tarjeta</option>
                <option value="MERCADO PAGO">Mercado Pago</option>
                <option value="APP">App</option>
              </select>
            </div>
            <div style="display:flex; gap:6px">
              <button id="fx-save" class="fx-btn fx-btn-primary">Guardar</button>
              <button id="fx-cancel" class="fx-btn">Cancelar</button>
            </div>
          </div>
          <div id="fx-error" class="subtitle" style="color:#f87171; margin-top:8px; display:none"></div>
        </div>
      </div>

      <div id="fixed-actions" style="margin-top:18px; padding-top:16px; border-top:1px solid rgba(255,255,255,0.05); display:flex; gap:8px; flex-wrap:wrap">
        <button id="fx-add-btn" class="fx-btn fx-btn-primary">+ Agregar gasto fijo</button>
        <div class="subtitle" style="margin-left:auto; align-self:center">
          ¿No tenés gastos fijos? Empezá agregando arriba.
        </div>
      </div>
    </div>

    <div class="panel" style="margin-top:18px">
      <h2 id="grouped-title">Gastos agrupados</h2>
      <div id="grouped-list"></div>
    </div>

    <div class="row">
      <div class="panel">
        <h2 id="comparison-title">Comparativa</h2>
        <div id="comparison-list"></div>
      </div>
      <div class="panel" id="projection-panel">
        <h2 id="projection-title">Proyección</h2>
        <div id="projection-card"></div>
      </div>
    </div>

    <div class="panel">
      <div style="display:flex; justify-content:space-between; align-items:flex-end; margin-bottom:14px; flex-wrap:wrap; gap:10px">
        <div>
          <h2 id="all-title" style="margin-bottom:2px">Todos los gastos</h2>
          <div class="subtitle" id="all-subtitle">Cargando…</div>
        </div>
        <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap">
          <div style="text-align:right">
            <div style="font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.06em">Total listado</div>
            <div class="big" id="all-total" style="font-size:22px">—</div>
          </div>
          <select id="all-sort" class="fx-input" style="width:auto; padding:6px 8px; font-size:12px">
            <option value="date_desc">Más recientes</option>
            <option value="date_asc">Más antiguos</option>
            <option value="amount_desc">Mayor monto</option>
            <option value="amount_asc">Menor monto</option>
          </select>
        </div>
      </div>
      <div style="overflow-x:auto">
        <table>
          <thead><tr>
            <th style="cursor:pointer" onclick="allSort('date')">Fecha</th>
            <th>Nombre</th>
            <th>Categoría</th>
            <th style="cursor:pointer; text-align:right" onclick="allSort('amount')">Monto</th>
          </tr></thead>
          <tbody id="all-tbody"></tbody>
        </table>
      </div>
      <div id="all-footer" class="subtitle" style="margin-top:10px; text-align:right; display:none"></div>
    </div>

    <div class="panel" style="margin-top:18px">
      <h2 id="recent-title">Últimos 10 gastos</h2>
      <table>
        <thead><tr><th>Fecha</th><th>Nombre</th><th>Categoría</th>
                   <th style="text-align:right">Monto</th></tr></thead>
        <tbody id="recent-tbody"></tbody>
      </table>
    </div>
  </main>

  <div id="category-modal" class="category-modal" hidden>
    <div class="category-modal-backdrop" data-close-category-modal></div>
    <section class="category-modal-card" role="dialog" aria-modal="true" aria-labelledby="category-modal-title">
      <div class="category-modal-head">
        <div>
          <h2 id="category-modal-title">Gastos de la categoría</h2>
          <div id="category-modal-summary" class="subtitle"></div>
        </div>
        <button class="category-modal-close" type="button" aria-label="Cerrar" data-close-category-modal>×</button>
      </div>
      <div id="category-modal-body"></div>
    </section>
  </div>

<script>
const fmt = new Intl.NumberFormat('es-AR');
const currency = (n, c) => '$ ' + fmt.format(n) + ' ' + c;
const monthlyDashboard = location.pathname === '/dashboard/mensual';
let trendChart, categoryChart;
let currentPeriod = 'month';
let selectedMonth = null;
let latestMonth = null;
let firstLoad = true;
let activeSummary = null;
const kpiLastValues = {};

const PERIOD_LABELS = {
  today: 'Hoy',
  week: 'Esta semana',
  month: 'Este mes',
  last_month: 'Mes pasado',
  year: 'Este año',
};

function trendTitleFor(period) {
  if (period === 'today') return 'Tendencia (hoy)';
  if (period === 'week') return 'Tendencia (semana)';
  if (period === 'month') return 'Tendencia (mes)';
  if (period === 'last_month') return 'Tendencia (mes pasado)';
  if (period === 'year') return 'Tendencia (año)';
  return 'Tendencia';
}

function comparisonTitleFor(period) {
  const cur = PERIOD_LABELS[period] || 'Período actual';
  const prevPeriod = {
    today: 'ayer', week: 'semana pasada', month: 'mes pasado',
    last_month: 'mes antepasado', year: 'año pasado',
  }[period] || 'anterior';
  return `${cur} vs ${prevPeriod}`;
}

function projectionApplies(period) {
  return period !== 'today';
}

function shiftMonth(month, offset) {
  const [year, monthNumber] = month.split('-').map(Number);
  const value = new Date(year, monthNumber - 1 + offset, 1);
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}`;
}

function updateMonthNav(summary) {
  selectedMonth = summary.month;
  latestMonth = summary.current_month;
  document.getElementById('month-nav-label').textContent = summary.period_label;
  document.getElementById('next-month').disabled = selectedMonth >= latestMonth;
}

/* ==============================
   Animated number counters
   ============================== */
function animateNumber(el, to, duration = 700) {
  const fromText = el.dataset.value;
  const from = fromText === undefined ? 0 : parseFloat(fromText);
  if (from === to) {
    el.textContent = fmt.format(to);
    el.dataset.value = to;
    return;
  }
  const start = performance.now();
  const ease = t => 1 - Math.pow(1 - t, 3); // easeOutCubic
  function step(now) {
    const p = Math.min((now - start) / duration, 1);
    const v = from + (to - from) * ease(p);
    el.textContent = fmt.format(Math.round(v));
    if (p < 1) requestAnimationFrame(step);
    else el.dataset.value = to;
  }
  requestAnimationFrame(step);
}

/* ==============================
   Fetch + render
   ============================== */
async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + ' ' + r.status);
  return r.json();
}

async function refresh() {
  const subtitle = document.getElementById('subtitle');
  subtitle.classList.add('refreshing');

  const periodParam = '?period=' + currentPeriod;
  let summary, trend, recent, budgets, recurring, comparison, projection, fixed, monthSum, all;
  try {
    if (monthlyDashboard) {
      const monthParam = selectedMonth ? '?month=' + selectedMonth : '';
      const monthly = await getJSON('/api/monthly-summary' + monthParam);
      summary = monthly.summary;
      trend = monthly.trend;
      recent = {items: monthly.expenses.items.slice(0, 10)};
      budgets = monthly.budgets;
      recurring = {items: []};
      comparison = monthly.comparison;
      projection = {applies: false};
      fixed = {items: []};
      monthSum = null;
      all = monthly.expenses;
      updateMonthNav(monthly);
    } else {
      [summary, trend, recent, budgets, recurring, comparison, projection, fixed, monthSum, all] =
        await Promise.all([
          getJSON('/api/summary' + periodParam),
          getJSON('/api/trend' + periodParam),
          getJSON('/api/recent?limit=10'),
          getJSON('/api/budgets' + periodParam),
          getJSON('/api/recurring/upcoming'),
          getJSON('/api/comparison' + periodParam),
          getJSON('/api/projection' + periodParam),
          getJSON('/api/fixed-expenses'),
          getJSON('/api/month-summary'),
          getJSON('/api/expenses' + periodParam),
        ]);
    }
  } catch (err) {
    subtitle.classList.remove('refreshing');
    subtitle.textContent = 'Error: ' + err.message;
    return;
  }

  subtitle.textContent = summary.period_label + ' · ' + summary.start + ' → ' + summary.end;
  subtitle.classList.remove('refreshing');
  activeSummary = summary;

  document.getElementById('trend-title').textContent = monthlyDashboard
    ? 'Gastos por día'
    : trendTitleFor(currentPeriod);
  document.getElementById('budgets-title').textContent =
    'Presupuestos — ' + summary.period_label.toLowerCase();
  document.getElementById('comparison-title').textContent = monthlyDashboard
    ? `${summary.period_label} vs mes anterior`
    : comparisonTitleFor(currentPeriod);
  document.getElementById('projection-title').textContent =
    'Proyección — ' + summary.period_label.toLowerCase();
  document.getElementById('projection-panel').style.display =
    monthlyDashboard ? 'none' : (projectionApplies(currentPeriod) ? '' : 'none');

  // KPI cards (with animated counters on first load)
  const totals = Object.entries(summary.total_by_currency || {});
  const kpiHTML = totals.map(([c, v], idx) => {
    const prev = kpiLastValues[c];
    kpiLastValues[c] = v;
    return `
      <div class="panel" style="animation-delay:${0.04 * (idx + 1)}s">
        <h2>Total ${summary.period_label.toLowerCase()} <span class="pill ok">${c}</span></h2>
        <div class="big" data-value="${prev ?? 0}" style="margin-top:14px">$${fmt.format(firstLoad ? v : v)}</div>
        <div style="color:var(--muted); font-size:12px; margin-top:10px">
          Día ${summary.days_elapsed} de ${summary.days_in_period}
        </div>
      </div>`;
  }).join('') || '<div class="panel"><h2>Sin gastos</h2></div>';
  const kpiContainer = document.getElementById('kpis');
  kpiContainer.innerHTML = kpiHTML;
  // Animate counters from previous values (only after first load)
  if (!firstLoad) {
    kpiContainer.querySelectorAll('.big').forEach(el => {
      const target = parseFloat(el.dataset.value);
      const cur = kpiLastValues[Object.keys(kpiLastValues).find(k => String(kpiLastValues[k]) === el.dataset.value)];
      animateNumber(el, target, 700);
    });
  } else {
    // First load: just set the values
    firstLoad = false;
  }

  // Trend chart
  if (trendChart) trendChart.destroy();
  const tLabels = trend.points.map(p => p.date.slice(5));
  const tData = trend.points.map(p => p.amount);
  const trendGradient = (ctx) => {
    const chart = ctx.chart;
    const {ctx: c, chartArea} = chart;
    if (!chartArea) return 'rgba(52, 211, 153, 0.15)';
    const g = c.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
    g.addColorStop(0, 'rgba(52, 211, 153, 0.45)');
    g.addColorStop(1, 'rgba(52, 211, 153, 0)');
    return g;
  };
  trendChart = new Chart(document.getElementById('trend-chart'), {
    type: 'line',
    data: {
      labels: tLabels,
      datasets: [{
        label: 'ARS',
        data: tData,
        borderColor: '#34d399',
        backgroundColor: trendGradient,
        borderWidth: 2,
        fill: true,
        tension: 0.4,
        pointRadius: 0,
        pointHoverRadius: 5,
        pointHoverBackgroundColor: '#34d399',
        pointHoverBorderColor: '#fff',
        pointHoverBorderWidth: 2,
      }],
    },
    options: chartOpts(),
  });

  // Category pie/donut
  const cats = summary.by_category || [];
  if (categoryChart) categoryChart.destroy();
  if (cats.length) {
    categoryChart = new Chart(document.getElementById('category-chart'), {
      type: 'doughnut',
      data: {
        labels: cats.map(c => c.name),
        datasets: [{
          data: cats.map(c => c.amount),
          backgroundColor: palette(cats.length),
          borderColor: 'rgba(15, 23, 42, 0.6)',
          borderWidth: 2,
          hoverOffset: 12,
        }],
      },
      options: {
         responsive: true, maintainAspectRatio: false,
         cutout: '62%',
         onClick: (_event, elements) => {
           if (elements.length) {
             openCategoryModal(cats[elements[0].index].name);
           }
         },
         plugins: {
          legend: {
            position: 'bottom',
            labels: {
              color: '#e2e8f0',
              boxWidth: 10,
              padding: 10,
              font: { size: 11 },
            },
          },
        },
      },
    });
  } else {
    document.getElementById('category-chart').replaceWith(
      Object.assign(document.createElement('div'), {
        className: 'subtitle',
        style: 'padding: 40px; text-align: center',
        textContent: 'Sin gastos en este período.',
      })
    );
  }

  // Repeated expenses grouped by normalized name, category and currency.
  const groupedDiv = document.getElementById('grouped-list');
  document.getElementById('grouped-title').textContent =
    'Gastos agrupados — ' + summary.period_label;
  const grouped = summary.by_name_by_currency || {};
  const groupedCurrencies = Object.keys(grouped);
  if (!groupedCurrencies.length) {
    groupedDiv.innerHTML = '<div class="subtitle">Sin gastos en este período.</div>';
  } else {
    groupedDiv.innerHTML = groupedCurrencies.map(cur => `
      <div style="margin-bottom:14px">
        <div class="subtitle" style="margin-bottom:6px">${cur}</div>
        ${grouped[cur].map(g => `
          <div style="display:flex; justify-content:space-between; gap:12px; padding:9px 0; border-bottom:1px solid rgba(255,255,255,0.05)">
            <div><strong>${escapeHTML(g.name)}</strong><div class="subtitle">${escapeHTML(g.category)} · ${g.count} gasto${g.count === 1 ? '' : 's'}</div></div>
            <strong style="white-space:nowrap">${currency(g.amount, cur)}</strong>
          </div>`).join('')}
      </div>`).join('');
  }

  // Budgets
  const bDiv = document.getElementById('budgets-list');
  if (budgets.items.length === 0) {
    bDiv.innerHTML = '<div class="subtitle">No tenés presupuestos. Creá uno con <code>/presupuesto &lt;categoría&gt; &lt;monto&gt;</code> en Telegram.</div>';
  } else {
    const isMonth = currentPeriod === 'month' || monthlyDashboard;
    bDiv.innerHTML = budgets.items.map(b => {
      const color = b.level === 'exceeded' ? '#f87171'
                   : b.level === 'warning' ? '#fbbf24' : '#34d399';
      const limitDisp = isMonth ? b.limit : (b.effective_limit || b.limit);
      return `
        <div class="budget-row">
          <div><strong>${b.category}</strong> · ${currency(b.limit, b.currency)} mensual
            <span class="pill ${b.level === 'exceeded' ? 'bad' : b.level === 'warning' ? 'warn' : 'ok'}">
              ${b.percent.toFixed(0)}%
            </span>
          </div>
          <div class="subtitle">${currency(b.spent, b.currency)} de ${currency(limitDisp, b.currency)}${isMonth ? '' : ' (prorrateado)'}</div>
          <div class="budget-bar"><div class="budget-fill"
               style="width:${Math.min(100, b.percent)}%; background:${color}; color:${color}"></div></div>
        </div>`;
    }).join('');
  }

  // Recurring upcoming
  const rDiv = document.getElementById('recurring-list');
  if (recurring.items.length === 0) {
    rDiv.innerHTML = '<div class="subtitle">Sin vencimientos en los próximos 7 días.</div>';
  } else {
    rDiv.innerHTML = recurring.items.map(r =>
      `<div class="budget-row">
         <div><strong>${r.name}</strong> — ${currency(r.amount, r.currency)}</div>
         <div class="subtitle">vence ${r.next_due_date}</div>
       </div>`).join('');
  }

  // Comparison
  const cDiv = document.getElementById('comparison-list');
  if (comparison.by_category.length === 0) {
    cDiv.innerHTML = '<div class="subtitle">Sin datos comparables.</div>';
  } else {
    const dClass = comparison.delta_pct == null ? 'delta-flat'
                 : comparison.delta_pct > 0 ? 'delta-up'
                 : comparison.delta_pct < 0 ? 'delta-down' : 'delta-flat';
    const dArrow = comparison.delta_pct == null ? '·'
                 : comparison.delta_pct > 0 ? '↑' : comparison.delta_pct < 0 ? '↓' : '·';
    const dSign = comparison.delta_pct == null ? ''
                 : (comparison.delta_pct > 0 ? '+' : '');
    const top = `
      <div style="display:flex; flex-direction:column; gap:6px; padding:6px 0 4px">
        <div><strong>${comparison.current.label}</strong>
             <span style="color:var(--muted); margin-left:6px">${currency(comparison.current.total, 'ARS')}</span></div>
        <div><strong>${comparison.previous.label}</strong>
             <span style="color:var(--muted); margin-left:6px">${currency(comparison.previous.total, 'ARS')}</span></div>
        <div class="${dClass}" style="font-size:28px; font-weight:600; margin-top:8px">
          ${dArrow} ${dSign}${comparison.delta_pct == null ? '—' : comparison.delta_pct.toFixed(1) + '%'}
        </div>
      </div>`;
    const rows = comparison.by_category.slice(0, 6).map(r => {
      const cls = r.diff_pct == null ? 'delta-flat'
                : r.diff_pct > 0 ? 'delta-up'
                : r.diff_pct < 0 ? 'delta-down' : 'delta-flat';
      const arrow = r.diff_pct == null ? '' : r.diff_pct > 0 ? '↑' : r.diff_pct < 0 ? '↓' : '';
      const sign = r.diff_pct == null ? '' : r.diff_pct > 0 ? '+' : '';
      return `<tr>
        <td>${r.category}</td>
        <td class="amount">$${fmt.format(r.current)}</td>
        <td class="amount">$${fmt.format(r.previous)}</td>
        <td class="${cls}">${arrow} ${sign}${r.diff_pct == null ? '—' : r.diff_pct.toFixed(1) + '%'}</td>
      </tr>`;
    }).join('');
    const moreCount = Math.max(0, comparison.by_category.length - 6);
    const footer = moreCount > 0
      ? `<div class="subtitle" style="margin-top:8px; text-align:right">+${moreCount} más…</div>`
      : '';
    cDiv.innerHTML = top + `
      <table style="margin-top:18px">
        <thead><tr><th>Categoría</th>
                   <th style="text-align:right">${comparison.current.label}</th>
                   <th style="text-align:right">${comparison.previous.label}</th>
                   <th style="text-align:right">Δ</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>${footer}`;
  }

  // Projection
  const pDiv = document.getElementById('projection-card');
  if (projection.applies === false) {
    pDiv.innerHTML = '<div class="subtitle">La proyección aplica solo a períodos en curso (semana, mes, año). Para hoy ya tenés el total cerrado.</div>';
  } else if (projection.previous_total === 0 && projection.spent === 0) {
    pDiv.innerHTML = '<div class="subtitle">Sin datos suficientes para proyectar.</div>';
  } else {
    const dCls = projection.delta_vs_previous_pct == null ? 'delta-flat'
              : projection.delta_vs_previous_pct > 0 ? 'delta-up'
              : projection.delta_vs_previous_pct < 0 ? 'delta-down' : 'delta-flat';
    const arrow = projection.delta_vs_previous_pct == null ? '·'
                : projection.delta_vs_previous_pct > 0 ? '↑'
                : projection.delta_vs_previous_pct < 0 ? '↓' : '·';
    const sign = projection.delta_vs_previous_pct == null ? ''
              : (projection.delta_vs_previous_pct > 0 ? '+' : '');
    pDiv.innerHTML = `
      <div style="display:flex; flex-direction:column; gap:6px; padding:6px 0 4px">
        <div class="subtitle" style="font-size:11px; text-transform:uppercase; letter-spacing:0.06em; color:var(--muted)">Gastado</div>
        <div class="big" style="font-size:32px">$${fmt.format(projection.spent)}</div>
        <div class="subtitle">
          Día ${projection.days_elapsed} de ${projection.days_in_period}
        </div>
      </div>
      <div style="height:1px; background:rgba(255,255,255,0.06); margin:18px 0"></div>
      <div style="display:flex; flex-direction:column; gap:6px">
        <div class="subtitle" style="font-size:11px; text-transform:uppercase; letter-spacing:0.06em; color:var(--muted)">Proyección fin de período</div>
        <div class="big" style="color:var(--accent); font-size:32px">$${fmt.format(projection.projected_total)}</div>
        <div class="${dCls}" style="margin-top:4px; font-size:18px; font-weight:500">
          ${arrow} ${sign}${projection.delta_vs_previous_pct == null ? '—' : projection.delta_vs_previous_pct.toFixed(1) + '%'}
          vs período anterior
        </div>
      </div>`;
  }

  // Recent expenses table (last 10)
  document.getElementById('recent-tbody').innerHTML = recent.items.map(e => `
    <tr>
      <td>${e.date}</td>
      <td>${escapeHTML(e.name)}</td>
      <td>${escapeHTML(e.category)}</td>
      <td class="amount">$${fmt.format(e.amount)} ${e.currency}</td>
    </tr>`).join('');

  // Fixed expenses section
  if (!monthlyDashboard) renderFixedExpenses(fixed, monthSum);

  // All-expenses panel
  renderAllExpenses(all);
}

function renderFixedExpenses(fixed, monthSum) {
  const libEl = document.getElementById('fixed-liberado');
  if (libEl) {
    const prev = parseFloat(libEl.dataset.value || '0');
    animateNumber(libEl, monthSum.liberado, 700);
  }
  document.getElementById('fixed-income').textContent = '$' + fmt.format(monthSum.income);
  document.getElementById('fixed-extra').textContent = '$' + fmt.format(monthSum.extra);
  document.getElementById('fixed-paid').textContent = '$' + fmt.format(monthSum.total_fixed_paid);

  const libColor = monthSum.liberado >= 0 ? '#34d399' : '#f87171';
  if (libEl) {
    libEl.style.background = `linear-gradient(180deg, ${libColor} 0%, ${libColor}99 100%)`;
    libEl.style.webkitBackgroundClip = 'text';
    libEl.style.backgroundClip = 'text';
    libEl.style.webkitTextFillColor = 'transparent';
  }

  const sub = document.getElementById('fixed-subtitle');
  const items = fixed.items || [];
  if (items.length === 0) {
    sub.textContent = 'No tenés gastos fijos. Cargalos desde Telegram: /gastofijo_add CASA 90000 10 transferencia';
    document.getElementById('fixed-list').innerHTML = '';
    return;
  }
  const paid = items.filter(i => i.status === 'paid_exact' || i.status === 'paid_more' || i.status === 'paid_less').length;
  sub.textContent = `${paid}/${items.length} pagados · Mes ${monthSum.month_year}`;

  const STATUS_ICON = {
    pending: '⏳', paid_exact: '✅', paid_more: '💰', paid_less: '⚠️', skipped: '⏭️',
  };
  const statusPillCls = {
    pending: 'bad', paid_exact: 'ok', paid_more: 'warn',
    paid_less: 'warn', skipped: '',
  };
  const statusLabel = {
    pending: 'pendiente', paid_exact: 'pagado',
    paid_more: 'pagado +', paid_less: 'pagado -', skipped: 'salteado',
  };
  document.getElementById('fixed-list').innerHTML = items.map(b => {
    const icon = STATUS_ICON[b.status] || '⏳';
    const method = b.payment_method || '—';
    let actualStr = '';
    if (b.actual_amount != null && b.actual_amount !== b.expected_amount) {
      const diff = b.actual_amount - b.expected_amount;
      const sign = diff > 0 ? '+' : '';
      const cls = diff > 0 ? 'paid-more' : 'paid-less';
      actualStr = ` <span class="${cls}">→ $${fmt.format(b.actual_amount)} (${sign}${fmt.format(Math.abs(diff))})</span>`;
    }
    let actionsHTML = '';
    if (b.status === 'pending') {
      actionsHTML = `
        <div class="fx-actions">
          <input type="number" id="pay-amt-${b.id}" placeholder="monto (opcional)"
                 class="fx-input fx-btn-tiny" style="width:130px">
          <button class="fx-btn fx-btn-primary fx-btn-tiny"
                  onclick="fxPay(${b.id})">✓ Pagar</button>
          <button class="fx-btn fx-btn-tiny" onclick="fxSkip(${b.id})">Saltear</button>
        </div>`;
    } else if (b.status === 'paid_exact' || b.status === 'paid_more' || b.status === 'paid_less') {
      actionsHTML = `
        <div class="fx-actions">
          <button class="fx-btn fx-btn-tiny" onclick="fxUnpay(${b.id})">↶ Deshacer</button>
        </div>`;
    } else if (b.status === 'skipped') {
      actionsHTML = `
        <div class="fx-actions">
          <button class="fx-btn fx-btn-tiny" onclick="fxUnpay(${b.id})">↶ Reactivar</button>
        </div>`;
    }
    const pillBg = b.status === 'skipped'
      ? 'background:rgba(255,255,255,0.06); color:#94a3b8' : '';
    return `
      <div class="budget-row" data-fx-id="${b.id}">
        <div style="display:flex; align-items:center; gap:10px">
          <span style="font-size:18px; line-height:1">${icon}</span>
          <div style="flex:1">
            <div><strong>${escapeHTML(b.name)}</strong>
              <span class="pill ${statusPillCls[b.status] || 'bad'}"
                    style="${pillBg}">
                ${statusLabel[b.status] || b.status}
              </span>
              ${b.status === 'paid_exact' || b.status === 'paid_more' || b.status === 'paid_less'
                ? '<span class="pill" style="background:rgba(96,165,250,0.18); color:#60a5fa; margin-left:6px">📊 en totales</span>'
                : ''}
            </div>
            <div class="subtitle">$${fmt.format(b.expected_amount)} ${b.currency} · ${method} · día ${b.due_day_of_month}${actualStr}</div>
            ${actionsHTML}
          </div>
        </div>
      </div>`;
  }).join('');
}

function allSort(column) {
  const sel = document.getElementById('all-sort');
  if (sel.value.startsWith(column)) {
    // Toggle direction if same column.
    sel.value = sel.value.endsWith('_asc')
      ? column + '_desc'
      : column + '_asc';
  } else {
    sel.value = column + '_desc';
  }
  renderAllExpenses(currentAllData);
}

let currentAllData = null;

function renderAllExpenses(data) {
  currentAllData = data;
  const items = data.items || [];
  const sortKey = document.getElementById('all-sort').value;
  const sorted = [...items].sort((a, b) => {
    if (sortKey === 'date_desc') return b.date.localeCompare(a.date) || b.id - a.id;
    if (sortKey === 'date_asc')  return a.date.localeCompare(b.date) || a.id - b.id;
    if (sortKey === 'amount_desc') return b.amount - a.amount;
    if (sortKey === 'amount_asc')  return a.amount - b.amount;
    return 0;
  });
  document.getElementById('all-subtitle').textContent =
    `${data.count} gastos · ${data.start} → ${data.end}`;
  const totalEl = document.getElementById('all-total');
  totalEl.textContent = '$' + fmt.format(data.total || 0);
  document.getElementById('all-tbody').innerHTML = sorted.map(e => `
    <tr>
      <td>${e.date}</td>
      <td>${escapeHTML(e.name)}</td>
      <td>${escapeHTML(e.category)}</td>
      <td class="amount">$${fmt.format(e.amount)} ${e.currency}</td>
    </tr>`).join('');
  const footer = document.getElementById('all-footer');
  if (data.count >= 500) {
    footer.textContent = `Mostrando los primeros 500. Exportá para ver todos (/exportar).`;
    footer.style.display = 'block';
  } else {
    footer.style.display = 'none';
  }
}

async function openCategoryModal(category) {
  const modal = document.getElementById('category-modal');
  const body = document.getElementById('category-modal-body');
  const summaryEl = document.getElementById('category-modal-summary');
  document.getElementById('category-modal-title').textContent = category;
  summaryEl.textContent = 'Cargando…';
  body.innerHTML = '';
  modal.hidden = false;

  const params = new URLSearchParams({category, limit: '2000'});
  if (monthlyDashboard) {
    params.set('period', 'custom');
    params.set('custom_start', activeSummary.start);
    params.set('custom_end', activeSummary.end);
  } else {
    params.set('period', currentPeriod);
  }

  try {
    const data = await getJSON('/api/expenses?' + params.toString());
    const totals = Object.entries(data.total_by_currency || {})
      .map(([cur, amount]) => currency(amount, cur)).join(' · ');
    summaryEl.textContent = `${data.count} gasto${data.count === 1 ? '' : 's'} · ${totals || 'Sin movimientos'}`;
    if (!data.items.length) {
      body.innerHTML = '<div class="subtitle">No hay movimientos en este período.</div>';
      return;
    }
    body.innerHTML = `
      <div style="overflow-x:auto">
        <table>
          <thead><tr><th>Fecha</th><th>Nombre</th><th style="text-align:right">Monto</th></tr></thead>
          <tbody>${data.items.map(e => `
            <tr>
              <td>${e.date}</td>
              <td>${escapeHTML(e.name)}</td>
              <td class="amount">${currency(e.amount, e.currency)}</td>
            </tr>`).join('')}</tbody>
        </table>
      </div>
      ${data.count > data.items.length
        ? `<div class="subtitle" style="margin-top:10px">Mostrando ${data.items.length} de ${data.count} gastos.</div>`
        : ''}`;
  } catch (err) {
    summaryEl.textContent = '';
    body.innerHTML = `<div class="subtitle" style="color:#f87171">Error: ${escapeHTML(err.message)}</div>`;
  }
}

function closeCategoryModal() {
  document.getElementById('category-modal').hidden = true;
}

async function fxCreate() {
  const errEl = document.getElementById('fx-error');
  errEl.style.display = 'none';
  const name = document.getElementById('fx-name').value.trim();
  const amount = parseFloat(document.getElementById('fx-amount').value);
  const day = parseInt(document.getElementById('fx-day').value || '1', 10);
  const method = document.getElementById('fx-method').value;
  if (!name) { fxShowError('Falta el nombre.'); return; }
  if (!Number.isFinite(amount) || amount <= 0) { fxShowError('Monto inválido.'); return; }
  try {
    const r = await fetch('/api/fixed-expenses', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name, expected_amount: amount,
        due_day_of_month: day, payment_method: method, currency: 'ARS',
      }),
    });
    if (!r.ok) {
      const detail = (await r.json()).detail || ('HTTP ' + r.status);
      fxShowError(detail);
      return;
    }
    // Reset and hide the form, then refresh data.
    document.getElementById('fx-name').value = '';
    document.getElementById('fx-amount').value = '';
    document.getElementById('fx-day').value = '';
    document.getElementById('fx-method').value = '';
    document.getElementById('fixed-add-row').style.display = 'none';
    await refresh();
  } catch (err) {
    fxShowError('Error de red: ' + err.message);
  }
}

function fxShowError(msg) {
  const errEl = document.getElementById('fx-error');
  errEl.textContent = msg;
  errEl.style.display = 'block';
}

async function fxAction(path, body) {
  try {
    const r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : null,
    });
    if (!r.ok) {
      const detail = (await r.json().catch(() => null) || {}).detail
        || ('HTTP ' + r.status);
      alert('Error: ' + detail);
      return;
    }
    await refresh();
  } catch (err) {
    alert('Error: ' + err.message);
  }
}

async function fxPay(id) {
  const input = document.getElementById('pay-amt-' + id);
  const amount = input && input.value ? parseFloat(input.value) : null;
  if (amount !== null && (!Number.isFinite(amount) || amount <= 0)) {
    alert('Monto inválido.');
    return;
  }
  await fxAction('/api/fixed-expenses/' + id + '/pay',
    amount !== null ? { actual_amount: amount } : {});
}

async function fxSkip(id) {
  await fxAction('/api/fixed-expenses/' + id + '/skip');
}

async function fxUnpay(id) {
  await fxAction('/api/fixed-expenses/' + id + '/unpay');
}

function fxToggleAdd() {
  const row = document.getElementById('fixed-add-row');
  const isHidden = row.style.display === 'none' || !row.style.display;
  row.style.display = isHidden ? 'block' : 'none';
  if (isHidden) document.getElementById('fx-name').focus();
}

function palette(n) {
  // Apple-inspired soft palette
  const base = [
    '#34d399', '#60a5fa', '#fbbf24', '#f87171',
    '#a78bfa', '#22d3ee', '#f472b6', '#fb923c',
    '#94a3b8', '#4ade80',
  ];
  return Array.from({length: n}, (_, i) => base[i % base.length]);
}

function chartOpts() {
  return {
    responsive: true, maintainAspectRatio: false,
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { labels: { color: '#e2e8f0' } },
      tooltip: {
        backgroundColor: 'rgba(15, 23, 42, 0.95)',
        borderColor: 'rgba(255,255,255,0.1)',
        borderWidth: 1,
        titleColor: '#f1f5f9',
        bodyColor: '#cbd5e1',
        padding: 10,
        cornerRadius: 8,
      },
    },
    animation: { duration: 800, easing: 'easeOutQuart' },
    scales: {
      x: { ticks: { color: '#94a3b8' }, grid: { color: 'rgba(255,255,255,0.04)' } },
      y: { ticks: { color: '#94a3b8' }, grid: { color: 'rgba(255,255,255,0.04)' } },
    },
  };
}

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

if (monthlyDashboard) {
  document.getElementById('page-title').innerHTML =
    '<span class="emoji">📅</span> Resumen mensual';
  document.getElementById('period-bar').style.display = 'none';
  document.getElementById('month-nav').style.display = 'flex';
  document.getElementById('fixed-panel').style.display = 'none';
  document.getElementById('recurring-panel').style.display = 'none';
  document.getElementById('projection-panel').style.display = 'none';

  document.getElementById('prev-month').addEventListener('click', () => {
    selectedMonth = shiftMonth(selectedMonth, -1);
    refresh().catch(console.error);
  });
  document.getElementById('next-month').addEventListener('click', () => {
    if (selectedMonth < latestMonth) {
      selectedMonth = shiftMonth(selectedMonth, 1);
      refresh().catch(console.error);
    }
  });
}

document.querySelectorAll('#period-bar button').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#period-bar button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentPeriod = btn.dataset.p;
    // Brief fade-out on all panels for visual feedback.
    document.querySelectorAll('.panel').forEach(p => {
      p.classList.remove('fading');
      void p.offsetWidth; // force reflow
      p.classList.add('fading');
    });
    setTimeout(() => {
      document.querySelectorAll('.panel').forEach(p => p.classList.remove('fading'));
      refresh().catch(err => {
        console.error(err);
        document.getElementById('subtitle').textContent = 'Error: ' + err.message;
      });
    }, 180);
  });
});

refresh().catch(err => {
  console.error(err);
  document.getElementById('subtitle').textContent = 'Error: ' + err.message;
});
setInterval(() => refresh().catch(() => {}), 60000);

document.getElementById('fx-add-btn').addEventListener('click', fxToggleAdd);
document.getElementById('fx-cancel').addEventListener('click', () => {
  document.getElementById('fixed-add-row').style.display = 'none';
  document.getElementById('fx-error').style.display = 'none';
});
document.getElementById('fx-save').addEventListener('click', fxCreate);
document.getElementById('fx-name').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') fxCreate();
});
document.getElementById('fx-amount').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') fxCreate();
});
document.getElementById('all-sort').addEventListener('change', () => {
  if (currentAllData) renderAllExpenses(currentAllData);
});
document.querySelectorAll('[data-close-category-modal]').forEach(el => {
  el.addEventListener('click', closeCategoryModal);
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeCategoryModal();
});
</script>
</body>
</html>
"""
