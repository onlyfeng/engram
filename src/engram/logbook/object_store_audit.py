"""
engram_logbook.object_store_audit - 对象存储审计 API

提供对象存储（MinIO/S3）审计日志的写入功能，供 Gateway 等模块调用。

表结构（governance.object_store_audit_events）:
- event_id: 事件唯一标识
- provider: 对象存储提供者（minio, aws, gcs 等）
- event_ts: 事件时间戳（原始事件发生时间）
- bucket: 存储桶名称
- object_key: 对象键
- operation: 操作类型（s3:GetObject, s3:PutObject 等）
- status_code: HTTP 状态码
- request_id: 请求 ID（用于追踪和去重）
- principal: 操作者标识（IAM 用户/角色/访问密钥）
- remote_ip: 客户端 IP 地址
- raw: 原始审计日志（JSON 格式）
- ingested_at: 数据入库时间

使用示例:
    from engram.logbook.object_store_audit import (
        ObjectStoreAuditEvent,
        write_object_store_audit_event,
        write_object_store_audit_events_batch,
    )

    # 写入单个审计事件
    event = ObjectStoreAuditEvent(
        provider="minio",
        event_ts=datetime.now(timezone.utc),
        bucket="engram-artifacts",
        object_key="scm/1/git/commits/abc.diff",
        operation="s3:GetObject",
        status_code=200,
        request_id="REQ-123",
        principal="AKIAIOSFODNN7EXAMPLE",
        remote_ip="192.168.1.100",
    )
    write_object_store_audit_event(event)

    # 批量写入
    events = [event1, event2, event3]
    write_object_store_audit_events_batch(events)
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
class ObjectStoreAuditEvent:
    """对象存储审计事件数据类"""

    # 必填字段
    provider: str                       # 对象存储提供者（minio, aws, gcs 等）
    event_ts: datetime                  # 事件时间戳
    bucket: str                         # 存储桶名称
    operation: str                      # 操作类型

    # 可选字段
    object_key: Optional[str] = None    # 对象键
    status_code: Optional[int] = None   # HTTP 状态码
    request_id: Optional[str] = None    # 请求 ID
    principal: Optional[str] = None     # 操作者标识
    remote_ip: Optional[str] = None     # 客户端 IP 地址
    raw: Dict[str, Any] = field(default_factory=dict)  # 原始审计日志

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（排除 None 值）"""
        result = {}
        for k, v in asdict(self).items():
            if v is not None:
                if isinstance(v, datetime):
                    result[k] = v.isoformat()
                else:
                    result[k] = v
        return result


# =============================================================================
# 审计事件写入函数
# =============================================================================


def write_object_store_audit_event(
    event: ObjectStoreAuditEvent,
    dsn: Optional[str] = None,
) -> Optional[int]:
    """
    写入单个对象存储审计事件到数据库

    Args:
        event: ObjectStoreAuditEvent 对象
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
                    INSERT INTO governance.object_store_audit_events (
                        provider, event_ts, bucket, object_key, operation,
                        status_code, request_id, principal, remote_ip, raw
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s
                    ) RETURNING event_id
                """
                params = (
                    event.provider,
                    event.event_ts,
                    event.bucket,
                    event.object_key,
                    event.operation,
                    event.status_code,
                    event.request_id,
                    event.principal,
                    event.remote_ip,
                    json.dumps(event.raw) if event.raw else "{}",
                )
                cur.execute(sql, params)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()
    except (DbConnectionError, Exception):
        # 审计写入失败不应阻塞主流程
        return None


def write_object_store_audit_events_batch(
    events: List[ObjectStoreAuditEvent],
    dsn: Optional[str] = None,
) -> int:
    """
    批量写入对象存储审计事件到数据库

    Args:
        events: ObjectStoreAuditEvent 对象列表
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
                    INSERT INTO governance.object_store_audit_events (
                        provider, event_ts, bucket, object_key, operation,
                        status_code, request_id, principal, remote_ip, raw
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s
                    )
                """
                params_list = [
                    (
                        e.provider,
                        e.event_ts,
                        e.bucket,
                        e.object_key,
                        e.operation,
                        e.status_code,
                        e.request_id,
                        e.principal,
                        e.remote_ip,
                        json.dumps(e.raw) if e.raw else "{}",
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
# 便捷函数
# =============================================================================


def write_minio_audit_event(
    event_ts: datetime,
    bucket: str,
    operation: str,
    object_key: Optional[str] = None,
    status_code: Optional[int] = None,
    request_id: Optional[str] = None,
    principal: Optional[str] = None,
    remote_ip: Optional[str] = None,
    raw: Optional[Dict[str, Any]] = None,
    dsn: Optional[str] = None,
) -> Optional[int]:
    """
    写入 MinIO 审计事件

    Args:
        event_ts: 事件时间戳
        bucket: 存储桶名称
        operation: 操作类型（s3:GetObject, s3:PutObject 等）
        object_key: 对象键
        status_code: HTTP 状态码
        request_id: 请求 ID
        principal: 操作者标识
        remote_ip: 客户端 IP 地址
        raw: 原始审计日志
        dsn: 数据库连接字符串

    Returns:
        插入的 event_id，如果写入失败返回 None
    """
    event = ObjectStoreAuditEvent(
        provider="minio",
        event_ts=event_ts,
        bucket=bucket,
        operation=operation,
        object_key=object_key,
        status_code=status_code,
        request_id=request_id,
        principal=principal,
        remote_ip=remote_ip,
        raw=raw or {},
    )
    return write_object_store_audit_event(event, dsn=dsn)


def write_aws_s3_audit_event(
    event_ts: datetime,
    bucket: str,
    operation: str,
    object_key: Optional[str] = None,
    status_code: Optional[int] = None,
    request_id: Optional[str] = None,
    principal: Optional[str] = None,
    remote_ip: Optional[str] = None,
    raw: Optional[Dict[str, Any]] = None,
    dsn: Optional[str] = None,
) -> Optional[int]:
    """
    写入 AWS S3 审计事件

    Args:
        event_ts: 事件时间戳
        bucket: 存储桶名称
        operation: 操作类型（s3:GetObject, s3:PutObject 等）
        object_key: 对象键
        status_code: HTTP 状态码
        request_id: 请求 ID
        principal: 操作者标识（IAM ARN）
        remote_ip: 客户端 IP 地址
        raw: 原始审计日志
        dsn: 数据库连接字符串

    Returns:
        插入的 event_id，如果写入失败返回 None
    """
    event = ObjectStoreAuditEvent(
        provider="aws",
        event_ts=event_ts,
        bucket=bucket,
        operation=operation,
        object_key=object_key,
        status_code=status_code,
        request_id=request_id,
        principal=principal,
        remote_ip=remote_ip,
        raw=raw or {},
    )
    return write_object_store_audit_event(event, dsn=dsn)


def check_request_id_exists(
    request_id: str,
    dsn: Optional[str] = None,
) -> bool:
    """
    检查 request_id 是否已存在（用于去重）

    Args:
        request_id: 请求 ID
        dsn: 数据库连接字符串

    Returns:
        True 表示已存在，False 表示不存在
    """
    if not request_id:
        return False

    if dsn is None:
        dsn = os.environ.get("POSTGRES_DSN")
        if not dsn:
            return False

    try:
        conn = get_connection(
            dsn=dsn,
            autocommit=True,
            search_path=["governance", "public"],
        )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM governance.object_store_audit_events
                    WHERE request_id = %s LIMIT 1
                    """,
                    (request_id,),
                )
                return cur.fetchone() is not None
        finally:
            conn.close()
    except (DbConnectionError, Exception):
        return False


def write_object_store_audit_events_batch_dedupe(
    events: List[ObjectStoreAuditEvent],
    dsn: Optional[str] = None,
) -> Dict[str, int]:
    """
    批量写入审计事件（自动去重）

    基于 request_id 去重，跳过已存在的事件。

    Args:
        events: ObjectStoreAuditEvent 对象列表
        dsn: 数据库连接字符串

    Returns:
        {"inserted": N, "skipped": M, "total": L}
    """
    if not events:
        return {"inserted": 0, "skipped": 0, "total": 0}

    if dsn is None:
        dsn = os.environ.get("POSTGRES_DSN")
        if not dsn:
            return {"inserted": 0, "skipped": len(events), "total": len(events)}

    # 提取有 request_id 的事件进行去重检查
    request_ids = [e.request_id for e in events if e.request_id]

    existing_ids = set()
    if request_ids:
        try:
            conn = get_connection(
                dsn=dsn,
                autocommit=True,
                search_path=["governance", "public"],
            )
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT DISTINCT request_id 
                        FROM governance.object_store_audit_events
                        WHERE request_id = ANY(%s)
                        """,
                        (request_ids,),
                    )
                    existing_ids = {row[0] for row in cur.fetchall()}
            finally:
                conn.close()
        except (DbConnectionError, Exception):
            pass

    # 过滤掉已存在的事件
    new_events = [
        e for e in events
        if not e.request_id or e.request_id not in existing_ids
    ]

    skipped = len(events) - len(new_events)
    inserted = write_object_store_audit_events_batch(new_events, dsn=dsn)

    return {
        "inserted": inserted,
        "skipped": skipped,
        "total": len(events),
    }


# =============================================================================
# 导出
# =============================================================================

__all__ = [
    # 数据类
    "ObjectStoreAuditEvent",
    # 写入函数
    "write_object_store_audit_event",
    "write_object_store_audit_events_batch",
    "write_object_store_audit_events_batch_dedupe",
    # 便捷函数
    "write_minio_audit_event",
    "write_aws_s3_audit_event",
    # 辅助函数
    "check_request_id_exists",
]
