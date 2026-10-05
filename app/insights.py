"""Aggregate-only spending insights shared by Telegram and future UIs."""
from __future__ import annotations

import json
from datetime import date

from sqlalchemy.orm import Session

from app.ai.schemas import SpendingAnalysis
from app.web.queries import resolve_period, summary_for_period


def spending_context(session: Session, user_id: int, today: date) -> str | None:
    current = summary_for_period(
        session, user_id, resolve_period("month", today)
    )
    if not current["total_by_currency"]:
        return None

    previous = summary_for_period(
        session, user_id, resolve_period("last_month", today)
    )
    payload = {
        "periodo_actual": {
            "label": current["period_label"],
            "desde": current["start"],
            "hasta": current["end"],
            "totales_por_moneda": current["total_by_currency"],
            "por_categoria": current["by_category_by_currency"],
            "gastos_repetidos": {
                currency: rows[:12]
                for currency, rows in current["by_name_by_currency"].items()
            },
        },
        "periodo_anterior": {
            "label": previous["period_label"],
            "totales_por_moneda": previous["total_by_currency"],
            "por_categoria": previous["by_category_by_currency"],
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def format_spending_analysis(
    analysis: SpendingAnalysis, period_label: str
) -> str:
    lines = [f"🧠 Análisis de gastos — {period_label}", "", analysis.summary]
    if analysis.observations:
        lines.extend(["", "🔎 Observaciones:"])
        lines.extend(f"• {item}" for item in analysis.observations[:5])
    if analysis.recommendations:
        lines.extend(["", "💡 Dónde podrías ajustar:"])
        for item in analysis.recommendations[:3]:
            lines.append(f"• {item.category}: {item.action}")
            lines.append(f"  Motivo: {item.reason}")
    if analysis.caveat:
        lines.extend(["", f"ℹ️ {analysis.caveat}"])
    return "\n".join(lines)
