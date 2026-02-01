"""
Gateway 公共 API 模块 (Public API)

此模块集中导出所有供插件作者和外部集成使用的稳定接口。
**插件作者只应从此模块导入**，以获得版本兼容性保证。

兼容性承诺
==========
本模块导出的所有符号在相同主版本号内保持向后兼容：
- Protocol 接口方法签名不变
- 公共类的构造函数和工厂方法签名不变
- 返回类型不变

版本说明
========
- v1.0 (2026-02-01): 初始版本
- v1.1 (2026-02-02): 引入 Tier A/B 分层延迟导入策略

导出清单
========

Tier A（核心稳定，直接导入）:
- RequestContext: 请求上下文 dataclass
- GatewayDepsProtocol: 依赖容器 Protocol
- GatewayDeps: 依赖容器实现类
- generate_correlation_id: 生成 correlation_id 的便捷函数
- WriteAuditPort: 审计写入接口
- UserDirectoryPort: 用户目录接口
- ActorPolicyConfigPort: Actor 策略配置接口
- McpErrorCode, McpErrorCategory, McpErrorReason: 错误码常量
- ToolResultErrorCode: 工具执行结果错误码

Tier B（可选依赖，延迟导入）:
- LogbookAdapter: Logbook 数据库适配器（需要 engram_logbook）
- get_adapter: 获取 LogbookAdapter 单例
- get_reliability_report: 获取可靠性统计报告
- execute_tool: MCP 工具执行入口
- dispatch_jsonrpc_request: 统一的 JSON-RPC 请求分发入口
- JsonRpcDispatchResult: JSON-RPC 请求处理结果封装

使用示例
========

插件开发者推荐导入方式::

    from engram.gateway.public_api import (
        RequestContext,
        GatewayDepsProtocol,
        WriteAuditPort,
        UserDirectoryPort,
    )

    # 定义自定义 handler
    async def my_handler(
        ctx: RequestContext,
        deps: GatewayDepsProtocol,
    ) -> dict:
        # 使用类型注解获得 IDE 支持
        adapter = deps.logbook_adapter
        ...

测试中使用 mock 依赖::

    from engram.gateway.public_api import GatewayDeps, RequestContext

    # 创建测试依赖（严格模式）
    deps = GatewayDeps.for_testing(
        config=fake_config,
        logbook_adapter=fake_adapter,
    )

    # 创建测试上下文
    ctx = RequestContext.for_testing(actor_user_id="test-user")

延迟导入（Tier B 符号）::

    # Tier B 符号在首次访问时才导入底层模块
    # 如果 engram_logbook 未安装，会抛出 ImportError 并提示安装指引
    from engram.gateway.public_api import LogbookAdapter  # 触发延迟导入
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

# ======================== Tier A: 核心类型（直接导入） ========================
from .di import (
    GatewayDeps,
    GatewayDepsProtocol,
    RequestContext,
    # 便捷函数
    create_gateway_deps,
    create_request_context,
    generate_correlation_id,
)

# ======================== Tier A: 错误码常量（直接导入） ========================
from .error_codes import McpErrorCategory, McpErrorCode, McpErrorReason
from .result_error_codes import ToolResultErrorCode

# ======================== Tier A: 服务端口 Protocol（直接导入） ========================
from .services.ports import (
    ActorPolicyConfigPort,
    ToolCallContext,
    ToolCallResult,
    ToolDefinition,
    ToolExecutorPort,
    ToolRouterPort,
    UserDirectoryPort,
    WriteAuditPort,
)

# ======================== Tier B: 类型注解专用（仅供 IDE/mypy） ========================
if TYPE_CHECKING:
    from .entrypoints.tool_executor import execute_tool as execute_tool
    from .logbook_adapter import LogbookAdapter as LogbookAdapter
    from .logbook_adapter import get_adapter as get_adapter
    from .logbook_adapter import get_reliability_report as get_reliability_report
    from .mcp_rpc import JsonRpcDispatchResult as JsonRpcDispatchResult
    from .mcp_rpc import dispatch_jsonrpc_request as dispatch_jsonrpc_request

# ======================== 延迟导入映射表 ========================

# Tier B 符号到其源模块的映射
_TIER_B_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # logbook_adapter 模块（依赖 engram_logbook）
    "LogbookAdapter": (".logbook_adapter", "LogbookAdapter"),
    "get_adapter": (".logbook_adapter", "get_adapter"),
    "get_reliability_report": (".logbook_adapter", "get_reliability_report"),
    # tool_executor 模块
    "execute_tool": (".entrypoints.tool_executor", "execute_tool"),
    # mcp_rpc 模块
    "dispatch_jsonrpc_request": (".mcp_rpc", "dispatch_jsonrpc_request"),
    "JsonRpcDispatchResult": (".mcp_rpc", "JsonRpcDispatchResult"),
}

# 安装指引映射
_TIER_B_INSTALL_HINTS: dict[str, str] = {
    ".logbook_adapter": (
        "此功能需要 engram_logbook 模块。\n"
        '请安装：pip install -e ".[full]" 或 pip install engram-logbook'
    ),
    ".entrypoints.tool_executor": (
        '此功能需要完整的 Gateway 依赖。\n请确保已安装所有依赖：pip install -e ".[full]"'
    ),
    ".mcp_rpc": ('此功能需要 MCP RPC 支持模块。\n请确保已安装所有依赖：pip install -e ".[full]"'),
}

# ======================== 导出清单 ========================

__all__ = [
    # Tier A: 核心类型
    "RequestContext",
    "GatewayDeps",
    "GatewayDepsProtocol",
    # Tier A: 便捷函数
    "create_request_context",
    "create_gateway_deps",
    "generate_correlation_id",
    # Tier A: 服务端口 Protocol
    "WriteAuditPort",
    "UserDirectoryPort",
    "ActorPolicyConfigPort",
    # Tier A: 工具执行器端口
    "ToolExecutorPort",
    "ToolRouterPort",
    "ToolDefinition",
    "ToolCallContext",
    "ToolCallResult",
    # Tier A: 错误码常量
    "McpErrorCode",
    "McpErrorCategory",
    "McpErrorReason",
    "ToolResultErrorCode",
    # Tier B: 适配器（延迟导入）
    "LogbookAdapter",
    "get_adapter",
    "get_reliability_report",
    # Tier B: 工具执行（延迟导入）
    "execute_tool",
    # Tier B: JSON-RPC 入口（延迟导入，方便 patch 测试）
    "dispatch_jsonrpc_request",
    "JsonRpcDispatchResult",
]


def __getattr__(name: str) -> Any:
    """
    延迟导入 Tier B 符号。

    当访问 Tier B 符号时，按需导入对应模块并返回真实对象。
    导入失败时抛出 ImportError，包含模块名和安装指引。

    Args:
        name: 请求的属性名

    Returns:
        延迟导入的模块或符号

    Raises:
        ImportError: 如果依赖模块不可用
        AttributeError: 如果请求的属性不存在
    """
    if name in _TIER_B_LAZY_IMPORTS:
        module_path, attr_name = _TIER_B_LAZY_IMPORTS[name]

        try:
            # 延迟导入目标模块
            module = importlib.import_module(module_path, __package__)
            # 获取目标符号
            obj = getattr(module, attr_name)
            # 缓存到模块全局命名空间，避免重复导入
            globals()[name] = obj
            return obj
        except ImportError as e:
            # 构建用户友好的错误消息
            install_hint = _TIER_B_INSTALL_HINTS.get(
                module_path,
                '请确保已安装所有依赖：pip install -e ".[full]"',
            )
            raise ImportError(
                f"无法导入 '{name}'（来自 {module_path}）: {e}\n\n{install_hint}"
            ) from e

    raise AttributeError(f"模块 {__name__!r} 没有属性 {name!r}")
