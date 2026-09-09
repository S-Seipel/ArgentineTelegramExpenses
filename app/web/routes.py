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
    days: int = Query(30, ge=1, le=365),
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    with session_scope() as s:
        return daily_trend(s, user_id, end=today, days=days)


@router.get("/api/recent")
async def api_recent(
    limit: int = Query(10, ge=1, le=100),
    user_id: int = Depends(_current_user_id),
):
    with session_scope() as s:
        return {"items": recent_expenses(s, user_id, limit)}


@router.get("/api/budgets")
async def api_budgets(
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    with session_scope() as s:
        return {"items": budget_status(s, user_id, today)}


@router.get("/api/comparison")
async def api_comparison(
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    current = resolve_period("month", today)
    previous = resolve_period("last_month", today)
    with session_scope() as s:
        return comparison(s, user_id, current, previous)


@router.get("/api/projection")
async def api_projection(
    user_id: int = Depends(_current_user_id),
    today: date = Depends(_today),
):
    current = resolve_period("month", today)
    previous = resolve_period("last_month", today)
    with session_scope() as s:
        return projection(s, user_id, current, previous)


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
    :root {
      --bg: #0f1419; --panel: #1a1f2e; --text: #e6e6e6;
      --muted: #8b95a5; --accent: #4caf93; --warn: #f5a623; --bad: #e85d75;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: var(--bg); color: var(--text);
           font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           padding: 24px; }
    h1 { font-size: 24px; margin-bottom: 4px; }
    .subtitle { color: var(--muted); margin-bottom: 24px; font-size: 14px; }
    .grid { display: grid; gap: 16px; margin-bottom: 24px;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }
    .panel { background: var(--panel); padding: 20px; border-radius: 12px; }
    .panel h2 { font-size: 14px; color: var(--muted);
                text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
    .big { font-size: 28px; font-weight: 600; }
    .pill { display: inline-block; padding: 2px 10px; border-radius: 12px;
            font-size: 12px; margin-left: 8px; }
    .pill.ok { background: rgba(76, 175, 147, 0.18); color: var(--accent); }
    .pill.warn { background: rgba(245, 166, 35, 0.18); color: var(--warn); }
    .pill.bad { background: rgba(232, 93, 117, 0.18); color: var(--bad); }
    .chart-wrap { position: relative; height: 280px; }
    .budget-bar { background: rgba(255,255,255,0.06); border-radius: 6px;
                  height: 10px; overflow: hidden; margin-top: 6px; }
    .budget-fill { height: 100%; transition: width 0.3s; }
    .budget-row { padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,0.04); }
    .budget-row:last-child { border: none; }
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 8px 6px; text-align: left; font-size: 13px; }
    th { color: var(--muted); font-weight: 500; }
    td.amount { font-variant-numeric: tabular-nums; text-align: right; }
    .toolbar { display: flex; gap: 8px; margin-bottom: 24px; flex-wrap: wrap; }
    .toolbar button { background: var(--panel); color: var(--text);
                     border: 1px solid rgba(255,255,255,0.1); padding: 8px 14px;
                     border-radius: 8px; cursor: pointer; font-size: 13px; }
    .toolbar button.active { background: var(--accent); color: var(--bg);
                              border-color: var(--accent); font-weight: 600; }
    .row { display: grid; gap: 16px;
           grid-template-columns: 2fr 1fr; margin-bottom: 24px; }
    @media (max-width: 800px) { .row { grid-template-columns: 1fr; } }
    .delta-up { color: var(--bad); }
    .delta-down { color: var(--accent); }
    .delta-flat { color: var(--muted); }
  </style>
</head>
<body>
  <h1>💸 Argentine Telegram Expenses</h1>
  <div class="subtitle" id="subtitle">Cargando…</div>

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
      <h2>Tendencia (últimos 30 días)</h2>
      <div class="chart-wrap"><canvas id="trend-chart"></canvas></div>
    </div>
    <div class="panel">
      <h2>Por categoría</h2>
      <div class="chart-wrap"><canvas id="category-chart"></canvas></div>
    </div>
  </div>

  <div class="row">
    <div class="panel">
      <h2>Presupuestos del mes</h2>
      <div id="budgets-list"></div>
    </div>
    <div class="panel">
      <h2>Próximos recurrentes</h2>
      <div id="recurring-list"></div>
    </div>
  </div>

  <div class="row">
    <div class="panel">
      <h2>Comparativa mes actual vs anterior</h2>
      <div id="comparison-list"></div>
    </div>
    <div class="panel">
      <h2>Proyección fin de mes</h2>
      <div id="projection-card"></div>
    </div>
  </div>

  <div class="panel">
    <h2>Últimos 10 gastos</h2>
    <table>
      <thead><tr><th>Fecha</th><th>Nombre</th><th>Categoría</th>
                 <th style="text-align:right">Monto</th></tr></thead>
      <tbody id="recent-tbody"></tbody>
    </table>
  </div>

<script>
const fmt = new Intl.NumberFormat('es-AR');
const currency = (n, c) => '$ ' + fmt.format(n) + ' ' + c;
let trendChart, categoryChart;
let currentPeriod = 'month';

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + ' ' + r.status);
  return r.json();
}

async function refresh() {
  const [summary, trend, recent, budgets, recurring, comparison, projection] =
    await Promise.all([
      getJSON('/api/summary?period=' + currentPeriod),
      getJSON('/api/trend?days=30'),
      getJSON('/api/recent?limit=10'),
      getJSON('/api/budgets'),
      getJSON('/api/recurring/upcoming'),
      getJSON('/api/comparison'),
      getJSON('/api/projection'),
    ]);

  document.getElementById('subtitle').textContent =
    summary.period_label + ' · ' + summary.start + ' → ' + summary.end;

  // KPI cards
  const totals = Object.entries(summary.total_by_currency || {});
  const kpiHTML = totals.map(([c, v]) => `
    <div class="panel">
      <h2>Total ${summary.period_label.toLowerCase()} <span class="pill ok">${c}</span></h2>
      <div class="big">$${fmt.format(v)}</div>
      <div style="color:var(--muted); font-size:12px; margin-top:4px">
        Día ${summary.days_elapsed} de ${summary.days_in_period}
      </div>
    </div>`).join('') || '<div class="panel"><h2>Sin gastos</h2></div>';
  document.getElementById('kpis').innerHTML = kpiHTML;

  // Trend chart
  const tLabels = trend.points.map(p => p.date.slice(5));
  const tData = trend.points.map(p => p.amount);
  if (trendChart) trendChart.destroy();
  trendChart = new Chart(document.getElementById('trend-chart'), {
    type: 'line',
    data: {
      labels: tLabels,
      datasets: [{
        label: 'ARS',
        data: tData,
        borderColor: '#4caf93',
        backgroundColor: 'rgba(76,175,147,0.15)',
        fill: true, tension: 0.3,
      }],
    },
    options: chartOpts(),
  });

  // Category pie
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
        }],
      },
      options: { plugins: { legend: { position: 'right', labels: { color: '#e6e6e6' } } } },
    });
  } else {
    document.getElementById('category-chart').replaceWith(
      Object.assign(document.createElement('div'), {
        className: 'subtitle', textContent: 'Sin gastos en este período.'
      })
    );
  }

  // Budgets
  const bDiv = document.getElementById('budgets-list');
  if (budgets.items.length === 0) {
    bDiv.innerHTML = '<div class="subtitle">No tenés presupuestos. Creá uno con <code>/presupuesto &lt;categoría&gt; &lt;monto&gt;</code> en Telegram.</div>';
  } else {
    bDiv.innerHTML = budgets.items.map(b => {
      const color = b.level === 'exceeded' ? '#e85d75'
                   : b.level === 'warning' ? '#f5a623' : '#4caf93';
      return `
        <div class="budget-row">
          <div><strong>${b.category}</strong> · ${currency(b.limit, b.currency)}
            <span class="pill ${b.level === 'exceeded' ? 'bad' : b.level === 'warning' ? 'warn' : 'ok'}">
              ${b.percent.toFixed(0)}%
            </span>
          </div>
          <div class="subtitle">${currency(b.spent, b.currency)} de ${currency(b.limit, b.currency)}</div>
          <div class="budget-bar"><div class="budget-fill"
               style="width:${Math.min(100, b.percent)}%; background:${color}"></div></div>
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
        <div class="${dClass}" style="font-size:24px; margin-top:8px">
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
      <table style="margin-top:12px">
        <thead><tr><th>Categoría</th>
                   <th style="text-align:right">${comparison.current.label}</th>
                   <th style="text-align:right">${comparison.previous.label}</th>
                   <th style="text-align:right">Δ</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  // Projection
  const pDiv = document.getElementById('projection-card');
  if (projection.previous_total === 0 && projection.spent === 0) {
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
        <div class="big" style="margin-bottom:8px">$${fmt.format(projection.spent)}</div>
        <div class="subtitle">
          Día ${projection.days_elapsed} de ${projection.days_in_period}
        </div>
        <div style="margin-top:16px"><strong>Proyección fin de mes</strong></div>
        <div class="big" style="color:var(--accent)">$${fmt.format(projection.projected_total)}</div>
        <div class="${dCls}" style="margin-top:8px; font-size:18px">
          ${arrow} ${sign}${projection.delta_vs_previous_pct == null ? '—' : projection.delta_vs_previous_pct.toFixed(1) + '%'}
          vs mes pasado
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
  const base = ['#4caf93', '#5b8def', '#f5a623', '#e85d75', '#9b6dd7',
                '#3ec1d3', '#c0e218', '#ff8c42', '#7e8c8d', '#d2527f'];
  return Array.from({length: n}, (_, i) => base[i % base.length]);
}

function chartOpts() {
  return {
    responsive: true, maintainAspectRatio: false,
    plugins: { legend: { labels: { color: '#e6e6e6' } } },
    scales: {
      x: { ticks: { color: '#8b95a5' }, grid: { color: 'rgba(255,255,255,0.04)' } },
      y: { ticks: { color: '#8b95a5' }, grid: { color: 'rgba(255,255,255,0.04)' } },
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
    refresh().catch(err => {
      console.error(err);
      document.getElementById('subtitle').textContent = 'Error: ' + err.message;
    });
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
