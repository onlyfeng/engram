"""
Memory Gateway - MCP Server 入口

提供 /mcp 端点，暴露以下 MCP 工具：
- memory_store: 存储记忆（含策略校验、审计、失败降级）
- memory_query: 查询记忆
- memory_promote: 提升记忆空间（可选）
- memory_reinforce: 强化记忆（可选）

启动命令:
    uvicorn gateway.main:app --host 0.0.0.0 --port 8787
    或
    python -m gateway.main
"""

import hashlib
import json
import logging
import sys
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import ConfigError, get_config, validate_config, resolve_validate_refs
from .openmemory_client import (
    OpenMemoryClient,
    OpenMemoryConnectionError,
    OpenMemoryError,
    OpenMemoryAPIError,
    StoreResult,
    get_client,
)
from .policy import PolicyAction, PolicyEngine, create_engine_from_settings
from .logbook_db import LogbookDatabase, get_db, set_default_dsn
from . import logbook_adapter

# 导入统一错误码
_LOGBOOK_PKG_NAME = "engram_logbook"  # 用于标识实际使用的包名
try:
    from engram.logbook.errors import ErrorCode
except ImportError:
    import sys
    print(
        "\n"
        "=" * 60 + "\n"
        "[ERROR] 缺少依赖: engram_logbook\n"
        "=" * 60 + "\n"
        "\n"
        "Gateway 依赖 engram_logbook 模块（统一错误码等），请先安装：\n"
        "\n"
        "  # 在 monorepo 根目录执行\n"
        "  pip install -e \".[full]\"\n"
        "\n"
        "  # 或 Docker 环境（已自动安装）\n"
        "  docker compose -f docker-compose.unified.yml up gateway\n"
        "\n"
        "=" * 60 + "\n"
    )
    sys.exit(1)
from .logbook_adapter import (
    LogbookDBCheckError,
    LogbookDBCheckResult,
    LogbookDBErrorCode,
    UnknownActorPolicy,
    check_db_schema,
    check_user_exists,
    ensure_db_ready,
    ensure_user,
    get_reliability_report,
    is_db_migrate_available,
)
from .minio_audit_webhook import router as minio_audit_router
from .evidence_store import (
    upload_evidence,
    EvidenceUploadError,
    EvidenceSizeLimitExceededError,
    EvidenceContentTypeError,
    EvidenceWriteError,
    EvidenceItemRequiredError,
    EvidenceUploadResult,
    ALLOWED_CONTENT_TYPES,
)
from .audit_event import (
    AuditWriteError,
    build_gateway_audit_event,
    build_evidence_refs_json,
    normalize_evidence,
    validate_evidence_for_strict_mode,
)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("gateway")

# FastAPI 应用
app = FastAPI(
    title="Memory Gateway",
    description="MCP Server for OpenMemory with governance and audit",
    version="0.1.0",
)

# 注册 MinIO Audit Webhook 路由
app.include_router(minio_audit_router)


# ===================== 请求/响应模型 =====================


class MemoryStoreRequest(BaseModel):
    """memory_store 请求模型"""
    payload_md: str = Field(..., description="记忆内容（Markdown 格式）")
    target_space: Optional[str] = Field(None, description="目标空间，默认为 team:<project>")
    meta_json: Optional[Dict[str, Any]] = Field(None, description="元数据")
    # 策略相关字段
    kind: Optional[str] = Field(None, description="知识类型: FACT/PROCEDURE/PITFALL/DECISION/REVIEW_GUIDE")
    evidence_refs: Optional[List[str]] = Field(None, description="证据链引用（v1 legacy 格式）")
    evidence: Optional[List[Dict[str, Any]]] = Field(None, description="结构化证据列表（v2 格式）")
    is_bulk: bool = Field(False, description="是否为批量提交")
    # 关联字段
    item_id: Optional[int] = Field(None, description="关联的 logbook.items.item_id")
    # 审计字段
    actor_user_id: Optional[str] = Field(None, description="执行操作的用户标识")


class MemoryStoreResponse(BaseModel):
    """
    memory_store 响应模型
    
    统一响应契约（详见 docs/gateway/07_capability_boundary.md）：
    - ok: 操作是否成功（true: 成功或已入队，false: 失败）
    - action: 操作结果类型
        - allow: 直接写入成功
        - redirect: 空间重定向后写入成功
        - deferred: 写入已入队 outbox（OpenMemory 不可用）
        - reject: 策略拒绝
        - error: 系统错误
    - outbox_id: action=deferred 时必需，outbox 队列 ID
    - correlation_id: 所有响应必需，请求追踪 ID
    """
    ok: bool
    action: str  # allow / redirect / deferred / reject / error
    space_written: Optional[str] = None
    memory_id: Optional[str] = None
    outbox_id: Optional[int] = None  # action=deferred 时必需
    correlation_id: Optional[str] = None  # 所有响应必需，请求追踪 ID
    evidence_refs: Optional[List[str]] = None
    message: Optional[str] = None


class MemoryQueryRequest(BaseModel):
    """memory_query 请求模型"""
    query: str = Field(..., description="查询文本")
    spaces: Optional[List[str]] = Field(None, description="搜索空间列表")
    filters: Optional[Dict[str, Any]] = Field(None, description="过滤条件")
    top_k: int = Field(10, description="返回结果数量")


class MemoryQueryResponse(BaseModel):
    """memory_query 响应模型"""
    ok: bool
    results: List[Dict[str, Any]]
    total: int
    spaces_searched: List[str]
    message: Optional[str] = None
    degraded: bool = False  # 降级标记：True 表示结果来自 Logbook 回退查询


class MCPToolCall(BaseModel):
    """MCP 工具调用请求（旧格式，保持兼容）"""
    tool: str = Field(..., description="工具名称")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="工具参数")


class MCPResponse(BaseModel):
    """MCP 响应（旧格式，保持兼容）"""
    ok: bool
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


# ===================== JSON-RPC 2.0 协议层（从 mcp_rpc 模块导入）=====================

from .mcp_rpc import (
    JsonRpcErrorCode,
    JsonRpcRequest,
    JsonRpcResponse,
    JsonRpcRouter,
    is_jsonrpc_request,
    parse_jsonrpc_request,
    make_jsonrpc_error,
    make_jsonrpc_result,
    get_tool_definitions,
    format_tool_result,
    make_tool_error,
    register_tool_executor,
    mcp_router,
    # 错误分类与转换
    GatewayError,
    GatewayErrorCategory,
    to_jsonrpc_error,
    make_business_error_result,
    make_dependency_error_result,
    # 新增的稳定错误结构
    ErrorData,
    ErrorCategory,
    ErrorReason,
    make_business_error_response,
    make_dependency_error_response,
    generate_correlation_id,
)


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
    actor_user_id: Optional[str] = Field(None, description="执行操作的用户标识（可选，用于 allowlist 鉴权）")


class GovernanceSettingsUpdateResponse(BaseModel):
    """governance_update 响应模型"""
    ok: bool
    action: str  # allow / reject
    settings: Optional[Dict[str, Any]] = None
    message: Optional[str] = None


# ===================== 核心功能实现 =====================


def compute_payload_sha(payload_md: str) -> str:
    """计算 payload 的 SHA256 哈希"""
    return hashlib.sha256(payload_md.encode("utf-8")).hexdigest()


def _write_audit_or_raise(
    db,
    actor_user_id: Optional[str],
    target_space: str,
    action: str,
    reason: str,
    payload_sha: Optional[str],
    evidence_refs_json: Dict[str, Any],
    validate_refs: bool = False,
    correlation_id: str = "",
) -> int:
    """
    写入审计记录，失败时抛出 AuditWriteError
    
    实现 "审计不可丢" 语义：如果审计写入失败，阻断主操作继续执行。
    
    Args:
        db: LogbookDatabase 实例
        actor_user_id: 操作者用户 ID
        target_space: 目标空间
        action: 操作类型 (allow/redirect/reject)
        reason: 操作原因
        payload_sha: 内容 SHA256 哈希
        evidence_refs_json: 证据引用 JSON
        validate_refs: 是否验证 evidence_refs_json 结构
        correlation_id: 关联 ID（用于日志）
        
    Returns:
        audit_id: 创建的审计记录 ID
        
    Raises:
        AuditWriteError: 审计写入失败时抛出，阻断主操作
    """
    try:
        audit_id = db.insert_audit(
            actor_user_id=actor_user_id,
            target_space=target_space,
            action=action,
            reason=reason,
            payload_sha=payload_sha,
            evidence_refs_json=evidence_refs_json,
            validate_refs=validate_refs,
        )
        logger.debug(f"审计记录写入成功: audit_id={audit_id}, correlation_id={correlation_id}")
        return audit_id
    except Exception as e:
        logger.error(f"审计写入失败，阻断操作: {e}, correlation_id={correlation_id}")
        raise AuditWriteError(
            message=f"审计写入失败，操作已阻断",
            original_error=e,
            audit_data={
                "actor_user_id": actor_user_id,
                "target_space": target_space,
                "action": action,
                "reason": reason,
                "correlation_id": correlation_id,
            },
        )


def _validate_actor_user(
    actor_user_id: str,
    config,
    target_space: str,
    payload_sha: str,
    evidence_refs: Optional[List[str]],
    correlation_id: str,
) -> Optional[MemoryStoreResponse]:
    """
    校验 actor_user_id 是否存在，根据配置执行相应策略
    
    策略说明:
    - reject: 用户不存在时拒绝请求
    - degrade: 用户不存在时降级到 private:unknown 空间
    - auto_create: 用户不存在时自动创建
    
    Args:
        actor_user_id: 操作者用户标识
        config: GatewayConfig 配置对象
        target_space: 原始目标空间
        payload_sha: 内容哈希
        evidence_refs: 证据引用
        correlation_id: 关联 ID
        
    Returns:
        None: 用户存在或已自动创建，继续正常流程
        MemoryStoreResponse: 需要拒绝或降级时返回响应对象
    """
    db = get_db()
    
    # 检查用户是否存在
    user_exists = check_user_exists(actor_user_id)
    
    if user_exists:
        # 用户存在，正常继续
        return None
    
    # 用户不存在，根据策略处理
    policy = config.unknown_actor_policy
    
    if policy == UnknownActorPolicy.REJECT:
        # 策略: 拒绝请求
        logger.warning(f"actor_user_id 不存在且策略为 reject: {actor_user_id}")
        
        # 构建 gateway_event
        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id=correlation_id,
            actor_user_id=actor_user_id,
            requested_space=target_space,
            final_space=None,
            action="reject",
            reason=ErrorCode.ACTOR_UNKNOWN_REJECT,
            payload_sha=payload_sha,
            evidence_refs=evidence_refs,
            extra={"actor_policy": "reject"},
        )
        evidence_refs_json = build_evidence_refs_json(evidence=None, gateway_event=gateway_event)
        
        # 写入审计
        db.insert_audit(
            actor_user_id=actor_user_id,
            target_space=target_space,
            action="reject",
            reason=ErrorCode.ACTOR_UNKNOWN_REJECT,
            payload_sha=payload_sha,
            evidence_refs_json=evidence_refs_json,
        )
        
        return MemoryStoreResponse(
                ok=False,
                action="reject",
                space_written=None,
                memory_id=None,
                outbox_id=None,
                correlation_id=correlation_id,
                evidence_refs=evidence_refs,
                message=f"用户不存在: {actor_user_id}",
            )
    
    elif policy == UnknownActorPolicy.DEGRADE:
        # 策略: 降级到 private:unknown 空间
        degrade_space = f"{config.private_space_prefix}unknown"
        logger.info(f"actor_user_id 不存在，降级到 {degrade_space}: {actor_user_id}")
        
        # 构建 gateway_event
        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id=correlation_id,
            actor_user_id=actor_user_id,
            requested_space=target_space,
            final_space=degrade_space,
            action="redirect",
            reason=ErrorCode.ACTOR_UNKNOWN_DEGRADE,
            payload_sha=payload_sha,
            evidence_refs=evidence_refs,
            extra={
                "actor_policy": "degrade",
                "original_space": target_space,
                "degrade_space": degrade_space,
            },
        )
        evidence_refs_json = build_evidence_refs_json(evidence=None, gateway_event=gateway_event)
        
        # 写入审计
        db.insert_audit(
            actor_user_id=actor_user_id,
            target_space=target_space,
            action="redirect",
            reason=ErrorCode.ACTOR_UNKNOWN_DEGRADE,
            payload_sha=payload_sha,
            evidence_refs_json=evidence_refs_json,
        )
        
        # 返回一个特殊响应，指示需要降级
        return MemoryStoreResponse(
            ok=True,  # 标记为继续处理
            action="redirect",
            space_written=degrade_space,  # 使用降级空间
            memory_id=None,
            outbox_id=None,
            correlation_id=correlation_id,
            evidence_refs=evidence_refs,
            message=f"用户不存在，降级到 {degrade_space}",
        )
    
    elif policy == UnknownActorPolicy.AUTO_CREATE:
        # 策略: 自动创建用户
        logger.info(f"actor_user_id 不存在，自动创建: {actor_user_id}")
        
        try:
            ensure_user(user_id=actor_user_id, display_name=actor_user_id)
            
            # 构建 gateway_event
            gateway_event = build_gateway_audit_event(
                operation="memory_store",
                correlation_id=correlation_id,
                actor_user_id=actor_user_id,
                requested_space=target_space,
                final_space=target_space,
                action="allow",
                reason=ErrorCode.ACTOR_AUTOCREATED,
                payload_sha=payload_sha,
                evidence_refs=evidence_refs,
                extra={"actor_policy": "auto_create"},
            )
            evidence_refs_json = build_evidence_refs_json(evidence=None, gateway_event=gateway_event)
            
            # 写入审计
            db.insert_audit(
                actor_user_id=actor_user_id,
                target_space=target_space,
                action="allow",
                reason=ErrorCode.ACTOR_AUTOCREATED,
                payload_sha=payload_sha,
                evidence_refs_json=evidence_refs_json,
            )
            
            # 返回 None 表示继续正常流程
            return None
            
        except Exception as e:
            logger.error(f"自动创建用户失败: {actor_user_id}, error={e}")
            
            # 构建 gateway_event
            gateway_event = build_gateway_audit_event(
                operation="memory_store",
                correlation_id=correlation_id,
                actor_user_id=actor_user_id,
                requested_space=target_space,
                final_space=None,
                action="reject",
                reason=ErrorCode.ACTOR_AUTOCREATE_FAILED,
                payload_sha=payload_sha,
                evidence_refs=evidence_refs,
                extra={
                    "actor_policy": "auto_create",
                    "error": str(e)[:500],
                },
            )
            evidence_refs_json = build_evidence_refs_json(evidence=None, gateway_event=gateway_event)
            
            # 写入审计
            db.insert_audit(
                actor_user_id=actor_user_id,
                target_space=target_space,
                action="reject",
                reason=ErrorCode.ACTOR_AUTOCREATE_FAILED,
                payload_sha=payload_sha,
                evidence_refs_json=evidence_refs_json,
            )
            
            return MemoryStoreResponse(
                ok=False,
                action="error",
                space_written=None,
                memory_id=None,
                outbox_id=None,
                correlation_id=correlation_id,
                evidence_refs=evidence_refs,
                message=f"自动创建用户失败: {str(e)}",
            )
    
    # 未知策略（不应发生）
    logger.error(f"未知 actor 策略: {policy}")
    return None


async def memory_store_impl(
    payload_md: str,
    target_space: Optional[str] = None,
    meta_json: Optional[Dict[str, Any]] = None,
    kind: Optional[str] = None,
    evidence_refs: Optional[List[str]] = None,
    evidence: Optional[List[Dict[str, Any]]] = None,
    is_bulk: bool = False,
    item_id: Optional[int] = None,
    actor_user_id: Optional[str] = None,
) -> MemoryStoreResponse:
    """
    memory_store 核心实现

    流程:
    1. 读取治理 settings
    2. 规范化 evidence（v2 优先，v1 映射）
    3. 策略决策 (policy)
    4. 写入审计 (insert audit)
    5. 调用 OpenMemory
    6. 成功返回 memory_id / 失败写入 outbox
    
    Evidence 处理规则:
    - 若 evidence(v2) 非空：优先使用 evidence(v2) 参与审计与 validate_refs
    - 若仅 evidence_refs(v1) 非空：映射为 v2 external 格式（sha256 为空）
    - 在 strict 模式下，missing sha256 会触发 evidence_validation 校验
    """
    # 生成 correlation_id 用于追踪本次请求
    correlation_id = f"corr-{uuid.uuid4().hex[:16]}"
    
    # 获取配置
    config = get_config()
    
    # 默认目标空间
    if not target_space:
        target_space = config.default_team_space

    payload_sha = compute_payload_sha(payload_md)
    
    # 规范化 evidence：v2 优先，v1 映射为 external
    normalized_evidence, evidence_source = normalize_evidence(evidence, evidence_refs)
    logger.debug(f"Evidence 规范化: source={evidence_source}, count={len(normalized_evidence)}")

    try:
        # 0. Actor 校验：检查 actor_user_id 是否存在
        actor_degraded = False  # 标记是否发生降级
        original_actor_user_id = actor_user_id  # 保留原始 actor_user_id 用于审计
        
        if actor_user_id:
            actor_check_result = _validate_actor_user(
                actor_user_id=actor_user_id,
                config=config,
                target_space=target_space,
                payload_sha=payload_sha,
                evidence_refs=evidence_refs,
                correlation_id=correlation_id,
            )
            
            # 如果返回了响应对象，说明需要拒绝或降级
            if actor_check_result:
                if actor_check_result.action == "reject" or actor_check_result.action == "error":
                    return actor_check_result
                # 如果是降级（redirect），更新 target_space 并继续处理
                if actor_check_result.action == "redirect" and actor_check_result.space_written:
                    target_space = actor_check_result.space_written
                    actor_degraded = True
                    # 重新计算 payload_sha 以确保一致性（使用新空间）
                    logger.info(f"Actor 降级: {actor_user_id} -> space={target_space}")

        # 1. Dedupe Check：检查是否已成功写入过
        dedup_record = logbook_adapter.check_dedup(
            target_space=target_space,
            payload_sha=payload_sha,
        )
        if dedup_record:
            # 已存在成功写入的记录，直接返回并写入审计 reason=dedup_hit
            logger.info(f"Dedupe hit: target_space={target_space}, payload_sha={payload_sha[:16]}...")
            
            # 从 last_error 中提取 memory_id（格式：memory_id=xxx）
            memory_id = None
            last_error = dedup_record.get("last_error")
            if last_error and last_error.startswith("memory_id="):
                memory_id = last_error.split("=", 1)[1]
            
            # 构建 gateway_event（使用 normalized_evidence）
            original_outbox_id = dedup_record.get("outbox_id")
            gateway_event = build_gateway_audit_event(
                operation="memory_store",
                correlation_id=correlation_id,
                actor_user_id=actor_user_id,
                requested_space=target_space,
                final_space=target_space,
                action="allow",
                reason=ErrorCode.DEDUP_HIT,
                payload_sha=payload_sha,
                payload_len=len(payload_md),
                evidence=normalized_evidence,
                memory_id=memory_id,
                extra={
                    "original_outbox_id": original_outbox_id,
                    "evidence_source": evidence_source,
                    "correlation_id": correlation_id,  # 添加 correlation_id 到 extra
                },
            )
            evidence_refs_json = build_evidence_refs_json(evidence=normalized_evidence, gateway_event=gateway_event)
            # 提升 original_outbox_id 到顶层（符合测试契约）
            if original_outbox_id is not None:
                evidence_refs_json["original_outbox_id"] = original_outbox_id
            
            # 写入审计（dedup_hit）
            db = get_db()
            db.insert_audit(
                actor_user_id=actor_user_id,
                target_space=target_space,
                action="allow",
                reason=ErrorCode.DEDUP_HIT,
                payload_sha=payload_sha,
                evidence_refs_json=evidence_refs_json,
            )
            
            return MemoryStoreResponse(
                ok=True,
                action="allow",
                space_written=target_space,
                memory_id=memory_id,
                outbox_id=None,
                correlation_id=correlation_id,
                evidence_refs=evidence_refs,
                message="dedup_hit: 已存在相同内容的成功写入记录",
            )

        # 1. 读取治理设置
        db = get_db()
        settings = db.get_or_create_settings(config.project_key)
        logger.info(f"获取治理设置: project={config.project_key}, team_write_enabled={settings.get('team_write_enabled')}")
        
        # 1.5. 选择 evidence 校验模式并解析 validate_refs 有效值
        # 从 policy_json 中读取模式，默认为 "compat"
        policy_json = settings.get("policy_json") or {}
        evidence_mode = policy_json.get("evidence_mode", "compat")
        
        # 调用 resolve_validate_refs 获取最终决策
        validate_refs_decision = resolve_validate_refs(
            mode=evidence_mode,
            config=config,
            caller_override=None,  # 暂不支持调用方 override
        )
        validate_refs_effective = validate_refs_decision.effective
        validate_refs_reason = validate_refs_decision.reason
        logger.debug(f"Evidence 校验决策: mode={evidence_mode}, effective={validate_refs_effective}, reason={validate_refs_reason}")
        
        # 1.6. strict 模式下执行 evidence 校验
        evidence_validation = None
        if evidence_mode == "strict" and normalized_evidence:
            evidence_validation = validate_evidence_for_strict_mode(normalized_evidence)
            logger.debug(f"Evidence 校验结果: is_valid={evidence_validation.is_valid}, "
                        f"errors={evidence_validation.error_codes}, warnings={evidence_validation.compat_warnings}")
            
            # 记录 compat_warnings 用于可观测性（即使 is_valid=True）
            if evidence_validation.compat_warnings:
                logger.info(f"Evidence compat warnings (strict mode): {evidence_validation.compat_warnings}")

        # 2. 策略决策
        engine = create_engine_from_settings(settings)
        decision = engine.decide(
            target_space=target_space,
            actor_user_id=actor_user_id,
            payload_md=payload_md,
            kind=kind,
            evidence_refs=evidence_refs,
            is_bulk=is_bulk,
        )
        logger.info(f"策略决策: action={decision.action.value}, reason={decision.reason}")

        # 如果策略拒绝
        if decision.action == PolicyAction.REJECT:
            # 构建 gateway_event（使用 normalized_evidence）
            gateway_event = build_gateway_audit_event(
                operation="memory_store",
                correlation_id=correlation_id,
                actor_user_id=actor_user_id,
                requested_space=target_space,
                final_space=None,
                action="reject",
                reason=ErrorCode.policy_reason(decision.reason),
                payload_sha=payload_sha,
                payload_len=len(payload_md),
                evidence=normalized_evidence,
                extra={
                    "policy_reason": decision.reason,
                    "evidence_source": evidence_source,
                },
                validate_refs_effective=validate_refs_effective,
                validate_refs_reason=validate_refs_reason,
                evidence_validation=evidence_validation.to_dict() if evidence_validation else None,
            )
            evidence_refs_json = build_evidence_refs_json(evidence=normalized_evidence, gateway_event=gateway_event)
            
            # 写入审计（使用 _write_audit_or_raise 确保审计不可丢）
            _write_audit_or_raise(
                db=db,
                actor_user_id=actor_user_id,
                target_space=target_space,
                action="reject",
                reason=ErrorCode.policy_reason(decision.reason),
                payload_sha=payload_sha,
                evidence_refs_json=evidence_refs_json,
                validate_refs=validate_refs_effective,
                correlation_id=correlation_id,
            )
            return MemoryStoreResponse(
                ok=False,
                action="reject",
                space_written=None,
                memory_id=None,
                outbox_id=None,
                correlation_id=correlation_id,
                evidence_refs=evidence_refs,
                message=f"策略拒绝: {decision.reason}",
            )

        # 确定最终写入空间
        final_space = decision.final_space
        action = decision.action.value

        # ================================================================
        # 3. 调用 OpenMemory
        # ================================================================
        try:
            client = get_client()
            result = client.store(
                content=payload_md,
                space=final_space,
                metadata=meta_json,
            )
            
            # 检查存储是否成功
            if not result.success:
                raise OpenMemoryError(
                    message=result.error or "存储失败",
                    status_code=None,
                    response=None,
                )
            
            memory_id = result.memory_id
            logger.info(f"OpenMemory 写入成功: memory_id={memory_id}, space={final_space}")

            # 3.1 写入成功审计（包含 memory_id）
            post_audit_gateway_event = build_gateway_audit_event(
                operation="memory_store",
                correlation_id=correlation_id,
                actor_user_id=actor_user_id,
                requested_space=target_space,
                final_space=final_space,
                action=action,
                reason=ErrorCode.policy_reason(decision.reason),
                payload_sha=payload_sha,
                payload_len=len(payload_md),
                evidence=normalized_evidence,
                memory_id=memory_id,
                extra={"evidence_source": evidence_source},
                validate_refs_effective=validate_refs_effective,
                validate_refs_reason=validate_refs_reason,
                evidence_validation=evidence_validation.to_dict() if evidence_validation else None,
            )
            post_audit_evidence_refs_json = build_evidence_refs_json(
                evidence=normalized_evidence, 
                gateway_event=post_audit_gateway_event
            )

            # 写入成功审计（若失败则返回 error）
            _write_audit_or_raise(
                db=db,
                actor_user_id=actor_user_id,
                target_space=final_space,
                action=action,
                reason=ErrorCode.policy_reason(decision.reason),
                payload_sha=payload_sha,
                evidence_refs_json=post_audit_evidence_refs_json,
                validate_refs=validate_refs_effective,
                correlation_id=correlation_id,
            )

            return MemoryStoreResponse(
                ok=True,
                action=action,
                space_written=final_space,
                memory_id=memory_id,
                outbox_id=None,
                correlation_id=correlation_id,
                evidence_refs=evidence_refs,
                message=None,
            )

        except (OpenMemoryConnectionError, OpenMemoryError) as e:
            # ================================================================
            # 4. OpenMemory 失败：先写 outbox，再写审计（确保 outbox_id 一致性）
            # ================================================================
            error_msg = str(e.message if hasattr(e, 'message') else e)
            logger.error(f"OpenMemory 写入失败: {error_msg}")

            # 4.1 先写入 outbox（获取 outbox_id 用于审计）
            outbox_id = db.enqueue_outbox(
                payload_md=payload_md,
                target_space=final_space,
                item_id=item_id,
                last_error=error_msg,
            )
            logger.info(f"已入队 outbox: outbox_id={outbox_id}")

            # 提取错误码用于规范化 reason（使用统一错误码）
            if isinstance(e, OpenMemoryConnectionError):
                error_reason = ErrorCode.OPENMEMORY_WRITE_FAILED_CONNECTION
                error_code = "connection_error"
            elif isinstance(e, OpenMemoryAPIError):
                status_code = getattr(e, 'status_code', None)
                error_reason = ErrorCode.openmemory_api_error(status_code)
                error_code = f"api_error_{status_code}" if status_code else "api_error"
            elif isinstance(e, OpenMemoryError):
                error_reason = ErrorCode.OPENMEMORY_WRITE_FAILED_GENERIC
                error_code = "openmemory_error"
            else:
                error_reason = ErrorCode.OPENMEMORY_WRITE_FAILED_UNKNOWN
                error_code = "unknown"

            # 4.2 构建失败审计（包含 outbox_id）
            failure_gateway_event = build_gateway_audit_event(
                operation="memory_store",
                correlation_id=correlation_id,
                actor_user_id=actor_user_id,
                requested_space=target_space,
                final_space=final_space,
                action="deferred",
                reason=error_reason,
                payload_sha=payload_sha,
                payload_len=len(payload_md),
                evidence=normalized_evidence,
                outbox_id=outbox_id,
                extra={
                    "last_error": error_msg[:500],
                    "error_code": error_code,
                    "evidence_source": evidence_source,
                },
                validate_refs_effective=validate_refs_effective,
                validate_refs_reason=validate_refs_reason,
                evidence_validation=evidence_validation.to_dict() if evidence_validation else None,
            )
            failure_evidence_refs_json = build_evidence_refs_json(
                evidence=normalized_evidence, 
                gateway_event=failure_gateway_event
            )

            # 4.3 写入失败审计（包含 outbox_id，确保 evidence_refs_json->>'outbox_id' 可查询）
            # 注意：预审计已存在，此处记录失败结果和补偿信息
            try:
                db.insert_audit(
                    actor_user_id=actor_user_id,
                    target_space=final_space,
                    action="deferred",
                    reason=error_reason,
                    payload_sha=payload_sha,
                    evidence_refs_json=failure_evidence_refs_json,
                    validate_refs=validate_refs_effective,
                )
            except Exception as failure_audit_err:
                # 失败审计写入失败：记录错误但不阻断返回（outbox 已保证可恢复）
                logger.error(
                    f"失败审计写入失败: {failure_audit_err}, "
                    f"correlation_id={correlation_id}, outbox_id={outbox_id}"
                )

            return MemoryStoreResponse(
                ok=False,
                action="deferred",  # 使用 deferred 表示已入队 outbox
                space_written=None,
                memory_id=None,
                outbox_id=outbox_id,  # 显式返回 outbox_id
                correlation_id=correlation_id,  # 显式返回 correlation_id
                evidence_refs=evidence_refs,
                message=f"OpenMemory 不可用，已入队补偿队列 (outbox_id={outbox_id}): {error_msg}",
            )

    except AuditWriteError as e:
        # 审计写入失败：操作已阻断，返回明确的错误信息
        logger.error(f"审计写入失败，操作已阻断: {e}, correlation_id={correlation_id}")
        return MemoryStoreResponse(
            ok=False,
            action="error",
            space_written=None,
            memory_id=None,
            outbox_id=None,
            correlation_id=correlation_id,
            evidence_refs=evidence_refs,
            message=f"审计写入失败，操作已阻断: {e.message}",
        )
    except Exception as e:
        logger.exception(f"memory_store 未预期错误: {e}")
        return MemoryStoreResponse(
            ok=False,
            action="error",
            space_written=None,
            memory_id=None,
            outbox_id=None,
            correlation_id=correlation_id,
            evidence_refs=evidence_refs,
            message=f"内部错误: {str(e)}",
        )


async def memory_query_impl(
    query: str,
    spaces: Optional[List[str]] = None,
    filters: Optional[Dict[str, Any]] = None,
    top_k: int = 10,
) -> MemoryQueryResponse:
    """
    memory_query 核心实现
    
    当 OpenMemory 查询失败时，会降级到 Logbook 的 knowledge_candidates 表进行回退查询。
    """
    # 获取配置
    config = get_config()
    
    # 默认搜索空间
    if not spaces:
        spaces = [config.default_team_space]

    try:
        client = get_client()
        # 使用 openmemory_client 的 search 方法
        # filters 中可以包含 spaces 信息
        combined_filters = filters.copy() if filters else {}
        combined_filters["spaces"] = spaces
        
        result = client.search(
            query=query,
            limit=top_k,
            filters=combined_filters,
        )

        if not result.success:
            return MemoryQueryResponse(
                ok=False,
                results=[],
                total=0,
                spaces_searched=spaces,
                message=f"查询失败: {result.error}",
            )

        return MemoryQueryResponse(
            ok=True,
            results=result.results,
            total=len(result.results),
            spaces_searched=spaces,
            message=None,
        )

    except OpenMemoryError as e:
        # OpenMemory 查询失败，降级到 Logbook 回退查询
        logger.warning(f"OpenMemory 查询失败，降级到 Logbook 回退查询: {e.message}")
        
        try:
            # 从 filters 中提取 evidence_filter 和 space_filter（如果有）
            evidence_filter = None
            space_filter = None
            if filters:
                evidence_filter = filters.get("evidence")
                # 如果 spaces 有值，取第一个作为 space_filter
                if spaces:
                    space_filter = spaces[0]
            
            # 调用 Logbook 回退查询
            candidates = logbook_adapter.query_knowledge_candidates(
                keyword=query,
                top_k=top_k,
                evidence_filter=evidence_filter,
                space_filter=space_filter,
            )
            
            # 将 knowledge_candidates 结果转换为统一的结果格式
            results = []
            for candidate in candidates:
                results.append({
                    "id": f"kc_{candidate['candidate_id']}",
                    "content": candidate["content_md"],
                    "title": candidate["title"],
                    "kind": candidate["kind"],
                    "confidence": candidate["confidence"],
                    "evidence_refs": candidate.get("evidence_refs_json"),
                    "created_at": str(candidate["created_at"]) if candidate.get("created_at") else None,
                    "source": "logbook_fallback",
                })
            
            return MemoryQueryResponse(
                ok=True,
                results=results,
                total=len(results),
                spaces_searched=spaces or [],
                message=f"降级查询（OpenMemory 不可用）: {e.message}",
                degraded=True,
            )
            
        except Exception as fallback_error:
            logger.exception(f"Logbook 回退查询也失败: {fallback_error}")
            return MemoryQueryResponse(
                ok=False,
                results=[],
                total=0,
                spaces_searched=spaces or [],
                message=f"查询失败（OpenMemory: {e.message}, 回退: {str(fallback_error)}）",
                degraded=True,
            )
            
    except Exception as e:
        logger.exception(f"memory_query 未预期错误: {e}")
        return MemoryQueryResponse(
            ok=False,
            results=[],
            total=0,
            spaces_searched=spaces or [],
            message=f"内部错误: {str(e)}",
        )


async def governance_update_impl(
    team_write_enabled: Optional[bool] = None,
    policy_json: Optional[Dict[str, Any]] = None,
    admin_key: Optional[str] = None,
    actor_user_id: Optional[str] = None,
) -> GovernanceSettingsUpdateResponse:
    """
    governance_update 核心实现
    
    鉴权方式（满足其一即可）：
    1. admin_key 与环境变量 GOVERNANCE_ADMIN_KEY 匹配
    2. actor_user_id 在 policy_json.allowlist_users 中
    
    流程:
    1. 鉴权校验
    2. 读取当前设置
    3. 更新设置（合并变更）
    4. 写入审计日志
    5. 返回更新后的设置
    """
    config = get_config()
    db = get_db()
    
    # 读取当前设置
    current_settings = db.get_or_create_settings(config.project_key)
    current_policy = current_settings.get("policy_json") or {}
    allowlist_users = current_policy.get("allowlist_users", [])
    
    # 鉴权校验
    auth_passed = False
    auth_method = None
    reject_reason = None
    
    # 方式 1: admin_key 匹配
    if admin_key and config.governance_admin_key:
        if admin_key == config.governance_admin_key:
            auth_passed = True
            auth_method = "admin_key"
    
    # 方式 2: actor_user_id 在 allowlist_users 中
    if not auth_passed and actor_user_id:
        if actor_user_id in allowlist_users:
            auth_passed = True
            auth_method = "allowlist_user"
    
    # 如果鉴权失败
    if not auth_passed:
        if not admin_key and not actor_user_id:
            reject_reason = ErrorCode.GOVERNANCE_UPDATE_MISSING_CREDENTIALS
        elif admin_key and not config.governance_admin_key:
            reject_reason = ErrorCode.GOVERNANCE_UPDATE_ADMIN_KEY_NOT_CONFIGURED
        elif admin_key:
            reject_reason = ErrorCode.GOVERNANCE_UPDATE_INVALID_ADMIN_KEY
        else:
            reject_reason = ErrorCode.GOVERNANCE_UPDATE_USER_NOT_IN_ALLOWLIST
        
        # 写入审计日志（拒绝）
        db.insert_audit(
            actor_user_id=actor_user_id,
            target_space=f"governance:{config.project_key}",
            action="reject",
            reason=reject_reason,
            payload_sha=None,
            evidence_refs_json={
                "source": "gateway",
                "operation": "governance_update",
                "auth_method_attempted": "admin_key" if admin_key else "allowlist",
            },
        )
        
        logger.warning(f"governance_update 鉴权失败: {reject_reason}, actor={actor_user_id}")
        
        return GovernanceSettingsUpdateResponse(
            ok=False,
            action="reject",
            settings=None,
            message=f"鉴权失败: {reject_reason}",
        )
    
    # 鉴权通过，执行更新
    try:
        # 合并策略变更
        new_team_write_enabled = team_write_enabled if team_write_enabled is not None else current_settings.get("team_write_enabled", False)
        
        # 合并 policy_json（如果提供）
        if policy_json is not None:
            new_policy = {**current_policy, **policy_json}
        else:
            new_policy = current_policy
        
        # 使用模块级 logbook_adapter 获取 adapter（便于测试 patch）
        adapter = logbook_adapter.get_adapter()
        
        # 执行更新
        success = adapter.upsert_settings(
            project_key=config.project_key,
            team_write_enabled=new_team_write_enabled,
            policy_json=new_policy,
            updated_by=actor_user_id,
        )
        
        if not success:
            raise RuntimeError("upsert_settings 返回失败")
        
        # 读取更新后的设置
        updated_settings = db.get_settings(config.project_key)
        
        # 根据认证方式选择 reason
        if auth_method == "admin_key":
            auth_reason = ErrorCode.GOVERNANCE_UPDATE_ADMIN_KEY
        else:
            auth_reason = ErrorCode.GOVERNANCE_UPDATE_ALLOWLIST_USER
        
        # 写入审计日志（允许）
        db.insert_audit(
            actor_user_id=actor_user_id,
            target_space=f"governance:{config.project_key}",
            action="allow",
            reason=auth_reason,
            payload_sha=None,
            evidence_refs_json={
                "source": "gateway",
                "operation": "governance_update",
                "auth_method": auth_method,
                "changes": {
                    "team_write_enabled": team_write_enabled,
                    "policy_json_updated": policy_json is not None,
                },
            },
        )
        
        logger.info(f"governance_update 成功: project={config.project_key}, actor={actor_user_id}, auth_method={auth_method}")
        
        return GovernanceSettingsUpdateResponse(
            ok=True,
            action="allow",
            settings=updated_settings,
            message=None,
        )
        
    except Exception as e:
        logger.exception(f"governance_update 执行失败: {e}")
        
        # 写入审计日志（错误）
        db.insert_audit(
            actor_user_id=actor_user_id,
            target_space=f"governance:{config.project_key}",
            action="reject",
            reason=ErrorCode.GOVERNANCE_UPDATE_INTERNAL_ERROR,
            payload_sha=None,
            evidence_refs_json={
                "source": "gateway",
                "operation": "governance_update",
                "error": str(e)[:500],
            },
        )
        
        return GovernanceSettingsUpdateResponse(
            ok=False,
            action="reject",
            settings=None,
            message=f"更新失败: {str(e)}",
        )


# ===================== HTTP 端点 =====================


@app.get("/health")
async def health_check():
    """
    健康检查
    
    返回字段:
    - ok: 服务是否健康
    - status: 状态字符串 (保持向后兼容)
    - service: 服务名称 (保持向后兼容)
    """
    return {
        "ok": True,
        "status": "ok",
        "service": "memory-gateway",
    }


async def _execute_tool(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """
    执行工具调用的内部实现
    
    Args:
        tool: 工具名称
        args: 工具参数
        
    Returns:
        工具执行结果 dict
        
    Raises:
        ValueError: 未知工具
        Exception: 执行错误
    """
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
        )
        return {"ok": result.ok, **result.model_dump()}

    elif tool == "memory_query":
        result = await memory_query_impl(
            query=args.get("query", ""),
            spaces=args.get("spaces"),
            filters=args.get("filters"),
            top_k=args.get("top_k", 10),
        )
        return {"ok": result.ok, **result.model_dump()}

    elif tool == "reliability_report":
        report = get_reliability_report()
        return {"ok": True, **report}

    elif tool == "governance_update":
        result = await governance_update_impl(
            team_write_enabled=args.get("team_write_enabled"),
            policy_json=args.get("policy_json"),
            admin_key=args.get("admin_key"),
            actor_user_id=args.get("actor_user_id"),
        )
        return {"ok": result.ok, **result.model_dump()}

    elif tool == "evidence_upload":
        # evidence_upload 工具：上传证据内容到 Logbook 存储
        # 参数校验
        content = args.get("content")
        content_type = args.get("content_type")
        
        if not content:
            return {
                "ok": False,
                "error_code": "MISSING_REQUIRED_PARAMETER",
                "retryable": False,
                "suggestion": "参数 'content' 为必填项，请提供证据内容",
            }
        
        if not content_type:
            return {
                "ok": False,
                "error_code": "MISSING_REQUIRED_PARAMETER",
                "retryable": False,
                "suggestion": "参数 'content_type' 为必填项，请提供内容类型",
                "allowed_types": list(ALLOWED_CONTENT_TYPES),
            }
        
        # 可选参数
        title = args.get("title")
        actor_user_id = args.get("actor_user_id")
        project_key = args.get("project_key")
        item_id = args.get("item_id")
        
        try:
            # 若 item_id 缺失，自动创建 item（通过 logbook_adapter 封装调用）
            if item_id is None:
                # 构建 scope_json
                scope_json = {
                    "source": "gateway",
                }
                if project_key:
                    scope_json["project_key"] = project_key
                
                # 通过 logbook_adapter 调用 create_item
                item_id = logbook_adapter.create_item(
                    item_type="evidence",
                    title=title or "evidence_upload",
                    scope_json=scope_json,
                    owner_user_id=actor_user_id,
                )
                logger.info(f"evidence_upload: 自动创建 item, item_id={item_id}")
            
            # 调用 upload_evidence
            result: EvidenceUploadResult = upload_evidence(
                content=content,
                content_type=content_type,
                actor_user_id=actor_user_id,
                project_key=project_key,
                item_id=item_id,
                title=title,
            )
            
            # 构建 v2 evidence 对象
            evidence_obj = result.to_evidence_object(title=title)
            
            return {
                "ok": True,
                "item_id": item_id,
                "attachment_id": result.attachment_id,
                "sha256": result.sha256,
                "evidence": evidence_obj,
                "artifact_uri": result.artifact_uri,
                "size_bytes": result.size_bytes,
                "content_type": result.content_type,
            }
            
        except EvidenceSizeLimitExceededError as e:
            return {
                "ok": False,
                "error_code": e.error_code,
                "retryable": e.retryable,
                "suggestion": e.details.get("suggestion"),
                "size_bytes": e.details.get("size_bytes"),
                "max_bytes": e.details.get("max_bytes"),
            }
        except EvidenceContentTypeError as e:
            return {
                "ok": False,
                "error_code": e.error_code,
                "retryable": e.retryable,
                "content_type": e.details.get("content_type"),
                "allowed_types": e.details.get("allowed_types"),
            }
        except EvidenceWriteError as e:
            return {
                "ok": False,
                "error_code": e.error_code,
                "retryable": e.retryable,
                "message": e.message,
                "original_error": e.details.get("original_error"),
            }
        except EvidenceItemRequiredError as e:
            # 不应发生（因为前面已处理 item_id 缺失），但保留作为安全网
            return {
                "ok": False,
                "error_code": e.error_code,
                "retryable": e.retryable,
                "suggestion": e.details.get("suggestion"),
            }
        except EvidenceUploadError as e:
            # 通用 evidence 上传错误
            return {
                "ok": False,
                "error_code": e.error_code,
                "retryable": e.retryable,
                "message": e.message,
                **e.details,
            }
        except Exception as e:
            logger.exception(f"evidence_upload 未预期错误: {e}")
            return {
                "ok": False,
                "error_code": "INTERNAL_ERROR",
                "retryable": True,
                "message": str(e),
            }

    else:
        raise ValueError(f"未知工具: {tool}")


# ===================== 注册工具执行器到 mcp_rpc 模块 =====================

# 注册工具执行器，使 mcp_rpc.py 中的 tools/call handler 能够调用业务实现
# 这样 JSON-RPC 层的协议处理在 mcp_rpc.py，业务逻辑在 main.py
register_tool_executor(_execute_tool)


# CORS 配置常量
MCP_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, Mcp-Session-Id",
    "Access-Control-Expose-Headers": "Mcp-Session-Id",
    "Access-Control-Max-Age": "86400",
}


@app.options("/mcp")
async def mcp_options():
    """
    MCP 端点的 CORS 预检请求处理
    
    返回必要的 CORS headers，允许跨域调用 /mcp 端点。
    """
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
    
    JSON-RPC 2.0 支持的方法:
    - tools/list: 返回可用工具清单
    - tools/call: 调用工具 (params: {name, arguments})
    
    旧格式支持的工具:
    - memory_store: 存储记忆
    - memory_query: 查询记忆
    - reliability_report: 获取可靠性统计报告（只读）
    - governance_update: 更新治理设置（需鉴权）
    
    支持的请求头:
    - Mcp-Session-Id: MCP 会话 ID（可选，用于日志关联）
    
    错误响应格式:
    - 所有 JSON-RPC 错误都返回结构化的 error.data (ErrorData)
    - error.data 包含: category, reason, retryable, correlation_id, details
    """
    # 生成 correlation_id 用于追踪
    correlation_id = generate_correlation_id()
    
    # 提取 Mcp-Session-Id 用于日志关联（可选，不强依赖）
    mcp_session_id = request.headers.get("Mcp-Session-Id") or request.headers.get("mcp-session-id")
    if mcp_session_id:
        logger.info(f"MCP 请求: Mcp-Session-Id={mcp_session_id}, correlation_id={correlation_id}")
    
    # 解析原始请求 JSON
    try:
        body = await request.json()
    except Exception as e:
        # JSON 解析失败 - 使用结构化错误响应
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
            headers=MCP_CORS_HEADERS,
        )
    
    # 自动识别请求格式：根据是否含 jsonrpc 字段判断
    if is_jsonrpc_request(body):
        # ========== JSON-RPC 2.0 分支 ==========
        rpc_request, parse_error = parse_jsonrpc_request(body)
        if parse_error:
            # 请求解析失败 - 增强 error.data
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
                headers=MCP_CORS_HEADERS,
            )
        
        # 使用路由器分发请求（dispatch 内部已使用 to_jsonrpc_error 处理异常）
        response = await mcp_router.dispatch(rpc_request, correlation_id=correlation_id)
        
        # 确保响应中的错误有 correlation_id
        if response.error and response.error.data:
            if isinstance(response.error.data, dict) and "correlation_id" not in response.error.data:
                response.error.data["correlation_id"] = correlation_id
        
        return JSONResponse(
            content=response.model_dump(exclude_none=True),
            headers=MCP_CORS_HEADERS,
        )
    
    else:
        # ========== 旧协议分支（保持向后兼容）==========
        try:
            mcp_request = MCPToolCall(**body)
        except Exception as e:
            return JSONResponse(
                content={"ok": False, "error": f"无效的请求格式: {str(e)}", "correlation_id": correlation_id},
                status_code=400,
                headers=MCP_CORS_HEADERS,
            )
        
        tool = mcp_request.tool
        args = mcp_request.arguments

        try:
            result = await _execute_tool(tool, args)
            return JSONResponse(
                content=MCPResponse(ok=result.get("ok", True), result=result).model_dump(),
                headers=MCP_CORS_HEADERS,
            )
        except ValueError as e:
            return JSONResponse(
                content=MCPResponse(ok=False, error=str(e)).model_dump(),
                headers=MCP_CORS_HEADERS,
            )
        except Exception as e:
            logger.exception(f"MCP 调用失败: {e}")
            return JSONResponse(
                content=MCPResponse(ok=False, error=str(e)).model_dump(),
                headers=MCP_CORS_HEADERS,
            )


@app.post("/memory/store", response_model=MemoryStoreResponse)
async def memory_store_endpoint(request: MemoryStoreRequest):
    """直接调用 memory_store（REST 风格）"""
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
    )


@app.post("/memory/query", response_model=MemoryQueryResponse)
async def memory_query_endpoint(request: MemoryQueryRequest):
    """直接调用 memory_query（REST 风格）"""
    return await memory_query_impl(
        query=request.query,
        spaces=request.spaces,
        filters=request.filters,
        top_k=request.top_k,
    )


@app.get("/reliability/report", response_model=ReliabilityReportResponse)
async def reliability_report_endpoint():
    """
    获取可靠性统计报告（只读端点）
    
    聚合 logbook.outbox_memory 与 governance.write_audit 的统计数据。
    报告结构符合 schemas/reliability_report_v1.schema.json。
    """
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
    """
    更新治理设置（受保护端点）
    
    鉴权方式（满足其一即可）：
    1. admin_key 与环境变量 GOVERNANCE_ADMIN_KEY 匹配
    2. actor_user_id 在 policy_json.allowlist_users 中
    
    变更内容会记录到 governance.write_audit 表。
    """
    return await governance_update_impl(
        team_write_enabled=request.team_write_enabled,
        policy_json=request.policy_json,
        admin_key=request.admin_key,
        actor_user_id=request.actor_user_id,
    )


# ===================== 启动入口 =====================


def _format_db_repair_commands(error_code: str = None, missing_items = None) -> str:
    """
    格式化数据库修复命令提示。
    
    Args:
        error_code: 错误代码（如 SCHEMA_MISSING, TABLE_MISSING 等）
        missing_items: 缺失项列表或字典
    
    Returns:
        格式化的修复命令字符串
    """
    lines = [
        "",
        "======================================",
        "修复命令",
        "======================================",
        "",
        "# 方案 1: 完整初始化（首次部署或重建）",
        "# 先初始化角色权限，再执行迁移",
        "python logbook_postgres/scripts/db_bootstrap.py",
        "python logbook_postgres/scripts/db_migrate.py --apply-roles --apply-openmemory-grants",
        "",
        "# 方案 2: 仅执行迁移（角色已存在）",
        "python logbook_postgres/scripts/db_migrate.py",
        "",
        "# 方案 3: Docker 环境",
        "docker compose -f docker-compose.unified.yml up bootstrap_roles logbook_migrate openmemory_migrate",
        "",
        "# 验证修复结果",
        "python logbook_postgres/scripts/db_migrate.py --verify",
        "",
    ]
    
    if error_code:
        lines.insert(1, f"错误代码: {error_code}")
    
    if missing_items:
        lines.append("缺失项详情:")
        # 处理字典类型的 missing_items
        if isinstance(missing_items, dict):
            items_list = []
            for key, value in missing_items.items():
                if isinstance(value, list):
                    for v in value:
                        items_list.append(f"{key}: {v}")
                else:
                    items_list.append(f"{key}: {value}")
            missing_items = items_list
        
        # 处理列表类型
        for item in list(missing_items)[:10]:  # 最多显示 10 项
            lines.append(f"  - {item}")
        if len(missing_items) > 10:
            lines.append(f"  ... 还有 {len(missing_items) - 10} 项")
        lines.append("")
    
    return "\n".join(lines)


def check_logbook_db_on_startup(config) -> bool:
    """
    启动时检查 Logbook DB 结构（统一入口）
    
    此函数是 Logbook DB 就绪性检查的统一入口，优先通过
    LogbookAdapter.ensure_db_ready(auto_migrate=...) 实现检查逻辑，
    避免在各处重复实现检查代码。
    
    策略说明:
    =========
    
    1. 默认行为（LOGBOOK_CHECK_ON_STARTUP=true, AUTO_MIGRATE_ON_STARTUP=false）:
       - 仅检查 DB 结构是否完整
       - 如果缺失，输出可操作的修复命令提示
       - 返回 False 阻止服务启动
    
    2. 自动迁移模式（AUTO_MIGRATE_ON_STARTUP=true）:
       - 检查 DB 结构
       - 如果缺失，自动执行 db_migrate 迁移脚本
       - 迁移成功则返回 True，失败则返回 False
    
    3. 跳过检查（LOGBOOK_CHECK_ON_STARTUP=false）:
       - 不执行任何检查，直接返回 True
       - 适用于已知 DB 结构完整的生产环境
    
    环境变量:
    ========
    
    - LOGBOOK_CHECK_ON_STARTUP: 是否在启动时检查 DB（默认 true）
    - AUTO_MIGRATE_ON_STARTUP: 检测到 DB 缺失时是否自动迁移（默认 false）
    
    错误码:
    ======
    
    - LOGBOOK_DB_SCHEMA_MISSING: schema 缺失（如 governance, logbook）
    - LOGBOOK_DB_TABLE_MISSING: 表缺失
    - LOGBOOK_DB_INDEX_MISSING: 索引缺失
    - LOGBOOK_DB_MIGRATE_FAILED: 自动迁移失败
    
    Args:
        config: GatewayConfig 配置对象
        
    Returns:
        True 如果检查通过（或跳过检查），False 如果检查失败
    """
    if not config.logbook_check_on_startup:
        logger.info("跳过 Logbook DB 检查 (LOGBOOK_CHECK_ON_STARTUP=false)")
        return True
    
    if not is_db_migrate_available():
        logger.warning("db_migrate 模块不可用，跳过 Logbook DB 检查")
        return True
    
    logger.info("========================================")
    logger.info("DB 层预检: 检查 Logbook 数据库结构...")
    logger.info("========================================")
    
    try:
        result = ensure_db_ready(
            dsn=config.postgres_dsn,
            auto_migrate=config.auto_migrate_on_startup,
        )
        
        if result.ok:
            if result.message:
                logger.info(f"[OK] Logbook DB 检查通过: {result.message}")
            else:
                logger.info("[OK] Logbook DB 检查通过: 所有 schema/表/索引/物化视图已就绪")
            return True
        else:
            logger.error("========================================")
            logger.error("[FAIL] Logbook DB 检查失败")
            logger.error("========================================")
            logger.error(f"原因: {result.message}")
            
            # 输出修复命令
            repair_hint = _format_db_repair_commands(
                error_code=getattr(result, 'code', None),
                missing_items=getattr(result, 'missing_items', None),
            )
            logger.error(repair_hint)
            return False
            
    except LogbookDBCheckError as e:
        logger.error("========================================")
        logger.error("[FAIL] Logbook DB 检查失败")
        logger.error("========================================")
        logger.error(f"错误码: {e.code}")
        logger.error(f"原因: {e.message}")
        
        # 输出修复命令
        repair_hint = _format_db_repair_commands(
            error_code=e.code,
            missing_items=e.missing_items,
        )
        logger.error(repair_hint)
        return False
        
    except Exception as e:
        logger.error("========================================")
        logger.error("[FAIL] Logbook DB 检查时发生未预期错误")
        logger.error("========================================")
        logger.exception(f"错误详情: {e}")
        
        # 输出修复命令
        repair_hint = _format_db_repair_commands()
        logger.error(repair_hint)
        return False


def main():
    """CLI 启动入口"""
    import uvicorn
    import argparse
    
    if any(flag in sys.argv for flag in ("-h", "--help")):
        parser = argparse.ArgumentParser(
            prog="engram-gateway",
            description="Engram Gateway 服务入口",
        )
        parser.add_argument(
            "--host",
            default="0.0.0.0",
            help="监听地址（默认 0.0.0.0）",
        )
        parser.add_argument(
            "--port",
            type=int,
            default=8787,
            help="监听端口（默认 8787）",
        )
        parser.print_help()
        return
    
    # 启动时校验配置
    try:
        config = get_config()
        validate_config()
        logger.info(f"配置加载成功: project={config.project_key}, port={config.gateway_port}")
        
        # 使用配置中的 postgres_dsn 初始化 DB 实例
        # 这确保 DB 连接使用配置文件中的 DSN，而不仅依赖环境变量
        set_default_dsn(config.postgres_dsn)
        get_db(dsn=config.postgres_dsn)
        logger.info(f"数据库连接初始化完成")
        
        # Logbook DB 结构检查
        if not check_logbook_db_on_startup(config):
            logger.error("Logbook DB 检查失败，服务无法启动")
            sys.exit(1)
        
    except ConfigError as e:
        logger.error(f"配置错误: {e}")
        sys.exit(1)
    
    uvicorn.run(
        "engram.gateway.main:app",
        host="0.0.0.0",
        port=config.gateway_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
