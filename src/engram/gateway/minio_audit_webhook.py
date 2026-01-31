"""
MinIO Audit Webhook 处理模块

提供 POST /minio/audit 端点，接收 MinIO 审计日志并落库。

功能:
- Token 认证 (MINIO_AUDIT_WEBHOOK_AUTH_TOKEN)
- 请求体大小限制
- JSON 解析容错
- 原始审计数据落库到 governance.object_store_audit_events

配置:
- MINIO_AUDIT_WEBHOOK_AUTH_TOKEN: webhook 认证 token（必须配置）
- MINIO_AUDIT_MAX_PAYLOAD_SIZE: 最大 payload 大小（默认 1MB）

安全说明:
- 必须配置 MINIO_AUDIT_WEBHOOK_AUTH_TOKEN 环境变量
- 未配置时返回 503 Service Unavailable

使用方式:
    from engram.gateway.minio_audit_webhook import router as minio_audit_router
    app.include_router(minio_audit_router)
"""

import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .config import get_config

logger = logging.getLogger("gateway.minio_audit")

# 创建路由器
router = APIRouter(prefix="/minio", tags=["minio"])


class MinioAuditError(Exception):
    """MinIO Audit 处理错误"""

    def __init__(self, message: str, status_code: int = 500, request_id: str = ""):
        self.message = message
        self.status_code = status_code
        self.request_id = request_id  # 用于追踪
        super().__init__(message)


def _verify_auth_token(request: Request) -> bool:
    """
    验证请求的认证 token

    支持两种方式:
    1. Authorization: Bearer <token>
    2. X-Minio-Auth-Token: <token>

    Returns:
        True 如果 token 验证通过

    Raises:
        MinioAuditError:
            - 503: webhook 未配置 (MINIO_AUDIT_WEBHOOK_AUTH_TOKEN 为空)
            - 401: token 缺失
            - 403: token 无效
    """
    config = get_config()
    expected_token = config.minio_audit_webhook_auth_token

    # 如果未配置 token，拒绝服务（需要管理员配置）
    if not expected_token:
        raise MinioAuditError(
            "MinIO audit webhook 未配置，请设置 MINIO_AUDIT_WEBHOOK_AUTH_TOKEN", status_code=503
        )

    # 尝试从 Authorization header 获取 token
    auth_header = request.headers.get("Authorization")
    if auth_header:
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            if token == expected_token:
                return True

    # 尝试从 X-Minio-Auth-Token header 获取 token
    minio_token = request.headers.get("X-Minio-Auth-Token")
    if minio_token:
        if minio_token == expected_token:
            return True

    # token 缺失或无效
    if not auth_header and not minio_token:
        raise MinioAuditError("缺少认证 token", status_code=401)

    raise MinioAuditError("认证 token 无效", status_code=403)


async def _read_body_with_limit(request: Request) -> bytes:
    """
    读取请求体并检查大小限制

    Returns:
        请求体字节数据

    Raises:
        MinioAuditError: 请求体过大
    """
    config = get_config()
    max_size = config.minio_audit_max_payload_size

    # 检查 Content-Length header
    content_length = request.headers.get("Content-Length")
    if content_length:
        try:
            length = int(content_length)
            if length > max_size:
                raise MinioAuditError(
                    f"请求体过大: {length} 字节 (最大 {max_size})", status_code=413
                )
        except ValueError:
            pass  # 忽略无效的 Content-Length

    # 读取请求体（带大小限制）
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > max_size:
            raise MinioAuditError(f"请求体过大: 超过 {max_size} 字节限制", status_code=413)

    return body


def _parse_minio_audit_event(body: bytes) -> Dict[str, Any]:
    """
    解析 MinIO 审计事件 JSON

    MinIO 审计日志格式参考:
    https://min.io/docs/minio/linux/operations/monitoring/audit-logging.html

    Args:
        body: 请求体字节数据

    Returns:
        解析后的审计事件字典

    Raises:
        MinioAuditError: JSON 解析失败
    """
    if not body:
        raise MinioAuditError("请求体为空", status_code=400)

    try:
        # 尝试解析 JSON
        data: dict[str, Any] = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict):
            raise MinioAuditError("JSON 必须是对象类型", status_code=400)
        return data
    except json.JSONDecodeError as e:
        raise MinioAuditError(f"JSON 解析失败: {e}", status_code=400)
    except UnicodeDecodeError as e:
        raise MinioAuditError(f"编码错误: {e}", status_code=400)


def normalize_to_schema(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    将 MinIO 审计事件归一化为 object_store_audit_event_v1 schema 格式

    归一化结构包含:
    - schema_version: 固定为 "1.0"
    - provider: 固定为 "minio"
    - raw: 完整保留原始事件
    - 以及其他标准化字段

    MinIO 审计事件结构:
    {
        "version": "1",
        "deploymentid": "...",
        "time": "2024-01-15T10:00:00.000Z",
        "trigger": "incoming",
        "api": {
            "name": "PutObject",
            "bucket": "engram",
            "object": "scm/1/git/commits/abc.diff",
            "status": "200 OK",
            "statusCode": 200,
            "timeToResponse": "15ms",
            ...
        },
        "remotehost": "192.168.1.100:52431",
        "requestID": "REQ-123",
        "userAgent": "...",
        "requestClaims": {
            "accessKey": "...",
            "sub": "...",
            ...
        },
        ...
    }

    Args:
        event: MinIO 原始审计事件

    Returns:
        归一化后的事件字典，符合 object_store_audit_event_v1 schema
    """
    api_info = event.get("api", {})
    request_claims = event.get("requestClaims", {})

    # 提取操作者标识（优先使用 accessKey，其次是 claims 中的其他标识）
    principal = request_claims.get("accessKey") or request_claims.get("sub") or None

    # 解析 remote_host（可能包含端口）
    remote_host = event.get("remotehost", "")
    if remote_host and ":" in remote_host:
        # 移除端口号，只保留 IP
        remote_host = remote_host.rsplit(":", 1)[0]

    # 构建操作类型（使用 s3: 前缀以符合 S3 API 命名约定）
    api_name = api_info.get("name")
    if api_name:
        operation = f"s3:{api_name}"
    else:
        operation = "unknown"

    # 判断操作是否成功（2xx 状态码为成功）
    status_code = api_info.get("statusCode")
    success = status_code is not None and 200 <= status_code < 300

    # 提取 bucket（默认 "unknown" 以满足 schema required 约束）
    bucket = api_info.get("bucket") or "unknown"

    # 提取 user_agent
    user_agent = event.get("userAgent")

    # 提取响应时间（如果有）
    duration_ms = None
    time_to_response = api_info.get("timeToResponse", "")
    if time_to_response:
        # 尝试解析 "15ms" 格式
        if time_to_response.endswith("ms"):
            try:
                duration_ms = int(time_to_response[:-2])
            except ValueError:
                pass

    return {
        # Schema 核心字段
        "schema_version": "1.0",
        "provider": "minio",
        "event_ts": event.get("time"),
        "bucket": bucket,
        "object_key": api_info.get("object"),
        "operation": operation,
        "status_code": status_code,
        "success": success,
        "request_id": event.get("requestID"),
        "principal": principal,
        "remote_ip": remote_host if remote_host else None,
        "user_agent": user_agent,
        "bytes_sent": None,  # MinIO 审计日志中不包含此字段
        "bytes_received": None,  # MinIO 审计日志中不包含此字段
        "duration_ms": duration_ms,
        # 原始事件完整保留
        "raw": event,
    }


def _extract_audit_fields(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    从 MinIO 审计事件中提取关键字段（使用归一化函数）

    此函数是 normalize_to_schema 的包装，用于向后兼容。

    Returns:
        提取后的字段字典，适配 governance.object_store_audit_events 表结构
    """
    return normalize_to_schema(event)


def _insert_audit_to_db(audit_data: Dict[str, Any]) -> int:
    """
    将审计数据写入 governance.object_store_audit_events 表

    归一化后的 audit_data 包含:
    - schema_version: "1.0"
    - provider: "minio"
    - raw: 完整原始事件
    - 以及其他标准化字段

    Args:
        audit_data: 归一化后的审计数据字典（符合 object_store_audit_event_v1 schema）

    Returns:
        创建的 event_id

    Raises:
        MinioAuditError: 审计写入失败时抛出，包含 request_id 用于追踪
            - audit-first 策略: 审计写入失败阻断整个请求处理
    """
    import psycopg

    config = get_config()
    request_id = audit_data.get("request_id") or ""

    # 记录 schema_version 用于日志追踪
    schema_version = audit_data.get("schema_version", "1.0")
    logger.debug(f"插入审计数据，schema_version={schema_version}, request_id={request_id}")

    try:
        conn = psycopg.connect(config.postgres_dsn, autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO governance.object_store_audit_events
                        (provider, event_ts, bucket, object_key, operation,
                         status_code, request_id, principal, remote_ip, raw)
                    VALUES (
                        %s,
                        COALESCE(%s::timestamptz, now()),
                        %s, %s, %s, %s, %s, %s, %s::inet, %s
                    )
                    RETURNING event_id
                    """,
                    (
                        audit_data.get("provider", "minio"),
                        audit_data.get("event_ts"),
                        audit_data.get("bucket"),
                        audit_data.get("object_key"),
                        audit_data.get("operation", "unknown"),
                        audit_data.get("status_code"),
                        request_id,
                        audit_data.get("principal"),
                        audit_data.get("remote_ip"),
                        json.dumps(audit_data.get("raw", {})),
                    ),
                )
                result = cur.fetchone()
                return result[0] if result else 0
        finally:
            conn.close()
    except psycopg.Error as e:
        logger.error(f"写入审计日志失败: {e}, request_id={request_id}")
        raise MinioAuditError(
            f"数据库写入失败: {e}",
            status_code=500,
            request_id=request_id,
        )


@router.post("/audit")
async def minio_audit_webhook(request: Request) -> JSONResponse:
    """
    MinIO Audit Webhook 端点

    接收 MinIO 审计日志并落库到 governance.artifact_ops_audit 表。

    认证方式:
    - Authorization: Bearer <token>
    - X-Minio-Auth-Token: <token>

    请求体:
    - JSON 格式的 MinIO 审计事件

    响应:
    - 200: 成功落库
    - 400: 请求体解析失败
    - 401: 缺少认证 token
    - 403: 认证 token 无效
    - 413: 请求体过大
    - 500: 内部错误
    - 503: webhook 未配置
    """
    try:
        # 1. 验证 token
        _verify_auth_token(request)

        # 2. 读取请求体（带大小限制）
        body = await _read_body_with_limit(request)

        # 3. 解析 JSON
        event = _parse_minio_audit_event(body)

        # 4. 提取关键字段
        audit_data = _extract_audit_fields(event)

        # 5. 写入数据库
        event_id = _insert_audit_to_db(audit_data)

        logger.info(
            f"MinIO audit 落库成功: event_id={event_id}, "
            f"operation={audit_data.get('operation')}, "
            f"bucket={audit_data.get('bucket')}, "
            f"object_key={audit_data.get('object_key')}"
        )

        return JSONResponse(
            content={
                "ok": True,
                "event_id": event_id,
                "message": "审计日志已记录",
            },
            status_code=200,
        )

    except MinioAuditError as e:
        # 根据错误类型选择合适的日志级别：
        # - 503 未配置：info（配置问题，不应产生告警噪声）
        # - 401/403 认证失败：debug（正常的认证拒绝）
        # - 400 请求格式错误：debug（客户端错误）
        # - 500+ 服务端错误：warning（审计写入失败需要告警）
        if e.status_code == 503:
            logger.info(f"MinIO audit webhook 未配置: {e.message}")
        elif e.status_code in (401, 403, 400, 413):
            logger.debug(f"MinIO audit webhook 请求被拒绝: {e.message} (status={e.status_code})")
        else:
            logger.warning(
                f"MinIO audit webhook 错误: {e.message} (status={e.status_code}, request_id={e.request_id})"
            )

        # 构建错误响应，包含追踪信息
        error_response: Dict[str, Any] = {
            "ok": False,
            "error": e.message,
        }
        # 包含 request_id 用于追踪（audit-first 策略：便于定位失败的审计事件）
        if e.request_id:
            error_response["request_id"] = e.request_id

        return JSONResponse(
            content=error_response,
            status_code=e.status_code,
        )
    except Exception as e:
        logger.exception(f"MinIO audit webhook 未预期错误: {e}")
        return JSONResponse(
            content={
                "ok": False,
                "error": f"内部错误: {str(e)}",
            },
            status_code=500,
        )
