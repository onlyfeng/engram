"""
step3_readers.py - Step3 统一的 Evidence 读取模块

功能:
- 统一读取各类 Evidence URI 的文本内容
- 支持 artifact key、artifact:// 和 memory:// 等格式
- 可选 SHA256 校验（用于 consistency check 模式）

URI 类型支持:
1. Artifact Key（无 scheme 或 artifact://）
   - scm/proj_a/1/svn/r100/abc123.diff
   - artifact://scm/proj_a/1/svn/r100/abc123.diff
   - 复用 Step1 的 resolve_to_local_path 或 artifacts 模块

2. Memory URI - patch_blobs（memory://patch_blobs/...）
   - memory://patch_blobs/{source_type}/{source_id}/{sha256}
   - 解析出 (source_type, source_id, sha256)，查询 scm.patch_blobs 表
   - 读取 patch_blobs.uri 指向的制品

3. Memory URI - attachments（memory://attachments/...）
   - memory://attachments/{attachment_id}/{sha256}
   - 解析出 (attachment_id, sha256)，查询 logbook.attachments 表
   - 读取 attachments.uri 指向的制品

使用示例:
    from step3_readers import read_evidence_text

    # 读取 artifact key
    text = read_evidence_text("scm/proj_a/1/svn/r100/abc123.diff")

    # 读取 patch_blobs memory:// URI 并校验 SHA256
    text = read_evidence_text(
        "memory://patch_blobs/git/1:abc123/sha256hash",
        verify_sha256=True
    )

    # 读取 attachments memory:// URI
    text = read_evidence_text(
        "memory://attachments/12345/sha256hash",
        verify_sha256=True
    )
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union


@dataclass
class EvidenceReadResult:
    """Evidence 读取结果"""
    text: str                      # 文本内容
    sha256: str                    # 内容的 SHA256 哈希
    size_bytes: int                # 内容大小（字节）
    uri: str                       # 原始 URI
    artifact_uri: Optional[str]    # 底层 artifact URI（memory:// URI 时有值）
    source_type: Optional[str]     # 源类型（memory://patch_blobs 时有值）
    source_id: Optional[str]       # 源标识（memory://patch_blobs 时有值）

    def __repr__(self) -> str:
        return f"EvidenceReadResult(uri={self.uri!r}, size={self.size_bytes})"


class EvidenceReadError(Exception):
    """Evidence 读取错误基类"""
    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.details = details or {}


class EvidenceNotFoundError(EvidenceReadError):
    """Evidence 未找到"""
    pass


class EvidenceSha256MismatchError(EvidenceReadError):
    """SHA256 校验失败"""
    pass


class EvidenceUriInvalidError(EvidenceReadError):
    """URI 格式无效"""
    pass


def _compute_sha256(content: bytes) -> str:
    """计算 SHA256 哈希"""
    return hashlib.sha256(content).hexdigest()


def read_evidence_text(
    uri: str,
    conn=None,
    artifacts_root: Optional[Union[str, Path]] = None,
    config=None,
    verify_sha256: bool = False,
    expected_sha256: Optional[str] = None,
    encoding: str = "utf-8",
    docs_root: Optional[Union[str, Path]] = None,
) -> EvidenceReadResult:
    """
    统一读取 Evidence 文本内容

    支持的 URI 格式:
    1. Artifact Key（无 scheme 或 artifact://）
       - scm/proj_a/1/svn/r100/abc123.diff
       - artifact://scm/proj_a/1/svn/r100/abc123.diff

    2. Memory URI - patch_blobs（memory://patch_blobs/...）
       - memory://patch_blobs/{source_type}/{source_id}/{sha256}
       - 解析并查询 Step1 的 scm.patch_blobs 表

    3. Memory URI - attachments（memory://attachments/...）
       - memory://attachments/{attachment_id}/{sha256}
       - 解析并查询 Step1 的 logbook.attachments 表

    4. Memory URI - docs（memory://docs/...）
       - memory://docs/{rel_path}/{sha256}
       - 读取本地文件系统中的文档

    Args:
        uri: Evidence URI 或 artifact key
        conn: 可选的数据库连接（memory:// URI 需要）
        artifacts_root: 制品根目录（可选，不提供则使用配置）
        config: Step1 配置实例（可选）
        verify_sha256: 是否验证 SHA256（consistency check 模式）
        expected_sha256: 预期的 SHA256 值（verify_sha256=True 时可选，
                         如果不提供，对于 memory:// URI 会使用 URI 中或 DB 中的值）
        encoding: 文本编码（默认 utf-8）
        docs_root: 文档根目录（memory://docs/ URI 需要，默认为当前工作目录）

    Returns:
        EvidenceReadResult 对象

    Raises:
        EvidenceNotFoundError: Evidence 未找到
        EvidenceSha256MismatchError: SHA256 校验失败
        EvidenceUriInvalidError: URI 格式无效

    示例:
        # 简单读取
        result = read_evidence_text("scm/proj_a/1/svn/r100/abc123.diff")
        print(result.text)

        # 带 SHA256 校验
        result = read_evidence_text(
            "memory://patch_blobs/git/1:abc123/sha256hash",
            verify_sha256=True
        )

        # 读取本地文档
        result = read_evidence_text(
            "memory://docs/contracts/evidence_packet.md/abc123...",
            docs_root="/path/to/repo"
        )
    """
    # 导入 Step1 模块
    try:
        from engram_step1.uri import (
            parse_uri,
            UriType,
            parse_evidence_uri,
            resolve_to_local_path,
        )
        from engram_step1.config import get_config, get_effective_artifacts_root
    except ImportError as e:
        raise EvidenceReadError(
            f"无法导入 Step1 模块，请确保 PYTHONPATH 包含 step1_logbook_postgres/scripts: {e}",
            {"import_error": str(e)},
        )

    # 解析 URI
    parsed = parse_uri(uri)

    # 根据 URI 类型分派处理
    if parsed.uri_type == UriType.MEMORY:
        # memory:// URI - 需要查询数据库或读取本地文件
        return _read_memory_uri(
            uri=uri,
            parsed=parsed,
            conn=conn,
            artifacts_root=artifacts_root,
            config=config,
            verify_sha256=verify_sha256,
            expected_sha256=expected_sha256,
            encoding=encoding,
            docs_root=docs_root,
        )
    elif parsed.uri_type == UriType.ARTIFACT:
        # artifact key 或 artifact:// URI
        return _read_artifact_uri(
            uri=uri,
            parsed=parsed,
            artifacts_root=artifacts_root,
            config=config,
            verify_sha256=verify_sha256,
            expected_sha256=expected_sha256,
            encoding=encoding,
        )
    elif parsed.uri_type == UriType.FILE:
        # file:// URI
        return _read_file_uri(
            uri=uri,
            parsed=parsed,
            verify_sha256=verify_sha256,
            expected_sha256=expected_sha256,
            encoding=encoding,
        )
    else:
        raise EvidenceUriInvalidError(
            f"不支持的 URI 类型: {parsed.uri_type.value}",
            {"uri": uri, "uri_type": parsed.uri_type.value},
        )


def _read_artifact_uri(
    uri: str,
    parsed,
    artifacts_root: Optional[Union[str, Path]],
    config,
    verify_sha256: bool,
    expected_sha256: Optional[str],
    encoding: str,
) -> EvidenceReadResult:
    """
    读取 artifact key / artifact:// URI
    """
    from engram_step1.uri import resolve_to_local_path
    from engram_step1.config import get_effective_artifacts_root

    # 获取 artifacts_root
    if artifacts_root is None:
        try:
            artifacts_root = get_effective_artifacts_root()
        except Exception:
            artifacts_root = "./.agentx/artifacts"

    # 解析到本地路径
    local_path = resolve_to_local_path(uri, artifacts_root)

    if local_path is None:
        # 尝试直接组合路径
        root = Path(artifacts_root)
        full_path = root / parsed.path
        if full_path.exists():
            local_path = str(full_path.resolve())
        else:
            raise EvidenceNotFoundError(
                f"Artifact 文件不存在: {uri}",
                {
                    "uri": uri,
                    "tried_path": str(full_path),
                    "artifacts_root": str(artifacts_root),
                },
            )

    # 读取文件内容
    path = Path(local_path)
    try:
        content = path.read_bytes()
    except OSError as e:
        raise EvidenceNotFoundError(
            f"读取 artifact 文件失败: {local_path}",
            {"uri": uri, "path": local_path, "error": str(e)},
        )

    # 计算 SHA256
    actual_sha256 = _compute_sha256(content)

    # 校验 SHA256
    if verify_sha256 and expected_sha256:
        if actual_sha256.lower() != expected_sha256.lower():
            raise EvidenceSha256MismatchError(
                f"SHA256 校验失败",
                {
                    "uri": uri,
                    "expected": expected_sha256,
                    "actual": actual_sha256,
                },
            )

    # 解码为文本
    try:
        text = content.decode(encoding)
    except UnicodeDecodeError as e:
        raise EvidenceReadError(
            f"文本解码失败 (encoding={encoding}): {uri}",
            {"uri": uri, "encoding": encoding, "error": str(e)},
        )

    return EvidenceReadResult(
        text=text,
        sha256=actual_sha256,
        size_bytes=len(content),
        uri=uri,
        artifact_uri=None,
        source_type=None,
        source_id=None,
    )


def _read_file_uri(
    uri: str,
    parsed,
    verify_sha256: bool,
    expected_sha256: Optional[str],
    encoding: str,
) -> EvidenceReadResult:
    """
    读取 file:// URI
    """
    path = Path(parsed.path)

    if not path.exists():
        raise EvidenceNotFoundError(
            f"文件不存在: {uri}",
            {"uri": uri, "path": str(path)},
        )

    try:
        content = path.read_bytes()
    except OSError as e:
        raise EvidenceNotFoundError(
            f"读取文件失败: {uri}",
            {"uri": uri, "path": str(path), "error": str(e)},
        )

    # 计算 SHA256
    actual_sha256 = _compute_sha256(content)

    # 校验 SHA256
    if verify_sha256 and expected_sha256:
        if actual_sha256.lower() != expected_sha256.lower():
            raise EvidenceSha256MismatchError(
                f"SHA256 校验失败",
                {
                    "uri": uri,
                    "expected": expected_sha256,
                    "actual": actual_sha256,
                },
            )

    # 解码为文本
    try:
        text = content.decode(encoding)
    except UnicodeDecodeError as e:
        raise EvidenceReadError(
            f"文本解码失败 (encoding={encoding}): {uri}",
            {"uri": uri, "encoding": encoding, "error": str(e)},
        )

    return EvidenceReadResult(
        text=text,
        sha256=actual_sha256,
        size_bytes=len(content),
        uri=uri,
        artifact_uri=None,
        source_type=None,
        source_id=None,
    )


def _read_memory_uri(
    uri: str,
    parsed,
    conn,
    artifacts_root: Optional[Union[str, Path]],
    config,
    verify_sha256: bool,
    expected_sha256: Optional[str],
    encoding: str,
    docs_root: Optional[Union[str, Path]] = None,
) -> EvidenceReadResult:
    """
    读取 memory://patch_blobs/..., memory://attachments/... 或 memory://docs/... URI

    流程:
    1. 解析 URI 判断资源类型 (patch_blobs, attachments 或 docs)
    2. 查询对应的 Step1 表获取 artifact_uri
       - patch_blobs -> scm.patch_blobs 表
       - attachments -> logbook.attachments 表
       - docs -> 本地文件系统
    3. 读取 artifact_uri 指向的制品
    4. 可选校验 SHA256
    """
    from engram_step1.uri import parse_evidence_uri, parse_attachment_evidence_uri
    from engram_step1.config import get_config as step1_get_config, get_effective_artifacts_root
    from engram_step1.db import get_connection as step1_get_connection

    # 解析 path 组件
    path_parts = parsed.path.strip("/").split("/")
    if len(path_parts) < 2:
        raise EvidenceUriInvalidError(
            f"memory:// URI 路径格式无效: {uri}",
            {"uri": uri, "path": parsed.path},
        )

    resource_type = path_parts[0]

    # 支持 patch_blobs, attachments 和 docs 三种资源类型
    if resource_type not in ("patch_blobs", "attachments", "docs"):
        raise EvidenceUriInvalidError(
            f"不支持的 memory:// 资源类型: {resource_type}，支持: patch_blobs, attachments, docs",
            {"uri": uri, "resource_type": resource_type},
        )

    # 分派到不同的处理函数
    if resource_type == "attachments":
        return _read_attachment_memory_uri(
            uri=uri,
            parsed=parsed,
            path_parts=path_parts,
            conn=conn,
            artifacts_root=artifacts_root,
            config=config,
            verify_sha256=verify_sha256,
            expected_sha256=expected_sha256,
            encoding=encoding,
        )

    if resource_type == "docs":
        return _read_docs_memory_uri(
            uri=uri,
            parsed=parsed,
            path_parts=path_parts,
            docs_root=docs_root,
            verify_sha256=verify_sha256,
            expected_sha256=expected_sha256,
            encoding=encoding,
        )

    # 解析 patch_blobs 路径
    # 格式: patch_blobs/{source_type}/{source_id}/{sha256}
    #   或: patch_blobs/sha256/{sha256_value}
    #   或: patch_blobs/blob_id/{blob_id}
    blob_path_parts = path_parts[1:]

    if len(blob_path_parts) < 2:
        raise EvidenceUriInvalidError(
            f"patch_blobs 路径格式无效: {uri}",
            {"uri": uri, "path_parts": blob_path_parts},
        )

    # 获取配置
    if config is None:
        config = step1_get_config()

    # 获取 artifacts_root
    if artifacts_root is None:
        try:
            artifacts_root = get_effective_artifacts_root()
        except Exception:
            artifacts_root = "./.agentx/artifacts"

    # 管理数据库连接
    should_close_conn = False
    if conn is None:
        conn = step1_get_connection(config=config)
        should_close_conn = True

    try:
        # 查询 patch_blob 记录
        blob_info = _query_patch_blob(blob_path_parts, uri, conn)

        if blob_info is None:
            raise EvidenceNotFoundError(
                f"patch_blob 未找到: {uri}",
                {"uri": uri},
            )

        db_source_type = blob_info["source_type"]
        db_source_id = blob_info["source_id"]
        db_sha256 = blob_info["sha256"]
        db_artifact_uri = blob_info["artifact_uri"]
        db_size_bytes = blob_info.get("size_bytes")

        # 读取 artifact 内容
        content = _read_artifact_content(db_artifact_uri, artifacts_root)

        # 计算实际 SHA256
        actual_sha256 = _compute_sha256(content)

        # 确定要校验的预期 SHA256
        # 优先使用传入的 expected_sha256，否则使用 DB 记录的值
        sha256_to_verify = expected_sha256 or db_sha256

        # 校验 SHA256
        if verify_sha256:
            if actual_sha256.lower() != sha256_to_verify.lower():
                raise EvidenceSha256MismatchError(
                    f"SHA256 校验失败",
                    {
                        "uri": uri,
                        "expected": sha256_to_verify,
                        "actual": actual_sha256,
                        "artifact_uri": db_artifact_uri,
                    },
                )

        # 解码为文本
        try:
            text = content.decode(encoding)
        except UnicodeDecodeError as e:
            raise EvidenceReadError(
                f"文本解码失败 (encoding={encoding}): {uri}",
                {"uri": uri, "encoding": encoding, "error": str(e)},
            )

        return EvidenceReadResult(
            text=text,
            sha256=actual_sha256,
            size_bytes=db_size_bytes or len(content),
            uri=uri,
            artifact_uri=db_artifact_uri,
            source_type=db_source_type,
            source_id=db_source_id,
        )

    finally:
        if should_close_conn:
            conn.close()


def _read_attachment_memory_uri(
    uri: str,
    parsed,
    path_parts: list,
    conn,
    artifacts_root: Optional[Union[str, Path]],
    config,
    verify_sha256: bool,
    expected_sha256: Optional[str],
    encoding: str,
) -> EvidenceReadResult:
    """
    读取 memory://attachments/<attachment_id>/<sha256> URI

    流程:
    1. 解析 URI 获取 (attachment_id, sha256)
    2. 查询 logbook.attachments 表获取 uri（指向制品）
    3. 读取 uri 指向的制品
    4. 可选校验 SHA256
    """
    from engram_step1.config import get_config as step1_get_config, get_effective_artifacts_root
    from engram_step1.db import get_connection as step1_get_connection

    # 获取配置
    if config is None:
        config = step1_get_config()

    # 获取 artifacts_root
    if artifacts_root is None:
        try:
            artifacts_root = get_effective_artifacts_root()
        except Exception:
            artifacts_root = "./.agentx/artifacts"

    # 管理数据库连接
    should_close_conn = False
    if conn is None:
        conn = step1_get_connection(config=config)
        should_close_conn = True

    try:
        # 查询 attachment 记录
        attachment_info = _query_attachment(path_parts, uri, conn)

        if attachment_info is None:
            raise EvidenceNotFoundError(
                f"attachment 未找到: {uri}",
                {"uri": uri},
            )

        db_attachment_id = attachment_info["attachment_id"]
        db_sha256 = attachment_info["sha256"]
        db_artifact_uri = attachment_info["artifact_uri"]
        db_kind = attachment_info.get("kind")
        db_item_id = attachment_info.get("item_id")
        db_size_bytes = attachment_info.get("size_bytes")

        # 读取 artifact 内容
        content = _read_artifact_content(db_artifact_uri, artifacts_root)

        # 计算实际 SHA256
        actual_sha256 = _compute_sha256(content)

        # 确定要校验的预期 SHA256
        sha256_to_verify = expected_sha256 or db_sha256

        # 校验 SHA256
        if verify_sha256 and sha256_to_verify:
            if actual_sha256.lower() != sha256_to_verify.lower():
                raise EvidenceSha256MismatchError(
                    f"SHA256 校验失败",
                    {
                        "uri": uri,
                        "expected": sha256_to_verify,
                        "actual": actual_sha256,
                        "artifact_uri": db_artifact_uri,
                    },
                )

        # 解码为文本
        try:
            text = content.decode(encoding)
        except UnicodeDecodeError as e:
            raise EvidenceReadError(
                f"文本解码失败 (encoding={encoding}): {uri}",
                {"uri": uri, "encoding": encoding, "error": str(e)},
            )

        return EvidenceReadResult(
            text=text,
            sha256=actual_sha256,
            size_bytes=db_size_bytes or len(content),
            uri=uri,
            artifact_uri=db_artifact_uri,
            source_type=db_kind,  # 使用 kind 作为 source_type
            source_id=f"attachment:{db_attachment_id}" if db_item_id is None else f"item:{db_item_id}",
        )

    finally:
        if should_close_conn:
            conn.close()


def _query_attachment(path_parts: list, original_uri: str, conn) -> Optional[dict]:
    """
    查询 logbook.attachments 记录

    支持的路径格式:
    - attachments/<attachment_id>/<sha256> - Canonical 格式
    - attachments/<attachment_id> - 简化格式（无 sha256 校验）
    - attachments/sha256/<sha256_value> - 按 SHA256 查找
    """
    import re

    # SHA256 格式正则：64 位十六进制
    _SHA256_PATTERN = re.compile(r"^[a-fA-F0-9]{64}$")

    def _is_sha256_hex(value: str) -> bool:
        return bool(_SHA256_PATTERN.match(value))

    # path_parts[0] 应该是 "attachments"
    if len(path_parts) < 2:
        raise EvidenceUriInvalidError(
            f"attachments 路径格式无效: {original_uri}",
            {"uri": original_uri, "path_parts": path_parts},
        )

    # 获取 attachments 后面的部分
    att_path_parts = path_parts[1:]
    lookup_type = att_path_parts[0]

    with conn.cursor() as cur:
        if lookup_type == "sha256" and len(att_path_parts) >= 2:
            # memory://attachments/sha256/{sha256_value}
            sha256_value = att_path_parts[1]
            cur.execute(
                """
                SELECT attachment_id, item_id, kind, sha256, uri, size_bytes
                FROM logbook.attachments
                WHERE sha256 = %s
                LIMIT 1
                """,
                (sha256_value,),
            )
            row = cur.fetchone()

        elif _is_sha256_hex(lookup_type) and len(att_path_parts) == 1:
            # memory://attachments/<sha256> - 直接用 sha256 查询
            sha256_value = lookup_type
            cur.execute(
                """
                SELECT attachment_id, item_id, kind, sha256, uri, size_bytes
                FROM logbook.attachments
                WHERE sha256 = %s
                LIMIT 1
                """,
                (sha256_value,),
            )
            row = cur.fetchone()

        else:
            # 尝试解析 attachment_id
            try:
                attachment_id = int(lookup_type)
            except ValueError:
                raise EvidenceUriInvalidError(
                    f"无效的 attachment_id: {lookup_type}",
                    {"uri": original_uri, "lookup_type": lookup_type},
                )

            # Canonical 格式: attachments/<attachment_id>/<sha256>
            uri_sha256 = att_path_parts[1] if len(att_path_parts) >= 2 else None

            cur.execute(
                """
                SELECT attachment_id, item_id, kind, sha256, uri, size_bytes
                FROM logbook.attachments
                WHERE attachment_id = %s
                """,
                (attachment_id,),
            )
            row = cur.fetchone()

            if row is not None and uri_sha256:
                # 校验 sha256 一致性
                _, _, _, db_sha256, _, _ = row
                if db_sha256 and db_sha256.lower() != uri_sha256.lower():
                    raise EvidenceSha256MismatchError(
                        f"Attachment URI sha256 校验失败: URI 中的 sha256 与数据库记录不一致",
                        {
                            "uri": original_uri,
                            "uri_sha256": uri_sha256,
                            "db_sha256": db_sha256,
                            "attachment_id": attachment_id,
                        },
                    )

        if row is None:
            return None

        attachment_id, item_id, kind, sha256_val, artifact_uri, size_bytes = row
        return {
            "attachment_id": attachment_id,
            "item_id": item_id,
            "kind": kind,
            "sha256": sha256_val,
            "artifact_uri": artifact_uri,
            "size_bytes": size_bytes,
        }


def _query_patch_blob(path_parts: list, original_uri: str, conn) -> Optional[dict]:
    """
    查询 patch_blob 记录

    支持的路径格式:
    - sha256/{sha256_value}  - 按 SHA256 查找
    - blob_id/{blob_id}      - 按 blob_id 查找
    - {source_type}/{source_id}/{sha256} - Canonical 格式
    - {source_type}/{source_id} - 旧格式
    """
    import re

    # SHA256 格式正则：64 位十六进制
    _SHA256_PATTERN = re.compile(r"^[a-fA-F0-9]{64}$")

    def _is_sha256_hex(value: str) -> bool:
        return bool(_SHA256_PATTERN.match(value))

    lookup_type = path_parts[0]

    # 检测 Canonical URI 格式: {source_type}/{source_id}/{sha256}
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
                FROM scm.patch_blobs
                WHERE sha256 = %s
                LIMIT 1
                """,
                (sha256_value,),
            )

        elif lookup_type == "blob_id":
            # memory://patch_blobs/blob_id/{blob_id}
            blob_id = int(path_parts[1])
            cur.execute(
                """
                SELECT blob_id, source_type, source_id, sha256, uri, size_bytes
                FROM scm.patch_blobs
                WHERE blob_id = %s
                """,
                (blob_id,),
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
                FROM scm.patch_blobs
                WHERE sha256 = %s
                LIMIT 1
                """,
                (uri_sha256,),
            )
            row = cur.fetchone()

            if row is not None:
                # 校验 source_type/source_id 一致性
                _, db_source_type, db_source_id, _, _, _ = row
                if db_source_type != uri_source_type or db_source_id != uri_source_id:
                    raise EvidenceSha256MismatchError(
                        f"Canonical URI source 校验失败: URI 中的 source_type/source_id 与数据库记录不一致",
                        {
                            "uri": original_uri,
                            "uri_source_type": uri_source_type,
                            "uri_source_id": uri_source_id,
                            "db_source_type": db_source_type,
                            "db_source_id": db_source_id,
                            "sha256": uri_sha256,
                        },
                    )
                # 返回结果
                blob_id, source_type, source_id, sha256_val, artifact_uri, size_bytes = row
                return {
                    "blob_id": blob_id,
                    "source_type": source_type,
                    "source_id": source_id,
                    "sha256": sha256_val,
                    "artifact_uri": artifact_uri,
                    "size_bytes": size_bytes,
                }

            # 按 source_type+source_id 查询
            cur.execute(
                """
                SELECT blob_id, source_type, source_id, sha256, uri, size_bytes
                FROM scm.patch_blobs
                WHERE source_type = %s AND source_id = %s
                """,
                (uri_source_type, uri_source_id),
            )
            row = cur.fetchone()

            if row is not None:
                # 校验 sha256 一致性
                _, _, _, db_sha256, _, _ = row
                if db_sha256.lower() != uri_sha256:
                    raise EvidenceSha256MismatchError(
                        f"Canonical URI sha256 校验失败: URI 中的 sha256 与数据库记录不一致",
                        {
                            "uri": original_uri,
                            "uri_sha256": uri_sha256,
                            "db_sha256": db_sha256,
                            "source_type": uri_source_type,
                            "source_id": uri_source_id,
                        },
                    )
                blob_id, source_type, source_id, sha256_val, artifact_uri, size_bytes = row
                return {
                    "blob_id": blob_id,
                    "source_type": source_type,
                    "source_id": source_id,
                    "sha256": sha256_val,
                    "artifact_uri": artifact_uri,
                    "size_bytes": size_bytes,
                }

            return None

        else:
            # 旧格式: {source_type}/{source_id}
            source_type = lookup_type
            source_id = "/".join(path_parts[1:])
            cur.execute(
                """
                SELECT blob_id, source_type, source_id, sha256, uri, size_bytes
                FROM scm.patch_blobs
                WHERE source_type = %s AND source_id = %s
                """,
                (source_type, source_id),
            )

        # 获取结果（对于非 canonical URI）
        row = cur.fetchone()

        if row is None:
            return None

        blob_id, source_type, source_id, sha256_val, artifact_uri, size_bytes = row
        return {
            "blob_id": blob_id,
            "source_type": source_type,
            "source_id": source_id,
            "sha256": sha256_val,
            "artifact_uri": artifact_uri,
            "size_bytes": size_bytes,
        }


def _read_docs_memory_uri(
    uri: str,
    parsed,
    path_parts: list,
    docs_root: Optional[Union[str, Path]],
    verify_sha256: bool,
    expected_sha256: Optional[str],
    encoding: str,
) -> EvidenceReadResult:
    """
    读取 memory://docs/<rel_path>/<sha256> URI

    流程:
    1. 解析 URI 获取 (rel_path, sha256)
    2. 根据 docs_root 定位本地文件
    3. 读取文件内容
    4. 可选校验 SHA256

    Args:
        uri: 原始 URI
        parsed: 解析后的 URI 对象
        path_parts: URI 路径分段（第一个元素是 "docs"）
        docs_root: 文档根目录（默认为当前工作目录）
        verify_sha256: 是否校验 SHA256
        expected_sha256: 预期的 SHA256 值
        encoding: 文本编码

    Returns:
        EvidenceReadResult 对象
    """
    import os
    import re

    # SHA256 格式正则：64 位十六进制
    _SHA256_PATTERN = re.compile(r"^[a-fA-F0-9]{64}$")

    def _is_sha256_hex(value: str) -> bool:
        return bool(_SHA256_PATTERN.match(value))

    # path_parts[0] 是 "docs"
    if len(path_parts) < 2:
        raise EvidenceUriInvalidError(
            f"docs URI 路径格式无效: {uri}",
            {"uri": uri, "path_parts": path_parts},
        )

    # 解析路径：docs/<rel_path>/<sha256>
    # 最后一个元素如果是 sha256，则分离出来
    docs_path_parts = path_parts[1:]
    uri_sha256 = None

    if docs_path_parts and _is_sha256_hex(docs_path_parts[-1]):
        uri_sha256 = docs_path_parts[-1].lower()
        docs_path_parts = docs_path_parts[:-1]

    if not docs_path_parts:
        raise EvidenceUriInvalidError(
            f"docs URI 缺少相对路径: {uri}",
            {"uri": uri},
        )

    # 重建相对路径
    rel_path = "/".join(docs_path_parts)

    # 确定 docs_root
    if docs_root is None:
        # 默认使用当前工作目录
        docs_root = Path.cwd()
    else:
        docs_root = Path(docs_root)

    # 构建完整路径
    full_path = docs_root / rel_path

    # 安全检查：防止路径遍历
    try:
        full_path = full_path.resolve()
        docs_root_resolved = docs_root.resolve()
        # 确保路径在 docs_root 内或其子目录
        # 注意：允许 docs_root 之外的路径（如仓库根目录下的任意文件）
        # 这里只做基本的存在性检查
    except Exception as e:
        raise EvidenceUriInvalidError(
            f"无法解析文档路径: {uri}",
            {"uri": uri, "rel_path": rel_path, "error": str(e)},
        )

    # 检查文件存在性
    if not full_path.exists():
        raise EvidenceNotFoundError(
            f"文档文件不存在: {uri}",
            {"uri": uri, "path": str(full_path), "rel_path": rel_path},
        )

    if not full_path.is_file():
        raise EvidenceNotFoundError(
            f"路径不是文件: {uri}",
            {"uri": uri, "path": str(full_path)},
        )

    # 读取文件内容
    try:
        content = full_path.read_bytes()
    except OSError as e:
        raise EvidenceNotFoundError(
            f"读取文档文件失败: {uri}",
            {"uri": uri, "path": str(full_path), "error": str(e)},
        )

    # 计算 SHA256
    actual_sha256 = _compute_sha256(content)

    # 确定要校验的预期 SHA256
    sha256_to_verify = expected_sha256 or uri_sha256

    # 校验 SHA256
    if verify_sha256 and sha256_to_verify:
        if actual_sha256.lower() != sha256_to_verify.lower():
            raise EvidenceSha256MismatchError(
                f"文档 SHA256 校验失败",
                {
                    "uri": uri,
                    "expected": sha256_to_verify,
                    "actual": actual_sha256,
                    "path": str(full_path),
                },
            )

    # 解码为文本
    try:
        text = content.decode(encoding)
    except UnicodeDecodeError as e:
        raise EvidenceReadError(
            f"文本解码失败 (encoding={encoding}): {uri}",
            {"uri": uri, "encoding": encoding, "error": str(e)},
        )

    # 构建 source_id：使用冒号分隔的格式
    # 格式: <docs_root_name>:<rel_path>
    docs_root_name = docs_root.name if docs_root.name else "docs"
    source_id = f"{docs_root_name}:{rel_path}"

    return EvidenceReadResult(
        text=text,
        sha256=actual_sha256,
        size_bytes=len(content),
        uri=uri,
        artifact_uri=uri,  # 对于 docs，artifact_uri 就是 URI 本身
        source_type="docs",
        source_id=source_id,
    )


def _read_artifact_content(
    artifact_uri: str,
    artifacts_root: Union[str, Path],
) -> bytes:
    """
    读取 artifact 内容

    Args:
        artifact_uri: artifact URI（可能是 artifact key 或 file:// URI）
        artifacts_root: 制品根目录

    Returns:
        文件内容

    Raises:
        EvidenceNotFoundError: 文件不存在
    """
    from engram_step1.uri import resolve_to_local_path, parse_uri, UriType

    # 解析 URI 类型
    parsed = parse_uri(artifact_uri)

    # 尝试解析为本地路径
    local_path = resolve_to_local_path(artifact_uri, artifacts_root)

    if local_path is None:
        if parsed.uri_type == UriType.ARTIFACT:
            # artifact key: 直接组合路径
            root = Path(artifacts_root)
            full_path = root / parsed.path
            if full_path.exists():
                local_path = str(full_path)
            else:
                raise EvidenceNotFoundError(
                    f"artifact 文件不存在: {artifact_uri}",
                    {"artifact_uri": artifact_uri, "tried_path": str(full_path)},
                )
        else:
            raise EvidenceNotFoundError(
                f"无法解析 artifact URI: {artifact_uri}",
                {"artifact_uri": artifact_uri},
            )

    return Path(local_path).read_bytes()


# ============================================================
# 便捷函数
# ============================================================


def read_evidence_text_simple(
    uri: str,
    verify_sha256: bool = False,
    encoding: str = "utf-8",
) -> str:
    """
    简化版 read_evidence_text，直接返回文本内容

    使用默认配置，自动管理数据库连接。

    Args:
        uri: Evidence URI
        verify_sha256: 是否校验 SHA256
        encoding: 文本编码

    Returns:
        文本内容

    示例:
        text = read_evidence_text_simple("scm/proj_a/1/svn/r100/abc123.diff")
    """
    result = read_evidence_text(uri, verify_sha256=verify_sha256, encoding=encoding)
    return result.text


def verify_evidence_sha256(
    uri: str,
    expected_sha256: str,
    conn=None,
    artifacts_root: Optional[Union[str, Path]] = None,
    config=None,
) -> bool:
    """
    验证 Evidence 的 SHA256 是否匹配

    Args:
        uri: Evidence URI
        expected_sha256: 预期的 SHA256 值
        conn: 数据库连接
        artifacts_root: 制品根目录
        config: 配置实例

    Returns:
        True 如果匹配，False 否则
    """
    try:
        result = read_evidence_text(
            uri,
            conn=conn,
            artifacts_root=artifacts_root,
            config=config,
            verify_sha256=False,  # 不自动校验，我们手动比较
        )
        return result.sha256.lower() == expected_sha256.lower()
    except EvidenceReadError:
        return False


def get_evidence_info(
    uri: str,
    conn=None,
    config=None,
) -> Optional[dict]:
    """
    获取 Evidence 的元数据（不读取完整内容）

    Args:
        uri: Evidence URI
        conn: 数据库连接
        config: 配置实例

    Returns:
        元数据字典，包含:
        - uri: 原始 URI
        - artifact_uri: 底层 artifact URI
        - source_type: 源类型
        - source_id: 源标识
        - sha256: SHA256 值
        - size_bytes: 大小
    """
    try:
        from engram_step1.uri import parse_uri, UriType
        from engram_step1.config import get_config as step1_get_config
        from engram_step1.db import get_connection as step1_get_connection
    except ImportError:
        return None

    parsed = parse_uri(uri)

    if parsed.uri_type != UriType.MEMORY:
        # 非 memory:// URI，返回基本信息
        return {
            "uri": uri,
            "artifact_uri": uri if parsed.uri_type == UriType.ARTIFACT else None,
            "source_type": None,
            "source_id": None,
            "sha256": None,
            "size_bytes": None,
        }

    # memory:// URI 需要查询数据库或文件系统
    path_parts = parsed.path.strip("/").split("/")
    if len(path_parts) < 2:
        return None

    resource_type = path_parts[0]
    if resource_type not in ("patch_blobs", "attachments", "docs"):
        return None

    # docs 资源类型不需要数据库查询
    if resource_type == "docs":
        return _get_docs_evidence_info(uri, path_parts)

    if config is None:
        config = step1_get_config()

    should_close_conn = False
    if conn is None:
        conn = step1_get_connection(config=config)
        should_close_conn = True

    try:
        if resource_type == "patch_blobs":
            blob_info = _query_patch_blob(path_parts[1:], uri, conn)
            if blob_info is None:
                return None

            return {
                "uri": uri,
                "artifact_uri": blob_info["artifact_uri"],
                "source_type": blob_info["source_type"],
                "source_id": blob_info["source_id"],
                "sha256": blob_info["sha256"],
                "size_bytes": blob_info.get("size_bytes"),
                "blob_id": blob_info.get("blob_id"),
            }
        else:
            # resource_type == "attachments"
            att_info = _query_attachment(path_parts, uri, conn)
            if att_info is None:
                return None

            return {
                "uri": uri,
                "artifact_uri": att_info["artifact_uri"],
                "source_type": att_info.get("kind"),
                "source_id": f"attachment:{att_info['attachment_id']}",
                "sha256": att_info["sha256"],
                "size_bytes": att_info.get("size_bytes"),
                "attachment_id": att_info.get("attachment_id"),
                "item_id": att_info.get("item_id"),
                "kind": att_info.get("kind"),
            }
    except EvidenceReadError:
        return None
    finally:
        if should_close_conn:
            conn.close()


def _get_docs_evidence_info(uri: str, path_parts: list) -> Optional[dict]:
    """
    获取 docs 资源类型的元数据

    Args:
        uri: 原始 URI
        path_parts: URI 路径分段

    Returns:
        元数据字典
    """
    import re

    # SHA256 格式正则：64 位十六进制
    _SHA256_PATTERN = re.compile(r"^[a-fA-F0-9]{64}$")

    def _is_sha256_hex(value: str) -> bool:
        return bool(_SHA256_PATTERN.match(value))

    # 解析路径：docs/<rel_path>/<sha256>
    docs_path_parts = path_parts[1:]
    uri_sha256 = None

    if docs_path_parts and _is_sha256_hex(docs_path_parts[-1]):
        uri_sha256 = docs_path_parts[-1].lower()
        docs_path_parts = docs_path_parts[:-1]

    if not docs_path_parts:
        return None

    rel_path = "/".join(docs_path_parts)

    return {
        "uri": uri,
        "artifact_uri": uri,
        "source_type": "docs",
        "source_id": f"docs:{rel_path}",
        "sha256": uri_sha256,
        "size_bytes": None,  # 需要读取文件才能获取
        "rel_path": rel_path,
    }


# ============================================================
# 本地文档索引辅助函数
# ============================================================


def build_docs_evidence_uri(rel_path: str, sha256: str) -> str:
    """
    构建 docs 的 canonical evidence URI

    格式: memory://docs/<rel_path>/<sha256>

    Args:
        rel_path: 相对路径（如 "contracts/evidence_packet.md"）
        sha256: 内容 SHA256 哈希

    Returns:
        Canonical evidence URI
    """
    sha256_norm = sha256.strip().lower() if sha256 else ""
    # 规范化路径分隔符
    rel_path_norm = rel_path.replace("\\", "/").strip("/")
    return f"memory://docs/{rel_path_norm}/{sha256_norm}"


def scan_docs_directory(
    docs_root: Union[str, Path],
    patterns: Optional[list] = None,
    exclude_patterns: Optional[list] = None,
) -> list:
    """
    扫描文档目录，返回待索引的文档列表

    Args:
        docs_root: 文档根目录
        patterns: 包含的文件模式列表（默认 ["*.md", "*.txt"]）
        exclude_patterns: 排除的文件模式列表

    Returns:
        文档信息列表，每个元素包含:
        - rel_path: 相对路径
        - full_path: 完整路径
        - sha256: 文件 SHA256
        - size_bytes: 文件大小
        - artifact_uri: canonical evidence URI
        - source_id: 源标识
        - source_type: 源类型 (固定为 "docs")
    """
    import fnmatch

    docs_root = Path(docs_root).resolve()
    if not docs_root.exists():
        return []

    if patterns is None:
        patterns = ["*.md", "*.txt"]

    if exclude_patterns is None:
        exclude_patterns = []

    results = []

    for pattern in patterns:
        for file_path in docs_root.rglob(pattern):
            if not file_path.is_file():
                continue

            rel_path = str(file_path.relative_to(docs_root))
            # 规范化路径分隔符
            rel_path = rel_path.replace("\\", "/")

            # 检查排除模式
            should_exclude = False
            for exc_pattern in exclude_patterns:
                if fnmatch.fnmatch(rel_path, exc_pattern):
                    should_exclude = True
                    break
            if should_exclude:
                continue

            # 读取文件计算 SHA256
            try:
                content = file_path.read_bytes()
                sha256 = _compute_sha256(content)
                size_bytes = len(content)
            except OSError:
                continue

            # 构建 source_id
            source_id = f"docs:{rel_path}"

            # 构建 artifact_uri
            artifact_uri = build_docs_evidence_uri(rel_path, sha256)

            results.append({
                "rel_path": rel_path,
                "full_path": str(file_path),
                "sha256": sha256,
                "size_bytes": size_bytes,
                "artifact_uri": artifact_uri,
                "source_id": source_id,
                "source_type": "docs",
            })

    return results
