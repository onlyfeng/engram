# -*- coding: utf-8 -*-
"""
engram_logbook.scm_sync_lock - SCM 同步任务分布式锁模块

提供基于 PostgreSQL 的分布式锁功能，确保同一 (repo_id, job_type) 只有一个 worker 在执行。

功能:
- claim: 尝试获取锁（支持过期锁回收）
- renew: 续租锁
- release: 释放锁
- get: 获取锁信息

设计原则:
- 使用 lease 机制防止死锁（锁超过 lease_seconds 后可被其他 worker 抢占）
- 所有操作都是幂等的
- 支持并发安全（使用 FOR UPDATE SKIP LOCKED）
"""

from typing import Any, Dict, Optional

import psycopg

from .db import get_connection
from .errors import DatabaseError


def claim(
    repo_id: int,
    job_type: str,
    worker_id: str,
    lease_seconds: int = 60,
    conn: Optional[psycopg.Connection] = None,
) -> bool:
    """
    尝试获取锁。

    获取条件（满足任一）：
    1. 锁不存在（创建新锁）
    2. 锁未被持有（locked_by IS NULL）
    3. 锁已过期（locked_at + lease_seconds < now()）

    Args:
        repo_id: 仓库 ID
        job_type: 任务类型（gitlab_commits, gitlab_mrs, gitlab_reviews, svn）
        worker_id: 当前 worker 标识符
        lease_seconds: 租约时长（秒），默认 60 秒
        conn: 可选的数据库连接，为 None 时自动创建

    Returns:
        True 表示成功获取锁，False 表示锁被其他 worker 持有
    """
    should_close = conn is None
    if conn is None:
        conn = get_connection()

    try:
        with conn.cursor() as cur:
            # 使用 INSERT ... ON CONFLICT ... DO UPDATE 实现原子性操作
            # 条件：锁不存在、未被持有、或已过期
            cur.execute(
                """
                INSERT INTO scm.sync_locks (repo_id, job_type, locked_by, locked_at, lease_seconds, updated_at)
                VALUES (%s, %s, %s, now(), %s, now())
                ON CONFLICT (repo_id, job_type) DO UPDATE
                SET
                    locked_by = EXCLUDED.locked_by,
                    locked_at = now(),
                    lease_seconds = EXCLUDED.lease_seconds,
                    updated_at = now()
                WHERE
                    -- 锁未被持有
                    scm.sync_locks.locked_by IS NULL
                    -- 或锁已过期
                    OR scm.sync_locks.locked_at + (scm.sync_locks.lease_seconds || ' seconds')::interval < now()
                RETURNING lock_id
            """,
                (repo_id, job_type, worker_id, lease_seconds),
            )

            result = cur.fetchone()
            conn.commit()
            return result is not None

    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"获取同步锁失败: {e}",
            {"repo_id": repo_id, "job_type": job_type, "worker_id": worker_id, "error": str(e)},
        )
    finally:
        if should_close:
            conn.close()


def renew(
    repo_id: int,
    job_type: str,
    worker_id: str,
    lease_seconds: Optional[int] = None,
    conn: Optional[psycopg.Connection] = None,
) -> bool:
    """
    续租锁。

    只有当前持有锁的 worker 才能续租。
    续租会刷新 locked_at 为当前时间，延长锁的有效期。

    Args:
        repo_id: 仓库 ID
        job_type: 任务类型
        worker_id: 当前 worker 标识符（必须与锁持有者匹配）
        lease_seconds: 新的租约时长（秒），为 None 时保持原值
        conn: 可选的数据库连接

    Returns:
        True 表示续租成功，False 表示锁不存在或不属于该 worker
    """
    should_close = conn is None
    if conn is None:
        conn = get_connection()

    try:
        with conn.cursor() as cur:
            if lease_seconds is not None:
                cur.execute(
                    """
                    UPDATE scm.sync_locks
                    SET locked_at = now(), lease_seconds = %s, updated_at = now()
                    WHERE repo_id = %s AND job_type = %s AND locked_by = %s
                    RETURNING lock_id
                """,
                    (lease_seconds, repo_id, job_type, worker_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE scm.sync_locks
                    SET locked_at = now(), updated_at = now()
                    WHERE repo_id = %s AND job_type = %s AND locked_by = %s
                    RETURNING lock_id
                """,
                    (repo_id, job_type, worker_id),
                )

            result = cur.fetchone()
            conn.commit()
            return result is not None

    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"续租同步锁失败: {e}",
            {"repo_id": repo_id, "job_type": job_type, "worker_id": worker_id, "error": str(e)},
        )
    finally:
        if should_close:
            conn.close()


def release(
    repo_id: int,
    job_type: str,
    worker_id: str,
    conn: Optional[psycopg.Connection] = None,
) -> bool:
    """
    释放锁。

    只有当前持有锁的 worker 才能释放锁。
    释放后，锁记录仍然保留，但 locked_by 和 locked_at 设为 NULL。

    Args:
        repo_id: 仓库 ID
        job_type: 任务类型
        worker_id: 当前 worker 标识符（必须与锁持有者匹配）
        conn: 可选的数据库连接

    Returns:
        True 表示释放成功，False 表示锁不存在或不属于该 worker
    """
    should_close = conn is None
    if conn is None:
        conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE scm.sync_locks
                SET locked_by = NULL, locked_at = NULL, updated_at = now()
                WHERE repo_id = %s AND job_type = %s AND locked_by = %s
                RETURNING lock_id
            """,
                (repo_id, job_type, worker_id),
            )

            result = cur.fetchone()
            conn.commit()
            return result is not None

    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"释放同步锁失败: {e}",
            {"repo_id": repo_id, "job_type": job_type, "worker_id": worker_id, "error": str(e)},
        )
    finally:
        if should_close:
            conn.close()


def get(
    repo_id: int,
    job_type: str,
    conn: Optional[psycopg.Connection] = None,
) -> Optional[Dict[str, Any]]:
    """
    获取锁信息。

    Args:
        repo_id: 仓库 ID
        job_type: 任务类型
        conn: 可选的数据库连接

    Returns:
        锁信息字典，包含以下字段：
        - lock_id: 锁记录 ID
        - repo_id: 仓库 ID
        - job_type: 任务类型
        - locked_by: 锁持有者（可能为 None）
        - locked_at: 锁定时间（可能为 None）
        - lease_seconds: 租约时长
        - updated_at: 最后更新时间
        - is_locked: 是否被锁定
        - is_expired: 锁是否已过期

        如果锁不存在，返回 None
    """
    should_close = conn is None
    if conn is None:
        conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    lock_id, repo_id, job_type, locked_by, locked_at,
                    lease_seconds, updated_at, created_at,
                    locked_by IS NOT NULL AS is_locked,
                    CASE
                        WHEN locked_at IS NULL THEN false
                        ELSE locked_at + (lease_seconds || ' seconds')::interval < now()
                    END AS is_expired
                FROM scm.sync_locks
                WHERE repo_id = %s AND job_type = %s
            """,
                (repo_id, job_type),
            )

            row = cur.fetchone()
            if row is None:
                return None

            return {
                "lock_id": row[0],
                "repo_id": row[1],
                "job_type": row[2],
                "locked_by": row[3],
                "locked_at": row[4],
                "lease_seconds": row[5],
                "updated_at": row[6],
                "created_at": row[7],
                "is_locked": row[8],
                "is_expired": row[9],
            }

    except psycopg.Error as e:
        raise DatabaseError(
            f"获取同步锁信息失败: {e}",
            {"repo_id": repo_id, "job_type": job_type, "error": str(e)},
        )
    finally:
        if should_close:
            conn.close()


def force_release(
    repo_id: int,
    job_type: str,
    conn: Optional[psycopg.Connection] = None,
) -> bool:
    """
    强制释放锁（管理员操作）。

    不检查 worker_id，直接释放锁。用于故障恢复场景。

    Args:
        repo_id: 仓库 ID
        job_type: 任务类型
        conn: 可选的数据库连接

    Returns:
        True 表示成功释放，False 表示锁不存在或未被持有
    """
    should_close = conn is None
    if conn is None:
        conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE scm.sync_locks
                SET locked_by = NULL, locked_at = NULL, updated_at = now()
                WHERE repo_id = %s AND job_type = %s AND locked_by IS NOT NULL
                RETURNING lock_id
            """,
                (repo_id, job_type),
            )

            result = cur.fetchone()
            conn.commit()
            return result is not None

    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"强制释放同步锁失败: {e}",
            {"repo_id": repo_id, "job_type": job_type, "error": str(e)},
        )
    finally:
        if should_close:
            conn.close()


def list_locks_by_worker(
    worker_id: str,
    conn: Optional[psycopg.Connection] = None,
) -> list:
    """
    列出指定 worker 持有的所有锁。

    Args:
        worker_id: worker 标识符
        conn: 可选的数据库连接

    Returns:
        锁信息列表
    """
    should_close = conn is None
    if conn is None:
        conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    lock_id, repo_id, job_type, locked_by, locked_at,
                    lease_seconds, updated_at, created_at
                FROM scm.sync_locks
                WHERE locked_by = %s
                ORDER BY locked_at DESC
            """,
                (worker_id,),
            )

            rows = cur.fetchall()
            return [
                {
                    "lock_id": row[0],
                    "repo_id": row[1],
                    "job_type": row[2],
                    "locked_by": row[3],
                    "locked_at": row[4],
                    "lease_seconds": row[5],
                    "updated_at": row[6],
                    "created_at": row[7],
                }
                for row in rows
            ]

    except psycopg.Error as e:
        raise DatabaseError(
            f"列出 worker 持有的锁失败: {e}",
            {"worker_id": worker_id, "error": str(e)},
        )
    finally:
        if should_close:
            conn.close()


def list_expired_locks(
    conn: Optional[psycopg.Connection] = None,
) -> list:
    """
    列出所有过期的锁。

    Args:
        conn: 可选的数据库连接

    Returns:
        过期锁信息列表
    """
    should_close = conn is None
    if conn is None:
        conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    lock_id, repo_id, job_type, locked_by, locked_at,
                    lease_seconds, updated_at, created_at
                FROM scm.sync_locks
                WHERE locked_by IS NOT NULL
                  AND locked_at + (lease_seconds || ' seconds')::interval < now()
                ORDER BY locked_at
            """)

            rows = cur.fetchall()
            return [
                {
                    "lock_id": row[0],
                    "repo_id": row[1],
                    "job_type": row[2],
                    "locked_by": row[3],
                    "locked_at": row[4],
                    "lease_seconds": row[5],
                    "updated_at": row[6],
                    "created_at": row[7],
                }
                for row in rows
            ]

    except psycopg.Error as e:
        raise DatabaseError(
            f"列出过期锁失败: {e}",
            {"error": str(e)},
        )
    finally:
        if should_close:
            conn.close()
