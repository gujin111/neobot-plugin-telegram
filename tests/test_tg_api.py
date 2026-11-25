"""tg_api：Bot API 客户端、错误包装、限流退避、长轮询、文件下载。"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx

from tg_api import (
    TelegramAPI,
    TelegramError,
    TelegramPoller,
    TelegramRetryAfter,
    TelegramTransportError,
    _stringify,
)

TOKEN = "123456:TEST-TOKEN"


def make_api(handler, **kwargs) -> TelegramAPI:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TelegramAPI(TOKEN, client=client, **kwargs)


def run(coro):
    return asyncio.run(coro)


def test_custom_headers_are_applied_to_client():
    async def scenario():
        api = TelegramAPI(TOKEN, headers={"X-Proxy-Key": "pk-test"})
        try:
            assert api.client.headers.get("x-proxy-key") == "pk-test"
        finally:
            await api.close()

    run(scenario())


def test_token_validation():
    import pytest

    with pytest.raises(ValueError):
        TelegramAPI("")


def test_call_returns_result_and_wraps_business_error():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 7, "username": "bot"}})
        return httpx.Response(200, json={"ok": False, "error_code": 400, "description": "chat not found"})

    async def scenario():
        api = make_api(handler)
        try:
            assert (await api.get_me())["username"] == "bot"
            try:
                await api.send_message(1, "hi")
            except TelegramError as exc:
                assert exc.code == 400
                # 与 Koishi SenderError 同款文案
                assert str(exc) == "Telegram API error 400. chat not found"
            else:  # pragma: no cover
                raise AssertionError("应当抛 TelegramError")
        finally:
            await api.close()

    run(scenario())
    assert calls[0].url.path == f"/bot{TOKEN}/getMe"
    assert json.loads(calls[1].content) == {"chat_id": 1, "text": "hi"}


def test_call_retries_on_429_with_retry_after():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 2}})
        return httpx.Response(200, json={"ok": True, "result": True})

    async def scenario():
        api = make_api(handler, max_retries=1)
        with patch("asyncio.sleep", new=AsyncMock()) as sleeper:
            assert await api.delete_webhook() is True
            assert sleeper.await_count == 1
            assert sleeper.await_args[0][0] == 2.0
        await api.close()

    run(scenario())
    assert state["n"] == 2


def test_call_raises_retry_after_when_retries_exhausted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 1}})

    async def scenario():
        api = make_api(handler, max_retries=0)
        try:
            await api.get_me()
        except TelegramRetryAfter as exc:
            assert exc.retry_after == 1.0
        else:  # pragma: no cover
            raise AssertionError("应当抛 TelegramRetryAfter")
        await api.close()

    run(scenario())


def test_call_retries_transport_error_then_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async def scenario():
        api = make_api(handler, max_retries=1)
        with patch("asyncio.sleep", new=AsyncMock()):
            try:
                await api.get_me()
            except TelegramTransportError as exc:
                assert "ConnectError" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("应当抛 TelegramTransportError")
        await api.close()

    run(scenario())


def test_multipart_upload_and_download():
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(f"/file/bot{TOKEN}/"):
            seen["download"] = request
            return httpx.Response(200, content=b"PNG", headers={"content-type": "image/png"})
        seen["send"] = request
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async def scenario():
        api = make_api(handler, endpoint="https://tg.example")
        try:
            result = await api.call(
                "sendPhoto",
                {"chat_id": 5, "caption": "hi", "has_spoiler": True},
                files=[("file0", ("a.png", b"\x89PNG", "image/png"))],
            )
            assert result["message_id"] == 1
            data, mime = await api.download_file("photos/a.png")
            assert (data, mime) == (b"PNG", "image/png")
        finally:
            await api.close()

    run(scenario())
    body = seen["send"].content
    assert b"multipart/form-data" in seen["send"].headers["content-type"].encode()
    assert b'name="has_spoiler"' in body and b"true" in body
    assert b'filename="a.png"' in body
    assert seen["download"].url.host == "tg.example"
    assert seen["download"].url.path == f"/file/bot{TOKEN}/photos/a.png"


def test_stringify_normalizes_multipart_fields():
    assert _stringify({"a": True, "b": False, "c": {"x": 1}, "d": [1, 2], "e": 3, "f": None}) == {
        "a": "true",
        "b": "false",
        "c": '{"x": 1}',
        "d": "[1, 2]",
        "e": "3",
        "f": "None",
    }


class _StubAPI:
    """长轮询用的假 API：第一轮给两条更新，之后置空并触发停止。"""

    def __init__(self, *, webhook: str = "", updates: list[dict] | None = None) -> None:
        self.webhook = webhook
        self.updates = list(updates or [])
        self.deleted: list[bool] = []
        self.offsets: list[int | None] = []
        self.calls = 0
        self.self_id = "123456"

    async def get_webhook_info(self):
        return {"url": self.webhook}

    async def delete_webhook(self, *, drop_pending_updates: bool = False):
        self.deleted.append(drop_pending_updates)
        return True

    async def get_updates(self, *, offset=None, timeout=0, limit=100, allowed_updates=None):
        self.offsets.append(offset)
        self.calls += 1
        if offset is None:
            return [{"update_id": 40}]
        payload, self.updates = self.updates, []
        return payload


def test_poller_removes_webhook_and_tracks_offset():
    stub = _StubAPI(webhook="https://old.example/hook", updates=[{"update_id": 41}, {"update_id": 42}])
    received: list[list[dict]] = []
    states: list[str] = []

    async def scenario():
        poller = TelegramPoller(
            stub,  # type: ignore[arg-type]
            timeout=1,
            retry_times=1,
            retry_interval=0.5,
            allowed_updates=("message", "chat_member"),
            on_updates=lambda updates: (received.append(updates), poller._stop.set()),
            on_state=lambda state, message="": states.append(state),
        )
        await poller.run()
        assert stub.deleted == [False]
        assert received == [[{"update_id": 41}, {"update_id": 42}]]
        assert poller.stats.offset == 42
        assert poller.stats.received == 2

    asyncio.run(scenario())
    assert "online" in states
    # 探活必须显式带上 allowed_updates，否则会沿用「除 chat_member 外」的默认设置
    assert stub.offsets[0] is None


def test_poller_gives_up_on_initial_failure():
    class _Broken(_StubAPI):
        async def get_updates(self, *, offset=None, timeout=0, limit=100, allowed_updates=None):
            raise TelegramError("getUpdates", 401, "Unauthorized")

    async def scenario():
        poller = TelegramPoller(_Broken(), retry_times=0, retry_interval=0.01)
        with patch("asyncio.sleep", new=AsyncMock()):
            await poller.run()
        assert poller.stats.online is False
        assert "401" in poller.stats.last_error

    asyncio.run(scenario())
