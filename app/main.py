"""Top-level FastAPI application that hosts the Telegram bot + healthcheck.

The bot runs inside the same asyncio loop as the FastAPI app. We intentionally
avoid calling ``application.run_polling()`` (which blocks) and instead drive
the PTB application lifecycle manually.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.bot.app import BotDependencies, build_application
from app.bot.service import BotRuntime
from app.categories import ensure_seed, refresh_from_db
from app.config.settings import get_settings
from app.database.database import get_session_factory
from app.recurring.scheduler import recurring_reminder_loop

logger = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _hydrate_categories() -> None:
    """Make sure the ``categories`` table is seeded and the registry is fresh."""
    factory = get_session_factory()
    with factory() as session:
        from app.categories.repository import CategoryRepository

        repo = CategoryRepository(session)
        rows = ensure_seed(repo)
    refresh_from_db(factory)
    logger.info("Categories ready: %d rows", len(rows))


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(get_settings().log_level)
    settings = get_settings()

    deps = BotDependencies(runtime=BotRuntime.create(settings))

    try:
        _hydrate_categories()
    except Exception:
        logger.exception("Categories hydration failed; continuing without DB")

    bot_app = None
    try:
        bot_app = build_application(settings, deps=deps)
        await bot_app.initialize()
        if bot_app.updater:
            await bot_app.updater.start_polling()
        await bot_app.start()
        logger.info("Telegram bot started; FastAPI ready")
    except Exception:
        logger.exception(
            "Telegram bot failed to start (likely bad token or network). "
            "API will continue running."
        )
        bot_app = None

    scheduler_task: asyncio.Task | None = None
    if bot_app is not None:
        scheduler_task = asyncio.create_task(
            recurring_reminder_loop(bot_app, deps.runtime),
            name="recurring-reminder-loop",
        )

    app.state.bot_application = bot_app
    app.state.deps = deps

    try:
        yield
    finally:
        logger.info("Shutting down")
        if scheduler_task is not None:
            scheduler_task.cancel()
            try:
                await scheduler_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Error stopping reminder scheduler")
        if bot_app is not None:
            try:
                if bot_app.updater:
                    await bot_app.updater.stop()
                await bot_app.stop()
                await bot_app.shutdown()
            except Exception:
                logger.exception("Error stopping Telegram bot")
        ai = deps.runtime.ai
        close = getattr(ai, "aclose", None)
        if close is not None:
            try:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("Error closing AI service")


app = FastAPI(
    title="Telegram Expenses",
    version="0.1.0",
    lifespan=lifespan,
)

from app.web.routes import router as web_router  # noqa: E402

app.include_router(web_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/")
async def root() -> dict:
    return {"name": "telegram-expenses", "version": "0.1.0"}
