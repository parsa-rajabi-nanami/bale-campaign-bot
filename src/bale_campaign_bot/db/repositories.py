"""Persistence operations kept separate from handlers and services."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bale_campaign_bot.db.models import (
    Admin,
    BotState,
    ConversationState,
    ProcessedUpdate,
    Subscriber,
)

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SubscriberRepository:
    def __init__(self, session: AsyncSession, *, clock: Clock = _utc_now) -> None:
        self._session = session
        self._clock = clock

    async def get_by_chat_id(self, chat_id: int) -> Subscriber | None:
        result = await self._session.execute(
            select(Subscriber).where(Subscriber.chat_id == chat_id)
        )
        return result.scalar_one_or_none()

    async def opt_in(
        self,
        *,
        chat_id: int,
        first_name: str,
        last_name: str | None,
        username: str | None,
        language_code: str | None,
        source: str | None,
        consent_version: str,
        now: datetime | None = None,
    ) -> Subscriber:
        timestamp = now or self._clock()
        subscriber = await self.get_by_chat_id(chat_id)
        if subscriber is None:
            subscriber = Subscriber(
                chat_id=chat_id,
                first_name=first_name,
                last_name=last_name,
                username=username,
                language_code=language_code,
                source=source,
                is_active=True,
                subscribed_at=timestamp,
                unsubscribed_at=None,
                consent_version=consent_version,
            )
            self._session.add(subscriber)
        else:
            subscriber.first_name = first_name
            subscriber.last_name = last_name
            subscriber.username = username
            subscriber.language_code = language_code
            subscriber.source = source
            subscriber.is_active = True
            subscriber.subscribed_at = timestamp
            subscriber.unsubscribed_at = None
            subscriber.consent_version = consent_version
        await self._session.flush()
        return subscriber

    async def opt_out(self, chat_id: int, *, now: datetime | None = None) -> Subscriber | None:
        subscriber = await self.get_by_chat_id(chat_id)
        if subscriber is None:
            return None
        subscriber.is_active = False
        subscriber.unsubscribed_at = now or self._clock()
        await self._session.flush()
        return subscriber

    async def list_active_chat_ids(self) -> list[int]:
        result = await self._session.scalars(
            select(Subscriber.chat_id).where(Subscriber.is_active.is_(True)).order_by(Subscriber.id)
        )
        return list(result)


class AdminRepository:
    def __init__(self, session: AsyncSession, *, clock: Clock = _utc_now) -> None:
        self._session = session
        self._clock = clock

    async def get(self, user_id: int) -> Admin | None:
        return await self._session.get(Admin, user_id)

    async def add(self, user_id: int, *, role: str = "admin") -> Admin:
        admin = await self.get(user_id)
        if admin is None:
            admin = Admin(user_id=user_id, role=role)
            self._session.add(admin)
        else:
            admin.role = role
        await self._session.flush()
        return admin

    async def is_admin(self, user_id: int) -> bool:
        return await self.get(user_id) is not None


class ConversationRepository:
    def __init__(self, session: AsyncSession, *, clock: Clock = _utc_now) -> None:
        self._session = session
        self._clock = clock

    async def get(self, user_id: int, chat_id: int) -> ConversationState | None:
        return await self._session.get(ConversationState, (user_id, chat_id))

    async def set(
        self,
        user_id: int,
        chat_id: int,
        state: str,
        data: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> ConversationState:
        conversation = await self.get(user_id, chat_id)
        if conversation is None:
            conversation = ConversationState(
                user_id=user_id, chat_id=chat_id, state=state, data=data
            )
            self._session.add(conversation)
        else:
            conversation.state = state
            conversation.data = data
            conversation.updated_at = now or self._clock()
        await self._session.flush()
        return conversation

    async def clear(self, user_id: int, chat_id: int) -> None:
        conversation = await self.get(user_id, chat_id)
        if conversation is not None:
            await self._session.delete(conversation)
            await self._session.flush()


class UpdateRepository:
    def __init__(self, session: AsyncSession, *, clock: Clock = _utc_now) -> None:
        self._session = session
        self._clock = clock

    async def has_processed(self, update_id: int) -> bool:
        return await self._session.get(ProcessedUpdate, update_id) is not None

    async def latest_processed_id(self) -> int | None:
        return await self._session.scalar(select(func.max(ProcessedUpdate.update_id)))

    async def mark_processed(self, update_id: int) -> bool:
        if await self.has_processed(update_id):
            return False
        self._session.add(ProcessedUpdate(update_id=update_id))
        await self._session.flush()
        return True


class BotStateRepository:
    def __init__(self, session: AsyncSession, *, clock: Clock = _utc_now) -> None:
        self._session = session
        self._clock = clock

    async def get(self, key: str) -> dict[str, Any] | None:
        state = await self._session.get(BotState, key)
        return state.value if state is not None else None

    async def set(self, key: str, value: dict[str, Any]) -> None:
        state = await self._session.get(BotState, key)
        if state is None:
            self._session.add(BotState(key=key, value=value))
        else:
            state.value = value
            state.updated_at = self._clock()
        await self._session.flush()
