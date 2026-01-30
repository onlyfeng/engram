#!/usr/bin/env python3
"""
scm_sync_status - SCM 同步状态摘要与 Prometheus 输出（简化版）
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List
from urllib.parse import urlparse

from psycopg.rows import dict_row

import db as db_api


def _load_circuit_breakers(conn) -> List[Dict[str, Any]]:
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


def _aggregate_circuit_breakers(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_scope: Dict[str, Any] = {}
    total_open = 0
    for entry in entries:
        key = entry["key"]
        parts = key.split(":", 1)
        scope = parts[1] if len(parts) > 1 else "global"
        state = entry.get("state") or {}
        if state.get("state") == "open":
            total_open += 1
        scope_data = by_scope.setdefault(
            scope,
            {
                "count": 0,
                "open": 0,
                "entries": [],
                "failure_count": 0,
                "success_count": 0,
            },
        )
        scope_data["count"] += 1
        if state.get("state") == "open":
            scope_data["open"] += 1
        scope_data["failure_count"] += int(state.get("failure_count", 0) or 0)
        scope_data["success_count"] += int(state.get("success_count", 0) or 0)
        scope_data["entries"].append(entry)
    return {
        "by_scope": by_scope,
        "total_count": len(entries),
        "total_open": total_open,
    }


def _load_rate_limit_buckets(conn) -> List[Dict[str, Any]]:
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
    paused_repos = {d["repo_id"] for d in paused_details if d["repo_id"] is not None and not d["is_expired"]}
    return {
        "pauses_by_reason": pauses_by_reason,
        "paused_details": paused_details,
        "paused_repos_count": len(paused_repos),
    }


def _default_error_budget() -> Dict[str, Any]:
    return {
        "window_minutes": 60,
        "samples": 0,
        "total_requests": 0,
        "completed_runs": 0,
        "failure": {"count": 0, "rate": 0.0},
        "rate_limit_429": {"count": 0, "rate": 0.0},
        "timeout": {"count": 0, "rate": 0.0},
    }


def get_sync_summary(conn) -> Dict[str, Any]:
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
        "error_budget": _default_error_budget(),
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
        lines.append(f'scm_circuit_breaker_failure_count{{key="{key}"}} {state.get("failure_count", 0)}')
        lines.append(f'scm_circuit_breaker_success_count{{key="{key}"}} {state.get("success_count", 0)}')

    circuit_breakers = summary.get("circuit_breakers", {})
    lines.append(f"scm_circuit_breakers_by_scope {len(circuit_breakers.get('by_scope', {}))}")
    lines.append(f"scm_circuit_breakers_total_failures {sum(v.get('failure_count', 0) for v in circuit_breakers.get('by_scope', {}).values())}")

    # rate limit buckets (new)
    for bucket in summary.get("rate_limit_buckets", []):
        key = bucket["instance_key"]
        lines.append(f'scm_rate_limit_bucket_tokens{{instance_key="{key}"}} {bucket["tokens_remaining"]}')
        lines.append(f'scm_rate_limit_bucket_paused{{instance_key="{key}"}} {1 if bucket["is_paused"] else 0}')
        lines.append(f'scm_rate_limit_bucket_pause_seconds{{instance_key="{key}"}} {bucket["pause_remaining_seconds"]}')

    # token bucket metrics (legacy)
    for bucket in summary.get("token_bucket_states", []):
        key = bucket["instance_key"]
        lines.append(f'scm_token_bucket_tokens_remaining{{instance_key="{key}"}} {bucket["tokens_remaining"]}')
        lines.append(f'scm_token_bucket_wait_seconds{{instance_key="{key}"}} {bucket["wait_seconds"]}')
        lines.append(f'scm_token_bucket_is_paused{{instance_key="{key}"}} {1 if bucket["is_paused"] else 0}')
        lines.append(f'scm_token_bucket_pause_remaining_seconds{{instance_key="{key}"}} {bucket["pause_remaining_seconds"]}')

    # rate_limit pause_until metric
    for bucket in summary.get("token_bucket_states", []):
        if bucket["paused_until"]:
            try:
                ts = datetime.fromisoformat(bucket["paused_until"]).timestamp()
            except Exception:
                ts = 0
        else:
            ts = 0
        lines.append(f'scm_rate_limit_pause_until{{instance_key="{bucket["instance_key"]}"}} {int(ts)}')

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
