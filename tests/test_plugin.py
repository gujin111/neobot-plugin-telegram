"""插件级用例：状态查询命令、AI 直答（用假编排器）、配置清理、链路诊断。

需要 ``neobot_modloader``（PYTHONPATH 指向 NeoBot 的 packages/*/src），
缺失时 ``_loader.load_plugin`` 会 skip。
"""

from __future__ import annotations

import asyncio
import pathlib
from types import SimpleNamespace

from _loader import load_plugin

CHAT = "123456789"
NAME = "小明"


class Logger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def _push(self, level: str, message, **fields) -> None:
        suffix = "".join(f" {key}={value}" for key, value in fields.items())
        self.lines.append(f"{level} {message}{suffix}")

    def info(self, message, **fields):
        self._push("INFO", message, **fields)

    def warning(self, message, **fields):
        self._push("WARN", message, **fields)

    def error(self, message, **fields):
        self._push("ERROR", message, **fields)

    def debug(self, message, **fields):
        self._push("DEBUG", message, **fields)

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

    def by_alias(self, alias: str):
        for command in self.commands.values():
            if alias == command.name or alias in command.aliases:
                return command
        return None


class Services:
    def __init__(self, provided: dict | None = None) -> None:
        self.provided: dict[str, object] = dict(provided or {})

    def provide(self, name, service, **kwargs):
        self.provided[name] = service

    def get(self, name, default=None):
        return self.provided.get(name, default)


class Context:
    def __init__(self, config: dict, *, services: dict | None = None) -> None:
        self.config = config
        self.data_dir = pathlib.Path(config.get("_data_dir", "."))
        self.logger = Logger()
        self.app_commands = AppCommands()
        self.adapter = SimpleNamespace(self_id=99999)
        self.services = Services(services)


class FakeAPI:
    """替代 TelegramAPI：记录调用，可选返回固定结果。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.answers: list[str] = []

    async def call(self, method, params=None, *, files=None, retries=None):
        self.calls.append((method, dict(params or {})))
        return {"message_id": len(self.calls)}

    async def answer_callback_query(self, callback_id, **extra):
        self.answers.append(callback_id)
        return True

    async def probe(self, *, timeout=6.0):
        return True, "@bot（12 ms）"

    async def close(self):
        return None

    @property
    def gateway_url(self) -> str:
        return ""

    def sent_texts(self) -> list[str]:
        return [params.get("text", "") for method, params in self.calls if method == "sendMessage"]


class FakeEvent:
    """冒充 ReplyEvent：只需要 generated_text / state / error。"""

    def __init__(self, text: str = "", state: str = "COMPLETED", error=None) -> None:
        self.generated_text = text
        self.state = SimpleNamespace(name=state)
        self.error = error


class FakeOrchestrator:
    def __init__(self, event) -> None:
        self.event = event
        self.calls: list[dict] = []

    def start_background_reply(self, **kwargs):
        self.calls.append(kwargs)
        return self.event


def make_runtime(config: dict, *, orchestrator=None):
    module = load_plugin()
    services = {"reply_orchestrator": orchestrator} if orchestrator is not None else {}
    ctx = Context(config, services=services)
    cfg = module.Config.model_validate(config)
    runtime = module.TelegramRuntime(ctx, cfg)
    runtime.api = FakeAPI()
    return module, ctx, runtime


def tg_event(**overrides):
    from tg_model import IncomingEvent, parse_text

    text = overrides.pop("text", "你好")
    event = IncomingEvent(
        kind="message",
        chat_id=str(overrides.pop("chat_id", CHAT)),
        chat_type="private",
        message_id=int(overrides.pop("message_id", 11)),
        user_id=str(overrides.pop("user_id", "7")),
        user_name=str(overrides.pop("user_name", NAME)),
        spans=parse_text(text, []),
    )
    for key, value in overrides.items():
        setattr(event, key, value)
    return event


# ── 命令：只保留状态查询 ────────────────────────────────────────────


def test_only_status_commands_are_registered(scratch):
    module, ctx, _ = make_runtime({"_data_dir": str(scratch), "token": "1:abc"})
    module.register_commands(ctx, module.Config.model_validate({"token": "1:abc"}))
    commands = ctx.app_commands.commands
    assert set(commands) == {"tg", "tg诊断"}
    for name, command in commands.items():
        assert command.description, f"{name} 缺 description"
        assert command.permission == 0
    assert ctx.app_commands.by_alias("telegram").name == "tg"
    assert ctx.app_commands.by_alias("电报").name == "tg"
    assert ctx.app_commands.by_alias("tgdiag").name == "tg诊断"
    # 桥接类命令必须不存在
    for gone in ("tg绑定", "tg解绑", "tg列表", "tg推送", "tg会话", "tg重连"):
        assert gone not in commands


def test_status_text_reports_ai_endpoint(scratch):
    module, ctx, runtime = make_runtime(
        {"_data_dir": str(scratch), "token": "123456:abc", "ai_endpoint_id": "10001"}
    )
    text = runtime.status_text()
    assert "Telegram 直连 AI" in text
    assert "AI 工作区：私聊 10001" in text
    assert "计数器" in text


def test_diag_reports_orchestrator_and_probe(scratch):
    orchestrator = FakeOrchestrator(FakeEvent("ok"))
    module, ctx, runtime = make_runtime(
        {"_data_dir": str(scratch), "token": "123456:abc", "ai_endpoint_id": "10001"},
        orchestrator=orchestrator,
    )
    text = asyncio.run(runtime.diag_report())
    assert "宿主编排器：可用" in text
    assert "连通性探测：通" in text
    assert "123456:***" in text


def test_diag_without_orchestrator_warns(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch), "token": "1:a"})
    text = asyncio.run(runtime.diag_report())
    assert "宿主编排器：不可用" in text


# ── AI 直答 ─────────────────────────────────────────────────────────


def test_reply_with_ai_sends_generated_text(scratch):
    orchestrator = FakeOrchestrator(FakeEvent("你好呀，我是 NeoBot"))
    module, ctx, runtime = make_runtime(
        {"_data_dir": str(scratch), "ai_endpoint_id": "10001", "ai_prefix": "[TG {name}] "},
        orchestrator=orchestrator,
    )
    asyncio.run(runtime.reply_with_ai(tg_event(text="在吗")))
    call = orchestrator.calls[0]
    assert call["kind"] == "private"
    assert call["conversation_id"] == "10001"
    assert call["content"].startswith("[TG 小明] ")
    assert "在吗" in call["content"]
    assert call["proactive"] is True
    assert "manager_name" in call

    assert runtime.api.calls[-1][0] == "sendMessage"
    assert "你好呀，我是 NeoBot" in runtime.api.calls[-1][1]["text"]
    assert runtime.counters["ai_replies"] == 1


def test_reply_without_endpoint_hints_user(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch)})
    asyncio.run(runtime.reply_with_ai(tg_event()))
    assert runtime.api.calls, "应当给用户发提示"
    assert "AI 工作区" in runtime.api.calls[-1][1]["text"]
    assert runtime.counters["ai_replies"] == 0


def test_ask_ai_busy_returns_empty(scratch):
    orchestrator = FakeOrchestrator(None)  # 宿主返回 None：已有回复在进行
    module, ctx, runtime = make_runtime(
        {"_data_dir": str(scratch), "ai_endpoint_id": "10001"}, orchestrator=orchestrator
    )
    text = asyncio.run(runtime._ask_ai("[TG 小明] 在吗"))
    assert text == ""
    assert runtime.counters["ai_busy"] == 1


def test_ask_ai_empty_generation_returns_empty(scratch):
    orchestrator = FakeOrchestrator(FakeEvent("", state="FAILED", error="model down"))
    module, ctx, runtime = make_runtime(
        {"_data_dir": str(scratch), "ai_endpoint_id": "10001"}, orchestrator=orchestrator
    )
    text = asyncio.run(runtime._ask_ai("[TG 小明] 在吗"))
    assert text == ""
    assert "AI 未产出回复" in ctx.logger.text()


def test_reply_sends_error_message_when_ai_fails(scratch):
    orchestrator = FakeOrchestrator(FakeEvent("", state="FAILED"))
    module, ctx, runtime = make_runtime(
        {"_data_dir": str(scratch), "ai_endpoint_id": "10001", "ai_error_reply": "（忙，稍后再试）"},
        orchestrator=orchestrator,
    )
    asyncio.run(runtime.reply_with_ai(tg_event()))
    assert runtime.api.calls[-1][1]["text"] == "（忙，稍后再试）"
    assert runtime.counters["ai_failures"] == 1


def test_compose_includes_prefix_and_media_placeholder(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch), "ai_prefix": "[TG {name}] "})
    event = tg_event(text="看图")
    event.media = [SimpleNamespace(kind="photo")]
    composed = runtime.compose(event)
    assert composed.startswith("[TG 小明] ")
    assert "看图" in composed

    empty = tg_event(text="")
    empty.media = [SimpleNamespace(kind="photo")]
    assert "photo" in runtime.compose(empty)


# ── 入站分发 ───────────────────────────────────────────────────────

def test_dispatch_ignores_other_bots(scratch):
    orchestrator = FakeOrchestrator(FakeEvent("x"))
    module, ctx, runtime = make_runtime(
        {"_data_dir": str(scratch), "ai_endpoint_id": "10001"}, orchestrator=orchestrator
    )
    event = tg_event(user_is_bot=True)
    asyncio.run(runtime.dispatch(event))
    assert orchestrator.calls == []
    assert runtime.counters["dropped_bot"] == 1


def test_dispatch_skips_callback_and_answers_it(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch)})
    event = tg_event()
    event.kind = "callback_query"
    event.callback_id = "cb1"
    asyncio.run(runtime.dispatch(event))
    assert runtime.api.answers == ["cb1"]


# ── 配置与地址解析 ─────────────────────────────────────────────────


def test_resolve_endpoint_priority_and_normalization(scratch):
    module, _, _ = make_runtime({"_data_dir": str(scratch)})
    cases = [
        ({}, ("https://api.telegram.org", "default")),
        ({"endpoint": "https://tg.example/"}, ("https://tg.example", "config")),
        (
            {"reverse_proxy_url": "https://w.example/", "reverse_proxy_secret": "/v1-abc/"},
            ("https://w.example/v1-abc", "reverse_proxy"),
        ),
        ({"reverse_proxy_url": "https://w.example", "reverse_proxy_secret": ""}, ("https://w.example", "reverse_proxy")),
        (
            {"reverse_proxy_url": "https://w.example", "endpoint": "https://ignored.example"},
            ("https://w.example", "reverse_proxy"),
        ),
        ({"reverse_proxy_url": '"https://w.example"'}, ("https://w.example", "reverse_proxy")),
        ({"reverse_proxy_url": '"https://w.example'}, ("https://w.example", "reverse_proxy")),
        ({"reverse_proxy_url": "w.example"}, ("https://w.example", "reverse_proxy")),
        ({"endpoint": "tg.example"}, ("https://tg.example", "config")),
    ]
    for raw, expected in cases:
        assert module._resolve_endpoint(module.Config.model_validate(raw)) == expected, raw


def test_config_strips_quotes(scratch):
    module = load_plugin()
    config = module.Config.model_validate(
        {"token": '"123456:abc"', "proxy": '"http://127.0.0.1:7890/"', "ai_endpoint_id": '"10001"'}
    )
    assert config.token == "123456:abc"
    assert config.proxy == "http://127.0.0.1:7890/"
    assert config.ai_endpoint_id == "10001"


def test_diag_masks_reverse_proxy_secret(scratch):
    module, _, runtime = make_runtime(
        {
            "_data_dir": str(scratch),
            "reverse_proxy_url": "https://w.example",
            "reverse_proxy_secret": "v1-secret",
        }
    )
    text = runtime.diag_text()
    assert "https://w.example/***" in text
    assert "v1-secret" not in text
    hint = module._connection_hint(runtime.config)
    assert "反代" in hint and "v1-secret" not in hint


def test_hint_explains_hostname_resolution_failure(scratch):
    module, _, runtime = make_runtime(
        {"_data_dir": str(scratch), "proxy": "http://host.docker.internal:7890/"}
    )
    hint = module._connection_hint(runtime.config, "ConnectError: getaddrinfo failed")
    assert "主机名解析失败" in hint
    assert "127.0.0.1" in hint
    assert "http://host.docker.internal:7890" in hint


# ── 生命周期 ─────────────────────────────────────────────────────────


def test_start_without_token_stays_idle(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch)})
    asyncio.run(runtime.start())
    assert runtime.state == "no-token"


def test_start_disabled(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch), "enabled": False, "token": "1:a"})
    asyncio.run(runtime.start())
    assert runtime.state == "disabled"


def test_on_load_and_shutdown(scratch):
    module, ctx, _ = make_runtime({"_data_dir": str(scratch)})
    asyncio.run(module.on_load(ctx))
    assert "tg" in ctx.app_commands.commands
    asyncio.run(module.on_shutdown(ctx))
    assert module._RUNTIME is None


def test_service_facade_exposed(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch), "ai_endpoint_id": "10001"})
    module._provide_service(ctx, runtime)
    facade = ctx.services.provided.get("telegram")
    assert facade is not None
    assert "Telegram 直连 AI" in facade.status()
