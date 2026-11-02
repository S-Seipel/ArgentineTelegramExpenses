"""Local translations for bot UI; never send user text to a translator.

Names, categories, numeric values, currency codes and dates are opaque data.
``localize_response`` translates known message structures and exact strings
only. Unknown prose (including AI-authored analysis) stays unchanged rather
than applying unsafe word replacements to user data.
"""
from __future__ import annotations

import re


_MESSAGES = {
    "start": (
        "👋 Hola. Contame un gasto (ej: *gasté 10k en un café*) o preguntame (*¿cuánto gasté hoy?*).",
        "👋 Hi. Tell me an expense (for example, *I spent 100 on coffee*) or ask *how much did I spend today?*.",
        "👋 Olá. Conte uma despesa (por exemplo, *gastei 100 em café*) ou pergunte *quanto gastei hoje?*.",
    ),
    "unknown": (
        "🤔 No estoy seguro de qué querés hacer. Probá con un gasto como *gasté 5k en café* o una consulta como *¿cuánto gasté esta semana?*.",
        "🤔 I couldn't understand the request. Tell me an expense or ask how much you spent this week.",
        "🤔 Não entendi o pedido. Conte uma despesa ou pergunte quanto gastou nesta semana.",
    ),
    "unauthorized": ("🔒 No tenés acceso a este bot.", "🔒 You don't have access to this bot.", "🔒 Você não tem acesso a este bot."),
    "preferences_saved": ("✅ Preferencias guardadas.", "✅ Preferences saved.", "✅ Preferências salvas."),
    "language_changed": ("✅ Idioma actualizado.", "✅ Language updated.", "✅ Idioma atualizado."),
    "voice_on": ("🔊 Respuestas por voz activadas.", "🔊 Voice replies enabled.", "🔊 Respostas por voz ativadas."),
    "voice_off": ("🔇 Respuestas por voz desactivadas.", "🔇 Voice replies disabled.", "🔇 Respostas por voz desativadas."),
    "voice_unavailable": (
        "🔇 No pude generar el audio; la respuesta en texto sigue disponible.",
        "🔇 Audio is unavailable; the text reply is still available.",
        "🔇 O áudio não está disponível; a resposta em texto continua disponível.",
    ),
    "undo_empty": ("🤷 No hay cambios para deshacer.", "🤷 There are no changes to undo.", "🤷 Não há alterações para desfazer."),
    "undo_done": ("↩️ Último cambio deshecho.", "↩️ Last change undone.", "↩️ Última alteração desfeita."),
    "undo_conflict": (
        "⚠️ El gasto cambió después; no sobrescribí los cambios posteriores.",
        "⚠️ The expense changed afterward; later changes were preserved.",
        "⚠️ A despesa foi alterada depois; as alterações posteriores foram preservadas.",
    ),
    "location_saved": ("📍 Ubicación guardada.", "📍 Location saved.", "📍 Localização salva."),
    "location_prompt": (
        "📍 Enviá una ubicación de Telegram para asociarla al gasto seleccionado.",
        "📍 Send a Telegram location to attach it to the selected expense.",
        "📍 Envie uma localização do Telegram para associá-la à despesa selecionada.",
    ),
    "no_expenses": ("No tenés gastos registrados en {period}.", "You have no expenses recorded for {period}.", "Você não tem despesas registradas em {period}."),
    "expense_registered": ("Gasto registrado", "Expense recorded", "Despesa registrada"),
    "expenses_registered": ("Gastos registrados", "Expenses recorded", "Despesas registradas"),
    "recurring_registered": ("Recurrente registrado", "Recurring expense recorded", "Despesa recorrente registrada"),
    "category": ("Categoría", "Category", "Categoria"),
    "date": ("Fecha", "Date", "Data"),
    "cycle": ("Ciclo", "Cycle", "Ciclo"),
    "payday": ("Cobro", "Payday", "Recebimento"),
    "income": ("Ingreso base", "Base income", "Renda base"),
    "extra_income": ("Extras", "Extra income", "Renda extra"),
    "gross_income": ("Ingreso bruto", "Gross income", "Renda bruta"),
    "savings_target": ("Ahorro target", "Savings target", "Meta de poupança"),
    "savings_reserved": ("Ahorro reservado", "Savings reserved", "Poupança reservada"),
    "savings_realized": ("Ahorro realizado", "Savings transferred", "Poupança realizada"),
    "savings_committed": ("Comprometido", "Committed savings", "Poupança comprometida"),
    "expenses_realized": ("Gastos realizados", "Expenses incurred", "Despesas realizadas"),
    "fixed_pending": ("Pendientes fijos", "Pending fixed bills", "Contas fixas pendentes"),
    "recurring_pending": ("Pendientes recurrentes", "Pending recurring expenses", "Despesas recorrentes pendentes"),
    "available": ("Disponible", "Available", "Disponível"),
    "daily_safe": ("Gasto diario seguro", "Daily spending allowance", "Limite diário de gastos"),
    "users": ("Usuarios autorizados", "Authorized users", "Usuários autorizados"),
    "owner_only": ("Sólo el propietario puede administrar usuarios.", "Only the owner can manage users.", "Somente o proprietário pode gerenciar usuários."),
    "timezone_saved": ("✅ Zona horaria actualizada.", "✅ Timezone updated.", "✅ Fuso horário atualizado."),
}


_HELP = {
    "es": (
        "📖 *¿Cómo te puedo ayudar?*\n\n"
        "Escribí o mandá un audio con tus gastos. También podés enviar un ticket por foto y revisar su propuesta antes de guardarla.\n\n"
        "*Gastos y consultas*\n"
        "/hoy /semana /mes — totales\n/gastos — últimos gastos\n"
        "/desglose — categorías\n/buscar <texto> — buscar por nombre\n"
        "/analisis — análisis de gastos\n/exportar — CSV\n"
        "/editar <id> <monto> — corregir\n/borrar <id> — borrar\n"
        "/undo — deshacer la última edición o eliminación\n"
        "/ubicacion <id> — asociar una ubicación enviada por vos\n\n"
        "*Ciclos por moneda*\n/config_payday <día|primer_viernes> [moneda]\n"
        "/cycle [moneda] — resumen\n/disponible [moneda] — disponible\n"
        "/gastar_diario [moneda] — sugerencia diaria\n"
        "/ingreso <monto> [moneda]\n/extra <monto> [moneda] [nota]\n"
        "/target <porcentaje|monto> [moneda]\n/reservar <monto> [moneda]\n"
        "/liberar <monto> [moneda]\n/ahorro_realizado <monto> [moneda] [nota]\n\n"
        "*Fijos, recurrentes y presupuestos*\n/gastosfijos\n"
        "/gastofijo_add <nombre> <monto> [día] [método]\n/pague <nombre> [monto]\n/salte <nombre>\n"
        "/recurrentes\n/recurrente_add <nombre> <monto> <día>\n"
        "/recurrente_obligacion <id> on|off\n/presupuesto <categoría> <monto> [moneda]\n/presupuestos\n\n"
        "*Preferencias*\n/idioma es|en|pt\n/voz on|off\n/zona <zona IANA>\n\n"
        "Dashboard: http://localhost:8000/dashboard"
    ),
    "en": (
        "📖 *How can I help?*\n\n"
        "Send your expenses as text or audio. You can also send a receipt photo and review the proposal before saving it.\n\n"
        "*Expenses and queries*\n/hoy /semana /mes — totals\n/gastos — latest expenses\n"
        "/desglose — categories\n/buscar <text> — search by name\n"
        "/analisis — spending analysis\n/exportar — CSV\n"
        "/editar <id> <amount> — edit\n/borrar <id> — delete\n"
        "/undo — undo the last edit or deletion\n"
        "/ubicacion <id> — attach a location you send\n\n"
        "*Cycles by currency*\n/config_payday <day|first_friday> [currency]\n"
        "/cycle [currency] — summary\n/disponible [currency] — available\n"
        "/gastar_diario [currency] — daily allowance\n"
        "/ingreso <amount> [currency]\n/extra <amount> [currency] [note]\n"
        "/target <percentage|amount> [currency]\n/reservar <amount> [currency]\n"
        "/liberar <amount> [currency]\n/ahorro_realizado <amount> [currency] [note]\n\n"
        "*Bills, recurring expenses and budgets*\n/gastosfijos\n"
        "/gastofijo_add <name> <amount> [day] [method]\n/pague <name> [amount]\n/salte <name>\n"
        "/recurrentes\n/recurrente_add <name> <amount> <day>\n"
        "/recurrente_obligacion <id> on|off\n/presupuesto <category> <amount> [currency]\n/presupuestos\n\n"
        "*Preferences*\n/idioma es|en|pt\n/voz on|off\n/zona <IANA timezone>\n\n"
        "Dashboard: http://localhost:8000/dashboard"
    ),
    "pt": (
        "📖 *Como posso ajudar?*\n\n"
        "Envie suas despesas por texto ou áudio. Também pode enviar uma foto do recibo e revisar a proposta antes de salvar.\n\n"
        "*Despesas e consultas*\n/hoy /semana /mes — totais\n/gastos — últimas despesas\n"
        "/desglose — categorias\n/buscar <texto> — buscar por nome\n"
        "/analisis — análise de gastos\n/exportar — CSV\n"
        "/editar <id> <valor> — editar\n/borrar <id> — excluir\n"
        "/undo — desfazer a última edição ou exclusão\n"
        "/ubicacion <id> — associar uma localização enviada por você\n\n"
        "*Ciclos por moeda*\n/config_payday <dia|primer_viernes> [moeda]\n"
        "/cycle [moeda] — resumo\n/disponible [moeda] — disponível\n"
        "/gastar_diario [moeda] — limite diário\n"
        "/ingreso <valor> [moeda]\n/extra <valor> [moeda] [nota]\n"
        "/target <porcentagem|valor> [moeda]\n/reservar <valor> [moeda]\n"
        "/liberar <valor> [moeda]\n/ahorro_realizado <valor> [moeda] [nota]\n\n"
        "*Contas, recorrentes e orçamentos*\n/gastosfijos\n"
        "/gastofijo_add <nome> <valor> [dia] [método]\n/pague <nome> [valor]\n/salte <nome>\n"
        "/recurrentes\n/recurrente_add <nome> <valor> <dia>\n"
        "/recurrente_obligacion <id> on|off\n/presupuesto <categoria> <valor> [moeda]\n/presupuestos\n\n"
        "*Preferências*\n/idioma es|en|pt\n/voz on|off\n/zona <fuso IANA>\n\n"
        "Dashboard: http://localhost:8000/dashboard"
    ),
}


def _locale(value: str) -> str:
    normalized = str(value or "es").lower().replace("_", "-").split("-", 1)[0]
    return normalized if normalized in {"es", "en", "pt"} else "es"


def translate(key: str, locale: str = "es", **values) -> str:
    locale = _locale(locale)
    if key == "help":
        return _HELP[locale]
    if key == "help_owner":
        return {
            "es": "*Propietario*\n/usuarios — listar accesos\n/usuario_add <id> — autorizar\n/usuario_off <id> — pausar",
            "en": "*Owner*\n/usuarios — list access\n/usuario_add <id> — authorize\n/usuario_off <id> — deactivate",
            "pt": "*Proprietário*\n/usuarios — listar acessos\n/usuario_add <id> — autorizar\n/usuario_off <id> — desativar",
        }[locale]
    template = _MESSAGES[key][{"es": 0, "en": 1, "pt": 2}[locale]]
    return template.format(**values)


_EXACT = {
    "No recibí ningún mensaje.": ("I didn't receive a message.", "Não recebi nenhuma mensagem."),
    "El mensaje es demasiado largo.": ("The message is too long.", "A mensagem é longa demais."),
    "Comando no reconocido.": ("Unknown command.", "Comando desconhecido."),
    "👍 Listo, no guardo nada.": ("👍 Done; nothing was saved.", "👍 Pronto; nada foi salvo."),
    "🤷 No tenés gastos para borrar.": ("🤷 You have no expenses to delete.", "🤷 Você não tem despesas para excluir."),
    "🤷 No tenés gastos para editar.": ("🤷 You have no expenses to edit.", "🤷 Você não tem despesas para editar."),
    "❓ El nombre no puede estar vacío.": ("❓ The name cannot be empty.", "❓ O nome não pode ficar vazio."),
    "❓ El monto tiene que ser mayor a cero.": ("❓ The amount must be greater than zero.", "❓ O valor deve ser maior que zero."),
    "❓ El ID tiene que ser un número entero.": ("❓ The ID must be an integer.", "❓ O ID deve ser um número inteiro."),
    "❓ El día tiene que estar entre 1 y 31.": ("❓ The day must be between 1 and 31.", "❓ O dia deve estar entre 1 e 31."),
    "Necesito un poco más de información para registrar el gasto.": ("I need a little more information to record the expense.", "Preciso de mais informações para registrar a despesa."),
    "⚠️ No pude guardar el gasto porque la base de datos no responde. Probá más tarde.": ("⚠️ The expense could not be saved because the database is unavailable. Try again later.", "⚠️ Não foi possível salvar a despesa porque o banco está indisponível. Tente mais tarde."),
    "⚠️ No pude consultar la base de datos. Probá más tarde.": ("⚠️ The database is unavailable. Try again later.", "⚠️ O banco de dados está indisponível. Tente mais tarde."),
    "🤖 No pude entender el mensaje ahora. Probá de nuevo en un momento.": ("🤖 I couldn't process the message. Try again in a moment.", "🤖 Não consegui processar a mensagem. Tente novamente em instantes."),
    "🤔 No pude identificar un gasto válido en el mensaje.": ("🤔 I couldn't identify a valid expense in the message.", "🤔 Não identifiquei uma despesa válida na mensagem."),
    "Sin variación": ("No change", "Sem variação"),
    "Por categoría:": ("By category:", "Por categoria:"),
    "🗑️ Gasto borrado:": ("🗑️ Expense deleted:", "🗑️ Despesa excluída:"),
    "🔁 *Tus gastos recurrentes*": ("🔁 *Your recurring expenses*", "🔁 *Suas despesas recorrentes*"),
    "💰 *Tus presupuestos mensuales*": ("💰 *Your monthly budgets*", "💰 *Seus orçamentos mensais*"),
    "🔒 Ciclo cerrado: no hay diario para mostrar.": ("🔒 Closed cycle: no daily allowance is available.", "🔒 Ciclo encerrado: não há limite diário disponível."),
    "📅 El ciclo ya cerró; no hay diario para hoy.": ("📅 The cycle is closed; no daily allowance is available today.", "📅 O ciclo encerrou; não há limite diário para hoje."),
    "🔒 El ciclo está cerrado. Los números son los del snapshot; no cambian.": ("🔒 The cycle is closed. Its saved amounts are frozen.", "🔒 O ciclo está encerrado. Os valores salvos permanecem fixos."),
    "Los ciclos ya abiertos conservan sus fechas; el cambio se aplica al próximo ciclo.": ("Open cycles keep their dates; the change applies to the next cycle.", "Os ciclos abertos mantêm suas datas; a alteração vale para o próximo ciclo."),
    "ℹ️ /liberado es un alias de /disponible.": ("ℹ️ /liberado is an alias for /disponible.", "ℹ️ /liberado é um atalho para /disponible."),
    "✅ Usuario autorizado.": ("✅ User authorized.", "✅ Usuário autorizado."),
    "✅ Usuario pausado.": ("✅ User deactivated.", "✅ Usuário desativado."),
    "Usuario no encontrado.": ("User not found.", "Usuário não encontrado."),
    "Gasto no encontrado.": ("Expense not found.", "Despesa não encontrada."),
    "📍 Ubicación quitada.": ("📍 Location removed.", "📍 Localização removida."),
    "👍 Perfecto. Cuando me escribas el gasto lo registro. Si querés guardar uno nuevo, contame: *gasté X en Y*.": ("👍 Send me an expense to record it. For example, *I spent X on Y*.", "👍 Envie uma despesa para registrá-la. Por exemplo, *gastei X em Y*."),
    "🤖 MiniMax no responde en este momento. Revisá la conexión, la Subscription Key y el saldo del Token Plan.": ("🤖 MiniMax is unavailable. Check the connection, subscription key and Token Plan balance.", "🤖 O MiniMax está indisponível. Verifique a conexão, a chave de assinatura e o saldo do Token Plan."),
    "🤖 Ollama no responde en este momento. Verificá que esté corriendo (`ollama serve`) y que el modelo esté descargado (`ollama list`).": ("🤖 Ollama is unavailable. Check that it is running (`ollama serve`) and the model is downloaded (`ollama list`).", "🤖 O Ollama está indisponível. Verifique se está em execução (`ollama serve`) e se o modelo foi baixado (`ollama list`)."),
    "⚠️ No pude correr la comparación porque la base de datos no responde. Probá más tarde.": ("⚠️ The comparison is unavailable because the database isn't responding. Try again later.", "⚠️ A comparação está indisponível porque o banco não responde. Tente mais tarde."),
    "⚠️ No pude guardar el recurrente porque la base de datos no responde.": ("⚠️ The recurring expense could not be saved because the database is unavailable.", "⚠️ Não foi possível salvar a despesa recorrente porque o banco está indisponível."),
    "🤔 No pude identificar el gasto recurrente. Intentá reformularlo.": ("🤔 I couldn't identify the recurring expense. Please rephrase it.", "🤔 Não identifiquei a despesa recorrente. Reformule o pedido."),
    "Todavía no tengo gastos del mes para analizar.": ("There are no expenses this month to analyze yet.", "Ainda não há despesas deste mês para analisar."),
    "🤖 No pude consultar la IA para analizar tus gastos. Probá de nuevo en un momento.": ("🤖 The spending analysis is unavailable. Try again in a moment.", "🤖 A análise de gastos está indisponível. Tente novamente em instantes."),
    "🤖 No pude generar un análisis válido. Probá de nuevo.": ("🤖 I couldn't generate a valid analysis. Please try again.", "🤖 Não consegui gerar uma análise válida. Tente novamente."),
    "📅 No hay un ciclo abierto para hoy. El anterior ya está cerrado o el siguiente todavía no comenzó.": ("📅 There is no open cycle today. The previous cycle is closed or the next one hasn't started yet.", "📅 Não há ciclo aberto para hoje. O anterior encerrou ou o próximo ainda não começou."),
    "⚠️ No pude leer el ciclo: el historial tiene un formato no compatible.": ("⚠️ The cycle history uses an unsupported format.", "⚠️ O histórico do ciclo usa um formato não compatível."),
    "⚠️ Hay demasiados ciclos pendientes de actualizar. Revisá el historial antes de continuar.": ("⚠️ Too many cycles need updating. Review the history before continuing.", "⚠️ Há muitos ciclos para atualizar. Revise o histórico antes de continuar."),
    "⚠️ No pude resolver tu ciclo actual. Volvé a intentarlo.": ("⚠️ The current cycle could not be loaded. Please try again.", "⚠️ Não foi possível carregar o ciclo atual. Tente novamente."),
    "💼 Ingreso base actualizado.": ("💼 Base income updated.", "💼 Renda base atualizada."),
    "🎯 Target de ahorro actualizado.": ("🎯 Savings target updated.", "🎯 Meta de poupança atualizada."),
    "🔒 Reserva actualizada.": ("🔒 Savings reservation updated.", "🔒 Reserva de poupança atualizada."),
    "🔓 Reserva liberada.": ("🔓 Savings reservation released.", "🔓 Reserva de poupança liberada."),
    "⚠️ Ese extra ya fue registrado en otra moneda.": ("⚠️ That extra income was already recorded in another currency.", "⚠️ Essa renda extra já foi registrada em outra moeda."),
    "⚠️ Ese ahorro ya fue registrado en otra moneda.": ("⚠️ Those savings were already recorded in another currency.", "⚠️ Essa poupança já foi registrada em outra moeda."),
    "❓ El monto tiene que ser 0 o más.": ("❓ The amount must be zero or greater.", "❓ O valor deve ser zero ou maior."),
    "❓ El monto tiene que ser mayor a 0.": ("❓ The amount must be greater than zero.", "❓ O valor deve ser maior que zero."),
    "❓ El monto no es un número válido.": ("❓ The amount is not a valid number.", "❓ O valor não é um número válido."),
    "❓ El id tiene que ser un número entero.": ("❓ The ID must be an integer.", "❓ O ID deve ser um número inteiro."),
    "Valores válidos: 0 o más.": ("Valid values: zero or greater.", "Valores válidos: zero ou maior."),
    "Valores válidos: mayor a 0.": ("Valid values: greater than zero.", "Valores válidos: maior que zero."),
}

_ERRORS = {
    "Elegí es, en o pt.": ("Choose es, en or pt.", "Escolha es, en ou pt."),
    "Usá una zona horaria IANA válida.": ("Use a valid IANA timezone.", "Use um fuso horário IANA válido."),
    "La preferencia de voz debe ser true o false.": ("The voice preference must be true or false.", "A preferência de voz deve ser true ou false."),
    "El ID de Telegram debe ser un entero positivo válido.": ("The Telegram ID must be a valid positive integer.", "O ID do Telegram deve ser um inteiro positivo válido."),
    "Sólo el propietario puede administrar usuarios.": ("Only the owner can manage users.", "Somente o proprietário pode gerenciar usuários."),
    "No podés pausar al propietario.": ("You cannot deactivate the owner.", "Você não pode desativar o proprietário."),
    "Usuario no autorizado.": ("User is not authorized.", "Usuário não autorizado."),
    "Gasto no encontrado.": ("Expense not found.", "Despesa não encontrada."),
}


def _period(value: str, language: str) -> str:
    fixed = {
        "hoy": ("today", "hoje"), "ayer": ("yesterday", "ontem"),
        "este mes": ("this month", "este mês"), "esta semana": ("this week", "esta semana"),
        "histórico": ("all time", "todo o histórico"), "mes pasado": ("last month", "mês passado"),
        "el mes pasado": ("last month", "o mês passado"),
        "últimos gastos": ("latest expenses", "últimas despesas"),
    }
    pair = fixed.get(value.lower())
    if pair:
        return pair[0 if language == "en" else 1]
    match = re.fullmatch(r"[Úú]ltimos (\d+) días", value)
    if match:
        return f"last {match[1]} days" if language == "en" else f"últimos {match[1]} dias"
    return value


def localize_response(text: str, language: str = "es") -> str:
    """Translate known bot UI while leaving arbitrary user/AI prose intact."""
    language = _locale(language)
    if language == "es":
        return text
    index = 0 if language == "en" else 1
    if text.startswith("📖 *¿Cómo te puedo ayudar?*"):
        return translate("help", language)
    for key in ("start", "unknown", "unauthorized", "preferences_saved", "language_changed",
                "voice_on", "voice_off", "voice_unavailable", "undo_empty", "undo_done",
                "undo_conflict", "location_saved", "location_prompt", "owner_only", "timezone_saved"):
        if text == _MESSAGES[key][0]:
            return translate(key, language)
    lines = text.split("\n")
    for position, line in enumerate(lines):
        if line in _EXACT:
            lines[position] = _EXACT[line][index]
            continue
        error_prefix = next((prefix for prefix in ("⚠️ ", "❓ ", "🤔 ") if line.startswith(prefix)), "")
        error_body = line[len(error_prefix):]
        if error_body in _ERRORS:
            lines[position] = error_prefix + _ERRORS[error_body][index]
            continue
        if position == 0:
            for key in ("expense_registered", "expenses_registered", "recurring_registered"):
                match = re.fullmatch(r"([^\w\s]+\s*)?" + re.escape(_MESSAGES[key][0]), line)
                if match:
                    lines[position] = (match[1] or "") + translate(key, language)
                    break
            if lines[position] != line:
                continue
        if line.startswith("📂 Categoría: "):
            lines[position] = f"📂 {translate('category', language)}: " + line[len("📂 Categoría: "):]
            continue
        if line.startswith("📅 Fecha: "):
            lines[position] = f"📅 {translate('date', language)}: " + line[len("📅 Fecha: "):]
            continue
        # Cycle labels are generated by the formatter, not entered by users.
        replaced = False
        for key in ("income", "extra_income", "gross_income", "savings_target", "savings_reserved",
                    "savings_realized", "savings_committed", "expenses_realized", "fixed_pending", "recurring_pending", "available"):
            prefix = f"• {_MESSAGES[key][0]}: "
            if line.startswith(prefix):
                lines[position] = f"• {translate(key, language)}: " + line[len(prefix):]
                replaced = True
                break
        if replaced:
            continue
        for prefix, key in (("💰 Disponible: ", "available"), ("🔒 Comprometido: ", "savings_committed")):
            if line.startswith(prefix):
                lines[position] = prefix.split(" ", 1)[0] + " " + translate(key, language) + ": " + line[len(prefix):]
                replaced = True
                break
        if replaced:
            continue
        headers = (
            (r"📋 \*Últimos gastos \((.*)\)\*", "📋 *Latest expenses ({period})*", "📋 *Últimas despesas ({period})*"),
            (r"🏆 \*Gasto más grande \((.*)\)\*", "🏆 *Largest expense ({period})*", "🏆 *Maior despesa ({period})*"),
            (r"📊 \*Desglose por categoría \((.*)\)\*", "📊 *Spending by category ({period})*", "📊 *Gastos por categoria ({period})*"),
            (r"💸 \*Gasto (.*)\*", "💸 *Spending {period}*", "💸 *Gastos {period}*"),
        )
        for pattern, en, pt in headers:
            match = re.fullmatch(pattern, line)
            if match:
                lines[position] = (en if language == "en" else pt).format(period=_period(match[1], language))
                replaced = True
                break
        if replaced:
            continue
        match = re.fullmatch(r"No tenés gastos registrados en (.*)\.", line)
        if match:
            lines[position] = translate("no_expenses", language, period=_period(match[1], language))
            continue
        match = re.fullmatch(r"✏️ Gasto #(\d+) actualizado:", line)
        if match:
            lines[position] = f"✏️ Expense #{match[1]} updated:" if language == "en" else f"✏️ Despesa #{match[1]} atualizada:"
            continue
        match = re.fullmatch(r"(🎁 Extra|🏦 Ahorro realizado) registrado \(id #(\d+)\)\.", line)
        if match:
            savings = match[1].startswith("🏦")
            label = (("🏦 Savings transfer" if savings else "🎁 Extra income") if language == "en"
                     else ("🏦 Poupança realizada" if savings else "🎁 Renda extra"))
            action = "recorded" if language == "en" else "registrada"
            lines[position] = f"{label} {action} (id #{match[2]})."
            continue
        match = re.fullmatch(r"(✏️|🗑️) (Extra|Ahorro) #(\d+) (actualizado|borrado)\.", line)
        if match:
            label = ({"Extra": "Extra income", "Ahorro": "Savings"} if language == "en" else {"Extra": "Renda extra", "Ahorro": "Poupança"})[match[2]]
            action = ({"actualizado": "updated", "borrado": "deleted"} if language == "en" else {"actualizado": "atualizada", "borrado": "excluída"})[match[4]]
            lines[position] = f"{match[1]} {label} #{match[3]} {action}."
            continue
        match = re.fullmatch(r"❓ (?:Uso:|Usá) (.*)", line)
        if match:
            lines[position] = ("❓ Usage: " if language == "en" else "❓ Uso: ") + match[1]
            continue
        match = re.fullmatch(r"❓ El ID tiene que ser un número entero\. Ej: (.*)", line)
        if match:
            lines[position] = ("❓ The ID must be an integer. Example: " if language == "en" else "❓ O ID deve ser um número inteiro. Exemplo: ") + match[1]
            continue
        match = re.fullmatch(r"❓ No pude interpretar el monto (`.*`)\.(.*)", line)
        if match:
            tail = match[2]
            if tail.startswith(" Ej: "):
                tail = (" Example: " if language == "en" else " Exemplo: ") + tail[len(" Ej: "):]
            lines[position] = (f"❓ I couldn't read the amount {match[1]}." if language == "en" else f"❓ Não consegui ler o valor {match[1]}.") + tail
            continue
        match = re.fullmatch(r"🤷 No pude (borrar|editar) el gasto #(\d+)\.", line)
        if match:
            operation = {"borrar": ("delete", "excluir"), "editar": ("edit", "editar")}[match[1]][index]
            lines[position] = f"🤷 I couldn't {operation} expense #{match[2]}." if language == "en" else f"🤷 Não consegui {operation} a despesa #{match[2]}."
            continue
        match = re.fullmatch(r"🤷 No encontré el gasto #(\d+)\. Usá `/gastos` para ver los IDs válidos\.", line)
        if match:
            lines[position] = (f"🤷 Expense #{match[1]} was not found. Use `/gastos` to see valid IDs." if language == "en"
                               else f"🤷 Despesa #{match[1]} não encontrada. Use `/gastos` para ver os IDs válidos.")
            continue
        match = re.fullmatch(r"💰 Disponible en este ciclo: (.*)", line)
        if match:
            lines[position] = ("💰 Available this cycle: " if language == "en" else "💰 Disponível neste ciclo: ") + match[1]
            continue
        match = re.fullmatch(r"💸 Aún no tengo datos suficientes para calcular tu disponible en ([A-Z]{3})\. Cargá un ingreso o un gasto para empezar\.", line)
        if match:
            lines[position] = (f"💸 There isn't enough data to calculate your available balance in {match[1]}. Enter income or an expense to start." if language == "en"
                               else f"💸 Ainda não há dados suficientes para calcular o disponível em {match[1]}. Registre uma renda ou despesa para começar.")
            continue
        match = re.fullmatch(r"💸 Sin ingresos registrados este ciclo \(([A-Z]{3})\)\.", line)
        if match:
            lines[position] = f"💸 No income recorded for this cycle ({match[1]})." if language == "en" else f"💸 Nenhuma renda registrada neste ciclo ({match[1]})."
            continue
        match = re.fullmatch(r"📆 Ciclo (#\d+ · [A-Z]{3} .*?) — (open|closed)", line)
        if match:
            state = {"en": {"open": "open", "closed": "closed"}, "pt": {"open": "aberto", "closed": "encerrado"}}[language][match[2]]
            lines[position] = f"📆 {translate('cycle', language)} {match[1]} — {state}"
            continue
        match = re.fullmatch(r"💵 Cobro: primer (lunes|martes|miércoles|jueves|viernes|sábado|domingo) de cada mes", line)
        if match:
            weekdays = {
                "lunes": ("Monday", "segunda-feira"), "martes": ("Tuesday", "terça-feira"),
                "miércoles": ("Wednesday", "quarta-feira"), "jueves": ("Thursday", "quinta-feira"),
                "viernes": ("Friday", "sexta-feira"), "sábado": ("Saturday", "sábado"),
                "domingo": ("Sunday", "domingo"),
            }
            day = weekdays[match[1]][index]
            ordinal = "primeiro" if match[1] in {"sábado", "domingo"} else "primeira"
            lines[position] = f"💵 Payday: first {day} of each month" if language == "en" else f"💵 Recebimento: {ordinal} {day} de cada mês"
            continue
        match = re.fullmatch(r"💵 Cobro: día (\d+) de cada mes", line)
        if match:
            lines[position] = f"💵 Payday: day {match[1]} of each month" if language == "en" else f"💵 Recebimento: dia {match[1]} de cada mês"
            continue
        match = re.fullmatch(r"📅 Podés gastar (.*) por día \(quedan (\d+) día\(s\) en el ciclo\)\.", line)
        if match:
            lines[position] = (f"📅 You can spend {match[1]} per day ({match[2]} day(s) left in the cycle)." if language == "en"
                               else f"📅 Você pode gastar {match[1]} por dia ({match[2]} dia(s) restantes no ciclo).")
    return "\n".join(lines)
