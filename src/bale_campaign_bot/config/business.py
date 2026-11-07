"""Pydantic schema and bilingual validation for operator-owned business YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


class BusinessConfigError(ValueError):
    """Invalid YAML or business configuration with bilingual operator guidance."""


class BusinessModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MenuButton(BusinessModel):
    label: str = Field(min_length=1, max_length=64)
    url: AnyHttpUrl | None = None
    callback: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def require_exactly_one_action(self) -> MenuButton:
        if (self.url is None) == (self.callback is None):
            raise ValueError("exactly one of url or callback must be set")
        if self.callback is not None and len(self.callback.encode("utf-8")) > 64:
            raise ValueError("callback must fit within Bale's 64-byte callback_data limit")
        return self


class ContactInfo(BusinessModel):
    phone: str | None = Field(default=None, max_length=40)
    website: AnyHttpUrl | None = None
    address: str | None = Field(default=None, max_length=500)
    social_links: list[AnyHttpUrl] = Field(default_factory=list)

    @field_validator("phone", "website", "address", mode="before")
    @classmethod
    def empty_optional_values_are_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value


class QuietHours(BusinessModel):
    enabled: bool = False
    start: str = "22:00"
    end: str = "08:00"

    @field_validator("start", "end")
    @classmethod
    def validate_hhmm(cls, value: str) -> str:
        try:
            hour_text, minute_text = value.split(":", maxsplit=1)
            hour, minute = int(hour_text), int(minute_text)
        except (ValueError, TypeError):
            raise ValueError("time must use 24-hour HH:MM format") from None
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("time must use 24-hour HH:MM format")
        return f"{hour:02d}:{minute:02d}"


class CampaignDefaults(BusinessModel):
    send_rate_per_second: float = Field(default=10.0, gt=0, le=1000)
    concurrency: int = Field(default=5, ge=1, le=100)
    quiet_hours: QuietHours = Field(default_factory=QuietHours)


class BusinessProfile(BusinessModel):
    name: str = Field(min_length=1, max_length=120)
    short_description: str = Field(min_length=1, max_length=500)
    welcome_message: str = Field(min_length=1, max_length=4096)
    about_text: str = Field(min_length=1, max_length=4096)
    contact: ContactInfo = Field(default_factory=ContactInfo)
    working_hours: str = Field(default="", max_length=500)
    support_contact: str | None = Field(default=None, max_length=200)
    default_menu: list[MenuButton] = Field(default_factory=list, max_length=20)
    consent_text: str = Field(min_length=1, max_length=2000)
    consent_version: str = Field(min_length=1, max_length=64)
    opt_out_confirmation_text: str = Field(min_length=1, max_length=1000)
    footer_text: str | None = Field(default=None, max_length=1000)
    footer_enabled: bool = False
    timezone: str = "Asia/Tehran"
    locale: Literal["fa", "en"] = "fa"

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError(f"Unknown IANA timezone: {value}") from None
        return value

    @field_validator("support_contact", "footer_text", mode="before")
    @classmethod
    def empty_optional_text_is_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def enabled_footer_has_text(self) -> BusinessProfile:
        if self.footer_enabled and not self.footer_text:
            raise ValueError("footer_text is required when footer_enabled is true")
        return self

    @property
    def is_rtl(self) -> bool:
        return self.locale == "fa"


class BusinessConfig(BusinessModel):
    business: BusinessProfile
    campaign_defaults: CampaignDefaults = Field(default_factory=CampaignDefaults)


def validate_business_config(data: Any) -> BusinessConfig:
    """Validate a decoded YAML object and raise bilingual field-specific errors."""
    try:
        return BusinessConfig.model_validate(data)
    except ValidationError as exc:
        details: list[str] = []
        for error in exc.errors():
            field_path = ".".join(str(part) for part in error["loc"]) or "root"
            detail = str(error["msg"])
            persian = (
                "این فیلد الزامی است" if detail == "Field required" else "مقدار این فیلد معتبر نیست"
            )
            details.append(f"- {field_path}: {persian} / {detail}")
        message = "پیکربندی کسب‌وکار معتبر نیست / Business configuration is invalid:\n"
        raise BusinessConfigError(message + "\n".join(details)) from None


def load_business_config(path: str | Path) -> BusinessConfig:
    """Load and validate business YAML with actionable bilingual failures."""
    config_path = Path(path)
    try:
        with config_path.open(encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
    except FileNotFoundError:
        raise BusinessConfigError(
            f"فایل پیکربندی پیدا نشد / Business config not found: {config_path}"
        ) from None
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        raise BusinessConfigError(
            f"خواندن فایل پیکربندی ممکن نیست / Cannot read valid YAML: {config_path}"
        ) from None
    if not isinstance(data, dict):
        raise BusinessConfigError("ریشهٔ YAML باید یک شیء باشد / YAML root must be a mapping")
    return validate_business_config(data)
