"""
engram.gateway - MCP Gateway 模块

提供 MCP Server 入口，连接 Cursor IDE 与 OpenMemory，负责：
- 治理开关和策略校验 (team_write_enabled + policy)
- 写入审计 (governance.write_audit)
- 失败降级 (logbook.outbox_memory)
- OpenMemory API 调用

子模块：
- logbook_adapter: Logbook 适配器
- openmemory_client: OpenMemory HTTP API 客户端
- outbox_worker: Outbox 队列处理 Worker
- mcp_rpc: MCP JSON-RPC 2.0 协议实现
- policy: 策略决策引擎

懒加载策略：
- import engram.gateway 不触发子模块加载
- 访问 engram.gateway.logbook_adapter 等属性时才按需加载
- 静态类型提示通过 TYPE_CHECKING 块支持
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "logbook_adapter",
    "openmemory_client",
    "outbox_worker",
]

# TYPE_CHECKING 块仅用于静态类型提示，不触发实际导入
if TYPE_CHECKING:
    from . import logbook_adapter as logbook_adapter
    from . import openmemory_client as openmemory_client
    from . import outbox_worker as outbox_worker

# 懒加载子模块列表
_LAZY_SUBMODULES = {"logbook_adapter", "openmemory_client", "outbox_worker"}


def __getattr__(name: str):
    """懒加载子模块

    仅在访问属性时才导入子模块，避免 import engram.gateway 时
    触发整个依赖链加载。

    Args:
        name: 属性名

    Returns:
        对应的子模块

    Raises:
        AttributeError: 属性不存在
    """
    if name in _LAZY_SUBMODULES:
        import importlib

        module = importlib.import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
