"""静态兼容审计：把插件用到的宿主 API 逐个对照 NeoBot 源码。

不启动宿主、不需要依赖装好，只做 AST/源码比对——这是 1.0.0 那次「拿仓库 main 当
用户环境」事故留下的教训：**结论必须来自某个具体版本源码**，所以本工具强制
``--ref`` 指定宿主目录（发布包解压后的根目录，含 ``packages/``）。

检查四项：

1. ``ctx.<成员>``（含 ``self.ctx.<成员>``）是否都在该版本的 ``RuntimePluginContext`` 上；
2. ``ctx.app_commands.<成员>`` 是否在 ``PluginCommandRegistrar`` 上；
3. ``ctx.services.<成员>`` 是否在 ``ServiceAccess`` 上；
4. 命令注册用到的关键字参数是否被 ``PluginCommandRegistrar.register`` 接受，
   以及事件处理器的注入参数名是否在该版本的注入表里。

用法：

    python tools/check_compat.py --ref <宿主目录> [--ref <另一个宿主目录>] [--json]
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import re
import sys
from typing import Any

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN_FILES = ["__init__.py", "tg_api.py", "tg_model.py", "tg_send.py", "tg_bridge.py"]

#: 这些成员由运行时按需 duck-typing 提供，缺失时插件自己已做降级，不算不兼容
OPTIONAL_CTX = {"services", "app_commands", "logger", "adapter", "users", "plugin_host", "config", "data_dir"}


def _class_members(tree: ast.AST, class_name: str) -> set[str]:
    """收集类里 self.X 赋值、def X、@property X。"""
    members: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                members.add(item.name)
            elif isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        members.add(target.id)
                    elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
                        members.add(target.attr)
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                members.add(item.target.id)
            elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                members.add(item.name)
    # __init__ 里的 self.x = ...
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            members.add(node.attr)
    return members


def _function_params(tree: ast.AST, class_name: str, func_name: str) -> tuple[set[str], bool]:
    """返回 (参数名集合, 是否接受 **kwargs)。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == func_name:
                    args = item.args
                    names = {arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
                    names.discard("self")
                    return names, args.kwarg is not None
    return set(), False


def _plugin_ctx_usage() -> dict[str, set[str]]:
    """扫描插件：ctx.X / ctx.app_commands.X / ctx.services.X / ctx.adapter.X。"""
    used: dict[str, set[str]] = {"ctx": set(), "app_commands": set(), "services": set(), "adapter": set()}
    for name in PLUGIN_FILES:
        path = PLUGIN_ROOT / name
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            # getattr(ctx, "app_commands", None) 这类写法也要算进来
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "getattr" and len(node.args) >= 2:
                owner, attr = node.args[0], node.args[1]
                owner_name = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
                if owner_name in ("ctx", "context") and isinstance(attr, ast.Constant) and isinstance(attr.value, str):
                    used["ctx"].add(attr.value)
                continue
            if not isinstance(node, ast.Attribute):
                continue
            base = node.value
            # ctx.X
            if isinstance(base, ast.Name) and base.id in ("ctx", "context"):
                used["ctx"].add(node.attr)
                continue
            # *.ctx.X
            if isinstance(base, ast.Attribute) and base.attr == "ctx":
                used["ctx"].add(node.attr)
                continue
            # ctx.app_commands.X / ctx.services.X / ctx.adapter.X
            if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name) and base.value.id in ("ctx", "context"):
                if base.attr in used:
                    used[base.attr].add(node.attr)
    return used


def _registration_kwargs() -> set[str]:
    """插件里 ctx.app_commands.register(...) / registrar.register(...) 用到的关键字。"""
    kwargs: set[str] = set()
    for name in PLUGIN_FILES:
        path = PLUGIN_ROOT / name
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            target = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if target != "register":
                continue
            kwargs.update(kw.arg for kw in node.keywords if kw.arg)
    return kwargs


def _handler_params() -> set[str]:
    """@plugin.message / @plugin.on_* 处理器的注入参数名（不含 self）。"""
    path = PLUGIN_ROOT / "__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorated = False
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(target, ast.Attribute) and target.attr in ("message", "event", "on_load", "on_startup", "on_shutdown"):
                decorated = True
        if not decorated:
            continue
        for arg in (*node.args.posonlyargs, *node.args.args):
            if arg.arg != "self":
                names.add(arg.arg)
    return names


def audit(ref: pathlib.Path) -> dict[str, Any]:
    modloader = ref / "packages" / "modloader" / "src" / "neobot_modloader"
    context_py = modloader / "context.py"
    service_py = modloader / "service_registry.py"
    if not context_py.is_file():
        raise SystemExit(f"找不到宿主源码：{context_py}（--ref 要指向发布包根目录）")

    context_tree = ast.parse(context_py.read_text(encoding="utf-8"), filename=str(context_py))
    ctx_members = _class_members(context_tree, "RuntimePluginContext")
    registrar_members = _class_members(context_tree, "PluginCommandRegistrar")
    registrar_params, registrar_accepts_kwargs = _function_params(context_tree, "PluginCommandRegistrar", "register")
    service_members = (
        _class_members(ast.parse(service_py.read_text(encoding="utf-8")), "ServiceAccess") if service_py.is_file() else set()
    )

    # 注入表：把 modloader 包里所有字符串字面量收集起来做简单存在性判断
    literals: set[str] = set()
    for path in modloader.rglob("*.py"):
        literals.update(re.findall(r"[\"']([a-z_][a-z0-9_]*)[\"']", path.read_text(encoding="utf-8")))

    used = _plugin_ctx_usage()
    problems: list[str] = []

    missing_ctx = {name for name in used["ctx"] if name not in ctx_members and name not in OPTIONAL_CTX}
    missing_registrar = {name for name in used["app_commands"] if name not in registrar_members}
    missing_services = {name for name in used["services"] if name not in service_members}
    bad_kwargs = (
        set()
        if registrar_accepts_kwargs
        else {name for name in _registration_kwargs() if registrar_params and name not in registrar_params}
    )
    bad_handler_params = {name for name in _handler_params() if name not in literals}

    if missing_ctx:
        problems.append(f"RuntimePluginContext 缺少：{sorted(missing_ctx)}")
    if missing_registrar:
        problems.append(f"PluginCommandRegistrar 缺少：{sorted(missing_registrar)}")
    if missing_services:
        problems.append(f"ServiceAccess 缺少：{sorted(missing_services)}")
    if bad_kwargs:
        problems.append(f"register() 不接受的关键字：{sorted(bad_kwargs)}")
    if bad_handler_params:
        problems.append(f"注入参数名未在宿主源码出现：{sorted(bad_handler_params)}")

    return {
        "ref": str(ref),
        "version": _read_version(ref),
        "ctx_used": sorted(used["ctx"]),
        "registrar_members": sorted(registrar_members),
        "registrar_params": sorted(registrar_params),
        "accepts_kwargs": registrar_accepts_kwargs,
        "handler_params": sorted(_handler_params()),
        "ok": not problems,
        "problems": problems,
    }


def _read_version(ref: pathlib.Path) -> str:
    pyproject = ref / "pyproject.toml"
    if pyproject.is_file():
        match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.MULTILINE)
        if match:
            return match.group(1)
    return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description="NeoBot 宿主 API 兼容审计")
    parser.add_argument("--ref", action="append", required=True, help="宿主发布包根目录（可多次）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    results = []
    for raw in args.ref:
        ref = pathlib.Path(raw).resolve()
        results.append(audit(ref))

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for item in results:
            status = "OK  " if item["ok"] else "FAIL"
            print(f"[{status}] {item['version']}  ({item['ref']})")
            print(f"    ctx 使用：{'、'.join(item['ctx_used'])}")
            print(f"    register 接受：{'、'.join(item['registrar_params'])}{' + **kwargs' if item.get('accepts_kwargs') else ''}")
            print(f"    处理器注入参数：{'、'.join(item['handler_params'])}")
            for problem in item["problems"]:
                print(f"    x {problem}")
    return 0 if all(item["ok"] for item in results) else 1


if __name__ == "__main__":
    sys.exit(main())
