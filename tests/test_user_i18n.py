import pytest

from app.users.i18n import localize_response, translate


def test_spanish_response_is_unchanged():
    text = "☕ Gasto registrado\n\n*Categoría: Café*\n💰 $ 1.000 ARS\n📂 Categoría: Comida\n📅 Fecha: 02/11"
    assert localize_response(text, "es") == text


@pytest.mark.parametrize("locale", ["en", "pt", "es"])
def test_help_contains_new_commands_and_keeps_canonical_command_names(locale):
    help_text = translate("help", locale)
    for command in ("/undo", "/ubicacion", "/idioma", "/voz", "/zona", "/config_payday", "/ingreso", "/disponible"):
        assert command in help_text
    assert len(help_text) < 4096
    assert "/usuario_add" in translate("help_owner", locale)


@pytest.mark.parametrize("locale,heading,category,date", [
    ("en", "Expense recorded", "Category", "Date"),
    ("pt", "Despesa registrada", "Categoria", "Data"),
])
def test_expense_localization_preserves_user_data(locale, heading, category, date):
    text = "☕ Gasto registrado\n\n*Ingreso base: Café Disponible*\n💰 $ 1.234,56 ARS\n📂 Categoría: Comida\n📅 Fecha: 02/11/2026"
    result = localize_response(text, locale)
    assert heading in result
    assert f"📂 {category}: Comida" in result
    assert f"📅 {date}: 02/11/2026" in result
    assert "*Ingreso base: Café Disponible*" in result
    assert "$ 1.234,56 ARS" in result


def test_cycle_localization_preserves_amounts_currency_and_dates():
    text = "📆 Ciclo #2 · USD (2026-11-15 → 2026-12-14) — open\n• Ingreso base: USD 100,25\n• Ahorro reservado: USD 20,00\n• Disponible: USD -5,40"
    result = localize_response(text, "en")
    assert result == "📆 Cycle #2 · USD (2026-11-15 → 2026-12-14) — open\n• Base income: USD 100,25\n• Savings reserved: USD 20,00\n• Available: USD -5,40"


def test_money_acknowledgement_and_payday_use_localized_labels():
    message = '💼 Ingreso base actualizado.\n→ USD: USD 100,25\n💰 Disponible: USD 100,25 USD\n🔒 Comprometido: USD 0,00 USD'
    result = localize_response(message, 'en')
    assert result == '💼 Base income updated.\n→ USD: USD 100,25\n💰 Available: USD 100,25 USD\n🔒 Committed savings: USD 0,00 USD'
    assert localize_response('💵 Cobro: primer viernes de cada mes', 'en') == '💵 Payday: first Friday of each month'
    assert localize_response('💵 Cobro: primer viernes de cada mes', 'pt') == '💵 Recebimento: primeira sexta-feira de cada mês'
    assert localize_response('💵 Cobro: día 15 de cada mes', 'en') == '💵 Payday: day 15 of each month'


def test_account_validation_and_crud_failures_use_selected_language():
    assert localize_response('⚠️ Sólo el propietario puede administrar usuarios.', 'en') == '⚠️ Only the owner can manage users.'
    assert localize_response('❓ Elegí es, en o pt.', 'pt') == '❓ Escolha es, en ou pt.'
    assert localize_response('🤷 No pude borrar el gasto #10.', 'en') == "🤷 I couldn't delete expense #10."


@pytest.mark.parametrize("source,expected", [
    ("No tenés gastos registrados en este mes.", "You have no expenses recorded for this month."),
    ("💸 *Gasto hoy*\n\n*100.00 USD*", "💸 *Spending today*\n\n*100.00 USD*"),
    ("📋 *Últimos gastos (Últimos 7 días)*\n\n*Café*", "📋 *Latest expenses (last 7 days)*\n\n*Café*"),
    ("✏️ Gasto #42 actualizado:\n*Café*", "✏️ Expense #42 updated:\n*Café*"),
    ("🗑️ Gasto borrado:\n*Últimos gastos*", "🗑️ Expense deleted:\n*Últimos gastos*"),
])
def test_known_query_crud_structures_translate_without_touching_data(source, expected):
    assert localize_response(source, "en") == expected


def test_unknown_or_ai_authored_prose_does_not_translate_arbitrary_words():
    data = "El comercio se llama Disponible y el producto Categoría. Café, ARS y números 123,45."
    assert localize_response(data, "en") == data
    assert localize_response("*Gasto registrado*", "pt") == "*Gasto registrado*"


def test_supported_locale_aliases_and_unknown_locale_fallback():
    assert translate("voice_on", "en-US") == translate("voice_on", "en")
    assert translate("voice_on", "pt_BR") == translate("voice_on", "pt")
    assert translate("voice_on", "xx") == translate("voice_on", "es")
    assert localize_response("Comando no reconocido.", "en") == "Unknown command."
