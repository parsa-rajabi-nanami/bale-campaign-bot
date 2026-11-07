"""Persistence operations kept separate from handlers and services."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bale_campaign_bot.db.models import (
    Admin,
    BotState,
    Campaign,
    CampaignRecipient,
    ConversationState,
    ProcessedUpdate,
    Subscriber,
    SubscriberTag,
    Tag,
)

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SubscriberRepository:
    def __init__(self, session: AsyncSession, *, clock: Clock = _utc_now) -> None:
        self._session = session
        self._clock = clock

    async def get_by_chat_id(self, chat_id: int) -> Subscriber | None:
        result = await self._session.execute(
            select(Subscriber).where(Subscriber.chat_id == chat_id)
        )
        return result.scalar_one_or_none()

    async def opt_in(
        self,
        *,
        chat_id: int,
        first_name: str,
        last_name: str | None,
        username: str | None,
        language_code: str | None,
        source: str | None,
        consent_version: str,
        now: datetime | None = None,
    ) -> Subscriber:
        timestamp = now or self._clock()
        subscriber = await self.get_by_chat_id(chat_id)
        if subscriber is None:
            subscriber = Subscriber(
                chat_id=chat_id,
                first_name=first_name,
                last_name=last_name,
                username=username,
                language_code=language_code,
                source=source,
                is_active=True,
                subscribed_at=timestamp,
                unsubscribed_at=None,
                consent_version=consent_version,
            )
            self._session.add(subscriber)
        else:
            subscriber.first_name = first_name
            subscriber.last_name = last_name
            subscriber.username = username
            subscriber.language_code = language_code
            subscriber.source = source
            subscriber.is_active = True
            subscriber.subscribed_at = timestamp
            subscriber.unsubscribed_at = None
            subscriber.consent_version = consent_version
        await self._session.flush()
        return subscriber

    async def opt_out(self, chat_id: int, *, now: datetime | None = None) -> Subscriber | None:
        subscriber = await self.get_by_chat_id(chat_id)
        if subscriber is None:
            return None
        subscriber.is_active = False
        subscriber.unsubscribed_at = now or self._clock()
        await self._session.flush()
        return subscriber

    async def list_active_chat_ids(self) -> list[int]:
        result = await self._session.scalars(
            select(Subscriber.chat_id).where(Subscriber.is_active.is_(True)).order_by(Subscriber.id)
        )
        return list(result)

    async def find_linked_phones(self, phone_numbers: set[str]) -> set[str]:
        if not phone_numbers:
            return set()
        result = await self._session.scalars(
            select(Subscriber.phone_e164).where(Subscriber.phone_e164.in_(phone_numbers))
        )
        return {phone for phone in result if phone is not None}

    async def export_page(
        self,
        *,
        after_id: int = 0,
        limit: int = 1000,
        include_inactive: bool = False,
    ) -> list[tuple[int, int, str, str | None, str | None, bool, str | None]]:
        query = select(
            Subscriber.id,
            Subscriber.chat_id,
            Subscriber.first_name,
            Subscriber.last_name,
            Subscriber.username,
            Subscriber.is_active,
            Subscriber.phone_e164,
        ).where(Subscriber.id > after_id)
        if not include_inactive:
            query = query.where(Subscriber.is_active.is_(True))
        result = await self._session.execute(query.order_by(Subscriber.id).limit(limit))
        return [(row[0], row[1], row[2], row[3], row[4], row[5], row[6]) for row in result]


class AdminRepository:
    def __init__(self, session: AsyncSession, *, clock: Clock = _utc_now) -> None:
        self._session = session
        self._clock = clock

    async def get(self, user_id: int) -> Admin | None:
        return await self._session.get(Admin, user_id)

    async def add(self, user_id: int, *, role: str = "admin") -> Admin:
        admin = await self.get(user_id)
        if admin is None:
            admin = Admin(user_id=user_id, role=role)
            self._session.add(admin)
        else:
            admin.role = role
        await self._session.flush()
        return admin

    async def is_admin(self, user_id: int) -> bool:
        return await self.get(user_id) is not None


class ConversationRepository:
    def __init__(self, session: AsyncSession, *, clock: Clock = _utc_now) -> None:
        self._session = session
        self._clock = clock

    async def get(self, user_id: int, chat_id: int) -> ConversationState | None:
        return await self._session.get(ConversationState, (user_id, chat_id))

    async def set(
        self,
        user_id: int,
        chat_id: int,
        state: str,
        data: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> ConversationState:
        conversation = await self.get(user_id, chat_id)
        if conversation is None:
            conversation = ConversationState(
                user_id=user_id, chat_id=chat_id, state=state, data=data
            )
            self._session.add(conversation)
        else:
            conversation.state = state
            conversation.data = data
            conversation.updated_at = now or self._clock()
        await self._session.flush()
        return conversation

    async def clear(self, user_id: int, chat_id: int) -> None:
        conversation = await self.get(user_id, chat_id)
        if conversation is not None:
            await self._session.delete(conversation)
            await self._session.flush()


class UpdateRepository:
    def __init__(self, session: AsyncSession, *, clock: Clock = _utc_now) -> None:
        self._session = session
        self._clock = clock

    async def has_processed(self, update_id: int) -> bool:
        return await self._session.get(ProcessedUpdate, update_id) is not None

    async def latest_processed_id(self) -> int | None:
        return await self._session.scalar(select(func.max(ProcessedUpdate.update_id)))

    async def mark_processed(self, update_id: int) -> bool:
        if await self.has_processed(update_id):
            return False
        self._session.add(ProcessedUpdate(update_id=update_id))
        await self._session.flush()
        return True


class BotStateRepository:
    def __init__(self, session: AsyncSession, *, clock: Clock = _utc_now) -> None:
        self._session = session
        self._clock = clock

    async def get(self, key: str) -> dict[str, Any] | None:
        state = await self._session.get(BotState, key)
        return state.value if state is not None else None

    async def set(self, key: str, value: dict[str, Any]) -> None:
        state = await self._session.get(BotState, key)
        if state is None:
            self._session.add(BotState(key=key, value=value))
        else:
            state.value = value
            state.updated_at = self._clock()
        await self._session.flush()


class CampaignRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, campaign_id: int) -> Campaign | None:
        return await self._session.get(Campaign, campaign_id)

    async def create_draft(
        self,
        *,
        name: str,
        created_by: int,
        source_chat_id: int,
        source_message_id: int,
    ) -> Campaign:
        campaign = Campaign(
            name=name,
            created_by=created_by,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            status="draft",
            audience_spec={"include_tags": [], "exclude_tags": []},
            cta_buttons=[],
        )
        self._session.add(campaign)
        await self._session.flush()
        return campaign

    async def list_for_admin(self, created_by: int, *, limit: int = 20) -> list[Campaign]:
        result = await self._session.scalars(
            select(Campaign)
            .where(Campaign.created_by == created_by)
            .order_by(Campaign.created_at.desc(), Campaign.id.desc())
            .limit(limit)
        )
        return list(result)


class TagRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_name(self, name: str) -> Tag | None:
        return await self._session.scalar(select(Tag).where(Tag.name == name))

    async def create(self, name: str, description: str | None = None) -> Tag:
        tag = Tag(name=name, description=description)
        self._session.add(tag)
        await self._session.flush()
        return tag

    async def list_names(self, *, limit: int = 100) -> list[str]:
        result = await self._session.scalars(select(Tag.name).order_by(Tag.name).limit(limit))
        return list(result)

    async def assign_to_chat(self, name: str, chat_id: int) -> bool:
        tag = await self.get_by_name(name)
        subscriber = await SubscriberRepository(self._session).get_by_chat_id(chat_id)
        if tag is None or subscriber is None:
            return False
        existing = await self._session.get(SubscriberTag, (subscriber.id, tag.id))
        if existing is None:
            self._session.add(SubscriberTag(subscriber_id=subscriber.id, tag_id=tag.id))
            await self._session.flush()
        return True

    async def count_active(
        self,
        *,
        include_tags: tuple[str, ...] = (),
        exclude_tags: tuple[str, ...] = (),
    ) -> int:
        query = select(func.count(Subscriber.id)).where(Subscriber.is_active.is_(True))
        if include_tags:
            matching_all = (
                select(SubscriberTag.subscriber_id)
                .join(Tag, Tag.id == SubscriberTag.tag_id)
                .where(Tag.name.in_(include_tags))
                .group_by(SubscriberTag.subscriber_id)
                .having(func.count(func.distinct(Tag.name)) == len(set(include_tags)))
            )
            query = query.where(Subscriber.id.in_(matching_all))
        if exclude_tags:
            matching_any = (
                select(SubscriberTag.subscriber_id)
                .join(Tag, Tag.id == SubscriberTag.tag_id)
                .where(Tag.name.in_(exclude_tags))
            )
            query = query.where(Subscriber.id.not_in(matching_any))
        count = await self._session.scalar(query)
        return count or 0


@dataclass(frozen=True, slots=True)
class RecipientTarget:
    recipient_id: int
    subscriber_id: int
    chat_id: int
    attempts: int


@dataclass(frozen=True, slots=True)
class RecipientCompletion:
    recipient_id: int
    subscriber_id: int
    status: str
    sent_message_id: int | None = None
    safe_error: str | None = None


class BroadcastRepository:
    """Atomic queue operations for scheduler and campaign sender workers."""

    def __init__(self, session: AsyncSession, *, clock: Clock = _utc_now) -> None:
        self._session = session
        self._clock = clock

    async def materialize_recipients(self, campaign: Campaign) -> int:
        audience = campaign.audience_spec
        include_tags = tuple(
            item for item in audience.get("include_tags", []) if isinstance(item, str)
        )
        exclude_tags = tuple(
            item for item in audience.get("exclude_tags", []) if isinstance(item, str)
        )
        query = select(
            literal(campaign.id),
            Subscriber.id,
            literal("pending"),
            literal(0),
        ).where(Subscriber.is_active.is_(True))
        if include_tags:
            matching_all = (
                select(SubscriberTag.subscriber_id)
                .join(Tag, Tag.id == SubscriberTag.tag_id)
                .where(Tag.name.in_(include_tags))
                .group_by(SubscriberTag.subscriber_id)
                .having(func.count(func.distinct(Tag.name)) == len(set(include_tags)))
            )
            query = query.where(Subscriber.id.in_(matching_all))
        if exclude_tags:
            matching_any = (
                select(SubscriberTag.subscriber_id)
                .join(Tag, Tag.id == SubscriberTag.tag_id)
                .where(Tag.name.in_(exclude_tags))
            )
            query = query.where(Subscriber.id.not_in(matching_any))
        query = query.order_by(Subscriber.id)
        values = [
            CampaignRecipient.campaign_id,
            CampaignRecipient.subscriber_id,
            CampaignRecipient.status,
            CampaignRecipient.attempts,
        ]
        dialect = self._session.get_bind().dialect.name
        statement: Any
        if dialect == "sqlite":
            statement = sqlite_insert(CampaignRecipient).from_select(values, query)
            statement = statement.on_conflict_do_nothing(
                index_elements=["campaign_id", "subscriber_id"]
            )
        elif dialect == "postgresql":
            statement = pg_insert(CampaignRecipient).from_select(values, query)
            statement = statement.on_conflict_do_nothing(
                index_elements=["campaign_id", "subscriber_id"]
            )
        else:
            raise RuntimeError(f"Unsupported database dialect for campaign snapshots: {dialect}")
        await self._session.execute(statement)
        count = await self._session.scalar(
            select(func.count(CampaignRecipient.id)).where(
                CampaignRecipient.campaign_id == campaign.id
            )
        )
        campaign.recipient_count = count or 0
        await self._session.flush()
        return campaign.recipient_count

    async def claim_batch(self, campaign_id: int, *, limit: int) -> list[RecipientTarget]:
        if limit < 1:
            return []
        inactive_subscribers = select(Subscriber.id).where(Subscriber.is_active.is_(False))
        skipped = await self._session.execute(
            update(CampaignRecipient)
            .where(
                CampaignRecipient.campaign_id == campaign_id,
                CampaignRecipient.status == "pending",
                CampaignRecipient.subscriber_id.in_(inactive_subscribers),
            )
            .values(status="skipped", last_error="subscriber_opted_out", updated_at=self._clock())
            .returning(CampaignRecipient.id)
        )
        skipped_count = len(list(skipped.scalars()))
        if skipped_count:
            await self._session.execute(
                update(Campaign)
                .where(Campaign.id == campaign_id)
                .values(skipped_count=Campaign.skipped_count + skipped_count)
            )

        active_subscribers = select(Subscriber.id).where(Subscriber.is_active.is_(True))
        candidate_ids = (
            select(CampaignRecipient.id)
            .where(
                CampaignRecipient.campaign_id == campaign_id,
                CampaignRecipient.status == "pending",
                CampaignRecipient.subscriber_id.in_(active_subscribers),
            )
            .order_by(CampaignRecipient.id)
            .limit(limit)
        )
        claimed = await self._session.scalars(
            update(CampaignRecipient)
            .where(
                CampaignRecipient.id.in_(candidate_ids),
                CampaignRecipient.status == "pending",
            )
            .values(
                status="sending",
                attempts=CampaignRecipient.attempts + 1,
                updated_at=self._clock(),
            )
            .returning(CampaignRecipient.id)
        )
        claimed_ids = list(claimed)
        if not claimed_ids:
            return []
        rows = await self._session.execute(
            select(
                CampaignRecipient.id,
                CampaignRecipient.subscriber_id,
                Subscriber.chat_id,
                CampaignRecipient.attempts,
            )
            .join(Subscriber, Subscriber.id == CampaignRecipient.subscriber_id)
            .where(CampaignRecipient.id.in_(claimed_ids))
            .order_by(CampaignRecipient.id)
        )
        return [RecipientTarget(*row) for row in rows]

    async def increment_attempt(self, recipient_id: int) -> int:
        result = await self._session.execute(
            update(CampaignRecipient)
            .where(CampaignRecipient.id == recipient_id, CampaignRecipient.status == "sending")
            .values(attempts=CampaignRecipient.attempts + 1, updated_at=self._clock())
            .returning(CampaignRecipient.attempts)
        )
        return result.scalar_one()

    async def delivery_state(self, campaign_id: int, subscriber_id: int) -> tuple[str, bool] | None:
        row = await self._session.execute(
            select(Campaign.status, Subscriber.is_active)
            .select_from(Campaign)
            .join(Subscriber, Subscriber.id == subscriber_id)
            .where(Campaign.id == campaign_id)
        )
        result = row.first()
        return (result[0], result[1]) if result is not None else None

    async def is_subscriber_active(self, subscriber_id: int) -> bool:
        active = await self._session.scalar(
            select(Subscriber.is_active).where(Subscriber.id == subscriber_id)
        )
        return active is True

    async def get_campaign(self, campaign_id: int) -> Campaign | None:
        return await self._session.get(Campaign, campaign_id)

    async def set_progress_message(self, campaign_id: int, message_id: int) -> None:
        campaign = await self._session.get(Campaign, campaign_id)
        if campaign is not None and campaign.progress_message_id is None:
            campaign.progress_message_id = message_id
            await self._session.flush()

    async def cancel_campaign(self, campaign_id: int) -> int:
        if await self._session.get(Campaign, campaign_id) is None:
            return 0
        result = await self._session.scalars(
            update(CampaignRecipient)
            .where(
                CampaignRecipient.campaign_id == campaign_id,
                CampaignRecipient.status == "pending",
            )
            .values(status="skipped", last_error="campaign_cancelled", updated_at=self._clock())
            .returning(CampaignRecipient.id)
        )
        count = len(list(result))
        if count:
            await self._session.execute(
                update(Campaign)
                .where(Campaign.id == campaign_id)
                .values(skipped_count=Campaign.skipped_count + count)
            )
        return count

    async def finish_batch(
        self,
        campaign_id: int,
        completions: list[RecipientCompletion],
    ) -> None:
        counters = {"sent": 0, "failed": 0, "blocked": 0, "skipped": 0}
        for completion in completions:
            if completion.status not in {*counters, "pending"}:
                raise ValueError("recipient completion status is invalid")
            result = await self._session.scalars(
                update(CampaignRecipient)
                .where(
                    CampaignRecipient.id == completion.recipient_id,
                    CampaignRecipient.status == "sending",
                )
                .values(
                    status=completion.status,
                    sent_message_id=completion.sent_message_id,
                    last_error=completion.safe_error,
                    updated_at=self._clock(),
                )
                .returning(CampaignRecipient.id)
            )
            if result.first() is None:
                continue
            if completion.status in counters:
                counters[completion.status] += 1
            if completion.status == "blocked":
                await self.deactivate_subscriber(completion.subscriber_id)
        counter_columns = {
            "sent": Campaign.sent_count,
            "failed": Campaign.failed_count,
            "blocked": Campaign.blocked_count,
            "skipped": Campaign.skipped_count,
        }
        updates = {
            column.key: column + count
            for status, count in counters.items()
            if count
            for column in (counter_columns[status],)
        }
        if updates:
            await self._session.execute(
                update(Campaign).where(Campaign.id == campaign_id).values(updates)
            )

    async def deactivate_subscriber(self, subscriber_id: int) -> None:
        await self._session.execute(
            update(Subscriber)
            .where(Subscriber.id == subscriber_id)
            .values(is_active=False, unsubscribed_at=self._clock())
        )

    async def reset_stuck_sending(self) -> int:
        resumable = select(Campaign.id).where(Campaign.status.in_(("running", "paused")))
        terminal_counts = await self._session.execute(
            select(CampaignRecipient.campaign_id, func.count(CampaignRecipient.id))
            .join(Campaign, Campaign.id == CampaignRecipient.campaign_id)
            .where(
                CampaignRecipient.status == "sending",
                Campaign.status.in_(("completed", "cancelled", "failed", "draft", "scheduled")),
            )
            .group_by(CampaignRecipient.campaign_id)
        )
        terminal_rows = list(terminal_counts)
        resumable_count = await self._session.scalar(
            select(func.count(CampaignRecipient.id)).where(
                CampaignRecipient.status == "sending",
                CampaignRecipient.campaign_id.in_(resumable),
            )
        )
        await self._session.execute(
            update(CampaignRecipient)
            .where(
                CampaignRecipient.status == "sending",
                CampaignRecipient.campaign_id.in_(resumable),
            )
            .values(
                status="pending",
                last_error="recovered_after_restart",
                updated_at=self._clock(),
            )
        )
        terminal_campaign_ids = [campaign_id for campaign_id, _ in terminal_rows]
        if terminal_campaign_ids:
            await self._session.execute(
                update(CampaignRecipient)
                .where(
                    CampaignRecipient.status == "sending",
                    CampaignRecipient.campaign_id.in_(terminal_campaign_ids),
                )
                .values(
                    status="skipped",
                    last_error="recovered_terminal_campaign",
                    updated_at=self._clock(),
                )
            )
            for campaign_id, count in terminal_rows:
                await self._session.execute(
                    update(Campaign)
                    .where(Campaign.id == campaign_id)
                    .values(skipped_count=Campaign.skipped_count + count)
                )
        return (resumable_count or 0) + sum(count for _, count in terminal_rows)

    async def pending_or_sending_count(self, campaign_id: int) -> int:
        count = await self._session.scalar(
            select(func.count(CampaignRecipient.id)).where(
                CampaignRecipient.campaign_id == campaign_id,
                CampaignRecipient.status.in_(("pending", "sending")),
            )
        )
        return count or 0

    async def complete_if_drained(self, campaign_id: int) -> bool:
        if await self.pending_or_sending_count(campaign_id):
            return False
        result = await self._session.scalars(
            update(Campaign)
            .where(Campaign.id == campaign_id, Campaign.status == "running")
            .values(status="completed", completed_at=self._clock())
            .returning(Campaign.id)
        )
        return result.first() is not None

    async def pending_summary_ids(self, *, limit: int = 20) -> list[int]:
        result = await self._session.scalars(
            select(Campaign.id)
            .where(Campaign.status == "completed", Campaign.summary_sent_at.is_(None))
            .order_by(Campaign.completed_at, Campaign.id)
            .limit(limit)
        )
        return list(result)

    async def mark_summary_sent(self, campaign_id: int) -> None:
        campaign = await self._session.get(Campaign, campaign_id)
        if campaign is not None and campaign.summary_sent_at is None:
            campaign.summary_sent_at = self._clock()
            await self._session.flush()
