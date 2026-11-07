"""Durable, rate-limited campaign delivery with bounded batch recovery."""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bale_campaign_bot.bale.client import BaleClient
from bale_campaign_bot.bale.errors import (
    BaleAPIError,
    BaleError,
    BaleHTTPError,
    BaleNetworkError,
)
from bale_campaign_bot.bale.models import InlineKeyboardButton, InlineKeyboardMarkup
from bale_campaign_bot.config.business import BusinessConfig
from bale_campaign_bot.db.models import Campaign
from bale_campaign_bot.db.repositories import (
    BroadcastRepository,
    RecipientCompletion,
    RecipientTarget,
)
from bale_campaign_bot.services.scheduler import CampaignScheduler

Clock = Callable[[], datetime]
MonotonicClock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]
Jitter = Callable[[], float]
logger = structlog.get_logger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AdaptiveTokenBucket:
    """Process-wide token bucket that slows after 429s and recovers gradually."""

    def __init__(
        self,
        rate_per_second: float,
        *,
        monotonic: MonotonicClock,
        sleep: Sleep = asyncio.sleep,
        jitter: Jitter = random.random,
        recovery_every: int = 20,
    ) -> None:
        if rate_per_second <= 0 or recovery_every < 1:
            raise ValueError("rate and recovery interval must be positive")
        self._configured_rate = rate_per_second
        self._rate = rate_per_second
        self._minimum_rate = min(rate_per_second, 0.1)
        self._capacity = max(1.0, rate_per_second)
        self._tokens = self._capacity
        self._monotonic = monotonic
        self._sleep = sleep
        self._jitter = jitter
        self._recovery_every = recovery_every
        self._successes_since_recovery = 0
        self._rate_limit_count = 0
        self._blocked_until = 0.0
        self._updated_at = monotonic()
        self._lock = asyncio.Lock()

    @property
    def current_rate(self) -> float:
        return self._rate

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = self._monotonic()
                self._tokens = min(
                    self._capacity,
                    self._tokens + max(0.0, now - self._updated_at) * self._rate,
                )
                self._updated_at = now
                wait = max(0.0, self._blocked_until - now)
                if wait == 0.0 and self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                if wait == 0.0:
                    wait = (1.0 - self._tokens) / self._rate
            await self._sleep(max(wait, 0.001))

    async def rate_limited(self, retry_after: float | None) -> float:
        async with self._lock:
            now = self._monotonic()
            self._rate_limit_count += 1
            self._rate = max(self._minimum_rate, self._rate * 0.5)
            delay = retry_after
            if delay is None:
                base = min(60.0, 0.5 * (2 ** min(self._rate_limit_count - 1, 7)))
                delay = base * (0.5 + self._jitter())
            delay = max(0.0, min(delay, 86_400.0))
            self._blocked_until = max(self._blocked_until, now + delay)
            self._tokens = 0.0
            self._updated_at = now
            return delay

    async def record_success(self) -> None:
        async with self._lock:
            self._successes_since_recovery += 1
            if self._successes_since_recovery >= self._recovery_every:
                increment = max(0.1, self._configured_rate * 0.05)
                self._rate = min(self._configured_rate, self._rate + increment)
                self._successes_since_recovery = 0
                if self._rate >= self._configured_rate:
                    self._rate_limit_count = 0


class BroadcastWorker:
    def __init__(
        self,
        client: BaleClient,
        session_factory: async_sessionmaker[AsyncSession],
        business: BusinessConfig,
        *,
        rate_per_second: float,
        concurrency: int,
        max_attempts: int = 5,
        batch_size: int | None = None,
        lease_seconds: int = 60,
        progress_interval_seconds: float = 7.0,
        clock: Clock = _utc_now,
        monotonic: MonotonicClock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        jitter: Jitter = random.random,
    ) -> None:
        if concurrency < 1 or max_attempts < 1:
            raise ValueError("concurrency and max attempts must be positive")
        if progress_interval_seconds < 1:
            raise ValueError("progress update interval must be at least one second")
        self._client = client
        self._session_factory = session_factory
        self._business = business
        self._rate = rate_per_second
        self._concurrency = concurrency
        self._max_attempts = max_attempts
        if batch_size is not None and batch_size < 1:
            raise ValueError("batch size must be positive")
        self._batch_size = min(100, batch_size or max(5, concurrency * 2))
        self._lease_seconds = lease_seconds
        self._progress_interval = progress_interval_seconds
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self._bucket = AdaptiveTokenBucket(
            rate_per_second,
            monotonic=monotonic,
            sleep=sleep,
            jitter=jitter,
        )
        self._scheduler = CampaignScheduler(
            session_factory,
            owner=f"sender-{uuid.uuid4().hex}",
            clock=clock,
            lease_seconds=lease_seconds,
        )
        self._last_progress_at: dict[int, float] = {}
        self._monotonic = monotonic
        self._database_write_lock = asyncio.Lock()

    async def run(self, stop_event: asyncio.Event) -> None:
        """Run one leased scheduler/sender loop until graceful shutdown."""
        while not stop_event.is_set():
            if not await self._scheduler.acquire_lease():
                await self._sleep(1.0)
                continue
            lease_lost = asyncio.Event()
            renew_task = asyncio.create_task(
                self._renew_lease(stop_event, lease_lost),
                name="campaign-lease-renewal",
            )
            try:
                recovered = await self._scheduler.recover_stuck_recipients()
                if recovered:
                    logger.warning("campaign_recipients_recovered", count=recovered)
                while not stop_event.is_set() and not lease_lost.is_set():
                    await self._scheduler.activate_due_campaigns()
                    await self._send_pending_summaries()
                    campaign_ids = await self._scheduler.running_campaign_ids()
                    if not campaign_ids:
                        await self._sleep(0.5)
                        continue
                    for campaign_id in campaign_ids:
                        if stop_event.is_set() or lease_lost.is_set():
                            break
                        await self._process_batch(campaign_id)
            finally:
                renew_task.cancel()
                await asyncio.gather(renew_task, return_exceptions=True)
                await self._scheduler.release_lease()

    async def _renew_lease(self, stop_event: asyncio.Event, lease_lost: asyncio.Event) -> None:
        interval = max(1.0, self._lease_seconds / 3)
        while not stop_event.is_set():
            await self._sleep(interval)
            if stop_event.is_set():
                return
            if not await self._scheduler.renew_lease():
                lease_lost.set()
                logger.error("campaign_lease_lost")
                return

    async def _process_batch(self, campaign_id: int) -> None:
        await self._ensure_progress_message(campaign_id)
        async with self._session_factory() as session, session.begin():
            repo = BroadcastRepository(session, clock=self._clock)
            campaign = await repo.get_campaign(campaign_id)
            if campaign is None or campaign.status != "running":
                return
            targets = await repo.claim_batch(campaign_id, limit=self._batch_size)
        if targets:
            semaphore = asyncio.Semaphore(self._concurrency)

            async def limited_delivery(target: RecipientTarget) -> RecipientCompletion:
                async with semaphore:
                    return await self._deliver(campaign, target)

            completions = await asyncio.gather(*(limited_delivery(target) for target in targets))
            async with (
                self._database_write_lock,
                self._session_factory() as session,
                session.begin(),
            ):
                await BroadcastRepository(session, clock=self._clock).finish_batch(
                    campaign_id,
                    completions,
                )
            await self._update_progress(campaign_id)
        async with self._session_factory() as session, session.begin():
            completed = await BroadcastRepository(session, clock=self._clock).complete_if_drained(
                campaign_id
            )
        if completed:
            await self._update_progress(campaign_id, force=True)
            await self._send_summary(campaign_id)

    async def _deliver(
        self,
        campaign: Campaign,
        target: RecipientTarget,
    ) -> RecipientCompletion:
        campaign_id = campaign.id
        async with self._session_factory() as session:
            active = await BroadcastRepository(session).is_subscriber_active(target.subscriber_id)
        if not active:
            return RecipientCompletion(
                target.recipient_id,
                target.subscriber_id,
                "skipped",
                safe_error="subscriber_opted_out",
            )

        attempt = target.attempts
        for retry_index in range(self._max_attempts):
            if retry_index:
                async with self._session_factory() as session:
                    delivery_state = await BroadcastRepository(session).delivery_state(
                        campaign_id,
                        target.subscriber_id,
                    )
                if delivery_state is None or delivery_state[0] != "running":
                    status = (
                        "pending"
                        if delivery_state is not None and delivery_state[0] == "paused"
                        else "skipped"
                    )
                    return RecipientCompletion(
                        target.recipient_id,
                        target.subscriber_id,
                        status,
                        safe_error="campaign_not_running",
                    )
                if not delivery_state[1]:
                    return RecipientCompletion(
                        target.recipient_id,
                        target.subscriber_id,
                        "skipped",
                        safe_error="subscriber_opted_out",
                    )
                async with (
                    self._database_write_lock,
                    self._session_factory() as session,
                    session.begin(),
                ):
                    attempt = await BroadcastRepository(
                        session,
                        clock=self._clock,
                    ).increment_attempt(target.recipient_id)
            if attempt > self._max_attempts:
                return RecipientCompletion(
                    target.recipient_id,
                    target.subscriber_id,
                    "failed",
                    safe_error="attempt_limit_reached",
                )
            await self._bucket.acquire()
            try:
                result = await self._client.copy_message(
                    target.chat_id,
                    campaign.source_chat_id,
                    campaign.source_message_id,
                )
            except BaleError as exc:
                if self._is_permanent_recipient_error(exc):
                    return RecipientCompletion(
                        target.recipient_id,
                        target.subscriber_id,
                        "blocked",
                        safe_error=self._safe_error_code(exc),
                    )
                retryable = self._is_retryable(exc)
                if self._is_rate_limited(exc):
                    hint = (
                        exc.retry_after if isinstance(exc, (BaleAPIError, BaleHTTPError)) else None
                    )
                    delay = await self._bucket.rate_limited(hint)
                elif retryable and attempt < self._max_attempts:
                    delay = min(60.0, 0.5 * (2 ** (attempt - 1))) * (0.5 + self._jitter())
                else:
                    delay = 0.0
                if retryable and attempt < self._max_attempts:
                    if delay > 0 and not self._is_rate_limited(exc):
                        await self._sleep(delay)
                    continue
                return RecipientCompletion(
                    target.recipient_id,
                    target.subscriber_id,
                    "failed",
                    safe_error=self._safe_error_code(exc),
                )

            await self._bucket.record_success()
            await self._send_followups(campaign, target.chat_id)
            return RecipientCompletion(
                target.recipient_id,
                target.subscriber_id,
                "sent",
                sent_message_id=result.message_id,
            )
        return RecipientCompletion(
            target.recipient_id,
            target.subscriber_id,
            "failed",
            safe_error="attempt_limit_reached",
        )

    async def _send_followups(self, campaign: Campaign, chat_id: int) -> None:
        campaign_id = campaign.id
        if campaign.cta_buttons:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=item["label"], url=item["url"])]
                    for item in campaign.cta_buttons
                ]
            )
            try:
                await self._client.send_message(
                    chat_id,
                    "برای ادامه از این پیوندها استفاده کنید:",
                    reply_markup=keyboard,
                )
            except BaleError as exc:
                logger.warning(
                    "campaign_cta_followup_failed",
                    campaign_id=campaign_id,
                    error_type=type(exc).__name__,
                )
        profile = self._business.business
        if profile.footer_enabled and profile.footer_text:
            try:
                await self._client.send_message(chat_id, profile.footer_text)
            except BaleError as exc:
                logger.warning(
                    "campaign_footer_followup_failed",
                    campaign_id=campaign_id,
                    error_type=type(exc).__name__,
                )

    async def _ensure_progress_message(self, campaign_id: int) -> None:
        campaign = await self._get_campaign(campaign_id)
        if campaign.progress_message_id is not None:
            return
        try:
            message = await self._client.send_message(
                campaign.created_by,
                self._progress_text(campaign),
            )
        except BaleError as exc:
            logger.warning(
                "campaign_progress_message_failed",
                campaign_id=campaign_id,
                error_type=type(exc).__name__,
            )
            return
        async with self._session_factory() as session, session.begin():
            await BroadcastRepository(session, clock=self._clock).set_progress_message(
                campaign_id,
                message.message_id,
            )

    async def _update_progress(self, campaign_id: int, *, force: bool = False) -> None:
        now = self._monotonic()
        previous = self._last_progress_at.get(campaign_id, 0.0)
        if not force and now - previous < self._progress_interval:
            return
        campaign = await self._get_campaign(campaign_id)
        if campaign.progress_message_id is None:
            await self._ensure_progress_message(campaign_id)
            campaign = await self._get_campaign(campaign_id)
        if campaign.progress_message_id is None:
            return
        self._last_progress_at[campaign_id] = now
        try:
            await self._client.edit_message_text(
                campaign.created_by,
                campaign.progress_message_id,
                self._progress_text(campaign),
            )
        except BaleError as exc:
            logger.warning(
                "campaign_progress_edit_failed",
                campaign_id=campaign_id,
                error_type=type(exc).__name__,
            )

    async def _send_pending_summaries(self) -> None:
        async with self._session_factory() as session:
            repo = BroadcastRepository(session, clock=self._clock)
            campaign_ids = await repo.pending_summary_ids()
        for campaign_id in campaign_ids:
            await self._send_summary(campaign_id)

    async def _send_summary(self, campaign_id: int) -> None:
        campaign = await self._get_campaign(campaign_id)
        if campaign.summary_sent_at is not None or campaign.status != "completed":
            return
        started = self._normalize_time(campaign.started_at) or self._clock()
        completed = self._normalize_time(campaign.completed_at) or self._clock()
        duration = max(0.0, (completed - started).total_seconds())
        effective_rate = campaign.sent_count / duration if duration > 0 else 0.0
        summary = (
            f"گزارش کمپین #{campaign.id}: {_safe_summary_name(campaign.name)}\n"
            f"ارسال‌شده: {campaign.sent_count}\n"
            f"ناموفق: {campaign.failed_count}\n"
            f"مسدود: {campaign.blocked_count}\n"
            f"ردشده: {campaign.skipped_count}\n"
            f"مدت: {duration:.1f} ثانیه\n"
            f"نرخ مؤثر: {effective_rate:.2f} پیام در ثانیه"
        )
        try:
            await self._client.send_message(campaign.created_by, summary)
        except BaleError as exc:
            logger.warning(
                "campaign_summary_send_failed",
                campaign_id=campaign_id,
                error_type=type(exc).__name__,
            )
            return
        async with self._session_factory() as session, session.begin():
            await BroadcastRepository(session, clock=self._clock).mark_summary_sent(campaign_id)
        await self._update_progress(campaign_id, force=True)
        self._last_progress_at.pop(campaign_id, None)

    async def _get_campaign(self, campaign_id: int) -> Campaign:
        async with self._session_factory() as session:
            campaign = await BroadcastRepository(session).get_campaign(campaign_id)
            if campaign is None:
                raise RuntimeError("campaign disappeared while sending")
            return campaign

    def _progress_text(self, campaign: Campaign) -> str:
        status = {
            "running": "در حال ارسال",
            "paused": "متوقف",
            "completed": "تکمیل‌شده",
            "cancelled": "لغوشده",
            "failed": "ناموفق",
        }.get(campaign.status, "زمان‌بندی‌شده")
        return (
            f"پیشرفت کمپین #{campaign.id}\n"
            f"وضعیت: {status}\n"
            f"ارسال‌شده: {campaign.sent_count} از {campaign.recipient_count}\n"
            f"ناموفق: {campaign.failed_count}، مسدود: {campaign.blocked_count}، "
            f"ردشده: {campaign.skipped_count}"
        )

    @staticmethod
    def _normalize_time(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @staticmethod
    def _is_permanent_recipient_error(error: BaleError) -> bool:
        if isinstance(error, BaleHTTPError) and error.status_code == 403:
            return True
        if isinstance(error, BaleAPIError):
            if error.error_code == 403:
                return True
            description = error.description.casefold()
            return any(
                fragment in description
                for fragment in (
                    "blocked",
                    "forbidden",
                    "chat not found",
                    "user not found",
                    "bot was blocked",
                    "deactivated",
                )
            )
        return False

    @staticmethod
    def _is_retryable(error: BaleError) -> bool:
        return (
            isinstance(error, BaleNetworkError)
            or (isinstance(error, BaleHTTPError) and error.status_code >= 500)
            or BroadcastWorker._is_rate_limited(error)
        )

    @staticmethod
    def _is_rate_limited(error: BaleError) -> bool:
        return (isinstance(error, BaleAPIError) and error.rate_limited) or (
            isinstance(error, BaleHTTPError) and error.status_code == 429
        )

    @staticmethod
    def _safe_error_code(error: BaleError) -> str:
        if isinstance(error, BaleAPIError) and error.error_code is not None:
            return f"{type(error).__name__}:code={error.error_code}"
        if isinstance(error, BaleHTTPError):
            return f"{type(error).__name__}:status={error.status_code}"
        return type(error).__name__


def _safe_summary_name(value: str) -> str:
    return (
        value.replace("*", chr(0xFF0A))
        .replace("_", chr(0xFF3F))
        .replace("[", chr(0xFF3B))
        .replace("]", chr(0xFF3D))
        .replace("(", chr(0xFF08))
        .replace(")", chr(0xFF09))
    )
