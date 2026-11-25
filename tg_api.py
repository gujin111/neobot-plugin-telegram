"""Telegram Bot API 客户端与长轮询（零宿主依赖，只依赖 httpx）。

只保留收发消息必需的几个方法：调用、探活、长轮询、去 webhook。
代理直接交给 httpx（`http://` 或 `https://` 代理；SOCKS 请用代理软件的 HTTP 端口）。
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

DEFAULT_ENDPOINT = "https://api.telegram.org"
DEFAULT_ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "callback_query",
    "inline_query",
    "chosen_inline_result",
    "my_chat_member",
    "chat_member",
    "chat_join_request",
]
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class TelegramError(Exception):
    """Bot API 返回 ok=false。"""

    def __init__(self, method: str, code: int | None, description: str, payload: Any = None) -> None:
        super().__init__(f"{method} 失败（{code}）：{description}")
        self.method = method
        self.code = code
        self.description = description
        self.payload = payload

    @property
    def is_auth_error(self) -> bool:
        return self.code == 401


class TelegramTransportError(Exception):
    """网络层失败（连不上、超时、TLS 等）。"""

    def __init__(self, method: str, reason: str) -> None:
        super().__init__(f"{method} 连接失败：{reason}")
        self.method = method
        self.reason = reason


class TelegramRetryAfter(Exception):
    """429：Telegram 明确要求等 retry_after 秒。"""

    def __init__(self, method: str, retry_after: float) -> None:
        super().__init__(f"{method} 被限流，需等待 {retry_after:.0f}s")
        self.method = method
        self.retry_after = retry_after


def _make_client(proxy: str | None, timeout: float, headers: dict[str, str] | None) -> httpx.AsyncClient:
    kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": True}
    if headers:
        kwargs["headers"] = dict(headers)
    if proxy:
        kwargs["proxy"] = proxy
    try:
        return httpx.AsyncClient(**kwargs)
    except TypeError:  # httpx < 0.26 用 proxies=
        kwargs.pop("proxy", None)
        if proxy:
            kwargs["proxies"] = proxy
        return httpx.AsyncClient(**kwargs)


def _decode_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        return response.text


def _retry_after(payload: Any) -> float:
    if isinstance(payload, dict):
        parameters = payload.get("parameters")
        if isinstance(parameters, dict) and parameters.get("retry_after") is not None:
            with contextlib.suppress(TypeError, ValueError):
                return max(0.0, float(parameters["retry_after"]))
        if payload.get("retry_after") is not None:
            with contextlib.suppress(TypeError, ValueError):
                return max(0.0, float(payload["retry_after"]))
    return 0.0


def _stringify(params: dict[str, Any] | None) -> dict[str, str]:
    """把参数转成字符串（布尔转小写，None 丢弃，容器转 JSON）。"""
    out: dict[str, str] = {}
    for key, value in (params or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, (dict, list)):
            import json

            out[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            out[key] = str(value)
    return out


def _backoff(attempt: int) -> float:
    return min(30.0, 0.8 * (2 ** max(0, attempt - 1))) + random.uniform(0, 0.4)


class TelegramAPI:
    """一个 token 一个实例；所有网络调用都经过 `call()`。"""

    def __init__(
        self,
        token: str,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        proxy: str | None = None,
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        logger: Any | None = None,
        max_retries: int = 3,
    ) -> None:
        if not token or ":" not in token:
            raise ValueError("Telegram token 形如 123456:ABC-DEF，不能为空且必须含 ':'")
        self.token = token
        self.self_id = token.split(":", 1)[0]
        self.endpoint = (endpoint or DEFAULT_ENDPOINT).rstrip("/")
        self.proxy = proxy or ""
        self.max_retries = max(0, int(max_retries))
        self._timeout = timeout
        self._headers = dict(headers or {})
        self._client = client
        self._owns_client = client is None
        self._logger = logger

    # ── 基础设施 ──

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = _make_client(self.proxy, self._timeout, self._headers)
        return self._client

    def _log(self, level: str, message: str, **fields: Any) -> None:
        if self._logger is None:
            return
        with contextlib.suppress(Exception):
            getattr(self._logger, level)(message, **fields)

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            with contextlib.suppress(Exception):
                await self._client.aclose()
        self._client = None

    def _url(self, method: str) -> str:
        return f"{self.endpoint}/bot{self.token}/{method}"

    # ── 调用 ──

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """调用一个 Bot API 方法，返回 result；429 按 retry_after 等待，网络错误退避重试。"""
        payload = _stringify(params)
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self.client.post(self._url(method), data=payload)
            except Exception as exc:  # noqa: BLE001 - httpx 的异常族太多
                if attempt > self.max_retries:
                    raise TelegramTransportError(method, f"{type(exc).__name__}: {exc}") from exc
                delay = _backoff(attempt)
                self._log("debug", f"{method} 网络异常，{delay:.1f}s 后重试", error=str(exc)[:120])
                await asyncio.sleep(delay)
                continue

            body = _decode_body(response)
            if isinstance(body, dict) and body.get("ok"):
                return body.get("result")

            description = ""
            code = response.status_code
            if isinstance(body, dict):
                description = str(body.get("description") or "")
                code = int(body.get("error_code") or code)
            else:
                description = str(body)[:200]

            if code == 429 or response.status_code == 429:
                wait = _retry_after(body) or _backoff(attempt)
                if attempt > self.max_retries:
                    raise TelegramRetryAfter(method, wait)
                self._log("debug", f"{method} 被限流，等 {wait:.0f}s")
                await asyncio.sleep(wait)
                continue

            if response.status_code in RETRYABLE_STATUS and attempt <= self.max_retries:
                await asyncio.sleep(_backoff(attempt))
                continue

            raise TelegramError(method, code, description or f"HTTP {response.status_code}", body)

    # ── 常用方法 ──

    async def get_me(self) -> dict[str, Any]:
        return await self.call("getMe") or {}

    async def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: int = 0,
        limit: int = 100,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        result = await self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "limit": limit,
                "allowed_updates": allowed_updates or DEFAULT_ALLOWED_UPDATES,
            },
        )
        return list(result or [])

    async def get_webhook_info(self) -> dict[str, Any]:
        return await self.call("getWebhookInfo") or {}

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> bool:
        return bool(await self.call("deleteWebhook", {"drop_pending_updates": drop_pending_updates}))

    async def send_message(self, chat_id: int | str, text: str, **extra: Any) -> dict[str, Any]:
        return await self.call("sendMessage", {"chat_id": chat_id, "text": text, **extra}) or {}

    async def answer_callback_query(self, callback_query_id: str, **extra: Any) -> bool:
        return bool(await self.call("answerCallbackQuery", {"callback_query_id": callback_query_id, **extra}))

    async def probe(self, *, timeout: float = 6.0) -> tuple[bool, str]:
        """真实探活：拿 getMe 当连通性判据。"""
        started = asyncio.get_event_loop().time()
        try:
            me = await asyncio.wait_for(self.get_me(), timeout=timeout)
        except TelegramError as exc:
            hint = "（token 无效）" if exc.is_auth_error else ""
            return False, f"{exc}{hint}"
        except Exception as exc:  # noqa: BLE001
            return False, f"失败：{type(exc).__name__}: {exc}"
        cost = (asyncio.get_event_loop().time() - started) * 1000
        name = me.get("username") or me.get("first_name") or me.get("id")
        return True, f"@{name}（{cost:.0f} ms）"


@dataclass
class PollingStats:
    offset: int = 0
    received: int = 0
    failures: int = 0
    last_error: str = ""
    webhook_removed: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class TelegramPoller:
    """getUpdates 长轮询：启动时摘 webhook，失败退避重试，可被 stop() 取消。"""

    def __init__(
        self,
        api: TelegramAPI,
        *,
        timeout: int = 25,
        retry_times: int = 6,
        retry_interval: float = 5.0,
        allowed_updates: list[str] | None = None,
        on_updates: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
        on_state: Callable[[str, str], None] | None = None,
        logger: Any | None = None,
    ) -> None:
        self.api = api
        self.timeout = max(1, int(timeout))
        self.retry_times = max(0, int(retry_times))
        self.retry_interval = max(0.5, float(retry_interval))
        self.allowed_updates = allowed_updates or DEFAULT_ALLOWED_UPDATES
        self.on_updates = on_updates
        self.on_state = on_state
        self.stats = PollingStats()
        self._logger = logger
        self._task: asyncio.Task[None] | None = None
        self._running = False

    # ── 生命周期 ──

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._running = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    @property
    def running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    def _log(self, level: str, message: str, **fields: Any) -> None:
        if self._logger is None:
            return
        with contextlib.suppress(Exception):
            getattr(self._logger, level)(message, **fields)

    # ── 主循环 ──

    def _state(self, state: str, message: str = "") -> None:
        if self.on_state is not None:
            with contextlib.suppress(Exception):
                self.on_state(state, message)

    async def run(self) -> None:
        await self._remove_webhook()
        failures = 0
        while self._running:
            try:
                updates = await self.api.get_updates(
                    offset=self.stats.offset, timeout=self.timeout, allowed_updates=self.allowed_updates
                )
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except TelegramRetryAfter as exc:
                failures += 1
                self.stats.failures = failures
                self.stats.last_error = str(exc)
                self._state("retry", str(exc))
                await asyncio.sleep(exc.retry_after)
                continue
            except Exception as exc:  # noqa: BLE001
                failures += 1
                self.stats.failures = failures
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                self._state("retry", self.stats.last_error)
                self._log("warning", "长轮询失败", error=self.stats.last_error, failures=failures)
                if self.retry_times and failures > self.retry_times:
                    self._state("error", self.stats.last_error)
                    self._running = False
                    return
                await asyncio.sleep(self.retry_interval)
                continue

            failures = 0
            self.stats.failures = 0
            self.stats.last_error = ""
            if updates:
                self.stats.received += len(updates)
                self.stats.offset = max(
                    self.stats.offset, max(int(item.get("update_id") or 0) for item in updates) + 1
                )
                if self.on_updates is not None:
                    try:
                        await self.on_updates(updates)
                    except Exception as exc:  # noqa: BLE001 - 处理失败不该中断轮询
                        self._log("warning", "处理更新失败", error=f"{type(exc).__name__}: {exc}")
            self._state("online")

    async def _remove_webhook(self) -> None:
        try:
            info = await self.api.get_webhook_info()
        except Exception as exc:  # noqa: BLE001
            self._log("debug", "getWebhookInfo 失败", error=str(exc)[:120])
            return
        if not (info or {}).get("url"):
            return
        try:
            await self.api.delete_webhook(drop_pending_updates=False)
            self.stats.webhook_removed = True
            self._log("info", "检测到 webhook，已删除以启用长轮询")
        except Exception as exc:  # noqa: BLE001
            self._log("warning", "删除 webhook 失败", error=str(exc)[:120])
