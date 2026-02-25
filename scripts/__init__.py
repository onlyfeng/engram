"""
scripts 包初始化。

支持两类场景：
1. 正常 `import scripts.xxx` 导入子模块
2. 某些测试清理 `sys.modules["scripts"]` 后，仍能通过属性访问恢复子模块绑定
"""

from __future__ import annotations

import importlib
import sys


def __getattr__(name: str):
    """
    按需恢复 scripts 子模块属性绑定。

    当 `scripts` 包对象被重建但 `scripts.<submodule>` 仍在 `sys.modules` 中时，
    通过该钩子确保 `scripts.<submodule>` 属性可解析。
    """
    module_name = f"{__name__}.{name}"

    module = sys.modules.get(module_name)
    if module is None:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name == module_name:
                raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
            raise

    globals()[name] = module
    return module
