#!/usr/bin/env python3
"""
seek_indexer.py - Step3 索引同步工具

从 Step1 数据库读取 patch_blobs/attachments，执行分块并同步到索引后端。

功能：
1. 增量同步 - 基于游标从 Step1 读取新数据
2. 全量重建 - 重新索引所有数据
3. 单记录索引 - 指定 blob_id/attachment_id 进行索引

输入参数：
    --mode: 同步模式（incremental/full/single）
    --source: 数据源（patch_blobs/attachments/all）
    --blob-id: 指定 blob_id（single 模式）
    --attachment-id: 指定 attachment_id（single 模式）
    --batch-size: 每批处理数量
    --dry-run: 仅预览，不实际写入索引

输出：
    - JSON 格式的同步报告
    - 支持 --json 输出便于流水线解析

使用:
    # Makefile 入口（推荐）
    make step3-index                                    # 增量同步（默认）
    make step3-index INDEX_MODE=full                    # 全量重建
    make step3-index BLOB_ID=12345 INDEX_MODE=single    # 单记录索引
    make step3-index JSON_OUTPUT=1                      # JSON 输出
    make step3-index INDEX_MODE=full DRY_RUN=1          # 预览模式

    # 直接调用（在 apps/step3_seekdb_rag_hybrid 目录下）
    python -m seek_indexer --mode incremental
    python -m seek_indexer --mode full --source patch_blobs
    python -m seek_indexer --mode single --blob-id 12345
    python -m seek_indexer --mode incremental --json
    python -m seek_indexer --mode full --dry-run --json
"""

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.rows import dict_row

# 导入 Step1 模块
from engram_step1.config import add_config_argument, get_config
from engram_step1.db import get_connection
from engram_step1.errors import DatabaseError, EngramError

# 导入 Step3 模块
from step3_seekdb_rag_hybrid.step3_chunking import (
    CHUNKING_VERSION,
    ChunkResult,
    chunk_content,
)
from step3_seekdb_rag_hybrid.step3_readers import (
    read_evidence_text,
    EvidenceReadError,
    EvidenceNotFoundError,
)
from step3_seekdb_rag_hybrid.index_backend import (
    IndexBackend,
    ChunkDoc,
)
from step3_seekdb_rag_hybrid.embedding_provider import (
    EmbeddingProvider,
    EmbeddingModelInfo,
    EmbeddingError,
    EmbeddingModelMismatchError,
    get_embedding_provider,
    set_embedding_provider,
    check_embedding_consistency,
)
from step3_seekdb_rag_hybrid.step3_backend_factory import (
    add_backend_arguments,
    create_backend_from_args,
    create_backend_from_env,
    create_dual_write_backends,
    create_shadow_backend,
    get_backend_info,
    get_dual_write_config,
    validate_backend_config,
    BackendType,
    DualWriteConfig,
    PGVectorConfig,
)
from step3_seekdb_rag_hybrid.collection_naming import (
    make_collection_id,
    parse_collection_id,
    make_version_tag,
    # [DEPRECATED] 兼容别名，请勿在新代码中使用
    # make_collection_name,  # -> 使用 make_collection_id
    # parse_collection_name,  # -> 使用 parse_collection_id
)
from step3_seekdb_rag_hybrid.active_collection import (
    make_kv_namespace,
    get_active_collection,
    set_active_collection,
    resolve_collection_id,
    KV_NAMESPACE_PREFIX,
    ACTIVE_COLLECTION_KEY,
)
from step3_seekdb_rag_hybrid.env_compat import get_bool

# 环境变量：是否自动初始化 pgvector 后端（默认开启）
# canonical: STEP3_PGVECTOR_AUTO_INIT，别名: STEP3_AUTO_INIT（已废弃，计划于 2026-Q3 移除）
# 布尔解析规则：支持 1/0/true/false/yes/no（不区分大小写）
PGVECTOR_AUTO_INIT = get_bool(
    "STEP3_PGVECTOR_AUTO_INIT",
    deprecated_aliases=["STEP3_AUTO_INIT"],
    default=True,
)

# 环境变量：是否启用双写（默认关闭）
PGVECTOR_DUAL_WRITE = os.environ.get("STEP3_PGVECTOR_DUAL_WRITE", "0").lower() in ("1", "true", "yes")

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ============ 数据结构 ============


@dataclass
class DualWriteStats:
    """双写统计信息"""
    enabled: bool = False                        # 是否启用双写
    shadow_strategy: str = ""                    # shadow 后端策略
    dry_run: bool = False                        # shadow 是否为 dry-run 模式
    
    # 统计
    shadow_indexed: int = 0                      # shadow 成功写入数
    shadow_errors: int = 0                       # shadow 写入失败数
    shadow_error_records: List[Dict[str, Any]] = field(default_factory=list)  # 失败记录
    
    def add_error(self, chunk_id: str, error: str, max_errors: int = 50):
        """记录 shadow 写入错误"""
        self.shadow_errors += 1
        if len(self.shadow_error_records) < max_errors:
            self.shadow_error_records.append({
                "chunk_id": chunk_id,
                "error": str(error)[:200],  # 限制错误消息长度
            })
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "enabled": self.enabled,
            "shadow_strategy": self.shadow_strategy,
            "dry_run": self.dry_run,
            "shadow_indexed": self.shadow_indexed,
            "shadow_errors": self.shadow_errors,
            "shadow_error_records": self.shadow_error_records,
        }


@dataclass
class IndexResult:
    """索引操作结果"""
    # 统计信息
    total_processed: int = 0
    total_chunks: int = 0
    total_indexed: int = 0
    total_errors: int = 0
    
    # 详细信息
    processed_blob_ids: List[int] = field(default_factory=list)
    processed_attachment_ids: List[int] = field(default_factory=list)
    error_records: List[Dict[str, Any]] = field(default_factory=list)
    
    # 参数
    mode: str = "incremental"
    source: str = "all"
    batch_size: int = 100
    dry_run: bool = False
    
    # Collection/版本信息
    collection: Optional[str] = None
    version_tag: Optional[str] = None
    activated: bool = False
    
    # 时间信息
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_seconds: float = 0.0
    
    # 游标信息
    last_blob_id: int = 0
    last_attachment_id: int = 0
    
    # Embedding 模型信息（用于自检与回滚）
    embedding_model_id: Optional[str] = None
    embedding_dim: Optional[int] = None
    embedding_normalize: bool = True
    
    # 双写统计
    dual_write: Optional[DualWriteStats] = None

    def add_error(self, record_type: str, record_id: int, error: str, max_errors: int = 50):
        """添加错误记录"""
        self.total_errors += 1
        if len(self.error_records) < max_errors:
            self.error_records.append({
                "type": record_type,
                "id": record_id,
                "error": error,
            })

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "success": self.total_errors == 0,
            "summary": {
                "total_processed": self.total_processed,
                "total_chunks": self.total_chunks,
                "total_indexed": self.total_indexed,
                "total_errors": self.total_errors,
            },
            "parameters": {
                "mode": self.mode,
                "source": self.source,
                "batch_size": self.batch_size,
                "dry_run": self.dry_run,
                "chunking_version": CHUNKING_VERSION,
            },
            "collection": {
                "name": self.collection,
                "version_tag": self.version_tag,
                "activated": self.activated,
            },
            "embedding": {
                "model_id": self.embedding_model_id,
                "dim": self.embedding_dim,
                "normalize": self.embedding_normalize,
            },
            "cursors": {
                "last_blob_id": self.last_blob_id,
                "last_attachment_id": self.last_attachment_id,
            },
            "timing": {
                "started_at": self.started_at,
                "completed_at": self.completed_at,
                "duration_seconds": self.duration_seconds,
            },
            "details": {
                "processed_blob_ids": self.processed_blob_ids[:100],  # 最多显示 100 个
                "processed_attachment_ids": self.processed_attachment_ids[:100],
                "errors": self.error_records,
            },
        }
        
        # 添加双写统计（如果启用）
        if self.dual_write is not None:
            result["dual_write"] = self.dual_write.to_dict()
        
        return result


# ============ SQL 查询 ============


SQL_FETCH_PATCH_BLOBS = """
-- 增量读取 scm.patch_blobs，包含提交元信息
SELECT
    pb.blob_id,
    pb.source_type,
    pb.source_id,
    pb.uri,
    pb.sha256,
    pb.size_bytes,
    pb.format,
    pb.chunking_version,
    pb.created_at,
    -- 关联仓库信息
    r.repo_id,
    r.project_key,
    r.repo_type,
    r.url AS repo_url,
    -- 根据 source_type 关联获取 author/ts/message
    CASE
        WHEN pb.source_type = 'svn' THEN sv.author_raw
        WHEN pb.source_type = 'git' THEN gc.author_raw
    END AS author_raw,
    CASE
        WHEN pb.source_type = 'svn' THEN sv.ts
        WHEN pb.source_type = 'git' THEN gc.ts
    END AS commit_ts,
    CASE
        WHEN pb.source_type = 'svn' THEN sv.message
        WHEN pb.source_type = 'git' THEN gc.message
    END AS commit_message,
    CASE
        WHEN pb.source_type = 'svn' THEN sv.is_bulk
        WHEN pb.source_type = 'git' THEN gc.is_bulk
    END AS is_bulk,
    -- meta_json 包含解析后的 user_id 等信息
    CASE
        WHEN pb.source_type = 'svn' THEN sv.meta_json
        WHEN pb.source_type = 'git' THEN gc.meta_json
    END AS commit_meta_json
FROM scm.patch_blobs pb
JOIN scm.repos r ON r.repo_id = CAST(split_part(pb.source_id, ':', 1) AS bigint)
LEFT JOIN scm.svn_revisions sv
    ON pb.source_type = 'svn'
    AND sv.repo_id = r.repo_id
    AND COALESCE(sv.rev_num, sv.rev_id) = CAST(split_part(pb.source_id, ':', 2) AS bigint)
LEFT JOIN scm.git_commits gc
    ON pb.source_type = 'git'
    AND gc.repo_id = r.repo_id
    AND COALESCE(gc.commit_sha, gc.commit_id) = split_part(pb.source_id, ':', 2)
WHERE pb.blob_id > :last_blob_id
  AND (:project_key IS NULL OR r.project_key = :project_key)
ORDER BY pb.blob_id
LIMIT :batch_size;
"""

SQL_FETCH_SINGLE_BLOB = """
-- 获取单个 patch_blob，包含提交元信息
SELECT
    pb.blob_id,
    pb.source_type,
    pb.source_id,
    pb.uri,
    pb.sha256,
    pb.size_bytes,
    pb.format,
    pb.chunking_version,
    pb.created_at,
    -- 关联仓库信息
    r.repo_id,
    r.project_key,
    r.repo_type,
    r.url AS repo_url,
    -- 根据 source_type 关联获取 author/ts/message
    CASE
        WHEN pb.source_type = 'svn' THEN sv.author_raw
        WHEN pb.source_type = 'git' THEN gc.author_raw
    END AS author_raw,
    CASE
        WHEN pb.source_type = 'svn' THEN sv.ts
        WHEN pb.source_type = 'git' THEN gc.ts
    END AS commit_ts,
    CASE
        WHEN pb.source_type = 'svn' THEN sv.message
        WHEN pb.source_type = 'git' THEN gc.message
    END AS commit_message,
    CASE
        WHEN pb.source_type = 'svn' THEN sv.is_bulk
        WHEN pb.source_type = 'git' THEN gc.is_bulk
    END AS is_bulk,
    -- meta_json 包含解析后的 user_id 等信息
    CASE
        WHEN pb.source_type = 'svn' THEN sv.meta_json
        WHEN pb.source_type = 'git' THEN gc.meta_json
    END AS commit_meta_json
FROM scm.patch_blobs pb
JOIN scm.repos r ON r.repo_id = CAST(split_part(pb.source_id, ':', 1) AS bigint)
LEFT JOIN scm.svn_revisions sv
    ON pb.source_type = 'svn'
    AND sv.repo_id = r.repo_id
    AND COALESCE(sv.rev_num, sv.rev_id) = CAST(split_part(pb.source_id, ':', 2) AS bigint)
LEFT JOIN scm.git_commits gc
    ON pb.source_type = 'git'
    AND gc.repo_id = r.repo_id
    AND COALESCE(gc.commit_sha, gc.commit_id) = split_part(pb.source_id, ':', 2)
WHERE pb.blob_id = :blob_id;
"""

SQL_UPDATE_CHUNKING_VERSION = """
-- 更新 chunking_version 标记
UPDATE scm.patch_blobs
SET chunking_version = :chunking_version
WHERE blob_id = :blob_id;
"""

SQL_GET_CURSOR = """
-- 获取同步游标
SELECT value_json FROM logbook.kv
WHERE namespace = :namespace AND key = :key;
"""

SQL_SET_CURSOR = """
-- 设置同步游标
INSERT INTO logbook.kv (namespace, key, value_json)
VALUES (:namespace, :key, :value_json)
ON CONFLICT (namespace, key)
DO UPDATE SET value_json = EXCLUDED.value_json, updated_at = NOW();
"""

SQL_FETCH_ATTACHMENTS = """
-- 增量读取 logbook.attachments
SELECT
    a.attachment_id,
    a.item_id,
    a.kind,
    a.uri,
    a.sha256,
    a.size_bytes,
    a.meta_json,
    a.created_at,
    -- 关联 logbook.items 获取上下文
    i.item_type,
    i.title,
    i.scope_json,
    i.owner_user_id,
    i.status
FROM logbook.attachments a
JOIN logbook.items i ON i.item_id = a.item_id
WHERE a.attachment_id > :last_attachment_id
  AND (:project_key IS NULL OR (a.meta_json->>'project_key') = :project_key 
       OR (i.scope_json->>'project_key') = :project_key)
ORDER BY a.attachment_id
LIMIT :batch_size;
"""

SQL_FETCH_SINGLE_ATTACHMENT = """
-- 获取单个 attachment
SELECT
    a.attachment_id,
    a.item_id,
    a.kind,
    a.uri,
    a.sha256,
    a.size_bytes,
    a.meta_json,
    a.created_at,
    -- 关联 logbook.items 获取上下文
    i.item_type,
    i.title,
    i.scope_json,
    i.owner_user_id,
    i.status
FROM logbook.attachments a
JOIN logbook.items i ON i.item_id = a.item_id
WHERE a.attachment_id = :attachment_id;
"""


# ============ Collection/Version 命名 ============
# 
# 已迁移到 collection_naming.py 模块：
# - make_collection_id / make_collection_name: 生成 collection 名称
# - parse_collection_id / parse_collection_name: 解析 collection 名称
# - make_version_tag: 生成版本标签
# - to_seekdb_collection_name: 转换为 SeekDB 名称
# - to_pgvector_table_name: 转换为 PGVector 表名


# ============ 游标管理 ============


def make_cursor_key(source: str) -> str:
    """
    生成游标 key
    
    namespace 已经包含 backend + collection 信息，
    因此 key 只需要区分数据源即可
    
    Args:
        source: 数据源（patch_blobs/attachments）
    
    Returns:
        游标 key
    """
    return f"cursor:{source}"


@dataclass
class SyncCursorData:
    """同步游标数据"""
    last_id: int = 0
    chunking_version: Optional[str] = None
    embedding_info: Optional[EmbeddingModelInfo] = None
    updated_at: Optional[str] = None


def get_sync_cursor(
    conn: psycopg.Connection,
    source: str,
    namespace: str,
) -> int:
    """获取同步游标（仅返回 last_id）"""
    cursor_data = get_sync_cursor_full(conn, source, namespace)
    return cursor_data.last_id


def get_sync_cursor_full(
    conn: psycopg.Connection,
    source: str,
    namespace: str,
) -> SyncCursorData:
    """
    获取完整的同步游标数据
    
    Args:
        conn: 数据库连接
        source: 数据源（patch_blobs/attachments）
        namespace: logbook.kv 的 namespace（包含 backend+collection）
    
    Returns:
        SyncCursorData 包含 last_id、chunking_version、embedding_info 等
    """
    cursor_key = make_cursor_key(source)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(SQL_GET_CURSOR, {"namespace": namespace, "key": cursor_key})
        row = cur.fetchone()
        
        if not row or not row.get("value_json"):
            return SyncCursorData()
        
        data = row["value_json"] if isinstance(row["value_json"], dict) else json.loads(row["value_json"])
        
        # 解析 embedding 信息
        embedding_info = None
        if "embedding" in data and isinstance(data["embedding"], dict):
            embedding_info = EmbeddingModelInfo.from_dict(data["embedding"])
        
        return SyncCursorData(
            last_id=data.get("last_id", 0),
            chunking_version=data.get("chunking_version"),
            embedding_info=embedding_info,
            updated_at=data.get("updated_at"),
        )


def update_sync_cursor(
    conn: psycopg.Connection,
    source: str,
    namespace: str,
    last_id: int,
    embedding_info: Optional[EmbeddingModelInfo] = None,
) -> None:
    """
    更新同步游标
    
    Args:
        conn: 数据库连接
        source: 数据源（patch_blobs/attachments）
        namespace: logbook.kv 的 namespace（包含 backend+collection）
        last_id: 最后处理的 ID
        embedding_info: Embedding 模型信息（用于自检与回滚）
    """
    cursor_key = make_cursor_key(source)
    cursor_data = {
        "last_id": last_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "chunking_version": CHUNKING_VERSION,
    }
    
    # 写入 embedding 模型信息
    if embedding_info:
        cursor_data["embedding"] = embedding_info.to_dict()
    
    value_json = json.dumps(cursor_data)
    with conn.cursor() as cur:
        cur.execute(SQL_SET_CURSOR, {
            "namespace": namespace,
            "key": cursor_key,
            "value_json": value_json,
        })


# ============ pgvector 初始化 ============


def try_initialize_pgvector_backend(backend: Optional[IndexBackend]) -> bool:
    """
    尝试初始化 pgvector 后端（创建扩展、表结构等）
    
    仅当后端为 pgvector 且 STEP3_PGVECTOR_AUTO_INIT=1 时执行。
    
    Args:
        backend: 索引后端实例
    
    Returns:
        是否初始化成功（非 pgvector 后端返回 True）
    """
    if backend is None:
        return True
    
    # 检查是否为 pgvector 后端
    is_pgvector = (
        getattr(backend, 'backend_name', '') == 'pgvector'
        or hasattr(backend, 'initialize')
    )
    
    if not is_pgvector:
        return True
    
    # 检查环境变量开关
    if not PGVECTOR_AUTO_INIT:
        logger.debug("STEP3_PGVECTOR_AUTO_INIT=0，跳过 pgvector 自动初始化")
        return True
    
    # 尝试初始化
    try:
        if hasattr(backend, 'initialize'):
            logger.info("正在初始化 pgvector 后端（创建扩展和表结构）...")
            backend.initialize()
            logger.info("pgvector 后端初始化成功")
        return True
    except Exception as e:
        error_msg = str(e).lower()
        
        # 提供可操作的错误提示
        if 'extension' in error_msg or 'pgvector' in error_msg:
            logger.error(
                f"pgvector 初始化失败: {e}\n"
                f"可能原因: PostgreSQL 未安装 pgvector 扩展\n"
                f"解决方案:\n"
                f"  1. 安装 pgvector: CREATE EXTENSION IF NOT EXISTS vector;\n"
                f"  2. 或联系 DBA 安装 pgvector 扩展\n"
                f"  3. 若不需要自动初始化，设置 STEP3_PGVECTOR_AUTO_INIT=0"
            )
        elif 'permission' in error_msg or 'denied' in error_msg:
            logger.error(
                f"pgvector 初始化失败: {e}\n"
                f"可能原因: 数据库用户权限不足\n"
                f"解决方案:\n"
                f"  1. 授予用户 CREATE 权限\n"
                f"  2. 或联系 DBA 手动创建表结构\n"
                f"  3. 若不需要自动初始化，设置 STEP3_PGVECTOR_AUTO_INIT=0"
            )
        elif 'connection' in error_msg or 'connect' in error_msg:
            logger.error(
                f"pgvector 初始化失败: {e}\n"
                f"可能原因: 无法连接到 pgvector 数据库\n"
                f"解决方案:\n"
                f"  1. 检查 STEP3_PGVECTOR_DSN 环境变量配置\n"
                f"  2. 确认数据库服务正常运行\n"
                f"  3. 若不需要自动初始化，设置 STEP3_PGVECTOR_AUTO_INIT=0"
            )
        else:
            logger.error(
                f"pgvector 初始化失败: {e}\n"
                f"若不需要自动初始化，设置 STEP3_PGVECTOR_AUTO_INIT=0"
            )
        
        return False


# ============ 索引函数 ============


# 全局索引后端实例（可通过 set_index_backend 设置）
_index_backend: Optional[IndexBackend] = None


def set_index_backend(backend: Optional[IndexBackend]) -> None:
    """设置全局索引后端实例"""
    global _index_backend
    _index_backend = backend


def get_index_backend() -> Optional[IndexBackend]:
    """获取全局索引后端实例"""
    return _index_backend


def read_artifact_content(
    uri: str,
    conn: Optional[psycopg.Connection] = None,
    config=None,
) -> Optional[str]:
    """
    读取 artifact 内容
    
    使用 step3_readers.read_evidence_text 统一读取各类 URI
    支持 artifact key、artifact://、memory:// 等格式
    
    Args:
        uri: Evidence URI 或 artifact key
        conn: 数据库连接（memory:// URI 需要）
        config: Step1 配置实例
    
    Returns:
        文本内容，读取失败返回 None
    """
    try:
        result = read_evidence_text(uri, conn=conn, config=config)
        return result.text
    except EvidenceNotFoundError as e:
        logger.warning(f"Evidence 未找到: {uri}, 详情: {e.details}")
        return None
    except EvidenceReadError as e:
        logger.warning(f"读取 Evidence 失败: {uri}, 错误: {e}")
        return None
    except Exception as e:
        logger.warning(f"读取 artifact 失败: {uri}, 错误: {e}")
        return None


def chunk_result_to_chunk_doc(chunk: ChunkResult) -> ChunkDoc:
    """
    将 ChunkResult 转换为 ChunkDoc（用于 IndexBackend）
    
    Args:
        chunk: 分块结果
    
    Returns:
        ChunkDoc 实例
    """
    metadata = chunk.metadata or {}
    
    # 提取 commit_ts 并转为 ISO 字符串
    commit_ts = metadata.get("commit_ts")
    if commit_ts and hasattr(commit_ts, "isoformat"):
        commit_ts = commit_ts.isoformat()
    elif commit_ts and not isinstance(commit_ts, str):
        commit_ts = str(commit_ts)
    
    return ChunkDoc(
        chunk_id=chunk.chunk_id,
        content=chunk.content,
        project_key=metadata.get("project_key", ""),
        module=metadata.get("module", ""),
        source_type=chunk.source_type,
        source_id=chunk.source_id,
        owner_user_id=metadata.get("owner_user_id", ""),
        commit_ts=commit_ts,
        artifact_uri=chunk.artifact_uri,
        sha256=chunk.sha256,
        chunk_idx=chunk.chunk_idx,
        excerpt=chunk.excerpt,
        metadata=metadata,
    )


# 全局 Embedding Provider 实例
_embedding_provider: Optional[EmbeddingProvider] = None


def set_embedding_provider_instance(provider: Optional[EmbeddingProvider]) -> None:
    """设置全局 Embedding Provider 实例"""
    global _embedding_provider
    _embedding_provider = provider
    # 同时设置 embedding_provider 模块的全局实例
    set_embedding_provider(provider)


def get_embedding_provider_instance() -> Optional[EmbeddingProvider]:
    """获取全局 Embedding Provider 实例"""
    global _embedding_provider
    if _embedding_provider is None:
        try:
            _embedding_provider = get_embedding_provider()
        except EmbeddingError:
            pass
    return _embedding_provider


def upsert_to_index(
    chunks: List[ChunkResult],
    dry_run: bool = False,
    backend: Optional[IndexBackend] = None,
    embedding_provider: Optional[EmbeddingProvider] = None,
    shadow_backend: Optional[IndexBackend] = None,
    dual_write_config: Optional[DualWriteConfig] = None,
    dual_write_stats: Optional[DualWriteStats] = None,
) -> int:
    """
    将分块 upsert 到索引后端（支持双写）
    
    Args:
        chunks: 分块列表
        dry_run: 如果为 True，仅返回数量不实际写入
        backend: 主索引后端实例（可选，不提供则使用全局实例）
        embedding_provider: Embedding Provider（可选，用于生成向量）
        shadow_backend: Shadow 后端实例（可选，用于双写）
        dual_write_config: 双写配置
        dual_write_stats: 双写统计对象（将被更新）
    
    Returns:
        成功索引的数量（primary 后端）
    """
    if dry_run:
        logger.info(f"[DRY-RUN] 将索引 {len(chunks)} 个分块")
        return len(chunks)
    
    # 获取后端实例
    index_backend = backend or get_index_backend()
    
    # 转换为 ChunkDoc
    docs = [chunk_result_to_chunk_doc(chunk) for chunk in chunks]
    
    # 获取 embedding provider
    provider = embedding_provider or get_embedding_provider_instance()
    
    # 如果有 provider，生成向量
    if provider is not None:
        try:
            texts = [doc.content for doc in docs]
            vectors = provider.embed_texts(texts)
            for doc, vector in zip(docs, vectors):
                doc.vector = vector
            logger.debug(f"生成 {len(vectors)} 个向量 (model={provider.model_id}, dim={provider.dim})")
        except EmbeddingError as e:
            logger.warning(f"Embedding 生成失败，将使用无向量模式: {e}")
    
    indexed = 0
    
    if index_backend is not None:
        # 使用 IndexBackend 实现
        try:
            indexed = index_backend.upsert(docs)
            logger.info(f"成功索引 {indexed} 个分块到 {index_backend.backend_name}")
        except Exception as e:
            logger.error(f"索引失败: {e}")
            raise
    else:
        # 模板实现：仅打印日志
        logger.info(f"[STUB] 索引 {len(chunks)} 个分块到后端（无 IndexBackend 实例）")
        for chunk in chunks[:3]:  # 仅显示前 3 个
            excerpt = chunk.excerpt[:50] if chunk.excerpt else ""
            logger.debug(f"  - {chunk.chunk_id}: {excerpt}...")
        indexed = len(chunks)
    
    # 双写到 shadow 后端
    if shadow_backend is not None and dual_write_stats is not None:
        dual_write_stats.enabled = True
        
        # 检查是否为 dry-run 模式
        shadow_dry_run = dual_write_config.dry_run if dual_write_config else False
        dual_write_stats.dry_run = shadow_dry_run
        
        if shadow_dry_run:
            # dry-run 模式：仅记录，不实际写入
            logger.info(f"[DUAL-WRITE-DRY-RUN] Shadow 后端将索引 {len(docs)} 个分块")
            dual_write_stats.shadow_indexed += len(docs)
        else:
            # 实际写入 shadow 后端
            try:
                shadow_indexed = shadow_backend.upsert(docs)
                dual_write_stats.shadow_indexed += shadow_indexed
                logger.info(
                    f"[DUAL-WRITE] Shadow 后端成功索引 {shadow_indexed} 个分块 "
                    f"(strategy={dual_write_stats.shadow_strategy})"
                )
            except Exception as e:
                # Shadow 写入失败不阻断主写入，但记录告警
                error_msg = str(e)
                logger.warning(f"[DUAL-WRITE] Shadow 后端写入失败（不影响主写入）: {error_msg}")
                
                # 为每个 chunk 记录错误（简化处理）
                for doc in docs:
                    dual_write_stats.add_error(doc.chunk_id, error_msg)
    
    return indexed


# ============ 索引同步主流程 ============


def process_patch_blob(
    conn: psycopg.Connection,
    row: Dict[str, Any],
    dry_run: bool = False,
    backend: Optional[IndexBackend] = None,
    embedding_provider: Optional[EmbeddingProvider] = None,
    shadow_backend: Optional[IndexBackend] = None,
    dual_write_config: Optional[DualWriteConfig] = None,
    dual_write_stats: Optional[DualWriteStats] = None,
) -> tuple[int, int]:
    """
    处理单个 patch_blob 记录
    
    Args:
        conn: 数据库连接
        row: patch_blob 记录
        dry_run: 预览模式
        backend: 索引后端
        embedding_provider: Embedding Provider
        shadow_backend: Shadow 后端（用于双写）
        dual_write_config: 双写配置
        dual_write_stats: 双写统计
    
    Returns:
        (chunks_count, indexed_count)
    """
    blob_id = row["blob_id"]
    uri = row.get("uri")
    
    if not uri:
        logger.warning(f"blob_id={blob_id} 没有 uri，跳过")
        return 0, 0
    
    # 读取内容（使用 step3_readers.read_evidence_text）
    content = read_artifact_content(uri, conn=conn)
    if not content:
        logger.warning(f"blob_id={blob_id} 内容为空或读取失败，跳过")
        return 0, 0
    
    # 提取 author_user_id（从 commit_meta_json）
    commit_meta = row.get("commit_meta_json")
    author_user_id = None
    if commit_meta:
        if isinstance(commit_meta, str):
            try:
                commit_meta = json.loads(commit_meta)
            except json.JSONDecodeError:
                commit_meta = {}
        author_user_id = commit_meta.get("resolved_user_id")
    
    # 构建标准 metadata
    metadata = {
        # 核心标识字段
        "project_key": row.get("project_key"),
        "source_type": row["source_type"],
        "source_id": row["source_id"],
        # 仓库信息
        "repo_id": row.get("repo_id"),
        "repo_url": row.get("repo_url"),
        "repo_type": row.get("repo_type"),
        # 提交元信息
        "author": row.get("author_raw"),
        "author_user_id": author_user_id,
        "owner_user_id": author_user_id,  # 用于过滤
        "commit_ts": row.get("commit_ts"),
        "commit_message": row.get("commit_message"),
        "is_bulk": row.get("is_bulk", False),
        # blob 信息
        "blob_id": blob_id,
    }
    
    # 分块
    chunks = chunk_content(
        content=content,
        content_type=row.get("format", "diff"),
        source_type=row["source_type"],
        source_id=row["source_id"],
        sha256=row["sha256"],
        artifact_uri=uri,
        metadata=metadata,
    )
    
    if not chunks:
        logger.warning(f"blob_id={blob_id} 分块结果为空")
        return 0, 0
    
    # 索引
    indexed = upsert_to_index(
        chunks,
        dry_run=dry_run,
        backend=backend,
        embedding_provider=embedding_provider,
        shadow_backend=shadow_backend,
        dual_write_config=dual_write_config,
        dual_write_stats=dual_write_stats,
    )
    
    # 更新 chunking_version（非 dry-run 模式）
    if not dry_run:
        with conn.cursor() as cur:
            cur.execute(SQL_UPDATE_CHUNKING_VERSION, {
                "blob_id": blob_id,
                "chunking_version": CHUNKING_VERSION,
            })
    
    return len(chunks), indexed


def process_attachment(
    conn: psycopg.Connection,
    row: Dict[str, Any],
    dry_run: bool = False,
    backend: Optional[IndexBackend] = None,
    embedding_provider: Optional[EmbeddingProvider] = None,
    shadow_backend: Optional[IndexBackend] = None,
    dual_write_config: Optional[DualWriteConfig] = None,
    dual_write_stats: Optional[DualWriteStats] = None,
) -> tuple[int, int]:
    """
    处理单个 attachment 记录
    
    Args:
        conn: 数据库连接
        row: attachment 记录
        dry_run: 预览模式
        backend: 索引后端
        embedding_provider: Embedding Provider
        shadow_backend: Shadow 后端（用于双写）
        dual_write_config: 双写配置
        dual_write_stats: 双写统计
    
    Returns:
        (chunks_count, indexed_count)
    """
    attachment_id = row["attachment_id"]
    uri = row.get("uri")
    
    if not uri:
        logger.warning(f"attachment_id={attachment_id} 没有 uri，跳过")
        return 0, 0
    
    # 读取内容（使用 step3_readers.read_evidence_text）
    content = read_artifact_content(uri, conn=conn)
    if not content:
        logger.warning(f"attachment_id={attachment_id} 内容为空或读取失败，跳过")
        return 0, 0
    
    # 解析 meta_json 和 scope_json
    meta_json = row.get("meta_json")
    if meta_json and isinstance(meta_json, str):
        try:
            meta_json = json.loads(meta_json)
        except json.JSONDecodeError:
            meta_json = {}
    meta_json = meta_json or {}
    
    scope_json = row.get("scope_json")
    if scope_json and isinstance(scope_json, str):
        try:
            scope_json = json.loads(scope_json)
        except json.JSONDecodeError:
            scope_json = {}
    scope_json = scope_json or {}
    
    # 提取 project_key（优先 meta_json，然后 scope_json）
    project_key = meta_json.get("project_key") or scope_json.get("project_key")
    
    # 映射 kind 到 content_type
    kind = row.get("kind", "text")
    content_type_map = {
        "patch": "diff",
        "diff": "diff",
        "log": "log",
        "spec": "md",
        "md": "md",
        "markdown": "md",
        "report": "text",
    }
    content_type = content_type_map.get(kind, "text")
    
    # 构建标准 metadata
    metadata = {
        # 核心标识字段
        "project_key": project_key,
        "source_type": "logbook",
        "source_id": f"attachment:{attachment_id}",
        # 关联信息
        "attachment_id": attachment_id,
        "item_id": row.get("item_id"),
        "item_type": row.get("item_type"),
        "title": row.get("title"),
        "kind": kind,
        # 所有者信息
        "owner_user_id": row.get("owner_user_id"),
        "status": row.get("status"),
    }
    
    # 分块
    chunks = chunk_content(
        content=content,
        content_type=content_type,
        source_type="logbook",
        source_id=f"attachment:{attachment_id}",
        sha256=row.get("sha256", ""),
        artifact_uri=uri,
        metadata=metadata,
    )
    
    if not chunks:
        logger.warning(f"attachment_id={attachment_id} 分块结果为空")
        return 0, 0
    
    # 索引
    indexed = upsert_to_index(
        chunks,
        dry_run=dry_run,
        backend=backend,
        embedding_provider=embedding_provider,
        shadow_backend=shadow_backend,
        dual_write_config=dual_write_config,
        dual_write_stats=dual_write_stats,
    )
    
    return len(chunks), indexed


def run_incremental_sync(
    conn: psycopg.Connection,
    source: str = "all",
    project_key: Optional[str] = None,
    batch_size: int = 100,
    dry_run: bool = False,
    backend: Optional[IndexBackend] = None,
    embedding_provider: Optional[EmbeddingProvider] = None,
    skip_consistency_check: bool = False,
    collection: Optional[str] = None,
    rebuild_backend_for_collection: bool = True,
    shadow_backend: Optional[IndexBackend] = None,
    dual_write_config: Optional[DualWriteConfig] = None,
) -> IndexResult:
    """
    执行增量同步
    
    Args:
        conn: 数据库连接
        source: 数据源 (all/patch_blobs/attachments)
        project_key: 项目标识过滤
        batch_size: 批量大小
        dry_run: 预览模式
        backend: 索引后端
        embedding_provider: Embedding Provider
        skip_consistency_check: 跳过模型一致性检查
        collection: 目标 collection 名称（可选，不提供则自动生成）
        rebuild_backend_for_collection: 当 collection 与后端不一致时是否重建后端
        shadow_backend: Shadow 后端（用于双写）
        dual_write_config: 双写配置
    """
    result = IndexResult(
        mode="incremental",
        source=source,
        batch_size=batch_size,
        dry_run=dry_run,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    
    start_time = datetime.now(timezone.utc)
    
    # 初始化双写统计
    dual_write_stats = None
    if shadow_backend is not None or dual_write_config is not None:
        dual_write_stats = DualWriteStats(
            enabled=shadow_backend is not None,
            shadow_strategy=dual_write_config.shadow_strategy if dual_write_config else "",
            dry_run=dual_write_config.dry_run if dual_write_config else False,
        )
        result.dual_write = dual_write_stats
        logger.info(
            f"双写已启用: shadow_strategy={dual_write_stats.shadow_strategy}, "
            f"dry_run={dual_write_stats.dry_run}"
        )
    
    # 获取后端信息
    backend_name = backend.backend_name if backend else None
    
    # 获取 embedding provider 并记录模型信息
    provider = embedding_provider or get_embedding_provider_instance()
    if provider:
        result.embedding_model_id = provider.model_id
        result.embedding_dim = provider.dim
        result.embedding_normalize = provider.normalize
        embedding_info = provider.get_model_info()
    else:
        embedding_info = None
    
    # 解析 collection 名称（优先级：explicit > active > default）
    collection = resolve_collection_id(
        conn=conn,
        backend_name=backend_name,
        project_key=project_key,
        embedding_model_id=result.embedding_model_id,
        explicit_collection_id=collection,
        chunking_version=CHUNKING_VERSION,
    )
    
    # 确保后端使用正确的 collection
    # 检查后端的 collection_id/canonical_id 是否与目标 collection 一致
    if backend is not None and rebuild_backend_for_collection:
        backend_collection_id = getattr(backend, 'canonical_id', None) or getattr(backend, 'collection_id', None)
        if backend_collection_id != collection:
            logger.info(
                f"后端 collection ({backend_collection_id}) 与目标 collection ({collection}) 不一致，"
                f"重建后端实例"
            )
            # 重建后端，使用正确的 collection_id
            backend = create_backend_from_env(
                chunking_version=CHUNKING_VERSION,
                embedding_model_id=result.embedding_model_id,
                embedding_provider=provider,
                collection_id=collection,
            )
            # 初始化 pgvector 后端（若适用）
            if not try_initialize_pgvector_backend(backend):
                logger.warning("pgvector 初始化失败，继续运行但可能影响功能")
    
    # 生成 namespace（包含 backend + collection）
    backend_name = backend.backend_name if backend else None
    namespace = make_kv_namespace(backend_name, collection)
    
    # 记录 collection 信息
    result.collection = collection
    
    logger.info(f"使用 collection: {collection}, namespace: {namespace}")
    
    # 同步 patch_blobs
    if source in ("all", "patch_blobs"):
        cursor_data = get_sync_cursor_full(conn, "patch_blobs", namespace)
        last_blob_id = cursor_data.last_id
        result.last_blob_id = last_blob_id
        
        # Embedding 模型一致性检查
        if not skip_consistency_check and embedding_info and cursor_data.embedding_info:
            if not embedding_info.is_compatible(cursor_data.embedding_info):
                logger.warning(
                    f"Embedding 模型变更: {cursor_data.embedding_info.model_id} -> {embedding_info.model_id}, "
                    f"维度: {cursor_data.embedding_info.dim} -> {embedding_info.dim}"
                )
                result.add_error("consistency", 0, 
                    f"Embedding 模型不匹配: 索引使用 {cursor_data.embedding_info.model_id}(dim={cursor_data.embedding_info.dim}), "
                    f"当前配置 {embedding_info.model_id}(dim={embedding_info.dim})"
                )
        
        logger.info(f"开始增量同步 patch_blobs (namespace={namespace}, last_id={last_blob_id})")
        
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(SQL_FETCH_PATCH_BLOBS, {
                "last_blob_id": last_blob_id,
                "project_key": project_key,
                "batch_size": batch_size,
            })
            rows = cur.fetchall()
        
        logger.info(f"获取到 {len(rows)} 条 patch_blobs 记录")
        
        max_blob_id = last_blob_id
        for row in rows:
            try:
                chunks_count, indexed_count = process_patch_blob(
                    conn, row, dry_run,
                    backend=backend,
                    embedding_provider=provider,
                    shadow_backend=shadow_backend,
                    dual_write_config=dual_write_config,
                    dual_write_stats=dual_write_stats,
                )
                result.total_processed += 1
                result.total_chunks += chunks_count
                result.total_indexed += indexed_count
                result.processed_blob_ids.append(row["blob_id"])
                max_blob_id = max(max_blob_id, row["blob_id"])
            except Exception as e:
                result.add_error("patch_blob", row["blob_id"], str(e))
                logger.error(f"处理 blob_id={row['blob_id']} 失败: {e}")
        
        # 更新游标（包含 embedding 信息）
        if not dry_run and max_blob_id > last_blob_id:
            update_sync_cursor(conn, "patch_blobs", namespace, max_blob_id, embedding_info)
            result.last_blob_id = max_blob_id
    
    # 同步 attachments
    if source in ("all", "attachments"):
        cursor_data = get_sync_cursor_full(conn, "attachments", namespace)
        last_attachment_id = cursor_data.last_id
        result.last_attachment_id = last_attachment_id
        
        # Embedding 模型一致性检查
        if not skip_consistency_check and embedding_info and cursor_data.embedding_info:
            if not embedding_info.is_compatible(cursor_data.embedding_info):
                logger.warning(
                    f"Embedding 模型变更: {cursor_data.embedding_info.model_id} -> {embedding_info.model_id}"
                )
        
        logger.info(f"开始增量同步 attachments (namespace={namespace}, last_id={last_attachment_id})")
        
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(SQL_FETCH_ATTACHMENTS, {
                "last_attachment_id": last_attachment_id,
                "project_key": project_key,
                "batch_size": batch_size,
            })
            rows = cur.fetchall()
        
        logger.info(f"获取到 {len(rows)} 条 attachments 记录")
        
        max_attachment_id = last_attachment_id
        for row in rows:
            try:
                chunks_count, indexed_count = process_attachment(
                    conn, row, dry_run,
                    backend=backend,
                    embedding_provider=provider,
                    shadow_backend=shadow_backend,
                    dual_write_config=dual_write_config,
                    dual_write_stats=dual_write_stats,
                )
                result.total_processed += 1
                result.total_chunks += chunks_count
                result.total_indexed += indexed_count
                result.processed_attachment_ids.append(row["attachment_id"])
                max_attachment_id = max(max_attachment_id, row["attachment_id"])
            except Exception as e:
                result.add_error("attachment", row["attachment_id"], str(e))
                logger.error(f"处理 attachment_id={row['attachment_id']} 失败: {e}")
        
        # 更新游标（包含 embedding 信息）
        if not dry_run and max_attachment_id > last_attachment_id:
            update_sync_cursor(conn, "attachments", namespace, max_attachment_id, embedding_info)
            result.last_attachment_id = max_attachment_id
    
    # 计算耗时
    end_time = datetime.now(timezone.utc)
    result.completed_at = end_time.isoformat()
    result.duration_seconds = (end_time - start_time).total_seconds()
    
    return result


def run_full_rebuild(
    conn: psycopg.Connection,
    source: str = "all",
    project_key: Optional[str] = None,
    batch_size: int = 100,
    dry_run: bool = False,
    backend: Optional[IndexBackend] = None,
    embedding_provider: Optional[EmbeddingProvider] = None,
    activate: bool = False,
    version_tag: Optional[str] = None,
    shadow_backend: Optional[IndexBackend] = None,
    dual_write_config: Optional[DualWriteConfig] = None,
) -> IndexResult:
    """
    执行全量重建
    
    全量重建会创建新的 collection（带版本标签），保留旧 collection 以便回滚。
    可选择在完成后激活新 collection。
    
    Args:
        conn: 数据库连接
        source: 数据源 (all/patch_blobs/attachments)
        project_key: 项目标识过滤
        batch_size: 批量大小
        dry_run: 预览模式
        backend: 索引后端
        embedding_provider: Embedding Provider
        activate: 完成后是否激活新 collection
        version_tag: 版本标签（不提供则自动生成时间戳）
        shadow_backend: Shadow 后端（用于双写）
        dual_write_config: 双写配置
    
    Returns:
        IndexResult 包含新 collection 名称和处理统计
    """
    # 获取 embedding provider
    provider = embedding_provider or get_embedding_provider_instance()
    embedding_model_id = provider.model_id if provider else None
    
    # 生成版本标签和 collection 名称
    if version_tag is None:
        version_tag = make_version_tag()
    
    new_collection = make_collection_id(
        project_key=project_key,
        chunking_version=CHUNKING_VERSION,
        embedding_model_id=embedding_model_id,
        version_tag=version_tag,
    )
    
    logger.info(f"开始全量重建，创建新 collection: {new_collection}")
    
    # 为全量重建创建使用新 collection_id 的后端实例
    # 这确保后端直接写入新 collection
    new_shadow_backend = None
    if backend is not None:
        new_backend = create_backend_from_env(
            chunking_version=CHUNKING_VERSION,
            embedding_model_id=embedding_model_id,
            embedding_provider=provider,
            collection_id=new_collection,
        )
        logger.info(f"全量重建使用新后端实例，collection_id={new_collection}")
        # 初始化 pgvector 后端（若适用）
        if not try_initialize_pgvector_backend(new_backend):
            logger.warning("pgvector 初始化失败，继续运行但可能影响功能")
        
        # 如果启用了双写，也为 shadow 后端创建新实例
        if dual_write_config and dual_write_config.enabled:
            primary_config = PGVectorConfig.from_env()
            new_shadow_backend = create_shadow_backend(
                primary_config=primary_config,
                dual_write_config=dual_write_config,
                chunking_version=CHUNKING_VERSION,
                embedding_model_id=embedding_model_id,
                embedding_provider=provider,
                collection_id=new_collection,
            )
            if new_shadow_backend:
                if not try_initialize_pgvector_backend(new_shadow_backend):
                    logger.warning("Shadow pgvector 初始化失败，继续运行但可能影响双写功能")
    else:
        new_backend = None
    
    # 获取后端名称（用于 activate）
    backend_name = new_backend.backend_name if new_backend else (backend.backend_name if backend else None)
    
    # 执行增量同步（从 0 开始，相当于全量）
    # 新 collection 的游标从 0 开始
    result = run_incremental_sync(
        conn=conn,
        source=source,
        project_key=project_key,
        batch_size=batch_size,
        dry_run=dry_run,
        backend=new_backend,
        embedding_provider=provider,
        skip_consistency_check=True,  # 全量重建不需要一致性检查
        collection=new_collection,
        rebuild_backend_for_collection=False,  # 后端已经使用正确的 collection
        shadow_backend=new_shadow_backend,
        dual_write_config=dual_write_config,
    )
    
    # 更新 mode 标识和 collection 信息
    result.mode = "full"
    result.collection = new_collection
    result.version_tag = version_tag
    
    # 激活新 collection
    if activate and not dry_run and backend_name:
        set_active_collection(conn, backend_name, new_collection, project_key)
        result.activated = True
        logger.info(f"已激活新 collection: {new_collection}")
    
    return result


def rollback_collection(
    conn: psycopg.Connection,
    backend_name: str,
    target_collection: str,
    project_key: Optional[str] = None,
) -> bool:
    """
    回滚到指定的 collection
    
    Args:
        conn: 数据库连接
        backend_name: 索引后端名称
        target_collection: 要回滚到的 collection 名称
        project_key: 项目标识
    
    Returns:
        是否成功
    """
    try:
        set_active_collection(conn, backend_name, target_collection, project_key)
        logger.info(f"回滚成功，当前 active collection: {target_collection}")
        return True
    except Exception as e:
        logger.error(f"回滚失败: {e}")
        return False


def run_single_index(
    conn: psycopg.Connection,
    blob_id: Optional[int] = None,
    attachment_id: Optional[int] = None,
    dry_run: bool = False,
    backend: Optional[IndexBackend] = None,
    embedding_provider: Optional[EmbeddingProvider] = None,
) -> IndexResult:
    """
    索引单条记录
    
    Args:
        conn: 数据库连接
        blob_id: patch_blob ID
        attachment_id: attachment ID
        dry_run: 预览模式
        backend: 索引后端
        embedding_provider: Embedding Provider
    """
    result = IndexResult(
        mode="single",
        source="patch_blobs" if blob_id else "attachments",
        dry_run=dry_run,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    
    start_time = datetime.now(timezone.utc)
    
    # 获取 embedding provider 并记录模型信息
    provider = embedding_provider or get_embedding_provider_instance()
    if provider:
        result.embedding_model_id = provider.model_id
        result.embedding_dim = provider.dim
        result.embedding_normalize = provider.normalize
    
    if blob_id:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(SQL_FETCH_SINGLE_BLOB, {"blob_id": blob_id})
            row = cur.fetchone()
        
        if not row:
            result.add_error("patch_blob", blob_id, "记录不存在")
        else:
            try:
                chunks_count, indexed_count = process_patch_blob(
                    conn, row, dry_run, backend=backend, embedding_provider=provider
                )
                result.total_processed += 1
                result.total_chunks += chunks_count
                result.total_indexed += indexed_count
                result.processed_blob_ids.append(blob_id)
            except Exception as e:
                result.add_error("patch_blob", blob_id, str(e))
    
    if attachment_id:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(SQL_FETCH_SINGLE_ATTACHMENT, {"attachment_id": attachment_id})
            row = cur.fetchone()
        
        if not row:
            result.add_error("attachment", attachment_id, "记录不存在")
        else:
            try:
                chunks_count, indexed_count = process_attachment(
                    conn, row, dry_run, backend=backend, embedding_provider=provider
                )
                result.total_processed += 1
                result.total_chunks += chunks_count
                result.total_indexed += indexed_count
                result.processed_attachment_ids.append(attachment_id)
            except Exception as e:
                result.add_error("attachment", attachment_id, str(e))
    
    end_time = datetime.now(timezone.utc)
    result.completed_at = end_time.isoformat()
    result.duration_seconds = (end_time - start_time).total_seconds()
    
    return result


# ============ 诊断模式（只读） ============


@dataclass
class ActiveCollectionInfo:
    """Active Collection 信息"""
    backend_name: Optional[str] = None
    project_key: Optional[str] = None
    active_collection: Optional[str] = None
    found: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "backend_name": self.backend_name,
            "project_key": self.project_key or "default",
            "active_collection": self.active_collection,
            "found": self.found,
        }


def show_active_collection(
    conn,
    backend_name: str,
    project_key: Optional[str] = None,
) -> ActiveCollectionInfo:
    """
    显示当前 backend+project 的 active_collection（只读操作）
    
    Args:
        conn: 数据库连接
        backend_name: 索引后端名称
        project_key: 项目标识
    
    Returns:
        ActiveCollectionInfo 包含 active_collection 信息
    """
    info = ActiveCollectionInfo(
        backend_name=backend_name,
        project_key=project_key,
    )
    
    try:
        active = get_active_collection(conn, backend_name, project_key)
        if active:
            info.active_collection = active
            info.found = True
            logger.info(f"找到 active_collection: {active}")
        else:
            logger.info(f"未找到 active_collection (backend={backend_name}, project={project_key or 'default'})")
    except Exception as e:
        logger.error(f"读取 active_collection 失败: {e}")
    
    return info


@dataclass
class CollectionValidationResult:
    """Collection 验证结果"""
    collection_id: str
    backend_name: str
    
    # 验证状态
    valid: bool = False
    available: bool = False
    
    # preflight 检查
    preflight_passed: bool = False
    preflight_errors: List[str] = field(default_factory=list)
    
    # 后端状态
    backend_healthy: bool = False
    backend_stats: Optional[Dict[str, Any]] = None
    
    # 建议
    recommendations: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "collection_id": self.collection_id,
            "backend_name": self.backend_name,
            "valid": self.valid,
            "available": self.available,
            "preflight": {
                "passed": self.preflight_passed,
                "errors": self.preflight_errors,
            },
            "backend": {
                "healthy": self.backend_healthy,
                "stats": self.backend_stats,
            },
            "recommendations": self.recommendations,
        }


def validate_collection(
    conn,
    collection_id: str,
    backend_name: Optional[str] = None,
    embedding_model_id: Optional[str] = None,
) -> CollectionValidationResult:
    """
    验证指定 collection 的可用性（只读操作）
    
    执行 preflight_check + get_stats/health_check，不写入任何数据。
    
    Args:
        conn: 数据库连接
        collection_id: 要验证的 collection_id
        backend_name: 后端名称（可选，默认从环境变量读取）
        embedding_model_id: Embedding 模型 ID（可选）
    
    Returns:
        CollectionValidationResult 包含验证结果和建议
    """
    # 获取后端名称
    if backend_name is None:
        backend_name = os.environ.get("STEP3_INDEX_BACKEND", "pgvector")
    
    result = CollectionValidationResult(
        collection_id=collection_id,
        backend_name=backend_name,
    )
    
    try:
        # 创建后端实例（使用指定的 collection_id）
        backend = create_backend_from_env(
            chunking_version=CHUNKING_VERSION,
            embedding_model_id=embedding_model_id,
            collection_id=collection_id,
        )
        
        if backend is None:
            result.preflight_errors.append("无法创建后端实例")
            result.recommendations.append("检查 STEP3_INDEX_BACKEND 和相关配置")
            return result
        
        logger.info(f"创建后端实例: {backend.backend_name}, collection_id={collection_id}")
        
        # 1. Preflight 检查
        if hasattr(backend, 'preflight_check'):
            try:
                preflight_result = backend.preflight_check()
                if isinstance(preflight_result, dict):
                    result.preflight_passed = preflight_result.get("passed", False)
                    if not result.preflight_passed:
                        result.preflight_errors.extend(preflight_result.get("errors", []))
                elif isinstance(preflight_result, bool):
                    result.preflight_passed = preflight_result
                else:
                    result.preflight_passed = bool(preflight_result)
                logger.info(f"Preflight 检查: {'通过' if result.preflight_passed else '失败'}")
            except Exception as e:
                result.preflight_errors.append(f"preflight_check 异常: {str(e)}")
                logger.warning(f"Preflight 检查异常: {e}")
        else:
            # 无 preflight_check 方法，跳过
            result.preflight_passed = True
            logger.debug("后端无 preflight_check 方法，跳过")
        
        # 2. Health 检查
        if hasattr(backend, 'health_check'):
            try:
                health = backend.health_check()
                if isinstance(health, dict):
                    result.backend_healthy = health.get("healthy", False)
                elif isinstance(health, bool):
                    result.backend_healthy = health
                else:
                    result.backend_healthy = bool(health)
                logger.info(f"Health 检查: {'健康' if result.backend_healthy else '不健康'}")
            except Exception as e:
                result.recommendations.append(f"health_check 失败: {str(e)}")
                logger.warning(f"Health 检查异常: {e}")
        else:
            result.backend_healthy = True
            logger.debug("后端无 health_check 方法，假定健康")
        
        # 3. 获取统计信息
        if hasattr(backend, 'get_stats'):
            try:
                stats = backend.get_stats()
                result.backend_stats = stats
                logger.info(f"统计信息: {stats}")
            except Exception as e:
                result.recommendations.append(f"get_stats 失败: {str(e)}")
                logger.warning(f"获取统计信息异常: {e}")
        
        # 4. 综合判断可用性
        result.valid = result.preflight_passed
        result.available = result.preflight_passed and result.backend_healthy
        
        # 5. 生成建议
        if not result.preflight_passed:
            result.recommendations.append("请检查后端配置和连接")
        if not result.backend_healthy:
            result.recommendations.append("后端服务可能不健康，请检查服务状态")
        if result.available:
            result.recommendations.append("Collection 可用，可以进行索引/查询操作")
        
    except Exception as e:
        result.preflight_errors.append(f"验证过程异常: {str(e)}")
        result.recommendations.append("请检查配置和网络连接")
        logger.error(f"验证 collection 失败: {e}")
    
    return result


def print_active_collection_report(info: ActiveCollectionInfo):
    """打印 Active Collection 报告（文本格式）"""
    print("\n" + "=" * 50)
    print("Active Collection 信息")
    print("=" * 50)
    print(f"  后端: {info.backend_name}")
    print(f"  项目: {info.project_key or 'default'}")
    print(f"  状态: {'已找到' if info.found else '未设置'}")
    if info.active_collection:
        print(f"  Active Collection: {info.active_collection}")
    else:
        print("  Active Collection: (未设置)")
    print("=" * 50 + "\n")


def print_validation_report(result: CollectionValidationResult):
    """打印 Collection 验证报告（文本格式）"""
    print("\n" + "=" * 60)
    print("Collection 验证报告")
    print("=" * 60)
    
    print(f"\n【目标 Collection】")
    print(f"  Collection ID: {result.collection_id}")
    print(f"  后端: {result.backend_name}")
    
    print(f"\n【Preflight 检查】")
    print(f"  状态: {'通过' if result.preflight_passed else '失败'}")
    if result.preflight_errors:
        print(f"  错误:")
        for err in result.preflight_errors:
            print(f"    - {err}")
    
    print(f"\n【后端状态】")
    print(f"  健康: {'是' if result.backend_healthy else '否'}")
    if result.backend_stats:
        print(f"  统计:")
        for key, value in result.backend_stats.items():
            print(f"    - {key}: {value}")
    
    print(f"\n【综合判断】")
    print(f"  有效: {'是' if result.valid else '否'}")
    print(f"  可用: {'是' if result.available else '否'}")
    
    if result.recommendations:
        print(f"\n【建议】")
        for rec in result.recommendations:
            print(f"  - {rec}")
    
    print("\n" + "=" * 60 + "\n")


# ============ CLI 部分 ============


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="Step3 索引同步工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 增量同步（默认）
    python seek_indexer.py --mode incremental

    # 全量重建（创建新 collection，不激活）
    python seek_indexer.py --mode full --source patch_blobs

    # 全量重建并激活新 collection
    python seek_indexer.py --mode full --activate

    # 全量重建并指定版本标签
    python seek_indexer.py --mode full --version-tag v2.0.0 --activate

    # 回滚到指定 collection
    python seek_indexer.py --mode rollback --collection "proj1:v1:bge-m3:20260128T100000"

    # 单记录索引
    python seek_indexer.py --mode single --blob-id 12345

    # JSON 输出
    python seek_indexer.py --mode incremental --json

    # 预览模式（不实际写入）
    python seek_indexer.py --mode full --dry-run --json

    # 显示当前 active collection（只读诊断）
    python seek_indexer.py --mode show-active
    python seek_indexer.py --mode show-active --project-key myproj --json

    # 验证指定 collection 的可用性（只读诊断）
    python seek_indexer.py --mode validate-collection --collection "proj1:v1:bge-m3"
    python seek_indexer.py --mode validate-collection --collection "proj1:v1:bge-m3" --json

Collection 命名格式:
    {project_key}:{chunking_version}:{embedding_model_id}[:{version_tag}]
    例如: proj1:v1:bge-m3:20260128T120000

环境变量:
    PROJECT_KEY     项目标识（用于筛选）
    BATCH_SIZE      每批处理数量（默认 100）
        """,
    )
    
    add_config_argument(parser)
    
    # 模式参数
    parser.add_argument(
        "--mode",
        type=str,
        choices=["incremental", "full", "single", "rollback", "show-active", "validate-collection"],
        default=os.environ.get("INDEX_MODE", "incremental"),
        help="同步模式: incremental(增量)/full(全量重建)/single(单条)/rollback(回滚)/show-active(显示活跃collection)/validate-collection(验证collection)",
    )
    
    parser.add_argument(
        "--source",
        type=str,
        choices=["patch_blobs", "attachments", "all"],
        default=os.environ.get("INDEX_SOURCE", "all"),
        help="数据源: patch_blobs/attachments/all",
    )
    
    # 单记录模式参数
    parser.add_argument(
        "--blob-id",
        type=int,
        default=None,
        help="指定 blob_id（single 模式）",
    )
    parser.add_argument(
        "--attachment-id",
        type=int,
        default=None,
        help="指定 attachment_id（single 模式）",
    )
    
    # 筛选参数
    parser.add_argument(
        "--project-key",
        type=str,
        default=os.environ.get("PROJECT_KEY"),
        help="按项目标识筛选",
    )
    
    # Collection/版本管理参数
    parser.add_argument(
        "--collection",
        type=str,
        default=None,
        help="目标 collection 名称（增量模式）或回滚目标（rollback 模式）",
    )
    parser.add_argument(
        "--version-tag",
        type=str,
        default=None,
        help="版本标签，用于 full 模式（默认自动生成时间戳）",
    )
    parser.add_argument(
        "--activate",
        action="store_true",
        default=False,
        help="全量重建完成后激活新 collection（仅 full 模式）",
    )
    
    # 批量参数
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(os.environ.get("BATCH_SIZE", "100")),
        help="每批处理数量（默认 100）",
    )
    
    # 运行选项
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes"),
        help="仅预览，不实际写入索引",
    )
    
    # 输出选项
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="显示详细输出",
    )
    
    # 添加后端选项
    add_backend_arguments(parser)
    
    return parser.parse_args()


def print_report(result: IndexResult):
    """打印索引报告（文本格式）"""
    print("\n" + "=" * 60)
    print("Step3 索引同步报告")
    print("=" * 60)
    
    print(f"\n【运行参数】")
    print(f"  模式: {result.mode}")
    print(f"  数据源: {result.source}")
    print(f"  批量大小: {result.batch_size}")
    print(f"  预览模式: {'是' if result.dry_run else '否'}")
    print(f"  分块版本: {CHUNKING_VERSION}")
    
    # Collection 信息
    if result.collection:
        print(f"\n【Collection】")
        print(f"  名称: {result.collection}")
        if result.version_tag:
            print(f"  版本标签: {result.version_tag}")
        print(f"  已激活: {'是' if result.activated else '否'}")
    
    # Embedding 模型信息
    if result.embedding_model_id:
        print(f"\n【Embedding 模型】")
        print(f"  模型: {result.embedding_model_id}")
        print(f"  维度: {result.embedding_dim}")
        print(f"  归一化: {'是' if result.embedding_normalize else '否'}")
    
    print(f"\n【处理统计】")
    print(f"  处理记录数: {result.total_processed}")
    print(f"  生成分块数: {result.total_chunks}")
    print(f"  索引成功数: {result.total_indexed}")
    print(f"  错误数: {result.total_errors}")
    print(f"  耗时: {result.duration_seconds:.2f} 秒")
    
    if result.last_blob_id > 0:
        print(f"\n【游标状态】")
        print(f"  last_blob_id: {result.last_blob_id}")
    
    # 双写统计
    if result.dual_write is not None and result.dual_write.enabled:
        print(f"\n【双写统计】")
        print(f"  Shadow 策略: {result.dual_write.shadow_strategy}")
        print(f"  Shadow Dry-Run: {'是' if result.dual_write.dry_run else '否'}")
        print(f"  Shadow 成功: {result.dual_write.shadow_indexed}")
        print(f"  Shadow 失败: {result.dual_write.shadow_errors}")
        if result.dual_write.shadow_error_records:
            print(f"  Shadow 错误详情:")
            for err in result.dual_write.shadow_error_records[:5]:
                print(f"    - {err['chunk_id']}: {err['error'][:80]}...")
            if len(result.dual_write.shadow_error_records) > 5:
                print(f"    ... 还有 {len(result.dual_write.shadow_error_records) - 5} 个错误")
    
    if result.error_records:
        print(f"\n【错误记录】")
        for err in result.error_records[:10]:
            print(f"  - {err['type']}:{err['id']}: {err['error']}")
        if len(result.error_records) > 10:
            print(f"  ... 还有 {len(result.error_records) - 10} 个错误")
    
    print("\n" + "=" * 60)
    if result.total_errors == 0:
        print("索引同步成功完成")
    else:
        print(f"索引同步完成，但有 {result.total_errors} 个错误")
    print("=" * 60 + "\n")


def main() -> int:
    """主入口"""
    args = parse_args()
    
    # 设置日志级别
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    elif args.json:
        logging.getLogger().setLevel(logging.WARNING)
    
    # 验证参数
    if args.mode == "single":
        if not args.blob_id and not args.attachment_id:
            logger.error("single 模式需要指定 --blob-id 或 --attachment-id")
            if args.json:
                print(json.dumps({"success": False, "error": "missing blob-id or attachment-id"}))
            return 1
    
    if args.mode == "rollback":
        if not args.collection:
            logger.error("rollback 模式需要指定 --collection 参数")
            if args.json:
                print(json.dumps({"success": False, "error": "missing --collection for rollback"}))
            return 1
    
    if args.mode == "validate-collection":
        if not args.collection:
            logger.error("validate-collection 模式需要指定 --collection 参数")
            if args.json:
                print(json.dumps({"success": False, "error": "missing --collection for validate-collection"}))
            return 1
    
    try:
        # 加载配置
        config = get_config(args.config_path)
        config.load()
        
        # 初始化索引后端（从环境变量/CLI 参数）
        # 如果指定了 --collection 且不是全量重建模式，则使用该 collection_id 创建后端
        initial_collection_id = None
        if hasattr(args, 'collection') and args.collection and args.mode != 'full':
            initial_collection_id = args.collection
            logger.info(f"使用指定的 collection_id 初始化后端: {initial_collection_id}")
        
        # 初始化双写配置
        dual_write_config = get_dual_write_config()
        shadow_backend = None
        
        try:
            backend = create_backend_from_args(args, collection_id=initial_collection_id)
            set_index_backend(backend)
            logger.info(f"索引后端已初始化: {backend.backend_name}")
            
            # 初始化 pgvector 后端（若适用）
            if not try_initialize_pgvector_backend(backend):
                logger.warning("pgvector 初始化失败，继续运行但可能影响功能")
            
            # 如果启用了双写，创建 shadow 后端
            if dual_write_config.enabled and backend.backend_name == "pgvector":
                primary_config = PGVectorConfig.from_env()
                shadow_backend = create_shadow_backend(
                    primary_config=primary_config,
                    dual_write_config=dual_write_config,
                    collection_id=initial_collection_id,
                )
                if shadow_backend:
                    logger.info(
                        f"双写 Shadow 后端已初始化: strategy={dual_write_config.shadow_strategy}, "
                        f"dry_run={dual_write_config.dry_run}"
                    )
                    # 初始化 shadow pgvector 后端
                    if not try_initialize_pgvector_backend(shadow_backend):
                        logger.warning("Shadow pgvector 初始化失败，继续运行但可能影响双写功能")
                else:
                    logger.warning("双写已启用但 Shadow 后端创建失败")
        except Exception as e:
            logger.warning(f"初始化索引后端失败，使用 stub 模式: {e}")
            backend = None
            shadow_backend = None
        
        # 获取数据库连接
        conn = get_connection(config=config)
        
        try:
            # 获取索引后端
            if backend is None:
                backend = get_index_backend()
            backend_name = backend.backend_name if backend else None
            
            # 复用 resolve_collection 逻辑：解析并确保后端使用正确的 collection
            # 对于 full 和 rollback 模式，跳过此逻辑（它们有自己的 collection 处理）
            if backend is not None and args.mode not in ('full', 'rollback'):
                # 获取 embedding provider
                provider = get_embedding_provider_instance()
                embedding_model_id = provider.model_id if provider else None
                
                # 解析 collection（优先级：显式指定 > active_collection > 默认）
                resolved_collection = resolve_collection_id(
                    conn=conn,
                    backend_name=backend.backend_name,
                    project_key=args.project_key,
                    embedding_model_id=embedding_model_id,
                    explicit_collection_id=getattr(args, 'collection', None),
                    chunking_version=CHUNKING_VERSION,
                )
                logger.info(f"解析 collection: {resolved_collection}")
                
                # 检查后端的 collection_id 是否与解析的 collection 一致
                backend_collection_id = getattr(backend, 'canonical_id', None) or getattr(backend, 'collection_id', None)
                if backend_collection_id != resolved_collection:
                    logger.info(
                        f"后端 collection ({backend_collection_id}) 与目标 collection ({resolved_collection}) 不一致，"
                        f"重建后端实例"
                    )
                    # 重建后端，使用正确的 collection_id
                    backend = create_backend_from_env(
                        chunking_version=CHUNKING_VERSION,
                        embedding_model_id=embedding_model_id,
                        embedding_provider=provider,
                        collection_id=resolved_collection,
                    )
                    set_index_backend(backend)
                    backend_name = backend.backend_name
                    logger.info(f"后端已重建，使用 collection: {resolved_collection}")
                    # 初始化 pgvector 后端（若适用）
                    if not try_initialize_pgvector_backend(backend):
                        logger.warning("pgvector 初始化失败，继续运行但可能影响功能")
            
            # 执行索引
            if args.mode == "single":
                result = run_single_index(
                    conn=conn,
                    blob_id=args.blob_id,
                    attachment_id=args.attachment_id,
                    dry_run=args.dry_run,
                    backend=backend,
                )
            elif args.mode == "full":
                # 全量模式：创建新 collection
                result = run_full_rebuild(
                    conn=conn,
                    source=args.source,
                    project_key=args.project_key,
                    batch_size=args.batch_size,
                    dry_run=args.dry_run,
                    backend=backend,
                    activate=args.activate,
                    version_tag=args.version_tag,
                    shadow_backend=shadow_backend,
                    dual_write_config=dual_write_config,
                )
            elif args.mode == "rollback":
                # 回滚模式
                if not backend_name:
                    logger.error("rollback 模式需要配置索引后端")
                    if args.json:
                        print(json.dumps({"success": False, "error": "no index backend configured"}))
                    return 1
                
                if args.dry_run:
                    logger.info(f"[DRY-RUN] 将回滚到 collection: {args.collection}")
                    success = True
                else:
                    success = rollback_collection(
                        conn=conn,
                        backend_name=backend_name,
                        target_collection=args.collection,
                        project_key=args.project_key,
                    )
                
                if args.json:
                    print(json.dumps({
                        "success": success,
                        "mode": "rollback",
                        "target_collection": args.collection,
                        "dry_run": args.dry_run,
                    }, ensure_ascii=False, indent=2))
                else:
                    if success:
                        print(f"回滚成功: {args.collection}")
                    else:
                        print(f"回滚失败: {args.collection}")
                
                if not args.dry_run:
                    conn.commit()
                return 0 if success else 1
            elif args.mode == "show-active":
                # 显示 active collection（只读诊断模式）
                info = show_active_collection(
                    conn=conn,
                    backend_name=backend_name or os.environ.get("STEP3_INDEX_BACKEND", "pgvector"),
                    project_key=args.project_key,
                )
                
                if args.json:
                    print(json.dumps(info.to_dict(), ensure_ascii=False, indent=2))
                else:
                    print_active_collection_report(info)
                
                return 0
            elif args.mode == "validate-collection":
                # 验证 collection 可用性（只读诊断模式）
                # 获取 embedding provider 信息
                provider = get_embedding_provider_instance()
                embedding_model_id = provider.model_id if provider else None
                
                validation_result = validate_collection(
                    conn=conn,
                    collection_id=args.collection,
                    backend_name=backend_name,
                    embedding_model_id=embedding_model_id,
                )
                
                if args.json:
                    print(json.dumps(validation_result.to_dict(), ensure_ascii=False, indent=2))
                else:
                    print_validation_report(validation_result)
                
                return 0 if validation_result.available else 1
            else:
                # 增量模式
                result = run_incremental_sync(
                    conn=conn,
                    source=args.source,
                    project_key=args.project_key,
                    batch_size=args.batch_size,
                    dry_run=args.dry_run,
                    backend=backend,
                    collection=args.collection,
                    shadow_backend=shadow_backend,
                    dual_write_config=dual_write_config,
                )
            
            # 提交事务（非 dry-run 模式）
            if not args.dry_run:
                conn.commit()
            else:
                conn.rollback()
            
            # 输出结果
            if args.json:
                print(json.dumps(result.to_dict(), default=str, ensure_ascii=False, indent=2))
            else:
                print_report(result)
            
            return 0 if result.total_errors == 0 else 1
            
        except psycopg.Error as e:
            conn.rollback()
            raise DatabaseError(f"数据库操作失败: {e}", {"error": str(e)})
        finally:
            conn.close()
    
    except EngramError as e:
        if args.json:
            print(json.dumps({"success": False, "error": e.to_dict()}, default=str, ensure_ascii=False))
        else:
            logger.error(f"{e.error_type}: {e.message}")
        return e.exit_code
    
    except Exception as e:
        logger.exception(f"未预期的错误: {e}")
        if args.json:
            print(json.dumps({
                "success": False,
                "error": {"type": "UNEXPECTED_ERROR", "message": str(e)},
            }, default=str, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
