"""Telegram 独立 Bot —— NeoBot 插件（3.0.0）。

TG 用户直接和 AI 对话：插件收消息 → 调宿主主模型服务（``provider.chat``）→ 文本发回 TG。
**全程不涉及 QQ**：上下文由插件按 TG 会话维护。

开箱可用：默认走内置反代（URL 路径密钥 + ``X-Proxy-Key`` 请求头两道校验），
所以只需填 ``token`` 就能在直连不通的网络里工作。

模块：
    tg_api.py    Bot API 客户端与长轮询（httpx，代理直接交给它）
    tg_model.py  Update 解码（拍平成事件：谁、在哪、说了什么、带了什么媒体）
    tg_send.py   出站文本（HTML 转义 + 分段）
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from neobot_modloader import Plugin

try:  # 正常路径：NeoBot 以包的形式加载 plugins/telegram/
    from .tg_api import (
        DEFAULT_ENDPOINT,
        TelegramAPI,
        TelegramError,
        TelegramPoller,
        TelegramRetryAfter,
        TelegramTransportError,
    )
    from .tg_model import IncomingEvent, decode_update, update_subtype
    from .tg_send import escape_html, split_text
except ImportError:  # pragma: no cover - 顶层模块加载（pytest 收集 / 手工脚本）
    from tg_api import (
        DEFAULT_ENDPOINT,
        TelegramAPI,
        TelegramError,
        TelegramPoller,
        TelegramRetryAfter,
        TelegramTransportError,
    )
    from tg_model import IncomingEvent, decode_update, update_subtype
    from tg_send import escape_html, split_text

#: 内置反代：用户只填 token 即可用；留空 reverse_proxy_url 则回落 endpoint / proxy
DEFAULT_REVERSE_PROXY_URL = "https://tg.gujin99.xyz"
DEFAULT_REVERSE_PROXY_SECRET = "v1-a5e68cb17320"
DEFAULT_PROXY_KEY = "pk-a5e68cb17320"

#: 单条消息的字数上限（按段落切分，留出安全余量）
CHUNK_SIZE = 3500


def strip_wrapping_quotes(value: object) -> str:
    """去掉粘贴时带进来的包裹果引号（``"https://…"`` → ``https://…``）。"""
    text = str(value or "").strip()
    changed = True
    while changed and text:
        changed = False
        for quote in ('"', "'", "`"):
            if text.startswith(quote):
                text = text[1:].lstrip()
                changed = True
            if text.endswith(quote):
                text = text[:-1].rstrip()
                changed = True
    return text


def mask(value: object, keep: int = 3) -> str:
    """给日志/诊断用的打码：保留前缀，其余星号。"""
    text = strip_wrapping_quotes(value)
    if not text:
        return "未设置"
    return text if len(text) <= keep else text[:keep] + "***"


class Config(BaseModel):
    """插件配置；运行值在 data/plugins_data/telegram/config.toml。"""

    model_config = ConfigDict(extra="allow")

    @field_validator(
        "token",
        "endpoint",
        "reverse_proxy_url",
        "reverse_proxy_secret",
        "proxy_key",
        "proxy",
        "system_prompt",
        mode="before",
    )
    @classmethod
    def _clean_strings(cls, value: object) -> str:
        return strip_wrapping_quotes(value)

    enabled: bool = Field(default=True, description="总开关；关闭后不连 Telegram（命令仍可用）")
    token: str = Field(default="", description="BotFather 发的机器人令牌：123456:ABC-DEF...")

    # 网络
    reverse_proxy_url: str = Field(
        default=DEFAULT_REVERSE_PROXY_URL,
        description="默认走内置反代（只填 token 即可用）；留空则改用 endpoint / proxy 直连",
    )
    reverse_proxy_secret: str = Field(
        default=DEFAULT_REVERSE_PROXY_SECRET, description="反代路径密钥，与 Worker 的 PATH_SECRET 一致"
    )
    proxy_key: str = Field(
        default=DEFAULT_PROXY_KEY, description="反代请求头密钥 X-Proxy-Key，与 Worker 的 PROXY_KEY 一致"
    )
    endpoint: str = Field(default=DEFAULT_ENDPOINT, description="Bot API 终结点；反代留空时才用它")
    proxy: str = Field(
        default="", description="http(s) 代理，如 http://127.0.0.1:7890；SOCKS 请用其 HTTP 端口"
    )
    polling_timeout: int = Field(default=25, ge=1, le=60, description="长轮询单次等待秒数")
    retry_times: int = Field(default=6, ge=0, description="连续失败多少次后放弃（0 = 一直重试）")
    retry_interval: float = Field(default=5.0, ge=0.5, description="失败重试间隔（秒）")
    ignore_telegram_bots: bool = Field(default=True, description="忽略其它机器人发的消息")

    # AI
    system_prompt: str = Field(
        default="你是 NeoBot，一个友好的聊天助手。用简短自然的方式回答。", description="系统提示词（人格）"
    )
    history_limit: int = Field(default=12, ge=0, le=100, description="每个 TG 会话保留的历史条数（0 = 无上下文）")
    ai_timeout: float = Field(default=120.0, ge=5.0, description="单次模型调用超时（秒）")
    ai_error_reply: str = Field(
        default="（AI 暂时没有响应，请稍后再试）", description="模型失败时回给用户的文案；留空则不回复"
    )


_PLUGIN_KWARGS: dict[str, Any] = dict(
    version="3.0.0",
    description="Telegram 独立 Bot：只填 token 即可用（内置反代），AI 由宿主模型驱动",
    usage="发送 /tg 查看状态；/tg诊断 做一次链路探测",
    author="NeoBot 第三方插件",
    config=Config,
)

plugin = Plugin("telegram", **_PLUGIN_KWARGS)


# ── 地址解析与提示 ───────────────────────────────────────────────────


def _resolve_endpoint(config: Config) -> tuple[str, str]:
    """算出实际使用的 API 基址，返回 (基址, 来源)。"""
    reverse = strip_wrapping_quotes(config.reverse_proxy_url).rstrip("/")
    if reverse:
        if not reverse.startswith(("http://", "https://")):
            reverse = "https://" + reverse
        secret = strip_wrapping_quotes(config.reverse_proxy_secret).strip("/")
        return (f"{reverse}/{secret}" if secret else reverse), "reverse_proxy"
    endpoint = strip_wrapping_quotes(config.endpoint).rstrip("/")
    if endpoint and endpoint != DEFAULT_ENDPOINT.rstrip("/"):
        if not endpoint.startswith(("http://", "https://")):
            endpoint = "https://" + endpoint
        return endpoint, "config"
    return DEFAULT_ENDPOINT.rstrip("/"), "default"


_SOURCE_LABEL = {"reverse_proxy": "反代", "config": "endpoint 配置", "default": "官方默认"}


def _mask_endpoint(endpoint: str, config: Config) -> str:
    secret = strip_wrapping_quotes(config.reverse_proxy_secret).strip("/")
    return endpoint.replace(secret, "***") if secret and secret in endpoint else endpoint


def _connection_hint(config: Config, error: str = "") -> str:
    """连接失败时给一句能照做的建议。"""
    endpoint, source = _resolve_endpoint(config)
    proxy = strip_wrapping_quotes(config.proxy)
    if "getaddrinfo" in (error or "").lower():
        return (
            "代理主机名解析失败：容器里的 host.docker.internal 只有 Docker Desktop 提供，"
            "同机改用 127.0.0.1，或直接清空 proxy 走内置反代。"
        )
    if proxy:
        return f"已配置代理 {proxy}：确认代理进程在跑、端口对；连不上也可以清空 proxy 走内置反代。"
    if source == "reverse_proxy":
        return (
            f"已配置反代 {_mask_endpoint(endpoint, config)}：确认域名已激活、路径密钥与 "
            "proxy_key 都跟 Worker 一致，且反代同时转发 /bot<token>/… 与 /file/bot<token>/…。"
        )
    return "当前直连 api.telegram.org 且没有代理/反代：国内网络通常连不通，建议留空 reverse_proxy_url 用内置反代。"


# ── 运行时 ───────────────────────────────────────────────────────────


class TelegramRuntime:
    """一个实例持有 API 客户端、轮询器、会话上下文与计数器。"""

    def __init__(self, ctx: Any, config: Config) -> None:
        self.ctx = ctx
        self.config = config
        self.api: TelegramAPI | None = None
        self.poller: TelegramPoller | None = None
        self.me: dict[str, Any] = {}
        self.bot_username = ""
        self.state = "stopped"
        self.last_error = ""
        self.started_at = 0.0
        self.counters: dict[str, int] = {"updates": 0, "messages": 0, "replies": 0, "failures": 0, "errors": 0}
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._retry_task: asyncio.Task[None] | None = None

    # ── 基础设施 ──

    def log(self, level: str, message: str, **fields: Any) -> None:
        logger = getattr(self.ctx, "logger", None)
        if logger is None:
            return
        with contextlib.suppress(Exception):
            getattr(logger, level)(message, **fields)

    # ── 生命周期 ──

    async def start(self) -> None:
        if not self.config.enabled:
            self.state = "disabled"
            self.log("info", "Telegram 已禁用（enabled=false）")
            return
        token = strip_wrapping_quotes(self.config.token)
        if not token:
            self.state = "no-token"
            self.log("warning", "未配置 token，插件待机；在面板填入后重载")
            return

        endpoint, source = _resolve_endpoint(self.config)
        self.log(
            "info",
            "正在连接 Telegram",
            endpoint=_mask_endpoint(endpoint, self.config),
            source=_SOURCE_LABEL.get(source, source),
            proxy=strip_wrapping_quotes(self.config.proxy) or "未配置代理",
        )
        try:
            self.api = TelegramAPI(
                token,
                endpoint=endpoint,
                proxy=strip_wrapping_quotes(self.config.proxy) or None,
                headers={"X-Proxy-Key": self.config.proxy_key} if self.config.proxy_key else None,
                logger=getattr(self.ctx, "logger", None),
            )
            self.me = await self.api.get_me()
            self.bot_username = str(self.me.get("username") or "")
            self.poller = TelegramPoller(
                self.api,
                timeout=int(self.config.polling_timeout),
                retry_times=int(self.config.retry_times),
                retry_interval=float(self.config.retry_interval),
                on_updates=self.handle_updates,
                on_state=self._on_state,
                logger=getattr(self.ctx, "logger", None),
            )
            self.started_at = time.time()
            await self.poller.start()
            self.log("info", f"Telegram 已连接：@{self.bot_username or self.me.get('id')}")
        except Exception as exc:  # noqa: BLE001 - 网络问题不该打断插件加载
            self.state = "error"
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.log("warning", f"Telegram 连接异常：{self.last_error}")
            self.log("warning", "排查建议：" + _connection_hint(self.config, self.last_error))
            self._schedule_retry()

    async def stop(self) -> None:
        if self._retry_task is not None:
            self._retry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._retry_task
            self._retry_task = None
        if self.poller is not None:
            await self.poller.stop()
        if self.api is not None:
            await self.api.close()
        self.state = "stopped"

    def _on_state(self, state: str, message: str = "") -> None:
        self.state = state
        if message:
            self.last_error = message

    def _schedule_retry(self) -> None:
        """初次连接失败后，隔一会儿探活，通了就自动接上轮询。"""
        if self._retry_task is not None and not self._retry_task.done():
            return

        async def runner() -> None:
            interval = max(15.0, float(self.config.retry_interval) * 3)
            while self.state == "error":
                await asyncio.sleep(interval)
                if self.state != "error" or self.api is None:
                    return
                try:
                    ok, detail = await self.api.probe(timeout=8)
                except Exception:  # noqa: BLE001
                    continue
                if not ok:
                    continue
                self.log("info", f"Telegram 恢复连通：{detail}")
                try:
                    self.me = await self.api.get_me()
                    self.bot_username = str(self.me.get("username") or "")
                    if self.poller is not None:
                        await self.poller.start()
                except Exception as exc:  # noqa: BLE001
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    continue
                return

        self._retry_task = asyncio.create_task(runner())

    # ── 入站 ──

    async def handle_updates(self, updates: list[dict[str, Any]]) -> None:
        self.counters["updates"] += len(updates)
        for update in updates:
            self.log("debug", f"收到 Telegram 更新：{update_subtype(update)}")
            try:
                event = decode_update(update)
            except Exception as exc:  # noqa: BLE001
                self.counters["errors"] += 1
                self.log("warning", f"更新解析失败：{type(exc).__name__}: {exc}")
                continue
            if event is not None:
                await self.dispatch(event)

    async def dispatch(self, event: IncomingEvent) -> None:
        if event.kind == "callback_query":
            if self.api is not None and event.callback_id:
                with contextlib.suppress(Exception):
                    await self.api.answer_callback_query(event.callback_id)
            return
        if event.user_is_bot and self.config.ignore_telegram_bots:
            return
        self.counters["messages"] += 1
        await self.reply_with_ai(event)

    # ── AI 对话 ──

    @property
    def provider(self) -> Any | None:
        """宿主的主模型服务（neobot_chat 的 Provider）。"""
        services = getattr(self.ctx, "services", None)
        if services is None:
            return None
        try:
            return services.get("provider")
        except Exception:  # noqa: BLE001 - 老宿主没有服务注册表
            return None

    def provider_label(self) -> str:
        return "宿主 provider（可用）" if self.provider is not None else "宿主 provider（不可用）"

    def history_for(self, chat_id: str) -> list[dict[str, Any]]:
        return self._history.setdefault(str(chat_id), [])

    async def reply_with_ai(self, event: IncomingEvent) -> None:
        if self.provider is None:
            self.counters["failures"] += 1
            await self._safe_send(
                event.chat_id,
                "宿主没有可用的模型服务（provider），暂时无法回复；请先在 NeoBot 面板里配置模型",
                reply_to=event.message_id,
            )
            return
        lock = self._locks.setdefault(str(event.chat_id), asyncio.Lock())
        async with lock:
            text = await self._ask_ai(str(event.chat_id), event.body)
        if not text:
            self.counters["failures"] += 1
            if self.config.ai_error_reply:
                await self._safe_send(event.chat_id, self.config.ai_error_reply, reply_to=event.message_id)
            return
        self.counters["replies"] += 1
        await self._safe_send(event.chat_id, text, reply_to=event.message_id)

    async def _ask_ai(self, chat_id: str, user_text: str) -> str:
        """直接调宿主模型服务，全程在 Telegram 侧完成。"""
        provider = self.provider
        if provider is None or not user_text:
            return ""
        history = self.history_for(chat_id)
        messages: list[dict[str, Any]] = []
        if self.config.system_prompt.strip():
            messages.append({"role": "system", "content": self.config.system_prompt})
        messages.extend(history)
        messages.append({"role": "user", "content": user_text})
        try:
            result = await asyncio.wait_for(provider.chat(messages), timeout=float(self.config.ai_timeout))
        except asyncio.TimeoutError:
            self.counters["errors"] += 1
            self.log("warning", f"模型调用超时（{self.config.ai_timeout}s）")
            return ""
        except Exception as exc:  # noqa: BLE001 - 模型侧异常不该打爆插件
            self.counters["errors"] += 1
            self.log("warning", f"模型调用失败：{type(exc).__name__}: {exc}")
            return ""

        text = str((result or {}).get("content") or "").strip() if isinstance(result, dict) else ""
        if not text:
            self.log("warning", "模型返回了空内容")
            return ""
        if self.config.history_limit > 0:
            history += [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": text},
            ]
            limit = int(self.config.history_limit) * 2
            del history[: max(0, len(history) - limit)]
        return text

    # ── 发送 ──

    async def _safe_send(self, chat_id: int | str, text: str, *, reply_to: int = 0) -> None:
        try:
            await self.send_text(chat_id, text, reply_to=reply_to)
        except Exception as exc:  # noqa: BLE001
            self.counters["errors"] += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.log("warning", f"发送到 Telegram 失败：{self.last_error}")

    async def send_text(self, chat_id: int | str, text: str, *, reply_to: int = 0) -> int:
        """发送纯文本；超长自动分段。返回发送条数。"""
        if self.api is None:
            raise RuntimeError("Telegram 未连接（检查 token / 代理 / 反代）")
        chunks = split_text(escape_html(text), CHUNK_SIZE)
        if not chunks:
            chunks = ["（空回复）"]
        sent = 0
        for index, chunk in enumerate(chunks):
            params: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            }
            if reply_to and index == 0:
                params["reply_to_message_id"] = reply_to
            await self.api.call("sendMessage", params)
            sent += 1
        return sent

    # ── 状态 / 诊断 ──

    def _proxy_line(self) -> str:
        proxy = strip_wrapping_quotes(self.config.proxy)
        return proxy or "未设置（走反代或直连）"

    def status_text(self) -> str:
        endpoint, source = _resolve_endpoint(self.config)
        lines = [
            "Telegram 独立 Bot · 状态",
            f"· 运行状态：{self.state}",
            f"· 机器人：@{self.bot_username}" if self.bot_username else "· 机器人：未连接",
            f"· 终结点：{_mask_endpoint(endpoint, self.config)}（来源：{_SOURCE_LABEL.get(source, source)}）",
            f"· 代理：{self._proxy_line()}",
            f"· 模型服务：{self.provider_label()}",
            "· 计数器：" + "，".join(f"{key}={value}" for key, value in self.counters.items()),
        ]
        if self.last_error:
            lines.append(f"· 最近错误：{self.last_error}")
        return "\n".join(lines)

    def diag_text(self) -> str:
        token = strip_wrapping_quotes(self.config.token)
        endpoint, source = _resolve_endpoint(self.config)
        lines = [
            "Telegram 链路诊断",
            f"· 版本：{plugin.version}",
            f"· token：{token.split(':')[0] + ':***' if ':' in token else mask(token)}",
            f"· endpoint：{_mask_endpoint(endpoint, self.config)}（来源：{_SOURCE_LABEL.get(source, source)}）",
            f"· 代理：{self._proxy_line()}",
            f"· proxy_key：{mask(self.config.proxy_key)}（X-Proxy-Key 请求头）",
            f"· 模型服务：{self.provider_label()}；超时={self.config.ai_timeout}s",
            f"· 上下文：{self.config.history_limit} 条/会话；system_prompt {len(self.config.system_prompt)} 字",
        ]
        if self.poller is not None:
            stats = self.poller.stats
            lines.append(f"· 轮询：offset={stats.offset} 收到={stats.received} 失败={stats.failures}")
        return "\n".join(lines)

    async def diag_report(self) -> str:
        lines = [self.diag_text()]
        if self.state in ("no-token", "disabled"):
            lines.append("· 连通性探测：跳过（未配置 token 或已禁用）")
            return "\n".join(lines)
        if self.api is None:
            lines.append(f"· 客户端：未建立（state={self.state}）")
            lines.append("· 建议：" + _connection_hint(self.config, self.last_error))
            return "\n".join(lines)
        ok, detail = await self.api.probe()
        lines.append(f"· 连通性探测：{'通' if ok else '不通'} —— {detail}")
        if not ok:
            lines.append("· 建议：" + _connection_hint(self.config, detail))
        return "\n".join(lines)


# ── 插件级单例 ───────────────────────────────────────────────────────

_RUNTIME: TelegramRuntime | None = None


def runtime_of(ctx: Any, config: Config | None = None) -> TelegramRuntime:
    global _RUNTIME
    if _RUNTIME is None or _RUNTIME.ctx is not ctx:
        _RUNTIME = TelegramRuntime(ctx, config or Config.model_validate(dict(getattr(ctx, "config", None) or {})))
    elif config is not None:
        _RUNTIME.config = config
    return _RUNTIME


# ── 命令（只有状态查询） ─────────────────────────────────────────────


def register_commands(ctx: Any, config: Config) -> None:
    registrar = getattr(ctx, "app_commands", None)
    if registrar is None or not getattr(registrar, "available", False):
        ctx.logger.warning("[telegram] 宿主命令表不可用，命令未注册")
        return

    registrar.register(
        "tg",
        description="Telegram 独立 Bot 的运行状态（别名：telegram / 电报）",
        usage="",
        permission=0,
        aliases=("telegram", "电报"),
        params=(),
    )(lambda command_context: _run_status(ctx))
    registrar.register(
        "tg诊断",
        description="Telegram 链路诊断：token / 反代 / 代理 / 模型服务（别名：tgdiag）",
        usage="",
        permission=0,
        aliases=("tgdiag", "电报诊断"),
        params=(),
    )(lambda command_context: _run_diag(ctx))
    ctx.logger.info("[telegram] 已注册命令：/tg、/tg诊断（含别名）")


async def _run_status(ctx: Any) -> str:
    return runtime_of(ctx).status_text()


async def _run_diag(ctx: Any) -> str:
    return await runtime_of(ctx).diag_report()


# ── 生命周期 ─────────────────────────────────────────────────────────


def _config_of(ctx: Any) -> Config:
    return Config.model_validate(dict(getattr(ctx, "config", None) or {}))


@plugin.on_load
async def on_load(ctx: Any) -> None:
    config = _config_of(ctx)
    runtime_of(ctx, config)
    register_commands(ctx, config)
    ctx.logger.info(f"[telegram] 已加载：模型服务={'可用' if runtime_of(ctx).provider else '未就绪'}")


@plugin.on_startup
async def on_startup(ctx: Any) -> None:
    await runtime_of(ctx, _config_of(ctx)).start()


@plugin.on_shutdown
async def on_shutdown(ctx: Any) -> None:
    global _RUNTIME
    if _RUNTIME is None:
        return
    await _RUNTIME.stop()
    ctx.logger.info("[telegram] 已停止")
    _RUNTIME = None


__all__ = [
    "CHUNK_SIZE",
    "Config",
    "DEFAULT_PROXY_KEY",
    "DEFAULT_REVERSE_PROXY_SECRET",
    "DEFAULT_REVERSE_PROXY_URL",
    "TelegramRuntime",
    "mask",
    "on_load",
    "on_shutdown",
    "on_startup",
    "plugin",
    "register_commands",
    "runtime_of",
    "strip_wrapping_quotes",
]
