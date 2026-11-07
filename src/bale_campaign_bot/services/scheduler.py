"""Database-leased scheduler for due campaign snapshots and state transitions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bale_campaign_bot.db.models import Campaign, SchedulerLease
from bale_campaign_bot.db.repositories import BroadcastRepository

Clock = Callable[[], datetime]
LEASE_NAME = "campaign-sender"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_timestamp(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class CampaignScheduler:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        owner: str,
        clock: Clock = _utc_now,
        lease_seconds: int = 60,
    ) -> None:
        if lease_seconds < 10:
            raise ValueError("scheduler lease must be at least 10 seconds")
        self._session_factory = session_factory
        self._owner = owner
        self._clock = clock
        self._lease_duration = timedelta(seconds=lease_seconds)

    async def acquire_lease(self) -> bool:
        now = _utc_timestamp(self._clock())
        try:
            async with self._session_factory() as session, session.begin():
                lease = await session.get(SchedulerLease, LEASE_NAME, with_for_update=True)
                if lease is None:
                    session.add(
                        SchedulerLease(
                            name=LEASE_NAME,
                            owner=self._owner,
                            expires_at=now + self._lease_duration,
                        )
                    )
                    await session.flush()
                    return True
                if _utc_timestamp(lease.expires_at) > now and lease.owner != self._owner:
                    return False
                lease.owner = self._owner
                lease.expires_at = now + self._lease_duration
                await session.flush()
            return True
        except IntegrityError:
            return False

    async def renew_lease(self) -> bool:
        now = _utc_timestamp(self._clock())
        async with self._session_factory() as session, session.begin():
            lease = await session.get(SchedulerLease, LEASE_NAME, with_for_update=True)
            if lease is None or lease.owner != self._owner:
                return False
            lease.expires_at = now + self._lease_duration
            await session.flush()
        return True

    async def release_lease(self) -> None:
        async with self._session_factory() as session, session.begin():
            lease = await session.get(SchedulerLease, LEASE_NAME, with_for_update=True)
            if lease is not None and lease.owner == self._owner:
                await session.delete(lease)

    async def recover_stuck_recipients(self) -> int:
        async with self._session_factory() as session, session.begin():
            return await BroadcastRepository(session, clock=self._clock).reset_stuck_sending()

    async def activate_due_campaigns(self, *, limit: int = 20) -> list[int]:
        now = _utc_timestamp(self._clock())
        started: list[int] = []
        async with self._session_factory() as session, session.begin():
            campaigns = await session.scalars(
                select(Campaign)
                .where(
                    Campaign.status == "scheduled",
                    Campaign.scheduled_at <= now,
                )
                .order_by(Campaign.scheduled_at, Campaign.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            for campaign in campaigns:
                campaign.status = "running"
                campaign.started_at = now
                await BroadcastRepository(session, clock=self._clock).materialize_recipients(
                    campaign
                )
                started.append(campaign.id)
        return started

    async def running_campaign_ids(self, *, limit: int = 20) -> list[int]:
        async with self._session_factory() as session:
            ids = await session.scalars(
                select(Campaign.id)
                .where(Campaign.status == "running")
                .order_by(Campaign.started_at, Campaign.id)
                .limit(limit)
            )
            return list(ids)
