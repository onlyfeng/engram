#!/usr/bin/env python3
"""
active_collection.py - Active Collection 管理模块

提供统一的 collection 管理功能，包括：
- logbook.kv 的 namespace/key 生成
- active collection 的读取/设置
- collection_id 解析（优先级：explicit > active > default）

该模块被 seek_indexer.py 和 seek_query.py 共同使用，确保一致性。
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from step3_seekdb_rag_hybrid.step3_chunking import CHUNKING_VERSION
from step3_seekdb_rag_hybrid.collection_naming import make_collection_id

logger = logging.getLogger(__name__)


# ============ Constants ============


# logbook.kv 的 namespace 前缀
KV_NAMESPACE_PREFIX = "seekdb.sync"

# 用于存储 active collection 的 key
ACTIVE_COLLECTION_KEY = "active_collection"


# ============ SQL ============


SQL_GET_KV_VALUE = """
-- 从 logbook.kv 获取值
SELECT value_json FROM logbook.kv
WHERE namespace = :namespace AND key = :key;
"""

SQL_SET_KV_VALUE = """
-- 设置 logbook.kv 的值
INSERT INTO logbook.kv (namespace, key, value_json)
VALUES (:namespace, :key, :value_json)
ON CONFLICT (namespace, key)
DO UPDATE SET value_json = EXCLUDED.value_json, updated_at = NOW();
"""


# ============ Namespace/Key 生成 ============


def make_kv_namespace(
    backend_name: Optional[str] = None,
    collection_id: Optional[str] = None,
) -> str:
    """
    生成 logbook.kv 的 namespace
    
    格式: seekdb.sync:{backend}:{collection_id}
    这样不同 backend 和 collection 的游标完全隔离
    
    Args:
        backend_name: 索引后端名称（如 seekdb/pgvector）
        collection_id: collection 标识（canonical 冒号格式）
    
    Returns:
        namespace 字符串
    
    Examples:
        >>> make_kv_namespace("seekdb", "proj1:v2:bge-m3")
        'seekdb.sync:seekdb:proj1:v2:bge-m3'
        >>> make_kv_namespace("pgvector")
        'seekdb.sync:pgvector'
        >>> make_kv_namespace()
        'seekdb.sync'
    """
    parts = [KV_NAMESPACE_PREFIX]
    if backend_name:
        parts.append(backend_name)
    if collection_id:
        parts.append(collection_id)
    return ":".join(parts)


def make_active_collection_key(project_key: Optional[str] = None) -> str:
    """
    生成 active collection 的 key
    
    Args:
        project_key: 项目标识
    
    Returns:
        key 字符串
    
    Examples:
        >>> make_active_collection_key("webapp")
        'active_collection:webapp'
        >>> make_active_collection_key()
        'active_collection:default'
    """
    return f"{ACTIVE_COLLECTION_KEY}:{project_key or 'default'}"


# ============ Active Collection 读写 ============


def get_active_collection(
    conn,
    backend_name: str,
    project_key: Optional[str] = None,
) -> Optional[str]:
    """
    从 logbook.kv 获取当前 active collection 名称
    
    Args:
        conn: 数据库连接（psycopg.Connection）
        backend_name: 索引后端名称（如 seekdb/pgvector）
        project_key: 项目标识
    
    Returns:
        active collection 名称（canonical 冒号格式），不存在则返回 None
    """
    from psycopg.rows import dict_row
    
    # active collection 存储在基础 namespace 下（不含 collection_id）
    namespace = make_kv_namespace(backend_name)
    key = make_active_collection_key(project_key)
    
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(SQL_GET_KV_VALUE, {"namespace": namespace, "key": key})
            row = cur.fetchone()
            
            if not row or not row.get("value_json"):
                return None
            
            data = row["value_json"] if isinstance(row["value_json"], dict) else json.loads(row["value_json"])
            return data.get("collection")
    except Exception as e:
        logger.warning(f"读取 active_collection 失败: {e}")
        return None


def set_active_collection(
    conn,
    backend_name: str,
    collection_id: str,
    project_key: Optional[str] = None,
) -> None:
    """
    设置 active collection（切换别名）
    
    Args:
        conn: 数据库连接（psycopg.Connection）
        backend_name: 索引后端名称
        collection_id: 要激活的 collection 名称（canonical 冒号格式）
        project_key: 项目标识
    """
    namespace = make_kv_namespace(backend_name)
    key = make_active_collection_key(project_key)
    
    value_json = json.dumps({
        "collection": collection_id,
        "activated_at": datetime.now(timezone.utc).isoformat(),
    })
    
    with conn.cursor() as cur:
        cur.execute(SQL_SET_KV_VALUE, {
            "namespace": namespace,
            "key": key,
            "value_json": value_json,
        })
    
    logger.info(f"切换 active collection: {collection_id} (backend={backend_name}, project={project_key or 'default'})")


# ============ Collection ID 解析 ============


def get_default_collection_id(
    project_key: Optional[str] = None,
    embedding_model_id: Optional[str] = None,
    chunking_version: str = CHUNKING_VERSION,
) -> str:
    """
    生成默认的 collection ID
    
    当 active_collection 不存在时使用。
    
    Args:
        project_key: 项目标识
        embedding_model_id: Embedding 模型 ID
        chunking_version: 分块版本
    
    Returns:
        默认的 collection ID（canonical 冒号格式）
    """
    return make_collection_id(
        project_key=project_key,
        chunking_version=chunking_version,
        embedding_model_id=embedding_model_id,
    )


def resolve_collection_id(
    conn=None,
    backend_name: Optional[str] = None,
    project_key: Optional[str] = None,
    embedding_model_id: Optional[str] = None,
    explicit_collection_id: Optional[str] = None,
    chunking_version: str = CHUNKING_VERSION,
) -> str:
    """
    解析要使用的 collection ID
    
    优先级:
    1. 显式指定的 collection（explicit_collection_id）
    2. logbook.kv 中的 active_collection
    3. 使用默认命名规则生成
    
    Args:
        conn: 数据库连接（可选，用于读取 active_collection）
        backend_name: 索引后端名称
        project_key: 项目标识
        embedding_model_id: Embedding 模型 ID
        explicit_collection_id: 显式指定的 collection ID
        chunking_version: 分块版本
    
    Returns:
        要使用的 collection ID（canonical 冒号格式）
    
    Examples:
        # explicit 优先
        >>> resolve_collection_id(explicit_collection_id="custom:v1:bge-m3")
        'custom:v1:bge-m3'
        
        # active_collection 次之（需要 conn 和 backend_name）
        # >>> resolve_collection_id(conn=conn, backend_name="seekdb")
        # 'webapp:v1:bge-m3'  # 从 logbook.kv 读取
        
        # 默认命名
        >>> resolve_collection_id(project_key="webapp", embedding_model_id="bge-m3")
        'webapp:v1:bge-m3'
    """
    # 1. 优先使用显式指定的 collection
    if explicit_collection_id:
        logger.debug(f"使用显式指定的 collection: {explicit_collection_id}")
        return explicit_collection_id
    
    # 2. 尝试从 logbook.kv 读取 active_collection
    if conn is not None and backend_name:
        active = get_active_collection(conn, backend_name, project_key)
        if active:
            logger.info(f"使用 active_collection: {active}")
            return active
    
    # 3. 回退到默认命名
    default = get_default_collection_id(project_key, embedding_model_id, chunking_version)
    logger.debug(f"使用默认 collection: {default}")
    return default
