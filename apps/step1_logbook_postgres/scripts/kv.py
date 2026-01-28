"""
kv.py - 键值存储操作模块

提供 logbook.kv 表的 JSON 键值存储操作。

唯一键与冲突处理说明：
- logbook.kv: PRIMARY KEY(namespace, key) -> DO UPDATE
"""

import json
import os
from typing import Any, Dict, List, Optional, Tuple

import psycopg


def get_conn(
    dsn: Optional[str] = None,
    autocommit: bool = False,
) -> psycopg.Connection:
    """
    获取数据库连接

    Args:
        dsn: 数据库连接字符串，为 None 时从环境变量 POSTGRES_DSN 读取
        autocommit: 是否启用自动提交模式

    Returns:
        psycopg.Connection 对象

    Raises:
        ConnectionError: 连接失败时抛出
    """
    if dsn is None:
        dsn = os.environ.get("POSTGRES_DSN")
        if not dsn:
            raise ValueError("POSTGRES_DSN 环境变量未设置，且未提供 dsn 参数")

    try:
        return psycopg.connect(dsn, autocommit=autocommit)
    except Exception as e:
        raise ConnectionError(f"数据库连接失败: {e}")


# ============ KV 读写操作 ============


def kv_set_json(
    conn: psycopg.Connection,
    namespace: str,
    key: str,
    value: Any,
) -> bool:
    """
    设置 logbook.kv 中的 JSON 值（upsert）

    唯一键: (namespace, key) PRIMARY KEY
    冲突处理: DO UPDATE - 更新 value_json 和 updated_at

    Args:
        conn: 数据库连接
        namespace: 命名空间（如 'sync', 'config', 'state'）
        key: 键名
        value: 任意可 JSON 序列化的值

    Returns:
        True 表示成功
    """
    value_json = json.dumps(value, ensure_ascii=False, default=str)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO logbook.kv (namespace, key, value_json, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (namespace, key) DO UPDATE
            SET value_json = EXCLUDED.value_json,
                updated_at = now()
            """,
            (namespace, key, value_json),
        )
        return True


def kv_get_json(
    conn: psycopg.Connection,
    namespace: str,
    key: str,
    default: Any = None,
) -> Any:
    """
    获取 logbook.kv 中的 JSON 值

    Args:
        conn: 数据库连接
        namespace: 命名空间
        key: 键名
        default: 不存在时的默认值

    Returns:
        存储的值（已反序列化），不存在返回 default
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT value_json FROM logbook.kv
            WHERE namespace = %s AND key = %s
            """,
            (namespace, key),
        )
        result = cur.fetchone()

        if result is None:
            return default

        # psycopg3 自动将 jsonb 解析为 Python 对象
        return result[0]


def kv_delete(
    conn: psycopg.Connection,
    namespace: str,
    key: str,
) -> bool:
    """
    删除 logbook.kv 中的键值对

    Args:
        conn: 数据库连接
        namespace: 命名空间
        key: 键名

    Returns:
        True 如果删除成功，False 如果键不存在
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM logbook.kv
            WHERE namespace = %s AND key = %s
            """,
            (namespace, key),
        )
        return cur.rowcount > 0


def kv_exists(
    conn: psycopg.Connection,
    namespace: str,
    key: str,
) -> bool:
    """
    检查 logbook.kv 中的键是否存在

    Args:
        conn: 数据库连接
        namespace: 命名空间
        key: 键名

    Returns:
        True 如果存在，False 如果不存在
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM logbook.kv
            WHERE namespace = %s AND key = %s
            """,
            (namespace, key),
        )
        return cur.fetchone() is not None


def kv_list_keys(
    conn: psycopg.Connection,
    namespace: str,
    prefix: Optional[str] = None,
) -> List[str]:
    """
    列出命名空间下的所有键（可按前缀过滤）

    Args:
        conn: 数据库连接
        namespace: 命名空间
        prefix: 键名前缀（可选）

    Returns:
        键名列表
    """
    with conn.cursor() as cur:
        if prefix:
            cur.execute(
                """
                SELECT key FROM logbook.kv
                WHERE namespace = %s AND key LIKE %s
                ORDER BY key
                """,
                (namespace, f"{prefix}%"),
            )
        else:
            cur.execute(
                """
                SELECT key FROM logbook.kv
                WHERE namespace = %s
                ORDER BY key
                """,
                (namespace,),
            )

        return [row[0] for row in cur.fetchall()]


def kv_get_all(
    conn: psycopg.Connection,
    namespace: str,
    prefix: Optional[str] = None,
) -> Dict[str, Any]:
    """
    获取命名空间下的所有键值对

    Args:
        conn: 数据库连接
        namespace: 命名空间
        prefix: 键名前缀（可选）

    Returns:
        键值对字典
    """
    with conn.cursor() as cur:
        if prefix:
            cur.execute(
                """
                SELECT key, value_json FROM logbook.kv
                WHERE namespace = %s AND key LIKE %s
                ORDER BY key
                """,
                (namespace, f"{prefix}%"),
            )
        else:
            cur.execute(
                """
                SELECT key, value_json FROM logbook.kv
                WHERE namespace = %s
                ORDER BY key
                """,
                (namespace,),
            )

        return {row[0]: row[1] for row in cur.fetchall()}


def kv_set_batch(
    conn: psycopg.Connection,
    namespace: str,
    items: Dict[str, Any],
) -> int:
    """
    批量设置键值对

    Args:
        conn: 数据库连接
        namespace: 命名空间
        items: 键值对字典

    Returns:
        设置的键值对数量
    """
    count = 0
    for key, value in items.items():
        kv_set_json(conn, namespace, key, value)
        count += 1
    return count


# ============ 常用命名空间常量 ============

NS_SYNC = "sync"        # 同步状态（如 last_rev, last_commit_ts）
NS_CONFIG = "config"    # 配置项
NS_STATE = "state"      # 运行时状态
NS_CACHE = "cache"      # 缓存数据


# ============ 便捷函数（带命名空间） ============


def sync_get(
    conn: psycopg.Connection,
    key: str,
    default: Any = None,
) -> Any:
    """获取 sync 命名空间下的值"""
    return kv_get_json(conn, NS_SYNC, key, default)


def sync_set(
    conn: psycopg.Connection,
    key: str,
    value: Any,
) -> bool:
    """设置 sync 命名空间下的值"""
    return kv_set_json(conn, NS_SYNC, key, value)


def config_get(
    conn: psycopg.Connection,
    key: str,
    default: Any = None,
) -> Any:
    """获取 config 命名空间下的值"""
    return kv_get_json(conn, NS_CONFIG, key, default)


def config_set(
    conn: psycopg.Connection,
    key: str,
    value: Any,
) -> bool:
    """设置 config 命名空间下的值"""
    return kv_set_json(conn, NS_CONFIG, key, value)
