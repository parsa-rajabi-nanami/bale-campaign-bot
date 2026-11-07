from __future__ import annotations

import asyncio
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bale_campaign_bot.bale.client import BaleClient
from bale_campaign_bot.bale.models import Update
from bale_campaign_bot.bot.dispatcher import UpdateDispatcher
from bale_campaign_bot.bot.handlers.admin import is_authorized_admin
from bale_campaign_bot.bot.router import BotRouter, CommandRateLimiter
from bale_campaign_bot.db.models import Admin, ProcessedUpdate


class FakePollingClient:
    def __init__(self, updates: list[list[Update]]) -> None:
        self._updates = iter(updates)
        self.offsets: list[int | None] = []
        self.stop_event: asyncio.Event | None = None

    async def get_updates(
        self,
        *,
        offset: int | None,
        limit: int,
        timeout: int,
    ) -> list[Update]:
        assert limit == 100
        assert timeout == 30
        self.offsets.append(offset)
        try:
            return next(self._updates)
        except StopIteration:
            assert self.stop_event is not None
            self.stop_event.set()
            return []


class FakeUpdateRouter:
    def __init__(self) -> None:
        self.update_ids: list[int] = []

    async def dispatch(self, update: Update) -> None:
        self.update_ids.append(update.update_id)


async def test_dispatcher_persists_dedupe_and_restarts_from_next_offset(
    test_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = FakePollingClient([[Update(update_id=4)]])
    router = FakeUpdateRouter()
    dispatcher = UpdateDispatcher(
        cast(BaleClient, client),
        cast(BotRouter, router),
        test_session_factory,
        sleep=lambda _: asyncio.sleep(0),
    )
    stop_event = asyncio.Event()
    client.stop_event = stop_event
    await dispatcher.run(stop_event)
    assert client.offsets == [None, 5]
    assert router.update_ids == [4]

    async with test_session_factory() as session:
        processed = await session.get(ProcessedUpdate, 4)
        latest = await session.scalar(select(ProcessedUpdate.update_id))
    assert processed is not None
    assert latest == 4


async def test_dispatcher_skips_previously_processed_updates(
    test_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with test_session_factory() as session, session.begin():
        session.add(ProcessedUpdate(update_id=7))
    client = FakePollingClient([[Update(update_id=7)]])
    router = FakeUpdateRouter()
    dispatcher = UpdateDispatcher(
        cast(BaleClient, client),
        cast(BotRouter, router),
        test_session_factory,
    )
    stop_event = asyncio.Event()
    client.stop_event = stop_event
    await dispatcher.run(stop_event)
    assert router.update_ids == []
    assert client.offsets == [8, 8]


async def test_admin_authorization_requires_private_chat_and_allowlist_or_record(
    test_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert not await is_authorized_admin(
        test_session_factory,
        user_id=100,
        chat_type="private",
        configured_admin_ids=set(),
    )
    assert not await is_authorized_admin(
        test_session_factory,
        user_id=100,
        chat_type="group",
        configured_admin_ids={100},
    )
    assert await is_authorized_admin(
        test_session_factory,
        user_id=100,
        chat_type="private",
        configured_admin_ids={100},
    )
    async with test_session_factory() as session, session.begin():
        session.add(Admin(user_id=200, role="admin"))
    assert await is_authorized_admin(
        test_session_factory,
        user_id=200,
        chat_type="private",
        configured_admin_ids=set(),
    )


async def test_command_rate_limiter_is_per_user_and_bounded() -> None:
    timestamp = [0.0]
    limiter = CommandRateLimiter(
        interval_seconds=1.0,
        max_entries=2,
        clock=lambda: timestamp[0],
    )
    assert limiter.allow(1)
    assert not limiter.allow(1)
    timestamp[0] = 1.0
    assert limiter.allow(1)
    assert limiter.allow(2)
    timestamp[0] = 1.5
    assert limiter.allow(3)
