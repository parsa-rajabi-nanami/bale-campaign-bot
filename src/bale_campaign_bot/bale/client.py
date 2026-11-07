"""Small async HTTP client for the documented subset of the Bale Bot API."""

from __future__ import annotations

import asyncio
import email.utils
import math
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import TypeAdapter, ValidationError

from bale_campaign_bot.bale.errors import (
    BaleAPIError,
    BaleError,
    BaleHTTPError,
    BaleNetworkError,
    BaleProtocolError,
)
from bale_campaign_bot.bale.models import (
    ApiEnvelope,
    File,
    InlineKeyboardMarkup,
    Message,
    MessageId,
    ReplyKeyboardMarkup,
    Update,
    User,
    WebhookInfo,
)

ResultT = TypeVar("ResultT")
Sleep = Callable[[float], Awaitable[None]]
RandomValue = Callable[[], float]
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded retry count and exponential backoff limits."""

    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 10.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if not math.isfinite(self.base_delay) or not math.isfinite(self.max_delay):
            raise ValueError("retry delays must be finite")
        if self.base_delay < 0 or self.max_delay < 0:
            raise ValueError("retry delays cannot be negative")
        if self.max_delay < self.base_delay:
            raise ValueError("max_delay must be greater than or equal to base_delay")

    def delay(self, failed_attempt: int, jitter: float, retry_after: float | None = None) -> float:
        """Return a backoff delay; an explicit Bale/HTTP hint takes precedence."""
        if retry_after is not None:
            return retry_after
        exponent = min(max(failed_attempt, 0), 1023)
        exponential = min(self.max_delay, self.base_delay * math.pow(2.0, exponent))
        bounded_jitter = min(max(jitter, 0.0), 1.0) if math.isfinite(jitter) else 0.5
        return exponential * (0.5 + bounded_jitter * 0.5)


class BaleClient:
    """Async client with safe errors and bounded retries for temporary failures."""

    _RATE_LIMIT_TEXT = ("too many requests", "rate limit", "ratelimit")

    def __init__(
        self,
        token: str,
        *,
        timeout: float = 20.0,
        retry_policy: RetryPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy: str | None = None,
        sleep: Sleep = asyncio.sleep,
        jitter: RandomValue = random.random,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        if not token.strip():
            raise ValueError("Bale bot token cannot be empty")
        self._token = token
        self._retry_policy = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._jitter = jitter
        self._clock = clock
        self._http = httpx.AsyncClient(
            base_url=f"https://tapi.bale.ai/bot{token}/",
            timeout=httpx.Timeout(timeout),
            transport=transport,
            proxy=proxy,
        )

    async def __aenter__(self) -> BaleClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_me(self) -> User:
        return await self._request("getMe", User)

    async def get_updates(
        self,
        *,
        offset: int | None = None,
        limit: int = 100,
        timeout: int | None = None,
    ) -> list[Update]:
        if not 1 <= limit <= 100:
            raise ValueError("getUpdates limit must be between 1 and 100")
        if timeout is not None and timeout < 0:
            raise ValueError("getUpdates timeout cannot be negative")
        params: dict[str, object] = {"limit": limit}
        if offset is not None:
            params["offset"] = offset
        if timeout is not None:
            params["timeout"] = timeout
        request_timeout = timeout + 5.0 if timeout is not None else None
        return await self._request(
            "getUpdates",
            list[Update],
            params,
            request_timeout=request_timeout,
        )

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None = None,
    ) -> Message:
        if not 1 <= len(text) <= 4096:
            raise ValueError("sendMessage text must contain between 1 and 4096 characters")
        params: dict[str, object] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup.model_dump(mode="json", exclude_none=True)
        return await self._request("sendMessage", Message, params)

    async def copy_message(self, chat_id: int, from_chat_id: int, message_id: int) -> MessageId:
        return await self._request(
            "copyMessage",
            MessageId,
            {"chat_id": chat_id, "from_chat_id": from_chat_id, "message_id": message_id},
        )

    async def edit_message_text(self, chat_id: int, message_id: int, text: str) -> Message | bool:
        """Edit plain progress text; deliberately offers no keyboard argument."""
        if not 1 <= len(text) <= 4096:
            raise ValueError("editMessageText text must contain between 1 and 4096 characters")
        return await self._request(
            "editMessageText",
            Message | bool,
            {"chat_id": chat_id, "message_id": message_id, "text": text},
        )

    async def get_file(self, file_id: str) -> File:
        return await self._request("getFile", File, {"file_id": file_id})

    async def set_webhook(self, url: str) -> bool:
        if url:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname:
                raise ValueError("Webhook URL must be an absolute HTTPS URL")
            if parsed.port not in (None, 443, 88):
                raise ValueError("Bale webhooks support only ports 443 and 88")
        return await self._request("setWebhook", bool, {"url": url})

    async def get_webhook_info(self) -> WebhookInfo:
        return await self._request("getWebhookInfo", WebhookInfo)

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> bool:
        """Best-effort callback acknowledgment; the method's support can vary."""
        params: dict[str, object] = {"callback_query_id": callback_query_id}
        if text is not None:
            params["text"] = text
        try:
            return await self._request("answerCallbackQuery", bool, params)
        except BaleError:
            return False

    async def _request(
        self,
        method: str,
        result_type: Any,
        params: Mapping[str, object] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> ResultT:
        adapter: TypeAdapter[ResultT] = TypeAdapter(result_type)
        for attempt in range(self._retry_policy.max_attempts):
            try:
                response = await self._http.post(
                    method,
                    json=dict(params or {}),
                    timeout=request_timeout,
                )
            except httpx.RequestError:
                if attempt + 1 >= self._retry_policy.max_attempts:
                    raise BaleNetworkError(method, attempt + 1) from None
                await self._pause(attempt)
                continue

            if response.status_code == 429:
                envelope = self._try_envelope(method, response)
                hint = self._response_retry_hint(response, envelope)
                if attempt + 1 < self._retry_policy.max_attempts:
                    await self._pause(attempt, hint)
                    continue
                if envelope is not None and not envelope.ok:
                    raise self._api_error(method, envelope, retry_after=hint, rate_limited=True)
                raise BaleHTTPError(method, response.status_code, retry_after=hint)

            if response.status_code >= 500:
                if attempt + 1 < self._retry_policy.max_attempts:
                    await self._pause(attempt)
                    continue
                raise BaleHTTPError(method, response.status_code)

            envelope = self._try_envelope(method, response)
            if envelope is None:
                raise BaleProtocolError(method, "response was not a JSON object")
            if not envelope.ok:
                error = self._api_error(method, envelope)
                if error.rate_limited and attempt + 1 < self._retry_policy.max_attempts:
                    await self._pause(attempt, error.retry_after)
                    continue
                raise error
            if not response.is_success:
                raise BaleHTTPError(method, response.status_code)
            if envelope.result is None:
                raise BaleProtocolError(method, "successful response had no result")
            try:
                return adapter.validate_python(envelope.result)
            except ValidationError:
                raise BaleProtocolError(
                    method,
                    "result did not match the expected response model",
                ) from None

        raise AssertionError("retry loop ended without a response")

    def _try_envelope(self, method: str, response: httpx.Response) -> ApiEnvelope[Any] | None:
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                return None
            return ApiEnvelope[Any].model_validate(payload)
        except (ValueError, ValidationError):
            if response.status_code == 429 or response.status_code >= 500:
                return None
            raise BaleProtocolError(method, "response was not a valid Bale JSON envelope") from None

    def _api_error(
        self,
        method: str,
        envelope: ApiEnvelope[Any],
        *,
        retry_after: float | None = None,
        rate_limited: bool | None = None,
    ) -> BaleAPIError:
        description = envelope.description
        if description is not None:
            description = description.replace(self._token, "[REDACTED]")
        limited = rate_limited if rate_limited is not None else self._looks_rate_limited(envelope)
        hint = retry_after if retry_after is not None else self._parameter_retry_hint(envelope)
        parameters = (
            envelope.parameters.model_dump(exclude_none=True) if envelope.parameters else None
        )
        if parameters:
            parameters = self._redact(parameters)
        return BaleAPIError(
            method,
            error_code=envelope.error_code,
            description=description,
            parameters=parameters,
            retry_after=hint,
            rate_limited=limited,
        )

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return value.replace(self._token, "[REDACTED]")
        if isinstance(value, dict):
            return {self._redact(key): self._redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    def _looks_rate_limited(self, envelope: ApiEnvelope[Any]) -> bool:
        if envelope.error_code == 429:
            return True
        description = (envelope.description or "").casefold()
        return any(fragment in description for fragment in self._RATE_LIMIT_TEXT)

    def _response_retry_hint(
        self,
        response: httpx.Response,
        envelope: ApiEnvelope[Any] | None,
    ) -> float | None:
        parameter_hint = self._parameter_retry_hint(envelope) if envelope is not None else None
        if parameter_hint is not None:
            return parameter_hint
        header = response.headers.get("Retry-After")
        if header is None:
            return None
        try:
            seconds = float(header)
        except ValueError:
            try:
                retry_at = email.utils.parsedate_to_datetime(header)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                seconds = (retry_at - self._clock()).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return None
        return max(0.0, seconds) if math.isfinite(seconds) else None

    @staticmethod
    def _parameter_retry_hint(envelope: ApiEnvelope[Any]) -> float | None:
        if envelope.parameters is None:
            return None
        value = envelope.parameters.retry_after
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return None
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, seconds) if math.isfinite(seconds) else None

    async def _pause(self, failed_attempt: int, retry_after: float | None = None) -> None:
        delay = self._retry_policy.delay(failed_attempt, self._jitter(), retry_after)
        await self._sleep(delay)
