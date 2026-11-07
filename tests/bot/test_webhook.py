from __future__ import annotations

import json
from typing import Any

from bale_campaign_bot.bale.models import Update
from bale_campaign_bot.bot.webhook import BaleWebhookApp


async def _request(
    app: BaleWebhookApp,
    *,
    path: str = "/" + "x" * 40,
    body: bytes = b'{"update_id": 1, "message": {"message_id": 2, "date": 1, '
    b'"chat": {"id": 3, "type": "private"}, "text": "/start"}}',
) -> tuple[int, bytes]:
    incoming: list[dict[str, Any]] = [{"type": "http.request", "body": body, "more_body": False}]
    outgoing: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return incoming.pop(0)

    async def send(message: dict[str, Any]) -> None:
        outgoing.append(message)

    await app(
        {"type": "http", "method": "POST", "path": path, "headers": []},
        receive,
        send,
    )
    return outgoing[0]["status"], outgoing[1]["body"]


async def test_webhook_validates_secret_path_and_dispatches() -> None:
    class Dispatcher:
        updates: list[int]

        def __init__(self) -> None:
            self.updates = []

        async def dispatch_update(self, update: Update) -> bool:
            self.updates.append(update.update_id)
            return True

    dispatcher = Dispatcher()
    app = BaleWebhookApp(dispatcher, route_path="/bale/" + "x" * 40)
    status, body = await _request(app, path="/bale/" + "x" * 40)
    assert (status, body) == (200, b"ok")
    assert dispatcher.updates == [1]


async def test_webhook_rejects_bad_path_payload_and_oversized_body() -> None:
    class Dispatcher:
        async def dispatch_update(self, _update: Update) -> bool:
            raise AssertionError("invalid requests must not dispatch")

    app = BaleWebhookApp(Dispatcher(), route_path="/" + "x" * 40, max_body_bytes=64)
    status, _ = await _request(app, path="/wrong", body=b"{}")
    assert status == 404
    status, _ = await _request(app, path="/" + "x" * 40, body=b"invalid")
    assert status == 400
    status, _ = await _request(app, path="/" + "x" * 40, body=json.dumps({"x": "y" * 100}).encode())
    assert status == 413
