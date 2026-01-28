#!/usr/bin/env python3
"""
scm_sync_gitlab_commits.py - GitLab Commits 同步脚本

功能:
- 使用 GitLab REST API 拉取 commits
- 从 [since_time] 范围增量同步（可配置批量大小上限）
- 获取每个 commit 的 diff
- 写入 scm.git_commits 与 scm.patch_blobs（source_type='git', source_id='<repo_id>:<sha>'）
- 同步完成后更新 kv 游标

使用:
    python scm_sync_gitlab_commits.py [--config PATH] [--batch-size N] [--verbose]

配置文件示例:
    [gitlab]
    url = "https://gitlab.example.com"
    project_id = 123                   # 或 "namespace/project"
    private_token = "glpat-xxx"
    batch_size = 100                   # 每次同步最大 commit 数
    ref_name = "main"                  # 可选，默认分支名
"""

import argparse
import hashlib
import json
import logging
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import psycopg

from engram_step1.config import (
    Config,
    add_config_argument,
    get_config,
    get_gitlab_config,
    get_incremental_config,
    get_bulk_thresholds,
    get_http_config,
    get_scm_sync_mode,
    get_scm_sync_config,
    is_strict_mode,
    SCM_SYNC_MODE_STRICT,
    SCM_SYNC_MODE_BEST_EFFORT,
    DEFAULT_OVERLAP_SECONDS,
    DEFAULT_TIME_WINDOW_DAYS,
    DEFAULT_FORWARD_WINDOW_SECONDS,
    DEFAULT_FORWARD_WINDOW_MIN_SECONDS,
    DEFAULT_ADAPTIVE_SHRINK_FACTOR,
    DEFAULT_ADAPTIVE_GROW_FACTOR,
    DEFAULT_ADAPTIVE_COMMIT_THRESHOLD,
)
from engram_step1.cursor import load_gitlab_cursor, save_gitlab_cursor, normalize_iso_ts_z
from engram_step1.db import get_connection, create_item, add_event, attach
from engram_step1.errors import DatabaseError, EngramError, ValidationError
from engram_step1.source_id import build_git_source_id, build_gitlab_repo_url
from engram_step1.uri import build_evidence_uri, build_evidence_ref_for_patch_blob
from engram_step1.scm_auth import (
    TokenProvider,
    create_gitlab_token_provider,
    mask_token,
    redact,
)
from engram_step1.gitlab_client import (
    GitLabClient,
    GitLabAPIError,
    GitLabAPIResult,
    GitLabErrorCategory,
    HttpConfig,
)
from engram_step1.scm_sync_policy import DegradationController, DegradationConfig
from engram_step1 import scm_sync_lock
from identity_resolve import resolve_and_enrich_meta
from artifacts import write_text_artifact, get_scm_path, SCM_TYPE_GIT, build_scm_artifact_path
from engram_step1.hashing import sha256 as compute_sha256
import db as scm_db
from db import insert_sync_run_start, insert_sync_run_finish
import scm_repo

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# 默认配置值
DEFAULT_BATCH_SIZE = 100
DEFAULT_LOCK_LEASE_SECONDS = 120  # 默认锁租约时间（秒）

# physical_job_type 常量
# 使用 physical_job_type 确保同一语义任务在队列/锁表中有唯一键
# 参见 engram_step1.scm_sync_job_types 模块
JOB_TYPE_GITLAB_COMMITS = "gitlab_commits"  # physical_job_type: GitLab 提交记录


def _generate_worker_id() -> str:
    """生成唯一的 worker ID（基于 hostname + pid + uuid 片段）"""
    import socket
    import os
    hostname = socket.gethostname()[:16]
    pid = os.getpid()
    short_uuid = str(uuid.uuid4())[:8]
    return f"{hostname}-{pid}-{short_uuid}"
DEFAULT_REF_NAME = None  # 使用默认分支
DEFAULT_REQUEST_TIMEOUT = 60


@dataclass
class GitCommit:
    """Git commit 数据结构"""
    sha: str
    author_name: str
    author_email: str
    authored_date: Optional[datetime]
    committer_name: str
    committer_email: str
    committed_date: Optional[datetime]
    message: str
    parent_ids: List[str] = field(default_factory=list)
    web_url: str = ""
    stats: Dict[str, int] = field(default_factory=dict)


# Diff 模式枚举
class DiffMode:
    """Diff 获取模式"""
    ALWAYS = "always"          # 始终获取完整 diff，失败则视为错误
    BEST_EFFORT = "best_effort"  # 尽力获取 diff，失败则降级使用 ministat/diffstat
    NONE = "none"               # 不获取 diff


@dataclass
class SyncConfig:
    """同步配置"""
    gitlab_url: str
    project_id: str  # 可以是数字 ID 或 namespace/project 格式
    token_provider: TokenProvider  # 使用 TokenProvider 替代 private_token
    batch_size: int = DEFAULT_BATCH_SIZE
    ref_name: Optional[str] = DEFAULT_REF_NAME
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT
    # 增量同步相关配置
    overlap_seconds: int = DEFAULT_OVERLAP_SECONDS  # 向前重叠秒数，防止边界丢失
    time_window_days: int = DEFAULT_TIME_WINDOW_DAYS  # 首次同步拉取天数
    # 前向窗口配置
    forward_window_seconds: int = DEFAULT_FORWARD_WINDOW_SECONDS  # 前向窗口秒数
    forward_window_min_seconds: int = DEFAULT_FORWARD_WINDOW_MIN_SECONDS  # 最小窗口秒数
    # 自适应窗口配置
    adaptive_shrink_factor: float = DEFAULT_ADAPTIVE_SHRINK_FACTOR
    adaptive_grow_factor: float = DEFAULT_ADAPTIVE_GROW_FACTOR
    adaptive_commit_threshold: int = DEFAULT_ADAPTIVE_COMMIT_THRESHOLD
    # 错误处理相关配置
    strict: bool = False  # 严格模式：不可恢复的错误时不推进游标
    diff_mode: str = DiffMode.BEST_EFFORT  # Diff 获取模式
    # Tenant 维度限流配置
    tenant_id: Optional[str] = None  # 租户 ID，用于 tenant 维度的限流


class GitLabSyncError(EngramError):
    """GitLab 同步错误"""
    exit_code = 11
    error_type = "GITLAB_SYNC_ERROR"


class GitLabAPIError(GitLabSyncError):
    """GitLab API 错误"""
    error_type = "GITLAB_API_ERROR"


class GitLabParseError(GitLabSyncError):
    """GitLab 响应解析错误"""
    error_type = "GITLAB_PARSE_ERROR"


# ============ 统一的 Patch 获取异常分类 ============

class PatchFetchError(EngramError):
    """Patch 获取错误基类"""
    exit_code = 20
    error_type = "PATCH_FETCH_ERROR"
    error_category = "unknown"


class PatchFetchTimeoutError(PatchFetchError):
    """Patch 获取超时错误"""
    error_type = "PATCH_FETCH_TIMEOUT"
    error_category = "timeout"


class PatchFetchContentTooLargeError(PatchFetchError):
    """Patch 内容过大错误"""
    error_type = "PATCH_FETCH_CONTENT_TOO_LARGE"
    error_category = "content_too_large"


class PatchFetchHttpError(PatchFetchError):
    """Patch 获取 HTTP 错误"""
    error_type = "PATCH_FETCH_HTTP_ERROR"
    error_category = "http_error"


class PatchFetchParseError(PatchFetchError):
    """Patch 内容解析错误"""
    error_type = "PATCH_FETCH_PARSE_ERROR"
    error_category = "parse_error"


@dataclass
class ChangeSummary:
    """变更摘要数据结构，用于 is_bulk 判断"""
    total_changes: int = 0  # additions + deletions
    files_changed: int = 0
    diff_size_bytes: int = 0
    # 可扩展的附加信息
    extra: Dict[str, Any] = field(default_factory=dict)


def is_bulk(
    change_summary: ChangeSummary,
    config: Optional[Config] = None,
) -> tuple:
    """
    判断 commit 是否为 bulk commit（大批量变更）

    统一的 bulk 判断逻辑，基于配置的阈值进行判断。

    阈值获取优先级:
    1. scm.bulk_thresholds.* (新键名)
    2. bulk.* (旧键名，向后兼容)
    3. 默认值

    Args:
        change_summary: 变更摘要
        config: 配置实例，用于获取阈值

    Returns:
        tuple: (is_bulk: bool, reason: Optional[str])
            - is_bulk: 是否为 bulk commit
            - reason: bulk 的原因说明，非 bulk 时为 None
    """
    # 从配置获取阈值（使用统一的 get_bulk_thresholds，支持新旧键兼容）
    thresholds = get_bulk_thresholds(config)
    total_changes_threshold = thresholds["git_total_changes_threshold"]
    files_changed_threshold = thresholds["git_files_changed_threshold"]
    diff_size_threshold = thresholds["diff_size_threshold"]

    # 检查总变更行数
    if change_summary.total_changes > total_changes_threshold:
        return (True, f"total_changes:{change_summary.total_changes}>{total_changes_threshold}")

    # 检查变更文件数
    if change_summary.files_changed > files_changed_threshold:
        return (True, f"files_changed:{change_summary.files_changed}>{files_changed_threshold}")

    # 检查 diff 大小
    if change_summary.diff_size_bytes > diff_size_threshold:
        size_mb = change_summary.diff_size_bytes / (1024 * 1024)
        threshold_mb = diff_size_threshold / (1024 * 1024)
        return (True, f"diff_size:{size_mb:.2f}MB>{threshold_mb:.2f}MB")

    return (False, None)


@dataclass
class FetchDiffResult:
    """diff 获取结果，支持成功和降级场景"""
    success: bool
    diffs: Optional[List[Dict[str, Any]]] = None
    error: Optional[PatchFetchError] = None
    error_category: Optional[str] = None
    error_message: Optional[str] = None
    endpoint: Optional[str] = None  # 原始请求端点
    status_code: Optional[int] = None  # HTTP 状态码（仅 HTTP 错误时）


def generate_ministat_from_stats(
    stats: Dict[str, int],
    files_changed: int = 0,
    commit_sha: Optional[str] = None,
) -> str:
    """
    从 stats 统计信息生成最小 diffstat（用于 GitLab 降级场景）

    当无法获取完整 diff 时，基于 API 返回的统计信息生成摘要。

    Args:
        stats: 统计信息 {"additions": N, "deletions": N, "total": N}
        files_changed: 变更文件数
        commit_sha: 可选的 commit SHA（用于生成注释）

    Returns:
        ministat 格式的摘要字符串
    """
    additions = stats.get("additions", 0)
    deletions = stats.get("deletions", 0)
    total_files = stats.get("total", files_changed) or files_changed

    output_lines = []

    # 添加降级标识头
    if commit_sha:
        short_sha = commit_sha[:8] if len(commit_sha) > 8 else commit_sha
        output_lines.append(f"# ministat for {short_sha} (degraded: diff unavailable)")
    else:
        output_lines.append("# ministat (degraded: diff unavailable)")
    output_lines.append("")

    # 简化的统计摘要
    output_lines.append(f" {total_files} file(s) changed")
    output_lines.append(f" {additions} insertion(s)(+)")
    output_lines.append(f" {deletions} deletion(s)(-)")

    return "\n".join(output_lines)


def generate_diffstat(diffs: List[Dict[str, Any]]) -> str:
    """
    从 GitLab diff API 返回的 diff 列表生成 diffstat 摘要

    生成类似 `git diff --stat` 的输出格式。

    Args:
        diffs: GitLab diff API 返回的 diff 列表

    Returns:
        diffstat 格式的摘要字符串
    """
    if not diffs:
        return ""

    output_lines = []
    total_additions = 0
    total_deletions = 0

    # 计算最长文件名用于对齐
    max_name_len = 0
    for d in diffs:
        new_path = d.get("new_path", d.get("old_path", ""))
        max_name_len = max(max_name_len, len(new_path))
    max_name_len = min(max_name_len, 60)  # 限制最大宽度

    for d in diffs:
        old_path = d.get("old_path", "/dev/null")
        new_path = d.get("new_path", "/dev/null")
        diff_content = d.get("diff", "")

        # 统计增删行数
        additions = 0
        deletions = 0
        for line in diff_content.split("\n"):
            if line.startswith("+") and not line.startswith("+++"):
                additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1

        total_additions += additions
        total_deletions += deletions

        # 确定显示的文件名
        display_name = new_path if new_path != "/dev/null" else old_path
        if len(display_name) > 60:
            display_name = "..." + display_name[-57:]

        # 文件操作类型
        if d.get("new_file"):
            op_marker = "(new)"
        elif d.get("deleted_file"):
            op_marker = "(deleted)"
        elif d.get("renamed_file"):
            op_marker = f"(renamed from {old_path})"
        else:
            op_marker = ""

        # 生成变更指示条
        total_changes = additions + deletions
        bar_width = min(total_changes, 50)
        if total_changes > 0:
            add_bars = int(bar_width * additions / total_changes)
            del_bars = bar_width - add_bars
            bar = "+" * add_bars + "-" * del_bars
        else:
            bar = ""

        line = f" {display_name:<{max_name_len}} | {total_changes:>5} {bar}"
        if op_marker:
            line += f" {op_marker}"
        output_lines.append(line)

    # 添加总计行
    files_count = len(diffs)
    output_lines.append(
        f" {files_count} file(s) changed, "
        f"{total_additions} insertion(s)(+), {total_deletions} deletion(s)(-)"
    )

    return "\n".join(output_lines)


def convert_api_result_to_fetch_diff_result(
    api_result: GitLabAPIResult,
    sha: str,
    max_size_bytes: int = 10 * 1024 * 1024,
) -> FetchDiffResult:
    """
    将 GitLabAPIResult 转换为 FetchDiffResult（兼容旧接口）

    Args:
        api_result: GitLabAPIResult 对象
        sha: commit SHA
        max_size_bytes: 内容最大字节数

    Returns:
        FetchDiffResult 对象
    """
    if api_result.success:
        # 检查响应大小
        if api_result.response:
            content_length = len(api_result.response.content)
            if content_length > max_size_bytes:
                error = PatchFetchContentTooLargeError(
                    f"diff 内容过大: {content_length} bytes > {max_size_bytes} bytes",
                    {"sha": sha, "size_bytes": content_length, "max_bytes": max_size_bytes},
                )
                return FetchDiffResult(
                    success=False,
                    error=error,
                    error_category="content_too_large",
                    error_message=str(error.message),
                    endpoint=api_result.endpoint,
                )

        return FetchDiffResult(
            success=True,
            diffs=api_result.data,
            endpoint=api_result.endpoint,
        )

    # 错误分类映射
    category_map = {
        GitLabErrorCategory.TIMEOUT: "timeout",
        GitLabErrorCategory.RATE_LIMITED: "rate_limited",
        GitLabErrorCategory.AUTH_ERROR: "auth_error",
        GitLabErrorCategory.SERVER_ERROR: "http_error",
        GitLabErrorCategory.CLIENT_ERROR: "http_error",
        GitLabErrorCategory.NETWORK_ERROR: "http_error",
        GitLabErrorCategory.PARSE_ERROR: "parse_error",
        GitLabErrorCategory.CONTENT_TOO_LARGE: "content_too_large",
    }
    error_category = category_map.get(
        api_result.error_category, "unknown"
    ) if api_result.error_category else "unknown"

    # 创建对应的 PatchFetchError
    error: Optional[PatchFetchError] = None
    if api_result.error_category == GitLabErrorCategory.TIMEOUT:
        error = PatchFetchTimeoutError(
            api_result.error_message or "请求超时",
            {"sha": sha},
        )
    elif api_result.error_category == GitLabErrorCategory.CONTENT_TOO_LARGE:
        error = PatchFetchContentTooLargeError(
            api_result.error_message or "内容过大",
            {"sha": sha},
        )
    elif api_result.error_category == GitLabErrorCategory.PARSE_ERROR:
        error = PatchFetchParseError(
            api_result.error_message or "解析失败",
            {"sha": sha},
        )
    else:
        error = PatchFetchHttpError(
            api_result.error_message or "HTTP 错误",
            {"sha": sha, "status_code": api_result.status_code},
        )

    return FetchDiffResult(
        success=False,
        error=error,
        error_category=error_category,
        error_message=api_result.error_message,
        endpoint=api_result.endpoint,
        status_code=api_result.status_code,
    )


def get_commit_diff_safe(
    client: GitLabClient,
    project_id: str,
    sha: str,
    max_size_bytes: int = 10 * 1024 * 1024,
) -> FetchDiffResult:
    """
    安全获取 commit 的 diff（支持降级）

    使用新的 GitLabClient 并转换结果为 FetchDiffResult。

    Args:
        client: GitLabClient 实例
        project_id: 项目 ID 或路径
        sha: commit SHA
        max_size_bytes: 内容最大字节数

    Returns:
        FetchDiffResult 对象
    """
    api_result = client.get_commit_diff_safe(project_id, sha, max_size_bytes)
    return convert_api_result_to_fetch_diff_result(api_result, sha, max_size_bytes)


def get_last_sync_cursor(repo_id: int, config: Optional[Config] = None) -> Dict[str, Any]:
    """
    从 KV 存储获取上次同步的游标

    使用统一的 cursor 模块，自动兼容旧格式。

    Args:
        repo_id: 仓库 ID
        config: 配置实例

    Returns:
        游标数据字典，包含 last_commit_sha, last_commit_ts 等
    """
    cursor = load_gitlab_cursor(repo_id, config)
    # 返回兼容旧接口的字典
    return {
        "last_commit_sha": cursor.last_commit_sha,
        "last_commit_ts": cursor.last_commit_ts,
        "last_sync_at": cursor.last_sync_at,
        "last_sync_count": cursor.last_sync_count,
    }


def update_sync_cursor(
    repo_id: int,
    last_commit_sha: str,
    last_commit_ts: str,
    synced_count: int,
    config: Optional[Config] = None,
) -> None:
    """
    更新 KV 同步游标

    使用统一的 cursor 模块。时间戳在保存前会被标准化为 Z 结尾的 UTC 格式。

    Args:
        repo_id: 仓库 ID
        last_commit_sha: 最后同步的 commit SHA
        last_commit_ts: 最后同步的 commit 时间
        synced_count: 本次同步的 commit 数
        config: 配置实例
    """
    # 标准化时间戳为 Z 结尾格式，确保存储一致
    normalized_ts = normalize_iso_ts_z(last_commit_ts) or last_commit_ts
    save_gitlab_cursor(repo_id, last_commit_sha, normalized_ts, synced_count, config)
    logger.info(f"更新同步游标: repo_id={repo_id}, last_commit_sha={last_commit_sha[:8]}")


def parse_commit(data: Dict[str, Any]) -> GitCommit:
    """
    解析 GitLab API 返回的 commit 数据

    Args:
        data: API 返回的 commit JSON

    Returns:
        GitCommit 对象
    """
    # 解析时间
    authored_date = None
    committed_date = None

    if data.get("authored_date"):
        try:
            authored_date = datetime.fromisoformat(data["authored_date"].replace("Z", "+00:00"))
        except ValueError:
            logger.warning(f"无法解析 authored_date: {data['authored_date']}")

    if data.get("committed_date"):
        try:
            committed_date = datetime.fromisoformat(data["committed_date"].replace("Z", "+00:00"))
        except ValueError:
            logger.warning(f"无法解析 committed_date: {data['committed_date']}")

    return GitCommit(
        sha=data["id"],
        author_name=data.get("author_name", ""),
        author_email=data.get("author_email", ""),
        authored_date=authored_date,
        committer_name=data.get("committer_name", ""),
        committer_email=data.get("committer_email", ""),
        committed_date=committed_date,
        message=data.get("message", ""),
        parent_ids=data.get("parent_ids", []),
        web_url=data.get("web_url", ""),
        stats=data.get("stats", {}),
    )


def insert_git_commits(
    conn: psycopg.Connection,
    repo_id: int,
    commits: List[GitCommit],
    config: Optional[Config] = None,
) -> int:
    """
    将 Git commit 插入数据库

    使用 ON CONFLICT DO UPDATE 实现 upsert

    Args:
        conn: 数据库连接
        repo_id: 仓库 ID
        commits: GitCommit 列表
        config: 配置实例

    Returns:
        插入/更新的记录数
    """
    if not commits:
        return 0

    inserted = 0
    with conn.cursor() as cur:
        for commit in commits:
            # 构建 source_id（统一格式: git:<repo_id>:<commit_sha>）
            source_id = build_git_source_id(repo_id, commit.sha)

            # 构建 meta_json
            meta_json = {
                "author_email": commit.author_email,
                "committer_name": commit.committer_name,
                "committer_email": commit.committer_email,
                "parent_ids": commit.parent_ids,
                "web_url": commit.web_url,
                "stats": commit.stats,
            }

            # 判断是否为 merge commit
            is_merge = len(commit.parent_ids) > 1

            # 使用统一的 is_bulk 函数判断（此时还没有 diff 内容）
            stats = commit.stats
            total_changes = stats.get("additions", 0) + stats.get("deletions", 0)
            change_summary = ChangeSummary(
                total_changes=total_changes,
                files_changed=stats.get("total", 0),  # GitLab API 的 total 表示文件数
                diff_size_bytes=0,  # 此时还没有 diff
            )
            bulk_flag, bulk_reason = is_bulk(change_summary, config)
            
            # 在 meta_json 中标注 bulk 信息
            if bulk_flag:
                meta_json["is_bulk_by_stats"] = True
                meta_json["bulk_reason"] = bulk_reason

            # 解析作者身份并填充到 meta_json
            meta_json = resolve_and_enrich_meta(
                meta_json,
                account_type="gitlab",
                username=commit.author_name,
                email=commit.author_email,
                config=config,
            )

            # 构建 author_raw（包含 name 和 email）
            author_raw = commit.author_name
            if commit.author_email:
                author_raw = f"{commit.author_name} <{commit.author_email}>"

            try:
                cur.execute(
                    """
                    INSERT INTO scm.git_commits
                        (commit_sha, repo_id, author_raw, ts, message, is_merge, is_bulk, bulk_reason, meta_json, source_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (repo_id, COALESCE(commit_sha, commit_id)) DO UPDATE SET
                        author_raw = EXCLUDED.author_raw,
                        ts = EXCLUDED.ts,
                        message = EXCLUDED.message,
                        is_merge = EXCLUDED.is_merge,
                        is_bulk = EXCLUDED.is_bulk,
                        bulk_reason = EXCLUDED.bulk_reason,
                        meta_json = EXCLUDED.meta_json,
                        source_id = EXCLUDED.source_id
                    """,
                    (
                        commit.sha,
                        repo_id,
                        author_raw,
                        commit.committed_date or commit.authored_date,
                        commit.message,
                        is_merge,
                        bulk_flag,
                        bulk_reason,
                        json.dumps(meta_json),
                        source_id,
                    ),
                )
                inserted += 1
            except psycopg.Error as e:
                logger.error(f"插入 commit {commit.sha[:8]} 失败: {e}")
                raise DatabaseError(
                    f"插入 Git commit 失败: {e}",
                    {"commit_sha": commit.sha, "error": str(e)},
                )

    conn.commit()
    return inserted


def insert_patch_blob(
    conn: psycopg.Connection,
    repo_id: int,
    commit_sha: str,
    content: str,
    patch_format: str = "diff",
    is_degraded: bool = False,
    degrade_reason: Optional[str] = None,
    source_fetch_error: Optional[str] = None,
    original_endpoint: Optional[str] = None,
    project_key: str = "default",
) -> Optional[Dict[str, Any]]:
    """
    将 commit diff 或 diffstat 存入 patch_blobs

    流程：
    1. 通过 write_text_artifact() 写入 artifacts 文件系统
    2. 调用 db.upsert_patch_blob() 写入 scm.patch_blobs 表

    支持的格式：
    - diff: 完整的 unified diff 格式
    - diffstat: 仅包含变更统计摘要（用于 bulk commit）
    - ministat: 最小统计摘要（用于降级场景）

    Args:
        conn: 数据库连接
        repo_id: 仓库 ID
        commit_sha: commit SHA
        content: diff 或 diffstat 内容
        patch_format: 格式类型 ("diff"、"diffstat" 或 "ministat")
        is_degraded: 是否为降级模式
        degrade_reason: 降级原因（如 timeout/content_too_large/http_error）
        source_fetch_error: 原始获取错误信息
        original_endpoint: 原始请求端点

    Returns:
        包含 patch 信息的字典 {blob_id, sha256, size_bytes, source_id, commit_sha, format, degraded}
        或 None（如果内容为空）
    """
    if not content:
        return None

    # 构建 source_id: <repo_id>:<sha>
    source_id = f"{repo_id}:{commit_sha}"

    # 生成文件扩展名（根据格式区分）
    ext_map = {"diff": "diff", "diffstat": "diffstat", "ministat": "ministat"}
    file_ext = ext_map.get(patch_format, "diff")
    
    # 先计算内容 sha256，再构建 v2 路径
    content_bytes = content.encode("utf-8") if isinstance(content, str) else content
    content_sha256 = compute_sha256(content_bytes)
    
    # 使用 build_scm_artifact_path 生成 v2 路径
    # 格式: scm/<project_key>/<repo_id>/git/<commit_sha>/<sha256>.<ext>
    rel_path = build_scm_artifact_path(
        project_key=project_key,
        repo_id=str(repo_id),
        source_type="git",
        rev_or_sha=commit_sha,
        sha256=content_sha256,
        ext=file_ext,
    )

    try:
        # 1. 写入 artifacts 文件系统
        artifact_result = write_text_artifact(rel_path, content)
        
        # 从 write_text_artifact 返回值获取 uri/sha256/size_bytes
        uri = artifact_result["uri"]
        sha256 = artifact_result["sha256"]
        size_bytes = artifact_result["size_bytes"]
        
        logger.debug(
            f"写入 artifact: uri={uri}, sha256={sha256[:8]}..., size={size_bytes}, "
            f"format={patch_format}, degraded={is_degraded}"
        )

        # 2. 构建 meta_json（包含降级元数据与 evidence_uri）
        # 注意：对 source_fetch_error 和 original_endpoint 进行脱敏，避免泄露敏感信息
        meta_json: Dict[str, Any] = {"materialize_status": "done"}
        if is_degraded:
            meta_json["degraded"] = True
            meta_json["degrade_reason"] = degrade_reason
            if source_fetch_error:
                meta_json["source_fetch_error"] = redact(source_fetch_error)
            if original_endpoint:
                meta_json["original_endpoint"] = redact(original_endpoint)
        
        # 构建 canonical evidence_uri (memory://patch_blobs/<source_type>/<source_id>/<sha256>)
        # 用于 analysis.* 和 governance.* 表中的 evidence_refs_json 引用
        evidence_uri = build_evidence_uri("git", source_id, sha256)
        meta_json["evidence_uri"] = evidence_uri

        # 3. 调用 db.upsert_patch_blob() 写入数据库
        blob_id = scm_db.upsert_patch_blob(
            conn,
            source_type="git",
            source_id=source_id,
            sha256=sha256,
            uri=uri,
            size_bytes=size_bytes,
            format=patch_format,
            meta_json=meta_json,
        )
        
        conn.commit()
        
        # 返回包含完整信息的字典（用于 logbook attachments）
        return {
            "blob_id": blob_id,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "source_id": source_id,
            "commit_sha": commit_sha,
            "format": patch_format,
            "degraded": is_degraded,
        }

    except Exception as e:
        logger.error(f"插入 patch_blob 失败: {e}")
        raise DatabaseError(
            f"插入 patch_blob 失败: {e}",
            {"source_id": source_id, "rel_path": rel_path, "error": str(e)},
        )


def format_diff_content(diffs: List[Dict[str, Any]]) -> str:
    """
    将 GitLab diff API 返回的 diff 列表格式化为统一 diff 格式

    Args:
        diffs: GitLab diff API 返回的 diff 列表

    Returns:
        格式化后的 diff 字符串
    """
    if not diffs:
        return ""

    parts = []
    for d in diffs:
        # 构建 diff header
        old_path = d.get("old_path", "/dev/null")
        new_path = d.get("new_path", "/dev/null")
        diff_header = f"--- a/{old_path}\n+++ b/{new_path}\n"
        diff_content = d.get("diff", "")
        parts.append(diff_header + diff_content)

    return "\n".join(parts)


def _parse_iso_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    """
    解析 ISO 8601 格式的时间字符串为带时区的 datetime
    
    Args:
        dt_str: ISO 8601 格式的时间字符串（如 "2024-01-01T12:00:00Z"）
    
    Returns:
        带时区的 datetime 对象，解析失败时返回 None
    """
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _get_commit_timestamp(commit: GitCommit) -> datetime:
    """
    获取 commit 的时间戳（用于排序和比较）
    
    优先使用 committed_date，其次 authored_date
    
    Args:
        commit: GitCommit 对象
    
    Returns:
        commit 的时间戳，无时区信息时假定 UTC
    """
    ts = commit.committed_date or commit.authored_date
    if ts is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    # 确保有时区信息
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _get_commit_sort_key(commit: GitCommit) -> tuple:
    """
    获取 commit 的排序键 (ts, sha)
    
    使用 (时间戳, sha) 作为复合排序键，保证同秒内的稳定排序。
    
    Args:
        commit: GitCommit 对象
    
    Returns:
        排序键元组 (datetime, sha)
    """
    return (_get_commit_timestamp(commit), commit.sha)


def _deduplicate_commits(
    commits: List[GitCommit],
    cursor_sha: Optional[str] = None,
    cursor_ts: Optional[datetime] = None,
) -> List[GitCommit]:
    """
    对 commits 进行去重和过滤
    
    1. 按 sha 去重（保留第一个出现的）
    2. 过滤掉 <= cursor watermark 的记录：
       - 跳过 ts < cursor_ts 的记录
       - 跳过 ts == cursor_ts 且 sha <= cursor_sha 的记录
    3. 按 (committed_date, sha) 升序排序（确保同秒内稳定处理顺序）
    
    Args:
        commits: 原始 commit 列表
        cursor_sha: 游标中的最后一个 commit sha（可选）
        cursor_ts: 游标中的时间戳（可选）
    
    Returns:
        去重并过滤后的 commit 列表（按 (ts, sha) 升序排列）
    """
    if not commits:
        return []
    
    # 1. 按 sha 去重
    seen_shas: set = set()
    unique_commits: List[GitCommit] = []
    for commit in commits:
        if commit.sha not in seen_shas:
            seen_shas.add(commit.sha)
            unique_commits.append(commit)
    
    # 2. 过滤掉 <= cursor watermark 的记录
    # 使用 (ts, sha) 复合水位线：
    # - 跳过 ts < cursor_ts 的记录
    # - 跳过 ts == cursor_ts 且 sha <= cursor_sha 的记录
    filtered_commits: List[GitCommit] = []
    cursor_ts_tz = None
    if cursor_ts:
        cursor_ts_tz = cursor_ts if cursor_ts.tzinfo else cursor_ts.replace(tzinfo=timezone.utc)
    
    for commit in unique_commits:
        commit_ts = _get_commit_timestamp(commit)
        
        if cursor_ts_tz:
            if commit_ts < cursor_ts_tz:
                # ts < cursor_ts: 跳过
                continue
            elif commit_ts == cursor_ts_tz:
                # ts == cursor_ts: 需要比较 sha
                if cursor_sha and commit.sha <= cursor_sha:
                    # sha <= cursor_sha: 跳过（已处理过）
                    continue
        elif cursor_sha and commit.sha == cursor_sha:
            # 仅有 cursor_sha 无 cursor_ts 时，跳过完全匹配的
            continue
        
        filtered_commits.append(commit)
    
    # 3. 按 (committed_date, sha) 升序排序（确保同秒内稳定处理顺序）
    filtered_commits.sort(key=_get_commit_sort_key)
    
    return filtered_commits


@dataclass
class FetchWindow:
    """时间窗口定义"""
    since: datetime
    until: datetime


@dataclass
class AdaptiveWindowState:
    """
    自适应窗口状态
    
    用于在多轮同步中跟踪窗口大小的动态调整
    """
    current_window_seconds: int  # 当前窗口大小（秒）
    min_window_seconds: int      # 最小窗口大小（秒）
    max_window_seconds: int      # 最大窗口大小（秒）
    shrink_factor: float         # 缩小因子
    grow_factor: float           # 增长因子
    commit_threshold: int        # 触发缩小的 commit 数阈值
    rate_limit_count: int = 0    # 429 错误计数
    
    def shrink(self, reason: str = "unknown") -> int:
        """
        缩小窗口
        
        Args:
            reason: 缩小原因（用于日志）
            
        Returns:
            调整后的窗口大小
        """
        old_size = self.current_window_seconds
        new_size = int(self.current_window_seconds * self.shrink_factor)
        self.current_window_seconds = max(new_size, self.min_window_seconds)
        logger.info(
            f"自适应窗口缩小: {old_size}s -> {self.current_window_seconds}s (原因: {reason})"
        )
        return self.current_window_seconds
    
    def grow(self) -> int:
        """
        增长窗口
        
        Returns:
            调整后的窗口大小
        """
        old_size = self.current_window_seconds
        new_size = int(self.current_window_seconds * self.grow_factor)
        self.current_window_seconds = min(new_size, self.max_window_seconds)
        if self.current_window_seconds != old_size:
            logger.debug(
                f"自适应窗口增长: {old_size}s -> {self.current_window_seconds}s"
            )
        return self.current_window_seconds
    
    def record_rate_limit(self) -> None:
        """记录 429 限流错误"""
        self.rate_limit_count += 1
        # 遇到限流自动缩小窗口
        self.shrink(reason=f"rate_limit_count={self.rate_limit_count}")
    
    def reset_rate_limit_count(self) -> None:
        """重置限流计数"""
        self.rate_limit_count = 0


def compute_commit_fetch_window(
    cursor_ts: Optional[datetime],
    overlap_seconds: int,
    forward_window_seconds: int,
    now: Optional[datetime] = None,
) -> FetchWindow:
    """
    计算 commit 获取的时间窗口
    
    基于游标时间戳、重叠窗口和前向窗口计算 since/until 时间范围。
    
    窗口策略：
    - since = cursor_ts - overlap_seconds（向前回溯，确保边界不丢失）
    - until = since + forward_window_seconds（限制单次获取范围）
    - until 不超过当前时间
    
    Args:
        cursor_ts: 游标时间戳（可能为 None，表示首次同步）
        overlap_seconds: 重叠秒数（向前回溯）
        forward_window_seconds: 前向窗口秒数
        now: 当前时间（用于测试注入，默认使用 UTC 当前时间）
        
    Returns:
        FetchWindow 对象，包含 since 和 until
    """
    if now is None:
        now = datetime.now(timezone.utc)
    
    # 确保 now 有时区信息
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    
    if cursor_ts is None:
        # 首次同步：从很早开始，until 限制为 forward_window_seconds
        # 使用 1970-01-01 作为起始点（或可配置的 time_window_days 限制）
        since = datetime(1970, 1, 1, tzinfo=timezone.utc)
        until = min(since + timedelta(seconds=forward_window_seconds), now)
    else:
        # 确保 cursor_ts 有时区信息
        if cursor_ts.tzinfo is None:
            cursor_ts = cursor_ts.replace(tzinfo=timezone.utc)
        
        # 增量同步：向前回溯 overlap_seconds
        since = cursor_ts - timedelta(seconds=overlap_seconds)
        
        # until = since + forward_window_seconds，但不超过 now
        until = min(since + timedelta(seconds=forward_window_seconds), now)
    
    return FetchWindow(since=since, until=until)


def select_next_batch(
    commits: List[GitCommit],
    cursor_sha: Optional[str],
    cursor_ts: Optional[datetime],
    batch_size: int,
) -> List[GitCommit]:
    """
    从 commits 列表中选择下一批待处理的 commits
    
    处理逻辑：
    1. 过滤掉 <= cursor watermark 的 commits（使用 (ts, sha) 复合水位线）
    2. 按 (ts, sha) 升序排序
    3. 截断到 batch_size
    
    此函数是 _deduplicate_commits 的封装，增加了 batch_size 限制。
    
    Args:
        commits: 原始 commit 列表
        cursor_sha: 游标中的最后一个 commit SHA
        cursor_ts: 游标中的时间戳
        batch_size: 批量大小限制
        
    Returns:
        待处理的 commit 列表（已排序和截断）
    """
    # 使用已有的去重和过滤函数
    filtered = _deduplicate_commits(commits, cursor_sha, cursor_ts)
    
    # 截断到 batch_size
    if len(filtered) > batch_size:
        filtered = filtered[:batch_size]
    
    return filtered


def compute_batch_cursor_target(
    commits: List[GitCommit],
) -> Optional[tuple]:
    """
    计算本批次应推进到的游标目标
    
    确保游标只推进到本轮完整处理的最晚 (ts, sha)。
    
    Args:
        commits: 已按 (ts, sha) 升序排列的 commit 列表
        
    Returns:
        (target_ts, target_sha) 元组，或 None 如果列表为空
    """
    if not commits:
        return None
    
    # 最后一个 commit 是最晚的（按 (ts, sha) 排序）
    last_commit = commits[-1]
    target_ts = _get_commit_timestamp(last_commit)
    target_sha = last_commit.sha
    
    return (target_ts, target_sha)


def is_unrecoverable_api_error(error_category: Optional[str], status_code: Optional[int] = None) -> bool:
    """
    判断 API 错误是否为不可恢复的错误
    
    不可恢复的错误包括：
    - 429 (Rate Limited): 限流错误，重试后仍失败
    - 5xx (Server Error): 服务器端错误
    - 网络连接错误/超时
    
    Args:
        error_category: 错误分类
        status_code: HTTP 状态码
        
    Returns:
        是否为不可恢复的错误
    """
    unrecoverable_categories = {
        "rate_limited",  # 429 限流
        "http_error",    # 5xx/网络错误
        "timeout",       # 超时
    }
    
    if error_category in unrecoverable_categories:
        return True
    
    # 检查 5xx 状态码
    if status_code and 500 <= status_code < 600:
        return True
    
    # 429 状态码
    if status_code == 429:
        return True
    
    return False


def sync_gitlab_commits(
    sync_config: SyncConfig,
    project_key: str,
    config: Optional[Config] = None,
    verbose: bool = True,
    fetch_diffs: bool = True,
    lock_lease_seconds: int = DEFAULT_LOCK_LEASE_SECONDS,
    job_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    执行 GitLab commits 同步

    Args:
        sync_config: 同步配置
        project_key: 项目标识
        config: Config 实例
        verbose: 是否输出详细信息
        fetch_diffs: 是否获取 diff（会被 sync_config.diff_mode 覆盖）
        lock_lease_seconds: 锁租约时长（秒），默认 120 秒
        job_payload: 可选的 job payload，包含 scheduler 注入的 suggested_* 参数
            - suggested_batch_size: 建议的批量大小
            - suggested_forward_window_seconds: 建议的前向窗口秒数
            - suggested_diff_mode: 建议的 diff 获取模式

    Returns:
        同步结果统计，如果获取锁失败则返回 {"locked": True, "skipped": True}
    """
    # === 优先使用 job_payload 中的 suggested_* 参数覆盖 sync_config ===
    # 这些参数由 scheduler 根据熔断/限流状态注入
    if job_payload:
        if "suggested_batch_size" in job_payload and job_payload["suggested_batch_size"] is not None:
            original_batch_size = sync_config.batch_size
            sync_config.batch_size = int(job_payload["suggested_batch_size"])
            if sync_config.batch_size != original_batch_size:
                logger.info(
                    f"使用 payload 建议的 batch_size: {original_batch_size} -> {sync_config.batch_size}"
                )
        
        if "suggested_forward_window_seconds" in job_payload and job_payload["suggested_forward_window_seconds"] is not None:
            original_window = sync_config.forward_window_seconds
            sync_config.forward_window_seconds = int(job_payload["suggested_forward_window_seconds"])
            if sync_config.forward_window_seconds != original_window:
                logger.info(
                    f"使用 payload 建议的 forward_window_seconds: {original_window} -> {sync_config.forward_window_seconds}"
                )
        
        if "suggested_diff_mode" in job_payload and job_payload["suggested_diff_mode"] is not None:
            original_diff_mode = sync_config.diff_mode
            sync_config.diff_mode = job_payload["suggested_diff_mode"]
            if sync_config.diff_mode != original_diff_mode:
                logger.info(
                    f"使用 payload 建议的 diff_mode: {original_diff_mode} -> {sync_config.diff_mode}"
                )
    
    # diff_mode=none 时禁用 diff 获取
    if sync_config.diff_mode == DiffMode.NONE:
        fetch_diffs = False
    
    # 生成 run_id 用于追踪本次同步运行
    run_id = str(uuid.uuid4())
    
    # 生成 worker_id 用于锁标识
    worker_id = _generate_worker_id()
    
    result = {
        "success": False,
        "run_id": run_id,
        "synced_count": 0,
        "diff_count": 0,
        "since": None,
        "skipped_count": 0,  # 因去重/过滤跳过的数量
        "error": None,
        "error_category": None,  # 标准字段：错误分类
        "retry_after": None,  # 标准字段：建议的重试等待秒数（可选）
        "counts": {},  # 标准字段：计数统计
        "cursor_advance_reason": None,  # 游标推进原因
        "cursor_advance_stopped_at": None,  # strict 模式下游标停止的位置（commit sha）
        "degraded_reasons": {},  # 降级原因分布 {reason: count}
        "missing_types": [],  # best_effort 模式下记录的缺失类型
        "unrecoverable_errors": [],  # 不可恢复的错误列表
        "strict_mode": sync_config.strict,  # 是否启用严格模式
        "sync_mode": SCM_SYNC_MODE_STRICT if sync_config.strict else SCM_SYNC_MODE_BEST_EFFORT,
        "diff_mode": sync_config.diff_mode,  # Diff 获取模式
        "locked": False,  # 是否因锁被跳过
        "skipped": False,  # 是否被跳过
    }
    
    logger.info(f"[run_id={run_id[:8]}] 开始 GitLab commits 同步 (strict={sync_config.strict}, diff_mode={sync_config.diff_mode})")
    
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
        repo_id = scm_repo.ensure_repo(
            repo_type="git",
            url=repo_url,
            project_key=project_key,
            default_branch=sync_config.ref_name,
            config=config,
        )
        result["repo_id"] = repo_id
        lock_repo_id = repo_id
        logger.debug(f"仓库记录: repo_id={repo_id}, url={repo_url}")

        # 1.5. 尝试获取分布式锁（repo_id + job_type）
        lock_acquired = scm_sync_lock.claim(
            repo_id=repo_id,
            job_type=JOB_TYPE_GITLAB_COMMITS,
            worker_id=worker_id,
            lease_seconds=lock_lease_seconds,
            conn=conn,
        )
        if not lock_acquired:
            # 锁被其他 worker 持有，返回 locked/skip 结果
            lock_info = scm_sync_lock.get(repo_id, JOB_TYPE_GITLAB_COMMITS, conn=conn)
            logger.warning(
                f"[run_id={run_id[:8]}] 锁被其他 worker 持有，跳过本次同步 "
                f"(repo_id={repo_id}, job_type={JOB_TYPE_GITLAB_COMMITS}, "
                f"locked_by={lock_info.get('locked_by') if lock_info else 'unknown'})"
            )
            result["locked"] = True
            result["skipped"] = True
            result["success"] = True  # locked/skipped 视为成功（不需要重试）
            result["message"] = "锁被其他 worker 持有，跳过本次同步"
            result["error_category"] = "lock_held"
            result["counts"] = {"synced_count": 0, "diff_count": 0, "skipped_count": 0}
            return result
        
        logger.debug(f"[run_id={run_id[:8]}] 成功获取锁 (repo_id={repo_id}, worker_id={worker_id})")

        # 2. 获取上次同步的游标
        cursor = get_last_sync_cursor(repo_id, config)
        cursor_ts_str = cursor.get("last_commit_ts")
        cursor_sha = cursor.get("last_commit_sha")
        cursor_ts = _parse_iso_datetime(cursor_ts_str)
        
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
                job_type="gitlab_commits",
                mode="incremental",
                cursor_before=sync_run_cursor_before,
                meta_json={
                    "strict": sync_config.strict,
                    "diff_mode": sync_config.diff_mode,
                    "batch_size": sync_config.batch_size,
                },
            )
            conn.commit()
            sync_run_started = True
        except Exception as e:
            logger.warning(f"记录 sync_run 开始失败 (非致命): {e}")
        
        # 3. 计算 since 时间（考虑 overlap）
        if cursor_ts:
            # 增量同步：向前回溯 overlap_seconds
            overlap_delta = timedelta(seconds=sync_config.overlap_seconds)
            since_dt = cursor_ts - overlap_delta
            since = since_dt.isoformat()
            logger.info(
                f"增量同步: cursor_ts={cursor_ts_str}, overlap={sync_config.overlap_seconds}s, since={since}"
            )
        else:
            # 首次同步：使用 time_window_days 限制范围
            if sync_config.time_window_days > 0:
                since_dt = datetime.now(timezone.utc) - timedelta(days=sync_config.time_window_days)
                since = since_dt.isoformat()
                logger.info(
                    f"首次同步: time_window_days={sync_config.time_window_days}, since={since}"
                )
            else:
                since = None
                logger.info("首次同步，获取所有 commits")

        result["since"] = since

        # 4. 拉取 commits（分页）
        logger.info(f"从 GitLab 获取 commits (project_id={sync_config.project_id}, since={since})")

        all_commits: List[GitCommit] = []
        seen_shas: set = set()  # 跨页去重
        page = 1
        
        while True:
            commits_data = client.get_commits(
                sync_config.project_id,
                since=since,
                ref_name=sync_config.ref_name,
                per_page=min(sync_config.batch_size, 100),
                page=page,
            )

            if not commits_data:
                break

            # 解析并跨页去重
            for data in commits_data:
                commit = parse_commit(data)
                if commit.sha not in seen_shas:
                    seen_shas.add(commit.sha)
                    all_commits.append(commit)

            # 达到 batch_size 限制
            if len(all_commits) >= sync_config.batch_size:
                break

            # 检查是否还有更多页
            if len(commits_data) < min(sync_config.batch_size, 100):
                break

            page += 1

        logger.info(f"获取到 {len(all_commits)} 个 commits (分页去重后)")
        
        # 5. 本地去重并过滤 <= cursor watermark 的记录
        original_count = len(all_commits)
        all_commits = _deduplicate_commits(all_commits, cursor_sha, cursor_ts)
        skipped_count = original_count - len(all_commits)
        result["skipped_count"] = skipped_count
        
        if skipped_count > 0:
            logger.info(f"过滤掉 {skipped_count} 个重复/已处理的 commits")
        
        # 截断到 batch_size
        if len(all_commits) > sync_config.batch_size:
            all_commits = all_commits[:sync_config.batch_size]
        
        logger.info(f"待处理 {len(all_commits)} 个 commits (按时间升序)")

        if not all_commits:
            logger.info("无新 commits 需要同步")
            result["success"] = True
            result["message"] = "无新 commits 需要同步"
            result["counts"] = {"synced_count": 0, "diff_count": 0, "skipped_count": result.get("skipped_count", 0)}
            return result

        # 4. 写入 git_commits 表
        synced_count = insert_git_commits(conn, repo_id, all_commits, config)
        logger.info(f"写入 git_commits: {synced_count} 条记录")
        result["synced_count"] = synced_count

        # 5. 获取并存储 diff（可选）
        diff_count = 0
        bulk_count = 0
        degraded_count = 0
        degraded_reasons: Dict[str, int] = {}  # 降级原因分布
        unrecoverable_errors: List[Dict[str, Any]] = []  # 不可恢复的错误列表
        # 收集 patch 信息用于 logbook attachments
        collected_patches = []
        # 跟踪最后一个成功处理的 commit（用于 strict 模式）
        last_successful_commit: Optional[GitCommit] = None
        encountered_unrecoverable_error = False  # 是否遇到不可恢复的错误
        
        if fetch_diffs:
            for commit in all_commits:
                try:
                    # 使用安全的 diff 获取方法，支持降级
                    fetch_result = get_commit_diff_safe(
                        client, sync_config.project_id, commit.sha
                    )
                    
                    # 初始化降级标志
                    is_degraded = False
                    degrade_reason = None
                    source_fetch_error = None
                    original_endpoint = None
                    
                    if not fetch_result.success:
                        error_category = fetch_result.error_category
                        status_code = fetch_result.status_code
                        
                        # 检查是否为不可恢复的错误
                        if is_unrecoverable_api_error(error_category, status_code):
                            error_info = {
                                "commit_sha": commit.sha,
                                "error_category": error_category,
                                "status_code": status_code,
                                "error_message": fetch_result.error_message,
                            }
                            unrecoverable_errors.append(error_info)
                            encountered_unrecoverable_error = True
                            
                            # diff_mode=always: 失败则视为错误，不降级
                            if sync_config.diff_mode == DiffMode.ALWAYS:
                                logger.error(
                                    f"Commit {commit.sha[:8]} diff 获取失败 (strict/always mode): "
                                    f"{error_category}, status={status_code}"
                                )
                                # strict 模式下，记录错误但继续处理（不存储 patch）
                                if sync_config.strict:
                                    # 不更新 last_successful_commit，中断后续处理
                                    break
                                continue
                        
                        # best_effort 模式：降级处理，使用 ministat 格式
                        is_degraded = True
                        degrade_reason = error_category
                        source_fetch_error = fetch_result.error_message
                        original_endpoint = fetch_result.endpoint
                        
                        # 使用 stats 生成 ministat
                        content_to_store = generate_ministat_from_stats(
                            commit.stats, commit_sha=commit.sha
                        )
                        patch_format = "ministat"
                        degraded_count += 1
                        
                        # 记录降级原因分布
                        reason_key = error_category or "unknown"
                        degraded_reasons[reason_key] = degraded_reasons.get(reason_key, 0) + 1
                        
                        logger.info(
                            f"Commit {commit.sha[:8]} diff 获取失败 ({degrade_reason})，"
                            f"降级使用 ministat 格式"
                        )
                    else:
                        diffs = fetch_result.diffs or []
                        
                        if not diffs:
                            # 无 diff 内容，视为成功处理
                            last_successful_commit = commit
                            continue
                        
                        # 计算完整 diff 内容用于判断大小
                        diff_content = format_diff_content(diffs)
                        diff_size_bytes = len(diff_content.encode("utf-8")) if diff_content else 0
                        
                        # 构建 ChangeSummary 进行 bulk 判断
                        stats = commit.stats
                        total_changes = stats.get("additions", 0) + stats.get("deletions", 0)
                        change_summary = ChangeSummary(
                            total_changes=total_changes,
                            files_changed=len(diffs),
                            diff_size_bytes=diff_size_bytes,
                        )
                        bulk_flag, bulk_reason = is_bulk(change_summary, config)
                        
                        # 根据 bulk 判断选择格式
                        if bulk_flag:
                            # bulk commit: 使用 diffstat 格式
                            content_to_store = generate_diffstat(diffs)
                            patch_format = "diffstat"
                            bulk_count += 1
                            logger.info(
                                f"Commit {commit.sha[:8]} 判定为 bulk ({bulk_reason})，使用 diffstat 格式"
                            )
                        else:
                            # 正常 commit: 使用完整 diff
                            content_to_store = diff_content
                            patch_format = "diff"
                    
                    if content_to_store:
                        patch_result = insert_patch_blob(
                            conn,
                            repo_id,
                            commit.sha,
                            content_to_store,
                            patch_format,
                            is_degraded=is_degraded,
                            degrade_reason=degrade_reason,
                            source_fetch_error=source_fetch_error,
                            original_endpoint=original_endpoint,
                            project_key=project_key,
                        )
                        if patch_result:
                            diff_count += 1
                            # 收集 patch 信息用于 logbook attachments
                            collected_patches.append(patch_result)
                            # 更新最后一个成功处理的 commit
                            last_successful_commit = commit
                            if verbose:
                                logger.debug(
                                    f"存储 {patch_format}: commit={commit.sha[:8]}, "
                                    f"blob_id={patch_result['blob_id']}, degraded={is_degraded}"
                                )
                except GitLabAPIError as e:
                    logger.warning(f"获取 diff 失败 (commit={commit.sha[:8]}): {e.message}")
                    # 记录不可恢复的错误
                    if hasattr(e, 'category') and is_unrecoverable_api_error(
                        e.category.value if e.category else None, 
                        getattr(e, 'status_code', None)
                    ):
                        unrecoverable_errors.append({
                            "commit_sha": commit.sha,
                            "error_category": e.category.value if e.category else "unknown",
                            "status_code": getattr(e, 'status_code', None),
                            "error_message": e.message,
                        })
                        encountered_unrecoverable_error = True
                        if sync_config.strict and sync_config.diff_mode == DiffMode.ALWAYS:
                            break
                except Exception as e:
                    logger.warning(f"处理 diff 失败 (commit={commit.sha[:8]}): {e}")

            logger.info(
                f"写入 patch_blobs: {diff_count} 条记录 "
                f"(bulk={bulk_count}, degraded={degraded_count})"
            )
            result["diff_count"] = diff_count
            result["bulk_count"] = bulk_count
            result["degraded_count"] = degraded_count
            result["degraded_reasons"] = degraded_reasons
            result["unrecoverable_errors"] = unrecoverable_errors
        else:
            # 不获取 diff 时，所有 commit 都视为成功处理
            last_successful_commit = all_commits[-1] if all_commits else None

        # 6. 更新游标（水位线单调递增）
        # strict 模式下，如果遇到不可恢复的错误，只推进到最后一个成功处理的 commit
        cursor_advance_reason: Optional[str] = None
        
        # 在 best_effort 模式下记录缺失类型（哪些 commit 的 diff 缺失）
        missing_types = []
        if not sync_config.strict and encountered_unrecoverable_error:
            # 收集所有不可恢复错误的类型
            for err_info in unrecoverable_errors:
                err_cat = err_info.get("error_category", "unknown")
                if err_cat not in missing_types:
                    missing_types.append(err_cat)
            result["missing_types"] = missing_types
        
        if all_commits:
            # 确定要推进到的 commit
            if sync_config.strict and encountered_unrecoverable_error:
                # strict 模式 + 遇到不可恢复的错误：推进到最后一个成功处理的 commit
                if last_successful_commit:
                    target_commit = last_successful_commit
                    cursor_advance_reason = f"strict_partial_success:stopped_before_unrecoverable_error"
                    result["cursor_advance_stopped_at"] = last_successful_commit.sha
                    logger.info(
                        f"[strict mode] 遇到不可恢复的错误，游标推进到最后一个成功处理的 commit: "
                        f"{target_commit.sha[:8]}"
                    )
                else:
                    # 没有成功处理任何 commit，不推进游标
                    target_commit = None
                    cursor_advance_reason = "strict_no_success:no_commit_processed"
                    logger.warning(
                        f"[strict mode] 遇到不可恢复的错误且没有成功处理任何 commit，游标不推进"
                    )
            else:
                # 非 strict 模式或无错误：推进到最后一个 commit
                target_commit = all_commits[-1]  # 升序排列，最后一个是最新的
                if encountered_unrecoverable_error:
                    # best_effort 模式：推进游标但记录降级信息
                    error_types_str = ",".join(missing_types) if missing_types else "unknown"
                    cursor_advance_reason = f"best_effort_with_errors:degraded={error_types_str}"
                    logger.warning(
                        f"[best_effort mode] 遇到不可恢复的错误但仍推进游标，"
                        f"缺失类型: {error_types_str}"
                    )
                else:
                    cursor_advance_reason = "batch_complete"
            
            if target_commit:
                target_ts = _get_commit_timestamp(target_commit)
                target_sha = target_commit.sha
                
                # 确保水位线单调递增：使用 (ts, sha) 复合水位线比较
                # 仅当 (target_ts > cursor_ts) 或 (target_ts == cursor_ts 且 target_sha > cursor_sha) 时更新
                should_update = True
                if cursor_ts:
                    cursor_ts_tz = cursor_ts if cursor_ts.tzinfo else cursor_ts.replace(tzinfo=timezone.utc)
                    if target_ts < cursor_ts_tz:
                        # 新的时间戳比旧的小，保持原游标不变
                        should_update = False
                        cursor_advance_reason = "watermark_unchanged"
                        logger.debug(
                            f"水位线不变: target_ts={target_ts.isoformat()} < cursor_ts={cursor_ts_tz.isoformat()}"
                        )
                    elif target_ts == cursor_ts_tz:
                        # 时间戳相等时，比较 sha
                        if cursor_sha and target_sha <= cursor_sha:
                            should_update = False
                            cursor_advance_reason = "watermark_unchanged"
                            logger.debug(
                                f"水位线不变: target_ts={target_ts.isoformat()} == cursor_ts, "
                                f"target_sha={target_sha[:8]} <= cursor_sha={cursor_sha[:8]}"
                            )
                
                if should_update:
                    update_sync_cursor(
                        repo_id,
                        target_commit.sha,
                        target_ts.isoformat(),
                        synced_count,
                        config,
                    )
                    result["last_commit_sha"] = target_commit.sha
                    result["last_commit_ts"] = target_ts.isoformat()
                else:
                    # 保留原游标信息
                    result["last_commit_sha"] = cursor_sha
                    result["last_commit_ts"] = cursor_ts_str
            else:
                # 没有要推进的 commit
                result["last_commit_sha"] = cursor_sha
                result["last_commit_ts"] = cursor_ts_str
        
        result["cursor_advance_reason"] = cursor_advance_reason
        result["success"] = True
        result["has_more"] = len(all_commits) >= sync_config.batch_size
        
        # 更新 limiter 统计到 ClientStats
        client.update_stats_with_limiter_info()
        result["request_stats"] = client.stats.to_dict()  # 用于降级控制器
        
        # 标准字段: counts 统计
        result["counts"] = {
            "synced_count": result.get("synced_count", 0),
            "diff_count": result.get("diff_count", 0),
            "bulk_count": result.get("bulk_count", 0),
            "degraded_count": result.get("degraded_count", 0),
            "skipped_count": result.get("skipped_count", 0),
        }

        # 7. 创建 logbook item/event 和 attachments（如果有 patch 结果）
        if collected_patches:
            try:
                # 获取时间范围用于标题
                first_commit = all_commits[0] if all_commits else None
                last_commit = all_commits[-1] if all_commits else None
                time_range = ""
                if first_commit and last_commit:
                    first_sha = first_commit.sha[:8]
                    last_sha = last_commit.sha[:8]
                    time_range = f" {first_sha}..{last_sha}"

                # 创建同步批次 logbook item（包含 run_id）
                batch_title = f"GitLab Sync Batch{time_range} (repo_id={repo_id})"
                item_id = create_item(
                    item_type="scm_sync_batch",
                    title=batch_title,
                    scope_json={
                        "run_id": run_id,
                        "source_type": "git",
                        "repo_id": repo_id,
                        "synced_count": synced_count,
                        "first_commit_sha": first_commit.sha if first_commit else None,
                        "last_commit_sha": last_commit.sha if last_commit else None,
                    },
                    status="done",
                    config=config,
                )
                result["logbook_item_id"] = item_id
                logger.debug(f"[run_id={run_id[:8]}] 创建同步批次 logbook item: item_id={item_id}")

                # 添加 sync_completed 事件（包含请求统计与降级统计）
                event_payload = {
                    "run_id": run_id,
                    "synced_count": synced_count,
                    "patch_count": len(collected_patches),
                    "bulk_count": bulk_count,
                    "degraded_count": degraded_count,
                    # 请求统计
                    "request_stats": client.stats.to_dict(),
                }
                add_event(
                    item_id=item_id,
                    event_type="sync_completed",
                    payload_json=event_payload,
                    status_from="running",
                    status_to="done",
                    source="scm_sync_gitlab_commits",
                    config=config,
                )

                # 将 patch_blobs 以 kind='patch' 写入 attachments
                attachment_count = 0
                for patch_info in collected_patches:
                    if not patch_info:
                        continue
                    
                    # 构建 evidence ref
                    evidence_ref = build_evidence_ref_for_patch_blob(
                        source_type="git",
                        source_id=patch_info["source_id"],
                        sha256=patch_info["sha256"],
                        size_bytes=patch_info.get("size_bytes"),
                    )
                    
                    # 写入 attachment（uri 使用 canonical memory://...）
                    attach(
                        item_id=item_id,
                        kind="patch",
                        uri=evidence_ref["artifact_uri"],
                        sha256=patch_info["sha256"],
                        size_bytes=patch_info.get("size_bytes"),
                        meta_json={
                            "source_type": "git",
                            "source_id": patch_info["source_id"],
                            "commit_sha": patch_info["commit_sha"],
                            "format": patch_info.get("format"),
                            "degraded": patch_info.get("degraded", False),
                        },
                        config=config,
                    )
                    attachment_count += 1

                result["attachment_count"] = attachment_count
                logger.info(f"写入 {attachment_count} 个 patch attachments 到 logbook item {item_id}")

            except Exception as e:
                # logbook 写入失败不影响同步主流程
                logger.warning(f"创建 logbook 记录失败 (非致命): {e}")
                result["logbook_error"] = str(e)

    except EngramError as e:
        # 保留 EngramError 的错误分类
        result["error"] = str(e)
        result["error_category"] = getattr(e, "error_category", None) or getattr(e, "error_type", "engram_error")
        result["counts"] = {
            "synced_count": result.get("synced_count", 0),
            "diff_count": result.get("diff_count", 0),
            "skipped_count": result.get("skipped_count", 0),
        }
        raise
    except Exception as e:
        logger.exception(f"同步过程中发生错误: {e}")
        result["error"] = str(e)
        result["error_category"] = "exception"
        result["counts"] = {
            "synced_count": result.get("synced_count", 0),
            "diff_count": result.get("diff_count", 0),
            "skipped_count": result.get("skipped_count", 0),
        }
        raise GitLabSyncError(
            f"GitLab 同步失败: {e}",
            {"error": str(e)},
        )
    finally:
        # 释放分布式锁（确保在任何情况下都释放）
        if lock_acquired and lock_repo_id is not None:
            try:
                released = scm_sync_lock.release(
                    repo_id=lock_repo_id,
                    job_type=JOB_TYPE_GITLAB_COMMITS,
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
                if result.get("last_commit_sha"):
                    cursor_after = {
                        "last_commit_sha": result.get("last_commit_sha"),
                        "last_commit_ts": result.get("last_commit_ts"),
                    }
                
                # 构建计数统计（包含 limiter stats）
                request_stats = result.get("request_stats", {})
                counts = {
                    "synced_count": result.get("synced_count", 0),
                    "diff_count": result.get("diff_count", 0),
                    "bulk_count": result.get("bulk_count", 0),
                    "degraded_count": result.get("degraded_count", 0),
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
                
                # 构建降级信息
                degradation = None
                if result.get("degraded_reasons"):
                    degradation = {"degraded_reasons": result.get("degraded_reasons")}
                
                insert_sync_run_finish(
                    conn=sync_run_conn,
                    run_id=run_id,
                    status=final_status,
                    cursor_after=cursor_after,
                    counts=counts,
                    error_summary_json=error_summary,
                    degradation_json=degradation,
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
        description="GitLab Commits 同步脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 使用配置文件同步
    python scm_sync_gitlab_commits.py --config config.toml

    # 指定项目和 token
    python scm_sync_gitlab_commits.py --gitlab-url https://gitlab.com --project-id 12345 --token glpat-xxx

    # 循环同步直到完成
    python scm_sync_gitlab_commits.py --loop

    # Backfill 模式：回填指定时间范围的 commits（不更新游标）
    python scm_sync_gitlab_commits.py --backfill --since 2024-01-01 --until 2024-01-31

    # Backfill 模式：回填并更新游标
    python scm_sync_gitlab_commits.py --backfill --since 2024-01-01 --update-watermark

    # Backfill dry-run：预览将处理的范围
    python scm_sync_gitlab_commits.py --backfill --since 2024-01-01 --dry-run
        """,
    )

    add_config_argument(parser)

    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help=f"每次同步的最大 commit 数 (默认: {DEFAULT_BATCH_SIZE})",
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
        "--ref-name",
        type=str,
        default=None,
        help="分支/tag 名称",
    )

    parser.add_argument(
        "--no-diff",
        action="store_true",
        help="不获取 diff 内容（等同于 --diff-mode=none）",
    )

    parser.add_argument(
        "--strict",
        action="store_true",
        help="严格模式：发生不可恢复的 API 错误时，仅推进游标到最后一个成功处理的 commit",
    )

    parser.add_argument(
        "--sync-mode",
        type=str,
        choices=["strict", "best_effort"],
        default=None,
        help="同步模式: strict=严格模式(错误时不推进游标), best_effort=尽力模式(错误时记录并推进，默认)",
    )

    parser.add_argument(
        "--diff-mode",
        type=str,
        choices=["always", "best_effort", "none"],
        default=None,
        help="Diff 获取模式: always=失败则报错, best_effort=失败则降级(默认), none=不获取",
    )

    parser.add_argument(
        "--loop",
        action="store_true",
        help="循环同步直到全部完成",
    )

    # Backfill 模式参数
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Backfill 模式：回填指定时间范围的 commits，默认不更新游标",
    )

    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Backfill 起始时间（ISO 8601 格式，如 2024-01-01 或 2024-01-01T00:00:00Z），仅在 --backfill 模式下有效",
    )

    parser.add_argument(
        "--until",
        type=str,
        default=None,
        help="Backfill 结束时间（ISO 8601 格式），仅在 --backfill 模式下有效，默认为当前时间",
    )

    parser.add_argument(
        "--update-watermark",
        action="store_true",
        help="Backfill 完成后更新游标（默认不更新），仅在 --backfill 模式下有效",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅预览将处理的范围与计数，不写入 DB、不写入制品",
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


def _parse_datetime_arg(dt_str: str) -> datetime:
    """
    解析命令行时间参数为 datetime 对象
    
    支持格式:
    - ISO 8601 完整格式: 2024-01-01T00:00:00Z
    - 日期格式: 2024-01-01 (自动补充为 00:00:00 UTC)
    
    Args:
        dt_str: 时间字符串
        
    Returns:
        带时区的 datetime 对象
    """
    # 尝试完整 ISO 格式
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except ValueError:
        pass
    
    # 尝试日期格式
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    
    raise ValidationError(
        f"无法解析时间格式: {dt_str}",
        {"hint": "支持格式: 2024-01-01 或 2024-01-01T00:00:00Z"},
    )


def backfill_gitlab_commits(
    sync_config: SyncConfig,
    project_key: str,
    since: str,
    until: Optional[str] = None,
    update_watermark: bool = False,
    dry_run: bool = False,
    config: Optional[Config] = None,
    verbose: bool = True,
    fetch_diffs: bool = True,
) -> Dict[str, Any]:
    """
    执行 GitLab commits 回填（backfill）

    与增量同步不同，backfill 模式：
    - 使用指定的 since/until 时间范围（不依赖游标）
    - 默认不更新游标（除非 update_watermark=True）
    - 支持 dry_run 模式（仅输出范围，不写 DB）

    Args:
        sync_config: 同步配置
        project_key: 项目标识
        since: 起始时间（ISO 8601 格式）
        until: 结束时间（ISO 8601 格式），None 表示当前时间
        update_watermark: 是否更新游标
        dry_run: 仅预览，不写 DB
        config: Config 实例
        verbose: 是否输出详细信息
        fetch_diffs: 是否获取 diff

    Returns:
        回填结果统计
    """
    # diff_mode=none 时禁用 diff 获取
    if sync_config.diff_mode == DiffMode.NONE:
        fetch_diffs = False

    # 生成 run_id 用于追踪本次运行
    run_id = str(uuid.uuid4())

    result = {
        "success": False,
        "mode": "backfill",
        "run_id": run_id,
        "dry_run": dry_run,
        "update_watermark": update_watermark,
        "synced_count": 0,
        "diff_count": 0,
        "since": since,
        "until": until,
        "error": None,
        "error_category": None,  # 标准字段
        "retry_after": None,  # 标准字段
        "counts": {},  # 标准字段
    }

    logger.info(f"[run_id={run_id[:8]}] 开始 GitLab commits backfill (dry_run={dry_run})")

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

    # 1. 获取或创建仓库记录
    repo_url = build_gitlab_repo_url(sync_config.gitlab_url, sync_config.project_id)

    if dry_run:
        logger.info("[dry-run] 检查仓库配置...")

    repo_id = scm_repo.ensure_repo(
        repo_type="git",
        url=repo_url,
        project_key=project_key,
        default_branch=sync_config.ref_name,
        config=config,
    )
    result["repo_id"] = repo_id

    # 2. 确定时间范围
    if until is None:
        until = datetime.now(timezone.utc).isoformat()
        logger.info(f"结束时间未指定，使用当前时间: {until}")

    result["until"] = until

    logger.info(f"Backfill 范围: since={since}, until={until}")

    # 3. dry-run 模式：获取 commit 数量预览
    if dry_run:
        logger.info("[dry-run] 模式：获取 commit 数量预览...")

        # 拉取一页来估算
        try:
            commits_data = client.get_commits(
                sync_config.project_id,
                since=since,
                until=until,
                ref_name=sync_config.ref_name,
                per_page=1,
                page=1,
            )
            # 这里只能估算，实际需要遍历所有页
            # 简化处理：显示第一页信息
            logger.info(f"[dry-run] 仓库 ID: {repo_id}")
            logger.info(f"[dry-run] 时间范围: {since} ~ {until}")
            logger.info(f"[dry-run] 分支: {sync_config.ref_name or '默认'}")
            logger.info(f"[dry-run] 更新游标: {update_watermark}")
            logger.info(f"[dry-run] 获取 diffs: {fetch_diffs}")
            
            if commits_data:
                logger.info(f"[dry-run] 检测到有 commits 在指定范围内")
            else:
                logger.info(f"[dry-run] 指定范围内无 commits")

            result["success"] = True
            result["message"] = f"[dry-run] 时间范围: {since} ~ {until}"
            result["counts"] = {"synced_count": 0, "diff_count": 0}
            return result
        except Exception as e:
            logger.warning(f"[dry-run] 预览获取失败: {e}")
            result["success"] = True
            result["message"] = f"[dry-run] 时间范围: {since} ~ {until} (预览请求失败)"
            result["counts"] = {"synced_count": 0, "diff_count": 0}
            return result

    # 4. 执行实际同步
    conn = get_connection(config=config)
    try:
        # 拉取 commits（分页）
        logger.info(f"从 GitLab 获取 commits (project_id={sync_config.project_id}, since={since}, until={until})")

        all_commits: List[GitCommit] = []
        seen_shas: set = set()
        page = 1

        while True:
            commits_data = client.get_commits(
                sync_config.project_id,
                since=since,
                until=until,
                ref_name=sync_config.ref_name,
                per_page=min(sync_config.batch_size, 100),
                page=page,
            )

            if not commits_data:
                break

            for data in commits_data:
                commit = parse_commit(data)
                if commit.sha not in seen_shas:
                    seen_shas.add(commit.sha)
                    all_commits.append(commit)

            # 达到 batch_size 限制
            if len(all_commits) >= sync_config.batch_size:
                break

            # 检查是否还有更多页
            if len(commits_data) < min(sync_config.batch_size, 100):
                break

            page += 1

        logger.info(f"获取到 {len(all_commits)} 个 commits")

        # 按时间排序
        all_commits.sort(key=_get_commit_sort_key)

        if not all_commits:
            logger.info("指定范围内无 commits")
            result["success"] = True
            result["message"] = "指定范围内无 commits"
            result["counts"] = {"synced_count": 0, "diff_count": 0}
            return result

        # 写入 git_commits 表
        synced_count = insert_git_commits(conn, repo_id, all_commits, config)
        logger.info(f"写入 git_commits: {synced_count} 条记录")
        result["synced_count"] = synced_count

        # 获取并存储 diff
        diff_count = 0
        bulk_count = 0
        degraded_count = 0
        collected_patches = []

        if fetch_diffs:
            for commit in all_commits:
                try:
                    fetch_result = get_commit_diff_safe(
                        client, sync_config.project_id, commit.sha
                    )

                    is_degraded = False
                    degrade_reason = None
                    source_fetch_error = None
                    original_endpoint = None

                    if not fetch_result.success:
                        is_degraded = True
                        degrade_reason = fetch_result.error_category
                        source_fetch_error = fetch_result.error_message
                        original_endpoint = fetch_result.endpoint

                        content_to_store = generate_ministat_from_stats(
                            commit.stats, commit_sha=commit.sha
                        )
                        patch_format = "ministat"
                        degraded_count += 1
                    else:
                        diffs = fetch_result.diffs or []
                        if not diffs:
                            continue

                        diff_content = format_diff_content(diffs)
                        diff_size_bytes = len(diff_content.encode("utf-8")) if diff_content else 0

                        stats = commit.stats
                        total_changes = stats.get("additions", 0) + stats.get("deletions", 0)
                        change_summary = ChangeSummary(
                            total_changes=total_changes,
                            files_changed=len(diffs),
                            diff_size_bytes=diff_size_bytes,
                        )
                        bulk_flag, bulk_reason = is_bulk(change_summary, config)

                        if bulk_flag:
                            content_to_store = generate_diffstat(diffs)
                            patch_format = "diffstat"
                            bulk_count += 1
                        else:
                            content_to_store = diff_content
                            patch_format = "diff"

                    if content_to_store:
                        patch_result = insert_patch_blob(
                            conn,
                            repo_id,
                            commit.sha,
                            content_to_store,
                            patch_format,
                            is_degraded=is_degraded,
                            degrade_reason=degrade_reason,
                            source_fetch_error=source_fetch_error,
                            original_endpoint=original_endpoint,
                            project_key=project_key,
                        )
                        if patch_result:
                            diff_count += 1
                            collected_patches.append(patch_result)

                except Exception as e:
                    logger.warning(f"处理 diff 失败 (commit={commit.sha[:8]}): {e}")

            logger.info(f"写入 patch_blobs: {diff_count} 条记录 (bulk={bulk_count}, degraded={degraded_count})")
            result["diff_count"] = diff_count
            result["bulk_count"] = bulk_count
            result["degraded_count"] = degraded_count

        # 5. 更新游标（仅当 update_watermark=True）
        if update_watermark and all_commits:
            target_commit = all_commits[-1]  # 最新的 commit
            target_ts = _get_commit_timestamp(target_commit)
            update_sync_cursor(
                repo_id,
                target_commit.sha,
                target_ts.isoformat(),
                synced_count,
                config,
            )
            result["last_commit_sha"] = target_commit.sha
            result["last_commit_ts"] = target_ts.isoformat()
            result["watermark_updated"] = True
        else:
            result["watermark_updated"] = False
            if all_commits:
                logger.info("Backfill 模式：跳过游标更新（使用 --update-watermark 可更新）")

        result["success"] = True
        # 标准字段: counts 统计
        result["counts"] = {
            "synced_count": result.get("synced_count", 0),
            "diff_count": result.get("diff_count", 0),
            "bulk_count": result.get("bulk_count", 0),
            "degraded_count": result.get("degraded_count", 0),
        }

    except EngramError as e:
        result["error"] = str(e)
        result["error_category"] = getattr(e, "error_category", None) or getattr(e, "error_type", "engram_error")
        result["counts"] = {
            "synced_count": result.get("synced_count", 0),
            "diff_count": result.get("diff_count", 0),
        }
        raise
    except Exception as e:
        logger.exception(f"Backfill 过程中发生错误: {e}")
        result["error"] = str(e)
        result["error_category"] = "exception"
        result["counts"] = {
            "synced_count": result.get("synced_count", 0),
            "diff_count": result.get("diff_count", 0),
        }
        raise GitLabSyncError(
            f"GitLab Backfill 失败: {e}",
            {"error": str(e)},
        )
    finally:
        conn.close()

    return result


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

        # 解析 diff_mode
        # 优先级: CLI --diff-mode > CLI --no-diff > 配置文件 > 默认值
        if args.diff_mode:
            diff_mode = args.diff_mode
        elif args.no_diff:
            diff_mode = DiffMode.NONE
        else:
            diff_mode = gitlab_cfg.get("diff_mode") or DiffMode.BEST_EFFORT

        # 解析 strict 模式（使用统一的配置函数）
        # 优先级: CLI --strict > CLI --sync-mode > 配置文件 scm.sync.mode > 默认值
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
            ref_name=args.ref_name or gitlab_cfg.get("ref_name"),
            request_timeout=gitlab_cfg.get("request_timeout") or DEFAULT_REQUEST_TIMEOUT,
            overlap_seconds=incremental_cfg["overlap_seconds"],
            time_window_days=incremental_cfg["time_window_days"],
            forward_window_seconds=incremental_cfg["forward_window_seconds"],
            forward_window_min_seconds=incremental_cfg["forward_window_min_seconds"],
            adaptive_shrink_factor=incremental_cfg["adaptive_shrink_factor"],
            adaptive_grow_factor=incremental_cfg["adaptive_grow_factor"],
            adaptive_commit_threshold=incremental_cfg["adaptive_commit_threshold"],
            strict=strict,
            diff_mode=diff_mode,
        )

        logger.info(f"GitLab URL: {sync_config.gitlab_url}")
        logger.info(f"Project ID: {sync_config.project_id}")
        logger.info(f"Batch size: {sync_config.batch_size}")
        logger.info(f"Overlap: {sync_config.overlap_seconds}s, Time window: {sync_config.time_window_days}d")
        logger.info(f"Strict mode: {sync_config.strict}, Diff mode: {sync_config.diff_mode}")

        # Backfill 模式
        if args.backfill:
            if args.since is None:
                raise ValidationError(
                    "Backfill 模式需要指定 --since 参数",
                    {"hint": "示例: --backfill --since 2024-01-01 --until 2024-01-31"},
                )

            logger.info("进入 Backfill 模式")
            if args.dry_run:
                logger.info("[dry-run] 仅预览，不执行实际同步")

            result = backfill_gitlab_commits(
                sync_config,
                project_key,
                since=args.since,
                until=args.until,
                update_watermark=args.update_watermark,
                dry_run=args.dry_run,
                config=config,
                verbose=args.verbose,
                fetch_diffs=not args.no_diff,
            )

            if args.json:
                print(json.dumps(result, default=str, ensure_ascii=False))

            return 0

        # 普通增量同步模式
        if args.dry_run:
            raise ValidationError(
                "--dry-run 仅在 --backfill 模式下有效",
                {"hint": "示例: --backfill --since 2024-01-01 --dry-run"},
            )

        # 执行同步
        total_synced = 0
        total_diffs = 0
        loop_count = 0
        max_loops = 1000  # 防止无限循环
        
        # 初始化降级控制器（loop 模式下使用）
        degradation_controller = None
        if args.loop:
            degradation_config = DegradationConfig.from_config(config)
            degradation_controller = DegradationController(
                config=degradation_config,
                initial_diff_mode=sync_config.diff_mode,
                initial_batch_size=sync_config.batch_size,
                initial_forward_window_seconds=sync_config.forward_window_seconds,
            )
            logger.info("启用降级控制器 (loop 模式)")

        while True:
            loop_count += 1
            if loop_count > max_loops:
                logger.warning(f"达到最大循环次数限制 ({max_loops})，退出")
                break

            result = sync_gitlab_commits(
                sync_config,
                project_key,
                config,
                verbose=args.verbose,
                fetch_diffs=not args.no_diff and sync_config.diff_mode != DiffMode.NONE,
            )
            total_synced += result.get("synced_count", 0)
            total_diffs += result.get("diff_count", 0)

            if args.json:
                print(json.dumps(result, default=str, ensure_ascii=False))

            if not args.loop or not result.get("has_more", False):
                break
            
            # loop 模式下，使用降级控制器调整下一轮参数
            if degradation_controller:
                suggestion = degradation_controller.update(
                    request_stats=result.get("request_stats"),
                    unrecoverable_errors=result.get("unrecoverable_errors", []),
                    degraded_count=result.get("degraded_count", 0),
                    bulk_count=result.get("bulk_count", 0),
                    synced_count=result.get("synced_count", 0),
                )
                
                # 应用建议到 sync_config
                sync_config.diff_mode = suggestion.diff_mode
                sync_config.batch_size = suggestion.batch_size
                sync_config.forward_window_seconds = suggestion.forward_window_seconds
                
                # 如果需要暂停，等待
                if suggestion.should_pause:
                    logger.warning(f"降级控制器建议暂停: {suggestion.pause_reason}")
                
                if suggestion.sleep_seconds > 0:
                    logger.info(f"等待 {suggestion.sleep_seconds:.1f}s 后继续...")
                    import time
                    time.sleep(suggestion.sleep_seconds)

            logger.info(f"继续同步下一批（第 {loop_count + 1} 轮）...")

        if args.loop and not args.json:
            logger.info(f"同步完成，共 {loop_count} 轮，总计 {total_synced} 个 commits，{total_diffs} 个 diffs")
            if degradation_controller:
                logger.info(f"降级控制器状态: {degradation_controller.get_state()}")

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
