"""Database-backed per-user conversation state."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bale_campaign_bot.db.repositories import ConversationRepository

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class FSMContext:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        user_id: int,
        chat_id: int,
        clock: Clock = _utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._user_id = user_id
        self._chat_id = chat_id
        self._clock = clock

    async def get(self) -> tuple[str, dict[str, Any]] | None:
        async with self._session_factory() as session:
            state = await ConversationRepository(session, clock=self._clock).get(
                self._user_id,
                self._chat_id,
            )
            return (state.state, dict(state.data)) if state is not None else None

    async def set(self, state: str, data: dict[str, Any] | None = None) -> None:
        async with self._session_factory() as session, session.begin():
            await ConversationRepository(session, clock=self._clock).set(
                self._user_id,
                self._chat_id,
                state,
                dict(data or {}),
                now=self._clock(),
            )

    async def clear(self) -> None:
        async with self._session_factory() as session, session.begin():
            await ConversationRepository(session, clock=self._clock).clear(
                self._user_id,
                self._chat_id,
            )
