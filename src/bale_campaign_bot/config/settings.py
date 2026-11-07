"""Validated process settings loaded from environment variables and `.env`."""

from __future__ import annotations

import math
from functools import cached_property
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    bale_bot_token: SecretStr | None = None
    database_url: str = "sqlite+aiosqlite:///./data/bale_campaign_bot.db"
    admin_ids: str = ""
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True
    https_proxy: str | None = None
    business_config_path: str = "config/business.yaml"
    send_rate_per_second: float = 10.0
    send_concurrency: int = 5
    timezone: str = "Asia/Tehran"
    setup_code_ttl_seconds: int = 3600

    @field_validator("bale_bot_token", mode="before")
    @classmethod
    def normalize_token(cls, value: object) -> object:
        if value is None or value == "":
            return None
        if isinstance(value, str) and any(char.isspace() for char in value):
            raise ValueError("BALE_BOT_TOKEN cannot contain whitespace or newlines")
        return value

    @field_validator("https_proxy", mode="before")
    @classmethod
    def normalize_proxy(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("database_url")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        normalized = value.strip()
        if normalized.startswith("sqlite://"):
            normalized = normalized.replace("sqlite://", "sqlite+aiosqlite://", 1)
        elif normalized.startswith("postgresql://"):
            normalized = normalized.replace("postgresql://", "postgresql+asyncpg://", 1)
        if not normalized.startswith(("sqlite+aiosqlite://", "postgresql+asyncpg://")):
            raise ValueError("DATABASE_URL must use SQLite or PostgreSQL")
        return normalized

    @field_validator("admin_ids")
    @classmethod
    def validate_admin_ids(cls, value: str) -> str:
        parts = [part.strip() for part in value.split(",") if part.strip()]
        try:
            parsed = [int(part) for part in parts]
        except ValueError:
            raise ValueError("ADMIN_IDS must be comma-separated integer user IDs") from None
        if any(user_id <= 0 for user_id in parsed):
            raise ValueError("ADMIN_IDS must contain positive integer user IDs")
        if len(parsed) != len(set(parsed)):
            raise ValueError("ADMIN_IDS cannot contain duplicate user IDs")
        return ",".join(str(user_id) for user_id in parsed)

    @field_validator("send_rate_per_second")
    @classmethod
    def validate_send_rate(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("SEND_RATE_PER_SECOND must be greater than zero")
        return value

    @field_validator("send_concurrency")
    @classmethod
    def validate_concurrency(cls, value: int) -> int:
        if value < 1:
            raise ValueError("SEND_CONCURRENCY must be at least 1")
        return value

    @field_validator("setup_code_ttl_seconds")
    @classmethod
    def validate_setup_code_ttl(cls, value: int) -> int:
        if value < 60:
            raise ValueError("SETUP_CODE_TTL_SECONDS must be at least 60")
        return value

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError(f"Unknown IANA timezone: {value}") from None
        return value

    @cached_property
    def admin_id_set(self) -> frozenset[int]:
        return frozenset(int(part) for part in self.admin_ids.split(",") if part)

    @property
    def token_value(self) -> str | None:
        return self.bale_bot_token.get_secret_value() if self.bale_bot_token else None
