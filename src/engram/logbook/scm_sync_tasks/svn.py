# -*- coding: utf-8 -*-
"""
svn - SVN 同步核心实现

本模块提供 SVN revisions 同步的核心逻辑，移除对根目录模块的依赖。

功能:
- SVN revision 获取与解析
- SVN diff 获取
- 数据库写入
- 游标管理

设计原则:
- 纯业务逻辑，不依赖根目录模块（如 scm_repo）
- 使用 engram.logbook.scm_db 进行数据库操作
- 使用 engram.logbook.cursor 进行游标管理
"""

from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional

from engram.logbook.errors import ValidationError
from engram.logbook.config import get_svn_auth
from engram.logbook.cursor import load_svn_cursor, save_svn_cursor
from engram.logbook.scm_db import (
    get_conn as get_connection,
    upsert_svn_revision,
    upsert_repo,
)


# ============ 异常定义 ============


class SvnCommandError(Exception):
    """SVN 命令执行错误"""
    pass


class SvnTimeoutError(Exception):
    """SVN 命令超时"""
    pass


class SvnParseError(Exception):
    """SVN 输出解析错误"""
    pass


class PatchFetchError(Exception):
    """Patch 获取基础错误"""
    error_category = "error"


class PatchFetchTimeoutError(PatchFetchError):
    """Patch 获取超时"""
    error_category = "timeout"


class PatchFetchContentTooLargeError(PatchFetchError):
    """Patch 内容过大"""
    error_category = "content_too_large"


class PatchFetchCommandError(PatchFetchError):
    """Patch 命令执行错误"""
    error_category = "command_error"


# ============ 数据类定义 ============


@dataclass
class FetchDiffResult:
    """Diff 获取结果"""
    success: bool
    content: str = ""
    error: Optional[Exception] = None
    error_category: Optional[str] = None
    error_message: Optional[str] = None
    endpoint: Optional[str] = None


@dataclass
class SvnRevision:
    """SVN revision 数据类"""
    revision: int
    author: str
    date: Optional[datetime]
    message: str
    changed_paths: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class SyncConfig:
    """同步配置"""
    svn_url: str
    batch_size: int
    overlap: int = 0
    username: Optional[str] = None
    password: Optional[str] = None
    timeout: int = 120


# ============ 解析函数 ============


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """解析 ISO 日期时间字符串"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def parse_svn_log_xml(xml_content: str) -> List[SvnRevision]:
    """解析 SVN log XML 输出"""
    if not xml_content.strip():
        return []
    try:
        root = ET.fromstring(xml_content)
    except Exception as exc:
        raise SvnParseError(f"无法解析 SVN log XML: {exc}") from exc
    revisions: List[SvnRevision] = []
    for entry in root.findall("logentry"):
        revision = int(entry.get("revision", "0"))
        author = entry.findtext("author") or ""
        date = _parse_dt(entry.findtext("date"))
        message = entry.findtext("msg") or ""
        changed_paths: List[Dict[str, Any]] = []
        for path in entry.findall("./paths/path"):
            item: Dict[str, Any] = {
                "path": path.text or "",
                "action": path.get("action"),
                "kind": path.get("kind"),
            }
            copyfrom_path = path.get("copyfrom-path")
            copyfrom_rev = path.get("copyfrom-rev")
            if copyfrom_path:
                item["copyfrom_path"] = copyfrom_path
            if copyfrom_rev:
                try:
                    item["copyfrom_rev"] = int(copyfrom_rev)
                except Exception:
                    item["copyfrom_rev"] = copyfrom_rev
            changed_paths.append(item)
        revisions.append(
            SvnRevision(
                revision=revision,
                author=author,
                date=date,
                message=message,
                changed_paths=changed_paths,
            )
        )
    return revisions


# ============ SVN 命令函数 ============


def get_svn_head_revision(svn_url: str, *, timeout: int = 60) -> int:
    """获取 SVN HEAD revision"""
    cmd = ["svn", "info", "--xml", svn_url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
    except subprocess.TimeoutExpired as exc:
        raise SvnTimeoutError("svn info 超时") from exc
    except subprocess.CalledProcessError as exc:
        raise SvnCommandError("svn info 命令执行失败") from exc
    try:
        root = ET.fromstring(proc.stdout)
        entry = root.find("entry")
        if entry is None:
            raise SvnParseError("svn info 缺少 entry")
        rev = entry.get("revision")
        if not rev:
            raise SvnParseError("svn info 缺少 revision")
        return int(rev)
    except SvnParseError:
        raise
    except Exception as exc:
        raise SvnParseError(f"svn info 解析失败: {exc}") from exc


def fetch_svn_log_xml(
    svn_url: str,
    *,
    start_rev: int,
    end_rev: int,
    verbose: bool = False,
    timeout: int = 120,
) -> str:
    """获取 SVN log XML"""
    cmd = ["svn", "log", "--xml", "-r", f"{start_rev}:{end_rev}"]
    if verbose:
        cmd.append("-v")
    cmd.append(svn_url)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
    except subprocess.TimeoutExpired as exc:
        raise SvnTimeoutError("svn log 超时") from exc
    except subprocess.CalledProcessError as exc:
        raise SvnCommandError("svn log 命令执行失败") from exc
    return proc.stdout


def _mask_svn_command_for_log(cmd: List[str]) -> str:
    """掩码 SVN 命令中的密码"""
    masked: List[str] = []
    i = 0
    while i < len(cmd):
        part = cmd[i]
        if part == "--password" and i + 1 < len(cmd):
            masked.append(part)
            masked.append("****")
            i += 2
            continue
        if part.startswith("--password="):
            masked.append("--password=****")
            i += 1
            continue
        masked.append(part)
        i += 1
    return " ".join(masked)


def run_svn_cmd(
    cmd: List[str],
    *,
    config=None,
) -> Any:
    """运行 SVN 命令"""
    auth = get_svn_auth(config)
    final_cmd = list(cmd)
    if auth and auth.username:
        final_cmd += ["--username", auth.username]
    if auth and auth.password:
        final_cmd += ["--password", auth.password]

    if config is not None:
        if config.get("scm.svn.non_interactive", False):
            final_cmd.append("--non-interactive")
        if config.get("scm.svn.trust_server_cert", False):
            final_cmd.append("--trust-server-cert")
        timeout = config.get("scm.svn.command_timeout", 120)
    else:
        timeout = 120

    proc = subprocess.run(final_cmd, capture_output=True, text=True, timeout=timeout)
    return SimpleNamespace(
        success=proc.returncode == 0,
        stdout=proc.stdout,
        stderr=proc.stderr,
        returncode=proc.returncode,
        cmd=final_cmd,
        cmd_masked=_mask_svn_command_for_log(final_cmd),
    )


def fetch_svn_diff(
    svn_url: str,
    *,
    revision: int,
    timeout: int = 120,
    max_size_bytes: Optional[int] = None,
) -> FetchDiffResult:
    """获取 SVN diff"""
    cmd = ["svn", "diff", "-c", str(revision), svn_url]
    endpoint = f"svn diff -c {revision}"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
        content = proc.stdout or ""
        if max_size_bytes is not None and len(content.encode("utf-8")) > max_size_bytes:
            err = PatchFetchContentTooLargeError("内容过大")
            return FetchDiffResult(
                success=False,
                content="",
                error=err,
                error_category=err.error_category,
                error_message=str(err),
                endpoint=endpoint,
            )
        return FetchDiffResult(success=True, content=content, endpoint=endpoint)
    except subprocess.TimeoutExpired as exc:
        err = PatchFetchTimeoutError("timeout")
        return FetchDiffResult(
            success=False,
            error=err,
            error_category=err.error_category,
            error_message=str(err),
            endpoint=endpoint,
        )
    except subprocess.CalledProcessError as exc:
        err = PatchFetchCommandError("command_error")
        return FetchDiffResult(
            success=False,
            error=err,
            error_category=err.error_category,
            error_message=str(err),
            endpoint=endpoint,
        )


# ============ 辅助函数 ============


def generate_ministat_from_changed_paths(
    changed_paths: Iterable[Dict[str, Any]],
    *,
    revision: Optional[int] = None,
) -> str:
    """从 changed_paths 生成 ministat"""
    paths = list(changed_paths)
    if not paths:
        return ""
    added = sum(1 for p in paths if p.get("action") == "A")
    modified = sum(1 for p in paths if p.get("action") == "M")
    deleted = sum(1 for p in paths if p.get("action") == "D")
    replaced = sum(1 for p in paths if p.get("action") == "R")
    total = len(paths)
    header = "ministat"
    if revision is not None:
        header += f" for r{revision}"
    header += " (degraded)"
    lines = [header]
    lines.append(
        f"{total} path(s) changed, {modified} modified, {added} added, {deleted} deleted, {replaced} replaced"
    )
    for item in paths[:5]:
        path = item.get("path") or ""
        if len(path) > 80:
            path = path[:40] + "..." + path[-20:]
        suffix = " (dir)" if item.get("kind") == "dir" else ""
        lines.append(f"- {path}{suffix}")
    return "\n".join(lines)


def generate_diffstat(diff_content: str) -> str:
    """生成 diffstat"""
    if not diff_content or not diff_content.strip():
        return ""
    current_file: Optional[str] = None
    stats: Dict[str, Dict[str, int]] = {}
    for line in diff_content.splitlines():
        if line.startswith("Index: "):
            current_file = line[len("Index: ") :].strip()
            stats.setdefault(current_file, {"insertions": 0, "deletions": 0})
            continue
        if current_file is None:
            continue
        if line.startswith("+++ ") or line.startswith("--- ") or line.startswith("@@") or line.startswith("==="):
            continue
        if line.startswith("+"):
            stats[current_file]["insertions"] += 1
        elif line.startswith("-"):
            stats[current_file]["deletions"] += 1
    if not stats:
        return ""
    lines = []
    total_ins = sum(v["insertions"] for v in stats.values())
    total_del = sum(v["deletions"] for v in stats.values())
    for name, counts in stats.items():
        lines.append(
            f"{name} | {counts['insertions']} insertion(s)(+), {counts['deletions']} deletion(s)(-)"
        )
    lines.append(
        f"{len(stats)} file(s) changed, {total_ins} insertion(s)(+), {total_del} deletion(s)(-)"
    )
    return "\n".join(lines)


# ============ 数据库操作 ============


def ensure_repo(
    conn,
    repo_type: str,
    url: str,
    *,
    project_key: Optional[str] = None,
    default_branch: Optional[str] = None,
) -> int:
    """
    确保仓库存在（使用包内 API）

    此函数替代根目录 scm_repo.ensure_repo，使用 engram.logbook.scm_db.upsert_repo。
    """
    return upsert_repo(
        conn,
        repo_type=repo_type,
        url=url,
        project_key=project_key or "default",
        default_branch=default_branch,
    )


def insert_svn_revisions(
    conn,
    repo_id: int,
    revisions: List[SvnRevision],
    config: Optional[SyncConfig] = None,
) -> int:
    """插入 SVN revisions 到数据库"""
    count = 0
    for rev in revisions:
        upsert_svn_revision(
            conn,
            repo_id,
            rev.revision,
            author_raw=rev.author,
            ts=rev.date.isoformat() if rev.date else None,
            message=rev.message,
            source_id=str(rev.revision),
        )
        count += 1
    return count


def sync_patches_for_revisions(
    svn_url: str,
    revisions: List[SvnRevision],
    *,
    max_size_bytes: Optional[int] = None,
) -> Dict[str, Any]:
    """同步 revisions 的 patches"""
    total = len(revisions)
    success = 0
    failed = 0
    patches: List[Dict[str, Any]] = []
    for rev in revisions:
        result = fetch_svn_diff(svn_url, revision=rev.revision, max_size_bytes=max_size_bytes)
        if result.success:
            success += 1
        else:
            failed += 1
        patches.append(
            {
                "revision": rev.revision,
                "success": result.success,
                "error_category": result.error_category,
            }
        )
    return {
        "total": total,
        "success": success,
        "failed": failed,
        "skipped": 0,
        "bulk_count": 0,
        "patches": patches,
    }


# ============ 同步主函数 ============


def sync_svn_revisions(
    sync_config: SyncConfig,
    *,
    project_key: str,
    verbose: bool = False,
    dsn: Optional[str] = None,
) -> Dict[str, Any]:
    """
    增量同步 SVN revisions

    Args:
        sync_config: 同步配置
        project_key: 项目标识
        verbose: 是否详细输出
        dsn: 数据库连接字符串（可选）

    Returns:
        同步结果字典
    """
    import os
    
    # 获取或创建数据库连接
    dsn = dsn or os.environ.get("LOGBOOK_DSN") or os.environ.get("POSTGRES_DSN") or ""
    conn = get_connection(dsn)

    try:
        # 确保仓库存在
        repo_id = ensure_repo(
            conn,
            repo_type="svn",
            url=sync_config.svn_url,
            project_key=project_key,
        )
        conn.commit()

        cursor = load_svn_cursor(repo_id, config=None)
        last_rev = getattr(cursor, "last_rev", 0) or 0
        head_rev = get_svn_head_revision(sync_config.svn_url)
        if head_rev <= last_rev:
            return {"success": True, "synced_count": 0, "message": "无需同步"}

        start_rev = 1 if last_rev <= 0 else max(1, last_rev - sync_config.overlap + 1)
        xml_content = fetch_svn_log_xml(
            sync_config.svn_url,
            start_rev=start_rev,
            end_rev=head_rev,
            verbose=verbose,
            timeout=sync_config.timeout,
        )
        revisions = parse_svn_log_xml(xml_content)
        if not revisions:
            return {"success": True, "synced_count": 0, "message": "无需同步"}

        synced = insert_svn_revisions(conn, repo_id, revisions, sync_config)
        conn.commit()

        max_rev = max(r.revision for r in revisions)
        save_svn_cursor(repo_id, max_rev, synced, config=None)
        return {"success": True, "synced_count": synced, "last_rev": max_rev}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def backfill_svn_revisions(
    sync_config: SyncConfig,
    *,
    project_key: str,
    start_rev: int,
    end_rev: Optional[int] = None,
    update_watermark: bool = False,
    dry_run: bool = False,
    fetch_patches: bool = False,
    dsn: Optional[str] = None,
) -> Dict[str, Any]:
    """
    回填 SVN revisions

    Args:
        sync_config: 同步配置
        project_key: 项目标识
        start_rev: 起始 revision
        end_rev: 结束 revision（可选，默认为 HEAD）
        update_watermark: 是否更新游标
        dry_run: 是否模拟运行
        fetch_patches: 是否获取 patches
        dsn: 数据库连接字符串（可选）

    Returns:
        同步结果字典
    """
    import os
    
    if end_rev is not None and start_rev > end_rev:
        raise ValidationError("起始 revision 大于结束 revision", {})

    # 获取或创建数据库连接
    dsn = dsn or os.environ.get("LOGBOOK_DSN") or os.environ.get("POSTGRES_DSN") or ""
    conn = get_connection(dsn)

    try:
        # 确保仓库存在
        repo_id = ensure_repo(
            conn,
            repo_type="svn",
            url=sync_config.svn_url,
            project_key=project_key,
        )
        conn.commit()

        if end_rev is None:
            end_rev = get_svn_head_revision(sync_config.svn_url)
        xml_content = fetch_svn_log_xml(
            sync_config.svn_url,
            start_rev=start_rev,
            end_rev=end_rev,
            verbose=False,
            timeout=sync_config.timeout,
        )
        revisions = parse_svn_log_xml(xml_content)
        result: Dict[str, Any] = {
            "success": True,
            "synced_count": 0,
            "watermark_updated": False,
            "last_rev": end_rev,
            "dry_run": dry_run,
        }
        if dry_run:
            result["message"] = "dry-run"
            return result

        synced = insert_svn_revisions(conn, repo_id, revisions, sync_config)
        conn.commit()
        result["synced_count"] = synced

        if update_watermark and revisions:
            max_rev = max(r.revision for r in revisions)
            save_svn_cursor(repo_id, max_rev, synced, config=None)
            result["watermark_updated"] = True
            result["last_rev"] = max_rev

        if fetch_patches:
            result["patch_stats"] = sync_patches_for_revisions(
                sync_config.svn_url, revisions
            )

        return result
    finally:
        try:
            conn.close()
        except Exception:
            pass
