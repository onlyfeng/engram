"""
Gateway 应用工厂 (Application Factory)

提供 create_app() 函数，负责：
1. 创建 FastAPI 应用实例
2. 组装 GatewayContainer（仅用于依赖组装，不作为业务依赖来源）
3. 从 container 获取 GatewayDeps，作为统一依赖传递给所有 handler
4. 注册所有路由和中间件
5. 返回可运行的 FastAPI app

依赖注入设计（ADR：入口层统一 + deps 参数透传）:
============================================================
- GatewayContainer: 仅负责组装/持有依赖实例，不直接暴露给业务逻辑
- GatewayDeps: 通过 container.deps 获取，作为统一依赖接口传递给 handler
- 所有 handler 调用都显式传入 deps=deps，确保依赖来源单一可控

用法:
    # 直接创建应用
    app = create_app()

    # 使用自定义配置创建应用
    from .config import GatewayConfig
    config = GatewayConfig(project_key="test", ...)
    app = create_app(config=config)

    # 测试场景
    app = create_app(skip_db_check=True)

    # 使用 lifespan（推荐生产环境）
    from .main import lifespan
    app = create_app(lifespan=lifespan)
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import GatewayConfig
from .container import (
    GatewayContainer,
    get_container,
    set_container,
)

logger = logging.getLogger("gateway")


# ===================== 错误码常量 =====================


class ReliabilityReportErrorCode:
    """reliability_report 相关错误码"""

    IMPORT_FAILED = "RELIABILITY_REPORT_IMPORT_FAILED"
    EXECUTION_FAILED = "RELIABILITY_REPORT_EXECUTION_FAILED"
    DEPENDENCY_UNAVAILABLE = "RELIABILITY_REPORT_DEPENDENCY_UNAVAILABLE"


# ===================== 请求/响应模型 =====================


class MemoryStoreRequest(BaseModel):
    """memory_store 请求模型"""

    payload_md: str = Field(..., description="记忆内容（Markdown 格式）")
    target_space: Optional[str] = Field(None, description="目标空间，默认为 team:<project>")
    meta_json: Optional[Dict[str, Any]] = Field(None, description="元数据")
    # 策略相关字段
    kind: Optional[str] = Field(
        None, description="知识类型: FACT/PROCEDURE/PITFALL/DECISION/REVIEW_GUIDE"
    )
    evidence_refs: Optional[List[str]] = Field(None, description="证据链引用（v1 legacy 格式）")
    evidence: Optional[List[Dict[str, Any]]] = Field(None, description="结构化证据列表（v2 格式）")
    is_bulk: bool = Field(False, description="是否为批量提交")
    # 关联字段
    item_id: Optional[int] = Field(None, description="关联的 logbook.items.item_id")
    # 审计字段
    actor_user_id: Optional[str] = Field(None, description="执行操作的用户标识")


class MemoryQueryRequest(BaseModel):
    """memory_query 请求模型"""

    query: str = Field(..., description="查询文本")
    spaces: Optional[List[str]] = Field(None, description="搜索空间列表")
    filters: Optional[Dict[str, Any]] = Field(None, description="过滤条件")
    top_k: int = Field(10, description="返回结果数量")


class MCPToolCall(BaseModel):
    """MCP 工具调用请求（旧格式，保持兼容）"""

    tool: str = Field(..., description="工具名称")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="工具参数")


class MCPResponse(BaseModel):
    """MCP 响应（旧格式，保持兼容）"""

    ok: bool
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class ReliabilityReportResponse(BaseModel):
    """
    可靠性报告响应模型

    结构与 schemas/reliability_report_v1.schema.json 保持一致。

    降级语义（依赖缺失时的行为）：
    =================================
    当 logbook_adapter 或其依赖不可用时，reliability_report 端点会返回降级响应：
    - ok=false：表示报告生成失败
    - message：包含具体错误描述
    - error_code：标准化错误码，便于客户端处理
    - 各统计字段返回空字典 {}

    错误码说明：
    - RELIABILITY_REPORT_IMPORT_FAILED：logbook_adapter 导入失败（依赖缺失）
    - RELIABILITY_REPORT_EXECUTION_FAILED：报告生成执行失败（DB 连接等问题）
    - RELIABILITY_REPORT_DEPENDENCY_UNAVAILABLE：依赖服务不可用
    """

    ok: bool
    outbox_stats: Dict[str, Any] = Field(default_factory=dict, description="outbox_memory 表统计")
    audit_stats: Dict[str, Any] = Field(default_factory=dict, description="write_audit 表统计")
    v2_evidence_stats: Dict[str, Any] = Field(
        default_factory=dict, description="v2 evidence 覆盖率统计"
    )
    content_intercept_stats: Dict[str, Any] = Field(
        default_factory=dict, description="内容拦截统计"
    )
    generated_at: str = Field(default="", description="报告生成时间 (ISO 8601)")
    message: Optional[str] = None
    error_code: Optional[str] = Field(None, description="错误码（仅在 ok=false 时返回）")


class GovernanceSettingsUpdateRequest(BaseModel):
    """governance_update 请求模型"""

    team_write_enabled: Optional[bool] = Field(None, description="是否启用团队写入")
    policy_json: Optional[Dict[str, Any]] = Field(None, description="策略 JSON")
    # 鉴权字段
    admin_key: Optional[str] = Field(None, description="管理密钥（与 GOVERNANCE_ADMIN_KEY 匹配）")
    actor_user_id: Optional[str] = Field(
        None, description="执行操作的用户标识（可选，用于 allowlist 鉴权）"
    )


# CORS 配置常量
MCP_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, Mcp-Session-Id",
    "Access-Control-Expose-Headers": "Mcp-Session-Id, X-Correlation-ID",
    "Access-Control-Max-Age": "86400",
}


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


def create_app(
    config: Optional[GatewayConfig] = None,
    skip_db_check: bool = False,
    container: Optional[GatewayContainer] = None,
    lifespan: Optional[Callable] = None,
) -> FastAPI:
    """
    创建并配置 FastAPI 应用实例

    这是 Gateway 的应用工厂函数，负责：
    1. 创建 FastAPI 应用
    2. 注册所有路由（/mcp, /memory/*, /health, /reliability/report, /governance/*）
    3. 注册 MinIO Audit Webhook 路由

    依赖初始化策略（方案 A：延迟初始化）：
    ================================================
    - import-time: 仅创建 FastAPI 应用，不触发 get_config()/get_container()
    - lifespan: 负责配置验证、container 初始化、依赖预热
    - 请求时: handler 通过 get_container().deps 获取依赖

    这确保了：
    - 模块导入时不依赖环境变量（支持测试环境）
    - uvicorn 可以正常加载 app
    - lifespan 启动时才进行完整初始化

    Args:
        config: 可选的配置对象。如果提供，立即初始化 container。
        skip_db_check: 是否跳过 DB 检查（用于测试）。
        container: 可选的 GatewayContainer 实例。如果提供，立即设置为全局容器。
        lifespan: 可选的 lifespan 上下文管理器。如果提供，用于管理应用生命周期。
                  lifespan 可用于生产环境的增强初始化（如 DB 检查、依赖预热等）。

    Returns:
        配置好的 FastAPI 应用实例

    Raises:
        ConfigError: 配置无效（仅在显式传入 config 时）

    Note:
        - 如果传入 config 或 container，会立即初始化（用于测试场景）
        - 如果不传入，延迟到 lifespan/请求时初始化
        - handler 通过 get_container().deps 获取依赖
        - lifespan 中会预热 deps.logbook_adapter/deps.openmemory_client
    """
    # =================================================================
    # Import-Safe 策略（关键设计决策）
    # =================================================================
    # 1. 如果显式传入 container 或 config，立即初始化（用于测试场景）
    # 2. 否则延迟到 lifespan/请求时初始化（支持无环境变量的 import）
    #
    # 重要：不传参时绝不调用 get_container()/get_config()，确保：
    # - from engram.gateway.main import app 不依赖环境变量
    # - uvicorn engram.gateway.main:app 可正常加载
    # - 仅在 lifespan startup 或首次请求时才触发配置加载
    # =================================================================
    if container is not None:
        set_container(container)
    elif config is not None:
        set_container(GatewayContainer.create(config))
    # 注意：不传参时不调用 get_container()，保持 import-safe

    # 2. 创建 FastAPI 应用
    app = FastAPI(
        title="Memory Gateway",
        description="MCP Server for OpenMemory with governance and audit",
        version="0.1.0",
        lifespan=lifespan,
    )

    # 3. 注册 MinIO Audit Webhook 路由
    from .minio_audit_webhook import router as minio_audit_router

    app.include_router(minio_audit_router)

    # 4. 导入 handlers 和 MCP 相关模块
    from .handlers import (
        GovernanceSettingsUpdateResponse,
        MemoryQueryResponse,
        MemoryStoreResponse,
        execute_evidence_upload,
        governance_update_impl,
        memory_query_impl,
        memory_store_impl,
    )
    from .mcp_rpc import (
        ErrorCategory,
        ErrorData,
        ErrorReason,
        JsonRpcErrorCode,
        generate_correlation_id,
        is_jsonrpc_request,
        make_jsonrpc_error,
        mcp_router,
        parse_jsonrpc_request,
        register_tool_executor,
    )

    # 5. 定义工具执行器（延迟获取 deps，支持无环境变量 import）
    # NOTE: 使用位置参数 correlation_id 以匹配 ToolExecutor 类型定义
    async def _execute_tool(tool: str, args: Dict[str, Any], correlation_id: str) -> Dict[str, Any]:
        """
        执行工具调用的内部实现

        此函数实现 ToolExecutor 协议，签名为 (tool_name, tool_args, correlation_id)。

        依赖注入（延迟获取）：
        - 在每次调用时通过 get_container().deps 获取依赖
        - 这支持 import-time 不触发 get_config()（方案 A）
        - lifespan 负责预热，首次请求时依赖已初始化

        契约：所有工具的返回结果都必须包含 correlation_id 字段（单一来源原则）。

        Args:
            tool: 工具名称
            args: 工具参数
            correlation_id: 请求追踪 ID，用于审计日志关联，由 mcp_rpc.handle_tools_call 传入

        Returns:
            Dict[str, Any]: 工具执行结果，必须包含 correlation_id 字段
        """
        logger.debug(f"执行工具: tool={tool}, correlation_id={correlation_id}")

        # 延迟获取 deps（lifespan 中已预热，此处仅获取引用）
        deps = get_container().deps

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
                from .logbook_adapter import get_reliability_report

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

    # 注册工具执行器
    register_tool_executor(_execute_tool)

    # 6. 注册路由

    @app.get("/health")
    async def health_check():
        """健康检查"""
        return {
            "ok": True,
            "status": "ok",
            "service": "memory-gateway",
        }

    @app.options("/mcp")
    async def mcp_options():
        """MCP 端点的 CORS 预检请求处理"""
        return JSONResponse(
            content={"ok": True},
            headers=MCP_CORS_HEADERS,
        )

    @app.post("/mcp")
    async def mcp_endpoint(request: Request):
        """
        MCP 统一入口（双协议兼容）

        自动识别请求格式:
        - JSON-RPC 2.0: {"jsonrpc": "2.0", "method": "...", ...}
        - 旧格式 (MCPToolCall): {"tool": "...", "arguments": {...}}
        """
        correlation_id = generate_correlation_id()
        mcp_session_id = request.headers.get("Mcp-Session-Id") or request.headers.get(
            "mcp-session-id"
        )
        if mcp_session_id:
            logger.info(
                f"MCP 请求: Mcp-Session-Id={mcp_session_id}, correlation_id={correlation_id}"
            )

        # 创建带 correlation_id 的响应 headers（契约：单次生成语义）
        response_headers = _make_cors_headers_with_correlation_id(correlation_id)

        # 解析原始请求 JSON
        try:
            body = await request.json()
        except Exception as e:
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
                    data=error_data.to_dict(),
                ).model_dump(exclude_none=True),
                status_code=400,
                headers=response_headers,
            )

        # 自动识别请求格式
        if is_jsonrpc_request(body):
            # JSON-RPC 2.0 分支
            rpc_request, parse_error = parse_jsonrpc_request(body)
            if parse_error:
                if parse_error.error and parse_error.error.data is None:
                    error_data = ErrorData(
                        category=ErrorCategory.PROTOCOL,
                        reason=ErrorReason.INVALID_REQUEST,
                        retryable=False,
                        correlation_id=correlation_id,
                        details=None,
                    )
                    parse_error.error.data = error_data.to_dict()
                return JSONResponse(
                    content=parse_error.model_dump(exclude_none=True),
                    status_code=400,
                    headers=response_headers,
                )

            # rpc_request 已在 parse_error 分支中返回，此处必不为 None
            assert rpc_request is not None
            response = await mcp_router.dispatch(rpc_request, correlation_id=correlation_id)

            if response.error and response.error.data:
                if (
                    isinstance(response.error.data, dict)
                    and "correlation_id" not in response.error.data
                ):
                    response.error.data["correlation_id"] = correlation_id

            return JSONResponse(
                content=response.model_dump(exclude_none=True),
                headers=response_headers,
            )

        else:
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
                result = await _execute_tool(tool, args, correlation_id)
                result["correlation_id"] = correlation_id
                return JSONResponse(
                    content=MCPResponse(ok=result.get("ok", True), result=result).model_dump(),
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
        """直接调用 memory_store（REST 风格）"""
        # 在 REST 入口处生成 correlation_id，确保同一请求使用同一 ID
        correlation_id = generate_correlation_id()
        # 延迟获取 deps（支持无环境变量 import）
        deps = get_container().deps
        return await memory_store_impl(
            payload_md=request.payload_md,
            target_space=request.target_space,
            meta_json=request.meta_json,
            kind=request.kind,
            evidence_refs=request.evidence_refs,
            evidence=request.evidence,
            is_bulk=request.is_bulk,
            item_id=request.item_id,
            actor_user_id=request.actor_user_id,
            correlation_id=correlation_id,
            deps=deps,
        )

    @app.post("/memory/query", response_model=MemoryQueryResponse)
    async def memory_query_endpoint(request: MemoryQueryRequest):
        """直接调用 memory_query（REST 风格）"""
        # 在 REST 入口处生成 correlation_id，确保同一请求使用同一 ID
        correlation_id = generate_correlation_id()
        # 延迟获取 deps（支持无环境变量 import）
        deps = get_container().deps
        return await memory_query_impl(
            query=request.query,
            spaces=request.spaces,
            filters=request.filters,
            top_k=request.top_k,
            correlation_id=correlation_id,
            deps=deps,
        )

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
        """更新治理设置（受保护端点）"""
        # 延迟获取 deps（支持无环境变量 import）
        deps = get_container().deps
        return await governance_update_impl(
            team_write_enabled=request.team_write_enabled,
            policy_json=request.policy_json,
            admin_key=request.admin_key,
            actor_user_id=request.actor_user_id,
            deps=deps,
        )

    return app
