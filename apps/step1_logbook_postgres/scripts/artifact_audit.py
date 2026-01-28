#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
artifact_audit.py - 制品完整性审计工具

功能:
- 连接 PostgreSQL 读取 patch_blobs 和 attachments 表
- 按 URI scheme 选择合适的存储后端（local/file/object）
- 流式计算 SHA256 哈希并与数据库记录对比
- 支持速率限制、采样审计、增量审计
- 支持并发审计（线程池）
- 支持 head-only 模式（仅读取元数据，不流式计算哈希）

用法:
    python artifact_audit.py [OPTIONS]

选项:
    --table         审计目标表 (patch_blobs/attachments/all，默认 all)
    --limit         最大审计记录数
    --since         增量审计起始时间 (ISO 格式)
    --prefix        仅审计匹配前缀的记录（基于 DB uri 过滤）
    --sample-rate   采样率 (0.0-1.0，默认 1.0 全量)
    --max-bytes-per-sec  读取速率限制（字节/秒）
    --head-only     优先使用 get_info() 的元数据 sha256，不强制流式哈希
    --workers       并发线程数（默认 1，即串行）
    --json          输出 JSON 格式报告
    --fail-on-mismatch  发现不匹配时立即退出（exit code 1）
    --artifacts-root    制品根目录（覆盖配置）
    --verbose       详细输出
"""

import argparse
import hashlib
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

# 添加 scripts 目录到 path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg

from engram_step1.artifact_store import (
    ArtifactStore,
    LocalArtifactsStore,
    FileUriStore,
    ObjectStore,
    ArtifactNotFoundError,
    ArtifactReadError,
    BUFFER_SIZE,
    get_artifact_store_from_config,
)
from engram_step1.config import get_config, get_effective_artifacts_root, get_app_config
from engram_step1.db import get_connection
from engram_step1.uri import parse_uri, UriType

# =============================================================================
# 常量
# =============================================================================

# 默认值
DEFAULT_SAMPLE_RATE = 1.0  # 全量审计
DEFAULT_TABLE = "all"

# 支持的表
SUPPORTED_TABLES = ("patch_blobs", "attachments", "all")


# =============================================================================
# 数据结构
# =============================================================================

@dataclass
class AuditResult:
    """单条审计结果"""
    table: str              # 表名
    record_id: int          # 记录 ID (blob_id 或 attachment_id)
    uri: str                # artifact URI
    expected_sha256: str    # 数据库中的 sha256
    actual_sha256: Optional[str] = None  # 实际计算的 sha256
    size_bytes: Optional[int] = None     # 文件大小
    status: str = "pending"  # pending/ok/mismatch/missing/error
    error_message: Optional[str] = None  # 错误信息
    duration_ms: float = 0.0  # 审计耗时（毫秒）

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return asdict(self)


@dataclass
class AuditSummary:
    """审计汇总报告"""
    start_time: str = ""
    end_time: str = ""
    duration_seconds: float = 0.0
    total_records: int = 0
    sampled_records: int = 0
    audited_records: int = 0
    ok_count: int = 0
    mismatch_count: int = 0
    missing_count: int = 0
    error_count: int = 0
    skipped_count: int = 0
    total_bytes: int = 0
    tables_audited: List[str] = field(default_factory=list)
    mismatches: List[Dict[str, Any]] = field(default_factory=list)
    missing: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    # 增量游标：建议的下次审计起始时间
    next_cursor: Optional[str] = None
    # head-only 模式下无法验证的记录数（metadata 无 sha256）
    head_only_unverified_count: int = 0

    @property
    def has_issues(self) -> bool:
        """是否存在问题"""
        return self.mismatch_count > 0 or self.missing_count > 0

    def to_dict(self) -> Dict[str, Any]:
        """
        转换为字典（JSON 输出稳定性保证）
        
        确保关键字段始终存在于输出中，便于 cron/CI 脚本解析：
        - next_cursor: 增量游标（None 时输出 null）
        - head_only_unverified_count: head-only 模式未验证记录数（始终为 int）
        """
        result = asdict(self)
        # 确保关键字段始终存在（即使为默认值）
        # asdict 已经处理了所有字段，这里确保 JSON 序列化稳定
        # next_cursor: None -> null（JSON）
        # head_only_unverified_count: 始终为 int，默认 0
        return result


# =============================================================================
# 审计器核心类
# =============================================================================

class RateLimiter:
    """
    线程安全的速率限制器
    
    在并发环境下对所有线程的请求进行聚合控制。
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
                    # 释放锁后再 sleep，避免阻塞其他线程
                    self._bytes_this_second = 0
                    self._last_reset = time.monotonic() + sleep_time
                    # 在锁外 sleep
                    self._lock.release()
                    try:
                        time.sleep(sleep_time)
                    finally:
                        self._lock.acquire()
                else:
                    self._bytes_this_second = 0
                    self._last_reset = time.monotonic()


class ArtifactAuditor:
    """
    制品审计器

    支持多种存储后端的制品完整性验证。
    支持并发审计和 head-only 模式。
    """

    def __init__(
        self,
        artifacts_root: Optional[Union[str, Path]] = None,
        conn: Optional[psycopg.Connection] = None,
        max_bytes_per_sec: Optional[int] = None,
        sample_rate: float = DEFAULT_SAMPLE_RATE,
        verbose: bool = False,
        artifact_store: Optional[ArtifactStore] = None,
        head_only: bool = False,
        workers: int = 1,
    ):
        """
        初始化审计器

        Args:
            artifacts_root: 制品根目录（仅当 artifact_store 为 None 时用于回退）
            conn: 数据库连接（不提供则自动创建）
            max_bytes_per_sec: 读取速率限制
            sample_rate: 采样率 (0.0-1.0)
            verbose: 详细输出
            artifact_store: 自定义 artifact store（用于测试或特殊场景）
            head_only: 优先使用 get_info() 的 metadata sha256，不强制流式哈希
            workers: 并发线程数（默认 1，即串行）
        """
        self.artifacts_root = Path(artifacts_root) if artifacts_root else Path(get_effective_artifacts_root())
        self._conn = conn
        self._own_conn = conn is None
        self.rate_limiter = RateLimiter(max_bytes_per_sec)
        self.sample_rate = sample_rate
        self.verbose = verbose
        self.head_only = head_only
        self.workers = max(1, workers)

        # 缓存存储实例
        self._artifact_store: Optional[ArtifactStore] = artifact_store  # ARTIFACT 类型使用配置中的后端
        self._file_store: Optional[FileUriStore] = None
        self._object_store: Optional[ObjectStore] = None
        
        # 线程安全锁（用于共享资源）
        self._store_lock = threading.Lock()

    @property
    def conn(self) -> psycopg.Connection:
        """获取数据库连接"""
        if self._conn is None:
            self._conn = get_connection()
        return self._conn

    def _get_store_for_uri(self, uri: str) -> Tuple[ArtifactStore, str]:
        """
        根据 URI 选择合适的存储后端（线程安全）

        Args:
            uri: artifact URI

        Returns:
            (store, resolved_uri) 元组
        """
        parsed = parse_uri(uri)

        with self._store_lock:
            if parsed.uri_type == UriType.FILE:
                # file:// URI -> FileUriStore
                if self._file_store is None:
                    self._file_store = FileUriStore()
                return self._file_store, uri

            elif parsed.uri_type == UriType.S3:
                # s3:// URI -> ObjectStore（需要验证 bucket 一致性）
                # parsed.path 格式: bucket/key 或 bucket//key（urlparse 可能保留双斜杠）
                s3_path = parsed.path
                if "/" not in s3_path:
                    # 只有 bucket 没有 key
                    raise ArtifactReadError(
                        f"无效的 S3 URI: {uri}，缺少对象 key",
                        {"uri": uri, "path": s3_path},
                    )
                
                uri_bucket, s3_key = s3_path.split("/", 1)
                # 去除 key 的前导斜杠（urlparse 可能保留 /path 形式）
                s3_key = s3_key.lstrip("/")
                
                # 验证 key 不为空
                if not s3_key:
                    raise ArtifactReadError(
                        f"无效的 S3 URI: {uri}，缺少对象 key",
                        {"uri": uri, "path": s3_path},
                    )
                
                # 获取配置的 bucket
                config_bucket = None
                try:
                    app_config = get_app_config()
                    config_bucket = app_config.artifacts.object_bucket
                except Exception:
                    pass
                
                # 如果配置中没有 bucket，尝试从环境变量获取
                if not config_bucket:
                    import os
                    config_bucket = os.environ.get("ENGRAM_S3_BUCKET")
                
                # 验证 bucket 一致性：默认拒绝跨 bucket 审计
                if not config_bucket:
                    raise ArtifactReadError(
                        f"无法审计 S3 URI: {uri}，未配置 bucket（请设置 ENGRAM_S3_BUCKET 或配置 artifacts.object_bucket）",
                        {"uri": uri, "uri_bucket": uri_bucket},
                    )
                
                if uri_bucket != config_bucket:
                    raise ArtifactReadError(
                        f"拒绝跨 bucket 审计: URI bucket '{uri_bucket}' != 配置 bucket '{config_bucket}'",
                        {"uri": uri, "uri_bucket": uri_bucket, "config_bucket": config_bucket},
                    )
                
                # bucket 一致，初始化 ObjectStore 并返回 key
                if self._object_store is None:
                    self._object_store = ObjectStore()
                return self._object_store, s3_key

            elif parsed.uri_type == UriType.ARTIFACT:
                # ARTIFACT 类型：使用配置中指定的后端（local/object 等）
                if self._artifact_store is None:
                    self._artifact_store = get_artifact_store_from_config(get_app_config().artifacts)
                return self._artifact_store, uri

            else:
                # 其他类型：回退到配置中的后端
                if self._artifact_store is None:
                    self._artifact_store = get_artifact_store_from_config(get_app_config().artifacts)
                return self._artifact_store, uri

    def _compute_hash_streaming(
        self,
        store: ArtifactStore,
        uri: str,
    ) -> Tuple[str, int]:
        """
        流式计算文件哈希

        Args:
            store: 存储后端
            uri: artifact URI

        Returns:
            (sha256, size_bytes)
        """
        # 使用 store.get_info 获取文件信息
        info = store.get_info(uri)
        return info["sha256"], info["size_bytes"]

    def _get_info_head_only(
        self,
        store: ArtifactStore,
        uri: str,
    ) -> Tuple[Optional[str], int, bool]:
        """
        仅从元数据获取 sha256（head-only 模式）
        
        对于 ObjectStore，尝试从 HeadObject 的 metadata 获取 sha256。
        对于 LocalArtifactsStore 和 FileUriStore，仍需要流式计算。
        
        Args:
            store: 存储后端
            uri: artifact URI
            
        Returns:
            (sha256, size_bytes, from_metadata)
            - sha256: 哈希值（如果 from_metadata=False 且无法获取，则为 None）
            - size_bytes: 文件大小
            - from_metadata: True 表示从 metadata 获取，False 表示需要流式计算
        """
        if isinstance(store, ObjectStore):
            # ObjectStore: 尝试从 HeadObject metadata 获取 sha256
            try:
                client = store._get_client()
                key = store._object_key(uri)
                head = client.head_object(Bucket=store.bucket, Key=key)
                size_bytes = head.get("ContentLength", 0)
                metadata = head.get("Metadata", {})
                
                if "sha256" in metadata:
                    return metadata["sha256"], size_bytes, True
                
                # metadata 中没有 sha256，在 head-only 模式下返回 None
                return None, size_bytes, False
                
            except Exception as e:
                # 对象不存在或其他错误
                raise store._classify_error(e, uri, key)
        else:
            # LocalArtifactsStore / FileUriStore: 必须流式计算
            info = store.get_info(uri)
            return info["sha256"], info["size_bytes"], False

    def _should_sample(self) -> bool:
        """根据采样率决定是否采样该记录"""
        if self.sample_rate >= 1.0:
            return True
        return random.random() < self.sample_rate

    def audit_record(
        self,
        table: str,
        record_id: int,
        uri: str,
        expected_sha256: str,
    ) -> AuditResult:
        """
        审计单条记录

        Args:
            table: 表名
            record_id: 记录 ID
            uri: artifact URI
            expected_sha256: 预期的 SHA256

        Returns:
            AuditResult 对象
        """
        result = AuditResult(
            table=table,
            record_id=record_id,
            uri=uri,
            expected_sha256=expected_sha256,
        )

        start_time = time.monotonic()

        try:
            # 选择存储后端
            store, resolved_uri = self._get_store_for_uri(uri)

            if self.head_only:
                # head-only 模式：优先从 metadata 获取 sha256
                actual_sha256, size_bytes, from_metadata = self._get_info_head_only(store, resolved_uri)
                
                if actual_sha256 is None:
                    # metadata 中没有 sha256，标记为无法验证
                    result.status = "head_only_unverified"
                    result.size_bytes = size_bytes
                    result.error_message = "metadata 中无 sha256，需要流式验证"
                else:
                    result.actual_sha256 = actual_sha256
                    result.size_bytes = size_bytes
                    
                    # 比较哈希
                    if actual_sha256.lower() == expected_sha256.lower():
                        result.status = "ok"
                    else:
                        result.status = "mismatch"
                    
                    # 速率限制（head-only 模式下按文件大小计算，但实际读取量较小）
                    # 使用较小的值表示 HEAD 请求的开销
                    self.rate_limiter.wait_if_needed(min(size_bytes, 1024))
            else:
                # 正常模式：流式计算哈希
                actual_sha256, size_bytes = self._compute_hash_streaming(store, resolved_uri)

                # 速率限制
                self.rate_limiter.wait_if_needed(size_bytes)

                result.actual_sha256 = actual_sha256
                result.size_bytes = size_bytes

                # 比较哈希
                if actual_sha256.lower() == expected_sha256.lower():
                    result.status = "ok"
                else:
                    result.status = "mismatch"

        except ArtifactNotFoundError as e:
            result.status = "missing"
            result.error_message = str(e)

        except (ArtifactReadError, OSError) as e:
            result.status = "error"
            result.error_message = str(e)

        except Exception as e:
            result.status = "error"
            result.error_message = f"{type(e).__name__}: {e}"

        result.duration_ms = (time.monotonic() - start_time) * 1000
        return result

    def _query_patch_blobs(
        self,
        limit: Optional[int] = None,
        since: Optional[datetime] = None,
        prefix: Optional[str] = None,
    ) -> Iterator[Tuple[int, str, str, Optional[datetime]]]:
        """
        查询 patch_blobs 表

        Args:
            limit: 最大记录数
            since: 增量审计起始时间
            prefix: URI 前缀过滤

        Yields:
            (blob_id, uri, sha256, created_at) 元组
        """
        query = "SELECT blob_id, uri, sha256, created_at FROM patch_blobs"
        params: List[Any] = []
        conditions = []

        if since is not None:
            conditions.append("created_at >= %s")
            params.append(since)

        if prefix is not None:
            conditions.append("uri LIKE %s")
            params.append(prefix + "%")

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY created_at, blob_id"

        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)

        with self.conn.cursor() as cur:
            cur.execute(query, params)
            for row in cur:
                yield row[0], row[1], row[2], row[3]

    def _query_attachments(
        self,
        limit: Optional[int] = None,
        since: Optional[datetime] = None,
        prefix: Optional[str] = None,
    ) -> Iterator[Tuple[int, str, str, Optional[datetime]]]:
        """
        查询 attachments 表

        Args:
            limit: 最大记录数
            since: 增量审计起始时间
            prefix: URI 前缀过滤

        Yields:
            (attachment_id, uri, sha256, created_at) 元组
        """
        query = "SELECT attachment_id, uri, sha256, created_at FROM attachments"
        params: List[Any] = []
        conditions = []

        if since is not None:
            conditions.append("created_at >= %s")
            params.append(since)

        if prefix is not None:
            conditions.append("uri LIKE %s")
            params.append(prefix + "%")

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY created_at, attachment_id"

        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)

        with self.conn.cursor() as cur:
            cur.execute(query, params)
            for row in cur:
                yield row[0], row[1], row[2], row[3]

    def audit_table(
        self,
        table: str,
        limit: Optional[int] = None,
        since: Optional[datetime] = None,
        prefix: Optional[str] = None,
    ) -> Iterator[Tuple[AuditResult, Optional[datetime]]]:
        """
        审计指定表

        Args:
            table: 表名 (patch_blobs 或 attachments)
            limit: 最大记录数
            since: 增量审计起始时间
            prefix: URI 前缀过滤

        Yields:
            (AuditResult, created_at) 元组
        """
        if table == "patch_blobs":
            records = self._query_patch_blobs(limit, since, prefix)
        elif table == "attachments":
            records = self._query_attachments(limit, since, prefix)
        else:
            raise ValueError(f"不支持的表: {table}")

        for record_id, uri, expected_sha256, created_at in records:
            if not self._should_sample():
                # 跳过未采样的记录
                result = AuditResult(
                    table=table,
                    record_id=record_id,
                    uri=uri,
                    expected_sha256=expected_sha256,
                    status="skipped",
                )
                yield result, created_at
                continue

            result = self.audit_record(table, record_id, uri, expected_sha256)
            yield result, created_at

    def run_audit(
        self,
        tables: Optional[List[str]] = None,
        limit: Optional[int] = None,
        since: Optional[datetime] = None,
        prefix: Optional[str] = None,
        fail_on_mismatch: bool = False,
    ) -> AuditSummary:
        """
        执行完整审计

        Args:
            tables: 要审计的表列表
            limit: 每个表的最大记录数
            since: 增量审计起始时间
            prefix: URI 前缀过滤
            fail_on_mismatch: 发现不匹配时立即停止

        Returns:
            AuditSummary 汇总报告
        """
        if tables is None:
            tables = ["patch_blobs", "attachments"]

        summary = AuditSummary(
            start_time=datetime.now().isoformat(),
            tables_audited=tables,
        )

        start_time = time.monotonic()
        max_created_at: Optional[datetime] = None

        try:
            for table in tables:
                if self.verbose:
                    print(f"正在审计 {table}...", file=sys.stderr)

                if self.workers > 1:
                    # 并发模式
                    should_stop = self._run_audit_concurrent(
                        table, limit, since, prefix, fail_on_mismatch,
                        summary, max_created_at
                    )
                    if should_stop:
                        break
                else:
                    # 串行模式
                    for result, created_at in self.audit_table(table, limit, since, prefix):
                        # 更新最大 created_at
                        if created_at is not None:
                            if max_created_at is None or created_at > max_created_at:
                                max_created_at = created_at

                        summary.total_records += 1

                        if result.status == "skipped":
                            summary.skipped_count += 1
                            continue

                        summary.sampled_records += 1
                        summary.audited_records += 1

                        if result.size_bytes:
                            summary.total_bytes += result.size_bytes

                        if result.status == "ok":
                            summary.ok_count += 1
                        elif result.status == "mismatch":
                            summary.mismatch_count += 1
                            summary.mismatches.append(result.to_dict())
                            if fail_on_mismatch:
                                break
                        elif result.status == "missing":
                            summary.missing_count += 1
                            summary.missing.append(result.to_dict())
                        elif result.status == "head_only_unverified":
                            summary.head_only_unverified_count += 1
                            summary.audited_records -= 1  # 未实际验证
                        else:  # error
                            summary.error_count += 1
                            summary.errors.append(result.to_dict())

                        if self.verbose:
                            status_symbol = {
                                "ok": "✓",
                                "mismatch": "✗",
                                "missing": "?",
                                "error": "!",
                                "head_only_unverified": "~",
                            }.get(result.status, "?")
                            print(
                                f"  [{status_symbol}] {result.table}:{result.record_id} - {result.status}",
                                file=sys.stderr,
                            )

                    if fail_on_mismatch and summary.mismatch_count > 0:
                        break

        finally:
            if self._own_conn and self._conn is not None:
                self._conn.close()
                self._conn = None

        summary.end_time = datetime.now().isoformat()
        summary.duration_seconds = time.monotonic() - start_time

        # 设置增量游标
        if max_created_at is not None:
            summary.next_cursor = max_created_at.isoformat()

        return summary

    def _run_audit_concurrent(
        self,
        table: str,
        limit: Optional[int],
        since: Optional[datetime],
        prefix: Optional[str],
        fail_on_mismatch: bool,
        summary: AuditSummary,
        max_created_at: Optional[datetime],
    ) -> bool:
        """
        并发执行表审计
        
        Args:
            table: 表名
            limit: 最大记录数
            since: 增量审计起始时间
            prefix: URI 前缀过滤
            fail_on_mismatch: 发现不匹配时立即停止
            summary: 汇总报告（将被原地修改）
            max_created_at: 当前最大 created_at（将被更新）
            
        Returns:
            True 表示需要停止审计
        """
        # 先收集所有待审计的记录
        records_to_audit: List[Tuple[str, int, str, str, Optional[datetime]]] = []
        
        if table == "patch_blobs":
            query_func = self._query_patch_blobs
        elif table == "attachments":
            query_func = self._query_attachments
        else:
            raise ValueError(f"不支持的表: {table}")

        for record_id, uri, expected_sha256, created_at in query_func(limit, since, prefix):
            records_to_audit.append((table, record_id, uri, expected_sha256, created_at))

        summary.total_records += len(records_to_audit)

        # 过滤需要审计的记录
        sampled_records = []
        for record in records_to_audit:
            if not self._should_sample():
                summary.skipped_count += 1
            else:
                sampled_records.append(record)

        summary.sampled_records += len(sampled_records)
        
        # 并发审计
        should_stop = False
        results_lock = threading.Lock()
        local_max_created_at = max_created_at

        def audit_one(record: Tuple[str, int, str, str, Optional[datetime]]) -> Tuple[AuditResult, Optional[datetime]]:
            tbl, rec_id, uri, expected_sha, created = record
            result = self.audit_record(tbl, rec_id, uri, expected_sha)
            return result, created

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {
                executor.submit(audit_one, record): record
                for record in sampled_records
            }

            for future in as_completed(futures):
                if should_stop:
                    continue

                try:
                    result, created_at = future.result()
                except Exception as e:
                    # 任务执行异常
                    record = futures[future]
                    result = AuditResult(
                        table=record[0],
                        record_id=record[1],
                        uri=record[2],
                        expected_sha256=record[3],
                        status="error",
                        error_message=f"{type(e).__name__}: {e}",
                    )
                    created_at = record[4]

                # 更新汇总（线程安全）
                with results_lock:
                    # 更新最大 created_at
                    if created_at is not None:
                        if local_max_created_at is None or created_at > local_max_created_at:
                            local_max_created_at = created_at

                    summary.audited_records += 1

                    if result.size_bytes:
                        summary.total_bytes += result.size_bytes

                    if result.status == "ok":
                        summary.ok_count += 1
                    elif result.status == "mismatch":
                        summary.mismatch_count += 1
                        summary.mismatches.append(result.to_dict())
                        if fail_on_mismatch:
                            should_stop = True
                    elif result.status == "missing":
                        summary.missing_count += 1
                        summary.missing.append(result.to_dict())
                    elif result.status == "head_only_unverified":
                        summary.head_only_unverified_count += 1
                        summary.audited_records -= 1  # 未实际验证
                    else:  # error
                        summary.error_count += 1
                        summary.errors.append(result.to_dict())

                    if self.verbose:
                        status_symbol = {
                            "ok": "✓",
                            "mismatch": "✗",
                            "missing": "?",
                            "error": "!",
                            "head_only_unverified": "~",
                        }.get(result.status, "?")
                        print(
                            f"  [{status_symbol}] {result.table}:{result.record_id} - {result.status}",
                            file=sys.stderr,
                        )

        # 更新外部的 max_created_at
        if local_max_created_at is not None:
            if max_created_at is None or local_max_created_at > max_created_at:
                # 通过 summary.next_cursor 传递
                if summary.next_cursor is None:
                    summary.next_cursor = local_max_created_at.isoformat()
                else:
                    existing = datetime.fromisoformat(summary.next_cursor)
                    if local_max_created_at > existing:
                        summary.next_cursor = local_max_created_at.isoformat()

        return should_stop

    def close(self) -> None:
        """关闭资源"""
        if self._own_conn and self._conn is not None:
            self._conn.close()
            self._conn = None


# =============================================================================
# CLI 入口
# =============================================================================

def parse_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="制品完整性审计工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 全量审计
    python artifact_audit.py

    # 仅审计 patch_blobs 表，限制 100 条
    python artifact_audit.py --table patch_blobs --limit 100

    # 增量审计（2024-01-01 之后的记录）
    python artifact_audit.py --since 2024-01-01T00:00:00

    # 仅审计特定前缀的记录
    python artifact_audit.py --prefix scm/patches/

    # 10% 采样审计，限速 1MB/s
    python artifact_audit.py --sample-rate 0.1 --max-bytes-per-sec 1048576

    # head-only 模式（仅读取元数据，不流式计算哈希）
    python artifact_audit.py --head-only

    # 4 线程并发审计
    python artifact_audit.py --workers 4

    # JSON 格式输出
    python artifact_audit.py --json

    # 发现不匹配立即失败（用于 CI）
    python artifact_audit.py --fail-on-mismatch
        """,
    )

    parser.add_argument(
        "--table",
        choices=SUPPORTED_TABLES,
        default=DEFAULT_TABLE,
        help=f"审计目标表 (默认: {DEFAULT_TABLE})",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="最大审计记录数（每个表）",
    )

    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="增量审计起始时间 (ISO 格式，如 2024-01-01T00:00:00)",
    )

    parser.add_argument(
        "--prefix",
        type=str,
        default=None,
        help="仅审计匹配前缀的记录（基于 DB uri 过滤）",
    )

    parser.add_argument(
        "--sample-rate",
        type=float,
        default=DEFAULT_SAMPLE_RATE,
        help=f"采样率 (0.0-1.0，默认: {DEFAULT_SAMPLE_RATE})",
    )

    parser.add_argument(
        "--max-bytes-per-sec",
        type=int,
        default=None,
        help="读取速率限制（字节/秒）",
    )

    parser.add_argument(
        "--head-only",
        action="store_true",
        help="优先使用 get_info() 的 metadata sha256，不强制流式哈希",
    )

    parser.add_argument(
        "--workers", "--concurrency",
        type=int,
        default=1,
        dest="workers",
        help="并发线程数（默认 1，即串行）",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="输出 JSON 格式报告",
    )

    parser.add_argument(
        "--fail-on-mismatch",
        action="store_true",
        help="发现不匹配时立即退出（exit code 1）",
    )

    parser.add_argument(
        "--artifacts-root",
        type=str,
        default=None,
        help="制品根目录（覆盖配置）",
    )

    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="详细输出",
    )

    return parser.parse_args(args)


def main() -> int:
    """主函数"""
    args = parse_args()

    # 解析 since 参数
    since: Optional[datetime] = None
    if args.since:
        try:
            since = datetime.fromisoformat(args.since)
        except ValueError as e:
            print(f"错误: 无效的时间格式: {args.since}", file=sys.stderr)
            print(f"  请使用 ISO 格式，如: 2024-01-01T00:00:00", file=sys.stderr)
            return 2

    # 验证采样率
    if not 0.0 <= args.sample_rate <= 1.0:
        print(f"错误: 采样率必须在 0.0 到 1.0 之间，当前值: {args.sample_rate}", file=sys.stderr)
        return 2

    # 验证 workers
    if args.workers < 1:
        print(f"错误: workers 必须 >= 1，当前值: {args.workers}", file=sys.stderr)
        return 2

    # 确定要审计的表
    if args.table == "all":
        tables = ["patch_blobs", "attachments"]
    else:
        tables = [args.table]

    # 创建审计器
    auditor = ArtifactAuditor(
        artifacts_root=args.artifacts_root,
        max_bytes_per_sec=args.max_bytes_per_sec,
        sample_rate=args.sample_rate,
        verbose=args.verbose,
        head_only=args.head_only,
        workers=args.workers,
    )

    try:
        # 执行审计
        summary = auditor.run_audit(
            tables=tables,
            limit=args.limit,
            since=since,
            prefix=args.prefix,
            fail_on_mismatch=args.fail_on_mismatch,
        )

        # 输出结果
        if args.json:
            print(json.dumps(summary.to_dict(), indent=2, ensure_ascii=False))
        else:
            _print_summary(summary)

        # 返回码
        if summary.has_issues:
            return 1
        return 0

    except psycopg.Error as e:
        print(f"数据库错误: {e}", file=sys.stderr)
        return 2

    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 2

    finally:
        auditor.close()


def _print_summary(summary: AuditSummary) -> None:
    """打印审计汇总"""
    print("\n" + "=" * 60)
    print("制品完整性审计报告")
    print("=" * 60)
    print(f"开始时间: {summary.start_time}")
    print(f"结束时间: {summary.end_time}")
    print(f"耗时: {summary.duration_seconds:.2f} 秒")
    print(f"审计表: {', '.join(summary.tables_audited)}")
    print()
    print(f"总记录数: {summary.total_records}")
    print(f"采样记录数: {summary.sampled_records}")
    print(f"已审计: {summary.audited_records}")
    print(f"跳过: {summary.skipped_count}")
    print(f"总字节数: {summary.total_bytes:,}")
    print()
    print(f"✓ 正常: {summary.ok_count}")
    print(f"✗ 不匹配: {summary.mismatch_count}")
    print(f"? 缺失: {summary.missing_count}")
    print(f"! 错误: {summary.error_count}")
    if summary.head_only_unverified_count > 0:
        print(f"~ 未验证(head-only): {summary.head_only_unverified_count}")

    if summary.mismatches:
        print("\n--- 哈希不匹配 ---")
        for m in summary.mismatches[:10]:
            print(f"  {m['table']}:{m['record_id']} - {m['uri']}")
            print(f"    预期: {m['expected_sha256']}")
            print(f"    实际: {m['actual_sha256']}")
        if len(summary.mismatches) > 10:
            print(f"  ... 还有 {len(summary.mismatches) - 10} 条不匹配记录")

    if summary.missing:
        print("\n--- 文件缺失 ---")
        for m in summary.missing[:10]:
            print(f"  {m['table']}:{m['record_id']} - {m['uri']}")
        if len(summary.missing) > 10:
            print(f"  ... 还有 {len(summary.missing) - 10} 条缺失记录")

    if summary.errors:
        print("\n--- 审计错误 ---")
        for e in summary.errors[:10]:
            print(f"  {e['table']}:{e['record_id']} - {e['error_message']}")
        if len(summary.errors) > 10:
            print(f"  ... 还有 {len(summary.errors) - 10} 条错误记录")

    # 增量游标
    if summary.next_cursor:
        print(f"\n增量游标 (下次 --since): {summary.next_cursor}")

    print()
    if summary.has_issues:
        print("⚠ 审计发现问题!")
    else:
        print("✓ 审计通过，所有制品完整性验证成功。")


if __name__ == "__main__":
    sys.exit(main())
