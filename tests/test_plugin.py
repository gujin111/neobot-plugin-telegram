"""插件级用例：状态命令、独立 TG 对话（直接调宿主 provider）、配置清理、诊断。

需要 ``neobot_modloader``（PYTHONPATH 指向 NeoBot 的 packages/*/src），
缺失时 ``_loader.load_plugin`` 会 skip。
"""

from __future__ import annotations

import asyncio
import pathlib
from types import SimpleNamespace
from unittest.mock import patch

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

    def last_text(self) -> str:
        return self.calls[-1][1].get("text", "") if self.calls else ""


class FakeProvider:
    """冒充宿主的 provider 服务：实现 async chat(messages) -> Message。"""

    def __init__(self, reply: str = "你好呀，我是 NeoBot", *, error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools=None):
        self.calls.append([dict(message) for message in messages])
        if self.error is not None:
            raise self.error
        return {"role": "assistant", "content": self.reply}


def make_runtime(config: dict, *, provider=None):
    module = load_plugin()
    services = {"provider": provider} if provider is not None else {}
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


# ── 命令：只有状态查询 ──────────────────────────────────────────────


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
    for gone in ("tg绑定", "tg解绑", "tg列表", "tg推送", "tg会话", "tg重连"):
        assert gone not in commands


def test_status_text_reports_model_service(scratch):
    provider = FakeProvider()
    module, ctx, runtime = make_runtime(
        {"_data_dir": str(scratch), "token": "123456:abc", "history_limit": 8}, provider=provider
    )
    text = runtime.status_text()
    assert "Telegram 独立 Bot" in text
    assert "模型服务：宿主 provider（可用）" in text
    assert "上下文 8 条/会话" in text


def test_diag_reports_provider_and_probe(scratch):
    provider = FakeProvider()
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch), "token": "123456:abc"}, provider=provider)
    text = asyncio.run(runtime.diag_report())
    assert "模型服务：宿主 provider（可用）" in text
    assert "连通性探测：通" in text
    assert "123456:***" in text


def test_diag_without_provider_warns(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch), "token": "1:a"})
    text = asyncio.run(runtime.diag_report())
    assert "宿主 provider（不可用）" in text


# ── 独立 TG 对话 ────────────────────────────────────────────────────


def test_reply_calls_provider_and_sends_text(scratch):
    provider = FakeProvider("你好呀，我是 NeoBot")
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch)}, provider=provider)
    asyncio.run(runtime.reply_with_ai(tg_event(text="在吗")))
    assert len(provider.calls) == 1
    messages = provider.calls[0]
    assert messages[0]["role"] == "system"
    assert messages[-1] == {"role": "user", "content": "在吗"}
    assert runtime.api.last_text() == "你好呀，我是 NeoBot"
    assert runtime.counters["ai_replies"] == 1


def test_history_is_kept_and_trimmed(scratch):
    provider = FakeProvider("好的")
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch), "history_limit": 2}, provider=provider)
    for text in ("一", "二", "三", "四"):
        asyncio.run(runtime.reply_with_ai(tg_event(text=text)))
    last = provider.calls[-1]
    # system + 最多 2*2 条历史 + 当前 user
    assert last[0]["role"] == "system"
    assert last[-1] == {"role": "user", "content": "四"}
    assert len(last) <= 1 + 4 + 1
    # 最早那轮已被裁掉，最近的还在
    assert all(message["content"] != "一" for message in last)
    assert any(message["content"] == "三" for message in last)


def test_history_disabled(scratch):
    provider = FakeProvider("好")
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch), "history_limit": 0}, provider=provider)
    asyncio.run(runtime.reply_with_ai(tg_event(text="一")))
    asyncio.run(runtime.reply_with_ai(tg_event(text="二")))
    assert len(provider.calls[-1]) == 2  # system + 当前 user，无历史


def test_missing_provider_hints_user(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch)})
    asyncio.run(runtime.reply_with_ai(tg_event()))
    assert "provider" in runtime.api.last_text()
    assert runtime.counters["ai_failures"] == 1


def test_provider_error_sends_error_reply(scratch):
    provider = FakeProvider(error=RuntimeError("boom"))
    module, ctx, runtime = make_runtime(
        {"_data_dir": str(scratch), "ai_error_reply": "（忙，稍后再试）"}, provider=provider
    )
    asyncio.run(runtime.reply_with_ai(tg_event()))
    assert runtime.api.last_text() == "（忙，稍后再试）"
    assert runtime.counters["ai_failures"] == 1
    assert "模型调用失败" in ctx.logger.text()


def test_provider_timeout_sends_error_reply(scratch):
    provider = FakeProvider("不该出现")
    module, ctx, runtime = make_runtime(
        {"_data_dir": str(scratch), "ai_error_reply": "（超时）"}, provider=provider
    )

    async def fake_wait_for(coro, timeout):  # 关掉未 await 的协程，避免告警
        if hasattr(coro, "close"):
            coro.close()
        raise asyncio.TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        asyncio.run(runtime.reply_with_ai(tg_event()))
    assert runtime.api.last_text() == "（超时）"
    assert "模型调用超时" in ctx.logger.text()


def test_empty_model_content_counts_as_failure(scratch):
    provider = FakeProvider("")
    module, ctx, runtime = make_runtime(
        {"_data_dir": str(scratch), "ai_error_reply": "（空）"}, provider=provider
    )
    asyncio.run(runtime.reply_with_ai(tg_event()))
    assert runtime.api.last_text() == "（空）"
    assert runtime.counters["ai_failures"] == 1
    assert "空内容" in ctx.logger.text()


def test_compose_uses_text_or_media_placeholder(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch)})
    assert runtime.compose(tg_event(text="看图")) == "看图"
    empty = tg_event(text="")
    empty.media = [SimpleNamespace(kind="photo")]
    assert "photo" in runtime.compose(empty)


# ── 入站分发 ───────────────────────────────────────────────────────


def test_dispatch_ignores_other_bots(scratch):
    provider = FakeProvider()
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch)}, provider=provider)
    asyncio.run(runtime.dispatch(tg_event(user_is_bot=True)))
    assert provider.calls == []
    assert runtime.counters["dropped_bot"] == 1


def test_dispatch_answers_callback(scratch):
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
        {"token": '"123456:abc"', "proxy": '"http://127.0.0.1:7890/"', "system_prompt": '"你是猫娘"'}
    )
    assert config.token == "123456:abc"
    assert config.proxy == "http://127.0.0.1:7890/"
    assert config.system_prompt == "你是猫娘"


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
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch)}, provider=FakeProvider())
    module._provide_service(ctx, runtime)
    facade = ctx.services.provided.get("telegram")
    assert facade is not None
    assert "Telegram 独立 Bot" in facade.status()
