#!/usr/bin/env python3
"""
scm_materialize_patch_blob.py - 物化 patch blob 脚本

功能:
- 读取 scm.patch_blobs 表中 uri 不可解析的记录
- 根据 source_type 调用 SVN/GitLab 拉取 diff 内容
- 写入 ArtifactStore 并计算 sha256
- 安全策略更新 DB（仅当 sha256 匹配时可更新 uri 或写入 mirror 信息）

使用:
    python scm_materialize_patch_blob.py [--config PATH] [--blob-id ID] [--source-type TYPE] [--batch-size N] [--verbose]

配置文件示例:
    [materialize]
    batch_size = 50           # 每次处理最大 blob 数
    retry_failed = false      # 是否重试之前失败的记录
"""

import argparse
import hashlib
import json
import logging
import subprocess
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.rows import dict_row

from engram_step1.config import Config, add_config_argument, get_config, get_gitlab_config, get_http_config, get_svn_config
from engram_step1.db import get_connection
from db import (
    select_pending_blobs_for_materialize,
    mark_blob_in_progress,
    mark_blob_done,
    mark_blob_failed,
    MATERIALIZE_STATUS_PENDING,
    MATERIALIZE_STATUS_DONE,
    MATERIALIZE_STATUS_FAILED,
)
from engram_step1.errors import DatabaseError, EngramError, ValidationError
from engram_step1.uri import parse_uri, UriType, build_evidence_uri
from engram_step1.scm_auth import (
    TokenProvider,
    create_gitlab_token_provider,
    mask_token,
    redact,
)
from engram_step1.gitlab_client import (
    GitLabClient,
    GitLabAPIError,
    GitLabAuthError,
    GitLabTimeoutError,
    GitLabNetworkError,
    GitLabErrorCategory,
    HttpConfig,
)
from artifacts import (
    write_text_artifact,
    get_scm_path,
    artifact_exists,
    read_artifact,
    build_scm_artifact_path,
    build_legacy_scm_path,
    SCM_EXT_DIFF,
    SCM_EXT_DIFFSTAT,
    SCM_EXT_MINISTAT,
)
from engram_step1.artifact_store import get_default_store
from scm_sync_svn import run_svn_cmd, _mask_svn_command_for_log
from scm_sync_svn import generate_diffstat as svn_generate_diffstat
from scm_sync_svn import generate_ministat_from_changed_paths
from scm_sync_gitlab_commits import generate_ministat_from_stats

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# 默认配置值
DEFAULT_BATCH_SIZE = 50
DEFAULT_REQUEST_TIMEOUT = 60

# 最大 diff 内容大小 (10MB)
MAX_DIFF_SIZE_BYTES = 10 * 1024 * 1024


class MaterializeError(EngramError):
    """物化错误基类"""
    exit_code = 12
    error_type = "MATERIALIZE_ERROR"


class UriNotResolvableError(MaterializeError):
    """URI 不可解析"""
    error_type = "URI_NOT_RESOLVABLE"


class ChecksumMismatchError(MaterializeError):
    """校验和不匹配"""
    error_type = "CHECKSUM_MISMATCH"


class PayloadTooLargeError(MaterializeError):
    """内容超过大小限制"""
    error_type = "PAYLOAD_TOO_LARGE"


class FetchError(MaterializeError):
    """拉取内容失败"""
    error_type = "FETCH_ERROR"


class MaterializeStatus(Enum):
    """物化状态枚举"""
    PENDING = "pending"           # 待处理
    MATERIALIZED = "materialized" # 已物化
    FAILED = "failed"             # 失败
    SKIPPED = "skipped"           # 跳过（已存在）
    UNREACHABLE = "unreachable"   # 外部 URI 不可达


class ErrorCategory(str, Enum):
    """物化错误分类"""
    TIMEOUT = "timeout"                   # 请求超时
    HTTP_ERROR = "http_error"             # HTTP 错误（4xx/5xx）
    AUTH_ERROR = "auth_error"             # 认证错误（401/403）
    CONTENT_TOO_LARGE = "content_too_large"  # 内容超大小限制
    NETWORK_ERROR = "network_error"       # 网络连接错误
    VALIDATION_ERROR = "validation_error" # 验证错误
    UNKNOWN = "unknown"                   # 未知错误


class ShaMismatchPolicy(str, Enum):
    """SHA 不匹配时的处理策略"""
    STRICT = "strict"   # 不写入制品，只标记失败
    MIRROR = "mirror"   # 写入制品到 v2 路径（基于 actual_sha256），并记录 mirror 信息


@dataclass
class PatchBlobRecord:
    """Patch blob 记录"""
    blob_id: int
    source_type: str
    source_id: str
    uri: Optional[str]
    sha256: str
    size_bytes: Optional[int]
    format: str
    meta_json: Optional[Dict[str, Any]] = None


@dataclass
class MaterializeResult:
    """物化结果"""
    blob_id: int
    status: MaterializeStatus
    uri: Optional[str] = None
    sha256: Optional[str] = None
    size_bytes: Optional[int] = None
    error: Optional[str] = None
    attempts: int = 1  # 本次物化尝试次数
    error_category: Optional[ErrorCategory] = None  # 错误分类
    endpoint: Optional[str] = None  # 请求的 endpoint
    status_code: Optional[int] = None  # HTTP 状态码


def get_blobs_by_attachment_id(
    conn: psycopg.Connection,
    attachment_id: int,
) -> List[Dict[str, Any]]:
    """
    根据 attachment_id 反查关联的 patch_blobs 记录
    
    关联方式:
    1. 通过 sha256 关联: attachments.sha256 = patch_blobs.sha256
    2. 通过 meta_json 中的 blob_id 关联: attachments.meta_json.blob_id = patch_blobs.blob_id
    
    Args:
        conn: 数据库连接
        attachment_id: 附件 ID
        
    Returns:
        关联的 patch_blobs 记录列表
    """
    results = []
    with conn.cursor(row_factory=dict_row) as cur:
        # 先获取 attachment 信息
        cur.execute(
            """
            SELECT attachment_id, item_id, kind, uri, sha256, size_bytes, meta_json
            FROM logbook.attachments
            WHERE attachment_id = %s
            """,
            (attachment_id,),
        )
        attachment = cur.fetchone()
        
        if not attachment:
            logger.warning(f"attachment_id={attachment_id} 不存在")
            return []
        
        # 方式1: 通过 sha256 关联
        if attachment.get("sha256"):
            cur.execute(
                """
                SELECT blob_id, source_type, source_id, uri, sha256, size_bytes, 
                       format, meta_json
                FROM scm.patch_blobs
                WHERE sha256 = %s
                FOR UPDATE SKIP LOCKED
                """,
                (attachment["sha256"],),
            )
            rows = cur.fetchall()
            for row in rows:
                meta = row.get("meta_json")
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except json.JSONDecodeError:
                        meta = {}
                row["meta_json"] = meta or {}
                results.append(dict(row))
        
        # 方式2: 通过 meta_json.blob_id 关联
        if not results:
            meta_json = attachment.get("meta_json")
            if isinstance(meta_json, str):
                try:
                    meta_json = json.loads(meta_json)
                except json.JSONDecodeError:
                    meta_json = {}
            
            if meta_json and meta_json.get("blob_id"):
                cur.execute(
                    """
                    SELECT blob_id, source_type, source_id, uri, sha256, size_bytes, 
                           format, meta_json
                    FROM scm.patch_blobs
                    WHERE blob_id = %s
                    FOR UPDATE SKIP LOCKED
                    """,
                    (meta_json["blob_id"],),
                )
                rows = cur.fetchall()
                for row in rows:
                    meta = row.get("meta_json")
                    if isinstance(meta, str):
                        try:
                            meta = json.loads(meta)
                        except json.JSONDecodeError:
                            meta = {}
                    row["meta_json"] = meta or {}
                    results.append(dict(row))
    
    return results


def get_blobs_by_attachment_kind(
    conn: psycopg.Connection,
    kind: str,
    materialize_missing: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> List[Dict[str, Any]]:
    """
    根据 attachment kind 获取关联的待物化 patch_blobs 记录
    
    关联方式:
    - 通过 sha256 关联: attachments.sha256 = patch_blobs.sha256
    - 筛选条件: attachments.kind = kind
    
    Args:
        conn: 数据库连接
        kind: 附件类型（如 'patch'）
        materialize_missing: 是否只返回未物化的记录
        batch_size: 最大返回数量
        
    Returns:
        关联的 patch_blobs 记录列表
    """
    results = []
    
    # 构建物化状态条件
    materialize_condition = ""
    if materialize_missing:
        materialize_condition = """
            AND (
                pb.uri IS NULL 
                OR pb.uri = ''
                OR pb.meta_json->>'materialize_status' IN ('pending', 'failed')
            )
        """
    
    query = f"""
        SELECT DISTINCT 
            pb.blob_id, pb.source_type, pb.source_id, pb.uri, pb.sha256, 
            pb.size_bytes, pb.format, pb.meta_json,
            a.attachment_id AS ref_attachment_id
        FROM logbook.attachments a
        JOIN scm.patch_blobs pb ON a.sha256 = pb.sha256
        WHERE a.kind = %s
        {materialize_condition}
        ORDER BY pb.blob_id
        LIMIT %s
        FOR UPDATE OF pb SKIP LOCKED
    """
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, (kind, batch_size))
        for row in cur.fetchall():
            meta = row.get("meta_json")
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except json.JSONDecodeError:
                    meta = {}
            row["meta_json"] = meta or {}
            results.append(dict(row))
    
    return results


def parse_source_id(source_id: str) -> Tuple[str, str]:
    """
    解析 source_id 为 (repo_id, revision/sha)
    
    source_id 格式: <repo_id>:<rev> 或 <repo_id>:<sha>
    
    Args:
        source_id: 源 ID 字符串
        
    Returns:
        (repo_id, revision/sha) 元组
        
    Raises:
        ValidationError: source_id 格式无效
    """
    if ":" not in source_id:
        raise ValidationError(
            f"无效的 source_id 格式: {source_id}",
            {"source_id": source_id, "expected": "<repo_id>:<rev_or_sha>"},
        )
    
    parts = source_id.split(":", 1)
    return parts[0], parts[1]


def is_uri_resolvable(uri: Optional[str]) -> bool:
    """
    判断 URI 是否可解析（指向有效的本地文件）
    
    Args:
        uri: URI 字符串
        
    Returns:
        True 如果 URI 可解析到本地文件
    """
    if not uri:
        return False
    
    parsed = parse_uri(uri)
    
    # 本地 artifact URI
    if parsed.uri_type == UriType.ARTIFACT:
        return artifact_exists(uri)
    
    # file:// URI
    if parsed.uri_type == UriType.FILE:
        try:
            from pathlib import Path
            return Path(parsed.path).exists()
        except Exception:
            return False
    
    # 远程 URI 暂不验证可达性
    if parsed.is_remote:
        return True  # 假设可达，实际拉取时再验证
    
    return False


def get_git_commit_meta(
    conn: psycopg.Connection,
    repo_id: int,
    commit_sha: str,
) -> Optional[Dict[str, Any]]:
    """
    获取 git_commits 记录的 meta_json
    
    用于获取 stats 信息生成 ministat。
    
    Args:
        conn: 数据库连接
        repo_id: 仓库 ID
        commit_sha: commit SHA
        
    Returns:
        meta_json 字典，不存在返回 None
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT meta_json
            FROM scm.git_commits
            WHERE repo_id = %s AND commit_sha = %s
            """,
            (repo_id, commit_sha),
        )
        row = cur.fetchone()
        if not row:
            return None
        
        meta = row.get("meta_json")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        return meta or {}


def get_svn_revision_meta(
    conn: psycopg.Connection,
    repo_id: int,
    rev_num: int,
) -> Optional[Dict[str, Any]]:
    """
    获取 svn_revisions 记录的 meta_json
    
    用于获取 changed_paths 信息生成 ministat。
    
    Args:
        conn: 数据库连接
        repo_id: 仓库 ID
        rev_num: revision 号
        
    Returns:
        meta_json 字典，不存在返回 None
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT meta_json
            FROM scm.svn_revisions
            WHERE repo_id = %s AND COALESCE(rev_num, rev_id) = %s
            """,
            (repo_id, rev_num),
        )
        row = cur.fetchone()
        if not row:
            return None
        
        meta = row.get("meta_json")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        return meta or {}


def get_repo_info(conn: psycopg.Connection, repo_id: int) -> Optional[Dict[str, Any]]:
    """
    获取仓库信息
    
    Args:
        conn: 数据库连接
        repo_id: 仓库 ID
        
    Returns:
        仓库信息字典，不存在返回 None
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT repo_id, repo_type, url, project_key, default_branch
            FROM scm.repos
            WHERE repo_id = %s
            """,
            (repo_id,),
        )
        return cur.fetchone()


def fetch_svn_diff(
    svn_url: str,
    revision: int,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    config: Optional[Config] = None,
) -> Optional[str]:
    """
    从 SVN 获取指定 revision 的 diff
    
    使用统一的 run_svn_cmd() 执行，支持：
    - 自动注入认证参数（从配置读取）
    - --non-interactive 和 --trust-server-cert-failures（由配置控制）
    - 日志脱敏处理（不打印 password）
    - 错误分类（timeout/command_error/auth_error）
    
    Args:
        svn_url: SVN 仓库 URL
        revision: revision 号
        timeout: 超时时间（秒）
        config: 配置实例
        
    Returns:
        diff 内容，失败返回 None
        
    Raises:
        FetchError: 获取失败（包含错误分类信息）
    """
    cmd = ["svn", "diff", "-c", str(revision), svn_url]
    
    logger.debug(f"拉取 SVN diff: r{revision}")
    
    result = run_svn_cmd(cmd, timeout=timeout, config=config)
    
    if not result.success:
        # 映射 SVN 错误类型到物化错误分类
        error_category = ErrorCategory.UNKNOWN
        if result.error_type == "timeout":
            error_category = ErrorCategory.TIMEOUT
        elif result.error_type == "auth_error":
            error_category = ErrorCategory.AUTH_ERROR
        elif result.error_type == "command_error":
            error_category = ErrorCategory.HTTP_ERROR  # 使用通用错误分类
        
        # 对 URL 和错误消息进行脱敏
        error_details = {
            "svn_url": redact(svn_url),
            "revision": revision,
            "error_type": result.error_type,
            "error_category": error_category.value,
        }
        
        if result.error_type == "timeout":
            raise FetchError(
                f"svn diff -c {revision} 超时 ({timeout}s)",
                error_details,
            )
        elif result.error_type == "auth_error":
            raise FetchError(
                f"svn diff -c {revision} 认证失败: {redact(result.error_message)}",
                error_details,
            )
        else:
            raise FetchError(
                f"svn diff -c {revision} 执行失败: {redact(result.error_message)}",
                error_details,
            )
    
    return result.stdout


def fetch_gitlab_commit_diff(
    gitlab_url: str,
    project_id: str,
    commit_sha: str,
    token_provider: TokenProvider,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    config: Optional[Config] = None,
) -> Optional[str]:
    """
    从 GitLab 获取指定 commit 的 diff
    
    使用统一的 GitLabClient 模块，支持自动重试和退避策略。
    
    Args:
        gitlab_url: GitLab 实例 URL
        project_id: 项目 ID 或路径
        commit_sha: commit SHA
        token_provider: Token 提供者
        timeout: 超时时间（秒）
        config: 配置对象
        
    Returns:
        格式化的 diff 内容
        
    Raises:
        FetchError: 获取失败
    """
    # 构建 HTTP 配置
    http_cfg = get_http_config(config) if config else {}
    http_config = HttpConfig(
        timeout_seconds=timeout,
        max_attempts=http_cfg.get("max_attempts", 3) if http_cfg else 3,
        backoff_base_seconds=http_cfg.get("backoff_base_seconds", 1.0) if http_cfg else 1.0,
        backoff_max_seconds=http_cfg.get("backoff_max_seconds", 60.0) if http_cfg else 60.0,
    )
    
    # 创建 GitLab 客户端
    client = GitLabClient(
        base_url=gitlab_url,
        token_provider=token_provider,
        http_config=http_config,
    )
    
    logger.debug(f"请求 GitLab API: project={project_id}, commit={commit_sha[:8]}")
    
    try:
        diffs = client.get_commit_diff(project_id, commit_sha)
        
        if not diffs:
            return ""
        
        # 格式化为 unified diff
        parts = []
        for d in diffs:
            old_path = d.get("old_path", "/dev/null")
            new_path = d.get("new_path", "/dev/null")
            diff_header = f"--- a/{old_path}\n+++ b/{new_path}\n"
            diff_content = d.get("diff", "")
            parts.append(diff_header + diff_content)
        
        return "\n".join(parts)
        
    except GitLabAPIError as e:
        # 映射 GitLab 错误分类到物化错误分类
        error_category = ErrorCategory.HTTP_ERROR
        if e.category == GitLabErrorCategory.TIMEOUT:
            error_category = ErrorCategory.TIMEOUT
        elif e.category == GitLabErrorCategory.AUTH_ERROR:
            error_category = ErrorCategory.AUTH_ERROR
        elif e.category == GitLabErrorCategory.NETWORK_ERROR:
            error_category = ErrorCategory.NETWORK_ERROR
        elif e.category == GitLabErrorCategory.CONTENT_TOO_LARGE:
            error_category = ErrorCategory.CONTENT_TOO_LARGE
        
        # 对敏感信息进行脱敏
        raise FetchError(
            redact(e.message),
            {
                "project_id": project_id,
                "commit_sha": commit_sha,
                "status_code": e.status_code,
                "endpoint": redact(e.endpoint),
                "error_category": error_category.value,
            },
        )


def generate_artifact_uri(
    source_type: str,
    repo_id: str,
    rev_or_sha: str,
    sha256: str,
    patch_format: str = "diff",
    project_key: Optional[str] = None,
) -> str:
    """
    生成 artifact 存储路径（新版统一格式）
    
    新版路径规范:
        scm/<project_key>/<repo_id>/<source_type>/<rev_or_sha>/<sha256>.<ext>
    
    rev_or_sha 格式规范:
        - SVN: 自动转换为 r<rev> 格式（如输入 "100" 转为 "r100"）
        - Git: 保持原样，应为完整 40 位 SHA 或至少 7 位短 SHA
    
    Args:
        source_type: 源类型 (svn/git)
        repo_id: 仓库 ID
        rev_or_sha: revision 号（SVN，可为纯数字或 r<rev>）或 commit SHA（Git）
        sha256: 内容的 SHA256 哈希值
        patch_format: 格式类型 (diff/diffstat/ministat)
        project_key: 项目标识（可选，默认使用配置文件中的值）
        
    Returns:
        artifact 相对路径
    
    注意:
        - 如果 project_key 未提供且配置中也没有，将使用 "default" 作为默认值
        - 旧版路径仍可通过 resolve_scm_artifact_path() 读取
    
    示例:
        # SVN: 纯数字会自动加 r 前缀
        generate_artifact_uri("svn", "1", "100", "abc123...", "diff")
        # => "scm/default/1/svn/r100/abc123....diff"
        
        # SVN: 已有 r 前缀则保持
        generate_artifact_uri("svn", "1", "r100", "abc123...", "diff")
        # => "scm/default/1/svn/r100/abc123....diff"
        
        # Git: SHA 保持原样
        generate_artifact_uri("git", "2", "abc123def...", "e3b0c4...", "diff")
        # => "scm/default/2/git/abc123def.../e3b0c4....diff"
    """
    # 获取 project_key
    if not project_key:
        try:
            from engram_step1.config import get_config
            cfg = get_config()
            project_key = cfg.get("project.project_key", "default")
        except Exception:
            project_key = "default"
    
    # 映射 patch_format 到扩展名
    ext_map = {
        "diff": SCM_EXT_DIFF,
        "diffstat": SCM_EXT_DIFFSTAT,
        "ministat": SCM_EXT_MINISTAT,
    }
    ext = ext_map.get(patch_format, SCM_EXT_DIFF)
    
    # 规范化 rev_or_sha 格式
    # SVN: 确保以 r 前缀（如 "100" -> "r100"）
    # Git: 保持原样
    normalized_rev_or_sha = rev_or_sha
    if source_type == "svn":
        if not rev_or_sha.startswith("r"):
            normalized_rev_or_sha = f"r{rev_or_sha}"
    
    return build_scm_artifact_path(
        project_key=project_key,
        repo_id=repo_id,
        source_type=source_type,
        rev_or_sha=normalized_rev_or_sha,
        sha256=sha256,
        ext=ext,
    )


def generate_legacy_artifact_uri(
    source_type: str,
    repo_id: str,
    rev_or_sha: str,
    patch_format: str = "diff",
) -> str:
    """
    生成旧版 artifact 存储路径（用于兼容读取）
    
    旧版路径格式:
        SVN: scm/<repo_id>/svn/r<rev>.<ext>
        Git: scm/<repo_id>/git/commits/<sha>.<ext>
    
    Args:
        source_type: 源类型 (svn/git)
        repo_id: 仓库 ID
        rev_or_sha: revision 号或 commit SHA
        patch_format: 格式类型 (diff/diffstat)
        
    Returns:
        旧版格式的 artifact 相对路径
    
    注意:
        此函数仅用于读取旧版数据，新写入应使用 generate_artifact_uri()
    """
    ext_map = {
        "diff": SCM_EXT_DIFF,
        "diffstat": SCM_EXT_DIFFSTAT,
        "ministat": SCM_EXT_MINISTAT,
    }
    ext = ext_map.get(patch_format, SCM_EXT_DIFF)
    
    return build_legacy_scm_path(
        repo_id=repo_id,
        source_type=source_type,
        rev_or_sha=rev_or_sha,
        ext=ext,
    )


def compute_sha256(content: str) -> str:
    """计算内容的 SHA256 哈希"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def get_pending_blobs(
    conn: psycopg.Connection,
    source_type: Optional[str] = None,
    blob_id: Optional[int] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    retry_failed: bool = False,
) -> List[PatchBlobRecord]:
    """
    获取待物化的 patch blob 记录（并发安全）
    
    使用 FOR UPDATE SKIP LOCKED 避免并发重复处理。
    
    筛选条件:
    - uri 为空或不可解析
    - 或 meta_json.materialize_status in ('pending', 'failed')
    - 可选: 指定 source_type 或 blob_id
    
    Args:
        conn: 数据库连接
        source_type: 可选，筛选特定源类型
        blob_id: 可选，处理特定 blob
        batch_size: 最大返回数量
        retry_failed: 是否重试之前失败的记录
        
    Returns:
        PatchBlobRecord 列表
    """
    # 如果指定了 blob_id，使用单独查询
    if blob_id:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT blob_id, source_type, source_id, uri, sha256, size_bytes, format,
                       meta_json
                FROM scm.patch_blobs
                WHERE blob_id = %s
                FOR UPDATE SKIP LOCKED
                """,
                (blob_id,),
            )
            row = cur.fetchone()
            if not row:
                return []
            
            meta = row.get("meta_json")
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except json.JSONDecodeError:
                    meta = {}
            
            return [PatchBlobRecord(
                blob_id=row["blob_id"],
                source_type=row["source_type"],
                source_id=row["source_id"],
                uri=row["uri"],
                sha256=row["sha256"],
                size_bytes=row["size_bytes"],
                format=row["format"],
                meta_json=meta or {},
            )]
    
    # 使用新的并发安全查询函数
    rows = select_pending_blobs_for_materialize(
        conn,
        source_type=source_type,
        batch_size=batch_size,
        retry_failed=retry_failed,
    )
    
    records = []
    for row in rows:
        records.append(PatchBlobRecord(
            blob_id=row["blob_id"],
            source_type=row["source_type"],
            source_id=row["source_id"],
            uri=row["uri"],
            sha256=row["sha256"],
            size_bytes=row["size_bytes"],
            format=row["format"],
            meta_json=row.get("meta_json", {}),
        ))
    
    return records


def update_patch_blob_uri(
    conn: psycopg.Connection,
    blob_id: int,
    new_uri: str,
    sha256: str,
    size_bytes: int,
    expected_sha256: str,
) -> bool:
    """
    安全更新 patch blob 的 URI
    
    仅当计算的 sha256 与预期匹配时才更新。
    
    Args:
        conn: 数据库连接
        blob_id: blob ID
        new_uri: 新的 URI
        sha256: 实际计算的 sha256
        size_bytes: 内容大小
        expected_sha256: 预期的 sha256（来自数据库记录）
        
    Returns:
        True 如果更新成功
        
    Raises:
        ChecksumMismatchError: sha256 不匹配
    """
    # 验证 sha256
    if sha256 != expected_sha256:
        raise ChecksumMismatchError(
            f"SHA256 不匹配: 计算值={sha256[:16]}..., 预期值={expected_sha256[:16]}...",
            {
                "blob_id": blob_id,
                "computed": sha256,
                "expected": expected_sha256,
            },
        )
    
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE scm.patch_blobs
            SET uri = %s,
                size_bytes = %s
            WHERE blob_id = %s AND sha256 = %s
            RETURNING blob_id
            """,
            (new_uri, size_bytes, blob_id, expected_sha256),
        )
        result = cur.fetchone()
        
        if not result:
            logger.warning(f"更新 blob_id={blob_id} 失败，可能 sha256 已变更")
            return False
    
    conn.commit()
    return True


def update_patch_blob_with_mirror(
    conn: psycopg.Connection,
    blob_id: int,
    original_uri: str,
    mirror_uri: str,
    sha256: str,
    size_bytes: int,
) -> bool:
    """
    更新 patch blob 并记录 mirror 信息
    
    用于外部 URI 场景：保留原始 URI，同时记录本地 mirror。
    
    Args:
        conn: 数据库连接
        blob_id: blob ID
        original_uri: 原始 URI
        mirror_uri: 本地 mirror URI
        sha256: sha256 哈希
        size_bytes: 内容大小
        
    Returns:
        True 如果更新成功
    """
    mirror_info = {
        "mirror_uri": mirror_uri,
        "mirrored_at": datetime.utcnow().isoformat() + "Z",
    }
    
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE scm.patch_blobs
            SET uri = %s,
                size_bytes = %s,
                meta_json = COALESCE(meta_json, '{}'::jsonb) || %s::jsonb
            WHERE blob_id = %s AND sha256 = %s
            RETURNING blob_id
            """,
            (original_uri, size_bytes, json.dumps(mirror_info), blob_id, sha256),
        )
        result = cur.fetchone()
    
    conn.commit()
    return result is not None


def materialize_blob(
    conn: psycopg.Connection,
    record: PatchBlobRecord,
    config: Optional[Config] = None,
    on_sha_mismatch: ShaMismatchPolicy = ShaMismatchPolicy.STRICT,
) -> MaterializeResult:
    """
    物化单个 patch blob
    
    流程:
    1. 检查 URI 是否已可解析 -> 跳过
    2. 标记为处理中状态
    3. 解析 source_id 获取 repo_id 和 rev/sha
    4. 获取仓库信息
    5. 根据 source_type 调用对应的拉取函数
    6. 写入 ArtifactStore
    7. 校验 sha256
    8. 安全更新数据库（写入 materialize_status/materialized_at/attempts）
    
    Args:
        conn: 数据库连接
        record: PatchBlobRecord 记录
        config: 配置实例
        on_sha_mismatch: SHA 不匹配时的处理策略
            - strict: 不写入制品，只标记失败
            - mirror: 写入制品到 v2 路径（基于 actual_sha256），并记录 mirror 信息
        
    Returns:
        MaterializeResult 结果
    """
    blob_id = record.blob_id
    current_endpoint: Optional[str] = None
    
    # 读取历史 attempts 次数（从 meta_json）
    current_attempts = 1
    if record.meta_json:
        current_attempts = record.meta_json.get("attempts", 0) + 1
    
    # 1. 检查是否已可解析
    if record.uri and is_uri_resolvable(record.uri):
        logger.debug(f"blob_id={blob_id} 的 URI 已可解析，跳过")
        return MaterializeResult(
            blob_id=blob_id,
            status=MaterializeStatus.SKIPPED,
            uri=record.uri,
            attempts=current_attempts,
        )
    
    # 2. 标记为处理中
    mark_blob_in_progress(conn, blob_id)
    conn.commit()
    
    try:
        # 3. 解析 source_id
        repo_id_str, rev_or_sha = parse_source_id(record.source_id)
        repo_id = int(repo_id_str)
        
        # 4. 获取仓库信息
        repo_info = get_repo_info(conn, repo_id)
        if not repo_info:
            error_msg = f"仓库不存在: repo_id={repo_id}"
            mark_blob_failed(
                conn, blob_id, error_msg,
                error_category=ErrorCategory.VALIDATION_ERROR.value,
            )
            conn.commit()
            raise ValidationError(
                error_msg,
                {"blob_id": blob_id, "repo_id": repo_id},
            )
        
        repo_url = repo_info["url"]
        repo_type = repo_info["repo_type"]
        
        # 5. 根据 source_type 拉取 diff
        diff_content: Optional[str] = None
        
        if record.source_type == "svn":
            revision = int(rev_or_sha)
            current_endpoint = f"svn diff -c {revision} {repo_url}"
            logger.info(f"从 SVN 拉取 diff: repo_id={repo_id}, revision={revision}")
            diff_content = fetch_svn_diff(repo_url, revision, config=config)
            
        elif record.source_type == "git":
            commit_sha = rev_or_sha
            logger.info(f"从 GitLab 拉取 diff: repo_id={repo_id}, commit={commit_sha[:8]}")
            
            # 获取 GitLab 配置（优先新键，回退旧键与环境变量）
            gitlab_cfg = get_gitlab_config(config)
            gitlab_url = gitlab_cfg.get("url")
            
            if not gitlab_url:
                error_msg = "GitLab URL 未配置，无法拉取 diff"
                mark_blob_failed(
                    conn, blob_id, error_msg,
                    error_category=ErrorCategory.VALIDATION_ERROR.value,
                )
                conn.commit()
                raise ValidationError(
                    error_msg,
                    {"blob_id": blob_id, "missing": ["scm.gitlab.url/gitlab.url"]},
                )
            
            # 创建 token provider（支持 env/file/exec 等多种方式）
            token_provider = create_gitlab_token_provider(config)
            
            # 从 repo_url 提取 project_id
            # repo_url 格式: https://gitlab.example.com/namespace/project
            project_path = repo_url.replace(gitlab_url.rstrip("/"), "").strip("/")
            if project_path.startswith("/-/projects/"):
                project_id = project_path.replace("/-/projects/", "")
            else:
                project_id = project_path
            
            current_endpoint = f"/projects/{project_id}/repository/commits/{commit_sha}/diff"
            
            diff_content = fetch_gitlab_commit_diff(
                gitlab_url, project_id, commit_sha, token_provider, config=config
            )
        else:
            error_msg = f"不支持的 source_type: {record.source_type}"
            mark_blob_failed(
                conn, blob_id, error_msg,
                error_category=ErrorCategory.VALIDATION_ERROR.value,
            )
            conn.commit()
            raise ValidationError(
                error_msg,
                {"blob_id": blob_id, "source_type": record.source_type},
            )
        
        if diff_content is None:
            diff_content = ""
        
        # 6. 根据 record.format 处理内容
        content_to_store = diff_content
        
        if record.format == "diff":
            # 直接使用完整 diff 内容
            content_to_store = diff_content
            
        elif record.format == "diffstat":
            # 对拉取到的完整 diff 运行 diffstat 生成函数
            if diff_content.strip():
                content_to_store = svn_generate_diffstat(diff_content)
            else:
                content_to_store = ""
            logger.debug(f"blob_id={blob_id}: 从 diff 生成 diffstat")
            
        elif record.format == "ministat":
            # 根据 source_type 生成 ministat
            if record.source_type == "git":
                # Git: 优先使用 git_commits.meta_json.stats
                git_meta = get_git_commit_meta(conn, repo_id, rev_or_sha)
                if git_meta and git_meta.get("stats"):
                    stats = git_meta["stats"]
                    files_changed = stats.get("total", 0)
                    content_to_store = generate_ministat_from_stats(
                        stats, files_changed=files_changed, commit_sha=rev_or_sha
                    )
                    logger.debug(f"blob_id={blob_id}: 从 git_commits.meta_json.stats 生成 ministat")
                elif diff_content.strip():
                    # 回退：从 diff 解析统计信息
                    # 简单统计增删行数
                    additions = sum(1 for line in diff_content.split("\n") 
                                   if line.startswith("+") and not line.startswith("+++"))
                    deletions = sum(1 for line in diff_content.split("\n") 
                                   if line.startswith("-") and not line.startswith("---"))
                    stats = {"additions": additions, "deletions": deletions}
                    content_to_store = generate_ministat_from_stats(
                        stats, files_changed=0, commit_sha=rev_or_sha
                    )
                    logger.debug(f"blob_id={blob_id}: 从 diff 内容解析生成 ministat")
                else:
                    content_to_store = ""
                    
            elif record.source_type == "svn":
                # SVN: 使用 svn_revisions.meta_json.changed_paths
                revision = int(rev_or_sha)
                svn_meta = get_svn_revision_meta(conn, repo_id, revision)
                if svn_meta and svn_meta.get("changed_paths"):
                    changed_paths = svn_meta["changed_paths"]
                    content_to_store = generate_ministat_from_changed_paths(
                        changed_paths, revision=revision
                    )
                    logger.debug(f"blob_id={blob_id}: 从 svn_revisions.meta_json.changed_paths 生成 ministat")
                elif diff_content.strip():
                    # 回退：从 diff 解析统计信息
                    additions = sum(1 for line in diff_content.split("\n") 
                                   if line.startswith("+") and not line.startswith("+++"))
                    deletions = sum(1 for line in diff_content.split("\n") 
                                   if line.startswith("-") and not line.startswith("---"))
                    stats = {"additions": additions, "deletions": deletions}
                    content_to_store = generate_ministat_from_stats(stats, files_changed=0)
                    logger.debug(f"blob_id={blob_id}: 从 diff 内容解析生成 ministat (SVN)")
                else:
                    content_to_store = ""
            else:
                content_to_store = ""
        else:
            # 未知格式，使用完整 diff
            logger.warning(f"blob_id={blob_id}: 未知格式 '{record.format}'，使用完整 diff")
            content_to_store = diff_content
        
        # 7. 检查大小限制
        content_bytes = content_to_store.encode("utf-8")
        if len(content_bytes) > MAX_DIFF_SIZE_BYTES:
            error_msg = f"内容超过大小限制: {len(content_bytes)} > {MAX_DIFF_SIZE_BYTES}"
            mark_blob_failed(
                conn, blob_id, error_msg,
                endpoint=current_endpoint,
                error_category=ErrorCategory.CONTENT_TOO_LARGE.value,
            )
            conn.commit()
            return MaterializeResult(
                blob_id=blob_id,
                status=MaterializeStatus.FAILED,
                error=error_msg,
                error_category=ErrorCategory.CONTENT_TOO_LARGE,
                endpoint=current_endpoint,
                attempts=current_attempts,
            )
        
        # 8. 计算 sha256
        computed_sha256 = compute_sha256(content_to_store)
        
        # 从仓库信息获取 project_key
        project_key = repo_info.get("project_key", "default")
        
        # 9. 检查 SHA256 是否匹配（如果 record.sha256 存在）
        if record.sha256 and computed_sha256 != record.sha256:
            # SHA256 不匹配，根据策略处理
            error_msg = f"SHA256 不匹配: 计算值={computed_sha256[:16]}..., 预期值={record.sha256[:16]}..."
            logger.warning(f"blob_id={blob_id} {error_msg} (策略: {on_sha_mismatch.value})")
            
            if on_sha_mismatch == ShaMismatchPolicy.STRICT:
                # strict 模式：不写入制品，只标记失败
                mark_blob_failed(
                    conn, blob_id, error_msg,
                    endpoint=current_endpoint,
                    error_category=ErrorCategory.VALIDATION_ERROR.value,
                    actual_sha256=computed_sha256,
                )
                conn.commit()
                return MaterializeResult(
                    blob_id=blob_id,
                    status=MaterializeStatus.FAILED,
                    sha256=computed_sha256,
                    size_bytes=len(content_to_store.encode("utf-8")),
                    error=error_msg,
                    error_category=ErrorCategory.VALIDATION_ERROR,
                    endpoint=current_endpoint,
                    attempts=current_attempts,
                )
            
            elif on_sha_mismatch == ShaMismatchPolicy.MIRROR:
                # mirror 模式：写入制品到 v2 路径（基于 actual_sha256），并记录 mirror 信息
                artifact_uri = generate_artifact_uri(
                    source_type=record.source_type,
                    repo_id=repo_id_str,
                    rev_or_sha=rev_or_sha,
                    sha256=computed_sha256,  # 使用实际计算的 sha256
                    patch_format=record.format,
                    project_key=project_key,
                )
                
                artifact_result = write_text_artifact(artifact_uri, content_to_store)
                
                logger.info(
                    f"blob_id={blob_id} SHA 不匹配，mirror 模式写入: uri={artifact_result['uri']}"
                )
                
                # 标记失败，同时记录 mirror 信息
                mark_blob_failed(
                    conn, blob_id, error_msg,
                    endpoint=current_endpoint,
                    error_category=ErrorCategory.VALIDATION_ERROR.value,
                    mirror_uri=artifact_result["uri"],
                    actual_sha256=computed_sha256,
                )
                conn.commit()
                return MaterializeResult(
                    blob_id=blob_id,
                    status=MaterializeStatus.FAILED,
                    uri=artifact_result["uri"],
                    sha256=computed_sha256,
                    size_bytes=artifact_result["size_bytes"],
                    error=error_msg,
                    error_category=ErrorCategory.VALIDATION_ERROR,
                    endpoint=current_endpoint,
                    attempts=current_attempts,
                )
        
        # 10. SHA256 匹配或无预期值，正常写入 ArtifactStore
        artifact_uri = generate_artifact_uri(
            source_type=record.source_type,
            repo_id=repo_id_str,
            rev_or_sha=rev_or_sha,
            sha256=computed_sha256,
            patch_format=record.format,
            project_key=project_key,
        )
        
        artifact_result = write_text_artifact(artifact_uri, content_to_store)
        
        logger.debug(
            f"写入 artifact: uri={artifact_result['uri']}, "
            f"sha256={artifact_result['sha256'][:16]}..., format={record.format}"
        )
        
        # 11. 安全更新数据库（使用新的状态更新函数）
        if record.sha256:
            
            # 构建 canonical evidence_uri (memory://patch_blobs/<source_type>/<source_id>/<sha256>)
            # 用于 analysis.* 和 governance.* 表中的 evidence_refs_json 引用
            evidence_uri = build_evidence_uri(
                record.source_type, record.source_id, artifact_result["sha256"]
            )
            
            # sha256 匹配，使用乐观锁更新
            success = mark_blob_done(
                conn,
                blob_id,
                uri=artifact_result["uri"],
                sha256=artifact_result["sha256"],
                size_bytes=artifact_result["size_bytes"],
                expected_sha256=record.sha256,
                evidence_uri=evidence_uri,
            )
            
            if not success:
                error_msg = "并发更新冲突，可能 sha256 已变更"
                logger.warning(f"blob_id={blob_id} {error_msg}")
                return MaterializeResult(
                    blob_id=blob_id,
                    status=MaterializeStatus.FAILED,
                    uri=artifact_result["uri"],
                    sha256=artifact_result["sha256"],
                    size_bytes=artifact_result["size_bytes"],
                    error=error_msg,
                )
        else:
            # 构建 canonical evidence_uri (memory://patch_blobs/<source_type>/<source_id>/<sha256>)
            evidence_uri = build_evidence_uri(
                record.source_type, record.source_id, artifact_result["sha256"]
            )
            
            # 无 sha256，直接更新
            mark_blob_done(
                conn,
                blob_id,
                uri=artifact_result["uri"],
                sha256=artifact_result["sha256"],
                size_bytes=artifact_result["size_bytes"],
                evidence_uri=evidence_uri,
            )
        
        conn.commit()
        
        logger.info(
            f"物化成功: blob_id={blob_id}, uri={artifact_result['uri'][:50]}..., attempts={current_attempts}"
        )
        
        return MaterializeResult(
            blob_id=blob_id,
            status=MaterializeStatus.MATERIALIZED,
            uri=artifact_result["uri"],
            sha256=artifact_result["sha256"],
            size_bytes=artifact_result["size_bytes"],
            attempts=current_attempts,
        )
        
    except FetchError as e:
        error_msg = e.message if hasattr(e, 'message') else str(e)
        logger.error(f"物化失败 blob_id={blob_id}: {error_msg}, attempts={current_attempts}")
        
        # 从错误详情中提取上下文
        details = e.details if hasattr(e, 'details') else {}
        error_category_str = details.get("error_category", ErrorCategory.UNKNOWN.value)
        endpoint = details.get("endpoint", current_endpoint)
        status_code = details.get("status_code")
        
        # 映射错误分类
        try:
            error_cat = ErrorCategory(error_category_str)
        except ValueError:
            error_cat = ErrorCategory.UNKNOWN
        
        try:
            mark_blob_failed(
                conn, blob_id, error_msg,
                endpoint=endpoint,
                status_code=status_code,
                error_category=error_cat.value,
            )
            conn.commit()
        except Exception:
            pass
        
        return MaterializeResult(
            blob_id=blob_id,
            status=MaterializeStatus.FAILED,
            error=error_msg,
            error_category=error_cat,
            endpoint=endpoint,
            status_code=status_code,
            attempts=current_attempts,
        )
        
    except (MaterializeError, ValidationError, DatabaseError) as e:
        error_msg = e.message if hasattr(e, 'message') else str(e)
        logger.error(f"物化失败 blob_id={blob_id}: {error_msg}, attempts={current_attempts}")
        
        # 确定错误分类
        error_cat = ErrorCategory.VALIDATION_ERROR
        if isinstance(e, PayloadTooLargeError):
            error_cat = ErrorCategory.CONTENT_TOO_LARGE
        elif isinstance(e, DatabaseError):
            error_cat = ErrorCategory.UNKNOWN
        
        try:
            mark_blob_failed(
                conn, blob_id, error_msg,
                endpoint=current_endpoint,
                error_category=error_cat.value,
            )
            conn.commit()
        except Exception:
            pass
        
        return MaterializeResult(
            blob_id=blob_id,
            status=MaterializeStatus.FAILED,
            error=error_msg,
            error_category=error_cat,
            endpoint=current_endpoint,
            attempts=current_attempts,
        )
        
    except Exception as e:
        error_msg = str(e)
        logger.exception(f"物化失败 blob_id={blob_id}: {error_msg}, attempts={current_attempts}")
        
        try:
            mark_blob_failed(
                conn, blob_id, error_msg,
                endpoint=current_endpoint,
                error_category=ErrorCategory.UNKNOWN.value,
            )
            conn.commit()
        except Exception:
            pass
        
        return MaterializeResult(
            blob_id=blob_id,
            status=MaterializeStatus.FAILED,
            error=error_msg,
            error_category=ErrorCategory.UNKNOWN,
            endpoint=current_endpoint,
            attempts=current_attempts,
        )


def materialize_patch_blobs(
    source_type: Optional[str] = None,
    blob_id: Optional[int] = None,
    attachment_id: Optional[int] = None,
    kind: Optional[str] = None,
    materialize_missing: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
    retry_failed: bool = False,
    config: Optional[Config] = None,
    on_sha_mismatch: ShaMismatchPolicy = ShaMismatchPolicy.STRICT,
) -> Dict[str, Any]:
    """
    批量物化 patch blobs
    
    Args:
        source_type: 可选，筛选特定源类型
        blob_id: 可选，处理特定 blob
        attachment_id: 可选，根据 attachment_id 反查关联的 blob
        kind: 可选，按 attachment kind 批量处理（如 'patch'）
        materialize_missing: 是否只处理未物化的记录（配合 kind 使用）
        batch_size: 批量大小
        retry_failed: 是否重试失败记录
        config: 配置实例
        on_sha_mismatch: SHA 不匹配时的处理策略
            - strict: 不写入制品，只标记失败
            - mirror: 写入制品到 v2 路径（基于 actual_sha256），并记录 mirror 信息
        
    Returns:
        处理结果统计（包含 done/failed/attempts 分布）
    """
    # 生成 run_id 用于追踪本次批量处理
    run_id = str(uuid.uuid4())
    
    result = {
        "success": False,
        "run_id": run_id,
        "total": 0,
        "materialized": 0,
        "skipped": 0,
        "failed": 0,
        "attempts_distribution": {},  # 尝试次数分布 {attempts: count}
        "failure_counts": {          # 按错误类型的失败计数
            "timeout": 0,
            "http_error": 0,
            "auth_error": 0,
            "content_too_large": 0,
            "network_error": 0,
            "validation_error": 0,
            "unknown": 0,
        },
        "details": [],
    }
    
    logger.info(f"[run_id={run_id[:8]}] 开始批量物化 patch blobs")
    
    conn = get_connection(config=config)
    try:
        # 根据参数选择不同的获取方式
        records = []
        
        if attachment_id:
            # 根据 attachment_id 反查 blob
            logger.info(f"根据 attachment_id={attachment_id} 反查关联的 patch_blobs")
            blob_rows = get_blobs_by_attachment_id(conn, attachment_id)
            for row in blob_rows:
                records.append(PatchBlobRecord(
                    blob_id=row["blob_id"],
                    source_type=row["source_type"],
                    source_id=row["source_id"],
                    uri=row["uri"],
                    sha256=row["sha256"],
                    size_bytes=row["size_bytes"],
                    format=row["format"],
                    meta_json=row.get("meta_json", {}),
                ))
        elif kind:
            # 按 attachment kind 批量获取
            logger.info(f"按 attachment kind='{kind}' 获取关联的 patch_blobs")
            blob_rows = get_blobs_by_attachment_kind(
                conn, kind, materialize_missing=materialize_missing, batch_size=batch_size
            )
            for row in blob_rows:
                records.append(PatchBlobRecord(
                    blob_id=row["blob_id"],
                    source_type=row["source_type"],
                    source_id=row["source_id"],
                    uri=row["uri"],
                    sha256=row["sha256"],
                    size_bytes=row["size_bytes"],
                    format=row["format"],
                    meta_json=row.get("meta_json", {}),
                ))
        else:
            # 原有方式：获取待处理记录
            records = get_pending_blobs(
                conn,
                source_type=source_type,
                blob_id=blob_id,
                batch_size=batch_size,
                retry_failed=retry_failed,
            )
        
        result["total"] = len(records)
        logger.info(f"获取到 {len(records)} 条待物化记录")
        
        if not records:
            result["success"] = True
            result["message"] = "无待物化记录"
            return result
        
        # 逐条处理，收集 attempts 分布和失败类型统计
        attempts_counter: Counter = Counter()
        failure_counter: Counter = Counter()
        
        for record in records:
            mat_result = materialize_blob(conn, record, config, on_sha_mismatch)
            
            # 收集 attempts 分布
            attempts_counter[mat_result.attempts] += 1
            
            result["details"].append({
                "blob_id": mat_result.blob_id,
                "status": mat_result.status.value,
                "uri": mat_result.uri,
                "sha256": mat_result.sha256[:16] if mat_result.sha256 else None,
                "size_bytes": mat_result.size_bytes,
                "error": mat_result.error,
                "attempts": mat_result.attempts,
                "error_category": mat_result.error_category.value if mat_result.error_category else None,
                "endpoint": mat_result.endpoint,
                "status_code": mat_result.status_code,
            })
            
            if mat_result.status == MaterializeStatus.MATERIALIZED:
                result["materialized"] += 1
            elif mat_result.status == MaterializeStatus.SKIPPED:
                result["skipped"] += 1
            else:
                result["failed"] += 1
                # 统计失败类型
                if mat_result.error_category:
                    failure_counter[mat_result.error_category.value] += 1
                else:
                    failure_counter["unknown"] += 1
        
        # 计算 attempts 分布
        result["attempts_distribution"] = dict(sorted(attempts_counter.items()))
        
        # 更新失败类型计数
        for error_type in result["failure_counts"]:
            result["failure_counts"][error_type] = failure_counter.get(error_type, 0)
        
        result["success"] = True
        
        # 增强日志输出：包含 done/failed/attempts 分布和失败类型
        attempts_dist_str = ", ".join([f"{k}次:{v}" for k, v in sorted(attempts_counter.items())])
        failure_dist_str = ", ".join([f"{k}:{v}" for k, v in failure_counter.items() if v > 0])
        logger.info(
            f"[run_id={run_id[:8]}] 物化完成: "
            f"done={result['materialized']}, failed={result['failed']}, skipped={result['skipped']}, "
            f"attempts分布=[{attempts_dist_str}]"
            + (f", 失败类型=[{failure_dist_str}]" if failure_dist_str else "")
        )
        
    except EngramError:
        raise
    except Exception as e:
        logger.exception(f"物化过程中发生错误: {e}")
        result["error"] = str(e)
        raise MaterializeError(
            f"物化失败: {e}",
            {"error": str(e)},
        )
    finally:
        conn.close()
    
    return result


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="物化 patch blob 脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 物化所有待处理的 blob
    python scm_materialize_patch_blob.py

    # 物化指定 source_type 的 blob
    python scm_materialize_patch_blob.py --source-type svn

    # 物化指定的 blob
    python scm_materialize_patch_blob.py --blob-id 123

    # 重试之前失败的记录
    python scm_materialize_patch_blob.py --retry-failed

    # 根据 attachment_id 反查关联的 blob 并物化
    python scm_materialize_patch_blob.py --attachment-id 456

    # 批量物化 kind='patch' 类型附件关联的 blob
    python scm_materialize_patch_blob.py --kind patch --materialize-missing
        """,
    )
    
    add_config_argument(parser)
    
    parser.add_argument(
        "--blob-id",
        type=int,
        default=None,
        help="处理指定的 blob ID",
    )
    
    parser.add_argument(
        "--attachment-id",
        type=int,
        default=None,
        help="根据 attachment_id 反查关联的 patch_blob 进行物化",
    )
    
    parser.add_argument(
        "--kind",
        type=str,
        default=None,
        help="按 attachment kind 批量处理（如 'patch'），需配合 --materialize-missing",
    )
    
    parser.add_argument(
        "--materialize-missing",
        action="store_true",
        dest="materialize_missing",
        help="只处理未物化的记录（配合 --kind 使用）",
    )
    
    parser.add_argument(
        "--source-type",
        type=str,
        choices=["svn", "git"],
        default=None,
        help="筛选特定源类型",
    )
    
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help=f"每次处理的最大 blob 数 (默认: {DEFAULT_BATCH_SIZE})",
    )
    
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="重试之前失败的记录",
    )
    
    parser.add_argument(
        "--on-sha-mismatch",
        type=str,
        choices=["strict", "mirror"],
        default="strict",
        dest="on_sha_mismatch",
        help="SHA256 不匹配时的处理策略: "
             "strict (默认) = 不写入制品，只标记失败; "
             "mirror = 写入制品到 v2 路径并记录 mirror 信息",
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
        
        # 获取批量大小
        batch_size = args.batch_size or config.get("materialize.batch_size", DEFAULT_BATCH_SIZE)
        
        # 解析 SHA mismatch 策略
        sha_mismatch_policy = ShaMismatchPolicy(args.on_sha_mismatch)
        
        # 执行物化
        result = materialize_patch_blobs(
            source_type=args.source_type,
            blob_id=args.blob_id,
            attachment_id=args.attachment_id,
            kind=args.kind,
            materialize_missing=args.materialize_missing,
            batch_size=batch_size,
            retry_failed=args.retry_failed,
            config=config,
            on_sha_mismatch=sha_mismatch_policy,
        )
        
        if args.json:
            # 简化 details 输出
            output = {
                "success": result["success"],
                "total": result["total"],
                "materialized": result["materialized"],
                "skipped": result["skipped"],
                "failed": result["failed"],
                "failure_counts": result.get("failure_counts", {}),
            }
            if args.verbose:
                output["details"] = result["details"]
                output["attempts_distribution"] = result.get("attempts_distribution", {})
            print(json.dumps(output, default=str, ensure_ascii=False))
        else:
            # 构建失败类型信息
            failure_counts = result.get("failure_counts", {})
            failure_info = ", ".join([f"{k}:{v}" for k, v in failure_counts.items() if v > 0])
            
            print(
                f"物化完成: 总计={result['total']}, "
                f"成功={result['materialized']}, "
                f"跳过={result['skipped']}, "
                f"失败={result['failed']}"
                + (f" (失败类型: {failure_info})" if failure_info else "")
            )
        
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
