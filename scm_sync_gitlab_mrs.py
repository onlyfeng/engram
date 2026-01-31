#!/usr/bin/env python3
"""
scm_sync_gitlab_mrs - 弃用兼容入口

已弃用: 此脚本将在后续版本移除。
请使用: python scripts/scm_sync_gitlab_mrs.py
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

_DEPRECATION_MSG = (
    "警告: 根目录的 scm_sync_gitlab_mrs.py 已弃用，将在后续版本移除。\n"
    "请使用: python scripts/scm_sync_gitlab_mrs.py"
)

_SCRIPT_PATH = Path(__file__).resolve().parent / "scripts" / "scm_sync_gitlab_mrs.py"


def _load_module():
    """延迟加载实际模块以支持兼容导出"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_scm_sync_gitlab_mrs_impl", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def __getattr__(name):
    module = _load_module()
    return getattr(module, name)


def main() -> None:
    print(_DEPRECATION_MSG, file=sys.stderr)
    runpy.run_path(str(_SCRIPT_PATH), run_name="__main__")


if __name__ == "__main__":
    main()
