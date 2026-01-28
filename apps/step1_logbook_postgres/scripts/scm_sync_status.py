#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scm_sync_status.py - SCM 同步状态只读查询 CLI

用于运维排障的只读查询工具，支持查询：
- scm.repos: 仓库列表
- logbook.kv: 同步游标
- scm.sync_runs: 同步运行历史
- scm.sync_jobs: 同步任务队列
- scm.sync_locks: 分布式锁状态

输出格式: JSON / 表格（默认 JSON）

使用示例:
    # 查询所有仓库
    python scm_sync_status.py repos
    python scm_sync_status.py repos --format table
    
    # 查询同步游标
    python scm_sync_status.py cursors
    python scm_sync_status.py cursors --repo-id 1
    python scm_sync_status.py cursors --namespace scm.sync
    
    # 查询同步运行记录
    python scm_sync_status.py runs
    python scm_sync_status.py runs --repo-id 1 --limit 10
    python scm_sync_status.py runs --status failed
    
    # 查询同步任务队列
    python scm_sync_status.py jobs
    python scm_sync_status.py jobs --status pending
    
    # 查询锁状态
    python scm_sync_status.py locks
    python scm_sync_status.py locks --expired
    python scm_sync_status.py locks --worker-id worker-001
"""

import json
import os
import sys
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import typer
from psycopg.rows import dict_row

# 添加当前目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import (
    get_conn, 
    load_circuit_breaker_state, 
    build_circuit_breaker_key,
    list_paused_repo_jobs,
    PauseReasonCode,
)

# 尝试导入 key 规范化模块，若失败则使用内联实现（支持独立运行）
try:
    from engram_step1.scm_sync_keys import normalize_instance_key, extract_tenant_id
except ImportError:
    # 内联回退实现
    from urllib.parse import urlparse as _urlparse
    
    def normalize_instance_key(url_or_host):
        if not url_or_host:
            return None
        value = url_or_host.strip()
        if not value:
            return None
        if "://" in value:
            try:
                parsed = _urlparse(value)
                host = parsed.netloc or parsed.hostname or ""
            except Exception:
                host = value.split("://", 1)[-1].split("/", 1)[0]
        else:
            host = value.split("/", 1)[0]
        host = host.lower()
        if host.endswith(":80"):
            host = host[:-3]
        elif host.endswith(":443"):
            host = host[:-4]
        return host if host else None
    
    def extract_tenant_id(payload_json=None, project_key=None):
        if payload_json and isinstance(payload_json, dict):
            tenant_id = payload_json.get("tenant_id")
            if tenant_id and isinstance(tenant_id, str) and tenant_id.strip():
                return tenant_id.strip()
        if project_key and isinstance(project_key, str):
            project_key = project_key.strip()
            if "/" in project_key:
                tenant_part = project_key.split("/", 1)[0].strip()
                if tenant_part:
                    return tenant_part
        return None

# 尝试导入 redact 脱敏函数，若失败则使用内联实现
try:
    from engram_step1.scm_auth import redact, redact_dict
except ImportError:
    import re as _re
    
    # 内联回退实现
    _SENSITIVE_PATTERNS = [
        (_re.compile(r'\b(glp[a-z]{1,2}-[A-Za-z0-9_-]{10,})\b'), '[GITLAB_TOKEN]'),
        (_re.compile(r'(Bearer\s+)[A-Za-z0-9_.\-=]+', _re.IGNORECASE), r'\1[TOKEN]'),
        (_re.compile(r'(Authorization[:\s]+)(\S+\s+)?(\S+)', _re.IGNORECASE), r'\1[REDACTED]'),
        (_re.compile(r'(PRIVATE-TOKEN[:\s]+)[^\s,;]+', _re.IGNORECASE), r'\1[REDACTED]'),
        (_re.compile(r'(password[=:\s]+)[^\s&;,]+', _re.IGNORECASE), r'\1[REDACTED]'),
        (_re.compile(r'(token[=:\s]+)[^\s&;,]+', _re.IGNORECASE), r'\1[REDACTED]'),
        (_re.compile(r'(://[^:]+:)[^@]+(@)'), r'\1[REDACTED]\2'),
    ]
    _SENSITIVE_HEADERS = {
        'authorization', 'private-token', 'x-private-token',
        'x-gitlab-token', 'cookie', 'set-cookie',
    }
    
    def redact(text):
        if not text:
            return ""
        result = str(text)
        for pattern, replacement in _SENSITIVE_PATTERNS:
            result = pattern.sub(replacement, result)
        return result
    
    def redact_dict(data, sensitive_keys=None, deep=True):
        if not data:
            return {}
        all_sensitive_keys = _SENSITIVE_HEADERS.copy()
        if sensitive_keys:
            all_sensitive_keys.update(k.lower() for k in sensitive_keys)
        result = {}
        for key, value in data.items():
            key_lower = key.lower() if isinstance(key, str) else str(key).lower()
            if key_lower in all_sensitive_keys:
                result[key] = "[REDACTED]"
            elif isinstance(value, dict) and deep:
                result[key] = redact_dict(value, sensitive_keys, deep)
            elif isinstance(value, list) and deep:
                result[key] = [
                    redact_dict(item, sensitive_keys, deep) if isinstance(item, dict)
                    else redact(item) if isinstance(item, str)
                    else item
                    for item in value
                ]
            elif isinstance(value, str):
                result[key] = redact(value)
            else:
                result[key] = value
        return result

# ============ CLI 应用定义 ============

app = typer.Typer(
    name="scm-sync-status",
    help="SCM 同步状态只读查询工具（用于运维排障）",
    no_args_is_help=True,
)


class OutputFormat(str, Enum):
    """输出格式"""
    json = "json"
    table = "table"
    prometheus = "prometheus"


# ============ 工具函数 ============


def get_connection():
    """获取数据库连接"""
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        print(json.dumps({
            "ok": False,
            "code": "NO_DSN",
            "message": "POSTGRES_DSN 环境变量未设置",
        }))
        raise typer.Exit(1)
    return get_conn(dsn)


def output_json(data: Dict[str, Any], pretty: bool = False) -> None:
    """输出 JSON 格式结果"""
    indent = 2 if pretty else None
    print(json.dumps(data, ensure_ascii=False, indent=indent, default=str))


def make_ok_result(data: Any = None, **kwargs) -> Dict[str, Any]:
    """构造成功结果 (ok: true)"""
    result = {"ok": True}
    if data is not None:
        result["data"] = data
    result.update(kwargs)
    return result


def make_err_result(code: str, message: str, detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """构造错误结果 (ok: false)"""
    return {"ok": False, "code": code, "message": message, "detail": detail or {}}


def format_datetime(dt: Optional[datetime]) -> Optional[str]:
    """格式化 datetime 为 ISO 字符串"""
    if dt is None:
        return None
    return dt.isoformat()


def print_table(headers: List[str], rows: List[List[Any]], max_width: int = 40) -> None:
    """打印表格格式输出"""
    if not rows:
        print("(无数据)")
        return
    
    # 计算列宽
    col_widths = [len(str(h)) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            cell_str = str(cell) if cell is not None else ""
            # 截断过长的内容
            if len(cell_str) > max_width:
                cell_str = cell_str[:max_width - 3] + "..."
            col_widths[i] = max(col_widths[i], len(cell_str))
    
    # 打印表头
    header_line = " | ".join(str(h).ljust(col_widths[i]) for i, h in enumerate(headers))
    print(header_line)
    print("-" * len(header_line))
    
    # 打印数据行
    for row in rows:
        cells = []
        for i, cell in enumerate(row):
            cell_str = str(cell) if cell is not None else ""
            if len(cell_str) > max_width:
                cell_str = cell_str[:max_width - 3] + "..."
            cells.append(cell_str.ljust(col_widths[i]))
        print(" | ".join(cells))


# ============ 查询函数 ============


def query_repos(
    conn,
    repo_type: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """查询 scm.repos 表"""
    params: List[Any] = []
    where_clauses = []
    
    if repo_type:
        where_clauses.append("repo_type = %s")
        params.append(repo_type)
    
    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)
    
    params.append(limit)
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"""
            SELECT repo_id, repo_type, url, project_key, default_branch, created_at
            FROM scm.repos
            {where_sql}
            ORDER BY repo_id
            LIMIT %s
        """, params)
        rows = cur.fetchall()
        return [dict(row) for row in rows]


def query_kv_cursors(
    conn,
    namespace: str = "scm.sync",
    key_prefix: Optional[str] = None,
    repo_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """查询 logbook.kv 中的同步游标"""
    params: List[Any] = [namespace]
    where_clauses = ["namespace = %s"]
    
    if key_prefix:
        where_clauses.append("key LIKE %s")
        params.append(f"{key_prefix}%")
    elif repo_id is not None:
        # 按 repo_id 过滤，匹配 key 格式如 gitlab_cursor:1, svn_cursor:1
        where_clauses.append("key LIKE %s")
        params.append(f"%:{repo_id}")
    
    where_sql = "WHERE " + " AND ".join(where_clauses)
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"""
            SELECT namespace, key, value_json, updated_at
            FROM logbook.kv
            {where_sql}
            ORDER BY key
        """, params)
        rows = cur.fetchall()
        return [dict(row) for row in rows]


def query_sync_runs(
    conn,
    repo_id: Optional[int] = None,
    job_type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """查询 scm.sync_runs 表"""
    params: List[Any] = []
    where_clauses = []
    
    if repo_id is not None:
        where_clauses.append("repo_id = %s")
        params.append(repo_id)
    
    if job_type:
        where_clauses.append("job_type = %s")
        params.append(job_type)
    
    if status:
        where_clauses.append("status = %s")
        params.append(status)
    
    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)
    
    params.append(limit)
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"""
            SELECT 
                run_id, repo_id, job_type, mode, status,
                started_at, finished_at,
                cursor_before, cursor_after,
                counts, error_summary_json, degradation_json,
                logbook_item_id, meta_json, synced_count
            FROM scm.sync_runs
            {where_sql}
            ORDER BY started_at DESC
            LIMIT %s
        """, params)
        rows = cur.fetchall()
        return [dict(row) for row in rows]


def query_sync_jobs(
    conn,
    repo_id: Optional[int] = None,
    job_type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """查询 scm.sync_jobs 表"""
    params: List[Any] = []
    where_clauses = []
    
    if repo_id is not None:
        where_clauses.append("repo_id = %s")
        params.append(repo_id)
    
    if job_type:
        where_clauses.append("job_type = %s")
        params.append(job_type)
    
    if status:
        where_clauses.append("status = %s")
        params.append(status)
    
    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)
    
    params.append(limit)
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"""
            SELECT 
                job_id, repo_id, job_type, mode, status,
                priority, attempts, max_attempts,
                not_before, locked_by, locked_at, lease_seconds,
                last_error, last_run_id,
                payload_json, created_at, updated_at
            FROM scm.sync_jobs
            {where_sql}
            ORDER BY priority ASC, created_at ASC
            LIMIT %s
        """, params)
        rows = cur.fetchall()
        return [dict(row) for row in rows]


def query_sync_locks(
    conn,
    repo_id: Optional[int] = None,
    job_type: Optional[str] = None,
    worker_id: Optional[str] = None,
    expired_only: bool = False,
    active_only: bool = False,
) -> List[Dict[str, Any]]:
    """查询 scm.sync_locks 表"""
    params: List[Any] = []
    where_clauses = []
    
    if repo_id is not None:
        where_clauses.append("repo_id = %s")
        params.append(repo_id)
    
    if job_type:
        where_clauses.append("job_type = %s")
        params.append(job_type)
    
    if worker_id:
        where_clauses.append("locked_by = %s")
        params.append(worker_id)
    
    if expired_only:
        where_clauses.append("""
            locked_by IS NOT NULL 
            AND locked_at + (lease_seconds || ' seconds')::interval < now()
        """)
    
    if active_only:
        where_clauses.append("locked_by IS NOT NULL")
    
    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"""
            SELECT 
                lock_id, repo_id, job_type,
                locked_by, locked_at, lease_seconds,
                updated_at, created_at,
                locked_by IS NOT NULL AS is_locked,
                CASE 
                    WHEN locked_at IS NULL THEN false
                    ELSE locked_at + (lease_seconds || ' seconds')::interval < now()
                END AS is_expired
            FROM scm.sync_locks
            {where_sql}
            ORDER BY repo_id, job_type
        """, params)
        rows = cur.fetchall()
        return [dict(row) for row in rows]


def get_sync_summary(
    conn, 
    window_minutes: int = 60,
    top_lag_limit: int = 10,
) -> Dict[str, Any]:
    """
    获取同步状态摘要（固定 schema）
    
    Args:
        conn: 数据库连接
        window_minutes: 统计窗口分钟数（用于 error_budget 计算）
        top_lag_limit: 返回 lag 最大的仓库数量
    
    Returns:
        包含以下固定字段的摘要字典（所有输出已脱敏）:
        - circuit_breakers: 熔断器状态（按 scope 聚合）
        - rate_limit_buckets: 限流桶状态（含 pause_until/source）
        - error_budget: 错误预算统计（failure/429/timeout + samples）
        - pauses_by_reason: 按原因聚合的暂停统计
        - repos_count: 仓库总数
        - repos_by_type: 按类型统计仓库
        - jobs: pending/running/failed/dead 计数
        - expired_locks: 过期锁数量
        - by_instance: 每实例队列占用
        - by_tenant: 每租户队列占用
        - top_lag_repos: lag 最大的仓库列表（已脱敏）
    """
    summary: Dict[str, Any] = {}
    
    with conn.cursor() as cur:
        # 仓库总数
        cur.execute("SELECT COUNT(*) FROM scm.repos")
        summary["repos_count"] = cur.fetchone()[0]
        
        # 按类型统计仓库
        cur.execute("""
            SELECT repo_type, COUNT(*) 
            FROM scm.repos 
            GROUP BY repo_type
        """)
        summary["repos_by_type"] = {row[0]: row[1] for row in cur.fetchall()}
        
        # 同步运行统计（最近 24 小时）
        cur.execute("""
            SELECT status, COUNT(*) 
            FROM scm.sync_runs 
            WHERE started_at >= now() - interval '24 hours'
            GROUP BY status
        """)
        summary["runs_24h_by_status"] = {row[0]: row[1] for row in cur.fetchall()}
        
        # 同步任务队列统计 - 标准化字段
        cur.execute("""
            SELECT status, COUNT(*) 
            FROM scm.sync_jobs 
            GROUP BY status
        """)
        jobs_by_status = {row[0]: row[1] for row in cur.fetchall()}
        summary["jobs_by_status"] = jobs_by_status
        
        # 稳定字段：pending/running/failed/dead
        summary["jobs"] = {
            "pending": jobs_by_status.get("pending", 0),
            "running": jobs_by_status.get("running", 0),
            "failed": jobs_by_status.get("failed", 0),
            "dead": jobs_by_status.get("dead", 0),
        }
        
        # 锁状态统计 - expired_locks 作为顶级字段
        cur.execute("""
            SELECT 
                COUNT(*) FILTER (WHERE locked_by IS NOT NULL) AS active_locks,
                COUNT(*) FILTER (
                    WHERE locked_by IS NOT NULL 
                    AND locked_at + (lease_seconds || ' seconds')::interval < now()
                ) AS expired_locks
            FROM scm.sync_locks
        """)
        row = cur.fetchone()
        summary["locks"] = {
            "active": row[0] or 0,
            "expired": row[1] or 0,
        }
        summary["expired_locks"] = row[1] or 0  # 顶级稳定字段
        
        # 游标数量
        cur.execute("""
            SELECT COUNT(*) FROM logbook.kv 
            WHERE namespace = 'scm.sync'
        """)
        summary["cursors_count"] = cur.fetchone()[0]
        
        # ============ error_budget: 错误预算统计 ============
        # 统计 failure/429/timeout 及样本数
        cur.execute("""
            WITH recent_runs AS (
                SELECT 
                    run_id,
                    status,
                    counts,
                    error_summary_json
                FROM scm.sync_runs 
                WHERE started_at >= now() - interval '%s minutes'
            )
            SELECT
                COUNT(*) as total_runs,
                COUNT(*) FILTER (WHERE status = 'completed') as completed_runs,
                COUNT(*) FILTER (WHERE status = 'failed') as failed_runs,
                COALESCE(SUM((counts->>'total_429_hits')::int), 0) as total_429_hits,
                COALESCE(SUM((counts->>'total_requests')::int), 0) as total_requests,
                COALESCE(SUM((counts->>'timeout_count')::int), 0) as timeout_count
            FROM recent_runs
        """, (window_minutes,))
        window_row = cur.fetchone()
        
        total_runs = window_row[0] or 0
        completed_runs = window_row[1] or 0
        failed_runs = window_row[2] or 0
        total_429_hits = window_row[3] or 0
        total_requests = window_row[4] or 0
        timeout_count = window_row[5] or 0
        
        total_finished = completed_runs + failed_runs
        failure_rate = round(failed_runs / total_finished, 4) if total_finished > 0 else 0.0
        rate_429 = round(total_429_hits / total_requests, 4) if total_requests > 0 else 0.0
        timeout_rate = round(timeout_count / total_requests, 4) if total_requests > 0 else 0.0
        
        # 固定 schema: error_budget
        summary["error_budget"] = {
            "window_minutes": window_minutes,
            "samples": total_runs,
            "failure": {
                "count": failed_runs,
                "rate": failure_rate,
            },
            "rate_limit_429": {
                "count": total_429_hits,
                "rate": rate_429,
            },
            "timeout": {
                "count": timeout_count,
                "rate": timeout_rate,
            },
            "total_requests": total_requests,
            "completed_runs": completed_runs,
        }
        
        # 保留旧字段以兼容（标记为 deprecated）
        summary["window_stats"] = {
            "window_minutes": window_minutes,
            "total_runs": total_runs,
            "completed_runs": completed_runs,
            "failed_runs": failed_runs,
            "failed_rate": failure_rate,
            "total_429_hits": total_429_hits,
            "total_requests": total_requests,
            "rate_limit_rate": rate_429,
        }
        
        # 每实例/租户队列占用
        cur.execute("""
            SELECT 
                j.status,
                r.url,
                r.project_key,
                r.repo_type
            FROM scm.sync_jobs j
            JOIN scm.repos r ON j.repo_id = r.repo_id
            WHERE j.status IN ('pending', 'running')
        """)
        
        by_instance: Dict[str, int] = {}
        by_tenant: Dict[str, int] = {}
        
        for row in cur.fetchall():
            status, url, project_key, repo_type = row
            
            # 解析 gitlab_instance（仅 git 类型，使用统一的 key 规范化）
            gitlab_instance = None
            if repo_type == "git" and url:
                gitlab_instance = normalize_instance_key(url)
            
            # 解析 tenant_id（使用统一的提取函数）
            tenant_id = extract_tenant_id(project_key=project_key)
            
            # 按实例计数
            if gitlab_instance:
                by_instance[gitlab_instance] = by_instance.get(gitlab_instance, 0) + 1
            
            # 按租户计数
            if tenant_id:
                by_tenant[tenant_id] = by_tenant.get(tenant_id, 0) + 1
        
        summary["by_instance"] = by_instance
        summary["by_tenant"] = by_tenant
        
        # Top N lag 最大仓库（按最后同步时间排序，越久未同步的 lag 越大）
        # 注意：URL 和 project_key 会被脱敏处理
        cur.execute("""
            WITH latest_runs AS (
                SELECT DISTINCT ON (repo_id, job_type)
                    repo_id,
                    job_type,
                    status,
                    finished_at,
                    started_at
                FROM scm.sync_runs
                ORDER BY repo_id, job_type, started_at DESC
            ),
            repo_lag AS (
                SELECT 
                    r.repo_id,
                    r.repo_type,
                    r.url,
                    r.project_key,
                    lr.job_type,
                    lr.status as last_status,
                    lr.finished_at as last_finished_at,
                    EXTRACT(EPOCH FROM (now() - COALESCE(lr.finished_at, lr.started_at))) as lag_seconds
                FROM scm.repos r
                LEFT JOIN latest_runs lr ON r.repo_id = lr.repo_id
            )
            SELECT 
                repo_id,
                repo_type,
                url,
                project_key,
                job_type,
                last_status,
                last_finished_at,
                lag_seconds
            FROM repo_lag
            WHERE lag_seconds IS NOT NULL
            ORDER BY lag_seconds DESC NULLS LAST
            LIMIT %s
        """, (top_lag_limit,))
        
        top_lag_repos = []
        for row in cur.fetchall():
            repo_id, repo_type, url, project_key, job_type, last_status, last_finished_at, lag_seconds = row
            top_lag_repos.append({
                "repo_id": repo_id,
                "repo_type": repo_type,
                "url": redact(url) if url else None,  # 脱敏 URL
                "project_key": redact(project_key) if project_key else None,  # 脱敏 project_key
                "job_type": job_type,
                "last_status": last_status,
                "last_finished_at": format_datetime(last_finished_at),
                "lag_seconds": int(lag_seconds) if lag_seconds else None,
            })
        
        summary["top_lag_repos"] = top_lag_repos
        
        # ============ circuit_breakers: 熔断器状态（按 scope 聚合） ============
        cur.execute("""
            SELECT key, value_json, updated_at
            FROM logbook.kv
            WHERE namespace = 'scm.sync_health'
            ORDER BY key
        """)
        
        circuit_breakers_by_scope: Dict[str, List[Dict[str, Any]]] = {}
        circuit_breaker_states = []  # 保留旧格式以兼容
        
        for row in cur.fetchall():
            key, value_json, updated_at = row
            if isinstance(value_json, str):
                import json as json_mod
                value_json = json_mod.loads(value_json)
            
            # 解析 scope（从 key 格式 "cb:scope:instance" 提取）
            # 例如: "cb:global:gitlab.com" -> scope="global"
            parts = key.split(":", 2) if key else []
            scope = parts[1] if len(parts) >= 2 else "unknown"
            
            cb_entry = {
                "key": key,
                "scope": scope,
                "state": value_json.get("state", "unknown"),
                "failure_count": value_json.get("failure_count", 0),
                "success_count": value_json.get("success_count", 0),
                "last_failure_time": value_json.get("last_failure_time"),
                "updated_at": format_datetime(updated_at),
            }
            
            # 按 scope 聚合
            if scope not in circuit_breakers_by_scope:
                circuit_breakers_by_scope[scope] = []
            circuit_breakers_by_scope[scope].append(cb_entry)
            
            # 保留旧格式
            circuit_breaker_states.append({
                "key": key,
                "state": value_json,
                "updated_at": format_datetime(updated_at),
            })
        
        # 固定 schema: circuit_breakers（按 scope 聚合统计）
        summary["circuit_breakers"] = {
            "by_scope": {
                scope: {
                    "count": len(entries),
                    "open_count": sum(1 for e in entries if e["state"] == "open"),
                    "half_open_count": sum(1 for e in entries if e["state"] == "half_open"),
                    "closed_count": sum(1 for e in entries if e["state"] == "closed"),
                    "total_failures": sum(e["failure_count"] for e in entries),
                    "entries": entries,
                }
                for scope, entries in circuit_breakers_by_scope.items()
            },
            "total_count": len(circuit_breaker_states),
            "total_open": sum(1 for cb in circuit_breaker_states if cb["state"].get("state") == "open"),
        }
        summary["circuit_breaker_states"] = circuit_breaker_states  # 旧字段，兼容
        
        # ============ rate_limit_buckets: 限流桶状态 ============
        cur.execute("""
            SELECT instance_key, tokens, updated_at, rate, burst,
                   paused_until, meta_json
            FROM scm.sync_rate_limits
            ORDER BY instance_key
        """)
        
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rate_limit_buckets = []
        token_bucket_states = []  # 旧格式，兼容
        
        for row in cur.fetchall():
            instance_key, tokens, updated_at, rate, burst, paused_until, meta_json = row
            
            # 计算当前实际令牌数
            if updated_at.tzinfo is not None:
                updated_at_naive = updated_at.replace(tzinfo=None)
            else:
                updated_at_naive = updated_at
            
            elapsed = (now - updated_at_naive).total_seconds()
            current_tokens = min(burst, tokens + elapsed * rate)
            
            # 计算等待时间和暂停状态
            is_paused = False
            pause_remaining_seconds = 0.0
            wait_seconds = 0.0
            pause_source = None
            
            if paused_until:
                if paused_until.tzinfo is not None:
                    paused_until_naive = paused_until.replace(tzinfo=None)
                else:
                    paused_until_naive = paused_until
                
                if now < paused_until_naive:
                    is_paused = True
                    pause_remaining_seconds = (paused_until_naive - now).total_seconds()
                    wait_seconds = pause_remaining_seconds
                    # 从 meta_json 提取暂停原因（source）
                    if meta_json and isinstance(meta_json, dict):
                        pause_source = meta_json.get("pause_source") or meta_json.get("source")
            
            # 如果没有被暂停且令牌不足，计算需要等待的时间
            if not is_paused and current_tokens < 1:
                tokens_needed = 1 - current_tokens
                wait_seconds = tokens_needed / rate if rate > 0 else 0
            
            # 固定 schema: rate_limit_buckets
            rate_limit_buckets.append({
                "instance_key": instance_key,
                "tokens_remaining": round(current_tokens, 3),
                "pause_until": format_datetime(paused_until) if paused_until else None,
                "source": pause_source,  # 暂停原因来源
                "is_paused": is_paused,
                "pause_remaining_seconds": round(pause_remaining_seconds, 3),
                "wait_seconds": round(wait_seconds, 3),
                "rate": rate,
                "burst": burst,
            })
            
            # 旧格式，兼容
            token_bucket_states.append({
                "instance_key": instance_key,
                "tokens_remaining": round(current_tokens, 3),
                "paused_until": format_datetime(paused_until) if paused_until else None,
                "wait_seconds": round(wait_seconds, 3),
                "is_paused": is_paused,
                "pause_remaining_seconds": round(pause_remaining_seconds, 3),
                "rate": rate,
                "burst": burst,
                "meta_json": redact_dict(meta_json) if meta_json else None,  # 脱敏 meta
            })
        
        summary["rate_limit_buckets"] = rate_limit_buckets
        summary["token_bucket_states"] = token_bucket_states  # 旧字段，兼容
        
        # ============ pauses_by_reason: 按原因聚合暂停统计 ============
        paused_records = list_paused_repo_jobs(conn, include_expired=False)
        
        # 按 reason_code 聚合
        pauses_by_reason: Dict[str, int] = {}
        for record in paused_records:
            reason_code = record.reason_code or "unknown"
            pauses_by_reason[reason_code] = pauses_by_reason.get(reason_code, 0) + 1
        
        # 固定 schema: pauses_by_reason
        summary["pauses_by_reason"] = pauses_by_reason
        summary["paused_repos_count"] = len(paused_records)
        summary["paused_by_reason"] = pauses_by_reason  # 旧字段，兼容
        
        # 暂停记录详情（仅包含非敏感信息，已脱敏）
        paused_details = []
        for record in paused_records[:50]:  # 限制最多 50 条
            paused_details.append({
                "repo_id": record.repo_id,
                "job_type": record.job_type,
                "reason_code": record.reason_code or "unknown",
                "remaining_seconds": int(record.remaining_seconds()),
            })
        summary["paused_details"] = paused_details
    
    return summary


def format_prometheus_metrics(summary: Dict[str, Any]) -> str:
    """
    将 summary 格式化为 Prometheus 文本格式
    
    不引入额外依赖，手动生成 Prometheus text format。
    
    Args:
        summary: get_sync_summary 返回的摘要字典
    
    Returns:
        Prometheus text format 字符串
    """
    lines = []
    
    # 帮助函数：生成带标签的指标行
    def metric_line(name: str, value: Any, labels: Optional[Dict[str, str]] = None, help_text: str = "", metric_type: str = "gauge") -> List[str]:
        result = []
        if help_text:
            result.append(f"# HELP {name} {help_text}")
            result.append(f"# TYPE {name} {metric_type}")
        
        if labels:
            label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())
            result.append(f"{name}{{{label_str}}} {value}")
        else:
            result.append(f"{name} {value}")
        return result
    
    # scm_repos_total
    lines.extend(metric_line(
        "scm_repos_total",
        summary.get("repos_count", 0),
        help_text="Total number of repositories",
    ))
    
    # scm_repos_by_type
    for repo_type, count in summary.get("repos_by_type", {}).items():
        if not lines[-1].startswith("# HELP scm_repos_by_type"):
            lines.append("# HELP scm_repos_by_type Number of repositories by type")
            lines.append("# TYPE scm_repos_by_type gauge")
        lines.append(f'scm_repos_by_type{{repo_type="{repo_type}"}} {count}')
    
    # scm_jobs_total - 稳定字段
    jobs = summary.get("jobs", {})
    lines.append("# HELP scm_jobs_total Number of sync jobs by status")
    lines.append("# TYPE scm_jobs_total gauge")
    for status in ["pending", "running", "failed", "dead"]:
        count = jobs.get(status, 0)
        lines.append(f'scm_jobs_total{{status="{status}"}} {count}')
    
    # scm_expired_locks - 顶级稳定字段
    lines.extend(metric_line(
        "scm_expired_locks",
        summary.get("expired_locks", 0),
        help_text="Number of expired locks",
    ))
    
    # scm_locks_total
    locks = summary.get("locks", {})
    lines.append("# HELP scm_locks_total Number of locks by state")
    lines.append("# TYPE scm_locks_total gauge")
    lines.append(f'scm_locks_total{{state="active"}} {locks.get("active", 0)}')
    lines.append(f'scm_locks_total{{state="expired"}} {locks.get("expired", 0)}')
    
    # scm_cursors_total
    lines.extend(metric_line(
        "scm_cursors_total",
        summary.get("cursors_count", 0),
        help_text="Total number of sync cursors",
    ))
    
    # scm_runs_24h_by_status
    runs_24h = summary.get("runs_24h_by_status", {})
    if runs_24h:
        lines.append("# HELP scm_runs_24h_total Number of sync runs in last 24 hours by status")
        lines.append("# TYPE scm_runs_24h_total gauge")
        for status, count in runs_24h.items():
            lines.append(f'scm_runs_24h_total{{status="{status}"}} {count}')
    
    # Window stats - 最近窗口统计
    window_stats = summary.get("window_stats", {})
    window_minutes = window_stats.get("window_minutes", 60)
    
    # scm_window_failed_rate
    lines.extend(metric_line(
        "scm_window_failed_rate",
        window_stats.get("failed_rate", 0),
        labels={"window_minutes": str(window_minutes)},
        help_text="Failed rate in recent window",
    ))
    
    # scm_window_rate_limit_rate
    lines.extend(metric_line(
        "scm_window_rate_limit_rate",
        window_stats.get("rate_limit_rate", 0),
        labels={"window_minutes": str(window_minutes)},
        help_text="Rate limit (429) rate in recent window",
    ))
    
    # scm_window_runs_total
    lines.extend(metric_line(
        "scm_window_runs_total",
        window_stats.get("total_runs", 0),
        labels={"window_minutes": str(window_minutes)},
        help_text="Total sync runs in recent window",
    ))
    
    # scm_window_429_hits_total
    lines.extend(metric_line(
        "scm_window_429_hits_total",
        window_stats.get("total_429_hits", 0),
        labels={"window_minutes": str(window_minutes)},
        help_text="Total 429 hits in recent window",
    ))
    
    # scm_window_requests_total
    lines.extend(metric_line(
        "scm_window_requests_total",
        window_stats.get("total_requests", 0),
        labels={"window_minutes": str(window_minutes)},
        help_text="Total requests in recent window",
    ))
    
    # scm_queue_by_instance - 每实例队列占用
    by_instance = summary.get("by_instance", {})
    if by_instance:
        lines.append("# HELP scm_queue_by_instance Active jobs by GitLab instance")
        lines.append("# TYPE scm_queue_by_instance gauge")
        for instance, count in by_instance.items():
            lines.append(f'scm_queue_by_instance{{instance="{instance}"}} {count}')
    
    # scm_queue_by_tenant - 每租户队列占用
    by_tenant = summary.get("by_tenant", {})
    if by_tenant:
        lines.append("# HELP scm_queue_by_tenant Active jobs by tenant")
        lines.append("# TYPE scm_queue_by_tenant gauge")
        for tenant, count in by_tenant.items():
            lines.append(f'scm_queue_by_tenant{{tenant="{tenant}"}} {count}')
    
    # scm_repo_lag_seconds - Top N lag 最大仓库
    top_lag_repos = summary.get("top_lag_repos", [])
    if top_lag_repos:
        lines.append("# HELP scm_repo_lag_seconds Sync lag in seconds for top lagging repositories")
        lines.append("# TYPE scm_repo_lag_seconds gauge")
        for repo in top_lag_repos:
            lag = repo.get("lag_seconds")
            if lag is not None:
                repo_id = repo.get("repo_id", "")
                repo_type = repo.get("repo_type", "")
                job_type = repo.get("job_type", "") or ""
                lines.append(f'scm_repo_lag_seconds{{repo_id="{repo_id}",repo_type="{repo_type}",job_type="{job_type}"}} {lag}')
    
    # ============ error_budget 指标 ============
    error_budget = summary.get("error_budget", {})
    if error_budget:
        window_min = error_budget.get("window_minutes", 60)
        
        lines.extend(metric_line(
            "scm_error_budget_samples",
            error_budget.get("samples", 0),
            labels={"window_minutes": str(window_min)},
            help_text="Total samples in error budget window",
        ))
        
        failure = error_budget.get("failure", {})
        lines.extend(metric_line(
            "scm_error_budget_failure_count",
            failure.get("count", 0),
            labels={"window_minutes": str(window_min)},
            help_text="Failure count in error budget window",
        ))
        lines.extend(metric_line(
            "scm_error_budget_failure_rate",
            failure.get("rate", 0),
            labels={"window_minutes": str(window_min)},
            help_text="Failure rate in error budget window",
        ))
        
        rate_429 = error_budget.get("rate_limit_429", {})
        lines.extend(metric_line(
            "scm_error_budget_429_count",
            rate_429.get("count", 0),
            labels={"window_minutes": str(window_min)},
            help_text="429 rate limit hits in error budget window",
        ))
        lines.extend(metric_line(
            "scm_error_budget_429_rate",
            rate_429.get("rate", 0),
            labels={"window_minutes": str(window_min)},
            help_text="429 rate in error budget window",
        ))
        
        timeout = error_budget.get("timeout", {})
        lines.extend(metric_line(
            "scm_error_budget_timeout_count",
            timeout.get("count", 0),
            labels={"window_minutes": str(window_min)},
            help_text="Timeout count in error budget window",
        ))
        lines.extend(metric_line(
            "scm_error_budget_timeout_rate",
            timeout.get("rate", 0),
            labels={"window_minutes": str(window_min)},
            help_text="Timeout rate in error budget window",
        ))
    
    # ============ circuit_breakers 指标（按 scope 聚合） ============
    circuit_breakers = summary.get("circuit_breakers", {})
    by_scope = circuit_breakers.get("by_scope", {})
    if by_scope:
        lines.append("# HELP scm_circuit_breakers_by_scope Circuit breaker counts by scope")
        lines.append("# TYPE scm_circuit_breakers_by_scope gauge")
        for scope, data in by_scope.items():
            lines.append(f'scm_circuit_breakers_by_scope{{scope="{scope}",state="open"}} {data.get("open_count", 0)}')
            lines.append(f'scm_circuit_breakers_by_scope{{scope="{scope}",state="half_open"}} {data.get("half_open_count", 0)}')
            lines.append(f'scm_circuit_breakers_by_scope{{scope="{scope}",state="closed"}} {data.get("closed_count", 0)}')
        
        lines.append("# HELP scm_circuit_breakers_total_failures Total failures by scope")
        lines.append("# TYPE scm_circuit_breakers_total_failures gauge")
        for scope, data in by_scope.items():
            lines.append(f'scm_circuit_breakers_total_failures{{scope="{scope}"}} {data.get("total_failures", 0)}')
    
    # 兼容旧指标格式
    circuit_breaker_states = summary.get("circuit_breaker_states", [])
    if circuit_breaker_states:
        lines.append("# HELP scm_circuit_breaker_state Circuit breaker state (0=closed, 1=open, 2=half_open)")
        lines.append("# TYPE scm_circuit_breaker_state gauge")
        lines.append("# HELP scm_circuit_breaker_failure_count Circuit breaker failure count")
        lines.append("# TYPE scm_circuit_breaker_failure_count gauge")
        lines.append("# HELP scm_circuit_breaker_success_count Circuit breaker success count")
        lines.append("# TYPE scm_circuit_breaker_success_count gauge")
        
        state_map = {"closed": 0, "open": 1, "half_open": 2}
        
        for cb in circuit_breaker_states:
            key = cb.get("key", "")
            state = cb.get("state", {})
            cb_state = state.get("state", "closed")
            failure_count = state.get("failure_count", 0)
            success_count = state.get("success_count", 0)
            
            state_value = state_map.get(cb_state, 0)
            lines.append(f'scm_circuit_breaker_state{{key="{key}"}} {state_value}')
            lines.append(f'scm_circuit_breaker_failure_count{{key="{key}"}} {failure_count}')
            lines.append(f'scm_circuit_breaker_success_count{{key="{key}"}} {success_count}')
    
    # ============ rate_limit_buckets 指标 ============
    rate_limit_buckets = summary.get("rate_limit_buckets", [])
    if rate_limit_buckets:
        lines.append("# HELP scm_rate_limit_bucket_tokens Remaining tokens in the rate limiter bucket")
        lines.append("# TYPE scm_rate_limit_bucket_tokens gauge")
        lines.append("# HELP scm_rate_limit_bucket_paused Whether the bucket is paused (0=no, 1=yes)")
        lines.append("# TYPE scm_rate_limit_bucket_paused gauge")
        lines.append("# HELP scm_rate_limit_bucket_pause_seconds Remaining pause time in seconds")
        lines.append("# TYPE scm_rate_limit_bucket_pause_seconds gauge")
        
        for bucket in rate_limit_buckets:
            instance_key = bucket.get("instance_key", "")
            tokens_remaining = bucket.get("tokens_remaining", 0)
            is_paused = 1 if bucket.get("is_paused", False) else 0
            pause_remaining = bucket.get("pause_remaining_seconds", 0)
            source = bucket.get("source") or ""
            
            lines.append(f'scm_rate_limit_bucket_tokens{{instance_key="{instance_key}"}} {tokens_remaining}')
            lines.append(f'scm_rate_limit_bucket_paused{{instance_key="{instance_key}",source="{source}"}} {is_paused}')
            lines.append(f'scm_rate_limit_bucket_pause_seconds{{instance_key="{instance_key}"}} {pause_remaining}')
    
    # 兼容旧指标格式
    token_bucket_states = summary.get("token_bucket_states", [])
    if token_bucket_states:
        lines.append("# HELP scm_token_bucket_tokens_remaining Remaining tokens in the rate limiter bucket")
        lines.append("# TYPE scm_token_bucket_tokens_remaining gauge")
        lines.append("# HELP scm_token_bucket_wait_seconds Seconds to wait before next request is allowed")
        lines.append("# TYPE scm_token_bucket_wait_seconds gauge")
        lines.append("# HELP scm_token_bucket_is_paused Whether the token bucket is paused (0=no, 1=yes)")
        lines.append("# TYPE scm_token_bucket_is_paused gauge")
        lines.append("# HELP scm_token_bucket_pause_remaining_seconds Remaining pause time in seconds")
        lines.append("# TYPE scm_token_bucket_pause_remaining_seconds gauge")
        
        for tb in token_bucket_states:
            instance_key = tb.get("instance_key", "")
            tokens_remaining = tb.get("tokens_remaining", 0)
            wait_seconds = tb.get("wait_seconds", 0)
            is_paused = 1 if tb.get("is_paused", False) else 0
            pause_remaining = tb.get("pause_remaining_seconds", 0)
            
            lines.append(f'scm_token_bucket_tokens_remaining{{instance_key="{instance_key}"}} {tokens_remaining}')
            lines.append(f'scm_token_bucket_wait_seconds{{instance_key="{instance_key}"}} {wait_seconds}')
            lines.append(f'scm_token_bucket_is_paused{{instance_key="{instance_key}"}} {is_paused}')
            lines.append(f'scm_token_bucket_pause_remaining_seconds{{instance_key="{instance_key}"}} {pause_remaining}')
    
    # ============ pauses_by_reason 指标 ============
    paused_repos_count = summary.get("paused_repos_count", 0)
    lines.extend(metric_line(
        "scm_paused_repos_total",
        paused_repos_count,
        help_text="Total number of paused repo/job_type pairs",
    ))
    
    # 使用新字段名 pauses_by_reason（同时兼容旧字段名）
    pauses_by_reason = summary.get("pauses_by_reason") or summary.get("paused_by_reason", {})
    if pauses_by_reason:
        lines.append("# HELP scm_pauses_by_reason Number of paused repo/job_type by reason code")
        lines.append("# TYPE scm_pauses_by_reason gauge")
        for reason_code, count in pauses_by_reason.items():
            lines.append(f'scm_pauses_by_reason{{reason_code="{reason_code}"}} {count}')
        
        # 兼容旧指标名
        lines.append("# HELP scm_paused_by_reason Number of paused repo/job_type by reason code (deprecated)")
        lines.append("# TYPE scm_paused_by_reason gauge")
        for reason_code, count in pauses_by_reason.items():
            lines.append(f'scm_paused_by_reason{{reason_code="{reason_code}"}} {count}')
    
    return "\n".join(lines) + "\n"


# ============ CLI 命令 ============


@app.command("summary")
def cmd_summary(
    format: OutputFormat = typer.Option(
        OutputFormat.json, "--format", "-f",
        help="输出格式 (json/table/prometheus)"
    ),
    pretty: bool = typer.Option(
        False, "--pretty", "-p",
        help="美化 JSON 输出"
    ),
    window_minutes: int = typer.Option(
        60, "--window", "-w",
        help="统计窗口分钟数（用于 failed_rate/rate_limit_rate）"
    ),
    top_lag_limit: int = typer.Option(
        10, "--top-lag", "-t",
        help="返回 lag 最大的仓库数量"
    ),
):
    """
    显示同步状态摘要
    
    输出稳定字段：
    - jobs.pending/running/failed/dead: 任务队列状态
    - expired_locks: 过期锁数量
    - window_stats.failed_rate/rate_limit_rate: 最近窗口失败率和限流率
    - by_instance/by_tenant: 每实例/租户队列占用
    - top_lag_repos: lag 最大的仓库列表
    
    使用 --format prometheus 输出 Prometheus 文本格式指标
    """
    try:
        conn = get_connection()
        try:
            summary = get_sync_summary(
                conn, 
                window_minutes=window_minutes,
                top_lag_limit=top_lag_limit,
            )
            
            if format == OutputFormat.prometheus:
                # 输出 Prometheus text format
                print(format_prometheus_metrics(summary), end="")
            elif format == OutputFormat.table:
                print("=== SCM 同步状态摘要 ===\n")
                print(f"仓库总数: {summary['repos_count']}")
                print(f"  - 按类型: {summary['repos_by_type']}")
                
                print(f"\n任务队列状态:")
                jobs = summary.get('jobs', {})
                print(f"  - pending: {jobs.get('pending', 0)}")
                print(f"  - running: {jobs.get('running', 0)}")
                print(f"  - failed: {jobs.get('failed', 0)}")
                print(f"  - dead: {jobs.get('dead', 0)}")
                
                print(f"\n锁状态:")
                print(f"  - 活跃锁: {summary['locks']['active']}")
                print(f"  - 过期锁: {summary['expired_locks']}")
                
                print(f"\n最近 {window_minutes} 分钟窗口统计:")
                window = summary.get('window_stats', {})
                print(f"  - 总运行次数: {window.get('total_runs', 0)}")
                print(f"  - 失败率: {window.get('failed_rate', 0):.2%}")
                print(f"  - 限流率 (429): {window.get('rate_limit_rate', 0):.2%}")
                print(f"  - 总请求数: {window.get('total_requests', 0)}")
                print(f"  - 429 命中: {window.get('total_429_hits', 0)}")
                
                by_instance = summary.get('by_instance', {})
                if by_instance:
                    print(f"\n每实例队列占用:")
                    for instance, count in sorted(by_instance.items(), key=lambda x: -x[1]):
                        print(f"  - {instance}: {count}")
                
                by_tenant = summary.get('by_tenant', {})
                if by_tenant:
                    print(f"\n每租户队列占用:")
                    for tenant, count in sorted(by_tenant.items(), key=lambda x: -x[1]):
                        print(f"  - {tenant}: {count}")
                
                top_lag = summary.get('top_lag_repos', [])
                if top_lag:
                    print(f"\nTop {len(top_lag)} Lag 最大仓库:")
                    for repo in top_lag:
                        lag_s = repo.get('lag_seconds')
                        if lag_s is not None:
                            lag_str = f"{lag_s // 3600}h {(lag_s % 3600) // 60}m" if lag_s >= 3600 else f"{lag_s // 60}m {lag_s % 60}s"
                        else:
                            lag_str = "N/A"
                        print(f"  - repo_id={repo['repo_id']}, type={repo['repo_type']}, job={repo.get('job_type', '-')}, lag={lag_str}")
                
                print(f"\n游标数量: {summary['cursors_count']}")
                
                print(f"\n最近 24 小时同步运行:")
                for status, count in summary.get('runs_24h_by_status', {}).items():
                    print(f"  - {status}: {count}")
                
                # 熔断器状态
                circuit_breaker_states = summary.get('circuit_breaker_states', [])
                if circuit_breaker_states:
                    print(f"\n熔断器状态:")
                    for cb in circuit_breaker_states:
                        key = cb.get('key', '')
                        state = cb.get('state', {})
                        cb_state = state.get('state', 'unknown')
                        failure_count = state.get('failure_count', 0)
                        success_count = state.get('success_count', 0)
                        print(f"  - {key}: state={cb_state}, failures={failure_count}, successes={success_count}")
                
                # 令牌桶状态
                token_bucket_states = summary.get('token_bucket_states', [])
                if token_bucket_states:
                    print(f"\n令牌桶状态:")
                    for tb in token_bucket_states:
                        instance_key = tb.get('instance_key', '')
                        tokens = tb.get('tokens_remaining', 0)
                        is_paused = tb.get('is_paused', False)
                        wait_s = tb.get('wait_seconds', 0)
                        pause_s = tb.get('pause_remaining_seconds', 0)
                        status_str = f"PAUSED({pause_s:.1f}s)" if is_paused else f"OK"
                        print(f"  - {instance_key}: tokens={tokens:.1f}, status={status_str}, wait={wait_s:.1f}s")
                
                # 暂停统计（按 reason_code 聚合）
                paused_count = summary.get('paused_repos_count', 0)
                paused_by_reason = summary.get('paused_by_reason', {})
                if paused_count > 0 or paused_by_reason:
                    print(f"\n暂停统计 (按原因聚合):")
                    print(f"  - 总暂停数: {paused_count}")
                    for reason_code, count in sorted(paused_by_reason.items(), key=lambda x: -x[1]):
                        print(f"  - {reason_code}: {count}")
            else:
                output_json(make_ok_result(data=summary), pretty=pretty)
        finally:
            conn.close()
    except Exception as e:
        output_json(make_err_result(
            code="QUERY_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__}
        ))
        raise typer.Exit(1)


@app.command("repos")
def cmd_repos(
    repo_type: Optional[str] = typer.Option(
        None, "--type", "-t",
        help="仓库类型过滤 (git/svn)"
    ),
    limit: int = typer.Option(
        100, "--limit", "-l",
        help="最大返回数量"
    ),
    format: OutputFormat = typer.Option(
        OutputFormat.json, "--format", "-f",
        help="输出格式 (json/table)"
    ),
    pretty: bool = typer.Option(
        False, "--pretty", "-p",
        help="美化 JSON 输出"
    ),
):
    """
    查询仓库列表 (scm.repos)
    """
    try:
        conn = get_connection()
        try:
            rows = query_repos(conn, repo_type=repo_type, limit=limit)
            
            if format == OutputFormat.table:
                headers = ["repo_id", "type", "url", "project_key", "default_branch", "created_at"]
                table_rows = [
                    [r["repo_id"], r["repo_type"], r["url"], r["project_key"], 
                     r["default_branch"], format_datetime(r["created_at"])]
                    for r in rows
                ]
                print_table(headers, table_rows)
                print(f"\n共 {len(rows)} 条记录")
            else:
                output_json(make_ok_result(data=rows, count=len(rows)), pretty=pretty)
        finally:
            conn.close()
    except Exception as e:
        output_json(make_err_result(
            code="QUERY_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__}
        ))
        raise typer.Exit(1)


@app.command("cursors")
def cmd_cursors(
    namespace: str = typer.Option(
        "scm.sync", "--namespace", "-n",
        help="KV 命名空间"
    ),
    repo_id: Optional[int] = typer.Option(
        None, "--repo-id", "-r",
        help="按仓库 ID 过滤"
    ),
    key_prefix: Optional[str] = typer.Option(
        None, "--prefix",
        help="键名前缀过滤"
    ),
    format: OutputFormat = typer.Option(
        OutputFormat.json, "--format", "-f",
        help="输出格式 (json/table)"
    ),
    pretty: bool = typer.Option(
        False, "--pretty", "-p",
        help="美化 JSON 输出"
    ),
):
    """
    查询同步游标 (logbook.kv)
    """
    try:
        conn = get_connection()
        try:
            rows = query_kv_cursors(
                conn, 
                namespace=namespace, 
                key_prefix=key_prefix,
                repo_id=repo_id,
            )
            
            if format == OutputFormat.table:
                headers = ["namespace", "key", "value_json", "updated_at"]
                table_rows = [
                    [r["namespace"], r["key"], 
                     json.dumps(r["value_json"], ensure_ascii=False)[:60],
                     format_datetime(r["updated_at"])]
                    for r in rows
                ]
                print_table(headers, table_rows, max_width=60)
                print(f"\n共 {len(rows)} 条记录")
            else:
                output_json(make_ok_result(data=rows, count=len(rows)), pretty=pretty)
        finally:
            conn.close()
    except Exception as e:
        output_json(make_err_result(
            code="QUERY_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__}
        ))
        raise typer.Exit(1)


@app.command("runs")
def cmd_runs(
    repo_id: Optional[int] = typer.Option(
        None, "--repo-id", "-r",
        help="按仓库 ID 过滤"
    ),
    job_type: Optional[str] = typer.Option(
        None, "--job-type", "-j",
        help="按任务类型过滤 (gitlab_commits/gitlab_mrs/gitlab_reviews/svn)"
    ),
    status: Optional[str] = typer.Option(
        None, "--status", "-s",
        help="按状态过滤 (running/completed/failed/no_data)"
    ),
    limit: int = typer.Option(
        50, "--limit", "-l",
        help="最大返回数量"
    ),
    format: OutputFormat = typer.Option(
        OutputFormat.json, "--format", "-f",
        help="输出格式 (json/table)"
    ),
    pretty: bool = typer.Option(
        False, "--pretty", "-p",
        help="美化 JSON 输出"
    ),
):
    """
    查询同步运行记录 (scm.sync_runs)
    """
    try:
        conn = get_connection()
        try:
            rows = query_sync_runs(
                conn, 
                repo_id=repo_id, 
                job_type=job_type,
                status=status,
                limit=limit,
            )
            
            if format == OutputFormat.table:
                headers = ["run_id", "repo_id", "job_type", "status", "synced", "started_at", "finished_at"]
                table_rows = [
                    [str(r["run_id"])[:8], r["repo_id"], r["job_type"], r["status"],
                     r.get("synced_count", "-"),
                     format_datetime(r["started_at"]), format_datetime(r["finished_at"])]
                    for r in rows
                ]
                print_table(headers, table_rows)
                print(f"\n共 {len(rows)} 条记录")
            else:
                output_json(make_ok_result(data=rows, count=len(rows)), pretty=pretty)
        finally:
            conn.close()
    except Exception as e:
        output_json(make_err_result(
            code="QUERY_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__}
        ))
        raise typer.Exit(1)


@app.command("jobs")
def cmd_jobs(
    repo_id: Optional[int] = typer.Option(
        None, "--repo-id", "-r",
        help="按仓库 ID 过滤"
    ),
    job_type: Optional[str] = typer.Option(
        None, "--job-type", "-j",
        help="按任务类型过滤"
    ),
    status: Optional[str] = typer.Option(
        None, "--status", "-s",
        help="按状态过滤 (pending/running/completed/failed/dead)"
    ),
    limit: int = typer.Option(
        100, "--limit", "-l",
        help="最大返回数量"
    ),
    format: OutputFormat = typer.Option(
        OutputFormat.json, "--format", "-f",
        help="输出格式 (json/table)"
    ),
    pretty: bool = typer.Option(
        False, "--pretty", "-p",
        help="美化 JSON 输出"
    ),
):
    """
    查询同步任务队列 (scm.sync_jobs)
    """
    try:
        conn = get_connection()
        try:
            rows = query_sync_jobs(
                conn, 
                repo_id=repo_id, 
                job_type=job_type,
                status=status,
                limit=limit,
            )
            
            if format == OutputFormat.table:
                headers = ["job_id", "repo_id", "job_type", "status", "priority", "attempts", "locked_by", "created_at"]
                table_rows = [
                    [str(r["job_id"])[:8], r["repo_id"], r["job_type"], r["status"],
                     r["priority"], f"{r['attempts']}/{r['max_attempts']}",
                     r["locked_by"] or "-", format_datetime(r["created_at"])]
                    for r in rows
                ]
                print_table(headers, table_rows)
                print(f"\n共 {len(rows)} 条记录")
            else:
                output_json(make_ok_result(data=rows, count=len(rows)), pretty=pretty)
        finally:
            conn.close()
    except Exception as e:
        output_json(make_err_result(
            code="QUERY_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__}
        ))
        raise typer.Exit(1)


@app.command("locks")
def cmd_locks(
    repo_id: Optional[int] = typer.Option(
        None, "--repo-id", "-r",
        help="按仓库 ID 过滤"
    ),
    job_type: Optional[str] = typer.Option(
        None, "--job-type", "-j",
        help="按任务类型过滤"
    ),
    worker_id: Optional[str] = typer.Option(
        None, "--worker-id", "-w",
        help="按 worker 标识过滤"
    ),
    expired: bool = typer.Option(
        False, "--expired", "-e",
        help="仅显示过期的锁"
    ),
    active: bool = typer.Option(
        False, "--active", "-a",
        help="仅显示活跃的锁"
    ),
    format: OutputFormat = typer.Option(
        OutputFormat.json, "--format", "-f",
        help="输出格式 (json/table)"
    ),
    pretty: bool = typer.Option(
        False, "--pretty", "-p",
        help="美化 JSON 输出"
    ),
):
    """
    查询锁状态 (scm.sync_locks)
    """
    try:
        conn = get_connection()
        try:
            rows = query_sync_locks(
                conn, 
                repo_id=repo_id, 
                job_type=job_type,
                worker_id=worker_id,
                expired_only=expired,
                active_only=active,
            )
            
            if format == OutputFormat.table:
                headers = ["lock_id", "repo_id", "job_type", "locked_by", "locked_at", "lease_s", "expired"]
                table_rows = [
                    [r["lock_id"], r["repo_id"], r["job_type"],
                     r["locked_by"] or "-", format_datetime(r["locked_at"]),
                     r["lease_seconds"], "YES" if r["is_expired"] else "NO"]
                    for r in rows
                ]
                print_table(headers, table_rows)
                print(f"\n共 {len(rows)} 条记录")
            else:
                output_json(make_ok_result(data=rows, count=len(rows)), pretty=pretty)
        finally:
            conn.close()
    except Exception as e:
        output_json(make_err_result(
            code="QUERY_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__}
        ))
        raise typer.Exit(1)


# ============ 主入口 ============


def main():
    """主入口"""
    app()


if __name__ == "__main__":
    main()
