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
- v1.2 (2026-02-02): 引入 Tier C 分层（便捷/内部层），明确失败语义

Tier 分层定义
=============
- Tier A（核心稳定层）: 主版本内接口不变，插件作者优先依赖
- Tier B（可选依赖层）: 主版本内接口不变，失败时抛出 ImportError + 安装指引
- Tier C（便捷/内部层）: 可能在次版本调整签名，建议使用 Tier A 替代方案

导出清单
========

Tier A（核心稳定，直接导入）:
- RequestContext: 请求上下文 dataclass
- GatewayDepsProtocol: 依赖容器 Protocol（推荐用于类型注解）
- GatewayDeps: 依赖容器实现类
- WriteAuditPort: 审计写入接口
- UserDirectoryPort: 用户目录接口
- ActorPolicyConfigPort: Actor 策略配置接口
- ToolExecutorPort, ToolRouterPort: 工具执行器端口
- ToolDefinition, ToolCallContext, ToolCallResult: 工具调用数据类
- McpErrorCode, McpErrorCategory, McpErrorReason: 错误码常量
- ToolResultErrorCode: 工具执行结果错误码

Tier B（可选依赖，延迟导入，失败时抛出 ImportError）:
- LogbookAdapter: Logbook 数据库适配器（需要 engram_logbook）
- get_adapter: 获取 LogbookAdapter 单例
- get_reliability_report: 获取可靠性统计报告
- execute_tool: MCP 工具执行入口
- dispatch_jsonrpc_request: JSON-RPC 请求分发便捷函数
- JsonRpcDispatchResult: JSON-RPC 分发结果类型

Tier C（便捷/内部，可能在次版本调整）:
- create_request_context: 便捷函数，建议直接用 RequestContext(...)
- create_gateway_deps: 便捷函数，建议直接用 GatewayDeps(...)
- generate_correlation_id: 便捷函数，通常由中间件自动生成

Tier B 失败语义
===============
当 Tier B 符号依赖的模块不可用时，在 ``from ... import`` 语句执行时即触发懒加载
并抛出 ImportError。Python 的 ``from module import name`` 会调用 ``__getattr__(name)``，
因此 **import 语句执行时即报错**，而非延迟到符号使用时。

错误消息格式（必须包含以下字段）::

    ImportError: 无法导入 '{symbol_name}'（来自 {module_path}）

    原因: {original_error}

    {install_hint}

错误消息字段说明:
- symbol_name: 导入失败的符号名（如 LogbookAdapter）
- module_path: 来源模块路径（如 .logbook_adapter）
- original_error: 原始 ImportError 消息
- install_hint: 安装指引

示例::

    # engram_logbook 未安装时，以下 import 语句会立即抛出 ImportError
    from engram.gateway.public_api import LogbookAdapter
    # ImportError: 无法导入 'LogbookAdapter'（来自 .logbook_adapter）
    #
    # 原因: No module named 'engram_logbook'
    #
    # 此功能需要 engram_logbook 模块。
    # 请安装：pip install -e ".[full]" 或 pip install engram-logbook

使用示例
========

插件开发者推荐导入方式（优先 Protocol/错误码）::

    from engram.gateway.public_api import (
        # ✅ Tier A: Protocol（依赖抽象，便于测试 mock）
        RequestContext,
        GatewayDepsProtocol,
        WriteAuditPort,
        UserDirectoryPort,
        # ✅ Tier A: 错误码
        McpErrorCode,
        McpErrorReason,
    )

    # 定义自定义 handler
    async def my_handler(
        ctx: RequestContext,
        deps: GatewayDepsProtocol,  # ← 使用 Protocol 而非实现类
    ) -> dict:
        ...

测试中使用 mock 依赖::

    from engram.gateway.public_api import GatewayDeps, RequestContext

    # 创建测试依赖（严格模式）
    deps = GatewayDeps.for_testing(
        config=fake_config,
        logbook_adapter=fake_adapter,
    )

    # 创建测试上下文
    ctx = RequestContext(
        correlation_id="corr-test123",
        actor_user_id="test-user",
    )

Tier B 符号的安全使用（检查依赖可用性）::

    try:
        from engram.gateway.public_api import LogbookAdapter
        LOGBOOK_AVAILABLE = True
    except ImportError:
        LOGBOOK_AVAILABLE = False

    # 在代码中检查
    if not LOGBOOK_AVAILABLE:
        raise RuntimeError("此插件需要 engram_logbook 模块")

相关文档
========
- 向后兼容策略: docs/contracts/gateway_contract_convergence.md §11
- 导出项分析: docs/architecture/gateway_public_api_surface.md
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
    # mcp_rpc 模块（JSON-RPC 分发入口）
    "dispatch_jsonrpc_request": (".mcp_rpc", "dispatch_jsonrpc_request"),
    "JsonRpcDispatchResult": (".mcp_rpc", "JsonRpcDispatchResult"),
}

# 安装指引映射（统一格式：功能说明 + 安装命令）
_TIER_B_INSTALL_HINTS: dict[str, str] = {
    ".logbook_adapter": (
        "此功能需要 engram_logbook 模块。\n"
        '请安装：pip install -e ".[full]" 或 pip install engram-logbook'
    ),
    ".entrypoints.tool_executor": (
        '此功能需要完整的 Gateway 工具执行器依赖。\n请安装：pip install -e ".[full]"'
    ),
    ".mcp_rpc": ('此功能需要 MCP RPC 支持模块。\n请安装：pip install -e ".[full]"'),
}

# ======================== 依赖缺失 ImportError 消息模板 ========================

# 错误消息模板，包含以下字段：
# - symbol_name: 导入失败的符号名（如 LogbookAdapter）
# - module_path: 来源模块路径（如 .logbook_adapter）
# - original_error: 原始 ImportError 消息
# - install_hint: 安装指引
_IMPORT_ERROR_TEMPLATE = """\
无法导入 '{symbol_name}'（来自 {module_path}）

原因: {original_error}

{install_hint}"""


def _format_import_error(
    symbol_name: str,
    module_path: str,
    original_error: BaseException,
    install_hint: str,
) -> str:
    """
    格式化依赖缺失的 ImportError 消息

    Args:
        symbol_name: 导入失败的符号名（如 LogbookAdapter）
        module_path: 来源模块路径（如 .logbook_adapter）
        original_error: 原始 ImportError 异常
        install_hint: 安装指引文本

    Returns:
        格式化后的错误消息字符串
    """
    return _IMPORT_ERROR_TEMPLATE.format(
        symbol_name=symbol_name,
        module_path=module_path,
        original_error=str(original_error),
        install_hint=install_hint,
    )


# ======================== 导出清单 ========================

__all__ = [
    # ══════════════════════════════════════════════════════════════════
    # Tier A: 核心稳定层（主版本内接口不变）
    # ══════════════════════════════════════════════════════════════════
    # Tier A: 核心类型
    "RequestContext",
    "GatewayDeps",
    "GatewayDepsProtocol",
    # Tier A: 服务端口 Protocol（插件作者优先依赖）
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
    # ══════════════════════════════════════════════════════════════════
    # Tier B: 可选依赖层（延迟导入，失败时抛 ImportError + 安装指引）
    # ══════════════════════════════════════════════════════════════════
    # Tier B: 适配器（需要 engram_logbook）
    "LogbookAdapter",
    "get_adapter",
    "get_reliability_report",
    # Tier B: 工具执行（需要 Gateway 完整依赖）
    "execute_tool",
    # Tier B: JSON-RPC 分发入口
    "dispatch_jsonrpc_request",
    "JsonRpcDispatchResult",
    # ══════════════════════════════════════════════════════════════════
    # Tier C: 便捷/内部层（可能在次版本调整签名）
    # ══════════════════════════════════════════════════════════════════
    "create_request_context",
    "create_gateway_deps",
    "generate_correlation_id",
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
            error_message = _format_import_error(
                symbol_name=name,
                module_path=module_path,
                original_error=e,
                install_hint=install_hint,
            )
            raise ImportError(error_message) from e

    raise AttributeError(f"模块 {__name__!r} 没有属性 {name!r}")
