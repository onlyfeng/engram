#!/usr/bin/env python3
"""
scm_sync_gitlab_reviews.py - GitLab MR Reviews 同步脚本

功能:
- 对增量 MR 集合拉取 discussions/notes/approval state
- 将每条源事件映射为标准 event_type
- 写入 scm.review_events（source_event_id + payload_json 保留原始字段）

事件类型映射:
- note (general)     -> comment
- note (diff)        -> code_comment
- approval           -> approve
- unapproval         -> unapprove
- assignee change    -> assign
- reviewer change    -> reviewer_assign
- merge              -> merge
- close              -> close
- reopen             -> reopen

使用:
    python scm_sync_gitlab_reviews.py [--config PATH] [--batch-size N] [--verbose]

配置文件示例:
    [gitlab]
    url = "https://gitlab.example.com"
    project_id = 123                   # 或 "namespace/project"
    private_token = "glpat-xxx"
    batch_size = 50                    # 每次同步最大 MR 数
"""

import argparse
import json
import logging
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set

import psycopg

from engram_step1.config import (
    Config,
    add_config_argument,
    get_config,
    get_gitlab_config,
    get_incremental_config,
    get_http_config,
    get_scm_sync_mode,
    is_strict_mode,
    SCM_SYNC_MODE_STRICT,
    SCM_SYNC_MODE_BEST_EFFORT,
)
from engram_step1.cursor import (
    load_gitlab_reviews_cursor,
    save_gitlab_reviews_cursor,
    should_advance_mr_cursor,
    normalize_iso_ts_z,
)
from engram_step1.db import get_connection, create_item, add_event
from engram_step1.errors import DatabaseError, EngramError, ValidationError
from engram_step1.source_id import build_mr_source_id, build_gitlab_repo_url
from engram_step1.scm_auth import (
    TokenProvider,
    create_gitlab_token_provider,
    mask_token,
    redact,
)
from engram_step1.gitlab_client import (
    GitLabClient,
    GitLabAPIError,
    GitLabRateLimitError,
    GitLabAuthError,
    GitLabServerError,
    GitLabTimeoutError,
    GitLabNetworkError,
    GitLabErrorCategory,
    HttpConfig,
)
from identity_resolve import resolve_and_enrich_meta
from scm_repo import build_mr_id, ensure_repo
from engram_step1 import scm_sync_lock
from db import insert_sync_run_start, insert_sync_run_finish

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# KV 命名空间和键名（保留向后兼容）
KV_NAMESPACE = "scm.sync"
KV_KEY_PREFIX = "gitlab_reviews_cursor:"  # 旧格式 gitlab_reviews_cursor:<repo_id>，现已迁移到 cursor 模块

# 默认配置值
DEFAULT_BATCH_SIZE = 50
DEFAULT_REQUEST_TIMEOUT = 60
DEFAULT_LOCK_LEASE_SECONDS = 300  # 默认锁租约时间（秒），reviews 需要更长（多 API 调用）
DEFAULT_RENEW_INTERVAL_MRS = 10  # 每处理 N 个 MR 后续租一次

# physical_job_type 常量
# 使用 physical_job_type 确保同一语义任务在队列/锁表中有唯一键
# 参见 engram_step1.scm_sync_job_types 模块
JOB_TYPE_GITLAB_REVIEWS = "gitlab_reviews"  # physical_job_type: GitLab Review 事件


def _generate_worker_id() -> str:
    """生成唯一的 worker ID（基于 hostname + pid + uuid 片段）"""
    import socket
    import os
    hostname = socket.gethostname()[:16]
    pid = os.getpid()
    short_uuid = str(uuid.uuid4())[:8]
    return f"{hostname}-{pid}-{short_uuid}"

# 事件类型常量
EVENT_TYPE_COMMENT = "comment"
EVENT_TYPE_CODE_COMMENT = "code_comment"
EVENT_TYPE_APPROVE = "approve"
EVENT_TYPE_UNAPPROVE = "unapprove"
EVENT_TYPE_ASSIGN = "assign"
EVENT_TYPE_REVIEWER_ASSIGN = "reviewer_assign"
EVENT_TYPE_MERGE = "merge"
EVENT_TYPE_CLOSE = "close"
EVENT_TYPE_REOPEN = "reopen"
EVENT_TYPE_LABEL = "label"
EVENT_TYPE_MILESTONE = "milestone"
EVENT_TYPE_UNKNOWN = "unknown"


@dataclass
class ReviewEvent:
    """Review 事件数据结构"""
    source_event_id: str          # GitLab 原始事件 ID（格式: <type>:<id>）
    mr_id: str                    # MR 标识
    event_type: str               # 标准事件类型
    actor_username: Optional[str] = None
    actor_email: Optional[str] = None
    ts: Optional[datetime] = None
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SyncConfig:
    """同步配置"""
    gitlab_url: str
    project_id: str
    token_provider: TokenProvider  # 使用 TokenProvider 替代 private_token
    batch_size: int = DEFAULT_BATCH_SIZE
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT
    include_merged: bool = True    # 是否包含已合并的 MR
    include_closed: bool = True    # 是否包含已关闭的 MR
    overlap_seconds: int = 300     # 向前重叠的秒数，用于防止边界丢失
    strict: bool = False           # 严格模式：不可恢复的错误时不推进游标


class GitLabReviewSyncError(EngramError):
    """GitLab Review 同步错误"""
    exit_code = 13
    error_type = "GITLAB_REVIEW_SYNC_ERROR"


# GitLabAPIError 现在从 engram_step1.gitlab_client 导入


# ============ 不可恢复错误分类 ============

# 不可恢复的错误类型（strict 模式下这些错误会阻止游标推进）
UNRECOVERABLE_ERROR_CATEGORIES = {
    GitLabErrorCategory.RATE_LIMITED,    # 429
    GitLabErrorCategory.SERVER_ERROR,    # 5xx
    GitLabErrorCategory.TIMEOUT,         # 请求超时
    GitLabErrorCategory.AUTH_ERROR,      # 401/403
    GitLabErrorCategory.NETWORK_ERROR,   # 网络错误
}


def classify_gitlab_error(error: Exception, api_call: str = "", context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    分类 GitLab 错误，返回结构化错误信息
    
    Args:
        error: 捕获的异常
        api_call: API 调用名称
        context: 额外的上下文信息（如 mr_iid, page 等）
        
    Returns:
        包含 error_category, status_code, message, is_unrecoverable, api_call, context 的字典
    """
    error_info = {
        "error_category": "unknown",
        "status_code": None,
        "message": str(error),
        "is_unrecoverable": False,
        "api_call": api_call,
    }
    
    if context:
        error_info.update(context)
    
    if isinstance(error, GitLabRateLimitError):
        error_info["error_category"] = GitLabErrorCategory.RATE_LIMITED.value
        error_info["status_code"] = 429
        error_info["is_unrecoverable"] = True
        if hasattr(error, 'retry_after'):
            error_info["retry_after"] = error.retry_after
    elif isinstance(error, GitLabAuthError):
        error_info["error_category"] = GitLabErrorCategory.AUTH_ERROR.value
        error_info["status_code"] = getattr(error, 'status_code', 401)
        error_info["is_unrecoverable"] = True
    elif isinstance(error, GitLabServerError):
        error_info["error_category"] = GitLabErrorCategory.SERVER_ERROR.value
        error_info["status_code"] = getattr(error, 'status_code', 500)
        error_info["is_unrecoverable"] = True
    elif isinstance(error, GitLabTimeoutError):
        error_info["error_category"] = GitLabErrorCategory.TIMEOUT.value
        error_info["is_unrecoverable"] = True
    elif isinstance(error, GitLabNetworkError):
        error_info["error_category"] = GitLabErrorCategory.NETWORK_ERROR.value
        error_info["is_unrecoverable"] = True
    elif isinstance(error, GitLabAPIError):
        # 通用 API 错误，根据 category 判断
        if hasattr(error, 'category') and error.category:
            error_info["error_category"] = error.category.value
            error_info["is_unrecoverable"] = error.category in UNRECOVERABLE_ERROR_CATEGORIES
        error_info["status_code"] = getattr(error, 'status_code', None)
    
    return error_info


def is_unrecoverable_error(error: Exception) -> bool:
    """判断错误是否为不可恢复的错误"""
    return classify_gitlab_error(error).get("is_unrecoverable", False)


def parse_datetime(date_str: Optional[str]) -> Optional[datetime]:
    """解析 ISO 8601 日期时间字符串"""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except ValueError:
        logger.warning(f"无法解析日期时间: {date_str}")
        return None


def map_note_to_event_type(note: Dict[str, Any]) -> str:
    """
    将 GitLab note 映射为标准事件类型

    Args:
        note: GitLab note 数据

    Returns:
        标准事件类型
    """
    # 系统生成的 note（如合并、关闭等操作）
    if note.get("system", False):
        body = note.get("body", "").lower()
        # 解析系统消息类型
        if "approved" in body:
            if "unapproved" in body:
                return EVENT_TYPE_UNAPPROVE
            return EVENT_TYPE_APPROVE
        elif "merged" in body:
            return EVENT_TYPE_MERGE
        elif "closed" in body:
            return EVENT_TYPE_CLOSE
        elif "reopened" in body:
            return EVENT_TYPE_REOPEN
        elif "assigned" in body or "unassigned" in body:
            return EVENT_TYPE_ASSIGN
        elif "requested review" in body or "removed review request" in body:
            return EVENT_TYPE_REVIEWER_ASSIGN
        elif "added" in body and "label" in body:
            return EVENT_TYPE_LABEL
        elif "milestone" in body:
            return EVENT_TYPE_MILESTONE
        # 其他系统消息
        return EVENT_TYPE_UNKNOWN

    # 普通评论 vs 代码评论
    if note.get("position") or note.get("type") == "DiffNote":
        return EVENT_TYPE_CODE_COMMENT
    
    return EVENT_TYPE_COMMENT


def extract_events_from_notes(
    mr_id: str,
    notes: List[Dict[str, Any]],
    existing_event_ids: Set[str],
) -> List[ReviewEvent]:
    """
    从 notes 列表中提取 review 事件

    Args:
        mr_id: MR 标识
        notes: GitLab notes 列表
        existing_event_ids: 已存在的事件 ID 集合

    Returns:
        ReviewEvent 列表
    """
    events = []
    
    for note in notes:
        note_id = note.get("id")
        if not note_id:
            continue
        
        source_event_id = f"note:{note_id}"
        
        # 跳过已存在的事件
        if source_event_id in existing_event_ids:
            continue
        
        event_type = map_note_to_event_type(note)
        
        # 跳过不需要记录的系统消息类型
        if event_type == EVENT_TYPE_UNKNOWN:
            continue
        
        # 提取作者信息
        author = note.get("author", {})
        actor_username = author.get("username")
        actor_email = author.get("email")  # 可能为空
        
        # 创建事件
        event = ReviewEvent(
            source_event_id=source_event_id,
            mr_id=mr_id,
            event_type=event_type,
            actor_username=actor_username,
            actor_email=actor_email,
            ts=parse_datetime(note.get("created_at")),
            payload={
                "note_id": note_id,
                "body": note.get("body"),
                "system": note.get("system", False),
                "resolvable": note.get("resolvable", False),
                "resolved": note.get("resolved", False),
                "noteable_type": note.get("noteable_type"),
                "position": note.get("position"),
                "author": author,
                "created_at": note.get("created_at"),
                "updated_at": note.get("updated_at"),
            },
        )
        events.append(event)
    
    return events


def extract_events_from_approvals(
    mr_id: str,
    approvals: Dict[str, Any],
    existing_event_ids: Set[str],
) -> List[ReviewEvent]:
    """
    从 approval 状态中提取事件

    Args:
        mr_id: MR 标识
        approvals: GitLab approvals 响应
        existing_event_ids: 已存在的事件 ID 集合

    Returns:
        ReviewEvent 列表
    """
    events = []
    
    approved_by = approvals.get("approved_by", [])
    for approval in approved_by:
        user = approval.get("user", {})
        user_id = user.get("id")
        if not user_id:
            continue
        
        # 使用 user_id 作为唯一标识（approval 没有独立的事件 ID）
        source_event_id = f"approval:{mr_id}:{user_id}"
        
        if source_event_id in existing_event_ids:
            continue
        
        event = ReviewEvent(
            source_event_id=source_event_id,
            mr_id=mr_id,
            event_type=EVENT_TYPE_APPROVE,
            actor_username=user.get("username"),
            actor_email=user.get("email"),
            ts=None,  # approval 状态不包含时间戳，需要从 notes 中获取
            payload={
                "user_id": user_id,
                "user": user,
                "approved": True,
            },
        )
        events.append(event)
    
    return events


def extract_events_from_state_events(
    mr_id: str,
    state_events: List[Dict[str, Any]],
    existing_event_ids: Set[str],
) -> List[ReviewEvent]:
    """
    从 resource_state_events 中提取事件

    Args:
        mr_id: MR 标识
        state_events: GitLab resource_state_events 列表
        existing_event_ids: 已存在的事件 ID 集合

    Returns:
        ReviewEvent 列表
    """
    events = []
    
    state_type_mapping = {
        "merged": EVENT_TYPE_MERGE,
        "closed": EVENT_TYPE_CLOSE,
        "reopened": EVENT_TYPE_REOPEN,
    }
    
    for state_event in state_events:
        event_id = state_event.get("id")
        if not event_id:
            continue
        
        source_event_id = f"state:{event_id}"
        
        if source_event_id in existing_event_ids:
            continue
        
        state = state_event.get("state")
        event_type = state_type_mapping.get(state)
        if not event_type:
            continue
        
        user = state_event.get("user", {})
        
        event = ReviewEvent(
            source_event_id=source_event_id,
            mr_id=mr_id,
            event_type=event_type,
            actor_username=user.get("username"),
            actor_email=user.get("email"),
            ts=parse_datetime(state_event.get("created_at")),
            payload={
                "state_event_id": event_id,
                "state": state,
                "user": user,
                "created_at": state_event.get("created_at"),
            },
        )
        events.append(event)
    
    return events


def get_or_create_mr(
    conn: psycopg.Connection,
    repo_id: int,
    mr_data: Dict[str, Any],
    gitlab_url: str,
) -> str:
    """
    获取或创建 MR 记录

    Args:
        conn: 数据库连接
        repo_id: 仓库 ID
        mr_data: GitLab MR 数据
        gitlab_url: GitLab 实例 URL

    Returns:
        mr_id
    """
    mr_iid = mr_data.get("iid")
    # 使用统一的 build_mr_id 函数构建 mr_id，确保与 MR 同步脚本一致
    mr_id = build_mr_id(repo_id, mr_iid)
    # 构建 source_id 用于统一标识
    source_id = build_mr_source_id(repo_id, mr_iid)
    
    # 提取 MR 信息
    author = mr_data.get("author", {})
    author_username = author.get("username")
    author_email = author.get("email")
    status = mr_data.get("state", "unknown")
    web_url = mr_data.get("web_url", "")
    
    # 构建 meta_json（保留 author raw 字段）
    meta_json = {
        "iid": mr_iid,
        "title": mr_data.get("title"),
        "description": mr_data.get("description"),
        "source_branch": mr_data.get("source_branch"),
        "target_branch": mr_data.get("target_branch"),
        "author": author,  # 保留 author 原始信息
        "created_at": mr_data.get("created_at"),
        "updated_at": mr_data.get("updated_at"),
        "merged_at": mr_data.get("merged_at"),
        "closed_at": mr_data.get("closed_at"),
        "labels": mr_data.get("labels", []),
        "milestone": mr_data.get("milestone"),
    }
    
    # 通过 identity_resolve 解析作者身份
    meta_json = resolve_and_enrich_meta(
        meta_json,
        account_type="gitlab",
        username=author_username,
        email=author_email,
    )
    
    # 获取解析后的 author_user_id，解析失败写 NULL（不回退到 username）
    identity_resolved = meta_json.get("identity_resolved", {})
    author_user_id = identity_resolved.get("resolved_user_id")  # 解析失败时为 None
    
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scm.mrs (mr_id, repo_id, author_user_id, status, url, meta_json, source_id, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (mr_id) DO UPDATE SET
                status = EXCLUDED.status,
                url = EXCLUDED.url,
                meta_json = EXCLUDED.meta_json,
                source_id = EXCLUDED.source_id,
                updated_at = now()
            RETURNING mr_id
            """,
            (mr_id, repo_id, author_user_id, status, web_url, json.dumps(meta_json), source_id),
        )
        result = cur.fetchone()
        conn.commit()
        return result[0]


def get_existing_event_ids(
    conn: psycopg.Connection,
    mr_id: str,
) -> Set[str]:
    """
    获取 MR 已存在的 source_event_id 集合

    Args:
        conn: 数据库连接
        mr_id: MR 标识

    Returns:
        source_event_id 集合
    """
    existing_ids = set()
    
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_event_id
            FROM scm.review_events
            WHERE mr_id = %s
            """,
            (mr_id,),
        )
        for row in cur.fetchall():
            if row[0]:
                existing_ids.add(row[0])
    
    return existing_ids


@dataclass
class ReviewInsertStats:
    """Review 事件插入统计结果"""
    total: int = 0       # 总共尝试插入的事件数
    inserted: int = 0    # 实际新插入的事件数
    skipped: int = 0     # 因幂等跳过的事件数（已存在）


def insert_review_events(
    conn: psycopg.Connection,
    events: List[ReviewEvent],
) -> ReviewInsertStats:
    """
    将 review 事件插入数据库

    Args:
        conn: 数据库连接
        events: ReviewEvent 列表

    Returns:
        ReviewInsertStats 统计结果
    """
    stats = ReviewInsertStats()
    if not events:
        return stats
    
    stats.total = len(events)
    with conn.cursor() as cur:
        for event in events:
            # 构建 payload（source_event_id 已作为单独列存储，保留在 payload 中供兼容）
            payload = dict(event.payload)
            payload["source_event_id"] = event.source_event_id

            # 解析评审者身份并填充到 payload
            payload = resolve_and_enrich_meta(
                payload,
                account_type="gitlab",
                username=event.actor_username,
                email=event.actor_email,
            )

            # 获取解析后的 reviewer_user_id，解析失败写 NULL（不回退到 username）
            identity_resolved = payload.get("identity_resolved", {})
            resolved_user_id = identity_resolved.get("resolved_user_id")
            reviewer_user_id = resolved_user_id  # 解析失败时为 None
            
            try:
                cur.execute(
                    """
                    INSERT INTO scm.review_events
                        (mr_id, source_event_id, reviewer_user_id, event_type, payload_json, ts)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (mr_id, source_event_id) DO NOTHING
                    """,
                    (
                        event.mr_id,
                        event.source_event_id,
                        reviewer_user_id,
                        event.event_type,
                        json.dumps(payload, ensure_ascii=False, default=str),
                        event.ts or datetime.utcnow(),
                    ),
                )
                if cur.rowcount > 0:
                    stats.inserted += 1
                else:
                    stats.skipped += 1
            except psycopg.Error as e:
                logger.error(f"插入 review event 失败 (source_event_id={event.source_event_id}): {e}")
                raise DatabaseError(
                    f"插入 review event 失败: {e}",
                    {"source_event_id": event.source_event_id, "error": str(e)},
                )
    
    conn.commit()
    return stats


def get_last_sync_cursor(repo_id: int, config: Optional[Config] = None) -> Dict[str, Any]:
    """
    从 cursor 模块获取上次同步的游标

    使用统一的 cursor 模块，支持 v1→v2 自动升级。

    Args:
        repo_id: 仓库 ID
        config: 配置实例

    Returns:
        游标数据字典，包含 last_mr_updated_at, last_mr_iid 等字段
    """
    cursor = load_gitlab_reviews_cursor(repo_id, config)
    return {
        "last_mr_updated_at": cursor.last_mr_updated_at,
        "last_mr_iid": cursor.last_mr_iid,
        "last_event_ts": cursor.last_event_ts,
        "last_sync_at": cursor.last_sync_at,
    }


def update_sync_cursor(
    repo_id: int,
    last_mr_updated_at: str,
    last_mr_iid: Optional[int],
    synced_mr_count: int,
    synced_event_count: int,
    last_event_ts: Optional[str] = None,
    config: Optional[Config] = None,
) -> None:
    """
    使用 cursor 模块更新同步游标

    时间戳在保存前会被标准化为 Z 结尾的 UTC 格式。

    Args:
        repo_id: 仓库 ID
        last_mr_updated_at: 最后同步的 MR 更新时间
        last_mr_iid: 最后同步的 MR IID
        synced_mr_count: 本次同步的 MR 数
        synced_event_count: 本次同步的事件数
        last_event_ts: 可选的事件级水位线
        config: 配置实例
    """
    # 标准化时间戳为 Z 结尾格式，确保存储一致
    normalized_updated_at = normalize_iso_ts_z(last_mr_updated_at) or last_mr_updated_at
    normalized_event_ts = normalize_iso_ts_z(last_event_ts) if last_event_ts else None
    
    save_gitlab_reviews_cursor(
        repo_id=repo_id,
        last_mr_updated_at=normalized_updated_at,
        last_mr_iid=last_mr_iid,
        synced_mr_count=synced_mr_count,
        synced_event_count=synced_event_count,
        last_event_ts=normalized_event_ts,
        config=config,
    )
    logger.info(f"更新同步游标: repo_id={repo_id}, last_mr_updated_at={normalized_updated_at}, last_mr_iid={last_mr_iid}")


def sync_gitlab_reviews(
    sync_config: SyncConfig,
    project_key: str,
    config: Optional[Config] = None,
    verbose: bool = True,
    backfill_since: Optional[str] = None,
    backfill_until: Optional[str] = None,
    update_watermark: bool = True,
    lock_lease_seconds: int = DEFAULT_LOCK_LEASE_SECONDS,
    renew_interval_mrs: int = DEFAULT_RENEW_INTERVAL_MRS,
) -> Dict[str, Any]:
    """
    执行 GitLab MR reviews 同步

    Args:
        sync_config: 同步配置
        project_key: 项目标识
        config: Config 实例
        verbose: 是否输出详细信息
        backfill_since: 回填模式起始时间（ISO 格式），覆盖游标
        backfill_until: 回填模式截止时间（ISO 格式），可选，默认为当前时间
        update_watermark: 是否更新游标（回填模式下默认为 False）
        lock_lease_seconds: 锁租约时长（秒），默认 300 秒（reviews 多 API 调用需要更长）
        renew_interval_mrs: 每处理 N 个 MR 后续租一次，默认 10

    Returns:
        同步结果统计，如果获取锁失败则返回 {"locked": True, "skipped": True}
    """
    # 生成 run_id 用于追踪本次同步运行
    run_id = str(uuid.uuid4())
    
    # 生成 worker_id 用于锁标识
    worker_id = _generate_worker_id()
    
    result = {
        "success": False,
        "run_id": run_id,
        "synced_mr_count": 0,
        "synced_event_count": 0,
        "skipped_event_count": 0,
        "since": None,
        "backfill_mode": backfill_since is not None,
        "error": None,
        "cursor_advance_reason": None,  # 游标推进原因
        "cursor_advance_stopped_at": None,  # strict 模式下游标停止的位置
        "unrecoverable_errors": [],  # 不可恢复的错误列表
        "missing_types": [],  # best_effort 模式下记录的缺失类型
        "strict_mode": sync_config.strict,  # 是否启用严格模式
        "sync_mode": SCM_SYNC_MODE_STRICT if sync_config.strict else SCM_SYNC_MODE_BEST_EFFORT,
        "locked": False,  # 是否因锁被跳过
        "skipped": False,  # 是否被跳过
    }
    
    logger.info(f"[run_id={run_id[:8]}] 开始 GitLab Reviews 同步")
    
    # 锁相关变量（用于 finally 块）
    lock_acquired = False
    lock_repo_id: Optional[int] = None

    # sync_run 记录变量（用于 finally 块）
    sync_run_started = False
    sync_run_repo_id = None
    sync_run_cursor_before = None
    sync_run_conn = None

    # 构建 HTTP 配置（使用 HttpConfig.from_config 统一加载，包含 rate limit / concurrency）
    http_config = HttpConfig.from_config(config)
    # 覆盖 timeout（sync_config 中可能有自定义值）
    http_config.timeout_seconds = sync_config.request_timeout

    client = GitLabClient(
        base_url=sync_config.gitlab_url,
        token_provider=sync_config.token_provider,
        http_config=http_config,
    )

    conn = get_connection(config=config)
    try:
        # 1. 获取或创建仓库记录（使用统一的 ensure_repo）
        repo_url = build_gitlab_repo_url(sync_config.gitlab_url, sync_config.project_id)
        repo_id = ensure_repo(
            repo_type="git",
            url=repo_url,
            project_key=project_key,
            config=config,
        )
        result["repo_id"] = repo_id
        lock_repo_id = repo_id
        logger.debug(f"仓库记录: repo_id={repo_id}, url={repo_url}")

        # 1.5. 尝试获取分布式锁（repo_id + job_type）
        lock_acquired = scm_sync_lock.claim(
            repo_id=repo_id,
            job_type=JOB_TYPE_GITLAB_REVIEWS,
            worker_id=worker_id,
            lease_seconds=lock_lease_seconds,
            conn=conn,
        )
        if not lock_acquired:
            # 锁被其他 worker 持有，返回 locked/skip 结果
            lock_info = scm_sync_lock.get(repo_id, JOB_TYPE_GITLAB_REVIEWS, conn=conn)
            logger.warning(
                f"[run_id={run_id[:8]}] 锁被其他 worker 持有，跳过本次同步 "
                f"(repo_id={repo_id}, job_type={JOB_TYPE_GITLAB_REVIEWS}, "
                f"locked_by={lock_info.get('locked_by') if lock_info else 'unknown'})"
            )
            result["locked"] = True
            result["skipped"] = True
            result["message"] = "锁被其他 worker 持有，跳过本次同步"
            return result
        
        logger.debug(f"[run_id={run_id[:8]}] 成功获取锁 (repo_id={repo_id}, worker_id={worker_id})")

        # 2. 获取上次同步的游标
        cursor = get_last_sync_cursor(repo_id, config)
        last_mr_updated_at = cursor.get("last_mr_updated_at")
        last_mr_iid = cursor.get("last_mr_iid")

        # 保存游标快照用于 sync_run 记录
        sync_run_cursor_before = dict(cursor)
        sync_run_repo_id = repo_id
        sync_run_conn = conn
        
        # 记录 sync_run 开始
        try:
            insert_sync_run_start(
                conn=conn,
                run_id=run_id,
                repo_id=repo_id,
                job_type="gitlab_reviews",
                mode="backfill" if backfill_since else "incremental",
                cursor_before=sync_run_cursor_before,
                meta_json={
                    "batch_size": sync_config.batch_size,
                },
            )
            conn.commit()
            sync_run_started = True
        except Exception as e:
            logger.warning(f"记录 sync_run 开始失败 (非致命): {e}")

        # 回填模式：使用指定的起始/截止时间覆盖游标
        # 回填模式下默认不更新游标（除非显式指定 update_watermark=True）
        is_backfill_mode = backfill_since is not None
        should_update_cursor = update_watermark if is_backfill_mode else True
        
        if backfill_since:
            updated_after = backfill_since
            # backfill_until 用于限制查询范围（如果 API 支持）
            backfill_until_str = backfill_until or datetime.utcnow().isoformat() + "Z"
            logger.info(
                f"回填模式: 从 {updated_after} 开始扫描，截止到 {backfill_until_str}（忽略游标）"
                f"，update_watermark={should_update_cursor}"
            )
        else:
            # 应用 overlap_seconds：向前回退一段时间以防止边界丢失
            updated_after = None
            if last_mr_updated_at:
                try:
                    last_ts = datetime.fromisoformat(last_mr_updated_at.replace("Z", "+00:00"))
                    overlap_delta = timedelta(seconds=sync_config.overlap_seconds)
                    overlap_ts = last_ts - overlap_delta
                    updated_after = overlap_ts.isoformat().replace("+00:00", "Z")
                    logger.info(f"上次同步时间: {last_mr_updated_at}, 应用 overlap {sync_config.overlap_seconds}s 后: {updated_after}")
                except (ValueError, TypeError) as e:
                    logger.warning(f"解析上次同步时间失败: {last_mr_updated_at}, 将重新全量同步: {e}")
                    updated_after = None
            else:
                logger.info("首次同步，获取所有 MRs")

        result["since"] = updated_after
        result["last_mr_iid"] = last_mr_iid

        # 3. 拉取增量 MRs
        logger.info(f"从 GitLab 获取 MRs (project_id={sync_config.project_id}, updated_after={updated_after})")

        all_mrs = []
        unrecoverable_errors: List[Dict[str, Any]] = []
        page = 1
        api_call_failed = False  # 标记关键 API 调用是否失败
        
        while True:
            try:
                mrs_data = client.get_merge_requests(
                    sync_config.project_id,
                    state="all",
                    updated_after=updated_after,
                    per_page=min(sync_config.batch_size, 100),
                    page=page,
                    order_by="updated_at",
                    sort="asc",
                )
            except GitLabAPIError as e:
                error_info = classify_gitlab_error(e, "get_merge_requests", {"page": page})
                logger.error(f"获取 MRs 列表失败 (page={page}): {e.message}, category={error_info['error_category']}")
                
                if error_info["is_unrecoverable"]:
                    unrecoverable_errors.append(error_info)
                    api_call_failed = True
                    # strict 模式下遇到不可恢复错误立即停止
                    if sync_config.strict:
                        logger.warning(f"[strict mode] get_merge_requests 遇到不可恢复错误，停止同步")
                        break
                # best_effort 模式下停止获取更多 MR
                break

            if not mrs_data:
                break

            all_mrs.extend(mrs_data)

            # 达到 batch_size 限制
            if len(all_mrs) >= sync_config.batch_size:
                all_mrs = all_mrs[:sync_config.batch_size]
                break

            # 检查是否还有更多页
            if len(mrs_data) < min(sync_config.batch_size, 100):
                break

            page += 1

        logger.info(f"获取到 {len(all_mrs)} 个 MRs, 不可恢复错误数: {len(unrecoverable_errors)}")

        if not all_mrs:
            logger.info("无新 MRs 需要同步")
            result["success"] = True
            result["message"] = "无新 MRs 需要同步"
            return result

        # 4. 对每个 MR 拉取 discussions/notes/approvals
        total_events_synced = 0
        total_events_skipped = 0
        # 跟踪最新的 (updated_at, iid) 对
        new_last_mr_updated_at = last_mr_updated_at
        new_last_mr_iid = last_mr_iid
        
        # MR 处理计数（用于续租）
        mr_processed_count = 0

        for mr_data in all_mrs:
            # 分段续租：每处理 renew_interval_mrs 个 MR 后续租一次
            mr_processed_count += 1
            if mr_processed_count % renew_interval_mrs == 0:
                try:
                    renewed = scm_sync_lock.renew(
                        repo_id=repo_id,
                        job_type=JOB_TYPE_GITLAB_REVIEWS,
                        worker_id=worker_id,
                        conn=conn,
                    )
                    if renewed:
                        logger.debug(
                            f"[run_id={run_id[:8]}] 续租成功 "
                            f"(已处理 {mr_processed_count}/{len(all_mrs)} MRs)"
                        )
                    else:
                        logger.warning(
                            f"[run_id={run_id[:8]}] 续租失败，锁可能已被抢占"
                        )
                except Exception as e:
                    logger.warning(f"[run_id={run_id[:8]}] 续租时发生异常: {e}")
            mr_iid = mr_data.get("iid")
            mr_updated_at = mr_data.get("updated_at")
            
            if verbose:
                logger.debug(f"处理 MR !{mr_iid}: {mr_data.get('title', '')[:50]}")

            # 创建或更新 MR 记录
            mr_id = get_or_create_mr(conn, repo_id, mr_data, sync_config.gitlab_url)

            # 获取已存在的事件 ID
            existing_event_ids = get_existing_event_ids(conn, mr_id)

            all_events = []
            mr_had_unrecoverable_error = False  # 当前 MR 是否遇到不可恢复错误

            # 4.1 获取 notes
            try:
                notes_page = 1
                while True:
                    notes = client.get_mr_notes(
                        sync_config.project_id,
                        mr_iid,
                        per_page=100,
                        page=notes_page,
                    )
                    if not notes:
                        break
                    
                    events = extract_events_from_notes(mr_id, notes, existing_event_ids)
                    all_events.extend(events)
                    
                    # 更新已知事件 ID 避免重复
                    existing_event_ids.update(e.source_event_id for e in events)
                    
                    if len(notes) < 100:
                        break
                    notes_page += 1

            except GitLabAPIError as e:
                error_info = classify_gitlab_error(e, "get_mr_notes", {"mr_iid": mr_iid})
                logger.warning(f"获取 MR !{mr_iid} notes 失败: {e.message}, category={error_info['error_category']}")
                
                if error_info["is_unrecoverable"]:
                    unrecoverable_errors.append(error_info)
                    mr_had_unrecoverable_error = True

            # 4.2 获取 approvals
            try:
                approvals = client.get_mr_approvals(sync_config.project_id, mr_iid)
                events = extract_events_from_approvals(mr_id, approvals, existing_event_ids)
                all_events.extend(events)
                existing_event_ids.update(e.source_event_id for e in events)

            except GitLabAPIError as e:
                error_info = classify_gitlab_error(e, "get_mr_approvals", {"mr_iid": mr_iid})
                logger.warning(f"获取 MR !{mr_iid} approvals 失败: {e.message}, category={error_info['error_category']}")
                
                if error_info["is_unrecoverable"]:
                    unrecoverable_errors.append(error_info)
                    mr_had_unrecoverable_error = True

            # 4.3 获取 state events
            try:
                state_events_page = 1
                while True:
                    state_events = client.get_mr_resource_state_events(
                        sync_config.project_id,
                        mr_iid,
                        per_page=100,
                        page=state_events_page,
                    )
                    if not state_events:
                        break
                    
                    events = extract_events_from_state_events(mr_id, state_events, existing_event_ids)
                    all_events.extend(events)
                    existing_event_ids.update(e.source_event_id for e in events)
                    
                    if len(state_events) < 100:
                        break
                    state_events_page += 1

            except GitLabAPIError as e:
                error_info = classify_gitlab_error(e, "get_mr_resource_state_events", {"mr_iid": mr_iid})
                logger.warning(f"获取 MR !{mr_iid} state events 失败: {e.message}, category={error_info['error_category']}")
                
                if error_info["is_unrecoverable"]:
                    unrecoverable_errors.append(error_info)
                    mr_had_unrecoverable_error = True

            # 5. 写入事件
            if all_events:
                insert_stats = insert_review_events(conn, all_events)
                total_events_synced += insert_stats.inserted
                total_events_skipped += insert_stats.skipped
                if verbose:
                    logger.debug(f"MR !{mr_iid}: 新增 {insert_stats.inserted} 个事件, 跳过 {insert_stats.skipped} 个")

            # 使用单调递增规则更新最新的水位线
            if mr_updated_at:
                if should_advance_mr_cursor(
                    mr_updated_at, mr_iid, new_last_mr_updated_at, new_last_mr_iid
                ):
                    new_last_mr_updated_at = mr_updated_at
                    new_last_mr_iid = mr_iid

        logger.info(f"写入 review_events: 新增 {total_events_synced} 条, 跳过 {total_events_skipped} 条")
        result["synced_mr_count"] = len(all_mrs)
        result["synced_event_count"] = total_events_synced
        result["skipped_event_count"] = total_events_skipped
        
        # 记录不可恢复的错误到结果
        result["unrecoverable_errors"] = unrecoverable_errors

        # 6. 更新游标（使用单调递增规则）
        # 检查是否有不可恢复的错误
        encountered_unrecoverable_error = len(unrecoverable_errors) > 0
        
        cursor_advance_reason: Optional[str] = None
        should_advance = True
        
        if sync_config.strict and encountered_unrecoverable_error:
            # strict 模式：不推进游标
            should_advance = False
            # 构建详细的停止原因
            error_categories = list(set(
                err.get("error_category", "unknown") 
                for err in unrecoverable_errors
            ))
            cursor_advance_reason = f"strict_mode:unrecoverable_error_encountered:categories={','.join(error_categories)}"
            result["cursor_advance_stopped_at"] = f"!{last_mr_iid}" if last_mr_iid else "start"
            logger.warning(
                f"[strict mode] 遇到不可恢复的错误，游标不推进。"
                f"错误类型: {', '.join(error_categories)}, 错误数: {len(unrecoverable_errors)}"
            )
        elif encountered_unrecoverable_error:
            # best_effort 模式：推进游标但记录缺失类型
            missing_types = list(set(
                err.get("error_category", "unknown") 
                for err in unrecoverable_errors
            ))
            result["missing_types"] = missing_types
            cursor_advance_reason = f"best_effort_with_errors:degraded={','.join(missing_types)}"
            logger.warning(
                f"[best_effort mode] 遇到不可恢复的错误但仍推进游标，"
                f"缺失类型: {','.join(missing_types)}, 错误数: {len(unrecoverable_errors)}"
            )
        else:
            cursor_advance_reason = "batch_complete"
        
        # 仅在 should_update_cursor=True 且单调递增时推进
        if should_advance and should_update_cursor and new_last_mr_updated_at and should_advance_mr_cursor(
            new_last_mr_updated_at,
            new_last_mr_iid or 0,
            last_mr_updated_at,
            last_mr_iid,
        ):
            update_sync_cursor(
                repo_id,
                new_last_mr_updated_at,
                new_last_mr_iid,
                len(all_mrs),
                total_events_synced,
                config=config,
            )
            # 标准化时间戳用于返回结果
            result["last_mr_updated_at"] = normalize_iso_ts_z(new_last_mr_updated_at) or new_last_mr_updated_at
            result["last_mr_iid"] = new_last_mr_iid
            result["watermark_updated"] = True
        elif should_advance and not should_update_cursor:
            cursor_advance_reason = "backfill_mode:update_watermark=false"
            result["watermark_updated"] = False
            logger.info(
                f"回填模式: 跳过游标更新 (update_watermark=False)"
            )
        elif should_advance:
            cursor_advance_reason = "watermark_unchanged"
            result["watermark_updated"] = False
            logger.debug(
                f"游标未推进: 新值 ({new_last_mr_updated_at}, {new_last_mr_iid}) "
                f"<= 旧值 ({last_mr_updated_at}, {last_mr_iid})"
            )
        elif not all_mrs and encountered_unrecoverable_error:
            # 没有成功获取任何 MR 且遇到不可恢复错误
            if sync_config.strict:
                cursor_advance_reason = "strict_no_success:api_call_failed"
                result["cursor_advance_stopped_at"] = f"!{last_mr_iid}" if last_mr_iid else "start"
            else:
                missing_types = list(set(
                    err.get("error_category", "unknown") 
                    for err in unrecoverable_errors
                ))
                result["missing_types"] = missing_types
                cursor_advance_reason = f"best_effort_no_data:errors={','.join(missing_types)}"
        
        result["cursor_advance_reason"] = cursor_advance_reason

        result["success"] = True
        result["has_more"] = len(all_mrs) >= sync_config.batch_size
        
        # 更新 limiter 统计到 ClientStats
        client.update_stats_with_limiter_info()
        result["request_stats"] = client.stats.to_dict()

        # 7. 创建 logbook item/event（与 commits 同风格）
        total_events_synced = result.get("synced_event_count", 0)
        if len(all_mrs) > 0:
            try:
                first_mr = all_mrs[0] if all_mrs else None
                last_mr = all_mrs[-1] if all_mrs else None
                mr_range = ""
                if first_mr and last_mr:
                    mr_range = f" !{first_mr.get('iid', 0)}..!{last_mr.get('iid', 0)}"

                # 创建同步批次 logbook item（包含 run_id）
                batch_title = f"GitLab Reviews Sync Batch{mr_range} (repo_id={repo_id})"
                item_id = create_item(
                    item_type="scm_sync_batch",
                    title=batch_title,
                    scope_json={
                        "run_id": run_id,
                        "source_type": "review",
                        "repo_id": repo_id,
                        "synced_mr_count": len(all_mrs),
                        "synced_event_count": total_events_synced,
                    },
                    status="done",
                    config=config,
                )
                result["logbook_item_id"] = item_id
                logger.debug(f"[run_id={run_id[:8]}] 创建同步批次 logbook item: item_id={item_id}")

                # 添加 sync_completed 事件（包含请求统计）
                event_payload = {
                    "run_id": run_id,
                    "synced_mr_count": len(all_mrs),
                    "synced_event_count": total_events_synced,
                    "skipped_event_count": result.get("skipped_event_count", 0),
                    # 请求统计
                    "request_stats": client.stats.to_dict(),
                }
                add_event(
                    item_id=item_id,
                    event_type="sync_completed",
                    payload_json=event_payload,
                    status_from="running",
                    status_to="done",
                    source="scm_sync_gitlab_reviews",
                    config=config,
                )
            except Exception as e:
                # logbook 写入失败不影响同步主流程
                logger.warning(f"创建 logbook 记录失败 (非致命): {e}")
                result["logbook_error"] = str(e)

    except EngramError:
        raise
    except Exception as e:
        logger.exception(f"同步过程中发生错误: {e}")
        result["error"] = str(e)
        raise GitLabReviewSyncError(
            f"GitLab Reviews 同步失败: {e}",
            {"error": str(e)},
        )
    finally:
        # 释放分布式锁（确保在任何情况下都释放）
        if lock_acquired and lock_repo_id is not None:
            try:
                released = scm_sync_lock.release(
                    repo_id=lock_repo_id,
                    job_type=JOB_TYPE_GITLAB_REVIEWS,
                    worker_id=worker_id,
                    conn=conn,
                )
                if released:
                    logger.debug(f"[run_id={run_id[:8]}] 成功释放锁 (repo_id={lock_repo_id})")
                else:
                    logger.warning(f"[run_id={run_id[:8]}] 释放锁失败或锁已被抢占 (repo_id={lock_repo_id})")
            except Exception as e:
                logger.warning(f"[run_id={run_id[:8]}] 释放锁时发生异常 (非致命): {e}")
        
        # 记录 sync_run 结束（确保即使失败或无数据也写入记录）
        if sync_run_started and sync_run_conn:
            try:
                # 确定最终状态
                if result.get("error"):
                    final_status = "failed"
                elif result.get("synced_mr_count", 0) == 0:
                    final_status = "no_data"
                else:
                    final_status = "completed"
                
                # 构建游标后快照
                cursor_after = None
                if result.get("last_mr_updated_at"):
                    cursor_after = {
                        "last_mr_updated_at": result.get("last_mr_updated_at"),
                        "last_mr_iid": result.get("last_mr_iid"),
                    }
                
                # 构建计数统计（包含 limiter stats）
                request_stats = result.get("request_stats", {})
                counts = {
                    "synced_mr_count": result.get("synced_mr_count", 0),
                    "synced_event_count": result.get("synced_event_count", 0),
                    "skipped_event_count": result.get("skipped_event_count", 0),
                    # limiter 统计字段（稳定、向后兼容）
                    "total_requests": request_stats.get("total_requests", 0),
                    "total_429_hits": request_stats.get("total_429_hits", 0),
                    "timeout_count": request_stats.get("timeout_count", 0),
                    "avg_wait_time_ms": request_stats.get("avg_wait_time_ms", 0),
                }
                
                # 构建错误摘要
                error_summary = None
                if result.get("error"):
                    error_summary = {"message": result.get("error")}
                
                insert_sync_run_finish(
                    conn=sync_run_conn,
                    run_id=run_id,
                    status=final_status,
                    cursor_after=cursor_after,
                    counts=counts,
                    error_summary_json=error_summary,
                    logbook_item_id=result.get("logbook_item_id"),
                )
                sync_run_conn.commit()
            except Exception as e:
                logger.warning(f"记录 sync_run 结束失败 (非致命): {e}")
        
        conn.close()

    return result


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="GitLab MR Reviews 同步脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 使用配置文件同步
    python scm_sync_gitlab_reviews.py --config config.toml

    # 指定项目和 token
    python scm_sync_gitlab_reviews.py --gitlab-url https://gitlab.com --project-id 12345 --token glpat-xxx

    # 循环同步直到完成
    python scm_sync_gitlab_reviews.py --loop
        """,
    )

    add_config_argument(parser)

    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help=f"每次同步的最大 MR 数 (默认: {DEFAULT_BATCH_SIZE})",
    )

    parser.add_argument(
        "--gitlab-url",
        type=str,
        default=None,
        help="GitLab 实例 URL（覆盖配置文件）",
    )

    parser.add_argument(
        "--project-id",
        type=str,
        default=None,
        help="GitLab 项目 ID 或路径（覆盖配置文件）",
    )

    parser.add_argument(
        "--token",
        type=str,
        default=None,
        help="GitLab Private Token（覆盖配置文件）",
    )

    parser.add_argument(
        "--loop",
        action="store_true",
        help="循环同步直到全部完成",
    )

    parser.add_argument(
        "--strict",
        action="store_true",
        help="严格模式：发生不可恢复的 API 错误时，不推进游标",
    )

    parser.add_argument(
        "--sync-mode",
        type=str,
        choices=["strict", "best_effort"],
        default=None,
        help="同步模式: strict=严格模式(错误时不推进游标), best_effort=尽力模式(错误时记录并推进，默认)",
    )

    parser.add_argument(
        "--backfill-hours",
        type=float,
        default=None,
        help="回填模式：从 (当前时间 - 指定小时数) 开始扫描，强制启用幂等路径",
    )

    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="回填模式：从指定的 ISO 时间开始扫描，如 2024-01-01T00:00:00Z",
    )

    parser.add_argument(
        "--until",
        type=str,
        default=None,
        help="回填模式：截止到指定的 ISO 时间，如 2024-01-31T23:59:59Z（可选，默认为当前时间）",
    )

    parser.add_argument(
        "--no-update-cursor",
        action="store_true",
        help="回填模式下不更新游标（默认：回填时不更新游标）",
    )

    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="显示详细输出",
    )

    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="静默模式，只显示错误",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果",
    )

    return parser.parse_args()


def main() -> int:
    """主入口"""
    args = parse_args()

    # 设置日志级别
    if args.quiet:
        logging.getLogger().setLevel(logging.ERROR)
    elif args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        # 加载配置
        config = get_config(args.config_path)
        config.load()

        # 获取 GitLab 配置（使用兼容读取，优先 scm.gitlab.* 回退 gitlab.*）
        gitlab_cfg = get_gitlab_config(config)

        # CLI 参数覆盖配置文件
        gitlab_url = args.gitlab_url or gitlab_cfg.get("url")
        project_id = args.project_id or gitlab_cfg.get("project_id")
        private_token = args.token or gitlab_cfg.get("private_token")

        if not gitlab_url:
            raise ValidationError(
                "缺少 GitLab URL，请在配置文件中设置 scm.gitlab.url 或使用 --gitlab-url 参数",
                {"hint": "配置示例: [scm.gitlab]\\nurl = \"https://gitlab.example.com\""},
            )

        if not project_id:
            raise ValidationError(
                "缺少 GitLab 项目 ID，请在配置文件中设置 scm.gitlab.project 或使用 --project-id 参数",
                {"hint": "配置示例: [scm.gitlab]\\nproject = 123  # 或 \"namespace/project\""},
            )

        # 创建 token provider（支持 env/file/exec 等多种方式）
        token_provider = create_gitlab_token_provider(config, private_token)

        # 获取 project_key
        project_key = config.get("project.project_key", "default")

        # 获取增量同步配置
        incremental_cfg = get_incremental_config(config)

        # 解析 strict 模式（使用统一的配置函数）
        cli_sync_override = None
        if args.strict:
            cli_sync_override = "strict"
        elif args.sync_mode:
            cli_sync_override = args.sync_mode
        
        strict = is_strict_mode(config, cli_sync_override)

        # 构建同步配置
        sync_config = SyncConfig(
            gitlab_url=gitlab_url,
            project_id=str(project_id),
            token_provider=token_provider,
            batch_size=args.batch_size or gitlab_cfg.get("batch_size") or DEFAULT_BATCH_SIZE,
            request_timeout=gitlab_cfg.get("request_timeout") or DEFAULT_REQUEST_TIMEOUT,
            overlap_seconds=incremental_cfg.get("overlap_seconds", 300),
            strict=strict,
        )

        logger.info(f"GitLab URL: {sync_config.gitlab_url}")
        logger.info(f"Project ID: {sync_config.project_id}")
        logger.info(f"Batch size: {sync_config.batch_size}")
        logger.info(f"Overlap seconds: {sync_config.overlap_seconds}")

        # 计算回填起始/截止时间
        backfill_since = None
        backfill_until = None
        if args.backfill_hours is not None:
            backfill_since = (datetime.utcnow() - timedelta(hours=args.backfill_hours)).isoformat() + "Z"
            logger.info(f"回填模式: --backfill-hours={args.backfill_hours}, 起始时间={backfill_since}")
        elif args.since:
            backfill_since = args.since
            logger.info(f"回填模式: --since={backfill_since}")
        
        # 处理 --until 参数（仅在回填模式下有效）
        if hasattr(args, 'until') and args.until:
            backfill_until = args.until
            logger.info(f"回填模式: --until={backfill_until}")
        
        # 确定是否更新 watermark（回填模式默认不更新，除非显式指定）
        is_backfill_mode = backfill_since is not None
        update_watermark = True
        if is_backfill_mode:
            # 回填模式默认不更新，使用 --no-update-cursor 可显式禁用
            update_watermark = not getattr(args, 'no_update_cursor', True)

        # 执行同步
        total_mr_synced = 0
        total_events_synced = 0
        total_events_skipped = 0
        loop_count = 0
        max_loops = 1000  # 防止无限循环

        while True:
            loop_count += 1
            if loop_count > max_loops:
                logger.warning(f"达到最大循环次数限制 ({max_loops})，退出")
                break

            result = sync_gitlab_reviews(
                sync_config,
                project_key,
                config,
                verbose=args.verbose,
                backfill_since=backfill_since,
                backfill_until=backfill_until,
                update_watermark=update_watermark,
            )
            total_mr_synced += result.get("synced_mr_count", 0)
            total_events_synced += result.get("synced_event_count", 0)
            total_events_skipped += result.get("skipped_event_count", 0)

            if args.json:
                print(json.dumps(result, default=str, ensure_ascii=False))

            if not args.loop or not result.get("has_more", False):
                break

            logger.info(f"继续同步下一批（第 {loop_count + 1} 轮）...")

        # 输出统计
        if not args.json:
            if backfill_since:
                logger.info(f"回填完成: 扫描 {total_mr_synced} 个 MR, 新增事件 {total_events_synced} 条, 跳过事件 {total_events_skipped} 条")
            elif args.loop:
                logger.info(f"同步完成，共 {loop_count} 轮，总计 {total_mr_synced} 个 MRs，{total_events_synced} 个事件")

        return 0

    except EngramError as e:
        if args.json:
            print(json.dumps(e.to_dict(), default=str, ensure_ascii=False))
        else:
            logger.error(f"{e.error_type}: {e.message}")
            if args.verbose and e.details:
                logger.error(f"详情: {e.details}")
        return e.exit_code

    except Exception as e:
        logger.exception(f"未预期的错误: {e}")
        if args.json:
            print(json.dumps({
                "error": True,
                "type": "UNEXPECTED_ERROR",
                "message": str(e),
            }, default=str, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
