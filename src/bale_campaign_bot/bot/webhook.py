"""Small ASGI endpoint for Bale webhook updates without an extra web framework."""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import structlog
from pydantic import ValidationError

from bale_campaign_bot.bale.models import Update

ASGIScope = dict[str, Any]
ASGIMessage = dict[str, Any]
ASGIReceive = Callable[[], Awaitable[ASGIMessage]]
ASGISend = Callable[[ASGIMessage], Awaitable[None]]
logger = structlog.get_logger(__name__)


class UpdateHandler(Protocol):
    async def dispatch_update(self, update: Update) -> bool: ...


class BaleWebhookApp:
    def __init__(
        self,
        dispatcher: UpdateHandler,
        *,
        route_path: str,
        max_body_bytes: int = 1_000_000,
    ) -> None:
        if not route_path.startswith("/") or max_body_bytes < 1:
            raise ValueError("absolute webhook route path and positive body limit are required")
        self._dispatcher = dispatcher
        self._path = route_path
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope_type != "http":
            return
        path = scope.get("path", "")
        if scope.get("method") != "POST":
            await self._respond(send, 405, b"method not allowed")
            return
        if not isinstance(path, str) or not hmac.compare_digest(path, self._path):
            await self._respond(send, 404, b"not found")
            return
        body = bytearray()
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return
            if message.get("type") != "http.request":
                continue
            chunk = message.get("body", b"")
            if not isinstance(chunk, bytes) or len(body) + len(chunk) > self._max_body_bytes:
                await self._respond(send, 413, b"payload too large")
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        try:
            update = Update.model_validate_json(body)
        except ValidationError:
            await self._respond(send, 400, b"invalid update")
            return
        try:
            await self._dispatcher.dispatch_update(update)
        except Exception as exc:
            logger.error(
                "webhook_update_failed",
                update_id=update.update_id,
                error_type=type(exc).__name__,
            )
            await self._respond(send, 500, b"update processing failed")
            return
        await self._respond(send, 200, b"ok")

    @staticmethod
    async def _respond(send: ASGISend, status: int, body: bytes) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _lifespan(receive: ASGIReceive, send: ASGISend) -> None:
        while True:
            message = await receive()
            if message.get("type") == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message.get("type") == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
