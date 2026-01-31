"""
engram_logbook.outbox - Outbox 队列操作模块

提供 outbox_memory 表的操作函数，用于 Gateway 写入 OpenMemory 的补偿队列。

状态流转:
    pending -> sent   (写入成功)
    pending -> dead   (重试耗尽)

Lease 协议:
    1. Worker 调用 claim_outbox(worker_id, limit, lease_seconds) 获取任务
    2. 处理成功后调用 ack_sent(outbox_id, worker_id, memory_id)
    3. 可重试失败调用 fail_retry(outbox_id, worker_id, error, next_attempt_at)
    4. 不可恢复失败调用 mark_dead(outbox_id, worker_id, error)
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Union

import psycopg

from .config import Config
from .db import get_connection
from .errors import DatabaseError
from .hashing import sha256


def check_dedup(
    target_space: str,
    payload_sha: str,
    config: Optional[Config] = None,
) -> Optional[Dict[str, Any]]:
    """
    检查是否存在已成功写入的重复记录（幂等去重）

    查询条件：target_space + payload_sha + status='sent'

    Args:
        target_space: 目标空间 (team:<project> / private:<user> / org:shared)
        payload_sha: payload 的 SHA256 哈希
        config: 配置实例

    Returns:
        如果存在已成功写入的记录，返回该记录的字典：
        {
            "outbox_id": int,
            "target_space": str,
            "payload_sha": str,
            "status": str,
            "created_at": datetime,
            "updated_at": datetime,
            "last_error": str | None (可能包含 memory_id=xxx)
        }
        不存在返回 None
    """
    conn = get_connection(config=config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT outbox_id, target_space, payload_sha, status, last_error, created_at, updated_at
                FROM outbox_memory
                WHERE target_space = %s
                  AND payload_sha = %s
                  AND status = 'sent'
                LIMIT 1
                """,
                (target_space, payload_sha),
            )
            row = cur.fetchone()
            if row:
                return {
                    "outbox_id": row[0],
                    "target_space": row[1],
                    "payload_sha": row[2],
                    "status": row[3],
                    "last_error": row[4],  # 可能包含 memory_id=xxx
                    "created_at": row[5],
                    "updated_at": row[6],
                }
            return None
    except psycopg.Error as e:
        raise DatabaseError(
            f"查询 outbox_memory 去重记录失败: {e}",
            {"target_space": target_space, "payload_sha": payload_sha, "error": str(e)},
        )
    finally:
        conn.close()


def enqueue_memory(
    payload_md: Optional[str] = None,
    target_space: Optional[str] = None,
    item_id: Optional[int] = None,
    last_error: Optional[str] = None,
    config: Optional[Config] = None,
    dsn: Optional[str] = None,
    user_id: Optional[str] = None,
    space: Optional[str] = None,
    kind: Optional[str] = None,
    project_key: Optional[str] = None,
) -> int:
    """
    将记忆入队到 outbox_memory 表

    Args:
        payload_md: Markdown 格式的记忆内容
        target_space: 目标空间 (team:<project> / private:<user> / org:shared)
        item_id: 关联的 items.item_id（可选）
        last_error: 最后一次错误信息（可选，用于重入队场景）
        config: 配置实例
        dsn: 数据库 DSN（可选）
        user_id: 用户 ID（可选，用于构建 target_space）
        space: target_space 的别名（可选）
        kind: 证据类型（兼容参数，当前不落库）
        project_key: 项目键（兼容参数，当前不落库）

    Returns:
        创建的 outbox_id
    """
    if payload_md is None:
        raise ValueError("payload_md 不能为空")
    if target_space is None:
        target_space = space or (f"private:{user_id}" if user_id else None)
    if not target_space:
        raise ValueError("target_space 不能为空")

    # 计算 payload 的 SHA256 哈希
    payload_sha = sha256(payload_md)

    conn = get_connection(dsn=dsn, config=config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO outbox_memory
                    (item_id, target_space, payload_md, payload_sha, status, retry_count, last_error)
                VALUES (%s, %s, %s, %s, 'pending', 0, %s)
                RETURNING outbox_id
                """,
                (item_id, target_space, payload_md, payload_sha, last_error),
            )
            result = cur.fetchone()
            conn.commit()
            return result[0]
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"入队 outbox_memory 失败: {e}",
            {"target_space": target_space, "payload_sha": payload_sha, "error": str(e)},
        )
    finally:
        conn.close()


def mark_sent(
    outbox_id: int,
    config: Optional[Config] = None,
) -> bool:
    """
    标记 outbox 记录为已发送 (pending -> sent)

    Args:
        outbox_id: Outbox 记录 ID
        config: 配置实例

    Returns:
        True 表示成功更新
    """
    conn = get_connection(config=config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE outbox_memory
                SET status = 'sent', updated_at = now()
                WHERE outbox_id = %s AND status = 'pending'
                RETURNING outbox_id
                """,
                (outbox_id,),
            )
            result = cur.fetchone()
            conn.commit()
            return result is not None
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"标记 outbox_memory 为 sent 失败: {e}",
            {"outbox_id": outbox_id, "error": str(e)},
        )
    finally:
        conn.close()


def mark_dead(
    outbox_id: int,
    error: str,
    config: Optional[Config] = None,
) -> bool:
    """
    标记 outbox 记录为死信 (pending -> dead)

    Args:
        outbox_id: Outbox 记录 ID
        error: 错误信息
        config: 配置实例

    Returns:
        True 表示成功更新
    """
    conn = get_connection(config=config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE outbox_memory
                SET status = 'dead', last_error = %s, updated_at = now()
                WHERE outbox_id = %s AND status = 'pending'
                RETURNING outbox_id
                """,
                (error, outbox_id),
            )
            result = cur.fetchone()
            conn.commit()
            return result is not None
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"标记 outbox_memory 为 dead 失败: {e}",
            {"outbox_id": outbox_id, "error": str(e)},
        )
    finally:
        conn.close()


def get_pending(
    limit: int = 100,
    config: Optional[Config] = None,
    dsn: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    获取待处理的 outbox 记录 (status = 'pending')

    注意：此函数仅用于查询，不加锁。如需并发安全消费，请使用 claim_pending。

    Args:
        limit: 返回记录数量上限
        config: 配置实例

    Returns:
        pending 状态的 outbox 记录列表
    """
    conn = get_connection(dsn=dsn, config=config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT outbox_id, item_id, target_space, payload_md, payload_sha,
                       status, retry_count, next_attempt_at, last_error, created_at, updated_at
                FROM outbox_memory
                WHERE status = 'pending'
                  AND next_attempt_at <= now()
                ORDER BY next_attempt_at ASC, created_at ASC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()

            results = []
            for row in rows:
                results.append(
                    {
                        "outbox_id": row[0],
                        "item_id": row[1],
                        "target_space": row[2],
                        "payload_md": row[3],
                        "payload_sha": row[4],
                        "status": row[5],
                        "retry_count": row[6],
                        "next_attempt_at": row[7],
                        "last_error": row[8],
                        "created_at": row[9],
                        "updated_at": row[10],
                    }
                )

            return results
    except psycopg.Error as e:
        raise DatabaseError(
            f"获取 pending outbox 记录失败: {e}",
            {"limit": limit, "error": str(e)},
        )
    finally:
        conn.close()


def claim_pending(
    limit: int = 10,
    config: Optional[Config] = None,
) -> List[Dict[str, Any]]:
    """
    并发安全地获取并锁定待处理的 outbox 记录

    使用 FOR UPDATE SKIP LOCKED 保障多消费者并发安全：
    - 已被其他事务锁定的行会被跳过
    - 返回的记录在当前事务中被锁定

    注意：调用方需要在同一事务中处理返回的记录并提交/回滚。

    Args:
        limit: 返回记录数量上限
        config: 配置实例

    Returns:
        pending 状态且已锁定的 outbox 记录列表
    """
    conn = get_connection(config=config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT outbox_id, item_id, target_space, payload_md, payload_sha,
                       status, retry_count, next_attempt_at, last_error, created_at, updated_at
                FROM outbox_memory
                WHERE status = 'pending'
                  AND next_attempt_at <= now()
                ORDER BY next_attempt_at ASC, created_at ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (limit,),
            )
            rows = cur.fetchall()

            results = []
            for row in rows:
                results.append(
                    {
                        "outbox_id": row[0],
                        "item_id": row[1],
                        "target_space": row[2],
                        "payload_md": row[3],
                        "payload_sha": row[4],
                        "status": row[5],
                        "retry_count": row[6],
                        "next_attempt_at": row[7],
                        "last_error": row[8],
                        "created_at": row[9],
                        "updated_at": row[10],
                        "_conn": conn,  # 返回连接以便调用方在同一事务中操作
                    }
                )

            return results
    except psycopg.Error as e:
        conn.close()
        raise DatabaseError(
            f"claim pending outbox 记录失败: {e}",
            {"limit": limit, "error": str(e)},
        )


def increment_retry(
    outbox_id: int,
    error: str,
    backoff_seconds: int = 60,
    config: Optional[Config] = None,
) -> int:
    """
    增加重试计数并记录错误，同时设置下次重试时间

    使用指数退避：next_attempt_at = now() + backoff_seconds * 2^retry_count

    Args:
        outbox_id: Outbox 记录 ID
        error: 本次错误信息
        backoff_seconds: 基础退避秒数（默认60秒）
        config: 配置实例

    Returns:
        更新后的 retry_count
    """
    conn = get_connection(config=config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE outbox_memory
                SET retry_count = retry_count + 1,
                    next_attempt_at = now() + ((%s * power(2, retry_count)) * interval '1 second'),
                    last_error = %s,
                    updated_at = now()
                WHERE outbox_id = %s
                RETURNING retry_count
                """,
                (backoff_seconds, error, outbox_id),
            )
            result = cur.fetchone()
            conn.commit()
            return result[0] if result else 0
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"增加 outbox_memory retry_count 失败: {e}",
            {"outbox_id": outbox_id, "error": str(e)},
        )
    finally:
        conn.close()


def get_by_id(
    outbox_id: int,
    config: Optional[Config] = None,
) -> Optional[Dict[str, Any]]:
    """
    根据 outbox_id 获取单条记录

    Args:
        outbox_id: Outbox 记录 ID
        config: 配置实例

    Returns:
        outbox 记录字典，不存在返回 None
    """
    conn = get_connection(config=config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT outbox_id, item_id, target_space, payload_md, payload_sha,
                       status, retry_count, next_attempt_at, locked_at, locked_by,
                       last_error, created_at, updated_at
                FROM outbox_memory
                WHERE outbox_id = %s
                """,
                (outbox_id,),
            )
            row = cur.fetchone()
            if row:
                return {
                    "outbox_id": row[0],
                    "item_id": row[1],
                    "target_space": row[2],
                    "payload_md": row[3],
                    "payload_sha": row[4],
                    "status": row[5],
                    "retry_count": row[6],
                    "next_attempt_at": row[7],
                    "locked_at": row[8],
                    "locked_by": row[9],
                    "last_error": row[10],
                    "created_at": row[11],
                    "updated_at": row[12],
                }
            return None
    except psycopg.Error as e:
        raise DatabaseError(
            f"获取 outbox_memory 记录失败: {e}",
            {"outbox_id": outbox_id, "error": str(e)},
        )
    finally:
        conn.close()


# ============ Lease 协议函数 ============


def claim_outbox(
    worker_id: str,
    limit: int = 10,
    lease_seconds: int = 60,
    config: Optional[Config] = None,
) -> List[Dict[str, Any]]:
    """
    并发安全地获取并锁定待处理的 outbox 记录（Lease 协议）

    使用 FOR UPDATE SKIP LOCKED 保障多消费者并发安全：
    - 已被其他事务锁定的行会被跳过
    - 选中的记录立即更新 locked_by, locked_at 并 COMMIT
    - 返回记录列表，不暴露数据库连接

    Lease 过期逻辑：
    - locked_at 超过 lease_seconds 的记录视为过期，可被重新 claim
    - 建议 Worker 处理时间 < lease_seconds，否则可能被其他 Worker 抢占

    Args:
        worker_id: Worker 标识符（用于锁定归属验证）
        limit: 返回记录数量上限
        lease_seconds: 租约有效期（秒）
        config: 配置实例

    Returns:
        已锁定的 outbox 记录列表（字典格式）
    """
    conn = get_connection(config=config)
    try:
        with conn.cursor() as cur:
            # 使用 CTE: 先 SELECT FOR UPDATE SKIP LOCKED，再 UPDATE 设置锁定信息
            # 条件：pending 状态 + 到达重试时间 + (未锁定 OR 锁已过期)
            # 注意：PostgreSQL 的 interval 需要使用 make_interval 或乘法实现参数化
            cur.execute(
                """
                WITH candidates AS (
                    SELECT outbox_id
                    FROM outbox_memory
                    WHERE status = 'pending'
                      AND next_attempt_at <= now()
                      AND (locked_at IS NULL OR locked_at < now() - make_interval(secs := %s))
                    ORDER BY next_attempt_at ASC, created_at ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE outbox_memory o
                SET locked_by = %s,
                    locked_at = now(),
                    updated_at = now()
                FROM candidates c
                WHERE o.outbox_id = c.outbox_id
                RETURNING o.outbox_id, o.item_id, o.target_space, o.payload_md, o.payload_sha,
                          o.status, o.retry_count, o.next_attempt_at, o.locked_at, o.locked_by,
                          o.last_error, o.created_at, o.updated_at
                """,
                (float(lease_seconds), limit, worker_id),
            )
            rows = cur.fetchall()
            conn.commit()

            results = []
            for row in rows:
                results.append(
                    {
                        "outbox_id": row[0],
                        "item_id": row[1],
                        "target_space": row[2],
                        "payload_md": row[3],
                        "payload_sha": row[4],
                        "status": row[5],
                        "retry_count": row[6],
                        "next_attempt_at": row[7],
                        "locked_at": row[8],
                        "locked_by": row[9],
                        "last_error": row[10],
                        "created_at": row[11],
                        "updated_at": row[12],
                    }
                )

            return results
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"claim outbox 记录失败: {e}",
            {"worker_id": worker_id, "limit": limit, "error": str(e)},
        )
    finally:
        conn.close()


def ack_sent(
    outbox_id: int,
    worker_id: str,
    memory_id: Optional[str] = None,
    config: Optional[Config] = None,
) -> bool:
    """
    确认 outbox 记录已成功发送 (pending -> sent)

    仅当 locked_by 匹配 worker_id 时才执行更新（防止过期锁被误操作）。

    Args:
        outbox_id: Outbox 记录 ID
        worker_id: Worker 标识符（必须与 claim 时的一致）
        memory_id: 写入 OpenMemory 后返回的 memory_id（可选，记录到 last_error 用于追踪）
        config: 配置实例

    Returns:
        True 表示成功更新，False 表示未更新（可能是锁已被其他 worker 抢占或状态已变更）
    """
    conn = get_connection(config=config)
    try:
        with conn.cursor() as cur:
            # 可选：将 memory_id 记录到某个字段（这里用 last_error 记录成功信息或留空）
            note = f"memory_id={memory_id}" if memory_id else None
            cur.execute(
                """
                UPDATE outbox_memory
                SET status = 'sent',
                    locked_at = NULL,
                    locked_by = NULL,
                    last_error = %s,
                    updated_at = now()
                WHERE outbox_id = %s
                  AND status = 'pending'
                  AND locked_by = %s
                RETURNING outbox_id
                """,
                (note, outbox_id, worker_id),
            )
            result = cur.fetchone()
            conn.commit()
            return result is not None
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"ack_sent outbox_memory 失败: {e}",
            {"outbox_id": outbox_id, "worker_id": worker_id, "error": str(e)},
        )
    finally:
        conn.close()


def fail_retry(
    outbox_id: int,
    worker_id: str,
    error: str,
    next_attempt_at: Union[datetime, str],
    config: Optional[Config] = None,
) -> bool:
    """
    标记 outbox 记录处理失败，安排重试

    仅当 locked_by 匹配 worker_id 时才执行更新。
    增加 retry_count，释放锁，设置下次重试时间。

    注意：退避计算由调用方（Gateway Worker）负责，本函数只接收计算好的 next_attempt_at。

    Args:
        outbox_id: Outbox 记录 ID
        worker_id: Worker 标识符（必须与 claim 时的一致）
        error: 本次失败的错误信息
        next_attempt_at: 下次重试时间（datetime 或 ISO 格式字符串，由调用方计算）
        config: 配置实例

    Returns:
        True 表示成功更新，False 表示未更新
    """
    # 如果是 ISO 字符串，转换为 datetime
    if isinstance(next_attempt_at, str):
        next_attempt_at = datetime.fromisoformat(next_attempt_at)

    conn = get_connection(config=config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE outbox_memory
                SET retry_count = retry_count + 1,
                    next_attempt_at = %s,
                    last_error = %s,
                    locked_at = NULL,
                    locked_by = NULL,
                    updated_at = now()
                WHERE outbox_id = %s
                  AND status = 'pending'
                  AND locked_by = %s
                RETURNING outbox_id
                """,
                (next_attempt_at, error, outbox_id, worker_id),
            )
            result = cur.fetchone()
            conn.commit()
            return result is not None
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"fail_retry outbox_memory 失败: {e}",
            {"outbox_id": outbox_id, "worker_id": worker_id, "error": str(e)},
        )
    finally:
        conn.close()


def mark_dead_by_worker(
    outbox_id: int,
    worker_id: str,
    error: str,
    config: Optional[Config] = None,
) -> bool:
    """
    标记 outbox 记录为死信 (pending -> dead)，带 worker_id 验证

    仅当 locked_by 匹配 worker_id 时才执行更新（Lease 协议）。
    用于不可恢复的错误场景（如重试次数耗尽、数据格式错误等）。

    Args:
        outbox_id: Outbox 记录 ID
        worker_id: Worker 标识符（必须与 claim 时的一致）
        error: 错误信息
        config: 配置实例

    Returns:
        True 表示成功更新，False 表示未更新
    """
    conn = get_connection(config=config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE outbox_memory
                SET status = 'dead',
                    last_error = %s,
                    locked_at = NULL,
                    locked_by = NULL,
                    updated_at = now()
                WHERE outbox_id = %s
                  AND status = 'pending'
                  AND locked_by = %s
                RETURNING outbox_id
                """,
                (error, outbox_id, worker_id),
            )
            result = cur.fetchone()
            conn.commit()
            return result is not None
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"mark_dead outbox_memory 失败: {e}",
            {"outbox_id": outbox_id, "worker_id": worker_id, "error": str(e)},
        )
    finally:
        conn.close()


def renew_lease(
    outbox_id: int,
    worker_id: str,
    config: Optional[Config] = None,
) -> bool:
    """
    续期 Lease 租约

    仅当 status='pending' 且 locked_by 匹配 worker_id 时才执行更新。
    更新 locked_at 和 updated_at 为当前时间，延长租约有效期。

    典型使用场景：
    - 长时间 OpenMemory 调用前续期，防止被其他 Worker 抢占
    - store 调用成功后、ack_sent 前续期，确保 ack 不会因租约过期失败

    Args:
        outbox_id: Outbox 记录 ID
        worker_id: Worker 标识符（必须与 claim 时的一致）
        config: 配置实例

    Returns:
        True 表示成功续期，False 表示未更新（可能是锁已被抢占或状态已变更）
    """
    conn = get_connection(config=config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE outbox_memory
                SET locked_at = now(),
                    updated_at = now()
                WHERE outbox_id = %s
                  AND status = 'pending'
                  AND locked_by = %s
                RETURNING outbox_id
                """,
                (outbox_id, worker_id),
            )
            result = cur.fetchone()
            conn.commit()
            return result is not None
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"renew_lease outbox_memory 失败: {e}",
            {"outbox_id": outbox_id, "worker_id": worker_id, "error": str(e)},
        )
    finally:
        conn.close()


def renew_lease_batch(
    outbox_ids: List[int],
    worker_id: str,
    config: Optional[Config] = None,
) -> int:
    """
    批量续期 Lease 租约

    仅更新 status='pending' 且 locked_by 匹配 worker_id 的记录。

    Args:
        outbox_ids: Outbox 记录 ID 列表
        worker_id: Worker 标识符（必须与 claim 时的一致）
        config: 配置实例

    Returns:
        成功续期的记录数
    """
    if not outbox_ids:
        return 0

    conn = get_connection(config=config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE outbox_memory
                SET locked_at = now(),
                    updated_at = now()
                WHERE outbox_id = ANY(%s)
                  AND status = 'pending'
                  AND locked_by = %s
                """,
                (outbox_ids, worker_id),
            )
            count = cur.rowcount
            conn.commit()
            return count
    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"renew_lease_batch outbox_memory 失败: {e}",
            {"outbox_ids": outbox_ids, "worker_id": worker_id, "error": str(e)},
        )
    finally:
        conn.close()
