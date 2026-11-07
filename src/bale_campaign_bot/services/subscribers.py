"""Subscriber opt-in, opt-out, and explicitly shared phone-number operations."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bale_campaign_bot.bale.models import Chat, User
from bale_campaign_bot.db.models import Subscriber
from bale_campaign_bot.db.repositories import SubscriberRepository

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def normalize_iranian_mobile(phone_number: str) -> str | None:
    """Normalize Iranian 09xx, +98 9xx, and 0098 9xx mobile formats to E.164."""
    stripped = "".join(
        str(unicodedata.digit(character)) if character.isdigit() else character
        for character in phone_number.strip()
    )
    compact = re.sub(r"[\s().-]", "", stripped)
    if re.fullmatch(r"\+?\d+", compact) is None:
        return None
    digits = re.sub(r"\D", "", compact)
    if stripped.startswith("+"):
        if not digits.startswith("98"):
            return None
        national = digits[2:]
    elif digits.startswith("0098"):
        national = digits[4:]
    elif digits.startswith("98"):
        national = digits[2:]
    elif digits.startswith("0"):
        national = digits[1:]
    else:
        return None
    if len(national) != 10 or not national.startswith("9"):
        return None
    return f"+98{national}"


class PhoneLinkStatus(StrEnum):
    LINKED = "linked"
    INVALID = "invalid"
    NOT_SUBSCRIBED = "not_subscribed"
    ALREADY_LINKED = "already_linked"


class SubscriberService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def start(
        self,
        *,
        user: User,
        chat: Chat,
        source: str | None,
        consent_version: str,
    ) -> Subscriber:
        async with self._session_factory() as session, session.begin():
            repository = SubscriberRepository(session, clock=self._clock)
            return await repository.opt_in(
                chat_id=chat.id,
                first_name=user.first_name,
                last_name=user.last_name,
                username=user.username,
                language_code=user.language_code,
                source=source,
                consent_version=consent_version,
                now=self._clock(),
            )

    async def stop(self, chat_id: int) -> bool:
        async with self._session_factory() as session, session.begin():
            subscriber = await SubscriberRepository(session, clock=self._clock).opt_out(
                chat_id,
                now=self._clock(),
            )
            return subscriber is not None

    async def link_phone(self, chat_id: int, phone_number: str) -> PhoneLinkStatus:
        normalized = normalize_iranian_mobile(phone_number)
        if normalized is None:
            return PhoneLinkStatus.INVALID
        try:
            async with self._session_factory() as session, session.begin():
                subscriber = await SubscriberRepository(session, clock=self._clock).get_by_chat_id(
                    chat_id
                )
                if subscriber is None or not subscriber.is_active:
                    return PhoneLinkStatus.NOT_SUBSCRIBED
                if subscriber.phone_e164 not in (None, normalized):
                    return PhoneLinkStatus.ALREADY_LINKED
                subscriber.phone_e164 = normalized
                await session.flush()
            return PhoneLinkStatus.LINKED
        except IntegrityError:
            return PhoneLinkStatus.ALREADY_LINKED
