"""宿主模拟器：不连 Telegram、不启动 NeoBot，直接看插件的行为。

跑法（在插件目录内）：

    python devtools/simulate.py

会依次验证：

1. `/help` 命令展示页能看到的全部命令、别名、用法与参数；
2. Telegram → QQ：一条假 Telegram 消息投递到绑定的 QQ 会话；
3. QQ → Telegram：一条假 QQ 消息转发到绑定的 Telegram 会话（假 API 记录调用）；
4. 权限：普通成员执行绑定命令被拒；
5. 诊断文本：token 打码、计数器齐全。

任一断言失败退出码 1，可直接接进 CI。
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

# Windows 控制台默认 GBK，打印「⇄」这类字符会 UnicodeEncodeError：统一切到 UTF-8
for _stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(_stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover - 老 Python / 特殊流
            pass

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" —— {detail}" if detail and not ok else ""))
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

    def _push(self, level: str, message) -> None:
        self.lines.append(f"{level} {message}")

    def info(self, message, **fields):
        self._push("INFO", message)

    def warning(self, message, **fields):
        self._push("WARN", message)

    def error(self, message, **fields):
        self._push("ERROR", message)

    def debug(self, message, **fields):
        self._push("DEBUG", message)


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


class Services:
    def __init__(self) -> None:
        self.provided: dict[str, object] = {}

    def provide(self, name, service, **kwargs):
        self.provided[name] = service

    def get(self, name, default=None):
        return self.provided.get(name, default)


class Context:
    def __init__(self, data_dir: pathlib.Path) -> None:
        self.data_dir = data_dir
        self.config: dict = {}
        self.logger = Logger()
        self.app_commands = AppCommands()
        self.adapter = SimpleNamespace(self_id=99999)
        self.services = Services()
        self.to_qq: list[tuple[str, str, str]] = []

    async def send_group(self, group_id, message, *, proactive=False):
        self.to_qq.append(("group", str(group_id), getattr(message, "plain_text", str(message))))
        return {"status": "ok"}

    async def send_private(self, user_id, message, *, proactive=False):
        self.to_qq.append(("private", str(user_id), getattr(message, "plain_text", str(message))))
        return {"status": "ok"}


class FakeAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, params=None, *, files=None, retries=None):
        self.calls.append((method, dict(params or {})))
        print(f"    · Telegram API ← {method} {params}")
        return {"message_id": len(self.calls)}

    async def answer_callback_query(self, callback_id, **extra):
        return True

    async def close(self):
        return None


def main() -> int:
    module = load_plugin()
    scratch = ROOT / ".scratch"
    workdir = scratch / f"sim-{uuid4().hex[:8]}"
    # 不用 tempfile.mkdtemp：本机沙箱下它创建的目录写文件会 PermissionError
    workdir.mkdir(parents=True, exist_ok=True)
    print(f"工作目录：{workdir}\n")

    config = {
        "token": "123456:SIMULATED",
        "proxy": "http://127.0.0.1:7890",
        "prefix_from_tg": "[TG {name}] ",
        "prefix_from_qq": "[QQ {name}] ",
        "admins": [10001],
    }
    ctx = Context(workdir)
    ctx.config = config
    cfg = module.Config.model_validate(config)

    # 1) 命令展示页
    print("== /help 命令展示页 ==")
    module.register_commands(ctx, cfg)
    commands = ctx.app_commands.commands
    for name, command in commands.items():
        aliases = f" 别名：{'、'.join(command.aliases)}" if command.aliases else ""
        usage = f" 用法：/{name} {command.usage}".rstrip()
        print(f"  /{name}{aliases}")
        print(f"      {command.description}")
        print(f"     {usage}")
        for param_name, param_desc in command.params:
            print(f"      - {param_name}：{param_desc}")
    check("注册了 8 条快捷命令", len(commands) == 8, str(sorted(commands)))
    check("别名 tgbind 指向 /tg绑定", ctx.app_commands.commands["tg绑定"].aliases[0] == "tgbind")
    check("每条命令都有 description", all(item.description for item in commands.values()))

    runtime = module.TelegramRuntime(ctx, cfg)
    runtime.api = FakeAPI()
    runtime.chats["-1001234567890"] = {
        "id": "-1001234567890",
        "type": "supergroup",
        "title": "测试 TG 群",
        "last_seen": 0,
    }

    # 2) Telegram → QQ
    print("\n== Telegram → QQ ==")
    binding = runtime.bind_chat(tg_chat_id="-1001234567890", qq_kind="group", qq_id="123456789")
    print(f"  绑定：{binding.describe()}")
    update = {
        "update_id": 1,
        "message": {
            "message_id": 7,
            "date": 1700000000,
            "chat": {"id": -1001234567890, "type": "supergroup", "title": "测试 TG 群"},
            "from": {"id": 7, "first_name": "小明", "username": "ming"},
            "text": "在 Telegram 里说话",
        },
    }
    asyncio.run(runtime.handle_updates([update]))
    check("投递到绑定的 QQ 群", len(ctx.to_qq) == 1, str(ctx.to_qq))
    if ctx.to_qq:
        print(f"  QQ 收到：{ctx.to_qq[-1][2]!r}")
    check("带上了 TG 前缀与发言人", "TG" in ctx.to_qq[-1][2] and "ming" in ctx.to_qq[-1][2])

    # 3) QQ → Telegram
    print("\n== QQ → Telegram ==")
    event = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 123456789,
        "user_id": 20001,
        "self_id": 99999,
        "sender": {"card": "群友甲"},
        "message": [{"type": "text", "data": {"text": "在 QQ 里回复"}}],
    }
    sent = asyncio.run(runtime.forward_from_qq(event))
    check("转发到 Telegram", sent == 1 and runtime.api.calls, str(runtime.api.calls))
    if runtime.api.calls:
        print(f"  Telegram 收到：{runtime.api.calls[-1][1].get('text')!r}")
    check(
        "转成 HTML 且带 QQ 前缀",
        "QQ" in runtime.api.calls[-1][1]["text"] and "在 QQ 里回复" in runtime.api.calls[-1][1]["text"],
    )

    # 4) 权限
    print("\n== 权限 ==")
    member_ctx = SimpleNamespace(
        kind="group",
        conv_id="123456789",
        user_id=20001,
        raw_args="-100999",
        command=SimpleNamespace(name="tg绑定"),
        service=SimpleNamespace(permissions=SimpleNamespace(can=lambda uid, level: False)),
    )
    reply = asyncio.run(module._run_command(ctx, runtime, "bind", member_ctx))
    check("普通成员被拒", "权限不足" in reply, reply)
    print(f"  回复：{reply}")

    admin_ctx = SimpleNamespace(
        kind="group",
        conv_id="123456789",
        user_id=10001,
        raw_args="-100999",
        command=SimpleNamespace(name="tg绑定"),
        service=SimpleNamespace(permissions=SimpleNamespace(can=lambda uid, level: False)),
    )
    reply = asyncio.run(module._run_command(ctx, runtime, "bind", admin_ctx))
    check("插件管理员放行", "已绑定" in reply, reply)

    # 5) 诊断
    print("\n== 诊断 ==")
    diag = runtime.diag_text()
    print("\n".join("  " + line for line in diag.splitlines()))
    check("token 已打码", "123456:***" in diag and "SIMULATED" not in diag)
    check("计数齐全", "tg_to_qq=1" in diag and "qq_to_tg=1" in diag)

    print()
    # 收尾：别把模拟数据留在插件目录里（SDK check 会遍历目录）
    shutil.rmtree(workdir, ignore_errors=True)
    try:
        scratch.rmdir()  # 空目录顺手删掉
    except OSError:
        pass
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：{'、'.join(FAILURES)}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
