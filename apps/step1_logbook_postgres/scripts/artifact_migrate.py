#!/usr/bin/env python3
"""
artifact_migrate.py - 制品存储后端迁移工具

支持在不同存储后端之间迁移制品：
- local -> local：本地目录之间迁移
- local -> object：本地迁移到 S3/MinIO
- object -> local：S3/MinIO 迁移到本地
- object -> object：跨 bucket 或跨区域迁移

功能特性：
- 流式读写，支持大文件
- SHA256 校验确保数据完整性
- 支持 dry-run 预览
- 可选更新数据库 URI 引用
- 可选删除源文件或移动到 trash
- 并发迁移支持
- 详细的迁移统计和错误报告

使用示例：
    # Dry-run 预览
    python artifact_migrate.py --source-backend local --target-backend object --prefix scm/ --dry-run
    
    # 实际迁移并校验
    python artifact_migrate.py --source-backend local --target-backend object --prefix scm/ --verify
    
    # 迁移并更新数据库
    python artifact_migrate.py --source-backend local --target-backend object --prefix scm/ --update-db
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from engram_step1.artifact_store import (
    BACKEND_FILE,
    BACKEND_LOCAL,
    BACKEND_OBJECT,
    BUFFER_SIZE,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactStore,
    FileUriStore,
    LocalArtifactsStore,
    ObjectStore,
    get_artifact_store,
)
from engram_step1.config import Config, get_config
from engram_step1.errors import EngramError
from engram_step1.uri import (
    classify_uri_type,
    is_artifact_key,
    is_physical_uri,
    normalize_to_artifact_key,
    parse_uri,
    strip_artifact_scheme,
    try_convert_to_artifact_key,
    UriConversionResult,
)

# 日志配置
logger = logging.getLogger(__name__)

# =============================================================================
# DB 更新模式常量
# =============================================================================

DB_UPDATE_MODE_NONE = "none"                    # 不更新 DB
DB_UPDATE_MODE_TO_ARTIFACT_KEY = "to-artifact-key"  # 将 DB 中 URI 归一为 artifact key
DB_UPDATE_MODE_TO_PHYSICAL_S3 = "to-physical-s3"    # 将 DB 中 artifact key 转换为 s3:// URI

DB_UPDATE_MODES = [
    DB_UPDATE_MODE_NONE,
    DB_UPDATE_MODE_TO_ARTIFACT_KEY,
    DB_UPDATE_MODE_TO_PHYSICAL_S3,
]


# =============================================================================
# 错误定义
# =============================================================================


class MigrationError(EngramError):
    """迁移错误基类"""
    error_type = "MIGRATION_ERROR"


class MigrationVerifyError(MigrationError):
    """迁移校验失败"""
    error_type = "MIGRATION_VERIFY_ERROR"


class MigrationDbUpdateError(MigrationError):
    """数据库更新失败"""
    error_type = "MIGRATION_DB_UPDATE_ERROR"


class MigrationOpsCredentialsRequiredError(MigrationError):
    """需要 ops 凭证但未提供"""
    error_type = "MIGRATION_OPS_CREDENTIALS_REQUIRED"


# =============================================================================
# 数据结构
# =============================================================================


@dataclass
class MigrationItem:
    """单个迁移项"""
    key: str                        # 制品 key（相对路径）
    source_uri: str                 # 源 URI
    target_uri: Optional[str] = None  # 目标 URI
    source_size: int = 0            # 源文件大小
    source_sha256: Optional[str] = None  # 源文件 SHA256
    target_sha256: Optional[str] = None  # 目标文件 SHA256
    status: str = "pending"         # pending/migrated/verified/failed/skipped
    error: Optional[str] = None     # 错误信息
    duration_ms: float = 0          # 迁移耗时（毫秒）


@dataclass
class MigrationResult:
    """迁移结果统计"""
    scanned_count: int = 0          # 扫描的文件数
    migrated_count: int = 0         # 成功迁移的文件数
    verified_count: int = 0         # 校验通过的文件数
    skipped_count: int = 0          # 跳过的文件数（目标已存在）
    failed_count: int = 0           # 失败的文件数
    deleted_count: int = 0          # 已删除源文件数
    trashed_count: int = 0          # 已移动到 trash 的文件数
    db_updated_count: int = 0       # 更新的数据库记录数
    total_size_bytes: int = 0       # 总大小（字节）
    migrated_size_bytes: int = 0    # 已迁移大小（字节）
    duration_seconds: float = 0     # 总耗时（秒）
    dry_run: bool = False           # 是否为 dry-run 模式
    errors: List[Dict[str, Any]] = field(default_factory=list)
    items: List[MigrationItem] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "scanned_count": self.scanned_count,
            "migrated_count": self.migrated_count,
            "verified_count": self.verified_count,
            "skipped_count": self.skipped_count,
            "failed_count": self.failed_count,
            "deleted_count": self.deleted_count,
            "trashed_count": self.trashed_count,
            "db_updated_count": self.db_updated_count,
            "total_size_bytes": self.total_size_bytes,
            "migrated_size_bytes": self.migrated_size_bytes,
            "duration_seconds": self.duration_seconds,
            "dry_run": self.dry_run,
            "errors": self.errors[:100] if self.errors else [],
            "error_count": len(self.errors),
        }


@dataclass
class DbUpdateItem:
    """单个数据库更新项"""
    table: str                       # 表名 (scm.patch_blobs / logbook.attachments)
    record_id: Optional[int] = None  # 记录 ID
    old_uri: str = ""                # 原始 URI
    new_uri: Optional[str] = None    # 转换后的 URI
    status: str = "pending"          # pending/converted/rejected/skipped
    error: Optional[str] = None      # 拒绝原因


@dataclass
class DbUpdatePreview:
    """数据库更新预览（基于迁移项精确映射）"""
    patch_blobs_count: int = 0       # patch_blobs 需更新记录数
    attachments_count: int = 0       # attachments 需更新记录数
    converted_count: int = 0         # 成功转换数
    rejected_count: int = 0          # 拒绝转换数（无法确定映射）
    skipped_count: int = 0           # 跳过数（已是目标格式）
    items: List[DbUpdateItem] = field(default_factory=list)  # 详细更新项列表
    rejected_items: List[DbUpdateItem] = field(default_factory=list)  # 拒绝转换的项
    
    # 以下字段已弃用，保留兼容性
    patch_blobs_sql: str = ""        # [已弃用] 简单前缀 SQL 预览
    attachments_sql: str = ""        # [已弃用] 简单前缀 SQL 预览
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "patch_blobs_count": self.patch_blobs_count,
            "attachments_count": self.attachments_count,
            "converted_count": self.converted_count,
            "rejected_count": self.rejected_count,
            "skipped_count": self.skipped_count,
            "rejected_uris": [item.old_uri for item in self.rejected_items[:20]],
        }


# =============================================================================
# 速率限制器
# =============================================================================


class RateLimiter:
    """
    线程安全的速率限制器
    
    用于控制迁移操作的 I/O 速率。
    """

    def __init__(self, max_bytes_per_sec: Optional[int] = None):
        self.max_bytes_per_sec = max_bytes_per_sec
        self._bytes_this_second = 0
        self._last_reset = time.monotonic()
        self._lock = threading.Lock()

    def wait_if_needed(self, bytes_processed: int) -> None:
        """如果超出速率限制则等待（线程安全）"""
        if self.max_bytes_per_sec is None or self.max_bytes_per_sec <= 0:
            return

        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_reset

            if elapsed >= 1.0:
                self._bytes_this_second = bytes_processed
                self._last_reset = now
                return

            self._bytes_this_second += bytes_processed

            if self._bytes_this_second >= self.max_bytes_per_sec:
                sleep_time = 1.0 - elapsed
                if sleep_time > 0:
                    self._bytes_this_second = 0
                    self._last_reset = time.monotonic() + sleep_time
                    self._lock.release()
                    try:
                        time.sleep(sleep_time)
                    finally:
                        self._lock.acquire()
                else:
                    self._bytes_this_second = 0
                    self._last_reset = time.monotonic()


# =============================================================================
# 迁移器
# =============================================================================


class ArtifactMigrator:
    """制品迁移器"""

    def __init__(
        self,
        source_store: ArtifactStore,
        target_store: ArtifactStore,
        source_backend: str,
        target_backend: str,
        prefix: Optional[str] = None,
        dry_run: bool = True,
        verify: bool = False,
        update_db: bool = False,
        db_update_mode: str = DB_UPDATE_MODE_NONE,
        delete_source: bool = False,
        trash_prefix: Optional[str] = None,
        limit: Optional[int] = None,
        concurrency: int = 1,
        max_bytes_per_sec: Optional[int] = None,
        prefix_mappings: Optional[Dict[str, Dict[str, str]]] = None,
        s3_uri_prefix: Optional[str] = None,
        config: Optional[Config] = None,
        verbose: bool = False,
        require_ops: bool = False,
    ):
        """
        初始化迁移器

        Args:
            source_store: 源存储后端
            target_store: 目标存储后端
            source_backend: 源后端类型
            target_backend: 目标后端类型
            prefix: 迁移前缀范围
            dry_run: 是否为预览模式
            verify: 是否校验迁移结果
            update_db: [已弃用] 是否更新数据库 URI，请使用 db_update_mode
            db_update_mode: DB 更新模式（none/to-artifact-key/to-physical-s3）
            delete_source: 是否删除源文件
            trash_prefix: 软删除目标前缀
            limit: 最大迁移数量
            concurrency: 并发数
            max_bytes_per_sec: 迁移速率限制（字节/秒）
            prefix_mappings: 物理路径前缀到 artifact key 的映射
                格式: {"file://": {"/mnt/artifacts/": ""}, "s3://": {"bucket/prefix/": ""}}
            s3_uri_prefix: S3 URI 前缀（用于 to-physical-s3 模式）
                格式: "s3://bucket/prefix/"
            config: 配置对象
            verbose: 详细输出
        """
        self.source_store = source_store
        self.target_store = target_store
        self.source_backend = source_backend
        self.target_backend = target_backend
        self.prefix = prefix or ""
        self.dry_run = dry_run
        self.verify = verify
        # 兼容旧的 update_db 参数
        if update_db and db_update_mode == DB_UPDATE_MODE_NONE:
            # 旧参数为 True，新参数未设置，默认使用 to-artifact-key
            self.db_update_mode = DB_UPDATE_MODE_TO_ARTIFACT_KEY
        else:
            self.db_update_mode = db_update_mode
        self.update_db = update_db or (db_update_mode != DB_UPDATE_MODE_NONE)
        self.delete_source = delete_source
        self.trash_prefix = trash_prefix
        self.limit = limit
        self.concurrency = max(1, concurrency)
        self.max_bytes_per_sec = max_bytes_per_sec
        self.prefix_mappings = prefix_mappings or {}
        self.s3_uri_prefix = s3_uri_prefix
        self.config = config or get_config()
        self.verbose = verbose
        self.require_ops = require_ops

        self._conn = None
        self._rate_limiter: Optional[RateLimiter] = None
        
        # 如果 require_ops=True 且 delete_source=True，检查源存储的凭证
        if self.require_ops and self.delete_source:
            self._check_ops_credentials()

    def _check_ops_credentials(self) -> None:
        """
        检查源存储是否使用 ops 凭证（用于 delete_source 操作）
        
        仅对 ObjectStore 类型的源存储进行检查。
        
        Raises:
            MigrationOpsCredentialsRequiredError: 需要 ops 凭证但未提供
        """
        if isinstance(self.source_store, ObjectStore):
            if not self.source_store.is_ops_credentials():
                raise MigrationOpsCredentialsRequiredError(
                    "指定了 --require-ops 但源存储未使用 ops 凭证。\n"
                    "请设置 ENGRAM_S3_USE_OPS=true 并配置 ENGRAM_S3_OPS_ACCESS_KEY/SECRET_KEY，"
                    "或移除 --require-ops 参数。"
                )

    def _get_conn(self):
        """获取数据库连接"""
        if self._conn is None:
            from engram_step1.db import get_connection
            self._conn = get_connection(config=self.config)
        return self._conn

    def close(self):
        """关闭资源"""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def scan_source(self) -> Iterator[MigrationItem]:
        """
        扫描源存储，返回待迁移项

        Yields:
            MigrationItem: 待迁移项
        """
        count = 0

        if isinstance(self.source_store, LocalArtifactsStore):
            # 本地存储：遍历文件系统
            scan_dir = self.source_store.root
            if self.prefix:
                scan_dir = scan_dir / self.prefix.lstrip("/")

            if not scan_dir.exists():
                logger.warning(f"源目录不存在: {scan_dir}")
                return

            for root, dirs, files in os.walk(scan_dir):
                # 跳过隐藏目录和 .tmp 目录
                dirs[:] = [d for d in dirs if not d.startswith('.')]

                for filename in files:
                    if filename.startswith('.') or filename.endswith('.tmp'):
                        continue

                    full_path = Path(root) / filename
                    try:
                        relative_path = full_path.relative_to(self.source_store.root)
                        key = str(relative_path).replace("\\", "/")

                        # 获取文件大小
                        size = full_path.stat().st_size

                        yield MigrationItem(
                            key=key,
                            source_uri=key,
                            source_size=size,
                        )

                        count += 1
                        if self.limit and count >= self.limit:
                            return

                    except Exception as e:
                        logger.warning(f"扫描文件失败: {full_path}, 错误: {e}")

        elif isinstance(self.source_store, ObjectStore):
            # 对象存储：列出对象
            try:
                client = self.source_store._get_client()
                bucket = self.source_store.bucket

                # 构建前缀
                list_prefix = self.source_store.prefix
                if self.prefix:
                    if list_prefix:
                        list_prefix = f"{list_prefix}/{self.prefix.lstrip('/')}"
                    else:
                        list_prefix = self.prefix.lstrip("/")

                paginator = client.get_paginator('list_objects_v2')
                pages = paginator.paginate(
                    Bucket=bucket,
                    Prefix=list_prefix,
                )

                for page in pages:
                    for obj in page.get('Contents', []):
                        object_key = obj['Key']

                        # 移除 store 的 prefix，得到相对 key
                        if self.source_store.prefix and object_key.startswith(self.source_store.prefix + "/"):
                            key = object_key[len(self.source_store.prefix) + 1:]
                        else:
                            key = object_key

                        yield MigrationItem(
                            key=key,
                            source_uri=key,
                            source_size=obj.get('Size', 0),
                        )

                        count += 1
                        if self.limit and count >= self.limit:
                            return

            except Exception as e:
                logger.error(f"扫描对象存储失败: {e}")
                raise MigrationError(f"扫描对象存储失败: {e}")

        elif isinstance(self.source_store, FileUriStore):
            # FileUriStore：遍历 allowed_roots + prefix
            allowed_roots = self.source_store.allowed_roots
            if not allowed_roots:
                logger.warning("FileUriStore 未配置 allowed_roots，无法扫描")
                return

            for root_path in allowed_roots:
                root = Path(root_path)
                if self.prefix:
                    scan_dir = root / self.prefix.lstrip("/")
                else:
                    scan_dir = root

                if not scan_dir.exists():
                    logger.warning(f"扫描目录不存在: {scan_dir}")
                    continue

                for dirpath, dirs, files in os.walk(scan_dir):
                    # 跳过隐藏目录和 .tmp 目录
                    dirs[:] = [d for d in dirs if not d.startswith('.')]

                    for filename in files:
                        if filename.startswith('.') or filename.endswith('.tmp'):
                            continue

                        full_path = Path(dirpath) / filename
                        try:
                            # 计算相对于 root 的路径作为 key
                            relative_path = full_path.relative_to(root)
                            key = str(relative_path).replace("\\", "/")

                            # 生成 file:// URI
                            file_uri = self.source_store._ensure_file_uri(str(full_path))

                            # 获取文件大小
                            size = full_path.stat().st_size

                            yield MigrationItem(
                                key=key,
                                source_uri=file_uri,
                                source_size=size,
                            )

                            count += 1
                            if self.limit and count >= self.limit:
                                return

                        except Exception as e:
                            logger.warning(f"扫描文件失败: {full_path}, 错误: {e}")

        else:
            raise MigrationError(f"不支持的源存储类型: {type(self.source_store)}")

    def _get_rate_limiter(self) -> Optional[RateLimiter]:
        """获取速率限制器（惰性初始化）"""
        if self._rate_limiter is None and self.max_bytes_per_sec:
            self._rate_limiter = RateLimiter(self.max_bytes_per_sec)
        return self._rate_limiter

    def migrate_item(self, item: MigrationItem) -> MigrationItem:
        """
        迁移单个制品

        Args:
            item: 待迁移项

        Returns:
            更新后的迁移项
        """
        start_time = time.time()

        # 速率限制
        rate_limiter = self._get_rate_limiter()
        if rate_limiter:
            rate_limiter.wait_if_needed(item.source_size)

        try:
            # 确定源和目标的读写 URI
            # FileUriStore 需要使用完整的 file:// URI，其他后端使用 key
            source_read_uri = item.source_uri if isinstance(self.source_store, FileUriStore) else item.key
            
            # 对于 FileUriStore 目标，构造目标 URI（基于第一个 allowed_root + key）
            if isinstance(self.target_store, FileUriStore):
                allowed_roots = self.target_store.allowed_roots
                if allowed_roots:
                    target_path = Path(allowed_roots[0]) / item.key
                    target_write_uri = self.target_store._ensure_file_uri(str(target_path))
                else:
                    target_write_uri = item.key
            else:
                target_write_uri = item.key

            # 检查目标是否已存在
            if self.target_store.exists(target_write_uri):
                if self.verify:
                    # 校验现有目标
                    target_info = self.target_store.get_info(target_write_uri)
                    source_info = self.source_store.get_info(source_read_uri)

                    if target_info["sha256"] == source_info["sha256"]:
                        item.status = "skipped"
                        item.target_uri = target_write_uri
                        item.source_sha256 = source_info["sha256"]
                        item.target_sha256 = target_info["sha256"]
                        return item
                    else:
                        # 哈希不匹配，需要重新迁移
                        logger.warning(f"目标已存在但哈希不匹配，将重新迁移: {item.key}")
                else:
                    item.status = "skipped"
                    item.target_uri = target_write_uri
                    return item

            if self.dry_run:
                # Dry-run 模式，不实际迁移
                item.status = "pending"
                item.target_uri = target_write_uri
                return item

            # 读取源文件并计算哈希
            if isinstance(self.source_store, ObjectStore) and hasattr(self.source_store, 'get_stream'):
                # 流式读取
                content_stream = self._stream_with_hash(
                    self.source_store.get_stream(source_read_uri),
                    item,
                )
                result = self.target_store.put(target_write_uri, content_stream)
            else:
                # 完整读取
                content = self.source_store.get(source_read_uri)
                item.source_sha256 = hashlib.sha256(content).hexdigest()
                result = self.target_store.put(target_write_uri, content)

            item.target_uri = result.get("uri", target_write_uri)
            item.target_sha256 = result.get("sha256")
            item.status = "migrated"

            # 校验
            if self.verify:
                if item.source_sha256 and item.target_sha256:
                    if item.source_sha256 != item.target_sha256:
                        item.status = "failed"
                        item.error = f"哈希不匹配: source={item.source_sha256}, target={item.target_sha256}"
                    else:
                        item.status = "verified"
                else:
                    # 需要重新获取信息进行校验
                    target_info = self.target_store.get_info(target_write_uri)
                    if not item.source_sha256:
                        source_info = self.source_store.get_info(source_read_uri)
                        item.source_sha256 = source_info["sha256"]

                    if item.source_sha256 != target_info["sha256"]:
                        item.status = "failed"
                        item.error = f"哈希不匹配: source={item.source_sha256}, target={target_info['sha256']}"
                    else:
                        item.status = "verified"

        except ArtifactNotFoundError:
            item.status = "failed"
            item.error = "源文件不存在"
        except Exception as e:
            item.status = "failed"
            item.error = str(e)
            logger.warning(f"迁移失败 {item.key}: {e}")

        finally:
            item.duration_ms = (time.time() - start_time) * 1000

        return item

    def _stream_with_hash(
        self,
        stream: Iterator[bytes],
        item: MigrationItem,
    ) -> Iterator[bytes]:
        """
        包装流式读取，同时计算哈希

        Args:
            stream: 源数据流
            item: 迁移项（用于更新哈希）

        Yields:
            bytes: 数据块
        """
        hasher = hashlib.sha256()
        for chunk in stream:
            hasher.update(chunk)
            yield chunk
        item.source_sha256 = hasher.hexdigest()

    def delete_source_item(self, item: MigrationItem) -> bool:
        """
        删除或移动源文件

        Args:
            item: 已迁移的项

        Returns:
            是否成功
        """
        if self.dry_run:
            return True

        try:
            if isinstance(self.source_store, LocalArtifactsStore):
                source_path = self.source_store.root / item.key

                if self.trash_prefix:
                    # 移动到 trash
                    trash_path = self.source_store.root / self.trash_prefix / item.key
                    trash_path.parent.mkdir(parents=True, exist_ok=True)
                    source_path.rename(trash_path)
                else:
                    # 直接删除
                    source_path.unlink()
                return True

            elif isinstance(self.source_store, ObjectStore):
                if self.trash_prefix:
                    # 复制到 trash 然后删除
                    client = self.source_store._get_client()
                    bucket = self.source_store.bucket
                    source_key = self.source_store._object_key(item.key)
                    trash_key = f"{self.trash_prefix}/{item.key}"

                    # 复制
                    client.copy_object(
                        CopySource={'Bucket': bucket, 'Key': source_key},
                        Bucket=bucket,
                        Key=trash_key,
                    )
                    # 删除源
                    client.delete_object(Bucket=bucket, Key=source_key)
                else:
                    # 直接删除
                    client = self.source_store._get_client()
                    bucket = self.source_store.bucket
                    source_key = self.source_store._object_key(item.key)
                    client.delete_object(Bucket=bucket, Key=source_key)
                return True

            elif isinstance(self.source_store, FileUriStore):
                # FileUriStore: 使用 source_uri (file:// URI) 解析路径
                source_path = self.source_store._parse_file_uri(item.source_uri)

                if self.trash_prefix:
                    # 移动到 trash（基于第一个 allowed_root）
                    allowed_roots = self.source_store.allowed_roots
                    if allowed_roots:
                        trash_root = Path(allowed_roots[0]) / self.trash_prefix
                    else:
                        trash_root = source_path.parent / self.trash_prefix
                    
                    trash_path = trash_root / item.key
                    trash_path.parent.mkdir(parents=True, exist_ok=True)
                    source_path.rename(trash_path)
                else:
                    # 直接删除
                    source_path.unlink()
                return True

        except Exception as e:
            logger.warning(f"删除源文件失败 {item.key}: {e}")
            return False

        return False

    def preview_db_update(self, migrated_keys: Optional[List[str]] = None) -> DbUpdatePreview:
        """
        预览数据库更新（基于迁移项精确映射）

        根据 db_update_mode 分析数据库中的 URI，确定哪些可以转换、哪些需要拒绝。

        Args:
            migrated_keys: 已迁移的 key 列表（可选，用于交叉验证）

        Returns:
            DbUpdatePreview: 包含详细更新项列表的预览
        """
        preview = DbUpdatePreview()

        if self.db_update_mode == DB_UPDATE_MODE_NONE:
            return preview

        conn = self._get_conn()

        try:
            with conn.cursor() as cur:
                # 查询 patch_blobs 表中的 URI
                prefix_pattern = f"{self.prefix}%" if self.prefix else "%"
                
                cur.execute(
                    """
                    SELECT id, uri FROM scm.patch_blobs
                    WHERE uri LIKE %s OR uri LIKE %s OR uri LIKE %s OR uri LIKE %s
                    """,
                    (prefix_pattern, f"artifact://{prefix_pattern}", 
                     f"file://%{prefix_pattern}", f"s3://%{prefix_pattern}")
                )
                patch_blobs_rows = cur.fetchall()

                # 查询 attachments 表中的 URI
                cur.execute(
                    """
                    SELECT id, uri FROM logbook.attachments
                    WHERE uri LIKE %s OR uri LIKE %s OR uri LIKE %s OR uri LIKE %s
                    """,
                    (prefix_pattern, f"artifact://{prefix_pattern}", 
                     f"file://%{prefix_pattern}", f"s3://%{prefix_pattern}")
                )
                attachments_rows = cur.fetchall()

            # 处理 patch_blobs
            for record_id, uri in patch_blobs_rows:
                item = self._convert_uri_for_db_update(
                    table="scm.patch_blobs",
                    record_id=record_id,
                    uri=uri,
                )
                if item.status == "converted":
                    preview.converted_count += 1
                    preview.patch_blobs_count += 1
                    preview.items.append(item)
                elif item.status == "rejected":
                    preview.rejected_count += 1
                    preview.rejected_items.append(item)
                else:  # skipped
                    preview.skipped_count += 1

            # 处理 attachments
            for record_id, uri in attachments_rows:
                item = self._convert_uri_for_db_update(
                    table="logbook.attachments",
                    record_id=record_id,
                    uri=uri,
                )
                if item.status == "converted":
                    preview.converted_count += 1
                    preview.attachments_count += 1
                    preview.items.append(item)
                elif item.status == "rejected":
                    preview.rejected_count += 1
                    preview.rejected_items.append(item)
                else:  # skipped
                    preview.skipped_count += 1

        except Exception as e:
            logger.warning(f"预览数据库更新失败: {e}")

        return preview

    def _convert_uri_for_db_update(
        self,
        table: str,
        record_id: int,
        uri: str,
    ) -> DbUpdateItem:
        """
        根据 db_update_mode 转换单个 URI

        Args:
            table: 表名
            record_id: 记录 ID
            uri: 原始 URI

        Returns:
            DbUpdateItem: 转换结果
        """
        item = DbUpdateItem(
            table=table,
            record_id=record_id,
            old_uri=uri,
        )

        if self.db_update_mode == DB_UPDATE_MODE_TO_ARTIFACT_KEY:
            # to-artifact-key 模式：将各种 URI 归一为无 scheme 的 artifact key
            return self._convert_to_artifact_key(item)
        
        elif self.db_update_mode == DB_UPDATE_MODE_TO_PHYSICAL_S3:
            # to-physical-s3 模式：将 artifact key 转换为 s3:// URI
            return self._convert_to_physical_s3(item)
        
        else:
            item.status = "skipped"
            return item

    def _convert_to_artifact_key(self, item: DbUpdateItem) -> DbUpdateItem:
        """
        将 URI 转换为 artifact key（to-artifact-key 模式）

        转换规则:
        1. artifact://... -> 移除 scheme，归一为无 scheme
        2. 无 scheme（已是 artifact key）-> 保持不变，跳过
        3. file:// 或 s3:// -> 需要通过 prefix_mappings 确定映射
           - 能确定映射：转换为 artifact key
           - 无法确定：拒绝并记录
        """
        uri = item.old_uri
        parsed = parse_uri(uri)
        
        # 情况1: 无 scheme，已是 artifact key -> 跳过
        if parsed.scheme is None:
            item.status = "skipped"
            return item
        
        # 情况2: artifact:// scheme -> 移除 scheme
        if parsed.scheme == "artifact":
            item.new_uri = strip_artifact_scheme(uri)
            item.status = "converted"
            return item
        
        # 情况3: physical uri (file://, s3://) -> 需要映射
        if parsed.scheme in ("file", "s3", "gs"):
            result = try_convert_to_artifact_key(uri, self.prefix_mappings)
            if result.success:
                item.new_uri = result.converted_uri
                item.status = "converted"
            else:
                item.status = "rejected"
                item.error = result.error
            return item
        
        # 情况4: 其他 scheme (http, memory 等) -> 跳过
        item.status = "skipped"
        return item

    def _convert_to_physical_s3(self, item: DbUpdateItem) -> DbUpdateItem:
        """
        将 artifact key 转换为 s3:// URI（to-physical-s3 模式）

        转换规则:
        1. 无 scheme 或 artifact:// -> 添加 s3:// 前缀
        2. 已是 s3:// -> 跳过
        3. file:// 或其他 -> 拒绝
        """
        uri = item.old_uri
        parsed = parse_uri(uri)
        
        # 检查是否配置了 s3_uri_prefix
        if not self.s3_uri_prefix:
            item.status = "rejected"
            item.error = "未配置 s3_uri_prefix，无法转换为 s3:// URI"
            return item
        
        # 情况1: 已是 s3:// -> 跳过
        if parsed.scheme == "s3":
            item.status = "skipped"
            return item
        
        # 情况2: 无 scheme 或 artifact:// -> 转换为 s3://
        if parsed.scheme is None or parsed.scheme == "artifact":
            artifact_key = strip_artifact_scheme(uri)
            # 构建 s3:// URI
            s3_prefix = self.s3_uri_prefix.rstrip("/")
            item.new_uri = f"{s3_prefix}/{artifact_key}"
            item.status = "converted"
            return item
        
        # 情况3: 其他 scheme -> 拒绝
        item.status = "rejected"
        item.error = f"不支持将 {parsed.scheme}:// 转换为 s3://"
        return item

    def update_db_uris(
        self,
        migrated_items: Optional[List[MigrationItem]] = None,
        uri_mapping: Optional[Dict[str, str]] = None,
        preview: Optional[DbUpdatePreview] = None,
    ) -> int:
        """
        更新数据库中的 URI 引用

        基于 db_update_mode 和预览结果执行精确更新。

        Args:
            migrated_items: 已迁移的项列表（兼容旧逻辑）
            uri_mapping: 旧 URI -> 新 URI 映射（可选，兼容旧逻辑）
            preview: 预览结果（优先使用，包含精确映射）

        Returns:
            更新的记录数
        """
        if self.dry_run:
            return 0

        if self.db_update_mode == DB_UPDATE_MODE_NONE:
            return 0

        # 如果提供了预览结果，使用精确映射更新
        if preview and preview.items:
            return self._execute_db_update_from_preview(preview)

        # 兼容旧逻辑：基于迁移项更新
        if migrated_items:
            return self._execute_db_update_legacy(migrated_items, uri_mapping)

        # 无数据可更新
        return 0

    def _execute_db_update_from_preview(self, preview: DbUpdatePreview) -> int:
        """
        根据预览结果执行数据库更新

        Args:
            preview: 预览结果

        Returns:
            更新的记录数
        """
        conn = self._get_conn()
        updated_count = 0

        try:
            with conn.cursor() as cur:
                for item in preview.items:
                    if item.status != "converted" or not item.new_uri:
                        continue

                    if item.table == "scm.patch_blobs":
                        cur.execute(
                            """
                            UPDATE scm.patch_blobs
                            SET uri = %s, updated_at = now()
                            WHERE id = %s AND uri = %s
                            """,
                            (item.new_uri, item.record_id, item.old_uri)
                        )
                    elif item.table == "logbook.attachments":
                        cur.execute(
                            """
                            UPDATE logbook.attachments
                            SET uri = %s
                            WHERE id = %s AND uri = %s
                            """,
                            (item.new_uri, item.record_id, item.old_uri)
                        )
                    
                    updated_count += cur.rowcount

                conn.commit()

        except Exception as e:
            conn.rollback()
            raise MigrationDbUpdateError(f"更新数据库失败: {e}")

        return updated_count

    def _execute_db_update_legacy(
        self,
        migrated_items: List[MigrationItem],
        uri_mapping: Optional[Dict[str, str]] = None,
    ) -> int:
        """
        兼容旧逻辑：基于迁移项更新数据库

        Args:
            migrated_items: 已迁移的项列表
            uri_mapping: 旧 URI -> 新 URI 映射

        Returns:
            更新的记录数
        """
        conn = self._get_conn()
        updated_count = 0

        try:
            with conn.cursor() as cur:
                for item in migrated_items:
                    if item.status not in ("migrated", "verified"):
                        continue

                    old_uri = item.source_uri
                    new_uri = item.target_uri or item.key

                    # 如果提供了映射，使用映射
                    if uri_mapping and old_uri in uri_mapping:
                        new_uri = uri_mapping[old_uri]

                    # 更新 patch_blobs
                    cur.execute(
                        """
                        UPDATE scm.patch_blobs
                        SET uri = %s, updated_at = now()
                        WHERE uri = %s AND uri != %s
                        """,
                        (new_uri, old_uri, new_uri)
                    )
                    updated_count += cur.rowcount

                    # 更新 attachments
                    cur.execute(
                        """
                        UPDATE logbook.attachments
                        SET uri = %s
                        WHERE uri = %s AND uri != %s
                        """,
                        (new_uri, old_uri, new_uri)
                    )
                    updated_count += cur.rowcount

                conn.commit()

        except Exception as e:
            conn.rollback()
            raise MigrationDbUpdateError(f"更新数据库失败: {e}")

        return updated_count

    def run(self) -> MigrationResult:
        """
        执行迁移

        Returns:
            MigrationResult: 迁移结果
        """
        start_time = time.time()
        result = MigrationResult(dry_run=self.dry_run)

        # 扫描源文件
        logger.info(f"开始扫描源存储 (backend={self.source_backend}, prefix={self.prefix})")
        items = list(self.scan_source())
        result.scanned_count = len(items)
        result.total_size_bytes = sum(item.source_size for item in items)

        logger.info(f"扫描完成: {result.scanned_count} 个文件, 总大小 {result.total_size_bytes} 字节")

        if not items:
            result.duration_seconds = time.time() - start_time
            return result

        # 执行迁移
        if self.concurrency > 1 and not self.dry_run:
            # 并发迁移
            with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
                futures = {
                    executor.submit(self.migrate_item, item): item
                    for item in items
                }

                for future in as_completed(futures):
                    try:
                        migrated_item = future.result()
                        result.items.append(migrated_item)

                        if migrated_item.status == "migrated":
                            result.migrated_count += 1
                            result.migrated_size_bytes += migrated_item.source_size
                        elif migrated_item.status == "verified":
                            result.migrated_count += 1
                            result.verified_count += 1
                            result.migrated_size_bytes += migrated_item.source_size
                        elif migrated_item.status == "skipped":
                            result.skipped_count += 1
                        elif migrated_item.status == "failed":
                            result.failed_count += 1
                            result.errors.append({
                                "key": migrated_item.key,
                                "error": migrated_item.error,
                            })

                        if self.verbose:
                            logger.info(f"迁移 {migrated_item.key}: {migrated_item.status}")

                    except Exception as e:
                        result.failed_count += 1
                        result.errors.append({"error": str(e)})
        else:
            # 串行迁移
            for item in items:
                migrated_item = self.migrate_item(item)
                result.items.append(migrated_item)

                if migrated_item.status == "migrated":
                    result.migrated_count += 1
                    result.migrated_size_bytes += migrated_item.source_size
                elif migrated_item.status == "verified":
                    result.migrated_count += 1
                    result.verified_count += 1
                    result.migrated_size_bytes += migrated_item.source_size
                elif migrated_item.status == "skipped":
                    result.skipped_count += 1
                elif migrated_item.status == "failed":
                    result.failed_count += 1
                    result.errors.append({
                        "key": migrated_item.key,
                        "error": migrated_item.error,
                    })

                if self.verbose:
                    logger.info(f"迁移 {migrated_item.key}: {migrated_item.status}")

        # 删除/移动源文件
        if self.delete_source and not self.dry_run:
            for item in result.items:
                if item.status in ("migrated", "verified"):
                    if self.delete_source_item(item):
                        if self.trash_prefix:
                            result.trashed_count += 1
                        else:
                            result.deleted_count += 1

        # 更新数据库
        if self.db_update_mode != DB_UPDATE_MODE_NONE and not self.dry_run:
            try:
                # 使用新的精确映射预览逻辑
                db_preview = self.preview_db_update()
                
                if db_preview.rejected_count > 0:
                    logger.warning(
                        f"数据库更新预览: {db_preview.rejected_count} 条记录无法转换，将跳过"
                    )
                    for rejected_item in db_preview.rejected_items[:10]:
                        logger.warning(f"  - {rejected_item.old_uri}: {rejected_item.error}")
                    if db_preview.rejected_count > 10:
                        logger.warning(f"  ... 还有 {db_preview.rejected_count - 10} 条记录")
                
                result.db_updated_count = self.update_db_uris(preview=db_preview)
                logger.info(f"数据库更新完成: {result.db_updated_count} 条记录")
            except MigrationDbUpdateError as e:
                result.errors.append({"db_update_error": str(e)})
        elif self.update_db and not self.dry_run:
            # 兼容旧逻辑
            try:
                result.db_updated_count = self.update_db_uris(migrated_items=result.items)
                logger.info(f"数据库更新完成: {result.db_updated_count} 条记录")
            except MigrationDbUpdateError as e:
                result.errors.append({"db_update_error": str(e)})

        result.duration_seconds = time.time() - start_time

        logger.info(
            f"迁移完成: 成功 {result.migrated_count}, 跳过 {result.skipped_count}, "
            f"失败 {result.failed_count}, 耗时 {result.duration_seconds:.2f}s"
        )

        return result


# =============================================================================
# 工厂函数
# =============================================================================


def create_migrator(
    source_backend: str,
    target_backend: str,
    source_root: Optional[str] = None,
    target_root: Optional[str] = None,
    source_config: Optional[Dict[str, Any]] = None,
    target_config: Optional[Dict[str, Any]] = None,
    prefix: Optional[str] = None,
    dry_run: bool = True,
    verify: bool = False,
    update_db: bool = False,
    db_update_mode: str = DB_UPDATE_MODE_NONE,
    delete_source: bool = False,
    trash_prefix: Optional[str] = None,
    limit: Optional[int] = None,
    concurrency: int = 1,
    max_bytes_per_sec: Optional[int] = None,
    prefix_mappings: Optional[Dict[str, Dict[str, str]]] = None,
    s3_uri_prefix: Optional[str] = None,
    config: Optional[Config] = None,
    verbose: bool = False,
    require_ops: bool = False,
) -> ArtifactMigrator:
    """
    创建迁移器实例

    Args:
        source_backend: 源后端类型
        target_backend: 目标后端类型
        source_root: 源存储根目录（local 后端）
        target_root: 目标存储根目录（local 后端）
        source_config: 源后端额外配置
        target_config: 目标后端额外配置
        prefix: 迁移前缀
        dry_run: 是否为预览模式
        verify: 是否校验
        update_db: [已弃用] 是否更新数据库，请使用 db_update_mode
        db_update_mode: DB 更新模式（none/to-artifact-key/to-physical-s3）
        delete_source: 是否删除源
        trash_prefix: 软删除前缀
        limit: 最大数量
        concurrency: 并发数
        max_bytes_per_sec: 速率限制
        prefix_mappings: 物理路径前缀到 artifact key 的映射
        s3_uri_prefix: S3 URI 前缀（用于 to-physical-s3 模式）
        config: 配置对象
        verbose: 详细输出

    Returns:
        ArtifactMigrator 实例
    """
    source_kwargs = source_config or {}
    if source_root:
        source_kwargs["root"] = source_root

    target_kwargs = target_config or {}
    if target_root:
        target_kwargs["root"] = target_root

    source_store = get_artifact_store(backend=source_backend, **source_kwargs)
    target_store = get_artifact_store(backend=target_backend, **target_kwargs)

    return ArtifactMigrator(
        source_store=source_store,
        target_store=target_store,
        source_backend=source_backend,
        target_backend=target_backend,
        prefix=prefix,
        dry_run=dry_run,
        verify=verify,
        update_db=update_db,
        db_update_mode=db_update_mode,
        delete_source=delete_source,
        trash_prefix=trash_prefix,
        limit=limit,
        concurrency=concurrency,
        max_bytes_per_sec=max_bytes_per_sec,
        prefix_mappings=prefix_mappings,
        s3_uri_prefix=s3_uri_prefix,
        config=config,
        verbose=verbose,
        require_ops=require_ops,
    )


def run_migration(
    source_backend: str,
    target_backend: str,
    source_root: Optional[str] = None,
    target_root: Optional[str] = None,
    source_config: Optional[Dict[str, Any]] = None,
    target_config: Optional[Dict[str, Any]] = None,
    prefix: Optional[str] = None,
    dry_run: bool = True,
    verify: bool = False,
    update_db: bool = False,
    db_update_mode: str = DB_UPDATE_MODE_NONE,
    delete_source: bool = False,
    trash_prefix: Optional[str] = None,
    limit: Optional[int] = None,
    concurrency: int = 1,
    max_bytes_per_sec: Optional[int] = None,
    prefix_mappings: Optional[Dict[str, Dict[str, str]]] = None,
    s3_uri_prefix: Optional[str] = None,
    config: Optional[Config] = None,
    verbose: bool = False,
    require_ops: bool = False,
) -> MigrationResult:
    """
    执行迁移（便捷函数）

    Args:
        source_backend: 源后端类型
        target_backend: 目标后端类型
        ... 其他参数同 create_migrator

    Returns:
        MigrationResult: 迁移结果
    """
    migrator = create_migrator(
        source_backend=source_backend,
        target_backend=target_backend,
        source_root=source_root,
        target_root=target_root,
        source_config=source_config,
        target_config=target_config,
        prefix=prefix,
        dry_run=dry_run,
        verify=verify,
        update_db=update_db,
        db_update_mode=db_update_mode,
        delete_source=delete_source,
        trash_prefix=trash_prefix,
        limit=limit,
        concurrency=concurrency,
        max_bytes_per_sec=max_bytes_per_sec,
        prefix_mappings=prefix_mappings,
        s3_uri_prefix=s3_uri_prefix,
        config=config,
        verbose=verbose,
        require_ops=require_ops,
    )

    try:
        return migrator.run()
    finally:
        migrator.close()


# =============================================================================
# CLI 入口
# =============================================================================


def main() -> int:
    """CLI 入口"""
    parser = argparse.ArgumentParser(
        description="制品存储后端迁移工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # Dry-run 预览迁移
  python artifact_migrate.py --source-backend local --target-backend object --prefix scm/

  # 实际迁移并校验
  python artifact_migrate.py --source-backend local --target-backend object --prefix scm/ --verify --execute

  # [已弃用] 迁移并更新数据库（请使用 --db-update-mode）
  python artifact_migrate.py --source-backend local --target-backend object --prefix scm/ --update-db --execute

  # 迁移并将 DB 中 URI 归一为 artifact key（推荐）
  python artifact_migrate.py --source-backend local --target-backend object --prefix scm/ \\
    --db-update-mode to-artifact-key --execute

  # 将 file:// URI 归一为 artifact key（需指定前缀映射）
  python artifact_migrate.py --source-backend file --target-backend object --prefix scm/ \\
    --db-update-mode to-artifact-key \\
    --prefix-mapping "file:///mnt/artifacts/=" --execute

  # 将 artifact key 转换为 s3:// URI
  python artifact_migrate.py --source-backend local --target-backend object --prefix scm/ \\
    --db-update-mode to-physical-s3 --s3-uri-prefix s3://my-bucket/engram/ --execute

  # 4 线程并发迁移，限速 10MB/s
  python artifact_migrate.py --source-backend local --target-backend object --prefix scm/ \\
    --concurrency 4 --max-bytes-per-sec 10485760 --execute

  # 迁移后删除源文件
  python artifact_migrate.py --source-backend local --target-backend object --prefix scm/ \\
    --delete-source --execute

  # 迁移后将源文件移动到 trash
  python artifact_migrate.py --source-backend local --target-backend object --prefix scm/ \\
    --delete-source --trash-prefix .trash/ --execute

  # file:// 后端迁移（NFS/SMB 共享）
  python artifact_migrate.py --source-backend file --target-backend local \\
    --source-uri-root /mnt/nfs/artifacts --prefix scm/ --target-root /local/artifacts

  # file:// 到 file:// 迁移（跨 NFS 共享）
  python artifact_migrate.py --source-backend file --target-backend file \\
    --source-uri-root /mnt/nfs1/artifacts --target-uri-root /mnt/nfs2/artifacts \\
    --prefix scm/ --execute

  # file:// 到 object 迁移
  python artifact_migrate.py --source-backend file --target-backend object \\
    --source-uri-root /mnt/nfs/artifacts --prefix scm/ --execute

DB 更新模式 (--db-update-mode):
  none            不更新数据库 URI（默认）
  to-artifact-key 将 DB 中的 URI 归一为无 scheme 的 artifact key:
                  - artifact://xxx -> xxx（移除 scheme）
                  - file:// / s3:// -> 需要 --prefix-mapping 确定映射
  to-physical-s3  将 artifact key 转换为 s3:// URI:
                  - xxx -> s3://bucket/prefix/xxx
                  - 需要 --s3-uri-prefix 指定目标前缀

环境变量:
  ENGRAM_ARTIFACTS_ROOT     源/目标制品根目录（local 后端）
  ENGRAM_S3_ENDPOINT        S3/MinIO 端点（object 后端）
  ENGRAM_S3_ACCESS_KEY      S3 访问密钥
  ENGRAM_S3_SECRET_KEY      S3 密钥
  ENGRAM_S3_BUCKET          S3 存储桶
        """
    )

    parser.add_argument(
        "--source-backend",
        choices=["local", "file", "object"],
        required=True,
        help="源存储后端类型: local (本地目录), file (file:// URI), object (S3/MinIO)"
    )

    parser.add_argument(
        "--target-backend",
        choices=["local", "file", "object"],
        required=True,
        help="目标存储后端类型: local (本地目录), file (file:// URI), object (S3/MinIO)"
    )

    parser.add_argument(
        "--prefix",
        default="",
        help="迁移前缀范围（如 scm/ 或 attachments/）"
    )

    parser.add_argument(
        "--source-root",
        help="源存储根目录（local 后端）"
    )

    parser.add_argument(
        "--target-root",
        help="目标存储根目录（local 后端）"
    )

    # file 后端参数
    parser.add_argument(
        "--source-uri-root",
        action="append",
        dest="source_uri_roots",
        metavar="PATH",
        help="源 file:// 后端的 allowed_roots（可多次指定），如 /mnt/nfs/artifacts"
    )

    parser.add_argument(
        "--target-uri-root",
        action="append",
        dest="target_uri_roots",
        metavar="PATH",
        help="目标 file:// 后端的 allowed_roots（可多次指定），如 /mnt/nfs/artifacts"
    )

    parser.add_argument(
        "--execute",
        action="store_true",
        help="执行实际迁移（默认为 dry-run 模式）"
    )

    parser.add_argument(
        "--verify",
        action="store_true",
        help="迁移后校验 SHA256 哈希"
    )

    parser.add_argument(
        "--update-db",
        action="store_true",
        help="[已弃用] 迁移后更新数据库 URI 引用，请使用 --db-update-mode"
    )

    parser.add_argument(
        "--db-update-mode",
        choices=DB_UPDATE_MODES,
        default=DB_UPDATE_MODE_NONE,
        help="DB 更新模式: none（不更新）, to-artifact-key（归一为 artifact key）, to-physical-s3（转为 s3:// URI）"
    )

    parser.add_argument(
        "--prefix-mapping",
        action="append",
        dest="prefix_mappings",
        metavar="MAPPING",
        help="物理路径前缀到 artifact key 的映射，格式: 'scheme://physical_prefix=artifact_prefix'，如 'file:///mnt/artifacts/='"
    )

    parser.add_argument(
        "--s3-uri-prefix",
        metavar="URI",
        help="S3 URI 前缀（用于 to-physical-s3 模式），如 's3://my-bucket/engram/'"
    )

    parser.add_argument(
        "--delete-source",
        action="store_true",
        help="迁移成功后删除源文件"
    )

    parser.add_argument(
        "--trash-prefix",
        metavar="PREFIX",
        help="软删除目标前缀（与 --delete-source 配合使用）"
    )

    parser.add_argument(
        "--require-ops",
        action="store_true",
        help="强制要求使用 ops 凭证（删除源文件时，针对 object 后端）。\n"
             "需设置 ENGRAM_S3_USE_OPS=true 并配置 ENGRAM_S3_OPS_ACCESS_KEY/SECRET_KEY"
    )

    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="最大迁移数量"
    )

    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        metavar="N",
        help="并发迁移线程数（默认 1，即串行）"
    )

    parser.add_argument(
        "--max-bytes-per-sec",
        type=int,
        metavar="BYTES",
        help="迁移速率限制（字节/秒）"
    )

    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="详细输出"
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果"
    )

    args = parser.parse_args()

    # 配置日志
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 验证参数
    if args.trash_prefix and not args.delete_source:
        print("警告: --trash-prefix 需要与 --delete-source 配合使用", file=sys.stderr)

    if args.concurrency < 1:
        print(f"错误: --concurrency 必须 >= 1，当前值: {args.concurrency}", file=sys.stderr)
        return 2

    # 验证 db-update-mode 相关参数
    if args.db_update_mode == DB_UPDATE_MODE_TO_PHYSICAL_S3 and not args.s3_uri_prefix:
        print("错误: to-physical-s3 模式需要指定 --s3-uri-prefix", file=sys.stderr)
        return 2

    # dry-run 模式
    dry_run = not args.execute

    # 构建源后端配置
    source_config = {}
    if args.source_backend == BACKEND_FILE:
        if args.source_uri_roots:
            source_config["allowed_roots"] = args.source_uri_roots
        else:
            print("错误: file 后端需要指定 --source-uri-root", file=sys.stderr)
            return 2

    # 构建目标后端配置
    target_config = {}
    if args.target_backend == BACKEND_FILE:
        if args.target_uri_roots:
            target_config["allowed_roots"] = args.target_uri_roots
        else:
            print("错误: file 后端需要指定 --target-uri-root", file=sys.stderr)
            return 2

    # 解析前缀映射
    # 格式: "scheme://physical_prefix=artifact_prefix"
    # 例如: "file:///mnt/artifacts/=" 表示 file:///mnt/artifacts/xxx -> xxx
    prefix_mappings: Dict[str, Dict[str, str]] = {}
    if args.prefix_mappings:
        for mapping in args.prefix_mappings:
            if "=" not in mapping:
                print(f"错误: 前缀映射格式无效: {mapping}，应为 'scheme://prefix=artifact_prefix'", file=sys.stderr)
                return 2
            
            physical_part, artifact_prefix = mapping.split("=", 1)
            
            # 解析 scheme 和 physical_prefix
            if "://" in physical_part:
                scheme_end = physical_part.index("://") + 3
                scheme_key = physical_part[:scheme_end]  # 如 "file://"
                physical_prefix = physical_part[scheme_end:]  # 如 "/mnt/artifacts/"
                
                # 确保 physical_prefix 以 / 开头（file:// 的路径）
                if scheme_key == "file://" and not physical_prefix.startswith("/"):
                    physical_prefix = "/" + physical_prefix
            else:
                print(f"错误: 前缀映射缺少 scheme: {mapping}", file=sys.stderr)
                return 2
            
            if scheme_key not in prefix_mappings:
                prefix_mappings[scheme_key] = {}
            prefix_mappings[scheme_key][physical_prefix] = artifact_prefix

    try:
        result = run_migration(
            source_backend=args.source_backend,
            target_backend=args.target_backend,
            source_root=args.source_root,
            target_root=args.target_root,
            source_config=source_config if source_config else None,
            target_config=target_config if target_config else None,
            prefix=args.prefix,
            dry_run=dry_run,
            verify=args.verify,
            update_db=args.update_db,
            db_update_mode=args.db_update_mode,
            delete_source=args.delete_source,
            trash_prefix=args.trash_prefix,
            limit=args.limit,
            concurrency=args.concurrency,
            max_bytes_per_sec=args.max_bytes_per_sec,
            prefix_mappings=prefix_mappings if prefix_mappings else None,
            s3_uri_prefix=args.s3_uri_prefix,
            verbose=args.verbose,
            require_ops=args.require_ops,
        )

        if args.json:
            print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        else:
            # 打印摘要
            print("\n" + "=" * 60)
            print("迁移执行摘要")
            print("=" * 60)
            print(f"  模式:         {'dry-run' if dry_run else '实际执行'}")
            print(f"  源后端:       {args.source_backend}")
            print(f"  目标后端:     {args.target_backend}")
            print(f"  迁移前缀:     {args.prefix or '(全部)'}")
            print(f"  扫描文件数:   {result.scanned_count}")
            print(f"  总大小:       {result.total_size_bytes / 1024 / 1024:.2f} MB")
            print("-" * 60)
            print(f"  迁移成功:     {result.migrated_count}")
            print(f"  校验通过:     {result.verified_count}")
            print(f"  跳过:         {result.skipped_count}")
            print(f"  失败:         {result.failed_count}")
            if args.delete_source:
                print(f"  已删除源:     {result.deleted_count}")
                print(f"  已移动源:     {result.trashed_count}")
            if args.db_update_mode != DB_UPDATE_MODE_NONE or args.update_db:
                print(f"  DB 更新模式:  {args.db_update_mode}")
                print(f"  DB 更新:      {result.db_updated_count}")
            print("-" * 60)
            print(f"  已迁移大小:   {result.migrated_size_bytes / 1024 / 1024:.2f} MB")
            print(f"  耗时:         {result.duration_seconds:.2f} 秒")
            if result.duration_seconds > 0:
                throughput = result.migrated_size_bytes / result.duration_seconds / 1024 / 1024
                print(f"  吞吐量:       {throughput:.2f} MB/s")

            if result.errors:
                print("-" * 60)
                print("错误列表:")
                for err in result.errors[:10]:
                    if isinstance(err, dict):
                        print(f"  - {err.get('key', 'unknown')}: {err.get('error', 'unknown')}")
                    else:
                        print(f"  - {err}")
                if len(result.errors) > 10:
                    print(f"  ... 还有 {len(result.errors) - 10} 个错误")

            print("=" * 60)

        # 返回码
        if result.failed_count > 0:
            return 1
        return 0

    except MigrationOpsCredentialsRequiredError as e:
        print(f"凭证错误: {e}", file=sys.stderr)
        print("\n提示: 设置 ENGRAM_S3_USE_OPS=true 来使用 ops 凭证", file=sys.stderr)
        return 5
    except MigrationError as e:
        print(f"迁移错误: {e}", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"未知错误: {e}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
