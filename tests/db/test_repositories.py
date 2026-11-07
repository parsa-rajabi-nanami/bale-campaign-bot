from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bale_campaign_bot.db.models import Admin, Base, Subscriber, SubscriberTag, Tag
from bale_campaign_bot.db.repositories import (
    AdminRepository,
    BotStateRepository,
    ConversationRepository,
    SubscriberRepository,
    UpdateRepository,
)
from bale_campaign_bot.db.session import create_engine, create_session_factory

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def database_session() -> AsyncIterator[AsyncSession]:
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        yield session
    await engine.dispose()


async def test_subscriber_opt_in_out_and_resubscribe(database_session: AsyncSession) -> None:
    repo = SubscriberRepository(database_session)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    subscriber = await repo.opt_in(
        chat_id=100,
        first_name="A",
        last_name=None,
        username="ada",
        language_code="fa",
        source="campaign-x",
        consent_version="v1",
        now=now,
    )
    assert subscriber.is_active
    assert subscriber.subscribed_at == now
    await repo.opt_out(100, now=now)
    opted_out = await repo.get_by_chat_id(100)
    assert opted_out is not None and opted_out.is_active is False
    assert await repo.list_active_chat_ids() == []

    reactivated = await repo.opt_in(
        chat_id=100,
        first_name="Ada",
        last_name="Lovelace",
        username=None,
        language_code="en",
        source=None,
        consent_version="v2",
        now=now,
    )
    assert reactivated.id == subscriber.id
    assert reactivated.is_active
    assert reactivated.consent_version == "v2"
    assert await repo.list_active_chat_ids() == [100]


async def test_admin_and_persistent_conversation_state(database_session: AsyncSession) -> None:
    admins = AdminRepository(database_session)
    admin = await admins.add(101, role="owner")
    assert admin.role == "owner"
    assert await admins.is_admin(101)
    assert not await admins.is_admin(202)
    await admins.add(101, role="admin")
    assert (await admins.get(101)).role == "admin"  # type: ignore[union-attr]

    conversations = ConversationRepository(database_session)
    state = await conversations.set(101, 101, "campaign_name", {"draft_id": 3})
    assert state.data == {"draft_id": 3}
    state = await conversations.set(101, 101, "campaign_payload", {"draft_id": 3})
    assert state.state == "campaign_payload"
    await conversations.clear(101, 101)
    assert await conversations.get(101, 101) is None


async def test_update_dedup_and_bot_state(database_session: AsyncSession) -> None:
    updates = UpdateRepository(database_session)
    assert not await updates.has_processed(5)
    assert await updates.mark_processed(5)
    assert await updates.has_processed(5)
    assert not await updates.mark_processed(5)

    bot_state = BotStateRepository(database_session)
    assert await bot_state.get("owner") is None
    await bot_state.set("owner", {"user_id": 100})
    assert await bot_state.get("owner") == {"user_id": 100}
    await bot_state.set("owner", {"user_id": 101})
    assert await bot_state.get("owner") == {"user_id": 101}


async def test_sqlite_foreign_keys_enforce_tag_assignment_cascade(
    database_session: AsyncSession,
) -> None:
    subscriber = Subscriber(
        chat_id=77,
        first_name="User",
        is_active=True,
        subscribed_at=datetime.now(UTC),
        consent_version="v1",
    )
    tag = Tag(name="vip")
    database_session.add_all([subscriber, tag])
    await database_session.flush()
    assignment = SubscriberTag(subscriber_id=subscriber.id, tag_id=tag.id)
    database_session.add(assignment)
    await database_session.flush()
    await database_session.execute(delete(Subscriber).where(Subscriber.id == subscriber.id))
    assert (
        await database_session.scalar(
            select(SubscriberTag.subscriber_id).where(SubscriberTag.subscriber_id == subscriber.id)
        )
        is None
    )
    assert await database_session.get(Admin, 77) is None
