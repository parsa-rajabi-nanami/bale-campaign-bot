from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
import respx

from bale_campaign_bot.bale.client import BaleClient, RetryPolicy
from bale_campaign_bot.bale.errors import (
    BaleAPIError,
    BaleHTTPError,
    BaleNetworkError,
    BaleProtocolError,
)
from bale_campaign_bot.bale.models import InlineKeyboardButton, InlineKeyboardMarkup

pytestmark = pytest.mark.asyncio


def make_client(
    sleeps: list[float],
    *,
    max_attempts: int = 3,
    clock: Callable[[], datetime] | None = None,
) -> BaleClient:
    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return BaleClient(
        "TEST_TOKEN",
        retry_policy=RetryPolicy(max_attempts=max_attempts, base_delay=0.2, max_delay=1.0),
        sleep=record_sleep,
        jitter=lambda: 0.5,
        clock=clock if clock is not None else lambda: datetime.now(UTC),
    )


async def test_get_me_parses_success(fake_bale_server: respx.MockRouter) -> None:
    fake_bale_server.post("getMe").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "result": {"id": 123, "is_bot": True, "first_name": "Campaign"}},
        )
    )
    async with make_client([]) as client:
        user = await client.get_me()
    assert user.id == 123
    assert user.first_name == "Campaign"


async def test_api_error_envelope_is_typed_and_token_is_redacted(
    fake_bale_server: respx.MockRouter,
) -> None:
    fake_bale_server.post("getMe").mock(
        return_value=httpx.Response(
            401,
            json={
                "ok": False,
                "error_code": 401,
                "description": "invalid token TEST_TOKEN",
                "parameters": {"retry_after": "bad", "opaque": "x"},
            },
        )
    )
    async with make_client([]) as client:
        with pytest.raises(BaleAPIError) as captured:
            await client.get_me()
    assert captured.value.error_code == 401
    assert "TEST_TOKEN" not in str(captured.value)
    assert captured.value.parameters == {"retry_after": "bad", "opaque": "x"}


async def test_rate_limit_uses_parameters_hint_then_succeeds(
    fake_bale_server: respx.MockRouter,
) -> None:
    route = fake_bale_server.post("getMe")
    call_count = 0

    def response(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                429,
                json={"ok": False, "description": "slow down", "parameters": {"retry_after": 2}},
            )
        return httpx.Response(
            200,
            json={"ok": True, "result": {"id": 7, "is_bot": True, "first_name": "Bot"}},
        )

    route.mock(side_effect=response)
    sleeps: list[float] = []
    async with make_client(sleeps) as client:
        user = await client.get_me()
    assert user.id == 7
    assert sleeps == [2.0]
    assert call_count == 2


async def test_rate_limit_uses_retry_after_header(fake_bale_server: respx.MockRouter) -> None:
    route = fake_bale_server.post("getMe")
    call_count = 0

    def response(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return (
            httpx.Response(
                429,
                headers={"Retry-After": "3"},
                json={"ok": False, "description": "too many requests"},
            )
            if call_count == 1
            else httpx.Response(
                200,
                json={"ok": True, "result": {"id": 8, "is_bot": True, "first_name": "Bot"}},
            )
        )

    route.mock(side_effect=response)
    sleeps: list[float] = []
    async with make_client(sleeps) as client:
        user = await client.get_me()
    assert user.id == 8
    assert sleeps == [3.0]


async def test_rate_limit_without_hint_uses_backoff_then_raises(
    fake_bale_server: respx.MockRouter,
) -> None:
    fake_bale_server.post("getMe").mock(
        return_value=httpx.Response(429, json={"ok": False, "description": "rate limit"})
    )
    sleeps: list[float] = []
    async with make_client(sleeps, max_attempts=2) as client:
        with pytest.raises(BaleAPIError) as captured:
            await client.get_me()
    assert captured.value.rate_limited
    assert captured.value.retry_after is None
    assert sleeps == [pytest.approx(0.15)]


async def test_network_failure_retries_without_exposing_token(
    fake_bale_server: respx.MockRouter,
) -> None:
    route = fake_bale_server.post("getMe")
    calls = 0

    def fail(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection failed")

    route.mock(side_effect=fail)
    sleeps: list[float] = []
    async with make_client(sleeps, max_attempts=2) as client:
        with pytest.raises(BaleNetworkError) as captured:
            await client.get_me()
    assert captured.value.attempts == 2
    assert "TEST_TOKEN" not in str(captured.value)
    assert calls == 2
    assert sleeps == [pytest.approx(0.15)]


async def test_get_updates_validates_limit_and_parses_updates(
    fake_bale_server: respx.MockRouter,
) -> None:
    route = fake_bale_server.post("getUpdates").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 11,
                        "message": {
                            "message_id": 3,
                            "date": 1_700_000_000,
                            "chat": {"id": 123, "type": "private"},
                            "from": {"id": 123, "is_bot": False, "first_name": "Ada"},
                            "text": "/start campaign-a",
                        },
                    }
                ],
            },
        )
    )
    async with make_client([]) as client:
        with pytest.raises(ValueError):
            await client.get_updates(limit=101)
        with pytest.raises(ValueError):
            await client.get_updates(timeout=-1)
        updates = await client.get_updates(offset=10, limit=1, timeout=5)
    assert updates[0].update_id == 11
    assert updates[0].message is not None
    assert updates[0].message.from_user is not None
    assert updates[0].message.from_user.first_name == "Ada"
    assert route.calls[0].request.read() == b'{"limit":1,"offset":10,"timeout":5}'
    assert route.calls[0].request.extensions["timeout"]["read"] == 10.0


async def test_get_updates_omits_optional_parameters(fake_bale_server: respx.MockRouter) -> None:
    route = fake_bale_server.post("getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": []})
    )
    async with make_client([]) as client:
        updates = await client.get_updates()
    assert updates == []
    assert route.calls[0].request.read() == b'{"limit":100}'


async def test_retry_policy_validation_and_jitter() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError, match="cannot be negative"):
        RetryPolicy(base_delay=-1)
    with pytest.raises(ValueError, match="finite"):
        RetryPolicy(base_delay=float("inf"))
    with pytest.raises(ValueError, match="greater than or equal"):
        RetryPolicy(base_delay=2, max_delay=1)
    policy = RetryPolicy(max_attempts=2, base_delay=0.4, max_delay=0.5)
    assert policy.delay(0, 0) == pytest.approx(0.2)
    assert policy.delay(3, 1) == pytest.approx(0.5)
    assert policy.delay(0, 0.5, retry_after=4) == 4


async def test_empty_token_and_message_length_are_rejected() -> None:
    with pytest.raises(ValueError, match="token cannot be empty"):
        BaleClient(" ")
    async with make_client([]) as client:
        with pytest.raises(ValueError, match="between 1 and 4096"):
            await client.send_message(123, "")
        with pytest.raises(ValueError, match="between 1 and 4096"):
            await client.send_message(123, "x" * 4097)


async def test_send_message_and_copy_message_parse_results(
    fake_bale_server: respx.MockRouter,
) -> None:
    message_response = {
        "ok": True,
        "result": {
            "message_id": 42,
            "date": 1_700_000_000,
            "chat": {"id": 123, "type": "private"},
            "text": "hello",
        },
    }
    sent = fake_bale_server.post("sendMessage").mock(
        return_value=httpx.Response(200, json=message_response)
    )
    copied = fake_bale_server.post("copyMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 43}})
    )
    async with make_client([]) as client:
        message = await client.send_message(123, "hello")
        message_id = await client.copy_message(123, 456, 9)
    assert message.message_id == 42
    assert message.text == "hello"
    assert message_id.message_id == 43
    assert sent.calls[0].request.read() == b'{"chat_id":123,"text":"hello"}'
    assert copied.calls[0].request.read() == b'{"chat_id":123,"from_chat_id":456,"message_id":9}'


async def test_get_file_and_webhook_info(fake_bale_server: respx.MockRouter) -> None:
    fake_bale_server.post("getFile").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "result": {"file_id": "F1", "file_path": "photos/a.jpg"}},
        )
    )
    fake_bale_server.post("setWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )
    fake_bale_server.post("getWebhookInfo").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"url": ""}})
    )
    async with make_client([]) as client:
        file = await client.get_file("F1")
        assert await client.set_webhook("https://example.com/bale-hook")
        assert await client.set_webhook("")
        info = await client.get_webhook_info()
    assert file.file_path == "photos/a.jpg"
    assert info.url == ""


async def test_send_message_serializes_documented_inline_keyboard(
    fake_bale_server: respx.MockRouter,
) -> None:
    route = fake_bale_server.post("sendMessage").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "message_id": 50,
                    "date": 1_700_000_000,
                    "chat": {"id": 123, "type": "private"},
                    "text": "Welcome",
                },
            },
        )
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="About", callback_data="about")],
            [InlineKeyboardButton(text="Website", url="https://example.com")],
        ]
    )
    async with make_client([]) as client:
        await client.send_message(123, "Welcome", reply_markup=keyboard)
    assert route.calls[0].request.read() == (
        b'{"chat_id":123,"text":"Welcome","reply_markup":{"inline_keyboard":'
        b'[[{"text":"About","callback_data":"about"}],[{"text":"Website",'
        b'"url":"https://example.com"}]]}}'
    )


async def test_callback_acknowledgment_is_best_effort(fake_bale_server: respx.MockRouter) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        query_id = json.loads(request.content)["callback_query_id"]
        if query_id == "q1":
            return httpx.Response(200, json={"ok": True, "result": True})
        return httpx.Response(400, json={"ok": False, "description": "unsupported"})

    fake_bale_server.post("answerCallbackQuery").mock(side_effect=respond)
    async with make_client([]) as client:
        assert await client.answer_callback_query("q1", "Done")
        assert not await client.answer_callback_query("q2")


@pytest.mark.parametrize(
    "url",
    ["http://example.com/hook", "example.com/hook", "https://example.com:444/hook"],
)
async def test_set_webhook_rejects_unsupported_urls(url: str) -> None:
    async with make_client([]) as client:
        with pytest.raises(ValueError):
            await client.set_webhook(url)


async def test_server_error_retries_with_backoff_then_succeeds(
    fake_bale_server: respx.MockRouter,
) -> None:
    route = fake_bale_server.post("getMe")
    calls = 0

    def response(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={"ok": True, "result": {"id": 9, "is_bot": True, "first_name": "Bot"}},
        )

    route.mock(side_effect=response)
    sleeps: list[float] = []
    async with make_client(sleeps) as client:
        user = await client.get_me()
    assert user.id == 9
    assert calls == 2
    assert sleeps == [pytest.approx(0.15)]


async def test_server_error_exhaustion_is_safe(fake_bale_server: respx.MockRouter) -> None:
    fake_bale_server.post("getMe").mock(return_value=httpx.Response(503, text="sensitive body"))
    async with make_client([], max_attempts=1) as client:
        with pytest.raises(BaleHTTPError) as captured:
            await client.get_me()
    assert captured.value.status_code == 503
    assert "sensitive body" not in str(captured.value)


async def test_http_200_rate_limit_error_retries(fake_bale_server: respx.MockRouter) -> None:
    route = fake_bale_server.post("getMe")
    calls = 0

    def response(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={"ok": False, "error_code": 429, "parameters": {"retry_after": 1}},
            )
        return httpx.Response(
            200,
            json={"ok": True, "result": {"id": 10, "is_bot": True, "first_name": "Bot"}},
        )

    route.mock(side_effect=response)
    sleeps: list[float] = []
    async with make_client(sleeps) as client:
        user = await client.get_me()
    assert user.id == 10
    assert sleeps == [1.0]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(200, text="not-json"), "valid Bale JSON envelope"),
        (
            httpx.Response(200, json={"ok": True, "result": {"id": 1}}),
            "expected response model",
        ),
        (httpx.Response(200, json={"ok": True}), "had no result"),
    ],
)
async def test_malformed_responses_raise_protocol_error(
    fake_bale_server: respx.MockRouter,
    response: httpx.Response,
    message: str,
) -> None:
    fake_bale_server.post("getMe").mock(return_value=response)
    async with make_client([]) as client:
        with pytest.raises(BaleProtocolError, match=message):
            await client.get_me()


async def test_retry_after_http_date_uses_injected_clock(
    fake_bale_server: respx.MockRouter,
) -> None:
    route = fake_bale_server.post("getMe")
    calls = 0

    def response(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "Wed, 21 Oct 2015 07:28:10 GMT"},
                json={"ok": False},
            )
        return httpx.Response(
            200,
            json={"ok": True, "result": {"id": 11, "is_bot": True, "first_name": "Bot"}},
        )

    route.mock(side_effect=response)
    sleeps: list[float] = []
    client = make_client(
        sleeps,
        clock=lambda: datetime(2015, 10, 21, 7, 28, 0, tzinfo=UTC),
    )
    async with client:
        user = await client.get_me()
    assert user.id == 11
    assert sleeps == [10.0]
