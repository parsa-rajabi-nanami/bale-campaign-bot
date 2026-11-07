from __future__ import annotations

import json

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bale_campaign_bot.bale.client import BaleClient
from bale_campaign_bot.bale.models import Chat, Message, User
from bale_campaign_bot.bot.handlers.admin import AdminHandlers
from bale_campaign_bot.config.business import validate_business_config
from bale_campaign_bot.db.models import Admin
from bale_campaign_bot.services.admins import AdminBootstrapService
from bale_campaign_bot.services.subscribers import SubscriberService


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


@pytest.mark.asyncio
async def test_admin_contact_import_matches_existing_linked_subscribers_only(
    fake_bale_server: respx.MockRouter,
    test_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []

    def send_message(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        sent.append(payload["text"])
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
    fake_bale_server.post("getFile").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "result": {"file_id": "upload", "file_path": "files/list.csv"}},
        )
    )
    subscriber_service = SubscriberService(test_session_factory)
    await subscriber_service.start(
        user=User(id=90, is_bot=False, first_name="Subscriber"),
        chat=Chat(id=90, type="private"),
        source=None,
        consent_version="v1",
    )
    await subscriber_service.link_phone(90, "09123456789")

    client = BaleClient("TEST_TOKEN")

    async def download_file(_path: str, *, max_bytes: int) -> bytes:
        assert max_bytes == 1_000_000
        return b"phone\n09123456789\n+989876543210\n"

    monkeypatch.setattr(client, "download_file", download_file)
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
        AdminBootstrapService(test_session_factory),
        test_session_factory,
        business,
        configured_admin_ids=(42,),
    )
    sender = User(id=42, is_bot=False, first_name="Admin")
    private_chat = Chat(id=42, type="private")

    async with client:
        await handlers.handle_message(
            Message(message_id=1, date=1, chat=private_chat, from_user=sender, text="/import")
        )
        await handlers.handle_message(
            Message(
                message_id=2,
                date=2,
                chat=private_chat,
                from_user=sender,
                document={"file_id": "upload", "file_name": "list.csv", "file_size": 40},
            )
        )

    assert "شماره‌های متصل و عضو قبلی: 1" in sent[-1]
    assert "پیدا‌نشده (قابل پیام نیستند): 1" in sent[-1]
    assert "هیچ اشتراکی ایجاد یا پیامی ارسال نشد" in sent[-1]
