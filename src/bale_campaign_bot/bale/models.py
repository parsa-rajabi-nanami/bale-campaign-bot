"""Typed representations of the Bale Bot API's JSON objects."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BaleModel(BaseModel):
    """Base for wire models that may receive newer Bale fields over time."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class ResponseParameters(BaleModel):
    """Optional error hints. The API does not guarantee a stable field schema."""

    retry_after: Any | None = None


class ApiEnvelope[ResultT](BaleModel):
    """Common Bale response envelope."""

    ok: bool
    result: ResultT | None = None
    description: str | None = None
    error_code: int | None = None
    parameters: ResponseParameters | None = None


class User(BaleModel):
    id: int
    is_bot: bool
    first_name: str
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None


class Chat(BaleModel):
    id: int
    type: str
    title: str | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None


class MessageId(BaleModel):
    message_id: int


class KeyboardButton(BaleModel):
    text: str
    request_contact: bool | None = None
    request_location: bool | None = None


class ReplyKeyboardMarkup(BaleModel):
    keyboard: list[list[KeyboardButton]]
    resize_keyboard: bool = True
    one_time_keyboard: bool = False


class InlineKeyboardButton(BaleModel):
    text: str = Field(min_length=1)
    url: str | None = None
    callback_data: str | None = None

    @model_validator(mode="after")
    def validate_action(self) -> InlineKeyboardButton:
        if (self.url is None) == (self.callback_data is None):
            raise ValueError("exactly one of url or callback_data must be set")
        if (
            self.callback_data is not None
            and not 1 <= len(self.callback_data.encode("utf-8")) <= 64
        ):
            raise ValueError("callback_data must contain between 1 and 64 bytes")
        return self


class InlineKeyboardMarkup(BaleModel):
    inline_keyboard: list[list[InlineKeyboardButton]]


class Contact(BaleModel):
    phone_number: str
    first_name: str
    last_name: str | None = None
    user_id: int | None = None
    vcard: str | None = None


class Message(BaleModel):
    message_id: int
    date: int
    chat: Chat
    from_user: User | None = Field(default=None, alias="from")
    text: str | None = None
    caption: str | None = None
    entities: list[dict[str, Any]] | None = None
    caption_entities: list[dict[str, Any]] | None = None
    photo: list[dict[str, Any]] | None = None
    animation: dict[str, Any] | None = None
    audio: dict[str, Any] | None = None
    document: dict[str, Any] | None = None
    video: dict[str, Any] | None = None
    voice: dict[str, Any] | None = None
    contact: Contact | None = None
    location: dict[str, Any] | None = None


class CallbackQuery(BaleModel):
    id: str
    from_user: User = Field(alias="from")
    message: Message | None = None
    data: str | None = None


class Update(BaleModel):
    update_id: int
    message: Message | None = None
    edited_message: Message | None = None
    callback_query: CallbackQuery | None = None


class File(BaleModel):
    file_id: str
    file_unique_id: str | None = None
    file_size: int | None = None
    file_path: str | None = None


class WebhookInfo(BaleModel):
    url: str
    has_custom_certificate: bool | None = None
    pending_update_count: int | None = None
    last_error_date: int | None = None
    last_error_message: str | None = None
