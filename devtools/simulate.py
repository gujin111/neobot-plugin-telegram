"""宿主模拟器：不连 Telegram、不启动 NeoBot，直接看插件的命令与 AI 直答链路。

跑法（在插件目录内，需要 PYTHONPATH 指向 NeoBot 的 packages/*/src）：

    python devtools/simulate.py

依次验证：

1. `/help` 命令展示页只剩两条状态命令；
2. TG 消息 → 宿主回复管线（假编排器）→ AI 文本发回 TG；
3. 未配置 AI 工作区时给用户的提示；
4. `/tg诊断` 的连通性探测与编排器可用性。

任一断言失败退出码 1。
"""

from __future__ import annotations

import asyncio
import importlib.machinery
import pathlib
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


class AppCommands:
    available = True

    def __init__(self) -> None:
        self.commands: dict[str, SimpleNamespace] = {}

    def register(self, name, *, description="", usage="", permission=0, aliases=(), params=()):
        def decorate(handler):
            self.commands[name] = SimpleNamespace(
                name=name, handler=handler, description=description,
                usage=usage, permission=permission, aliases=tuple(aliases), params=tuple(params),
            )
            return handler

        return decorate


class Services:
    def __init__(self, provided: dict) -> None:
        self.provided = dict(provided)

    def provide(self, name, service, **kwargs):
        self.provided[name] = service

    def get(self, name, default=None):
        return self.provided.get(name, default)


class Context:
    def __init__(self, data_dir: pathlib.Path, orchestrator=None) -> None:
        self.data_dir = data_dir
        self.config: dict = {}
        self.logger = Logger()
        self.app_commands = AppCommands()
        self.adapter = SimpleNamespace(self_id=99999)
        self.services = Services({"reply_orchestrator": orchestrator} if orchestrator else {})


class FakeAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, params=None, *, files=None, retries=None):
        self.calls.append((method, dict(params or {})))
        return {"message_id": len(self.calls)}

    async def answer_callback_query(self, callback_id, **extra):
        return True

    async def probe(self, *, timeout=6.0):
        return True, "@sim_bot（8 ms）"

    async def close(self):
        return None

    @property
    def gateway_url(self) -> str:
        return ""


class FakeEvent:
    def __init__(self, text: str, state: str = "COMPLETED") -> None:
        self.generated_text = text
        self.state = SimpleNamespace(name=state)
        self.error = None


class FakeOrchestrator:
    """模拟宿主 reply_orchestrator：记下调用参数，返回一段假的 AI 文本。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def start_background_reply(self, **kwargs):
        self.calls.append(kwargs)
        return FakeEvent(self.reply)


def tg_update() -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 21,
            "date": 1700000000,
            "chat": {"id": 778899, "type": "private", "first_name": "小明"},
            "from": {"id": 7, "first_name": "小明", "username": "ming"},
            "text": "你好，介绍一下你自己",
        },
    }


def main() -> int:
    module = load_plugin()
    scratch = ROOT / ".scratch"
    workdir = scratch / f"sim-{uuid4().hex[:8]}"
    workdir.mkdir(parents=True, exist_ok=True)

    orchestrator = FakeOrchestrator("我是 NeoBot，很高兴在 Telegram 见到你。")
    config = {
        "token": "123456:SIMULATED",
        "reverse_proxy_url": "https://tg.example.com",
        "reverse_proxy_secret": "v1-demo",
        "ai_endpoint_kind": "private",
        "ai_endpoint_id": "10001",
        "ai_prefix": "[TG {name}] ",
    }
    ctx = Context(workdir, orchestrator=orchestrator)
    ctx.config = config
    cfg = module.Config.model_validate(config)

    print("== /help 命令展示页 ==")
    module.register_commands(ctx, cfg)
    for name, command in ctx.app_commands.commands.items():
        print(f"  /{name}  别名：{'、'.join(command.aliases) or '无'}")
        print(f"      {command.description}")
    check("只注册两条状态命令", set(ctx.app_commands.commands) == {"tg", "tg诊断"})
    check("桥接命令已移除", "tg绑定" not in ctx.app_commands.commands)

    runtime = module.TelegramRuntime(ctx, cfg)
    runtime.api = FakeAPI()
    runtime.state = "online"
    runtime.bot_username = "sim_bot"

    print("\n== TG 消息 → 宿主 AI → 回 TG ==")
    asyncio.run(runtime.handle_updates([tg_update()]))
    check("调用了宿主编排器", len(orchestrator.calls) == 1, str(orchestrator.calls))
    if orchestrator.calls:
        call = orchestrator.calls[0]
        print(f"  交给 AI 的内容：{call['content']!r}")
        print(f"  工作区：{call['kind']} {call['conversation_id']}，proactive={call['proactive']}")
        check("内容带 TG 前缀", call["content"].startswith("[TG ") and "ming" in call["content"])
    sent = [params.get("text", "") for _, params in runtime.api.calls]
    print(f"  回给 Telegram：{sent[-1]!r}" if sent else "  没有任何发送")
    check("AI 文本回传到 TG", bool(sent) and "我是 NeoBot" in sent[-1])
    check("计数器记录了回复", runtime.counters["ai_replies"] == 1)

    print("\n== 未配置 AI 工作区 ==")
    bare = module.TelegramRuntime(ctx, module.Config.model_validate({"token": "1:a"}))
    bare.api = FakeAPI()
    asyncio.run(bare.reply_with_ai(module.decode_update(tg_update())))
    print(f"  回给用户：{bare.api.calls[-1][1]['text']!r}")
    check("提示去配置 AI 工作区", "AI 工作区" in bare.api.calls[-1][1]["text"])

    print("\n== /tg状态 ==")
    print("\n".join("  " + line for line in runtime.status_text().splitlines()))

    print("\n== /tg诊断 ==")
    diag = asyncio.run(runtime.diag_report())
    print("\n".join("  " + line for line in diag.splitlines()))
    check("诊断显示编排器可用", "宿主编排器：可用" in diag)
    check("诊断做了连通性探测", "连通性探测：通" in diag)

    print()
    import shutil

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
