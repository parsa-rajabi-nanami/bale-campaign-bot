from __future__ import annotations

import asyncio
import stat
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import respx

from bale_campaign_bot import cli
from bale_campaign_bot.bale.models import User, WebhookInfo
from bale_campaign_bot.config.business import load_business_config
from bale_campaign_bot.config.settings import Settings


def business_answers() -> dict[str, object]:
    return {
        "business": {
            "name": "Demo",
            "short_description": "Short description",
            "welcome_message": "Welcome",
            "about_text": "About",
            "contact": {"website": "", "social_links": []},
            "consent_text": "I agree to receive messages",
            "consent_version": "1",
            "opt_out_confirmation_text": "You are unsubscribed",
            "timezone": "Asia/Tehran",
            "locale": "en",
        },
        "campaign_defaults": {"send_rate_per_second": 10, "concurrency": 5},
    }


@pytest.mark.asyncio
async def test_init_validates_token_and_writes_private_files(
    fake_bale_server: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_bale_server.post("getMe").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "result": {"id": 123, "is_bot": True, "first_name": "DemoBot"}},
        )
    )
    monkeypatch.setattr(cli, "_business_answers", business_answers)
    env_path = tmp_path / ".env"
    business_path = tmp_path / "config" / "business.yaml"

    result = await cli.run_init(
        token_reader=lambda: "TEST_TOKEN",
        env_path=env_path,
        business_path=business_path,
    )

    assert result == 0
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(business_path.stat().st_mode) == 0o600
    settings = Settings(_env_file=env_path)
    assert settings.token_value == "TEST_TOKEN"
    assert load_business_config(business_path).business.name == "Demo"


@pytest.mark.asyncio
async def test_init_refuses_overwrite_without_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("keep this", encoding="utf-8")
    monkeypatch.setattr(cli, "_ask_yes_no", lambda _: False)

    result = await cli.run_init(
        token_reader=lambda: pytest.fail("token must not be requested after refusal"),
        env_path=env_path,
        business_path=tmp_path / "business.yaml",
    )
    assert result == 1
    assert env_path.read_text(encoding="utf-8") == "keep this"


@pytest.mark.asyncio
async def test_doctor_checks_local_db_and_bale_api(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    business_path = tmp_path / "business.yaml"
    import yaml

    business_path.write_text(yaml.safe_dump(business_answers()), encoding="utf-8")
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'doctor.db'}"
    await asyncio.to_thread(cli.run_migrations, database_url)

    class FakeClient:
        def __init__(self, token: str, *, proxy: str | None = None) -> None:
            assert token == "token"
            assert proxy is None

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def get_me(self) -> User:
            return User(id=1, is_bot=True, first_name="Demo", username="demo")

        async def get_webhook_info(self) -> WebhookInfo:
            return WebhookInfo(url="")

    monkeypatch.setattr(cli, "BaleClient", cast(Any, FakeClient))
    settings = Settings(
        _env_file=None,
        bale_bot_token="token",
        database_url=database_url,
        business_config_path=str(business_path),
    )

    from io import StringIO

    output = StringIO()
    result = await cli.run_doctor(settings, stream=output)
    assert result == 0
    assert "[OK] migrations" in output.getvalue()
    assert "long polling" in output.getvalue()
