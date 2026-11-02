"""FastAPI router exposing the dashboard UI and JSON endpoints.

All endpoints scope data to the single configured ``telegram_allowed_user_id``
since the bot is single-user. Bind to ``127.0.0.1`` only (see docker-compose).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import re
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


def _recurring_to_payload(item) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "amount": str(item.amount),
        "currency": item.currency,
        "is_active": item.is_active,
        "is_obligation": item.is_obligation,
        "next_due_date": item.next_due_date.isoformat(),
        "obligation_due_date": (
            item.obligation_due_date.isoformat() if item.obligation_due_date else None
        ),
    }


@router.get("/api/recurring")
async def api_recurring_templates(user_id: int = Depends(_current_user_id)):
    from app.recurring.repository import RecurringRepository
    from app.recurring.service import RecurringService

    with session_scope() as session:
        items = RecurringService(RecurringRepository(session)).list(user_id)
        return {"items": [_recurring_to_payload(item) for item in items]}


@router.patch("/api/recurring/{recurring_id}/obligation")
async def api_recurring_obligation(
    recurring_id: int,
    payload: dict,
    user_id: int = Depends(_current_user_id),
):
    from app.recurring.repository import RecurringRepository
    from app.recurring.service import RecurringService

    if set(payload) != {"is_obligation"} or type(payload["is_obligation"]) is not bool:
        raise HTTPException(status_code=400, detail="is_obligation debe ser true o false.")
    with session_scope() as session:
        item = RecurringService(RecurringRepository(session)).set_obligation(
            user_id, recurring_id, payload["is_obligation"]
        )
        if item is None:
            raise HTTPException(status_code=404, detail="Recurrente no encontrado.")
        return {"item": _recurring_to_payload(item)}


@router.get("/api/fixed-expenses")
async def api_fixed_expenses(
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    with session_scope() as s:
        bills = fixed_expenses_current_month(s, user_id, today)
    return {"items": bills}


@router.post("/api/fixed-expenses/repair-mirrors")
async def api_repair_fixed_expense_mirrors(
    user_id: int = Depends(_current_user_id),
):
    from app.fixed_expenses.repository import FixedExpenseRepository
    from app.fixed_expenses.service import FixedExpenseService, MirrorRepairError

    try:
        with session_scope() as session:
            result = FixedExpenseService(
                FixedExpenseRepository(session)
            ).repair_mirrors(user_id)
    except MirrorRepairError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "mirror_repair_ambiguous", "message": str(exc)},
        ) from exc
    return {
        "scanned": result.scanned,
        "repaired": result.repaired,
        "remaining_violations": result.remaining_violations,
    }


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
    month_year: str | None = Query(default=None),
):
    if month_year is not None and (
        not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month_year)
        or month_year.startswith("0000-")
    ):
        raise HTTPException(status_code=400, detail="Usá month_year=YYYY-MM.")
    with session_scope() as s:
        return month_summary_dashboard(s, user_id, today, month_year=month_year)


def _parse_cycle_currency(value) -> str:
    """Validate before resolving a cycle: a typo must not create history."""
    if isinstance(value, str):
        value = value.strip()
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z]{3}", value):
        raise HTTPException(status_code=400, detail={
            "code": "invalid_currency", "message": "Usá un código de moneda de tres letras, como ARS o USD.",
        })
    return value.upper()


def _cycle_currency(currency: str = Query(default="ARS")) -> str:
    return _parse_cycle_currency(currency)


def _payday_to_payload(config) -> dict:
    rule = getattr(config, "pay_rule", "fixed_day")
    weekday = getattr(config, "payday_weekday", None)
    days = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
    label = (
        f"primer {days[weekday]} de cada mes"
        if rule == "first_weekday" else f"día {config.payday_day} de cada mes"
    )
    return {
        "currency": getattr(config, "currency", "ARS"),
        "payday_day": config.payday_day,
        "pay_rule": rule,
        "payday_weekday": weekday,
        "payday_ordinal": 1 if rule == "first_weekday" else None,
        "payday_label": label,
    }


@router.get("/api/settings/payday")
async def api_payday_settings(
    user_id: int = Depends(_current_user_id),
    currency: str = Depends(_cycle_currency),
):
    from app.money import UserSettingsService

    with session_scope() as session:
        config = UserSettingsService(session).get_settings(user_id, currency=currency)
        return {"payday": {**_payday_to_payload(config), "inherited": getattr(config, "inherited", False)}}


@router.patch("/api/settings/payday")
async def api_update_payday_settings(
    payload: dict,
    user_id: int = Depends(_current_user_id),
    settings: Settings = Depends(get_settings),
    currency: str | None = Query(default=None),
):
    from app.money import UserSettingsService

    allowed = {"pay_rule", "payday_day", "payday_weekday", "currency"}
    if not payload or set(payload) - allowed:
        raise HTTPException(status_code=400, detail="Configuración de cobro inválida.")
    body_currency = payload.get("currency")
    if currency is not None and body_currency is not None and (
        _parse_cycle_currency(currency) != _parse_cycle_currency(body_currency)
    ):
        raise HTTPException(status_code=400, detail="Las monedas de la URL y el cuerpo no coinciden.")
    currency = _parse_cycle_currency(body_currency if "currency" in payload else (currency or "ARS"))
    rule = payload.get("pay_rule", "fixed_day")
    try:
        with session_scope() as session:
            config = UserSettingsService(session).set_payday_rule(
                user_id, rule, payday_day=payload.get("payday_day"),
                weekday=payload.get("payday_weekday"), timezone=settings.timezone,
                currency=currency,
            )
            return {
                "payday": {**_payday_to_payload(config), "inherited": getattr(config, "inherited", False)},
                "message": "Calendario guardado. Los ciclos abiertos conservan sus fechas; el cambio se aplica al próximo ciclo.",
            }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Usá un día fijo de 1 a 31 o el primer día de semana elegido (0=lunes, 6=domingo).") from exc


@router.get("/api/cycle/current")
async def api_cycle_current(
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
    currency: str = Depends(_cycle_currency),
):
    """Return the open financial cycle that contains today, or null."""
    from app.money import CycleService

    with session_scope() as s:
        view = CycleService(s).get_current_cycle(user_id, today, currency=currency)
    if view is None:
        return {"cycle": None}
    return {
        "cycle": {
            "id": view.cycle_id,
            "cycle_index": view.cycle_index,
            "start_date": view.start_date.isoformat(),
            "end_date": view.end_date.isoformat(),
            **_payday_to_payload(view),
            "status": view.status,
        }
    }


@router.post("/api/cycle/open")
async def api_cycle_open(
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
    currency: str = Depends(_cycle_currency),
):
    """Open the initial cycle for the user.

    Returns the cycle view on success, raises 409 when the user already
    has history that requires rollover (R2 territory).
    """
    from app.money import CycleRolloverRequired, CycleService

    try:
        with session_scope() as s:
            view = CycleService(s).open_initial_cycle(user_id, today, currency=currency)
    except CycleRolloverRequired as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "cycle_rollover_required",
                "message": (
                    "Ya existe historial. Usá la acción de actualizar ciclo."
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
            **_payday_to_payload(view),
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
        **_payday_to_payload(summary),
        "legacy_mixed_currency": getattr(summary, "legacy_mixed_currency", False),
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
            **_payday_to_payload(summary),
            "status": summary.status,
        }
    # MoneyByCurrency omits zero entries. Preserve that domain contract while
    # making zero explicit for currencies this summary actually contains.
    # A null daily allowance remains unavailable; it must never become zero.
    money = payload["money"]
    currencies = sorted({
        currency
        for values in money.values()
        if isinstance(values, dict)
        for currency in values
    })
    for values in money.values():
        if isinstance(values, dict):
            for currency in currencies:
                values.setdefault(currency, "0.00")
    return payload


def _cycle_lifecycle_http_error(exc: Exception) -> HTTPException:
    from app.money import CycleCatchUpLimitExceeded, SnapshotVersionMismatchError

    if isinstance(exc, SnapshotVersionMismatchError):
        code, message = "unsupported_snapshot_version", "El historial usa un formato no compatible."
    elif isinstance(exc, CycleCatchUpLimitExceeded):
        code, message = "cycle_catch_up_limit_exceeded", "Hay demasiados ciclos pendientes de actualizar."
    else:
        code, message = "cycle_not_current", "No hay un ciclo abierto para hoy. El ciclo anterior ya cerró o el siguiente todavía no comenzó."
    return HTTPException(status_code=409, detail={"code": code, "message": message})


@router.get("/api/cycle-summary")
async def api_cycle_summary(
    user_id: int = Depends(_current_user_id),
    cycle_id: int | None = Query(default=None),
    today: date = Depends(_today),
    currency: str = Depends(_cycle_currency),
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
        SnapshotVersionMismatchError,
    )

    with session_scope() as s:
        cycle_service = CycleService(s)
        engine = MoneyEngine(s)
        if cycle_id is None:
            try:
                cycle = cycle_service.ensure_current_cycle(user_id, today, currency=currency)
            except (CycleCatchUpLimitExceeded, LookupError, SnapshotVersionMismatchError) as exc:
                raise _cycle_lifecycle_http_error(exc) from exc
            cycle_id = cycle.id
        try:
            summary = engine.get_cycle_summary(user_id, cycle_id, today=today)
        except SnapshotVersionMismatchError as exc:
            raise _cycle_lifecycle_http_error(exc) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail={
                "code": "cycle_not_found", "message": "Ciclo no encontrado."
            }) from exc
        return _summary_to_payload(summary)


@router.get("/api/cycle-history")
async def api_cycle_history(
    user_id: int = Depends(_current_user_id),
    limit: int = Query(100, ge=1, le=500),
    currency: str = Depends(_cycle_currency),
):
    """Read closed cycles solely from their immutable v1 snapshots."""
    from sqlalchemy import select

    from app.money.models import FinancialCycle
    from app.money.repository import cycle_currency_scope
    from app.money.engine import SnapshotVersionMismatchError
    from app.money.snapshot import ensure_snapshot_version

    with session_scope() as session:
        cycles = session.execute(
            select(FinancialCycle).where(
                FinancialCycle.telegram_user_id == user_id,
                FinancialCycle.status == "closed",
                cycle_currency_scope(currency),
            ).order_by(FinancialCycle.cycle_index.desc()).limit(limit)
        ).scalars().all()
        engine = MoneyEngine(session)
        try:
            items = []
            for cycle in cycles:
                ensure_snapshot_version(cycle.closing_snapshot)
                items.append(_summary_to_payload(
                    engine.get_cycle_summary(user_id, cycle.id)
                ))
        except SnapshotVersionMismatchError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "unsupported_snapshot_version",
                    "message": str(exc),
                },
            ) from exc
    return {"items": items}


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
    currency: str = Depends(_cycle_currency),
):
    """Run ``ensure_current_cycle(today)``. Idempotent."""
    from app.money import (
        CycleCatchUpLimitExceeded,
        CycleService,
        SnapshotVersionMismatchError,
    )

    with session_scope() as s:
        cycle_service = CycleService(s)
        try:
            cycle = cycle_service.ensure_current_cycle(user_id, today, currency=currency)
        except (CycleCatchUpLimitExceeded, LookupError, SnapshotVersionMismatchError) as exc:
            raise _cycle_lifecycle_http_error(exc) from exc
    return {
        "cycle": {
            "id": cycle.id,
            "cycle_index": cycle.cycle_index,
            "start_date": cycle.start_date.isoformat(),
            "end_date": cycle.end_date.isoformat(),
            **_payday_to_payload(cycle),
            "status": cycle.status,
        }
    }


# ---------------------------------------------------------------------------
# R3 — income / savings mutations
# ---------------------------------------------------------------------------


def _resolve_open_cycle(
    user_id: int, cycle_id: int | None, today: date, *, currency: str = "ARS"
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
        SnapshotVersionMismatchError,
    )

    with session_scope() as s:
        cycle_service = CycleService(s)
        if cycle_id is None:
            try:
                return cycle_service.ensure_current_cycle(user_id, today, currency=currency)
            except (CycleCatchUpLimitExceeded, LookupError, SnapshotVersionMismatchError) as exc:
                raise _cycle_lifecycle_http_error(exc) from exc
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
        if cycle.currency != currency:
            raise HTTPException(status_code=409, detail={
                "code": "cycle_currency_mismatch",
                "message": "La moneda no coincide con la del ciclo elegido.",
            })
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


def _parse_amount(raw, *, positive: bool = False) -> Decimal:
    """Validate a monetary input before any implicit cycle lifecycle change."""
    try:
        value = raw if isinstance(raw, Decimal) else Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail={
            "code": "invalid_amount", "message": "Ingresá un monto numérico válido.",
        }) from exc
    if not value.is_finite() or value < 0 or (positive and value == 0):
        raise HTTPException(status_code=400, detail={
            "code": "invalid_argument",
            "message": "El monto debe ser finito y mayor que cero." if positive else "El monto debe ser finito y no negativo.",
        })
    return value


def _parse_cycle_id(raw) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not re.fullmatch(r"[1-9][0-9]*", str(raw)):
        raise HTTPException(status_code=400, detail={"code": "invalid_cycle_id", "message": "El ciclo debe ser un identificador entero positivo."})
    return int(raw)


def _state_error_to_http(exc: Exception) -> HTTPException:
    from app.money import (
        CycleClosedError,
        CycleCurrencyMismatchError,
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
    if isinstance(exc, CycleCurrencyMismatchError):
        return HTTPException(status_code=409, detail={"code": "cycle_currency_mismatch", "message": str(exc)})
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


def _validate_money_source_currency(user_id: int, source_key, currency: str, *, kind: str) -> None:
    """Reject a replay in another currency before opening an unrelated cycle."""
    from app.money import MoneyStateService

    try:
        with session_scope() as session:
            MoneyStateService(session).validate_source_currency(
                user_id, source_key, currency, kind=kind,
            )
    except Exception as exc:
        raise _state_error_to_http(exc) from exc


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

    currency = _parse_cycle_currency(payload.get("currency", "ARS"))

    amount = _parse_amount(payload.get("amount"))
    cycle_id = payload.get("cycle_id")
    cycle = _resolve_open_cycle(
        user_id,
        _parse_cycle_id(cycle_id),
        today,
        currency=currency,
    )
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

    currency = _parse_cycle_currency(payload.get("currency", "ARS"))
    source_key = payload.get("source_key")
    _validate_money_source_currency(user_id, source_key, currency, kind="extra")

    amount = _parse_amount(payload.get("amount"), positive=True)
    note = payload.get("note")
    cycle_id = payload.get("cycle_id")
    cycle = _resolve_open_cycle(
        user_id,
        _parse_cycle_id(cycle_id),
        today,
        currency=currency,
    )
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

    currency = _parse_cycle_currency(payload.get("currency", "ARS"))

    target_pct = payload.get("target_pct")
    target_amount = payload.get("target_amount")
    pct_decimal = _parse_amount(target_pct) if target_pct is not None else None
    amt_decimal = _parse_amount(target_amount) if target_amount is not None else None
    if (pct_decimal is None) == (amt_decimal is None):
        raise HTTPException(status_code=400, detail={"code": "invalid_argument", "message": "Usá exactamente uno de target_pct o target_amount."})
    if pct_decimal is not None and pct_decimal > 1:
        raise HTTPException(status_code=400, detail={"code": "invalid_argument", "message": "target_pct debe estar entre 0 y 1."})

    cycle_id = payload.get("cycle_id")
    cycle = _resolve_open_cycle(
        user_id,
        _parse_cycle_id(cycle_id),
        today,
        currency=currency,
    )
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

    currency = _parse_cycle_currency(payload.get("currency", "ARS"))

    amount = _parse_amount(payload.get("amount"))
    cycle_id = payload.get("cycle_id")
    cycle = _resolve_open_cycle(
        user_id,
        _parse_cycle_id(cycle_id),
        today,
        currency=currency,
    )
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

    currency = _parse_cycle_currency(payload.get("currency", "ARS"))

    amount = _parse_amount(payload.get("amount"), positive=True)
    cycle_id = payload.get("cycle_id")
    cycle = _resolve_open_cycle(
        user_id,
        _parse_cycle_id(cycle_id),
        today,
        currency=currency,
    )
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

    currency = _parse_cycle_currency(payload.get("currency", "ARS"))
    source_key = payload.get("source_key")
    _validate_money_source_currency(user_id, source_key, currency, kind="savings_realized")

    amount = _parse_amount(payload.get("amount"), positive=True)
    note = payload.get("note")
    cycle_id = payload.get("cycle_id")
    cycle = _resolve_open_cycle(
        user_id,
        _parse_cycle_id(cycle_id),
        today,
        currency=currency,
    )
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

    <div class="panel" id="active-cycle-panel">
      <h2>Ciclo activo</h2>
      <label style="display:flex;align-items:center;gap:10px;margin-bottom:14px">Moneda del ciclo
        <input id="cycle-currency" class="fx-input" value="ARS" maxlength="3" pattern="[A-Za-z]{3}" list="cycle-currencies" style="width:90px" onchange="changeCycleCurrency()" aria-label="Moneda del ciclo">
        <datalist id="cycle-currencies"><option value="ARS"><option value="USD"><option value="EUR"></datalist>
      </label>
      <div id="active-cycle">Cargando…</div>
      <details style="margin-top:18px;padding-top:14px;border-top:1px solid var(--glass-border)">
        <summary style="cursor:pointer">Configurar calendario de cobro</summary>
        <p id="payday-current-setting" class="subtitle" style="margin-top:10px">Cargando configuración…</p>
        <form id="payday-form" onsubmit="savePaydaySettings(event)" style="display:flex;flex-wrap:wrap;gap:12px;align-items:end;margin-top:12px">
          <label style="flex:1 1 180px">Calendario
            <select id="payday-rule" class="fx-input" onchange="updatePaydayFields()">
              <option value="fixed_day">Día fijo del mes</option>
              <option value="first_weekday">Primer día de semana del mes</option>
            </select>
          </label>
          <label id="payday-day-field" style="flex:1 1 100px">Día del mes
            <input id="payday-day" class="fx-input" type="number" min="1" max="31" step="1" value="1" required>
          </label>
          <label id="payday-weekday-field" hidden style="flex:1 1 140px">Día de semana
            <select id="payday-weekday" class="fx-input" disabled>
              <option value="0">Lunes</option><option value="1">Martes</option>
              <option value="2">Miércoles</option><option value="3">Jueves</option>
              <option value="4" selected>Viernes</option><option value="5">Sábado</option>
              <option value="6">Domingo</option>
            </select>
          </label>
          <button id="payday-save" type="submit" class="fx-btn fx-btn-primary">Guardar calendario</button>
        </form>
        <p id="payday-status" class="subtitle" role="status" style="margin-top:10px">Los ciclos ya abiertos conservan sus fechas. Guardar no abre un ciclo ni registra ingresos.</p>
      </details>
    </div>

    <div class="row">
       <div class="panel" id="recurring-panel">
        <h2 id="trend-title">Tendencia</h2>
        <div class="chart-wrap"><canvas id="trend-chart"></canvas></div>
      </div>
      <div class="panel">
        <h2 id="category-title">Por categoría</h2>
        <div id="category-charts"></div>
      </div>
    </div>

    <div class="row">
      <div class="panel">
        <h2 id="budgets-title">Presupuestos</h2>
        <div id="budgets-list"></div>
      </div>
      <div class="panel">
        <h2>Recurrentes</h2>
        <div id="recurring-list"></div>
      </div>
    </div>

    <div class="panel" id="fixed-panel">
      <div style="display:flex; justify-content:space-between; align-items:flex-end; margin-bottom:14px; flex-wrap:wrap; gap:10px">
        <div>
          <h2 id="fixed-title" style="margin-bottom:2px">Gastos fijos</h2>
          <div class="subtitle" id="fixed-subtitle">Cargando…</div>
        </div>
      </div>
      <p class="subtitle" style="margin-bottom:14px">El disponible y el ahorro se calculan en el ciclo de cada moneda. Registrá ingresos con /ingreso &lt;monto&gt; [moneda] y extras con /extra &lt;monto&gt; [moneda].</p>
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
let trendChart;
let categoryCharts = [];
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

function selectedCycleCurrency() {
  return document.getElementById('cycle-currency').value.trim().toUpperCase();
}

async function changeCycleCurrency() {
  const currency = selectedCycleCurrency();
  if (!/^[A-Z]{3}$/.test(currency)) {
    document.getElementById('active-cycle').textContent = 'Usá una moneda de tres letras, como ARS o USD.';
    return;
  }
  document.getElementById('cycle-currency').value = currency;
  document.getElementById('payday-status').textContent = 'Los ciclos ya abiertos conservan sus fechas. Guardar no abre un ciclo ni registra ingresos.';
  await Promise.all([refreshPaydaySettings(), refreshActiveCycle()]);
}

function updatePaydayFields() {
  const fixed = document.getElementById('payday-rule').value === 'fixed_day';
  document.getElementById('payday-day-field').hidden = !fixed;
  document.getElementById('payday-day').disabled = !fixed;
  document.getElementById('payday-weekday-field').hidden = fixed;
  document.getElementById('payday-weekday').disabled = fixed;
}

function setPaydayLoading(loading) {
  const form = document.getElementById('payday-form');
  form.setAttribute('aria-busy', String(loading));
  form.querySelectorAll('input,select,button').forEach(control => { control.disabled = loading; });
  if (!loading) updatePaydayFields();
}

async function refreshPaydaySettings() {
  const currency = selectedCycleCurrency();
  setPaydayLoading(true);
  document.getElementById('payday-current-setting').textContent = 'Cargando calendario ' + currency + '…';
  try {
    const {payday} = await getJSON('/api/settings/payday?currency=' + encodeURIComponent(currency));
    if (currency !== selectedCycleCurrency()) return;
    document.getElementById('payday-rule').value = payday.pay_rule;
    document.getElementById('payday-day').value = payday.payday_day;
    document.getElementById('payday-weekday').value = payday.payday_weekday == null ? 4 : payday.payday_weekday;
    document.getElementById('payday-current-setting').textContent = 'Configuración ' + currency + ': ' + payday.payday_label + (payday.inherited ? ' (heredada de ARS hasta guardar o abrir el primer ciclo).' : '.');
    updatePaydayFields();
  } catch (err) {
    if (currency === selectedCycleCurrency()) {
      document.getElementById('payday-current-setting').textContent = 'No se pudo cargar el calendario de cobro.';
    }
  } finally {
    if (currency === selectedCycleCurrency()) setPaydayLoading(false);
  }
}

async function savePaydaySettings(event) {
  event.preventDefault();
  const status = document.getElementById('payday-status');
  const rule = document.getElementById('payday-rule').value;
  const payload = {pay_rule: rule, currency: selectedCycleCurrency()};
  if (rule === 'fixed_day') payload.payday_day = Number(document.getElementById('payday-day').value);
  else payload.payday_weekday = Number(document.getElementById('payday-weekday').value);
  setPaydayLoading(true);
  document.getElementById('cycle-currency').disabled = true;
  try {
    const response = await fetch('/api/settings/payday', {
      method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
    });
    if (!response.ok) throw new Error('payday settings ' + response.status);
    const result = await response.json();
    await refreshPaydaySettings();
    await refreshActiveCycle();
    status.textContent = result.message;
  } catch (err) {
    status.textContent = 'No se pudo guardar el calendario. Revisá los valores y volvé a intentarlo.';
  } finally {
    setPaydayLoading(false);
    document.getElementById('cycle-currency').disabled = false;
  }
}

async function refreshActiveCycle() {
  const target = document.getElementById('active-cycle');
  const currency = selectedCycleCurrency();
  try {
    const current = await getJSON('/api/cycle/current?currency=' + encodeURIComponent(currency));
    if (currency !== selectedCycleCurrency()) return;
    if (!current.cycle) {
      target.innerHTML = '<p>No hay un ciclo activo para hoy en ' + escapeHTML(currency) + '.</p>' +
        '<button type="button" onclick="ensureActiveCycle(this)">Abrir o actualizar ciclo</button>';
      return;
    }
    const summary = await getJSON('/api/cycle-summary?cycle_id=' + encodeURIComponent(current.cycle.id));
    if (currency !== selectedCycleCurrency()) return;
    const money = summary.money || {};
    const fields = [
      ['income', 'Ingreso base'], ['extra', 'Ingresos extra'],
      ['gross_income', 'Ingreso bruto'], ['savings_target', 'Meta de ahorro'],
      ['savings_reserved', 'Ahorro reservado'], ['savings_realized', 'Ahorro realizado'],
      ['savings_committed', 'Ahorro comprometido'], ['realized_expenses', 'Gastos realizados'],
      ['pending_reserved_obligations', 'Obligaciones pendientes'],
      ['available_to_spend', 'Disponible'], ['daily_safe_to_spend', 'Gasto diario seguro'],
    ];
    const currencies = new Set(fields.flatMap(([key]) => Object.keys(money[key] || {})));
    const range = escapeHTML(summary.currency || currency) + ' · ' + escapeHTML(summary.start_date) + ' → ' + escapeHTML(summary.end_date);
    const calendar = '<p class="subtitle">Cobro del ciclo: ' + escapeHTML(summary.payday_label) + '.</p>';
    if (!currencies.size) {
      target.innerHTML = '<p>' + range + '</p>' + calendar + '<p>Aún no hay ingresos ni movimientos en este ciclo.</p>';
      return;
    }
    const incomeState = (money.warnings || []).includes('no_income_recorded')
      ? '<p>Aún no registraste ingresos en este ciclo.</p>' : '';
    target.innerHTML = '<p>' + range + '</p>' + calendar + incomeState + '<div class="grid">' +
      [...currencies].sort().map(currency =>
        '<div class="panel"><h3>' + escapeHTML(currency) + '</h3>' +
        fields.map(([key, label]) => {
          const value = money[key] && money[key][currency];
          return '<div style="display:flex;justify-content:space-between;gap:12px;margin:6px 0">' +
            '<span>' + label + '</span><strong style="white-space:nowrap">' +
            (value == null ? 'No disponible' : escapeHTML(value) + ' ' + escapeHTML(currency)) +
            '</strong></div>';
        }).join('') + '</div>'
      ).join('') + '</div>';
  } catch (err) {
    if (currency === selectedCycleCurrency()) target.textContent = 'No se pudo cargar el ciclo activo.';
  }
}

async function ensureActiveCycle(button) {
  const currency = selectedCycleCurrency();
  button.disabled = true;
  try {
    const response = await fetch('/api/cycle/ensure?currency=' + encodeURIComponent(currency), {method: 'POST'});
    if (!response.ok) throw new Error('cycle ensure ' + response.status);
    await refreshActiveCycle();
  } catch (err) {
    if (currency === selectedCycleCurrency()) document.getElementById('active-cycle').textContent =
      'No se pudo actualizar el ciclo. Volvé a intentarlo.';
  } finally {
    button.disabled = false;
  }
}

async function toggleRecurringObligation(id, enabled, button) {
  button.disabled = true;
  try {
    const response = await fetch('/api/recurring/' + encodeURIComponent(id) + '/obligation', {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({is_obligation: enabled}),
    });
    if (!response.ok) throw new Error('recurring obligation ' + response.status);
    await refresh();
  } catch (err) {
    button.textContent = 'No se pudo guardar. Reintentar';
  } finally {
    button.disabled = false;
  }
}

async function refresh() {
  if (!monthlyDashboard) refreshActiveCycle();
  const subtitle = document.getElementById('subtitle');
  subtitle.classList.add('refreshing');

  const periodParam = '?period=' + currentPeriod;
  let summary, trend, recent, budgets, recurring, comparison, projection, fixed, all;
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
      all = monthly.expenses;
      updateMonthNav(monthly);
    } else {
      [summary, trend, recent, budgets, recurring, comparison, projection, fixed, all] =
        await Promise.all([
          getJSON('/api/summary' + periodParam),
          getJSON('/api/trend' + periodParam),
          getJSON('/api/recent?limit=10'),
          getJSON('/api/budgets' + periodParam),
          getJSON('/api/recurring'),
          getJSON('/api/comparison' + periodParam),
          getJSON('/api/projection' + periodParam),
          getJSON('/api/fixed-expenses'),
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
  const trendSeries = Object.entries(trend.points_by_currency || {});
  const timeline = trendSeries.length ? trendSeries[0][1] : (trend.points || []);
  const trendColors = palette(trendSeries.length);
  trendChart = new Chart(document.getElementById('trend-chart'), {
    type: 'line',
    data: {
      labels: timeline.map(p => p.date.slice(5)),
      datasets: trendSeries.map(([cur, points], index) => ({
        label: cur,
        data: points.map(p => p.amount),
        borderColor: trendColors[index],
        backgroundColor: trendColors[index] + '22',
        borderWidth: 2,
        fill: true,
        tension: 0.4,
        pointRadius: 0,
        pointHoverRadius: 5,
        pointHoverBackgroundColor: '#34d399',
        pointHoverBorderColor: '#fff',
        pointHoverBorderWidth: 2,
      })),
    },
    options: chartOpts(),
  });

  // Category pie/donut
  categoryCharts.forEach(chart => chart.destroy());
  categoryCharts = [];
  const categoryTarget = document.getElementById('category-charts');
  const categoryGroups = Object.entries(summary.by_category_by_currency || {});
  categoryTarget.innerHTML = categoryGroups.length ? categoryGroups.map(([cur], index) =>
    '<h3>' + escapeHTML(cur) + '</h3><div class="chart-wrap tall"><canvas id="category-chart-' + index + '"></canvas></div>'
  ).join('') : '<div class="subtitle">Sin gastos en este período.</div>';
  categoryGroups.forEach(([cur, cats], index) => {
    categoryCharts.push(new Chart(document.getElementById('category-chart-' + index), {
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
    }));
  });

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
    rDiv.innerHTML = '<div class="subtitle">No hay recurrentes activos. Podés crearlos desde Telegram.</div>';
  } else {
    rDiv.innerHTML = recurring.items.map(r =>
      `<div class="budget-row">
         <div><strong>${escapeHTML(r.name)}</strong> — ${currency(r.amount, r.currency)}</div>
         <div class="subtitle">Próximo aviso: ${escapeHTML(r.next_due_date)} · ${r.is_obligation ? 'Descuenta del disponible' : 'Solo recordatorio'}</div>
         ${r.is_obligation && r.obligation_due_date ? '<div class="subtitle">Compromiso: ' + escapeHTML(r.obligation_due_date) + '</div>' : ''}
         <button type="button" aria-label="${r.is_obligation ? 'Quitar' : 'Descontar'} ${escapeHTML(r.name)} del disponible"
           onclick="toggleRecurringObligation(${r.id}, ${!r.is_obligation}, this)">${r.is_obligation ? 'Quitar del disponible' : 'Descontar del disponible'}</button>
       </div>`).join('');
  }

  // Comparison uses server-produced amounts and percentages per currency.
  const cDiv = document.getElementById('comparison-list');
  const comparisonGroups = Object.entries(comparison.by_category_by_currency || {});
  cDiv.innerHTML = comparisonGroups.length ? comparisonGroups.map(([cur, items]) => {
    const delta = comparison.delta_pct_by_currency[cur];
    const rows = items.slice(0, 6).map(row => `<tr>
      <td>${escapeHTML(row.category)}</td>
      <td class="amount">${currency(row.current, cur)}</td>
      <td class="amount">${currency(row.previous, cur)}</td>
      <td>${row.diff_pct == null ? '—' : fmt.format(row.diff_pct) + '%'}</td>
    </tr>`).join('');
    return `<section style="margin-bottom:20px">
      <h3>${escapeHTML(cur)}</h3>
      <div>${escapeHTML(comparison.current.label)}: ${currency(comparison.current_total_by_currency[cur] || 0, cur)}</div>
      <div>${escapeHTML(comparison.previous.label)}: ${currency(comparison.previous_total_by_currency[cur] || 0, cur)}</div>
      <div class="subtitle">Variación: ${delta == null ? 'Sin base de comparación' : fmt.format(delta) + '%'}</div>
      <table style="margin-top:12px"><thead><tr><th>Categoría</th><th>Actual</th><th>Anterior</th><th>Δ</th></tr></thead>
        <tbody>${rows}</tbody></table>
    </section>`;
  }).join('') : '<div class="subtitle">Sin datos comparables.</div>';

  // Projection values also come from the server, with no mixed-currency total.
  const pDiv = document.getElementById('projection-card');
  const projectionCurrencies = Object.keys(projection.projected_total_by_currency || {});
  if (projection.applies === false) {
    pDiv.innerHTML = '<div class="subtitle">La proyección aplica solo a períodos en curso (semana, mes, año).</div>';
  } else if (!projectionCurrencies.length) {
    pDiv.innerHTML = '<div class="subtitle">Sin datos suficientes para proyectar.</div>';
  } else {
    pDiv.innerHTML = projectionCurrencies.map(cur => {
      const delta = projection.delta_vs_previous_pct_by_currency[cur];
      return `<section style="margin-bottom:20px">
        <h3>${escapeHTML(cur)}</h3>
        <div class="subtitle">Gastado</div>
        <div class="big" style="font-size:28px">${currency(projection.spent_by_currency[cur] || 0, cur)}</div>
        <div class="subtitle">Día ${projection.days_elapsed} de ${projection.days_in_period}</div>
        <div class="subtitle" style="margin-top:14px">Proyección fin de período</div>
        <div class="big" style="color:var(--accent);font-size:28px">${currency(projection.projected_total_by_currency[cur], cur)}</div>
        <div class="subtitle">${delta == null ? 'Sin base de comparación' : fmt.format(delta) + '% vs período anterior'}</div>
      </section>`;
    }).join('');
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
  if (!monthlyDashboard) renderFixedExpenses(fixed);

  // All-expenses panel
  renderAllExpenses(all);
}

function renderFixedExpenses(fixed) {
  const sub = document.getElementById('fixed-subtitle');
  const items = fixed.items || [];
  if (items.length === 0) {
    sub.textContent = 'No tenés gastos fijos. Cargalos desde Telegram: /gastofijo_add CASA 90000 10 transferencia';
    document.getElementById('fixed-list').innerHTML = '';
    return;
  }
  const paid = items.filter(i => i.status === 'paid_exact' || i.status === 'paid_more' || i.status === 'paid_less').length;
  sub.textContent = `${paid}/${items.length} pagados este mes · importes separados por moneda`;

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
  document.getElementById('active-cycle-panel').style.display = 'none';
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

if (!monthlyDashboard) refreshPaydaySettings();
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
