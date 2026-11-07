from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bale_campaign_bot.bale.client import BaleClient
from bale_campaign_bot.bale.errors import BaleAPIError, BaleNetworkError
from bale_campaign_bot.bale.models import Chat, Message, MessageId, User
from bale_campaign_bot.config.business import BusinessConfig, validate_business_config
from bale_campaign_bot.db.models import (
    Campaign,
    CampaignRecipient,
    SchedulerLease,
    Subscriber,
)
from bale_campaign_bot.db.repositories import BroadcastRepository
from bale_campaign_bot.services.campaigns import CampaignService, CampaignStatus
from bale_campaign_bot.services.scheduler import CampaignScheduler
from bale_campaign_bot.services.sender import AdaptiveTokenBucket, BroadcastWorker
from bale_campaign_bot.services.subscribers import SubscriberService


def business_config() -> BusinessConfig:
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
                "footer_enabled": True,
                "footer_text": "Business footer",
            }
        }
    )


async def add_subscriber(
    session_factory: async_sessionmaker[AsyncSession],
    chat_id: int,
) -> None:
    await SubscriberService(session_factory).start(
        user=User(id=chat_id, is_bot=False, first_name="Member"),
        chat=Chat(id=chat_id, type="private"),
        source=None,
        consent_version="v1",
    )


async def make_scheduled_campaign(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    chats: tuple[int, ...],
    with_followups: bool = False,
) -> int:
    for chat_id in chats:
        await add_subscriber(session_factory, chat_id)
    service = CampaignService(session_factory, clock=lambda: now)
    campaign_id = await service.create_draft(
        name="Reliability test",
        created_by=900,
        source_chat_id=900,
        source_message_id=15,
    )
    assert await service.set_audience(campaign_id) == len(chats)
    if with_followups:
        await service.set_cta_buttons(campaign_id, [("Open", "https://example.com")])
    await service.schedule(campaign_id, now + timedelta(seconds=1))
    return campaign_id


async def test_adaptive_bucket_honors_retry_hint_slows_and_recovers() -> None:
    monotonic_now = [0.0]
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        monotonic_now[0] += seconds

    bucket = AdaptiveTokenBucket(
        10,
        monotonic=lambda: monotonic_now[0],
        sleep=sleep,
        jitter=lambda: 0.0,
        recovery_every=2,
    )
    for _ in range(10):
        await bucket.acquire()
    assert bucket.current_rate == 10
    assert await bucket.rate_limited(3) == 3
    assert bucket.current_rate == 5
    await bucket.acquire()
    assert sleeps == [3]
    await bucket.record_success()
    await bucket.record_success()
    assert bucket.current_rate == 5.5


async def test_scheduler_lease_snapshot_recovery_and_active_opt_out_filter(
    test_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 11, 7, 9, tzinfo=UTC)
    campaign_id = await make_scheduled_campaign(
        test_session_factory,
        now=now,
        chats=(201, 202),
    )
    scheduler = CampaignScheduler(
        test_session_factory,
        owner="sender-one",
        clock=lambda: now + timedelta(seconds=2),
    )
    another = CampaignScheduler(
        test_session_factory,
        owner="sender-two",
        clock=lambda: now + timedelta(seconds=2),
    )
    assert await scheduler.acquire_lease()
    assert not await another.acquire_lease()
    assert await scheduler.activate_due_campaigns() == [campaign_id]
    assert await SubscriberService(test_session_factory).stop(201)

    async with test_session_factory() as session, session.begin():
        targets = await BroadcastRepository(session).claim_batch(campaign_id, limit=10)
    assert [(target.chat_id, target.attempts) for target in targets] == [(202, 1)]
    recovered = await scheduler.recover_stuck_recipients()
    assert recovered == 1
    async with test_session_factory() as session:
        recipients = list(
            await session.scalars(select(CampaignRecipient).order_by(CampaignRecipient.id))
        )
        campaign = await session.get(Campaign, campaign_id)
        lease = await session.get(SchedulerLease, "campaign-sender")
    assert [recipient.status for recipient in recipients] == ["skipped", "pending"]
    assert recipients[0].last_error == "subscriber_opted_out"
    assert recipients[1].attempts == 1
    assert campaign is not None and campaign.skipped_count == 1
    assert lease is not None and lease.owner == "sender-one"
    await scheduler.release_lease()


class FakeSendingClient:
    def __init__(self) -> None:
        self.copy_calls: list[int] = []
        self.copy_attempts: defaultdict[int, int] = defaultdict(int)
        self.sent: list[tuple[int, str, object | None]] = []
        self.next_message_id = 1
        self.stop_on_completion: asyncio.Event | None = None

    async def copy_message(self, chat_id: int, from_chat_id: int, message_id: int) -> MessageId:
        assert from_chat_id == 900 and message_id == 15
        self.copy_calls.append(chat_id)
        self.copy_attempts[chat_id] += 1
        if chat_id == 301:
            raise BaleAPIError(
                "copyMessage",
                error_code=403,
                description="bot was blocked by the user",
            )
        if chat_id == 302 and self.copy_attempts[chat_id] == 1:
            raise BaleNetworkError("copyMessage", attempts=3)
        return MessageId(message_id=1000 + chat_id)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: object | None = None,
    ) -> Message:
        self.sent.append((chat_id, text, reply_markup))
        message = Message(
            message_id=self.next_message_id,
            date=1_700_000_000,
            chat=Chat(id=chat_id, type="private"),
            text=text,
        )
        self.next_message_id += 1
        return message

    async def edit_message_text(self, chat_id: int, message_id: int, text: str) -> bool:
        assert chat_id == 900 and message_id > 0
        if "تکمیل‌شده" in text and self.stop_on_completion is not None:
            self.stop_on_completion.set()
        return True


async def test_worker_retries_blocks_deactivates_and_sends_safe_followups(
    test_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 11, 7, 9, tzinfo=UTC)
    campaign_id = await make_scheduled_campaign(
        test_session_factory,
        now=now,
        chats=(301, 302),
        with_followups=True,
    )
    client = FakeSendingClient()
    stop_event = asyncio.Event()
    client.stop_on_completion = stop_event
    worker = BroadcastWorker(
        cast(BaleClient, client),
        test_session_factory,
        business_config(),
        rate_per_second=1000,
        concurrency=2,
        batch_size=10,
        clock=lambda: now + timedelta(seconds=2),
        jitter=lambda: 0,
    )
    await asyncio.wait_for(worker.run(stop_event), timeout=5)

    assert client.copy_attempts[301] == 1
    assert client.copy_attempts[302] == 2
    followups = [item for item in client.sent if item[0] == 302]
    assert len(followups) == 2
    assert any(text == "Business footer" for _, text, _ in followups)
    assert any(markup is not None for _, _, markup in followups)
    async with test_session_factory() as session:
        campaign = await session.get(Campaign, campaign_id)
        blocked = await session.scalar(select(Subscriber).where(Subscriber.chat_id == 301))
        recipients = list(
            await session.scalars(
                select(CampaignRecipient)
                .where(CampaignRecipient.campaign_id == campaign_id)
                .order_by(CampaignRecipient.id)
            )
        )
    assert campaign is not None
    assert campaign.status == "completed"
    assert campaign.sent_count == 1 and campaign.blocked_count == 1
    assert campaign.summary_sent_at is not None
    assert blocked is not None and blocked.is_active is False
    assert [(item.status, item.attempts) for item in recipients] == [("blocked", 1), ("sent", 2)]


async def test_campaign_pause_resume_cancel_marks_pending_skipped(
    test_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 11, 7, 9, tzinfo=UTC)
    campaign_id = await make_scheduled_campaign(
        test_session_factory,
        now=now,
        chats=(401, 402),
    )
    scheduler = CampaignScheduler(
        test_session_factory,
        owner="sender-controller-test",
        clock=lambda: now + timedelta(seconds=2),
    )
    assert await scheduler.activate_due_campaigns() == [campaign_id]
    service = CampaignService(test_session_factory, clock=lambda: now + timedelta(seconds=2))
    await service.transition(campaign_id, CampaignStatus.PAUSED)
    await service.transition(campaign_id, CampaignStatus.RUNNING)
    assert await service.cancel(campaign_id) == 2
    campaign = await service.get(campaign_id)
    assert campaign.status == "cancelled" and campaign.skipped_count == 2
    async with test_session_factory() as session:
        statuses = list(
            await session.scalars(
                select(CampaignRecipient.status).where(CampaignRecipient.campaign_id == campaign_id)
            )
        )
    assert statuses == ["skipped", "skipped"]
