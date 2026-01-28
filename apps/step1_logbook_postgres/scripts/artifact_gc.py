#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
artifact_gc.py - 制品垃圾回收脚本

功能:
    支持两种 GC 模式:

    1. 孤立清理模式（--prefix）:
       扫描 artifacts_root（local/file）或 bucket prefix（object），
       对比数据库中的引用集合，输出并可选删除未引用的制品文件。

    2. Tmp 清理模式（--tmp-prefix）:
       仅基于文件年龄清理 tmp 目录下的临时文件，不依赖数据库。
       适用于清理构建过程中产生的临时文件。

安全特性:
    - 默认 dry-run 模式，仅显示待删除列表
    - 严格的路径前缀限制，防止越权删除
    - 支持软删除（移动到 trash 目录）
    - 被数据库引用的制品始终受到保护（孤立清理模式）

Object 后端软删除注意事项:
    - 软删除时使用 MetadataDirective='COPY' 保留所有用户元数据（包括 x-amz-meta-sha256）
    - S3 CopyObject 会保留用户自定义元数据，但某些系统元数据可能需要显式处理
    - 如需验证元数据完整性，请在恢复前检查 trash 对象的 HeadObject 响应

使用示例:
    # ========== 孤立清理模式（基于 DB 引用）==========
    # 仅扫描并显示待删除文件（dry-run 模式）
    python artifact_gc.py --prefix scm/

    # 软删除（推荐，移动到 trash 目录）
    python artifact_gc.py --prefix scm/ --trash-prefix .trash/ --delete

    # 仅删除 30 天前的文件（软删除）
    python artifact_gc.py --prefix scm/ --older-than-days 30 --trash-prefix .trash/ --delete

    # 强制要求软删除（生产安全策略）
    python artifact_gc.py --prefix scm/ --require-trash --trash-prefix .trash/ --delete

    # 硬删除（需显式确认，生产环境不推荐）
    python artifact_gc.py --prefix scm/ --delete --force-hard-delete

    # ========== Tmp 清理模式（不依赖 DB）==========
    # 清理 tmp/ 目录下超过 7 天的文件
    python artifact_gc.py --tmp-prefix tmp/ --tmp-older-than-days 7

    # 执行实际删除
    python artifact_gc.py --tmp-prefix tmp/ --tmp-older-than-days 7 --delete

    # JSON 输出（包含 backend/bucket/prefix 元信息）
    python artifact_gc.py --tmp-prefix tmp/ --tmp-older-than-days 1 --json

生产安全策略:
    - 默认要求 --trash-prefix 或显式 --force-hard-delete 才能执行删除
    - 使用 --require-trash 可强制禁止硬删除
    - 建议配合 S3 Versioning 使用，参考 scripts/ops/s3_hardening.sh

环境变量:
    POSTGRES_DSN          数据库连接字符串（仅孤立清理模式需要）
    ENGRAM_ARTIFACTS_ROOT 制品根目录（local 后端）
"""

import argparse
import json
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# 添加 scripts 目录到 path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engram_step1.artifact_store import (
    LocalArtifactsStore,
    FileUriStore,
    ObjectStore,
    ArtifactStore,
    get_artifact_store,
    BACKEND_LOCAL,
    BACKEND_FILE,
    BACKEND_OBJECT,
    PathTraversalError,
)
from engram_step1.uri import (
    normalize_uri,
    parse_uri,
    UriType,
    ARTIFACT_KEY_SCHEMES,
    PHYSICAL_URI_SCHEMES,
    is_artifact_key,
    is_physical_uri,
    PhysicalRef,
    parse_physical_uri,
)


# =============================================================================
# 速率限制器
# =============================================================================


class RateLimiter:
    """
    线程安全的速率限制器
    
    用于控制 GC 操作的 I/O 速率。
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
                # 重置计数器
                self._bytes_this_second = bytes_processed
                self._last_reset = now
                return

            self._bytes_this_second += bytes_processed

            if self._bytes_this_second >= self.max_bytes_per_sec:
                # 等待到下一秒
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
# 错误定义
# =============================================================================


class GCError(Exception):
    """GC 错误基类"""
    pass


class GCPrefixError(GCError):
    """前缀不在允许范围内"""
    pass


class GCDatabaseError(GCError):
    """数据库查询错误"""
    pass


class GCOpsCredentialsRequiredError(GCError):
    """需要 ops 凭证但未提供"""
    pass


# =============================================================================
# GC 结果数据类
# =============================================================================


@dataclass
class GCCandidate:
    """待删除的制品候选"""
    uri: str                    # 制品 URI
    full_path: str              # 完整路径
    size_bytes: int             # 文件大小
    mtime: float                # 修改时间戳
    age_days: float             # 文件年龄（天）


@dataclass
class GCResult:
    """GC 执行结果"""
    scanned_count: int = 0          # 扫描的文件总数
    referenced_count: int = 0       # 被引用的文件数
    candidates_count: int = 0       # 待删除候选数
    deleted_count: int = 0          # 实际删除数
    trashed_count: int = 0          # 软删除数
    failed_count: int = 0           # 删除失败数
    protected_count: int = 0        # 被保护的文件数（在引用集合中）
    skipped_by_age: int = 0         # 因年龄不足跳过的文件数
    total_size_bytes: int = 0       # 待删除文件总大小
    deleted_size_bytes: int = 0     # 已删除文件总大小
    errors: List[str] = field(default_factory=list)
    candidates: List[GCCandidate] = field(default_factory=list)
    # 元信息（用于 JSON 输出）
    backend: Optional[str] = None   # 存储后端类型
    bucket: Optional[str] = None    # 对象存储 bucket（仅 object 后端）
    prefix: Optional[str] = None    # 扫描前缀
    gc_mode: str = "orphan"         # GC 模式: "orphan"（孤立清理）或 "tmp"（tmp 清理）


# =============================================================================
# 数据库引用查询
# =============================================================================


def _normalize_uri_for_gc(uri: str) -> tuple:
    """
    规范化 URI 用于 GC 比对，区分 artifact key 和 physical uri

    双轨分类规范:
    1. Artifact Key（逻辑键）: 无 scheme 或 artifact:// scheme
       - 规范化为相对路径，用于与本地扫描文件匹配
       - 示例: "scm/proj_a/1/r100.diff", "artifact://scm/proj_a/1/r100.diff"

    2. Physical URI（物理地址）: file://, s3://, gs://, https:// 等
       - 返回结构化的 PhysicalRef，包含 scheme/bucket/key 等信息
       - 这些 URI 绑定特定后端，需要特殊逻辑匹配
       - 示例: "s3://bucket/scm/r100.diff", "file:///mnt/artifacts/scm/r100.diff"

    Args:
        uri: 原始 URI 字符串

    Returns:
        (result, uri_type) 元组:
        - 对于 artifact_key: (normalized_key: str, "artifact_key")
        - 对于 physical_uri: (PhysicalRef, "physical_uri")
        - 对于空/无效 URI: ("", None)
    """
    if not uri:
        return ("", None)

    parsed = parse_uri(uri)

    # Artifact Key: 无 scheme 或 artifact:// scheme
    # 规范化为相对路径，用于与扫描的本地文件匹配
    if parsed.scheme in ARTIFACT_KEY_SCHEMES:
        return (normalize_uri(parsed.path), "artifact_key")

    # Physical URI: file://, s3://, gs://, http://, https://, ftp://
    # 返回结构化的 PhysicalRef，便于后续精确匹配
    if parsed.scheme in PHYSICAL_URI_SCHEMES:
        physical_ref = parse_physical_uri(uri)
        if physical_ref:
            return (physical_ref, "physical_uri")
        # 回退：如果解析失败，返回空
        return ("", None)

    # 其他 scheme（如 memory://）: 忽略，不参与 GC 匹配
    return ("", None)


@dataclass
class ReferencedUris:
    """
    引用的 URI 集合，区分 artifact key 和 physical ref
    
    artifact_keys: 逻辑键集合（规范化的相对路径）
    physical_refs: 物理引用列表（PhysicalRef 结构，包含 scheme/bucket/key 等）
    """
    artifact_keys: Set[str] = field(default_factory=set)   # 逻辑键集合（规范化路径）
    physical_refs: List[PhysicalRef] = field(default_factory=list)  # 物理引用列表（结构化）

    def __contains__(self, uri: str) -> bool:
        """支持 `uri in referenced_uris` 语法（仅检查 artifact_keys）"""
        return uri in self.artifact_keys

    def __len__(self) -> int:
        """返回总引用数"""
        return len(self.artifact_keys) + len(self.physical_refs)
    
    def has_physical_ref_for_key(self, artifact_key: str, store_bucket: str, store_prefix: str) -> bool:
        """
        检查是否存在匹配的 physical ref（用于 object 后端）
        
        匹配条件:
        1. physical_ref.bucket == store_bucket
        2. physical_ref.key 去除 store_prefix 后等于 artifact_key
        
        Args:
            artifact_key: 要匹配的 artifact key（规范化后的相对路径）
            store_bucket: 当前 store 的 bucket
            store_prefix: 当前 store 的 prefix
        
        Returns:
            True 如果存在匹配的 physical ref
        """
        for ref in self.physical_refs:
            if ref.scheme not in ("s3", "gs"):
                continue
            if ref.bucket != store_bucket:
                continue
            # 尝试从 key 还原 artifact key
            key = ref.key
            if store_prefix:
                prefix = store_prefix.rstrip("/") + "/"
                if key.startswith(prefix):
                    restored_key = key[len(prefix):]
                else:
                    # key 不以 store_prefix 开头，无法还原
                    continue
            else:
                restored_key = key
            restored_key = normalize_uri(restored_key)
            if restored_key == artifact_key:
                return True
        return False


def get_referenced_uris(
    dsn: Optional[str] = None,
    prefix: Optional[str] = None,
    search_path: Optional[List[str]] = None,
) -> ReferencedUris:
    """
    从数据库查询所有被引用的制品 URI

    查询的表（schema-qualified）:
    - scm.patch_blobs.uri
    - logbook.attachments.uri

    双轨分类处理:
    - Artifact Key（无 scheme / artifact://）: 规范化为相对路径，加入 artifact_keys 集合
    - Physical URI（file:// / s3:// 等）: 解析为 PhysicalRef 结构，加入 physical_refs 列表
      注意: physical_refs 包含完整的 scheme/bucket/key 信息，用于精确匹配

    Args:
        dsn: 数据库连接字符串
        prefix: 可选的 URI 前缀过滤
        search_path: 可选的 search_path 列表（默认为 ["scm", "logbook", "public"]）

    Returns:
        ReferencedUris 结构，包含:
        - artifact_keys: 规范化的 artifact key 集合
        - physical_refs: PhysicalRef 结构列表（用于 object 后端精确匹配）
    """
    from engram_step1.db import get_connection, DbConnectionError

    if dsn is None:
        dsn = os.environ.get("POSTGRES_DSN")
        if not dsn:
            raise GCDatabaseError("POSTGRES_DSN 环境变量未设置")

    # 设置 search_path，确保 scm 和 logbook schema 可访问
    if search_path is None:
        search_path = ["scm", "logbook", "public"]

    result = ReferencedUris()

    try:
        # 使用 engram_step1.db.get_connection 获取连接，自动设置 search_path
        conn = get_connection(dsn=dsn, autocommit=True, search_path=search_path)
        try:
            with conn.cursor() as cur:
                # 规范化前缀用于过滤
                normalized_prefix = normalize_uri(prefix) if prefix else None

                # 查询 1: scm.patch_blobs.uri
                query_patch_blobs = """
                    SELECT DISTINCT uri
                    FROM scm.patch_blobs
                    WHERE uri IS NOT NULL AND uri != ''
                """
                params_patch: List = []

                if normalized_prefix:
                    query_patch_blobs += " AND uri LIKE %s"
                    params_patch.append(f"{normalized_prefix}%")

                cur.execute(query_patch_blobs, params_patch)
                rows = cur.fetchall()

                for row in rows:
                    uri = row[0]
                    if uri:
                        # 规范化 URI（区分 artifact key 和 physical uri）
                        normalized, uri_type = _normalize_uri_for_gc(uri)
                        if uri_type == "artifact_key" and normalized:
                            result.artifact_keys.add(normalized)
                        elif uri_type == "physical_uri" and isinstance(normalized, PhysicalRef):
                            result.physical_refs.append(normalized)

                # 查询 2: logbook.attachments.uri
                query_attachments = """
                    SELECT DISTINCT uri
                    FROM logbook.attachments
                    WHERE uri IS NOT NULL AND uri != ''
                """
                params_attach: List = []

                if normalized_prefix:
                    # attachments 的 URI 可能带有 scheme，需要匹配 path 部分
                    # 使用 LIKE 匹配可能不够精确，但 prefix 过滤只是优化
                    query_attachments += " AND uri LIKE %s"
                    params_attach.append(f"%{normalized_prefix}%")

                cur.execute(query_attachments, params_attach)
                rows = cur.fetchall()

                for row in rows:
                    uri = row[0]
                    if uri:
                        # 规范化 URI（区分 artifact key 和 physical uri）
                        normalized, uri_type = _normalize_uri_for_gc(uri)
                        if uri_type == "artifact_key" and normalized:
                            result.artifact_keys.add(normalized)
                        elif uri_type == "physical_uri" and isinstance(normalized, PhysicalRef):
                            result.physical_refs.append(normalized)

        finally:
            conn.close()

    except DbConnectionError as e:
        raise GCDatabaseError(f"数据库连接失败: {e}")
    except Exception as e:
        raise GCDatabaseError(f"查询数据库失败: {e}")

    return result


# =============================================================================
# 文件扫描
# =============================================================================


def scan_local_artifacts(
    store: LocalArtifactsStore,
    prefix: str,
    allowed_prefixes: Optional[List[str]] = None,
) -> List[Tuple[str, str, int, float]]:
    """
    扫描本地制品目录

    Args:
        store: 本地存储实例
        prefix: 扫描的前缀
        allowed_prefixes: 允许的前缀列表（安全限制）

    Returns:
        列表，每项为 (uri, full_path, size_bytes, mtime)

    Raises:
        GCPrefixError: 前缀不在允许范围内
    """
    # 规范化前缀
    prefix = normalize_uri(prefix)

    # 验证前缀是否在允许范围内
    if allowed_prefixes is not None:
        normalized_allowed = [normalize_uri(p) for p in allowed_prefixes]
        if not normalized_allowed:
            raise GCPrefixError("allowed_prefixes 为空，拒绝所有操作")

        is_allowed = any(
            prefix.startswith(allowed) or allowed.startswith(prefix)
            for allowed in normalized_allowed
        )
        if not is_allowed:
            raise GCPrefixError(
                f"前缀 '{prefix}' 不在允许范围内: {normalized_allowed}"
            )

    # 构建扫描目录
    scan_dir = store.root / prefix
    if not scan_dir.exists():
        return []

    results: List[Tuple[str, str, int, float]] = []

    # 递归扫描目录
    for root, dirs, files in os.walk(scan_dir):
        # 跳过隐藏目录和 trash 目录
        dirs[:] = [d for d in dirs if not d.startswith('.')]

        for filename in files:
            # 跳过隐藏文件和临时文件
            if filename.startswith('.') or filename.endswith('.tmp'):
                continue

            full_path = Path(root) / filename

            try:
                stat = full_path.stat()
                size_bytes = stat.st_size
                mtime = stat.st_mtime

                # 计算相对于 artifacts_root 的 URI
                relative_path = full_path.relative_to(store.root)
                uri = normalize_uri(str(relative_path))

                # 验证 URI 是否在前缀范围内
                if uri.startswith(prefix):
                    results.append((uri, str(full_path), size_bytes, mtime))

            except OSError as e:
                # 文件可能在扫描过程中被删除
                continue

    return results


def scan_file_uri_artifacts(
    store: FileUriStore,
    prefix: str,
    allowed_roots: Optional[List[str]] = None,
) -> List[Tuple[str, str, int, float]]:
    """
    扫描 FileUriStore 的制品

    对 allowed_roots 做安全校验后扫描目录，返回 file:// URI 格式的结果。

    Args:
        store: FileUriStore 实例
        prefix: 扫描的前缀（相对路径，如 "scm/" 或 "attachments/"）
        allowed_roots: 允许扫描的根路径列表（安全限制）
                       - 如果提供，会与 store.allowed_roots 取交集
                       - 如果为 None，使用 store.allowed_roots

    Returns:
        列表，每项为 (uri, full_path, size_bytes, mtime)
        uri: file:// 格式的 URI
        full_path: 完整本地路径

    Raises:
        GCPrefixError: 前缀校验失败或无可用的 allowed_roots
    """
    # 规范化前缀
    prefix = normalize_uri(prefix)

    # 确定有效的 allowed_roots
    effective_roots: List[str] = []

    if allowed_roots is not None:
        # 如果调用方指定了 allowed_roots，需要与 store 的 allowed_roots 取交集
        if store.allowed_roots is not None:
            # 两者都有限制，取交集
            store_roots_set = set(str(Path(r).resolve()) for r in store.allowed_roots)
            for root in allowed_roots:
                resolved = str(Path(root).resolve())
                if resolved in store_roots_set:
                    effective_roots.append(root)
                # 也检查嵌套情况：调用方的 root 是否在 store 的某个 root 下
                for store_root in store.allowed_roots:
                    store_resolved = str(Path(store_root).resolve())
                    if resolved.startswith(store_resolved + os.sep):
                        if root not in effective_roots:
                            effective_roots.append(root)
                        break
        else:
            # store 无限制，使用调用方指定的
            effective_roots = list(allowed_roots)
    else:
        # 调用方未指定，使用 store 的 allowed_roots
        if store.allowed_roots is not None:
            effective_roots = list(store.allowed_roots)
        else:
            raise GCPrefixError(
                "FileUriStore 未配置 allowed_roots，GC 扫描需要明确指定扫描范围"
            )

    if not effective_roots:
        raise GCPrefixError("allowed_roots 为空，拒绝所有操作")

    results: List[Tuple[str, str, int, float]] = []

    for root in effective_roots:
        root_path = Path(root)
        if not root_path.exists():
            continue

        # 构建扫描目录
        scan_dir = root_path / prefix
        if not scan_dir.exists():
            continue

        # 递归扫描目录
        for dir_path, dirs, files in os.walk(scan_dir):
            # 跳过隐藏目录和 trash 目录
            dirs[:] = [d for d in dirs if not d.startswith('.')]

            for filename in files:
                # 跳过隐藏文件和临时文件
                if filename.startswith('.') or filename.endswith('.tmp'):
                    continue

                full_path = Path(dir_path) / filename

                try:
                    stat = full_path.stat()
                    size_bytes = stat.st_size
                    mtime = stat.st_mtime

                    # 计算相对于 root 的路径
                    try:
                        relative_path = full_path.relative_to(root_path)
                        relative_uri = normalize_uri(str(relative_path))
                    except ValueError:
                        # 路径不在 root 下（不应该发生）
                        continue

                    # 验证 URI 是否在前缀范围内
                    if not relative_uri.startswith(prefix):
                        continue

                    # 构建 file:// URI
                    file_uri = store._ensure_file_uri(str(full_path))

                    results.append((file_uri, str(full_path), size_bytes, mtime))

                except OSError:
                    # 文件可能在扫描过程中被删除
                    continue

    return results


def scan_object_store_artifacts(
    store: ObjectStore,
    prefix: str,
    allowed_prefixes: Optional[List[str]] = None,
) -> List[Tuple[str, str, int, float]]:
    """
    扫描对象存储中的制品

    Args:
        store: 对象存储实例
        prefix: 扫描的前缀
        allowed_prefixes: 允许的前缀列表（安全限制）

    Returns:
        列表，每项为 (uri, full_path/key, size_bytes, mtime)

    Raises:
        GCPrefixError: 前缀不在允许范围内
    """
    # 规范化前缀
    prefix = normalize_uri(prefix)

    # 验证前缀是否在允许范围内
    if allowed_prefixes is not None:
        normalized_allowed = [normalize_uri(p) for p in allowed_prefixes]
        if not normalized_allowed:
            raise GCPrefixError("allowed_prefixes 为空，拒绝所有操作")

        is_allowed = any(
            prefix.startswith(allowed) or allowed.startswith(prefix)
            for allowed in normalized_allowed
        )
        if not is_allowed:
            raise GCPrefixError(
                f"前缀 '{prefix}' 不在允许范围内: {normalized_allowed}"
            )

    results: List[Tuple[str, str, int, float]] = []

    try:
        client = store._get_client()

        # 构建完整的 S3 前缀
        full_prefix = store._object_key(prefix)

        # 使用分页器列出所有对象
        paginator = client.get_paginator('list_objects_v2')
        page_iterator = paginator.paginate(
            Bucket=store.bucket,
            Prefix=full_prefix,
        )

        for page in page_iterator:
            for obj in page.get('Contents', []):
                key = obj['Key']
                size_bytes = obj['Size']
                last_modified = obj['LastModified']

                # 将时间转换为时间戳
                mtime = last_modified.timestamp()

                # 移除 store.prefix 获取 URI
                if store.prefix:
                    uri = key[len(store.prefix):].lstrip('/')
                else:
                    uri = key

                uri = normalize_uri(uri)

                # 验证 URI 是否在前缀范围内
                if uri.startswith(prefix):
                    full_path = f"s3://{store.bucket}/{key}"
                    results.append((uri, full_path, size_bytes, mtime))

    except Exception as e:
        raise GCError(f"扫描对象存储失败: {e}")

    return results


# =============================================================================
# 删除操作
# =============================================================================


def delete_local_file(
    full_path: str,
    trash_prefix: Optional[str] = None,
    artifacts_root: Optional[Path] = None,
) -> Tuple[bool, Optional[str]]:
    """
    删除或软删除本地文件

    Args:
        full_path: 文件完整路径
        trash_prefix: 软删除目标前缀（相对于 artifacts_root）
        artifacts_root: 制品根目录（软删除时必需）

    Returns:
        (成功, 错误消息)
    """
    path = Path(full_path)

    if not path.exists():
        return True, None  # 文件已不存在，视为成功

    try:
        if trash_prefix:
            # 软删除：移动到 trash 目录
            if not artifacts_root:
                return False, "软删除需要指定 artifacts_root"

            relative_path = path.relative_to(artifacts_root)
            trash_path = artifacts_root / trash_prefix / relative_path

            # 确保 trash 目录存在
            trash_path.parent.mkdir(parents=True, exist_ok=True)

            # 如果目标已存在，添加时间戳
            if trash_path.exists():
                timestamp = int(time.time())
                trash_path = trash_path.with_name(
                    f"{trash_path.stem}.{timestamp}{trash_path.suffix}"
                )

            shutil.move(str(path), str(trash_path))
        else:
            # 硬删除
            path.unlink()

        return True, None

    except Exception as e:
        return False, str(e)


def delete_file_uri_file(
    store: FileUriStore,
    file_uri: str,
    trash_prefix: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """
    删除或软删除 file:// URI 指向的文件

    复用本地删除/软删除策略。

    Args:
        store: FileUriStore 实例
        file_uri: file:// 格式的 URI
        trash_prefix: 软删除目标前缀（相对于文件所在的 allowed_root）

    Returns:
        (成功, 错误消息)
    """
    try:
        # 解析 file:// URI 为本地路径
        file_path = store._parse_file_uri(file_uri)

        if not file_path.exists():
            return True, None  # 文件已不存在，视为成功

        if trash_prefix:
            # 软删除：移动到 trash 目录
            # 查找文件所属的 allowed_root
            trash_root = None
            relative_path = None

            if store.allowed_roots:
                for root in store.allowed_roots:
                    root_path = Path(root)
                    try:
                        if root_path.exists():
                            resolved_root = root_path.resolve()
                        else:
                            resolved_root = root_path.absolute()
                        resolved_file = file_path.resolve() if file_path.exists() else file_path.absolute()

                        if str(resolved_file).startswith(str(resolved_root) + os.sep):
                            trash_root = resolved_root
                            relative_path = resolved_file.relative_to(resolved_root)
                            break
                    except (OSError, ValueError):
                        continue

            if trash_root is None:
                # 无法确定 trash root，回退到文件所在目录的父目录
                trash_root = file_path.parent.parent
                relative_path = Path(file_path.parent.name) / file_path.name

            trash_path = trash_root / trash_prefix / relative_path

            # 确保 trash 目录存在
            trash_path.parent.mkdir(parents=True, exist_ok=True)

            # 如果目标已存在，添加时间戳
            if trash_path.exists():
                timestamp = int(time.time())
                trash_path = trash_path.with_name(
                    f"{trash_path.stem}.{timestamp}{trash_path.suffix}"
                )

            shutil.move(str(file_path), str(trash_path))
        else:
            # 硬删除
            file_path.unlink()

        return True, None

    except Exception as e:
        return False, str(e)


def delete_object_store_file(
    store: ObjectStore,
    key: str,
    trash_prefix: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """
    删除或软删除对象存储中的文件

    软删除时会保留原对象的 metadata（包括 sha256 等用户自定义元数据）。
    注意：S3 CopyObject 默认使用 MetadataDirective='COPY' 会保留所有用户元数据，
    但系统元数据（如 ContentType）需要显式复制。

    Args:
        store: 对象存储实例
        key: 对象键
        trash_prefix: 软删除目标前缀

    Returns:
        (成功, 错误消息)
    """
    try:
        client = store._get_client()

        if trash_prefix:
            # 软删除：复制到 trash 前缀然后删除原文件
            trash_key = f"{trash_prefix.rstrip('/')}/{key}"

            # 先获取原对象的 metadata（用于日志/验证）
            # 使用 MetadataDirective='COPY' 保留所有用户元数据
            # 包括 x-amz-meta-sha256 等自定义元数据
            client.copy_object(
                Bucket=store.bucket,
                CopySource={'Bucket': store.bucket, 'Key': key},
                Key=trash_key,
                MetadataDirective='COPY',  # 显式指定复制元数据
            )

        # 删除原对象
        client.delete_object(Bucket=store.bucket, Key=key)

        return True, None

    except Exception as e:
        return False, str(e)


# =============================================================================
# Tmp 清理模式（不依赖 DB）
# =============================================================================


def run_tmp_gc(
    tmp_prefix: str,
    older_than_days: int,
    dry_run: bool = True,
    delete: bool = False,
    backend: Optional[str] = None,
    artifacts_root: Optional[str] = None,
    verbose: bool = True,
    concurrency: int = 1,
    max_bytes_per_sec: Optional[int] = None,
    require_ops: bool = False,
) -> GCResult:
    """
    执行 tmp 目录清理（不依赖数据库）

    仅基于文件年龄清理 tmp 目录下的临时文件，不查询数据库引用。
    适用于清理构建过程中产生的临时文件。

    Args:
        tmp_prefix: tmp 目录前缀（必须），如 "tmp/" 或 ".tmp/"
        older_than_days: 仅删除指定天数之前的文件（必须）
        dry_run: 仅显示待删除列表，不实际删除
        delete: 执行实际删除
        backend: 存储后端类型
        artifacts_root: 制品根目录（local 后端）
        verbose: 输出详细信息

    Returns:
        GCResult 结果对象
    """
    result = GCResult()
    result.gc_mode = "tmp"
    result.prefix = tmp_prefix

    # 规范化前缀
    tmp_prefix = normalize_uri(tmp_prefix)
    if not tmp_prefix:
        raise GCPrefixError("必须指定 tmp 目录前缀")

    if older_than_days is None or older_than_days < 0:
        raise GCPrefixError("tmp 清理模式必须指定有效的 --tmp-older-than-days 参数")

    if verbose:
        print(f"[TMP-GC] 开始扫描 tmp 前缀: {tmp_prefix}")
        print(f"[TMP-GC] 仅清理 {older_than_days} 天前的文件")
        if dry_run and not delete:
            print("[TMP-GC] 模式: dry-run（仅显示待删除列表）")
        elif delete:
            print("[TMP-GC] 模式: 硬删除")

    # 获取存储实例并扫描文件
    store_kwargs = {}
    if artifacts_root:
        store_kwargs['root'] = artifacts_root

    store = get_artifact_store(backend=backend, **store_kwargs)

    # 设置后端信息
    if isinstance(store, LocalArtifactsStore):
        result.backend = BACKEND_LOCAL
    elif isinstance(store, FileUriStore):
        result.backend = BACKEND_FILE
    elif isinstance(store, ObjectStore):
        result.backend = BACKEND_OBJECT
        result.bucket = store.bucket
        
        # 对 object 后端执行删除时检查 ops 凭证
        if delete and require_ops and not store.is_ops_credentials():
            raise GCOpsCredentialsRequiredError(
                "指定了 --require-ops 但当前未使用 ops 凭证。\n"
                "请设置 ENGRAM_S3_USE_OPS=true 并配置 ENGRAM_S3_OPS_ACCESS_KEY/SECRET_KEY，"
                "或移除 --require-ops 参数。"
            )

    if verbose:
        print(f"[TMP-GC] 存储后端: {type(store).__name__}")
        print("[TMP-GC] 正在扫描 tmp 文件...")

    # 根据后端类型扫描文件
    scanned_files: List[Tuple[str, str, int, float]] = []

    if isinstance(store, LocalArtifactsStore):
        scanned_files = scan_local_artifacts(store, tmp_prefix, allowed_prefixes=None)
    elif isinstance(store, FileUriStore):
        scanned_files = scan_file_uri_artifacts(store, tmp_prefix, allowed_roots=None)
    elif isinstance(store, ObjectStore):
        scanned_files = scan_object_store_artifacts(store, tmp_prefix, allowed_prefixes=None)
    else:
        raise GCError(f"不支持的存储后端类型: {type(store)}")

    result.scanned_count = len(scanned_files)
    if verbose:
        print(f"[TMP-GC] 扫描到 {len(scanned_files)} 个 tmp 文件")

    # 筛选待删除的候选文件（仅基于年龄，不查询 DB）
    now = time.time()
    candidates: List[GCCandidate] = []

    for uri, full_path, size_bytes, mtime in scanned_files:
        # 规范化 URI
        normalized_uri = normalize_uri(uri)

        # 计算文件年龄
        age_seconds = now - mtime
        age_days = age_seconds / (24 * 3600)

        # 检查年龄限制
        if age_days < older_than_days:
            result.skipped_by_age += 1
            continue

        # 添加到候选列表
        candidate = GCCandidate(
            uri=normalized_uri,
            full_path=full_path,
            size_bytes=size_bytes,
            mtime=mtime,
            age_days=age_days,
        )
        candidates.append(candidate)
        result.total_size_bytes += size_bytes

    result.candidates_count = len(candidates)
    result.candidates = candidates

    if verbose:
        print(f"[TMP-GC] 因年龄不足跳过的文件: {result.skipped_by_age}")
        print(f"[TMP-GC] 待删除候选: {len(candidates)} 个")
        print(f"[TMP-GC] 待删除总大小: {result.total_size_bytes / 1024 / 1024:.2f} MB")

    # 输出待删除列表
    if verbose and candidates:
        print("\n[TMP-GC] 待删除文件列表:")
        for i, candidate in enumerate(candidates[:50], 1):
            age_str = f"{candidate.age_days:.1f}d"
            size_str = f"{candidate.size_bytes / 1024:.1f}KB"
            print(f"  {i}. {candidate.uri} ({size_str}, {age_str})")
        if len(candidates) > 50:
            print(f"  ... 还有 {len(candidates) - 50} 个文件")

    # 执行删除操作
    rate_limiter = RateLimiter(max_bytes_per_sec)
    
    if delete and not dry_run and candidates:
        if verbose:
            print("\n[TMP-GC] 开始删除 tmp 文件...")

        def delete_one(candidate: GCCandidate) -> Tuple[bool, Optional[str], int]:
            """删除单个文件，返回 (成功, 错误消息, 文件大小)"""
            # 速率限制
            rate_limiter.wait_if_needed(candidate.size_bytes)
            
            if isinstance(store, LocalArtifactsStore):
                success, error = delete_local_file(candidate.full_path)
            elif isinstance(store, FileUriStore):
                success, error = delete_file_uri_file(store, candidate.uri)
            elif isinstance(store, ObjectStore):
                key = candidate.full_path.replace(f"s3://{store.bucket}/", "")
                success, error = delete_object_store_file(store, key)
            else:
                success, error = False, "不支持的存储类型"
            return success, error, candidate.size_bytes, candidate.uri

        if concurrency > 1:
            # 并发删除
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {executor.submit(delete_one, c): c for c in candidates}
                for future in as_completed(futures):
                    try:
                        success, error, size, uri = future.result()
                        if success:
                            result.deleted_count += 1
                            result.deleted_size_bytes += size
                        else:
                            result.failed_count += 1
                            if error:
                                result.errors.append(f"{uri}: {error}")
                    except Exception as e:
                        result.failed_count += 1
                        result.errors.append(str(e))
        else:
            # 串行删除
            for candidate in candidates:
                success, error, size, uri = delete_one(candidate)
                if success:
                    result.deleted_count += 1
                    result.deleted_size_bytes += size
                else:
                    result.failed_count += 1
                    if error:
                        result.errors.append(f"{uri}: {error}")

        if verbose:
            print(f"[TMP-GC] 删除完成: {result.deleted_count} 个文件")
            if result.failed_count > 0:
                print(f"[TMP-GC] 删除失败: {result.failed_count} 个文件")

    elif dry_run and candidates:
        if verbose:
            print("\n[TMP-GC] dry-run 模式，未执行实际删除")

    return result


# =============================================================================
# GC 核心逻辑（孤立清理模式）
# =============================================================================


def run_gc(
    prefix: str,
    dry_run: bool = True,
    delete: bool = False,
    older_than_days: Optional[int] = None,
    trash_prefix: Optional[str] = None,
    dsn: Optional[str] = None,
    backend: Optional[str] = None,
    artifacts_root: Optional[str] = None,
    allowed_prefixes: Optional[List[str]] = None,
    verbose: bool = True,
    concurrency: int = 1,
    max_bytes_per_sec: Optional[int] = None,
    require_ops: bool = False,
) -> GCResult:
    """
    执行制品垃圾回收（孤立清理模式）

    基于数据库引用清理未被引用的制品文件。

    Args:
        prefix: 扫描的前缀（必须）
        dry_run: 仅显示待删除列表，不实际删除
        delete: 执行实际删除
        older_than_days: 仅删除指定天数之前的文件
        trash_prefix: 软删除目标前缀
        dsn: 数据库连接字符串
        backend: 存储后端类型
        artifacts_root: 制品根目录（local 后端）
        allowed_prefixes: 允许操作的前缀列表
        verbose: 输出详细信息

    Returns:
        GCResult 结果对象
    """
    result = GCResult()
    result.gc_mode = "orphan"
    result.prefix = prefix

    # 规范化前缀
    prefix = normalize_uri(prefix)
    if not prefix:
        raise GCPrefixError("必须指定扫描前缀")

    # 验证前缀在允许范围内
    if allowed_prefixes is not None:
        normalized_allowed = [normalize_uri(p) for p in allowed_prefixes if p]
        if not normalized_allowed:
            raise GCPrefixError("allowed_prefixes 为空列表，拒绝所有操作")

        is_allowed = any(
            prefix.startswith(allowed) for allowed in normalized_allowed
        )
        if not is_allowed:
            raise GCPrefixError(
                f"前缀 '{prefix}' 不在允许范围内: {normalized_allowed}"
            )

    if verbose:
        print(f"[GC] 开始扫描前缀: {prefix}")
        if dry_run and not delete:
            print("[GC] 模式: dry-run（仅显示待删除列表）")
        elif delete:
            if trash_prefix:
                print(f"[GC] 模式: 软删除（移动到 {trash_prefix}）")
            else:
                print("[GC] 模式: 硬删除")

    # 1. 查询数据库获取被引用的 URI 集合
    if verbose:
        print("[GC] 正在查询数据库中的引用...")

    referenced: Optional[ReferencedUris] = None
    try:
        referenced = get_referenced_uris(dsn=dsn, prefix=prefix)
        result.referenced_count = len(referenced)
        if verbose:
            print(f"[GC] 数据库中有 {len(referenced)} 个被引用的制品")
            if referenced.physical_refs:
                print(f"[GC]   其中 {len(referenced.physical_refs)} 个为物理 URI 引用")
    except GCDatabaseError as e:
        result.errors.append(str(e))
        if verbose:
            print(f"[GC] 警告: 无法查询数据库，所有文件将被保护: {e}")
        # 数据库查询失败时，保护所有文件
        referenced = None

    # 2. 获取存储实例并扫描文件
    store_kwargs = {}
    if artifacts_root:
        store_kwargs['root'] = artifacts_root

    store = get_artifact_store(backend=backend, **store_kwargs)

    # 设置后端信息
    if isinstance(store, LocalArtifactsStore):
        result.backend = BACKEND_LOCAL
    elif isinstance(store, FileUriStore):
        result.backend = BACKEND_FILE
    elif isinstance(store, ObjectStore):
        result.backend = BACKEND_OBJECT
        result.bucket = store.bucket
        
        # 对 object 后端执行删除时检查 ops 凭证
        if delete and require_ops and not store.is_ops_credentials():
            raise GCOpsCredentialsRequiredError(
                "指定了 --require-ops 但当前未使用 ops 凭证。\n"
                "请设置 ENGRAM_S3_USE_OPS=true 并配置 ENGRAM_S3_OPS_ACCESS_KEY/SECRET_KEY，"
                "或移除 --require-ops 参数。"
            )

    if verbose:
        print(f"[GC] 存储后端: {type(store).__name__}")
        print("[GC] 正在扫描文件...")

    # 根据后端类型扫描文件
    scanned_files: List[Tuple[str, str, int, float]] = []

    if isinstance(store, LocalArtifactsStore):
        scanned_files = scan_local_artifacts(
            store, prefix, allowed_prefixes
        )
    elif isinstance(store, FileUriStore):
        scanned_files = scan_file_uri_artifacts(
            store, prefix, allowed_roots=None  # 使用 store 自身的 allowed_roots
        )
    elif isinstance(store, ObjectStore):
        scanned_files = scan_object_store_artifacts(
            store, prefix, allowed_prefixes
        )
    else:
        raise GCError(f"不支持的存储后端类型: {type(store)}")

    result.scanned_count = len(scanned_files)
    if verbose:
        print(f"[GC] 扫描到 {len(scanned_files)} 个文件")

    # 3. 筛选待删除的候选文件
    now = time.time()
    candidates: List[GCCandidate] = []
    
    # 对于 object 后端，获取 store 的 bucket 和 prefix
    store_bucket = getattr(store, 'bucket', None)
    store_prefix = getattr(store, 'prefix', '') or ''

    for uri, full_path, size_bytes, mtime in scanned_files:
        # 规范化 URI
        normalized_uri = normalize_uri(uri)

        # 如果数据库查询失败，保护所有文件
        if referenced is None:
            result.protected_count += 1
            continue

        # 检查是否被 artifact key 引用
        if normalized_uri in referenced.artifact_keys:
            result.protected_count += 1
            continue
        
        # 对于 object 后端，还需要检查 physical_refs
        # 只有当 physical ref 的 bucket==store.bucket 且 key 可还原为 artifact key 时才匹配
        if isinstance(store, ObjectStore) and referenced.physical_refs:
            if referenced.has_physical_ref_for_key(normalized_uri, store_bucket, store_prefix):
                result.protected_count += 1
                continue

        # 计算文件年龄
        age_seconds = now - mtime
        age_days = age_seconds / (24 * 3600)

        # 检查年龄限制
        if older_than_days is not None and age_days < older_than_days:
            result.skipped_by_age += 1
            continue

        # 添加到候选列表
        candidate = GCCandidate(
            uri=normalized_uri,
            full_path=full_path,
            size_bytes=size_bytes,
            mtime=mtime,
            age_days=age_days,
        )
        candidates.append(candidate)
        result.total_size_bytes += size_bytes

    result.candidates_count = len(candidates)
    result.candidates = candidates

    if verbose:
        print(f"[GC] 被引用保护的文件: {result.protected_count}")
        if older_than_days:
            print(f"[GC] 因年龄不足跳过的文件: {result.skipped_by_age}")
        print(f"[GC] 待删除候选: {len(candidates)} 个")
        print(f"[GC] 待删除总大小: {result.total_size_bytes / 1024 / 1024:.2f} MB")

    # 4. 输出待删除列表
    if verbose and candidates:
        print("\n[GC] 待删除文件列表:")
        for i, candidate in enumerate(candidates[:50], 1):
            age_str = f"{candidate.age_days:.1f}d"
            size_str = f"{candidate.size_bytes / 1024:.1f}KB"
            print(f"  {i}. {candidate.uri} ({size_str}, {age_str})")
        if len(candidates) > 50:
            print(f"  ... 还有 {len(candidates) - 50} 个文件")

    # 5. 执行删除操作
    rate_limiter = RateLimiter(max_bytes_per_sec)
    
    if delete and not dry_run and candidates:
        if verbose:
            print("\n[GC] 开始删除文件...")

        def delete_one(candidate: GCCandidate) -> Tuple[bool, Optional[str], int, str]:
            """删除单个文件，返回 (成功, 错误消息, 文件大小, uri)"""
            # 速率限制
            rate_limiter.wait_if_needed(candidate.size_bytes)
            
            if isinstance(store, LocalArtifactsStore):
                success, error = delete_local_file(
                    candidate.full_path,
                    trash_prefix=trash_prefix,
                    artifacts_root=store.root if trash_prefix else None,
                )
            elif isinstance(store, FileUriStore):
                success, error = delete_file_uri_file(
                    store, candidate.uri, trash_prefix=trash_prefix
                )
            elif isinstance(store, ObjectStore):
                key = candidate.full_path.replace(f"s3://{store.bucket}/", "")
                success, error = delete_object_store_file(
                    store, key, trash_prefix=trash_prefix
                )
            else:
                success, error = False, "不支持的存储类型"
            return success, error, candidate.size_bytes, candidate.uri

        if concurrency > 1:
            # 并发删除
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {executor.submit(delete_one, c): c for c in candidates}
                for future in as_completed(futures):
                    try:
                        success, error, size, uri = future.result()
                        if success:
                            if trash_prefix:
                                result.trashed_count += 1
                            else:
                                result.deleted_count += 1
                            result.deleted_size_bytes += size
                        else:
                            result.failed_count += 1
                            if error:
                                result.errors.append(f"{uri}: {error}")
                    except Exception as e:
                        result.failed_count += 1
                        result.errors.append(str(e))
        else:
            # 串行删除
            for candidate in candidates:
                success, error, size, uri = delete_one(candidate)
                if success:
                    if trash_prefix:
                        result.trashed_count += 1
                    else:
                        result.deleted_count += 1
                    result.deleted_size_bytes += size
                else:
                    result.failed_count += 1
                    if error:
                        result.errors.append(f"{uri}: {error}")

        if verbose:
            if trash_prefix:
                print(f"[GC] 软删除完成: {result.trashed_count} 个文件")
            else:
                print(f"[GC] 删除完成: {result.deleted_count} 个文件")
            if result.failed_count > 0:
                print(f"[GC] 删除失败: {result.failed_count} 个文件")

    elif dry_run and candidates:
        if verbose:
            print("\n[GC] dry-run 模式，未执行实际删除")

    return result


# =============================================================================
# CLI 入口
# =============================================================================


def main():
    """CLI 入口"""
    parser = argparse.ArgumentParser(
        description="制品垃圾回收工具 - 扫描并删除未被数据库引用的制品文件或清理 tmp 目录",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # ========== 孤立清理模式（基于 DB 引用）==========
  # 仅扫描并显示待删除文件（dry-run 模式）
  python artifact_gc.py --prefix scm/

  # 软删除（推荐，移动到 trash 目录）
  python artifact_gc.py --prefix scm/ --trash-prefix .trash/ --delete

  # 仅删除 30 天前的文件（软删除）
  python artifact_gc.py --prefix scm/ --older-than-days 30 --trash-prefix .trash/ --delete

  # 强制要求软删除（生产安全策略）
  python artifact_gc.py --prefix scm/ --require-trash --trash-prefix .trash/ --delete

  # 硬删除（需显式确认，不推荐）
  python artifact_gc.py --prefix scm/ --delete --force-hard-delete

  # ========== Tmp 清理模式（不依赖 DB）==========
  # 清理 tmp/ 目录下超过 7 天的文件
  python artifact_gc.py --tmp-prefix tmp/ --tmp-older-than-days 7

  # 执行实际删除
  python artifact_gc.py --tmp-prefix tmp/ --tmp-older-than-days 7 --delete

  # JSON 输出（包含 backend/bucket/prefix 元信息）
  python artifact_gc.py --tmp-prefix tmp/ --tmp-older-than-days 1 --json

生产安全:
  - 硬删除（无 --trash-prefix）需要 --force-hard-delete 显式确认
  - 使用 --require-trash 可强制要求软删除，禁止硬删除
  - 建议启用 S3 Versioning，参考 scripts/ops/s3_hardening.sh

环境变量:
  POSTGRES_DSN          数据库连接字符串（仅孤立清理模式需要）
  ENGRAM_ARTIFACTS_ROOT 制品根目录
        """
    )

    # ========== 孤立清理模式参数 ==========
    parser.add_argument(
        "--prefix",
        help="扫描的前缀（孤立清理模式），如 scm/ 或 attachments/"
    )

    # ========== Tmp 清理模式参数 ==========
    parser.add_argument(
        "--tmp-prefix",
        metavar="PREFIX",
        help="tmp 目录前缀（tmp 清理模式），如 tmp/ 或 .tmp/"
    )

    parser.add_argument(
        "--tmp-older-than-days",
        type=int,
        metavar="DAYS",
        help="仅删除指定天数之前的 tmp 文件（tmp 清理模式必须）"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="仅显示待删除列表，不实际删除（默认启用）"
    )

    parser.add_argument(
        "--delete",
        action="store_true",
        help="执行实际删除操作"
    )

    parser.add_argument(
        "--older-than-days",
        type=int,
        metavar="DAYS",
        help="仅删除指定天数之前的文件"
    )

    parser.add_argument(
        "--trash-prefix",
        metavar="PREFIX",
        help="软删除目标前缀（移动而非删除）"
    )

    parser.add_argument(
        "--require-trash",
        action="store_true",
        help="强制要求 --trash-prefix（生产安全策略，防止意外硬删除）"
    )

    parser.add_argument(
        "--force-hard-delete",
        action="store_true",
        help="确认执行硬删除（无 --trash-prefix 时需显式确认）"
    )

    parser.add_argument(
        "--require-ops",
        action="store_true",
        help="强制要求使用 ops 凭证（object 后端删除操作）。\n"
             "需设置 ENGRAM_S3_USE_OPS=true 并配置 ENGRAM_S3_OPS_ACCESS_KEY/SECRET_KEY"
    )

    parser.add_argument(
        "--allowed-prefixes",
        nargs="+",
        metavar="PREFIX",
        help="允许操作的前缀列表（安全限制）"
    )

    parser.add_argument(
        "--dsn",
        help="数据库连接字符串（默认使用 POSTGRES_DSN 环境变量）"
    )

    parser.add_argument(
        "--backend",
        choices=["local", "file", "object"],
        help="存储后端类型"
    )

    parser.add_argument(
        "--artifacts-root",
        help="制品根目录（local 后端）"
    )

    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        metavar="N",
        help="并发删除线程数（默认 1，即串行）"
    )

    parser.add_argument(
        "--max-bytes-per-sec",
        type=int,
        metavar="BYTES",
        help="删除操作速率限制（字节/秒）"
    )

    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="静默模式，仅输出统计信息"
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果"
    )

    args = parser.parse_args()

    # 验证参数：必须指定 --prefix 或 --tmp-prefix 之一
    if args.tmp_prefix and args.prefix:
        print("错误: 不能同时指定 --prefix 和 --tmp-prefix", file=sys.stderr)
        sys.exit(2)

    if not args.tmp_prefix and not args.prefix:
        print("错误: 必须指定 --prefix（孤立清理模式）或 --tmp-prefix（tmp 清理模式）", file=sys.stderr)
        sys.exit(2)

    # 如果使用 tmp 清理模式，验证 --tmp-older-than-days 参数
    if args.tmp_prefix and args.tmp_older_than_days is None:
        print("错误: tmp 清理模式必须指定 --tmp-older-than-days 参数", file=sys.stderr)
        sys.exit(2)

    # 生产安全检查：硬删除需要显式确认
    if args.delete and not args.trash_prefix:
        # --require-trash 强制要求软删除
        if args.require_trash:
            print("错误: --require-trash 已启用，必须指定 --trash-prefix 才能执行删除", file=sys.stderr)
            print("提示: 使用 --trash-prefix .trash/ 启用软删除", file=sys.stderr)
            sys.exit(2)
        
        # 硬删除需要 --force-hard-delete 确认
        if not args.force_hard_delete:
            print("=" * 60, file=sys.stderr)
            print("警告: 您正在执行硬删除操作（无 --trash-prefix）", file=sys.stderr)
            print("=" * 60, file=sys.stderr)
            print("", file=sys.stderr)
            print("硬删除将永久移除文件，无法通过应用层恢复！", file=sys.stderr)
            print("", file=sys.stderr)
            print("生产环境建议:", file=sys.stderr)
            print("  1. 使用软删除: --trash-prefix .trash/", file=sys.stderr)
            print("  2. 启用 S3 Versioning 以便恢复误删除", file=sys.stderr)
            print("  3. 参考 scripts/ops/s3_hardening.sh 配置安全策略", file=sys.stderr)
            print("", file=sys.stderr)
            print("如确认执行硬删除，请添加 --force-hard-delete 参数", file=sys.stderr)
            print("=" * 60, file=sys.stderr)
            sys.exit(2)

    # 如果指定了 --delete，则禁用 dry-run
    dry_run = not args.delete

    try:
        # 根据参数选择清理模式
        if args.tmp_prefix:
            # Tmp 清理模式（不依赖 DB）
            result = run_tmp_gc(
                tmp_prefix=args.tmp_prefix,
                older_than_days=args.tmp_older_than_days,
                dry_run=dry_run,
                delete=args.delete,
                backend=args.backend,
                artifacts_root=args.artifacts_root,
                verbose=not args.quiet,
                concurrency=args.concurrency,
                max_bytes_per_sec=args.max_bytes_per_sec,
                require_ops=args.require_ops,
            )
        else:
            # 孤立清理模式（基于 DB 引用）
            result = run_gc(
                prefix=args.prefix,
                dry_run=dry_run,
                delete=args.delete,
                older_than_days=args.older_than_days,
                trash_prefix=args.trash_prefix,
                dsn=args.dsn,
                backend=args.backend,
                artifacts_root=args.artifacts_root,
                allowed_prefixes=args.allowed_prefixes,
                verbose=not args.quiet,
                concurrency=args.concurrency,
                max_bytes_per_sec=args.max_bytes_per_sec,
                require_ops=args.require_ops,
            )

        if args.json:
            # JSON 输出（包含元信息）
            output = {
                # 元信息
                "gc_mode": result.gc_mode,
                "backend": result.backend,
                "bucket": result.bucket,
                "prefix": result.prefix,
                # 统计数据
                "scanned_count": result.scanned_count,
                "referenced_count": result.referenced_count,
                "protected_count": result.protected_count,
                "candidates_count": result.candidates_count,
                "skipped_by_age": result.skipped_by_age,
                "deleted_count": result.deleted_count,
                "trashed_count": result.trashed_count,
                "failed_count": result.failed_count,
                "total_size_bytes": result.total_size_bytes,
                "deleted_size_bytes": result.deleted_size_bytes,
                "errors": result.errors,
                "candidates": [
                    {
                        "uri": c.uri,
                        "full_path": c.full_path,
                        "size_bytes": c.size_bytes,
                        "age_days": round(c.age_days, 2),
                    }
                    for c in result.candidates
                ],
            }
            print(json.dumps(output, indent=2, ensure_ascii=False))
        else:
            # 输出统计摘要
            print("\n" + "=" * 60)
            if result.gc_mode == "tmp":
                print("TMP-GC 执行摘要")
            else:
                print("GC 执行摘要")
            print("=" * 60)
            print(f"  扫描前缀:     {result.prefix}")
            print(f"  存储后端:     {result.backend or 'auto'}")
            if result.bucket:
                print(f"  Bucket:       {result.bucket}")
            print(f"  扫描文件数:   {result.scanned_count}")
            if result.gc_mode == "orphan":
                print(f"  数据库引用数: {result.referenced_count}")
                print(f"  被保护文件数: {result.protected_count}")
            print(f"  待删除候选:   {result.candidates_count}")
            if args.older_than_days or args.tmp_older_than_days:
                print(f"  因年龄跳过:   {result.skipped_by_age}")
            print(f"  待删除大小:   {result.total_size_bytes / 1024 / 1024:.2f} MB")

            if args.delete:
                print("-" * 60)
                if args.trash_prefix:
                    print(f"  软删除成功:   {result.trashed_count}")
                else:
                    print(f"  硬删除成功:   {result.deleted_count}")
                print(f"  删除失败:     {result.failed_count}")
                print(f"  已释放空间:   {result.deleted_size_bytes / 1024 / 1024:.2f} MB")

            if result.errors:
                print("-" * 60)
                print("错误信息:")
                for error in result.errors[:10]:
                    print(f"  - {error}")
                if len(result.errors) > 10:
                    print(f"  ... 还有 {len(result.errors) - 10} 个错误")

            print("=" * 60)

        # 如果有失败，返回非零退出码
        sys.exit(1 if result.failed_count > 0 else 0)

    except GCPrefixError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(2)
    except GCDatabaseError as e:
        print(f"数据库错误: {e}", file=sys.stderr)
        sys.exit(3)
    except GCOpsCredentialsRequiredError as e:
        print(f"凭证错误: {e}", file=sys.stderr)
        print("\n提示: 设置 ENGRAM_S3_USE_OPS=true 来使用 ops 凭证", file=sys.stderr)
        sys.exit(5)
    except GCError as e:
        print(f"GC 错误: {e}", file=sys.stderr)
        sys.exit(4)
    except KeyboardInterrupt:
        print("\n操作已取消", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"未知错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
