"""
MCP 工具执行入口模块

提供 execute_tool() 函数和 DefaultToolExecutor 类，用于执行 MCP 工具调用。

设计原则：
===========
1. Import-Safe: 模块导入时不触发 get_config()/get_container()
2. 依赖通过 get_deps 回调注入，支持延迟获取
3. 返回结果始终包含 correlation_id（单一来源原则）

使用方式：
=========
    # 函数式调用
    from .entrypoints import execute_tool
    from .dependencies import get_deps_for_request

    result = await execute_tool(
        tool="memory_store",
        args={"payload_md": "..."},
        correlation_id="corr-abc123...",
        get_deps=get_deps_for_request,
    )

    # 对象式调用（符合 ToolExecutorPort 协议）
    from .entrypoints.tool_executor import DefaultToolExecutor
    from .services.ports import ToolCallContext

    executor = DefaultToolExecutor()
    context = ToolCallContext(
        correlation_id="corr-abc123...",
        get_deps=get_deps_for_request,
    )
    result = await executor.call_tool("memory_store", {"payload_md": "..."}, context)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, List

from ..result_error_codes import ToolResultErrorCode

if TYPE_CHECKING:
    from ..di import GatewayDepsProtocol
    from ..services.ports import ToolCallContext, ToolCallResult

logger = logging.getLogger("gateway")


class ReliabilityReportErrorCode:
    """reliability_report 相关错误码"""

    IMPORT_FAILED = "RELIABILITY_REPORT_IMPORT_FAILED"
    EXECUTION_FAILED = "RELIABILITY_REPORT_EXECUTION_FAILED"
    DEPENDENCY_UNAVAILABLE = "RELIABILITY_REPORT_DEPENDENCY_UNAVAILABLE"


async def execute_tool(
    tool: str,
    args: Dict[str, Any],
    *,
    correlation_id: str,
    get_deps: Callable[[], "GatewayDepsProtocol"],
) -> Dict[str, Any]:
    """
    执行 MCP 工具调用

    此函数是工具执行的核心入口，实现所有工具的路由和调用。
    设计为 import-safe：模块顶层不触发 get_config()/get_container()。

    依赖注入（延迟获取）：
    - 通过 get_deps 回调在调用时获取依赖
    - 支持 routes.py 注册时不触发容器初始化
    - lifespan 负责预热，首次请求时依赖已初始化

    契约：所有工具的返回结果都必须包含 correlation_id 字段（单一来源原则）。

    Args:
        tool: 工具名称
        args: 工具参数字典
        correlation_id: 请求追踪 ID，用于审计日志关联
        get_deps: 获取依赖的回调函数（延迟调用）

    Returns:
        Dict[str, Any]: 工具执行结果，必须包含 correlation_id 字段

    Raises:
        ValueError: 未知工具名称
    """
    logger.debug(f"执行工具: tool={tool}, correlation_id={correlation_id}")

    # 延迟导入 handlers，保持 import-safe
    from ..handlers import (
        execute_evidence_upload,
        governance_update_impl,
        memory_query_impl,
        memory_store_impl,
    )

    # 延迟获取 deps（lifespan 中已预热，此处仅获取引用）
    deps = get_deps()

    result_dict: Dict[str, Any]

    if tool == "memory_store":
        store_result = await memory_store_impl(
            payload_md=args.get("payload_md", ""),
            target_space=args.get("target_space"),
            meta_json=args.get("meta_json"),
            kind=args.get("kind"),
            evidence_refs=args.get("evidence_refs"),
            evidence=args.get("evidence"),
            is_bulk=args.get("is_bulk", False),
            item_id=args.get("item_id"),
            actor_user_id=args.get("actor_user_id"),
            correlation_id=correlation_id,
            deps=deps,
        )
        result_dict = {"ok": store_result.ok, **store_result.model_dump()}

    elif tool == "memory_query":
        query_result = await memory_query_impl(
            query=args.get("query", ""),
            spaces=args.get("spaces"),
            filters=args.get("filters"),
            top_k=args.get("top_k", 10),
            correlation_id=correlation_id,
            deps=deps,
        )
        result_dict = {"ok": query_result.ok, **query_result.model_dump()}

    elif tool == "reliability_report":
        # 函数内导入：仅在 reliability_report 工具被调用时才导入依赖
        # 这支持依赖缺失时的优雅降级（返回 ok=false + error_code）
        try:
            from ..logbook_adapter import get_reliability_report

            report = get_reliability_report()
            result_dict = {"ok": True, **report}
        except ImportError as e:
            logger.warning(f"reliability_report 依赖导入失败: {e}")
            result_dict = {
                "ok": False,
                "message": f"reliability_report 依赖不可用: {e}",
                "error_code": ReliabilityReportErrorCode.IMPORT_FAILED,
                "outbox_stats": {},
                "audit_stats": {},
                "v2_evidence_stats": {},
                "content_intercept_stats": {},
                "generated_at": "",
            }
        except Exception as e:
            logger.exception(f"reliability_report 执行失败: {e}")
            result_dict = {
                "ok": False,
                "message": f"报告生成失败: {e}",
                "error_code": ReliabilityReportErrorCode.EXECUTION_FAILED,
                "outbox_stats": {},
                "audit_stats": {},
                "v2_evidence_stats": {},
                "content_intercept_stats": {},
                "generated_at": "",
            }

    elif tool == "governance_update":
        gov_result = await governance_update_impl(
            team_write_enabled=args.get("team_write_enabled"),
            policy_json=args.get("policy_json"),
            admin_key=args.get("admin_key"),
            actor_user_id=args.get("actor_user_id"),
            correlation_id=correlation_id,
            deps=deps,
        )
        result_dict = {"ok": gov_result.ok, **gov_result.model_dump()}

    elif tool == "evidence_upload":
        result_dict = await execute_evidence_upload(
            content=args.get("content"),
            content_type=args.get("content_type"),
            title=args.get("title"),
            actor_user_id=args.get("actor_user_id"),
            project_key=args.get("project_key"),
            item_id=args.get("item_id"),
            deps=deps,
        )

    else:
        raise ValueError(f"未知工具: {tool}")

    # 契约：确保所有工具结果都包含 correlation_id
    # 即使响应模型已包含 correlation_id，此处也确保使用入口层生成的值
    result_dict["correlation_id"] = correlation_id
    return result_dict


def list_tools() -> list:
    """
    列出所有可用工具

    返回所有 MCP 可用工具的定义列表，按 name 字母顺序排序。

    契约要求：
    - 每个工具必须包含 name, description, inputSchema 字段
    - 工具列表按 name 字母顺序排序（确保响应稳定性）

    Returns:
        工具定义列表（dict 格式）
    """
    from ..mcp_rpc import AVAILABLE_TOOLS

    # 按 name 字母顺序排序，确保响应稳定性
    sorted_tools = sorted(AVAILABLE_TOOLS, key=lambda t: t.name)
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.inputSchema,
        }
        for tool in sorted_tools
    ]


class DefaultToolExecutor:
    """
    默认工具执行器实现

    实现 ToolExecutorPort 协议，提供标准的工具执行逻辑。

    主要功能：
    1. 参数解析：校验必需参数，转换类型
    2. 工具路由：根据工具名分发到对应 handler
    3. 结果封装：统一封装为 ToolCallResult
    4. 错误处理：映射为统一错误模型

    错误码映射：
    - UNKNOWN_TOOL: 工具名不在可用列表中
    - MISSING_REQUIRED_PARAMETER: 缺少必需参数
    - INVALID_PARAM_TYPE: 参数类型错误
    - INTERNAL_ERROR: 未预期的内部错误
    """

    # 可用工具名列表（在 call_tool 中动态获取）
    _available_tools: List[str] = [
        "memory_store",
        "memory_query",
        "reliability_report",
        "governance_update",
        "evidence_upload",
    ]

    def list_tools(self) -> List[Dict[str, Any]]:
        """
        列出所有可用工具

        Returns:
            工具定义列表（dict 格式），按 name 字母顺序排序
        """
        return list_tools()

    async def call_tool(
        self,
        name: str,
        arguments: Dict[str, Any],
        context: "ToolCallContext",
    ) -> "ToolCallResult":
        """
        执行工具调用

        参数解析 → 路由到对应 handler → 结果封装

        Args:
            name: 工具名称
            arguments: 工具参数字典
            context: 调用上下文，包含 correlation_id 和 get_deps 回调

        Returns:
            ToolCallResult: 执行结果封装

        错误处理：
        - 未知工具 → UNKNOWN_TOOL
        - 缺少参数 → MISSING_REQUIRED_PARAMETER
        - 内部异常 → INTERNAL_ERROR
        """
        # 延迟导入，避免循环依赖
        from ..services.ports import ToolCallResult

        correlation_id = context.correlation_id
        get_deps = context.get_deps

        # 1. 校验工具名
        if name not in self._available_tools:
            logger.warning(f"未知工具: {name}, correlation_id={correlation_id}")
            return ToolCallResult(
                ok=False,
                error_code="UNKNOWN_TOOL",
                error_message=f"未知工具: {name}",
                retryable=False,
            )

        # 2. 工具特定参数校验
        validation_error = self._validate_tool_params(name, arguments)
        if validation_error:
            logger.warning(
                f"参数校验失败: {validation_error}, tool={name}, correlation_id={correlation_id}"
            )
            return validation_error

        # 3. 执行工具调用
        try:
            result = await execute_tool(
                tool=name,
                args=arguments,
                correlation_id=correlation_id,
                get_deps=get_deps,
            )

            # 4. 封装结果
            # execute_tool 返回的结果已包含 ok 字段
            ok = result.get("ok", True)
            if ok:
                return ToolCallResult(
                    ok=True,
                    result=result,
                )
            else:
                # 失败时，从 result 中提取错误信息并填充到 ToolCallResult
                return ToolCallResult(
                    ok=False,
                    result=result,
                    error_code=result.get("error_code"),
                    error_message=result.get("message") or result.get("suggestion"),
                    retryable=result.get("retryable", False),
                )

        except ValueError as e:
            # ValueError 通常是参数校验错误
            error_msg = str(e)
            error_code = "INVALID_PARAM_VALUE"
            if "未知工具" in error_msg or "unknown tool" in error_msg.lower():
                error_code = "UNKNOWN_TOOL"
            elif "缺少" in error_msg or "missing" in error_msg.lower():
                error_code = ToolResultErrorCode.MISSING_REQUIRED_PARAMETER

            logger.warning(f"工具执行参数错误: {e}, tool={name}, correlation_id={correlation_id}")
            return ToolCallResult(
                ok=False,
                error_code=error_code,
                error_message=str(e),
                retryable=False,
            )

        except Exception as e:
            # 未预期的内部错误
            logger.exception(f"工具执行内部错误: {e}, tool={name}, correlation_id={correlation_id}")
            return ToolCallResult(
                ok=False,
                error_code="INTERNAL_ERROR",
                error_message=f"内部错误: {str(e)}",
                retryable=True,  # 内部错误可能是暂时性的
            )

    def _validate_tool_params(
        self,
        name: str,
        arguments: Dict[str, Any],
    ) -> "ToolCallResult | None":
        """
        校验工具参数

        Args:
            name: 工具名称
            arguments: 工具参数

        Returns:
            ToolCallResult 如果校验失败，否则 None
        """
        from ..services.ports import ToolCallResult

        # 各工具的必需参数
        required_params: Dict[str, List[str]] = {
            "memory_store": ["payload_md"],
            "memory_query": ["query"],
            "evidence_upload": ["content", "content_type"],
            "governance_update": [],  # 无必需参数
            "reliability_report": [],  # 无必需参数
        }

        required = required_params.get(name, [])
        for param in required:
            if param not in arguments or arguments[param] is None:
                return ToolCallResult(
                    ok=False,
                    error_code=ToolResultErrorCode.MISSING_REQUIRED_PARAMETER,
                    error_message=f"缺少必需参数: {param}",
                    retryable=False,
                )

        return None


__all__ = [
    "execute_tool",
    "list_tools",
    "ReliabilityReportErrorCode",
    "DefaultToolExecutor",
]
