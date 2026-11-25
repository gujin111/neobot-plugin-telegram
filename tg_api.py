"""Telegram Bot API 客户端（纯 stdlib + httpx，不依赖 NeoBot）。

移植自 Koishi 适配器 ``@satorijs/adapter-telegram`` 的实现语义：

* ``Internal.define`` 的动态方法 → 这里收敛成 ``TelegramAPI.call()``；
* ``SenderError`` / ``Telegram API error <code>. <description>`` 的错误包装；
* 长轮询的 offset 推进、``getWebhookInfo`` 自动摘 webhook、重试与退避；
* 文件下载走 ``/file/bot<token>/<path>``，支持自建反代与代理。

与 Koishi 版本的两处刻意改进（README 有说明）：

1. ``allowed_updates`` 不再用空列表探活后沿用，默认显式带上 ``chat_member``，
   否则 Telegram 永远不投递该更新（Koishi 4.5.11 的已知副作用）；
2. 429 会读 ``parameters.retry_after`` 精确退避，而不是笼统按重试间隔。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_ENDPOINT = "https://api.telegram.org"

#: Telegram 在「除 chat_member 外全部」之上，我们默认补上的更新类型
DEFAULT_ALLOWED_UPDATES: tuple[str, ...] = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
    "message_reaction",
    "message_reaction_count",
    "inline_query",
    "chosen_inline_result",
    "callback_query",
    "shipping_query",
    "pre_checkout_query",
    "purchased_paid_media",
    "poll",
    "poll_answer",
    "my_chat_member",
    "chat_member",
    "chat_join_request",
    "chat_boost",
    "removed_chat_boost",
)


class TelegramError(Exception):
    """Telegram 返回的业务错误（HTTP 200 + ok=false，或非 2xx）。"""

    def __init__(self, method: str, code: int | None, description: str, payload: Any = None) -> None:
        self.method = method
        self.code = code
        self.description = description or "unknown error"
        self.payload = payload
        super().__init__(f"Telegram API error {code}. {self.description}")


class TelegramTransportError(Exception):
    """网络层错误（超时、连接重置、代理不可用）。"""

    def __init__(self, method: str, reason: str) -> None:
        self.method = method
        self.reason = reason
        super().__init__(f"Telegram transport error on {method}: {reason}")


class TelegramRetryAfter(Exception):
    """429：``retry_after`` 秒后可重试。"""

    def __init__(self, method: str, retry_after: float) -> None:
        self.method = method
        self.retry_after = retry_after
        super().__init__(f"Telegram rate limited on {method}, retry after {retry_after}s")


def _make_client(proxy: str | None, timeout: float, *, method: str = "client") -> httpx.AsyncClient:
    """建 httpx 客户端：兼容 httpx 新旧代理参数，并把 socks 缺依赖翻译成人话。"""
    kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": True}
    if proxy:
        kwargs["proxy"] = proxy
    try:
        return httpx.AsyncClient(**kwargs)
    except TypeError:
        # httpx < 0.26 只认 proxies=
        if proxy:
            kwargs.pop("proxy", None)
            kwargs["proxies"] = proxy
        return httpx.AsyncClient(**kwargs)
    except ImportError as exc:
        raise TelegramTransportError(
            method,
            f"代理不可用：{exc}。socks5:// 需要 socksio，请安装 httpx[socks] 后重载插件。",
        ) from exc
    except ValueError as exc:
        raise TelegramTransportError(
            method, f"代理地址不合法（{proxy!r}）：代理要带协议头，如 http://127.0.0.1:7890。{exc}"
        ) from exc


class TelegramAPI:
    """一个机器人令牌对应一个实例。"""

    def __init__(
        self,
        token: str,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        proxy: str | None = None,
        timeout: float = 30.0,
        file_timeout: float = 120.0,
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
        self._logger = logger
        self._timeout = timeout
        self._file_timeout = file_timeout
        self._client = client
        self._owns_client = client is None

    # ── 客户端 ──

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = _make_client(self.proxy, self._timeout, method="client")
        return self._client

    async def probe(self, *, timeout: float = 6.0) -> tuple[bool, str]:
        """快速连通性探测（getMe + 短超时），供 /tg诊断 使用。"""
        started = time.monotonic()
        try:
            me = await self.call("getMe", timeout=timeout)
        except (TelegramError, TelegramTransportError, TelegramRetryAfter) as exc:
            return False, f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        elapsed = (time.monotonic() - started) * 1000
        name = (me or {}).get("username") or (me or {}).get("id")
        return True, f"@{name}（{elapsed:.0f} ms）"

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    def _log(self, level: str, message: str, **fields: Any) -> None:
        logger = self._logger
        if logger is None:
            return
        try:
            getattr(logger, level)(message, **fields)
        except Exception:  # pragma: no cover - 日志失败不影响主流程
            pass

    # ── 调用 ──

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        files: Sequence[tuple[str, tuple[str, bytes, str]]] | None = None,
        retries: int | None = None,
        timeout: float | None = None,
    ) -> Any:
        """调用 Bot API 方法。

        ``files`` 非空时走 multipart（值形如 ``("a.png", b"...", "image/png")``），
        否则发 JSON。业务错误抛 :class:`TelegramError`，网络错误抛
        :class:`TelegramTransportError`，限流抛 :class:`TelegramRetryAfter`。
        """
        url = f"{self.endpoint}/bot{self.token}/{method}"
        payload = {k: v for k, v in (params or {}).items() if v is not None}
        attempts = self.max_retries if retries is None else max(0, int(retries))
        last_error: Exception | None = None

        for attempt in range(attempts + 1):
            try:
                if files:
                    response = await self.client.post(
                        url, data=_stringify(payload), files=list(files), timeout=timeout
                    )
                else:
                    response = await self.client.post(url, json=payload, timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - httpx 的各种网络异常
                last_error = TelegramTransportError(method, f"{type(exc).__name__}: {exc}")
                if attempt >= attempts:
                    raise last_error from exc
                await asyncio.sleep(_backoff(attempt))
                continue

            data = _decode_body(response)
            if response.status_code == 429 or (isinstance(data, dict) and data.get("error_code") == 429):
                retry_after = _retry_after(data)
                if attempt >= attempts:
                    raise TelegramRetryAfter(method, retry_after)
                self._log("warning", f"Telegram 限流，{retry_after:.0f}s 后重试", method=method)
                await asyncio.sleep(retry_after)
                continue

            if response.status_code >= 500:
                last_error = TelegramTransportError(method, f"HTTP {response.status_code}")
                if attempt >= attempts:
                    raise last_error
                await asyncio.sleep(_backoff(attempt))
                continue

            if isinstance(data, dict) and data.get("ok"):
                return data.get("result")

            code = data.get("error_code") if isinstance(data, dict) else response.status_code
            description = data.get("description") if isinstance(data, dict) else response.text[:200]
            raise TelegramError(method, code, str(description or "unknown error"), data)

        raise last_error or TelegramError(method, None, "unknown error")

    # ── 常用方法 ──

    async def get_me(self) -> dict[str, Any]:
        return await self.call("getMe")

    async def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: int = 0,
        limit: int = 100,
        allowed_updates: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout, "limit": limit}
        if offset is not None:
            params["offset"] = offset
        if allowed_updates is not None:
            params["allowed_updates"] = list(allowed_updates)
        # 长轮询要给 HTTP 客户端留出比 Telegram 端更长的等待时间
        result = await self.call("getUpdates", params)
        return list(result or [])

    async def get_webhook_info(self) -> dict[str, Any]:
        return await self.call("getWebhookInfo")

    async def set_webhook(self, url: str, **extra: Any) -> bool:
        return bool(await self.call("setWebhook", {"url": url, **extra}))

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> bool:
        return bool(await self.call("deleteWebhook", {"drop_pending_updates": drop_pending_updates}))

    async def send_message(self, chat_id: int | str, text: str, **extra: Any) -> dict[str, Any]:
        return await self.call("sendMessage", {"chat_id": chat_id, "text": text, **extra})

    async def edit_message_text(self, chat_id: int | str, message_id: int, text: str, **extra: Any) -> Any:
        return await self.call(
            "editMessageText", {"chat_id": chat_id, "message_id": int(message_id), "text": text, **extra}
        )

    async def delete_message(self, chat_id: int | str, message_id: int) -> bool:
        return bool(await self.call("deleteMessage", {"chat_id": chat_id, "message_id": int(message_id)}))

    async def answer_callback_query(self, callback_query_id: str, **extra: Any) -> bool:
        return bool(await self.call("answerCallbackQuery", {"callback_query_id": callback_query_id, **extra}))

    async def get_file(self, file_id: str) -> dict[str, Any]:
        return await self.call("getFile", {"file_id": file_id})

    async def set_my_commands(self, commands: list[dict[str, str]], **extra: Any) -> bool:
        return bool(await self.call("setMyCommands", {"commands": commands, **extra}))

    # ── 文件 ──

    async def download_file(self, file_path: str) -> tuple[bytes, str]:
        """按 ``file_path`` 下载（``getFile`` 返回的路径），返回 (bytes, mime)。"""
        url = f"{self.endpoint}/file/bot{self.token}/{file_path.lstrip('/')}"
        try:
            response = await self.client.get(url, timeout=self._file_timeout)
        except Exception as exc:  # noqa: BLE001
            raise TelegramTransportError("downloadFile", f"{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise TelegramError("downloadFile", response.status_code, response.text[:200])
        mime = response.headers.get("content-type") or "application/octet-stream"
        return response.content, mime

    async def download_by_id(self, file_id: str) -> tuple[bytes, str, str]:
        """``getFile`` + ``download_file``，返回 (bytes, mime, file_path)。"""
        info = await self.get_file(file_id)
        file_path = str(info.get("file_path") or "")
        if not file_path:
            raise TelegramError("getFile", None, f"file_path 为空: {file_id}")
        data, mime = await self.download_file(file_path)
        return data, mime, file_path


# ── 长轮询 ──


@dataclass
class PollingStats:
    started_at: float = 0.0
    last_update_at: float = 0.0
    offset: int = 0
    received: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_error: str = ""
    online: bool = False
    removals: list[str] = field(default_factory=list)


class TelegramPoller:
    """长轮询循环：对齐 offset、自动摘 webhook、退避重连。

    语义对照 Koishi ``HttpPolling``：先 ``getWebhookInfo``（有 webhook 就删掉并告警），
    再用一次 ``timeout=0`` 的 ``getUpdates`` 探活并把 offset 对齐到最后一条；
    主循环失败时初次连接超过 ``retry_times`` 进入离线，之后按 ``retry_interval`` 无限重试。
    """

    def __init__(
        self,
        api: TelegramAPI,
        *,
        timeout: int = 25,
        retry_times: int = 6,
        retry_interval: float = 5.0,
        allowed_updates: Sequence[str] | None = None,
        on_updates: Callable[[list[dict[str, Any]]], Awaitable[None] | None] | None = None,
        on_state: Callable[[str, str], None] | None = None,
        logger: Any | None = None,
        max_offset_advance: int = 0,
    ) -> None:
        self.api = api
        self.timeout = max(0, int(timeout))
        self.retry_times = max(0, int(retry_times))
        self.retry_interval = max(0.5, float(retry_interval))
        self.allowed_updates = tuple(allowed_updates or DEFAULT_ALLOWED_UPDATES)
        self._on_updates = on_updates
        self._on_state = on_state
        self._logger = logger
        self._stop = asyncio.Event()
        self.stats = PollingStats()
        self._task: asyncio.Task[None] | None = None

    # ── 生命周期 ──

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 停止路径吞掉异常
                pass

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _state(self, state: str, message: str = "") -> None:
        self.stats.online = state == "online"
        if self._on_state is not None:
            self._on_state(state, message)

    def _log(self, level: str, message: str, **fields: Any) -> None:
        logger = self._logger
        if logger is None:
            return
        try:
            getattr(logger, level)(message, **fields)
        except Exception:  # pragma: no cover
            pass

    # ── 主循环 ──

    async def run(self) -> None:
        import time

        self.stats = PollingStats(started_at=time.time())
        try:
            info = await self.api.get_webhook_info()
            url = str(info.get("url") or "")
            if url:
                self._log("warning", "检测到已有 webhook，正在摘除（改用长轮询）", webhook=url)
                self.stats.removals.append(url)
                await self.api.delete_webhook(drop_pending_updates=False)

            # 探活 + 对齐 offset：明确带上 allowed_updates，避免沿用「除 chat_member 外」的默认
            previous = await self.api.get_updates(offset=None, timeout=0, allowed_updates=self.allowed_updates)
            for update in previous:
                self.stats.offset = max(self.stats.offset, int(update.get("update_id", 0)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 初次准备失败按离线处理
            self.stats.failures += 1
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            self._log("warning", "长轮询初始化失败", error=self.stats.last_error)
            self._state("offline", self.stats.last_error)
            if self._stop.is_set():
                return

        initial = True
        failures = 0
        while not self._stop.is_set():
            try:
                updates = await self.api.get_updates(
                    offset=self.stats.offset + 1,
                    timeout=self.timeout,
                    allowed_updates=self.allowed_updates,
                )
                failures = 0
                self.stats.consecutive_failures = 0
                if self._stop.is_set():
                    return
                self._state("online")
                if updates:
                    self.stats.received += len(updates)
                    self.stats.last_update_at = time.time()
                    for update in updates:
                        self.stats.offset = max(self.stats.offset, int(update.get("update_id", 0)))
                    if self._on_updates is not None:
                        result = self._on_updates(updates)
                        if asyncio.iscoroutine(result):
                            await result
                initial = False
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except TelegramRetryAfter as exc:
                failures += 1
                self.stats.failures += 1
                self.stats.consecutive_failures = failures
                self.stats.last_error = str(exc)
                self._log("warning", f"Telegram 限流，{exc.retry_after:.0f}s 后重试", offset=self.stats.offset)
                await self._sleep(exc.retry_after)
            except TelegramError as exc:
                failures += 1
                self.stats.failures += 1
                self.stats.consecutive_failures = failures
                self.stats.last_error = f"{exc.code}: {exc.description}"
                self._log("warning", f"长轮询失败：{exc.code} {exc.description}", offset=self.stats.offset)
                if initial and failures > self.retry_times:
                    self._state("offline", self.stats.last_error)
                    return
                self._state("reconnect", self.stats.last_error)
                await self._sleep(self.retry_interval)
            except Exception as exc:  # noqa: BLE001 - 网络层
                failures += 1
                self.stats.failures += 1
                self.stats.consecutive_failures = failures
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                self._log("warning", f"长轮询异常：{self.stats.last_error}")
                if initial and failures > self.retry_times:
                    self._state("offline", self.stats.last_error)
                    return
                self._state("reconnect", self.stats.last_error)
                await self._sleep(self.retry_interval)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.0, seconds))
        except (TimeoutError, asyncio.TimeoutError):
            return


# ── 内部工具 ──


def _decode_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError):
        return {"ok": False, "description": response.text[:200]}


def _retry_after(payload: Any) -> float:
    if isinstance(payload, dict):
        parameters = payload.get("parameters")
        if isinstance(parameters, dict) and parameters.get("retry_after") is not None:
            try:
                return max(1.0, float(parameters["retry_after"]))
            except (TypeError, ValueError):
                pass
    return 5.0


def _stringify(payload: dict[str, Any]) -> dict[str, str]:
    """multipart 表单只接受字符串值：布尔转 true/false，其余 JSON 化。"""
    result: dict[str, str] = {}
    for key, value in payload.items():
        if isinstance(value, bool):
            result[key] = "true" if value else "false"
        elif isinstance(value, (dict, list)):
            result[key] = json.dumps(value, ensure_ascii=False)
        else:
            result[key] = str(value)
    return result


def _backoff(attempt: int) -> float:
    return min(30.0, 1.5 ** attempt) * (0.75 + random.random() * 0.5)
