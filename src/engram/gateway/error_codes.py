"""
Gateway ErrorCode 适配层

本模块提供两层错误码定义：

1. **MCP/JSON-RPC 层错误码**（本模块定义）:
   - `McpErrorCode`: JSON-RPC 2.0 标准错误码
   - `McpErrorCategory`: 错误分类常量
   - `McpErrorReason`: 错误原因码常量
   - `to_jsonrpc_error()`: 统一异常转换函数

2. **业务层错误码**（从 engram.logbook.errors 导入）:
   - `ErrorCode`: Logbook 业务层错误码常量

用法：
    # MCP/JSON-RPC 层
    from engram.gateway.error_codes import (
        McpErrorCode,
        McpErrorCategory,
        McpErrorReason,
        to_jsonrpc_error,
    )

    # 业务层
    from engram.gateway.error_codes import ErrorCode

注意：
- MCP 错误码用于 JSON-RPC 协议层响应
- 业务层错误码用于 audit 记录和业务逻辑
- 工具执行结果 result.error_code 使用 ToolResultErrorCode（见 result_error_codes.py），
  不在本模块承诺范围内

================================================================================
错误码命名空间边界声明 (Boundary Declaration)
================================================================================

本模块定义的 McpErrorReason 错误码**仅用于** JSON-RPC error.data.reason 字段。

禁止行为：
- 禁止将 ToolResultErrorCode.* (result_error_codes.py) 用于 error.data.reason
- 禁止将 McpErrorReason.* 用于 result.error_code

边界检查由 CI 门禁强制执行：
- 测试: tests/gateway/test_mcp_jsonrpc_contract.py::TestErrorCodeBoundaryMisuse
- 文档: docs/contracts/mcp_jsonrpc_error_v1.md §3.0

参见: result_error_codes.py 的对应边界声明
================================================================================
"""

from typing import Any, Dict, Optional

# =============================================================================
# MCP/JSON-RPC 2.0 错误码定义
# =============================================================================


class McpErrorCode:
    """
    JSON-RPC 2.0 标准错误码

    用于 MCP Gateway 的 JSON-RPC 错误响应。

    标准错误码 (-32700 ~ -32600):
    - PARSE_ERROR (-32700): JSON 解析错误
    - INVALID_REQUEST (-32600): 无效请求
    - METHOD_NOT_FOUND (-32601): 方法不存在
    - INVALID_PARAMS (-32602): 无效参数
    - INTERNAL_ERROR (-32603): 内部错误

    自定义错误码 (-32000 ~ -32099):
    - DEPENDENCY_UNAVAILABLE (-32001): 依赖服务不可用
    - BUSINESS_REJECTION (-32002): 业务拒绝

    已废弃:
    - TOOL_EXECUTION_ERROR (-32000): 已废弃，参见 §13.5

    参见: docs/contracts/mcp_jsonrpc_error_v1.md
    """

    PARSE_ERROR = -32700  # 解析错误
    INVALID_REQUEST = -32600  # 无效请求
    METHOD_NOT_FOUND = -32601  # 方法不存在
    INVALID_PARAMS = -32602  # 无效参数
    INTERNAL_ERROR = -32603  # 内部错误
    # 自定义服务器错误 (-32000 to -32099)
    # ⚠️ 已废弃：不再生成此错误码，仅为向后兼容保留定义
    # 迁移指南：参数问题使用 -32602，内部错误使用 -32603
    # 参见: docs/contracts/mcp_jsonrpc_error_v1.md §13.5
    TOOL_EXECUTION_ERROR = -32000  # @deprecated since v1.0
    DEPENDENCY_UNAVAILABLE = -32001  # 依赖服务不可用
    BUSINESS_REJECTION = -32002  # 业务拒绝


class McpErrorCategory:
    """
    错误分类常量

    用于 JSON-RPC error.data.category 字段。

    分类说明:
    - protocol: 协议层错误（JSON-RPC 格式、方法不存在）
    - validation: 参数校验错误
    - business: 业务逻辑拒绝（策略拒绝、鉴权失败）
    - dependency: 依赖服务错误（OpenMemory/Logbook 不可用）
    - internal: 内部错误（未处理的异常）

    参见: docs/contracts/mcp_jsonrpc_error_v1.md
    """

    PROTOCOL = "protocol"  # 协议层错误
    VALIDATION = "validation"  # 参数校验错误
    BUSINESS = "business"  # 业务逻辑拒绝
    DEPENDENCY = "dependency"  # 依赖服务错误
    INTERNAL = "internal"  # 内部错误


class McpErrorReason:
    """
    错误原因码常量

    用于 JSON-RPC error.data.reason 字段。

    原因码按分类组织:
    - 协议层: PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND
    - 参数校验: MISSING_REQUIRED_PARAM, INVALID_PARAM_TYPE, INVALID_PARAM_VALUE, UNKNOWN_TOOL
    - 业务拒绝: POLICY_REJECT, AUTH_FAILED, ACTOR_UNKNOWN, GOVERNANCE_UPDATE_DENIED
    - 依赖不可用: OPENMEMORY_*, LOGBOOK_DB_*
    - 内部错误: INTERNAL_ERROR, TOOL_EXECUTOR_NOT_REGISTERED, UNHANDLED_EXCEPTION

    注意：DEPENDENCY_MISSING 不在 McpErrorReason 中，而在 ToolResultErrorCode 中。
    - ToolResultErrorCode: 用于业务层 result.error_code，表示工具执行时依赖缺失
    - 参见: engram.gateway.result_error_codes.ToolResultErrorCode

    参见: docs/contracts/mcp_jsonrpc_error_v1.md
    """

    # 协议层
    PARSE_ERROR = "PARSE_ERROR"
    INVALID_REQUEST = "INVALID_REQUEST"
    METHOD_NOT_FOUND = "METHOD_NOT_FOUND"

    # 参数校验
    MISSING_REQUIRED_PARAM = "MISSING_REQUIRED_PARAM"
    INVALID_PARAM_TYPE = "INVALID_PARAM_TYPE"
    INVALID_PARAM_VALUE = "INVALID_PARAM_VALUE"
    UNKNOWN_TOOL = "UNKNOWN_TOOL"

    # 业务拒绝
    POLICY_REJECT = "POLICY_REJECT"
    AUTH_FAILED = "AUTH_FAILED"
    ACTOR_UNKNOWN = "ACTOR_UNKNOWN"
    GOVERNANCE_UPDATE_DENIED = "GOVERNANCE_UPDATE_DENIED"

    # 依赖不可用
    OPENMEMORY_UNAVAILABLE = "OPENMEMORY_UNAVAILABLE"
    OPENMEMORY_CONNECTION_FAILED = "OPENMEMORY_CONNECTION_FAILED"
    OPENMEMORY_API_ERROR = "OPENMEMORY_API_ERROR"
    LOGBOOK_DB_UNAVAILABLE = "LOGBOOK_DB_UNAVAILABLE"
    LOGBOOK_DB_CHECK_FAILED = "LOGBOOK_DB_CHECK_FAILED"
    # 注意: DEPENDENCY_MISSING 不在此处，参见 ToolResultErrorCode

    # 内部错误
    INTERNAL_ERROR = "INTERNAL_ERROR"
    TOOL_EXECUTOR_NOT_REGISTERED = "TOOL_EXECUTOR_NOT_REGISTERED"
    UNHANDLED_EXCEPTION = "UNHANDLED_EXCEPTION"


# =============================================================================
# PUBLIC_MCP_ERROR_REASONS: 对外契约列表
# =============================================================================
#
# 此 tuple 定义了 MCP JSON-RPC 错误响应中 error.data.reason 字段的所有有效值。
#
# SSOT（单一事实来源）策略：
# - Schema (schemas/mcp_jsonrpc_error_v1.schema.json) 为枚举值权威来源
# - 本列表跟随 Schema 同步
# - McpErrorReason 类与本列表保持一致
#
# 同步检查清单（新增/删除 reason 码时）：
# 1. Schema enum (definitions.error_reason.enum)
# 2. McpErrorReason 类常量
# 3. PUBLIC_MCP_ERROR_REASONS tuple
# 4. docs/contracts/mcp_jsonrpc_error_v1.md §4 表格
#
# 验证命令：
# - make check-mcp-error-contract
# - pytest tests/gateway/test_mcp_jsonrpc_contract.py::TestErrorReasonWhitelistConsistency -q
#
# 参见:
# - docs/contracts/mcp_jsonrpc_error_v1.md §13.3
# - docs/contracts/mcp_jsonrpc_error_v1_drift_matrix.md
# =============================================================================

PUBLIC_MCP_ERROR_REASONS: tuple[str, ...] = (
    # 协议层
    "PARSE_ERROR",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    # 参数校验
    "MISSING_REQUIRED_PARAM",
    "INVALID_PARAM_TYPE",
    "INVALID_PARAM_VALUE",
    "UNKNOWN_TOOL",
    # 业务拒绝
    "POLICY_REJECT",
    "AUTH_FAILED",
    "ACTOR_UNKNOWN",
    "GOVERNANCE_UPDATE_DENIED",
    # 依赖不可用
    "OPENMEMORY_UNAVAILABLE",
    "OPENMEMORY_CONNECTION_FAILED",
    "OPENMEMORY_API_ERROR",
    "LOGBOOK_DB_UNAVAILABLE",
    "LOGBOOK_DB_CHECK_FAILED",
    # 注意: DEPENDENCY_MISSING 不在此处，它属于 ToolResultErrorCode（业务层 result.error_code）
    # 内部错误
    "INTERNAL_ERROR",
    "TOOL_EXECUTOR_NOT_REGISTERED",
    "UNHANDLED_EXCEPTION",
)


def _extract_mcp_error_reason_public_constants() -> set[str]:
    """
    通过反射提取 McpErrorReason 类的公开字符串常量

    条件：非 __*__、非 _* 开头、值为 str 类型

    Returns:
        McpErrorReason 所有公开字符串常量的集合
    """
    public_reasons: set[str] = set()
    for name in dir(McpErrorReason):
        # 排除 dunder 属性
        if name.startswith("__") and name.endswith("__"):
            continue
        # 排除私有属性（以单下划线开头）
        if name.startswith("_"):
            continue
        value = getattr(McpErrorReason, name)
        # 只取字符串类型的类属性
        if isinstance(value, str):
            public_reasons.add(value)
    return public_reasons


def verify_public_mcp_error_reasons() -> tuple[bool, str]:
    """
    校验 PUBLIC_MCP_ERROR_REASONS 与 McpErrorReason 公开常量的一致性

    此函数供测试/CI 使用，验证契约列表与代码实现保持同步。

    Returns:
        (is_valid, message) 元组：
        - is_valid: True 表示一致，False 表示不一致
        - message: 描述信息，不一致时包含差异详情

    Example:
        >>> is_valid, msg = verify_public_mcp_error_reasons()
        >>> assert is_valid, msg
    """
    public_set = set(PUBLIC_MCP_ERROR_REASONS)
    class_set = _extract_mcp_error_reason_public_constants()

    if public_set == class_set:
        return True, "PUBLIC_MCP_ERROR_REASONS 与 McpErrorReason 公开常量一致"

    only_in_tuple = public_set - class_set
    only_in_class = class_set - public_set

    message_parts = ["PUBLIC_MCP_ERROR_REASONS 与 McpErrorReason 公开常量不一致。"]
    if only_in_tuple:
        message_parts.append(f"仅在 PUBLIC_MCP_ERROR_REASONS 中: {sorted(only_in_tuple)}")
    if only_in_class:
        message_parts.append(f"仅在 McpErrorReason 中: {sorted(only_in_class)}")
    message_parts.append("修复方法：确保 PUBLIC_MCP_ERROR_REASONS 包含所有 McpErrorReason 公开常量")

    return False, "\n".join(message_parts)


# =============================================================================
# 异常类型到错误属性的映射规则
# =============================================================================

# 映射规则类型定义
ExceptionMappingRule = Dict[str, Any]

# 映射规则结构: (category, reason, retryable, code, message_prefix, details_defaults)
# - category: 错误分类常量 (McpErrorCategory.*)
# - reason: 错误原因码常量 (McpErrorReason.*) 或 None（需要动态计算）
# - retryable: 是否可重试（布尔值或 None 表示需要动态计算）
# - code: JSON-RPC 错误码 (McpErrorCode.*)
# - message_prefix: 错误消息前缀
# - details_defaults: 默认 details 字典（如 {"service": "openmemory"}）
#
# 注意：此映射仅用于非 GatewayError 的异常类型。
# GatewayError 有自己的 category/reason/retryable 属性，在 to_jsonrpc_error 中单独处理。

EXCEPTION_TYPE_MAPPING: Dict[str, ExceptionMappingRule] = {
    # ===== 工具调用异常 =====
    "ToolCallError": {
        "category": None,  # 从异常属性获取
        "reason": None,  # 从异常属性获取
        "retryable": None,  # 从异常属性获取
        "code": None,  # 根据 category 动态计算
        "message_prefix": None,  # 直接使用异常消息
        "details_defaults": {},
    },
    # ===== OpenMemory 异常 =====
    "OpenMemoryConnectionError": {
        "category": McpErrorCategory.DEPENDENCY,
        "reason": McpErrorReason.OPENMEMORY_CONNECTION_FAILED,
        "retryable": True,
        "code": McpErrorCode.DEPENDENCY_UNAVAILABLE,
        "message_prefix": "OpenMemory 连接失败",
        "details_defaults": {"service": "openmemory"},
    },
    "OpenMemoryAPIError": {
        "category": McpErrorCategory.DEPENDENCY,
        "reason": McpErrorReason.OPENMEMORY_API_ERROR,
        "retryable": None,  # 需要根据 status_code 动态计算：5xx 可重试，4xx 不可重试
        "code": McpErrorCode.DEPENDENCY_UNAVAILABLE,
        "message_prefix": "OpenMemory API 错误",
        "details_defaults": {"service": "openmemory"},
    },
    "OpenMemoryError": {
        "category": McpErrorCategory.DEPENDENCY,
        "reason": McpErrorReason.OPENMEMORY_UNAVAILABLE,
        "retryable": True,
        "code": McpErrorCode.DEPENDENCY_UNAVAILABLE,
        "message_prefix": "OpenMemory 错误",
        "details_defaults": {"service": "openmemory"},
    },
    # ===== Logbook 异常 =====
    "LogbookDBCheckError": {
        "category": McpErrorCategory.DEPENDENCY,
        "reason": McpErrorReason.LOGBOOK_DB_CHECK_FAILED,
        "retryable": False,  # DB 结构问题通常不可自动重试
        "code": McpErrorCode.DEPENDENCY_UNAVAILABLE,
        "message_prefix": "Logbook DB 检查失败",
        "details_defaults": {"service": "logbook_db"},
    },
    # ===== 标准异常 =====
    "ValueError": {
        "category": McpErrorCategory.VALIDATION,
        "reason": None,  # 需要根据错误消息动态推断
        "retryable": False,
        "code": McpErrorCode.INVALID_PARAMS,
        "message_prefix": None,  # 直接使用错误消息
        "details_defaults": {},
    },
    "RuntimeError": {
        "category": McpErrorCategory.INTERNAL,
        "reason": None,  # 需要根据错误消息动态推断
        "retryable": False,
        "code": McpErrorCode.INTERNAL_ERROR,
        "message_prefix": None,  # 直接使用错误消息
        "details_defaults": {},
    },
    # ===== 数据库异常（通过类名模式匹配）=====
    # psycopg2/database/operational 异常
    "_database_pattern": {
        "category": McpErrorCategory.DEPENDENCY,
        "reason": McpErrorReason.LOGBOOK_DB_UNAVAILABLE,
        "retryable": True,  # 数据库连接问题通常可重试
        "code": McpErrorCode.DEPENDENCY_UNAVAILABLE,
        "message_prefix": "数据库错误",
        "details_defaults": {"service": "logbook_db"},
    },
    # ===== 默认/未知异常 =====
    "_unknown": {
        "category": McpErrorCategory.INTERNAL,
        "reason": McpErrorReason.UNHANDLED_EXCEPTION,
        "retryable": False,
        "code": McpErrorCode.INTERNAL_ERROR,
        "message_prefix": "内部错误",
        "details_defaults": {},
    },
}


def _infer_value_error_reason(error_msg: str) -> str:
    """
    根据 ValueError 错误消息推断更具体的原因码

    Args:
        error_msg: 错误消息字符串

    Returns:
        McpErrorReason 常量
    """
    if "缺少" in error_msg or "missing" in error_msg.lower() or "required" in error_msg.lower():
        return McpErrorReason.MISSING_REQUIRED_PARAM
    elif "未知工具" in error_msg or "unknown tool" in error_msg.lower():
        return McpErrorReason.UNKNOWN_TOOL
    elif "类型" in error_msg or "type" in error_msg.lower():
        return McpErrorReason.INVALID_PARAM_TYPE
    else:
        return McpErrorReason.INVALID_PARAM_VALUE


def _infer_runtime_error_reason(error_msg: str) -> str:
    """
    根据 RuntimeError 错误消息推断更具体的原因码

    Args:
        error_msg: 错误消息字符串

    Returns:
        McpErrorReason 常量
    """
    if "执行器未注册" in error_msg or "not registered" in error_msg.lower():
        return McpErrorReason.TOOL_EXECUTOR_NOT_REGISTERED
    else:
        return McpErrorReason.INTERNAL_ERROR


def _is_database_exception(error_type_name: str) -> bool:
    """
    检查异常类型名是否匹配数据库异常模式

    Args:
        error_type_name: 异常类型名称

    Returns:
        True 如果匹配数据库异常模式
    """
    lower_name = error_type_name.lower()
    return "psycopg2" in lower_name or "database" in lower_name or "operational" in lower_name


def _compute_openmemory_api_error_retryable(error: Exception) -> bool:
    """
    计算 OpenMemoryAPIError 的 retryable 属性

    5xx 错误可重试，4xx 不可重试

    Args:
        error: OpenMemoryAPIError 异常

    Returns:
        是否可重试
    """
    if hasattr(error, "status_code") and error.status_code:
        status_code: int = getattr(error, "status_code")
        return status_code >= 500
    return False


def get_exception_mapping(error: Exception) -> ExceptionMappingRule:
    """
    根据异常类型获取对应的映射规则

    此函数根据异常类型名称查找 EXCEPTION_TYPE_MAPPING，
    并返回包含 category, reason, retryable, code, message_prefix, details_defaults 的字典。

    对于需要动态计算的字段（reason=None 或 retryable=None），
    会根据异常内容计算具体值。

    Args:
        error: 异常对象

    Returns:
        映射规则字典，包含所有必需字段
    """
    error_type_name = type(error).__name__
    error_msg = str(error)

    # 1. 精确匹配异常类型名
    if error_type_name in EXCEPTION_TYPE_MAPPING:
        mapping = EXCEPTION_TYPE_MAPPING[error_type_name].copy()

        # 动态计算 reason（如果为 None）
        if mapping["reason"] is None:
            if error_type_name == "ValueError":
                mapping["reason"] = _infer_value_error_reason(error_msg)
            elif error_type_name == "RuntimeError":
                mapping["reason"] = _infer_runtime_error_reason(error_msg)

        # 动态计算 retryable（如果为 None）
        if mapping["retryable"] is None:
            if error_type_name == "OpenMemoryAPIError":
                mapping["retryable"] = _compute_openmemory_api_error_retryable(error)

        return mapping

    # 2. 检查数据库异常模式
    if _is_database_exception(error_type_name):
        return EXCEPTION_TYPE_MAPPING["_database_pattern"].copy()

    # 3. 未知异常
    return EXCEPTION_TYPE_MAPPING["_unknown"].copy()


# =============================================================================
# to_jsonrpc_error 函数（委托给 mcp_rpc 模块）
# =============================================================================


def to_jsonrpc_error(
    error: Exception,
    req_id: Optional[Any] = None,
    tool_name: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> Any:  # 返回 JsonRpcResponse，但为避免循环导入使用 Any
    """
    将异常转换为 JSON-RPC 错误响应

    统一的错误转换函数，使用 EXCEPTION_TYPE_MAPPING 显式映射将异常转换为结构化错误响应。
    此函数委托给 mcp_rpc.to_jsonrpc_error() 实现。

    支持的异常类型：
    - GatewayError: 网关自定义错误（使用自身的 category/reason/retryable）
    - OpenMemoryConnectionError: OpenMemory 连接错误 (dependency, 可重试)
    - OpenMemoryAPIError: OpenMemory API 错误 (dependency, 5xx 可重试)
    - OpenMemoryError: OpenMemory 通用错误 (dependency, 可重试)
    - LogbookDBCheckError: Logbook 数据库检查错误 (dependency, 不可重试)
    - ValueError: 参数校验错误 (validation, 不可重试)
    - RuntimeError: 内部运行时错误 (internal, 不可重试)
    - psycopg2/database 异常: 数据库连接错误 (dependency, 可重试)
    - 其他异常: 未处理异常 (internal, 不可重试)

    映射规则详见: EXCEPTION_TYPE_MAPPING

    Args:
        error: 异常对象
        req_id: JSON-RPC 请求 ID
        tool_name: 工具名称（可选，用于上下文）
        correlation_id: 关联 ID（可选，用于追踪）

    Returns:
        JsonRpcResponse - 包含结构化 error.data 的错误响应

    Example:
        >>> from engram.gateway.error_codes import to_jsonrpc_error
        >>> response = to_jsonrpc_error(ValueError("缺少参数 name"), req_id=1)
    """
    # 延迟导入避免循环依赖
    from engram.gateway.mcp_rpc import to_jsonrpc_error as _to_jsonrpc_error

    return _to_jsonrpc_error(
        error=error,
        req_id=req_id,
        tool_name=tool_name,
        correlation_id=correlation_id,
    )


# =============================================================================
# 业务层 ErrorCode（从 engram.logbook.errors 导入或 stub）
# =============================================================================

try:
    from engram.logbook.errors import ErrorCode
except ImportError:
    # Stub 类：仅包含 gateway import-time 需要的属性/方法

    class ErrorCode:  # type: ignore[no-redef]
        """
        ErrorCode stub 类

        当 engram_logbook 模块不可用时提供基本的错误码常量。
        仅包含 gateway 模块在 import-time 需要的属性和方法。

        WARNING: 此为降级 stub，完整功能需安装 engram_logbook:
            pip install -e ".[full]"
        """

        # -------------------------------------------------------------------------
        # OpenMemory 相关错误码
        # -------------------------------------------------------------------------
        OPENMEMORY_WRITE_FAILED_CONNECTION = "openmemory_write_failed:connection_error"
        OPENMEMORY_WRITE_FAILED_API = "openmemory_write_failed:api_error"
        OPENMEMORY_WRITE_FAILED_GENERIC = "openmemory_write_failed:openmemory_error"
        OPENMEMORY_WRITE_FAILED_UNKNOWN = "openmemory_write_failed:unknown"

        @staticmethod
        def openmemory_api_error(status_code: Optional[int] = None) -> str:
            """生成 OpenMemory API 错误码，含状态码"""
            if status_code:
                return f"openmemory_write_failed:api_error_{status_code}"
            return ErrorCode.OPENMEMORY_WRITE_FAILED_API

        # -------------------------------------------------------------------------
        # Outbox Worker 相关错误码
        # -------------------------------------------------------------------------
        OUTBOX_FLUSH_SUCCESS = "outbox_flush_success"
        OUTBOX_FLUSH_RETRY = "outbox_flush_retry"
        OUTBOX_FLUSH_DEAD = "outbox_flush_dead"
        OUTBOX_FLUSH_CONFLICT = "outbox_flush_conflict"
        OUTBOX_FLUSH_DEDUP_HIT = "outbox_flush_dedup_hit"
        OUTBOX_FLUSH_DB_TIMEOUT = "outbox_flush_db_timeout"
        OUTBOX_FLUSH_DB_ERROR = "outbox_flush_db_error"
        OUTBOX_STALE = "outbox_stale"

        # -------------------------------------------------------------------------
        # Actor 用户相关错误码
        # -------------------------------------------------------------------------
        ACTOR_UNKNOWN_REJECT = "actor_unknown:reject"
        ACTOR_UNKNOWN_DEGRADE = "actor_unknown:degrade"
        ACTOR_AUTOCREATED = "actor_autocreated"
        ACTOR_AUTOCREATE_FAILED = "actor_autocreate_failed"

        # -------------------------------------------------------------------------
        # 去重相关错误码
        # -------------------------------------------------------------------------
        DEDUP_HIT = "dedup_hit"

        # -------------------------------------------------------------------------
        # 治理相关错误码
        # -------------------------------------------------------------------------
        GOVERNANCE_UPDATE_MISSING_CREDENTIALS = "governance_update:missing_credentials"
        GOVERNANCE_UPDATE_ADMIN_KEY_NOT_CONFIGURED = "governance_update:admin_key_not_configured"
        GOVERNANCE_UPDATE_INVALID_ADMIN_KEY = "governance_update:invalid_admin_key"
        GOVERNANCE_UPDATE_USER_NOT_IN_ALLOWLIST = "governance_update:user_not_in_allowlist"
        GOVERNANCE_UPDATE_INTERNAL_ERROR = "governance_update:internal_error"
        GOVERNANCE_UPDATE_ADMIN_KEY = "governance_update:admin_key"
        GOVERNANCE_UPDATE_ALLOWLIST_USER = "governance_update:allowlist_user"

        # -------------------------------------------------------------------------
        # 数据库相关错误码
        # -------------------------------------------------------------------------
        DB_CONNECTION_ERROR = "db_error:connection"
        DB_TIMEOUT_ERROR = "db_error:timeout"
        DB_QUERY_ERROR = "db_error:query"
        DB_TRANSACTION_ERROR = "db_error:transaction"

        # -------------------------------------------------------------------------
        # 工具调用相关错误码
        # -------------------------------------------------------------------------
        TOOL_UNKNOWN = "tool_call:unknown_tool"
        TOOL_MISSING_PARAM = "tool_call:missing_param"
        TOOL_INVALID_PARAM_TYPE = "tool_call:invalid_param_type"
        TOOL_INVALID_PARAM_VALUE = "tool_call:invalid_param_value"
        TOOL_EXECUTOR_NOT_REGISTERED = "tool_call:executor_not_registered"
        TOOL_INTERNAL_ERROR = "tool_call:internal_error"

        # -------------------------------------------------------------------------
        # 策略相关错误码
        # -------------------------------------------------------------------------
        @staticmethod
        def policy_reason(reason: str) -> str:
            """生成策略决策 reason，格式: policy:<reason>"""
            return f"policy:{reason}"


__all__ = [
    # MCP/JSON-RPC 层
    "McpErrorCode",
    "McpErrorCategory",
    "McpErrorReason",
    "EXCEPTION_TYPE_MAPPING",
    "ExceptionMappingRule",
    "get_exception_mapping",
    "to_jsonrpc_error",
    # MCP 错误原因契约列表（对外承诺）
    "PUBLIC_MCP_ERROR_REASONS",
    "verify_public_mcp_error_reasons",
    # 业务层
    "ErrorCode",
]
