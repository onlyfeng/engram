"""
engram_logbook.errors - 错误定义模块

定义所有 CLI 工具可能抛出的异常类型，统一错误码和错误消息格式。

退出码约定:
    0   - 成功
    1   - 通用错误 (ENGRAM_ERROR)
    2   - 配置错误 (CONFIG_ERROR)
    3   - 数据库错误 (DATABASE_ERROR)
    4   - 哈希计算错误 (HASHING_ERROR)
    5   - I/O 错误 (IO_ERROR)
    6   - 校验错误 (VALIDATION_ERROR)
    7   - 约束冲突 (CONSTRAINT_ERROR)
    11  - 身份同步错误 (IDENTITY_SYNC_ERROR)

错误码规范 (用于 audit.reason 字段):
    格式: <domain>_<action>:<detail>
    示例:
    - openmemory_write_failed:connection_error
    - outbox_flush_retry
    - actor_unknown:reject
"""

from typing import Any, Dict, Optional

# =============================================================================
# 错误码常量（用于 audit.reason 字段归一化）
# =============================================================================


class ErrorCode:
    """
    统一错误码常量，用于 audit.reason 字段。

    命名规范:
    - 前缀表示来源/领域: OPENMEMORY_, OUTBOX_, ACTOR_, DB_, POLICY_, GOVERNANCE_
    - 后缀表示详细错误类型
    - 使用冒号分隔领域和具体错误码

    使用示例:
        reason = ErrorCode.OPENMEMORY_WRITE_FAILED_CONNECTION
        # => "openmemory_write_failed:connection_error"
    """

    # -------------------------------------------------------------------------
    # OpenMemory 相关错误码
    # -------------------------------------------------------------------------
    # OpenMemory 写入失败
    OPENMEMORY_WRITE_FAILED_CONNECTION = "openmemory_write_failed:connection_error"
    OPENMEMORY_WRITE_FAILED_API = (
        "openmemory_write_failed:api_error"  # 可追加状态码 :api_error_<code>
    )
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
    # Outbox 刷新结果
    OUTBOX_FLUSH_SUCCESS = "outbox_flush_success"
    OUTBOX_FLUSH_RETRY = "outbox_flush_retry"
    OUTBOX_FLUSH_DEAD = "outbox_flush_dead"
    OUTBOX_FLUSH_CONFLICT = "outbox_flush_conflict"
    OUTBOX_FLUSH_DEDUP_HIT = "outbox_flush_dedup_hit"

    # Outbox 数据库错误
    OUTBOX_FLUSH_DB_TIMEOUT = "outbox_flush_db_timeout"
    OUTBOX_FLUSH_DB_ERROR = "outbox_flush_db_error"

    # Outbox 对账相关
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
    # 策略相关错误码（前缀 policy:）
    # -------------------------------------------------------------------------
    @staticmethod
    def policy_reason(reason: str) -> str:
        """生成策略决策 reason，格式: policy:<reason>"""
        return f"policy:{reason}"

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


# =============================================================================
# 退出码枚举
# =============================================================================


class ExitCode:
    """退出码常量"""

    SUCCESS = 0
    ENGRAM_ERROR = 1
    CONFIG_ERROR = 2
    DATABASE_ERROR = 3
    HASHING_ERROR = 4
    IO_ERROR = 5
    VALIDATION_ERROR = 6
    CONSTRAINT_ERROR = 7
    IDENTITY_SYNC_ERROR = 11
    MATERIALIZE_ERROR = 12
    WRITE_DISABLED_ERROR = 13  # 制品存储只读模式，写入被禁止


# =============================================================================
# 基础异常类
# =============================================================================


class EngramError(Exception):
    """engram_logbook 基础异常类"""

    exit_code: int = ExitCode.ENGRAM_ERROR
    error_type: str = "ENGRAM_ERROR"

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        """
        转换为可序列化的字典格式

        格式: {ok: false, code: str, message: str, detail: dict}
        """
        return {
            "ok": False,
            "code": self.error_type,
            "message": self.message,
            "detail": self.details,
        }


# =============================================================================
# 配置相关错误 (exit_code = 2)
# =============================================================================


class ConfigError(EngramError):
    """配置相关错误"""

    exit_code = ExitCode.CONFIG_ERROR
    error_type = "CONFIG_ERROR"


# 别名，向后兼容
EngramConfigError = ConfigError


class ConfigNotFoundError(ConfigError):
    """配置文件未找到"""

    error_type = "CONFIG_NOT_FOUND"


class ConfigParseError(ConfigError):
    """配置文件解析错误"""

    error_type = "CONFIG_PARSE_ERROR"


class ConfigValueError(ConfigError):
    """配置值无效"""

    error_type = "CONFIG_VALUE_ERROR"


# =============================================================================
# 数据库相关错误 (exit_code = 3)
# =============================================================================


class DatabaseError(EngramError):
    """数据库相关错误"""

    exit_code = ExitCode.DATABASE_ERROR
    error_type = "DATABASE_ERROR"


# 别名，向后兼容
EngramDatabaseError = DatabaseError


class DbConnectionError(DatabaseError):
    """数据库连接错误"""

    error_type = "CONNECTION_ERROR"


class DbTimeoutError(DatabaseError):
    """数据库语句超时错误（statement_timeout）"""

    error_type = "DB_TIMEOUT_ERROR"


class QueryError(DatabaseError):
    """数据库查询错误"""

    error_type = "QUERY_ERROR"


class TransactionError(DatabaseError):
    """事务错误"""

    error_type = "TRANSACTION_ERROR"


# =============================================================================
# 哈希计算错误 (exit_code = 4)
# =============================================================================


class HashingError(EngramError):
    """哈希计算错误"""

    exit_code = ExitCode.HASHING_ERROR
    error_type = "HASHING_ERROR"


class FileHashNotFoundError(HashingError):
    """文件未找到（用于哈希计算）"""

    error_type = "FILE_NOT_FOUND"


# =============================================================================
# I/O 相关错误 (exit_code = 5)
# =============================================================================


class EngramIOError(EngramError):
    """I/O 相关错误"""

    exit_code = ExitCode.IO_ERROR
    error_type = "IO_ERROR"


class FileReadError(EngramIOError):
    """文件读取错误"""

    error_type = "FILE_READ_ERROR"


class FileWriteError(EngramIOError):
    """文件写入错误"""

    error_type = "FILE_WRITE_ERROR"


# =============================================================================
# 校验错误 (exit_code = 6)
# =============================================================================


class ValidationError(EngramError):
    """输入验证错误"""

    exit_code = ExitCode.VALIDATION_ERROR
    error_type = "VALIDATION_ERROR"


class SchemaValidationError(ValidationError):
    """数据结构校验错误"""

    error_type = "SCHEMA_VALIDATION_ERROR"


class FormatValidationError(ValidationError):
    """格式校验错误（如 SHA256 格式）"""

    error_type = "FORMAT_VALIDATION_ERROR"


class Sha256MismatchError(ValidationError):
    """SHA256 哈希值不匹配"""

    error_type = "SHA256_MISMATCH"


class MemoryUriNotFoundError(ValidationError):
    """memory:// URI 指向的资源未找到"""

    error_type = "MEMORY_URI_NOT_FOUND"


class MemoryUriInvalidError(ValidationError):
    """memory:// URI 格式无效"""

    error_type = "MEMORY_URI_INVALID"


# =============================================================================
# 约束冲突错误 (exit_code = 7)
# =============================================================================


class ConstraintError(EngramError):
    """约束冲突错误"""

    exit_code = ExitCode.CONSTRAINT_ERROR
    error_type = "CONSTRAINT_ERROR"


class UniqueConstraintError(ConstraintError):
    """唯一约束冲突"""

    error_type = "UNIQUE_CONSTRAINT_ERROR"


class ForeignKeyError(ConstraintError):
    """外键约束冲突"""

    error_type = "FOREIGN_KEY_ERROR"


class ReferenceNotFoundError(ConstraintError):
    """引用目标不存在"""

    error_type = "REFERENCE_NOT_FOUND"


# =============================================================================
# 物化错误 (exit_code = 12)
# =============================================================================


class MaterializeError(EngramError):
    """物化错误基类"""

    exit_code = ExitCode.MATERIALIZE_ERROR
    error_type = "MATERIALIZE_ERROR"


class UriNotResolvableError(MaterializeError):
    """URI 不可解析"""

    error_type = "URI_NOT_RESOLVABLE"


class ChecksumMismatchError(MaterializeError):
    """SHA256 校验不匹配（源内容可能已变更）"""

    error_type = "CHECKSUM_MISMATCH"


class PayloadTooLargeError(MaterializeError):
    """Diff 内容超过大小限制"""

    error_type = "PAYLOAD_TOO_LARGE"


class FetchError(MaterializeError):
    """从 SVN/GitLab 拉取失败"""

    error_type = "FETCH_ERROR"


# =============================================================================
# 工具函数
# =============================================================================


def make_success_result(**kwargs) -> Dict[str, Any]:
    """
    构造成功结果

    Args:
        **kwargs: 额外的结果字段

    Returns:
        {ok: true, ...kwargs}
    """
    return {"ok": True, **kwargs}


def make_error_result(
    code: str, message: str, detail: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    构造错误结果

    Args:
        code: 错误码
        message: 错误消息
        detail: 错误详情

    Returns:
        {ok: false, code, message, detail}
    """
    return {
        "ok": False,
        "code": code,
        "message": message,
        "detail": detail or {},
    }
