from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bale_campaign_bot.bale.models import Chat, User
from bale_campaign_bot.db.models import Campaign
from bale_campaign_bot.services.audiences import AudienceError, AudienceService
from bale_campaign_bot.services.campaigns import (
    CampaignError,
    CampaignService,
    CampaignStatus,
)
from bale_campaign_bot.services.subscribers import SubscriberService


async def test_audience_counts_include_exclude_tags_and_never_opted_out(
    test_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    subscribers = SubscriberService(test_session_factory)
    for chat_id in (101, 102, 103):
        await subscribers.start(
            user=User(id=chat_id, is_bot=False, first_name="Member"),
            chat=Chat(id=chat_id, type="private"),
            source=None,
            consent_version="v1",
        )
    await subscribers.stop(103)
    audience = AudienceService(test_session_factory)
    await audience.create_tag("buyers")
    await audience.create_tag("vip")
    assert await audience.assign_tag("buyers", 101)
    assert await audience.assign_tag("vip", 101)
    assert await audience.assign_tag("buyers", 102)

    assert await audience.count() == 2
    assert await audience.count(include_tags=("buyers",)) == 2
    assert await audience.count(include_tags=("buyers", "vip")) == 1
    assert await audience.count(exclude_tags=("vip",)) == 1
    with pytest.raises(AudienceError, match="Unknown tag"):
        await audience.count(include_tags=("missing",))


async def test_campaign_draft_audience_cta_schedule_and_transitions(
    test_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 11, 7, 9, tzinfo=UTC)
    service = CampaignService(test_session_factory, clock=lambda: now)
    campaign_id = await service.create_draft(
        name="Autumn launch",
        created_by=1,
        source_chat_id=1,
        source_message_id=55,
    )
    audiences = AudienceService(test_session_factory)
    await audiences.create_tag("customers")
    await SubscriberService(test_session_factory).start(
        user=User(id=100, is_bot=False, first_name="Customer"),
        chat=Chat(id=100, type="private"),
        source=None,
        consent_version="v1",
    )
    assert await audiences.assign_tag("customers", 100)
    assert await service.set_audience(campaign_id, include_tags=("customers",)) == 1
    await service.set_cta_buttons(campaign_id, [("Shop", "https://example.com/shop")])
    with pytest.raises(CampaignError, match="HTTPS"):
        await service.set_cta_buttons(campaign_id, [("Bad", "javascript:alert(1)")])
    with pytest.raises(CampaignError, match="Cannot transition"):
        await service.transition(campaign_id, CampaignStatus.COMPLETED)
    with pytest.raises(CampaignError, match="future"):
        await service.schedule(campaign_id, now - timedelta(seconds=1))

    scheduled_at = now + timedelta(minutes=30)
    await service.schedule(campaign_id, scheduled_at)
    campaign = await service.get(campaign_id)
    assert campaign.status == "scheduled"
    assert campaign.scheduled_at == scheduled_at
    assert campaign.audience_spec == {"include_tags": ["customers"], "exclude_tags": []}
    assert campaign.cta_buttons == [{"label": "Shop", "url": "https://example.com/shop"}]

    with pytest.raises(CampaignError, match="scheduled"):
        await service.schedule(campaign_id, scheduled_at + timedelta(minutes=1))
    await service.transition(campaign_id, CampaignStatus.RUNNING)
    await service.transition(campaign_id, CampaignStatus.PAUSED)
    await service.transition(campaign_id, CampaignStatus.RUNNING)
    await service.transition(campaign_id, CampaignStatus.COMPLETED)
    final = await service.get(campaign_id)
    assert final.status == "completed"
    assert final.started_at == now
    assert final.completed_at == now

    async with test_session_factory() as session:
        stored = await session.get(Campaign, campaign_id)
    assert stored is not None
