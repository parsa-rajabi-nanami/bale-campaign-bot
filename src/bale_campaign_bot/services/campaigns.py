"""Campaign drafts, audience snapshots for previews, and lifecycle transitions."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bale_campaign_bot.db.models import Campaign
from bale_campaign_bot.db.repositories import (
    BroadcastRepository,
    CampaignRepository,
    TagRepository,
)

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CampaignStatus(StrEnum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class CampaignError(ValueError):
    """Missing campaign, invalid configuration, or disallowed transition."""


_TRANSITIONS: dict[CampaignStatus, frozenset[CampaignStatus]] = {
    CampaignStatus.DRAFT: frozenset({CampaignStatus.SCHEDULED, CampaignStatus.CANCELLED}),
    CampaignStatus.SCHEDULED: frozenset(
        {
            CampaignStatus.RUNNING,
            CampaignStatus.PAUSED,
            CampaignStatus.CANCELLED,
            CampaignStatus.FAILED,
        }
    ),
    CampaignStatus.RUNNING: frozenset(
        {
            CampaignStatus.PAUSED,
            CampaignStatus.COMPLETED,
            CampaignStatus.CANCELLED,
            CampaignStatus.FAILED,
        }
    ),
    CampaignStatus.PAUSED: frozenset(
        {CampaignStatus.RUNNING, CampaignStatus.CANCELLED, CampaignStatus.FAILED}
    ),
    CampaignStatus.COMPLETED: frozenset(),
    CampaignStatus.CANCELLED: frozenset(),
    CampaignStatus.FAILED: frozenset({CampaignStatus.SCHEDULED}),
}


class CampaignService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def create_draft(
        self,
        *,
        name: str,
        created_by: int,
        source_chat_id: int,
        source_message_id: int,
    ) -> int:
        normalized_name = name.strip()
        if not normalized_name or len(normalized_name) > 200:
            raise CampaignError("Campaign name must contain 1-200 characters.")
        async with self._session_factory() as session, session.begin():
            campaign = await CampaignRepository(session).create_draft(
                name=normalized_name,
                created_by=created_by,
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
            )
            return campaign.id

    async def get(self, campaign_id: int) -> Campaign:
        async with self._session_factory() as session:
            campaign = await CampaignRepository(session).get(campaign_id)
            if campaign is None:
                raise CampaignError("Campaign does not exist.")
            return self._normalize_loaded_times(campaign)

    async def set_audience(
        self,
        campaign_id: int,
        *,
        include_tags: Sequence[str] = (),
        exclude_tags: Sequence[str] = (),
    ) -> int:
        includes = tuple(dict.fromkeys(name.strip() for name in include_tags if name.strip()))
        excludes = tuple(dict.fromkeys(name.strip() for name in exclude_tags if name.strip()))
        overlap = set(includes) & set(excludes)
        if overlap:
            raise CampaignError(
                f"Tag(s) cannot be both included and excluded: {', '.join(sorted(overlap))}"
            )
        async with self._session_factory() as session, session.begin():
            campaign = await CampaignRepository(session).get(campaign_id)
            if campaign is None:
                raise CampaignError("Campaign does not exist.")
            self._require_status(campaign, CampaignStatus.DRAFT)
            available = set(await TagRepository(session).list_names())
            unknown = (set(includes) | set(excludes)) - available
            if unknown:
                raise CampaignError(f"Unknown tag(s): {', '.join(sorted(unknown))}")
            campaign.audience_spec = {
                "include_tags": list(includes),
                "exclude_tags": list(excludes),
            }
            campaign.recipient_count = await TagRepository(session).count_active(
                include_tags=includes,
                exclude_tags=excludes,
            )
            return campaign.recipient_count

    async def set_cta_buttons(
        self,
        campaign_id: int,
        buttons: Sequence[tuple[str, str]],
    ) -> None:
        normalized: list[dict[str, str]] = []
        for label, url in buttons:
            label, url = label.strip(), url.strip()
            parsed = urlsplit(url)
            if (
                not label
                or len(label) > 64
                or any(ord(char) < 32 for char in label)
                or len(url) > 2048
                or parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise CampaignError("CTA buttons require a label and an absolute HTTPS URL.")
            normalized.append({"label": label, "url": url})
        if len(normalized) > 20:
            raise CampaignError("A campaign can have at most 20 CTA buttons.")
        async with self._session_factory() as session, session.begin():
            campaign = await CampaignRepository(session).get(campaign_id)
            if campaign is None:
                raise CampaignError("Campaign does not exist.")
            self._require_status(campaign, CampaignStatus.DRAFT)
            campaign.cta_buttons = normalized

    async def schedule(self, campaign_id: int, scheduled_at: datetime) -> None:
        timestamp = self._as_utc(scheduled_at)
        now = self._as_utc(self._clock())
        if timestamp < now:
            raise CampaignError("Campaign schedule must be in the future.")
        async with self._session_factory() as session, session.begin():
            campaign = await CampaignRepository(session).get(campaign_id)
            if campaign is None:
                raise CampaignError("Campaign does not exist.")
            self._require_status(campaign, CampaignStatus.DRAFT)
            if campaign.recipient_count < 1:
                raise CampaignError(
                    "Campaign audience is empty; at least one active subscriber is required."
                )
            self._transition(campaign, CampaignStatus.SCHEDULED)
            campaign.scheduled_at = timestamp

    async def transition(self, campaign_id: int, target: CampaignStatus) -> None:
        async with self._session_factory() as session, session.begin():
            campaign = await CampaignRepository(session).get(campaign_id)
            if campaign is None:
                raise CampaignError("Campaign does not exist.")
            self._transition(campaign, target)

    async def cancel(self, campaign_id: int) -> int:
        async with self._session_factory() as session, session.begin():
            campaign = await CampaignRepository(session).get(campaign_id)
            if campaign is None:
                raise CampaignError("Campaign does not exist.")
            self._transition(campaign, CampaignStatus.CANCELLED)
            return await BroadcastRepository(session, clock=self._clock).cancel_campaign(
                campaign_id
            )

    async def list_for_admin(self, user_id: int, *, limit: int = 20) -> list[Campaign]:
        async with self._session_factory() as session:
            campaigns = await CampaignRepository(session).list_for_admin(user_id, limit=limit)
            return [self._normalize_loaded_times(campaign) for campaign in campaigns]

    @staticmethod
    def _normalize_loaded_times(campaign: Campaign) -> Campaign:
        """SQLite returns timezone-aware columns as naive; interpret stored values as UTC."""
        for field in ("scheduled_at", "started_at", "completed_at"):
            timestamp = getattr(campaign, field)
            if timestamp is not None:
                normalized = (
                    timestamp.replace(tzinfo=UTC)
                    if timestamp.tzinfo is None
                    else timestamp.astimezone(UTC)
                )
                setattr(campaign, field, normalized)
        return campaign

    @staticmethod
    def _as_utc(timestamp: datetime) -> datetime:
        if timestamp.tzinfo is None:
            raise CampaignError("Campaign timestamps must include a timezone.")
        return timestamp.astimezone(UTC)

    @staticmethod
    def _require_status(campaign: Campaign, status: CampaignStatus) -> None:
        if campaign.status != status.value:
            raise CampaignError(
                f"Campaign must be {status.value}; current status is {campaign.status}."
            )

    def _transition(self, campaign: Campaign, target: CampaignStatus) -> None:
        try:
            current = CampaignStatus(campaign.status)
        except ValueError:
            raise CampaignError("Campaign has an unknown status.") from None
        if target not in _TRANSITIONS[current]:
            raise CampaignError(
                f"Cannot transition campaign from {current.value} to {target.value}."
            )
        campaign.status = target.value
        now = self._as_utc(self._clock())
        if target is CampaignStatus.RUNNING and campaign.started_at is None:
            campaign.started_at = now
        if target in {CampaignStatus.COMPLETED, CampaignStatus.CANCELLED, CampaignStatus.FAILED}:
            campaign.completed_at = now
