# ruff: noqa: RUF001
"""Private-chat owner claim, campaign wizard, and strict admin authorization."""

from __future__ import annotations

import math
from collections.abc import Callable, Collection
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bale_campaign_bot.bale.client import BaleClient
from bale_campaign_bot.bale.models import Message
from bale_campaign_bot.bot.fsm import FSMContext
from bale_campaign_bot.bot.handlers.public import parse_command
from bale_campaign_bot.bot.i18n import fa
from bale_campaign_bot.bot.keyboards import (
    admin_main_menu,
    audience_choice_keyboard,
    confirmation_keyboard,
    schedule_choice_keyboard,
)
from bale_campaign_bot.config.business import BusinessConfig
from bale_campaign_bot.db.repositories import AdminRepository
from bale_campaign_bot.services.admins import AdminBootstrapService, ClaimStatus
from bale_campaign_bot.services.audiences import AudienceError, AudienceService
from bale_campaign_bot.services.campaigns import CampaignError, CampaignService, CampaignStatus

Clock = Callable[[], datetime]
_MENU_STATE = "campaign"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _safe_text(value: str) -> str:
    """Neutralize Bale's documented formatting/link marker characters."""
    substitutions: dict[str, str | int | None] = {
        "*": chr(0xFF0A),
        "_": chr(0xFF3F),
        "[": chr(0xFF3B),
        "]": chr(0xFF3D),
        "(": chr(0xFF08),
        ")": chr(0xFF09),
    }
    return value.translate(str.maketrans(substitutions))


def _quiet_adjust(
    timestamp: datetime,
    *,
    timezone: ZoneInfo,
    enabled: bool,
    start_text: str,
    end_text: str,
) -> datetime:
    """Move a requested time to the next configured quiet-hours end if needed."""
    local = timestamp.astimezone(timezone)
    if not enabled:
        return local
    start_hour, start_minute = (int(part) for part in start_text.split(":"))
    end_hour, end_minute = (int(part) for part in end_text.split(":"))
    start = time(start_hour, start_minute)
    end = time(end_hour, end_minute)
    local_time = local.timetz().replace(tzinfo=None)
    crosses_midnight = start >= end
    is_quiet = (
        local_time >= start or local_time < end if crosses_midnight else start <= local_time < end
    )
    if not is_quiet:
        return local
    extra_day = timedelta(days=1) if crosses_midnight and local_time >= start else timedelta()
    end_date = local.date() + extra_day
    return datetime.combine(end_date, end, tzinfo=timezone)


def _parse_local_datetime(value: str, timezone: ZoneInfo) -> datetime:
    local = datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=timezone)
    round_trip = local.astimezone(UTC).astimezone(timezone).replace(tzinfo=None)
    if round_trip != local.replace(tzinfo=None):
        raise ValueError("local time does not exist in this timezone")
    return local


class AdminHandlers:
    def __init__(
        self,
        client: BaleClient,
        bootstrap: AdminBootstrapService,
        session_factory: async_sessionmaker[AsyncSession],
        business: BusinessConfig,
        *,
        configured_admin_ids: Collection[int],
        clock: Clock = _utc_now,
    ) -> None:
        self._client = client
        self._bootstrap = bootstrap
        self._session_factory = session_factory
        self._business = business
        self._admin_ids = frozenset(configured_admin_ids)
        self._clock = clock
        self._audiences = AudienceService(session_factory)
        self._campaigns = CampaignService(session_factory, clock=clock)

    async def handle_message(self, message: Message) -> bool:
        """Handle an admin update; return false when public handlers may process it."""
        if message.chat.type != "private" or message.from_user is None:
            return False
        command, args = parse_command(message.text or "")
        if command == "/claim":
            await self._handle_claim(message, args)
            return True
        user_id = message.from_user.id
        if not await is_authorized_admin(
            self._session_factory,
            user_id=user_id,
            chat_type=message.chat.type,
            configured_admin_ids=self._admin_ids,
        ):
            return False

        if await self._handle_campaign_control(message.chat.id, command):
            return True

        fsm = FSMContext(self._session_factory, user_id=user_id, chat_id=message.chat.id)
        stored = await fsm.get()
        if stored is not None:
            state, data = stored
            if (message.text or "").strip() == fa.CANCEL_CAMPAIGN or command == "/cancel":
                campaign_id = data.get("campaign_id")
                if isinstance(campaign_id, int):
                    try:
                        await self._campaigns.transition(campaign_id, CampaignStatus.CANCELLED)
                    except CampaignError:
                        await self._client.send_message(
                            message.chat.id,
                            fa.CAMPAIGN_STATE_INVALID,
                        )
                        return True
                await fsm.clear()
                await self._send_menu(message.chat.id, "فرایند کمپین لغو شد.")
                return True
            if command == "/test" and "campaign_id" in data:
                await self._send_test(message, data)
                return True
            await self._continue_campaign(message, fsm, state, data)
            return True

        text = (message.text or "").strip()
        if command in {"/admin", "/start"} or text == fa.BACK_TO_MENU:
            await self._send_menu(message.chat.id)
            return True
        if command in {"/campaign", "/newcampaign"} or text == fa.ADMIN_NEW_CAMPAIGN:
            await fsm.set("campaign_wait_payload")
            await self._client.send_message(
                message.chat.id,
                "پیام کمپین را بفرستید (متن، عکس، ویدیو، فایل، صدا یا پیام پشتیبانی‌شده).\n"
                "برای لغو، «لغو» را بفرستید.",
            )
            return True
        if command in {"/campaigns"} or text == fa.ADMIN_CAMPAIGNS:
            await self._send_campaign_list(message.chat.id, user_id)
            return True
        if command == "/tags" or text == fa.ADMIN_SUBSCRIBERS:
            await self._send_audience_help(message.chat.id)
            return True
        if command == "/tag":
            await self._handle_tag_command(message.chat.id, args)
            return True
        if command == "/addadmin":
            await self._handle_add_admin(message.chat.id, user_id, args)
            return True
        if command == "/stats" or text == fa.ADMIN_STATS:
            await self._send_campaign_list(message.chat.id, user_id)
            return True
        if command == "/settings" or text == fa.ADMIN_SETTINGS:
            await self._client.send_message(
                message.chat.id,
                "مدیران دیگر را با /addadmin در گفت‌وگوی خصوصی اضافه کنید.\n"
                "مدیر تازه باید شناسه عددی حساب Bale خود را بفرستد.",
            )
            return True
        return False

    async def _handle_claim(self, message: Message, args: list[str]) -> None:
        if message.from_user is None:
            return
        if len(args) != 1:
            await self._client.send_message(message.chat.id, fa.CLAIM_USAGE)
            return
        status = await self._bootstrap.claim(
            code=args[0],
            user_id=message.from_user.id,
            chat_type=message.chat.type,
        )
        messages = {
            ClaimStatus.CLAIMED: fa.CLAIM_SUCCESS,
            ClaimStatus.INVALID: fa.CLAIM_INVALID,
            ClaimStatus.EXPIRED: fa.CLAIM_EXPIRED,
            ClaimStatus.ALREADY_CLAIMED: fa.CLAIM_USED,
            ClaimStatus.PRIVATE_CHAT_REQUIRED: fa.CLAIM_PRIVATE_ONLY,
        }
        await self._client.send_message(message.chat.id, messages[status])

    async def _continue_campaign(
        self,
        message: Message,
        fsm: FSMContext,
        state: str,
        data: dict[str, Any],
    ) -> None:
        text = (message.text or "").strip()
        user = message.from_user
        if user is None:
            return
        if state == "campaign_wait_payload":
            if message.message_id <= 0:
                await self._client.send_message(message.chat.id, "پیام کمپین شناسه معتبری ندارد.")
                return
            await fsm.set(
                "campaign_wait_name",
                {"source_chat_id": message.chat.id, "source_message_id": message.message_id},
            )
            await self._client.send_message(message.chat.id, "نام کمپین را بفرستید.")
            return
        if state == "campaign_wait_name":
            if not text or len(text) > 200:
                await self._client.send_message(
                    message.chat.id,
                    "نام باید بین 1 تا 200 نویسه باشد.",
                )
                return
            campaign_id = await self._campaigns.create_draft(
                name=text,
                created_by=user.id,
                source_chat_id=int(data["source_chat_id"]),
                source_message_id=int(data["source_message_id"]),
            )
            campaign = await self._campaigns.get(campaign_id)
            await self._client.copy_message(
                message.chat.id,
                campaign.source_chat_id,
                campaign.source_message_id,
            )
            await self._client.send_message(
                message.chat.id,
                "پیش‌نمایش بالا ارسال شد. مخاطب را انتخاب کنید:",
                reply_markup=audience_choice_keyboard(),
            )
            await fsm.set("campaign_wait_audience", {"campaign_id": campaign_id})
            return
        if state == "campaign_wait_audience":
            if text == fa.AUDIENCE_ALL or command_is(text, "/all"):
                await self._finish_audience(message.chat.id, fsm, int(data["campaign_id"]), (), ())
                return
            if text == fa.AUDIENCE_TAGS or command_is(text, "/tags"):
                tags = await self._audiences.list_tags()
                displayed = (
                    ", ".join(_safe_text(tag) for tag in tags) if tags else "هیچ برچسبی نیست"
                )
                await fsm.set(
                    "campaign_wait_tags",
                    {
                        "campaign_id": int(data["campaign_id"]),
                        "include_tags": [],
                        "exclude_tags": [],
                    },
                )
                await self._client.send_message(
                    message.chat.id,
                    "برچسب‌های موجود: "
                    + displayed
                    + "\nبرچسب‌ها را با +نام برای شمول و -نام برای استثنا بفرستید؛ "
                    "برای ادامه «تمام» را بفرستید.",
                )
                return
            await self._client.send_message(
                message.chat.id,
                "یکی از گزینه‌های مخاطب را انتخاب کنید.",
            )
            return
        if state == "campaign_wait_tags":
            if text.casefold() in {"تمام", "done"}:
                await self._finish_audience(
                    message.chat.id,
                    fsm,
                    int(data["campaign_id"]),
                    tuple(str(item) for item in data.get("include_tags", [])),
                    tuple(str(item) for item in data.get("exclude_tags", [])),
                )
                return
            includes = list(data.get("include_tags", []))
            excludes = list(data.get("exclude_tags", []))
            for entry in text.split(","):
                entry = entry.strip()
                if len(entry) < 2 or entry[0] not in {"+", "-"}:
                    await self._client.send_message(
                        message.chat.id,
                        "هر برچسب را با + یا - بنویسید؛ نمونه: +خریدار,-غیرفعال",
                    )
                    return
                target = includes if entry[0] == "+" else excludes
                name = entry[1:].strip()
                if name and name not in target:
                    target.append(name)
            await fsm.set(
                state,
                {
                    "campaign_id": int(data["campaign_id"]),
                    "include_tags": includes,
                    "exclude_tags": excludes,
                },
            )
            await self._client.send_message(
                message.chat.id,
                "برچسب ذخیره شد؛ برچسب دیگر بفرستید یا «تمام».",
            )
            return
        if state == "campaign_wait_cta":
            campaign_id = int(data["campaign_id"])
            try:
                buttons = self._parse_cta(text)
                await self._campaigns.set_cta_buttons(campaign_id, buttons)
            except CampaignError:
                await self._client.send_message(message.chat.id, fa.CAMPAIGN_INPUT_INVALID)
                return
            await self._client.send_message(
                message.chat.id,
                "ارسال تست فقط پیام اصلی را به شما کپی می‌کند. برای زمان‌بندی انتخاب کنید:",
                reply_markup=schedule_choice_keyboard(),
            )
            await fsm.set("campaign_wait_schedule", {"campaign_id": campaign_id})
            return
        if state == "campaign_wait_schedule":
            campaign_id = int(data["campaign_id"])
            if text == fa.SCHEDULE_NOW or command_is(text, "/now"):
                schedule = self._clock().astimezone(
                    ZoneInfo(self._business.business.timezone)
                ) + timedelta(seconds=1)
                send_now = True
            elif text == fa.SCHEDULE_LATER or command_is(text, "/at"):
                await fsm.set(
                    state,
                    {"campaign_id": campaign_id, "awaiting_time": True, "send_now": False},
                )
                await self._client.send_message(
                    message.chat.id,
                    "زمان محلی را با قالب YYYY-MM-DD HH:MM بفرستید "
                    f"({self._business.business.timezone}).",
                )
                return
            elif data.get("awaiting_time") is True:
                send_now = False
                try:
                    schedule = _parse_local_datetime(
                        text,
                        ZoneInfo(self._business.business.timezone),
                    )
                    if schedule <= self._clock().astimezone(schedule.tzinfo):
                        raise ValueError
                except ValueError:
                    await self._client.send_message(message.chat.id, fa.CAMPAIGN_SCHEDULE_INVALID)
                    return
            else:
                await self._client.send_message(
                    message.chat.id,
                    "ارسال اکنون یا زمان‌بندی را انتخاب کنید.",
                )
                return
            defaults = self._business.campaign_defaults
            quiet = defaults.quiet_hours
            adjusted = _quiet_adjust(
                schedule,
                timezone=ZoneInfo(self._business.business.timezone),
                enabled=quiet.enabled,
                start_text=quiet.start,
                end_text=quiet.end,
            )
            campaign = await self._campaigns.get(campaign_id)
            await fsm.set(
                "campaign_wait_confirmation",
                {
                    "campaign_id": campaign_id,
                    "scheduled_at": adjusted.isoformat(),
                    "send_now": send_now,
                },
            )
            local_text = adjusted.strftime("%Y-%m-%d %H:%M %Z")
            estimate = math.ceil(campaign.recipient_count / defaults.send_rate_per_second)
            await self._client.send_message(
                message.chat.id,
                "تأیید کمپین\n"
                f"نام: {_safe_text(campaign.name)}\n"
                f"مخاطبان: {campaign.recipient_count}\n"
                f"زمان ارسال: {local_text}\n"
                f"مدت تخمینی: حدود {estimate} ثانیه\n"
                f"دکمهٔ پیگیری جداگانه: {len(campaign.cta_buttons)}",
                reply_markup=confirmation_keyboard(),
            )
            return
        if state == "campaign_wait_confirmation":
            if text == fa.SEND_TEST or command_is(text, "/test"):
                await self._send_test(message, data)
                return
            if text != fa.CONFIRM_CAMPAIGN and not command_is(text, "/confirm"):
                await self._client.send_message(
                    message.chat.id,
                    "تأیید و شروع یا لغو را انتخاب کنید.",
                )
                return
            scheduled_at = datetime.fromisoformat(str(data["scheduled_at"]))
            if data.get("send_now") is True:
                timezone = ZoneInfo(self._business.business.timezone)
                scheduled_at = self._clock().astimezone(timezone) + timedelta(seconds=1)
                quiet = self._business.campaign_defaults.quiet_hours
                scheduled_at = _quiet_adjust(
                    scheduled_at,
                    timezone=timezone,
                    enabled=quiet.enabled,
                    start_text=quiet.start,
                    end_text=quiet.end,
                )
            try:
                await self._campaigns.schedule(int(data["campaign_id"]), scheduled_at)
            except CampaignError:
                await self._client.send_message(
                    message.chat.id,
                    fa.CAMPAIGN_SCHEDULE_INVALID,
                )
                return
            await fsm.clear()
            await self._client.send_message(
                message.chat.id,
                "کمپین برای ارسال ثبت شد. مدیریت و وضعیت در «کمپین‌ها» در دسترس است.",
                reply_markup=admin_main_menu(),
            )

    async def _finish_audience(
        self,
        chat_id: int,
        fsm: FSMContext,
        campaign_id: int,
        includes: tuple[str, ...],
        excludes: tuple[str, ...],
    ) -> None:
        try:
            count = await self._campaigns.set_audience(
                campaign_id,
                include_tags=includes,
                exclude_tags=excludes,
            )
        except (AudienceError, CampaignError):
            await self._client.send_message(chat_id, fa.TAG_INPUT_INVALID)
            return
        if count == 0:
            await fsm.set("campaign_wait_audience", {"campaign_id": campaign_id})
            await self._client.send_message(
                chat_id,
                "این مخاطب هیچ عضو فعالی ندارد. همهٔ مخاطبان یا برچسب‌های دیگری را انتخاب کنید.",
                reply_markup=audience_choice_keyboard(),
            )
            return
        await fsm.set("campaign_wait_cta", {"campaign_id": campaign_id})
        await self._client.send_message(
            chat_id,
            f"مخاطب انتخاب شد: {count} نفر فعال.\n"
            "اختیاری: دکمهٔ پیگیری را در خط‌های `عنوان | https://...` بفرستید؛ "
            "برای بدون دکمه «-» را بفرستید.",
        )

    async def _send_test(self, message: Message, data: dict[str, Any]) -> None:
        campaign = await self._campaigns.get(int(data["campaign_id"]))
        await self._client.copy_message(
            message.chat.id,
            campaign.source_chat_id,
            campaign.source_message_id,
        )
        await self._client.send_message(message.chat.id, "ارسال تست فقط به حساب شما انجام شد.")

    async def _send_menu(self, chat_id: int, message: str = "منوی مدیریت") -> None:
        await self._client.send_message(chat_id, message, reply_markup=admin_main_menu())

    async def _send_campaign_list(self, chat_id: int, user_id: int) -> None:
        campaigns = await self._campaigns.list_for_admin(user_id)
        if not campaigns:
            await self._client.send_message(chat_id, "هنوز کمپینی ثبت نشده است.")
            return
        status_labels = {
            "draft": "پیش‌نویس",
            "scheduled": "زمان‌بندی‌شده",
            "running": "در حال ارسال",
            "paused": "متوقف",
            "completed": "تکمیل‌شده",
            "cancelled": "لغوشده",
            "failed": "ناموفق",
        }
        rows = [
            f"#{campaign.id} {_safe_text(campaign.name)} — "
            f"{status_labels.get(campaign.status, 'نامشخص')} — "
            f"مخاطب: {campaign.recipient_count}، ارسال: {campaign.sent_count}، "
            f"ناموفق: {campaign.failed_count}، مسدود: {campaign.blocked_count}"
            for campaign in campaigns
        ]
        await self._client.send_message(
            chat_id,
            "\n".join(rows)[:3900] + "\nکنترل: /pause_ID ، /resume_ID ، /cancel_ID",
        )

    async def _handle_campaign_control(self, chat_id: int, command: str) -> bool:
        control, separator, raw_id = command.partition("_")
        if not separator or control not in {"/pause", "/resume", "/cancel"}:
            return False
        try:
            campaign_id = int(raw_id)
            if campaign_id < 1:
                raise ValueError
            if control == "/pause":
                if (await self._campaigns.get(campaign_id)).status != "running":
                    raise CampaignError("only running campaigns can pause")
                await self._campaigns.transition(campaign_id, CampaignStatus.PAUSED)
                reply = "کمپین متوقف شد؛ کارهای در حال اجرا تمام می‌شوند."
            elif control == "/resume":
                if (await self._campaigns.get(campaign_id)).status != "paused":
                    raise CampaignError("only paused campaigns can resume")
                await self._campaigns.transition(campaign_id, CampaignStatus.RUNNING)
                reply = "کمپین از سر گرفته شد."
            else:
                await self._campaigns.cancel(campaign_id)
                reply = "کمپین لغو شد و گیرندگان باقی‌مانده رد شدند."
        except (ValueError, CampaignError):
            await self._client.send_message(chat_id, fa.CAMPAIGN_STATE_INVALID)
            return True
        await self._client.send_message(chat_id, reply)
        return True

    async def _send_audience_help(self, chat_id: int) -> None:
        count = await self._audiences.count()
        tags = await self._audiences.list_tags()
        visible = ", ".join(_safe_text(tag) for tag in tags) or "بدون برچسب"
        await self._client.send_message(
            chat_id,
            f"اعضای فعالِ قابل پیام: {count}\nبرچسب‌ها: {visible}\n"
            "ساخت برچسب: /tag create نام\nاختصاص به شناسهٔ گفت‌وگوی عضو: /tag assign نام شناسه",
        )

    async def _handle_tag_command(self, chat_id: int, args: list[str]) -> None:
        if len(args) >= 2 and args[0] == "create":
            try:
                name = await self._audiences.create_tag(" ".join(args[1:]))
            except AudienceError:
                await self._client.send_message(chat_id, fa.TAG_INPUT_INVALID)
                return
            await self._client.send_message(chat_id, f"برچسب «{_safe_text(name)}» ساخته شد.")
            return
        if len(args) == 3 and args[0] == "assign":
            try:
                chat_id_value = int(args[2])
            except ValueError:
                await self._client.send_message(chat_id, "شناسهٔ گفت‌وگو باید عدد باشد.")
                return
            found = await self._audiences.assign_tag(args[1], chat_id_value)
            await self._client.send_message(
                chat_id,
                "برچسب به عضو ذخیره‌شده اختصاص یافت."
                if found
                else "برچسب یا عضوِ ذخیره‌شده پیدا نشد؛ شماره تلفن به‌تنهایی کافی نیست.",
            )
            return
        await self._client.send_message(chat_id, fa.TAG_CREATE_HELP)

    async def _handle_add_admin(self, chat_id: int, user_id: int, args: list[str]) -> None:
        try:
            target_id = int(args[0]) if len(args) == 1 else 0
        except ValueError:
            target_id = 0
        if target_id <= 0:
            await self._client.send_message(chat_id, "دستور: /addadmin شناسه_عددی")
            return
        async with self._session_factory() as session, session.begin():
            owner = await AdminRepository(session).get(user_id)
            if owner is None or owner.role != "owner":
                await self._client.send_message(chat_id, "فقط مالک ثبت‌شده می‌تواند مدیر اضافه کند.")
                return
            await AdminRepository(session).add(target_id)
        await self._client.send_message(chat_id, f"مدیر {target_id} اضافه شد.")

    def _parse_cta(self, text: str) -> list[tuple[str, str]]:
        if text in {"-", "skip", "/skip"}:
            return []
        if not text:
            raise CampaignError("یک یا چند دکمهٔ CTA بفرستید یا «-» را برای ردکردن بفرستید.")
        buttons: list[tuple[str, str]] = []
        for line in text.splitlines():
            parts = line.split("|", maxsplit=1)
            if len(parts) != 2:
                raise CampaignError("قالب هر دکمه: عنوان | https://example.com")
            buttons.append((parts[0], parts[1]))
        return buttons


async def is_authorized_admin(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: int,
    chat_type: str,
    configured_admin_ids: Collection[int],
) -> bool:
    """Require a private chat and either an environment allowlist or DB admin row."""
    if chat_type != "private":
        return False
    if user_id in configured_admin_ids:
        return True
    async with session_factory() as session:
        return await AdminRepository(session).is_admin(user_id)


def command_is(text: str, command: str) -> bool:
    parsed, _ = parse_command(text)
    return parsed == command
