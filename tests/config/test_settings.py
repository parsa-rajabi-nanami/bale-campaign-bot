from __future__ import annotations

import pytest
from pydantic import ValidationError

from bale_campaign_bot.config.settings import Settings


def test_settings_defaults_and_secret_token() -> None:
    settings = Settings(_env_file=None, bale_bot_token="123:secret", admin_ids=" 12, 34 ")
    assert settings.database_url == "sqlite+aiosqlite:///./data/bale_campaign_bot.db"
    assert settings.token_value == "123:secret"
    assert "secret" not in repr(settings.bale_bot_token)
    assert settings.admin_id_set == frozenset({12, 34})


def test_empty_proxy_is_treated_as_unset() -> None:
    settings = Settings(_env_file=None, https_proxy="")
    assert settings.https_proxy is None


@pytest.mark.parametrize("admin_ids", ["zero", "-1", "10,10"])
def test_settings_reject_invalid_admin_ids(admin_ids: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, admin_ids=admin_ids)


@pytest.mark.parametrize(
    ("database_url", "expected"),
    [
        ("sqlite:///./bot.db", "sqlite+aiosqlite:///./bot.db"),
        (
            "postgresql://user:pass@localhost/bot",
            "postgresql+asyncpg://user:pass@localhost/bot",
        ),
    ],
)
def test_settings_normalize_sync_style_database_urls(database_url: str, expected: str) -> None:
    settings = Settings(_env_file=None, database_url=database_url)
    assert settings.database_url == expected


def test_settings_reject_invalid_database_and_token_values() -> None:
    with pytest.raises(ValidationError, match="must use SQLite or PostgreSQL"):
        Settings(_env_file=None, database_url="mysql://db")
    with pytest.raises(ValidationError, match="cannot contain whitespace"):
        Settings(_env_file=None, bale_bot_token="token with spaces")
    with pytest.raises(ValidationError, match="greater than zero"):
        Settings(_env_file=None, send_rate_per_second=0)
