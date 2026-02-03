"""
Gateway 路由注册模块

提供 register_routes() 函数，负责统一注册所有路由：
- /health: 健康检查
- /mcp: MCP 统一入口（双协议兼容）
- /memory/*: REST 风格的记忆存取接口
- /reliability/report: 可靠性报告
- /governance/*: 治理设置管理
- /minio/audit: MinIO Audit Webhook

设计原则：
================
1. Import-Safe: 模块导入时不触发 get_config()/get_container()
2. Request-Time 延迟导入: 可选依赖（如 evidence_upload 相关）在请求时才导入
3. 单一职责: routes.py 只负责路由注册，不包含业务逻辑
4. 端口注入: 所有业务操作通过 deps.tool_executor 调用，不直接导入 handlers 实现

使用方式:
    from .routes import register_routes

    app = FastAPI(...)
    register_routes(app)
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

# 从 api_models 导入所有 API 模型和常量
from .api_models import (
    MCP_CORS_HEADERS,
    GovernanceSettingsUpdateRequest,
    MCPResponse,
    MCPToolCall,
    MemoryQueryRequest,
    MemoryStoreRequest,
    ReliabilityReportErrorCode,
    ReliabilityReportResponse,
    build_mcp_allow_headers,
)
from .error_redaction import (
    DEFAULT_PUBLIC_ERROR_MESSAGE,
    sanitize_error_details,
    sanitize_error_message,
    sanitize_header_list,
)

# 向后兼容导出：确保从 routes 导入模型的代码继续工作
__all__ = [
    "MCP_CORS_HEADERS",
    "GovernanceSettingsUpdateRequest",
    "MCPResponse",
    "MCPToolCall",
    "MemoryQueryRequest",
    "MemoryStoreRequest",
    "ReliabilityReportErrorCode",
    "ReliabilityReportResponse",
    "register_routes",
]

logger = logging.getLogger("gateway")


def _make_cors_headers_with_correlation_id(correlation_id: str) -> dict:
    """
    创建带有 correlation_id 的 CORS 头

    在 MCP_CORS_HEADERS 基础上添加 X-Correlation-ID header，
    确保每个请求都能通过 header 追踪 correlation_id。

    契约：X-Correlation-ID 必须与业务响应中的 correlation_id 一致（单次生成语义）
    """
    return {
        **MCP_CORS_HEADERS,
        "X-Correlation-ID": correlation_id,
    }


def register_routes(app: FastAPI) -> None:
    """
    统一注册所有 Gateway 路由

    此函数负责注册所有路由，包括：
    - MinIO Audit Webhook (/minio/audit)
    - 健康检查 (/health)
    - MCP 端点 (/mcp)
    - REST 记忆接口 (/memory/store, /memory/query)
    - 可靠性报告 (/reliability/report)
    - 治理设置 (/governance/settings/update)

    设计原则：
    ==========
    1. Import-Safe: 不在函数调用时触发 get_config()/get_container()
    2. Request-Time 延迟导入: 可选依赖（如 evidence_upload）在请求处理时才导入
    3. 所有 handler 通过 get_deps_for_request() 获取依赖（请求时延迟获取）
    4. correlation_id 通过 get_request_correlation_id_or_new() 获取（单一来源）

    Args:
        app: FastAPI 应用实例
    """
    # 延迟导入: 在函数调用时才导入，不在模块顶层导入
    # 这确保 routes.py 可以在无环境变量时被导入
    from .dependencies import get_deps_for_request, get_request_correlation_id_or_new

    # 导入 API 模型（Response 类型），用于 REST 端点的返回类型声明
    # 注意：只导入类型定义，不导入 handler 实现函数
    # 业务操作通过 deps.tool_executor 调用
    from .handlers import (
        GovernanceSettingsUpdateResponse,
        MemoryQueryResponse,
        MemoryStoreResponse,
    )
    from .mcp_rpc import (
        ErrorCategory,
        ErrorData,
        ErrorReason,
        JsonRpcErrorCode,
        dispatch_jsonrpc_request,
        handle_tools_call_with_executor,
        is_jsonrpc_request,
        is_valid_correlation_id,
        make_jsonrpc_error,
        mcp_router,
        register_tool_executor,
    )

    # 1. 注册 MinIO Audit Webhook 路由
    from .minio_audit_webhook import router as minio_audit_router

    app.include_router(minio_audit_router)

    # 导入 ToolCallContext 用于构造工具调用上下文
    from .services.ports import ToolCallContext

    # 2. 定义工具执行器（薄包装，通过端口依赖委托）
    # NOTE: 使用位置参数 correlation_id 以匹配 ToolExecutor 类型定义
    # 设计原则：通过 get_deps_for_request() 获取 deps.tool_executor，不直接导入实现模块

    async def _execute_tool(tool: str, args: Dict[str, Any], correlation_id: str) -> Dict[str, Any]:
        """
        执行工具调用的薄包装

        此函数实现 ToolRouterPort.execute 协议，签名为 (tool_name, tool_args, correlation_id)。
        通过 deps.tool_executor 获取 ToolExecutorPort 实例执行调用。

        设计原则：
        - 不直接导入 entrypoints.tool_executor 模块
        - 通过 get_deps_for_request() 获取依赖，支持测试时注入 mock

        Args:
            tool: 工具名称
            args: 工具参数
            correlation_id: 请求追踪 ID

        Returns:
            Dict[str, Any]: 工具执行结果，必须包含 correlation_id 字段
        """
        deps = get_deps_for_request()
        executor = deps.tool_executor
        context = ToolCallContext(
            correlation_id=correlation_id,
            get_deps=get_deps_for_request,
        )
        result = await executor.call_tool(tool, args, context)
        # 转换 ToolCallResult 为 dict 格式
        result_dict = result.to_dict()
        result_dict["correlation_id"] = correlation_id
        return result_dict

    # 3. 注册 tools/call handler（注入路径，优先于全局 executor）
    # ================================================================================
    #                       DI 注入路径（推荐）
    # ================================================================================
    #
    # 此 handler 使用 handle_tools_call_with_executor + _execute_tool 实现，
    # 通过 get_deps_for_request().tool_executor 获取依赖。
    #
    # 优势：
    # - 不依赖全局 _tool_executor 状态
    # - 测试时 reset 后无需额外注册即可工作
    # - 支持测试通过 GatewayContainer.create_for_testing() 注入 mock
    #
    # 此注册会覆盖 create_mcp_router() 中注册的默认 handle_tools_call。
    # ================================================================================

    async def _handle_tools_call_injected(params: Dict[str, Any]) -> Dict[str, Any]:
        """
        注入路径的 tools/call handler

        此 handler 使用 handle_tools_call_with_executor 处理请求，
        executor 通过 _execute_tool 从 DI 获取。

        与默认 handle_tools_call 的区别：
        - 不依赖全局 _tool_executor（通过 register_tool_executor 注册）
        - correlation_id 从 contextvars 获取（与默认行为一致）

        这确保 reset 后无需额外注册 tool_executor 也能正常工作。
        """
        from .mcp_rpc import get_current_correlation_id, get_tool_executor

        correlation_id = get_current_correlation_id()
        if correlation_id is None:
            raise RuntimeError(
                "correlation_id 未设置：_handle_tools_call_injected 必须在 dispatch 上下文中调用"
            )

        executor = get_tool_executor() or _execute_tool
        return await handle_tools_call_with_executor(params, executor, correlation_id)

    # 注册到 mcp_router，覆盖默认的 handle_tools_call
    mcp_router.register("tools/call", _handle_tools_call_injected)

    # 4. Legacy 模式：注册全局工具执行器（向后兼容）
    # ================================================================================
    #                       Legacy 模式（向后兼容）
    # ================================================================================
    #
    # 此注册保留用于向后兼容，允许：
    # - 直接使用 handle_tools_call（不经过 _handle_tools_call_injected）的场景
    # - 旧代码仍然可以通过 register_tool_executor 自定义执行器
    #
    # 新代码应优先使用注入路径（_handle_tools_call_injected）。
    # ================================================================================
    register_tool_executor(_execute_tool)

    # 3. 注册路由

    @app.get("/health")
    async def health_check():
        """健康检查"""
        return {
            "ok": True,
            "status": "ok",
            "service": "memory-gateway",
        }

    @app.options("/mcp")
    async def mcp_options(request: Request):
        """MCP 端点的 CORS 预检请求处理"""
        is_preflight = bool(request.headers.get("Access-Control-Request-Method"))
        requested_headers = request.headers.get("Access-Control-Request-Headers")
        response_headers = dict(MCP_CORS_HEADERS)
        allow_headers = MCP_CORS_HEADERS.get("Access-Control-Allow-Headers", "")
        if requested_headers:
            allow_headers = build_mcp_allow_headers(requested_headers)
            response_headers["Access-Control-Allow-Headers"] = allow_headers

        logger.info(
            "MCP CORS preflight",
            extra={
                "requested_headers": sanitize_header_list(requested_headers),
                "allow_headers": sanitize_header_list(allow_headers),
            },
        )
        return Response(status_code=204 if is_preflight else 200, headers=response_headers)

    @app.post("/mcp")
    async def mcp_endpoint(request: Request):
        """
        MCP 统一入口（双协议兼容）

        自动识别请求格式:
        - JSON-RPC 2.0: {"jsonrpc": "2.0", "method": "...", ...}
        - 旧格式 (MCPToolCall): {"tool": "...", "arguments": {...}}

        设计原则：
        - JSON-RPC 请求使用 dispatch_jsonrpc_request 统一处理
        - Legacy 请求保持 MCPResponse 结构，兼容旧客户端
        """
        # 通过 dependencies 模块获取 correlation_id（保持单一来源）
        # 优先使用中间件上下文，若不在上下文中则生成新的
        correlation_id = get_request_correlation_id_or_new()
        mcp_session_id = request.headers.get("Mcp-Session-Id") or request.headers.get(
            "mcp-session-id"
        )

        # 创建带 correlation_id 的响应 headers（契约：单次生成语义）
        try:
            response_headers = _make_cors_headers_with_correlation_id(correlation_id)
        except Exception as exc:
            logger.error(
                "MCP 响应头构建失败: correlation_id=%s, error=%s",
                correlation_id,
                sanitize_error_message(str(exc)),
            )
            response_headers = dict(MCP_CORS_HEADERS)
            requested_headers = request.headers.get("Access-Control-Request-Headers")
            if requested_headers:
                response_headers["Access-Control-Allow-Headers"] = build_mcp_allow_headers(
                    requested_headers
                )
            response_headers["X-Correlation-ID"] = correlation_id
            error_data = ErrorData(
                category=ErrorCategory.INTERNAL,
                reason=ErrorReason.UNHANDLED_EXCEPTION,
                retryable=False,
                correlation_id=correlation_id,
                details=sanitize_error_details(
                    {
                        "exception_type": type(exc).__name__,
                        "message": str(exc),
                    }
                ),
            )
            return JSONResponse(
                content=make_jsonrpc_error(
                    None,
                    JsonRpcErrorCode.INTERNAL_ERROR,
                    DEFAULT_PUBLIC_ERROR_MESSAGE,
                    data=error_data.to_dict(strict=True),
                ).model_dump(exclude_none=True),
                status_code=500,
                headers=response_headers,
            )

        # 解析原始请求 JSON
        try:
            body = await request.json()
        except Exception as e:
            logger.info(
                "MCP request",
                extra={
                    "is_jsonrpc": None,
                    "method": None,
                    "correlation_id": correlation_id,
                    "mcp_session_id_present": bool(mcp_session_id),
                },
            )
            error_data = ErrorData(
                category=ErrorCategory.PROTOCOL,
                reason=ErrorReason.PARSE_ERROR,
                retryable=False,
                correlation_id=correlation_id,
                details={"parse_error": str(e)[:200]},
            )
            return JSONResponse(
                content=make_jsonrpc_error(
                    None,
                    JsonRpcErrorCode.PARSE_ERROR,
                    f"JSON 解析失败: {str(e)}",
                    data=error_data.to_dict(strict=True),
                ).model_dump(exclude_none=True),
                status_code=400,
                headers=response_headers,
            )

        is_jsonrpc = isinstance(body, dict) and is_jsonrpc_request(body)
        method: str | None = body.get("method") if is_jsonrpc else None

        logger.info(
            "MCP request",
            extra={
                "is_jsonrpc": is_jsonrpc,
                "method": method,
                "correlation_id": correlation_id,
                "mcp_session_id_present": bool(mcp_session_id),
            },
        )

        if is_jsonrpc:
            # 使用统一入口函数处理 JSON-RPC 请求（方便 patch 测试）
            result = await dispatch_jsonrpc_request(
                body,
                correlation_id,
                strict_correlation_id=True,
            )
            response_headers = _make_cors_headers_with_correlation_id(result.correlation_id)

            if result.response.error:
                if result.response.error.data is None:
                    error_data = ErrorData(
                        category=ErrorCategory.PROTOCOL,
                        reason=ErrorReason.INVALID_REQUEST,
                        retryable=False,
                        correlation_id=result.correlation_id,
                        details=None,
                    )
                    result.response.error.data = error_data.to_dict(strict=True)
                elif isinstance(result.response.error.data, dict):
                    assert "correlation_id" in result.response.error.data, (
                        "契约违反: error.data 缺少 correlation_id。"
                        "JSON-RPC 入口链路必须由入口层生成并透传 correlation_id。"
                    )
                    assert is_valid_correlation_id(result.response.error.data["correlation_id"]), (
                        "契约违反: error.data.correlation_id 格式不合规。"
                    )
                    assert result.response.error.data["correlation_id"] == result.correlation_id, (
                        "契约违反: error.data.correlation_id 与入口 correlation_id 不一致。"
                    )

            return JSONResponse(
                content=result.to_dict(),
                status_code=result.http_status,
                headers=response_headers,
            )

        # 旧协议分支
        try:
            mcp_request = MCPToolCall(**body)
        except Exception as e:
            logger.warning(f"旧协议请求格式无效: correlation_id={correlation_id}, error={e}")
            return JSONResponse(
                content={
                    "ok": False,
                    "error": f"无效的请求格式: {str(e)}",
                    "correlation_id": correlation_id,
                },
                status_code=400,
                headers=response_headers,
            )

        tool = mcp_request.tool
        args = mcp_request.arguments

        logger.info(f"旧协议请求: tool={tool}, correlation_id={correlation_id}")

        try:
            tool_result = await _execute_tool(tool, args, correlation_id)
            tool_result["correlation_id"] = correlation_id
            if tool_result.get("ok", True):
                return JSONResponse(
                    content=MCPResponse(ok=True, result=tool_result).model_dump(),
                    headers=response_headers,
                )
            error_message = (
                tool_result.get("message")
                or tool_result.get("error")
                or tool_result.get("suggestion")
                or "工具执行失败"
            )
            return JSONResponse(
                content={
                    "ok": False,
                    "error": error_message,
                    "result": tool_result,
                    "correlation_id": correlation_id,
                },
                headers=response_headers,
            )
        except ValueError as e:
            logger.warning(
                f"旧协议工具调用参数错误: tool={tool}, correlation_id={correlation_id}, error={e}"
            )
            return JSONResponse(
                content={"ok": False, "error": str(e), "correlation_id": correlation_id},
                headers=response_headers,
            )
        except Exception as e:
            logger.exception(
                f"旧协议工具调用失败: tool={tool}, correlation_id={correlation_id}, error={e}"
            )
            return JSONResponse(
                content={"ok": False, "error": str(e), "correlation_id": correlation_id},
                headers=response_headers,
            )

    @app.post("/memory/store", response_model=MemoryStoreResponse)
    async def memory_store_endpoint(request: MemoryStoreRequest):
        """
        memory_store REST 端点

        通过 deps.tool_executor 调用业务逻辑，不直接导入 handler 实现。
        """
        # 通过 dependencies 模块获取 correlation_id 和 deps（保持单一来源）
        correlation_id = get_request_correlation_id_or_new()
        deps = get_deps_for_request()

        # 通过 tool_executor 端口调用，不直接导入 handler
        context = ToolCallContext(
            correlation_id=correlation_id,
            get_deps=get_deps_for_request,
        )
        result = await deps.tool_executor.call_tool(
            name="memory_store",
            arguments={
                "payload_md": request.payload_md,
                "target_space": request.target_space,
                "meta_json": request.meta_json,
                "kind": request.kind,
                "evidence_refs": request.evidence_refs,
                "evidence": request.evidence,
                "is_bulk": request.is_bulk,
                "item_id": request.item_id,
                "actor_user_id": request.actor_user_id,
            },
            context=context,
        )
        # 转换 ToolCallResult 为 Response 模型
        result_dict = result.to_dict()
        return MemoryStoreResponse(**result_dict)

    @app.post("/memory/query", response_model=MemoryQueryResponse)
    async def memory_query_endpoint(request: MemoryQueryRequest):
        """
        memory_query REST 端点

        通过 deps.tool_executor 调用业务逻辑，不直接导入 handler 实现。
        """
        # 通过 dependencies 模块获取 correlation_id 和 deps（保持单一来源）
        correlation_id = get_request_correlation_id_or_new()
        deps = get_deps_for_request()

        # 通过 tool_executor 端口调用，不直接导入 handler
        context = ToolCallContext(
            correlation_id=correlation_id,
            get_deps=get_deps_for_request,
        )
        result = await deps.tool_executor.call_tool(
            name="memory_query",
            arguments={
                "query": request.query,
                "spaces": request.spaces,
                "filters": request.filters,
                "top_k": request.top_k,
            },
            context=context,
        )
        # 转换 ToolCallResult 为 Response 模型
        result_dict = result.to_dict()
        return MemoryQueryResponse(**result_dict)

    @app.get("/reliability/report", response_model=ReliabilityReportResponse)
    async def reliability_report_endpoint():
        """
        获取可靠性统计报告

        降级语义：
        - 当 logbook_adapter 依赖缺失时，返回 ok=false + IMPORT_FAILED 错误码
        - 当报告生成执行失败时，返回 ok=false + EXECUTION_FAILED 错误码
        - 降级响应中各统计字段返回空字典，generated_at 返回空字符串
        """
        # 函数内导入：仅在路由触发时才导入依赖
        # 这支持依赖缺失时的优雅降级
        try:
            from .logbook_adapter import get_reliability_report
        except ImportError as e:
            logger.warning(f"reliability_report 依赖导入失败: {e}")
            return ReliabilityReportResponse(
                ok=False,
                message=f"reliability_report 依赖不可用: {e}",
                error_code=ReliabilityReportErrorCode.IMPORT_FAILED,
            )

        try:
            report = get_reliability_report()
            return ReliabilityReportResponse(
                ok=True,
                outbox_stats=report["outbox_stats"],
                audit_stats=report["audit_stats"],
                v2_evidence_stats=report["v2_evidence_stats"],
                content_intercept_stats=report["content_intercept_stats"],
                generated_at=report["generated_at"],
            )
        except Exception as e:
            logger.exception(f"获取可靠性报告失败: {e}")
            return ReliabilityReportResponse(
                ok=False,
                message=f"报告生成失败: {str(e)}",
                error_code=ReliabilityReportErrorCode.EXECUTION_FAILED,
            )

    @app.post("/governance/settings/update", response_model=GovernanceSettingsUpdateResponse)
    async def governance_settings_update_endpoint(request: GovernanceSettingsUpdateRequest):
        """
        更新治理设置（受保护端点）

        通过 deps.tool_executor 调用业务逻辑，不直接导入 handler 实现。
        """
        # 通过 dependencies 模块获取 correlation_id 和 deps（保持单一来源）
        correlation_id = get_request_correlation_id_or_new()
        deps = get_deps_for_request()

        # 通过 tool_executor 端口调用，不直接导入 handler
        context = ToolCallContext(
            correlation_id=correlation_id,
            get_deps=get_deps_for_request,
        )
        result = await deps.tool_executor.call_tool(
            name="governance_update",
            arguments={
                "team_write_enabled": request.team_write_enabled,
                "policy_json": request.policy_json,
                "admin_key": request.admin_key,
                "actor_user_id": request.actor_user_id,
            },
            context=context,
        )
        # 转换 ToolCallResult 为 Response 模型
        result_dict = result.to_dict()
        return GovernanceSettingsUpdateResponse(**result_dict)
