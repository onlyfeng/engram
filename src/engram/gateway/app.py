"""
Gateway 应用工厂 (Application Factory)

提供 create_app() 函数，负责：
1. 创建 FastAPI 应用实例
2. 初始化 GatewayContainer 依赖容器
3. 注册所有路由和中间件
4. 返回可运行的 FastAPI app

这是 Gateway 的集中组装入口，将应用创建与启动逻辑分离，
便于测试和不同部署场景。

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
from .logbook_adapter import get_reliability_report

logger = logging.getLogger("gateway")


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

    结构与 schemas/reliability_report_v1.schema.json 保持一致
    """

    ok: bool
    outbox_stats: Dict[str, Any] = Field(..., description="outbox_memory 表统计")
    audit_stats: Dict[str, Any] = Field(..., description="write_audit 表统计")
    v2_evidence_stats: Dict[str, Any] = Field(..., description="v2 evidence 覆盖率统计")
    content_intercept_stats: Dict[str, Any] = Field(..., description="内容拦截统计")
    generated_at: str = Field(..., description="报告生成时间 (ISO 8601)")
    message: Optional[str] = None


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
    1. 初始化或使用传入的 GatewayContainer
    2. 创建 FastAPI 应用
    3. 注册所有路由（/mcp, /memory/*, /health, /reliability/report, /governance/*）
    4. 注册 MinIO Audit Webhook 路由

    Args:
        config: 可选的配置对象。如果不提供，从环境变量加载。
        skip_db_check: 是否跳过 DB 检查（用于测试）。
        container: 可选的 GatewayContainer 实例。如果提供，使用该实例；
                   否则创建新实例。
        lifespan: 可选的 lifespan 上下文管理器。如果提供，用于管理应用生命周期。
                  lifespan 可用于生产环境的增强初始化（如 DB 检查、日志设置等）。

    Returns:
        配置好的 FastAPI 应用实例

    Raises:
        ConfigError: 配置无效

    Note:
        container 初始化总是在此函数中完成（无论是否提供 lifespan）。
        lifespan 提供额外的生命周期管理（如 DB 健康检查），但不是必需的。
        这确保了测试环境和生产环境都能正常工作。
    """
    # 1. 初始化容器（总是执行，确保测试兼容）
    # lifespan 提供额外的增强功能，但 container 初始化不依赖它
    if container is not None:
        set_container(container)
    elif config is not None:
        set_container(GatewayContainer.create(config))
    else:
        # 使用默认配置创建容器
        container = get_container()

    # 获取容器引用
    container = get_container()

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

    # 5. 定义工具执行器
    async def _execute_tool(
        tool: str, args: Dict[str, Any], *, correlation_id: str
    ) -> Dict[str, Any]:
        """
        执行工具调用的内部实现

        此函数实现 ToolExecutor 协议，签名为 (tool_name, tool_args, *, correlation_id)。
        correlation_id 作为 keyword-only 参数，确保调用时必须显式传递。

        契约：所有工具的返回结果都必须包含 correlation_id 字段（单一来源原则）。

        Args:
            tool: 工具名称
            args: 工具参数
            correlation_id: 请求追踪 ID，用于审计日志关联，由 mcp_rpc.handle_tools_call 传入

        Returns:
            Dict[str, Any]: 工具执行结果，必须包含 correlation_id 字段
        """
        logger.debug(f"执行工具: tool={tool}, correlation_id={correlation_id}")

        result_dict: Dict[str, Any]

        if tool == "memory_store":
            result = await memory_store_impl(
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
            )
            result_dict = {"ok": result.ok, **result.model_dump()}

        elif tool == "memory_query":
            result = await memory_query_impl(
                query=args.get("query", ""),
                spaces=args.get("spaces"),
                filters=args.get("filters"),
                top_k=args.get("top_k", 10),
                correlation_id=correlation_id,
            )
            result_dict = {"ok": result.ok, **result.model_dump()}

        elif tool == "reliability_report":
            report = get_reliability_report()
            result_dict = {"ok": True, **report}

        elif tool == "governance_update":
            result = await governance_update_impl(
                team_write_enabled=args.get("team_write_enabled"),
                policy_json=args.get("policy_json"),
                admin_key=args.get("admin_key"),
                actor_user_id=args.get("actor_user_id"),
            )
            result_dict = {"ok": result.ok, **result.model_dump()}

        elif tool == "evidence_upload":
            result_dict = await execute_evidence_upload(
                content=args.get("content"),
                content_type=args.get("content_type"),
                title=args.get("title"),
                actor_user_id=args.get("actor_user_id"),
                project_key=args.get("project_key"),
                item_id=args.get("item_id"),
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
                    )
                    parse_error.error.data = error_data.to_dict()
                return JSONResponse(
                    content=parse_error.model_dump(exclude_none=True),
                    status_code=400,
                    headers=response_headers,
                )

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
                result = await _execute_tool(tool, args, correlation_id=correlation_id)
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
        )

    @app.post("/memory/query", response_model=MemoryQueryResponse)
    async def memory_query_endpoint(request: MemoryQueryRequest):
        """直接调用 memory_query（REST 风格）"""
        # 在 REST 入口处生成 correlation_id，确保同一请求使用同一 ID
        correlation_id = generate_correlation_id()
        return await memory_query_impl(
            query=request.query,
            spaces=request.spaces,
            filters=request.filters,
            top_k=request.top_k,
            correlation_id=correlation_id,
        )

    @app.get("/reliability/report", response_model=ReliabilityReportResponse)
    async def reliability_report_endpoint():
        """获取可靠性统计报告"""
        try:
            report = get_reliability_report()
            return ReliabilityReportResponse(
                ok=True,
                outbox_stats=report["outbox_stats"],
                audit_stats=report["audit_stats"],
                v2_evidence_stats=report["v2_evidence_stats"],
                content_intercept_stats=report["content_intercept_stats"],
                generated_at=report["generated_at"],
                message=None,
            )
        except Exception as e:
            logger.exception(f"获取可靠性报告失败: {e}")
            return ReliabilityReportResponse(
                ok=False,
                outbox_stats={},
                audit_stats={},
                v2_evidence_stats={},
                content_intercept_stats={},
                generated_at="",
                message=f"获取报告失败: {str(e)}",
            )

    @app.post("/governance/settings/update", response_model=GovernanceSettingsUpdateResponse)
    async def governance_settings_update_endpoint(request: GovernanceSettingsUpdateRequest):
        """更新治理设置（受保护端点）"""
        return await governance_update_impl(
            team_write_enabled=request.team_write_enabled,
            policy_json=request.policy_json,
            admin_key=request.admin_key,
            actor_user_id=request.actor_user_id,
        )

    return app
