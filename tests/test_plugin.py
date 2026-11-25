"""插件级用例：命令注册（/help 展示）、管理准入、桥接路由、状态文本。

这些用例需要 ``neobot_modloader``（用 PYTHONPATH 指向 NeoBot 的 packages/*/src）；
缺失时 ``_loader.load_plugin`` 会 skip。
"""

from __future__ import annotations

import asyncio
import pathlib
from types import SimpleNamespace

from _loader import load_plugin

GROUP = 123456789
ADMIN = 10001
MEMBER = 20001
TG_GROUP = "-1001234567890"


class Logger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, message, **fields):
        self.lines.append(f"INFO {message}")

    def warning(self, message, **fields):
        self.lines.append(f"WARN {message}")

    def error(self, message, **fields):
        self.lines.append(f"ERROR {message}")

    def debug(self, message, **fields):
        self.lines.append(f"DEBUG {message}")

    def text(self) -> str:
        return "\n".join(self.lines)


class AppCommands:
    available = True

    def __init__(self) -> None:
        self.commands: dict[str, SimpleNamespace] = {}

    def register(self, name, *, description="", usage="", permission=0, aliases=(), params=()):
        def decorate(handler):
            self.commands[name] = SimpleNamespace(
                name=name,
                handler=handler,
                description=description,
                usage=usage,
                permission=permission,
                aliases=tuple(aliases),
                params=tuple(params),
            )
            return handler

        return decorate

    def by_alias(self, alias: str) -> SimpleNamespace | None:
        for command in self.commands.values():
            if alias == command.name or alias in command.aliases:
                return command
        return None


class Permissions:
    def __init__(self, admins: set[str]) -> None:
        self.admins = admins

    def can(self, user_id, level):
        return str(user_id) in self.admins and level >= 1


class Adapter:
    self_id = 99999


class Services:
    def __init__(self) -> None:
        self.provided: dict[str, object] = {}

    def provide(self, name, service, **kwargs):
        self.provided[name] = service

    def get(self, name, default=None):
        return self.provided.get(name, default)


class Context:
    def __init__(self, config: dict, *, host_admins: set[str] | None = None) -> None:
        self.config = config
        self.data_dir = pathlib.Path(config.get("_data_dir", "."))
        self.logger = Logger()
        self.app_commands = AppCommands()
        self.adapter = Adapter()
        self.services = Services()
        self.sent: list[tuple[str, str, str, str]] = []
        self._host_admins = host_admins or set()

    async def send_group(self, group_id, message, *, proactive=False):
        self.sent.append(("group", str(group_id), _text_of(message), str(proactive)))
        return {"status": "ok"}

    async def send_private(self, user_id, message, *, proactive=False):
        self.sent.append(("private", str(user_id), _text_of(message), str(proactive)))
        return {"status": "ok"}


def _text_of(payload) -> str:
    """从 MessageChain 里取纯文本；2.0.1 没有 plain_text，所以要逐级回退。"""
    for attr in ("plain_text", "text", "content"):
        value = getattr(payload, attr, None)
        if isinstance(value, str) and value:
            return value
    segments = getattr(payload, "message", None) or getattr(payload, "segments", None) or ()
    parts: list[str] = []
    for segment in segments:
        data = getattr(segment, "data", None)
        if data is None and isinstance(segment, dict):
            data = segment.get("data")
        if isinstance(data, dict):
            parts.append(str(data.get("text") or ""))
    joined = "".join(parts)
    return joined or str(payload)


class FakeAPI:
    """替代 TelegramAPI：记录 sendMessage 等调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.answers: list[str] = []

    async def call(self, method, params=None, *, files=None, retries=None):
        self.calls.append((method, dict(params or {})))
        return {"message_id": len(self.calls)}

    async def answer_callback_query(self, callback_id, **extra):
        self.answers.append(callback_id)
        return True

    async def close(self):
        return None


def make_runtime(config: dict, *, host_admins: set[str] | None = None):
    module = load_plugin()
    ctx = Context(config, host_admins=host_admins)
    cfg = module.Config.model_validate(config)
    runtime = module.TelegramRuntime(ctx, cfg)
    runtime.api = FakeAPI()
    return module, ctx, runtime


def command_context(
    *, raw_args: str = "", user_id: int = MEMBER, kind: str = "group", host_admins: set[str] | None = None
):
    service = SimpleNamespace(permissions=Permissions(host_admins or set()))
    return SimpleNamespace(
        kind=kind,
        conv_id=str(GROUP),
        user_id=user_id,
        raw_args=raw_args,
        args=raw_args.split(),
        command=SimpleNamespace(name="tg"),
        service=service,
        replies=[],
    )


# ── 命令展示页（/help） ──────────────────────────────────────────────


def test_all_shortcut_commands_registered_with_help_metadata(scratch):
    module, ctx, _ = make_runtime({"_data_dir": str(scratch), "token": "1:abc"})
    module.register_commands(ctx, module.Config.model_validate({"token": "1:abc"}))
    commands = ctx.app_commands.commands
    # 8 条快捷命令全部进宿主命令表
    assert set(commands) == {"tg", "tg绑定", "tg解绑", "tg列表", "tg推送", "tg会话", "tg诊断", "tg重连"}
    for name, command in commands.items():
        assert command.description, f"{name} 缺少 description"
    # 带参数的命令要有 usage 或 params，/help 详情页才不是空的
    for name in ("tg", "tg绑定", "tg解绑", "tg推送"):
        command = commands[name]
        assert command.usage or command.params, f"{name} 既没有 usage 也没有 params"
    # 别名都解析到真实命令（/help 里同一条，可直接当快捷命令用）
    assert ctx.app_commands.by_alias("tgbind").name == "tg绑定"
    assert ctx.app_commands.by_alias("telegram").name == "tg"
    assert ctx.app_commands.by_alias("电报").name == "tg"
    assert ctx.app_commands.by_alias("tgsend").name == "tg推送"
    assert ctx.app_commands.by_alias("tgdiag").name == "tg诊断"
    assert ctx.app_commands.by_alias("tgreload").name == "tg重连"
    # 绑定类命令自己把关权限，所以注册等级是 0（描述里写明需管理员）
    assert ctx.app_commands.by_alias("tgbind").permission == 0
    assert "需管理员" in ctx.app_commands.by_alias("tgbind").description


def test_register_commands_degrades_without_registrar(scratch):
    module, ctx, _ = make_runtime({"_data_dir": str(scratch)})
    ctx.app_commands = None
    module.register_commands(ctx, module.Config.model_validate({}))
    assert "命令未注册" in ctx.logger.text()


# ── 管理准入 ─────────────────────────────────────────────────────────


def test_may_admin_accepts_plugin_admins_and_host_admins(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch), "admins": [ADMIN]})
    assert module._may_admin(ctx, runtime.config, command_context(user_id=ADMIN), ADMIN) is True
    host_ctx = command_context(user_id=MEMBER, host_admins={str(MEMBER)})
    assert module._may_admin(ctx, runtime.config, host_ctx, MEMBER) is True
    assert module._may_admin(ctx, runtime.config, command_context(user_id=MEMBER), MEMBER) is False


def test_admin_commands_reject_member(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch), "admins": [ADMIN]})
    reply = asyncio.run(module._run_command(ctx, runtime, "bind", command_context(raw_args="-100", user_id=MEMBER)))
    assert "权限不足" in reply


# ── 命令行为 ─────────────────────────────────────────────────────────


def test_bind_list_unbind_and_send_flow(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch), "admins": [ADMIN]})
    runtime.chats[TG_GROUP] = {"id": TG_GROUP, "type": "supergroup", "title": "TG群", "last_seen": 0}

    bound = asyncio.run(module._run_command(ctx, runtime, "bind", command_context(raw_args=TG_GROUP, user_id=ADMIN)))
    assert "已绑定" in bound
    assert runtime.store.bindings[0].qq_kind == "group"
    assert runtime.store.bindings[0].tg_title == "TG群"

    listed = asyncio.run(module._run_command(ctx, runtime, "list", command_context(user_id=ADMIN)))
    assert "TG群" in listed

    sent = asyncio.run(module._run_command(ctx, runtime, "send", command_context(raw_args="部署完成", user_id=ADMIN)))
    assert "已推送到 1 个 Telegram 会话" in sent
    assert runtime.api.calls[-1][0] == "sendMessage"
    assert runtime.api.calls[-1][1]["chat_id"] == TG_GROUP
    assert "部署完成" in runtime.api.calls[-1][1]["text"]

    removed = asyncio.run(module._run_command(ctx, runtime, "unbind", command_context(raw_args=TG_GROUP, user_id=ADMIN)))
    assert "已解除 1 条绑定" in removed


def test_send_without_binding_hints(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch), "admins": [ADMIN]})
    reply = asyncio.run(module._run_command(ctx, runtime, "send", command_context(raw_args="x", user_id=ADMIN)))
    assert "还没有绑定" in reply


def test_status_subcommand_dispatch_and_chats(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch), "token": "123456:abc"})
    runtime.chats[TG_GROUP] = {"id": TG_GROUP, "type": "supergroup", "title": "TG群", "last_seen": 0}
    chats = asyncio.run(module._run_command(ctx, runtime, "status", command_context(raw_args="chats")))
    assert "TG群" in chats and TG_GROUP in chats
    status = asyncio.run(module._run_command(ctx, runtime, "status", command_context()))
    assert "Telegram 桥接状态" in status and "绑定：0 条" in status


def test_status_and_diag_mask_token(scratch):
    module, ctx, runtime = make_runtime(
        {"_data_dir": str(scratch), "token": "123456:secret", "proxy": "http://127.0.0.1:7890"}
    )
    diag = runtime.diag_text()
    assert "123456:***" in diag
    assert "secret" not in diag
    assert "proxy：http://127.0.0.1:7890" in diag
    assert "计数器" in diag


# ── 桥接路由 ─────────────────────────────────────────────────────────


def qq_event(*, user_id=MEMBER, text="你好", message_type="group"):
    return {
        "post_type": "message",
        "message_type": message_type,
        "group_id": GROUP if message_type == "group" else 0,
        "user_id": user_id,
        "self_id": Adapter.self_id,
        "sender": {"card": "小明", "nickname": "小明"},
        "message": [{"type": "text", "data": {"text": text}}],
    }


def test_forward_from_qq_requires_binding(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch)})
    assert asyncio.run(runtime.forward_from_qq(qq_event())) == 0
    assert runtime.api.calls == []


def test_forward_from_qq_user_message(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch), "prefix_from_qq": "[QQ {name}] "})
    runtime.bind_chat(tg_chat_id=TG_GROUP, qq_kind="group", qq_id=str(GROUP))
    assert asyncio.run(runtime.forward_from_qq(qq_event())) == 1
    method, params = runtime.api.calls[-1]
    assert method == "sendMessage" and params["chat_id"] == TG_GROUP
    assert "[QQ 小明] 小明: 你好" in params["text"]


def test_forward_from_qq_filters_bot_commands_and_echo(scratch):
    module, ctx, runtime = make_runtime(
        {"_data_dir": str(scratch), "forward_bot_messages": False, "forward_commands": False}
    )
    runtime.bind_chat(tg_chat_id=TG_GROUP, qq_kind="group", qq_id=str(GROUP))
    # 机器人自己的消息
    assert asyncio.run(runtime.forward_from_qq(qq_event(user_id=Adapter.self_id))) == 0
    # 命令
    assert asyncio.run(runtime.forward_from_qq(qq_event(text="/help"))) == 0
    # 回环：刚转发过的内容不再回传
    runtime.guard.mark("[QQ 小明] 小明: 复读")
    assert asyncio.run(runtime.forward_from_qq(qq_event(text="复读"))) == 0
    assert runtime.counters["dropped_echo"] == 1
    assert runtime.api.calls == []


def test_forward_from_qq_bot_message_when_enabled(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch), "forward_bot_messages": True})
    runtime.bind_chat(tg_chat_id=TG_GROUP, qq_kind="group", qq_id=str(GROUP))
    assert asyncio.run(runtime.forward_from_qq(qq_event(user_id=Adapter.self_id, text="AI 回复"))) == 1
    assert runtime.counters["qq_bot_messages"] == 1


# ── 入站（Telegram → QQ） ────────────────────────────────────────────


def test_tg_message_forwarded_to_bound_qq(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch), "prefix_from_tg": "[TG {name}] "})
    runtime.bind_chat(tg_chat_id=TG_GROUP, qq_kind="group", qq_id=str(GROUP))
    update = {
        "update_id": 1,
        "message": {
            "message_id": 5,
            "date": 1700000000,
            "chat": {"id": int(TG_GROUP), "type": "supergroup", "title": "TG群"},
            "from": {"id": 7, "first_name": "小明", "username": "ming"},
            "text": "从 TG 发的",
        },
    }
    asyncio.run(runtime.handle_updates([update]))
    assert ctx.sent and ctx.sent[-1][0] == "group" and ctx.sent[-1][1] == str(GROUP)
    assert "从 TG 发的" in ctx.sent[-1][2]
    assert runtime.counters["tg_to_qq"] == 1
    assert TG_GROUP in runtime.chats


def test_tg_bot_messages_are_ignored_by_default(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch)})
    runtime.bind_chat(tg_chat_id=TG_GROUP, qq_kind="group", qq_id=str(GROUP))
    update = {
        "update_id": 1,
        "message": {
            "message_id": 5,
            "date": 1700000000,
            "chat": {"id": int(TG_GROUP), "type": "supergroup"},
            "from": {"id": 8, "first_name": "别的机器人", "is_bot": True},
            "text": "bot says",
        },
    }
    asyncio.run(runtime.handle_updates([update]))
    assert ctx.sent == []
    assert runtime.counters["dropped_bot"] == 1


def test_callback_query_is_answered(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch)})
    update = {
        "update_id": 2,
        "callback_query": {
            "id": "cb9",
            "data": "x",
            "from": {"id": 7, "first_name": "小明"},
            "message": {"message_id": 1, "chat": {"id": 7, "type": "private"}},
        },
    }
    asyncio.run(runtime.handle_updates([update]))
    assert runtime.api.answers == ["cb9"]


def test_media_group_is_merged(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch)})
    runtime.bind_chat(tg_chat_id=TG_GROUP, qq_kind="group", qq_id=str(GROUP))
    updates = []
    for index, file_id in enumerate(("a", "b"), start=1):
        updates.append(
            {
                "update_id": index,
                "message": {
                    "message_id": index,
                    "date": 1700000000,
                    "chat": {"id": int(TG_GROUP), "type": "supergroup", "title": "TG群"},
                    "from": {"id": 7, "first_name": "小明"},
                    "media_group_id": "album-1",
                    "photo": [{"file_id": file_id, "file_size": 10}],
                },
            }
        )
    asyncio.run(runtime.handle_updates(updates))
    # 入缓冲，还没投递
    assert ctx.sent == []
    merged = runtime.buffer.flush_all()
    assert len(merged) == 1 and len(merged[0].media) == 2
    asyncio.run(runtime.dispatch(merged[0]))
    assert "photo" in ctx.sent[-1][2]


# ── 生命周期 ─────────────────────────────────────────────────────────


def test_start_without_token_stays_idle(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch)})
    asyncio.run(runtime.start())
    assert runtime.state == "no-token"


def test_start_disabled(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch), "enabled": False, "token": "1:a"})
    asyncio.run(runtime.start())
    assert runtime.state == "disabled"


def test_shutdown_releases_runtime(scratch):
    module, ctx, _ = make_runtime({"_data_dir": str(scratch)})
    asyncio.run(module.on_load(ctx))
    asyncio.run(module.on_shutdown(ctx))
    assert module._RUNTIME is None


def test_service_facade_exposed(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch)})
    module._provide_service(ctx, runtime)
    facade = ctx.services.provided.get("telegram")
    assert facade is not None
    assert "Telegram 桥接状态" in facade.status()
    assert facade.bindings() == []


# ── 连接失败的可操作性（1.0.1） ──────────────────────────────────────


def test_connection_hint_covers_direct_proxy_socks_and_endpoint(scratch):
    module, _, runtime = make_runtime({"_data_dir": str(scratch)})
    direct = module._connection_hint(runtime.config)
    assert "未配置代理" in direct and "proxy" in direct
    proxy = module._connection_hint(module.Config.model_validate({"proxy": "http://127.0.0.1:7890"}))
    assert "127.0.0.1:7890" in proxy
    socks = module._connection_hint(module.Config.model_validate({"proxy": "socks5://127.0.0.1:1080"}))
    assert "socksio" in socks
    endpoint = module._connection_hint(module.Config.model_validate({"endpoint": "https://tg.example"}))
    assert "tg.example" in endpoint and "/bot<token>" in endpoint


def test_diag_report_probes_connectivity(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch), "token": "123456:abc"})

    class Probe:
        def __init__(self, ok):
            self.ok = ok

        async def probe(self, timeout=6.0):
            return (self.ok, "@bot（42 ms）" if self.ok else "TelegramTransportError: boom")

    runtime.api = Probe(True)
    text = asyncio.run(runtime.diag_report())
    assert "连通性探测：通" in text and "@bot" in text

    runtime.api = Probe(False)
    text = asyncio.run(runtime.diag_report())
    assert "连通性探测：不通" in text
    assert "建议" in text and "未配置代理" in text


def test_start_failure_logs_hint_and_schedules_watchdog(scratch):
    import contextlib

    module, ctx, runtime = make_runtime({"_data_dir": str(scratch), "token": "123456:abc"})

    class Boom:
        def __init__(self, *args, **kwargs):
            pass

        async def get_me(self):
            raise RuntimeError("ConnectError: All connection attempts failed")

        async def probe(self, timeout=6.0):
            return False, "ConnectError: All connection attempts failed"

        async def close(self):
            return None

    original = module.TelegramAPI
    module.TelegramAPI = Boom
    try:

        async def scenario():
            await runtime.start()
            assert runtime.state == "error"
            assert runtime._watchdog is not None
            runtime._watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await runtime._watchdog
            runtime._watchdog = None

        asyncio.run(scenario())
    finally:
        module.TelegramAPI = original

    log = ctx.logger.text()
    assert "Telegram 连接异常" in log
    assert "排查建议" in log and "未配置代理" in log
    assert "endpoint" in log  # 启动日志里打印了实际使用的 endpoint

