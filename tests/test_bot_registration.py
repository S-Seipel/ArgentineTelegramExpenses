"""Verify commands are reachable through the constructed Telegram app."""

from telegram.ext import CommandHandler

from app.ai.service import StubAIService
from app.bot.app import BotDependencies, _command_handler, build_application
from app.bot.service import BotRuntime
from app.config.settings import Settings


def test_f1_commands_are_registered_on_application():
    settings = Settings(
        telegram_bot_token="123456:TESTTOKEN",
        telegram_allowed_user_id=1,
        database_url="sqlite:///:memory:",
        ollama_base_url="http://localhost:11434",
        ollama_model="test-model",
        timezone="UTC",
    )
    app = build_application(
        settings,
        deps=BotDependencies(BotRuntime(settings=settings, ai=StubAIService())),
    )
    handlers = [
        handler
        for group in app.handlers.values()
        for handler in group
        if isinstance(handler, CommandHandler)
    ]
    registered = {
        command: handler.callback
        for handler in handlers
        for command in handler.commands
    }
    f1_commands = {
        "config_payday", "cycle", "disponible", "gastar_diario",
        "ingreso", "extra", "extra_editar", "extra_borrar", "target",
        "reservar", "ahorro_realizado", "ahorro_editar", "ahorro_borrar",
        "liberar", "repair_fixed_payments",
        "recurrente_obligacion",
    }
    assert f1_commands <= registered.keys()
    assert all(registered[name] is _command_handler for name in f1_commands)
    assert registered["liberado"] is _command_handler
    assert {"gastosfijos", "pague", "salte", "hoy", "recurrentes"} <= registered.keys()
