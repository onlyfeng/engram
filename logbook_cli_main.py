#!/usr/bin/env python3
"""
logbook_cli_main - 弃用兼容入口（薄包装器）

[DEPRECATED] 此脚本已弃用。
计划移除: v2.0.0 或 2026-Q2

替代方案:
    engram-logbook [command] [options]
    engram-logbook artifacts [command] [options]
    engram-logbook scm [command] [options]

兼容导出:
    此包装器通过 __getattr__ 支持延迟导入 scripts/logbook_cli_main.py 中的符号。
"""

from __future__ import annotations

import sys
from pathlib import Path

_DEPRECATION_MSG = """\
[DEPRECATED] logbook_cli_main.py 已弃用。
计划移除: v2.0.0 或 2026-Q2

替代方案:
  engram-logbook [command] [options]
  engram-logbook artifacts [command] [options]
  engram-logbook scm [command] [options]"""

_SCRIPT_PATH = Path(__file__).resolve().parent / "scripts" / "logbook_cli_main.py"

# ---------------------------------------------------------------------------
# 兼容导出（延迟加载）
# ---------------------------------------------------------------------------
_module_cache = None


def _load_module():
    """延迟加载实际模块"""
    global _module_cache
    if _module_cache is None:
        import importlib.util
        module_name = "_logbook_cli_main_impl"
        spec = importlib.util.spec_from_file_location(module_name, _SCRIPT_PATH)
        _module_cache = importlib.util.module_from_spec(spec)
        # 需要在 exec_module 前将模块添加到 sys.modules，否则 dataclass 装饰器会失败
        sys.modules[module_name] = _module_cache
        spec.loader.exec_module(_module_cache)
    return _module_cache


def __getattr__(name: str):
    """延迟加载 scripts/logbook_cli_main.py 中的属性"""
    return getattr(_load_module(), name)


def main() -> None:
    """入口函数，打印弃用警告并转发到权威入口"""
    import runpy
    print(_DEPRECATION_MSG, file=sys.stderr)
    runpy.run_path(str(_SCRIPT_PATH), run_name="__main__")


if __name__ == "__main__":
    main()
