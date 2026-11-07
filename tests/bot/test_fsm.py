from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bale_campaign_bot.bot.fsm import FSMContext
from bale_campaign_bot.db.models import ConversationState


async def test_database_fsm_survives_new_context_instances(
    test_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 5, 2, tzinfo=UTC)
    first_context = FSMContext(
        test_session_factory,
        user_id=11,
        chat_id=11,
        clock=lambda: now,
    )
    await first_context.set("campaign_payload", {"source_message_id": 18})

    restarted_context = FSMContext(
        test_session_factory,
        user_id=11,
        chat_id=11,
        clock=lambda: now,
    )
    assert await restarted_context.get() == (
        "campaign_payload",
        {"source_message_id": 18},
    )
    await restarted_context.clear()
    assert await first_context.get() is None

    async with test_session_factory() as session:
        state = await session.get(ConversationState, (11, 11))
    assert state is None
