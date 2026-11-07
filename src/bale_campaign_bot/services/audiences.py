"""Tag management and opt-in-only audience counts."""

from __future__ import annotations

import re

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bale_campaign_bot.db.repositories import TagRepository

_TAG_NAME = re.compile(r"^[\w .-]{1,80}$", re.UNICODE)


class AudienceError(ValueError):
    """Invalid, missing, or duplicate audience data."""


class AudienceService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_tag(self, name: str) -> str:
        normalized = name.strip()
        if not _TAG_NAME.fullmatch(normalized):
            raise AudienceError("Tag names must be 1-80 letters, digits, spaces, dots, or hyphens.")
        try:
            async with self._session_factory() as session, session.begin():
                if await TagRepository(session).get_by_name(normalized) is not None:
                    raise AudienceError("A tag with this name already exists.")
                await TagRepository(session).create(normalized)
        except IntegrityError:
            raise AudienceError("A tag with this name already exists.") from None
        return normalized

    async def assign_tag(self, name: str, chat_id: int) -> bool:
        async with self._session_factory() as session, session.begin():
            return await TagRepository(session).assign_to_chat(name.strip(), chat_id)

    async def list_tags(self) -> list[str]:
        async with self._session_factory() as session:
            return await TagRepository(session).list_names()

    async def count(
        self,
        *,
        include_tags: tuple[str, ...] = (),
        exclude_tags: tuple[str, ...] = (),
    ) -> int:
        async with self._session_factory() as session:
            repo = TagRepository(session)
            available = set(await repo.list_names())
            unknown = (set(include_tags) | set(exclude_tags)) - available
            if unknown:
                raise AudienceError(f"Unknown tag(s): {', '.join(sorted(unknown))}")
            return await repo.count_active(
                include_tags=tuple(dict.fromkeys(include_tags)),
                exclude_tags=tuple(dict.fromkeys(exclude_tags)),
            )
