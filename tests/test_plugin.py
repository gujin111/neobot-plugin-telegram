"""插件级用例：命令集、独立 TG 对话（宿主 provider）、发送、配置与诊断。"""

from __future__ import annotations

import asyncio
import pathlib
from types import SimpleNamespace
from unittest.mock import patch

from _loader import load_plugin

CHAT = "123456789"


class Logger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def _push(self, level: str, message, **fields) -> None:
        self.lines.append(f"{level} {message}" + "".join(f" {k}={v}" for k, v in fields.items()))

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
        self.provided = dict(provided or {})

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

    async def call(self, method, params=None):
        self.calls.append((method, dict(params or {})))
        return {"message_id": len(self.calls)}

    async def answer_callback_query(self, callback_id, **extra):
        self.answers.append(callback_id)
        return True

    async def probe(self, *, timeout=6.0):
        return True, "@bot（12 ms）"

    async def close(self):
        return None

    def last_text(self) -> str:
        return self.calls[-1][1].get("text", "") if self.calls else ""

    def texts(self) -> list[str]:
        return [params.get("text", "") for method, params in self.calls if method == "sendMessage"]


class FakeProvider:
    """冒充宿主的 provider：async chat(messages) -> Message。"""

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
    runtime = module.TelegramRuntime(ctx, module.Config.model_validate(config))
    runtime.api = FakeAPI()
    return module, ctx, runtime


def tg_event(**overrides):
    from tg_model import IncomingEvent

    event = IncomingEvent(
        kind=overrides.pop("kind", "message"),
        chat_id=str(overrides.pop("chat_id", CHAT)),
        chat_type="private",
        message_id=int(overrides.pop("message_id", 11)),
        user_id=str(overrides.pop("user_id", "7")),
        user_name=str(overrides.pop("user_name", "小明")),
        text=str(overrides.pop("text", "你好")),
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
    assert ctx.app_commands.by_alias("telegram").name == "tg"
    assert ctx.app_commands.by_alias("电报诊断").name == "tg诊断"
    for name, command in commands.items():
        assert command.description and command.permission == 0
    for gone in ("tg绑定", "tg解绑", "tg列表", "tg推送", "tg会话", "tg重连"):
        assert gone not in commands


def test_commands_return_status_text(scratch):
    provider = FakeProvider()
    module, ctx, _ = make_runtime({"_data_dir": str(scratch), "token": "1:abc"}, provider=provider)
    module.register_commands(ctx, module.Config.model_validate({"token": "1:abc"}))
    inside = module.runtime_of(ctx)
    inside.api = FakeAPI()
    inside.state = "online"
    inside.bot_username = "sim_bot"
    status = asyncio.run(ctx.app_commands.commands["tg"].handler(None))
    diag = asyncio.run(ctx.app_commands.commands["tg诊断"].handler(None))
    assert "Telegram 独立 Bot · 状态" in status
    assert "连通性探测：通" in diag


# ── AI 对话 ─────────────────────────────────────────────────────────


def test_reply_calls_provider_and_sends_text(scratch):
    provider = FakeProvider("你好呀，我是 NeoBot")
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch)}, provider=provider)
    asyncio.run(runtime.reply_with_ai(tg_event(text="在吗")))
    messages = provider.calls[0]
    assert messages[0]["role"] == "system"
    assert messages[-1] == {"role": "user", "content": "在吗"}
    assert runtime.api.last_text() == "你好呀，我是 NeoBot"
    assert runtime.api.calls[-1][1]["parse_mode"] == "HTML"
    assert runtime.counters["replies"] == 1


def test_sent_text_is_escaped(scratch):
    provider = FakeProvider("<b>加粗</b> & 符号")
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch)}, provider=provider)
    asyncio.run(runtime.reply_with_ai(tg_event()))
    assert runtime.api.last_text() == "&lt;b&gt;加粗&lt;/b&gt; &amp; 符号"


def test_long_reply_is_split(scratch):
    provider = FakeProvider("字" * 8000)
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch)}, provider=provider)
    asyncio.run(runtime.reply_with_ai(tg_event()))
    texts = runtime.api.texts()
    assert len(texts) >= 3
    assert all(len(text) <= 3500 for text in texts)


def test_history_is_kept_and_trimmed(scratch):
    provider = FakeProvider("好的")
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch), "history_limit": 2}, provider=provider)
    for text in ("一", "二", "三", "四"):
        asyncio.run(runtime.reply_with_ai(tg_event(text=text)))
    last = provider.calls[-1]
    assert last[0]["role"] == "system"
    assert last[-1] == {"role": "user", "content": "四"}
    assert len(last) <= 1 + 4 + 1
    assert all(message["content"] != "一" for message in last)
    assert any(message["content"] == "三" for message in last)


def test_history_can_be_disabled(scratch):
    provider = FakeProvider("好")
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch), "history_limit": 0}, provider=provider)
    for text in ("一", "二"):
        asyncio.run(runtime.reply_with_ai(tg_event(text=text)))
    assert len(provider.calls[-1]) == 2


def test_token_shape_check():
    module = load_plugin()
    assert module.token_looks_valid("8123456789:" + "A" * 35) is True
    assert module.token_looks_valid('"8123456789:' + "A" * 35 + '"') is True
    for bad in ("", "abc:def", "@my_bot", "8123456789", "telegram:token"):
        assert module.token_looks_valid(bad) is False, bad


def test_hint_explains_404_token_problem(scratch):
    module, _, runtime = make_runtime({"_data_dir": str(scratch)})
    hint = module._connection_hint(runtime.config, "getMe 失败（404）：Not Found")
    assert "token 写错" in hint
    assert "not found" in hint  # 也提示"小写 not found 才是密钥问题"


def test_diag_reports_token_shape(scratch):
    module, _, runtime = make_runtime({"_data_dir": str(scratch), "token": "8123456789:" + "A" * 35})
    assert "token 形状：正常" in runtime.diag_text()
    module, _, runtime = make_runtime({"_data_dir": str(scratch), "token": "abc:def"})
    assert "token 形状：可疑" in runtime.diag_text()


def test_missing_provider_hints_user(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch)})
    asyncio.run(runtime.reply_with_ai(tg_event()))
    assert "provider" in runtime.api.last_text()
    assert runtime.counters["failures"] == 1


def test_provider_error_sends_error_reply(scratch):
    provider = FakeProvider(error=RuntimeError("boom"))
    module, ctx, runtime = make_runtime(
        {"_data_dir": str(scratch), "ai_error_reply": "（忙，稍后再试）"}, provider=provider
    )
    asyncio.run(runtime.reply_with_ai(tg_event()))
    assert runtime.api.last_text() == "（忙，稍后再试）"
    assert runtime.counters["failures"] == 1
    assert "模型调用失败" in ctx.logger.text()


def test_provider_timeout_sends_error_reply(scratch):
    provider = FakeProvider("不该出现")
    module, ctx, runtime = make_runtime(
        {"_data_dir": str(scratch), "ai_error_reply": "（超时）"}, provider=provider
    )

    async def fake_wait_for(coro, timeout):
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
    assert runtime.counters["failures"] == 1
    assert "空内容" in ctx.logger.text()


def test_body_uses_media_placeholder_when_no_text(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch)})
    assert tg_event(text="看图").body == "看图"
    empty = tg_event(text="")
    empty.media = ["photo"]
    assert empty.body == "[photo]"


# ── 入站分发 ───────────────────────────────────────────────────────


def test_dispatch_ignores_other_bots(scratch):
    provider = FakeProvider()
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch)}, provider=provider)
    asyncio.run(runtime.dispatch(tg_event(user_is_bot=True)))
    assert provider.calls == []


def test_dispatch_answers_callback(scratch):
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch)})
    asyncio.run(runtime.dispatch(tg_event(kind="callback_query", callback_id="cb1")))
    assert runtime.api.answers == ["cb1"]


def test_handle_updates_counts_and_decodes(scratch):
    provider = FakeProvider("收到")
    module, ctx, runtime = make_runtime({"_data_dir": str(scratch)}, provider=provider)
    update = {
        "update_id": 1,
        "message": {
            "message_id": 5,
            "chat": {"id": 778899, "type": "private"},
            "from": {"id": 7, "username": "ming"},
            "text": "你好",
        },
    }
    asyncio.run(runtime.handle_updates([update, {"update_id": 2, "chat_member": {}}]))
    assert runtime.counters["updates"] == 2
    assert runtime.counters["messages"] == 1
    assert provider.calls and provider.calls[0][-1]["content"] == "你好"


# ── 配置与地址解析 ─────────────────────────────────────────────────


def test_resolve_endpoint_priority_and_normalization(scratch):
    module, _, _ = make_runtime({"_data_dir": str(scratch)})
    builtin = f"{module.DEFAULT_REVERSE_PROXY_URL}/{module.DEFAULT_REVERSE_PROXY_SECRET}"
    cases = [
        ({}, (builtin, "reverse_proxy")),
        ({"reverse_proxy_url": "", "endpoint": "https://tg.example/"}, ("https://tg.example", "config")),
        ({"reverse_proxy_url": "", "endpoint": ""}, ("https://api.telegram.org", "default")),
        (
            {"reverse_proxy_url": "https://w.example/", "reverse_proxy_secret": "/v1-abc/"},
            ("https://w.example/v1-abc", "reverse_proxy"),
        ),
        ({"reverse_proxy_url": "w.example", "reverse_proxy_secret": ""}, ("https://w.example", "reverse_proxy")),
        ({"reverse_proxy_url": '"https://w.example"', "reverse_proxy_secret": ""}, ("https://w.example", "reverse_proxy")),
        ({"reverse_proxy_url": "", "endpoint": "tg.example"}, ("https://tg.example", "config")),
    ]
    for raw, expected in cases:
        assert module._resolve_endpoint(module.Config.model_validate(raw)) == expected, raw


def test_config_strips_quotes_and_masks():
    module = load_plugin()
    config = module.Config.model_validate(
        {"token": '"123456:abc"', "proxy": '"http://127.0.0.1:7890/"', "proxy_key": '"pk-x"'}
    )
    assert config.token == "123456:abc"
    assert config.proxy == "http://127.0.0.1:7890/"
    assert config.proxy_key == "pk-x"
    assert module.mask("pk-a5e68cb17320") == "pk-***"
    assert module.mask("") == "未设置"


def test_diag_masks_secrets(scratch):
    module, _, runtime = make_runtime(
        {"_data_dir": str(scratch), "token": "123456:secret", "reverse_proxy_url": "https://w.example", "reverse_proxy_secret": "v1-secret"}
    )
    text = runtime.diag_text()
    assert "https://w.example/***" in text
    assert "123456:***" in text
    assert "v1-secret" not in text
    assert "pk-***" in text


def test_hint_explains_hostname_resolution_failure(scratch):
    module, _, runtime = make_runtime(
        {"_data_dir": str(scratch), "proxy": "http://host.docker.internal:7890/"}
    )
    hint = module._connection_hint(runtime.config, "ConnectError: getaddrinfo failed")
    assert "主机名解析失败" in hint and "127.0.0.1" in hint


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


def test_provider_label_and_version(scratch):
    provider = FakeProvider()
    module, _, runtime = make_runtime({"_data_dir": str(scratch)}, provider=provider)
    assert runtime.provider_label() == "宿主 provider（可用）"
    assert module.plugin.version.startswith("3.")
