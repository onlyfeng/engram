"""
evidence_store - Evidence 上传存储模块

提供内联证据内容的上传通路：
- 将 bytes/text 内容写入 Logbook artifacts store
- 插入 logbook.attachments 表
- 返回 evidence(v2) object 供 memory_store 引用

大小限制策略:
- 默认阈值 1MB (EVIDENCE_MAX_SIZE_BYTES)
- 超限时返回可诊断错误，建议使用外部存储

环境变量:
    EVIDENCE_MAX_SIZE_BYTES   最大允许内容大小（字节），默认 1048576 (1MB)
    EVIDENCE_ARTIFACTS_PREFIX 制品 URI 前缀，默认 "attachments/evidence"
"""

import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Union

from pydantic import BaseModel, Field

# 导入 Logbook artifact_store
_LOGBOOK_PKG_NAME = "engram_logbook"
try:
    from engram.logbook.artifact_store import (
        ArtifactError,
        ArtifactSizeLimitExceededError,
        get_artifact_store,
    )
    from engram.logbook.db import attach as db_attach
    from engram.logbook.uri import build_attachment_evidence_uri
except ImportError as e:
    raise ImportError(
        f'evidence_store 需要 engram_logbook 模块: {e}\n请先安装:\n  pip install -e ".[full]"'
    )

logger = logging.getLogger("gateway.evidence_store")

# ===================== 常量 =====================

# 默认最大内容大小（1MB）
DEFAULT_MAX_SIZE_BYTES = 1 * 1024 * 1024

# 环境变量名
ENV_MAX_SIZE_BYTES = "EVIDENCE_MAX_SIZE_BYTES"
ENV_ARTIFACTS_PREFIX = "EVIDENCE_ARTIFACTS_PREFIX"

# 默认制品 URI 前缀
DEFAULT_ARTIFACTS_PREFIX = "attachments/evidence"

# 允许的内容类型
ALLOWED_CONTENT_TYPES = {
    "text/plain",
    "text/markdown",
    "text/x-diff",
    "text/x-patch",
    "application/json",
    "application/xml",
    "text/xml",
    "text/html",
    "text/csv",
    "text/yaml",
    "application/x-yaml",
}


# ===================== 错误定义 =====================


class EvidenceUploadError(Exception):
    """证据上传错误基类"""

    def __init__(
        self,
        message: str,
        error_code: str,
        retryable: bool = False,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.retryable = retryable
        self.details = details or {}


class EvidenceSizeLimitExceededError(EvidenceUploadError):
    """内容大小超出限制"""

    def __init__(self, size_bytes: int, max_bytes: int):
        super().__init__(
            message=f"内容大小 {size_bytes} 字节超出限制 {max_bytes} 字节",
            error_code="EVIDENCE_SIZE_LIMIT_EXCEEDED",
            retryable=False,
            details={
                "size_bytes": size_bytes,
                "max_bytes": max_bytes,
                "suggestion": "请使用外部存储（如 S3/MinIO）上传大文件，然后通过 URI 引用",
            },
        )


class EvidenceContentTypeError(EvidenceUploadError):
    """内容类型不允许"""

    def __init__(self, content_type: str, allowed_types: set):
        super().__init__(
            message=f"不支持的内容类型: {content_type}",
            error_code="EVIDENCE_CONTENT_TYPE_NOT_ALLOWED",
            retryable=False,
            details={
                "content_type": content_type,
                "allowed_types": list(allowed_types),
            },
        )


class EvidenceWriteError(EvidenceUploadError):
    """写入存储失败"""

    def __init__(self, message: str, original_error: Optional[Exception] = None):
        details = {}
        if original_error:
            details["original_error"] = str(original_error)
        super().__init__(
            message=f"写入存储失败: {message}",
            error_code="EVIDENCE_WRITE_FAILED",
            retryable=True,  # 存储写入失败通常可重试
            details=details,
        )


class EvidenceItemRequiredError(EvidenceUploadError):
    """未提供 item_id，无法创建 attachment 记录"""

    def __init__(self):
        super().__init__(
            message="上传证据需要提供 item_id，请先调用 create_item 创建条目后再上传证据",
            error_code="EVIDENCE_ITEM_REQUIRED",
            retryable=False,
            details={
                "suggestion": "先调用 Logbook 的 create_item 接口创建 item，"
                "然后使用返回的 item_id 调用 evidence_upload",
            },
        )


# ===================== 配置读取 =====================


def get_max_size_bytes() -> int:
    """
    获取最大允许内容大小

    优先级:
    1. 环境变量 EVIDENCE_MAX_SIZE_BYTES
    2. 默认值 DEFAULT_MAX_SIZE_BYTES (1MB)
    """
    env_value = os.environ.get(ENV_MAX_SIZE_BYTES)
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            logger.warning(f"无效的 {ENV_MAX_SIZE_BYTES} 值: {env_value}，使用默认值")
    return DEFAULT_MAX_SIZE_BYTES


def get_artifacts_prefix() -> str:
    """
    获取制品 URI 前缀

    优先级:
    1. 环境变量 EVIDENCE_ARTIFACTS_PREFIX
    2. 默认值 DEFAULT_ARTIFACTS_PREFIX
    """
    return os.environ.get(ENV_ARTIFACTS_PREFIX, DEFAULT_ARTIFACTS_PREFIX)


# ===================== 数据模型 =====================


class EvidenceUploadResult(BaseModel):
    """证据上传结果"""

    # 核心字段
    attachment_id: int = Field(..., description="附件记录 ID")
    sha256: str = Field(..., description="内容 SHA256 哈希")
    artifact_uri: str = Field(..., description="制品 URI (evidence uri)")
    size_bytes: int = Field(..., description="内容大小（字节）")

    # 可选元数据
    content_type: str = Field(..., description="内容类型")
    created_at: str = Field(..., description="创建时间 (ISO 8601)")

    def to_evidence_object(self, title: Optional[str] = None) -> Dict[str, Any]:
        """
        转换为 v2 evidence 对象，可直接用于 memory_store

        Args:
            title: 可选的证据标题

        Returns:
            v2 格式的 evidence dict

        注意:
            uri 字段使用 canonical evidence URI 格式:
            - attachment_id > 0: memory://attachments/<attachment_id>/<sha256>
            - attachment_id == 0: 使用物理存储路径 artifact_uri（fallback）
        """
        # 构建 canonical evidence URI
        if self.attachment_id > 0:
            # 使用 canonical 格式: memory://attachments/<attachment_id>/<sha256>
            canonical_uri = build_attachment_evidence_uri(self.attachment_id, self.sha256)
        else:
            # 无 attachment 记录时，fallback 到 artifact_uri
            canonical_uri = self.artifact_uri

        evidence = {
            "uri": canonical_uri,
            "sha256": self.sha256,
            "source_type": "artifact",
            "source_id": str(self.attachment_id),
            "timestamp": self.created_at,
        }
        if title:
            evidence["title"] = title
        return evidence


# ===================== 核心函数 =====================


def upload_evidence(
    content: Union[bytes, str],
    content_type: str,
    actor_user_id: Optional[str] = None,
    project_key: Optional[str] = None,
    item_id: Optional[int] = None,
    kind: str = "evidence",
    title: Optional[str] = None,
    meta_json: Optional[Dict[str, Any]] = None,
    encoding: str = "utf-8",
) -> EvidenceUploadResult:
    """
    上传证据内容到 Logbook 存储

    流程:
    1. 校验内容大小
    2. 校验内容类型
    3. 计算 SHA256 哈希
    4. 生成 artifact URI
    5. 写入 artifact store
    6. 插入 attachments 表
    7. 返回结果

    Args:
        content: 证据内容（bytes 或 str）
        content_type: 内容类型（如 text/plain, text/markdown, application/json）
        actor_user_id: 操作者用户标识（可选）
        project_key: 项目标识（可选，用于 URI 命名空间）
        item_id: 关联的 logbook.items.item_id（可选）
        kind: 附件类型，默认 "evidence"
        title: 证据标题（可选）
        meta_json: 元数据（可选）
        encoding: 字符串编码（当 content 为 str 时使用）

    Returns:
        EvidenceUploadResult 对象

    Raises:
        EvidenceSizeLimitExceededError: 内容大小超出限制
        EvidenceContentTypeError: 内容类型不允许
        EvidenceWriteError: 写入存储失败
    """
    # 1. 转换为 bytes
    if isinstance(content, str):
        content_bytes = content.encode(encoding)
    else:
        content_bytes = content

    size_bytes = len(content_bytes)

    # 2. 校验内容大小
    max_size = get_max_size_bytes()
    if size_bytes > max_size:
        logger.warning(f"证据内容超出大小限制: size={size_bytes}, max={max_size}")
        raise EvidenceSizeLimitExceededError(size_bytes, max_size)

    # 3. 校验内容类型
    if content_type not in ALLOWED_CONTENT_TYPES:
        logger.warning(f"不支持的内容类型: {content_type}, allowed={ALLOWED_CONTENT_TYPES}")
        raise EvidenceContentTypeError(content_type, ALLOWED_CONTENT_TYPES)

    # 4. 计算 SHA256 哈希
    sha256 = hashlib.sha256(content_bytes).hexdigest()

    # 5. 生成 artifact URI
    prefix = get_artifacts_prefix()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    unique_id = uuid.uuid4().hex[:12]

    # URI 格式: {prefix}/{project_key}/{date}/{unique_id}_{sha256[:8]}
    if project_key:
        artifact_uri = f"{prefix}/{project_key}/{timestamp}/{unique_id}_{sha256[:8]}"
    else:
        artifact_uri = f"{prefix}/{timestamp}/{unique_id}_{sha256[:8]}"

    # 根据 content_type 添加扩展名
    extension_map = {
        "text/plain": ".txt",
        "text/markdown": ".md",
        "text/x-diff": ".diff",
        "text/x-patch": ".patch",
        "application/json": ".json",
        "application/xml": ".xml",
        "text/xml": ".xml",
        "text/html": ".html",
        "text/csv": ".csv",
        "text/yaml": ".yaml",
        "application/x-yaml": ".yaml",
    }
    extension = extension_map.get(content_type, "")
    artifact_uri += extension

    # 6. 写入 artifact store
    try:
        store = get_artifact_store()
        put_result = store.put(uri=artifact_uri, content=content_bytes)

        # 验证哈希一致性
        if put_result.get("sha256") != sha256:
            logger.error(f"SHA256 不一致: calculated={sha256}, stored={put_result.get('sha256')}")
            raise EvidenceWriteError("SHA256 校验失败")

        logger.info(f"证据内容写入成功: uri={artifact_uri}, size={size_bytes}")

    except ArtifactSizeLimitExceededError:
        raise EvidenceSizeLimitExceededError(size_bytes, max_size)
    except ArtifactError as e:
        raise EvidenceWriteError(str(e), e)
    except Exception as e:
        logger.exception(f"写入 artifact store 失败: {e}")
        raise EvidenceWriteError(str(e), e)

    # 7. 插入 attachments 表
    # 构建元数据
    final_meta = meta_json.copy() if meta_json else {}
    final_meta.update(
        {
            "content_type": content_type,
            "actor_user_id": actor_user_id,
            "project_key": project_key,
        }
    )
    if title:
        final_meta["title"] = title

    # 7.1 校验 item_id 必须提供
    if item_id is None:
        logger.error("未提供 item_id，无法创建 attachment 记录")
        raise EvidenceItemRequiredError()

    try:
        attachment_id = db_attach(
            item_id=item_id,
            kind=kind,
            uri=artifact_uri,
            sha256=sha256,
            size_bytes=size_bytes,
            meta_json=final_meta,
        )
        logger.info(f"附件记录创建成功: attachment_id={attachment_id}")

    except EvidenceItemRequiredError:
        # 重新抛出业务错误
        raise
    except Exception as e:
        logger.exception(f"插入 attachments 表失败: {e}")
        raise EvidenceWriteError(f"插入 attachments 表失败: {e}", e)

    # 8. 返回结果
    created_at = datetime.now(timezone.utc).isoformat()

    return EvidenceUploadResult(
        attachment_id=attachment_id,
        sha256=sha256,
        artifact_uri=artifact_uri,
        size_bytes=size_bytes,
        content_type=content_type,
        created_at=created_at,
    )


def get_evidence_info(uri: str) -> Optional[Dict[str, Any]]:
    """
    获取已上传证据的元信息

    Args:
        uri: 制品 URI

    Returns:
        元信息字典 {uri, sha256, size_bytes, exists}，不存在返回 None
    """
    try:
        store = get_artifact_store()
        if not store.exists(uri):
            return None

        info = store.get_info(uri)
        return {
            "uri": uri,
            "sha256": info.get("sha256"),
            "size_bytes": info.get("size_bytes"),
            "exists": True,
        }
    except Exception as e:
        logger.exception(f"获取证据信息失败: {e}")
        return None
