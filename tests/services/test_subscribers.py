from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bale_campaign_bot.bale.client import BaleClient
from bale_campaign_bot.bale.models import Chat, Message, User
from bale_campaign_bot.bot.handlers.public import PublicHandlers
from bale_campaign_bot.config.business import BusinessConfig, validate_business_config
from bale_campaign_bot.db.models import Subscriber
from bale_campaign_bot.services.subscribers import (
    PhoneLinkStatus,
    SubscriberService,
    normalize_iranian_mobile,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0912 345 6789", "+989123456789"),
        ("+98 (912) 345-6789", "+989123456789"),
        ("0098-912-345-6789", "+989123456789"),
        ("۰۹۱۲۳۴۵۶۷۸۹", "+989123456789"),
        ("02112345678", None),
        ("abc 09123456789", None),
        ("+1 202 555 0101", None),
    ],
)
def test_normalize_iranian_phone(raw: str, expected: str | None) -> None:
    assert normalize_iranian_mobile(raw) == expected


async def test_subscriber_service_start_stop_and_phone_link(
    test_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 2, 3, tzinfo=UTC)
    service = SubscriberService(test_session_factory, clock=lambda: now)
    await service.start(
        user=User(id=400, is_bot=False, first_name="User", username="u"),
        chat=Chat(id=400, type="private"),
        source="spring-campaign",
        consent_version="v3",
    )
    assert await service.link_phone(400, "09123456789") is PhoneLinkStatus.LINKED
    assert await service.link_phone(400, "09123456789") is PhoneLinkStatus.LINKED
    assert await service.link_phone(400, "123") is PhoneLinkStatus.INVALID
    assert await service.stop(400)
    assert await service.link_phone(400, "09123456789") is PhoneLinkStatus.NOT_SUBSCRIBED

    async with test_session_factory() as session:
        subscriber = await session.scalar(select(Subscriber).where(Subscriber.chat_id == 400))
    assert subscriber is not None
    assert subscriber.is_active is False
    assert subscriber.phone_e164 == "+989123456789"
    assert subscriber.source == "spring-campaign"
    assert subscriber.consent_version == "v3"


def business_config(*, phone_linking_enabled: bool = True) -> BusinessConfig:
    return validate_business_config(
        {
            "business": {
                "name": "نمونه",
                "short_description": "معرفی",
                "welcome_message": "به {business_name} خوش آمدید",
                "about_text": "درباره ما",
                "contact": {},
                "default_menu": [{"label": "درباره", "callback": "about"}],
                "consent_text": "با عضویت، دریافت پیام را می‌پذیرم.",
                "consent_version": "v1",
                "opt_out_confirmation_text": "عضویت شما لغو شد.",
                "phone_linking_enabled": phone_linking_enabled,
            }
        }
    )


async def test_public_start_and_contact_identity_validation(
    fake_bale_server: respx.MockRouter,
    test_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sent: list[dict[str, Any]] = []

    def send(request: httpx.Request) -> httpx.Response:
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

    fake_bale_server.post("sendMessage").mock(side_effect=send)
    service = SubscriberService(test_session_factory)
    client = BaleClient("TEST_TOKEN")
    handlers = PublicHandlers(client, service, business_config())
    async with client:
        sender = User(id=500, is_bot=False, first_name="Ada", username="ada", language_code="fa")
        chat = Chat(id=500, type="private")
        await handlers.handle_message(
            Message(
                message_id=1,
                date=1_700_000_000,
                chat=chat,
                from_user=sender,
                text="/start campaign-42",
            )
        )
        assert sent[0]["text"] == "با عضویت، دریافت پیام را می‌پذیرم."
        assert sent[1]["text"] == "به نمونه خوش آمدید"
        assert "inline_keyboard" in sent[1]["reply_markup"]
        assert sent[2]["reply_markup"]["keyboard"][0][0]["request_contact"] is True

        await handlers.handle_message(
            Message(
                message_id=2,
                date=1_700_000_001,
                chat=chat,
                from_user=sender,
                contact={"phone_number": "09123456789", "first_name": "Other", "user_id": 999},
            )
        )
        async with test_session_factory() as session:
            subscriber = await session.scalar(
                select(Subscriber).where(Subscriber.chat_id == chat.id)
            )
        assert subscriber is not None and subscriber.phone_e164 is None

        await handlers.handle_message(
            Message(
                message_id=3,
                date=1_700_000_002,
                chat=chat,
                from_user=sender,
                contact={"phone_number": "09123456789", "first_name": "Ada", "user_id": sender.id},
            )
        )
        async with test_session_factory() as session:
            subscriber = await session.scalar(
                select(Subscriber).where(Subscriber.chat_id == chat.id)
            )
        assert subscriber is not None and subscriber.phone_e164 == "+989123456789"


async def test_stop_and_private_chat_guard(
    fake_bale_server: respx.MockRouter,
    test_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fake_bale_server.post("sendMessage").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "message_id": 2,
                    "date": 1,
                    "chat": {"id": 600, "type": "private"},
                    "text": "لغو عضویت شد",
                },
            },
        )
    )
    service = SubscriberService(test_session_factory)
    handlers = PublicHandlers(BaleClient("TEST_TOKEN"), service, business_config())
    await service.start(
        user=User(id=600, is_bot=False, first_name="User"),
        chat=Chat(id=600, type="private"),
        source=None,
        consent_version="v1",
    )
    async with handlers._client:
        await handlers.handle_message(
            Message(
                message_id=4,
                date=1,
                chat=Chat(id=600, type="group"),
                from_user=User(id=600, is_bot=False, first_name="User"),
                text="/stop",
            )
        )
        async with test_session_factory() as session:
            subscriber = await session.scalar(select(Subscriber).where(Subscriber.chat_id == 600))
        assert subscriber is not None and subscriber.is_active
        await handlers.handle_message(
            Message(
                message_id=5,
                date=1,
                chat=Chat(id=600, type="private"),
                from_user=User(id=600, is_bot=False, first_name="User"),
                text="/stop",
            )
        )
    async with test_session_factory() as session:
        subscriber = await session.scalar(select(Subscriber).where(Subscriber.chat_id == 600))
    assert subscriber is not None and subscriber.is_active is False
