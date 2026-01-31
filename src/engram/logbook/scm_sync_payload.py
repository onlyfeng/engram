# -*- coding: utf-8 -*-
"""
engram_logbook.scm_sync_payload - SCM 同步任务 Payload 契约定义

定义 sync_jobs.payload_json 字段的统一契约：
- PAYLOAD_SPEC_VERSION: payload 契约版本号
- PAYLOAD_FIELD_SPEC: 字段规范文档（字段名、含义、类型、是否必填、示例）
- MIN_REQUIRED_FIELDS: 每种 PhysicalJobType 的最小必需字段集合
- UNKNOWN_FIELD_PASSTHROUGH_RULES: 未知字段透传规则
- JobPayloadVersion: payload 版本枚举
- SyncJobPayloadV1: V1 版本的 payload 数据类
- parse_payload: 解析 payload 字典，兼容旧字段
- validate_payload: 验证 payload 合法性

设计原则:
1. 版本化设计，支持平滑升级
2. 向后兼容：新版本能解析旧格式的 payload
3. 字段名使用 snake_case
4. 所有字段都有合理的默认值
5. 关键字段做类型与范围校验
6. 未知字段透传保留，便于向前兼容

使用示例:
    # 解析 payload
    payload = parse_payload(job.payload)

    # 访问字段
    instance = payload.gitlab_instance
    since_ts = payload.since_ts

    # 序列化
    payload_dict = payload.to_json_dict()

    # 查看字段规范
    from engram.logbook.scm_sync_payload import PAYLOAD_FIELD_SPEC
    print(PAYLOAD_FIELD_SPEC["gitlab_instance"])
"""

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Union

__all__ = [
    # 契约规范常量
    "PAYLOAD_SPEC_VERSION",
    "PAYLOAD_FIELD_SPEC",
    "MIN_REQUIRED_FIELDS",
    "BACKFILL_RECOMMENDED_FIELDS",
    "INCREMENTAL_RECOMMENDED_FIELDS",
    "UNKNOWN_FIELD_PASSTHROUGH_RULES",
    "PhysicalJobType",
    # 枚举与数据类
    "JobPayloadVersion",
    "SyncJobPayloadV1",
    "DiffMode",
    "SyncMode",
    "WindowType",
    # 解析与验证函数
    "parse_payload",
    "validate_payload",
    "PayloadValidationError",
    # === 运行时 payload（供 worker/queue 使用）===
    "SyncJobPayload",
    "PayloadParseError",
    "parse_payload_runtime",
]


# =============================================================================
# Payload 契约规范版本
# =============================================================================

PAYLOAD_SPEC_VERSION = "v1.0.0"
"""
Payload 契约规范版本号

版本历史:
- v1.0.0 (2024-01): 初始版本，定义基础字段集合
"""


# =============================================================================
# PhysicalJobType 枚举
# =============================================================================


class PhysicalJobType(str, Enum):
    """
    物理任务类型枚举

    对应 sync_jobs.job_type 列的有效值。
    """

    SVN = "svn"
    GITLAB_COMMITS = "gitlab_commits"
    GITLAB_MRS = "gitlab_mrs"


# =============================================================================
# Payload 字段规范文档
# =============================================================================

PAYLOAD_FIELD_SPEC: Dict[str, Dict[str, Any]] = {
    # === Pool 过滤字段（claim 时 SQL 直接使用）===
    "gitlab_instance": {
        "meaning": "GitLab 实例标识（规范化后的 host，如 gitlab.example.com）",
        "type": "str",
        "required": False,
        "claim_filter": True,  # claim SQL 使用 payload_json ->> 'gitlab_instance' 过滤
        "example": "gitlab.example.com",
        "notes": "scheduler 写入时需调用 normalize_instance_key(url) 规范化",
    },
    "tenant_id": {
        "meaning": "租户 ID（用于多租户隔离）",
        "type": "str",
        "required": False,
        "claim_filter": True,  # claim SQL 使用 payload_json ->> 'tenant_id' 过滤
        "example": "tenant-acme",
    },
    # === 认证字段 ===
    "token": {
        "meaning": "可选的 payload 内联认证 token（覆盖 secrets 中的 token）",
        "type": "str",
        "required": False,
        "example": None,  # 不应在文档中出现真实 token
        "notes": "敏感字段，存储时应考虑加密或脱敏",
    },
    # === 执行参数 ===
    "batch_size": {
        "meaning": "每次 API 请求的批量大小（如 per_page）",
        "type": "int",
        "required": False,
        "example": 100,
        "notes": "若 suggested_batch_size 存在，优先使用 suggested_batch_size",
    },
    "suggested_batch_size": {
        "meaning": "熔断降级建议的 batch_size（scheduler 注入）",
        "type": "int",
        "required": False,
        "example": 50,
        "notes": "熔断器处于 half_open/degraded 状态时由 scheduler 建议",
    },
    "suggested_forward_window_seconds": {
        "meaning": "熔断降级建议的前向时间窗口秒数（scheduler 注入）",
        "type": "int",
        "required": False,
        "example": 3600,
    },
    "suggested_diff_mode": {
        "meaning": "熔断降级建议的 diff 获取模式（scheduler 注入）",
        "type": "str",
        "required": False,
        "example": "best_effort",
        "valid_values": ["always", "best_effort", "minimal", "none"],
        "notes": "minimal 表示仅获取 ministat，用于熔断降级场景",
    },
    "diff_mode": {
        "meaning": "Diff 获取模式",
        "type": "str",
        "required": False,
        "example": "best_effort",
        "valid_values": ["always", "best_effort", "minimal", "none"],
        "notes": "minimal 表示仅获取 ministat，不获取完整 diff 内容",
    },
    # === 熔断降级参数 ===
    "is_backfill_only": {
        "meaning": "是否仅执行 backfill（熔断降级模式，不做增量同步）",
        "type": "bool",
        "required": False,
        "default": False,
        "example": True,
    },
    "circuit_state": {
        "meaning": "熔断器状态（scheduler 注入）",
        "type": "str",
        "required": False,
        "example": "half_open",
        "valid_values": ["closed", "half_open", "open", "degraded"],
    },
    "is_probe_mode": {
        "meaning": "是否为探测模式（熔断器 half_open 状态的探测任务）",
        "type": "bool",
        "required": False,
        "default": False,
        "example": True,
    },
    "probe_budget": {
        "meaning": "探测预算（探测模式下的最大处理条数）",
        "type": "int",
        "required": False,
        "example": 10,
    },
    # === Backfill 时间窗口参数（Git/MR/Review）===
    "since": {
        "meaning": "backfill 开始时间（Unix timestamp 或 ISO8601 字符串）",
        "type": "Union[str, int, float]",
        "required": False,
        "example": "2024-01-01T00:00:00Z",
        "notes": "worker 侧需兼容数字和字符串格式",
    },
    "until": {
        "meaning": "backfill 结束时间（Unix timestamp 或 ISO8601 字符串）",
        "type": "Union[str, int, float]",
        "required": False,
        "example": 1704067200,
    },
    "update_watermark": {
        "meaning": "backfill 完成后是否更新 watermark（游标）",
        "type": "bool",
        "required": False,
        "default": False,
        "example": True,
    },
    # === 分块参数（TimeWindowChunk.to_payload / RevisionWindowChunk.to_payload）===
    "window_type": {
        "meaning": "窗口类型（time = 时间窗口，revision = SVN revision 窗口）",
        "type": "str",
        "required": False,
        "example": "time",
        "valid_values": ["time", "revision"],
    },
    "window_since": {
        "meaning": "分块时间窗口开始（TimeWindowChunk 生成）",
        "type": "str",
        "required": False,
        "example": "2024-01-01T00:00:00+00:00",
    },
    "window_until": {
        "meaning": "分块时间窗口结束（TimeWindowChunk 生成）",
        "type": "str",
        "required": False,
        "example": "2024-01-02T00:00:00+00:00",
    },
    "window_start_rev": {
        "meaning": "分块 revision 窗口开始（RevisionWindowChunk 生成）",
        "type": "int",
        "required": False,
        "example": 1000,
    },
    "window_end_rev": {
        "meaning": "分块 revision 窗口结束（RevisionWindowChunk 生成）",
        "type": "int",
        "required": False,
        "example": 1100,
    },
    "chunk_index": {
        "meaning": "当前分块索引（0-based）",
        "type": "int",
        "required": False,
        "example": 0,
    },
    "chunk_total": {
        "meaning": "总分块数",
        "type": "int",
        "required": False,
        "example": 5,
    },
    "watermark_constraint": {
        "meaning": "watermark 约束（分块任务的依赖条件）",
        "type": "str",
        "required": False,
        "example": "last_chunk_only",
    },
    # === SVN 专用参数 ===
    "start_rev": {
        "meaning": "SVN backfill 开始 revision",
        "type": "int",
        "required": False,
        "example": 1000,
        "job_types": [PhysicalJobType.SVN.value],
    },
    "end_rev": {
        "meaning": "SVN backfill 结束 revision",
        "type": "int",
        "required": False,
        "example": 1100,
        "job_types": [PhysicalJobType.SVN.value],
    },
    "fetch_patches": {
        "meaning": "SVN 是否获取 patch 内容",
        "type": "bool",
        "required": False,
        "example": True,
        "job_types": [PhysicalJobType.SVN.value],
    },
    "patch_path_filter": {
        "meaning": "SVN patch 路径过滤正则表达式",
        "type": "str",
        "required": False,
        "example": "^trunk/src/",
        "job_types": [PhysicalJobType.SVN.value],
    },
    # === MR 专用参数 ===
    "mr_state_filter": {
        "meaning": "MR 状态过滤（opened / merged / closed / all）",
        "type": "str",
        "required": False,
        "example": "merged",
        "valid_values": ["opened", "merged", "closed", "all"],
        "job_types": [PhysicalJobType.GITLAB_MRS.value],
    },
    "fetch_details": {
        "meaning": "MR 是否获取详情（changes/discussions）",
        "type": "bool",
        "required": False,
        "default": False,
        "example": True,
        "job_types": [PhysicalJobType.GITLAB_MRS.value],
    },
    # === 通用调试参数 ===
    "verbose": {
        "meaning": "是否输出详细日志",
        "type": "bool",
        "required": False,
        "default": False,
        "example": True,
    },
    "dry_run": {
        "meaning": "是否为试运行模式（不写入数据库）",
        "type": "bool",
        "required": False,
        "default": False,
        "example": True,
    },
    # === Scheduler 注入的调度元数据 ===
    "reason": {
        "meaning": "任务调度原因（scheduler 注入）",
        "type": "str",
        "required": False,
        "example": "incremental_due",
    },
    "scheduled_at": {
        "meaning": "调度时间戳（scheduler 注入）",
        "type": "str",
        "required": False,
        "example": "2024-01-15T10:00:00+00:00",
    },
    "logical_job_type": {
        "meaning": "逻辑任务类型（scheduler 注入）",
        "type": "str",
        "required": False,
        "example": "commits",
    },
    "physical_job_type": {
        "meaning": "物理任务类型（scheduler 注入，与 job_type 一致）",
        "type": "str",
        "required": False,
        "example": "gitlab_commits",
    },
    # === Bucket 惩罚信息（scheduler 注入）===
    "bucket_paused": {
        "meaning": "bucket 是否暂停",
        "type": "bool",
        "required": False,
        "example": False,
    },
    "bucket_pause_remaining_seconds": {
        "meaning": "bucket 暂停剩余秒数",
        "type": "int",
        "required": False,
        "example": 120,
    },
    "bucket_penalty_reason": {
        "meaning": "bucket 惩罚原因",
        "type": "str",
        "required": False,
        "example": "rate_limit",
    },
    "bucket_penalty_value": {
        "meaning": "bucket 惩罚值",
        "type": "int",
        "required": False,
        "example": 300,
    },
    # === 游标相关 ===
    "cursor_age_seconds": {
        "meaning": "游标年龄秒数（scheduler 注入，用于调度决策）",
        "type": "int",
        "required": False,
        "example": 3600,
    },
    "failure_rate": {
        "meaning": "失败率（scheduler 注入）",
        "type": "float",
        "required": False,
        "example": 0.1,
    },
    "rate_limit_rate": {
        "meaning": "速率限制率（scheduler 注入）",
        "type": "float",
        "required": False,
        "example": 0.05,
    },
    # === 预算快照（scheduler 注入，用于排障和可观测性）===
    "budget_snapshot": {
        "meaning": "调度时的并发预算快照",
        "type": "dict",
        "required": False,
        "example": {
            "global_running": 5,
            "global_pending": 10,
            "global_active": 15,
            "by_instance": {"gitlab.example.com": 3},
            "by_tenant": {"tenant-a": 2},
        },
        "notes": "包含 global_running, global_pending, global_active, by_instance, by_tenant 字段",
    },
    # === 熔断决策（scheduler 注入，用于排障和可观测性）===
    "circuit_breaker_decision": {
        "meaning": "调度时的熔断决策快照",
        "type": "dict",
        "required": False,
        "example": {
            "current_state": "closed",
            "allow_sync": True,
            "trigger_reason": None,
            "suggested_batch_size": 100,
        },
        "notes": "包含 current_state, allow_sync, trigger_reason, suggested_* 等字段",
    },
    # === 旧字段兼容 ===
    "page": {
        "meaning": "分页页码（旧字段，已废弃）",
        "type": "int",
        "required": False,
        "deprecated": True,
        "example": 1,
    },
}


# =============================================================================
# 每种 PhysicalJobType 的最小必需字段集合
# =============================================================================

MIN_REQUIRED_FIELDS: Dict[str, List[str]] = {
    # SVN 同步：无严格必需字段，但 backfill 需要 start_rev/end_rev
    PhysicalJobType.SVN.value: [],
    # GitLab Commits 同步：gitlab_instance 用于 claim 过滤
    PhysicalJobType.GITLAB_COMMITS.value: [],
    # GitLab MRs 同步
    PhysicalJobType.GITLAB_MRS.value: [],
}
"""
每种 PhysicalJobType 的最小必需字段集合

注意：实际上所有字段都是可选的（有默认值），
这里列出的是推荐在特定场景下提供的字段：

Backfill 场景推荐字段：
- SVN: start_rev, end_rev, update_watermark
- GitLab: since, until, update_watermark

Incremental 场景推荐字段：
- 所有类型: gitlab_instance（用于 claim 过滤）
"""

# Backfill 场景推荐字段
BACKFILL_RECOMMENDED_FIELDS: Dict[str, List[str]] = {
    PhysicalJobType.SVN.value: ["start_rev", "end_rev", "update_watermark"],
    PhysicalJobType.GITLAB_COMMITS.value: ["since", "until", "update_watermark", "gitlab_instance"],
    PhysicalJobType.GITLAB_MRS.value: ["since", "until", "update_watermark", "gitlab_instance"],
}

# Incremental 场景推荐字段
INCREMENTAL_RECOMMENDED_FIELDS: Dict[str, List[str]] = {
    PhysicalJobType.SVN.value: [],
    PhysicalJobType.GITLAB_COMMITS.value: ["gitlab_instance", "tenant_id"],
    PhysicalJobType.GITLAB_MRS.value: ["gitlab_instance", "tenant_id"],
}


# =============================================================================
# 未知字段透传规则
# =============================================================================

UNKNOWN_FIELD_PASSTHROUGH_RULES = """
未知字段透传规则 (Unknown Field Passthrough Rules)
===================================================

1. 写入路径 (Enqueue)
   - scheduler 构建 payload dict，写入 sync_jobs.payload_json
   - 任何字段（包括未在 PAYLOAD_FIELD_SPEC 中定义的）都会被 JSON 序列化保存
   - 示例：scheduler 注入 {"custom_metric": 123} 会被完整保存

2. 读取路径 (Claim -> Worker)
   - claim() 从数据库读取 payload_json，调用 parse_payload() 解析
   - parse_payload() 使用 SyncJobPayload.from_dict()：
     - 已知字段 -> 映射到 dataclass 属性
     - 未知字段 -> 保留在 payload.extra dict 中
   - Worker 通过 payload.extra.get("custom_field") 访问未知字段

3. 序列化路径 (to_dict)
   - SyncJobPayload.to_dict() 将 extra 合并回顶层
   - 保证往返（round-trip）不丢失数据

4. 兼容性保证
   - 向前兼容：新版本 scheduler 写入的新字段，旧版本 worker 可忽略（存在 extra）
   - 向后兼容：新版本 worker 解析旧 payload，缺失字段使用默认值

5. 使用示例
   ```python
   # Worker 访问未知字段
   payload = job["payload"]  # SyncJobPayload
   custom_value = payload.extra.get("custom_field", default_value)

   # 或使用 dict-like API（向后兼容）
   custom_value = payload.get("custom_field", default_value)
   ```

6. 注意事项
   - 不要假设 extra 中的字段类型，总是做防御性检查
   - 新增字段应先在 PAYLOAD_FIELD_SPEC 中文档化
   - claim SQL 过滤只支持 gitlab_instance/tenant_id，新增过滤字段需修改 SQL
"""


# ============ 枚举定义 ============


class JobPayloadVersion(str, Enum):
    """
    Payload 版本枚举

    用于标识 payload 的版本，支持平滑升级。
    """

    V1 = "v1"
    # 未来版本预留
    # V2 = "v2"


class DiffMode(str, Enum):
    """
    Diff 获取模式

    - ALWAYS: 总是获取 diff（失败则报错）
    - BEST_EFFORT: 尽力获取 diff（失败则降级）
    - MINIMAL: 仅获取 ministat（不获取完整 diff 内容，用于熔断降级）
    - NONE: 不获取 diff（仅同步元数据）
    """

    ALWAYS = "always"
    BEST_EFFORT = "best_effort"
    MINIMAL = "minimal"
    NONE = "none"


class SyncMode(str, Enum):
    """
    同步模式

    - INCREMENTAL: 增量同步（从游标位置继续）
    - BACKFILL: 回填同步（指定时间/revision 窗口）
    - PROBE: 熔断器探测模式（half_open 状态下的受限同步）
    """

    INCREMENTAL = "incremental"
    BACKFILL = "backfill"
    PROBE = "probe"


class WindowType(str, Enum):
    """
    窗口类型

    - TIME: 时间窗口（用于 Git/MR/Review）
    - REV: Revision 窗口（用于 SVN）

    注意：Schema 中还允许 "revision" 作为 "rev" 的别名（用于向后兼容），
    parse_payload 会自动将 "revision" 规范化为 "rev"。
    """

    TIME = "time"
    REV = "rev"

    @classmethod
    def normalize(cls, value: str) -> str:
        """
        规范化 window_type 值，将 "revision" 映射为 "rev"。

        Args:
            value: 原始 window_type 值

        Returns:
            规范化后的值
        """
        if value and value.lower() == "revision":
            return cls.REV.value
        return value


# ============ 异常定义 ============


class PayloadValidationError(ValueError):
    """
    Payload 验证错误

    当 payload 字段不符合契约时抛出。
    """

    def __init__(self, message: str, errors: Optional[List[str]] = None):
        super().__init__(message)
        self.errors = errors or []


# ============ 常量定义 ============


# 时间戳范围限制（合理的边界，防止异常值）
MIN_TIMESTAMP = 0  # 1970-01-01
MAX_TIMESTAMP = 4102444800  # 2100-01-01

# Revision 范围限制
MIN_REVISION = 0
MAX_REVISION = 2**31 - 1  # 避免溢出

# batch_size 范围
MIN_BATCH_SIZE = 1
MAX_BATCH_SIZE = 10000

# forward_window_seconds 范围
MIN_FORWARD_WINDOW_SECONDS = 60  # 1 分钟
MAX_FORWARD_WINDOW_SECONDS = 86400 * 30  # 30 天

# chunk 范围
MIN_CHUNK_SIZE = 1
MAX_CHUNK_SIZE = 100000
MAX_TOTAL_CHUNKS = 10000

# ISO8601 时间戳正则（支持多种格式）
ISO8601_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}"  # 日期部分
    r"(?:[T ]\d{2}:\d{2}:\d{2}"  # 时间部分（可选）
    r"(?:\.\d+)?"  # 毫秒（可选）
    r"(?:Z|[+-]\d{2}:?\d{2})?)?$"  # 时区（可选）
)


# ============ 辅助函数 ============


def _parse_timestamp(value: Any) -> Optional[float]:
    """
    解析时间戳（支持多种格式）

    支持的格式：
    - Unix timestamp（int/float）
    - ISO8601 字符串
    - datetime 对象

    Args:
        value: 待解析的值

    Returns:
        Unix timestamp（float），无效值返回 None
    """
    if value is None:
        return None

    # 已经是数字类型
    if isinstance(value, (int, float)):
        return float(value)

    # datetime 对象
    if isinstance(value, datetime):
        return value.timestamp()

    # 字符串类型
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None

        # 尝试解析为数字
        try:
            return float(value)
        except ValueError:
            pass

        # 尝试解析为 ISO8601
        try:
            # 简单格式：2024-01-01
            if len(value) == 10 and "-" in value:
                dt = datetime.strptime(value, "%Y-%m-%d")
                return dt.replace(tzinfo=timezone.utc).timestamp()

            # 带时间：2024-01-01T12:00:00
            if "T" in value or " " in value:
                # 移除毫秒和时区，简化解析
                clean_value = (
                    value.replace("T", " ").split(".")[0].split("+")[0].split("Z")[0].strip()
                )
                if len(clean_value) == 19:  # 2024-01-01 12:00:00
                    dt = datetime.strptime(clean_value, "%Y-%m-%d %H:%M:%S")
                    return dt.replace(tzinfo=timezone.utc).timestamp()
        except (ValueError, AttributeError):
            pass

    return None


def _validate_timestamp(value: Optional[float], field_name: str, errors: List[str]) -> None:
    """验证时间戳范围"""
    if value is None:
        return

    if not isinstance(value, (int, float)):
        errors.append(f"{field_name} 类型错误: 期望数字，实际 {type(value).__name__}")
        return

    if value < MIN_TIMESTAMP:
        errors.append(f"{field_name} 值过小: {value} < {MIN_TIMESTAMP}")

    if value > MAX_TIMESTAMP:
        errors.append(f"{field_name} 值过大: {value} > {MAX_TIMESTAMP}")


def _validate_revision(value: Optional[int], field_name: str, errors: List[str]) -> None:
    """验证 revision 范围"""
    if value is None:
        return

    if not isinstance(value, int):
        errors.append(f"{field_name} 类型错误: 期望 int，实际 {type(value).__name__}")
        return

    if value < MIN_REVISION:
        errors.append(f"{field_name} 值过小: {value} < {MIN_REVISION}")

    if value > MAX_REVISION:
        errors.append(f"{field_name} 值过大: {value} > {MAX_REVISION}")


def _validate_enum(
    value: Optional[str], field_name: str, valid_values: List[str], errors: List[str]
) -> None:
    """验证枚举值"""
    if value is None:
        return

    if not isinstance(value, str):
        errors.append(f"{field_name} 类型错误: 期望 str，实际 {type(value).__name__}")
        return

    if value.lower() not in [v.lower() for v in valid_values]:
        errors.append(f"{field_name} 值无效: '{value}'，有效值: {valid_values}")


def _validate_int_range(
    value: Optional[int], field_name: str, min_val: int, max_val: int, errors: List[str]
) -> None:
    """验证整数范围"""
    if value is None:
        return

    if not isinstance(value, int):
        errors.append(f"{field_name} 类型错误: 期望 int，实际 {type(value).__name__}")
        return

    if value < min_val:
        errors.append(f"{field_name} 值过小: {value} < {min_val}")

    if value > max_val:
        errors.append(f"{field_name} 值过大: {value} > {max_val}")


# ============ 数据类定义 ============


@dataclass
class SyncJobPayloadV1:
    """
    V1 版本的同步任务 Payload

    包含任务执行所需的所有参数。

    字段分组:
    1. 版本标识
    2. Repo 定位信息
    3. 时间/Revision 窗口
    4. 同步控制参数
    5. 分块参数
    6. 扩展字段
    """

    # === 版本标识 ===
    version: str = JobPayloadVersion.V1.value

    # === Repo 定位信息 ===
    # GitLab 实例标识（规范化后的 host，如 gitlab.example.com）
    gitlab_instance: Optional[str] = None
    # 租户 ID（用于多租户隔离）
    tenant_id: Optional[str] = None
    # 项目 key（如 group/project）
    project_key: Optional[str] = None

    # === 时间窗口（Git/MR/Review 专用）===
    # 窗口类型
    window_type: str = WindowType.TIME.value
    # 开始时间戳（Unix timestamp）
    since_ts: Optional[float] = None
    # 结束时间戳（Unix timestamp）
    until_ts: Optional[float] = None

    # === Revision 窗口（SVN 专用）===
    # 开始 revision
    start_rev: Optional[int] = None
    # 结束 revision
    end_rev: Optional[int] = None

    # === 同步控制参数 ===
    # 同步模式
    mode: str = SyncMode.INCREMENTAL.value
    # Diff 获取模式
    diff_mode: str = DiffMode.BEST_EFFORT.value
    # 严格模式（true = 失败即停止）
    strict: bool = False
    # 是否更新 watermark（游标）
    update_watermark: bool = True

    # === 批量参数 ===
    # 批量大小
    batch_size: Optional[int] = None
    # 前向窗口秒数
    forward_window_seconds: Optional[int] = None

    # === 分块参数（大窗口切分）===
    # 分块大小（时间秒数或 revision 数量）
    chunk_size: Optional[int] = None
    # 总分块数
    total_chunks: int = 1
    # 当前分块索引（0-based）
    current_chunk: int = 0

    # === 扩展字段（用于向前兼容）===
    # 存储未识别的字段，避免丢失数据
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_json_dict(
        self, include_none: bool = False, include_extra: bool = True
    ) -> Dict[str, Any]:
        """
        转换为 JSON 可序列化的字典

        Args:
            include_none: 是否包含值为 None 的字段
            include_extra: 是否包含扩展字段

        Returns:
            payload 字典
        """
        result: Dict[str, Any] = {}

        # 版本
        result["version"] = self.version

        # Repo 定位信息
        if include_none or self.gitlab_instance is not None:
            result["gitlab_instance"] = self.gitlab_instance
        if include_none or self.tenant_id is not None:
            result["tenant_id"] = self.tenant_id
        if include_none or self.project_key is not None:
            result["project_key"] = self.project_key

        # 窗口参数
        result["window_type"] = self.window_type

        if self.window_type == WindowType.TIME.value:
            if include_none or self.since_ts is not None:
                result["since"] = self.since_ts
            if include_none or self.until_ts is not None:
                result["until"] = self.until_ts
        elif self.window_type == WindowType.REV.value:
            if include_none or self.start_rev is not None:
                result["start_rev"] = self.start_rev
            if include_none or self.end_rev is not None:
                result["end_rev"] = self.end_rev

        # 同步控制参数
        result["mode"] = self.mode
        result["diff_mode"] = self.diff_mode
        result["strict"] = self.strict
        result["update_watermark"] = self.update_watermark

        # 批量参数
        if include_none or self.batch_size is not None:
            result["batch_size"] = self.batch_size
        if include_none or self.forward_window_seconds is not None:
            result["forward_window_seconds"] = self.forward_window_seconds

        # 分块参数
        if include_none or self.chunk_size is not None:
            result["chunk_size"] = self.chunk_size
        result["total_chunks"] = self.total_chunks
        result["current_chunk"] = self.current_chunk

        # 扩展字段
        if include_extra and self.extra:
            result.update(self.extra)

        return result

    def validate(self) -> List[str]:
        """
        验证 payload 字段

        Returns:
            错误列表，空列表表示验证通过
        """
        errors: List[str] = []

        # 验证版本
        _validate_enum(self.version, "version", [v.value for v in JobPayloadVersion], errors)

        # 验证窗口类型
        _validate_enum(self.window_type, "window_type", [v.value for v in WindowType], errors)

        # 验证时间窗口
        if self.window_type == WindowType.TIME.value:
            _validate_timestamp(self.since_ts, "since_ts", errors)
            _validate_timestamp(self.until_ts, "until_ts", errors)

            # 验证窗口边界
            if self.since_ts is not None and self.until_ts is not None:
                if self.since_ts > self.until_ts:
                    errors.append(
                        f"时间窗口无效: since_ts({self.since_ts}) > until_ts({self.until_ts})"
                    )

        # 验证 revision 窗口
        if self.window_type == WindowType.REV.value:
            _validate_revision(self.start_rev, "start_rev", errors)
            _validate_revision(self.end_rev, "end_rev", errors)

            # 验证窗口边界
            if self.start_rev is not None and self.end_rev is not None:
                if self.start_rev > self.end_rev:
                    errors.append(
                        f"revision 窗口无效: start_rev({self.start_rev}) > end_rev({self.end_rev})"
                    )

        # 验证同步模式
        _validate_enum(self.mode, "mode", [v.value for v in SyncMode], errors)

        # 验证 diff 模式
        _validate_enum(self.diff_mode, "diff_mode", [v.value for v in DiffMode], errors)

        # 验证批量参数
        if self.batch_size is not None:
            _validate_int_range(
                self.batch_size, "batch_size", MIN_BATCH_SIZE, MAX_BATCH_SIZE, errors
            )

        if self.forward_window_seconds is not None:
            _validate_int_range(
                self.forward_window_seconds,
                "forward_window_seconds",
                MIN_FORWARD_WINDOW_SECONDS,
                MAX_FORWARD_WINDOW_SECONDS,
                errors,
            )

        # 验证分块参数
        if self.chunk_size is not None:
            _validate_int_range(
                self.chunk_size, "chunk_size", MIN_CHUNK_SIZE, MAX_CHUNK_SIZE, errors
            )

        _validate_int_range(self.total_chunks, "total_chunks", 1, MAX_TOTAL_CHUNKS, errors)

        _validate_int_range(self.current_chunk, "current_chunk", 0, MAX_TOTAL_CHUNKS - 1, errors)

        # 验证 current_chunk < total_chunks
        if self.current_chunk >= self.total_chunks:
            errors.append(
                f"current_chunk({self.current_chunk}) >= total_chunks({self.total_chunks})"
            )

        return errors

    def is_backfill(self) -> bool:
        """是否为回填模式"""
        return self.mode == SyncMode.BACKFILL.value

    def is_strict(self) -> bool:
        """是否为严格模式"""
        return self.strict

    def should_fetch_diff(self) -> bool:
        """是否应该获取 diff"""
        return self.diff_mode != DiffMode.NONE.value

    def get_time_window(self) -> tuple:
        """
        获取时间窗口

        Returns:
            (since_ts, until_ts) 元组
        """
        return (self.since_ts, self.until_ts)

    def get_rev_window(self) -> tuple:
        """
        获取 revision 窗口

        Returns:
            (start_rev, end_rev) 元组
        """
        return (self.start_rev, self.end_rev)


# ============ 解析函数 ============


def parse_payload(
    data: Optional[Dict[str, Any]],
    strict: bool = False,
) -> SyncJobPayloadV1:
    """
    解析 payload 字典为 SyncJobPayloadV1 对象

    支持兼容旧字段名：
    - "since" -> since_ts
    - "until" -> until_ts
    - "gitlab_host" -> gitlab_instance（旧字段名）

    Args:
        data: payload 字典（可以为 None 或空）
        strict: 是否在验证失败时抛出异常

    Returns:
        SyncJobPayloadV1 对象

    Raises:
        PayloadValidationError: strict=True 且验证失败时
    """
    if data is None:
        data = {}

    if not isinstance(data, dict):
        if strict:
            raise PayloadValidationError(f"payload 必须是 dict 类型，实际 {type(data).__name__}")
        return SyncJobPayloadV1()

    # 收集已知字段
    known_fields = set()

    # === 版本 ===
    version = data.get("version", JobPayloadVersion.V1.value)
    known_fields.add("version")

    # === Repo 定位信息 ===
    # 兼容旧字段名 gitlab_host -> gitlab_instance
    gitlab_instance = data.get("gitlab_instance") or data.get("gitlab_host")
    known_fields.update(["gitlab_instance", "gitlab_host"])

    tenant_id = data.get("tenant_id")
    known_fields.add("tenant_id")

    project_key = data.get("project_key")
    known_fields.add("project_key")

    # === 窗口类型 ===
    window_type_raw = data.get("window_type", WindowType.TIME.value)
    # 规范化 "revision" -> "rev"（Schema 兼容）
    window_type = (
        WindowType.normalize(window_type_raw) if window_type_raw else WindowType.TIME.value
    )
    known_fields.add("window_type")

    # === 时间窗口 ===
    # 兼容多种字段名：since/since_ts, until/until_ts
    since_raw = data.get("since") or data.get("since_ts")
    until_raw = data.get("until") or data.get("until_ts")
    known_fields.update(["since", "since_ts", "until", "until_ts"])

    since_ts = _parse_timestamp(since_raw)
    until_ts = _parse_timestamp(until_raw)

    # === Revision 窗口 ===
    start_rev = data.get("start_rev")
    end_rev = data.get("end_rev")
    known_fields.update(["start_rev", "end_rev"])

    if start_rev is not None and not isinstance(start_rev, int):
        try:
            start_rev = int(start_rev)
        except (ValueError, TypeError):
            start_rev = None

    if end_rev is not None and not isinstance(end_rev, int):
        try:
            end_rev = int(end_rev)
        except (ValueError, TypeError):
            end_rev = None

    # 自动推断 window_type
    if window_type == WindowType.TIME.value and (start_rev is not None or end_rev is not None):
        # 如果有 revision 字段，切换到 REV 模式
        if since_ts is None and until_ts is None:
            window_type = WindowType.REV.value

    # === 同步控制参数 ===
    mode = data.get("mode", SyncMode.INCREMENTAL.value)
    known_fields.add("mode")

    # 兼容 diff_mode 的不同写法
    diff_mode = data.get("diff_mode") or data.get("diffMode") or DiffMode.BEST_EFFORT.value
    known_fields.update(["diff_mode", "diffMode"])

    # 规范化 diff_mode
    if isinstance(diff_mode, str):
        diff_mode = diff_mode.lower()

    strict_flag = data.get("strict", False)
    known_fields.add("strict")
    if isinstance(strict_flag, str):
        strict_flag = strict_flag.lower() in ("true", "1", "yes")
    elif isinstance(strict_flag, (int, float)) and not isinstance(strict_flag, bool):
        strict_flag = bool(strict_flag)

    update_watermark = data.get("update_watermark", True)
    known_fields.add("update_watermark")
    if isinstance(update_watermark, str):
        update_watermark = update_watermark.lower() in ("true", "1", "yes")
    elif isinstance(update_watermark, (int, float)) and not isinstance(update_watermark, bool):
        update_watermark = bool(update_watermark)

    # === 批量参数 ===
    batch_size = data.get("batch_size")
    known_fields.add("batch_size")
    if batch_size is not None and not isinstance(batch_size, int):
        try:
            batch_size = int(batch_size)
        except (ValueError, TypeError):
            batch_size = None

    forward_window_seconds = data.get("forward_window_seconds")
    known_fields.add("forward_window_seconds")
    if forward_window_seconds is not None and not isinstance(forward_window_seconds, int):
        try:
            forward_window_seconds = int(forward_window_seconds)
        except (ValueError, TypeError):
            forward_window_seconds = None

    # === 分块参数 ===
    chunk_size = data.get("chunk_size")
    known_fields.add("chunk_size")
    if chunk_size is not None and not isinstance(chunk_size, int):
        try:
            chunk_size = int(chunk_size)
        except (ValueError, TypeError):
            chunk_size = None

    total_chunks = data.get("total_chunks", 1)
    known_fields.add("total_chunks")
    if not isinstance(total_chunks, int):
        try:
            total_chunks = int(total_chunks)
        except (ValueError, TypeError):
            total_chunks = 1

    current_chunk = data.get("current_chunk", 0)
    known_fields.add("current_chunk")
    if not isinstance(current_chunk, int):
        try:
            current_chunk = int(current_chunk)
        except (ValueError, TypeError):
            current_chunk = 0

    # === 收集扩展字段 ===
    extra = {k: v for k, v in data.items() if k not in known_fields}

    # 构建 payload 对象
    payload = SyncJobPayloadV1(
        version=version,
        gitlab_instance=gitlab_instance,
        tenant_id=tenant_id,
        project_key=project_key,
        window_type=window_type,
        since_ts=since_ts,
        until_ts=until_ts,
        start_rev=start_rev,
        end_rev=end_rev,
        mode=mode,
        diff_mode=diff_mode,
        strict=strict_flag,
        update_watermark=update_watermark,
        batch_size=batch_size,
        forward_window_seconds=forward_window_seconds,
        chunk_size=chunk_size,
        total_chunks=total_chunks,
        current_chunk=current_chunk,
        extra=extra,
    )

    # 验证
    if strict:
        errors = payload.validate()
        if errors:
            raise PayloadValidationError(f"Payload 验证失败: {'; '.join(errors)}", errors=errors)

    return payload


def validate_payload(
    data: Union[Dict[str, Any], SyncJobPayloadV1],
) -> tuple:
    """
    验证 payload 是否符合契约

    Args:
        data: payload 字典或 SyncJobPayloadV1 对象

    Returns:
        (is_valid, errors) 元组
        - is_valid: bool, 是否有效
        - errors: List[str], 错误列表
    """
    if isinstance(data, SyncJobPayloadV1):
        errors = data.validate()
    else:
        try:
            payload = parse_payload(data)
            errors = payload.validate()
        except PayloadValidationError as e:
            return (False, e.errors or [str(e)])

    return (len(errors) == 0, errors)


# =============================================================================
# SyncJobPayload - 运行时 payload 解析（简化版，向后兼容）
# =============================================================================


class PayloadParseError(Exception):
    """Payload 解析错误（契约不匹配）"""

    pass


@dataclass
class SyncJobPayload:
    """
    同步任务 payload 的类型化表示（运行时使用）。

    设计原则：
    - 所有字段都有默认值（向后兼容：旧任务可能缺少新字段）
    - 未知字段保留在 extra 中（向前兼容：新字段不破坏旧代码）
    - 从 dict 解析时验证基本类型约束

    字段说明：
    - gitlab_instance: GitLab 实例 key（如 gitlab.example.com）
    - tenant_id: 租户 ID
    - token: 可选的 payload 内联 token
    - batch_size: 批次大小
    - suggested_batch_size: 熔断降级建议的 batch_size
    - suggested_diff_mode: 熔断降级建议的 diff_mode
    - is_backfill_only: 是否仅 backfill（熔断降级模式）
    - circuit_state: 熔断状态
    - since/until: backfill 时间窗口
    - update_watermark: backfill 是否更新 watermark
    - verbose: 是否输出详细日志
    - fetch_details: 是否获取详情
    - extra: 未知字段（向前兼容）
    """

    # === Pool 过滤字段 ===
    gitlab_instance: Optional[str] = None
    tenant_id: Optional[str] = None

    # === 认证字段 ===
    token: Optional[str] = None

    # === 执行参数 ===
    batch_size: Optional[int] = None
    suggested_batch_size: Optional[int] = None
    suggested_forward_window_seconds: Optional[int] = None
    suggested_diff_mode: Optional[str] = None
    diff_mode: Optional[str] = None

    # === 熔断降级参数 ===
    is_backfill_only: bool = False
    circuit_state: Optional[str] = None

    # === Backfill 时间窗口参数 ===
    since: Optional[Union[str, int, float]] = None
    until: Optional[Union[str, int, float]] = None
    update_watermark: bool = False

    # === 分块窗口参数（TimeWindowChunk / RevisionWindowChunk）===
    window_type: Optional[str] = None  # "time" | "revision"
    window_since: Optional[str] = None
    window_until: Optional[str] = None
    window_start_rev: Optional[int] = None
    window_end_rev: Optional[int] = None
    chunk_index: Optional[int] = None  # 当前分块索引（0-based）
    chunk_total: Optional[int] = None  # 总分块数
    # 旧字段名兼容（映射自 chunk_index/chunk_total）
    current_chunk: Optional[int] = None
    total_chunks: Optional[int] = None

    # === SVN 专用参数 ===
    start_rev: Optional[int] = None
    end_rev: Optional[int] = None
    fetch_patches: Optional[bool] = None
    patch_path_filter: Optional[str] = None

    # === MR 专用参数 ===
    mr_state_filter: Optional[str] = None
    fetch_details: bool = False

    # === 通用参数 ===
    verbose: bool = False
    dry_run: bool = False
    page: Optional[int] = None  # 旧字段兼容

    # === 向前兼容：保留未知字段 ===
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "SyncJobPayload":
        """
        从 dict 解析为 SyncJobPayload。

        规则：
        - 已知字段映射到对应属性
        - 未知字段保留在 extra 中
        - 类型验证失败抛出 PayloadParseError
        - 支持字段名映射（chunk_index <-> current_chunk）

        Args:
            data: payload dict（可为 None）

        Returns:
            SyncJobPayload 实例

        Raises:
            PayloadParseError: 类型验证失败
        """
        if data is None:
            return cls()

        if not isinstance(data, dict):
            raise PayloadParseError(f"payload must be a dict, got {type(data).__name__}")

        # 已知字段列表
        known_fields = {
            "gitlab_instance",
            "tenant_id",
            "token",
            "batch_size",
            "suggested_batch_size",
            "suggested_forward_window_seconds",
            "suggested_diff_mode",
            "diff_mode",
            "is_backfill_only",
            "circuit_state",
            "since",
            "until",
            "update_watermark",
            "window_type",
            "window_since",
            "window_until",
            "window_start_rev",
            "window_end_rev",
            "chunk_index",
            "chunk_total",
            "current_chunk",
            "total_chunks",
            "start_rev",
            "end_rev",
            "fetch_patches",
            "patch_path_filter",
            "mr_state_filter",
            "fetch_details",
            "verbose",
            "dry_run",
            "page",
        }

        # 分离已知字段和未知字段
        known_values = {}
        extra = {}

        for key, value in data.items():
            if key in known_fields:
                known_values[key] = value
            else:
                extra[key] = value

        # 类型验证和转换
        try:
            # 字符串字段
            for str_field in [
                "gitlab_instance",
                "tenant_id",
                "token",
                "suggested_diff_mode",
                "diff_mode",
                "circuit_state",
                "mr_state_filter",
                "patch_path_filter",
                "window_type",
                "window_since",
                "window_until",
            ]:
                if str_field in known_values:
                    val = known_values[str_field]
                    if val is not None and not isinstance(val, str):
                        known_values[str_field] = str(val)

            # 整数字段
            for int_field in [
                "batch_size",
                "suggested_batch_size",
                "suggested_forward_window_seconds",
                "start_rev",
                "end_rev",
                "page",
                "chunk_index",
                "chunk_total",
                "current_chunk",
                "total_chunks",
                "window_start_rev",
                "window_end_rev",
            ]:
                if int_field in known_values:
                    val = known_values[int_field]
                    if val is not None:
                        try:
                            known_values[int_field] = int(val)
                        except (ValueError, TypeError) as e:
                            raise PayloadParseError(
                                f"field '{int_field}' must be int, got {type(val).__name__}: {val}"
                            ) from e

            # 布尔字段
            for bool_field in [
                "is_backfill_only",
                "update_watermark",
                "fetch_details",
                "verbose",
                "dry_run",
                "fetch_patches",
            ]:
                if bool_field in known_values:
                    val = known_values[bool_field]
                    if val is not None:
                        known_values[bool_field] = bool(val)

            # since/until 可以是字符串或数字（时间戳）
            for time_field in ["since", "until"]:
                if time_field in known_values:
                    val = known_values[time_field]
                    if val is not None and not isinstance(val, (str, int, float)):
                        raise PayloadParseError(
                            f"field '{time_field}' must be str/int/float, got {type(val).__name__}"
                        )

            # === 字段名映射：chunk_index <-> current_chunk, chunk_total <-> total_chunks ===
            # 如果只有一方存在，映射到另一方
            if "chunk_index" in known_values and "current_chunk" not in known_values:
                known_values["current_chunk"] = known_values["chunk_index"]
            elif "current_chunk" in known_values and "chunk_index" not in known_values:
                known_values["chunk_index"] = known_values["current_chunk"]

            if "chunk_total" in known_values and "total_chunks" not in known_values:
                known_values["total_chunks"] = known_values["chunk_total"]
            elif "total_chunks" in known_values and "chunk_total" not in known_values:
                known_values["chunk_total"] = known_values["total_chunks"]

        except PayloadParseError:
            raise
        except Exception as e:
            raise PayloadParseError(f"payload validation failed: {e}") from e

        return cls(extra=extra, **known_values)

    def to_dict(self) -> Dict[str, Any]:
        """
        转换为 dict（用于序列化到数据库）。

        规则：
        - None 值的字段不包含在结果中（减少存储空间）
        - False 布尔值保留（与 None 区分）
        - extra 字段合并到顶层

        Returns:
            payload dict
        """

        result = {}

        # 序列化已知字段
        d = asdict(self)
        extra = d.pop("extra", {})

        for key, value in d.items():
            # 跳过 None 值（节省存储）
            if value is None:
                continue
            # 保留布尔 False（与 None 区分）
            result[key] = value

        # 合并 extra 字段（未知字段保留）
        result.update(extra)

        return result

    def get(self, key: str, default: Any = None) -> Any:
        """
        兼容 dict-like 访问（便于渐进迁移）。

        Args:
            key: 字段名
            default: 默认值

        Returns:
            字段值
        """
        if hasattr(self, key) and key != "extra":
            val = getattr(self, key)
            return val if val is not None else default
        return self.extra.get(key, default)

    def __getitem__(self, key: str) -> Any:
        """支持 payload[key] 访问（向后兼容）"""
        if hasattr(self, key) and key != "extra":
            return getattr(self, key)
        return self.extra[key]

    def __contains__(self, key: str) -> bool:
        """支持 'key' in payload 检查（向后兼容）"""
        if hasattr(self, key) and key != "extra":
            return getattr(self, key) is not None
        return key in self.extra


def parse_payload_runtime(
    payload_json: Any,
    job_id: Optional[str] = None,
) -> tuple:
    """
    安全解析 payload_json 为 SyncJobPayload（运行时使用）。

    Args:
        payload_json: 原始 payload（可以是 dict、JSON 字符串或 None）
        job_id: 可选，用于日志

    Returns:
        (payload, error) 元组：
        - 成功: (SyncJobPayload, None)
        - 失败: (None, error_message)
    """
    import json as json_mod
    import logging

    logger = logging.getLogger(__name__)

    try:
        # 处理 None
        if payload_json is None:
            return SyncJobPayload(), None

        # 处理 JSON 字符串
        if isinstance(payload_json, str):
            try:
                payload_json = json_mod.loads(payload_json)
            except json_mod.JSONDecodeError as e:
                return None, f"invalid JSON: {e}"

        # 处理已经是 SyncJobPayload 的情况
        if isinstance(payload_json, SyncJobPayload):
            return payload_json, None

        # 解析 dict
        return SyncJobPayload.from_dict(payload_json), None

    except PayloadParseError as e:
        error_msg = f"payload contract mismatch: {e}"
        if job_id:
            logger.warning(f"job_id={job_id}: {error_msg}")
        return None, error_msg
    except Exception as e:
        error_msg = f"unexpected payload parse error: {type(e).__name__}: {e}"
        if job_id:
            logger.error(f"job_id={job_id}: {error_msg}")
        return None, error_msg


# ============ 便捷构建函数 ============


def build_time_window_payload(
    since_ts: Optional[float] = None,
    until_ts: Optional[float] = None,
    *,
    gitlab_instance: Optional[str] = None,
    tenant_id: Optional[str] = None,
    mode: str = SyncMode.BACKFILL.value,
    diff_mode: str = DiffMode.BEST_EFFORT.value,
    strict: bool = False,
    **extra: Any,
) -> SyncJobPayloadV1:
    """
    构建时间窗口 payload（用于 Git/MR/Review）

    Args:
        since_ts: 开始时间戳
        until_ts: 结束时间戳
        gitlab_instance: GitLab 实例
        tenant_id: 租户 ID
        mode: 同步模式
        diff_mode: diff 获取模式
        strict: 严格模式
        **extra: 扩展字段

    Returns:
        SyncJobPayloadV1 对象
    """
    return SyncJobPayloadV1(
        window_type=WindowType.TIME.value,
        since_ts=since_ts,
        until_ts=until_ts,
        gitlab_instance=gitlab_instance,
        tenant_id=tenant_id,
        mode=mode,
        diff_mode=diff_mode,
        strict=strict,
        extra=extra,
    )


def build_rev_window_payload(
    start_rev: Optional[int] = None,
    end_rev: Optional[int] = None,
    *,
    tenant_id: Optional[str] = None,
    mode: str = SyncMode.BACKFILL.value,
    diff_mode: str = DiffMode.BEST_EFFORT.value,
    strict: bool = False,
    **extra: Any,
) -> SyncJobPayloadV1:
    """
    构建 revision 窗口 payload（用于 SVN）

    Args:
        start_rev: 开始 revision
        end_rev: 结束 revision
        tenant_id: 租户 ID
        mode: 同步模式
        diff_mode: diff 获取模式
        strict: 严格模式
        **extra: 扩展字段

    Returns:
        SyncJobPayloadV1 对象
    """
    return SyncJobPayloadV1(
        window_type=WindowType.REV.value,
        start_rev=start_rev,
        end_rev=end_rev,
        tenant_id=tenant_id,
        mode=mode,
        diff_mode=diff_mode,
        strict=strict,
        extra=extra,
    )
