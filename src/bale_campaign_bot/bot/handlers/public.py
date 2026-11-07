"""Public opt-in, opt-out, menu, and contact-sharing handlers."""

from __future__ import annotations

from bale_campaign_bot.bale.client import BaleClient
from bale_campaign_bot.bale.models import CallbackQuery, Message
from bale_campaign_bot.bot.i18n import fa
from bale_campaign_bot.bot.keyboards import (
    UNSUBSCRIBE_CALLBACK,
    phone_share_keyboard,
    public_menu,
)
from bale_campaign_bot.config.business import BusinessConfig
from bale_campaign_bot.services.subscribers import PhoneLinkStatus, SubscriberService


def parse_command(text: str) -> tuple[str, list[str]]:
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        return "", []
    command = parts[0].split("@", maxsplit=1)[0].casefold()
    return command, parts[1:]


class PublicHandlers:
    def __init__(
        self,
        client: BaleClient,
        subscribers: SubscriberService,
        business: BusinessConfig,
    ) -> None:
        self._client = client
        self._subscribers = subscribers
        self._business = business

    async def handle_message(self, message: Message) -> None:
        if message.chat.type != "private" or message.from_user is None:
            return
        if message.contact is not None:
            await self._handle_contact(message)
            return
        command, args = parse_command(message.text or "")
        if command == "/start":
            source = args[0][:256] if args else None
            await self._subscribers.start(
                user=message.from_user,
                chat=message.chat,
                source=source,
                consent_version=self._business.business.consent_version,
            )
            welcome = self._business.business.welcome_message.replace(
                "{business_name}",
                self._business.business.name,
            )
            await self._client.send_message(
                message.chat.id,
                welcome,
                reply_markup=public_menu(self._business.business),
            )
            if self._business.business.phone_linking_enabled:
                await self._client.send_message(
                    message.chat.id,
                    fa.SHARE_PHONE_PROMPT,
                    reply_markup=phone_share_keyboard(),
                )
            return
        if command == "/stop":
            await self._subscribers.stop(message.chat.id)
            await self._client.send_message(
                message.chat.id,
                self._business.business.opt_out_confirmation_text,
            )
            return
        if command == "/share_phone" and self._business.business.phone_linking_enabled:
            await self._client.send_message(
                message.chat.id,
                fa.SHARE_PHONE_PROMPT,
                reply_markup=phone_share_keyboard(),
            )
            return
        if command == "/about":
            await self._client.send_message(message.chat.id, self._business.business.about_text)
        elif command == "/contact":
            await self._client.send_message(message.chat.id, self._contact_text())

    async def handle_callback(self, query: CallbackQuery) -> None:
        await self._client.answer_callback_query(query.id)
        message = query.message
        if message is None or message.chat.type != "private" or query.data is None:
            return
        allowed = {button.callback for button in self._business.business.default_menu}
        if query.data == UNSUBSCRIBE_CALLBACK:
            await self._subscribers.stop(message.chat.id)
            await self._client.send_message(
                message.chat.id,
                self._business.business.opt_out_confirmation_text,
            )
            return
        if query.data not in allowed:
            return
        response = self._callback_response(query.data)
        if response is None:
            await self._client.send_message(message.chat.id, fa.CALLBACK_UNKNOWN)
        else:
            await self._client.send_message(message.chat.id, response)

    async def _handle_contact(self, message: Message) -> None:
        if not self._business.business.phone_linking_enabled or message.from_user is None:
            return
        contact = message.contact
        if contact is None:
            return
        if contact.user_id != message.from_user.id:
            await self._client.send_message(message.chat.id, fa.PHONE_NOT_YOURS)
            return
        status = await self._subscribers.link_phone(message.chat.id, contact.phone_number)
        messages = {
            PhoneLinkStatus.LINKED: fa.PHONE_LINKED,
            PhoneLinkStatus.INVALID: fa.PHONE_INVALID,
            PhoneLinkStatus.NOT_SUBSCRIBED: fa.PHONE_NOT_SUBSCRIBED,
            PhoneLinkStatus.ALREADY_LINKED: fa.PHONE_ALREADY_LINKED,
        }
        await self._client.send_message(message.chat.id, messages[status])

    def _callback_response(self, callback_data: str) -> str | None:
        profile = self._business.business
        if callback_data == "about":
            return profile.about_text
        if callback_data == "contact":
            return self._contact_text()
        if callback_data == "working_hours":
            return profile.working_hours or None
        if callback_data == "support":
            return profile.support_contact
        return None

    def _contact_text(self) -> str:
        profile = self._business.business
        contact = profile.contact
        values = [
            value
            for value in (
                contact.phone,
                str(contact.website) if contact.website else None,
                contact.address,
            )
            if value
        ]
        values.extend(str(link) for link in contact.social_links)
        if profile.support_contact:
            values.append(profile.support_contact)
        return "\n".join(values) if values else "اطلاعات تماس ثبت نشده است."
