"""Validated Bale keyboards assembled from operator configuration."""

from bale_campaign_bot.bale.models import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from bale_campaign_bot.config.business import BusinessProfile

from .i18n import fa

UNSUBSCRIBE_CALLBACK = "__unsubscribe__"


def public_menu(profile: BusinessProfile) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=button.label,
                url=str(button.url) if button.url is not None else None,
                callback_data=button.callback,
            )
        ]
        for button in profile.default_menu
    ]
    rows.append(
        [InlineKeyboardButton(text=fa.UNSUBSCRIBE_BUTTON, callback_data=UNSUBSCRIBE_CALLBACK)]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def phone_share_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=fa.SHARE_PHONE_BUTTON, request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
