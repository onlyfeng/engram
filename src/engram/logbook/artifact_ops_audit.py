"""
engram_logbook.artifact_ops_audit - 制品操作审计 API

提供制品操作（GC、删除、迁移等）的审计日志写入功能。

表结构（governance.artifact_ops_audit）:
- event_id: 事件唯一标识
- event_ts: 事件时间戳
- tool: 操作工具（artifact_gc, artifact_delete 等）
- operation: 操作类型（delete, move_to_trash, gc_run_summary 等）
- backend: 存储后端类型（local, file, object, minio, s3 等）
- uri: Artifact 标识（artifact key 或 physical URI）
- bucket: 存储桶名称（对象存储场景）
- object_key: 对象键（对象存储场景）
- trash_prefix: 垃圾桶前缀（软删除场景）
- using_ops_credentials: 是否使用运维凭据
- success: 操作是否成功
- error: 错误信息（失败时）
- details: 详细信息（JSON 格式）

使用示例:
    from engram.logbook.artifact_ops_audit import (
        AuditEvent,
        write_audit_event,
        write_delete_audit_event,
        write_gc_summary_audit_event,
    )

    # 写入单个删除事件
    event = AuditEvent(
        tool="artifact_delete",
        operation="move_to_trash",
        backend="object",
        uri="scm/proj/1/r100.diff",
        bucket="engram-artifacts",
        trash_prefix=".trash/",
        using_ops_credentials=True,
        success=True,
    )
    write_audit_event(event)

    # 使用便捷函数写入删除事件
    write_delete_audit_event(
        uri="scm/proj/1/r100.diff",
        backend="object",
        bucket="engram-artifacts",
        trashed=True,
        trash_prefix=".trash/",
        using_ops_credentials=True,
        success=True,
    )

    # 写入 GC 汇总事件
    write_gc_summary_audit_event(
        gc_mode="orphan",
        backend="object",
        bucket="engram-artifacts",
        prefix="scm/",
        scanned=1000,
        protected=800,
        candidates=200,
        deleted=150,
        trashed=50,
        failed=0,
        using_ops_credentials=True,
        details={"dry_run": False},
    )
"""

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from .db import get_connection, DbConnectionError


# =============================================================================
# 审计事件数据类
# =============================================================================


@dataclass
class AuditEvent:
    """审计事件数据类"""

    # 必填字段
    tool: str                           # 操作工具标识
    operation: str                      # 操作类型
    success: bool                       # 操作是否成功

    # 可选字段
    backend: Optional[str] = None       # 存储后端类型
    uri: Optional[str] = None           # Artifact 标识
    bucket: Optional[str] = None        # 存储桶名称
    object_key: Optional[str] = None    # 对象键
    trash_prefix: Optional[str] = None  # 垃圾桶前缀
    using_ops_credentials: Optional[bool] = None  # 是否使用运维凭据
    error: Optional[str] = None         # 错误信息
    details: Dict[str, Any] = field(default_factory=dict)  # 详细信息

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（排除 None 值）"""
        result = {}
        for k, v in asdict(self).items():
            if v is not None:
                result[k] = v
        return result


# =============================================================================
# 审计事件写入函数
# =============================================================================


def write_audit_event(
    event: AuditEvent,
    dsn: Optional[str] = None,
) -> Optional[int]:
    """
    写入单个审计事件到数据库

    Args:
        event: AuditEvent 对象
        dsn: 数据库连接字符串（可选，默认使用 POSTGRES_DSN 环境变量）

    Returns:
        插入的 event_id，如果写入失败返回 None
    """
    if dsn is None:
        dsn = os.environ.get("POSTGRES_DSN")
        if not dsn:
            # 无数据库连接配置，静默跳过
            return None

    try:
        conn = get_connection(
            dsn=dsn,
            autocommit=True,
            search_path=["governance", "public"],
        )
        try:
            with conn.cursor() as cur:
                # 构建 INSERT 语句
                sql = """
                    INSERT INTO governance.artifact_ops_audit (
                        tool, operation, backend, uri, bucket, object_key,
                        trash_prefix, using_ops_credentials, success, error, details
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s
                    ) RETURNING event_id
                """
                params = (
                    event.tool,
                    event.operation,
                    event.backend,
                    event.uri,
                    event.bucket,
                    event.object_key,
                    event.trash_prefix,
                    event.using_ops_credentials,
                    event.success,
                    event.error,
                    json.dumps(event.details) if event.details else "{}",
                )
                cur.execute(sql, params)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()
    except (DbConnectionError, Exception):
        # 审计写入失败不应阻塞主流程
        return None


def write_audit_events_batch(
    events: List[AuditEvent],
    dsn: Optional[str] = None,
) -> int:
    """
    批量写入审计事件到数据库

    Args:
        events: AuditEvent 对象列表
        dsn: 数据库连接字符串（可选）

    Returns:
        成功插入的事件数量
    """
    if not events:
        return 0

    if dsn is None:
        dsn = os.environ.get("POSTGRES_DSN")
        if not dsn:
            return 0

    try:
        conn = get_connection(
            dsn=dsn,
            autocommit=False,  # 批量写入使用事务
            search_path=["governance", "public"],
        )
        try:
            with conn.cursor() as cur:
                sql = """
                    INSERT INTO governance.artifact_ops_audit (
                        tool, operation, backend, uri, bucket, object_key,
                        trash_prefix, using_ops_credentials, success, error, details
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s
                    )
                """
                params_list = [
                    (
                        e.tool,
                        e.operation,
                        e.backend,
                        e.uri,
                        e.bucket,
                        e.object_key,
                        e.trash_prefix,
                        e.using_ops_credentials,
                        e.success,
                        e.error,
                        json.dumps(e.details) if e.details else "{}",
                    )
                    for e in events
                ]
                cur.executemany(sql, params_list)
            conn.commit()
            return len(events)
        except Exception:
            conn.rollback()
            return 0
        finally:
            conn.close()
    except (DbConnectionError, Exception):
        return 0


# =============================================================================
# 便捷函数：删除事件
# =============================================================================


def write_delete_audit_event(
    uri: str,
    backend: str,
    success: bool,
    trashed: bool = False,
    bucket: Optional[str] = None,
    object_key: Optional[str] = None,
    trash_prefix: Optional[str] = None,
    using_ops_credentials: Optional[bool] = None,
    require_ops: Optional[bool] = None,
    error: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    dsn: Optional[str] = None,
) -> Optional[int]:
    """
    写入删除操作审计事件

    Args:
        uri: Artifact URI
        backend: 存储后端类型
        success: 操作是否成功
        trashed: 是否为软删除
        bucket: 存储桶名称（object 后端）
        object_key: 对象键（object 后端）
        trash_prefix: 垃圾桶前缀
        using_ops_credentials: 是否使用运维凭据
        require_ops: 是否要求 ops 凭证
        error: 错误信息
        details: 额外详细信息
        dsn: 数据库连接字符串

    Returns:
        插入的 event_id，如果写入失败返回 None
    """
    operation = "move_to_trash" if trashed else "delete"

    event_details = details.copy() if details else {}
    if require_ops is not None:
        event_details["require_ops"] = require_ops

    event = AuditEvent(
        tool="artifact_delete",
        operation=operation,
        backend=backend,
        uri=uri,
        bucket=bucket,
        object_key=object_key,
        trash_prefix=trash_prefix,
        using_ops_credentials=using_ops_credentials,
        success=success,
        error=error,
        details=event_details,
    )

    return write_audit_event(event, dsn=dsn)


# =============================================================================
# 便捷函数：GC 汇总事件
# =============================================================================


def write_gc_summary_audit_event(
    gc_mode: str,
    backend: str,
    prefix: str,
    scanned: int,
    protected: int,
    candidates: int,
    deleted: int,
    trashed: int,
    failed: int,
    success: bool = True,
    bucket: Optional[str] = None,
    using_ops_credentials: Optional[bool] = None,
    require_ops: Optional[bool] = None,
    error: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    dsn: Optional[str] = None,
) -> Optional[int]:
    """
    写入 GC 运行汇总审计事件

    Args:
        gc_mode: GC 模式（orphan/tmp）
        backend: 存储后端类型
        prefix: 扫描前缀
        scanned: 扫描的文件总数
        protected: 被保护的文件数
        candidates: 待删除候选数
        deleted: 硬删除数
        trashed: 软删除数
        failed: 删除失败数
        success: 整体操作是否成功
        bucket: 存储桶名称（object 后端）
        using_ops_credentials: 是否使用运维凭据
        require_ops: 是否要求 ops 凭证
        error: 错误信息
        details: 额外详细信息
        dsn: 数据库连接字符串

    Returns:
        插入的 event_id，如果写入失败返回 None
    """
    event_details = details.copy() if details else {}
    event_details.update({
        "gc_mode": gc_mode,
        "prefix": prefix,
        "scanned": scanned,
        "protected": protected,
        "candidates": candidates,
        "deleted": deleted,
        "trashed": trashed,
        "failed": failed,
    })

    if require_ops is not None:
        event_details["require_ops"] = require_ops

    event = AuditEvent(
        tool="artifact_gc",
        operation="gc_run_summary",
        backend=backend,
        uri=None,  # 汇总事件无单个 URI
        bucket=bucket,
        trash_prefix=event_details.get("trash_prefix"),
        using_ops_credentials=using_ops_credentials,
        success=success,
        error=error,
        details=event_details,
    )

    return write_audit_event(event, dsn=dsn)


# =============================================================================
# 便捷函数：GC 单个删除事件
# =============================================================================


def write_gc_delete_audit_event(
    uri: str,
    backend: str,
    success: bool,
    trashed: bool = False,
    bucket: Optional[str] = None,
    object_key: Optional[str] = None,
    trash_prefix: Optional[str] = None,
    using_ops_credentials: Optional[bool] = None,
    require_ops: Optional[bool] = None,
    error: Optional[str] = None,
    size_bytes: Optional[int] = None,
    age_days: Optional[float] = None,
    dsn: Optional[str] = None,
) -> Optional[int]:
    """
    写入 GC 单个删除操作审计事件

    Args:
        uri: Artifact URI
        backend: 存储后端类型
        success: 操作是否成功
        trashed: 是否为软删除
        bucket: 存储桶名称（object 后端）
        object_key: 对象键（object 后端）
        trash_prefix: 垃圾桶前缀
        using_ops_credentials: 是否使用运维凭据
        require_ops: 是否要求 ops 凭证
        error: 错误信息
        size_bytes: 文件大小
        age_days: 文件年龄（天）
        dsn: 数据库连接字符串

    Returns:
        插入的 event_id，如果写入失败返回 None
    """
    operation = "gc_move_to_trash" if trashed else "gc_delete"

    event_details: Dict[str, Any] = {}
    if require_ops is not None:
        event_details["require_ops"] = require_ops
    if size_bytes is not None:
        event_details["size_bytes"] = size_bytes
    if age_days is not None:
        event_details["age_days"] = round(age_days, 2)

    event = AuditEvent(
        tool="artifact_gc",
        operation=operation,
        backend=backend,
        uri=uri,
        bucket=bucket,
        object_key=object_key,
        trash_prefix=trash_prefix,
        using_ops_credentials=using_ops_credentials,
        success=success,
        error=error,
        details=event_details,
    )

    return write_audit_event(event, dsn=dsn)


# =============================================================================
# 辅助函数
# =============================================================================


def get_using_ops_from_env() -> Optional[bool]:
    """
    从环境变量获取当前是否使用 ops 凭证

    Returns:
        True: ENGRAM_S3_USE_OPS=true
        False: ENGRAM_S3_USE_OPS=false 或未设置
        None: 不适用（非 object 后端）
    """
    use_ops = os.environ.get("ENGRAM_S3_USE_OPS", "").lower()
    if use_ops == "true":
        return True
    elif use_ops == "false":
        return False
    return None
