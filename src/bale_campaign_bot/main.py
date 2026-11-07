"""Runtime entry point for the long-polling bot process."""

from __future__ import annotations

import asyncio
import signal
import sys

import structlog
from pydantic import ValidationError

from bale_campaign_bot.bale.client import BaleClient
from bale_campaign_bot.bot.dispatcher import UpdateDispatcher
from bale_campaign_bot.bot.handlers.admin import AdminHandlers
from bale_campaign_bot.bot.handlers.public import PublicHandlers
from bale_campaign_bot.bot.router import BotRouter
from bale_campaign_bot.config.business import BusinessConfigError, load_business_config
from bale_campaign_bot.config.settings import Settings
from bale_campaign_bot.db.session import create_engine, create_session_factory, ensure_sqlite_parent
from bale_campaign_bot.logging import configure_logging
from bale_campaign_bot.services.admins import AdminBootstrapService
from bale_campaign_bot.services.subscribers import SubscriberService


async def _serve(settings: Settings) -> None:
    logger = structlog.get_logger(__name__)
    business = load_business_config(settings.business_config_path)
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for handled_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(handled_signal, stop_event.set)
        except NotImplementedError:
            signal.signal(handled_signal, lambda *_: loop.call_soon_threadsafe(stop_event.set))

    try:
        async with BaleClient(settings.token_value or "", proxy=settings.https_proxy) as client:
            bot = await client.get_me()
            setup_service = AdminBootstrapService(
                session_factory,
                ttl_seconds=settings.setup_code_ttl_seconds,
            )
            setup_code = await setup_service.create_setup_code()
            if setup_code is not None:
                print(
                    "One-time owner claim code "
                    f"(expires in {settings.setup_code_ttl_seconds} seconds): "
                    f"{setup_code}",
                    file=sys.stderr,
                    flush=True,
                )
            logger.info("bot_started", bot_id=bot.id, bot_username=bot.username)
            subscriber_service = SubscriberService(session_factory)
            public_handlers = PublicHandlers(client, subscriber_service, business)
            admin_handlers = AdminHandlers(
                client,
                setup_service,
                session_factory,
                business,
                configured_admin_ids=settings.admin_id_set,
            )
            router = BotRouter(client, public_handlers, admin_handlers)
            dispatcher = UpdateDispatcher(client, router, session_factory)
            dispatcher_task = asyncio.create_task(
                dispatcher.run(stop_event), name="bale-update-poller"
            )
            stop_task = asyncio.create_task(stop_event.wait(), name="shutdown-signal")
            try:
                done, _ = await asyncio.wait(
                    {dispatcher_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if dispatcher_task in done:
                    dispatcher_task.result()
            finally:
                stop_event.set()
                if not dispatcher_task.done():
                    dispatcher_task.cancel()
                stop_task.cancel()
                await asyncio.gather(dispatcher_task, stop_task, return_exceptions=True)
    finally:
        await engine.dispose()
        logger.info("bot_stopped")


def main() -> int:
    try:
        settings = Settings()
    except ValidationError as exc:
        details = "\n".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False, include_context=False, include_url=False)
        )
        print(f"Invalid settings / تنظیمات نامعتبر:\n{details}", file=sys.stderr)
        return 2
    if settings.token_value is None:
        print(
            "BALE_BOT_TOKEN is required. Run `python -m bale_campaign_bot.cli init`.",
            file=sys.stderr,
        )
        return 2
    try:
        load_business_config(settings.business_config_path)
        ensure_sqlite_parent(settings.database_url)
        from bale_campaign_bot.cli import run_migrations

        run_migrations(settings.database_url)
    except (BusinessConfigError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    configure_logging(json_logs=settings.log_json, log_level=settings.log_level)
    logger = structlog.get_logger(__name__)
    try:
        asyncio.run(_serve(settings))
    except Exception as exc:
        logger.error("bot_stopped_with_error", error_type=type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
