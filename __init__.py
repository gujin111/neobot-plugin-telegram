"""Telegram 独立 Bot —— NeoBot 插件（2.1.0）。

Telegram 用户直接在 TG 里和 AI 对话：插件收 TG 消息 → 直接调用宿主的主模型服务
（``provider``，接口 ``async chat(messages) -> Message``）→ 把文本发回 TG。
**全程不涉及任何 QQ 会话**：上下文由插件按 TG 会话自己维护。

为什么不用宿主的回复管线：那条管线以 QQ 会话（kind + conversation_id）为单位，
需要指定一个 QQ 群/私聊当"工作区"，还会把 AI 的回复发到 QQ。独立 TG bot 不需要这些，
所以这里只借用宿主的模型服务，其余全部在 Telegram 侧完成。

模块：
    tg_api.py    Bot API 客户端（代理 / 反代 / 重试 / 长轮询 / 文件）
    tg_model.py  Update 解码（实体、媒体、相册）
    tg_send.py   出站渲染（HTML、分段、媒体、按钮）
    tg_proxy.py  内置 SOCKS5 + 本地 CONNECT 网关 + auto 探测
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from neobot_modloader import Plugin

try:  # 正常路径：NeoBot 以包的形式加载 plugins/telegram/
    from .tg_api import (
        DEFAULT_ALLOWED_UPDATES,
        DEFAULT_ENDPOINT,
        TelegramAPI,
        TelegramError,
        TelegramPoller,
        TelegramRetryAfter,
        TelegramTransportError,
    )
    from .tg_model import IncomingEvent, MediaGroupBuffer, decode_update, update_subtype
    from .tg_proxy import ProxyUnavailable, detect_proxy, parse_proxy
    from .tg_send import PlanOptions, SendAction, build_actions, render_segments
except ImportError:  # pragma: no cover - 顶层模块加载（pytest 收集 / 手工脚本）
    from tg_api import (
        DEFAULT_ALLOWED_UPDATES,
        DEFAULT_ENDPOINT,
        TelegramAPI,
        TelegramError,
        TelegramPoller,
        TelegramRetryAfter,
        TelegramTransportError,
    )
    from tg_model import IncomingEvent, MediaGroupBuffer, decode_update, update_subtype
    from tg_proxy import ProxyUnavailable, detect_proxy, parse_proxy
    from tg_send import PlanOptions, SendAction, build_actions, render_segments

# ── 配置 ─────────────────────────────────────────────────────────────


#: 默认反代：插件作者提供的公共反代，用户只填 token 就能用（无需代理/域名）
#: 想直连或用自己的反代，把 reverse_proxy_url 清空 / 改成自己的即可
DEFAULT_REVERSE_PROXY_URL = "https://tg.gujin99.xyz"
DEFAULT_REVERSE_PROXY_SECRET = "v1-a5e68cb17320"


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


class Config(BaseModel):
    """插件配置；运行值在 data/plugins_data/telegram/config.toml。"""

    model_config = ConfigDict(extra="allow")

    @field_validator(
        "token",
        "endpoint",
        "reverse_proxy_url",
        "reverse_proxy_secret",
        "proxy",
        "system_prompt",
        mode="before",
    )
    @classmethod
    def _clean_strings(cls, value: object) -> str:
        return strip_wrapping_quotes(value)

    # ── 连接 ──
    enabled: bool = Field(default=True, description="总开关；关闭后不连 Telegram（命令仍可用）")
    token: str = Field(default="", description="BotFather 发的机器人令牌：123456:ABC-DEF...")
    endpoint: str = Field(default=DEFAULT_ENDPOINT, description="Bot API 终结点；反代留空时才用它")
    reverse_proxy_url: str = Field(
        default=DEFAULT_REVERSE_PROXY_URL,
        description="默认走内置反代（只需填 token 即可用）；留空则改用 endpoint / proxy 直连",
    )
    reverse_proxy_secret: str = Field(
        default=DEFAULT_REVERSE_PROXY_SECRET,
        description="内置反代的路径密钥；换成自己的反代时改成自己的密钥",
    )
    proxy: str = Field(
        default="",
        description="网络代理：http://…、socks5://…（内置实现，不需要 socksio），或 auto 自动探测",
    )
    protocol: Literal["polling", "webhook"] = Field(
        default="polling", description="通信方式；当前只实现 polling"
    )
    polling_timeout: int = Field(default=25, ge=1, le=60, description="长轮询单次等待秒数")
    retry_times: int = Field(default=6, ge=0, description="初次连接失败的最大重试次数")
    retry_interval: float = Field(default=5.0, ge=0.5, description="断开后的重试间隔（秒）")
    allowed_updates: list[str] = Field(
        default_factory=list, description="只接收这些类型的更新；留空 = 全部（含 chat_member）"
    )

    # ── 消息 ──
    parse_mode: Literal["html", "none"] = Field(default="html", description="发送时使用的解析格式")
    link_preview: bool = Field(default=True, description="发送链接时展示网页预览")
    chunk_size: int = Field(default=3500, ge=200, le=4096, description="长文本自动分段长度上限")
    ignore_telegram_bots: bool = Field(default=True, description="忽略其它机器人发的消息")

    # ── AI（直接调宿主模型服务，和 QQ 完全无关） ──
    system_prompt: str = Field(
        default="你是 NeoBot，一个友好的聊天助手。用简短自然的方式回答。",
        description="系统提示词（人格设定），每个 TG 会话都在此基础上对话",
    )
    history_limit: int = Field(
        default=12, ge=0, le=100, description="每个 TG 会话保留的历史消息条数（0 = 无上下文）"
    )
    ai_timeout: float = Field(default=120.0, ge=5.0, description="单次模型调用超时（秒）")
    ai_error_reply: str = Field(
        default="（AI 暂时没有响应，请稍后再试）", description="模型失败时回给用户的文案；留空则不回复"
    )


_PLUGIN_KWARGS: dict[str, Any] = dict(
    version="2.2.0",
    description="Telegram 独立 Bot：只填 token 即可用（内置反代），不涉及 QQ",
    usage="发送 /tg 查看状态；/tg诊断 做一次链路探测",
    author="NeoBot 第三方插件",
    config=Config,
)

try:
    # provides 是 NeoBot 2.1.0a15 才有的参数；老宿主上退化为不声明
    plugin = Plugin("telegram", provides=["telegram"], **_PLUGIN_KWARGS)
except TypeError:  # pragma: no cover - 仅老宿主走到这里
    plugin = Plugin("telegram", **_PLUGIN_KWARGS)


# ── 工具函数 ─────────────────────────────────────────────────────────


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


def _mask_endpoint(endpoint: str, config: Config) -> str:
    secret = strip_wrapping_quotes(config.reverse_proxy_secret).strip("/")
    if secret and secret in endpoint:
        return endpoint.replace(secret, "***")
    return endpoint


def _source_label(source: str) -> str:
    return {"reverse_proxy": "反代", "config": "endpoint 配置", "default": "官方默认"}.get(source, source)



def _connection_hint(config: Config, error: str = "") -> str:
    """连接失败时给出可执行的建议。"""
    endpoint, source = _resolve_endpoint(config)
    raw_proxy = strip_wrapping_quotes(config.proxy)
    lowered = (error or "").lower()
    tips: list[str] = []
    if raw_proxy.lower() == "auto":
        tips.append("proxy=auto 会依次试 127.0.0.1、host.docker.internal、容器默认网关、172.17.0.1 等地址。")
    if raw_proxy:
        try:
            label = parse_proxy(raw_proxy).label
        except ProxyUnavailable:
            label = raw_proxy
        tips.append(f"已配置代理 {label}：确认代理进程在运行、端口正确。")
        if raw_proxy.lower().startswith("socks"):
            tips.append("socks5:// 由插件内置握手；失败通常是端口不提供 SOCKS5 或账号密码不对。")
    if "getaddrinfo" in lowered:
        tips.append(
            "这次报的是「代理主机名解析失败」：host.docker.internal 只有 Docker Desktop 提供。"
            "同一台机器上用 127.0.0.1；容器里用宿主地址或 proxy=\"auto\"。"
        )
    if source == "reverse_proxy":
        tips.append(
            f"已配置反代 {_mask_endpoint(endpoint, config)}：确认域名已激活、路径密钥一致，"
            "且反代同时转发 /bot<token>/<method> 与 /file/bot<token>/...。"
        )
    elif source == "config":
        tips.append(f"已配置 endpoint {endpoint}：确认宿主机能访问它。")
    elif not raw_proxy:
        tips.append("当前直连 api.telegram.org 且未配置代理/反代：国内网络通常无法连通。")
    return " ".join(tips)


# ── 运行时 ─────────────────────────────────────────────────────────


class TelegramRuntime:
    """一个实例持有 API 客户端、轮询器与计数器。"""

    def __init__(self, ctx: Any, config: Config) -> None:
        self.ctx = ctx
        self.config = config
        self.api: TelegramAPI | None = None
        self.poller: TelegramPoller | None = None
        self.buffer = MediaGroupBuffer(window=1.2)
        self.me: dict[str, Any] = {}
        self.bot_username = ""
        self.started_at = 0.0
        self.state = "stopped"
        self.last_error = ""
        self.resolved_proxy = ""
        self.counters: dict[str, int] = {
            "updates": 0,
            "messages": 0,
            "ai_replies": 0,
            "ai_failures": 0,
            "dropped_bot": 0,
            "errors": 0,
        }
        self._flush_task: asyncio.Task[None] | None = None
        self._watchdog: asyncio.Task[None] | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._history: dict[str, list[dict[str, Any]]] = {}

    # ── 日志 ──

    def log(self, level: str, message: str, **fields: Any) -> None:
        logger = getattr(self.ctx, "logger", None)
        if logger is None:
            return
        try:
            getattr(logger, level)(message, **fields)
        except Exception:  # pragma: no cover - 日志失败不影响主流程
            pass

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
        if self.config.protocol != "polling":
            self.log("warning", "当前版本只实现 polling；已按 polling 启动")
        endpoint, source = _resolve_endpoint(self.config)
        proxy_value = await self._resolve_proxy()
        self.log(
            "info",
            "正在连接 Telegram",
            endpoint=_mask_endpoint(endpoint, self.config),
            source=source,
            proxy=(proxy_value or "未配置代理"),
        )
        try:
            self.api = TelegramAPI(
                token,
                endpoint=endpoint,
                proxy=proxy_value or None,
                logger=self.ctx.logger,
            )
            await self._login()
            await self._start_poller()
            self.log(
                "info",
                f"Telegram 已连接：@{self.bot_username or self.me.get('id')}"
                f"（id={self.me.get('id')}，模型服务={self.ai_provider_label()}）",
            )
        except Exception as exc:  # noqa: BLE001 - 插件加载不能被网络问题打断
            self.state = "error"
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.log("warning", f"Telegram 连接异常：{self.last_error}")
            self.log("warning", "排查建议：" + _connection_hint(self.config, self.last_error))
            self._schedule_watchdog()

    async def _login(self) -> None:
        assert self.api is not None
        self.me = await self.api.get_me()
        self.bot_username = str(self.me.get("username") or "")

    async def _start_poller(self) -> None:
        assert self.api is not None
        self.poller = TelegramPoller(
            self.api,
            timeout=int(self.config.polling_timeout),
            retry_times=int(self.config.retry_times),
            retry_interval=float(self.config.retry_interval),
            allowed_updates=self.config.allowed_updates or DEFAULT_ALLOWED_UPDATES,
            on_updates=self.handle_updates,
            on_state=self._on_state,
            logger=self.ctx.logger,
        )
        self.started_at = time.time()
        self.state = "starting"
        await self.poller.start()

    async def stop(self) -> None:
        for task in (self._watchdog, self._flush_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._watchdog = None
        self._flush_task = None
        if self.poller is not None:
            await self.poller.stop()
        if self.api is not None:
            await self.api.close()
        self.state = "stopped"

    async def reconnect(self) -> str:
        await self.stop()
        await self.start()
        return self.status_text()

    def _on_state(self, state: str, message: str = "") -> None:
        self.state = state
        if message:
            self.last_error = message

    def _schedule_watchdog(self) -> None:
        if self._watchdog is not None and not self._watchdog.done():
            return
        if self.api is None:
            return

        async def runner() -> None:
            interval = max(15.0, float(self.config.retry_interval) * 3)
            while self.state == "error" and self.api is not None:
                await asyncio.sleep(interval)
                if self.state != "error" or self.api is None:
                    return
                try:
                    ok, detail = await self.api.probe(timeout=8)
                except Exception as exc:  # noqa: BLE001
                    self.log("debug", f"连通性复检异常：{type(exc).__name__}: {exc}")
                    continue
                if not ok:
                    continue
                self.log("info", f"Telegram 恢复连通：{detail}")
                try:
                    await self._login()
                    await self._start_poller()
                except Exception as exc:  # noqa: BLE001
                    self.state = "error"
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    self.log("warning", f"恢复后启动轮询失败：{self.last_error}")
                return

        self._watchdog = asyncio.create_task(runner())

    # ── 代理 ──

    async def _resolve_proxy(self) -> str:
        raw = strip_wrapping_quotes(self.config.proxy)
        if raw.lower() != "auto":
            self.resolved_proxy = raw
            return raw
        detected = await detect_proxy()
        self.resolved_proxy = detected
        if detected:
            self.log("info", "proxy=auto 已探测到宿主代理", proxy=detected)
        else:
            self.log("warning", "proxy=auto 没找到可用代理端口")
        return detected

    # ── 入站 ──

    async def handle_updates(self, updates: list[dict[str, Any]]) -> None:
        self.counters["updates"] += len(updates)
        for update in updates:
            self.log("debug", f"收到 Telegram 更新：{update_subtype(update)}")
            try:
                event = decode_update(update, bot_username=self.bot_username)
            except Exception as exc:  # noqa: BLE001
                self.counters["errors"] += 1
                self.log("warning", f"更新解析失败：{type(exc).__name__}: {exc}")
                continue
            if event is None:
                continue
            if event.media_group_id:
                self.buffer.push(event)
                self._schedule_flush()
                continue
            await self.dispatch(event)

    def _schedule_flush(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return

        async def runner() -> None:
            try:
                await asyncio.sleep(self.buffer.window + 0.05)
                for merged in self.buffer.flush_due():
                    await self.dispatch(merged)
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception as exc:  # noqa: BLE001
                self.counters["errors"] += 1
                self.log("warning", f"相册合并派发失败：{type(exc).__name__}: {exc}")

        self._flush_task = asyncio.create_task(runner())

    async def dispatch(self, event: IncomingEvent) -> None:
        if event.kind == "callback_query":
            await self._answer_callback(event)
            return
        if event.kind != "message":
            self.log("debug", f"忽略的事件类型：{event.kind}")
            return
        if event.user_is_bot and self.config.ignore_telegram_bots:
            self.counters["dropped_bot"] += 1
            return
        self.counters["messages"] += 1
        await self.reply_with_ai(event)

    async def _answer_callback(self, event: IncomingEvent) -> None:
        if not (self.api and event.callback_id):
            return
        with contextlib.suppress(Exception):
            await self.api.answer_callback_query(event.callback_id)

    # ── AI 对话 ──

    def ai_provider_label(self) -> str:
        return "宿主 provider（可用）" if self._provider() is not None else "宿主 provider（不可用）"

    def _provider(self) -> Any | None:
        """宿主的主模型服务（neobot_chat 的 Provider：async chat(messages) -> Message）。"""
        services = getattr(self.ctx, "services", None)
        if services is None:
            return None
        try:
            return services.get("provider")
        except Exception:  # noqa: BLE001 - 老宿主没有服务注册表
            return None

    def compose(self, event: IncomingEvent) -> str:
        body = event.caption or event.text
        if body:
            return body
        kinds = "、".join(sorted({item.kind for item in event.media})) or "空消息"
        return f"[{kinds}]"

    def _history_for(self, chat_id: str) -> list[dict[str, Any]]:
        return self._history.setdefault(str(chat_id), [])

    async def reply_with_ai(self, event: IncomingEvent) -> None:
        if self._provider() is None:
            self.counters["ai_failures"] += 1
            await self._safe_send(
                event.chat_id,
                "宿主没有可用的模型服务（provider），暂时无法回复；请先在 NeoBot 面板里配置模型",
                reply_to=event.message_id,
            )
            return
        lock = self._locks.setdefault(str(event.chat_id), asyncio.Lock())
        async with lock:
            text = await self._ask_ai(str(event.chat_id), self.compose(event))
        if not text:
            self.counters["ai_failures"] += 1
            if self.config.ai_error_reply:
                await self._safe_send(event.chat_id, self.config.ai_error_reply, reply_to=event.message_id)
            return
        self.counters["ai_replies"] += 1
        await self._safe_send(event.chat_id, text, reply_to=event.message_id)

    async def _ask_ai(self, chat_id: str, user_text: str) -> str:
        """直接调宿主模型服务：完全在 Telegram 侧完成，不经过任何 QQ 会话。"""
        provider = self._provider()
        if provider is None:
            return ""
        history = self._history_for(chat_id)
        messages: list[dict[str, Any]] = []
        if self.config.system_prompt.strip():
            messages.append({"role": "system", "content": self.config.system_prompt})
        messages.extend(history)
        messages.append({"role": "user", "content": user_text})
        try:
            result = await asyncio.wait_for(
                provider.chat(messages), timeout=float(self.config.ai_timeout)
            )
        except asyncio.TimeoutError:
            self.counters["errors"] += 1
            self.log("warning", f"模型调用超时（{self.config.ai_timeout}s）")
            return ""
        except Exception as exc:  # noqa: BLE001 - 模型侧异常不该打爆插件
            self.counters["errors"] += 1
            self.log("warning", f"模型调用失败：{type(exc).__name__}: {exc}")
            return ""

        if isinstance(result, dict):
            text = str(result.get("content") or "").strip()
        else:
            text = str(getattr(result, "content", "") or "").strip()
        if not text:
            self.log("warning", "模型返回了空内容")
            return ""

        if self.config.history_limit > 0:
            history.append({"role": "user", "content": user_text})
            history.append({"role": "assistant", "content": text})
            limit = int(self.config.history_limit) * 2
            if len(history) > limit:
                del history[: len(history) - limit]
        return text

    # ── 发送 ──

    def plan_options(self, *, thread_id: int = 0) -> PlanOptions:
        return PlanOptions(
            parse_mode="html" if self.config.parse_mode == "html" else "",
            link_preview=bool(self.config.link_preview),
            chunk_size=int(self.config.chunk_size),
            thread_id=thread_id,
        )

    async def _safe_send(self, chat_id: int | str, text: str, *, reply_to: int = 0) -> None:
        try:
            await self.send_text(chat_id, text, reply_to=reply_to)
        except Exception as exc:  # noqa: BLE001
            self.counters["errors"] += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.log("warning", f"发送到 Telegram 失败：{self.last_error}")

    async def send_text(self, chat_id: int | str, text: str, *, reply_to: int = 0) -> list[Any]:
        if self.api is None:
            raise RuntimeError("Telegram 未连接（检查 token / 代理 / endpoint）")
        rendered = render_segments([{"type": "text", "data": {"text": text}}], reply_to_message_id=reply_to)
        return await self.send_actions(build_actions(chat_id, rendered, options=self.plan_options()))

    async def send_actions(self, actions: list[SendAction]) -> list[Any]:
        assert self.api is not None
        results: list[Any] = []
        for action in actions:
            try:
                if action.files:
                    results.append(await self.api.call(action.method, action.params, files=action.files))
                else:
                    results.append(await self.api.call(action.method, action.params))
            except (TelegramError, TelegramTransportError, TelegramRetryAfter):
                self.counters["errors"] += 1
                raise
        return results

    # ── 状态 / 诊断 ──

    def _proxy_line(self) -> str:
        raw = strip_wrapping_quotes(self.config.proxy)
        if not raw:
            return "未设置（直连）"
        if raw.lower() == "auto":
            return f"auto → {self.resolved_proxy or '未探测到'}"
        gateway = getattr(self.api, "gateway_url", "") if self.api is not None else ""
        if gateway:
            try:
                label = parse_proxy(raw).label
            except ProxyUnavailable:
                label = raw
            return f"{label}（内置 SOCKS5 网关：{gateway}）"
        return raw

    def status_text(self) -> str:
        endpoint, source = _resolve_endpoint(self.config)
        lines = [
            "Telegram 独立 Bot · 状态",
            f"· 运行状态：{self.state}",
            f"· 机器人：@{self.bot_username}" if self.bot_username else "· 机器人：未连接",
            f"· 终结点：{_mask_endpoint(endpoint, self.config)}（来源：{_source_label(source)}）",
            f"· 代理：{self._proxy_line()}",
            f"· 模型服务：{self.ai_provider_label()}（上下文 {self.config.history_limit} 条/会话）",
            "· 计数器：" + "，".join(f"{key}={value}" for key, value in self.counters.items()),
        ]
        if self.last_error:
            lines.append(f"· 最近错误：{self.last_error}")
        return "\n".join(lines)

    def diag_text(self) -> str:
        token = strip_wrapping_quotes(self.config.token)
        masked = f"{token.split(':')[0]}:***" if ":" in token else ("未配置" if not token else "格式异常")
        endpoint, source = _resolve_endpoint(self.config)
        lines = [
            "Telegram 链路诊断",
            f"· 版本：{plugin.version}",
            f"· token：{masked}",
            f"· endpoint：{_mask_endpoint(endpoint, self.config)}（来源：{_source_label(source)}）",
            f"· proxy：{self._proxy_line()}",
            f"· protocol：{self.config.protocol}（当前实际按 polling 运行）",
            f"· 模型服务：{self.ai_provider_label()}；超时={self.config.ai_timeout}s；"
            f"上下文={self.config.history_limit} 条/会话",
            f"· system_prompt：{len(self.config.system_prompt)} 字",
        ]
        if self.poller is not None:
            stats = self.poller.stats
            lines.append(
                f"· 轮询：offset={stats.offset} 收到={stats.received} 失败={stats.failures}"
                + (f" 最近错误={stats.last_error}" if stats.last_error else "")
            )
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


def _config_of(ctx: Any) -> Config:
    return Config.model_validate(dict(getattr(ctx, "config", None) or {}))


def _runtime(ctx: Any, config: Config | None = None) -> TelegramRuntime:
    global _RUNTIME
    if _RUNTIME is None or _RUNTIME.ctx is not ctx:
        _RUNTIME = TelegramRuntime(ctx, config or _config_of(ctx))
    elif config is not None:
        _RUNTIME.config = config
    return _RUNTIME


# ── 命令（只保留状态查询） ─────────────────────────────────────────


def register_commands(ctx: Any, config: Config) -> None:
    registrar = getattr(ctx, "app_commands", None)
    if registrar is None or not getattr(registrar, "available", False):
        ctx.logger.warning("[telegram] 宿主命令表不可用，命令未注册")
        return

    async def run_status(command_context: Any) -> str:
        return _runtime(ctx).status_text()

    async def run_diag(command_context: Any) -> str:
        return await _runtime(ctx).diag_report()

    registrar.register(
        "tg",
        description="Telegram 直连 AI 的运行状态（别名：telegram / 电报）",
        usage="",
        permission=0,
        aliases=("telegram", "电报"),
        params=(),
    )(run_status)
    registrar.register(
        "tg诊断",
        description="Telegram 链路诊断：token / 反代 / 代理 / 轮询 / 模型服务（别名：tgdiag）",
        usage="",
        permission=0,
        aliases=("tgdiag", "电报诊断"),
        params=(),
    )(run_diag)
    ctx.logger.info("[telegram] 已注册命令：/tg、/tg诊断（含别名）")


# ── 生命周期 ─────────────────────────────────────────────────────────


@plugin.on_load
async def on_load(ctx: Any) -> None:
    config = _config_of(ctx)
    runtime = _runtime(ctx, config)
    register_commands(ctx, config)
    ctx.logger.info(
        f"[telegram] 已加载：模型服务={runtime.ai_provider_label()}"
    )


@plugin.on_startup
async def on_startup(ctx: Any) -> None:
    config = _config_of(ctx)
    runtime = _runtime(ctx, config)
    await runtime.start()
    _provide_service(ctx, runtime)


@plugin.on_shutdown
async def on_shutdown(ctx: Any) -> None:
    global _RUNTIME
    runtime = _RUNTIME
    if runtime is None:
        return
    for merged in runtime.buffer.flush_all():
        with contextlib.suppress(Exception):
            await runtime.dispatch(merged)
    await runtime.stop()
    ctx.logger.info("[telegram] 已停止")
    _RUNTIME = None


def _provide_service(ctx: Any, runtime: TelegramRuntime) -> None:
    """把发送能力作为 telegram 服务开放给其它插件。"""
    services = getattr(ctx, "services", None)
    if services is None or not hasattr(services, "provide"):
        return

    class _Facade:
        version = plugin.version

        @staticmethod
        def status() -> str:
            return runtime.status_text()

        @staticmethod
        async def send_text(chat_id: int | str, text: str) -> None:
            await runtime.send_text(chat_id, text)

    try:
        services.provide("telegram", _Facade(), description="Telegram 直连 AI 的发送能力")
    except Exception as exc:  # noqa: BLE001
        runtime.log("debug", f"telegram 服务注册跳过：{type(exc).__name__}: {exc}")


__all__ = [
    "Config",
    "TelegramRuntime",
    "on_load",
    "on_shutdown",
    "on_startup",
    "plugin",
    "register_commands",
    "strip_wrapping_quotes",
]
