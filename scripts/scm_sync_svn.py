#!/usr/bin/env python3
"""
scm_sync_svn - SVN 同步兼容实现（测试用）

注意: 此文件已移动到 scripts/ 目录。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from types import SimpleNamespace

# 确保根目录在 sys.path 中，以支持导入根目录模块
_ROOT_DIR = Path(__file__).resolve().parent.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

from engram.logbook.errors import ValidationError
from engram.logbook.config import get_svn_auth
from engram.logbook.cursor import load_svn_cursor, save_svn_cursor
from db import get_conn as get_connection
from db import upsert_svn_revision
from scm_repo import ensure_repo


class SvnCommandError(Exception):
    pass


class SvnTimeoutError(Exception):
    pass


class SvnParseError(Exception):
    pass


class PatchFetchError(Exception):
    error_category = "error"


class PatchFetchTimeoutError(PatchFetchError):
    error_category = "timeout"


class PatchFetchContentTooLargeError(PatchFetchError):
    error_category = "content_too_large"


class PatchFetchCommandError(PatchFetchError):
    error_category = "command_error"


@dataclass
class FetchDiffResult:
    success: bool
    content: str = ""
    error: Optional[Exception] = None
    error_category: Optional[str] = None
    error_message: Optional[str] = None
    endpoint: Optional[str] = None


@dataclass
class SvnRevision:
    revision: int
    author: str
    date: Optional[datetime]
    message: str
    changed_paths: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class SyncConfig:
    svn_url: str
    batch_size: int
    overlap: int = 0
    username: Optional[str] = None
    password: Optional[str] = None
    timeout: int = 120


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def parse_svn_log_xml(xml_content: str) -> List[SvnRevision]:
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


def get_svn_head_revision(svn_url: str, *, timeout: int = 60) -> int:
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


def generate_ministat_from_changed_paths(
    changed_paths: Iterable[Dict[str, Any]],
    *,
    revision: Optional[int] = None,
) -> str:
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


def insert_svn_revisions(conn, repo_id: int, revisions: List[SvnRevision], config: Optional[SyncConfig] = None) -> int:
    count = 0
    for rev in revisions:
        upsert_svn_revision(
            conn,
            repo_id,
            rev.revision,
            author=rev.author,
            message=rev.message,
            committed_at=rev.date.isoformat() if rev.date else None,
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


def sync_svn_revisions(
    sync_config: SyncConfig,
    *,
    project_key: str,
    verbose: bool = False,
) -> Dict[str, Any]:
    repo_id = ensure_repo(
        None,
        repo_type="svn",
        url=sync_config.svn_url,
        project_key=project_key,
    )
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

    conn = get_connection("")
    try:
        synced = insert_svn_revisions(conn, repo_id, revisions, sync_config)
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    max_rev = max(r.revision for r in revisions)
    save_svn_cursor(repo_id, max_rev, synced, config=None)
    return {"success": True, "synced_count": synced, "last_rev": max_rev}


def backfill_svn_revisions(
    sync_config: SyncConfig,
    *,
    project_key: str,
    start_rev: int,
    end_rev: Optional[int] = None,
    update_watermark: bool = False,
    dry_run: bool = False,
    fetch_patches: bool = False,
) -> Dict[str, Any]:
    if end_rev is not None and start_rev > end_rev:
        raise ValidationError("起始 revision 大于结束 revision", {})
    repo_id = ensure_repo(
        None,
        repo_type="svn",
        url=sync_config.svn_url,
        project_key=project_key,
    )
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

    conn = get_connection("")
    try:
        synced = insert_svn_revisions(conn, repo_id, revisions, sync_config)
        conn.commit()
        result["synced_count"] = synced
    finally:
        try:
            conn.close()
        except Exception:
            pass

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


def parse_args(argv: Optional[List[str]] = None):
    parser = argparse.ArgumentParser(description="scm_sync_svn")
    parser.add_argument("--repo", dest="repo", default=None)
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--start-rev", type=int)
    parser.add_argument("--end-rev", type=int)
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--update-watermark", action="store_true")
    return parser.parse_args(argv)
