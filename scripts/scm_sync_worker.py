#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scm_sync_worker - SCM 同步 Worker CLI 入口（兼容层）

导入场景:
- 将 scripts.scm_sync_worker 直接映射到核心实现模块，保证 monkeypatch 生效一致

执行场景:
- 转发到 engram.logbook.cli.scm_sync.worker_main
"""

from __future__ import annotations

import importlib
import sys
import warnings
from pathlib import Path

# 确保根目录在 sys.path 中，以支持包导入
_ROOT_DIR = Path(__file__).resolve().parent.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))


def main():
    """CLI 入口函数 - 转发到 engram.logbook.cli.scm_sync.worker_main"""
    warnings.warn(
        "scripts/scm_sync_worker.py 已弃用，请使用 'python -m engram.logbook.cli.scm_sync worker' "
        "或 'engram-scm-worker' 代替",
        DeprecationWarning,
        stacklevel=2,
    )
    from engram.logbook.cli.scm_sync import worker_main

    return worker_main()


if __name__ == "__main__":
    sys.exit(main())

# 作为模块导入时：直接复用核心模块对象，保证 patch("scripts.scm_sync_worker.*")
# 与 patch("scm_sync_worker.*") 一致作用到同一实现。
_impl = importlib.import_module("engram.logbook.scm_sync_worker_core")
setattr(_impl, "main", main)
if isinstance(getattr(_impl, "__all__", None), list) and "main" not in _impl.__all__:
    _impl.__all__ = [*_impl.__all__, "main"]
sys.modules[__name__] = _impl
