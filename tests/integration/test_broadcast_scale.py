from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bale_campaign_bot.bale.client import BaleClient
from bale_campaign_bot.bale.models import Chat, Message, MessageId
from bale_campaign_bot.config.business import BusinessConfig, validate_business_config
from bale_campaign_bot.db.models import Subscriber
from bale_campaign_bot.services.campaigns import CampaignService
from bale_campaign_bot.services.sender import BroadcastWorker


class ScaleFakeBaleClient:
    def __init__(self, expected_sends: int) -> None:
        self.expected_sends = expected_sends
        self.completed_sends = 0
        self.in_flight = 0
        self.peak_in_flight = 0
        self.stop_event: asyncio.Event | None = None
        self._message_id = 10_000

    async def copy_message(
        self,
        chat_id: int,
        from_chat_id: int,
        message_id: int,
    ) -> MessageId:
        assert from_chat_id == 999 and message_id == 1
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        await asyncio.sleep(0)
        self.in_flight -= 1
        self.completed_sends += 1
        if self.completed_sends == self.expected_sends and self.stop_event is not None:
            self.stop_event.set()
        return MessageId(message_id=chat_id)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: object | None = None,
    ) -> Message:
        return Message(
            message_id=self._message_id,
            date=1_700_000_000,
            chat=Chat(id=chat_id, type="private"),
            text=text,
        )

    async def edit_message_text(self, chat_id: int, message_id: int, text: str) -> bool:
        return True


def _business_config() -> BusinessConfig:
    return validate_business_config(
        {
            "business": {
                "name": "Scale demo",
                "short_description": "Scale",
                "welcome_message": "Welcome",
                "about_text": "About",
                "consent_text": "I agree",
                "consent_version": "v1",
                "opt_out_confirmation_text": "Stopped",
            }
        }
    )


async def test_ten_thousand_recipients_use_bounded_batches_and_concurrency(
    test_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    recipient_count = 10_000
    now = datetime(2026, 11, 7, 9, tzinfo=UTC)
    subscribed_at = now.replace(tzinfo=None)
    async with test_session_factory() as session, session.begin():
        for batch_start in range(0, recipient_count, 1000):
            session.add_all(
                [
                    Subscriber(
                        chat_id=10_000 + index,
                        first_name="Member",
                        is_active=True,
                        subscribed_at=subscribed_at,
                        consent_version="v1",
                    )
                    for index in range(batch_start, batch_start + 1000)
                ]
            )
            await session.flush()

    campaigns = CampaignService(test_session_factory, clock=lambda: now)
    campaign_id = await campaigns.create_draft(
        name="Scale campaign",
        created_by=999,
        source_chat_id=999,
        source_message_id=1,
    )
    assert await campaigns.set_audience(campaign_id) == recipient_count
    await campaigns.schedule(campaign_id, now + timedelta(seconds=1))

    stop_event = asyncio.Event()
    client = ScaleFakeBaleClient(recipient_count)
    client.stop_event = stop_event
    worker = BroadcastWorker(
        cast(BaleClient, client),
        test_session_factory,
        _business_config(),
        rate_per_second=100_000,
        concurrency=32,
        batch_size=80,
        clock=lambda: now + timedelta(seconds=2),
    )
    await asyncio.wait_for(worker.run(stop_event), timeout=60)

    assert client.completed_sends == recipient_count
    assert client.peak_in_flight <= 32
    assert worker._batch_size == 80
