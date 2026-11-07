from __future__ import annotations

import json

import httpx
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bale_campaign_bot.bale.client import BaleClient
from bale_campaign_bot.bale.models import Chat, Message, User
from bale_campaign_bot.bot.handlers.admin import AdminHandlers
from bale_campaign_bot.config.business import validate_business_config
from bale_campaign_bot.db.models import Admin
from bale_campaign_bot.services.admins import AdminBootstrapService


async def test_claim_handler_requires_private_chat_and_claims_owner(
    fake_bale_server: respx.MockRouter,
    test_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sent: list[dict[str, object]] = []

    def send_message(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        sent.append(payload)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "message_id": len(sent),
                    "date": 1_700_000_000,
                    "chat": {"id": payload["chat_id"], "type": "private"},
                    "text": payload["text"],
                },
            },
        )

    fake_bale_server.post("sendMessage").mock(side_effect=send_message)
    bootstrap = AdminBootstrapService(
        test_session_factory,
        code_factory=lambda: "claim-me",
    )
    assert await bootstrap.create_setup_code() == "claim-me"
    client = BaleClient("TEST_TOKEN")
    business = validate_business_config(
        {
            "business": {
                "name": "Demo",
                "short_description": "Demo",
                "welcome_message": "Welcome",
                "about_text": "About",
                "consent_text": "I agree",
                "consent_version": "v1",
                "opt_out_confirmation_text": "Stopped",
            }
        }
    )
    handlers = AdminHandlers(
        client,
        bootstrap,
        test_session_factory,
        business,
        configured_admin_ids=(),
    )

    async with client:
        await handlers.handle_message(
            Message(
                message_id=1,
                date=1_700_000_000,
                chat=Chat(id=-10, type="group"),
                from_user=User(id=50, is_bot=False, first_name="Owner"),
                text="/claim claim-me",
            )
        )
        async with test_session_factory() as session:
            assert await session.scalar(select(Admin)) is None

        await handlers.handle_message(
            Message(
                message_id=2,
                date=1_700_000_001,
                chat=Chat(id=50, type="private"),
                from_user=User(id=50, is_bot=False, first_name="Owner"),
                text="/claim claim-me",
            )
        )

    async with test_session_factory() as session:
        owner = await session.scalar(select(Admin).where(Admin.role == "owner"))
    assert owner is not None and owner.user_id == 50
    assert [payload["text"] for payload in sent] == ["مالک ربات با موفقیت ثبت شد."]
