"""宿主模拟器：不连 Telegram、不启动 NeoBot，直接看命令与「独立 TG 对话」链路。

    python devtools/simulate.py

验证：命令只有两条 → TG 消息驱动宿主模型 → 文本回 TG（带 system_prompt）→
第二轮带上下文 → 没有 provider 时给提示 → 状态与诊断输出正常。
"""

from __future__ import annotations

import asyncio
import importlib.machinery
import pathlib
import shutil
import sys
import types
from types import SimpleNamespace
from uuid import uuid4

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(_stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover
            pass

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" —— {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(label)


def load_plugin():
    name = "telegram_plugin"
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / "__init__.py"))
    module = types.ModuleType(name)
    module.__file__ = str(ROOT / "__init__.py")
    module.__path__ = [str(ROOT)]
    module.__package__ = name
    sys.modules[name] = module
    loader.exec_module(module)
    return module


class Logger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def _push(self, level, message, **fields):
        self.lines.append(f"{level} {message}" + "".join(f" {k}={v}" for k, v in fields.items()))

    def info(self, message, **fields):
        self._push("INFO", message, **fields)

    def warning(self, message, **fields):
        self._push("WARN", message, **fields)

    def error(self, message, **fields):
        self._push("ERROR", message, **fields)

    def debug(self, message, **fields):
        self._push("DEBUG", message, **fields)


class AppCommands:
    available = True

    def __init__(self) -> None:
        self.commands: dict[str, SimpleNamespace] = {}

    def register(self, name, *, description="", usage="", permission=0, aliases=(), params=()):
        def decorate(handler):
            self.commands[name] = SimpleNamespace(
                name=name, handler=handler, description=description, usage=usage,
                permission=permission, aliases=tuple(aliases), params=tuple(params),
            )
            return handler

        return decorate


class Services:
    def __init__(self, provided: dict) -> None:
        self.provided = dict(provided)

    def get(self, name, default=None):
        return self.provided.get(name, default)


class FakeHostConfig:
    """冒充宿主的 config 服务：面板里写的昵称与人格。"""

    bot_nick_name = "小九"

    def __init__(self) -> None:
        self.bot = SimpleNamespace(bot_data="我是一个喜欢聊天、说话简短的猫娘。")


class FakePromptStore:
    def __init__(self) -> None:
        self.sections = {"gentle_language": "<温和发言>不喜欢吵架，有人吵架就劝架。</温和发言>"}

    def enabled(self, name):
        return True

    def get(self, name, sub="template", default=""):
        return self.sections.get(name, default)


class Context:
    def __init__(self, data_dir: pathlib.Path, provider=None) -> None:
        self.data_dir = data_dir
        self.config: dict = {}
        self.logger = Logger()
        self.app_commands = AppCommands()
        self.adapter = SimpleNamespace(self_id=99999)
        provided = {"config": FakeHostConfig(), "prompt_store": FakePromptStore()}
        if provider:
            provided["provider"] = provider
        self.services = Services(provided)


class FakeAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, params=None):
        self.calls.append((method, dict(params or {})))
        return {"message_id": len(self.calls)}

    async def answer_callback_query(self, callback_id, **extra):
        return True

    async def probe(self, *, timeout=6.0):
        return True, "@sim_bot（8 ms）"

    async def close(self):
        return None

    def texts(self) -> list[str]:
        return [params.get("text", "") for method, params in self.calls if method == "sendMessage"]


class FakeProvider:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools=None):
        self.calls.append([dict(message) for message in messages])
        return {"role": "assistant", "content": self.reply}


def tg_update(message_id: int, text: str) -> dict:
    return {
        "update_id": message_id,
        "message": {
            "message_id": message_id,
            "date": 1700000000,
            "chat": {"id": 778899, "type": "private", "first_name": "小明"},
            "from": {"id": 7, "first_name": "小明", "username": "ming"},
            "text": text,
        },
    }


def main() -> int:
    module = load_plugin()
    scratch = ROOT / ".scratch"
    workdir = scratch / f"sim-{uuid4().hex[:8]}"
    workdir.mkdir(parents=True, exist_ok=True)

    provider = FakeProvider("我是 NeoBot，很高兴在 Telegram 见到你。")
    config = {
        "token": "123456:SIMULATED",
        "host_prompt_sections": ["gentle_language"],
        "system_prompt": "回答尽量短，一两句就好。",
        "history_limit": 6,
    }
    ctx = Context(workdir, provider=provider)
    cfg = module.Config.model_validate(config)

    print("== /help 命令展示页 ==")
    module.register_commands(ctx, cfg)
    for name, command in ctx.app_commands.commands.items():
        print(f"  /{name}  别名：{'、'.join(command.aliases) or '无'}")
        print(f"      {command.description}")
    check("只注册两条状态命令", set(ctx.app_commands.commands) == {"tg", "tg诊断"})

    runtime = module.TelegramRuntime(ctx, cfg)
    runtime.api = FakeAPI()
    runtime.state = "online"
    runtime.bot_username = "sim_bot"

    print("\n== 第一条 TG 消息 ==")
    asyncio.run(runtime.handle_updates([tg_update(21, "你好，介绍一下你自己")]))
    check("调用了宿主模型服务", len(provider.calls) == 1)
    if provider.calls:
        messages = provider.calls[0]
        print("  system：")
        for line in messages[0]["content"].splitlines():
            print(f"      {line}")
        print(f"  user  ：{messages[-1]['content']!r}")
        check("沿用宿主人格", "猫娘" in messages[0]["content"])
        check("拼接了指定分区", "劝架" in messages[0]["content"])
        check("补上了 TG 对话规则", "不要调用任何工具" in messages[0]["content"])
    sent = runtime.api.texts()
    print(f"  回给 Telegram：{sent[-1]!r}" if sent else "  没有发送")
    check("模型文本回传到 TG", bool(sent) and "我是 NeoBot" in sent[-1])

    print("\n== 第二条（验证上下文）==")
    asyncio.run(runtime.handle_updates([tg_update(22, "刚才你说了什么？")]))
    messages = provider.calls[-1]
    print(f"  本轮消息条数：{len(messages)}（system 1 + 历史 2 + 当前 1）")
    check("带上了上一轮历史", len(messages) == 4)
    check("历史含上一轮提问", any(item["content"] == "你好，介绍一下你自己" for item in messages))

    print("\n== 没有 provider 时 ==")
    bare = module.TelegramRuntime(Context(workdir), module.Config.model_validate({}))
    bare.api = FakeAPI()
    asyncio.run(bare.reply_with_ai(module.decode_update(tg_update(23, "hi"))))
    print(f"  回给用户：{bare.api.texts()[-1]!r}")
    check("提示缺少模型服务", "provider" in bare.api.texts()[-1])

    print("\n== /tg状态 ==")
    print("\n".join("  " + line for line in runtime.status_text().splitlines()))

    print("\n== /tg诊断 ==")
    diag = asyncio.run(runtime.diag_report())
    print("\n".join("  " + line for line in diag.splitlines()))
    check("诊断显示模型服务可用", "宿主 provider（可用）" in diag)
    check("诊断做了连通性探测", "连通性探测：通" in diag)

    print()
    shutil.rmtree(workdir, ignore_errors=True)
    try:
        scratch.rmdir()
    except OSError:
        pass
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：{'、'.join(FAILURES)}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
