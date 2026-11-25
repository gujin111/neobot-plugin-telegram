"""测试用的插件加载器。

放在独立模块而不是 conftest 里：``--import-mode=importlib`` 下测试模块不能
``from conftest import ...``，而插件目录名带连字符、根目录又有 ``__init__.py``，
必须手工构造包模块才能让 ``__init__.py`` 的相对导入生效。
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_CACHED = None


def load_plugin():
    """加载插件入口模块（需要 neobot_modloader）。"""
    global _CACHED
    if _CACHED is not None:
        return _CACHED
    if importlib.util.find_spec("neobot_modloader") is None:
        pytest.skip("neobot_modloader 不可用：请把 PYTHONPATH 指向 NeoBot 的 packages/*/src")
    name = "telegram_plugin"
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / "__init__.py"))
    module = types.ModuleType(name)
    module.__file__ = str(ROOT / "__init__.py")
    module.__path__ = [str(ROOT)]
    module.__package__ = name
    sys.modules[name] = module
    loader.exec_module(module)
    _CACHED = module
    return module
