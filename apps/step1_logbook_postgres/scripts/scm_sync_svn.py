#!/usr/bin/env python3
"""
scm_sync_svn.py - SVN 日志同步脚本

功能:
- 使用 svn log --xml 拉取 SVN 日志
- 从 [last_rev+1, HEAD] 范围增量同步（可配置批量大小上限）
- 解析 author/date/msg/changed paths 写入 scm.svn_revisions 表
- 同步完成后更新 kv 游标（含 overlap 策略配置）

使用:
    python scm_sync_svn.py [--config PATH] [--batch-size N] [--overlap N] [--verbose]

配置文件示例:
    [svn]
    url = "svn://svn.example.com/project/trunk"
    batch_size = 100           # 每次同步最大 revision 数
    overlap = 0                # 重叠 revision 数（重新同步已同步的部分）
"""

import argparse
import json
import logging
import subprocess
import sys
import uuid
import warnings
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import psycopg

from engram_step1.config import (
    Config,
    add_config_argument,
    get_config,
    get_svn_config,
    get_svn_auth,
    get_bulk_thresholds,
    get_scm_sync_mode,
    is_strict_mode,
    SCM_SYNC_MODE_STRICT,
    SCM_SYNC_MODE_BEST_EFFORT,
)
from engram_step1.scm_sync_policy import SvnPatchFetchController
from engram_step1.scm_auth import redact
from engram_step1.cursor import load_svn_cursor, save_svn_cursor
from engram_step1.db import get_connection, create_item, add_event, attach
from engram_step1.source_id import build_svn_source_id
from engram_step1.uri import build_evidence_uri, build_evidence_ref_for_patch_blob
from db import upsert_patch_blob, upsert_svn_revision
from engram_step1.errors import DatabaseError, EngramError, ValidationError
from artifacts import write_text_artifact, get_scm_path, build_scm_artifact_path
from engram_step1.hashing import sha256 as compute_sha256
from identity_resolve import resolve_and_enrich_meta
from scm_repo import ensure_repo
from engram_step1 import scm_sync_lock
from db import insert_sync_run_start, insert_sync_run_finish

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# 默认配置值
DEFAULT_BATCH_SIZE = 100
DEFAULT_OVERLAP = 0
DEFAULT_LOCK_LEASE_SECONDS = 300  # 默认锁租约时间（秒），SVN fetch_patches 需要更长
DEFAULT_RENEW_INTERVAL_REVS = 10  # 每处理 N 个 revision 后续租一次

# physical_job_type 常量
# 使用 physical_job_type 确保同一语义任务在队列/锁表中有唯一键
# 参见 engram_step1.scm_sync_job_types 模块
JOB_TYPE_SVN = "svn"  # physical_job_type: SVN 提交记录


def _generate_worker_id() -> str:
    """生成唯一的 worker ID（基于 hostname + pid + uuid 片段）"""
    import socket
    import os
    hostname = socket.gethostname()[:16]
    pid = os.getpid()
    short_uuid = str(uuid.uuid4())[:8]
    return f"{hostname}-{pid}-{short_uuid}"

@dataclass
class SvnRevision:
    """SVN revision 数据结构"""
    revision: int
    author: str
    date: Optional[datetime]
    message: str
    changed_paths: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class SyncConfig:
    """同步配置"""
    svn_url: str
    batch_size: int = DEFAULT_BATCH_SIZE
    overlap: int = DEFAULT_OVERLAP
    strict: bool = False  # 严格模式：不可恢复的错误时不推进游标


class SvnSyncError(EngramError):
    """SVN 同步错误"""
    exit_code = 10
    error_type = "SVN_SYNC_ERROR"


class SvnCommandError(SvnSyncError):
    """SVN 命令执行错误"""
    error_type = "SVN_COMMAND_ERROR"


class SvnParseError(SvnSyncError):
    """SVN XML 解析错误"""
    error_type = "SVN_PARSE_ERROR"


class SvnTimeoutError(SvnSyncError):
    """SVN 命令超时错误"""
    error_type = "SVN_TIMEOUT_ERROR"


class SvnAuthError(SvnSyncError):
    """SVN 认证错误（用户名/密码无效或权限不足）"""
    error_type = "SVN_AUTH_ERROR"


# ============ SVN 命令统一执行器 ============


@dataclass
class SvnCmdResult:
    """SVN 命令执行结果"""
    success: bool
    stdout: str = ""
    stderr: str = ""
    error_type: Optional[str] = None  # timeout/command_error/auth_error
    error_message: Optional[str] = None


def _mask_svn_command_for_log(cmd: List[str]) -> str:
    """
    对 SVN 命令进行脱敏处理，用于日志输出
    
    - 隐藏 --password 参数值
    - 保留其他参数用于调试
    
    Args:
        cmd: SVN 命令列表
        
    Returns:
        脱敏后的命令字符串摘要
    """
    masked_parts = []
    skip_next = False
    
    for i, part in enumerate(cmd):
        if skip_next:
            skip_next = False
            continue
            
        if part == "--password":
            masked_parts.append("--password")
            masked_parts.append("****")
            skip_next = True
        elif part.startswith("--password="):
            masked_parts.append("--password=****")
        else:
            masked_parts.append(part)
    
    return " ".join(masked_parts)


def run_svn_cmd(
    cmd: List[str],
    timeout: Optional[int] = None,
    config: Optional[Config] = None,
    capture_output: bool = True,
) -> SvnCmdResult:
    """
    统一的 SVN 命令执行器
    
    功能:
    - 自动注入认证参数（--username/--password）从配置读取
    - 自动添加 --non-interactive 参数（由配置控制）
    - 自动添加 --trust-server-cert-failures=unknown-ca（由配置控制）
    - 捕获 stderr 并分类错误为 timeout/command_error/auth_error
    - 日志中不打印完整命令（脱敏处理 password）
    
    Args:
        cmd: SVN 命令列表（如 ["svn", "info", "--xml", url]）
        timeout: 超时秒数，None 使用配置默认值
        config: 配置实例
        capture_output: 是否捕获输出
        
    Returns:
        SvnCmdResult 结果对象
        
    Raises:
        SvnTimeoutError: 命令超时
        SvnAuthError: 认证失败
        SvnCommandError: 命令执行失败
    """
    # 获取 SVN 配置
    svn_cfg = get_svn_config(config)
    
    # 构建完整命令
    full_cmd = list(cmd)  # 复制命令列表
    
    # 注入认证参数（使用统一的 get_svn_auth 接口，支持 SVN_PASSWORD 环境变量回退）
    svn_auth = get_svn_auth(config)
    if svn_auth:
        if svn_auth.username:
            full_cmd.extend(["--username", svn_auth.username])
        if svn_auth.password:
            full_cmd.extend(["--password", svn_auth.password])
    
    # 注入安全选项
    if svn_cfg.get("non_interactive", True):
        full_cmd.append("--non-interactive")
    
    if svn_cfg.get("trust_server_cert", False):
        full_cmd.append("--trust-server-cert-failures=unknown-ca")
    
    # 确定超时时间
    if timeout is None:
        timeout = svn_cfg.get("command_timeout", 120)
    
    # 脱敏日志输出
    masked_cmd = _mask_svn_command_for_log(full_cmd)
    logger.debug(f"执行 SVN 命令: {masked_cmd}")
    
    try:
        result = subprocess.run(
            full_cmd,
            capture_output=capture_output,
            text=True,
            check=True,
            timeout=timeout,
        )
        return SvnCmdResult(
            success=True,
            stdout=result.stdout,
            stderr=result.stderr or "",
        )
        
    except subprocess.TimeoutExpired:
        error_msg = f"SVN 命令超时 ({timeout}s)"
        logger.warning(f"{error_msg}: {_mask_svn_command_for_log(cmd[:3])}...")
        return SvnCmdResult(
            success=False,
            error_type="timeout",
            error_message=error_msg,
        )
        
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        
        # 分析 stderr 判断错误类型
        error_type = "command_error"
        # 对错误消息进行脱敏，避免泄露敏感信息
        error_msg = redact(stderr) if stderr else redact(str(e))
        
        # 检测认证错误的关键字
        auth_keywords = [
            "authorization failed",
            "authentication failed",
            "could not authenticate",
            "access denied",
            "forbidden",
            "not authorized",
            "svn: E170001",  # SVN 认证错误码
            "svn: E215004",  # 无法认证
            "svn: E175013",  # 权限拒绝
        ]
        
        stderr_lower = stderr.lower()
        for keyword in auth_keywords:
            if keyword.lower() in stderr_lower:
                error_type = "auth_error"
                break
        
        logger.warning(
            f"SVN 命令失败 ({error_type}): "
            f"{_mask_svn_command_for_log(cmd[:3])}... "
            f"returncode={e.returncode}"
        )
        
        return SvnCmdResult(
            success=False,
            stderr=redact(stderr),  # 对 stderr 进行脱敏
            error_type=error_type,
            error_message=error_msg,
        )


# ============ 统一的 Patch 获取异常分类 ============

class PatchFetchError(EngramError):
    """Patch 获取错误基类"""
    exit_code = 20
    error_type = "PATCH_FETCH_ERROR"
    error_category = "unknown"  # 用于 meta_json 中标识错误类型


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


class PatchFetchCommandError(PatchFetchError):
    """Patch 获取命令执行错误（用于 SVN）"""
    error_type = "PATCH_FETCH_COMMAND_ERROR"
    error_category = "command_error"


@dataclass
class ChangeSummary:
    """变更摘要数据结构，用于 is_bulk 判断"""
    changed_paths_count: int = 0
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
    changed_paths_threshold = thresholds["svn_changed_paths_threshold"]
    diff_size_threshold = thresholds["diff_size_threshold"]

    # 检查 changed_paths 数量
    if change_summary.changed_paths_count > changed_paths_threshold:
        return (True, f"changed_paths:{change_summary.changed_paths_count}>{changed_paths_threshold}")

    # 检查 diff 大小
    if change_summary.diff_size_bytes > diff_size_threshold:
        size_mb = change_summary.diff_size_bytes / (1024 * 1024)
        threshold_mb = diff_size_threshold / (1024 * 1024)
        return (True, f"diff_size:{size_mb:.2f}MB>{threshold_mb:.2f}MB")

    return (False, None)


def generate_ministat_from_changed_paths(
    changed_paths: List[Dict[str, str]],
    revision: Optional[int] = None,
) -> str:
    """
    从 changed_paths 列表生成最小 diffstat（用于降级场景）

    当无法获取完整 diff 时，基于变更路径列表生成摘要信息。
    格式为简化的 diffstat，仅包含文件操作类型。

    Args:
        changed_paths: 变更路径列表，每项包含 path/action/kind
        revision: 可选的 revision 号（用于生成注释）

    Returns:
        ministat 格式的摘要字符串
    """
    if not changed_paths:
        return ""

    output_lines = []

    # 添加降级标识头
    if revision is not None:
        output_lines.append(f"# ministat for r{revision} (degraded: diff unavailable)")
    else:
        output_lines.append("# ministat (degraded: diff unavailable)")
    output_lines.append("")

    # 按操作类型分组统计
    action_counts = {"A": 0, "M": 0, "D": 0, "R": 0, "other": 0}
    action_names = {"A": "added", "M": "modified", "D": "deleted", "R": "replaced"}

    # 计算最长路径用于对齐（限制最大宽度）
    max_path_len = 0
    for p in changed_paths:
        path = p.get("path", "")
        max_path_len = max(max_path_len, len(path))
    max_path_len = min(max_path_len, 60)

    for p in changed_paths:
        path = p.get("path", "")
        action = p.get("action", "?")
        kind = p.get("kind", "")

        # 统计操作类型
        if action in action_counts:
            action_counts[action] += 1
        else:
            action_counts["other"] += 1

        # 截断过长的路径
        display_path = path
        if len(path) > 60:
            display_path = "..." + path[-57:]

        # 生成条目
        kind_marker = "(dir)" if kind == "dir" else ""
        output_lines.append(f" {display_path:<{max_path_len}} | {action} {kind_marker}")

    # 添加总计行
    output_lines.append("")
    summary_parts = []
    for action, name in action_names.items():
        count = action_counts[action]
        if count > 0:
            summary_parts.append(f"{count} {name}")
    if action_counts["other"] > 0:
        summary_parts.append(f"{action_counts['other']} other")

    output_lines.append(f" {len(changed_paths)} path(s) changed: {', '.join(summary_parts)}")

    return "\n".join(output_lines)


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


def generate_diffstat(diff_content: str) -> str:
    """
    从 diff 内容生成 diffstat 摘要

    生成类似 `git diff --stat` 的输出格式，包含：
    - 每个文件的增删行数
    - 总计统计

    Args:
        diff_content: 完整的 diff 内容

    Returns:
        diffstat 格式的摘要字符串
    """
    if not diff_content or not diff_content.strip():
        return ""

    lines = diff_content.split("\n")
    file_stats = {}
    current_file = None
    additions = 0
    deletions = 0

    for line in lines:
        # 检测文件头（支持 svn diff 和 git diff 格式）
        if line.startswith("Index: "):
            # SVN diff 格式: Index: path/to/file
            if current_file:
                file_stats[current_file] = {"additions": additions, "deletions": deletions}
            current_file = line[7:].strip()
            additions = 0
            deletions = 0
        elif line.startswith("+++ "):
            # Git/unified diff 格式: +++ b/path/to/file
            if current_file is None:
                path = line[4:].strip()
                if path.startswith("b/"):
                    path = path[2:]
                elif path.startswith("a/"):
                    path = path[2:]
                if path != "/dev/null":
                    current_file = path
                    additions = 0
                    deletions = 0
        elif line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1

    # 保存最后一个文件的统计
    if current_file:
        file_stats[current_file] = {"additions": additions, "deletions": deletions}

    if not file_stats:
        return ""

    # 生成 diffstat 输出
    output_lines = []
    total_additions = 0
    total_deletions = 0

    # 计算最长文件名用于对齐
    max_name_len = max(len(f) for f in file_stats.keys()) if file_stats else 0
    max_name_len = min(max_name_len, 60)  # 限制最大宽度

    for filepath, stats in sorted(file_stats.items()):
        adds = stats["additions"]
        dels = stats["deletions"]
        total_additions += adds
        total_deletions += dels

        # 截断过长的文件名
        display_name = filepath
        if len(filepath) > 60:
            display_name = "..." + filepath[-57:]

        # 生成变更指示条（类似 git diff --stat）
        total_changes = adds + dels
        bar_width = min(total_changes, 50)
        if total_changes > 0:
            add_bars = int(bar_width * adds / total_changes)
            del_bars = bar_width - add_bars
            bar = "+" * add_bars + "-" * del_bars
        else:
            bar = ""

        output_lines.append(
            f" {display_name:<{max_name_len}} | {total_changes:>5} {bar}"
        )

    # 添加总计行
    files_count = len(file_stats)
    output_lines.append(
        f" {files_count} file(s) changed, "
        f"{total_additions} insertion(s)(+), {total_deletions} deletion(s)(-)"
    )

    return "\n".join(output_lines)


def normalize_url(url: str) -> str:
    """
    规范化 URL：去除空白和尾随斜杠

    Args:
        url: 原始 URL

    Returns:
        规范化后的 URL
    """
    if not url:
        return url
    return url.strip().rstrip("/")


def get_or_create_repo(
    conn: psycopg.Connection,
    svn_url: str,
    project_key: str,
    config: Optional[Config] = None,
) -> int:
    """
    获取或创建 SVN 仓库记录

    .. deprecated::
        请使用 scm_repo.ensure_repo(repo_type='svn', url=..., project_key=...) 替代。
        此函数将在未来版本中移除。

    Args:
        conn: 数据库连接（已弃用，不再使用）
        svn_url: SVN 仓库 URL
        project_key: 项目标识
        config: 配置实例

    Returns:
        repo_id
    """
    warnings.warn(
        "get_or_create_repo() 已弃用，请使用 scm_repo.ensure_repo(repo_type='svn', ...) 替代",
        DeprecationWarning,
        stacklevel=2,
    )
    # 规范化 URL（与 ensure_repo 保持一致）
    normalized_url = normalize_url(svn_url)
    # 委托给统一的 ensure_repo 实现
    return ensure_repo(
        repo_type="svn",
        url=normalized_url,
        project_key=project_key,
        config=config,
    )


# 注：get_last_synced_revision() 和 update_sync_cursor() 已弃用
# 请使用 engram_step1.cursor.load_svn_cursor 和 save_svn_cursor 替代


def get_svn_head_revision(svn_url: str, config: Optional[Config] = None) -> int:
    """
    获取 SVN 仓库的 HEAD revision

    Args:
        svn_url: SVN 仓库 URL
        config: 配置实例

    Returns:
        HEAD revision 号

    Raises:
        SvnCommandError: svn 命令执行失败
        SvnTimeoutError: svn 命令超时
        SvnAuthError: 认证失败
    """
    cmd = ["svn", "info", "--xml", svn_url]
    result = run_svn_cmd(cmd, timeout=60, config=config)
    
    if not result.success:
        if result.error_type == "timeout":
            raise SvnTimeoutError(
                "svn info 命令超时",
                {"url": svn_url},
            )
        elif result.error_type == "auth_error":
            raise SvnAuthError(
                f"svn info 认证失败: {result.error_message}",
                {"url": svn_url},
            )
        else:
            raise SvnCommandError(
                f"svn info 命令执行失败: {result.error_message}",
                {"url": svn_url, "stderr": result.stderr},
            )
    
    try:
        root = ET.fromstring(result.stdout)
        # 从 <entry revision="XXX"> 获取 revision
        entry = root.find(".//entry")
        if entry is not None:
            rev = entry.get("revision")
            if rev:
                return int(rev)
        raise SvnParseError(
            "无法从 svn info 输出解析 HEAD revision",
            {"url": svn_url, "output": result.stdout[:500]},
        )
    except ET.ParseError as e:
        raise SvnParseError(
            f"svn info XML 解析失败: {e}",
            {"url": svn_url, "error": str(e)},
        )


def fetch_svn_log_xml(
    svn_url: str,
    start_rev: int,
    end_rev: int,
    verbose: bool = True,
    config: Optional[Config] = None,
) -> str:
    """
    获取 SVN 日志 XML 输出

    Args:
        svn_url: SVN 仓库 URL
        start_rev: 起始 revision（包含）
        end_rev: 结束 revision（包含）
        verbose: 是否包含 changed paths（使用 -v 参数）
        config: 配置实例

    Returns:
        svn log 的 XML 输出

    Raises:
        SvnCommandError: svn 命令执行失败
        SvnTimeoutError: svn 命令超时
        SvnAuthError: 认证失败
    """
    cmd = ["svn", "log", "--xml", "-r", f"{start_rev}:{end_rev}"]
    if verbose:
        cmd.append("-v")
    cmd.append(svn_url)

    logger.info(f"拉取 SVN 日志: r{start_rev}:r{end_rev}")

    result = run_svn_cmd(cmd, timeout=300, config=config)  # 5 分钟超时
    
    if not result.success:
        if result.error_type == "timeout":
            raise SvnTimeoutError(
                "svn log 命令超时",
                {"url": svn_url, "start_rev": start_rev, "end_rev": end_rev},
            )
        elif result.error_type == "auth_error":
            raise SvnAuthError(
                f"svn log 认证失败: {result.error_message}",
                {"url": svn_url, "start_rev": start_rev, "end_rev": end_rev},
            )
        else:
            raise SvnCommandError(
                f"svn log 命令执行失败: {result.error_message}",
                {"url": svn_url, "start_rev": start_rev, "end_rev": end_rev, "stderr": result.stderr},
            )
    
    return result.stdout


@dataclass
class FetchDiffResult:
    """diff 获取结果，支持成功和降级场景"""
    success: bool
    content: Optional[str] = None
    error: Optional[PatchFetchError] = None
    error_category: Optional[str] = None
    error_message: Optional[str] = None
    endpoint: Optional[str] = None  # 原始请求端点


def fetch_svn_diff(
    svn_url: str,
    revision: int,
    path_filter: Optional[str] = None,
    timeout: int = 120,
    max_size_bytes: int = 10 * 1024 * 1024,  # 10MB 默认上限
    config: Optional[Config] = None,
) -> FetchDiffResult:
    """
    获取指定 revision 的 diff 内容

    Args:
        svn_url: SVN 仓库 URL
        revision: revision 号
        path_filter: 可选的路径过滤器（限定 diff 范围）
        timeout: 命令超时时间（秒）
        max_size_bytes: diff 内容最大字节数，超过则返回降级结果
        config: 配置实例

    Returns:
        FetchDiffResult 对象，包含成功/失败信息和错误分类
    """
    # 如果有路径过滤器，添加到 URL 后面
    if path_filter:
        url = f"{svn_url}/{path_filter.lstrip('/')}"
    else:
        url = svn_url
    
    cmd = ["svn", "diff", "-c", str(revision), url]
    
    # 脱敏的端点信息用于日志
    endpoint = f"svn diff -c {revision} {url}"
    
    logger.debug(f"拉取 SVN diff: r{revision}")
    
    result = run_svn_cmd(cmd, timeout=timeout, config=config)
    
    if not result.success:
        # 根据错误类型分类
        if result.error_type == "timeout":
            error = PatchFetchTimeoutError(
                f"svn diff 命令超时: {timeout}s",
                {"revision": revision, "timeout": timeout},
            )
            logger.warning(f"svn diff -c {revision} 超时 ({timeout}s)")
            return FetchDiffResult(
                success=False,
                error=error,
                error_category="timeout",
                error_message=f"timeout after {timeout}s",
                endpoint=endpoint,
            )
        elif result.error_type == "auth_error":
            error = PatchFetchCommandError(
                f"svn diff 认证失败: {result.error_message}",
                {"revision": revision, "error_type": "auth_error"},
            )
            logger.warning(f"svn diff -c {revision} 认证失败")
            return FetchDiffResult(
                success=False,
                error=error,
                error_category="auth_error",
                error_message=result.error_message,
                endpoint=endpoint,
            )
        else:
            error = PatchFetchCommandError(
                f"svn diff 命令执行失败: {result.error_message}",
                {"revision": revision, "stderr": result.stderr},
            )
            logger.warning(f"svn diff -c {revision} 执行失败")
            return FetchDiffResult(
                success=False,
                error=error,
                error_category="command_error",
                error_message=result.error_message,
                endpoint=endpoint,
            )
    
    content = result.stdout
    
    # 检查内容大小
    content_bytes = len(content.encode("utf-8"))
    if content_bytes > max_size_bytes:
        error = PatchFetchContentTooLargeError(
            f"diff 内容过大: {content_bytes} bytes > {max_size_bytes} bytes",
            {"revision": revision, "size_bytes": content_bytes, "max_bytes": max_size_bytes},
        )
        logger.warning(f"svn diff -c {revision} 内容过大: {content_bytes} bytes")
        return FetchDiffResult(
            success=False,
            error=error,
            error_category="content_too_large",
            error_message=str(error.message),
            endpoint=endpoint,
        )
    
    return FetchDiffResult(success=True, content=content, endpoint=endpoint)


def sync_revision_patch(
    conn: psycopg.Connection,
    repo_id: int,
    svn_url: str,
    revision: int,
    path_filter: Optional[str] = None,
    changed_paths_count: int = 0,
    changed_paths: Optional[List[Dict[str, str]]] = None,
    config: Optional[Config] = None,
    project_key: str = "default",
) -> Optional[Dict[str, Any]]:
    """
    同步单个 revision 的 patch

    流程:
    1. 调用 svn diff -c <rev> 获取 diff 内容
    2. 如果获取失败，降级生成 ministat（基于 changed_paths）
    3. 判断是否为 bulk commit，决定使用 diff 或 diffstat 格式
    4. 写入 artifacts: scm/<repo_id>/svn/r<rev>.diff 或 r<rev>.diffstat 或 r<rev>.ministat
    5. upsert scm.patch_blobs 记录（包含降级元数据）

    Args:
        conn: 数据库连接
        repo_id: 仓库 ID
        svn_url: SVN 仓库 URL
        revision: revision 号
        path_filter: 可选的路径过滤器
        changed_paths_count: 变更路径数量（用于 bulk 判断）
        changed_paths: 变更路径列表（用于降级时生成 ministat）
        config: 配置实例

    Returns:
        patch 元数据（包含 uri, sha256, size_bytes, blob_id, format, is_bulk, degraded），如果失败返回 None
    """
    # 1. 获取 diff 内容
    fetch_result = fetch_svn_diff(svn_url, revision, path_filter, config=config)
    
    # 初始化降级标志和元数据
    is_degraded = False
    degrade_reason = None
    source_fetch_error = None
    original_endpoint = None
    
    if not fetch_result.success:
        # 降级处理：使用 ministat 格式
        is_degraded = True
        degrade_reason = fetch_result.error_category
        source_fetch_error = fetch_result.error_message
        original_endpoint = fetch_result.endpoint
        
        # 使用 changed_paths 生成 ministat
        if changed_paths:
            content_to_store = generate_ministat_from_changed_paths(
                changed_paths, revision=revision
            )
            patch_format = "ministat"
            file_ext = "ministat"
            logger.info(
                f"Revision {revision} diff 获取失败 ({degrade_reason})，"
                f"降级使用 ministat 格式"
            )
        else:
            # 无法生成 ministat，返回 None
            logger.warning(
                f"Revision {revision} diff 获取失败且无 changed_paths，跳过"
            )
            return None
    else:
        diff_content = fetch_result.content or ""
        
        # 空 diff 也可以记录
        if not diff_content.strip():
            logger.debug(f"Revision {revision} 的 diff 为空")
        
        # 2. 判断是否为 bulk commit
        change_summary = ChangeSummary(
            changed_paths_count=changed_paths_count,
            diff_size_bytes=len(diff_content.encode("utf-8")),
        )
        bulk_flag, bulk_reason = is_bulk(change_summary, config)
        
        # 3. 根据 bulk 判断选择格式
        if bulk_flag:
            # bulk commit: 使用 diffstat 格式
            content_to_store = generate_diffstat(diff_content)
            patch_format = "diffstat"
            file_ext = "diffstat"
            logger.info(f"Revision {revision} 判定为 bulk ({bulk_reason})，使用 diffstat 格式")
        else:
            # 正常 commit: 使用完整 diff
            content_to_store = diff_content
            patch_format = "diff"
            file_ext = "diff"
    
    # 4. 先计算内容 sha256，再构建 v2 路径并写入 artifacts
    # v2 路径格式: scm/<project_key>/<repo_id>/svn/<rev_or_sha>/<sha256>.<ext>
    content_bytes = content_to_store.encode("utf-8") if isinstance(content_to_store, str) else content_to_store
    content_sha256 = compute_sha256(content_bytes)
    
    # 构建 v2 路径: scm/<project_key>/<repo_id>/svn/r<rev>/<sha256>.<ext>
    # rev_or_sha 对于 SVN 使用 r<rev> 格式
    artifact_path = build_scm_artifact_path(
        project_key=project_key,
        repo_id=str(repo_id),
        source_type="svn",
        rev_or_sha=f"r{revision}",
        sha256=content_sha256,
        ext=file_ext,
    )
    
    artifact_result = write_text_artifact(artifact_path, content_to_store)
    
    logger.debug(
        f"写入 artifact: {artifact_result['uri']} "
        f"(size={artifact_result['size_bytes']}, sha256={artifact_result['sha256'][:16]}..., "
        f"format={patch_format}, degraded={is_degraded})"
    )
    
    # 5. 构建 meta_json（包含降级元数据与 evidence_uri）
    # 注意：对 source_fetch_error 和 original_endpoint 进行脱敏，避免泄露敏感信息
    meta_json: Dict[str, Any] = {"materialize_status": "done"}
    if is_degraded:
        meta_json["degraded"] = True
        meta_json["degrade_reason"] = degrade_reason
        if source_fetch_error:
            meta_json["source_fetch_error"] = redact(source_fetch_error)
        if original_endpoint:
            meta_json["original_endpoint"] = redact(original_endpoint)
    
    # 6. upsert scm.patch_blobs
    # source_id 格式: <repo_id>:<rev>
    source_id = f"{repo_id}:{revision}"
    
    # 构建 canonical evidence_uri (memory://patch_blobs/<source_type>/<source_id>/<sha256>)
    # 用于 analysis.* 和 governance.* 表中的 evidence_refs_json 引用
    evidence_uri = build_evidence_uri("svn", source_id, artifact_result["sha256"])
    meta_json["evidence_uri"] = evidence_uri
    
    blob_id = upsert_patch_blob(
        conn=conn,
        source_type="svn",
        source_id=source_id,
        sha256=artifact_result["sha256"],
        uri=artifact_result["uri"],
        size_bytes=artifact_result["size_bytes"],
        format=patch_format,
        meta_json=meta_json,
    )
    
    # 返回结果
    result = {
        "revision": revision,
        "uri": artifact_result["uri"],
        "sha256": artifact_result["sha256"],
        "size_bytes": artifact_result["size_bytes"],
        "blob_id": blob_id,
        "format": patch_format,
        "degraded": is_degraded,
    }
    
    if is_degraded:
        result["degrade_reason"] = degrade_reason
    else:
        # 非降级情况下才有 bulk 信息
        result["is_bulk"] = bulk_flag
        result["bulk_reason"] = bulk_reason
    
    return result


def sync_patches_for_revisions(
    conn: psycopg.Connection,
    repo_id: int,
    svn_url: str,
    revisions: List[SvnRevision],
    path_filter: Optional[str] = None,
    config: Optional[Config] = None,
    project_key: str = "default",
    patch_fetch_controller: Optional[SvnPatchFetchController] = None,
    # 锁续租相关参数
    worker_id: Optional[str] = None,
    renew_interval_revs: int = DEFAULT_RENEW_INTERVAL_REVS,
) -> Dict[str, Any]:
    """
    批量同步多个 revision 的 patches

    Args:
        conn: 数据库连接
        repo_id: 仓库 ID
        svn_url: SVN 仓库 URL
        revisions: SvnRevision 列表
        path_filter: 可选的路径过滤器
        config: 配置实例
        patch_fetch_controller: 可选的 patch 获取控制器，用于连续错误时暂停
        worker_id: worker 标识符（用于锁续租）
        renew_interval_revs: 每处理 N 个 revision 后续租一次

    Returns:
        同步结果统计
    """
    result = {
        "total": len(revisions),
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "bulk_count": 0,
        "degraded_count": 0,
        "patches": [],
        "skipped_by_controller": 0,  # 被控制器跳过的数量
    }
    
    # 如果未提供控制器，创建一个默认的
    controller = patch_fetch_controller or SvnPatchFetchController()
    
    # revision 处理计数（用于续租）
    rev_processed_count = 0
    
    for rev in revisions:
        # 分段续租：每处理 renew_interval_revs 个 revision 后续租一次
        rev_processed_count += 1
        if worker_id and rev_processed_count % renew_interval_revs == 0:
            try:
                renewed = scm_sync_lock.renew(
                    repo_id=repo_id,
                    job_type=JOB_TYPE_SVN,
                    worker_id=worker_id,
                    conn=conn,
                )
                if renewed:
                    logger.debug(
                        f"续租成功 (已处理 {rev_processed_count}/{len(revisions)} revisions)"
                    )
                else:
                    logger.warning("续租失败，锁可能已被抢占")
            except Exception as e:
                logger.warning(f"续租时发生异常: {e}")
        # 检查控制器是否建议跳过
        if controller.should_skip_patches:
            result["skipped"] += 1
            result["skipped_by_controller"] += 1
            logger.debug(f"跳过 revision {rev.revision} 的 patch（控制器建议: {controller.skip_reason}）")
            continue
        
        try:
            patch_info = sync_revision_patch(
                conn=conn,
                repo_id=repo_id,
                svn_url=svn_url,
                revision=rev.revision,
                path_filter=path_filter,
                changed_paths_count=len(rev.changed_paths),
                changed_paths=rev.changed_paths,  # 传递 changed_paths 用于降级
                config=config,
                project_key=project_key,
            )
            
            if patch_info:
                result["success"] += 1
                result["patches"].append(patch_info)
                if patch_info.get("is_bulk"):
                    result["bulk_count"] += 1
                if patch_info.get("degraded"):
                    result["degraded_count"] += 1
                    # 记录降级错误到控制器
                    degrade_reason = patch_info.get("degrade_reason")
                    if degrade_reason:
                        controller.record_error(degrade_reason)
                else:
                    # 成功获取完整 diff，记录成功
                    controller.record_success()
            else:
                result["skipped"] += 1
                
        except Exception as e:
            logger.warning(f"同步 revision {rev.revision} 的 patch 失败: {e}")
            result["failed"] += 1
            # 尝试从异常中获取错误类型
            error_category = getattr(e, "error_category", "unknown")
            controller.record_error(error_category)
    
    # 提交所有 patch_blobs 记录
    conn.commit()
    
    # 记录控制器状态
    if result["skipped_by_controller"] > 0:
        logger.warning(
            f"Patch 获取控制器跳过了 {result['skipped_by_controller']} 个 revision "
            f"(原因: {controller.skip_reason})"
        )
    
    logger.info(
        f"Patch 同步完成: 成功={result['success']}, "
        f"跳过={result['skipped']}, 失败={result['failed']}, "
        f"bulk={result['bulk_count']}, degraded={result['degraded_count']}"
    )
    
    return result


def parse_svn_log_xml(xml_content: str) -> List[SvnRevision]:
    """
    解析 svn log --xml 输出

    Args:
        xml_content: XML 格式的 svn log 输出

    Returns:
        SvnRevision 列表

    Raises:
        SvnParseError: XML 解析失败
    """
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as e:
        raise SvnParseError(
            f"XML 解析失败: {e}",
            {"error": str(e), "xml_snippet": xml_content[:500]},
        )

    revisions = []
    for log_entry in root.findall("logentry"):
        revision = int(log_entry.get("revision", 0))

        # 解析 author
        author_elem = log_entry.find("author")
        author = author_elem.text if author_elem is not None and author_elem.text else ""

        # 解析日期 (ISO 8601 格式: 2024-01-15T10:30:45.000000Z)
        date_elem = log_entry.find("date")
        date = None
        if date_elem is not None and date_elem.text:
            try:
                # 处理微秒和时区
                date_str = date_elem.text.replace("Z", "+00:00")
                # 移除微秒部分（如果存在）以简化解析
                if "." in date_str:
                    date_str = date_str.split(".")[0] + "+00:00"
                date = datetime.fromisoformat(date_str)
            except ValueError:
                logger.warning(f"无法解析日期: {date_elem.text}，revision={revision}")

        # 解析 message
        msg_elem = log_entry.find("msg")
        message = msg_elem.text if msg_elem is not None and msg_elem.text else ""

        # 解析 changed paths
        changed_paths = []
        paths_elem = log_entry.find("paths")
        if paths_elem is not None:
            for path_elem in paths_elem.findall("path"):
                path_info = {
                    "path": path_elem.text or "",
                    "action": path_elem.get("action", ""),  # A/D/M/R
                    "kind": path_elem.get("kind", ""),      # file/dir
                }
                # 可选: copyfrom 信息
                copyfrom_path = path_elem.get("copyfrom-path")
                copyfrom_rev = path_elem.get("copyfrom-rev")
                if copyfrom_path:
                    path_info["copyfrom_path"] = copyfrom_path
                if copyfrom_rev:
                    path_info["copyfrom_rev"] = int(copyfrom_rev)
                changed_paths.append(path_info)

        revisions.append(SvnRevision(
            revision=revision,
            author=author,
            date=date,
            message=message,
            changed_paths=changed_paths,
        ))

    return revisions


def insert_svn_revisions(
    conn: psycopg.Connection,
    repo_id: int,
    revisions: List[SvnRevision],
    config: Optional[Config] = None,
) -> int:
    """
    将 SVN revision 插入数据库

    使用 db.upsert_svn_revision 实现 upsert，基于 repo_id + rev_num 唯一索引
    (idx_svn_revisions_repo_revnum) 保证幂等，支持 overlap 策略

    Args:
        conn: 数据库连接
        repo_id: 仓库 ID
        revisions: SvnRevision 列表
        config: 配置实例

    Returns:
        插入/更新的记录数
    """
    if not revisions:
        return 0

    inserted = 0
    for rev in revisions:
        # 构建 meta_json（包含 changed_paths 等额外信息）
        meta_json = {
            "changed_paths": rev.changed_paths,
        }

        # 使用统一的 is_bulk 函数判断（此时还没有 diff 内容，仅根据 changed_paths 判断）
        change_summary = ChangeSummary(
            changed_paths_count=len(rev.changed_paths),
            diff_size_bytes=0,  # 此时还没有 diff
        )
        bulk_flag, bulk_reason = is_bulk(change_summary, config)
        
        # 在 meta_json 中标注 bulk 信息
        if bulk_flag:
            meta_json["is_bulk_by_paths"] = True
            meta_json["bulk_reason"] = bulk_reason

        # 解析作者身份并填充到 meta_json
        meta_json = resolve_and_enrich_meta(
            meta_json,
            account_type="svn",
            username=rev.author,
            config=config,
        )

        # 构建 source_id: svn:<repo_id>:<rev_num>
        source_id = build_svn_source_id(repo_id, rev.revision)

        try:
            # 调用 db.upsert_svn_revision，基于 repo_id + rev_num 查询/更新
            # rev_num = revision（保留 rev_id 兼容列可置空/同步）
            upsert_svn_revision(
                conn=conn,
                repo_id=repo_id,
                rev_num=rev.revision,
                author_raw=rev.author,
                ts=rev.date,
                message=rev.message,
                is_bulk=bulk_flag,
                bulk_reason=bulk_reason,
                meta_json=meta_json,
                source_id=source_id,
            )
            inserted += 1
        except Exception as e:
            logger.error(f"插入 revision {rev.revision} 失败: {e}")
            raise DatabaseError(
                f"插入 SVN revision 失败: {e}",
                {"revision": rev.revision, "error": str(e)},
            )

    conn.commit()
    return inserted


def sync_svn_revisions(
    sync_config: SyncConfig,
    project_key: str,
    config: Optional[Config] = None,
    verbose: bool = True,
    fetch_patches: bool = False,
    patch_path_filter: Optional[str] = None,
    lock_lease_seconds: int = DEFAULT_LOCK_LEASE_SECONDS,
    renew_interval_revs: int = DEFAULT_RENEW_INTERVAL_REVS,
) -> Dict[str, Any]:
    """
    执行 SVN 日志同步

    Args:
        sync_config: 同步配置
        project_key: 项目标识
        config: Config 实例
        verbose: 是否输出详细信息
        fetch_patches: 是否同步 patch（diff 内容）
        patch_path_filter: patch 的路径过滤器（限定 diff 范围）
        lock_lease_seconds: 锁租约时长（秒），默认 300 秒（SVN fetch_patches 需要更长）
        renew_interval_revs: 每处理 N 个 revision 后续租一次，默认 10

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
        "start_rev": None,
        "end_rev": None,
        "error": None,
        "error_category": None,  # 标准字段：错误分类
        "retry_after": None,  # 标准字段：建议的重试等待秒数（可选）
        "counts": {},  # 标准字段：计数统计
        "cursor_advance_reason": None,  # 游标推进原因
        "cursor_advance_stopped_at": None,  # strict 模式下游标停止的位置
        "unrecoverable_errors": [],  # 不可恢复的错误列表
        "degraded_reasons": {},  # 降级原因分布
        "missing_types": [],  # best_effort 模式下记录的缺失类型
        "strict_mode": sync_config.strict,  # 是否启用严格模式
        "sync_mode": SCM_SYNC_MODE_STRICT if sync_config.strict else SCM_SYNC_MODE_BEST_EFFORT,
        "locked": False,  # 是否因锁被跳过
        "skipped": False,  # 是否被跳过
    }
    
    logger.info(f"[run_id={run_id[:8]}] 开始 SVN 同步 (strict={sync_config.strict})")
    
    # 锁相关变量（用于 finally 块）
    lock_acquired = False
    lock_repo_id: Optional[int] = None

    # sync_run 记录变量（用于 finally 块）
    sync_run_started = False
    sync_run_repo_id = None
    sync_run_cursor_before = None
    sync_run_conn = None

    # 1. 规范化 URL 并获取或创建仓库记录（使用统一的 ensure_repo）
    normalized_svn_url = normalize_url(sync_config.svn_url)
    repo_id = ensure_repo(
        repo_type="svn",
        url=normalized_svn_url,
        project_key=project_key,
        config=config,
    )
    result["repo_id"] = repo_id
    lock_repo_id = repo_id

    conn = get_connection(config=config)
    try:
        # 1.5. 尝试获取分布式锁（repo_id + job_type）
        lock_acquired = scm_sync_lock.claim(
            repo_id=repo_id,
            job_type=JOB_TYPE_SVN,
            worker_id=worker_id,
            lease_seconds=lock_lease_seconds,
            conn=conn,
        )
        if not lock_acquired:
            # 锁被其他 worker 持有，返回 locked/skip 结果
            lock_info = scm_sync_lock.get(repo_id, JOB_TYPE_SVN, conn=conn)
            logger.warning(
                f"[run_id={run_id[:8]}] 锁被其他 worker 持有，跳过本次同步 "
                f"(repo_id={repo_id}, job_type={JOB_TYPE_SVN}, "
                f"locked_by={lock_info.get('locked_by') if lock_info else 'unknown'})"
            )
            result["locked"] = True
            result["skipped"] = True
            result["success"] = True  # locked/skipped 视为成功（不需要重试）
            result["message"] = "锁被其他 worker 持有，跳过本次同步"
            result["error_category"] = "lock_held"
            result["counts"] = {"synced_count": 0, "diff_count": 0}
            return result
        
        logger.debug(f"[run_id={run_id[:8]}] 成功获取锁 (repo_id={repo_id}, worker_id={worker_id})")

        # 2. 获取上次同步的 revision（使用统一的游标模块）
        cursor = load_svn_cursor(repo_id, config)
        last_synced = cursor.last_rev
        logger.info(f"上次同步 revision: {last_synced}")

        # 保存游标快照用于 sync_run 记录
        sync_run_cursor_before = {"last_rev": last_synced}
        sync_run_repo_id = repo_id
        sync_run_conn = conn
        
        # 记录 sync_run 开始
        try:
            insert_sync_run_start(
                conn=conn,
                run_id=run_id,
                repo_id=repo_id,
                job_type="svn",
                mode="incremental",
                cursor_before=sync_run_cursor_before,
                meta_json={
                    "batch_size": sync_config.batch_size,
                    "overlap": sync_config.overlap,
                    "fetch_patches": fetch_patches,
                },
            )
            conn.commit()
            sync_run_started = True
        except Exception as e:
            logger.warning(f"记录 sync_run 开始失败 (非致命): {e}")

        # 3. 计算本次同步范围（考虑 overlap）
        start_rev = max(1, last_synced + 1 - sync_config.overlap)
        if last_synced > 0 and sync_config.overlap > 0:
            logger.info(f"应用 overlap 策略: 回退 {sync_config.overlap} 个 revision")

        # 4. 获取 HEAD revision
        head_rev = get_svn_head_revision(sync_config.svn_url, config=config)
        logger.info(f"HEAD revision: {head_rev}")

        if start_rev > head_rev:
            logger.info("已是最新，无需同步")
            result["success"] = True
            result["message"] = "已是最新，无需同步"
            result["counts"] = {"synced_count": 0, "diff_count": 0}
            return result

        # 5. 应用 batch_size 限制
        end_rev = min(head_rev, start_rev + sync_config.batch_size - 1)
        logger.info(f"本次同步范围: r{start_rev} ~ r{end_rev} (batch_size={sync_config.batch_size})")

        result["start_rev"] = start_rev
        result["end_rev"] = end_rev

        # 6. 拉取 SVN 日志
        xml_content = fetch_svn_log_xml(
            sync_config.svn_url,
            start_rev,
            end_rev,
            verbose=verbose,
            config=config,
        )

        # 7. 解析日志
        revisions = parse_svn_log_xml(xml_content)
        logger.info(f"解析到 {len(revisions)} 个 revision")

        # 8. 写入数据库
        synced_count = insert_svn_revisions(conn, repo_id, revisions, config)
        logger.info(f"写入数据库: {synced_count} 条记录")

        result["synced_count"] = synced_count

        # 9. Patch 阶段：同步每个 revision 的 diff
        if fetch_patches and revisions:
            logger.info(f"开始同步 {len(revisions)} 个 revision 的 patches...")
            # 创建 patch 获取控制器，启用连续错误暂停策略
            patch_fetch_controller = SvnPatchFetchController(
                timeout_threshold=3,
                content_too_large_threshold=5,
            )
            patch_result = sync_patches_for_revisions(
                conn=conn,
                repo_id=repo_id,
                svn_url=sync_config.svn_url,
                revisions=revisions,
                path_filter=patch_path_filter,
                config=config,
                project_key=project_key,
                patch_fetch_controller=patch_fetch_controller,
                worker_id=worker_id,  # 传递 worker_id 用于锁续租
                renew_interval_revs=renew_interval_revs,
            )
            result["patch_stats"] = {
                "total": patch_result["total"],
                "success": patch_result["success"],
                "failed": patch_result["failed"],
                "skipped": patch_result["skipped"],
                "bulk_count": patch_result["bulk_count"],
                "skipped_by_controller": patch_result.get("skipped_by_controller", 0),
            }

        # 10. 更新游标（使用实际同步的最后 revision）
        # 检查是否有不可恢复的错误（来自 patch 获取阶段）
        unrecoverable_errors = result.get("unrecoverable_errors", [])
        patch_stats = result.get("patch_stats", {})
        
        # 从 patch_stats 中收集降级信息
        if fetch_patches and patch_stats:
            failed_count = patch_stats.get("failed", 0)
            if failed_count > 0:
                # 认为有失败的 patch 获取是潜在的不可恢复错误
                unrecoverable_errors.append({
                    "error_category": "patch_fetch_failed",
                    "count": failed_count,
                })
                result["unrecoverable_errors"] = unrecoverable_errors
        
        encountered_unrecoverable_error = len(unrecoverable_errors) > 0
        cursor_advance_reason: Optional[str] = None
        should_advance = True
        
        if revisions:
            max_rev = max(r.revision for r in revisions)
            
            if sync_config.strict and encountered_unrecoverable_error:
                # strict 模式：查找最后一个成功处理（包括 patch）的 revision
                # 简化处理：如果有任何 patch 失败，不推进游标
                should_advance = False
                cursor_advance_reason = "strict_mode:unrecoverable_error_encountered"
                result["cursor_advance_stopped_at"] = f"r{last_synced}"
                logger.warning(
                    f"[strict mode] 遇到不可恢复的错误，游标不推进 (维持在 r{last_synced})"
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
                    f"[best_effort mode] 遇到不可恢复的错误但仍推进游标到 r{max_rev}，"
                    f"缺失类型: {','.join(missing_types)}"
                )
            else:
                cursor_advance_reason = "batch_complete"
            
            if should_advance:
                save_svn_cursor(repo_id, max_rev, synced_count, config)
                logger.info(f"更新同步游标: repo_id={repo_id}, last_rev={max_rev}")
                result["last_rev"] = max_rev
            else:
                # 保留原游标
                result["last_rev"] = last_synced
        
        result["cursor_advance_reason"] = cursor_advance_reason
        result["success"] = True
        result["has_more"] = end_rev < head_rev
        if result["has_more"]:
            result["remaining"] = head_rev - end_rev
            logger.info(f"还有 {result['remaining']} 个 revision 待同步")
        
        # 标准字段: counts 统计
        result["counts"] = {
            "synced_count": result.get("synced_count", 0),
            "diff_count": result.get("diff_count", 0),
            "bulk_count": result.get("bulk_count", 0),
            "degraded_count": result.get("degraded_count", 0),
        }

        # 11. 创建 logbook item/event 和 attachments（如果有 patch 结果）
        patch_stats = result.get("patch_stats")
        # 从上面 fetch_patches 块中的 patch_result 获取 patches
        patches = patch_result["patches"] if fetch_patches and revisions and "patch_stats" in result else []
        
        if patches:
            try:
                # 创建同步批次 logbook item
                batch_title = f"SVN Sync Batch r{start_rev}-r{end_rev} (repo_id={repo_id})"
                item_id = create_item(
                    item_type="scm_sync_batch",
                    title=batch_title,
                    scope_json={
                        "source_type": "svn",
                        "repo_id": repo_id,
                        "start_rev": start_rev,
                        "end_rev": end_rev,
                        "synced_count": synced_count,
                    },
                    status="done",
                    config=config,
                )
                result["logbook_item_id"] = item_id
                logger.debug(f"创建同步批次 logbook item: item_id={item_id}")

                # 添加 sync_completed 事件
                event_payload = {
                    "synced_count": synced_count,
                    "patch_count": len(patches),
                    "bulk_count": patch_stats.get("bulk_count", 0) if patch_stats else 0,
                }
                add_event(
                    item_id=item_id,
                    event_type="sync_completed",
                    payload_json=event_payload,
                    status_from="running",
                    status_to="done",
                    source="scm_sync_svn",
                    config=config,
                )

                # 将 patch_blobs 以 kind='patch' 写入 attachments
                attachment_count = 0
                for patch_info in patches:
                    if not patch_info:
                        continue
                    
                    # 构建 evidence ref
                    source_id = f"{repo_id}:{patch_info['revision']}"
                    evidence_ref = build_evidence_ref_for_patch_blob(
                        source_type="svn",
                        source_id=source_id,
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
                            "source_type": "svn",
                            "source_id": source_id,
                            "revision": patch_info["revision"],
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
        result["error"] = str(e)
        result["error_category"] = getattr(e, "error_category", None) or getattr(e, "error_type", "engram_error")
        result["counts"] = {
            "synced_count": result.get("synced_count", 0),
            "diff_count": result.get("diff_count", 0),
        }
        raise
    except Exception as e:
        logger.exception(f"同步过程中发生错误: {e}")
        result["error"] = str(e)
        result["error_category"] = "exception"
        result["counts"] = {
            "synced_count": result.get("synced_count", 0),
            "diff_count": result.get("diff_count", 0),
        }
        raise SvnSyncError(
            f"SVN 同步失败: {e}",
            {"error": str(e)},
        )
    finally:
        # 释放分布式锁（确保在任何情况下都释放）
        if lock_acquired and lock_repo_id is not None:
            try:
                released = scm_sync_lock.release(
                    repo_id=lock_repo_id,
                    job_type=JOB_TYPE_SVN,
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
                if result.get("last_rev"):
                    cursor_after = {"last_rev": result.get("last_rev")}
                
                # 构建计数统计
                patch_stats = result.get("patch_stats", {})
                counts = {
                    "synced_count": result.get("synced_count", 0),
                    "patch_success": patch_stats.get("success", 0),
                    "patch_failed": patch_stats.get("failed", 0),
                    "bulk_count": patch_stats.get("bulk_count", 0),
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
        description="SVN 日志同步脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 使用配置文件同步
    python scm_sync_svn.py --config config.toml

    # 指定批量大小和 overlap
    python scm_sync_svn.py --batch-size 50 --overlap 5

    # 循环同步直到完成
    python scm_sync_svn.py --loop

    # Backfill 模式：回填指定范围的 revision（不更新游标）
    python scm_sync_svn.py --backfill --start-rev 100 --end-rev 200

    # Backfill 模式：回填并更新游标
    python scm_sync_svn.py --backfill --start-rev 100 --end-rev 200 --update-watermark

    # Backfill dry-run：预览将处理的范围
    python scm_sync_svn.py --backfill --start-rev 100 --end-rev 200 --dry-run
        """,
    )

    add_config_argument(parser)

    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help=f"每次同步的最大 revision 数 (默认: {DEFAULT_BATCH_SIZE})",
    )

    parser.add_argument(
        "--overlap",
        type=int,
        default=None,
        help=f"重叠 revision 数，用于重新同步可能变更的记录 (默认: {DEFAULT_OVERLAP})",
    )

    parser.add_argument(
        "--svn-url",
        type=str,
        default=None,
        help="SVN 仓库 URL（覆盖配置文件）",
    )

    parser.add_argument(
        "--loop",
        action="store_true",
        help="循环同步直到全部完成",
    )

    parser.add_argument(
        "--strict",
        action="store_true",
        help="严格模式：发生不可恢复的错误时，不推进游标",
    )

    parser.add_argument(
        "--sync-mode",
        type=str,
        choices=["strict", "best_effort"],
        default=None,
        help="同步模式: strict=严格模式(错误时不推进游标), best_effort=尽力模式(错误时记录并推进，默认)",
    )

    parser.add_argument(
        "--fetch-patches",
        action="store_true",
        help="同步 patch（获取每个 revision 的 diff 内容）",
    )

    parser.add_argument(
        "--patch-path-filter",
        type=str,
        default=None,
        help="Patch 路径过滤器，限定 diff 范围（例如: trunk/src）",
    )

    # Backfill 模式参数
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Backfill 模式：回填指定范围的 revision，默认不更新游标",
    )

    parser.add_argument(
        "--start-rev",
        type=int,
        default=None,
        help="Backfill 起始 revision（包含），仅在 --backfill 模式下有效",
    )

    parser.add_argument(
        "--end-rev",
        type=int,
        default=None,
        help="Backfill 结束 revision（包含），仅在 --backfill 模式下有效，默认为 HEAD",
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


def backfill_svn_revisions(
    sync_config: SyncConfig,
    project_key: str,
    start_rev: int,
    end_rev: Optional[int],
    update_watermark: bool = False,
    dry_run: bool = False,
    config: Optional[Config] = None,
    verbose: bool = True,
    fetch_patches: bool = False,
    patch_path_filter: Optional[str] = None,
) -> Dict[str, Any]:
    """
    执行 SVN 日志回填（backfill）

    与增量同步不同，backfill 模式：
    - 使用指定的 start_rev/end_rev 范围（不依赖游标）
    - 默认不更新游标（除非 update_watermark=True）
    - 支持 dry_run 模式（仅输出范围，不写 DB）

    Args:
        sync_config: 同步配置
        project_key: 项目标识
        start_rev: 起始 revision（包含）
        end_rev: 结束 revision（包含），None 表示 HEAD
        update_watermark: 是否更新游标
        dry_run: 仅预览，不写 DB
        config: Config 实例
        verbose: 是否输出详细信息
        fetch_patches: 是否同步 patch
        patch_path_filter: patch 的路径过滤器

    Returns:
        回填结果统计
    """
    result = {
        "success": False,
        "mode": "backfill",
        "dry_run": dry_run,
        "update_watermark": update_watermark,
        "synced_count": 0,
        "start_rev": start_rev,
        "end_rev": None,
        "error": None,
    }

    # 1. 规范化 URL 并获取或创建仓库记录
    normalized_svn_url = normalize_url(sync_config.svn_url)

    if dry_run:
        # dry-run 模式下也需要获取 repo_id（仅读取）
        logger.info("[dry-run] 检查仓库配置...")

    repo_id = ensure_repo(
        repo_type="svn",
        url=normalized_svn_url,
        project_key=project_key,
        config=config,
    )
    result["repo_id"] = repo_id

    # 2. 确定结束 revision
    if end_rev is None:
        head_rev = get_svn_head_revision(sync_config.svn_url, config=config)
        end_rev = head_rev
        logger.info(f"结束 revision 未指定，使用 HEAD: r{head_rev}")

    result["end_rev"] = end_rev

    # 验证范围
    if start_rev > end_rev:
        raise ValidationError(
            f"起始 revision ({start_rev}) 大于结束 revision ({end_rev})",
            {"start_rev": start_rev, "end_rev": end_rev},
        )

    revision_count = end_rev - start_rev + 1
    result["revision_count"] = revision_count

    logger.info(f"Backfill 范围: r{start_rev} ~ r{end_rev} (共 {revision_count} 个 revision)")

    # 3. dry-run 模式：仅输出范围信息
    if dry_run:
        logger.info("[dry-run] 模式：不执行实际同步")
        logger.info(f"[dry-run] 将处理 {revision_count} 个 revision")
        logger.info(f"[dry-run] 范围: r{start_rev} ~ r{end_rev}")
        logger.info(f"[dry-run] 仓库 ID: {repo_id}")
        logger.info(f"[dry-run] 更新游标: {update_watermark}")
        logger.info(f"[dry-run] 获取 patches: {fetch_patches}")

        result["success"] = True
        result["message"] = f"[dry-run] 将处理 {revision_count} 个 revision (r{start_rev} ~ r{end_rev})"
        result["counts"] = {"synced_count": 0, "diff_count": 0}
        return result

    # 4. 执行实际同步
    conn = get_connection(config=config)
    try:
        # 拉取 SVN 日志
        xml_content = fetch_svn_log_xml(
            sync_config.svn_url,
            start_rev,
            end_rev,
            verbose=verbose,
            config=config,
        )

        # 解析日志
        revisions = parse_svn_log_xml(xml_content)
        logger.info(f"解析到 {len(revisions)} 个 revision")

        # 写入数据库
        synced_count = insert_svn_revisions(conn, repo_id, revisions, config)
        logger.info(f"写入数据库: {synced_count} 条记录")

        result["synced_count"] = synced_count

        # Patch 阶段
        if fetch_patches and revisions:
            logger.info(f"开始同步 {len(revisions)} 个 revision 的 patches...")
            # 创建 patch 获取控制器，启用连续错误暂停策略
            patch_fetch_controller = SvnPatchFetchController(
                timeout_threshold=3,
                content_too_large_threshold=5,
            )
            patch_result = sync_patches_for_revisions(
                conn=conn,
                repo_id=repo_id,
                svn_url=sync_config.svn_url,
                revisions=revisions,
                path_filter=patch_path_filter,
                config=config,
                project_key=project_key,
                patch_fetch_controller=patch_fetch_controller,
            )
            result["patch_stats"] = {
                "total": patch_result["total"],
                "success": patch_result["success"],
                "failed": patch_result["failed"],
                "skipped": patch_result["skipped"],
                "bulk_count": patch_result["bulk_count"],
                "skipped_by_controller": patch_result.get("skipped_by_controller", 0),
            }

        # 5. 更新游标（仅当 update_watermark=True）
        if update_watermark and revisions:
            max_rev = max(r.revision for r in revisions)
            save_svn_cursor(repo_id, max_rev, synced_count, config)
            logger.info(f"更新同步游标: repo_id={repo_id}, last_rev={max_rev}")
            result["last_rev"] = max_rev
            result["watermark_updated"] = True
        else:
            result["watermark_updated"] = False
            if revisions:
                logger.info("Backfill 模式：跳过游标更新（使用 --update-watermark 可更新）")

        result["success"] = True
        # 标准字段: counts 统计
        result["counts"] = {
            "synced_count": result.get("synced_count", 0),
            "diff_count": result.get("diff_count", 0),
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
        raise SvnSyncError(
            f"SVN Backfill 失败: {e}",
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

        # 获取 SVN 配置（使用兼容读取，优先 scm.svn.* 回退 svn.*）
        svn_cfg = get_svn_config(config)

        # CLI 参数覆盖配置文件
        svn_url = args.svn_url or svn_cfg.get("url")
        if not svn_url:
            raise ValidationError(
                "缺少 SVN URL，请在配置文件中设置 scm.svn.url 或使用 --svn-url 参数",
                {"hint": "配置示例: [scm.svn]\\nurl = \"svn://svn.example.com/project\""},
            )

        # 获取 project_key
        project_key = config.get("project.project_key", "default")

        # 解析 strict 模式（使用统一的配置函数）
        cli_sync_override = None
        if args.strict:
            cli_sync_override = "strict"
        elif args.sync_mode:
            cli_sync_override = args.sync_mode
        
        strict = is_strict_mode(config, cli_sync_override)

        # 构建同步配置
        sync_config = SyncConfig(
            svn_url=svn_url,
            batch_size=args.batch_size or svn_cfg.get("batch_size") or DEFAULT_BATCH_SIZE,
            overlap=args.overlap if args.overlap is not None else (svn_cfg.get("overlap") or DEFAULT_OVERLAP),
            strict=strict,
        )

        logger.info(f"SVN URL: {sync_config.svn_url}")
        logger.info(f"Batch size: {sync_config.batch_size}, Overlap: {sync_config.overlap}")

        # Backfill 模式
        if args.backfill:
            if args.start_rev is None:
                raise ValidationError(
                    "Backfill 模式需要指定 --start-rev 参数",
                    {"hint": "示例: --backfill --start-rev 100 --end-rev 200"},
                )

            logger.info("进入 Backfill 模式")
            if args.dry_run:
                logger.info("[dry-run] 仅预览，不执行实际同步")

            result = backfill_svn_revisions(
                sync_config,
                project_key,
                start_rev=args.start_rev,
                end_rev=args.end_rev,
                update_watermark=args.update_watermark,
                dry_run=args.dry_run,
                config=config,
                verbose=args.verbose,
                fetch_patches=args.fetch_patches,
                patch_path_filter=args.patch_path_filter,
            )

            if args.json:
                print(json.dumps(result, default=str, ensure_ascii=False))

            return 0

        # 普通增量同步模式
        if args.dry_run:
            raise ValidationError(
                "--dry-run 仅在 --backfill 模式下有效",
                {"hint": "示例: --backfill --start-rev 100 --dry-run"},
            )

        # 执行同步
        total_synced = 0
        loop_count = 0
        max_loops = 1000  # 防止无限循环

        while True:
            loop_count += 1
            if loop_count > max_loops:
                logger.warning(f"达到最大循环次数限制 ({max_loops})，退出")
                break

            result = sync_svn_revisions(
                sync_config,
                project_key,
                config,
                verbose=args.verbose,
                fetch_patches=args.fetch_patches,
                patch_path_filter=args.patch_path_filter,
            )
            total_synced += result.get("synced_count", 0)

            if args.json:
                print(json.dumps(result, default=str, ensure_ascii=False))

            if not args.loop or not result.get("has_more", False):
                break

            logger.info(f"继续同步下一批（第 {loop_count + 1} 轮）...")

        if args.loop and not args.json:
            logger.info(f"同步完成，共 {loop_count} 轮，总计 {total_synced} 个 revision")

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
