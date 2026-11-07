from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bale_campaign_bot.db.models import Admin, BotState
from bale_campaign_bot.services.admins import AdminBootstrapService, ClaimStatus


async def test_claim_is_private_single_use_and_stores_only_hash(
    test_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 5, 1, tzinfo=UTC)
    service = AdminBootstrapService(
        test_session_factory,
        ttl_seconds=120,
        clock=lambda: now,
        code_factory=lambda: "one-time-claim-code",
    )
    code = await service.create_setup_code()
    assert code == "one-time-claim-code"
    assert await service.claim(code=code, user_id=4, chat_type="group") is (
        ClaimStatus.PRIVATE_CHAT_REQUIRED
    )
    assert await service.claim(code="wrong", user_id=4, chat_type="private") is ClaimStatus.INVALID
    assert await service.claim(code=code, user_id=4, chat_type="private") is ClaimStatus.CLAIMED
    assert await service.claim(code=code, user_id=5, chat_type="private") is (
        ClaimStatus.ALREADY_CLAIMED
    )
    assert await service.create_setup_code() is None

    async with test_session_factory() as session:
        owners = await session.scalars(select(Admin).where(Admin.role == "owner"))
        setup = await session.get(BotState, "admin_setup")
    assert [admin.user_id for admin in owners] == [4]
    assert setup is not None
    assert "code_hash" not in setup.value
    assert setup.value["claimed"] is True


async def test_claim_code_expires_and_can_be_rotated_on_restart(
    test_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 5, 1, tzinfo=UTC)
    current = [now]
    codes = iter(("expired-code", "new-code"))
    service = AdminBootstrapService(
        test_session_factory,
        ttl_seconds=60,
        clock=lambda: current[0],
        code_factory=lambda: next(codes),
    )
    assert await service.create_setup_code() == "expired-code"
    current[0] = now + timedelta(seconds=60)
    assert await service.claim(code="expired-code", user_id=8, chat_type="private") is (
        ClaimStatus.EXPIRED
    )
    assert await service.create_setup_code() == "new-code"
    assert await service.claim(code="expired-code", user_id=8, chat_type="private") is (
        ClaimStatus.INVALID
    )
    assert await service.claim(code="new-code", user_id=8, chat_type="private") is (
        ClaimStatus.CLAIMED
    )
