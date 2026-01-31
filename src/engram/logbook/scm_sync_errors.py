# -*- coding: utf-8 -*-
"""
scm_sync_errors.py - SCM 同步错误分类模块

统一定义错误类别枚举与分类函数，供 worker/reaper 复用。

错误分类策略：
- 永久性错误（PERMANENT）：不应重试，直接标记为 dead
  - auth_error: 认证错误（401）
  - auth_missing: 认证凭证缺失
  - auth_invalid: 认证凭证无效
  - repo_not_found: 仓库不存在（404）
  - repo_type_unknown: 仓库类型未知
  - permission_denied: 权限不足（403）

- 临时性错误（TRANSIENT）：应该重试，使用退避策略
  - rate_limit: API 速率限制（429）
  - timeout: 请求超时
  - network: 网络错误
  - server_error: 服务器错误（5xx）
  - connection: 连接错误

- 忽略类别（IGNORED）：非错误，不计入失败预算
  - lock_held: 锁被其他进程持有，安全让出并使用 jitter 重入队
"""

from enum import Enum
from typing import Optional, Tuple


class ErrorCategory(str, Enum):
    """错误类别枚举"""

    # === 永久性错误（不应重试）===
    AUTH_ERROR = "auth_error"  # 认证错误
    AUTH_MISSING = "auth_missing"  # 认证凭证缺失
    AUTH_INVALID = "auth_invalid"  # 认证凭证无效
    REPO_NOT_FOUND = "repo_not_found"  # 仓库不存在
    REPO_TYPE_UNKNOWN = "repo_type_unknown"  # 仓库类型未知
    PERMISSION_DENIED = "permission_denied"  # 权限不足

    # === 临时性错误（应该重试）===
    RATE_LIMIT = "rate_limit"  # API 速率限制（429）
    TIMEOUT = "timeout"  # 请求超时
    NETWORK = "network"  # 网络错误
    SERVER_ERROR = "server_error"  # 服务器错误（5xx）
    CONNECTION = "connection"  # 连接错误

    # === 其他 ===
    EXCEPTION = "exception"  # 未分类异常
    UNKNOWN = "unknown"  # 未知错误
    LEASE_LOST = "lease_lost"  # 租约丢失
    UNKNOWN_JOB_TYPE = "unknown_job_type"  # 未知任务类型
    LOCK_HELD = "lock_held"  # 外部资源锁被其他进程持有（可安全让出）
    CONTRACT_ERROR = "contract_error"  # 同步结果不符合契约


# 永久性错误分类集合（不应重试，直接 mark_dead）
PERMANENT_ERROR_CATEGORIES = {
    ErrorCategory.AUTH_ERROR.value,
    ErrorCategory.AUTH_MISSING.value,
    ErrorCategory.AUTH_INVALID.value,
    ErrorCategory.REPO_NOT_FOUND.value,
    ErrorCategory.REPO_TYPE_UNKNOWN.value,
    ErrorCategory.PERMISSION_DENIED.value,
}

# 临时性错误分类集合
TRANSIENT_ERROR_CATEGORIES = {
    ErrorCategory.RATE_LIMIT.value,
    ErrorCategory.TIMEOUT.value,
    ErrorCategory.NETWORK.value,
    ErrorCategory.SERVER_ERROR.value,
    ErrorCategory.CONNECTION.value,
    ErrorCategory.LEASE_LOST.value,  # 租约丢失也是临时性错误，应该重试
}

# 忽略类别（非错误）分类集合
# 这些类别不计入失败重试预算，表示正常的让出/跳过行为
IGNORED_ERROR_CATEGORIES = {
    ErrorCategory.LOCK_HELD.value,  # 锁被其他进程持有，安全让出并使用 jitter 重入队
}

# 临时性错误关键词匹配（用于从错误消息推断分类）
TRANSIENT_ERROR_KEYWORDS = [
    "429",
    "rate limit",
    "too many requests",
    "timeout",
    "timed out",
    "connection",
    "connect",
    "network",
    "502",
    "503",
    "504",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "temporary",
    "retry",
]

# 不同错误类型的退避时间配置（秒）
TRANSIENT_ERROR_BACKOFF = {
    ErrorCategory.RATE_LIMIT.value: 120,  # 速率限制：2 分钟
    ErrorCategory.TIMEOUT.value: 30,  # 超时：30 秒
    ErrorCategory.NETWORK.value: 60,  # 网络错误：1 分钟
    ErrorCategory.SERVER_ERROR.value: 90,  # 服务器错误：1.5 分钟
    ErrorCategory.CONNECTION.value: 45,  # 连接错误：45 秒
    ErrorCategory.LEASE_LOST.value: 0,  # 租约丢失：立即重试（任务可能被抢占）
    ErrorCategory.LOCK_HELD.value: 0,  # 锁被持有：使用 jitter 重入队（不计入失败次数）
    "default": 60,  # 默认：1 分钟
}

# lock_held 场景的抖动配置（秒）
LOCK_HELD_JITTER_MIN = 5  # 最小抖动秒数
LOCK_HELD_JITTER_MAX = 30  # 最大抖动秒数

# HeartbeatManager 续租失败阈值常量
DEFAULT_MAX_RENEW_FAILURES = 3  # 默认最大续租失败次数

# 统一的退避计算参数
DEFAULT_BACKOFF_BASE = 60  # 基础退避时间（秒）
DEFAULT_MAX_BACKOFF = 3600  # 默认最大退避时间（秒）= 1 小时


# Backoff 来源枚举（用于记录最终采用的 backoff 来源）
class BackoffSource(str, Enum):
    """Backoff 来源枚举"""

    RETRY_AFTER = "retry_after"  # 使用 result.retry_after（最高优先级）
    ERROR_CATEGORY = "error_category"  # 使用错误分类对应的 backoff
    DEFAULT = "default"  # 使用默认 backoff


def resolve_backoff(
    retry_after: Optional[int] = None,
    error_category: Optional[str] = None,
    error_message: Optional[str] = None,
    default_backoff: int = DEFAULT_BACKOFF_BASE,
) -> Tuple[int, str]:
    """
    解析最终的 backoff 时间，优先级：retry_after > 分类 backoff > 默认 backoff

    这是 worker 和 run_contract 的统一入口，确保 backoff 计算逻辑一致。

    Args:
        retry_after: result 中的 retry_after 值（最高优先级）
        error_category: 错误分类
        error_message: 错误消息（用于推断分类）
        default_backoff: 默认 backoff 时间（秒）

    Returns:
        (backoff_seconds, backoff_source) 元组
        - backoff_seconds: 最终的 backoff 时间（秒）
        - backoff_source: backoff 来源（retry_after/error_category/default）

    Example:
        >>> backoff, source = resolve_backoff(retry_after=120)
        >>> backoff
        120
        >>> source
        'retry_after'

        >>> backoff, source = resolve_backoff(error_category='rate_limit')
        >>> source
        'error_category'
    """
    # 优先级 1: result.retry_after（服务端明确指定）
    if retry_after is not None and retry_after > 0:
        return int(retry_after), BackoffSource.RETRY_AFTER.value

    # 优先级 2: 错误分类对应的 backoff
    if error_category or error_message:
        category_backoff = get_transient_error_backoff(error_category, error_message or "")
        # 检查是否真的有分类 backoff（非默认值）
        if error_category and error_category.lower() in TRANSIENT_ERROR_BACKOFF:
            return category_backoff, BackoffSource.ERROR_CATEGORY.value
        # 从错误消息推断的分类也算 error_category 来源
        if error_message and category_backoff != TRANSIENT_ERROR_BACKOFF["default"]:
            return category_backoff, BackoffSource.ERROR_CATEGORY.value

    # 优先级 3: 默认 backoff
    return default_backoff, BackoffSource.DEFAULT.value


def calculate_backoff_seconds(
    attempts: int,
    base_seconds: int = DEFAULT_BACKOFF_BASE,
    max_seconds: int = DEFAULT_MAX_BACKOFF,
    error_category: Optional[str] = None,
    error_message: Optional[str] = None,
) -> int:
    """
    统一的退避时间计算函数（供 queue/reaper 复用）。

    退避策略：
    1. 如果有错误类别，优先使用该类别的退避时间作为基础
    2. 然后应用指数退避：base * 2^(attempts-1)
    3. 结果不超过 max_seconds

    Args:
        attempts: 当前尝试次数（从 1 开始）
        base_seconds: 基础退避时间（秒）
        max_seconds: 最大退避时间（秒）
        error_category: 可选，错误分类标签
        error_message: 可选，错误消息（用于推断分类）

    Returns:
        退避秒数
    """
    # 确定基础退避时间
    if error_category or error_message:
        category_backoff = get_transient_error_backoff(error_category, error_message or "")
        effective_base = category_backoff
    else:
        effective_base = base_seconds

    # 计算指数退避：base * 2^(attempts-1)，最小为 attempts=1
    effective_attempts = max(1, attempts)
    backoff = effective_base * (2 ** (effective_attempts - 1))

    # 限制在 max_seconds 范围内
    return min(backoff, max_seconds)


def is_transient_error(error_category: Optional[str], error_message: str) -> bool:
    """
    判断是否为临时性外部错误。

    临时性错误包括：
    - 429 速率限制
    - 请求超时
    - 网络错误
    - 服务器错误（5xx）

    Args:
        error_category: 错误分类标签
        error_message: 错误消息

    Returns:
        True 表示是临时性错误
    """
    # 检查错误分类
    if error_category and error_category.lower() in TRANSIENT_ERROR_CATEGORIES:
        return True

    # 检查错误消息中的关键词
    error_lower = (error_message or "").lower()
    for keyword in TRANSIENT_ERROR_KEYWORDS:
        if keyword in error_lower:
            return True

    return False


def is_permanent_error(error_category: Optional[str]) -> bool:
    """
    判断是否为永久性错误（不应重试）。

    永久性错误包括：
    - auth_error: 认证错误
    - auth_missing: 认证凭证缺失
    - auth_invalid: 认证凭证无效
    - repo_not_found: 仓库不存在
    - repo_type_unknown: 仓库类型未知
    - permission_denied: 权限不足

    Args:
        error_category: 错误分类标签

    Returns:
        True 表示是永久性错误
    """
    if error_category and error_category.lower() in PERMANENT_ERROR_CATEGORIES:
        return True
    return False


def is_ignored_category(error_category: Optional[str]) -> bool:
    """
    判断是否为忽略类别（非错误）。

    忽略类别不计入失败重试预算，表示正常的让出/跳过行为：
    - lock_held: 锁被其他进程持有，安全让出并使用 jitter 重入队

    Args:
        error_category: 错误分类标签

    Returns:
        True 表示是忽略类别（不应计入失败预算）
    """
    if error_category and error_category.lower() in IGNORED_ERROR_CATEGORIES:
        return True
    return False


def get_transient_error_backoff(error_category: Optional[str], error_message: str) -> int:
    """
    根据临时性错误类型获取退避时间。

    Args:
        error_category: 错误分类标签
        error_message: 错误消息

    Returns:
        退避秒数
    """
    # 优先使用错误分类
    if error_category and error_category.lower() in TRANSIENT_ERROR_BACKOFF:
        return TRANSIENT_ERROR_BACKOFF[error_category.lower()]

    # 根据错误消息推断类型
    error_lower = (error_message or "").lower()

    if "429" in error_lower or "rate limit" in error_lower or "too many requests" in error_lower:
        return TRANSIENT_ERROR_BACKOFF[ErrorCategory.RATE_LIMIT.value]

    if "timeout" in error_lower or "timed out" in error_lower:
        return TRANSIENT_ERROR_BACKOFF[ErrorCategory.TIMEOUT.value]

    if "502" in error_lower or "503" in error_lower or "504" in error_lower:
        return TRANSIENT_ERROR_BACKOFF[ErrorCategory.SERVER_ERROR.value]

    if "connection" in error_lower or "network" in error_lower:
        return TRANSIENT_ERROR_BACKOFF[ErrorCategory.NETWORK.value]

    return TRANSIENT_ERROR_BACKOFF["default"]


def classify_exception(exc: Exception) -> Tuple[str, str]:
    """
    对异常进行分类，返回 (error_category, error_message)。

    根据异常类型和消息推断错误分类：
    - ConnectionError/TimeoutError 等内置异常
    - requests.exceptions 中的异常
    - HTTP 状态码相关异常

    Args:
        exc: 异常对象

    Returns:
        (error_category, error_message) 元组
    """
    error_message = str(exc)
    error_lower = error_message.lower()
    exc_type = type(exc).__name__

    # 1. 根据异常类型分类
    if isinstance(exc, TimeoutError) or "Timeout" in exc_type:
        return ErrorCategory.TIMEOUT.value, error_message

    if isinstance(exc, ConnectionError) or "Connection" in exc_type:
        return ErrorCategory.CONNECTION.value, error_message

    # 2. 根据错误消息中的 HTTP 状态码分类
    if "401" in error_lower or "unauthorized" in error_lower:
        return ErrorCategory.AUTH_ERROR.value, error_message

    if "403" in error_lower or "forbidden" in error_lower:
        return ErrorCategory.PERMISSION_DENIED.value, error_message

    if "404" in error_lower or "not found" in error_lower:
        return ErrorCategory.REPO_NOT_FOUND.value, error_message

    if "429" in error_lower or "rate limit" in error_lower or "too many requests" in error_lower:
        return ErrorCategory.RATE_LIMIT.value, error_message

    if "502" in error_lower or "503" in error_lower or "504" in error_lower:
        return ErrorCategory.SERVER_ERROR.value, error_message

    if "timeout" in error_lower or "timed out" in error_lower:
        return ErrorCategory.TIMEOUT.value, error_message

    if "connection" in error_lower or "network" in error_lower:
        return ErrorCategory.NETWORK.value, error_message

    # 3. 默认返回 exception 分类
    return ErrorCategory.EXCEPTION.value, error_message


def classify_last_error(last_error: Optional[str]) -> Tuple[bool, bool, str]:
    """
    对 last_error 进行轻量分类（供 reaper 使用）。

    Args:
        last_error: job 的 last_error 字符串

    Returns:
        (is_permanent, is_transient, error_category)
        - is_permanent: 是否为永久性错误
        - is_transient: 是否为临时性错误
        - error_category: 错误分类标签（用于获取 backoff 时间）
    """
    if not last_error:
        return False, False, ""

    error_lower = last_error.lower()

    # 检查永久性错误（使用 PERMANENT_ERROR_CATEGORIES）
    for category in PERMANENT_ERROR_CATEGORIES:
        if category in error_lower:
            return True, False, category

    # 额外检查一些永久性错误关键词
    permanent_keywords = [
        "401",
        "403",
        "404",
        "authentication failed",
        "unauthorized",
        "forbidden",
        "not found",
        "does not exist",
        "invalid token",
        "token expired",
    ]
    for keyword in permanent_keywords:
        if keyword in error_lower:
            # 更细致分类
            if (
                "401" in error_lower
                or "unauthorized" in error_lower
                or "authentication" in error_lower
                or "token" in error_lower
            ):
                return True, False, ErrorCategory.AUTH_ERROR.value
            if "403" in error_lower or "forbidden" in error_lower or "permission" in error_lower:
                return True, False, ErrorCategory.PERMISSION_DENIED.value
            if (
                "404" in error_lower
                or "not found" in error_lower
                or "does not exist" in error_lower
            ):
                return True, False, ErrorCategory.REPO_NOT_FOUND.value

    # 检查临时性错误
    if is_transient_error("", last_error):
        # 关键词匹配推断分类（优先级更高，避免被通用分类覆盖）
        if (
            "429" in error_lower
            or "rate limit" in error_lower
            or "too many requests" in error_lower
        ):
            return False, True, ErrorCategory.RATE_LIMIT.value
        if "timeout" in error_lower or "timed out" in error_lower:
            return False, True, ErrorCategory.TIMEOUT.value
        if "502" in error_lower or "503" in error_lower or "504" in error_lower:
            return False, True, ErrorCategory.SERVER_ERROR.value
        if "connection" in error_lower or "network" in error_lower:
            return False, True, ErrorCategory.NETWORK.value
        # 尝试匹配 TRANSIENT_ERROR_CATEGORIES 中的分类
        for category in TRANSIENT_ERROR_CATEGORIES:
            if category in error_lower:
                return False, True, category
        return False, True, "default"

    # 未知错误类型
    return False, False, ""


def last_error_text(exc: Optional[Exception], max_length: int = 1000) -> str:
    """
    从异常对象获取安全的错误文本（用于存储到数据库）。

    Args:
        exc: 异常对象（可为 None）
        max_length: 最大文本长度

    Returns:
        安全的错误文本（已截断）
    """
    if exc is None:
        return ""

    error_text = str(exc)

    # 截断过长的错误消息
    if len(error_text) > max_length:
        error_text = error_text[: max_length - 3] + "..."

    return error_text
