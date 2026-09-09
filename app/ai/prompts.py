"""Prompt templates for the local Ollama model.

These prompts are kept intentionally **short**. On a 4B CPU model, every
extra token in the system prompt adds ~30ms of prefill latency, and a
verbose schema dump adds hundreds of unnecessary tokens. We trade
explicitness for speed: the model already knows JSON; we only point out
the *edge cases* that have historically caused errors (jerga for amounts,
date resolution, category hygiene, JSON-only output).
"""
from __future__ import annotations

from datetime import date

from app.categories.categories import list_categories_text, DEFAULT_CATEGORY


def system_prompt(today_iso: str, timezone_name: str) -> str:
    """Concise system prompt (~250 tokens)."""
    return (
        "Asistente que registra gastos y responde consultas en español "
        "argentino. Devolvés SOLO un objeto JSON válido. Sin markdown, "
        "sin texto adicional.\n\n"
        "REGLAS:\n"
        "- amount: número decimal positivo, sin símbolos. "
        "'10k'/'10 lucas'/'10 mil'/'10 palos'/'10 mangos' = 10000. "
        "'$10.000' = 10000. '1.234,56' = 1234.56. "
        "'20 dólares'/'20 verdes' = 20 USD.\n"
        "- currency: ARS por default; USD si dice 'dólares'/'verdes'/'usd'.\n"
        "- date: YYYY-MM-DD. 'hoy' = hoy, 'ayer' = hoy-1. Si no hay pista, hoy.\n"
        "- name: nombre real del gasto, sin inventar comercios.\n"
        "- category: hoja válida de la lista provista; si no encaja, '"
        f"{DEFAULT_CATEGORY}"
        "'.\n"
        "- confidence: 0-1. Si falta info crítica (ej. monto), devolvé "
        "needs_clarification=true con pregunta concreta.\n"
        "- Si menciona periodicidad ('cada mes', 'anualmente'), "
        "type='register_recurring' y completá 'recurring'.\n"
        "- Borrar/editar/eliminar NO son intents: el bot los maneja por "
        "comando.\n"
        "- NUNCA generes SQL. NUNCA inventes montos."
    )


def user_instructions(today_iso: str, timezone_name: str) -> str:
    """Compact user-side context (~150 tokens)."""
    return (
        f"Hoy: {today_iso} ({timezone_name}).\n\n"
        "Categorías (usá hoja válida o '"
        f"{DEFAULT_CATEGORY}"
        "'):\n"
        f"{list_categories_text()}\n\n"
        "JSON schema:\n"
        "{type, expenses:[{name,amount,currency,category,date,confidence,"
        "needs_clarification,clarification_question}], "
        "recurring:{name,amount,currency,category,frequency,day_of_month,"
        "month_of_year,confidence}|null, "
        "query:{period,category,currency,limit,order_by}|null, "
        "confidence, rationale}\n\n"
        "type ∈ register_expense|register_recurring|query|greeting|help|"
        "cancel|confirm|unknown\n"
        "frequency ∈ monthly|yearly\n"
        "period ∈ today|yesterday|week|month|all|last_n_days\n"
        "order_by ∈ amount_desc|amount_asc|date_desc|date_asc"
    )


def today_str() -> str:
    return date.today().isoformat()
