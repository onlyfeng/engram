# -*- coding: utf-8 -*-
"""
scm_sync_status - SCM 同步状态摘要核心实现

功能:
- 从数据库聚合同步状态摘要
- 计算 error_budget
- 聚合熔断器状态
- 输出 Prometheus 指标格式

设计原则:
- 纯业务逻辑，不包含 argparse/打印
- CLI 入口在根目录 scm_sync_status.py

使用示例:
    from engram.logbook.scm_sync_status import (
        get_sync_summary,
        format_prometheus_metrics,
    )

    with db.get_connection() as conn:
        summary = get_sync_summary(conn)
        metrics = format_prometheus_metrics(summary)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List
from urllib.parse import urlparse

from psycopg.rows import dict_row


class InvariantSeverity(str, Enum):
    """不变量违规严重程度"""

    CRITICAL = "critical"  # 需要立即处理
    WARNING = "warning"  # 需要关注
    INFO = "info"  # 信息性提示


@dataclass
class InvariantViolation:
    """
    不变量违规记录

    表示检测到的一个健康检查违规项。
    """

    check_id: str  # 检查项 ID
    name: str  # 检查项名称
    severity: InvariantSeverity  # 严重程度
    count: int  # 违规数量
    description: str  # 描述
    remediation_hint: str  # 修复建议
    details: List[Dict[str, Any]] = field(default_factory=list)  # 详细记录

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check_id": self.check_id,
            "name": self.name,
            "severity": self.severity.value,
            "count": self.count,
            "description": self.description,
            "remediation_hint": self.remediation_hint,
            "details": self.details,
        }


@dataclass
class HealthCheckResult:
    """
    健康检查结果

    包含所有检查项的结果和整体健康状态。
    """

    healthy: bool  # 是否健康（无 critical 违规）
    violations: List[InvariantViolation] = field(default_factory=list)
    checked_at: float = field(default_factory=time.time)
    total_checks: int = 0
    passed_checks: int = 0
    failed_checks: int = 0

    @property
    def exit_code(self) -> int:
        """
        返回 CLI 退出码

        - 0: 健康（无违规）
        - 1: 有 warning 级别违规
        - 2: 有 critical 级别违规
        """
        if not self.violations:
            return 0

        has_critical = any(v.severity == InvariantSeverity.CRITICAL for v in self.violations)
        if has_critical:
            return 2

        has_warning = any(v.severity == InvariantSeverity.WARNING for v in self.violations)
        if has_warning:
            return 1

        return 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "healthy": self.healthy,
            "exit_code": self.exit_code,
            "checked_at": self.checked_at,
            "total_checks": self.total_checks,
            "passed_checks": self.passed_checks,
            "failed_checks": self.failed_checks,
            "violations": [v.to_dict() for v in self.violations],
        }


__all__ = [
    "get_sync_summary",
    "format_prometheus_metrics",
    "check_invariants",
    "format_health_check_output",
    "HealthCheckResult",
    "InvariantViolation",
    "InvariantSeverity",
    # 内部函数（供测试使用）
    "_load_error_budget",
    "_load_circuit_breakers",
    "_parse_circuit_breaker_key",
    "_aggregate_circuit_breakers",
    "_load_rate_limit_buckets",
    "_legacy_token_buckets",
    "_load_pauses",
    "_default_error_budget",
]


# ============ error_budget 聚合（从 scm.sync_runs 读取） ============


def _load_error_budget(conn, *, window_minutes: int = 60, db_api=None) -> Dict[str, Any]:
    """
    从 scm.sync_runs 表聚合 error_budget 统计

    读取 counts / error_summary_json / request_stats 字段，计算:
    - failure count/rate
    - rate_limit_429 count/rate
    - timeout count/rate

    Args:
        conn: 数据库连接
        window_minutes: 统计窗口（分钟）
        db_api: 数据库 API 模块（用于测试注入）

    Returns:
        error_budget 字典，结构与 _default_error_budget() 一致
    """
    if db_api is None:
        from engram.logbook import scm_db as db_api

    # 使用 db.get_sync_runs_health_stats 获取健康统计
    health_stats = db_api.get_sync_runs_health_stats(
        conn,
        window_minutes=window_minutes,
        window_count=100,  # 最多统计最近 100 次运行
    )

    total_runs = health_stats.get("total_runs", 0)
    failed_count = health_stats.get("failed_count", 0)
    completed_count = health_stats.get("completed_count", 0)
    total_requests = health_stats.get("total_requests", 0)
    total_429_hits = health_stats.get("total_429_hits", 0)
    total_timeout_count = health_stats.get("total_timeout_count", 0)

    # 计算各项比率
    failure_rate = failed_count / total_runs if total_runs > 0 else 0.0
    rate_limit_rate = total_429_hits / total_requests if total_requests > 0 else 0.0
    timeout_rate = total_timeout_count / total_requests if total_requests > 0 else 0.0

    return {
        "window_minutes": window_minutes,
        "samples": total_runs,
        "total_requests": total_requests,
        "completed_runs": completed_count,
        "failure": {
            "count": failed_count,
            "rate": failure_rate,
        },
        "rate_limit_429": {
            "count": total_429_hits,
            "rate": rate_limit_rate,
        },
        "timeout": {
            "count": total_timeout_count,
            "rate": timeout_rate,
        },
    }


def _load_circuit_breakers(conn) -> List[Dict[str, Any]]:
    """加载熔断器状态"""
    rows: List[Dict[str, Any]] = []
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT key, value_json FROM logbook.kv WHERE namespace = %s",
            ("scm.sync_health",),
        )
        for row in cur.fetchall():
            state = row["value_json"] or {}
            if isinstance(state, str):
                state = json.loads(state)
            rows.append(
                {
                    "key": row["key"],
                    "state": state,
                }
            )
    return rows


def _parse_circuit_breaker_key(key: str) -> Dict[str, str]:
    """
    解析熔断器 key，提取 project_key 和 scope

    key 格式: <project_key>:<scope>
    scope 可以是:
    - 'global'
    - 'instance:<instance_key>'
    - 'tenant:<tenant_id>'
    - 'pool:<pool_name>'

    Args:
        key: 熔断器 key 字符串

    Returns:
        包含 project_key, scope, scope_type, scope_value 的字典
    """
    parts = key.split(":", 1)
    project_key = parts[0] if parts else "default"
    scope = parts[1] if len(parts) > 1 else "global"

    # 进一步解析 scope 类型
    scope_type = "global"
    scope_value = ""

    if scope == "global":
        scope_type = "global"
    elif scope.startswith("instance:"):
        scope_type = "instance"
        scope_value = scope[9:]  # 去掉 'instance:' 前缀
    elif scope.startswith("tenant:"):
        scope_type = "tenant"
        scope_value = scope[7:]  # 去掉 'tenant:' 前缀
    elif scope.startswith("pool:"):
        scope_type = "pool"
        scope_value = scope[5:]  # 去掉 'pool:' 前缀
    else:
        # 向后兼容：旧格式可能没有前缀
        scope_type = "unknown"
        scope_value = scope

    return {
        "project_key": project_key,
        "scope": scope,
        "scope_type": scope_type,
        "scope_value": scope_value,
    }


def _aggregate_circuit_breakers(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    按 scope 聚合熔断器状态

    使用 build_circuit_breaker_key 规范解析 key，提取 scope_type 进行分组统计。
    """
    by_scope: Dict[str, Any] = {}
    total_open = 0
    total_half_open = 0

    for entry in entries:
        key = entry["key"]
        parsed = _parse_circuit_breaker_key(key)
        parsed["scope"]
        scope_type = parsed["scope_type"]

        state = entry.get("state") or {}
        state_value = state.get("state", "closed")

        if state_value == "open":
            total_open += 1
        elif state_value == "half_open":
            total_half_open += 1

        # 按 scope_type 分组（global/instance/tenant/pool）
        scope_data = by_scope.setdefault(
            scope_type,
            {
                "count": 0,
                "open_count": 0,
                "half_open_count": 0,
                "closed_count": 0,
                "total_failures": 0,
                "entries": [],
                # 向后兼容旧字段名
                "open": 0,
                "failure_count": 0,
                "success_count": 0,
            },
        )
        scope_data["count"] += 1

        if state_value == "open":
            scope_data["open_count"] += 1
            scope_data["open"] += 1  # 向后兼容
        elif state_value == "half_open":
            scope_data["half_open_count"] += 1
        else:
            scope_data["closed_count"] += 1

        failure_count = int(state.get("failure_count", 0) or 0)
        success_count = int(state.get("success_count", 0) or 0)
        scope_data["total_failures"] += failure_count
        scope_data["failure_count"] += failure_count  # 向后兼容
        scope_data["success_count"] += success_count  # 向后兼容

        # 添加解析后的 scope 信息到 entry
        enriched_entry = {
            **entry,
            "parsed_key": parsed,
        }
        scope_data["entries"].append(enriched_entry)

    return {
        "by_scope": by_scope,
        "total_count": len(entries),
        "total_open": total_open,
        "total_half_open": total_half_open,
    }


def _load_rate_limit_buckets(conn) -> List[Dict[str, Any]]:
    """加载速率限制桶状态"""
    buckets: List[Dict[str, Any]] = []
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT instance_key, tokens, rate, burst, paused_until, meta_json
            FROM scm.sync_rate_limits
            """
        )
        now = datetime.now(timezone.utc)
        for row in cur.fetchall():
            meta = row["meta_json"] or {}
            if isinstance(meta, str):
                meta = json.loads(meta)
            pause_until = row["paused_until"]
            is_paused = False
            pause_remaining = 0.0
            if pause_until:
                if pause_until.tzinfo is None:
                    pause_until = pause_until.replace(tzinfo=timezone.utc)
                is_paused = pause_until > now
                pause_remaining = max(0.0, (pause_until - now).total_seconds())
            tokens = float(row["tokens"])
            rate = float(row["rate"])
            wait_seconds = max(0.0, (1.0 - tokens) / rate) if rate > 0 and tokens < 1.0 else 0.0
            buckets.append(
                {
                    "instance_key": row["instance_key"],
                    "tokens_remaining": tokens,
                    "pause_until": pause_until.isoformat() if pause_until else None,
                    "source": meta.get("pause_source") or meta.get("source"),
                    "is_paused": is_paused,
                    "pause_remaining_seconds": pause_remaining,
                    "wait_seconds": wait_seconds,
                    "rate": rate,
                    "burst": int(row["burst"]),
                }
            )
    return buckets


def _legacy_token_buckets(buckets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """转换为旧版 token bucket 格式"""
    states: List[Dict[str, Any]] = []
    for b in buckets:
        states.append(
            {
                "instance_key": b["instance_key"],
                "tokens_remaining": b["tokens_remaining"],
                "paused_until": b["pause_until"],
                "wait_seconds": b["wait_seconds"],
                "is_paused": b["is_paused"],
                "pause_remaining_seconds": b["pause_remaining_seconds"],
                "rate": b["rate"],
                "burst": b["burst"],
            }
        )
    return states


def _load_pauses(conn) -> Dict[str, Any]:
    """加载暂停状态"""
    paused_details: List[Dict[str, Any]] = []
    pauses_by_reason: Dict[str, int] = {}
    now = time.time()
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT key, value_json FROM logbook.kv WHERE namespace = %s",
            ("scm.sync_pauses",),
        )
        for row in cur.fetchall():
            value = row["value_json"] or {}
            if isinstance(value, str):
                value = json.loads(value)
            reason_code = value.get("reason_code") or "unknown"
            pauses_by_reason[reason_code] = pauses_by_reason.get(reason_code, 0) + 1
            paused_until = float(value.get("paused_until", 0.0))
            remaining = max(0.0, paused_until - now)
            paused_details.append(
                {
                    "repo_id": value.get("repo_id"),
                    "job_type": value.get("job_type"),
                    "reason": value.get("reason"),
                    "reason_code": reason_code,
                    "paused_until": paused_until,
                    "paused_at": value.get("paused_at"),
                    "remaining_seconds": remaining,
                    "is_expired": remaining <= 0,
                }
            )
    paused_repos = {
        d["repo_id"] for d in paused_details if d["repo_id"] is not None and not d["is_expired"]
    }
    return {
        "pauses_by_reason": pauses_by_reason,
        "paused_details": paused_details,
        "paused_repos_count": len(paused_repos),
    }


def _default_error_budget() -> Dict[str, Any]:
    """返回默认的 error_budget 结构"""
    return {
        "window_minutes": 60,
        "samples": 0,
        "total_requests": 0,
        "completed_runs": 0,
        "failure": {"count": 0, "rate": 0.0},
        "rate_limit_429": {"count": 0, "rate": 0.0},
        "timeout": {"count": 0, "rate": 0.0},
    }


def get_sync_summary(conn, *, db_api=None) -> Dict[str, Any]:
    """
    获取同步状态摘要

    聚合以下信息：
    - 仓库统计
    - 运行状态统计
    - 任务状态统计
    - 锁状态
    - 熔断器状态
    - 速率限制状态
    - 错误预算
    - 暂停状态

    Args:
        conn: 数据库连接
        db_api: 数据库 API 模块（用于测试注入）

    Returns:
        摘要字典
    """
    if db_api is None:
        from engram.logbook import scm_db as db_api

    base = db_api.get_sync_status_summary(conn)
    jobs = db_api.list_sync_jobs(conn, limit=100)
    repos = {r["repo_id"]: r for r in db_api.list_repos(conn, limit=500)}
    for job in jobs:
        repo = repos.get(job.get("repo_id"))
        instance_key = ""
        tenant_id = ""
        if repo:
            url = repo.get("url") or ""
            parsed = urlparse(url)
            instance_key = parsed.netloc or url
            tenant_id = repo.get("project_key") or ""
        job["instance_key"] = instance_key
        job["tenant_id"] = tenant_id
    locks = db_api.list_sync_locks(conn, limit=100)
    expired_locks = db_api.list_expired_locks(conn, limit=100)
    circuit_states = _load_circuit_breakers(conn)
    circuit_breakers = _aggregate_circuit_breakers(circuit_states)
    rate_limit_buckets = _load_rate_limit_buckets(conn)
    token_bucket_states = _legacy_token_buckets(rate_limit_buckets)
    pauses_info = _load_pauses(conn)

    summary = {
        "repos_count": base["repos_count"],
        "repos_by_type": base["repos_by_type"],
        "runs_24h_by_status": base["runs_24h_by_status"],
        "jobs_by_status": base["jobs_by_status"],
        "locks": locks,
        "expired_locks": expired_locks,
        "cursors_count": base["cursors_count"],
        "jobs": jobs,
        "window_stats": {},
        "by_instance": {},
        "by_tenant": {},
        "top_lag_repos": [],
        "circuit_breakers": circuit_breakers,
        "rate_limit_buckets": rate_limit_buckets,
        "error_budget": _load_error_budget(conn, db_api=db_api),
        "pauses_by_reason": pauses_info["pauses_by_reason"],
        # 兼容旧字段
        "circuit_breaker_states": circuit_states,
        "token_bucket_states": token_bucket_states,
        "paused_by_reason": pauses_info["pauses_by_reason"],
        "paused_repos_count": pauses_info["paused_repos_count"],
        "paused_details": pauses_info["paused_details"],
    }
    return summary


def format_prometheus_metrics(summary: Dict[str, Any]) -> str:
    """
    将摘要格式化为 Prometheus 指标格式

    Args:
        summary: 同步状态摘要

    Returns:
        Prometheus 指标文本
    """
    lines: List[str] = []

    # base metrics
    lines.append(f"scm_repos_total {summary.get('repos_count', 0)}")
    for repo_type, count in summary.get("repos_by_type", {}).items():
        lines.append(f'scm_repos_by_type{{repo_type="{repo_type}"}} {count}')
    jobs_total = sum(summary.get("jobs_by_status", {}).values())
    lines.append(f"scm_jobs_total {jobs_total}")
    lines.append(f"scm_expired_locks {len(summary.get('expired_locks', []))}")
    lines.append(f"scm_cursors_total {summary.get('cursors_count', 0)}")

    # error_budget metrics
    eb = summary.get("error_budget", {})
    lines.append(f"scm_error_budget_samples {eb.get('samples', 0)}")
    lines.append(f"scm_error_budget_failure_count {eb.get('failure', {}).get('count', 0)}")
    lines.append(f"scm_error_budget_failure_rate {eb.get('failure', {}).get('rate', 0)}")
    lines.append(f"scm_error_budget_429_count {eb.get('rate_limit_429', {}).get('count', 0)}")
    lines.append(f"scm_error_budget_429_rate {eb.get('rate_limit_429', {}).get('rate', 0)}")
    lines.append(f"scm_error_budget_timeout_count {eb.get('timeout', {}).get('count', 0)}")
    lines.append(f"scm_error_budget_timeout_rate {eb.get('timeout', {}).get('rate', 0)}")

    # circuit breaker metrics
    for cb in summary.get("circuit_breaker_states", []):
        key = cb.get("key", "")
        state = cb.get("state", {})
        lines.append(f'scm_breaker_state{{instance_key="{key}"}} 1')
        lines.append(f'scm_breaker_failure_count{{key="{key}"}} {state.get("failure_count", 0)}')
        lines.append(f'scm_breaker_success_count{{key="{key}"}} {state.get("success_count", 0)}')
        lines.append(f'scm_circuit_breaker_state{{key="{key}"}} 1')
        lines.append(
            f'scm_circuit_breaker_failure_count{{key="{key}"}} {state.get("failure_count", 0)}'
        )
        lines.append(
            f'scm_circuit_breaker_success_count{{key="{key}"}} {state.get("success_count", 0)}'
        )

    circuit_breakers = summary.get("circuit_breakers", {})
    lines.append(f"scm_circuit_breakers_by_scope {len(circuit_breakers.get('by_scope', {}))}")
    lines.append(
        f"scm_circuit_breakers_total_failures {sum(v.get('failure_count', 0) for v in circuit_breakers.get('by_scope', {}).values())}"
    )

    # rate limit buckets (new)
    for bucket in summary.get("rate_limit_buckets", []):
        key = bucket["instance_key"]
        lines.append(
            f'scm_rate_limit_bucket_tokens{{instance_key="{key}"}} {bucket["tokens_remaining"]}'
        )
        lines.append(
            f'scm_rate_limit_bucket_paused{{instance_key="{key}"}} {1 if bucket["is_paused"] else 0}'
        )
        lines.append(
            f'scm_rate_limit_bucket_pause_seconds{{instance_key="{key}"}} {bucket["pause_remaining_seconds"]}'
        )

    # token bucket metrics (legacy)
    for bucket in summary.get("token_bucket_states", []):
        key = bucket["instance_key"]
        lines.append(
            f'scm_token_bucket_tokens_remaining{{instance_key="{key}"}} {bucket["tokens_remaining"]}'
        )
        lines.append(
            f'scm_token_bucket_wait_seconds{{instance_key="{key}"}} {bucket["wait_seconds"]}'
        )
        lines.append(
            f'scm_token_bucket_is_paused{{instance_key="{key}"}} {1 if bucket["is_paused"] else 0}'
        )
        lines.append(
            f'scm_token_bucket_pause_remaining_seconds{{instance_key="{key}"}} {bucket["pause_remaining_seconds"]}'
        )

    # rate_limit pause_until metric
    for bucket in summary.get("token_bucket_states", []):
        if bucket["paused_until"]:
            try:
                ts = datetime.fromisoformat(bucket["paused_until"]).timestamp()
            except Exception:
                ts = 0
        else:
            ts = 0
        lines.append(
            f'scm_rate_limit_pause_until{{instance_key="{bucket["instance_key"]}"}} {int(ts)}'
        )

    # pauses by reason
    for reason, count in summary.get("pauses_by_reason", {}).items():
        lines.append(f'scm_pauses_by_reason{{reason_code="{reason}"}} {count}')
        lines.append(f'scm_paused_by_reason{{reason_code="{reason}"}} {count}')
    lines.append(f"scm_paused_repos_total {summary.get('paused_repos_count', 0)}")

    # jobs by status
    for status, count in summary.get("jobs_by_status", {}).items():
        lines.append(f'scm_jobs_by_status{{status="{status}"}} {count}')

    # retry backoff seconds (from jobs with not_before)
    now = datetime.now(timezone.utc)
    for job in summary.get("jobs", []):
        not_before = job.get("not_before")
        if not not_before:
            continue
        if not_before.tzinfo is None:
            not_before = not_before.replace(tzinfo=timezone.utc)
        if not_before <= now:
            continue
        backoff_seconds = (not_before - now).total_seconds()
        instance_key = job.get("instance_key", "")
        tenant_id = job.get("tenant_id", "")
        job_type = job.get("job_type", "")
        lines.append(
            f'scm_retry_backoff_seconds{{instance_key="{instance_key}",tenant_id="{tenant_id}",job_type="{job_type}"}} {backoff_seconds}'
        )

    return "\n".join(lines) + "\n"


# ============ 健康检查不变量 ============


def check_invariants(
    conn,
    *,
    db_api=None,
    include_details: bool = False,
    grace_seconds: int = 60,
) -> HealthCheckResult:
    """
    执行系统健康不变量检查

    检查项：
    1. expired_running_jobs: running jobs 中 locked_at+lease 已过期数量
    2. orphan_locks: sync_locks 过期/孤立（锁存在但无对应 running job）数量
    3. gitlab_jobs_missing_dimensions: active gitlab_* jobs 缺失 gitlab_instance/tenant_id
    4. expired_pauses: paused_records 过期但仍存在于数据库
    5. circuit_breaker_inconsistencies: circuit_breaker state 与 error_budget 的矛盾状态

    Args:
        conn: 数据库连接
        db_api: 数据库 API 模块（用于测试注入）
        include_details: 是否包含详细记录（默认 False 以减少输出）
        grace_seconds: running job 过期宽限时间（秒）

    Returns:
        HealthCheckResult: 健康检查结果
    """
    if db_api is None:
        from engram.logbook import scm_db as db_api

    violations: List[InvariantViolation] = []
    total_checks = 5
    passed_checks = 0

    # 检查 1: expired_running_jobs
    expired_running_count = db_api.count_expired_running_jobs(conn, grace_seconds=grace_seconds)
    if expired_running_count > 0:
        details = []
        if include_details:
            expired_jobs = db_api.list_expired_running_jobs(
                conn, grace_seconds=grace_seconds, limit=10
            )
            details = [
                {"job_id": str(j["job_id"]), "repo_id": j["repo_id"], "job_type": j["job_type"]}
                for j in expired_jobs
            ]
        violations.append(
            InvariantViolation(
                check_id="expired_running_jobs",
                name="过期的 Running 任务",
                severity=InvariantSeverity.CRITICAL,
                count=expired_running_count,
                description=f"有 {expired_running_count} 个 running 状态的任务租约已过期",
                remediation_hint="运行 `engram-scm-sync reaper --once` 回收过期任务",
                details=details,
            )
        )
    else:
        passed_checks += 1

    # 检查 2: orphan_locks
    orphan_lock_count = db_api.count_orphan_locks(conn)
    if orphan_lock_count > 0:
        details = []
        if include_details:
            orphan_locks = db_api.list_orphan_locks(conn, limit=10)
            details = [
                {
                    "lock_id": lock["lock_id"],
                    "repo_id": lock["repo_id"],
                    "job_type": lock["job_type"],
                }
                for lock in orphan_locks
            ]
        violations.append(
            InvariantViolation(
                check_id="orphan_locks",
                name="孤立锁",
                severity=InvariantSeverity.WARNING,
                count=orphan_lock_count,
                description=f"有 {orphan_lock_count} 个锁没有对应的 running job",
                remediation_hint="运行 `engram-scm-sync admin locks force-release --lock-id <id>` 释放孤立锁",
                details=details,
            )
        )
    else:
        passed_checks += 1

    # 检查 3: gitlab_jobs_missing_dimensions
    missing_dims_count = db_api.count_gitlab_jobs_missing_dimensions(conn)
    if missing_dims_count > 0:
        details = []
        if include_details:
            missing_jobs = db_api.list_gitlab_jobs_missing_dimensions(conn, limit=10)
            details = [
                {
                    "job_id": str(j["job_id"]),
                    "repo_id": j["repo_id"],
                    "job_type": j["job_type"],
                    "gitlab_instance": j.get("gitlab_instance"),
                    "tenant_id": j.get("tenant_id"),
                }
                for j in missing_jobs
            ]
        violations.append(
            InvariantViolation(
                check_id="gitlab_jobs_missing_dimensions",
                name="GitLab 任务缺失维度",
                severity=InvariantSeverity.WARNING,
                count=missing_dims_count,
                description=f"有 {missing_dims_count} 个 gitlab_* 任务缺失 gitlab_instance 或 tenant_id 列",
                remediation_hint="检查 scheduler 入队逻辑，确保 payload 中包含维度信息；可使用 SQL 补填维度列",
                details=details,
            )
        )
    else:
        passed_checks += 1

    # 检查 4: expired_pauses
    expired_pause_count = db_api.count_expired_pauses_affecting_scheduling(conn)
    if expired_pause_count > 0:
        details = []
        if include_details:
            expired_pauses = db_api.list_expired_pauses(conn, limit=10)
            details = expired_pauses
        violations.append(
            InvariantViolation(
                check_id="expired_pauses",
                name="过期的暂停记录",
                severity=InvariantSeverity.INFO,
                count=expired_pause_count,
                description=f"有 {expired_pause_count} 个已过期的暂停记录仍在数据库中",
                remediation_hint="运行清理脚本删除过期的 scm.sync_pauses 记录，或等待自动清理",
                details=details,
            )
        )
    else:
        passed_checks += 1

    # 检查 5: circuit_breaker_inconsistencies
    cb_inconsistencies = db_api.get_circuit_breaker_inconsistencies(conn)
    if cb_inconsistencies:
        violations.append(
            InvariantViolation(
                check_id="circuit_breaker_inconsistencies",
                name="熔断器状态不一致",
                severity=InvariantSeverity.WARNING,
                count=len(cb_inconsistencies),
                description=f"有 {len(cb_inconsistencies)} 个熔断器状态与 error_budget 不一致",
                remediation_hint="运行 `engram-scm-sync admin jobs reset-dead` 重置死任务，或手动检查熔断器状态",
                details=cb_inconsistencies if include_details else [],
            )
        )
    else:
        passed_checks += 1

    # 确定整体健康状态
    has_critical = any(v.severity == InvariantSeverity.CRITICAL for v in violations)
    healthy = not has_critical

    return HealthCheckResult(
        healthy=healthy,
        violations=violations,
        checked_at=time.time(),
        total_checks=total_checks,
        passed_checks=passed_checks,
        failed_checks=total_checks - passed_checks,
    )


def format_health_check_output(result: HealthCheckResult, *, verbose: bool = False) -> str:
    """
    格式化健康检查结果为人类可读文本

    Args:
        result: 健康检查结果
        verbose: 是否显示详细信息

    Returns:
        str: 格式化的文本输出
    """
    lines: List[str] = []

    # 标题和状态
    status_icon = "✓" if result.healthy else "✗"
    status_text = "健康" if result.healthy else "不健康"
    lines.append(f"健康检查结果: {status_icon} {status_text}")
    lines.append(
        f"检查时间: {datetime.fromtimestamp(result.checked_at, tz=timezone.utc).isoformat()}"
    )
    lines.append(f"检查项: {result.passed_checks}/{result.total_checks} 通过")
    lines.append("")

    if not result.violations:
        lines.append("所有检查项均通过。")
    else:
        lines.append("违规项:")
        for v in result.violations:
            severity_icon = {
                InvariantSeverity.CRITICAL: "🔴",
                InvariantSeverity.WARNING: "🟡",
                InvariantSeverity.INFO: "🔵",
            }.get(v.severity, "⚪")

            lines.append(f"  {severity_icon} [{v.severity.value.upper()}] {v.name}")
            lines.append(f"     数量: {v.count}")
            lines.append(f"     描述: {v.description}")
            lines.append(f"     建议: {v.remediation_hint}")

            if verbose and v.details:
                lines.append("     详情:")
                for i, d in enumerate(v.details[:5]):
                    lines.append(f"       {i + 1}. {d}")
                if len(v.details) > 5:
                    lines.append(f"       ... 还有 {len(v.details) - 5} 条")
            lines.append("")

    return "\n".join(lines)
