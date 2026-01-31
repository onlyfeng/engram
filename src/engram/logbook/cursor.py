# -*- coding: utf-8 -*-
"""
engram_logbook.cursor - 游标工具模块

统一管理 SCM 同步游标（SVN/GitLab），提供:
- Cursor 数据结构（含 version、watermark、stats）
- load_cursor / save_cursor / upgrade_cursor 函数
- 向后兼容：自动识别并升级旧格式游标

游标存储格式 (v2):
    {
        "version": 2,
        "watermark": {
            # SVN: {"last_rev": int}
            # GitLab: {"last_commit_sha": str, "last_commit_ts": str}
        },
        "stats": {
            "last_sync_at": str (ISO 8601),
            "last_sync_count": int
        }
    }

旧格式 (v1，自动升级):
    SVN:    {"last_rev": int, "last_sync_at": str, "last_sync_count": int}
    GitLab: {"last_commit_sha": str, "last_commit_ts": str, "last_sync_at": str, "last_sync_count": int}
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .db import get_kv, set_kv

# === 时间戳解析与标准化 ===


def parse_iso_ts(ts_str: Optional[str]) -> Optional[datetime]:
    """
    将 ISO 8601 格式的时间戳字符串解析为带时区的 datetime 对象

    支持的格式：
    - "2024-01-15T12:00:00Z" -> UTC
    - "2024-01-15T12:00:00+00:00" -> UTC
    - "2024-01-15T12:00:00.123456Z" -> UTC (带微秒)

    Args:
        ts_str: ISO 8601 格式的时间戳字符串，或 None

    Returns:
        带时区的 datetime 对象，或 None（如果输入为 None 或解析失败）
    """
    if not ts_str:
        return None
    try:
        # 将 'Z' 替换为 '+00:00' 以便 fromisoformat 解析
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def normalize_iso_ts_z(ts_str: Optional[str]) -> Optional[str]:
    """
    将 ISO 8601 格式的时间戳字符串标准化为以 'Z' 结尾的 UTC 格式

    确保存储的游标时间戳格式一致：
    - "2024-01-15T12:00:00+00:00" -> "2024-01-15T12:00:00Z"
    - "2024-01-15T12:00:00Z" -> "2024-01-15T12:00:00Z" (不变)

    Args:
        ts_str: ISO 8601 格式的时间戳字符串，或 None

    Returns:
        标准化后的时间戳字符串（以 Z 结尾），或 None（如果输入为 None 或解析失败）
    """
    if not ts_str:
        return None

    dt = parse_iso_ts(ts_str)
    if dt is None:
        return ts_str  # 解析失败时返回原值

    # 确保转换为 UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    # 格式化为 ISO 格式并替换 +00:00 为 Z
    iso_str = dt.isoformat()
    if iso_str.endswith("+00:00"):
        iso_str = iso_str[:-6] + "Z"
    return iso_str


# 游标版本
CURSOR_VERSION = 2

# KV 命名空间
KV_NAMESPACE = "scm.sync"

# 游标类型
CURSOR_TYPE_SVN = "svn"
CURSOR_TYPE_GITLAB = "gitlab"
CURSOR_TYPE_GITLAB_MR = "gitlab_mr"
CURSOR_TYPE_GITLAB_REVIEWS = "gitlab_reviews"


@dataclass
class Cursor:
    """
    统一的游标数据结构

    Attributes:
        version: 游标格式版本号
        watermark: 水位线数据，根据类型不同包含不同字段
            - SVN: {"last_rev": int}
            - GitLab: {"last_commit_sha": str, "last_commit_ts": str}
        stats: 同步统计信息
            - last_sync_at: 最后同步时间 (ISO 8601)
            - last_sync_count: 最后同步的记录数
    """

    version: int = CURSOR_VERSION
    watermark: Dict[str, Any] = field(default_factory=dict)
    stats: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（用于存储）"""
        return {
            "version": self.version,
            "watermark": self.watermark,
            "stats": self.stats,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Cursor":
        """从字典创建 Cursor 对象"""
        return cls(
            version=data.get("version", CURSOR_VERSION),
            watermark=data.get("watermark", {}),
            stats=data.get("stats", {}),
        )

    # === 便捷访问方法 ===

    @property
    def last_sync_at(self) -> Optional[str]:
        """获取最后同步时间"""
        return self.stats.get("last_sync_at")

    @property
    def last_sync_count(self) -> Optional[int]:
        """获取最后同步记录数"""
        return self.stats.get("last_sync_count")

    # SVN 专用
    @property
    def last_rev(self) -> int:
        """获取最后同步的 SVN revision（仅 SVN 游标）"""
        return self.watermark.get("last_rev", 0)

    # GitLab 专用
    @property
    def last_commit_sha(self) -> Optional[str]:
        """获取最后同步的 commit SHA（仅 GitLab 游标）"""
        return self.watermark.get("last_commit_sha")

    @property
    def last_commit_ts(self) -> Optional[str]:
        """获取最后同步的 commit 时间戳（仅 GitLab 游标）"""
        return self.watermark.get("last_commit_ts")

    # GitLab MR 专用
    @property
    def last_mr_updated_at(self) -> Optional[str]:
        """获取最后同步的 MR 更新时间（仅 gitlab_mr/gitlab_reviews 游标）"""
        return self.watermark.get("last_mr_updated_at")

    @property
    def last_mr_iid(self) -> Optional[int]:
        """获取最后同步的 MR IID（仅 gitlab_mr/gitlab_reviews 游标，用于同一 updated_at 的 tie-break）"""
        return self.watermark.get("last_mr_iid")

    # GitLab Reviews 专用（可扩展为事件级水位线）
    @property
    def last_event_ts(self) -> Optional[str]:
        """获取最后同步的事件时间戳（仅 gitlab_reviews 游标，可选的事件级水位线）"""
        return self.watermark.get("last_event_ts")


def _build_cursor_key(cursor_type: str, repo_id: int) -> str:
    """
    构建游标的 KV key

    Args:
        cursor_type: 游标类型 (svn/gitlab)
        repo_id: 仓库 ID

    Returns:
        KV key，格式: <type>_cursor:<repo_id>
    """
    return f"{cursor_type}_cursor:{repo_id}"


def _detect_cursor_version(data: Dict[str, Any]) -> int:
    """
    检测游标数据的版本

    Args:
        data: 游标数据字典

    Returns:
        版本号 (1 或 2)
    """
    if "version" in data:
        return data["version"]
    # 无 version 字段说明是 v1 格式
    return 1


def upgrade_cursor(data: Dict[str, Any], cursor_type: str) -> Cursor:
    """
    升级旧格式游标到新格式

    支持的升级路径:
    - v1 (旧格式) → v2 (新格式)

    旧格式字段映射:
    - SVN v1:
        {"last_rev": int, "last_sync_at": str, "last_sync_count": int}
        → watermark: {"last_rev": int}
        → stats: {"last_sync_at": str, "last_sync_count": int}

    - GitLab v1:
        {"last_commit_sha": str, "last_commit_ts": str, "last_sync_at": str, "last_sync_count": int}
        → watermark: {"last_commit_sha": str, "last_commit_ts": str}
        → stats: {"last_sync_at": str, "last_sync_count": int}

    Args:
        data: 原始游标数据
        cursor_type: 游标类型 (svn/gitlab)

    Returns:
        升级后的 Cursor 对象
    """
    version = _detect_cursor_version(data)

    if version >= CURSOR_VERSION:
        # 已是最新版本，直接返回
        return Cursor.from_dict(data)

    # v1 → v2 升级
    if version == 1:
        watermark = {}
        stats = {}

        if cursor_type == CURSOR_TYPE_SVN:
            # SVN: last_rev 移入 watermark
            if "last_rev" in data:
                watermark["last_rev"] = data["last_rev"]
        elif cursor_type == CURSOR_TYPE_GITLAB:
            # GitLab: last_commit_sha, last_commit_ts 移入 watermark
            if "last_commit_sha" in data:
                watermark["last_commit_sha"] = data["last_commit_sha"]
            if "last_commit_ts" in data:
                watermark["last_commit_ts"] = data["last_commit_ts"]
        elif cursor_type == CURSOR_TYPE_GITLAB_MR:
            # GitLab MR: last_mr_updated_at, last_mr_iid 移入 watermark
            if "last_mr_updated_at" in data:
                watermark["last_mr_updated_at"] = data["last_mr_updated_at"]
            if "last_mr_iid" in data:
                watermark["last_mr_iid"] = data["last_mr_iid"]
        elif cursor_type == CURSOR_TYPE_GITLAB_REVIEWS:
            # GitLab Reviews: last_mr_updated_at, last_mr_iid 移入 watermark（MR 列表驱动）
            # 可选支持 last_event_ts（事件级水位线）
            if "last_mr_updated_at" in data or "last_updated_at" in data:
                watermark["last_mr_updated_at"] = data.get("last_mr_updated_at") or data.get(
                    "last_updated_at"
                )
            if "last_mr_iid" in data:
                watermark["last_mr_iid"] = data["last_mr_iid"]
            if "last_event_ts" in data:
                watermark["last_event_ts"] = data["last_event_ts"]

        # 通用 stats 字段
        if "last_sync_at" in data:
            stats["last_sync_at"] = data["last_sync_at"]
        if "last_sync_count" in data:
            stats["last_sync_count"] = data["last_sync_count"]
        # gitlab_reviews 特有的统计字段迁移
        if "last_sync_mr_count" in data:
            stats["last_sync_mr_count"] = data["last_sync_mr_count"]
        if "last_sync_event_count" in data:
            stats["last_sync_event_count"] = data["last_sync_event_count"]

        return Cursor(
            version=CURSOR_VERSION,
            watermark=watermark,
            stats=stats,
        )

    # 未知版本，尝试作为 v2 解析
    return Cursor.from_dict(data)


def load_cursor(
    cursor_type: str,
    repo_id: int,
    config=None,
) -> Cursor:
    """
    加载游标（自动升级旧格式）

    Args:
        cursor_type: 游标类型 (svn/gitlab)
        repo_id: 仓库 ID
        config: 可选的 Config 实例

    Returns:
        Cursor 对象，如果不存在返回空 Cursor
    """
    key = _build_cursor_key(cursor_type, repo_id)
    data = get_kv(KV_NAMESPACE, key, config=config)

    if not data:
        # 不存在，返回空游标
        return Cursor(version=CURSOR_VERSION, watermark={}, stats={})

    # 检测版本并升级
    return upgrade_cursor(data, cursor_type)


def save_cursor(
    cursor_type: str,
    repo_id: int,
    cursor: Cursor,
    config=None,
) -> bool:
    """
    保存游标

    Args:
        cursor_type: 游标类型 (svn/gitlab)
        repo_id: 仓库 ID
        cursor: Cursor 对象
        config: 可选的 Config 实例

    Returns:
        True 表示成功
    """
    key = _build_cursor_key(cursor_type, repo_id)
    return set_kv(KV_NAMESPACE, key, cursor.to_dict(), config=config)


# === 便捷函数（保持向后兼容）===


def load_svn_cursor(repo_id: int, config=None) -> Cursor:
    """
    加载 SVN 游标

    Args:
        repo_id: 仓库 ID
        config: 可选的 Config 实例

    Returns:
        Cursor 对象
    """
    return load_cursor(CURSOR_TYPE_SVN, repo_id, config)


def save_svn_cursor(
    repo_id: int,
    last_rev: int,
    synced_count: int,
    config=None,
) -> bool:
    """
    保存 SVN 游标

    Args:
        repo_id: 仓库 ID
        last_rev: 最后同步的 revision
        synced_count: 本次同步的记录数
        config: 可选的 Config 实例

    Returns:
        True 表示成功
    """
    cursor = Cursor(
        version=CURSOR_VERSION,
        watermark={"last_rev": last_rev},
        stats={
            "last_sync_at": normalize_iso_ts_z(datetime.now(timezone.utc).isoformat()),
            "last_sync_count": synced_count,
        },
    )
    return save_cursor(CURSOR_TYPE_SVN, repo_id, cursor, config)


def load_gitlab_cursor(repo_id: int, config=None) -> Cursor:
    """
    加载 GitLab 游标

    Args:
        repo_id: 仓库 ID
        config: 可选的 Config 实例

    Returns:
        Cursor 对象
    """
    return load_cursor(CURSOR_TYPE_GITLAB, repo_id, config)


def save_gitlab_cursor(
    repo_id: int,
    last_commit_sha: str,
    last_commit_ts: str,
    synced_count: int,
    config=None,
) -> bool:
    """
    保存 GitLab 游标

    Args:
        repo_id: 仓库 ID
        last_commit_sha: 最后同步的 commit SHA
        last_commit_ts: 最后同步的 commit 时间戳
        synced_count: 本次同步的记录数
        config: 可选的 Config 实例

    Returns:
        True 表示成功
    """
    cursor = Cursor(
        version=CURSOR_VERSION,
        watermark={
            "last_commit_sha": last_commit_sha,
            "last_commit_ts": last_commit_ts,
        },
        stats={
            "last_sync_at": normalize_iso_ts_z(datetime.now(timezone.utc).isoformat()),
            "last_sync_count": synced_count,
        },
    )
    return save_cursor(CURSOR_TYPE_GITLAB, repo_id, cursor, config)


# === GitLab MR 游标便捷函数 ===


def load_gitlab_mr_cursor(repo_id: int, config=None) -> Cursor:
    """
    加载 GitLab MR 游标

    Args:
        repo_id: 仓库 ID
        config: 可选的 Config 实例

    Returns:
        Cursor 对象，包含 watermark:
        - last_mr_updated_at: 最后同步的 MR 更新时间 (ISO 8601)
        - last_mr_iid: 最后同步的 MR IID（用于同一 updated_at 的 tie-break）
    """
    return load_cursor(CURSOR_TYPE_GITLAB_MR, repo_id, config)


def save_gitlab_mr_cursor(
    repo_id: int,
    last_mr_updated_at: str,
    last_mr_iid: int,
    synced_count: int,
    config=None,
) -> bool:
    """
    保存 GitLab MR 游标

    使用 (last_mr_updated_at, last_mr_iid) 作为复合水位线，
    确保在同一 updated_at 时间戳下能正确处理多个 MR。

    Args:
        repo_id: 仓库 ID
        last_mr_updated_at: 最后同步的 MR 更新时间 (ISO 8601)
        last_mr_iid: 最后同步的 MR IID（用于 tie-break）
        synced_count: 本次同步的 MR 数
        config: 可选的 Config 实例

    Returns:
        True 表示成功
    """
    cursor = Cursor(
        version=CURSOR_VERSION,
        watermark={
            "last_mr_updated_at": last_mr_updated_at,
            "last_mr_iid": last_mr_iid,
        },
        stats={
            "last_sync_at": normalize_iso_ts_z(datetime.now(timezone.utc).isoformat()),
            "last_sync_count": synced_count,
        },
    )
    return save_cursor(CURSOR_TYPE_GITLAB_MR, repo_id, cursor, config)


# === GitLab Reviews 游标便捷函数 ===


def load_gitlab_reviews_cursor(repo_id: int, config=None) -> Cursor:
    """
    加载 GitLab Reviews 游标

    Args:
        repo_id: 仓库 ID
        config: 可选的 Config 实例

    Returns:
        Cursor 对象，包含 watermark:
        - last_mr_updated_at: 最后同步的 MR 更新时间 (ISO 8601)
        - last_mr_iid: 最后同步的 MR IID（MR 列表驱动模式）
        - last_event_ts: 可选的事件级水位线 (ISO 8601)
    """
    return load_cursor(CURSOR_TYPE_GITLAB_REVIEWS, repo_id, config)


def save_gitlab_reviews_cursor(
    repo_id: int,
    last_mr_updated_at: str,
    last_mr_iid: Optional[int],
    synced_mr_count: int,
    synced_event_count: int,
    last_event_ts: Optional[str] = None,
    config=None,
) -> bool:
    """
    保存 GitLab Reviews 游标

    使用 (last_mr_updated_at, last_mr_iid) 作为 MR 列表驱动的水位线，
    可选 last_event_ts 作为事件级水位线。

    Args:
        repo_id: 仓库 ID
        last_mr_updated_at: 最后同步的 MR 更新时间 (ISO 8601)
        last_mr_iid: 最后同步的 MR IID（可为 None）
        synced_mr_count: 本次同步的 MR 数
        synced_event_count: 本次同步的事件数
        last_event_ts: 可选的事件级水位线 (ISO 8601)
        config: 可选的 Config 实例

    Returns:
        True 表示成功
    """
    watermark = {
        "last_mr_updated_at": last_mr_updated_at,
    }
    if last_mr_iid is not None:
        watermark["last_mr_iid"] = last_mr_iid
    if last_event_ts is not None:
        watermark["last_event_ts"] = last_event_ts

    cursor = Cursor(
        version=CURSOR_VERSION,
        watermark=watermark,
        stats={
            "last_sync_at": normalize_iso_ts_z(datetime.now(timezone.utc).isoformat()),
            "last_sync_mr_count": synced_mr_count,
            "last_sync_event_count": synced_event_count,
        },
    )
    return save_cursor(CURSOR_TYPE_GITLAB_REVIEWS, repo_id, cursor, config)


# === 游标推进辅助函数 ===


def should_advance_mr_cursor(
    new_updated_at: str,
    new_iid: int,
    last_updated_at: Optional[str],
    last_iid: Optional[int],
) -> bool:
    """
    判断是否应该推进 MR 游标（单调递增规则）

    使用 (updated_at, iid) 作为复合水位线，只有当新值严格大于旧值时才推进。
    比较规则：先比较 updated_at（基于 datetime，支持 Z 与 +00:00 等价），若相同再比较 iid。

    Args:
        new_updated_at: 新的 MR 更新时间 (ISO 8601 格式)
        new_iid: 新的 MR IID
        last_updated_at: 上次的 MR 更新时间（可为 None 表示首次同步）
        last_iid: 上次的 MR IID（可为 None）

    Returns:
        True 如果应该推进游标
    """
    # 首次同步，总是推进
    if last_updated_at is None:
        return True

    # 解析为 datetime 进行比较（支持 Z 与 +00:00 等价）
    new_dt = parse_iso_ts(new_updated_at)
    last_dt = parse_iso_ts(last_updated_at)

    # 如果解析失败，回退到字符串比较
    if new_dt is None or last_dt is None:
        if new_updated_at > last_updated_at:
            return True
        elif new_updated_at < last_updated_at:
            return False
    else:
        # 基于 datetime 比较
        if new_dt > last_dt:
            return True
        elif new_dt < last_dt:
            return False

    # updated_at 相同时，比较 iid（tie-break）
    if last_iid is None:
        return True
    return new_iid > last_iid


def should_advance_gitlab_commit_cursor(
    new_ts: str,
    new_sha: str,
    last_ts: Optional[str],
    last_sha: Optional[str],
) -> bool:
    """
    判断是否应该推进 GitLab commit 游标（单调递增规则）

    使用 (ts, sha) 作为复合水位线，只有当新值严格大于旧值时才推进。
    比较规则：先比较 ts（基于 datetime，支持 Z 与 +00:00 等价），若相同再比较 sha（字典序）。

    这保证了同一秒内有多个 commit 时的稳定处理顺序。

    Args:
        new_ts: 新的 commit 时间戳 (ISO 8601 格式)
        new_sha: 新的 commit SHA
        last_ts: 上次的 commit 时间戳（可为 None 表示首次同步）
        last_sha: 上次的 commit SHA（可为 None）

    Returns:
        True 如果应该推进游标
    """
    # 首次同步，总是推进
    if last_ts is None:
        return True

    # 解析为 datetime 进行比较（支持 Z 与 +00:00 等价）
    new_dt = parse_iso_ts(new_ts)
    last_dt = parse_iso_ts(last_ts)

    # 如果解析失败，回退到字符串比较
    if new_dt is None or last_dt is None:
        if new_ts > last_ts:
            return True
        elif new_ts < last_ts:
            return False
    else:
        # 基于 datetime 比较
        if new_dt > last_dt:
            return True
        elif new_dt < last_dt:
            return False

    # ts 相同时，比较 sha（tie-break）
    if last_sha is None:
        return True
    return new_sha > last_sha


# === 游标年龄计算 ===


def get_cursor_updated_at_timestamp(cursor: Cursor) -> Optional[float]:
    """
    获取游标最后更新时间的 Unix 时间戳

    从游标的 stats.last_sync_at 字段解析时间戳。

    Args:
        cursor: Cursor 对象

    Returns:
        Unix 时间戳（秒），如果没有同步记录返回 None
    """
    last_sync_at = cursor.last_sync_at
    if not last_sync_at:
        return None

    dt = parse_iso_ts(last_sync_at)
    if dt is None:
        return None

    return dt.timestamp()


def calculate_cursor_age_seconds(
    cursor: Cursor,
    now: Optional[float] = None,
) -> float:
    """
    计算游标年龄（距离上次同步的秒数）

    Args:
        cursor: Cursor 对象
        now: 当前时间戳，None 时使用 datetime.now()

    Returns:
        游标年龄（秒），如果从未同步返回 float('inf')
    """
    cursor_ts = get_cursor_updated_at_timestamp(cursor)

    if cursor_ts is None:
        return float("inf")

    if now is None:
        now = datetime.now(timezone.utc).timestamp()

    return max(0.0, now - cursor_ts)


def get_all_cursor_keys_for_repo(repo_id: int) -> list:
    """
    获取仓库所有可能的游标 key 列表

    Args:
        repo_id: 仓库 ID

    Returns:
        游标 key 列表
    """
    return [
        _build_cursor_key(CURSOR_TYPE_SVN, repo_id),
        _build_cursor_key(CURSOR_TYPE_GITLAB, repo_id),
        _build_cursor_key(CURSOR_TYPE_GITLAB_MR, repo_id),
        _build_cursor_key(CURSOR_TYPE_GITLAB_REVIEWS, repo_id),
    ]


def get_cursor_type_for_job(job_type: str, repo_type: str) -> Optional[str]:
    """
    根据 job_type 和 repo_type 获取对应的游标类型

    Args:
        job_type: 任务类型 ('commits', 'mrs', 'reviews')
        repo_type: 仓库类型 ('git', 'svn')

    Returns:
        游标类型，无效组合返回 None
    """
    if repo_type == "svn":
        if job_type == "commits":
            return CURSOR_TYPE_SVN
    elif repo_type == "git":
        if job_type == "commits":
            return CURSOR_TYPE_GITLAB
        elif job_type == "mrs":
            return CURSOR_TYPE_GITLAB_MR
        elif job_type == "reviews":
            return CURSOR_TYPE_GITLAB_REVIEWS

    return None
