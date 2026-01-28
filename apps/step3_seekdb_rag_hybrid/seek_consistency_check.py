#!/usr/bin/env python3
"""
seek_consistency_check.py - Step3 索引一致性检查工具

检查 SeekDB/RAG 索引与 Step1 数据之间的一致性问题：

patch_blobs 检查:
1. missing_index - patch_blobs 中有记录但未被索引（根据 chunking_version）
2. missing_evidence_uri - 缺失 evidence_uri 或 artifact_uri
3. unreadable_artifact - artifact URI 指向的文件不存在或无法读取
4. sha_mismatch - artifact 文件的 SHA256 与数据库记录不匹配
5. scheme_violation - evidence_uri scheme 不符合规范

attachments 检查:
6. attachment_missing_index - attachment 未被索引
7. attachment_missing_uri - attachment uri 为空
8. attachment_unreadable - attachment 制品文件不可读
9. attachment_sha_mismatch - attachment SHA256 不匹配

索引一致性检查（需要 --check-index）:
10. index_missing - 索引中不存在预期的 chunk
11. index_metadata_mismatch - 索引中 chunk 的元数据与数据库不一致
12. attachment_index_missing - attachment 索引中不存在预期的 chunk
13. attachment_index_metadata_mismatch - attachment 索引元数据不一致

Scheme 校验规则（--check-scheme，默认开启）:
- patch_blobs 必须使用 memory://patch_blobs/...
- attachments 必须使用 memory://attachments/...
- 其他来源必须落在 contracts 白名单（memory, artifact, file）

输入参数：
    - chunking_version: 要检查的分块版本号（必需）
    - project_key: 项目标识（可选，用于筛选特定项目）
    - sample_ratio: 抽样比例（0.0-1.0），默认 1.0 全量检查
    - limit: 最大检查记录数（可选，与 sample_ratio 互斥）

输出：
    - JSON 格式的检查报告
    - 可选写入 logbook.events 作为审计记录

使用:
    # Makefile 入口（推荐）
    make step3-check                                        # 检查默认版本
    make step3-check CHUNKING_VERSION=v1-2026-01            # 指定版本
    make step3-check PROJECT_KEY=myproject                  # 检查特定项目
    make step3-check SAMPLE_RATIO=0.1                       # 抽样 10% 检查
    make step3-check LIMIT=1000 JSON_OUTPUT=1               # 限制检查数并 JSON 输出
    make step3-check SKIP_ARTIFACTS=1                       # 仅检查索引状态
    make step3-check CHECK_INDEX=1 INDEX_BACKEND=pgvector   # 检查索引一致性
    make step3-check SKIP_ATTACHMENTS=1                     # 跳过 attachments 检查
    make step3-check SKIP_SCHEME_CHECK=1                    # 跳过 scheme 校验

    # 直接调用（在 apps/step3_seekdb_rag_hybrid 目录下）
    python -m seek_consistency_check --chunking-version v1-2026-01
    python -m seek_consistency_check --chunking-version v1-2026-01 --project-key myproject
    python -m seek_consistency_check --chunking-version v1-2026-01 --sample-ratio 0.1
    python -m seek_consistency_check --chunking-version v1-2026-01 --check-index --index-backend pgvector
    python -m seek_consistency_check --chunking-version v1-2026-01 --skip-attachments
    python -m seek_consistency_check --chunking-version v1-2026-01 --skip-scheme-check
    python -m seek_consistency_check --chunking-version v1-2026-01 --log-to-logbook --item-id 123
"""

import argparse
import hashlib
import json
import logging
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.rows import dict_row

# 导入 Step1 模块
from engram_step1.config import add_config_argument, get_config
from engram_step1.db import get_connection
from engram_step1.errors import DatabaseError, EngramError, make_success_result, make_error_result

# 导入 Step3 模块
from step3_seekdb_rag_hybrid.step3_chunking import (
    CHUNKING_VERSION,
    CHUNK_ID_NAMESPACE,
    generate_chunk_id,
    parse_chunk_id,
)

# 导入制品相关
from artifacts import artifact_exists, get_artifact_info, read_artifact

# 可选导入 IndexBackend（索引验证功能）
try:
    from step3_seekdb_rag_hybrid.index_backend.base import IndexBackend
    from step3_seekdb_rag_hybrid.index_backend.types import ChunkDoc
    INDEX_BACKEND_AVAILABLE = True
except ImportError:
    try:
        from index_backend.base import IndexBackend
        from index_backend.types import ChunkDoc
        INDEX_BACKEND_AVAILABLE = True
    except ImportError:
        IndexBackend = None  # type: ignore
        ChunkDoc = None  # type: ignore
        INDEX_BACKEND_AVAILABLE = False

# 导入后端工厂
try:
    from step3_seekdb_rag_hybrid.step3_backend_factory import (
        add_backend_arguments,
        create_backend_from_args,
        get_backend_info,
    )
    BACKEND_FACTORY_AVAILABLE = True
except ImportError:
    try:
        from step3_backend_factory import (
            add_backend_arguments,
            create_backend_from_args,
            get_backend_info,
        )
        BACKEND_FACTORY_AVAILABLE = True
    except ImportError:
        BACKEND_FACTORY_AVAILABLE = False
        add_backend_arguments = None  # type: ignore
        create_backend_from_args = None  # type: ignore
        get_backend_info = None  # type: ignore

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ============ 数据类定义 ============


@dataclass
class ConsistencyIssue:
    """
    一致性问题记录
    
    issue_type 取值:
        # patch_blobs 相关
        - missing_index: patch_blobs 中 chunking_version 不匹配
        - missing_evidence_uri: 缺失 evidence_uri 或 artifact_uri
        - unreadable_artifact: artifact URI 指向的文件不存在或无法读取
        - sha_mismatch: artifact 文件的 SHA256 与数据库记录不匹配
        - scheme_violation: evidence_uri scheme 不符合规范
        
        # attachments 相关
        - attachment_missing_index: attachment chunking_version 不匹配
        - attachment_missing_uri: attachment uri 为空
        - attachment_unreadable: attachment 制品文件不可读
        - attachment_sha_mismatch: attachment SHA256 不匹配
        
        # patch_blobs 索引相关（需要 IndexBackend）
        - index_missing: 索引中不存在预期的 chunk
        - index_version_mismatch: 索引中 chunk 的版本与预期不匹配
        - index_metadata_mismatch: 索引中 chunk 的元数据与数据库不一致
        
        # attachments 索引相关（需要 IndexBackend）
        - attachment_index_missing: attachment 索引中不存在预期的 chunk
        - attachment_index_metadata_mismatch: attachment 索引元数据不一致
    """
    issue_type: str
    blob_id: int
    source_type: str
    source_id: str
    uri: Optional[str] = None
    evidence_uri: Optional[str] = None
    sha256: Optional[str] = None
    chunking_version: Optional[str] = None
    project_key: Optional[str] = None
    details: Optional[str] = None  # 问题详情

    def to_dict(self) -> Dict[str, Any]:
        return {
            "issue_type": self.issue_type,
            "blob_id": self.blob_id,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "uri": self.uri,
            "evidence_uri": self.evidence_uri,
            "sha256": self.sha256,
            "chunking_version": self.chunking_version,
            "project_key": self.project_key,
            "details": self.details,
        }


@dataclass
class ConsistencyCheckResult:
    """一致性检查结果"""
    # 统计信息
    total_checked: int = 0
    total_issues: int = 0
    
    # 按问题类型分组的计数
    issue_counts: Dict[str, int] = field(default_factory=dict)
    
    # 问题示例（每种类型最多保留 10 个）
    issue_samples: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    
    # 检查参数
    chunking_version: str = ""
    project_key: Optional[str] = None
    sample_ratio: Optional[float] = None
    limit: Optional[int] = None
    
    # 时间信息
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_seconds: float = 0.0

    @property
    def has_issues(self) -> bool:
        return self.total_issues > 0

    def add_issue(self, issue: ConsistencyIssue, max_samples: int = 10):
        """添加一个问题记录"""
        self.total_issues += 1
        
        # 更新计数
        if issue.issue_type not in self.issue_counts:
            self.issue_counts[issue.issue_type] = 0
            self.issue_samples[issue.issue_type] = []
        
        self.issue_counts[issue.issue_type] += 1
        
        # 添加示例（最多 max_samples 个）
        if len(self.issue_samples[issue.issue_type]) < max_samples:
            self.issue_samples[issue.issue_type].append(issue.to_dict())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": {
                "total_checked": self.total_checked,
                "total_issues": self.total_issues,
                "has_issues": self.has_issues,
                "issue_counts": self.issue_counts,
            },
            "parameters": {
                "chunking_version": self.chunking_version,
                "project_key": self.project_key,
                "sample_ratio": self.sample_ratio,
                "limit": self.limit,
            },
            "timing": {
                "started_at": self.started_at,
                "completed_at": self.completed_at,
                "duration_seconds": self.duration_seconds,
            },
            "issue_samples": self.issue_samples,
        }


# ============ SQL 查询 ============


SQL_FETCH_PATCH_BLOBS_TO_CHECK = """
-- 获取需要检查的 patch_blobs 记录
-- 参数:
--   :chunking_version - 要检查的分块版本
--   :project_key - 项目标识（可选，NULL 表示不筛选）
--   :limit - 最大记录数
-- 注意: evidence_uri 使用 COALESCE 兼容旧数据（列优先，meta_json 回退）
SELECT
    pb.blob_id,
    pb.source_type,
    pb.source_id,
    pb.uri,
    COALESCE(pb.evidence_uri, pb.meta_json->>'evidence_uri') AS evidence_uri,
    pb.sha256,
    pb.size_bytes,
    pb.format,
    pb.chunking_version,
    pb.created_at,
    r.repo_id,
    r.project_key,
    r.repo_type,
    r.url AS repo_url
FROM scm.patch_blobs pb
JOIN scm.repos r ON r.repo_id = CAST(split_part(pb.source_id, ':', 1) AS bigint)
WHERE (
    -- 检查未索引（chunking_version 为空或不匹配）
    pb.chunking_version IS NULL 
    OR pb.chunking_version != :chunking_version
)
AND (:project_key IS NULL OR r.project_key = :project_key)
ORDER BY pb.blob_id
LIMIT :limit;
"""


SQL_FETCH_PATCH_BLOBS_FULL = """
-- 获取全量 patch_blobs 记录用于一致性检查
-- 参数:
--   :chunking_version - 要检查的分块版本
--   :project_key - 项目标识（可选，NULL 表示不筛选）
--   :limit - 最大记录数
-- 注意: evidence_uri 使用 COALESCE 兼容旧数据（列优先，meta_json 回退）
SELECT
    pb.blob_id,
    pb.source_type,
    pb.source_id,
    pb.uri,
    COALESCE(pb.evidence_uri, pb.meta_json->>'evidence_uri') AS evidence_uri,
    pb.sha256,
    pb.size_bytes,
    pb.format,
    pb.chunking_version,
    pb.created_at,
    r.repo_id,
    r.project_key,
    r.repo_type,
    r.url AS repo_url
FROM scm.patch_blobs pb
JOIN scm.repos r ON r.repo_id = CAST(split_part(pb.source_id, ':', 1) AS bigint)
WHERE (:project_key IS NULL OR r.project_key = :project_key)
ORDER BY pb.blob_id
LIMIT :limit;
"""


SQL_COUNT_PATCH_BLOBS = """
-- 统计符合条件的 patch_blobs 总数
SELECT COUNT(*) as total
FROM scm.patch_blobs pb
JOIN scm.repos r ON r.repo_id = CAST(split_part(pb.source_id, ':', 1) AS bigint)
WHERE (:project_key IS NULL OR r.project_key = :project_key);
"""


# ============ Attachments SQL 查询 ============


SQL_FETCH_ATTACHMENTS_FULL = """
-- 获取全量 attachments 记录用于一致性检查
-- 参数:
--   :chunking_version - 要检查的分块版本
--   :project_key - 项目标识（可选，NULL 表示不筛选）
--   :limit - 最大记录数
SELECT
    a.attachment_id,
    a.item_id,
    a.kind,
    a.uri,
    a.sha256,
    a.size_bytes,
    a.meta_json,
    a.chunking_version,
    a.created_at,
    i.project_key
FROM logbook.attachments a
LEFT JOIN logbook.items i ON i.item_id = a.item_id
WHERE (:project_key IS NULL OR i.project_key = :project_key)
ORDER BY a.attachment_id
LIMIT :limit;
"""


SQL_FETCH_ATTACHMENTS_TO_CHECK = """
-- 获取需要检查的 attachments 记录（chunking_version 不匹配）
-- 参数:
--   :chunking_version - 要检查的分块版本
--   :project_key - 项目标识（可选，NULL 表示不筛选）
--   :limit - 最大记录数
SELECT
    a.attachment_id,
    a.item_id,
    a.kind,
    a.uri,
    a.sha256,
    a.size_bytes,
    a.meta_json,
    a.chunking_version,
    a.created_at,
    i.project_key
FROM logbook.attachments a
LEFT JOIN logbook.items i ON i.item_id = a.item_id
WHERE (
    a.chunking_version IS NULL 
    OR a.chunking_version != :chunking_version
)
AND (:project_key IS NULL OR i.project_key = :project_key)
ORDER BY a.attachment_id
LIMIT :limit;
"""


SQL_COUNT_ATTACHMENTS = """
-- 统计符合条件的 attachments 总数
SELECT COUNT(*) as total
FROM logbook.attachments a
LEFT JOIN logbook.items i ON i.item_id = a.item_id
WHERE (:project_key IS NULL OR i.project_key = :project_key);
"""


# ============ Scheme 校验白名单 ============

# contracts 白名单：允许的其他 evidence_uri scheme
ALLOWED_EVIDENCE_SCHEMES = {
    "memory",       # memory://patch_blobs/..., memory://attachments/...
    "artifact",     # artifact://... 旧格式
    "file",         # file://... 本地文件
}


# ============ 检查函数 ============


def build_attachment_evidence_uri(attachment_id: int, sha256: str) -> str:
    """
    构建 attachment 的 canonical evidence URI（不依赖 DB 存储）
    
    格式: memory://attachments/<attachment_id>/<sha256>
    
    Args:
        attachment_id: 附件 ID
        sha256: 内容 SHA256 哈希
    
    Returns:
        Canonical evidence URI
    """
    sha256_norm = sha256.strip().lower() if sha256 else ""
    return f"memory://attachments/{attachment_id}/{sha256_norm}"


def check_evidence_uri_scheme(
    evidence_uri: str,
    source_type: str,
    record_type: str = "patch_blob",
) -> Optional[str]:
    """
    校验 evidence_uri 的 scheme 是否符合规范
    
    规则:
    - patch_blobs 记录必须使用 memory://patch_blobs/...
    - attachments 记录必须使用 memory://attachments/...
    - docs 记录必须使用 memory://docs/...
    - 其他来源必须落在 ALLOWED_EVIDENCE_SCHEMES 白名单
    
    Args:
        evidence_uri: evidence URI
        source_type: 来源类型
        record_type: 记录类型 ("patch_blob", "attachment" 或 "docs")
    
    Returns:
        错误详情（如果有问题），否则 None
    """
    if not evidence_uri:
        return None  # 缺失 URI 由其他检查处理
    
    # 解析 scheme
    if "://" in evidence_uri:
        scheme = evidence_uri.split("://")[0].lower()
    else:
        # 无 scheme，视为 artifact key
        scheme = "artifact"
    
    # 检查 scheme 是否在白名单
    if scheme not in ALLOWED_EVIDENCE_SCHEMES:
        return f"非法 scheme: {scheme}，允许的 scheme: {', '.join(sorted(ALLOWED_EVIDENCE_SCHEMES))}"
    
    # 检查 memory:// URI 的路径前缀
    if scheme == "memory":
        if evidence_uri.startswith("memory://patch_blobs/"):
            if record_type != "patch_blob":
                return f"attachment/docs 记录不应使用 memory://patch_blobs/ URI: {evidence_uri}"
        elif evidence_uri.startswith("memory://attachments/"):
            if record_type != "attachment":
                return f"patch_blob/docs 记录不应使用 memory://attachments/ URI: {evidence_uri}"
        elif evidence_uri.startswith("memory://docs/"):
            if record_type != "docs":
                return f"patch_blob/attachment 记录不应使用 memory://docs/ URI: {evidence_uri}"
        else:
            # 其他 memory:// 路径
            path_parts = evidence_uri.replace("memory://", "").split("/")
            resource_type = path_parts[0] if path_parts else ""
            return f"未知的 memory:// 资源类型: {resource_type}，允许: patch_blobs, attachments, docs"
    
    return None


def check_missing_index(
    row: Dict[str, Any],
    target_version: str,
) -> Optional[ConsistencyIssue]:
    """
    检查是否缺少索引（chunking_version 不匹配）
    
    Args:
        row: patch_blobs 记录
        target_version: 目标分块版本
    
    Returns:
        ConsistencyIssue 如果有问题，否则 None
    """
    current_version = row.get("chunking_version")
    
    if current_version is None or current_version != target_version:
        return ConsistencyIssue(
            issue_type="missing_index",
            blob_id=row["blob_id"],
            source_type=row["source_type"],
            source_id=row["source_id"],
            uri=row.get("uri"),
            evidence_uri=row.get("evidence_uri"),
            sha256=row.get("sha256"),
            chunking_version=current_version,
            project_key=row.get("project_key"),
            details=f"当前版本: {current_version or '(空)'}, 目标版本: {target_version}",
        )
    
    return None


def check_missing_evidence_uri(row: Dict[str, Any]) -> Optional[ConsistencyIssue]:
    """
    检查是否缺少 evidence_uri
    
    Args:
        row: patch_blobs 记录
    
    Returns:
        ConsistencyIssue 如果有问题，否则 None
    """
    evidence_uri = row.get("evidence_uri")
    uri = row.get("uri")
    
    # 检查是否两者都缺失
    if not evidence_uri and not uri:
        return ConsistencyIssue(
            issue_type="missing_evidence_uri",
            blob_id=row["blob_id"],
            source_type=row["source_type"],
            source_id=row["source_id"],
            uri=uri,
            evidence_uri=evidence_uri,
            sha256=row.get("sha256"),
            chunking_version=row.get("chunking_version"),
            project_key=row.get("project_key"),
            details="uri 和 evidence_uri 均为空",
        )
    
    # 只检查 evidence_uri 缺失（uri 存在时可作为 fallback）
    if not evidence_uri:
        return ConsistencyIssue(
            issue_type="missing_evidence_uri",
            blob_id=row["blob_id"],
            source_type=row["source_type"],
            source_id=row["source_id"],
            uri=uri,
            evidence_uri=evidence_uri,
            sha256=row.get("sha256"),
            chunking_version=row.get("chunking_version"),
            project_key=row.get("project_key"),
            details="evidence_uri 为空，可使用 uri 作为 fallback",
        )
    
    return None


def check_unreadable_artifact(row: Dict[str, Any]) -> Optional[ConsistencyIssue]:
    """
    检查 artifact 文件是否可读
    
    Args:
        row: patch_blobs 记录
    
    Returns:
        ConsistencyIssue 如果有问题，否则 None
    """
    # 优先检查 uri，其次检查 evidence_uri
    uri = row.get("uri") or row.get("evidence_uri")
    
    if not uri:
        return None  # 已在 check_missing_evidence_uri 中处理
    
    try:
        if not artifact_exists(uri):
            return ConsistencyIssue(
                issue_type="unreadable_artifact",
                blob_id=row["blob_id"],
                source_type=row["source_type"],
                source_id=row["source_id"],
                uri=row.get("uri"),
                evidence_uri=row.get("evidence_uri"),
                sha256=row.get("sha256"),
                chunking_version=row.get("chunking_version"),
                project_key=row.get("project_key"),
                details=f"制品文件不存在: {uri}",
            )
    except Exception as e:
        return ConsistencyIssue(
            issue_type="unreadable_artifact",
            blob_id=row["blob_id"],
            source_type=row["source_type"],
            source_id=row["source_id"],
            uri=row.get("uri"),
            evidence_uri=row.get("evidence_uri"),
            sha256=row.get("sha256"),
            chunking_version=row.get("chunking_version"),
            project_key=row.get("project_key"),
            details=f"无法访问制品文件: {uri}, 错误: {e}",
        )
    
    return None


def check_sha_mismatch(row: Dict[str, Any]) -> Optional[ConsistencyIssue]:
    """
    检查 SHA256 是否匹配
    
    Args:
        row: patch_blobs 记录
    
    Returns:
        ConsistencyIssue 如果有问题，否则 None
    """
    uri = row.get("uri") or row.get("evidence_uri")
    expected_sha256 = row.get("sha256")
    
    if not uri or not expected_sha256:
        return None  # 没有 URI 或 SHA256 无法检查
    
    try:
        if not artifact_exists(uri):
            return None  # 已在 check_unreadable_artifact 中处理
        
        # 获取实际的 SHA256
        artifact_info = get_artifact_info(uri)
        actual_sha256 = artifact_info.get("sha256", "")
        
        if actual_sha256.lower() != expected_sha256.lower():
            return ConsistencyIssue(
                issue_type="sha_mismatch",
                blob_id=row["blob_id"],
                source_type=row["source_type"],
                source_id=row["source_id"],
                uri=row.get("uri"),
                evidence_uri=row.get("evidence_uri"),
                sha256=expected_sha256,
                chunking_version=row.get("chunking_version"),
                project_key=row.get("project_key"),
                details=f"SHA256 不匹配: 期望={expected_sha256[:16]}..., 实际={actual_sha256[:16]}...",
            )
    except Exception as e:
        # 读取失败已在 check_unreadable_artifact 中处理，这里跳过
        pass
    
    return None


def check_scheme_violation(
    row: Dict[str, Any],
    record_type: str = "patch_blob",
) -> Optional[ConsistencyIssue]:
    """
    检查 evidence_uri 的 scheme 是否符合规范
    
    Args:
        row: patch_blobs 或 attachments 记录
        record_type: 记录类型 ("patch_blob" 或 "attachment")
    
    Returns:
        ConsistencyIssue 如果有问题，否则 None
    """
    evidence_uri = row.get("evidence_uri") or row.get("uri")
    source_type = row.get("source_type") or row.get("kind", "")
    
    error_detail = check_evidence_uri_scheme(evidence_uri, source_type, record_type)
    
    if error_detail:
        # 获取记录标识
        if record_type == "attachment":
            blob_id = row.get("attachment_id", 0)
            source_id = f"attachment:{blob_id}"
            source_type = row.get("kind", "attachment")
        else:
            blob_id = row.get("blob_id", 0)
            source_id = row.get("source_id", "")
            source_type = row.get("source_type", "")
        
        return ConsistencyIssue(
            issue_type="scheme_violation",
            blob_id=blob_id,
            source_type=source_type,
            source_id=source_id,
            uri=row.get("uri"),
            evidence_uri=evidence_uri,
            sha256=row.get("sha256"),
            chunking_version=row.get("chunking_version"),
            project_key=row.get("project_key"),
            details=error_detail,
        )
    
    return None


# ============ Attachment 检查函数 ============


def check_attachment_missing_index(
    row: Dict[str, Any],
    target_version: str,
) -> Optional[ConsistencyIssue]:
    """
    检查 attachment 是否缺少索引（chunking_version 不匹配）
    
    Args:
        row: attachments 记录
        target_version: 目标分块版本
    
    Returns:
        ConsistencyIssue 如果有问题，否则 None
    """
    current_version = row.get("chunking_version")
    attachment_id = row.get("attachment_id", 0)
    
    if current_version is None or current_version != target_version:
        return ConsistencyIssue(
            issue_type="attachment_missing_index",
            blob_id=attachment_id,
            source_type=row.get("kind", "attachment"),
            source_id=f"attachment:{attachment_id}",
            uri=row.get("uri"),
            evidence_uri=build_attachment_evidence_uri(attachment_id, row.get("sha256", "")),
            sha256=row.get("sha256"),
            chunking_version=current_version,
            project_key=row.get("project_key"),
            details=f"当前版本: {current_version or '(空)'}, 目标版本: {target_version}",
        )
    
    return None


def check_attachment_unreadable(row: Dict[str, Any]) -> Optional[ConsistencyIssue]:
    """
    检查 attachment artifact 文件是否可读
    
    复用 artifacts.artifact_exists 和 artifacts.read_artifact
    
    Args:
        row: attachments 记录
    
    Returns:
        ConsistencyIssue 如果有问题，否则 None
    """
    uri = row.get("uri")
    attachment_id = row.get("attachment_id", 0)
    
    if not uri:
        return ConsistencyIssue(
            issue_type="attachment_missing_uri",
            blob_id=attachment_id,
            source_type=row.get("kind", "attachment"),
            source_id=f"attachment:{attachment_id}",
            uri=None,
            evidence_uri=build_attachment_evidence_uri(attachment_id, row.get("sha256", "")),
            sha256=row.get("sha256"),
            chunking_version=row.get("chunking_version"),
            project_key=row.get("project_key"),
            details="attachment uri 为空",
        )
    
    try:
        if not artifact_exists(uri):
            return ConsistencyIssue(
                issue_type="attachment_unreadable",
                blob_id=attachment_id,
                source_type=row.get("kind", "attachment"),
                source_id=f"attachment:{attachment_id}",
                uri=uri,
                evidence_uri=build_attachment_evidence_uri(attachment_id, row.get("sha256", "")),
                sha256=row.get("sha256"),
                chunking_version=row.get("chunking_version"),
                project_key=row.get("project_key"),
                details=f"attachment 制品文件不存在: {uri}",
            )
    except Exception as e:
        return ConsistencyIssue(
            issue_type="attachment_unreadable",
            blob_id=attachment_id,
            source_type=row.get("kind", "attachment"),
            source_id=f"attachment:{attachment_id}",
            uri=uri,
            evidence_uri=build_attachment_evidence_uri(attachment_id, row.get("sha256", "")),
            sha256=row.get("sha256"),
            chunking_version=row.get("chunking_version"),
            project_key=row.get("project_key"),
            details=f"无法访问 attachment 制品文件: {uri}, 错误: {e}",
        )
    
    return None


def check_attachment_sha_mismatch(row: Dict[str, Any]) -> Optional[ConsistencyIssue]:
    """
    检查 attachment SHA256 是否匹配
    
    复用 artifacts.get_artifact_info 获取实际 SHA256
    
    Args:
        row: attachments 记录
    
    Returns:
        ConsistencyIssue 如果有问题，否则 None
    """
    uri = row.get("uri")
    expected_sha256 = row.get("sha256")
    attachment_id = row.get("attachment_id", 0)
    
    if not uri or not expected_sha256:
        return None  # 没有 URI 或 SHA256 无法检查
    
    try:
        if not artifact_exists(uri):
            return None  # 已在 check_attachment_unreadable 中处理
        
        # 获取实际的 SHA256
        artifact_info = get_artifact_info(uri)
        actual_sha256 = artifact_info.get("sha256", "")
        
        if actual_sha256.lower() != expected_sha256.lower():
            return ConsistencyIssue(
                issue_type="attachment_sha_mismatch",
                blob_id=attachment_id,
                source_type=row.get("kind", "attachment"),
                source_id=f"attachment:{attachment_id}",
                uri=uri,
                evidence_uri=build_attachment_evidence_uri(attachment_id, expected_sha256),
                sha256=expected_sha256,
                chunking_version=row.get("chunking_version"),
                project_key=row.get("project_key"),
                details=f"SHA256 不匹配: 期望={expected_sha256[:16]}..., 实际={actual_sha256[:16]}...",
            )
    except Exception as e:
        # 读取失败已在 check_attachment_unreadable 中处理，这里跳过
        pass
    
    return None


# ============ 索引验证函数（需要 IndexBackend） ============


def generate_expected_chunk_id(
    row: Dict[str, Any],
    chunking_version: str,
    chunk_idx: int = 0,
    namespace: str = CHUNK_ID_NAMESPACE,
) -> str:
    """
    根据 patch_blob 记录生成预期的 chunk_id
    
    使用 step3_chunking.generate_chunk_id 保持一致性：
    - 格式: <namespace>:<source_type>:<source_id>:<sha256_prefix>:<chunking_version>:<chunk_idx>
    - sha256_prefix 取前 12 位
    - source_id 中的冒号替换为点
    - namespace 默认为 CHUNK_ID_NAMESPACE ("engram")
    
    Args:
        row: patch_blobs 记录
        chunking_version: 分块版本
        chunk_idx: 分块索引（默认 0，检查第一个 chunk）
        namespace: 命名空间（默认 CHUNK_ID_NAMESPACE）
    
    Returns:
        预期的 chunk_id
    """
    source_type = row.get("source_type", "unknown")
    source_id = row.get("source_id", "")
    sha256 = row.get("sha256", "") or "unknown"
    
    # 使用 step3_chunking.generate_chunk_id 保持规则一致
    # 内部会：sha256[:12], source_id.replace(":", ".")
    return generate_chunk_id(
        source_type=source_type,
        source_id=source_id,
        sha256=sha256,
        chunk_idx=chunk_idx,
        namespace=namespace,
        chunking_version=chunking_version,
    )


def check_index_existence(
    row: Dict[str, Any],
    index_backend: "IndexBackend",
    chunking_version: str,
    max_probe_chunks: int = 5,
) -> List[ConsistencyIssue]:
    """
    检查索引中是否存在预期的 chunk
    
    对每条 patch_blob/attachment 验证"存在任意 chunk"
    
    探测策略（降低误判）：
    1. 首先尝试 chunk_idx=0
    2. 如果不存在，尝试调用 count_by_source 查询总数
    3. 如果 count_by_source 不支持（返回 -1），循环探测少量 chunk_idx（0~max_probe_chunks-1）
    4. 只有全部探测失败才报告 index_missing
    
    Args:
        row: patch_blobs 记录
        index_backend: 索引后端实例
        chunking_version: 分块版本
        max_probe_chunks: 最大探测 chunk 数量（默认 5）
    
    Returns:
        ConsistencyIssue 列表
    """
    issues = []
    
    # 仅检查已标记为已索引的记录（chunking_version 匹配）
    current_version = row.get("chunking_version")
    if current_version != chunking_version:
        return []  # 未索引的记录由 check_missing_index 处理
    
    source_type = row.get("source_type", "")
    source_id = row.get("source_id", "")
    
    try:
        # 策略 1: 先尝试 chunk_idx=0
        expected_chunk_id_0 = generate_expected_chunk_id(row, chunking_version, chunk_idx=0)
        exists_result = index_backend.exists([expected_chunk_id_0])
        
        if exists_result.get(expected_chunk_id_0, False):
            # chunk_idx=0 存在，通过检查
            return []
        
        # 策略 2: 尝试 count_by_source
        try:
            count = index_backend.count_by_source(source_type, source_id)
            if count > 0:
                # 存在 chunk，通过检查
                return []
            elif count == 0:
                # 明确返回 0，说明没有 chunk
                issues.append(ConsistencyIssue(
                    issue_type="index_missing",
                    blob_id=row["blob_id"],
                    source_type=source_type,
                    source_id=source_id,
                    uri=row.get("uri"),
                    evidence_uri=row.get("evidence_uri"),
                    sha256=row.get("sha256"),
                    chunking_version=current_version,
                    project_key=row.get("project_key"),
                    details=f"索引中不存在该来源的任何 chunk (count_by_source=0), 预期 chunk_id 前缀: {expected_chunk_id_0.rsplit(':', 1)[0]}",
                ))
                return issues
            # count == -1 表示不支持，继续下一个策略
        except Exception as e:
            logger.debug(f"count_by_source 调用失败 source_type={source_type}, source_id={source_id}: {e}")
        
        # 策略 3: 循环探测少量 chunk_idx（跳过 0，因为已经检查过）
        chunk_ids_to_probe = [
            generate_expected_chunk_id(row, chunking_version, chunk_idx=i)
            for i in range(1, max_probe_chunks)
        ]
        
        if chunk_ids_to_probe:
            exists_result = index_backend.exists(chunk_ids_to_probe)
            for chunk_id in chunk_ids_to_probe:
                if exists_result.get(chunk_id, False):
                    # 找到存在的 chunk，通过检查
                    return []
        
        # 全部探测失败，报告 index_missing
        issues.append(ConsistencyIssue(
            issue_type="index_missing",
            blob_id=row["blob_id"],
            source_type=source_type,
            source_id=source_id,
            uri=row.get("uri"),
            evidence_uri=row.get("evidence_uri"),
            sha256=row.get("sha256"),
            chunking_version=current_version,
            project_key=row.get("project_key"),
            details=f"索引中不存在预期的 chunk (探测 chunk_idx 0~{max_probe_chunks-1}): {expected_chunk_id_0}",
        ))
        
    except Exception as e:
        logger.debug(f"索引检查失败 blob_id={row['blob_id']}: {e}")
    
    return issues


def check_index_metadata(
    row: Dict[str, Any],
    index_backend: "IndexBackend",
    chunking_version: str,
    max_probe_chunks: int = 5,
) -> List[ConsistencyIssue]:
    """
    检查索引中 chunk 的元数据是否与数据库一致
    
    探测策略：
    1. 先尝试 chunk_idx=0
    2. 如果不存在，探测 chunk_idx 1~max_probe_chunks-1 找到第一个存在的 chunk
    3. 对找到的 chunk 进行元数据验证
    
    Args:
        row: patch_blobs 记录
        index_backend: 索引后端实例
        chunking_version: 分块版本
        max_probe_chunks: 最大探测 chunk 数量（默认 5）
    
    Returns:
        ConsistencyIssue 列表
    """
    issues = []
    
    # 仅检查已标记为已索引的记录
    current_version = row.get("chunking_version")
    if current_version != chunking_version:
        return []
    
    try:
        # 探测找到第一个存在的 chunk
        found_chunk_id = None
        chunk_ids_to_probe = [
            generate_expected_chunk_id(row, chunking_version, chunk_idx=i)
            for i in range(max_probe_chunks)
        ]
        
        exists_result = index_backend.exists(chunk_ids_to_probe)
        for chunk_id in chunk_ids_to_probe:
            if exists_result.get(chunk_id, False):
                found_chunk_id = chunk_id
                break
        
        if not found_chunk_id:
            return []  # 不存在的情况由 check_index_existence 处理
        
        # 获取索引中的元数据
        metadata_result = index_backend.get_chunk_metadata([found_chunk_id])
        chunk_meta = metadata_result.get(found_chunk_id)
        
        if not chunk_meta:
            return []  # 元数据获取失败，跳过
        
        # 验证关键元数据字段
        expected_sha256 = row.get("sha256", "")
        indexed_sha256 = chunk_meta.get("sha256", "")
        
        if expected_sha256 and indexed_sha256:
            # 比较 SHA256（可能是截断的，step3_chunking 使用前 12 位）
            # 允许前缀匹配
            sha256_prefix_len = 12
            expected_prefix = expected_sha256[:sha256_prefix_len].lower()
            indexed_prefix = indexed_sha256[:sha256_prefix_len].lower() if len(indexed_sha256) >= sha256_prefix_len else indexed_sha256.lower()
            
            if not expected_prefix.startswith(indexed_prefix) and not indexed_prefix.startswith(expected_prefix):
                if expected_sha256.lower() != indexed_sha256.lower():
                    issues.append(ConsistencyIssue(
                        issue_type="index_metadata_mismatch",
                        blob_id=row["blob_id"],
                        source_type=row["source_type"],
                        source_id=row["source_id"],
                        uri=row.get("uri"),
                        evidence_uri=row.get("evidence_uri"),
                        sha256=expected_sha256,
                        chunking_version=current_version,
                        project_key=row.get("project_key"),
                        details=f"SHA256 不匹配: DB={expected_sha256[:16]}..., Index={indexed_sha256[:16] if indexed_sha256 else '(空)'}...",
                    ))
        
        # 验证 source_id（注意 chunk_id 中 source_id 的冒号被替换为点）
        expected_source_id = row.get("source_id", "")
        indexed_source_id = chunk_meta.get("source_id", "")
        # 还原 source_id 的冒号（如果索引中存储的是替换后的格式）
        indexed_source_id_normalized = indexed_source_id.replace(".", ":") if "." in indexed_source_id and ":" not in indexed_source_id else indexed_source_id
        if expected_source_id and indexed_source_id:
            if expected_source_id != indexed_source_id and expected_source_id != indexed_source_id_normalized:
                issues.append(ConsistencyIssue(
                    issue_type="index_metadata_mismatch",
                    blob_id=row["blob_id"],
                    source_type=row["source_type"],
                    source_id=row["source_id"],
                    uri=row.get("uri"),
                    evidence_uri=row.get("evidence_uri"),
                    sha256=row.get("sha256"),
                    chunking_version=current_version,
                    project_key=row.get("project_key"),
                    details=f"source_id 不匹配: DB={expected_source_id}, Index={indexed_source_id}",
                ))
        
        # 验证 project_key
        expected_project = row.get("project_key", "")
        indexed_project = chunk_meta.get("project_key", "")
        if expected_project and indexed_project and expected_project != indexed_project:
            issues.append(ConsistencyIssue(
                issue_type="index_metadata_mismatch",
                blob_id=row["blob_id"],
                source_type=row["source_type"],
                source_id=row["source_id"],
                uri=row.get("uri"),
                evidence_uri=row.get("evidence_uri"),
                sha256=row.get("sha256"),
                chunking_version=current_version,
                project_key=expected_project,
                details=f"project_key 不匹配: DB={expected_project}, Index={indexed_project}",
            ))
            
    except Exception as e:
        logger.debug(f"索引元数据检查失败 blob_id={row['blob_id']}: {e}")
    
    return issues


def run_index_checks(
    rows: List[Dict[str, Any]],
    index_backend: "IndexBackend",
    chunking_version: str,
    result: "ConsistencyCheckResult",
    sample_size: int = 100,
) -> None:
    """
    对记录进行索引一致性检查（抽样）
    
    Args:
        rows: patch_blobs 记录列表
        index_backend: 索引后端实例
        chunking_version: 分块版本
        result: 检查结果对象（会被原地修改）
        sample_size: 抽样大小
    """
    if not INDEX_BACKEND_AVAILABLE or index_backend is None:
        logger.warning("IndexBackend 不可用，跳过索引一致性检查")
        return
    
    # 筛选已索引的记录
    indexed_rows = [r for r in rows if r.get("chunking_version") == chunking_version]
    
    if not indexed_rows:
        logger.info("没有已索引的记录，跳过索引检查")
        return
    
    # 抽样
    if len(indexed_rows) > sample_size:
        check_rows = random.sample(indexed_rows, sample_size)
        logger.info(f"索引检查: 从 {len(indexed_rows)} 条已索引记录中抽样 {sample_size} 条")
    else:
        check_rows = indexed_rows
        logger.info(f"索引检查: 检查全部 {len(check_rows)} 条已索引记录")
    
    # 执行检查
    for row in check_rows:
        # 检查存在性
        for issue in check_index_existence(row, index_backend, chunking_version):
            result.add_issue(issue)
        
        # 检查元数据一致性
        for issue in check_index_metadata(row, index_backend, chunking_version):
            result.add_issue(issue)


# ============ Attachment 索引验证函数 ============


def generate_expected_attachment_chunk_id(
    row: Dict[str, Any],
    chunking_version: str,
    chunk_idx: int = 0,
    namespace: str = CHUNK_ID_NAMESPACE,
) -> str:
    """
    根据 attachment 记录生成预期的 chunk_id
    
    使用 step3_chunking.generate_chunk_id 保持一致性：
    - 格式: <namespace>:logbook:attachment.<attachment_id>:<sha256_prefix>:<chunking_version>:<chunk_idx>
    
    Args:
        row: attachments 记录
        chunking_version: 分块版本
        chunk_idx: 分块索引（默认 0，检查第一个 chunk）
        namespace: 命名空间（默认 CHUNK_ID_NAMESPACE）
    
    Returns:
        预期的 chunk_id
    """
    attachment_id = row.get("attachment_id", 0)
    sha256 = row.get("sha256", "") or "unknown"
    
    # attachment 使用 source_type="logbook", source_id="attachment:<id>"
    # generate_chunk_id 内部会将冒号替换为点
    return generate_chunk_id(
        source_type="logbook",
        source_id=f"attachment:{attachment_id}",
        sha256=sha256,
        chunk_idx=chunk_idx,
        namespace=namespace,
        chunking_version=chunking_version,
    )


def check_attachment_index_existence(
    row: Dict[str, Any],
    index_backend: "IndexBackend",
    chunking_version: str,
    max_probe_chunks: int = 5,
) -> List[ConsistencyIssue]:
    """
    检查索引中是否存在预期的 attachment chunk
    
    探测策略：
    1. 首先尝试 chunk_idx=0
    2. 如果不存在，尝试调用 count_by_source 查询总数
    3. 如果 count_by_source 不支持，循环探测少量 chunk_idx
    4. 只有全部探测失败才报告 attachment_index_missing
    
    Args:
        row: attachments 记录
        index_backend: 索引后端实例
        chunking_version: 分块版本
        max_probe_chunks: 最大探测 chunk 数量（默认 5）
    
    Returns:
        ConsistencyIssue 列表
    """
    issues = []
    
    # 仅检查已标记为已索引的记录（chunking_version 匹配）
    current_version = row.get("chunking_version")
    if current_version != chunking_version:
        return []  # 未索引的记录由 check_attachment_missing_index 处理
    
    attachment_id = row.get("attachment_id", 0)
    source_type = "logbook"
    source_id = f"attachment:{attachment_id}"
    
    try:
        # 策略 1: 先尝试 chunk_idx=0
        expected_chunk_id_0 = generate_expected_attachment_chunk_id(row, chunking_version, chunk_idx=0)
        exists_result = index_backend.exists([expected_chunk_id_0])
        
        if exists_result.get(expected_chunk_id_0, False):
            # chunk_idx=0 存在，通过检查
            return []
        
        # 策略 2: 尝试 count_by_source
        try:
            count = index_backend.count_by_source(source_type, source_id)
            if count > 0:
                # 存在 chunk，通过检查
                return []
            elif count == 0:
                # 明确返回 0，说明没有 chunk
                issues.append(ConsistencyIssue(
                    issue_type="attachment_index_missing",
                    blob_id=attachment_id,
                    source_type=row.get("kind", "attachment"),
                    source_id=source_id,
                    uri=row.get("uri"),
                    evidence_uri=build_attachment_evidence_uri(attachment_id, row.get("sha256", "")),
                    sha256=row.get("sha256"),
                    chunking_version=current_version,
                    project_key=row.get("project_key"),
                    details=f"索引中不存在该 attachment 的任何 chunk (count_by_source=0), 预期 chunk_id 前缀: {expected_chunk_id_0.rsplit(':', 1)[0]}",
                ))
                return issues
            # count == -1 表示不支持，继续下一个策略
        except Exception as e:
            logger.debug(f"count_by_source 调用失败 source_type={source_type}, source_id={source_id}: {e}")
        
        # 策略 3: 循环探测少量 chunk_idx
        chunk_ids_to_probe = [
            generate_expected_attachment_chunk_id(row, chunking_version, chunk_idx=i)
            for i in range(1, max_probe_chunks)
        ]
        
        if chunk_ids_to_probe:
            exists_result = index_backend.exists(chunk_ids_to_probe)
            for chunk_id in chunk_ids_to_probe:
                if exists_result.get(chunk_id, False):
                    # 找到存在的 chunk，通过检查
                    return []
        
        # 全部探测失败，报告 attachment_index_missing
        issues.append(ConsistencyIssue(
            issue_type="attachment_index_missing",
            blob_id=attachment_id,
            source_type=row.get("kind", "attachment"),
            source_id=source_id,
            uri=row.get("uri"),
            evidence_uri=build_attachment_evidence_uri(attachment_id, row.get("sha256", "")),
            sha256=row.get("sha256"),
            chunking_version=current_version,
            project_key=row.get("project_key"),
            details=f"索引中不存在预期的 chunk (探测 chunk_idx 0~{max_probe_chunks-1}): {expected_chunk_id_0}",
        ))
        
    except Exception as e:
        logger.debug(f"attachment 索引检查失败 attachment_id={attachment_id}: {e}")
    
    return issues


def check_attachment_index_metadata(
    row: Dict[str, Any],
    index_backend: "IndexBackend",
    chunking_version: str,
    max_probe_chunks: int = 5,
) -> List[ConsistencyIssue]:
    """
    检查索引中 attachment chunk 的元数据是否与数据库一致
    
    验证字段：
    - artifact_uri: 应为 memory://attachments/<attachment_id>/<sha256>
    - sha256: 应与 DB 记录一致
    - source_id: 应为 attachment:<attachment_id>
    
    Args:
        row: attachments 记录
        index_backend: 索引后端实例
        chunking_version: 分块版本
        max_probe_chunks: 最大探测 chunk 数量（默认 5）
    
    Returns:
        ConsistencyIssue 列表
    """
    issues = []
    
    # 仅检查已标记为已索引的记录
    current_version = row.get("chunking_version")
    if current_version != chunking_version:
        return []
    
    attachment_id = row.get("attachment_id", 0)
    expected_sha256 = row.get("sha256", "")
    expected_evidence_uri = build_attachment_evidence_uri(attachment_id, expected_sha256)
    expected_source_id = f"attachment:{attachment_id}"
    
    try:
        # 探测找到第一个存在的 chunk
        found_chunk_id = None
        chunk_ids_to_probe = [
            generate_expected_attachment_chunk_id(row, chunking_version, chunk_idx=i)
            for i in range(max_probe_chunks)
        ]
        
        exists_result = index_backend.exists(chunk_ids_to_probe)
        for chunk_id in chunk_ids_to_probe:
            if exists_result.get(chunk_id, False):
                found_chunk_id = chunk_id
                break
        
        if not found_chunk_id:
            return []  # 不存在的情况由 check_attachment_index_existence 处理
        
        # 获取索引中的元数据
        metadata_result = index_backend.get_chunk_metadata([found_chunk_id])
        chunk_meta = metadata_result.get(found_chunk_id)
        
        if not chunk_meta:
            return []  # 元数据获取失败，跳过
        
        # 验证 artifact_uri
        indexed_artifact_uri = chunk_meta.get("artifact_uri", "")
        if indexed_artifact_uri and expected_evidence_uri:
            if indexed_artifact_uri.lower() != expected_evidence_uri.lower():
                issues.append(ConsistencyIssue(
                    issue_type="attachment_index_metadata_mismatch",
                    blob_id=attachment_id,
                    source_type=row.get("kind", "attachment"),
                    source_id=expected_source_id,
                    uri=row.get("uri"),
                    evidence_uri=expected_evidence_uri,
                    sha256=expected_sha256,
                    chunking_version=current_version,
                    project_key=row.get("project_key"),
                    details=f"artifact_uri 不匹配: 期望={expected_evidence_uri}, 索引={indexed_artifact_uri}",
                ))
        
        # 验证 SHA256
        indexed_sha256 = chunk_meta.get("sha256", "")
        if expected_sha256 and indexed_sha256:
            sha256_prefix_len = 12
            expected_prefix = expected_sha256[:sha256_prefix_len].lower()
            indexed_prefix = indexed_sha256[:sha256_prefix_len].lower() if len(indexed_sha256) >= sha256_prefix_len else indexed_sha256.lower()
            
            if not expected_prefix.startswith(indexed_prefix) and not indexed_prefix.startswith(expected_prefix):
                if expected_sha256.lower() != indexed_sha256.lower():
                    issues.append(ConsistencyIssue(
                        issue_type="attachment_index_metadata_mismatch",
                        blob_id=attachment_id,
                        source_type=row.get("kind", "attachment"),
                        source_id=expected_source_id,
                        uri=row.get("uri"),
                        evidence_uri=expected_evidence_uri,
                        sha256=expected_sha256,
                        chunking_version=current_version,
                        project_key=row.get("project_key"),
                        details=f"SHA256 不匹配: DB={expected_sha256[:16]}..., Index={indexed_sha256[:16] if indexed_sha256 else '(空)'}...",
                    ))
        
        # 验证 source_id
        indexed_source_id = chunk_meta.get("source_id", "")
        # 还原 source_id 的冒号（如果索引中存储的是替换后的格式）
        indexed_source_id_normalized = indexed_source_id.replace(".", ":") if "." in indexed_source_id and ":" not in indexed_source_id else indexed_source_id
        if indexed_source_id:
            if expected_source_id != indexed_source_id and expected_source_id != indexed_source_id_normalized:
                issues.append(ConsistencyIssue(
                    issue_type="attachment_index_metadata_mismatch",
                    blob_id=attachment_id,
                    source_type=row.get("kind", "attachment"),
                    source_id=expected_source_id,
                    uri=row.get("uri"),
                    evidence_uri=expected_evidence_uri,
                    sha256=expected_sha256,
                    chunking_version=current_version,
                    project_key=row.get("project_key"),
                    details=f"source_id 不匹配: 期望={expected_source_id}, Index={indexed_source_id}",
                ))
            
    except Exception as e:
        logger.debug(f"attachment 索引元数据检查失败 attachment_id={attachment_id}: {e}")
    
    return issues


def run_attachment_index_checks(
    rows: List[Dict[str, Any]],
    index_backend: "IndexBackend",
    chunking_version: str,
    result: "ConsistencyCheckResult",
    sample_size: int = 100,
) -> None:
    """
    对 attachment 记录进行索引一致性检查（抽样）
    
    Args:
        rows: attachments 记录列表
        index_backend: 索引后端实例
        chunking_version: 分块版本
        result: 检查结果对象（会被原地修改）
        sample_size: 抽样大小
    """
    if not INDEX_BACKEND_AVAILABLE or index_backend is None:
        logger.warning("IndexBackend 不可用，跳过 attachment 索引一致性检查")
        return
    
    # 筛选已索引的记录
    indexed_rows = [r for r in rows if r.get("chunking_version") == chunking_version]
    
    if not indexed_rows:
        logger.info("没有已索引的 attachment 记录，跳过索引检查")
        return
    
    # 抽样
    if len(indexed_rows) > sample_size:
        check_rows = random.sample(indexed_rows, sample_size)
        logger.info(f"Attachment 索引检查: 从 {len(indexed_rows)} 条已索引记录中抽样 {sample_size} 条")
    else:
        check_rows = indexed_rows
        logger.info(f"Attachment 索引检查: 检查全部 {len(check_rows)} 条已索引记录")
    
    # 执行检查
    for row in check_rows:
        # 检查存在性
        for issue in check_attachment_index_existence(row, index_backend, chunking_version):
            result.add_issue(issue)
        
        # 检查元数据一致性
        for issue in check_attachment_index_metadata(row, index_backend, chunking_version):
            result.add_issue(issue)


# ============ Docs 一致性检查函数 ============


def build_docs_evidence_uri(rel_path: str, sha256: str) -> str:
    """
    构建 docs 的 canonical evidence URI（不依赖 DB 存储）
    
    格式: memory://docs/<rel_path>/<sha256>
    
    Args:
        rel_path: 相对路径
        sha256: 内容 SHA256 哈希
    
    Returns:
        Canonical evidence URI
    """
    sha256_norm = sha256.strip().lower() if sha256 else ""
    rel_path_norm = rel_path.replace("\\", "/").strip("/")
    return f"memory://docs/{rel_path_norm}/{sha256_norm}"


def check_docs_file_readable(
    doc_info: Dict[str, Any],
    docs_root: str,
) -> Optional[ConsistencyIssue]:
    """
    检查文档文件是否可读
    
    Args:
        doc_info: 文档信息字典
        docs_root: 文档根目录
    
    Returns:
        ConsistencyIssue 如果有问题，否则 None
    """
    from pathlib import Path
    
    rel_path = doc_info.get("rel_path", "")
    full_path = Path(docs_root) / rel_path
    
    if not full_path.exists():
        return ConsistencyIssue(
            issue_type="docs_unreadable",
            blob_id=0,
            source_type="docs",
            source_id=f"docs:{rel_path}",
            uri=str(full_path),
            evidence_uri=doc_info.get("artifact_uri"),
            sha256=doc_info.get("sha256"),
            chunking_version=None,
            project_key=None,
            details=f"文档文件不存在: {full_path}",
        )
    
    if not full_path.is_file():
        return ConsistencyIssue(
            issue_type="docs_unreadable",
            blob_id=0,
            source_type="docs",
            source_id=f"docs:{rel_path}",
            uri=str(full_path),
            evidence_uri=doc_info.get("artifact_uri"),
            sha256=doc_info.get("sha256"),
            chunking_version=None,
            project_key=None,
            details=f"路径不是文件: {full_path}",
        )
    
    try:
        full_path.read_bytes()
    except OSError as e:
        return ConsistencyIssue(
            issue_type="docs_unreadable",
            blob_id=0,
            source_type="docs",
            source_id=f"docs:{rel_path}",
            uri=str(full_path),
            evidence_uri=doc_info.get("artifact_uri"),
            sha256=doc_info.get("sha256"),
            chunking_version=None,
            project_key=None,
            details=f"无法读取文档文件: {e}",
        )
    
    return None


def check_docs_sha_mismatch(
    doc_info: Dict[str, Any],
    docs_root: str,
) -> Optional[ConsistencyIssue]:
    """
    检查文档 SHA256 是否匹配
    
    Args:
        doc_info: 文档信息字典
        docs_root: 文档根目录
    
    Returns:
        ConsistencyIssue 如果有问题，否则 None
    """
    from pathlib import Path
    
    rel_path = doc_info.get("rel_path", "")
    expected_sha256 = doc_info.get("sha256", "")
    full_path = Path(docs_root) / rel_path
    
    if not expected_sha256:
        return None  # 没有预期 SHA256 无法检查
    
    if not full_path.exists():
        return None  # 文件不存在由 check_docs_file_readable 处理
    
    try:
        content = full_path.read_bytes()
        actual_sha256 = hashlib.sha256(content).hexdigest()
        
        if actual_sha256.lower() != expected_sha256.lower():
            return ConsistencyIssue(
                issue_type="docs_sha_mismatch",
                blob_id=0,
                source_type="docs",
                source_id=f"docs:{rel_path}",
                uri=str(full_path),
                evidence_uri=doc_info.get("artifact_uri"),
                sha256=expected_sha256,
                chunking_version=None,
                project_key=None,
                details=f"SHA256 不匹配: 期望={expected_sha256[:16]}..., 实际={actual_sha256[:16]}...",
            )
    except OSError:
        pass  # 读取失败由 check_docs_file_readable 处理
    
    return None


def run_docs_consistency_check(
    docs_root: str,
    patterns: Optional[list] = None,
    exclude_patterns: Optional[list] = None,
    result: "ConsistencyCheckResult" = None,
    sample_size: int = 100,
) -> int:
    """
    执行文档一致性检查（抽样）
    
    扫描 docs_root 目录，检查文档文件的可读性和 SHA256 一致性。
    
    Args:
        docs_root: 文档根目录
        patterns: 包含的文件模式列表（默认 ["*.md", "*.txt"]）
        exclude_patterns: 排除的文件模式列表
        result: 检查结果对象（会被原地修改）
        sample_size: 抽样大小
    
    Returns:
        检查的文档数量
    """
    from pathlib import Path
    
    # 导入扫描函数
    try:
        from step3_seekdb_rag_hybrid.step3_readers import scan_docs_directory
    except ImportError:
        from step3_readers import scan_docs_directory
    
    docs_root_path = Path(docs_root)
    if not docs_root_path.exists():
        logger.warning(f"文档目录不存在: {docs_root}")
        return 0
    
    # 扫描文档
    logger.info(f"扫描文档目录: {docs_root}")
    doc_files = scan_docs_directory(docs_root, patterns, exclude_patterns)
    
    if not doc_files:
        logger.info("没有发现文档文件，跳过 docs 检查")
        return 0
    
    # 抽样
    if len(doc_files) > sample_size:
        check_docs = random.sample(doc_files, sample_size)
        logger.info(f"Docs 检查: 从 {len(doc_files)} 个文档中抽样 {sample_size} 个")
    else:
        check_docs = doc_files
        logger.info(f"Docs 检查: 检查全部 {len(check_docs)} 个文档")
    
    # 执行检查
    docs_checked = 0
    for doc_info in check_docs:
        docs_checked += 1
        
        # 检查文件可读性
        issue = check_docs_file_readable(doc_info, docs_root)
        if issue:
            result.add_issue(issue)
            continue  # 不可读的文件跳过 SHA256 检查
        
        # 检查 SHA256
        issue = check_docs_sha_mismatch(doc_info, docs_root)
        if issue:
            result.add_issue(issue)
    
    return docs_checked


def run_consistency_check(
    conn: psycopg.Connection,
    chunking_version: str,
    project_key: Optional[str] = None,
    sample_ratio: Optional[float] = None,
    limit: Optional[int] = None,
    check_artifacts: bool = True,
    verify_sha256: bool = True,
    check_scheme: bool = True,
    check_attachments: bool = True,
    check_docs: bool = False,
    docs_root: Optional[str] = None,
    docs_patterns: Optional[list] = None,
    docs_exclude: Optional[list] = None,
    docs_sample_size: int = 100,
    index_backend: Optional["IndexBackend"] = None,
    index_sample_size: int = 100,
) -> ConsistencyCheckResult:
    """
    执行完整的一致性检查
    
    Args:
        conn: 数据库连接
        chunking_version: 要检查的分块版本
        project_key: 项目标识（可选）
        sample_ratio: 抽样比例（0.0-1.0）
        limit: 最大记录数
        check_artifacts: 是否检查制品文件存在性
        verify_sha256: 是否验证 SHA256
        check_scheme: 是否检查 scheme 规范
        check_attachments: 是否检查 attachments
        check_docs: 是否检查 docs（本地文档）
        docs_root: 文档根目录（check_docs=True 时必需）
        docs_patterns: 文档文件模式列表（默认 ["*.md", "*.txt"]）
        docs_exclude: 排除的文件模式列表
        docs_sample_size: docs 检查抽样大小（默认 100）
        index_backend: 可选的索引后端实例，用于验证索引一致性
        index_sample_size: 索引检查抽样大小（默认 100）
    
    Returns:
        ConsistencyCheckResult 检查结果
        
    Note:
        - 原有的制品检查逻辑（check_artifacts, verify_sha256）作为降级路径保持不变
        - 如果提供了 index_backend，会额外进行索引一致性检查
        - 索引检查包括: index_missing, index_version_mismatch, index_metadata_mismatch
        - docs 检查包括: docs_unreadable, docs_sha_mismatch
    """
    result = ConsistencyCheckResult(
        chunking_version=chunking_version,
        project_key=project_key,
        sample_ratio=sample_ratio,
        limit=limit,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    
    start_time = datetime.now(timezone.utc)
    
    # 确定查询限制
    query_limit = limit or 100000  # 默认最大 10 万条
    
    # 如果使用抽样，先获取总数
    if sample_ratio is not None and 0 < sample_ratio < 1.0:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(SQL_COUNT_PATCH_BLOBS, {"project_key": project_key})
            total_count = cur.fetchone()["total"]
            query_limit = int(total_count * sample_ratio) + 1
            logger.info(f"抽样模式: 总数={total_count}, 抽样比例={sample_ratio}, 预计检查={query_limit}")
    
    # 获取待检查的记录
    logger.info(f"开始获取 patch_blobs 记录 (版本={chunking_version}, 项目={project_key or '全部'})")
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(SQL_FETCH_PATCH_BLOBS_FULL, {
            "chunking_version": chunking_version,
            "project_key": project_key,
            "limit": query_limit,
        })
        
        rows = cur.fetchall()
    
    logger.info(f"获取到 {len(rows)} 条记录")
    
    # 如果使用抽样且行数超过目标，随机抽样
    if sample_ratio is not None and 0 < sample_ratio < 1.0 and len(rows) > query_limit:
        rows = random.sample(rows, query_limit)
        logger.info(f"随机抽样 {query_limit} 条记录")
    
    # 遍历检查
    for i, row in enumerate(rows):
        if i > 0 and i % 1000 == 0:
            logger.info(f"已检查 {i}/{len(rows)} 条记录, 发现 {result.total_issues} 个问题")
        
        result.total_checked += 1
        
        # 检查 1: 缺少索引
        issue = check_missing_index(row, chunking_version)
        if issue:
            result.add_issue(issue)
        
        # 检查 2: 缺少 evidence_uri
        issue = check_missing_evidence_uri(row)
        if issue:
            result.add_issue(issue)
        
        # 检查 3: 制品不可读
        if check_artifacts:
            issue = check_unreadable_artifact(row)
            if issue:
                result.add_issue(issue)
            else:
                # 检查 4: SHA256 不匹配（只有制品可读时才检查）
                if verify_sha256:
                    issue = check_sha_mismatch(row)
                    if issue:
                        result.add_issue(issue)
    
    # 检查 5-7: 索引一致性检查（可选，需要 IndexBackend）
    if index_backend is not None:
        logger.info("开始执行索引一致性检查...")
        run_index_checks(
            rows=rows,
            index_backend=index_backend,
            chunking_version=chunking_version,
            result=result,
            sample_size=index_sample_size,
        )
    elif INDEX_BACKEND_AVAILABLE:
        logger.debug("未提供 IndexBackend 实例，跳过索引一致性检查")
    
    # 检查 8: docs（本地文档）一致性检查（可选）
    if check_docs and docs_root:
        logger.info("开始执行 docs 一致性检查...")
        docs_checked = run_docs_consistency_check(
            docs_root=docs_root,
            patterns=docs_patterns,
            exclude_patterns=docs_exclude,
            result=result,
            sample_size=docs_sample_size,
        )
        result.total_checked += docs_checked
        logger.info(f"Docs 检查完成: 检查了 {docs_checked} 个文档")
    elif check_docs and not docs_root:
        logger.warning("check_docs=True 但未提供 docs_root，跳过 docs 检查")
    
    # 计算耗时
    end_time = datetime.now(timezone.utc)
    result.completed_at = end_time.isoformat()
    result.duration_seconds = (end_time - start_time).total_seconds()
    
    logger.info(f"检查完成: 共检查 {result.total_checked} 条, 发现 {result.total_issues} 个问题")
    
    return result


def log_to_logbook(
    conn: psycopg.Connection,
    item_id: int,
    result: ConsistencyCheckResult,
    actor_user_id: Optional[str] = None,
) -> int:
    """
    将检查结果记录到 logbook.events
    
    Args:
        conn: 数据库连接
        item_id: 关联的 item_id
        result: 检查结果
        actor_user_id: 操作者用户 ID
    
    Returns:
        event_id
    """
    payload_json = json.dumps(result.to_dict(), default=str)
    
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO logbook.events
                (item_id, event_type, status_from, status_to, payload_json, actor_user_id, source)
            VALUES (%s, %s, NULL, NULL, %s, %s, %s)
            RETURNING event_id
            """,
            (item_id, "seek_consistency_check", payload_json, actor_user_id, "seek_consistency_check"),
        )
        event_id = cur.fetchone()[0]
    
    logger.info(f"检查结果已记录到 logbook.events, event_id={event_id}")
    
    return event_id


def add_to_attachments(
    conn: psycopg.Connection,
    item_id: int,
    result: ConsistencyCheckResult,
) -> int:
    """
    将检查结果作为附件保存到 logbook.attachments
    
    Args:
        conn: 数据库连接
        item_id: 关联的 item_id
        result: 检查结果
    
    Returns:
        attachment_id
    """
    # 将结果转换为 JSON
    result_json = json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str)
    result_bytes = result_json.encode("utf-8")
    sha256 = hashlib.sha256(result_bytes).hexdigest()
    size_bytes = len(result_bytes)
    
    # 生成时间戳作为文件名的一部分
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    
    # 写入制品存储
    from artifacts import write_text_artifact
    
    uri = f"reports/seek_consistency_check/{result.chunking_version}_{timestamp}.json"
    artifact_result = write_text_artifact(uri, result_json)
    
    # 记录附件
    meta_json = json.dumps({
        "report_type": "seek_consistency_check",
        "chunking_version": result.chunking_version,
        "project_key": result.project_key,
        "total_checked": result.total_checked,
        "total_issues": result.total_issues,
    })
    
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO logbook.attachments
                (item_id, kind, uri, sha256, size_bytes, meta_json)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING attachment_id
            """,
            (item_id, "report", artifact_result["uri"], artifact_result["sha256"], 
             artifact_result["size_bytes"], meta_json),
        )
        attachment_id = cur.fetchone()[0]
    
    logger.info(f"检查报告已保存为附件, attachment_id={attachment_id}, uri={uri}")
    
    return attachment_id


# ============ 报告输出 ============


def print_report(result: ConsistencyCheckResult):
    """打印检查报告（文本格式）"""
    print("\n" + "=" * 60)
    print("Step3 索引一致性检查报告")
    print("=" * 60)
    
    print(f"\n【检查参数】")
    print(f"  分块版本: {result.chunking_version}")
    print(f"  项目标识: {result.project_key or '(全部)'}")
    if result.sample_ratio:
        print(f"  抽样比例: {result.sample_ratio * 100:.1f}%")
    if result.limit:
        print(f"  最大记录数: {result.limit}")
    
    print(f"\n【检查统计】")
    print(f"  检查记录数: {result.total_checked}")
    print(f"  发现问题数: {result.total_issues}")
    print(f"  耗时: {result.duration_seconds:.2f} 秒")
    
    print(f"\n【问题分类统计】")
    if not result.issue_counts:
        print("  无问题发现")
    else:
        for issue_type, count in sorted(result.issue_counts.items()):
            print(f"  - {issue_type}: {count}")
    
    # 打印每种类型的示例
    for issue_type, samples in result.issue_samples.items():
        print(f"\n【{issue_type} 示例】(共 {result.issue_counts.get(issue_type, 0)} 个)")
        for i, sample in enumerate(samples[:5], 1):  # 只显示前 5 个
            print(f"  {i}. blob_id={sample['blob_id']}, source_id={sample['source_id']}")
            if sample.get('details'):
                print(f"     {sample['details']}")
        if len(samples) > 5:
            print(f"  ... 还有 {len(samples) - 5} 个示例")
    
    print("\n" + "=" * 60)
    if result.has_issues:
        print(f"总计发现 {result.total_issues} 个问题，建议进行修复")
    else:
        print("未发现问题，索引一致性良好")
    print("=" * 60 + "\n")


# ============ 辅助函数 ============


def _create_index_backend(backend_type: Optional[str], args: argparse.Namespace) -> Optional["IndexBackend"]:
    """
    根据类型创建索引后端实例（复用 step3_backend_factory）
    
    Args:
        backend_type: 后端类型 (pgvector/seekdb) 或 None（从环境变量检测）
        args: 命令行参数（用于透传给 backend_factory）
    
    Returns:
        IndexBackend 实例，或 None 如果创建失败
    """
    if not INDEX_BACKEND_AVAILABLE:
        logger.warning("IndexBackend 模块不可用")
        return None
    
    if not BACKEND_FACTORY_AVAILABLE:
        logger.warning("step3_backend_factory 模块不可用，无法创建索引后端")
        return None
    
    try:
        # 如果指定了 backend_type，通过 --backend 参数传递
        if backend_type:
            # 规范化类型名（支持 pgvector/seekdb 的别名）
            normalized_type = backend_type.lower()
            if normalized_type in ("pg", "postgres", "postgresql"):
                normalized_type = "pgvector"
            elif normalized_type in ("seek",):
                normalized_type = "seekdb"
            
            # 临时设置 args.backend（如果尚未设置）
            if not hasattr(args, 'backend') or not args.backend:
                args.backend = normalized_type
        
        # 使用工厂函数创建后端
        backend = create_backend_from_args(args)
        return backend
        
    except Exception as e:
        logger.warning(f"创建索引后端失败: {e}")
        return None


# ============ CLI 部分 ============


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="Step3 索引一致性检查工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # Makefile 入口（推荐）
    make step3-check                                        # 检查默认版本
    make step3-check CHUNKING_VERSION=v1-2026-01            # 指定版本
    make step3-check PROJECT_KEY=myproject                  # 检查特定项目
    make step3-check SAMPLE_RATIO=0.1                       # 抽样 10%% 检查
    make step3-check LIMIT=1000 JSON_OUTPUT=1               # 限制检查数并 JSON 输出
    make step3-check SKIP_ARTIFACTS=1                       # 仅检查索引状态
    make step3-check CHECK_INDEX=1 INDEX_BACKEND=pgvector   # 检查索引一致性
    make step3-check SKIP_ATTACHMENTS=1                     # 跳过 attachments 检查
    make step3-check SKIP_SCHEME_CHECK=1                    # 跳过 scheme 校验

    # 直接调用
    python -m seek_consistency_check --chunking-version v1-2026-01
    python -m seek_consistency_check --chunking-version v1-2026-01 --project-key myproject
    python -m seek_consistency_check --chunking-version v1-2026-01 --sample-ratio 0.1
    python -m seek_consistency_check --chunking-version v1-2026-01 --limit 1000
    python -m seek_consistency_check --chunking-version v1-2026-01 --skip-artifacts
    python -m seek_consistency_check --chunking-version v1-2026-01 --skip-attachments
    python -m seek_consistency_check --chunking-version v1-2026-01 --skip-scheme-check
    python -m seek_consistency_check --chunking-version v1-2026-01 --check-index --index-backend pgvector
    python -m seek_consistency_check --chunking-version v1-2026-01 --check-index --backend seekdb
    python -m seek_consistency_check --chunking-version v1-2026-01 --log-to-logbook --item-id 123
        """,
    )
    
    add_config_argument(parser)
    
    # 必需参数
    parser.add_argument(
        "--chunking-version",
        type=str,
        default=CHUNKING_VERSION,
        help=f"要检查的分块版本号 (default: {CHUNKING_VERSION})",
    )
    
    # 筛选参数
    parser.add_argument(
        "--project-key",
        type=str,
        default=None,
        help="按项目标识筛选",
    )
    
    # 抽样参数（互斥组）
    sample_group = parser.add_mutually_exclusive_group()
    sample_group.add_argument(
        "--sample-ratio",
        type=float,
        default=None,
        help="抽样比例 (0.0-1.0)，与 --limit 互斥",
    )
    sample_group.add_argument(
        "--limit",
        type=int,
        default=None,
        help="最大检查记录数，与 --sample-ratio 互斥",
    )
    
    # 检查选项
    parser.add_argument(
        "--skip-artifacts",
        action="store_true",
        help="跳过制品文件检查（仅检查索引状态）",
    )
    parser.add_argument(
        "--skip-sha256",
        action="store_true",
        help="跳过 SHA256 验证",
    )
    parser.add_argument(
        "--skip-scheme-check",
        action="store_true",
        help="跳过 evidence_uri scheme 校验",
    )
    parser.add_argument(
        "--skip-attachments",
        action="store_true",
        help="跳过 logbook.attachments 检查",
    )

    # Docs 检查选项
    parser.add_argument(
        "--check-docs",
        action="store_true",
        help="启用本地文档（docs）一致性检查",
    )
    parser.add_argument(
        "--docs-root",
        type=str,
        default=None,
        help="文档根目录（--check-docs 时必需）",
    )
    parser.add_argument(
        "--docs-patterns",
        type=str,
        default="*.md,*.txt",
        help="文档文件模式，逗号分隔（默认 '*.md,*.txt'）",
    )
    parser.add_argument(
        "--docs-exclude",
        type=str,
        default=None,
        help="排除的文件模式，逗号分隔",
    )
    parser.add_argument(
        "--docs-sample-size",
        type=int,
        default=100,
        help="Docs 检查抽样大小 (默认: 100)",
    )
    
    # 索引检查选项（需要 IndexBackend）
    parser.add_argument(
        "--check-index",
        action="store_true",
        help="启用索引一致性检查（需要配置 IndexBackend）",
    )
    parser.add_argument(
        "--index-sample-size",
        type=int,
        default=100,
        help="索引检查抽样大小 (default: 100)",
    )
    parser.add_argument(
        "--index-backend",
        type=str,
        default=None,
        choices=["pgvector", "seekdb"],
        help="索引后端类型 (默认从 STEP3_INDEX_BACKEND 环境变量读取，需要 --check-index)",
    )
    
    # 添加后端工厂选项（如果可用）
    if BACKEND_FACTORY_AVAILABLE and add_backend_arguments:
        add_backend_arguments(parser)
    
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
    
    # Logbook 集成选项
    parser.add_argument(
        "--log-to-logbook",
        action="store_true",
        help="将检查结果记录到 logbook.events",
    )
    parser.add_argument(
        "--save-attachment",
        action="store_true",
        help="将检查报告保存为 logbook.attachments",
    )
    parser.add_argument(
        "--item-id",
        type=int,
        default=None,
        help="用于记录的 item_id（需要 --log-to-logbook 或 --save-attachment）",
    )
    parser.add_argument(
        "--actor",
        type=str,
        default=None,
        help="操作者用户 ID（用于 logbook 记录）",
    )
    
    return parser.parse_args()


def main() -> int:
    """主入口"""
    args = parse_args()
    
    # 设置日志级别
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # 验证参数
    if args.sample_ratio is not None:
        if not (0 < args.sample_ratio <= 1.0):
            logger.error("--sample-ratio 必须在 (0.0, 1.0] 范围内")
            return 1
    
    if (args.log_to_logbook or args.save_attachment) and not args.item_id:
        logger.error("使用 --log-to-logbook 或 --save-attachment 时必须指定 --item-id")
        return 1
    
    # 检查索引后端参数
    if args.check_index and not INDEX_BACKEND_AVAILABLE:
        logger.error("IndexBackend 模块不可用，无法启用索引检查")
        return 1
    
    try:
        # 加载配置
        config = get_config(args.config_path)
        config.load()
        
        # 获取数据库连接
        conn = get_connection(config=config)
        
        # 初始化索引后端（可选）
        index_backend = None
        if args.check_index:
            # 使用 _create_index_backend（内部复用 step3_backend_factory）
            index_backend = _create_index_backend(args.index_backend, args)
            
            if index_backend:
                logger.info(f"已初始化索引后端: {index_backend.backend_name}")
            else:
                logger.warning("无法初始化索引后端，将跳过索引检查")
        
        try:
            # 执行检查
            logger.info(f"开始执行 Step3 索引一致性检查...")
            logger.info(f"  分块版本: {args.chunking_version}")
            logger.info(f"  项目标识: {args.project_key or '(全部)'}")
            if index_backend:
                logger.info(f"  索引检查: 已启用 (抽样 {args.index_sample_size} 条)")
            
            # 解析 docs 相关参数
            docs_patterns = args.docs_patterns.split(",") if args.docs_patterns else None
            docs_exclude = args.docs_exclude.split(",") if args.docs_exclude else None

            result = run_consistency_check(
                conn=conn,
                chunking_version=args.chunking_version,
                project_key=args.project_key,
                sample_ratio=args.sample_ratio,
                limit=args.limit,
                check_artifacts=not args.skip_artifacts,
                verify_sha256=not args.skip_sha256,
                check_scheme=not args.skip_scheme_check,
                check_attachments=not args.skip_attachments,
                check_docs=args.check_docs,
                docs_root=args.docs_root,
                docs_patterns=docs_patterns,
                docs_exclude=docs_exclude,
                docs_sample_size=args.docs_sample_size,
                index_backend=index_backend,
                index_sample_size=args.index_sample_size,
            )
            
            # 记录到 logbook
            if args.log_to_logbook:
                log_to_logbook(
                    conn=conn,
                    item_id=args.item_id,
                    result=result,
                    actor_user_id=args.actor,
                )
            
            # 保存为附件
            if args.save_attachment:
                add_to_attachments(
                    conn=conn,
                    item_id=args.item_id,
                    result=result,
                )
            
            # 提交事务
            conn.commit()
            
            # 输出结果
            if args.json:
                output = result.to_dict()
                print(json.dumps(output, default=str, ensure_ascii=False, indent=2))
            else:
                print_report(result)
            
            return 0 if not result.has_issues else 1
            
        except psycopg.Error as e:
            conn.rollback()
            raise DatabaseError(
                f"数据库操作失败: {e}",
                {"error": str(e)},
            )
        finally:
            # 关闭索引后端
            if index_backend is not None:
                try:
                    index_backend.close()
                except Exception:
                    pass
            conn.close()
    
    except EngramError as e:
        if args.json:
            print(json.dumps(e.to_dict(), default=str, ensure_ascii=False))
        else:
            logger.error(f"{e.error_type}: {e.message}")
            if args.verbose and e.details:
                logger.error(f"详情: {e.details}")
        return e.exit_code
    
    except Exception as e:
        logger.exception(f"未预期的错误: {e}")
        if args.json:
            print(json.dumps({
                "error": True,
                "type": "UNEXPECTED_ERROR",
                "message": str(e),
            }, default=str, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
