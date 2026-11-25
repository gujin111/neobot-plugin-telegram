"""Bot API 客户端与长轮询：重试、429、请求头、offset 推进。"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from unittest.mock import AsyncMock, patch

from tg_api import (
    DEFAULT_ALLOWED_UPDATES,
    TelegramAPI,
    TelegramError,
    TelegramPoller,
    TelegramRetryAfter,
    TelegramTransportError,
    _stringify,
)

TOKEN = "123456:TEST"


def make_api(handler, **kwargs) -> TelegramAPI:
    return TelegramAPI(TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kwargs)


def run(coro):
    return asyncio.run(coro)


def test_token_validation():
    with pytest.raises(ValueError):
        TelegramAPI("bad-token")


def test_headers_are_applied_to_client():
    api = TelegramAPI(TOKEN, headers={"X-Proxy-Key": "pk-test"})
    assert api.client.headers.get("x-proxy-key") == "pk-test"
    run(api.close())


def test_call_returns_result_and_raises_on_business_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 1, "username": "bot"}})
        return httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})

    async def scenario():
        api = make_api(handler)
        try:
            me = await api.get_me()
            assert me["username"] == "bot"
            with pytest.raises(TelegramError) as info:
                await api.call("sendMessage", {"chat_id": 1, "text": "x"})
            assert info.value.is_auth_error
            assert "Unauthorized" in info.value.description
        finally:
            await api.close()

    run(scenario())


def test_call_retries_on_429_then_succeeds():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 3}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async def scenario():
        api = make_api(handler)
        try:
            with patch("asyncio.sleep", new=AsyncMock()) as sleeper:
                await api.call("sendMessage", {"chat_id": 1, "text": "hi"})
            assert calls["count"] == 2
            assert sleeper.await_args.args[0] == 3
        finally:
            await api.close()

    run(scenario())


def test_call_raises_retry_after_when_retries_exhausted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 2}})

    async def scenario():
        api = make_api(handler, max_retries=0)
        try:
            with patch("asyncio.sleep", new=AsyncMock()):
                with pytest.raises(TelegramRetryAfter):
                    await api.call("getUpdates")
        finally:
            await api.close()

    run(scenario())


def test_transport_error_is_retried_then_wrapped():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async def scenario():
        api = make_api(handler, max_retries=1)
        try:
            with patch("asyncio.sleep", new=AsyncMock()):
                with pytest.raises(TelegramTransportError) as info:
                    await api.call("getMe")
            assert "boom" in str(info.value)
        finally:
            await api.close()

    run(scenario())


def test_stringify_normalizes_values():
    assert _stringify({"a": True, "b": False, "c": None, "d": 1, "e": {"x": 1}, "f": ["y"]}) == {
        "a": "true",
        "b": "false",
        "d": "1",
        "e": '{"x":1}',
        "f": '["y"]',
    }


class _StubAPI:
    """冒充 TelegramAPI：只为轮询器提供三个方法。"""

    def __init__(self, *, webhook: str = "", updates=None, fail_times: int = 0) -> None:
        self.webhook = webhook
        self.updates = list(updates or [])
        self.fail_times = fail_times
        self.deleted = False
        self.offsets: list[int | None] = []

    async def get_webhook_info(self):
        return {"url": self.webhook}

    async def delete_webhook(self, *, drop_pending_updates: bool = False):
        self.deleted = True
        self.webhook = ""
        return True

    async def get_updates(self, *, offset=None, timeout=0, limit=100, allowed_updates=None):
        self.offsets.append(offset)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise TelegramTransportError("getUpdates", "temporary")
        if self.updates:
            return self.updates.pop(0)
        raise asyncio.CancelledError


def test_poller_removes_webhook_and_tracks_offset():
    api = _StubAPI(webhook="https://example/hook", updates=[[{"update_id": 5}, {"update_id": 6}]])
    received: list[list[dict]] = []
    states: list[str] = []

    async def on_updates(items):
        received.append(items)

    poller = TelegramPoller(
        api, timeout=1, retry_interval=0.01, on_updates=on_updates, on_state=lambda state, message="": states.append(state)
    )

    async def scenario():
        await poller.start()
        for _ in range(200):
            if received:
                break
            await asyncio.sleep(0.01)
        await poller.stop()

    run(scenario())
    assert api.deleted is True
    assert poller.stats.webhook_removed is True
    assert received and received[0][0]["update_id"] == 5
    assert poller.stats.offset == 7
    assert "online" in states
    assert poller.running is False


def test_poller_gives_up_after_too_many_failures():
    api = _StubAPI(fail_times=99)
    states: list[str] = []
    poller = TelegramPoller(
        api, timeout=1, retry_times=1, retry_interval=0.01, on_state=lambda state, message="": states.append(state)
    )

    async def scenario():
        await poller.start()
        for _ in range(200):
            if not poller.running:
                break
            await asyncio.sleep(0.01)
        await poller.stop()

    run(scenario())
    assert poller.running is False
    assert "error" in states
    assert poller.stats.failures >= 1


def test_default_allowed_updates_include_chat_member():
    assert "chat_member" in DEFAULT_ALLOWED_UPDATES
    assert "message" in DEFAULT_ALLOWED_UPDATES
