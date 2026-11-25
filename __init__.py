"""Telegram 桥接 —— NeoBot 插件。

把 Koishi 的 ``@koishijs/plugin-adapter-telegram``（``@satorijs/adapter-telegram``）
以**插件**形态移植到 NeoBot 2.1.x：自包含的 Telegram Bot API 客户端 + QQ ⇄ Telegram
双向桥接 + 全部快捷命令注册进宿主命令表（``/help`` 命令展示页可见）。

为什么是「桥接插件」而不是「新增一个协议适配器」：NeoBot 的适配器是宿主核心服务
（``service_registry`` 里 ``__host__`` 优先级最高，插件覆盖不了 ``adapter``），
AI 回复的出站固定走宿主 ``adapter.send``，通道只认 ``onebot`` / ``official``。
所以 Telegram 侧的收与发由本插件自己完成，QQ 侧只借用宿主的
``ctx.send_group`` / ``ctx.send_private`` 投递。

模块划分（纯逻辑与宿主解耦，便于单测）：

===================  =========================================================
``tg_api.py``        Bot API 客户端：代理 / 反代、重试退避、长轮询、文件下载
``tg_model.py``      Update 解码：实体解析、相册聚合、命令与回调
``tg_send.py``       出站渲染：HTML 转义、标签安全分段、媒体与按钮
``tg_bridge.py``     绑定存储、路由成型、回环防护
``__init__.py``      宿主接线：配置、生命周期、命令、消息处理
===================  =========================================================
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from neobot_modloader import MessageChain, Plugin

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
    from .tg_bridge import (
        Binding,
        BindingStore,
        LoopGuard,
        conversation_of,
        looks_like_command,
        qq_body_text,
        render_qq_to_tg,
        render_tg_to_qq,
    )
    from .tg_model import IncomingEvent, MediaGroupBuffer, decode_update, update_subtype
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
    from tg_bridge import (
        Binding,
        BindingStore,
        LoopGuard,
        conversation_of,
        looks_like_command,
        qq_body_text,
        render_qq_to_tg,
        render_tg_to_qq,
    )
    from tg_model import IncomingEvent, MediaGroupBuffer, decode_update, update_subtype
    from tg_send import PlanOptions, SendAction, build_actions, render_segments

# ── 配置 ─────────────────────────────────────────────────────────────


class Config(BaseModel):
    """插件配置；运行值在 data/plugins_data/telegram/config.toml。"""

    model_config = ConfigDict(extra="allow")

    enabled: bool = Field(default=True, description="总开关；关闭后不连 Telegram（命令仍可用）")
    token: str = Field(default="", description="BotFather 发的机器人令牌：123456:ABC-DEF...")
    endpoint: str = Field(default=DEFAULT_ENDPOINT, description="Bot API 终结点；国内直连不通时改为反代地址")
    reverse_proxy_url: str = Field(
        default="",
        description="反代基址（如 https://tg.example.workers.dev）；填了它优先于 endpoint，配合 extras/cloudflare-worker.js",
    )
    reverse_proxy_secret: str = Field(
        default="", description="反代路径密钥，与 Worker 的 PATH_SECRET 一致；Worker 没设密钥时留空"
    )
    proxy: str = Field(default="", description="网络代理，例如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080")
    protocol: Literal["polling", "webhook"] = Field(
        default="polling", description="通信方式；当前只实现 polling（webhook 需要宿主提供入站 HTTP 端点）"
    )
    polling_timeout: int = Field(default=25, ge=1, le=60, description="长轮询单次等待秒数")
    retry_times: int = Field(default=6, ge=0, description="初次连接失败的最大重试次数")
    retry_interval: float = Field(default=5.0, ge=0.5, description="断开后的重试间隔（秒）")
    allowed_updates: list[str] = Field(
        default_factory=list,
        description="只接收这些类型的更新；留空 = 全部（含 chat_member，修正 Koishi 侧的默认遗漏）",
    )
    parse_mode: Literal["html", "none"] = Field(default="html", description="发送时使用的解析格式")
    link_preview: bool = Field(default=True, description="发送链接时展示网页预览")
    chunk_size: int = Field(default=3500, ge=200, le=4096, description="长文本自动分段长度上限")
    prefix_from_tg: str = Field(
        default="[TG {name}] ", description="Telegram → QQ 的前缀；支持 {name} {chat} {id}"
    )
    prefix_from_qq: str = Field(
        default="[QQ {name}] ", description="QQ → Telegram 的前缀；支持 {name} {group} {id}"
    )
    forward_bot_messages: bool = Field(default=True, description="把 QQ 侧机器人自己的消息（含 AI 回复）转发到 Telegram")
    forward_user_messages: bool = Field(default=True, description="把 QQ 侧其他成员的消息转发到 Telegram")
    forward_commands: bool = Field(default=False, description="把 / 开头的命令也转发到 Telegram")
    ignore_telegram_bots: bool = Field(default=True, description="忽略 Telegram 侧其他机器人发的消息（防互刷）")
    admins: list[int | str] = Field(default_factory=list, description="额外管理员 QQ 号 / 官方 openid：可执行绑定与推送")
    allow_sub_admin: bool = Field(default=True, description="允许次级管理员执行绑定 / 推送类命令")


_PLUGIN_KWARGS: dict[str, Any] = dict(
    version="1.0.2",
    description="Telegram 桥接：移植 Koishi Telegram 适配器（长轮询 + QQ 双向互通）",
    usage="发送 @机器人 /tg 查看状态；/tg绑定 建立互通（/help 可查全部命令与别名）",
    author="NeoBot 第三方插件",
    config=Config,
)

try:
    # provides 是 NeoBot 2.1.0a15 才有的参数（插件服务声明）；老宿主上退化为不声明
    plugin = Plugin("telegram", provides=["telegram"], **_PLUGIN_KWARGS)
except TypeError:  # pragma: no cover - 仅老宿主走到这里
    plugin = Plugin("telegram", **_PLUGIN_KWARGS)

# ── 运行时 ─────────────────────────────────────────────────────────────


class TelegramRuntime:
    """插件运行时：一个实例持有 API 客户端、轮询器、绑定与计数器。"""

    def __init__(self, ctx: Any, config: Config) -> None:
        self.ctx = ctx
        self.config = config
        self.api: TelegramAPI | None = None
        self.poller: TelegramPoller | None = None
        self.store = BindingStore(Path(ctx.data_dir) / "bindings.json")
        self.buffer = MediaGroupBuffer(window=1.2)
        self.guard = LoopGuard(ttl=90.0)
        self.me: dict[str, Any] = {}
        self.bot_username = ""
        self.chats: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.started_at = 0.0
        self.counters: dict[str, int] = {
            "updates": 0,
            "tg_to_qq": 0,
            "qq_to_tg": 0,
            "qq_seen": 0,
            "qq_bot_messages": 0,
            "dropped_bot": 0,
            "dropped_echo": 0,
            "errors": 0,
        }
        self.last_error = ""
        self.state = "stopped"
        self._flush_task: asyncio.Task[None] | None = None
        self._watchdog: asyncio.Task[None] | None = None

    # ── 生命周期 ──

    @property
    def poll_options(self) -> PlanOptions:
        return PlanOptions(
            parse_mode="html" if self.config.parse_mode == "html" else "",
            link_preview=bool(self.config.link_preview),
            chunk_size=int(self.config.chunk_size),
        )

    def log(self, level: str, message: str, **fields: Any) -> None:
        logger = getattr(self.ctx, "logger", None)
        if logger is None:
            return
        try:
            getattr(logger, level)(message, **fields)
        except Exception:  # pragma: no cover - 日志失败不影响主流程
            pass

    async def start(self) -> None:
        """建立客户端并开始接收更新；失败只记日志，不阻断插件加载。"""
        self.store.load()
        if not self.config.enabled:
            self.state = "disabled"
            self.log("info", "Telegram 桥接已禁用（配置 enabled=false）")
            return
        token = (self.config.token or "").strip()
        if not token:
            self.state = "no-token"
            self.log("warning", "Telegram 未配置 token，插件待机；在面板「插件 → 配置」填入后重载")
            return
        if self.config.protocol != "polling":
            self.log(
                "warning",
                "当前版本只实现 polling；已按 polling 启动（webhook 需要宿主提供入站 HTTP 端点）",
                protocol=self.config.protocol,
            )
        endpoint, source = _resolve_endpoint(self.config)
        self.log(
            "info",
            "正在连接 Telegram",
            endpoint=_mask_endpoint(endpoint, self.config),
            source=source,
            proxy=(self.config.proxy or "未配置代理"),
        )
        try:
            self.api = TelegramAPI(
                token,
                endpoint=endpoint,
                proxy=self.config.proxy or None,
                logger=self.ctx.logger,
            )
            await self._login()
            await self._start_poller()
            self.log(
                "info",
                f"Telegram 已连接：@{self.bot_username or self.me.get('id')}"
                f"（id={self.me.get('id')}，代理={self.config.proxy or '无'}）",
            )
        except Exception as exc:  # noqa: BLE001 - 插件加载不能被网络问题打断
            self.state = "error"
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.log("warning", f"Telegram 连接异常：{self.last_error}")
            self.log("warning", "排查建议：" + _connection_hint(self.config))
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

    def _schedule_watchdog(self) -> None:
        """启动失败后按间隔重试，恢复后自动接上轮询（不用手动重载）。"""
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
                    self.log("debug", f"Telegram 仍不可达：{detail}")
                    continue
                self.log("info", f"Telegram 恢复连通：{detail}，正在启动轮询")
                try:
                    await self._login()
                    await self._start_poller()
                    self.log("info", f"Telegram 已连接：@{self.bot_username or self.me.get('id')}")
                except Exception as exc:  # noqa: BLE001
                    self.state = "error"
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    self.log("warning", f"恢复后启动轮询失败：{self.last_error}")
                return

        self._watchdog = asyncio.create_task(runner())

    async def stop(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._watchdog
            self._watchdog = None
        if self._flush_task is not None:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._flush_task
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

    # ── 入站（Telegram → QQ） ──

    async def handle_updates(self, updates: list[dict[str, Any]]) -> None:
        self.counters["updates"] += len(updates)
        for update in updates:
            self.log("debug", f"收到 Telegram 更新：{update_subtype(update)}")
            try:
                event = decode_update(update, bot_username=self.bot_username)
            except Exception as exc:  # noqa: BLE001 - 单条更新解析失败不影响后续
                self.counters["errors"] += 1
                self.log("warning", f"Telegram 更新解析失败：{type(exc).__name__}: {exc}")
                continue
            if event is None:
                continue
            self.note_chat(event)
            if event.media_group_id:
                # 相册：先入缓冲，等窗口到期由后台任务合并派发（不阻塞轮询）
                self.buffer.push(event)
                self._schedule_flush()
                continue
            await self.dispatch(event)

    def _schedule_flush(self) -> None:
        """相册窗口到期后把合并事件派发出去（不阻塞轮询循环）。"""
        if self._flush_task is not None and not self._flush_task.done():
            return

        async def runner() -> None:
            try:
                await asyncio.sleep(self.buffer.window + 0.05)
                for merged in self.buffer.flush_due():
                    await self.dispatch(merged)
            except asyncio.CancelledError:  # pragma: no cover - 停止路径
                raise
            except Exception as exc:  # noqa: BLE001
                self.counters["errors"] += 1
                self.log("warning", f"相册合并派发失败：{type(exc).__name__}: {exc}")

        self._flush_task = asyncio.create_task(runner())

    async def dispatch(self, event: IncomingEvent) -> None:
        if event.kind == "callback_query":
            await self._answer_callback(event)
        if event.kind in ("message", "message_updated"):
            if event.user_is_bot and self.config.ignore_telegram_bots:
                self.counters["dropped_bot"] += 1
                return
            await self.forward_to_qq(event)
        elif event.kind in ("chat_join_request", "my_chat_member"):
            await self.forward_notice_to_qq(event)
        else:
            self.log("debug", f"未桥接的 Telegram 事件类型：{event.kind}")

    async def _answer_callback(self, event: IncomingEvent) -> None:
        """回调必须应答，否则客户端一直转圈（与 Koishi 适配器同行为）。"""
        if not (self.api and event.callback_id):
            return
        try:
            await self.api.answer_callback_query(event.callback_id)
        except Exception as exc:  # noqa: BLE001
            self.log("debug", f"answerCallbackQuery 失败：{exc}")

    async def forward_to_qq(self, event: IncomingEvent) -> None:
        for binding in self.store.by_tg(event.chat_id):
            if not binding.tg_to_qq:
                continue
            text = render_tg_to_qq(event, prefix=self.config.prefix_from_tg, reply_text=event.reply_to_text)
            payload = MessageChain().text(text)
            self.guard.mark(text)
            try:
                if binding.qq_kind == "private":
                    await self.ctx.send_private(binding.qq_id, payload, proactive=False)
                else:
                    await self.ctx.send_group(binding.qq_id, payload, proactive=False)
                self.counters["tg_to_qq"] += 1
                self.log(
                    "info",
                    f"Telegram → QQ：{event.chat_title or event.chat_id} → {binding.qq_kind}:{binding.qq_id}",
                    chars=len(text),
                )
            except Exception as exc:  # noqa: BLE001
                self.counters["errors"] += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.log("warning", f"投递到 QQ 失败（{binding.qq_kind}:{binding.qq_id}）：{self.last_error}")

    async def forward_notice_to_qq(self, event: IncomingEvent) -> None:
        notice = ""
        if event.kind == "chat_join_request":
            notice = f"[TG] {event.join_request_user_name or event.join_request_user_id} 申请加入 {event.chat_title or event.chat_id}"
        elif event.kind == "my_chat_member" and event.my_chat_member_state:
            state = {"group-added": "被拉入", "group-deleted": "被移出"}.get(event.my_chat_member_state, "成员状态变化")
            notice = f"[TG] 机器人{state}会话 {event.chat_title or event.chat_id}"
        if not notice:
            return
        self.guard.mark(notice)
        for binding in self.store.by_tg(event.chat_id):
            if not binding.tg_to_qq:
                continue
            try:
                payload = MessageChain().text(notice)
                if binding.qq_kind == "private":
                    await self.ctx.send_private(binding.qq_id, payload, proactive=False)
                else:
                    await self.ctx.send_group(binding.qq_id, payload, proactive=False)
            except Exception as exc:  # noqa: BLE001
                self.log("warning", f"通知转发失败：{type(exc).__name__}: {exc}")

    # ── 出站（QQ → Telegram） ──

    async def forward_from_qq(self, event: dict[str, Any], message: Any = None) -> int:
        """把一条 QQ 事件转发到绑定的 Telegram 会话，返回转发条数。"""
        self.counters["qq_seen"] += 1
        kind, conv_id = conversation_of(event)
        if not conv_id:
            return 0
        bindings = self.store.by_qq(kind, conv_id)
        if not bindings:
            return 0
        self_ids = self._self_ids(event)
        user_id = str(event.get("user_id") or "")
        is_bot_message = str(event.get("post_type") or "") == "message_sent" or (user_id and user_id in self_ids)
        if is_bot_message:
            self.counters["qq_bot_messages"] += 1
            if not self.config.forward_bot_messages:
                return 0
        elif not self.config.forward_user_messages:
            return 0

        text = render_qq_to_tg(event, prefix=self.config.prefix_from_qq, strip_at=self_ids)
        if not text.strip():
            return 0
        # 命令判断必须看正文：前缀（如 "[QQ 小明] 小明: "）会让整串不再以 "/" 开头
        if not self.config.forward_commands and looks_like_command(qq_body_text(event, strip_at=self_ids)):
            return 0
        if self.guard.is_echo(text):
            self.counters["dropped_echo"] += 1
            self.log("debug", "命中回环防护，跳过转发到 Telegram")
            return 0
        self.guard.mark(text)

        sent = 0
        for binding in bindings:
            if not binding.qq_to_tg:
                continue
            try:
                await self.send_text(binding.tg_chat_id, text)
                sent += 1
                self.counters["qq_to_tg"] += 1
            except Exception as exc:  # noqa: BLE001
                self.counters["errors"] += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.log("warning", f"转发到 Telegram 失败（{binding.tg_chat_id}）：{self.last_error}")
        return sent

    async def send_text(self, chat_id: int | str, text: str, *, reply_to: int = 0) -> list[Any]:
        """向 Telegram 会话发纯文本（自动分段）。"""
        if self.api is None:
            raise RuntimeError("Telegram 未连接（检查 token / 代理 / endpoint）")
        rendered = render_segments([{"type": "text", "data": {"text": text}}], reply_to_message_id=reply_to)
        return await self._execute(build_actions(chat_id, rendered, options=self.poll_options))

    async def send_actions(self, actions: list[SendAction]) -> list[Any]:
        return await self._execute(actions)

    async def _execute(self, actions: list[SendAction]) -> list[Any]:
        assert self.api is not None
        results: list[Any] = []
        for action in actions:
            try:
                if action.files:
                    results.append(await self.api.call(action.method, action.params, files=action.files))
                else:
                    results.append(await self.api.call(action.method, action.params))
            except (TelegramError, TelegramTransportError, TelegramRetryAfter) as exc:
                self.counters["errors"] += 1
                self.last_error = str(exc)
                raise
        return results

    # ── 辅助 ──

    def _self_ids(self, event: dict[str, Any]) -> set[str]:
        ids: set[str] = set()
        for candidate in (
            event.get("self_id"),
            getattr(getattr(self.ctx, "adapter", None), "self_id", None),
        ):
            if candidate is not None and str(candidate):
                ids.add(str(candidate))
        return ids

    def note_chat(self, event: IncomingEvent) -> None:
        if not event.chat_id:
            return
        self.chats[event.chat_id] = {
            "id": event.chat_id,
            "type": event.chat_type,
            "title": event.chat_title,
            "last_seen": time.time(),
        }
        self.chats.move_to_end(event.chat_id)
        while len(self.chats) > 50:
            self.chats.popitem(last=False)

    def bind_chat(self, *, tg_chat_id: str, qq_kind: str, qq_id: str, note: str = "") -> Binding:
        known = self.chats.get(str(tg_chat_id), {})
        binding = Binding(
            tg_chat_id=str(tg_chat_id),
            qq_kind=qq_kind,
            qq_id=str(qq_id),
            tg_title=str(known.get("title") or ""),
            note=note,
        )
        self.store.add(binding)
        self.store.save()
        return binding

    def status_text(self) -> str:
        lines = ["Telegram 桥接状态"]
        lines.append(f"· 运行状态：{self.state}")
        lines.append(f"· 机器人：@{self.bot_username}" if self.bot_username else "· 机器人：未连接")
        endpoint, source = _resolve_endpoint(self.config)
        lines.append(f"· 终结点：{_mask_endpoint(endpoint, self.config)}（来源：{_source_label(source)}）")
        lines.append(f"· 代理：{self.config.proxy or '未设置'}")
        lines.append(f"· 绑定：{len(self.store.bindings)} 条")
        if self.poller is not None:
            stats = self.poller.stats
            lines.append(
                f"· 轮询：offset={stats.offset} 收到={stats.received} 失败={stats.failures}"
                + (f" 最近错误={stats.last_error}" if stats.last_error else "")
            )
        if self.last_error:
            lines.append(f"· 最近错误：{self.last_error}")
        return "\n".join(lines)

    def diag_text(self) -> str:
        token = (self.config.token or "").strip()
        masked = f"{token.split(':')[0]}:***" if ":" in token else ("未配置" if not token else "格式异常")
        endpoint, source = _resolve_endpoint(self.config)
        lines = [
            "Telegram 链路诊断",
            f"· 版本：{plugin.version}（min_neobot_version 2.0.0）",
            f"· token：{masked}",
            f"· endpoint：{_mask_endpoint(endpoint, self.config)}（来源：{_source_label(source)}）",
            f"· proxy：{self.config.proxy or '未设置'}",
            f"· protocol：{self.config.protocol}（当前版本实际按 polling 运行）",
            f"· allowed_updates：{len(self.config.allowed_updates) or '默认（含 chat_member）'}",
            f"· 相册窗口：{self.buffer.window}s；回环防护 TTL：{self.guard.ttl}s",
            "· 计数器：" + "，".join(f"{key}={value}" for key, value in self.counters.items()),
        ]
        if self.poller is not None:
            stats = self.poller.stats
            lines.append(
                f"· 已摘除的 webhook：{len(stats.removals)}"
                + (f"（最近：{stats.removals[-1]}）" if stats.removals else "")
            )
            lines.append(f"· 连续失败：{stats.consecutive_failures}；最后更新：{stats.last_update_at or '无'}")
        if self.counters["qq_bot_messages"] == 0 and self.config.forward_bot_messages:
            lines.append(
                "· 提示：还没观察到 QQ 侧机器人自己的消息。AI 回复能否回传取决于协议端是否上报"
                "（OneBot 的 post_type=message_sent / NapCat 的「上报自身消息」开关）。"
            )
        return "\n".join(lines)

    async def diag_report(self) -> str:
        """诊断 + 一次真实连通性探测（/tg诊断 用）。"""
        lines = [self.diag_text()]
        if self.state in ("no-token", "disabled"):
            lines.append("· 连通性探测：跳过（未配置 token 或已禁用）")
            return "\n".join(lines)
        if self.api is None:
            lines.append(f"· 客户端：未建立（state={self.state}）")
            lines.append("· 建议：" + _connection_hint(self.config))
            return "\n".join(lines)
        ok, detail = await self.api.probe()
        lines.append(f"· 连通性探测：{'通' if ok else '不通'} —— {detail}")
        if not ok:
            lines.append("· 建议：" + _connection_hint(self.config))
        return "\n".join(lines)


# 插件级单例：NeoBot 的插件是「一个插件一个运行时状态」的模型，与 bili_monitor 同风格。
_RUNTIME: TelegramRuntime | None = None


def _resolve_endpoint(config: Config) -> tuple[str, str]:
    """算出实际使用的 API 基址，返回 (基址, 来源)。

    优先级：``reverse_proxy_url``（+ ``reverse_proxy_secret``）> ``endpoint`` > 官方地址。
    反代基址与密钥都会做斜杠归一化，避免拼出 ``//`` 或 ``/secret/bot`` 这类怪路径。
    """
    reverse = (config.reverse_proxy_url or "").strip().rstrip("/")
    if reverse:
        secret = (config.reverse_proxy_secret or "").strip().strip("/")
        return (f"{reverse}/{secret}" if secret else reverse), "reverse_proxy"
    endpoint = (config.endpoint or "").strip().rstrip("/")
    if endpoint and endpoint != DEFAULT_ENDPOINT.rstrip("/"):
        return endpoint, "config"
    return DEFAULT_ENDPOINT.rstrip("/"), "default"


def _mask_endpoint(endpoint: str, config: Config) -> str:
    """把反代密钥从展示用的地址里隐去（日志、诊断都不该出现明文密钥）。"""
    secret = (config.reverse_proxy_secret or "").strip().strip("/")
    if secret and secret in endpoint:
        return endpoint.replace(secret, "***")
    return endpoint


def _source_label(source: str) -> str:
    return {"reverse_proxy": "反代", "config": "endpoint 配置", "default": "官方默认"}.get(source, source)


def _connection_hint(config: Config) -> str:
    """连接失败时给出可执行的排查建议（覆盖最常见的几种情况）。"""
    endpoint, source = _resolve_endpoint(config)
    proxy = (config.proxy or "").strip()
    tips: list[str] = []
    if proxy:
        tips.append(
            f"已配置代理 {proxy}：确认代理进程在运行、端口正确；"
            "若 NeoBot 跑在容器 / WSL 里，127.0.0.1 指向容器自身，应改用宿主地址。"
        )
        if proxy.lower().startswith("socks"):
            tips.append("socks5:// 需要 socksio（依赖 httpx[socks]），缺依赖会直接连不上。")
    if source == "reverse_proxy":
        tips.append(
            f"已配置反代 {_mask_endpoint(endpoint, config)}：确认 Worker 已部署、"
            "路径密钥与插件填的一致；反代要同时转发 /bot<token>/<method> 与 "
            "/file/bot<token>/...（extras/cloudflare-worker.js 已经实现）。"
        )
    elif source == "config":
        tips.append(
            f"已配置 endpoint {endpoint}：确认宿主机能访问它，且它把 "
            "/bot<token>/<method> 与 /file/bot<token>/... 都转发到 api.telegram.org。"
        )
    elif not proxy:
        tips.append(
            "当前是直连 api.telegram.org 且未配置代理或反代：国内网络通常无法连通。"
            "可填 reverse_proxy_url（配合 extras/cloudflare-worker.js，无需本地代理），或填 proxy。"
        )
    else:
        tips.append("当前用默认 api.telegram.org + 代理：若仍不通，可改用 reverse_proxy_url 走反代。")
    return " ".join(tips)


def _config_of(ctx: Any) -> Config:
    return Config.model_validate(dict(getattr(ctx, "config", None) or {}))


def _runtime(ctx: Any, config: Config | None = None) -> TelegramRuntime:
    global _RUNTIME
    if _RUNTIME is None or _RUNTIME.ctx is not ctx:
        _RUNTIME = TelegramRuntime(ctx, config or _config_of(ctx))
    elif config is not None:
        _RUNTIME.config = config
    return _RUNTIME


# ── 命令（全部注册进宿主命令表 → /help 展示） ──────────────────────────


def _admin_required(config: Config) -> int:
    return 1 if config.allow_sub_admin else 2


def _is_admin(ctx: Any, config: Config, user_id: Any) -> bool:
    """插件 admins 名单里的用户直接放行（宿主权限由 permission 等级兜底）。"""
    target = str(user_id)
    return any(str(item) == target for item in config.admins)


def _may_admin(ctx: Any, config: Config, command_context: Any, user_id: Any) -> bool:
    """管理类命令的准入：插件 admins 名单，或宿主权限达到管理员等级。

    命令统一用 ``permission=0`` 注册（沿用本工作区 group_admin 的做法），
    权限在处理器里自查：这样才能让「不在宿主管理员列表、但被插件 admins 显式
    授权」的号也能用，同时不放过普通成员。
    """
    if _is_admin(ctx, config, user_id):
        return True
    service = getattr(command_context, "service", None)
    permissions = getattr(service, "permissions", None)
    can = getattr(permissions, "can", None)
    if not callable(can):
        return False
    try:
        return bool(can(str(user_id), _admin_required(config)))
    except Exception:  # noqa: BLE001 - 宿主权限实现变化时按拒绝处理（fail-closed）
        return False


def _command_context_ids(command_context: Any) -> tuple[str, str]:
    kind = "private" if str(getattr(command_context, "kind", "group")) in ("private", "friend") else "group"
    return kind, str(getattr(command_context, "conv_id", "") or "")


def register_commands(ctx: Any, config: Config) -> None:
    """把全部快捷命令注册进 ``ctx.app_commands``，/help 列表与详情都能看到。"""
    registrar = getattr(ctx, "app_commands", None)
    if registrar is None or not getattr(registrar, "available", False):
        ctx.logger.warning("[telegram] 宿主命令表不可用，命令未注册（/help 里不会出现本插件命令）")
        return

    def handler(action: str):
        async def run(command_context: Any) -> str:
            runtime = _runtime(ctx)
            return await _run_command(ctx, runtime, action, command_context)

        return run

    specs: tuple[dict[str, Any], ...] = (
        {
            "name": "tg",
            "action": "status",
            "description": "Telegram 桥接状态总览（别名：telegram / 电报）",
            "usage": "[bind|unbind|list|send|chats|diag|reload]",
            "aliases": ("telegram", "电报"),
            "params": (
                ("bind", "把当前 QQ 会话与 Telegram 会话绑定：/tg bind <TG会话ID>"),
                ("unbind", "解除绑定：/tg unbind [TG会话ID]"),
                ("list", "列出全部绑定"),
                ("send", "向绑定的 Telegram 会话推送：/tg send <文本>"),
                ("chats", "列出最近收到消息的 Telegram 会话与 ID"),
                ("diag", "链路诊断（token 脱敏 / 计数 / 轮询状态）"),
                ("reload", "重连 Telegram"),
            ),
            "permission": 0,
        },
        {
            "name": "tg绑定",
            "action": "bind",
            "description": "把当前 QQ 会话与 Telegram 会话绑定（需管理员；别名：tgbind）",
            "usage": "<TG会话ID> [group|private] [QQ号]",
            "aliases": ("tgbind",),
            "params": (
                ("TG会话ID", "用 /tg chats 查看，通常是 -100 开头的群 ID 或用户 ID"),
                ("group|private", "绑定到群还是私聊；省略按当前会话"),
            ),
            "permission": 0,
        },
        {
            "name": "tg解绑",
            "action": "unbind",
            "description": "解除 Telegram 绑定（需管理员；别名：tgunbind）",
            "usage": "[TG会话ID]",
            "aliases": ("tgunbind",),
            "params": (("TG会话ID", "省略时解除当前 QQ 会话的全部绑定"),),
            "permission": 0,
        },
        {
            "name": "tg列表",
            "action": "list",
            "description": "列出全部 Telegram 绑定（别名：tglist / 电报列表）",
            "usage": "",
            "aliases": ("tglist", "电报列表"),
            "params": (),
            "permission": 0,
        },
        {
            "name": "tg推送",
            "action": "send",
            "description": "向绑定的 Telegram 会话推送文本（需管理员；别名：tgsend / 推送电报）",
            "usage": "<文本>",
            "aliases": ("tgsend", "推送电报"),
            "params": (("文本", "要推送的内容；发给当前 QQ 会话绑定的所有 Telegram 会话"),),
            "permission": 0,
        },
        {
            "name": "tg会话",
            "action": "chats",
            "description": "列出最近收到消息的 Telegram 会话与 ID（别名：tgchats）",
            "usage": "",
            "aliases": ("tgchats",),
            "params": (),
            "permission": 0,
        },
        {
            "name": "tg诊断",
            "action": "diag",
            "description": "Telegram 链路诊断：token / 代理 / 轮询 / 计数（别名：tgdiag）",
            "usage": "",
            "aliases": ("tgdiag", "电报诊断"),
            "params": (),
            "permission": 0,
        },
        {
            "name": "tg重连",
            "action": "reload",
            "description": "重连 Telegram（需管理员；别名：tgreload）",
            "usage": "",
            "aliases": ("tgreload",),
            "params": (),
            "permission": 0,
        },
    )

    for spec in specs:
        registrar.register(
            spec["name"],
            description=spec["description"],
            usage=spec["usage"],
            permission=spec["permission"],
            aliases=spec["aliases"],
            params=spec["params"],
        )(handler(spec["action"]))
    names = "、".join(f"/{spec['name']}" for spec in specs)
    ctx.logger.info(f"[telegram] 已注册命令：{names}（含别名，/help 可查）")


def _parse_binding_args(args: str, command_context: Any) -> tuple[str, str, str]:
    """解析 `/tg bind <TG会话ID> [group|private] [QQ号]`。"""
    parts = [item for item in str(args or "").split() if item]
    tg_chat_id = parts[0] if parts else ""
    kind = ""
    qq_id = ""
    for item in parts[1:]:
        lowered = item.casefold()
        if lowered in ("group", "群"):
            kind = "group"
        elif lowered in ("private", "friend", "私聊"):
            kind = "private"
        elif item.isdigit() or item.replace("-", "").isdigit():
            qq_id = item
    if not kind or not qq_id:
        fallback_kind, fallback_id = _command_context_ids(command_context)
        kind = kind or fallback_kind
        qq_id = qq_id or fallback_id
    return tg_chat_id, kind, qq_id


async def _run_command(ctx: Any, runtime: TelegramRuntime, action: str, command_context: Any) -> str:
    config = runtime.config
    user_id = getattr(command_context, "user_id", None)
    if action in ("bind", "unbind", "send", "reload") and not _may_admin(
        ctx, config, command_context, user_id
    ):
        return "权限不足：需要宿主管理员，或在插件配置的 admins 名单里。"
    args = getattr(command_context, "raw_args", "") or ""

    if action == "status":
        stripped = args.strip()
        if stripped:
            head = stripped.split()[0].casefold()
            mapping = {
                "bind": "bind",
                "绑定": "bind",
                "unbind": "unbind",
                "解绑": "unbind",
                "list": "list",
                "列表": "list",
                "send": "send",
                "推送": "send",
                "chats": "chats",
                "会话": "chats",
                "diag": "diag",
                "诊断": "diag",
                "reload": "reload",
                "重连": "reload",
            }
            target = mapping.get(head)
            if target:
                remainder = stripped[len(stripped.split()[0]) :].strip()
                return await _run_command(ctx, runtime, target, _WithArgs(command_context, remainder))
        return runtime.status_text()

    if action == "list":
        bindings = runtime.store.bindings
        if not bindings:
            return "还没有任何 Telegram 绑定。先让对方给机器人发一条消息，用 /tg会话 拿到 ID，再 /tg绑定 <ID>。"
        lines = ["Telegram 绑定（%d 条）：" % len(bindings)]
        for item in bindings:
            lines.append("· " + item.describe() + (f" 备注：{item.note}" if item.note else ""))
        return "\n".join(lines)

    if action == "chats":
        if not runtime.chats:
            return "最近还没有收到 Telegram 消息。确认对方已给机器人发消息、且插件状态为 online。"
        lines = ["最近的 Telegram 会话："]
        for chat in reversed(list(runtime.chats.values())):
            lines.append(f"· {chat.get('title') or chat.get('id')}（{chat.get('type')}，id={chat.get('id')}）")
        return "\n".join(lines)

    if action == "diag":
        return await runtime.diag_report()

    if action == "reload":
        try:
            return "已重连。\n" + await runtime.reconnect()
        except Exception as exc:  # noqa: BLE001
            return f"重连失败：{type(exc).__name__}: {exc}"

    if action == "bind":
        tg_chat_id, kind, qq_id = _parse_binding_args(args, command_context)
        if not tg_chat_id or not qq_id:
            return "用法：/tg绑定 <TG会话ID> [group|private] [QQ号]。先用 /tg会话 查看 TG 会话 ID。"
        try:
            binding = runtime.bind_chat(tg_chat_id=tg_chat_id, qq_kind=kind, qq_id=qq_id)
        except ValueError as exc:
            return f"绑定失败：{exc}"
        return f"已绑定：{binding.describe()}"

    if action == "unbind":
        tg_chat_id = (args.split() or [""])[0]
        if not tg_chat_id:
            kind, qq_id = _command_context_ids(command_context)
            removed = runtime.store.remove(qq_kind=kind, qq_id=qq_id)
        else:
            removed = runtime.store.remove(tg_chat_id=tg_chat_id)
        if removed:
            runtime.store.save()
        return f"已解除 {removed} 条绑定。" if removed else "没有找到匹配的绑定。"

    if action == "send":
        text = args.strip()
        if not text:
            return "用法：/tg推送 <文本>"
        kind, qq_id = _command_context_ids(command_context)
        targets = [item for item in runtime.store.by_qq(kind, qq_id) if item.qq_to_tg or item.tg_to_qq]
        if not targets:
            return "当前 QQ 会话还没有绑定 Telegram 会话，先用 /tg绑定 <TG会话ID>。"
        sent = 0
        failed: list[str] = []
        for binding in targets:
            try:
                await runtime.send_text(binding.tg_chat_id, text)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{binding.tg_chat_id}: {type(exc).__name__}")
        reply = f"已推送到 {sent} 个 Telegram 会话。"
        if failed:
            reply += " 失败：" + "；".join(failed)
        return reply

    return runtime.status_text()


class _WithArgs:
    """把「子命令 + 剩余参数」包成命令上下文（复用同一套解析）。"""

    def __init__(self, command_context: Any, raw_args: str) -> None:
        self._inner = command_context
        self.raw_args = raw_args

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)


# ── 宿主消息订阅（QQ → Telegram） ────────────────────────────────────


@plugin.message(priority=60, block=False)
async def bridge_qq_message(message: Any, event: dict[str, Any], ctx: Any, config: Config) -> None:
    """QQ 侧消息 → Telegram。

    只在消息属于「已绑定的 QQ 会话」时转发；机器人自己的消息按 ``forward_bot_messages``
    决定（协议端上报 ``message_sent`` 时 AI 回复也能过桥）。
    """
    runtime = _runtime(ctx, config)
    if not config.enabled or runtime.api is None:
        return
    try:
        await runtime.forward_from_qq(event or {})
    except Exception as exc:  # noqa: BLE001 - 转发失败不能影响宿主消息管线
        runtime.counters["errors"] += 1
        runtime.log("warning", f"QQ → Telegram 转发异常：{type(exc).__name__}: {exc}")


# ── 生命周期 ─────────────────────────────────────────────────────────


@plugin.on_load
async def on_load(ctx: Any) -> None:
    config = _config_of(ctx)
    runtime = _runtime(ctx, config)
    runtime.store.load()
    register_commands(ctx, config)
    ctx.logger.info(
        f"[telegram] 已加载：enabled={config.enabled} 绑定={len(runtime.store.bindings)} 条"
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
    ctx.logger.info("[telegram] 已停止（轮询与客户端已释放）")
    _RUNTIME = None


def _provide_service(ctx: Any, runtime: TelegramRuntime) -> None:
    """把发送能力作为 ``telegram`` 服务开放给其它插件（a15+ 的服务注册表）。"""
    services = getattr(ctx, "services", None)
    if services is None or not hasattr(services, "provide"):
        return

    class _Facade:
        """其它插件可用的最小门面：``services.require("telegram")``。"""

        version = plugin.version

        @staticmethod
        def status() -> str:
            return runtime.status_text()

        @staticmethod
        async def send_text(chat_id: int | str, text: str) -> None:
            await runtime.send_text(chat_id, text)

        @staticmethod
        def bindings() -> list[Binding]:
            return runtime.store.bindings

    try:
        services.provide("telegram", _Facade(), description="Telegram 桥接发送能力")
    except Exception as exc:  # noqa: BLE001 - 老宿主没有服务注册表时忽略
        runtime.log("debug", f"telegram 服务注册跳过：{type(exc).__name__}: {exc}")


__all__ = [
    "Config",
    "TelegramRuntime",
    "bridge_qq_message",
    "on_load",
    "on_shutdown",
    "on_startup",
    "plugin",
    "register_commands",
]
