"""
engram_logbook 兼容包

提供与 engram.logbook 等价的导入路径，满足旧测试与兼容接口。
"""

from __future__ import annotations

import importlib
import sys

_base = importlib.import_module("engram.logbook")

# 复用原包的搜索路径，支持 submodule 导入
__path__ = _base.__path__

# 复制公开属性（避免覆盖包元信息）
for _name, _value in _base.__dict__.items():
    if _name in {"__name__", "__package__", "__loader__", "__spec__", "__path__"}:
        continue
    globals()[_name] = _value

__all__ = getattr(_base, "__all__", [])

# 预注册常用子模块，避免 engram_logbook 与 engram.logbook 双重加载
_ALIAS_MODULES = [
    "config",
    "errors",
    "uri",
    "db",
    "migrate",
    "artifact_store",
    "artifact_ops_audit",
    "outbox",
    "scm_sync_queue",
    "scm_sync_policy",
    "scm_sync_errors",
    "scm_sync_payload",
    "scm_sync_runner",
    "scm_sync_worker",
    "scm_sync_job_types",
    "scm_sync_keys",
    "scm_sync_run_contract",
]

for _module_name in _ALIAS_MODULES:
    try:
        _module = importlib.import_module(f"engram.logbook.{_module_name}")
    except Exception:
        continue
    sys.modules[f"{__name__}.{_module_name}"] = _module
