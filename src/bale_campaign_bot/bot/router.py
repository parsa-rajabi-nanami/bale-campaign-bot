"""Routes Bale updates to public or owner-bootstrap handlers."""

from __future__ import annotations

import time
from collections.abc import Callable

from bale_campaign_bot.bale.client import BaleClient
from bale_campaign_bot.bale.models import Update
from bale_campaign_bot.bot.handlers.admin import AdminHandlers
from bale_campaign_bot.bot.handlers.public import PublicHandlers, parse_command
from bale_campaign_bot.bot.i18n.fa import RATE_LIMITED

MonotonicClock = Callable[[], float]


class CommandRateLimiter:
    """Bounded per-user command spacing to limit abusive command bursts."""

    def __init__(
        self,
        *,
        interval_seconds: float = 1.0,
        max_entries: int = 10_000,
        clock: MonotonicClock = time.monotonic,
    ) -> None:
        if interval_seconds <= 0 or max_entries < 1:
            raise ValueError("rate-limit interval and capacity must be positive")
        self._interval = interval_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._last_command_at: dict[int, float] = {}

    def allow(self, user_id: int) -> bool:
        now = self._clock()
        previous = self._last_command_at.get(user_id)
        if previous is not None and now - previous < self._interval:
            return False
        if len(self._last_command_at) >= self._max_entries and user_id not in self._last_command_at:
            cutoff = now - self._interval
            self._last_command_at = {
                stored_id: timestamp
                for stored_id, timestamp in self._last_command_at.items()
                if timestamp >= cutoff
            }
            if len(self._last_command_at) >= self._max_entries:
                oldest = min(self._last_command_at, key=self._last_command_at.__getitem__)
                del self._last_command_at[oldest]
        self._last_command_at[user_id] = now
        return True


class BotRouter:
    def __init__(
        self,
        client: BaleClient,
        public_handlers: PublicHandlers,
        admin_handlers: AdminHandlers,
        *,
        rate_limiter: CommandRateLimiter | None = None,
    ) -> None:
        self._client = client
        self._public = public_handlers
        self._admin = admin_handlers
        self._rate_limiter = rate_limiter or CommandRateLimiter()

    async def dispatch(self, update: Update) -> None:
        if update.callback_query is not None:
            await self._public.handle_callback(update.callback_query)
            return
        message = update.message
        if message is None:
            return
        command, _ = parse_command(message.text or "")
        if (
            command
            and message.chat.type == "private"
            and message.from_user is not None
            and not self._rate_limiter.allow(message.from_user.id)
        ):
            await self._client.send_message(message.chat.id, RATE_LIMITED)
            return
        if await self._admin.handle_message(message):
            return
        await self._public.handle_message(message)
