#!/usr/bin/env python3
"""
scm_sync_gitlab_mrs.py - GitLab Merge Requests 同步脚本

功能:
- 使用 GitLab REST API 拉取 merge_requests
- 从 [updated_after] 范围增量同步（分页支持）
- 获取每个 MR 的详细信息
- 写入 scm.mrs（mr_id 采用 scm_repo.build_mr_id(repo_id, iid) 格式）
- 使用统一的游标模块管理同步进度（支持 v1→v2 自动升级）

游标结构 (v2):
    watermark: {last_mr_updated_at, last_mr_iid}
    stats: {last_sync_at, last_sync_count}

使用:
    python scm_sync_gitlab_mrs.py [--config PATH] [--batch-size N] [--verbose]

配置文件示例:
    [gitlab]
    url = "https://gitlab.example.com"
    project_id = 123                   # 或 "namespace/project"
    private_token = "glpat-xxx"
    batch_size = 100                   # 每次同步最大 MR 数
"""

import argparse
import json
import logging
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

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
    load_gitlab_mr_cursor,
    save_gitlab_mr_cursor,
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

# [DEPRECATED] 旧的 KV 命名空间和键名，已迁移到 engram_step1.cursor 模块
# 保留用于参考，实际游标读写使用 load_gitlab_mr_cursor/save_gitlab_mr_cursor
# KV_NAMESPACE = "scm.sync"
# KV_KEY_PREFIX = "gitlab_mr_cursor:"  # 旧格式 gitlab_mr_cursor:<repo_id>

# 默认配置值
DEFAULT_BATCH_SIZE = 100
DEFAULT_REQUEST_TIMEOUT = 60
DEFAULT_LOCK_LEASE_SECONDS = 120  # 默认锁租约时间（秒）

# physical_job_type 常量
# 使用 physical_job_type 确保同一语义任务在队列/锁表中有唯一键
# 参见 engram_step1.scm_sync_job_types 模块
JOB_TYPE_GITLAB_MRS = "gitlab_mrs"  # physical_job_type: GitLab Merge Requests


def _generate_worker_id() -> str:
    """生成唯一的 worker ID（基于 hostname + pid + uuid 片段）"""
    import socket
    import os
    hostname = socket.gethostname()[:16]
    pid = os.getpid()
    short_uuid = str(uuid.uuid4())[:8]
    return f"{hostname}-{pid}-{short_uuid}"


@dataclass
class GitLabMergeRequest:
    """GitLab Merge Request 数据结构"""
    iid: int                                # MR 在项目内的 ID
    project_id: int                         # 项目 ID
    title: str
    description: str
    state: str                              # opened/closed/merged/locked
    author: Dict[str, Any]
    source_branch: str
    target_branch: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    merged_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    web_url: str = ""
    merge_commit_sha: Optional[str] = None
    sha: Optional[str] = None               # 最新 commit SHA
    labels: List[str] = field(default_factory=list)
    assignees: List[Dict[str, Any]] = field(default_factory=list)
    reviewers: List[Dict[str, Any]] = field(default_factory=list)
    draft: bool = False
    work_in_progress: bool = False
    changes_count: Optional[str] = None
    user_notes_count: int = 0
    upvotes: int = 0
    downvotes: int = 0
    raw_data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SyncConfig:
    """同步配置"""
    gitlab_url: str
    project_id: str  # 可以是数字 ID 或 namespace/project 格式
    token_provider: TokenProvider  # 使用 TokenProvider 替代 private_token
    batch_size: int = DEFAULT_BATCH_SIZE
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT
    state_filter: Optional[str] = None  # all/opened/closed/merged
    overlap_seconds: int = 300  # 向前重叠的秒数，用于防止边界丢失
    strict: bool = False  # 严格模式：不可恢复的错误时不推进游标
    # Tenant 维度限流配置
    tenant_id: Optional[str] = None  # 租户 ID，用于 tenant 维度的限流


class GitLabSyncError(EngramError):
    """GitLab 同步错误"""
    exit_code = 11
    error_type = "GITLAB_SYNC_ERROR"


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


def classify_gitlab_error(error: Exception) -> Dict[str, Any]:
    """
    分类 GitLab 错误，返回结构化错误信息
    
    Args:
        error: 捕获的异常
        
    Returns:
        包含 error_category, status_code, message, is_unrecoverable 的字典
    """
    error_info = {
        "error_category": "unknown",
        "status_code": None,
        "message": str(error),
        "is_unrecoverable": False,
    }
    
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


# [DEPRECATED] get_last_sync_cursor 和 update_sync_cursor 已被 load_gitlab_mr_cursor/save_gitlab_mr_cursor 取代
# 旧函数使用 KV_KEY_PREFIX 直接操作 kv 表，新函数使用统一的 cursor 模块，支持 v1→v2 自动升级


def parse_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    """解析 ISO 8601 格式的时间字符串"""
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except ValueError:
        logger.warning(f"无法解析时间: {dt_str}")
        return None


def parse_merge_request(data: Dict[str, Any]) -> GitLabMergeRequest:
    """
    解析 GitLab API 返回的 merge request 数据

    Args:
        data: API 返回的 merge request JSON

    Returns:
        GitLabMergeRequest 对象
    """
    return GitLabMergeRequest(
        iid=data["iid"],
        project_id=data["project_id"],
        title=data.get("title", ""),
        description=data.get("description") or "",
        state=data.get("state", "unknown"),
        author=data.get("author") or {},
        source_branch=data.get("source_branch", ""),
        target_branch=data.get("target_branch", ""),
        created_at=parse_datetime(data.get("created_at")),
        updated_at=parse_datetime(data.get("updated_at")),
        merged_at=parse_datetime(data.get("merged_at")),
        closed_at=parse_datetime(data.get("closed_at")),
        web_url=data.get("web_url", ""),
        merge_commit_sha=data.get("merge_commit_sha"),
        sha=data.get("sha"),
        labels=data.get("labels") or [],
        assignees=data.get("assignees") or [],
        reviewers=data.get("reviewers") or [],
        draft=data.get("draft", False),
        work_in_progress=data.get("work_in_progress", False),
        changes_count=data.get("changes_count"),
        user_notes_count=data.get("user_notes_count", 0),
        upvotes=data.get("upvotes", 0),
        downvotes=data.get("downvotes", 0),
        raw_data=data,
    )


def map_gitlab_state_to_status(state: str) -> str:
    """
    将 GitLab MR 状态映射到 scm.mrs 的 status

    Args:
        state: GitLab MR 状态 (opened/closed/merged/locked)

    Returns:
        映射后的状态
    """
    state_mapping = {
        "opened": "open",
        "closed": "closed",
        "merged": "merged",
        "locked": "locked",
    }
    return state_mapping.get(state, state)


@dataclass
class MRInsertStats:
    """MR 插入统计结果"""
    total: int = 0       # 总共处理的 MR 数
    inserted: int = 0    # 新插入的 MR 数
    updated: int = 0     # 更新的 MR 数


def insert_merge_requests(
    conn: psycopg.Connection,
    repo_id: int,
    mrs: List[GitLabMergeRequest],
) -> MRInsertStats:
    """
    将 Merge Requests 插入数据库

    使用 ON CONFLICT DO UPDATE 实现 upsert

    Args:
        conn: 数据库连接
        repo_id: 仓库 ID
        mrs: GitLabMergeRequest 列表

    Returns:
        MRInsertStats 统计结果
    """
    stats = MRInsertStats()
    if not mrs:
        return stats

    stats.total = len(mrs)
    with conn.cursor() as cur:
        for mr in mrs:
            # 构建全局唯一的 mr_id（基于 repo_id）
            mr_id = build_mr_id(repo_id, mr.iid)
            # 构建 source_id 用于统一标识
            source_id = build_mr_source_id(repo_id, mr.iid)

            # 构建 meta_json（存储详细信息）
            meta_json = {
                "iid": mr.iid,
                "project_id": mr.project_id,
                "title": mr.title,
                "description": mr.description,
                "source_branch": mr.source_branch,
                "target_branch": mr.target_branch,
                "author": mr.author,
                "assignees": mr.assignees,
                "reviewers": mr.reviewers,
                "labels": mr.labels,
                "draft": mr.draft,
                "work_in_progress": mr.work_in_progress,
                "merge_commit_sha": mr.merge_commit_sha,
                "sha": mr.sha,
                "changes_count": mr.changes_count,
                "user_notes_count": mr.user_notes_count,
                "upvotes": mr.upvotes,
                "downvotes": mr.downvotes,
                "created_at": mr.created_at.isoformat() if mr.created_at else None,
                "merged_at": mr.merged_at.isoformat() if mr.merged_at else None,
                "closed_at": mr.closed_at.isoformat() if mr.closed_at else None,
            }

            # 解析作者身份并填充到 meta_json
            author_username = mr.author.get("username") if mr.author else None
            author_email = mr.author.get("email") if mr.author else None
            author_display = mr.author.get("name") if mr.author else None
            meta_json = resolve_and_enrich_meta(
                meta_json,
                account_type="gitlab",
                username=author_username,
                email=author_email,
                display=author_display,
            )

            # 获取解析后的 author_user_id
            identity_resolved = meta_json.get("identity_resolved", {})
            author_user_id = identity_resolved.get("resolved_user_id")

            # 映射状态
            status = map_gitlab_state_to_status(mr.state)

            try:
                cur.execute(
                    """
                    INSERT INTO scm.mrs
                        (mr_id, repo_id, author_user_id, status, url, meta_json, source_id, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (mr_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        url = EXCLUDED.url,
                        meta_json = EXCLUDED.meta_json,
                        source_id = EXCLUDED.source_id,
                        updated_at = EXCLUDED.updated_at
                    RETURNING (xmax = 0) AS is_insert
                    """,
                    (
                        mr_id,
                        repo_id,
                        author_user_id,
                        status,
                        mr.web_url,
                        json.dumps(meta_json, ensure_ascii=False, default=str),
                        source_id,
                        mr.created_at,
                        mr.updated_at or datetime.utcnow(),
                    ),
                )
                row = cur.fetchone()
                if row and row[0]:
                    stats.inserted += 1
                else:
                    stats.updated += 1
            except psycopg.Error as e:
                logger.error(f"插入 MR {mr_id} 失败: {e}")
                raise DatabaseError(
                    f"插入 Merge Request 失败: {e}",
                    {"mr_id": mr_id, "error": str(e)},
                )

    conn.commit()
    return stats


def sync_gitlab_mrs(
    sync_config: SyncConfig,
    project_key: str,
    config: Optional[Config] = None,
    verbose: bool = True,
    fetch_details: bool = False,
    backfill_since: Optional[str] = None,
    backfill_until: Optional[str] = None,
    update_watermark: bool = True,
    lock_lease_seconds: int = DEFAULT_LOCK_LEASE_SECONDS,
) -> Dict[str, Any]:
    """
    执行 GitLab Merge Requests 同步

    Args:
        sync_config: 同步配置
        project_key: 项目标识
        config: Config 实例
        verbose: 是否输出详细信息
        fetch_details: 是否获取每个 MR 的详细信息
        backfill_since: 回填模式起始时间（ISO 格式），覆盖游标
        backfill_until: 回填模式截止时间（ISO 格式），可选，默认为当前时间
        update_watermark: 是否更新游标（回填模式下默认为 False）
        lock_lease_seconds: 锁租约时长（秒），默认 120 秒

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
        "synced_count": 0,
        "skipped_count": 0,
        "updated_after": None,
        "backfill_mode": backfill_since is not None,
        "error": None,
        "error_category": None,  # 标准字段：错误分类
        "retry_after": None,  # 标准字段：建议的重试等待秒数（可选）
        "counts": {},  # 标准字段：计数统计
        "cursor_advance_reason": None,  # 游标推进原因
        "cursor_advance_stopped_at": None,  # strict 模式下游标停止的位置
        "unrecoverable_errors": [],  # 不可恢复的错误列表
        "missing_types": [],  # best_effort 模式下记录的缺失类型
        "strict_mode": sync_config.strict,  # 是否启用严格模式
        "sync_mode": SCM_SYNC_MODE_STRICT if sync_config.strict else SCM_SYNC_MODE_BEST_EFFORT,
        "locked": False,  # 是否因锁被跳过
        "skipped": False,  # 是否被跳过
    }
    
    logger.info(f"[run_id={run_id[:8]}] 开始 GitLab MRs 同步")
    
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
        tenant_id=sync_config.tenant_id,  # 传入 tenant_id 用于 tenant 维度限流
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
            job_type=JOB_TYPE_GITLAB_MRS,
            worker_id=worker_id,
            lease_seconds=lock_lease_seconds,
            conn=conn,
        )
        if not lock_acquired:
            # 锁被其他 worker 持有，返回 locked/skip 结果
            lock_info = scm_sync_lock.get(repo_id, JOB_TYPE_GITLAB_MRS, conn=conn)
            logger.warning(
                f"[run_id={run_id[:8]}] 锁被其他 worker 持有，跳过本次同步 "
                f"(repo_id={repo_id}, job_type={JOB_TYPE_GITLAB_MRS}, "
                f"locked_by={lock_info.get('locked_by') if lock_info else 'unknown'})"
            )
            result["locked"] = True
            result["skipped"] = True
            result["success"] = True  # locked/skipped 视为成功（不需要重试）
            result["message"] = "锁被其他 worker 持有，跳过本次同步"
            result["error_category"] = "lock_held"
            result["counts"] = {"synced_count": 0, "scanned_count": 0, "inserted_count": 0, "skipped_count": 0}
            return result
        
        logger.debug(f"[run_id={run_id[:8]}] 成功获取锁 (repo_id={repo_id}, worker_id={worker_id})")

        # 2. 获取上次同步的游标（使用统一的 cursor 模块，自动处理 v1→v2 升级）
        cursor = load_gitlab_mr_cursor(repo_id, config)
        last_mr_updated_at = cursor.last_mr_updated_at
        last_mr_iid = cursor.last_mr_iid

        # 保存游标快照用于 sync_run 记录
        sync_run_cursor_before = {
            "last_mr_updated_at": last_mr_updated_at,
            "last_mr_iid": last_mr_iid,
        }
        sync_run_repo_id = repo_id
        sync_run_conn = conn
        
        # 记录 sync_run 开始
        try:
            insert_sync_run_start(
                conn=conn,
                run_id=run_id,
                repo_id=repo_id,
                job_type="gitlab_mrs",
                mode="backfill" if backfill_since else "incremental",
                cursor_before=sync_run_cursor_before,
                meta_json={
                    "batch_size": sync_config.batch_size,
                    "state_filter": sync_config.state_filter,
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

        result["updated_after"] = updated_after
        result["last_mr_iid"] = last_mr_iid

        # 3. 拉取 merge requests
        logger.info(f"从 GitLab 获取 MRs (project_id={sync_config.project_id}, updated_after={updated_after})")

        all_mrs = []
        unrecoverable_errors: List[Dict[str, Any]] = []
        page = 1
        api_call_failed = False  # 标记关键 API 调用是否失败
        
        while True:
            try:
                mrs_data = client.get_merge_requests(
                    sync_config.project_id,
                    updated_after=updated_after,
                    state=sync_config.state_filter or "all",
                    per_page=min(sync_config.batch_size, 100),
                    page=page,
                    order_by="updated_at",
                    sort="asc",  # 按更新时间升序，确保游标正确
                )
            except GitLabAPIError as e:
                error_info = classify_gitlab_error(e)
                error_info["api_call"] = "get_merge_requests"
                error_info["page"] = page
                logger.error(f"获取 MRs 列表失败 (page={page}): {e.message}, category={error_info['error_category']}")
                
                if error_info["is_unrecoverable"]:
                    unrecoverable_errors.append(error_info)
                    api_call_failed = True
                    # strict 模式下遇到不可恢复错误立即停止
                    if sync_config.strict:
                        logger.warning(f"[strict mode] get_merge_requests 遇到不可恢复错误，停止同步")
                        break
                # best_effort 模式下继续尝试下一页或停止
                break

            if not mrs_data:
                break

            for data in mrs_data:
                mr = parse_merge_request(data)

                # 如果需要详细信息，调用 detail endpoint
                if fetch_details:
                    try:
                        detail_data = client.get_merge_request_detail(
                            sync_config.project_id, mr.iid
                        )
                        mr = parse_merge_request(detail_data)
                    except GitLabAPIError as e:
                        error_info = classify_gitlab_error(e)
                        error_info["api_call"] = "get_merge_request_detail"
                        error_info["mr_iid"] = mr.iid
                        logger.warning(f"获取 MR 详情失败 (iid={mr.iid}): {e.message}, category={error_info['error_category']}")
                        
                        if error_info["is_unrecoverable"]:
                            unrecoverable_errors.append(error_info)
                            # strict 模式下记录但不立即停止（detail 是可选的）
                            # 但仍然记录错误供游标判断使用

                all_mrs.append(mr)

            # 达到 batch_size 限制
            if len(all_mrs) >= sync_config.batch_size:
                all_mrs = all_mrs[:sync_config.batch_size]
                break

            # 检查是否还有更多页
            if len(mrs_data) < min(sync_config.batch_size, 100):
                break

            page += 1

        # 记录不可恢复的错误到结果
        result["unrecoverable_errors"] = unrecoverable_errors
        
        logger.info(f"获取到 {len(all_mrs)} 个 MRs, 不可恢复错误数: {len(unrecoverable_errors)}")

        if not all_mrs:
            logger.info("无新 MRs 需要同步")
            result["success"] = True
            result["message"] = "无新 MRs 需要同步"
            result["counts"] = {"synced_count": 0, "scanned_count": 0, "inserted_count": 0, "skipped_count": 0}
            return result

        # 4. 写入 mrs 表
        insert_stats = insert_merge_requests(conn, repo_id, all_mrs)
        logger.info(f"写入 scm.mrs: 扫描 {insert_stats.total} 条, 新增 {insert_stats.inserted} 条, 更新 {insert_stats.updated} 条")
        result["synced_count"] = insert_stats.inserted + insert_stats.updated
        result["scanned_count"] = insert_stats.total
        result["inserted_count"] = insert_stats.inserted
        result["skipped_count"] = insert_stats.updated  # 在回填模式下，更新的记录视为已存在（跳过）

        # 5. 更新游标（使用单调递增规则，只通过 should_advance_mr_cursor 判断）
        # 按 (updated_at, iid) 排序，取最大的
        sorted_mrs = sorted(
            all_mrs,
            key=lambda m: (
                m.updated_at.isoformat() if m.updated_at else "",
                m.iid,
            ),
            reverse=True,
        )
        synced_count = result.get("synced_count", 0)
        
        # 记录游标推进原因
        cursor_advance_reason: Optional[str] = None
        
        # 检查是否有不可恢复的错误
        encountered_unrecoverable_error = len(unrecoverable_errors) > 0
        
        if sorted_mrs:
            latest = sorted_mrs[0]
            if latest.updated_at:
                new_updated_at = latest.updated_at.isoformat()
                new_iid = latest.iid

                # strict 模式下检查是否有不可恢复的错误
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

                # 使用单调递增规则判断是否推进游标
                # 仅在 should_update_cursor=True 且单调递增时推进
                if should_advance and should_update_cursor and should_advance_mr_cursor(
                    new_updated_at, new_iid, last_mr_updated_at, last_mr_iid
                ):
                    # 标准化时间戳为 Z 结尾格式，确保存储一致
                    normalized_updated_at = normalize_iso_ts_z(new_updated_at) or new_updated_at
                    # 使用统一的游标模块保存（支持 v2 格式）
                    save_gitlab_mr_cursor(
                        repo_id,
                        last_mr_updated_at=normalized_updated_at,
                        last_mr_iid=new_iid,
                        synced_count=synced_count,
                        config=config,
                    )
                    result["last_mr_iid"] = new_iid
                    result["last_mr_updated_at"] = normalized_updated_at
                    result["watermark_updated"] = True
                    logger.info(
                        f"更新同步游标: repo_id={repo_id}, "
                        f"last_mr_iid={new_iid}, last_mr_updated_at={new_updated_at}"
                    )
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
                        f"游标未推进: 新值 ({new_updated_at}, {new_iid}) "
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
        
        # 标准字段: counts 统计
        result["counts"] = {
            "synced_count": result.get("synced_count", 0),
            "scanned_count": result.get("scanned_count", 0),
            "inserted_count": result.get("inserted_count", 0),
            "skipped_count": result.get("skipped_count", 0),
        }

        # 6. 创建 logbook item/event（与 commits 同风格）
        synced_count = result.get("synced_count", 0)
        if synced_count > 0:
            try:
                first_mr = all_mrs[0] if all_mrs else None
                last_mr = all_mrs[-1] if all_mrs else None
                mr_range = ""
                if first_mr and last_mr:
                    mr_range = f" !{first_mr.iid}..!{last_mr.iid}"

                # 创建同步批次 logbook item（包含 run_id）
                batch_title = f"GitLab MR Sync Batch{mr_range} (repo_id={repo_id})"
                item_id = create_item(
                    item_type="scm_sync_batch",
                    title=batch_title,
                    scope_json={
                        "run_id": run_id,
                        "source_type": "mr",
                        "repo_id": repo_id,
                        "synced_count": synced_count,
                        "first_mr_iid": first_mr.iid if first_mr else None,
                        "last_mr_iid": last_mr.iid if last_mr else None,
                    },
                    status="done",
                    config=config,
                )
                result["logbook_item_id"] = item_id
                logger.debug(f"[run_id={run_id[:8]}] 创建同步批次 logbook item: item_id={item_id}")

                # 添加 sync_completed 事件（包含请求统计）
                event_payload = {
                    "run_id": run_id,
                    "synced_count": synced_count,
                    "inserted_count": result.get("inserted_count", 0),
                    "skipped_count": result.get("skipped_count", 0),
                    # 请求统计
                    "request_stats": client.stats.to_dict(),
                }
                add_event(
                    item_id=item_id,
                    event_type="sync_completed",
                    payload_json=event_payload,
                    status_from="running",
                    status_to="done",
                    source="scm_sync_gitlab_mrs",
                    config=config,
                )
            except Exception as e:
                # logbook 写入失败不影响同步主流程
                logger.warning(f"创建 logbook 记录失败 (非致命): {e}")
                result["logbook_error"] = str(e)

    except EngramError as e:
        result["error"] = str(e)
        result["error_category"] = getattr(e, "error_category", None) or getattr(e, "error_type", "engram_error")
        result["counts"] = {
            "synced_count": result.get("synced_count", 0),
            "scanned_count": result.get("scanned_count", 0),
            "inserted_count": result.get("inserted_count", 0),
            "skipped_count": result.get("skipped_count", 0),
        }
        raise
    except Exception as e:
        logger.exception(f"同步过程中发生错误: {e}")
        result["error"] = str(e)
        result["error_category"] = "exception"
        result["counts"] = {
            "synced_count": result.get("synced_count", 0),
            "scanned_count": result.get("scanned_count", 0),
            "inserted_count": result.get("inserted_count", 0),
            "skipped_count": result.get("skipped_count", 0),
        }
        raise GitLabSyncError(
            f"GitLab MR 同步失败: {e}",
            {"error": str(e)},
        )
    finally:
        # 释放分布式锁（确保在任何情况下都释放）
        if lock_acquired and lock_repo_id is not None:
            try:
                released = scm_sync_lock.release(
                    repo_id=lock_repo_id,
                    job_type=JOB_TYPE_GITLAB_MRS,
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
                elif result.get("synced_count", 0) == 0:
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
                    "synced_count": result.get("synced_count", 0),
                    "scanned_count": result.get("scanned_count", 0),
                    "inserted_count": result.get("inserted_count", 0),
                    "skipped_count": result.get("skipped_count", 0),
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
        description="GitLab Merge Requests 同步脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 使用配置文件同步
    python scm_sync_gitlab_mrs.py --config config.toml

    # 指定项目和 token
    python scm_sync_gitlab_mrs.py --gitlab-url https://gitlab.com --project-id 12345 --token glpat-xxx

    # 只同步已合并的 MR
    python scm_sync_gitlab_mrs.py --state merged

    # 循环同步直到完成
    python scm_sync_gitlab_mrs.py --loop
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
        "--state",
        type=str,
        choices=["all", "opened", "closed", "merged"],
        default=None,
        help="MR 状态过滤 (默认: all)",
    )

    parser.add_argument(
        "--fetch-details",
        action="store_true",
        help="获取每个 MR 的详细信息（会增加 API 调用）",
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
        "--loop",
        action="store_true",
        help="循环同步直到全部完成",
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
            state_filter=args.state or gitlab_cfg.get("mr_state_filter"),
            overlap_seconds=incremental_cfg.get("overlap_seconds", 300),
            strict=strict,
        )

        logger.info(f"GitLab URL: {sync_config.gitlab_url}")
        logger.info(f"Project ID: {sync_config.project_id}")
        logger.info(f"Batch size: {sync_config.batch_size}")
        logger.info(f"Overlap seconds: {sync_config.overlap_seconds}")
        if sync_config.state_filter:
            logger.info(f"State filter: {sync_config.state_filter}")

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
        # --no-update-cursor 用于回填模式，增量同步始终更新
        is_backfill_mode = backfill_since is not None
        update_watermark = True
        if is_backfill_mode:
            # 回填模式默认不更新，使用 --no-update-cursor 可显式禁用
            update_watermark = not getattr(args, 'no_update_cursor', True)

        # 执行同步
        total_synced = 0
        total_scanned = 0
        total_inserted = 0
        total_skipped = 0
        loop_count = 0
        max_loops = 1000  # 防止无限循环

        while True:
            loop_count += 1
            if loop_count > max_loops:
                logger.warning(f"达到最大循环次数限制 ({max_loops})，退出")
                break

            result = sync_gitlab_mrs(
                sync_config,
                project_key,
                config,
                verbose=args.verbose,
                fetch_details=args.fetch_details,
                backfill_since=backfill_since,
                backfill_until=backfill_until,
                update_watermark=update_watermark,
            )
            total_synced += result.get("synced_count", 0)
            total_scanned += result.get("scanned_count", 0)
            total_inserted += result.get("inserted_count", 0)
            total_skipped += result.get("skipped_count", 0)

            if args.json:
                print(json.dumps(result, default=str, ensure_ascii=False))

            if not args.loop or not result.get("has_more", False):
                break

            logger.info(f"继续同步下一批（第 {loop_count + 1} 轮）...")

        # 输出统计
        if not args.json:
            if backfill_since:
                logger.info(f"回填完成: 扫描 {total_scanned} 条 MR, 新增 {total_inserted} 条, 跳过(已存在) {total_skipped} 条")
            elif args.loop:
                logger.info(f"同步完成，共 {loop_count} 轮，总计 {total_synced} 个 MRs")

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
