"""High-level Telegram bot orchestration.

This module wires the AI service, expense service, query service and the
formatting layer into the python-telegram-bot ``Application``.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.ai.service import AIService
from app.bot.service import BotRuntime
from app.config.settings import Settings

logger = logging.getLogger(__name__)


class _SilentStop(Exception):
    """Internal control flow to abort handlers without raising to PTB."""


class BotDependencies:
    def __init__(self, runtime: BotRuntime) -> None:
        self.runtime = runtime

    @property
    def ai(self) -> AIService:
        return self.runtime.ai

    @property
    def settings(self) -> Settings:
        return self.runtime.settings


def build_application(
    settings: Settings, deps: BotDependencies | None = None
) -> Application:
    if deps is None:
        deps = BotDependencies(runtime=BotRuntime.create(settings))

    application = ApplicationBuilder().token(settings.telegram_bot_token).build()
    application.bot_data["deps"] = deps
    application.bot_data["settings"] = settings

    application.add_handler(
        CommandHandler(
            ["start", "help", "ayuda"], _command_handler
        )
    )
    application.add_handler(
        CommandHandler(["hoy", "semana", "mes", "gastos"], _command_handler)
    )
    application.add_handler(
        CommandHandler(
            ["desglose", "desglose_hoy", "desglose_semana", "desglose_mes"],
            _command_handler,
        )
    )
    application.add_handler(
        CommandHandler(["borrar", "borrar_ultimo"], _command_handler)
    )
    application.add_handler(
        CommandHandler(["editar", "editar_ultimo"], _command_handler)
    )
    application.add_handler(
        CommandHandler(["exportar", "csv"], _command_handler)
    )
    application.add_handler(
        CommandHandler(
            [
                "recurrente_add",
                "recurrente_add_anual",
                "recurrentes",
                "recurrente_del",
                "recurrente_off",
                "recurrente_on",
            ],
            _command_handler,
        )
    )
    application.add_handler(
        CommandHandler(
            [
                "presupuesto",
                "presupuestos",
                "presupuesto_del",
                "presupuesto_off",
                "presupuesto_on",
            ],
            _command_handler,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.VOICE | filters.AUDIO, _voice_handler
        )
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _text_handler)
    )

    return application


async def _command_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    from app.bot.handlers import handle_command

    deps: BotDependencies = context.application.bot_data["deps"]
    await handle_command(update, context, deps)


async def _text_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    from app.bot.handlers import handle_text

    deps: BotDependencies = context.application.bot_data["deps"]
    await handle_text(update, context, deps)


async def _voice_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    from app.bot.handlers import handle_voice

    deps: BotDependencies = context.application.bot_data["deps"]
    await handle_voice(update, context, deps)


__all__ = [
    "BotDependencies",
    "BotRuntime",
    "build_application",
    "_SilentStop",
]
