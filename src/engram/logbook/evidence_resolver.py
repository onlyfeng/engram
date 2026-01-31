"""
evidence_resolver.py - Evidence URI 解析模块

功能:
- 解析 memory:// URI 到实际内容
- 支持 patch_blobs 和 attachments 两类资源
- 校验 SHA256 一致性

URI 格式:
    memory://patch_blobs/{source_type}/{source_id}/{sha256}
        Canonical 格式：按 sha256 查询，并校验 source_type/source_id 一致
        或按 source_type+source_id 查询后校验 sha256 一致
    memory://patch_blobs/{source_type}/{source_id}
        旧格式：按 source_type/source_id 查询 patch_blobs 表
    memory://patch_blobs/sha256/{sha256_value}
        按 sha256 查询 patch_blobs 表
    memory://patch_blobs/blob_id/{blob_id}
        按 blob_id 查询 patch_blobs 表
    memory://attachments/{attachment_id}
        按 attachment_id 查询 attachments 表
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import psycopg

from .config import Config, get_config
from .db import get_connection
from .errors import (
    MemoryUriInvalidError,
    MemoryUriNotFoundError,
    Sha256MismatchError,
)
from .hashing import sha256 as compute_sha256
from .uri import UriType, parse_uri, resolve_to_local_path

# SHA256 格式正则：64 位十六进制
_SHA256_PATTERN = re.compile(r"^[a-fA-F0-9]{64}$")


@dataclass
class ResolvedEvidence:
    """解析后的 Evidence 数据"""

    content: bytes  # 实际内容
    sha256: str  # 内容的 SHA256 哈希
    uri: str  # 原始 memory:// URI
    artifact_uri: str  # 底层 artifact URI
    size_bytes: int  # 内容大小
    resource_type: str  # 资源类型 (patch_blobs/attachments)
    resource_id: str  # 资源标识

    def __repr__(self) -> str:
        return f"ResolvedEvidence(type={self.resource_type}, id={self.resource_id}, size={self.size_bytes})"


def resolve_memory_uri(
    uri: str,
    conn: Optional[psycopg.Connection] = None,
    artifacts_root: Optional[Union[str, Path]] = None,
    config: Optional[Config] = None,
    verify_sha256: bool = True,
) -> ResolvedEvidence:
    """
    解析 memory:// URI 到实际内容

    Args:
        uri: memory:// 格式的 URI
        conn: 可选的数据库连接（不提供则自动创建）
        artifacts_root: 制品根目录
        config: 配置实例
        verify_sha256: 是否验证 SHA256（默认 True）

    Returns:
        ResolvedEvidence 对象，包含内容和元数据

    Raises:
        MemoryUriInvalidError: URI 格式无效
        MemoryUriNotFoundError: 资源未找到
        Sha256MismatchError: SHA256 校验失败

    示例:
        # 按 source_type/source_id 查询 patch_blob
        resolve_memory_uri("memory://patch_blobs/git/repo-1:abc123")

        # 按 attachment_id 查询 attachment
        resolve_memory_uri("memory://attachments/12345")
    """
    # 解析 URI
    parsed = parse_uri(uri)

    if parsed.uri_type != UriType.MEMORY:
        raise MemoryUriInvalidError(
            f"非 memory:// URI: {uri}",
            {"uri": uri, "scheme": parsed.scheme},
        )

    # 解析路径组件
    path_parts = parsed.path.strip("/").split("/")
    if len(path_parts) < 2:
        raise MemoryUriInvalidError(
            f"memory:// URI 路径格式无效: {uri}",
            {"uri": uri, "path": parsed.path},
        )

    resource_type = path_parts[0]  # patch_blobs 或 attachments

    # 获取配置
    if config is None:
        config = get_config()

    # 获取 artifacts_root（使用统一的配置获取入口）
    if artifacts_root is None:
        from .config import get_effective_artifacts_root

        artifacts_root = get_effective_artifacts_root()

    # 是否需要管理连接
    should_close_conn = False
    if conn is None:
        conn = get_connection(config=config)
        should_close_conn = True

    try:
        if resource_type == "patch_blobs":
            return _resolve_patch_blob(path_parts[1:], uri, conn, artifacts_root, verify_sha256)
        elif resource_type == "attachments":
            return _resolve_attachment(path_parts[1:], uri, conn, artifacts_root, verify_sha256)
        else:
            raise MemoryUriInvalidError(
                f"未知的 memory:// 资源类型: {resource_type}",
                {"uri": uri, "resource_type": resource_type},
            )
    finally:
        if should_close_conn:
            conn.close()


def _is_sha256_hex(value: str) -> bool:
    """检查是否为有效的 64 位 SHA256 十六进制字符串"""
    return bool(_SHA256_PATTERN.match(value))


def _resolve_patch_blob(
    path_parts: list,
    original_uri: str,
    conn: psycopg.Connection,
    artifacts_root: Union[str, Path],
    verify_sha256: bool,
) -> ResolvedEvidence:
    """
    解析 patch_blobs 资源

    支持的路径格式:
    - sha256/{sha256_value}  - 按 SHA256 查找
    - blob_id/{blob_id}      - 按 blob_id 查找
    - {source_type}/{source_id}/{sha256} - Canonical 格式，按 sha256 或 source_type+source_id 查找并交叉校验
    - {source_type}/{source_id} - 旧格式，按 source_type/source_id 查找
    """
    if len(path_parts) < 2:
        raise MemoryUriInvalidError(
            f"patch_blobs 路径格式无效: {original_uri}",
            {"uri": original_uri, "path_parts": path_parts},
        )

    lookup_type = path_parts[0]

    # 检测 Canonical URI 格式: {source_type}/{source_id}/{sha256}
    # 条件: path_parts 长度 >= 3 且最后一个部分是 64 位十六进制
    # 同时第一个部分不能是特殊关键字 (sha256, blob_id)
    is_canonical_uri = (
        len(path_parts) >= 3
        and lookup_type not in ("sha256", "blob_id")
        and _is_sha256_hex(path_parts[-1])
    )

    with conn.cursor() as cur:
        if lookup_type == "sha256":
            # memory://patch_blobs/sha256/{sha256_value}
            sha256_value = path_parts[1]
            cur.execute(
                """
                SELECT blob_id, source_type, source_id, sha256, uri, size_bytes
                FROM patch_blobs
                WHERE sha256 = %s
                LIMIT 1
                """,
                (sha256_value,),
            )
            resource_id = f"sha256:{sha256_value}"

        elif lookup_type == "blob_id":
            # memory://patch_blobs/blob_id/{blob_id}
            blob_id = int(path_parts[1])
            cur.execute(
                """
                SELECT blob_id, source_type, source_id, sha256, uri, size_bytes
                FROM patch_blobs
                WHERE blob_id = %s
                """,
                (blob_id,),
            )
            resource_id = f"blob_id:{blob_id}"

        elif is_canonical_uri:
            # Canonical 格式: memory://patch_blobs/{source_type}/{source_id}/{sha256}
            uri_source_type = lookup_type
            uri_sha256 = path_parts[-1].lower()
            # source_id 可能包含 / ，所以取中间部分
            uri_source_id = "/".join(path_parts[1:-1])
            resource_id = f"{uri_source_type}:{uri_source_id}"

            # 策略：优先按 sha256 查询，校验 source_type/source_id 一致
            cur.execute(
                """
                SELECT blob_id, source_type, source_id, sha256, uri, size_bytes
                FROM patch_blobs
                WHERE sha256 = %s
                LIMIT 1
                """,
                (uri_sha256,),
            )
            row = cur.fetchone()

            if row is not None:
                # 按 sha256 找到了，校验 source_type/source_id 一致性
                _, db_source_type, db_source_id, _, _, _ = row
                if db_source_type != uri_source_type or db_source_id != uri_source_id:
                    raise Sha256MismatchError(
                        "Canonical URI source 校验失败: URI 中的 source_type/source_id 与数据库记录不一致",
                        {
                            "uri": original_uri,
                            "uri_source_type": uri_source_type,
                            "uri_source_id": uri_source_id,
                            "db_source_type": db_source_type,
                            "db_source_id": db_source_id,
                            "sha256": uri_sha256,
                        },
                    )
            else:
                # 按 sha256 没找到，尝试按 source_type+source_id 查询
                cur.execute(
                    """
                    SELECT blob_id, source_type, source_id, sha256, uri, size_bytes
                    FROM patch_blobs
                    WHERE source_type = %s AND source_id = %s
                    """,
                    (uri_source_type, uri_source_id),
                )
                row = cur.fetchone()

                if row is not None:
                    # 按 source_type+source_id 找到了，校验 sha256 一致性
                    _, _, _, db_sha256, _, _ = row
                    if db_sha256.lower() != uri_sha256:
                        raise Sha256MismatchError(
                            "Canonical URI sha256 校验失败: URI 中的 sha256 与数据库记录不一致",
                            {
                                "uri": original_uri,
                                "uri_sha256": uri_sha256,
                                "db_sha256": db_sha256,
                                "source_type": uri_source_type,
                                "source_id": uri_source_id,
                            },
                        )

        else:
            # 旧格式: memory://patch_blobs/{source_type}/{source_id}
            source_type = lookup_type
            source_id = "/".join(path_parts[1:])
            cur.execute(
                """
                SELECT blob_id, source_type, source_id, sha256, uri, size_bytes
                FROM patch_blobs
                WHERE source_type = %s AND source_id = %s
                """,
                (source_type, source_id),
            )
            resource_id = f"{source_type}:{source_id}"

        # 对于非 canonical URI，row 变量来自上面的查询
        if not is_canonical_uri:
            row = cur.fetchone()

        if row is None:
            raise MemoryUriNotFoundError(
                f"patch_blob 未找到: {original_uri}",
                {"uri": original_uri, "resource_id": resource_id},
            )

        blob_id, source_type, source_id, expected_sha256, artifact_uri, size_bytes = row

    # 读取 artifact 内容
    content = _read_artifact_content(artifact_uri, artifacts_root)

    # 校验 SHA256（内容哈希校验）
    if verify_sha256:
        actual_sha256 = compute_sha256(content)
        if actual_sha256 != expected_sha256:
            raise Sha256MismatchError(
                "patch_blob SHA256 校验失败",
                {
                    "uri": original_uri,
                    "expected": expected_sha256,
                    "actual": actual_sha256,
                    "blob_id": blob_id,
                },
            )

    return ResolvedEvidence(
        content=content,
        sha256=expected_sha256,
        uri=original_uri,
        artifact_uri=artifact_uri,
        size_bytes=size_bytes or len(content),
        resource_type="patch_blobs",
        resource_id=f"{source_type}:{source_id}",
    )


def _resolve_attachment(
    path_parts: list,
    original_uri: str,
    conn: psycopg.Connection,
    artifacts_root: Union[str, Path],
    verify_sha256: bool,
) -> ResolvedEvidence:
    """
    解析 attachments 资源

    支持的路径格式:
    - {attachment_id}  - 按 attachment_id 查找
    """
    if len(path_parts) < 1:
        raise MemoryUriInvalidError(
            f"attachments 路径格式无效: {original_uri}",
            {"uri": original_uri, "path_parts": path_parts},
        )

    attachment_id = int(path_parts[0])

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT attachment_id, item_id, kind, sha256, uri, size_bytes
            FROM attachments
            WHERE attachment_id = %s
            """,
            (attachment_id,),
        )
        row = cur.fetchone()

        if row is None:
            raise MemoryUriNotFoundError(
                f"attachment 未找到: {original_uri}",
                {"uri": original_uri, "attachment_id": attachment_id},
            )

        att_id, item_id, kind, expected_sha256, artifact_uri, size_bytes = row

    # 读取 artifact 内容
    content = _read_artifact_content(artifact_uri, artifacts_root)

    # 校验 SHA256
    if verify_sha256:
        actual_sha256 = compute_sha256(content)
        if actual_sha256 != expected_sha256:
            raise Sha256MismatchError(
                "attachment SHA256 校验失败",
                {
                    "uri": original_uri,
                    "expected": expected_sha256,
                    "actual": actual_sha256,
                    "attachment_id": att_id,
                },
            )

    return ResolvedEvidence(
        content=content,
        sha256=expected_sha256,
        uri=original_uri,
        artifact_uri=artifact_uri,
        size_bytes=size_bytes or len(content),
        resource_type="attachments",
        resource_id=f"{att_id}:{kind}",
    )


def _read_artifact_content(
    artifact_uri: str,
    artifacts_root: Union[str, Path],
) -> bytes:
    """
    读取 artifact 内容

    Args:
        artifact_uri: artifact URI
        artifacts_root: 制品根目录

    Returns:
        文件内容

    Raises:
        MemoryUriNotFoundError: 文件不存在
    """
    # 尝试解析为本地路径
    local_path = resolve_to_local_path(artifact_uri, artifacts_root)

    if local_path is None:
        # 尝试直接组合路径
        root = Path(artifacts_root)
        full_path = root / artifact_uri
        if not full_path.exists():
            raise MemoryUriNotFoundError(
                f"artifact 文件不存在: {artifact_uri}",
                {"artifact_uri": artifact_uri, "tried_path": str(full_path)},
            )
        local_path = str(full_path)

    return Path(local_path).read_bytes()


def verify_evidence_sha256(
    uri: str,
    expected_sha256: str,
    conn: Optional[psycopg.Connection] = None,
    artifacts_root: Optional[Union[str, Path]] = None,
    config: Optional[Config] = None,
) -> bool:
    """
    验证 evidence 的 SHA256 是否匹配

    Args:
        uri: memory:// URI
        expected_sha256: 预期的 SHA256 值
        conn: 数据库连接
        artifacts_root: 制品根目录
        config: 配置实例

    Returns:
        True 如果匹配，False 否则

    注意:
        此函数不会抛出 Sha256MismatchError，仅返回布尔值
    """
    try:
        evidence = resolve_memory_uri(
            uri,
            conn=conn,
            artifacts_root=artifacts_root,
            config=config,
            verify_sha256=False,  # 不自动验证，我们手动验证
        )
        actual_sha256 = compute_sha256(evidence.content)
        return actual_sha256.lower() == expected_sha256.lower()
    except (MemoryUriInvalidError, MemoryUriNotFoundError):
        return False


def get_evidence_info(
    uri: str,
    conn: Optional[psycopg.Connection] = None,
    config: Optional[Config] = None,
) -> Optional[dict]:
    """
    获取 evidence 的元数据（不读取内容）

    Args:
        uri: memory:// URI
        conn: 数据库连接
        config: 配置实例

    Returns:
        元数据字典，或 None 如果不存在

    元数据包含:
        - resource_type: 资源类型
        - resource_id: 资源标识
        - sha256: SHA256 值
        - artifact_uri: 底层 artifact URI
        - size_bytes: 大小
    """
    parsed = parse_uri(uri)

    if parsed.uri_type != UriType.MEMORY:
        return None

    path_parts = parsed.path.strip("/").split("/")
    if len(path_parts) < 2:
        return None

    resource_type = path_parts[0]

    if config is None:
        config = get_config()

    should_close_conn = False
    if conn is None:
        conn = get_connection(config=config)
        should_close_conn = True

    try:
        if resource_type == "patch_blobs":
            return _get_patch_blob_info(path_parts[1:], conn)
        elif resource_type == "attachments":
            return _get_attachment_info(path_parts[1:], conn)
        else:
            return None
    finally:
        if should_close_conn:
            conn.close()


def _get_patch_blob_info(path_parts: list, conn: psycopg.Connection) -> Optional[dict]:
    """
    获取 patch_blob 元数据

    支持的路径格式:
    - sha256/{sha256_value}  - 按 SHA256 查找
    - blob_id/{blob_id}      - 按 blob_id 查找
    - {source_type}/{source_id}/{sha256} - Canonical 格式
    - {source_type}/{source_id} - 旧格式
    """
    if len(path_parts) < 2:
        return None

    lookup_type = path_parts[0]

    # 检测 Canonical URI 格式
    is_canonical_uri = (
        len(path_parts) >= 3
        and lookup_type not in ("sha256", "blob_id")
        and _is_sha256_hex(path_parts[-1])
    )

    with conn.cursor() as cur:
        if lookup_type == "sha256":
            cur.execute(
                """
                SELECT blob_id, source_type, source_id, sha256, uri, size_bytes
                FROM patch_blobs WHERE sha256 = %s LIMIT 1
                """,
                (path_parts[1],),
            )
        elif lookup_type == "blob_id":
            cur.execute(
                """
                SELECT blob_id, source_type, source_id, sha256, uri, size_bytes
                FROM patch_blobs WHERE blob_id = %s
                """,
                (int(path_parts[1]),),
            )
        elif is_canonical_uri:
            # Canonical 格式: {source_type}/{source_id}/{sha256}
            uri_source_type = lookup_type
            uri_sha256 = path_parts[-1].lower()
            uri_source_id = "/".join(path_parts[1:-1])

            # 优先按 sha256 查询
            cur.execute(
                """
                SELECT blob_id, source_type, source_id, sha256, uri, size_bytes
                FROM patch_blobs WHERE sha256 = %s LIMIT 1
                """,
                (uri_sha256,),
            )
            row = cur.fetchone()

            if row is not None:
                # 校验 source_type/source_id 一致性
                _, db_source_type, db_source_id, _, _, _ = row
                if db_source_type != uri_source_type or db_source_id != uri_source_id:
                    return None  # 不一致，返回 None
            else:
                # 按 source_type+source_id 查询
                cur.execute(
                    """
                    SELECT blob_id, source_type, source_id, sha256, uri, size_bytes
                    FROM patch_blobs WHERE source_type = %s AND source_id = %s
                    """,
                    (uri_source_type, uri_source_id),
                )
                row = cur.fetchone()

                if row is not None:
                    # 校验 sha256 一致性
                    _, _, _, db_sha256, _, _ = row
                    if db_sha256.lower() != uri_sha256:
                        return None  # 不一致，返回 None
        else:
            # 旧格式
            source_type = lookup_type
            source_id = "/".join(path_parts[1:])
            cur.execute(
                """
                SELECT blob_id, source_type, source_id, sha256, uri, size_bytes
                FROM patch_blobs WHERE source_type = %s AND source_id = %s
                """,
                (source_type, source_id),
            )

        # 对于非 canonical URI，row 变量来自上面的查询
        if not is_canonical_uri:
            row = cur.fetchone()

        if row is None:
            return None

        blob_id, source_type, source_id, sha256_val, artifact_uri, size_bytes = row
        return {
            "resource_type": "patch_blobs",
            "resource_id": f"{source_type}:{source_id}",
            "blob_id": blob_id,
            "sha256": sha256_val,
            "artifact_uri": artifact_uri,
            "size_bytes": size_bytes,
        }


def _get_attachment_info(path_parts: list, conn: psycopg.Connection) -> Optional[dict]:
    """获取 attachment 元数据"""
    if len(path_parts) < 1:
        return None

    attachment_id = int(path_parts[0])

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT attachment_id, item_id, kind, sha256, uri, size_bytes
            FROM attachments WHERE attachment_id = %s
            """,
            (attachment_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None

        att_id, item_id, kind, sha256, artifact_uri, size_bytes = row
        return {
            "resource_type": "attachments",
            "resource_id": f"{att_id}:{kind}",
            "attachment_id": att_id,
            "item_id": item_id,
            "kind": kind,
            "sha256": sha256,
            "artifact_uri": artifact_uri,
            "size_bytes": size_bytes,
        }
