"""pytest 引导：插件/tests 目录进 sys.path + 自建 scratch 临时目录。

* 插件目录：让 ``tg_api`` / ``tg_model`` / ``tg_send`` / ``tg_bridge`` 可作为顶层模块导入；
* tests 目录：让 ``test_plugin.py`` 能 ``from _loader import load_plugin``
  （``--import-mode=importlib`` 不会自动加这个目录）；
* 不用 pytest 的 ``tmp_path``：本机沙箱下它创建 ``<temp>/pytest-of-<user>`` 会 WinError 5，
  统一改用插件目录里的 ``.scratch/``。

注意别把本文件放进插件根目录：根目录有 ``__init__.py``，pytest 会把根目录当包导入，
从而在收集期触发 ``__init__.py`` 的相对导入失败。
"""

from __future__ import annotations

import pathlib
import shutil
import sys
import uuid

import pytest

_HERE = pathlib.Path(__file__).resolve().parent
_PLUGIN_ROOT = _HERE.parent

for _path in (str(_PLUGIN_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

SCRATCH_ROOT = _PLUGIN_ROOT / ".scratch"


@pytest.fixture
def scratch():
    """每个用例一个独立目录（替代 tmp_path，避开沙箱下的 pytest tmpdir 限制）。"""
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    path = SCRATCH_ROOT / f"case-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
