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

from db import get_conn

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
    获取同步状态摘要
    
    Args:
        conn: 数据库连接
        window_minutes: 统计窗口分钟数（用于 failed_rate/rate_limit_rate）
        top_lag_limit: 返回 lag 最大的仓库数量
    
    Returns:
        包含以下字段的摘要字典:
        - repos_count: 仓库总数
        - repos_by_type: 按类型统计仓库
        - jobs: pending/running/failed/dead 计数
        - expired_locks: 过期锁数量
        - window_stats: 最近窗口的 failed_rate/rate_limit_rate
        - by_instance: 每实例队列占用
        - by_tenant: 每租户队列占用
        - top_lag_repos: lag 最大的仓库列表
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
        
        # 最近窗口统计：failed_rate 和 rate_limit_rate
        cur.execute("""
            WITH recent_runs AS (
                SELECT 
                    run_id,
                    status,
                    counts
                FROM scm.sync_runs 
                WHERE started_at >= now() - interval '%s minutes'
            )
            SELECT
                COUNT(*) as total_runs,
                COUNT(*) FILTER (WHERE status = 'completed') as completed_runs,
                COUNT(*) FILTER (WHERE status = 'failed') as failed_runs,
                COALESCE(SUM((counts->>'total_429_hits')::int), 0) as total_429_hits,
                COALESCE(SUM((counts->>'total_requests')::int), 0) as total_requests
            FROM recent_runs
        """, (window_minutes,))
        window_row = cur.fetchone()
        
        total_runs = window_row[0] or 0
        completed_runs = window_row[1] or 0
        failed_runs = window_row[2] or 0
        total_429_hits = window_row[3] or 0
        total_requests = window_row[4] or 0
        
        total_finished = completed_runs + failed_runs
        failed_rate = round(failed_runs / total_finished, 4) if total_finished > 0 else 0.0
        rate_limit_rate = round(total_429_hits / total_requests, 4) if total_requests > 0 else 0.0
        
        summary["window_stats"] = {
            "window_minutes": window_minutes,
            "total_runs": total_runs,
            "completed_runs": completed_runs,
            "failed_runs": failed_runs,
            "failed_rate": failed_rate,
            "total_429_hits": total_429_hits,
            "total_requests": total_requests,
            "rate_limit_rate": rate_limit_rate,
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
            
            # 解析 gitlab_instance（仅 git 类型）
            gitlab_instance = None
            if repo_type == "git" and url and "://" in url:
                try:
                    from urllib.parse import urlparse
                    parsed = urlparse(url)
                    gitlab_instance = parsed.netloc
                except Exception:
                    pass
            
            # 解析 tenant_id
            tenant_id = None
            if project_key and "/" in project_key:
                tenant_id = project_key.split("/")[0]
            
            # 按实例计数
            if gitlab_instance:
                by_instance[gitlab_instance] = by_instance.get(gitlab_instance, 0) + 1
            
            # 按租户计数
            if tenant_id:
                by_tenant[tenant_id] = by_tenant.get(tenant_id, 0) + 1
        
        summary["by_instance"] = by_instance
        summary["by_tenant"] = by_tenant
        
        # Top N lag 最大仓库（按最后同步时间排序，越久未同步的 lag 越大）
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
                "url": url,
                "project_key": project_key,
                "job_type": job_type,
                "last_status": last_status,
                "last_finished_at": format_datetime(last_finished_at),
                "lag_seconds": int(lag_seconds) if lag_seconds else None,
            })
        
        summary["top_lag_repos"] = top_lag_repos
    
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
