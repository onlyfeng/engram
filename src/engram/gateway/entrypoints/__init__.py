"""
Gateway Entrypoints 模块

提供可被外部调用的入口函数，保证 import-safe 特性。

设计原则：
===========
1. Import-Safe: 模块导入时不触发 get_config()/get_container()
2. 依赖通过参数注入：使用 Callable[[], GatewayDepsProtocol] 延迟获取
3. 单一职责: 每个 entrypoint 模块只暴露一个核心函数

模块结构：
- tool_executor: execute_tool() / DefaultToolExecutor - MCP 工具执行入口
"""

from .tool_executor import DefaultToolExecutor, execute_tool

__all__ = [
    "execute_tool",
    "DefaultToolExecutor",
]
