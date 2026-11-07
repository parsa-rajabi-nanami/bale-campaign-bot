from __future__ import annotations

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bale_campaign_bot.bale.client import BaleClient
from bale_campaign_bot.bale.models import Chat, Message, User
from bale_campaign_bot.bot.fsm import FSMContext
from bale_campaign_bot.bot.handlers.admin import (
    AdminHandlers,
    _parse_local_datetime,
    _quiet_adjust,
)
from bale_campaign_bot.config.business import BusinessConfig, validate_business_config
from bale_campaign_bot.db.models import Admin, Campaign, Subscriber
from bale_campaign_bot.services.admins import AdminBootstrapService
from bale_campaign_bot.services.subscribers import SubscriberService


def make_business_config() -> BusinessConfig:
    return validate_business_config(
        {
            "business": {
                "name": "Demo",
                "short_description": "Demo",
                "welcome_message": "Welcome",
                "about_text": "About",
                "consent_text": "I agree",
                "consent_version": "v1",
                "opt_out_confirmation_text": "Stopped",
                "timezone": "Asia/Tehran",
            },
            "campaign_defaults": {
                "send_rate_per_second": 10,
                "quiet_hours": {"enabled": False},
            },
        }
    )


def test_quiet_hours_move_campaign_to_next_allowed_local_time() -> None:
    timezone = ZoneInfo("Asia/Tehran")
    during_quiet = datetime(2026, 11, 7, 19, 30, tzinfo=UTC)
    adjusted = _quiet_adjust(
        during_quiet,
        timezone=timezone,
        enabled=True,
        start_text="22:00",
        end_text="08:00",
    )
    assert adjusted == datetime(2026, 11, 8, 8, 0, tzinfo=timezone)

    during_working_hours = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)
    unchanged = _quiet_adjust(
        during_working_hours,
        timezone=timezone,
        enabled=True,
        start_text="22:00",
        end_text="08:00",
    )
    assert unchanged == during_working_hours.astimezone(timezone)


def test_schedule_rejects_nonexistent_local_clock_time() -> None:
    with pytest.raises(ValueError):
        _parse_local_datetime("2026-03-08 02:30", ZoneInfo("America/New_York"))


async def test_campaign_wizard_persists_across_handler_restart_and_test_is_private(
    fake_bale_server: respx.MockRouter,
    test_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sent: list[dict[str, object]] = []
    copied: list[dict[str, object]] = []

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

    def copy_message(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        copied.append(payload)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(copied)}})

    fake_bale_server.post("sendMessage").mock(side_effect=send_message)
    fake_bale_server.post("copyMessage").mock(side_effect=copy_message)
    async with test_session_factory() as session, session.begin():
        session.add(Admin(user_id=900, role="owner"))
    await SubscriberService(test_session_factory).start(
        user=User(id=200, is_bot=False, first_name="Subscriber"),
        chat=Chat(id=200, type="private"),
        source=None,
        consent_version="v1",
    )

    now = datetime(2026, 11, 7, 9, tzinfo=UTC)
    bootstrap = AdminBootstrapService(test_session_factory, clock=lambda: now)
    client = BaleClient("TEST_TOKEN")
    business = make_business_config()

    def handlers() -> AdminHandlers:
        return AdminHandlers(
            client,
            bootstrap,
            test_session_factory,
            business,
            configured_admin_ids=(),
            clock=lambda: now,
        )

    def message(message_id: int, text: str) -> Message:
        return Message(
            message_id=message_id,
            date=1_700_000_000 + message_id,
            chat=Chat(id=900, type="private"),
            from_user=User(id=900, is_bot=False, first_name="Owner"),
            text=text,
        )

    async with client:
        first_instance = handlers()
        await first_instance.handle_message(message(1, "/campaign"))
        restarted_instance = handlers()
        await restarted_instance.handle_message(message(2, "Sample *campaign* content"))
        await restarted_instance.handle_message(message(3, "November launch"))
        await restarted_instance.handle_message(message(4, "همه مخاطبان"))
        await restarted_instance.handle_message(message(5, "Shop now | https://example.com/shop"))
        await restarted_instance.handle_message(message(6, "/test"))
        await restarted_instance.handle_message(message(7, "ارسال اکنون"))
        await restarted_instance.handle_message(message(8, "تأیید و شروع"))

    assert len(copied) == 2
    assert all(payload["chat_id"] == 900 for payload in copied)
    assert all(payload["from_chat_id"] == 900 for payload in copied)
    async with test_session_factory() as session:
        campaign = await session.scalar(select(Campaign))
        subscriber_count = await session.scalar(select(Subscriber.id))
    assert campaign is not None
    assert campaign.name == "November launch"
    assert campaign.source_chat_id == 900 and campaign.source_message_id == 2
    assert campaign.status == "scheduled"
    assert campaign.recipient_count == 1
    assert campaign.cta_buttons == [{"label": "Shop now", "url": "https://example.com/shop"}]
    assert subscriber_count is not None
    assert (
        await FSMContext(
            test_session_factory,
            user_id=900,
            chat_id=900,
        ).get()
        is None
    )
