from __future__ import annotations

import logging
from datetime import date
from typing import Sequence

from app.ai.schemas import AIError, AIUnavailable, ParsedMessage
from app.ai.service import AIService
from app.categories.categories import normalize_category
from app.config.settings import Settings
from app.database.database import session_scope
from app.expenses.repository import ExpenseRepository
from app.expenses.service import (
    ExpenseDraft,
    ExpenseService,
    PersistOutcome,
)
from app.queries.intents import QueryResult, resolve_query_spec
from app.queries.service import QueryService
from app.recurring.repository import RecurringRepository
from app.recurring.service import (
    RecurringDraft,
    RecurringService,
    RecurringValidationError,
)
from app.budgets.repository import BudgetRepository
from app.budgets.service import BudgetService
from app.utils.amounts import (
    detect_currency_hint,
    normalize_amount,
    normalize_amounts_multi,
)
from app.utils.dates import parse_relative_date
from app.utils.formatting import (
    CATEGORY_ICONS,
    format_amount,
    format_currency_amount,
    format_date_short,
)

logger = logging.getLogger(__name__)


class BotRuntime:
    """Holds shared singletons used by the bot dispatch layer."""

    def __init__(self, settings: Settings, ai: AIService) -> None:
        self.settings = settings
        self.ai = ai
        # user_id -> {"recurring_id": int, "name": str, "amount": Decimal, "currency": str}
        self.pending_reminders: dict[int, dict] = {}
        # user_id -> pending vision extraction dict awaiting confirm/cancel
        self.pending_visions: dict[int, dict] = {}

    @classmethod
    def create(cls, settings: Settings) -> "BotRuntime":
        from app.ai.service import build_default_ai_service

        return cls(settings=settings, ai=build_default_ai_service(settings))


async def _process_message_async(
    runtime: BotRuntime,
    text: str,
    *,
    user_id: int,
    today: date,
) -> str:
    if not text or not text.strip():
        return "No recibí ningún mensaje."

    cleaned = text.strip()
    if len(cleaned) > runtime.settings.max_message_length:
        return "El mensaje es demasiado largo."

    try:
        parsed: ParsedMessage = await runtime.ai.interpret(cleaned)
    except AIUnavailable:
        logger.exception("AI provider unreachable")
        return (
            "🤖 Ollama no responde en este momento. Verificá que esté "
            "corriendo (`ollama serve`) y que el modelo esté descargado "
            "(`ollama list`)."
        )
    except AIError:
        logger.exception("AI failure")
        return "🤖 No pude entender el mensaje ahora. Probá de nuevo en un momento."

    intent = parsed.intent
    intent_type = intent.type

    if intent_type == "greeting":
        return (
            "👋 Hola. Contame un gasto (ej: *gasté 10k en un café*) o "
            "preguntame (*¿cuánto gasté hoy?*)."
        )

    if intent_type == "help":
        return _help_text()

    if intent_type == "cancel":
        return "👍 Listo, no guardo nada."

    if intent_type == "confirm":
        return (
            "👍 Perfecto. Cuando me escribas el gasto lo registro. "
            "Si querés guardar uno nuevo, contame: *gasté X en Y*."
        )

    if intent_type == "unknown":
        if parsed.extracted and any(
            e.needs_clarification for e in parsed.extracted
        ):
            return _clarification_message(parsed.extracted)
        return (
            "🤔 No estoy seguro de qué querés hacer. Probá con un gasto "
            "como *gasté 5k en café* o una consulta como *¿cuánto gasté "
            "esta semana?*."
        )

    if intent_type == "register_expense":
        drafts = _build_drafts(
            parsed.extracted,
            today=today,
            original_message=cleaned,
        )
        try:
            with session_scope() as session:
                repo = ExpenseRepository(session)
                service = ExpenseService(repo)
                outcome = service.register_many(
                    user_id=user_id,
                    drafts=drafts,
                    original_message=cleaned,
                )
        except Exception:
            logger.exception("DB error while registering expense")
            return "⚠️ No pude guardar el gasto porque la base de datos no responde. Probá más tarde."

        if outcome.needs_clarification:
            if outcome.questions:
                bullets = "\n".join(f"• {q}" for q in outcome.questions)
                return f"🤔 {bullets}"
            return _clarification_message(parsed.extracted)

        if not outcome.saved:
            return "🤔 No pude identificar un gasto válido en el mensaje."

        confirmation = _expense_confirmation(outcome.saved)
        budget_warning = _check_budget_alerts(
            user_id=user_id, outcome=outcome, today=today
        )
        if budget_warning:
            confirmation = f"{confirmation}\n\n{budget_warning}"
        return confirmation

    if intent_type == "register_recurring":
        if parsed.recurring is None:
            return "🤔 No pude identificar el gasto recurrente. Intentá reformularlo."
        draft = _build_recurring_draft(parsed.recurring)
        try:
            with session_scope() as session:
                repo = RecurringRepository(session)
                service = RecurringService(repo)
                template = service.create_from_draft(
                    user_id=user_id, draft=draft, today=today
                )
        except RecurringValidationError as exc:
            return f"🤔 {exc}"
        except Exception:
            logger.exception("DB error while registering recurring")
            return "⚠️ No pude guardar el recurrente porque la base de datos no responde."

        return _recurring_confirmation(template)

    if intent_type == "query":
        spec = resolve_query_spec(intent, default_currency="ARS")
        try:
            with session_scope() as session:
                repo = ExpenseRepository(session)
                qservice = QueryService(repo)
                result = qservice.run(user_id=user_id, spec=spec)
        except Exception:
            logger.exception("DB error while querying")
            return "⚠️ No pude consultar la base de datos. Probá más tarde."
        return _format_query(result)

    return "🤖 Algo raro pasó con el mensaje. Probá reformulándolo."


def _draft_from_extracted(
    extracted,
    *,
    today: date,
    original_message: str,
    all_expenses,
    position: int,
) -> ExpenseDraft:
    parsed_date = extracted.date
    if parsed_date is None:
        parsed_date = today

    ai_amounts = [e.amount for e in all_expenses]
    if len(ai_amounts) > 1:
        normalized = normalize_amounts_multi(ai_amounts, original_message)
        amount = normalized[position]
    else:
        amount = normalize_amount(extracted.amount, original_message)

    currency = (extracted.currency or "ARS").upper()
    hint = detect_currency_hint(original_message)
    if hint is not None and (
        extracted.currency is None or extracted.currency.upper() == "ARS"
    ):
        # Only override ARS with the hint when no currency was named in slang;
        # honor explicit AI currency otherwise.
        if hint == "USD" and not _has_currency_word(
            original_message, "ARS"
        ):
            currency = "USD"

    return ExpenseDraft(
        name=extracted.name,
        amount=amount,
        currency=currency,
        category=normalize_category(extracted.category),
        expense_date=parsed_date,
        confidence=extracted.confidence,
        needs_clarification=extracted.needs_clarification,
        clarification_question=extracted.clarification_question,
    )


def _has_currency_word(text: str, target: str) -> bool:
    if target != "ARS":
        return False
    lowered = text.lower()
    return "peso" in lowered or "pesos" in lowered


def _build_drafts(extracted, *, today: date, original_message: str) -> list[ExpenseDraft]:
    drafts: list[ExpenseDraft] = []
    for idx, e in enumerate(extracted):
        drafts.append(
            _draft_from_extracted(
                e,
                today=today,
                original_message=original_message,
                all_expenses=extracted,
                position=idx,
            )
        )
    return drafts


def _build_recurring_draft(extracted) -> RecurringDraft:
    return RecurringDraft(
        name=extracted.name,
        amount=extracted.amount,
        currency=extracted.currency,
        category=extracted.category,
        frequency=extracted.frequency,
        day_of_month=extracted.day_of_month,
        month_of_year=extracted.month_of_year,
        confidence=extracted.confidence,
        needs_clarification=extracted.needs_clarification,
        clarification_question=extracted.clarification_question,
    )


def _recurring_confirmation(template) -> str:
    frequency_label = (
        "cada mes" if template.frequency == "monthly" else "cada año"
    )
    if template.frequency == "yearly" and template.month_of_year is not None:
        month_names = [
            "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
            "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
        ]
        when = (
            f"el {template.day_of_month} de {month_names[template.month_of_year]}"
        )
    else:
        when = f"el día {template.day_of_month} de {frequency_label}"
    return (
        f"🔁 Recurrente registrado\n\n"
        f"*{template.name}* — {format_currency_amount(template.amount, template.currency)}\n"
        f"📅 Te aviso {when}.\n"
        f"Próximo vencimiento: {format_date_short(template.next_due_date)}\n\n"
        f"Lo gestionás con `/recurrentes` o lo borrás con `/recurrente_del {template.id}`."
    )


def _help_text() -> str:
    return (
        "📖 *¿Cómo te puedo ayudar?*\n\n"
        "*Registrar gastos:*\n"
        "• *gasté 10k en un café*\n"
        "• *ayer gasté 20 dólares en Steam*\n"
        "• *hoy gasté 5k en café y 12k en Uber*\n"
        "• *gasto 30k en Netflix cada mes el día 15* (recurrente)\n"
        "• 📸 *mandame una foto del ticket* — extraigo los datos y los confirmás\n\n"
        "*Consultas:*\n"
        "• *cuánto gasté hoy*\n"
        "• *total del mes*\n"
        "• *cuánto llevo en comida*\n"
        "• *últimos 10 gastos*\n\n"
        "*Dashboard:* abrí http://localhost:8000/dashboard en el browser\n\n"
        "*Comandos:*\n"
        "/hoy /semana /mes — totales rápidos\n"
        "/gastos — últimos 10 gastos\n"
        "/desglose — desglose por categoría del mes\n"
        "/borrar_ultimo /borrar <id> — borrar gasto\n"
        "/editar_ultimo <monto> — corregir último gasto\n"
        "/exportar — CSV del mes\n"
        "/recurrente_add <nombre> <monto> <día> — recurrente mensual\n"
        "/recurrente_add_anual <nombre> <monto> <mes> <día> — anual\n"
        "/recurrentes — listar recurrentes\n"
        "/presupuesto <categoría> <monto> — límite mensual\n"
        "/presupuestos — listar presupuestos"
    )


def _clarification_message(extracted) -> str:
    questions = [
        e.clarification_question
        for e in extracted
        if e.clarification_question
    ]
    if not questions:
        return "Necesito un poco más de información para registrar el gasto."
    bullets = "\n".join(f"• {q}" for q in questions if q)
    return f"🤔 {bullets}"


def _check_budget_alerts(
    *, user_id: int, outcome, today
) -> str | None:
    """If the just-registered expense trips a budget threshold, append a warning.

    Uses a fresh session and is silent on error — a budget notification is
    nice-to-have, never a blocker for the expense itself.
    """
    from app.categories.models import Category

    alerts: list[str] = []
    seen: set[tuple[int, str]] = set()
    for saved in outcome.saved:
        cache_key = (saved.category_id, saved.currency)
        if cache_key in seen:
            continue
        seen.add(cache_key)
        try:
            with session_scope() as session:
                expense_repo = ExpenseRepository(session)
                category_name = (
                    session.query(Category.name)
                    .filter(Category.id == saved.category_id)
                    .scalar()
                )
                if not category_name:
                    continue
                month_start = today.replace(day=1)
                spent = expense_repo.sum_for_category(
                    user_id,
                    category_name=category_name,
                    start=month_start,
                    end=today,
                    currency=saved.currency,
                )
                budget_repo = BudgetRepository(session)
                service = BudgetService(budget_repo)
                alert = service.evaluate_after_expense(
                    user_id=user_id,
                    category_id=saved.category_id,
                    currency=saved.currency,
                    spent_in_month=spent,
                    today=today,
                )
                if alert is not None:
                    alerts.append(_format_budget_alert(alert))
        except Exception:
            logger.exception("Budget alert check failed")
            continue
    return "\n".join(alerts) if alerts else None


def _format_budget_alert(alert) -> str:
    pct = (alert.percent * 100).quantize(Decimal("1"))
    if alert.level == "exceeded":
        return (
            f"🚨 *Presupuesto superado*: llevás "
            f"{format_currency_amount(alert.spent, alert.currency)} "
            f"({pct}%) en *{alert.category_name}* este mes "
            f"(límite {format_currency_amount(alert.limit, alert.currency)})."
        )
    return (
        f"⚠️ *Cerca del límite*: llevás "
        f"{format_currency_amount(alert.spent, alert.currency)} "
        f"({pct}%) en *{alert.category_name}* este mes "
        f"(límite {format_currency_amount(alert.limit, alert.currency)})."
    )


def _expense_confirmation(saved) -> str:
    if len(saved) == 1:
        e = saved[0]
        from app.categories.categories import icon_for
        cat_name = e.category.name if hasattr(e.category, "name") else str(e.category)
        icon = icon_for(cat_name) or CATEGORY_ICONS.get(cat_name, "🧾")
        return (
            f"{icon} Gasto registrado\n\n"
            f"*{e.name}*\n"
            f"💰 {format_currency_amount(e.amount, e.currency)}\n"
            f"📂 Categoría: {cat_name}\n"
            f"📅 Fecha: {format_date_short(e.expense_date)}"
        )

    lines: list[str] = ["🧾 Gastos registrados", ""]
    totals: dict[str, float] = {}
    for e in saved:
        from app.categories.categories import icon_for
        cat_name = e.category.name if hasattr(e.category, "name") else str(e.category)
        icon = icon_for(cat_name) or CATEGORY_ICONS.get(cat_name, "🧾")
        lines.append(
            f"{icon} *{e.name}* — {format_currency_amount(e.amount, e.currency)}"
        )
        cur = e.currency.upper()
        totals[cur] = totals.get(cur, 0.0) + float(e.amount)
    lines.append("")
    from decimal import Decimal
    for cur, amt in totals.items():
        lines.append(f"*Total: {format_currency_amount(Decimal(str(amt)), cur)}*")
    return "\n".join(lines)


def _format_query(result: QueryResult) -> str:
    if result.items is not None:
        if not result.items:
            return f"No tenés gastos registrados en {result.period_label.lower()}."
        lines = [f"📋 *Últimos gastos ({result.period_label})*", ""]
        for row in result.items:
            from app.categories.categories import icon_for
            icon = icon_for(row.category) or CATEGORY_ICONS.get(row.category, "🧾")
            lines.append(
                f"{icon} {format_date_short(row.expense_date)} — "
                f"*{row.name}* {format_currency_amount(row.amount, row.currency)} "
                f"({row.category})"
            )
        return "\n".join(lines)

    if result.largest is not None:
        from app.categories.categories import icon_for
        row = result.largest
        icon = icon_for(row.category) or CATEGORY_ICONS.get(row.category, "🧾")
        return (
            f"🏆 *Gasto más grande ({result.period_label})*\n\n"
            f"{icon} *{row.name}*\n"
            f"{format_currency_amount(row.amount, row.currency)}\n"
            f"📂 {row.category}\n"
            f"📅 {format_date_short(row.expense_date)}"
        )

    if result.category_totals is not None:
        if not result.category_totals:
            return (
                f"No tenés gastos registrados en {result.period_label.lower()}."
            )
        from app.categories.categories import icon_for

        lines = [f"📊 *Desglose por categoría ({result.period_label})*", ""]
        grand: dict[str, Decimal] = {}
        for cat, amount in result.category_totals.items():
            icon = icon_for(cat) or CATEGORY_ICONS.get(cat, "🧾")
            lines.append(f"{icon} *{cat}*: {format_amount(amount)}")
        return "\n".join(lines)

    if not result.total_by_currency:
        suffix = (
            f" (filtros: {result.filters})" if result.filters else ""
        )
        return (
            f"No tenés gastos registrados en {result.period_label.lower()}{suffix}."
        )

    lines = [f"💸 *Gasto {result.period_label.lower()}*", ""]
    if result.filters:
        lines.append(f"_Filtros: {result.filters}_")
        lines.append("")
    for cur, amount in result.total_by_currency.items():
        lines.append(f"*{format_currency_amount(amount, cur)}*")
    return "\n".join(lines)
