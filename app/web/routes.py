"""FastAPI router exposing the dashboard UI and JSON endpoints.

All endpoints scope data to the single configured ``telegram_allowed_user_id``
since the bot is single-user. Bind to ``127.0.0.1`` only (see docker-compose).
"""
from __future__ import annotations

from datetime import date
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
      padding: 12px 0;
      border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    }
    .budget-row:last-child { border: none; }

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
      <h2 id="recent-title">Últimos gastos</h2>
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
  let summary, trend, recent, budgets, recurring, comparison, projection;
  try {
    [summary, trend, recent, budgets, recurring, comparison, projection] =
      await Promise.all([
        getJSON('/api/summary' + periodParam),
        getJSON('/api/trend' + periodParam),
        getJSON('/api/recent?limit=10'),
        getJSON('/api/budgets' + periodParam),
        getJSON('/api/recurring/upcoming'),
        getJSON('/api/comparison' + periodParam),
        getJSON('/api/projection' + periodParam),
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
        <div class="big" data-value="${prev ?? 0}">$${fmt.format(firstLoad ? v : v)}</div>
        <div style="color:var(--muted); font-size:12px; margin-top:6px">
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
      <div class="budget-row">
        <div><strong>${comparison.current.label}</strong>
             ${currency(comparison.current.total, 'ARS')}</div>
        <div><strong>${comparison.previous.label}</strong>
             ${currency(comparison.previous.total, 'ARS')}</div>
        <div class="${dClass}" style="font-size:26px; font-weight:600; margin-top:10px">
          ${dArrow} ${dSign}${comparison.delta_pct == null ? '—' : comparison.delta_pct.toFixed(1) + '%'}
        </div>
      </div>`;
    const rows = comparison.by_category.slice(0, 8).map(r => {
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
    cDiv.innerHTML = top + `
      <table style="margin-top:14px">
        <thead><tr><th>Categoría</th>
                   <th style="text-align:right">${comparison.current.label}</th>
                   <th style="text-align:right">${comparison.previous.label}</th>
                   <th style="text-align:right">Δ</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
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
      <div class="budget-row">
        <div><strong>Gastado</strong></div>
        <div class="big" style="margin-bottom:8px; font-size:32px">$${fmt.format(projection.spent)}</div>
        <div class="subtitle">
          Día ${projection.days_elapsed} de ${projection.days_in_period}
        </div>
        <div style="margin-top:18px"><strong>Proyección fin de período</strong></div>
        <div class="big" style="color:var(--accent); font-size:32px">$${fmt.format(projection.projected_total)}</div>
        <div class="${dCls}" style="margin-top:10px; font-size:18px; font-weight:500">
          ${arrow} ${sign}${projection.delta_vs_previous_pct == null ? '—' : projection.delta_vs_previous_pct.toFixed(1) + '%'}
          vs período anterior
        </div>
      </div>`;
  }

  // Recent expenses table
  document.getElementById('recent-tbody').innerHTML = recent.items.map(e => `
    <tr>
      <td>${e.date}</td>
      <td>${escapeHTML(e.name)}</td>
      <td>${escapeHTML(e.category)}</td>
      <td class="amount">$${fmt.format(e.amount)} ${e.currency}</td>
    </tr>`).join('');
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
</script>
</body>
</html>
"""
