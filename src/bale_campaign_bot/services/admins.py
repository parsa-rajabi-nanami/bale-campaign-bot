"""One-time, private-chat owner bootstrap."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bale_campaign_bot.db.models import Admin, BotState

Clock = Callable[[], datetime]
_STATE_KEY = "admin_setup"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ClaimStatus(StrEnum):
    CLAIMED = "claimed"
    INVALID = "invalid"
    EXPIRED = "expired"
    ALREADY_CLAIMED = "already_claimed"
    PRIVATE_CHAT_REQUIRED = "private_chat_required"


class AdminBootstrapService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        ttl_seconds: int = 3600,
        clock: Clock = _utc_now,
        code_factory: Callable[[], str] = lambda: secrets.token_urlsafe(18),
    ) -> None:
        if ttl_seconds < 60:
            raise ValueError("setup-code lifetime must be at least 60 seconds")
        self._session_factory = session_factory
        self._ttl = timedelta(seconds=ttl_seconds)
        self._clock = clock
        self._code_factory = code_factory

    async def create_setup_code(self) -> str | None:
        """Rotate and return a one-time code unless an owner is already configured."""
        code = self._code_factory()
        now = self._clock()
        value = {
            "code_hash": hashlib.sha256(code.encode("utf-8")).hexdigest(),
            "expires_at": (now + self._ttl).isoformat(),
            "claimed": False,
        }
        async with self._session_factory() as session, session.begin():
            owner = await session.scalar(select(Admin).where(Admin.role == "owner"))
            if owner is not None:
                return None
            state = await session.get(BotState, _STATE_KEY)
            if state is None:
                session.add(BotState(key=_STATE_KEY, value=value))
            else:
                state.value = value
                state.updated_at = now
            await session.flush()
        return code

    async def claim(self, *, code: str, user_id: int, chat_type: str) -> ClaimStatus:
        if chat_type != "private":
            return ClaimStatus.PRIVATE_CHAT_REQUIRED
        supplied_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
        now = self._clock()
        try:
            async with self._session_factory() as session, session.begin():
                state = await session.scalar(
                    select(BotState).where(BotState.key == _STATE_KEY).with_for_update()
                )
                if state is None or state.value.get("claimed") is True:
                    return ClaimStatus.ALREADY_CLAIMED
                expected_hash = state.value.get("code_hash")
                expires_text = state.value.get("expires_at")
                if not isinstance(expected_hash, str) or not isinstance(expires_text, str):
                    return ClaimStatus.INVALID
                try:
                    expires_at = datetime.fromisoformat(expires_text)
                except ValueError:
                    return ClaimStatus.INVALID
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
                if now >= expires_at:
                    return ClaimStatus.EXPIRED
                if not hmac.compare_digest(supplied_hash, expected_hash):
                    return ClaimStatus.INVALID
                owner = await session.scalar(select(Admin).where(Admin.role == "owner"))
                if owner is not None:
                    state.value = {"claimed": True}
                    state.updated_at = now
                    return ClaimStatus.ALREADY_CLAIMED
                session.add(Admin(user_id=user_id, role="owner"))
                state.value = {"claimed": True, "claimed_at": now.isoformat()}
                state.updated_at = now
                await session.flush()
            return ClaimStatus.CLAIMED
        except IntegrityError:
            return ClaimStatus.ALREADY_CLAIMED
