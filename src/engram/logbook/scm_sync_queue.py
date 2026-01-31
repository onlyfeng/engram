# -*- coding: utf-8 -*-
"""
engram_logbook.scm_sync_queue - SCM 同步任务队列模块

提供基于 PostgreSQL 的可靠任务队列功能，实现 claim/ack/fail 模式。

功能:
- enqueue: 将任务入队
- claim: 获取并锁定一个待执行任务
- ack: 确认任务完成
- fail_retry: 任务失败，安排重试
- mark_dead: 标记任务为死信（不再重试）
- requeue_without_penalty: 无惩罚重入队（用于 lock_held 等可安全让出的场景）
- renew_lease: 续租任务锁
- reset_dead_jobs: 重置死信任务（管理员操作）

设计原则:
- 使用 FOR UPDATE SKIP LOCKED 实现并发安全的任务获取
- 支持优先级调度（priority 越小越优先）
- 支持延迟执行（not_before）
- 支持指数退避重试
- 所有操作都是原子性的

状态转换矩阵 (State Transition Matrix):
=========================================

| 源状态  | 目标状态   | 操作                    | 条件                                             |
|---------|------------|-------------------------|--------------------------------------------------|
| (新建)  | pending    | enqueue                 | (repo_id, job_type, mode) 无活跃任务（唯一索引保证） |
| pending | running    | claim                   | not_before <= now()                              |
| running | running    | claim (抢占)            | locked_at + lease_seconds < now() (租约过期)     |
| failed  | running    | claim (重试)            | not_before <= now() AND attempts < max_attempts  |
| running | completed  | ack                     | locked_by = worker_id AND status = running       |
| running | failed     | fail_retry              | locked_by = worker_id AND attempts < max_attempts|
| running | dead       | fail_retry              | locked_by = worker_id AND attempts >= max_attempts|
| running | dead       | mark_dead               | locked_by = worker_id                            |
| running | pending    | requeue_without_penalty | locked_by = worker_id (attempts 回补 -1)         |
| dead    | pending    | reset_dead_jobs         | (管理员操作，attempts 重置为 0)                  |

并发边界条件:
- 并发 claim: FOR UPDATE SKIP LOCKED 确保原子性，只有一个 worker 能成功
- 重复 ack: 状态非 running 或 locked_by 不匹配时返回 False
- 过期 lease: running 状态但 locked_at + lease_seconds < now() 的任务可被重新 claim
- Worker 身份验证: ack/fail_retry/mark_dead/renew_lease/requeue 要求 locked_by = worker_id

字段语义:
- status: pending | running | completed | failed | dead
- attempts: 已尝试次数（claim 时 +1，requeue 时 -1 回补）
- not_before: 任务在此时间之前不会被 claim（用于延迟执行和退避重试）
- locked_by: 当前持有锁的 worker 标识符
- locked_at: 锁定时间（用于判断租约是否过期）
- lease_seconds: 租约时长（秒）
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg

from .db import get_connection
from .errors import DatabaseError
from .scm_auth import redact
from .scm_sync_errors import (
    DEFAULT_BACKOFF_BASE,
    DEFAULT_MAX_BACKOFF,
    calculate_backoff_seconds,
)
from .scm_sync_keys import normalize_instance_key

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
# DEFAULT_BACKOFF_BASE 已从 scm_sync_errors 导入


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
    strict_dimension_check: bool = False,
) -> Optional[str]:
    """
    将任务入队。

    如果同一 (repo_id, job_type, mode) 已存在 pending 或 running 状态的任务，
    则不会创建新任务（由唯一索引 idx_sync_jobs_unique_active 保证）。

    注意：同一 (repo_id, job_type) 可以有不同 mode 的活跃任务同时存在，
    例如 incremental 和 backfill 可以同时入队。

    注意：如果 payload 中包含 gitlab_instance 或 tenant_id，会同时写入到
    对应的 dimension 列（如果列存在），以便 claim 时快速过滤。

    维度列说明：
    - gitlab_instance: GitLab 实例主机名，用于 budget 查询和 pool 过滤
      对于 gitlab_* 类型任务，此字段建议非空
    - tenant_id: 租户 ID，用于 budget 查询和公平调度
      单租户部署中可以为空

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
        strict_dimension_check: 严格模式，gitlab 类型任务要求 gitlab_instance 非空

    Returns:
        创建的 job_id（UUID 字符串），如果任务已存在则返回 None

    Raises:
        ValueError: strict_dimension_check=True 时，gitlab 任务缺少 gitlab_instance
    """
    should_close = conn is None
    if conn is None:
        conn = get_connection()

    payload_dict = payload or {}
    payload_json = json.dumps(payload_dict)
    not_before_ts = not_before or datetime.now(timezone.utc)

    # 从 payload 提取 dimension 字段，同步写入到 DB 列（如果列存在）
    # 这些字段用于 claim 时的快速过滤，避免 json 解析开销
    gitlab_instance = payload_dict.get("gitlab_instance")
    tenant_id = payload_dict.get("tenant_id")

    # 对 gitlab_instance 进行规范化（如果提供）
    if gitlab_instance:
        gitlab_instance = normalize_instance_key(gitlab_instance)

    # 运行时保护：gitlab 类型任务建议提供 gitlab_instance
    # 在严格模式下，缺少 gitlab_instance 会抛出异常
    is_gitlab_job = job_type.startswith("gitlab_")
    if is_gitlab_job and not gitlab_instance:
        import logging

        logger = logging.getLogger(__name__)
        warning_msg = (
            f"gitlab 类型任务 (repo_id={repo_id}, job_type={job_type}) "
            f"缺少 gitlab_instance，可能影响 budget 查询和 pool 过滤"
        )
        if strict_dimension_check:
            raise ValueError(warning_msg)
        else:
            logger.warning(warning_msg)

    try:
        with conn.cursor() as cur:
            # 使用 ON CONFLICT DO NOTHING 处理唯一约束冲突
            # 同时写入 gitlab_instance 和 tenant_id 列（如果列存在，列不存在时 SQL 会报错）
            # 注意：迁移 11_sync_jobs_dimension_columns.sql 添加了这些列
            cur.execute(
                """
                INSERT INTO scm.sync_jobs (
                    repo_id, job_type, mode, priority, payload_json,
                    max_attempts, not_before, lease_seconds, status,
                    gitlab_instance, tenant_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s)
                ON CONFLICT (repo_id, job_type, mode) WHERE status IN ('pending', 'running')
                DO NOTHING
                RETURNING job_id
            """,
                (
                    repo_id,
                    job_type,
                    mode,
                    priority,
                    payload_json,
                    max_attempts,
                    not_before_ts,
                    lease_seconds,
                    gitlab_instance,
                    tenant_id,
                ),
            )

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
    enable_tenant_fair_claim: Optional[bool] = None,
    max_consecutive_same_tenant: Optional[int] = None,
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

    租户公平调度（enable_tenant_fair_claim=True）：
    - 在候选任务中先选出若干不同 tenant，再从每个 tenant 取最高优先级的 job
    - 防止某个 tenant 的大量任务长期占用队列，导致其他 tenant 饥饿
    - max_consecutive_same_tenant: 用于外部跟踪，本函数内部通过多 tenant 选择实现公平

    Args:
        worker_id: 当前 worker 标识符
        job_types: 可选，限制获取的任务类型列表
        lease_seconds: 可选，覆盖任务的租约时长
        instance_allowlist: 可选，限制获取的 GitLab 实例列表（基于 payload_json.gitlab_instance）
        tenant_allowlist: 可选，限制获取的租户 ID 列表（基于 payload_json.tenant_id）
        enable_tenant_fair_claim: 可选，启用租户公平调度，默认从配置读取
        max_consecutive_same_tenant: 可选，单租户最大连续 claim 次数（用于外部跟踪），默认从配置读取
        conn: 可选的数据库连接

    Returns:
        任务信息字典，包含 job_id, repo_id, job_type, mode, payload, attempts 等
        如果没有可用任务返回 None
    """
    from .config import get_claim_config

    # 读取配置（参数优先，否则从配置文件读取）
    claim_config = get_claim_config()
    if enable_tenant_fair_claim is None:
        enable_tenant_fair_claim = claim_config["enable_tenant_fair_claim"]
    if max_consecutive_same_tenant is None:
        max_consecutive_same_tenant = claim_config["max_consecutive_same_tenant"]
    # max_tenants_per_round 用于限制公平调度时选取的 tenant 数量
    # 目前未直接使用，但保留配置项以便后续扩展
    # max_tenants_per_round = claim_config["max_tenants_per_round"]

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

            # instance_allowlist 过滤
            # 优先使用 gitlab_instance 列（更高效），同时兼容从 payload_json 读取（向后兼容）
            # 允许未设置 gitlab_instance 的任务（如 SVN 任务）或值匹配的任务
            # 注意：对 allowlist 做规范化，确保与 scheduler 写入的格式一致（小写、无默认端口）
            if instance_allowlist:
                normalized_instances = [
                    normalize_instance_key(inst)
                    for inst in instance_allowlist
                    if normalize_instance_key(inst)
                ]
                if normalized_instances:
                    placeholders = ", ".join(["%s"] * len(normalized_instances))
                    # 使用列优先，但也兼容 payload_json（向后兼容旧数据）
                    filters.append(f"""(
                        gitlab_instance IS NULL
                        OR gitlab_instance IN ({placeholders})
                        OR (gitlab_instance IS NULL AND payload_json ->> 'gitlab_instance' IN ({placeholders}))
                    )""")
                    # 参数需要重复两次（分别用于列和 payload_json）
                    params.extend(normalized_instances)
                    params.extend(normalized_instances)

            # tenant_allowlist 过滤
            # 优先使用 tenant_id 列（更高效），同时兼容从 payload_json 读取（向后兼容）
            # 允许未设置 tenant_id 的任务或值匹配的任务
            if tenant_allowlist:
                placeholders = ", ".join(["%s"] * len(tenant_allowlist))
                # 使用列优先，但也兼容 payload_json（向后兼容旧数据）
                filters.append(f"""(
                    tenant_id IS NULL
                    OR tenant_id IN ({placeholders})
                    OR (tenant_id IS NULL AND payload_json ->> 'tenant_id' IN ({placeholders}))
                )""")
                # 参数需要重复两次
                params.extend(tenant_allowlist)
                params.extend(tenant_allowlist)

            # 组合额外过滤条件
            extra_filter = ""
            if filters:
                extra_filter = "AND " + " AND ".join(filters)

            # 基础 claimable 条件
            claimable_conditions = """(
                -- pending 任务
                (status = 'pending' AND not_before <= now())
                -- 或 running 但锁过期的任务
                OR (status = 'running' AND locked_at + (lease_seconds || ' seconds')::interval < now())
                -- 或 failed 可重试的任务
                OR (status = 'failed' AND not_before <= now() AND attempts < max_attempts)
            )"""

            if enable_tenant_fair_claim:
                # 租户公平调度模式
                # 实现方式：
                # 1. 先用 DISTINCT ON 选出若干不同 tenant 的最高优先级任务
                # 2. 从这些候选中按优先级选择一个
                # 3. 使用 FOR UPDATE SKIP LOCKED 确保并发安全
                query = f"""
                    WITH tenant_candidates AS (
                        -- 每个 tenant 的最高优先级任务 ID
                        -- DISTINCT ON 按 tenant_id 分组，每组取 priority 最小、created_at 最早的一个
                        SELECT DISTINCT ON (COALESCE(payload_json ->> 'tenant_id', ''))
                            job_id
                        FROM scm.sync_jobs
                        WHERE {claimable_conditions}
                        {extra_filter}
                        ORDER BY COALESCE(payload_json ->> 'tenant_id', ''), priority ASC, created_at ASC
                    ),
                    claimable AS (
                        -- 从候选任务中选择优先级最高的一个，使用 FOR UPDATE SKIP LOCKED
                        SELECT j.job_id, j.lease_seconds
                        FROM scm.sync_jobs j
                        WHERE j.job_id IN (SELECT job_id FROM tenant_candidates)
                        ORDER BY j.priority ASC, j.created_at ASC
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
            else:
                # 原有模式：按优先级顺序获取
                query = f"""
                    WITH claimable AS (
                        SELECT job_id, lease_seconds
                        FROM scm.sync_jobs
                        WHERE {claimable_conditions}
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
            cur.execute(
                """
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
            """,
                (run_id, job_id, worker_id),
            )

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
            cur.execute(
                """
                SELECT attempts, max_attempts
                FROM scm.sync_jobs
                WHERE job_id = %s AND locked_by = %s AND status = 'running'
                FOR UPDATE
            """,
                (job_id, worker_id),
            )

            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return False

            attempts, max_attempts = row

            # 判断是否达到最大尝试次数
            if attempts >= max_attempts:
                # 标记为 dead
                cur.execute(
                    """
                    UPDATE scm.sync_jobs
                    SET
                        status = 'dead',
                        locked_by = NULL,
                        locked_at = NULL,
                        last_error = %s,
                        updated_at = now()
                    WHERE job_id = %s
                """,
                    (redacted_error, job_id),
                )
            else:
                # 使用统一的 backoff 计算函数（指数退避: base * 2^(attempts-1)）
                if backoff_seconds is None:
                    backoff_seconds = calculate_backoff_seconds(
                        attempts=attempts,
                        base_seconds=DEFAULT_BACKOFF_BASE,
                        max_seconds=DEFAULT_MAX_BACKOFF,
                    )

                cur.execute(
                    """
                    UPDATE scm.sync_jobs
                    SET
                        status = 'failed',
                        locked_by = NULL,
                        locked_at = NULL,
                        last_error = %s,
                        not_before = now() + (%s || ' seconds')::interval,
                        updated_at = now()
                    WHERE job_id = %s
                """,
                    (redacted_error, backoff_seconds, job_id),
                )

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
            cur.execute(
                """
                UPDATE scm.sync_jobs
                SET
                    status = 'dead',
                    locked_by = NULL,
                    locked_at = NULL,
                    last_error = %s,
                    updated_at = now()
                WHERE job_id = %s AND locked_by = %s AND status = 'running'
                RETURNING job_id
            """,
                (redacted_error, job_id, worker_id),
            )

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
            cur.execute(
                """
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
            """,
                (jitter, redacted_reason, job_id, worker_id),
            )

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
                cur.execute(
                    """
                    UPDATE scm.sync_jobs
                    SET locked_at = now(), lease_seconds = %s, updated_at = now()
                    WHERE job_id = %s AND locked_by = %s AND status = 'running'
                    RETURNING job_id
                """,
                    (lease_seconds, job_id, worker_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE scm.sync_jobs
                    SET locked_at = now(), updated_at = now()
                    WHERE job_id = %s AND locked_by = %s AND status = 'running'
                    RETURNING job_id
                """,
                    (job_id, worker_id),
                )

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
            cur.execute(
                """
                SELECT
                    job_id, repo_id, job_type, mode, payload_json,
                    priority, status, attempts, max_attempts,
                    not_before, locked_by, locked_at, lease_seconds,
                    last_error, last_run_id, created_at, updated_at
                FROM scm.sync_jobs
                WHERE job_id = %s
            """,
                (job_id,),
            )

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
            cur.execute(
                """
                SELECT
                    job_id, repo_id, job_type, mode, priority,
                    status, attempts, max_attempts, locked_by,
                    last_error, created_at, updated_at
                FROM scm.sync_jobs
                WHERE status = %s
                ORDER BY priority ASC, created_at ASC
                LIMIT %s
            """,
                (status, limit),
            )

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
            cur.execute(
                """
                SELECT
                    job_id, repo_id, job_type, mode, priority,
                    status, attempts, locked_at, lease_seconds,
                    created_at
                FROM scm.sync_jobs
                WHERE locked_by = %s AND status = 'running'
                ORDER BY locked_at ASC
            """,
                (worker_id,),
            )

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
            cur.execute(
                """
                DELETE FROM scm.sync_jobs
                WHERE status = 'completed'
                  AND updated_at < now() - (%s || ' days')::interval
            """,
                (older_than_days,),
            )

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

            cur.execute(
                f"""
                UPDATE scm.sync_jobs
                SET
                    status = 'pending',
                    attempts = 0,
                    not_before = now(),
                    last_error = NULL,
                    updated_at = now()
                WHERE {where_clause}
            """,
                params,
            )

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
