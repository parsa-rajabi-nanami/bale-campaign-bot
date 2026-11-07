"""Private-chat owner claim handler and reusable strict admin check."""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bale_campaign_bot.bale.client import BaleClient
from bale_campaign_bot.bale.models import Message
from bale_campaign_bot.bot.handlers.public import parse_command
from bale_campaign_bot.bot.i18n import fa
from bale_campaign_bot.db.repositories import AdminRepository
from bale_campaign_bot.services.admins import AdminBootstrapService, ClaimStatus


class AdminHandlers:
    def __init__(self, client: BaleClient, bootstrap: AdminBootstrapService) -> None:
        self._client = client
        self._bootstrap = bootstrap

    async def handle_message(self, message: Message) -> None:
        if message.chat.type != "private" or message.from_user is None:
            return
        command, args = parse_command(message.text or "")
        if command != "/claim":
            return
        if len(args) != 1:
            await self._client.send_message(message.chat.id, fa.CLAIM_USAGE)
            return
        status = await self._bootstrap.claim(
            code=args[0],
            user_id=message.from_user.id,
            chat_type=message.chat.type,
        )
        messages = {
            ClaimStatus.CLAIMED: fa.CLAIM_SUCCESS,
            ClaimStatus.INVALID: fa.CLAIM_INVALID,
            ClaimStatus.EXPIRED: fa.CLAIM_EXPIRED,
            ClaimStatus.ALREADY_CLAIMED: fa.CLAIM_USED,
            ClaimStatus.PRIVATE_CHAT_REQUIRED: fa.CLAIM_PRIVATE_ONLY,
        }
        await self._client.send_message(message.chat.id, messages[status])


async def is_authorized_admin(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: int,
    chat_type: str,
    configured_admin_ids: Collection[int],
) -> bool:
    """Require a private chat and either an environment allowlist or DB admin row."""
    if chat_type != "private":
        return False
    if user_id in configured_admin_ids:
        return True
    async with session_factory() as session:
        return await AdminRepository(session).is_admin(user_id)
