# -*- coding: utf-8 -*-
"""
engram_step1.scm_sync_queue - SCM 同步任务队列模块

提供基于 PostgreSQL 的可靠任务队列功能，实现 claim/ack/fail 模式。

功能:
- enqueue: 将任务入队
- claim: 获取并锁定一个待执行任务
- ack: 确认任务完成
- fail_retry: 任务失败，安排重试
- mark_dead: 标记任务为死信（不再重试）
- renew_lease: 续租任务锁

设计原则:
- 使用 FOR UPDATE SKIP LOCKED 实现并发安全的任务获取
- 支持优先级调度（priority 越小越优先）
- 支持延迟执行（not_before）
- 支持指数退避重试
- 所有操作都是原子性的
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List

import psycopg

from .db import get_connection
from .errors import DatabaseError
from .scm_auth import redact


# 任务状态常量
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_DEAD = "dead"

# 默认配置
DEFAULT_PRIORITY = 100
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_LEASE_SECONDS = 300  # 5 分钟
DEFAULT_BACKOFF_BASE = 60    # 基础退避时间（秒）


def enqueue(
    repo_id: int,
    job_type: str,
    mode: str = "incremental",
    priority: int = DEFAULT_PRIORITY,
    payload: Optional[Dict] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    not_before: Optional[datetime] = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    conn: Optional[psycopg.Connection] = None,
) -> Optional[str]:
    """
    将任务入队。
    
    如果同一 (repo_id, job_type) 已存在 pending 或 running 状态的任务，
    则不会创建新任务（由唯一索引保证）。
    
    Args:
        repo_id: 仓库 ID
        job_type: 任务类型（gitlab_commits, gitlab_mrs, gitlab_reviews, svn）
        mode: 同步模式（incremental, backfill）
        priority: 优先级（数值越小越优先，默认 100）
        payload: 任务参数（如回填窗口 {"since": "2024-01-01", "until": "2024-06-01"}）
        max_attempts: 最大尝试次数
        not_before: 延迟执行时间（任务在此时间之前不会被 claim）
        lease_seconds: 租约时长（秒）
        conn: 可选的数据库连接
    
    Returns:
        创建的 job_id（UUID 字符串），如果任务已存在则返回 None
    """
    should_close = conn is None
    if conn is None:
        conn = get_connection()
    
    payload_json = json.dumps(payload or {})
    not_before_ts = not_before or datetime.now(timezone.utc)
    
    try:
        with conn.cursor() as cur:
            # 使用 ON CONFLICT DO NOTHING 处理唯一约束冲突
            cur.execute("""
                INSERT INTO scm.sync_jobs (
                    repo_id, job_type, mode, priority, payload_json,
                    max_attempts, not_before, lease_seconds, status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')
                ON CONFLICT (repo_id, job_type) WHERE status IN ('pending', 'running')
                DO NOTHING
                RETURNING job_id
            """, (repo_id, job_type, mode, priority, payload_json,
                  max_attempts, not_before_ts, lease_seconds))
            
            result = cur.fetchone()
            conn.commit()
            
            if result is not None:
                return str(result[0])
            return None
            
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"任务入队失败: {e}",
            {"repo_id": repo_id, "job_type": job_type, "error": str(e)},
        )
    finally:
        if should_close:
            conn.close()


def claim(
    worker_id: str,
    job_types: Optional[List[str]] = None,
    lease_seconds: Optional[int] = None,
    instance_allowlist: Optional[List[str]] = None,
    tenant_allowlist: Optional[List[str]] = None,
    conn: Optional[psycopg.Connection] = None,
) -> Optional[Dict[str, Any]]:
    """
    获取并锁定一个待执行任务。
    
    获取条件（按优先级排序）：
    1. status = 'pending' AND not_before <= now()
    2. status = 'running' 且锁已过期（locked_at + lease_seconds < now()）
    3. status = 'failed' AND not_before <= now() AND attempts < max_attempts
    
    Pool 过滤（通过 payload_json 中预写的 instance/tenant 字段快速过滤，无需 join repos）：
    - instance_allowlist: 只处理指定 GitLab 实例的任务
    - tenant_allowlist: 只处理指定租户的任务
    
    Args:
        worker_id: 当前 worker 标识符
        job_types: 可选，限制获取的任务类型列表
        lease_seconds: 可选，覆盖任务的租约时长
        instance_allowlist: 可选，限制获取的 GitLab 实例列表（基于 payload_json.gitlab_instance）
        tenant_allowlist: 可选，限制获取的租户 ID 列表（基于 payload_json.tenant_id）
        conn: 可选的数据库连接
    
    Returns:
        任务信息字典，包含 job_id, repo_id, job_type, mode, payload, attempts 等
        如果没有可用任务返回 None
    """
    should_close = conn is None
    if conn is None:
        conn = get_connection()
    
    try:
        with conn.cursor() as cur:
            # 构建查询条件
            filters: List[str] = []
            params: List[Any] = [worker_id]
            
            # job_type 过滤
            if job_types:
                placeholders = ", ".join(["%s"] * len(job_types))
                filters.append(f"job_type IN ({placeholders})")
                params.extend(job_types)
            
            # instance_allowlist 过滤（基于 payload_json ->> 'gitlab_instance'）
            # 允许未设置 gitlab_instance 的任务（如 SVN 任务）或值匹配的任务
            if instance_allowlist:
                placeholders = ", ".join(["%s"] * len(instance_allowlist))
                filters.append(f"""(
                    payload_json ->> 'gitlab_instance' IS NULL
                    OR payload_json ->> 'gitlab_instance' IN ({placeholders})
                )""")
                params.extend(instance_allowlist)
            
            # tenant_allowlist 过滤（基于 payload_json ->> 'tenant_id'）
            # 允许未设置 tenant_id 的任务或值匹配的任务
            if tenant_allowlist:
                placeholders = ", ".join(["%s"] * len(tenant_allowlist))
                filters.append(f"""(
                    payload_json ->> 'tenant_id' IS NULL
                    OR payload_json ->> 'tenant_id' IN ({placeholders})
                )""")
                params.extend(tenant_allowlist)
            
            # 组合额外过滤条件
            extra_filter = ""
            if filters:
                extra_filter = "AND " + " AND ".join(filters)
            
            # 使用 FOR UPDATE SKIP LOCKED 获取一个任务
            # 支持获取：
            # 1. pending 且 not_before 已过
            # 2. running 但锁已过期
            # 3. failed 且 not_before 已过
            query = f"""
                WITH claimable AS (
                    SELECT job_id, lease_seconds
                    FROM scm.sync_jobs
                    WHERE (
                        -- pending 任务
                        (status = 'pending' AND not_before <= now())
                        -- 或 running 但锁过期的任务
                        OR (status = 'running' AND locked_at + (lease_seconds || ' seconds')::interval < now())
                        -- 或 failed 可重试的任务
                        OR (status = 'failed' AND not_before <= now() AND attempts < max_attempts)
                    )
                    {extra_filter}
                    ORDER BY priority ASC, created_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE scm.sync_jobs j
                SET 
                    status = 'running',
                    locked_by = %s,
                    locked_at = now(),
                    attempts = attempts + 1,
                    updated_at = now()
                FROM claimable c
                WHERE j.job_id = c.job_id
                RETURNING 
                    j.job_id, j.repo_id, j.job_type, j.mode, j.payload_json,
                    j.priority, j.attempts, j.max_attempts, j.last_error,
                    j.lease_seconds, j.created_at
            """
            
            # 将 worker_id 放在参数列表开头（用于 UPDATE SET）
            cur.execute(query, params)
            
            row = cur.fetchone()
            conn.commit()
            
            if row is None:
                return None
            
            return {
                "job_id": str(row[0]),
                "repo_id": row[1],
                "job_type": row[2],
                "mode": row[3],
                "payload": row[4] if row[4] else {},
                "priority": row[5],
                "attempts": row[6],
                "max_attempts": row[7],
                "last_error": row[8],
                "lease_seconds": row[9],
                "created_at": row[10],
            }
            
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"claim 任务失败: {e}",
            {"worker_id": worker_id, "error": str(e)},
        )
    finally:
        if should_close:
            conn.close()


def ack(
    job_id: str,
    worker_id: str,
    run_id: Optional[str] = None,
    conn: Optional[psycopg.Connection] = None,
) -> bool:
    """
    确认任务完成（Acknowledge）。
    
    只有当前持有锁的 worker 才能 ack。
    
    Args:
        job_id: 任务 ID
        worker_id: 当前 worker 标识符（必须与锁持有者匹配）
        run_id: 可选，关联的 sync_run ID
        conn: 可选的数据库连接
    
    Returns:
        True 表示成功，False 表示任务不存在或不属于该 worker
    """
    should_close = conn is None
    if conn is None:
        conn = get_connection()
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE scm.sync_jobs
                SET 
                    status = 'completed',
                    locked_by = NULL,
                    locked_at = NULL,
                    last_run_id = %s,
                    last_error = NULL,
                    updated_at = now()
                WHERE job_id = %s AND locked_by = %s AND status = 'running'
                RETURNING job_id
            """, (run_id, job_id, worker_id))
            
            result = cur.fetchone()
            conn.commit()
            return result is not None
            
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"ack 任务失败: {e}",
            {"job_id": job_id, "worker_id": worker_id, "error": str(e)},
        )
    finally:
        if should_close:
            conn.close()


def fail_retry(
    job_id: str,
    worker_id: str,
    error: str,
    backoff_seconds: Optional[int] = None,
    conn: Optional[psycopg.Connection] = None,
) -> bool:
    """
    任务失败，安排重试。
    
    如果尝试次数已达到 max_attempts，自动标记为 dead。
    否则设置 status = 'failed'，并根据退避策略设置 not_before。
    
    Args:
        job_id: 任务 ID
        worker_id: 当前 worker 标识符
        error: 错误信息
        backoff_seconds: 可选，自定义退避时间（秒），为 None 时使用指数退避
        conn: 可选的数据库连接
    
    Returns:
        True 表示成功（可重试或已标记为 dead），False 表示任务不存在或不属于该 worker
    """
    should_close = conn is None
    if conn is None:
        conn = get_connection()
    
    # 对错误信息进行脱敏，防止敏感信息（如 token）泄露到数据库
    redacted_error = redact(error)
    
    try:
        with conn.cursor() as cur:
            # 先获取当前尝试次数和最大尝试次数
            cur.execute("""
                SELECT attempts, max_attempts
                FROM scm.sync_jobs
                WHERE job_id = %s AND locked_by = %s AND status = 'running'
                FOR UPDATE
            """, (job_id, worker_id))
            
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return False
            
            attempts, max_attempts = row
            
            # 判断是否达到最大尝试次数
            if attempts >= max_attempts:
                # 标记为 dead
                cur.execute("""
                    UPDATE scm.sync_jobs
                    SET 
                        status = 'dead',
                        locked_by = NULL,
                        locked_at = NULL,
                        last_error = %s,
                        updated_at = now()
                    WHERE job_id = %s
                """, (redacted_error, job_id))
            else:
                # 计算退避时间（指数退避: base * 2^(attempts-1)）
                if backoff_seconds is None:
                    backoff_seconds = DEFAULT_BACKOFF_BASE * (2 ** (attempts - 1))
                
                cur.execute("""
                    UPDATE scm.sync_jobs
                    SET 
                        status = 'failed',
                        locked_by = NULL,
                        locked_at = NULL,
                        last_error = %s,
                        not_before = now() + (%s || ' seconds')::interval,
                        updated_at = now()
                    WHERE job_id = %s
                """, (redacted_error, backoff_seconds, job_id))
            
            conn.commit()
            return True
            
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"fail_retry 任务失败: {e}",
            {"job_id": job_id, "worker_id": worker_id, "error": str(e)},
        )
    finally:
        if should_close:
            conn.close()


def mark_dead(
    job_id: str,
    worker_id: str,
    error: str,
    conn: Optional[psycopg.Connection] = None,
) -> bool:
    """
    标记任务为死信（不再重试）。
    
    Args:
        job_id: 任务 ID
        worker_id: 当前 worker 标识符
        error: 错误信息
        conn: 可选的数据库连接
    
    Returns:
        True 表示成功，False 表示任务不存在或不属于该 worker
    """
    should_close = conn is None
    if conn is None:
        conn = get_connection()
    
    # 对错误信息进行脱敏，防止敏感信息泄露
    redacted_error = redact(error)
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE scm.sync_jobs
                SET 
                    status = 'dead',
                    locked_by = NULL,
                    locked_at = NULL,
                    last_error = %s,
                    updated_at = now()
                WHERE job_id = %s AND locked_by = %s AND status = 'running'
                RETURNING job_id
            """, (redacted_error, job_id, worker_id))
            
            result = cur.fetchone()
            conn.commit()
            return result is not None
            
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"mark_dead 任务失败: {e}",
            {"job_id": job_id, "worker_id": worker_id, "error": str(e)},
        )
    finally:
        if should_close:
            conn.close()


def requeue_without_penalty(
    job_id: str,
    worker_id: str,
    reason: Optional[str] = None,
    jitter_seconds: int = 5,
    conn: Optional[psycopg.Connection] = None,
) -> bool:
    """
    将任务重新入队，不增加 attempts 惩罚（用于可安全让出的场景）。
    
    当任务因为外部资源锁定（如 locked==true && skipped==true）而无法执行时，
    应该将任务放回队列等待下次调度，而不计入失败重试次数。
    
    由于 claim() 时已经将 attempts +1，这里需要 -1 来补偿。
    
    Args:
        job_id: 任务 ID
        worker_id: 当前 worker 标识符
        reason: 可选，让出原因（记录到 last_error）
        jitter_seconds: 重新调度的抖动秒数（默认 5 秒）
        conn: 可选的数据库连接
    
    Returns:
        True 表示成功，False 表示任务不存在或不属于该 worker
    """
    import random
    
    should_close = conn is None
    if conn is None:
        conn = get_connection()
    
    # 计算抖动时间：0 到 jitter_seconds 之间的随机值
    jitter = random.uniform(0, jitter_seconds)
    
    # 对原因进行脱敏，防止敏感信息泄露
    redacted_reason = redact(reason) if reason else None
    
    try:
        with conn.cursor() as cur:
            # 将任务回退到 pending 状态，attempts -1 补偿
            cur.execute("""
                UPDATE scm.sync_jobs
                SET 
                    status = 'pending',
                    locked_by = NULL,
                    locked_at = NULL,
                    attempts = GREATEST(0, attempts - 1),
                    not_before = now() + (%s || ' seconds')::interval,
                    last_error = %s,
                    updated_at = now()
                WHERE job_id = %s AND locked_by = %s AND status = 'running'
                RETURNING job_id
            """, (jitter, redacted_reason, job_id, worker_id))
            
            result = cur.fetchone()
            conn.commit()
            return result is not None
            
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"requeue_without_penalty 失败: {e}",
            {"job_id": job_id, "worker_id": worker_id, "error": str(e)},
        )
    finally:
        if should_close:
            conn.close()


def renew_lease(
    job_id: str,
    worker_id: str,
    lease_seconds: Optional[int] = None,
    conn: Optional[psycopg.Connection] = None,
) -> bool:
    """
    续租任务锁。
    
    延长任务的锁持有时间，防止长时间任务被其他 worker 抢占。
    
    Args:
        job_id: 任务 ID
        worker_id: 当前 worker 标识符
        lease_seconds: 可选，新的租约时长
        conn: 可选的数据库连接
    
    Returns:
        True 表示续租成功，False 表示任务不存在或不属于该 worker
    """
    should_close = conn is None
    if conn is None:
        conn = get_connection()
    
    try:
        with conn.cursor() as cur:
            if lease_seconds is not None:
                cur.execute("""
                    UPDATE scm.sync_jobs
                    SET locked_at = now(), lease_seconds = %s, updated_at = now()
                    WHERE job_id = %s AND locked_by = %s AND status = 'running'
                    RETURNING job_id
                """, (lease_seconds, job_id, worker_id))
            else:
                cur.execute("""
                    UPDATE scm.sync_jobs
                    SET locked_at = now(), updated_at = now()
                    WHERE job_id = %s AND locked_by = %s AND status = 'running'
                    RETURNING job_id
                """, (job_id, worker_id))
            
            result = cur.fetchone()
            conn.commit()
            return result is not None
            
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"renew_lease 任务失败: {e}",
            {"job_id": job_id, "worker_id": worker_id, "error": str(e)},
        )
    finally:
        if should_close:
            conn.close()


def get_job(
    job_id: str,
    conn: Optional[psycopg.Connection] = None,
) -> Optional[Dict[str, Any]]:
    """
    获取任务详情。
    
    Args:
        job_id: 任务 ID
        conn: 可选的数据库连接
    
    Returns:
        任务信息字典，不存在返回 None
    """
    should_close = conn is None
    if conn is None:
        conn = get_connection()
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    job_id, repo_id, job_type, mode, payload_json,
                    priority, status, attempts, max_attempts,
                    not_before, locked_by, locked_at, lease_seconds,
                    last_error, last_run_id, created_at, updated_at
                FROM scm.sync_jobs
                WHERE job_id = %s
            """, (job_id,))
            
            row = cur.fetchone()
            if row is None:
                return None
            
            return {
                "job_id": str(row[0]),
                "repo_id": row[1],
                "job_type": row[2],
                "mode": row[3],
                "payload": row[4] if row[4] else {},
                "priority": row[5],
                "status": row[6],
                "attempts": row[7],
                "max_attempts": row[8],
                "not_before": row[9],
                "locked_by": row[10],
                "locked_at": row[11],
                "lease_seconds": row[12],
                "last_error": row[13],
                "last_run_id": str(row[14]) if row[14] else None,
                "created_at": row[15],
                "updated_at": row[16],
            }
            
    except psycopg.Error as e:
        raise DatabaseError(
            f"获取任务详情失败: {e}",
            {"job_id": job_id, "error": str(e)},
        )
    finally:
        if should_close:
            conn.close()


def list_jobs_by_status(
    status: str,
    limit: int = 100,
    conn: Optional[psycopg.Connection] = None,
) -> List[Dict[str, Any]]:
    """
    列出指定状态的任务。
    
    Args:
        status: 任务状态
        limit: 返回数量上限
        conn: 可选的数据库连接
    
    Returns:
        任务列表
    """
    should_close = conn is None
    if conn is None:
        conn = get_connection()
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    job_id, repo_id, job_type, mode, priority,
                    status, attempts, max_attempts, locked_by, 
                    last_error, created_at, updated_at
                FROM scm.sync_jobs
                WHERE status = %s
                ORDER BY priority ASC, created_at ASC
                LIMIT %s
            """, (status, limit))
            
            rows = cur.fetchall()
            return [
                {
                    "job_id": str(row[0]),
                    "repo_id": row[1],
                    "job_type": row[2],
                    "mode": row[3],
                    "priority": row[4],
                    "status": row[5],
                    "attempts": row[6],
                    "max_attempts": row[7],
                    "locked_by": row[8],
                    "last_error": row[9],
                    "created_at": row[10],
                    "updated_at": row[11],
                }
                for row in rows
            ]
            
    except psycopg.Error as e:
        raise DatabaseError(
            f"列出任务失败: {e}",
            {"status": status, "error": str(e)},
        )
    finally:
        if should_close:
            conn.close()


def list_jobs_by_worker(
    worker_id: str,
    conn: Optional[psycopg.Connection] = None,
) -> List[Dict[str, Any]]:
    """
    列出指定 worker 当前持有的任务。
    
    Args:
        worker_id: worker 标识符
        conn: 可选的数据库连接
    
    Returns:
        任务列表
    """
    should_close = conn is None
    if conn is None:
        conn = get_connection()
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    job_id, repo_id, job_type, mode, priority,
                    status, attempts, locked_at, lease_seconds,
                    created_at
                FROM scm.sync_jobs
                WHERE locked_by = %s AND status = 'running'
                ORDER BY locked_at ASC
            """, (worker_id,))
            
            rows = cur.fetchall()
            return [
                {
                    "job_id": str(row[0]),
                    "repo_id": row[1],
                    "job_type": row[2],
                    "mode": row[3],
                    "priority": row[4],
                    "status": row[5],
                    "attempts": row[6],
                    "locked_at": row[7],
                    "lease_seconds": row[8],
                    "created_at": row[9],
                }
                for row in rows
            ]
            
    except psycopg.Error as e:
        raise DatabaseError(
            f"列出 worker 任务失败: {e}",
            {"worker_id": worker_id, "error": str(e)},
        )
    finally:
        if should_close:
            conn.close()


def count_jobs_by_status(
    conn: Optional[psycopg.Connection] = None,
) -> Dict[str, int]:
    """
    统计各状态的任务数量。
    
    Args:
        conn: 可选的数据库连接
    
    Returns:
        状态 -> 数量的字典
    """
    should_close = conn is None
    if conn is None:
        conn = get_connection()
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT status, COUNT(*) as cnt
                FROM scm.sync_jobs
                GROUP BY status
            """)
            
            rows = cur.fetchall()
            return {row[0]: row[1] for row in rows}
            
    except psycopg.Error as e:
        raise DatabaseError(
            f"统计任务失败: {e}",
            {"error": str(e)},
        )
    finally:
        if should_close:
            conn.close()


def cleanup_completed_jobs(
    older_than_days: int = 7,
    conn: Optional[psycopg.Connection] = None,
) -> int:
    """
    清理已完成的旧任务。
    
    Args:
        older_than_days: 清理多少天前完成的任务
        conn: 可选的数据库连接
    
    Returns:
        删除的任务数量
    """
    should_close = conn is None
    if conn is None:
        conn = get_connection()
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM scm.sync_jobs
                WHERE status = 'completed'
                  AND updated_at < now() - (%s || ' days')::interval
            """, (older_than_days,))
            
            deleted = cur.rowcount
            conn.commit()
            return deleted
            
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"清理任务失败: {e}",
            {"older_than_days": older_than_days, "error": str(e)},
        )
    finally:
        if should_close:
            conn.close()


def reset_dead_jobs(
    repo_id: Optional[int] = None,
    job_type: Optional[str] = None,
    conn: Optional[psycopg.Connection] = None,
) -> int:
    """
    重置死信任务为 pending 状态（用于管理员手动重试）。
    
    Args:
        repo_id: 可选，限制特定仓库
        job_type: 可选，限制特定任务类型
        conn: 可选的数据库连接
    
    Returns:
        重置的任务数量
    """
    should_close = conn is None
    if conn is None:
        conn = get_connection()
    
    try:
        with conn.cursor() as cur:
            # 构建条件
            conditions = ["status = 'dead'"]
            params: List[Any] = []
            
            if repo_id is not None:
                conditions.append("repo_id = %s")
                params.append(repo_id)
            
            if job_type is not None:
                conditions.append("job_type = %s")
                params.append(job_type)
            
            where_clause = " AND ".join(conditions)
            
            cur.execute(f"""
                UPDATE scm.sync_jobs
                SET 
                    status = 'pending',
                    attempts = 0,
                    not_before = now(),
                    last_error = NULL,
                    updated_at = now()
                WHERE {where_clause}
            """, params)
            
            updated = cur.rowcount
            conn.commit()
            return updated
            
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"重置死信任务失败: {e}",
            {"repo_id": repo_id, "job_type": job_type, "error": str(e)},
        )
    finally:
        if should_close:
            conn.close()
