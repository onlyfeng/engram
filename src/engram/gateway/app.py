"""
Gateway 应用工厂 (Application Factory)

提供 create_app() 函数，负责：
1. 创建 FastAPI 应用实例
2. 组装 GatewayContainer（仅用于依赖组装，不作为业务依赖来源）
3. 安装应用级中间件（如鉴权）
4. 通过 routes.register_routes(app) 统一注册路由
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

from typing import Any, Callable, Dict, List, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .config import GatewayConfig
from .container import (
    GatewayContainer,
    set_container,
)
from .middleware import GatewayAuthMiddleware, install_middleware
from .routes import register_routes

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
    2. 安装应用级中间件（如鉴权）
    3. 调用 routes.register_routes() 统一注册路由

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

    # 2.1 安装 Gateway 鉴权中间件（仅在配置 token 时生效）
    app.add_middleware(GatewayAuthMiddleware)

    # 2.2 安装基础中间件（correlation_id、全局异常处理器）
    # 注意：FastAPI 中间件按添加顺序的逆序执行（LIFO），
    # 所以 install_middleware 应在 GatewayAuthMiddleware 之后调用
    install_middleware(app)

    # 3. 统一注册路由（/mcp, /memory/*, /health, /reliability/report, /governance/* 等）
    register_routes(app)

    return app
