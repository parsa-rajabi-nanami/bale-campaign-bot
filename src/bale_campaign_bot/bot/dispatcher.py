"""Long-polling update loop with database-backed offsets and deduplication."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bale_campaign_bot.bale.client import BaleClient
from bale_campaign_bot.bale.errors import BaleError
from bale_campaign_bot.bale.models import Update
from bale_campaign_bot.bot.router import BotRouter
from bale_campaign_bot.db.repositories import UpdateRepository

Sleep = Callable[[float], Awaitable[None]]
logger = structlog.get_logger(__name__)


class UpdateDispatcher:
    def __init__(
        self,
        client: BaleClient,
        router: BotRouter,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        poll_timeout: int = 30,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if poll_timeout < 0:
            raise ValueError("poll_timeout cannot be negative")
        self._client = client
        self._router = router
        self._session_factory = session_factory
        self._poll_timeout = poll_timeout
        self._sleep = sleep
        self._dispatch_lock = asyncio.Lock()

    async def run(self, stop_event: asyncio.Event) -> None:
        async with self._session_factory() as session:
            last_id = await UpdateRepository(session).latest_processed_id()
        offset = last_id + 1 if last_id is not None else None
        failure_count = 0
        while not stop_event.is_set():
            try:
                updates = await self._client.get_updates(
                    offset=offset,
                    limit=100,
                    timeout=self._poll_timeout,
                )
            except BaleError as exc:
                failure_count += 1
                delay = min(30.0, 0.5 * (2 ** min(failure_count - 1, 6)))
                logger.warning(
                    "bale_poll_failed",
                    method=exc.method,
                    error_type=type(exc).__name__,
                    retry_delay=delay,
                )
                await self._sleep(delay)
                continue
            failure_count = 0
            for update in updates:
                if stop_event.is_set():
                    return
                await self.dispatch_update(update)
                offset = max(offset or 0, update.update_id + 1)

    async def dispatch_update(self, update: Update) -> bool:
        """Deduplicate and route one typed update; returns false for a known duplicate."""
        async with self._dispatch_lock:
            async with self._session_factory() as session:
                processed = await UpdateRepository(session).has_processed(update.update_id)
            if processed:
                return False
            await self._router.dispatch(update)
            async with self._session_factory() as session, session.begin():
                await UpdateRepository(session).mark_processed(update.update_id)
            return True
