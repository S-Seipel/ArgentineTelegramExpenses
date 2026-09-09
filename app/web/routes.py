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
from app.utils.dates import today_in_tz
from app.web.queries import (
    budget_status,
    comparison,
    daily_trend,
    fixed_expenses_current_month,
    month_summary_dashboard,
    projection,
    recent_expenses,
    recurring_upcoming,
    resolve_period,
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
    limit: int = Query(500, ge=1, le=2000),
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    """Return ALL expenses for the period (default: current month).

    Used by the dashboard's 'Todos los gastos del mes' panel.
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
        )
    return {
        "period_label": window.label,
        "start": window.start.isoformat(),
        "end": window.end.isoformat(),
        "count": len(items),
        "total": sum(i["amount"] for i in items),
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

    /* ===========================================================
       Background chart colors (Apple-inspired palette)
       =========================================================== */

  </style>
</head>
<body>
  <main>
    <header>
      <h1><span class="emoji">💸</span> Argentine Telegram Expenses</h1>
      <div class="subtitle" id="subtitle">Cargando…</div>
    </header>

    <div class="toolbar" id="period-bar">
      <button data-p="month" class="active">Mes</button>
      <button data-p="week">Semana</button>
      <button data-p="today">Hoy</button>
      <button data-p="last_month">Mes pasado</button>
      <button data-p="year">Año</button>
    </div>

    <div class="grid" id="kpis"></div>

    <div class="row">
      <div class="panel">
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

<script>
const fmt = new Intl.NumberFormat('es-AR');
const currency = (n, c) => '$ ' + fmt.format(n) + ' ' + c;
let trendChart, categoryChart;
let currentPeriod = 'month';
let firstLoad = true;
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
  } catch (err) {
    subtitle.classList.remove('refreshing');
    subtitle.textContent = 'Error: ' + err.message;
    return;
  }

  subtitle.textContent = summary.period_label + ' · ' + summary.start + ' → ' + summary.end;
  subtitle.classList.remove('refreshing');

  document.getElementById('trend-title').textContent = trendTitleFor(currentPeriod);
  document.getElementById('budgets-title').textContent =
    'Presupuestos — ' + summary.period_label.toLowerCase();
  document.getElementById('comparison-title').textContent =
    comparisonTitleFor(currentPeriod);
  document.getElementById('projection-title').textContent =
    'Proyección — ' + summary.period_label.toLowerCase();
  document.getElementById('projection-panel').style.display =
    projectionApplies(currentPeriod) ? '' : 'none';

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

  // Budgets
  const bDiv = document.getElementById('budgets-list');
  if (budgets.items.length === 0) {
    bDiv.innerHTML = '<div class="subtitle">No tenés presupuestos. Creá uno con <code>/presupuesto &lt;categoría&gt; &lt;monto&gt;</code> en Telegram.</div>';
  } else {
    const isMonth = currentPeriod === 'month';
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
  renderFixedExpenses(fixed, monthSum);

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
</script>
</body>
</html>
"""
