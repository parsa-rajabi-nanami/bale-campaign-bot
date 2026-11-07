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


def admin_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=fa.ADMIN_NEW_CAMPAIGN), KeyboardButton(text=fa.ADMIN_CAMPAIGNS)],
            [KeyboardButton(text=fa.ADMIN_SUBSCRIBERS), KeyboardButton(text=fa.ADMIN_STATS)],
            [KeyboardButton(text=fa.ADMIN_SETTINGS)],
        ],
        resize_keyboard=True,
    )


def audience_choice_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=fa.AUDIENCE_ALL), KeyboardButton(text=fa.AUDIENCE_TAGS)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def schedule_choice_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=fa.SCHEDULE_NOW), KeyboardButton(text=fa.SCHEDULE_LATER)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def confirmation_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=fa.CONFIRM_CAMPAIGN), KeyboardButton(text=fa.SEND_TEST)],
            [KeyboardButton(text=fa.CANCEL_CAMPAIGN)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
