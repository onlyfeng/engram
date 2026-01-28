#!/usr/bin/env python3
"""
seek_query.py - Step3 证据检索工具

从索引后端检索与查询相关的证据，输出 Evidence Packet 格式。

功能：
1. 语义检索 - 基于 query_text 进行向量检索
2. 过滤检索 - 支持 project_key/source_type/owner 等过滤条件
3. 批量查询 - 支持从文件读取多个查询

输入参数：
    --query: 查询文本
    --query-file: 从文件读取查询（每行一个）
    --query-set: 使用内置查询集（如 nightly_default）
    --project-key: 项目标识过滤
    --source-type: 来源类型过滤（svn/git/logbook）
    --owner: 所有者过滤
    --top-k: 返回结果数量

输出：
    - Evidence Packet 格式的检索结果
    - 支持 --json 输出便于流水线解析

使用:
    # Makefile 入口（推荐）
    make step3-query QUERY='修复登录页面 XSS 漏洞'
    make step3-query QUERY='数据库优化' PROJECT_KEY=webapp SOURCE_TYPE=git
    make step3-query QUERY='内存泄漏修复' JSON_OUTPUT=1
    make step3-query QUERY_FILE=queries.txt JSON_OUTPUT=1

    # 直接调用（在 apps/step3_seekdb_rag_hybrid 目录下）
    python -m seek_query --query "修复登录页面 XSS 漏洞"
    python -m seek_query --query "数据库连接池优化" --project-key webapp --source-type git
    python -m seek_query --query "内存泄漏修复" --json
    python -m seek_query --query-file queries.txt --json
"""

import argparse
import hashlib
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Callable

import psycopg

# 导入 Step1 模块
from engram_step1.config import add_config_argument, get_config
from engram_step1.db import get_connection
from engram_step1.errors import EngramError

# 导入 Step3 模块
from step3_seekdb_rag_hybrid.step3_chunking import (
    CHUNKING_VERSION,
    parse_chunk_id,
)
from step3_seekdb_rag_hybrid.embedding_provider import (
    EmbeddingProvider,
    EmbeddingModelInfo,
    EmbeddingError,
    get_embedding_provider,
    set_embedding_provider,
)
from step3_seekdb_rag_hybrid.index_backend import (
    IndexBackend,
    QueryRequest,
    QueryHit,
)
from step3_seekdb_rag_hybrid.step3_backend_factory import (
    add_backend_arguments,
    create_backend_from_args,
    create_backend_from_env,
    create_shadow_backend_for_read,
    get_backend_info,
    DualReadConfig,
    PGVectorConfig,
    DUAL_READ_STRATEGY_SHADOW_ONLY_COMPARE,
    # Shadow 就绪性校验
    ShadowReadinessResult,
    validate_shadow_readiness,
    format_shadow_readiness_report,
)
from step3_seekdb_rag_hybrid.dual_read_compare import (
    CompareThresholds,
    CompareMetrics,
    CompareDecision,
    CompareReport,
    ViolationDetail,
    RankingDriftMetrics,
    ScoreDriftMetrics,
    ThresholdsSource,
    THRESHOLD_SOURCE_DEFAULT,
    THRESHOLD_SOURCE_ENV,
    THRESHOLD_SOURCE_CLI,
    compute_ranking_drift,
    compute_score_drift,
    evaluate_with_report,
)
from step3_seekdb_rag_hybrid.collection_naming import (
    make_collection_id,
    parse_collection_id,
)
from step3_seekdb_rag_hybrid.active_collection import (
    get_active_collection,
    get_default_collection_id,
    resolve_collection_id,
)
from step3_seekdb_rag_hybrid.env_compat import get_bool

# 环境变量：是否自动初始化 pgvector 后端（默认开启）
# canonical: STEP3_PGVECTOR_AUTO_INIT，别名: STEP3_AUTO_INIT（已废弃，计划于 2026-Q3 移除）
# 布尔解析规则：支持 1/0/true/false/yes/no（不区分大小写）
PGVECTOR_AUTO_INIT = get_bool(
    "STEP3_PGVECTOR_AUTO_INIT",
    deprecated_aliases=["STEP3_AUTO_INIT"],
    default=True,
)

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ============ 内置查询集定义 ============
# 预定义的查询集，可通过 --query-set 参数调用
# 用于 CI/CD 流程中避免在 YAML 中维护查询数组
BUILTIN_QUERY_SETS: Dict[str, List[str]] = {
    # Nightly dual-read 一致性测试默认查询集
    "nightly_default": [
        "bug fix",
        "性能优化",
        "数据库连接",
        "内存泄漏",
        "安全漏洞修复",
    ],
}


# ============ 数据结构 ============


@dataclass
class QueryFilters:
    """查询过滤条件"""
    project_key: Optional[str] = None
    module: Optional[str] = None
    source_type: Optional[str] = None
    source_id: Optional[str] = None
    owner_user_id: Optional[str] = None
    time_range_start: Optional[str] = None
    time_range_end: Optional[str] = None

    def to_filter_dict(self) -> Dict[str, Any]:
        """
        转换为索引后端的过滤条件格式（Filter DSL）
        
        DSL 格式规范:
        - 简单字段使用直接值: {"project_key": "webapp"}
        - module 字段使用 $prefix 操作符: {"module": {"$prefix": "src/"}}
        - commit_ts 字段使用范围操作符: {"commit_ts": {"$gte": "...", "$lte": "..."}}
        
        返回的格式与 index_backend.base.build_filter_dsl 一致，
        可通过 index_backend.base.validate_filter_dsl 校验。
        """
        filters: Dict[str, Any] = {}
        
        # 简单等值字段
        if self.project_key:
            filters["project_key"] = self.project_key
        if self.source_type:
            filters["source_type"] = self.source_type
        if self.source_id:
            filters["source_id"] = self.source_id
        if self.owner_user_id:
            filters["owner_user_id"] = self.owner_user_id
        
        # module 字段: 使用 $prefix 操作符进行前缀匹配
        if self.module:
            filters["module"] = {"$prefix": self.module}
        
        # commit_ts 字段: 使用范围操作符 $gte/$lte
        if self.time_range_start or self.time_range_end:
            filters["commit_ts"] = {}
            if self.time_range_start:
                filters["commit_ts"]["$gte"] = self.time_range_start
            if self.time_range_end:
                filters["commit_ts"]["$lte"] = self.time_range_end
        
        return filters

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "project_key": self.project_key,
            "module": self.module,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "owner_user_id": self.owner_user_id,
            "time_range_start": self.time_range_start,
            "time_range_end": self.time_range_end,
        }


@dataclass
class RetrievalContext:
    """
    检索上下文信息
    
    包含检索过程中使用的后端配置、embedding 模型信息、hybrid 配置等。
    用于追溯和调试检索结果的可再现性。
    
    字段说明:
        - backend_name: 后端名称（pgvector/seekdb）
        - backend_config: 后端配置摘要（不含敏感信息如密码）
        - collection_id: resolved 后的最终 collection_id（冒号格式）
        - embedding_model_id: embedding 模型标识
        - embedding_dim: embedding 向量维度
        - embedding_normalize: embedding 是否归一化
        - hybrid_config: hybrid 检索配置（vector_weight/text_weight 等）
        - query_request: 查询请求参数（top_k/min_score/filters DSL）
    """
    # 后端信息
    backend_name: Optional[str] = None
    backend_config: Optional[Dict[str, Any]] = None
    collection_id: Optional[str] = None
    
    # Embedding 模型信息
    embedding_model_id: Optional[str] = None
    embedding_dim: Optional[int] = None
    embedding_normalize: Optional[bool] = None
    
    # Hybrid 检索配置
    hybrid_config: Optional[Dict[str, Any]] = None
    
    # 查询请求参数
    query_request: Optional[Dict[str, Any]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """
        转换为字典
        
        仅输出非 None 的字段，保持输出精简。
        """
        result: Dict[str, Any] = {}
        
        # 后端信息
        if self.backend_name is not None:
            result["backend_name"] = self.backend_name
        if self.backend_config is not None:
            result["backend_config"] = self.backend_config
        if self.collection_id is not None:
            result["collection_id"] = self.collection_id
        
        # Embedding 模型信息
        if self.embedding_model_id is not None or self.embedding_dim is not None or self.embedding_normalize is not None:
            result["embedding"] = {}
            if self.embedding_model_id is not None:
                result["embedding"]["model_id"] = self.embedding_model_id
            if self.embedding_dim is not None:
                result["embedding"]["dim"] = self.embedding_dim
            if self.embedding_normalize is not None:
                result["embedding"]["normalize"] = self.embedding_normalize
        
        # Hybrid 配置
        if self.hybrid_config is not None:
            result["hybrid_config"] = self.hybrid_config
        
        # 查询请求参数
        if self.query_request is not None:
            result["query_request"] = self.query_request
        
        return result


@dataclass
class EvidenceResult:
    """
    证据检索结果
    
    字段说明:
        - chunk_id: 分块唯一标识
        - chunk_idx: 分块在原文档中的索引
        - content: 分块完整内容
        - artifact_uri: 制品 URI（memory:// 格式）
        - evidence_uri: artifact_uri 的别名（兼容旧接口）
        - sha256: 内容哈希
        - source_id: 来源标识（如 repo_id:commit_sha）
        - source_type: 来源类型（svn/git/logbook）
        - excerpt: 内容摘要（最多 25 行或 2000 字）
        - relevance_score: 相似度分数（0.0-1.0）
        - metadata: 扩展元数据
    """
    chunk_id: str
    chunk_idx: int
    content: str
    artifact_uri: str
    sha256: str
    source_id: str
    source_type: str
    excerpt: str
    relevance_score: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def evidence_uri(self) -> str:
        """artifact_uri 的别名（兼容旧接口）"""
        return self.artifact_uri

    def to_evidence_dict(self, include_content: bool = True) -> Dict[str, Any]:
        """
        转换为 Evidence Packet 的 Evidence 条目格式
        
        Args:
            include_content: 是否包含完整 content 字段（packet 格式不含）
        
        Returns:
            Evidence 字典
        """
        result = {
            "chunk_id": self.chunk_id,
            "chunk_idx": self.chunk_idx,
            "artifact_uri": self.artifact_uri,
            "evidence_uri": self.artifact_uri,  # 兼容别名
            "sha256": self.sha256,
            "source_id": self.source_id,
            "source_type": self.source_type,
            "excerpt": self.excerpt,
            "relevance_score": self.relevance_score,
        }
        if include_content:
            result["content"] = self.content
        # 添加扩展元数据
        result["metadata"] = self.metadata
        return result

    @classmethod
    def from_index_result(cls, index_result: Dict[str, Any]) -> "EvidenceResult":
        """从索引后端返回的结果构建 EvidenceResult"""
        chunk_id = index_result.get("chunk_id", "")
        parsed = parse_chunk_id(chunk_id) if chunk_id else {}
        
        # artifact_uri 优先，兼容 evidence_uri
        artifact_uri = index_result.get("artifact_uri") or index_result.get("evidence_uri", "")

        return cls(
            chunk_id=chunk_id,
            chunk_idx=index_result.get("chunk_idx", parsed.get("chunk_idx", 0)),
            content=index_result.get("content", ""),
            artifact_uri=artifact_uri,
            sha256=index_result.get("sha256", ""),
            source_id=index_result.get("source_id", parsed.get("source_id", "")),
            source_type=index_result.get("source_type", parsed.get("source_type", "")),
            excerpt=index_result.get("excerpt", ""),
            relevance_score=index_result.get("relevance_score", 0.0),
            metadata={
                k: v for k, v in index_result.items()
                if k not in {
                    "chunk_id", "chunk_idx", "content", "artifact_uri", "evidence_uri",
                    "sha256", "source_id", "source_type", "excerpt", "relevance_score"
                }
            },
        )
    
    @classmethod
    def from_query_hit(cls, hit: "QueryHit") -> "EvidenceResult":
        """
        从 QueryHit 构建 EvidenceResult
        
        Args:
            hit: 索引后端返回的 QueryHit 对象
        
        Returns:
            EvidenceResult 实例
        """
        return cls(
            chunk_id=hit.chunk_id,
            chunk_idx=hit.chunk_idx,
            content=hit.content,
            artifact_uri=hit.artifact_uri,
            sha256=hit.sha256,
            source_id=hit.source_id,
            source_type=hit.source_type,
            excerpt=hit.excerpt,
            relevance_score=hit.score,
            metadata=hit.metadata,
        )


@dataclass
class QueryResult:
    """
    查询结果
    
    输出格式:
        - packet: Evidence Packet 精简格式，不含 content（用于 Agent 快速预览）
        - full: 完整格式，包含 content 和调试信息（用于验证和分析）
    """
    query: str
    filters: Optional[QueryFilters] = None
    top_k: int = 10
    evidences: List[EvidenceResult] = field(default_factory=list)
    
    # 时间信息
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_ms: float = 0.0
    
    # 错误信息
    error: Optional[str] = None
    
    # Embedding 模型信息
    embedding_model_id: Optional[str] = None
    embedding_dim: Optional[int] = None
    
    # 双读比较报告（仅当 --dual-read-compare 启用时填充）
    compare_report: Optional[CompareReport] = None
    
    # 双读统计信息（仅当 --dual-read 启用时填充）
    dual_read_stats: Optional["DualReadStats"] = None
    
    # 检索上下文（后端配置、embedding 信息、hybrid 配置等）
    retrieval_context: Optional[RetrievalContext] = None

    def to_evidence_packet(self, include_compare: bool = True, include_dual_read: bool = True) -> Dict[str, Any]:
        """
        转换为 Evidence Packet 精简格式
        
        用于 Agent 快速预览和流水线传输。
        
        字段说明:
            - query: 原始查询文本
            - evidences: 证据列表（不含 content，仅 excerpt）
            - chunking_version: 分块版本
            - generated_at: 生成时间
            - result_count: 结果数量
            - retrieval_context: 检索上下文（后端、embedding、hybrid 配置等）
            - compare_report: 双读比较报告（仅当启用时）
            - dual_read: 双读统计信息（仅当启用时）
        
        注意：packet 格式不包含 content 字段，减少传输量。
        如需完整内容，使用 full 格式。
        
        Args:
            include_compare: 是否包含双读比较报告（默认 True，仅当有报告时追加）
            include_dual_read: 是否包含双读统计信息（默认 True，仅当有统计时追加）
        """
        packet = {
            "query": self.query,
            "evidences": [e.to_evidence_dict(include_content=False) for e in self.evidences],
            "chunking_version": CHUNKING_VERSION,
            "generated_at": self.completed_at or datetime.now(timezone.utc).isoformat(),
            "result_count": len(self.evidences),
        }
        # 仅在有过滤条件时包含
        if self.filters:
            filter_dict = self.filters.to_dict()
            active_filters = {k: v for k, v in filter_dict.items() if v is not None}
            if active_filters:
                packet["filters"] = active_filters
        # 追加检索上下文（仅当有上下文信息时）
        if self.retrieval_context is not None:
            packet["retrieval_context"] = self.retrieval_context.to_dict()
        # 追加双读比较报告（仅当启用且有报告时）
        if include_compare and self.compare_report is not None:
            packet["compare_report"] = self.compare_report.to_dict()
        # 追加双读统计信息（仅当启用且有统计时）
        if include_dual_read and self.dual_read_stats is not None:
            packet["dual_read"] = self.dual_read_stats.to_dict()
        return packet

    def to_dict(self, include_compare: bool = True, include_dual_read: bool = True) -> Dict[str, Any]:
        """
        转换为完整结果字典（full 格式）
        
        包含完整内容和调试信息，用于验证和分析。
        
        字段说明:
            - success: 是否成功
            - query: 原始查询文本
            - filters: 过滤条件（DSL 格式）
            - top_k: 请求的结果数量
            - result_count: 实际返回数量
            - evidences: 证据列表（含完整 content）
            - chunking_version: 分块版本
            - timing: 耗时统计
            - embedding: Embedding 模型信息（可选）
            - retrieval_context: 检索上下文（可选）
            - error: 错误信息（可选）
            - compare_report: 双读比较报告（仅当启用时）
            - dual_read: 双读统计信息（仅当启用时）
            
        Args:
            include_compare: 是否包含双读比较报告（默认 True，仅当有报告时追加）
            include_dual_read: 是否包含双读统计信息（默认 True，仅当有统计时追加）
        """
        result = {
            "success": self.error is None,
            "query": self.query,
            "filters": self.filters.to_dict() if self.filters else None,
            "top_k": self.top_k,
            "result_count": len(self.evidences),
            "evidences": [e.to_evidence_dict(include_content=True) for e in self.evidences],
            "chunking_version": CHUNKING_VERSION,
            "timing": {
                "started_at": self.started_at,
                "completed_at": self.completed_at,
                "duration_ms": self.duration_ms,
            },
        }
        if self.embedding_model_id:
            result["embedding"] = {
                "model_id": self.embedding_model_id,
                "dim": self.embedding_dim,
            }
        # 追加检索上下文（仅当有上下文信息时）
        if self.retrieval_context is not None:
            result["retrieval_context"] = self.retrieval_context.to_dict()
        if self.error:
            result["error"] = self.error
        # 追加双读比较报告（仅当启用且有报告时）
        if include_compare and self.compare_report is not None:
            result["compare_report"] = self.compare_report.to_dict()
        # 追加双读统计信息（仅当启用且有统计时）
        if include_dual_read and self.dual_read_stats is not None:
            result["dual_read"] = self.dual_read_stats.to_dict()
        return result


# ============ pgvector 初始化 ============


def try_initialize_pgvector_backend(backend: Optional[IndexBackend]) -> bool:
    """
    尝试初始化 pgvector 后端（创建扩展、表结构等）
    
    仅当后端为 pgvector 且 STEP3_PGVECTOR_AUTO_INIT=1 时执行。
    
    Args:
        backend: 索引后端实例
    
    Returns:
        是否初始化成功（非 pgvector 后端返回 True）
    """
    if backend is None:
        return True
    
    # 检查是否为 pgvector 后端
    is_pgvector = (
        getattr(backend, 'backend_name', '') == 'pgvector'
        or hasattr(backend, 'initialize')
    )
    
    if not is_pgvector:
        return True
    
    # 检查环境变量开关
    if not PGVECTOR_AUTO_INIT:
        logger.debug("STEP3_PGVECTOR_AUTO_INIT=0，跳过 pgvector 自动初始化")
        return True
    
    # 尝试初始化
    try:
        if hasattr(backend, 'initialize'):
            logger.info("正在初始化 pgvector 后端（创建扩展和表结构）...")
            backend.initialize()
            logger.info("pgvector 后端初始化成功")
        return True
    except Exception as e:
        error_msg = str(e).lower()
        
        # 提供可操作的错误提示
        if 'extension' in error_msg or 'pgvector' in error_msg:
            logger.error(
                f"pgvector 初始化失败: {e}\n"
                f"可能原因: PostgreSQL 未安装 pgvector 扩展\n"
                f"解决方案:\n"
                f"  1. 安装 pgvector: CREATE EXTENSION IF NOT EXISTS vector;\n"
                f"  2. 或联系 DBA 安装 pgvector 扩展\n"
                f"  3. 若不需要自动初始化，设置 STEP3_PGVECTOR_AUTO_INIT=0"
            )
        elif 'permission' in error_msg or 'denied' in error_msg:
            logger.error(
                f"pgvector 初始化失败: {e}\n"
                f"可能原因: 数据库用户权限不足\n"
                f"解决方案:\n"
                f"  1. 授予用户 CREATE 权限\n"
                f"  2. 或联系 DBA 手动创建表结构\n"
                f"  3. 若不需要自动初始化，设置 STEP3_PGVECTOR_AUTO_INIT=0"
            )
        elif 'connection' in error_msg or 'connect' in error_msg:
            logger.error(
                f"pgvector 初始化失败: {e}\n"
                f"可能原因: 无法连接到 pgvector 数据库\n"
                f"解决方案:\n"
                f"  1. 检查 STEP3_PGVECTOR_DSN 环境变量配置\n"
                f"  2. 确认数据库服务正常运行\n"
                f"  3. 若不需要自动初始化，设置 STEP3_PGVECTOR_AUTO_INIT=0"
            )
        else:
            logger.error(
                f"pgvector 初始化失败: {e}\n"
                f"若不需要自动初始化，设置 STEP3_PGVECTOR_AUTO_INIT=0"
            )
        
        return False


# ============ 全局实例管理 ============


# 全局索引后端实例
_index_backend: Optional[IndexBackend] = None

# 全局 Shadow 后端实例（用于双读）
_shadow_backend: Optional[IndexBackend] = None

# 全局双读配置
_dual_read_config: Optional[DualReadConfig] = None

# 全局 Embedding Provider 实例
_embedding_provider: Optional[EmbeddingProvider] = None


def set_index_backend(backend: Optional[IndexBackend]) -> None:
    """设置全局索引后端实例"""
    global _index_backend
    _index_backend = backend


def get_index_backend() -> Optional[IndexBackend]:
    """获取全局索引后端实例"""
    return _index_backend


def set_shadow_backend(backend: Optional[IndexBackend]) -> None:
    """设置全局 Shadow 后端实例（用于双读）"""
    global _shadow_backend
    _shadow_backend = backend


def get_shadow_backend() -> Optional[IndexBackend]:
    """获取全局 Shadow 后端实例"""
    return _shadow_backend


def set_dual_read_config(config: Optional[DualReadConfig]) -> None:
    """设置全局双读配置"""
    global _dual_read_config
    _dual_read_config = config


def get_dual_read_config() -> Optional[DualReadConfig]:
    """获取全局双读配置"""
    return _dual_read_config


def set_embedding_provider_instance(provider: Optional[EmbeddingProvider]) -> None:
    """设置全局 Embedding Provider 实例"""
    global _embedding_provider
    _embedding_provider = provider
    # 同时设置 embedding_provider 模块的全局实例
    set_embedding_provider(provider)


def get_embedding_provider_instance() -> Optional[EmbeddingProvider]:
    """获取全局 Embedding Provider 实例"""
    global _embedding_provider
    if _embedding_provider is None:
        try:
            _embedding_provider = get_embedding_provider()
        except EmbeddingError:
            pass
    return _embedding_provider


# ============ 超时执行辅助函数 ============


class ShadowQueryTimeoutError(Exception):
    """Shadow 查询超时错误"""
    def __init__(self, timeout_ms: int, message: str = "Shadow 查询超时"):
        self.timeout_ms = timeout_ms
        self.message = message
        super().__init__(f"{message} (timeout_ms={timeout_ms})")


def execute_with_timeout(
    func: Callable,
    timeout_ms: int,
    *args,
    **kwargs,
):
    """
    使用线程池执行函数并在超时后中止等待
    
    注意：这只是停止等待结果，后台线程可能仍在运行。
    对于数据库查询，建议配合 statement_timeout 使用以确保资源释放。
    
    Args:
        func: 要执行的函数
        timeout_ms: 超时时间（毫秒）
        *args: 传递给 func 的位置参数
        **kwargs: 传递给 func 的关键字参数
    
    Returns:
        func 的返回值
    
    Raises:
        ShadowQueryTimeoutError: 超时
        Exception: func 抛出的原始异常
    """
    if timeout_ms <= 0:
        # 无超时限制，直接执行
        return func(*args, **kwargs)
    
    timeout_seconds = timeout_ms / 1000.0
    
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(func, *args, **kwargs)
        try:
            return future.result(timeout=timeout_seconds)
        except FuturesTimeoutError:
            # 注意：无法真正取消正在执行的数据库查询
            # 后台线程会继续运行直到查询完成或 statement_timeout 生效
            raise ShadowQueryTimeoutError(timeout_ms)


# ============ 检索函数 ============


def query_evidence(
    query_text: str,
    filters: Optional[QueryFilters] = None,
    top_k: int = 10,
    backend: Optional[IndexBackend] = None,
    embedding_provider: Optional[EmbeddingProvider] = None,
) -> List[EvidenceResult]:
    """
    执行证据检索
    
    Args:
        query_text: 查询文本
        filters: 过滤条件
        top_k: 返回结果数量
        backend: 索引后端（可选，默认使用全局实例）
        embedding_provider: Embedding Provider（可选，用于生成查询向量）
    
    Returns:
        EvidenceResult 列表
    """
    logger.info(f"执行检索: query='{query_text[:50]}...', top_k={top_k}")
    
    filter_dict = filters.to_filter_dict() if filters else {}
    if filter_dict:
        logger.info(f"过滤条件: {filter_dict}")
    
    # 获取索引后端
    index_backend = backend or get_index_backend()
    
    if index_backend is not None:
        # 获取 embedding provider
        provider = embedding_provider or get_embedding_provider_instance()
        
        # 构建查询请求
        query_vector = None
        if provider is not None:
            try:
                query_vector = provider.embed_text(query_text)
                logger.debug(f"生成查询向量 (model={provider.model_id}, dim={provider.dim})")
            except EmbeddingError as e:
                logger.warning(f"Embedding 生成失败，将使用文本查询: {e}")
        
        request = QueryRequest(
            query_text=query_text,
            query_vector=query_vector,
            filters=filter_dict,
            top_k=top_k,
        )
        
        # 执行查询
        try:
            hits = index_backend.query(request)
            
            # 转换为 EvidenceResult（使用 from_query_hit 映射）
            results = [EvidenceResult.from_query_hit(hit) for hit in hits]
            
            logger.info(f"检索返回 {len(results)} 条结果")
            return results
            
        except Exception as e:
            logger.error(f"检索失败: {e}")
            raise
    else:
        # 模板实现：返回空结果
        logger.warning("[STUB] 索引后端未配置，返回空结果")
        return []


def _compare_results(
    primary_results: List[EvidenceResult],
    shadow_results: List[EvidenceResult],
    diff_threshold: float = 0.1,
) -> Dict[str, Any]:
    """
    比较 primary 和 shadow 结果
    
    Args:
        primary_results: primary 后端结果
        shadow_results: shadow 后端结果
        diff_threshold: 分数差异阈值
    
    Returns:
        比较结果字典，包含:
        - match: 是否完全匹配
        - primary_count: primary 结果数量
        - shadow_count: shadow 结果数量
        - common_count: 共同 chunk_id 数量
        - only_primary: 仅在 primary 中的 chunk_id 列表
        - only_shadow: 仅在 shadow 中的 chunk_id 列表
        - score_diffs: 分数差异超过阈值的 chunk_id 列表
    """
    primary_map = {r.chunk_id: r for r in primary_results}
    shadow_map = {r.chunk_id: r for r in shadow_results}
    
    primary_ids = set(primary_map.keys())
    shadow_ids = set(shadow_map.keys())
    
    common_ids = primary_ids & shadow_ids
    only_primary = primary_ids - shadow_ids
    only_shadow = shadow_ids - primary_ids
    
    # 检查分数差异
    score_diffs = []
    for chunk_id in common_ids:
        p_score = primary_map[chunk_id].relevance_score
        s_score = shadow_map[chunk_id].relevance_score
        if abs(p_score - s_score) > diff_threshold:
            score_diffs.append({
                "chunk_id": chunk_id,
                "primary_score": p_score,
                "shadow_score": s_score,
                "diff": abs(p_score - s_score),
            })
    
    match = (
        len(only_primary) == 0 and 
        len(only_shadow) == 0 and 
        len(score_diffs) == 0
    )
    
    return {
        "match": match,
        "primary_count": len(primary_results),
        "shadow_count": len(shadow_results),
        "common_count": len(common_ids),
        "only_primary": list(only_primary),
        "only_shadow": list(only_shadow),
        "score_diffs": score_diffs,
    }


@dataclass
class GateProfile:
    """
    门禁阈值 Profile 信息
    
    追踪门禁阈值的来源、名称和版本信息，用于审计和调试。
    """
    name: str = "dual_read_gate"                # Profile 名称
    version: str = "1.0"                        # Profile 版本
    source: str = THRESHOLD_SOURCE_DEFAULT      # 来源标识（default/env/cli）
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            "name": self.name,
            "version": self.version,
            "source": self.source,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GateProfile":
        """从字典构建 GateProfile"""
        return cls(
            name=data.get("name", "dual_read_gate"),
            version=data.get("version", "1.0"),
            source=data.get("source", THRESHOLD_SOURCE_DEFAULT),
        )


@dataclass
class DualReadGateThresholds:
    """
    双读门禁阈值配置
    
    所有阈值字段均为可选，None 表示不检查该项。
    """
    min_overlap: Optional[float] = None          # overlap_ratio 最小阈值 [0.0, 1.0]
    max_only_primary: Optional[int] = None       # only_primary 数量上限
    max_only_shadow: Optional[int] = None        # only_shadow 数量上限
    max_score_drift: Optional[float] = None      # score_diff_max 最大阈值
    profile: Optional[GateProfile] = None        # Profile 信息（来源追踪）
    
    def has_thresholds(self) -> bool:
        """是否配置了任何阈值"""
        return any([
            self.min_overlap is not None,
            self.max_only_primary is not None,
            self.max_only_shadow is not None,
            self.max_score_drift is not None,
        ])
    
    def to_dict(self) -> Dict[str, Any]:
        """
        转换为字典格式
        
        输出格式（保留兼容字段）：
        {
            "min_overlap": ...,         # 兼容字段
            "max_only_primary": ...,    # 兼容字段
            "max_only_shadow": ...,     # 兼容字段
            "max_score_drift": ...,     # 兼容字段
            "source": ...,              # 兼容字段
            "profile": {                # 新增字段
                "name": ...,
                "version": ...,
                "source": ...,
            }
        }
        """
        result: Dict[str, Any] = {}
        
        # 保留兼容字段（现有 consumers 期望的 keys）
        if self.min_overlap is not None:
            result["min_overlap"] = self.min_overlap
        if self.max_only_primary is not None:
            result["max_only_primary"] = self.max_only_primary
        if self.max_only_shadow is not None:
            result["max_only_shadow"] = self.max_only_shadow
        if self.max_score_drift is not None:
            result["max_score_drift"] = self.max_score_drift
        
        # 兼容字段：source（从 profile 提取）
        if self.profile is not None:
            result["source"] = self.profile.source
            # 新增字段：profile 完整信息
            result["profile"] = self.profile.to_dict()
        else:
            result["source"] = THRESHOLD_SOURCE_DEFAULT
            # 即使没有 profile，也提供默认 profile 信息
            result["profile"] = GateProfile().to_dict()
        
        return result


@dataclass
class DualReadGateViolation:
    """
    门禁违规详情
    """
    check_name: str           # 检查项名称
    threshold_value: float    # 阈值
    actual_value: float       # 实际值
    message: str              # 描述信息


@dataclass
class DualReadGateResult:
    """
    双读门禁检查结果
    """
    passed: bool = True                                      # 是否通过所有检查
    violations: List[DualReadGateViolation] = field(default_factory=list)  # 违规列表
    thresholds_applied: Optional[DualReadGateThresholds] = None  # 应用的阈值配置
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典，用于 JSON 输出"""
        result: Dict[str, Any] = {
            "passed": self.passed,
        }
        
        if self.thresholds_applied is not None:
            result["thresholds"] = {
                k: v for k, v in {
                    "min_overlap": self.thresholds_applied.min_overlap,
                    "max_only_primary": self.thresholds_applied.max_only_primary,
                    "max_only_shadow": self.thresholds_applied.max_only_shadow,
                    "max_score_drift": self.thresholds_applied.max_score_drift,
                }.items() if v is not None
            }
        
        if self.violations:
            result["violations"] = [
                {
                    "check": v.check_name,
                    "threshold": v.threshold_value,
                    "actual": v.actual_value,
                    "message": v.message,
                }
                for v in self.violations
            ]
        
        return result


def check_dual_read_gate(
    stats: "DualReadStats",
    thresholds: DualReadGateThresholds,
) -> DualReadGateResult:
    """
    执行双读门禁检查
    
    Args:
        stats: 双读统计信息
        thresholds: 门禁阈值配置
    
    Returns:
        DualReadGateResult 门禁检查结果
    """
    result = DualReadGateResult(
        passed=True,
        thresholds_applied=thresholds,
    )
    
    # 如果没有配置阈值，直接通过
    if not thresholds.has_thresholds():
        return result
    
    violations: List[DualReadGateViolation] = []
    
    # 检查 overlap_ratio
    if thresholds.min_overlap is not None:
        if stats.overlap_ratio < thresholds.min_overlap:
            violations.append(DualReadGateViolation(
                check_name="min_overlap",
                threshold_value=thresholds.min_overlap,
                actual_value=stats.overlap_ratio,
                message=f"重叠率 {stats.overlap_ratio:.4f} 低于阈值 {thresholds.min_overlap}",
            ))
    
    # 检查 only_primary 数量
    if thresholds.max_only_primary is not None:
        only_primary_count = len(stats.only_primary)
        if only_primary_count > thresholds.max_only_primary:
            violations.append(DualReadGateViolation(
                check_name="max_only_primary",
                threshold_value=float(thresholds.max_only_primary),
                actual_value=float(only_primary_count),
                message=f"仅 primary 数量 {only_primary_count} 超过阈值 {thresholds.max_only_primary}",
            ))
    
    # 检查 only_shadow 数量
    if thresholds.max_only_shadow is not None:
        only_shadow_count = len(stats.only_shadow)
        if only_shadow_count > thresholds.max_only_shadow:
            violations.append(DualReadGateViolation(
                check_name="max_only_shadow",
                threshold_value=float(thresholds.max_only_shadow),
                actual_value=float(only_shadow_count),
                message=f"仅 shadow 数量 {only_shadow_count} 超过阈值 {thresholds.max_only_shadow}",
            ))
    
    # 检查 score_diff_max
    if thresholds.max_score_drift is not None:
        if stats.score_diff_max > thresholds.max_score_drift:
            violations.append(DualReadGateViolation(
                check_name="max_score_drift",
                threshold_value=thresholds.max_score_drift,
                actual_value=stats.score_diff_max,
                message=f"最大分数漂移 {stats.score_diff_max:.4f} 超过阈值 {thresholds.max_score_drift}",
            ))
    
    result.violations = violations
    result.passed = len(violations) == 0
    
    return result


@dataclass
class ViolationSummary:
    """
    违规项聚合统计
    
    统计各类违规的触发次数和详情。
    """
    # 各 check_name 的触发计数（如 {"overlap": 3, "rbo": 2}）
    by_check: Dict[str, int] = field(default_factory=dict)
    
    # 各级别的触发计数（如 {"fail": 2, "warn": 3}）
    by_level: Dict[str, int] = field(default_factory=dict)
    
    # 触发项明细列表（最多保留前 N 个）
    details: List[Dict[str, Any]] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "by_check": self.by_check,
            "by_level": self.by_level,
            "details": self.details,
        }


@dataclass
class AggregateGateResult:
    """
    聚合门禁结果
    
    用于批量查询时，按查询列表聚合最差 recommendation 或统计 fail/warn 数。
    包含聚合 gate 摘要：fail/warn/pass 数、最差 recommendation、触发项统计。
    """
    # 是否通过聚合检查
    passed: bool = True
    
    # 最差的 recommendation（"safe_to_switch" < "investigate_required" < "abort_switch"）
    worst_recommendation: str = "safe_to_switch"
    
    # 统计计数
    total_queries: int = 0
    fail_count: int = 0
    warn_count: int = 0
    pass_count: int = 0
    error_count: int = 0
    
    # 失败的查询索引列表
    failed_query_indices: List[int] = field(default_factory=list)
    
    # 有警告的查询索引列表
    warned_query_indices: List[int] = field(default_factory=list)
    
    # 违规项聚合统计
    violation_summary: Optional[ViolationSummary] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典，用于 JSON 输出"""
        result = {
            "passed": self.passed,
            "worst_recommendation": self.worst_recommendation,
            "total_queries": self.total_queries,
            "fail_count": self.fail_count,
            "warn_count": self.warn_count,
            "pass_count": self.pass_count,
            "error_count": self.error_count,
            "failed_query_indices": self.failed_query_indices,
            "warned_query_indices": self.warned_query_indices,
        }
        if self.violation_summary is not None:
            result["violation_summary"] = self.violation_summary.to_dict()
        return result


# recommendation 优先级映射（值越大越严重）
_RECOMMENDATION_PRIORITY = {
    "safe_to_switch": 0,
    "investigate_required": 1,
    "abort_switch": 2,
}


def aggregate_gate_results(results: List["QueryResult"], max_violation_details: int = 20) -> AggregateGateResult:
    """
    聚合批量查询的门禁结果
    
    根据所有查询结果中的 compare_report 和 dual_read_stats.gate 决定聚合结果。
    包含聚合 gate 摘要：fail/warn/pass 数、最差 recommendation、触发项统计。
    
    聚合规则：
    1. 同时检查 compare_report.decision 和 dual_read_stats.gate（若存在）
    2. 计算 combined 决策：任一失败则 combined 失败
    3. 统计 fail/warn/pass/error 数量
    4. 取最差的 recommendation 作为聚合结果
    5. 任何 fail 级别违规或 error 则聚合结果为 passed=False
    6. 聚合所有触发的违规项统计（by_check, by_level）
    
    Args:
        results: 查询结果列表
        max_violation_details: 最多保留的违规详情数量（默认 20）
        
    Returns:
        AggregateGateResult 聚合门禁结果（含 violation_summary）
    """
    agg = AggregateGateResult(
        total_queries=len(results),
    )
    
    if not results:
        return agg
    
    # 触发项统计
    violation_by_check: Dict[str, int] = {}
    violation_by_level: Dict[str, int] = {}
    violation_details: List[Dict[str, Any]] = []
    
    for i, result in enumerate(results):
        # 处理查询错误
        if result.error is not None:
            agg.error_count += 1
            agg.failed_query_indices.append(i)
            continue
        
        # 同时检查 compare_report 和 dual_read_stats.gate
        compare_decision = None
        gate_result = None
        
        # 读取 compare_report.decision（若存在）
        if result.compare_report is not None and result.compare_report.decision is not None:
            compare_decision = result.compare_report.decision
            
            # 收集 compare 违规详情
            if compare_decision.violation_details:
                for v in compare_decision.violation_details:
                    check_name = v.check_name
                    level = v.level
                    
                    # 计数
                    violation_by_check[check_name] = violation_by_check.get(check_name, 0) + 1
                    violation_by_level[level] = violation_by_level.get(level, 0) + 1
                    
                    # 保留详情（限制数量，增加 source 字段）
                    if len(violation_details) < max_violation_details:
                        violation_details.append({
                            "query_index": i,
                            "check_name": check_name,
                            "level": level,
                            "actual_value": v.actual_value,
                            "threshold_value": v.threshold_value,
                            "reason": v.reason,
                            "source": "compare",
                        })
        
        # 读取 dual_read_stats.gate（若存在）
        if result.dual_read_stats is not None and result.dual_read_stats.gate is not None:
            gate_result = result.dual_read_stats.gate
            
            # 收集 gate 违规详情
            if gate_result.violations:
                for v in gate_result.violations:
                    check_name = v.check_name
                    level = "fail" if not gate_result.passed else "warn"
                    
                    # 计数
                    violation_by_check[check_name] = violation_by_check.get(check_name, 0) + 1
                    violation_by_level[level] = violation_by_level.get(level, 0) + 1
                    
                    # 保留详情（限制数量，增加 source 字段）
                    if len(violation_details) < max_violation_details:
                        violation_details.append({
                            "query_index": i,
                            "check_name": check_name,
                            "level": level,
                            "actual_value": v.actual_value,
                            "threshold_value": v.threshold_value,
                            "message": v.message,
                            "source": "gate",
                        })
        
        # 计算 combined 决策
        # compare_passed: None（无 compare）、True（通过）、False（失败）
        # gate_passed: None（无 gate）、True（通过）、False（失败）
        compare_passed = compare_decision.passed if compare_decision is not None else None
        compare_has_warnings = compare_decision.has_warnings if compare_decision is not None else False
        gate_passed = gate_result.passed if gate_result is not None else None
        
        # combined 决策逻辑：任一失败则 combined 失败
        if compare_passed is None and gate_passed is None:
            # 无任何门禁信息，视为通过
            agg.pass_count += 1
            continue
        
        # 计算 combined passed
        combined_passed = True
        if compare_passed is not None and not compare_passed:
            combined_passed = False
        if gate_passed is not None and not gate_passed:
            combined_passed = False
        
        # 计算 combined has_warnings
        combined_has_warnings = compare_has_warnings
        
        # 更新 recommendation（从 compare_decision 获取，若无则基于 gate 判定）
        recommendation = "safe_to_switch"
        if compare_decision is not None:
            recommendation = compare_decision.recommendation or "safe_to_switch"
        
        # 如果 gate 失败，recommendation 至少是 abort_switch
        if gate_passed is not None and not gate_passed:
            if _RECOMMENDATION_PRIORITY.get("abort_switch", 2) > _RECOMMENDATION_PRIORITY.get(recommendation, 0):
                recommendation = "abort_switch"
        
        # 更新 worst_recommendation
        current_priority = _RECOMMENDATION_PRIORITY.get(recommendation, 0)
        worst_priority = _RECOMMENDATION_PRIORITY.get(agg.worst_recommendation, 0)
        if current_priority > worst_priority:
            agg.worst_recommendation = recommendation
        
        # 统计计数
        if not combined_passed:
            agg.fail_count += 1
            agg.failed_query_indices.append(i)
        elif combined_has_warnings:
            agg.warn_count += 1
            agg.warned_query_indices.append(i)
        else:
            agg.pass_count += 1
    
    # 聚合判定：有任何 fail 或 error 则聚合不通过
    agg.passed = (agg.fail_count == 0) and (agg.error_count == 0)
    
    # 构建 violation_summary
    if violation_by_check or violation_by_level:
        agg.violation_summary = ViolationSummary(
            by_check=violation_by_check,
            by_level=violation_by_level,
            details=violation_details,
        )
    
    return agg


def add_to_attachments(
    conn: psycopg.Connection,
    item_id: int,
    aggregate_gate: AggregateGateResult,
    results: List["QueryResult"],
    query_set_name: Optional[str] = None,
    collection_id: Optional[str] = None,
    embedding_model_id: Optional[str] = None,
) -> int:
    """
    将查询结果作为附件保存到 logbook.attachments
    
    复用 seek_consistency_check.py 的 add_to_attachments() 模式：
    - 将结果转换为 JSON 并写入制品存储
    - 在 logbook.attachments 中创建记录（kind='report'）
    - meta_json 包含 collection_id/chunking_version/embedding_model_id/policy_version
    
    Args:
        conn: 数据库连接
        item_id: 关联的 item_id
        aggregate_gate: 聚合门禁结果
        results: 查询结果列表
        query_set_name: 查询集名称（可选）
        collection_id: collection ID（可选）
        embedding_model_id: embedding 模型 ID（可选）
    
    Returns:
        attachment_id
    """
    # 构建完整的结果数据
    result_dict = {
        "report_type": "seek_query",
        "aggregate_gate": aggregate_gate.to_dict(),
        "query_set_name": query_set_name,
        "collection_id": collection_id,
        "chunking_version": CHUNKING_VERSION,
        "embedding_model_id": embedding_model_id,
        "total_queries": len(results),
        "results": [r.to_dict(include_compare=True, include_dual_read=True) for r in results],
    }
    
    result_json = json.dumps(result_dict, ensure_ascii=False, indent=2, default=str)
    result_bytes = result_json.encode("utf-8")
    sha256 = hashlib.sha256(result_bytes).hexdigest()
    size_bytes = len(result_bytes)
    
    # 生成时间戳作为文件名的一部分
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    
    # 写入制品存储
    from artifacts import write_text_artifact
    
    prefix = query_set_name or "batch"
    uri = f"reports/seek_query/{prefix}_{timestamp}.json"
    artifact_result = write_text_artifact(uri, result_json)
    
    # 构建 meta_json，包含 Evidence Packet 互相引用所需的字段
    # policy_version 对于 query 操作为 None（策略主要用于索引时过滤）
    meta_json = json.dumps({
        "report_type": "seek_query",
        "collection_id": collection_id,
        "chunking_version": CHUNKING_VERSION,
        "embedding_model_id": embedding_model_id,
        "policy_version": None,  # 查询操作不涉及策略过滤
        "query_set_name": query_set_name,
        "total_queries": len(results),
        "passed": aggregate_gate.passed,
        "fail_count": aggregate_gate.fail_count,
        "warn_count": aggregate_gate.warn_count,
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
    
    logger.info(f"查询报告已保存为附件, attachment_id={attachment_id}, uri={uri}")
    
    return attachment_id


@dataclass
class DualReadStats:
    """
    双读统计信息
    
    用于 --dual-read 开关输出的轻量级统计结果。
    """
    # 基础健康信息
    primary_table: str = ""
    primary_strategy: str = ""
    primary_collection_id: str = ""
    shadow_table: str = ""
    shadow_strategy: str = ""
    shadow_collection_id: str = ""
    
    # 统计指标
    overlap_ratio: float = 0.0      # TopK chunk_id 重叠度（Jaccard 指数）
    primary_count: int = 0          # primary 结果数量
    shadow_count: int = 0           # shadow 结果数量
    common_count: int = 0           # 共同结果数量
    only_primary: List[str] = field(default_factory=list)  # 仅在 primary 中的 chunk_id
    only_shadow: List[str] = field(default_factory=list)   # 仅在 shadow 中的 chunk_id
    
    # 分数差异统计
    score_diff_mean: float = 0.0    # 分数差异均值
    score_diff_max: float = 0.0     # 分数差异最大值
    
    # 延迟信息
    primary_latency_ms: float = 0.0
    shadow_latency_ms: float = 0.0
    
    # 错误信息
    shadow_error: Optional[str] = None
    shadow_timed_out: bool = False  # 是否因超时失败
    
    # 门禁检查结果（可选，仅当配置了门禁阈值时填充）
    gate: Optional[DualReadGateResult] = None
    
    def to_dict(self, max_list_items: int = 5) -> Dict[str, Any]:
        """
        转换为字典，用于 JSON 输出
        
        Args:
            max_list_items: only_primary/only_shadow 列表的最大显示条数
        """
        # 截断 chunk_id 列表
        only_primary_truncated = self.only_primary[:max_list_items]
        only_shadow_truncated = self.only_shadow[:max_list_items]
        
        result = {
            "health": {
                "primary": {
                    "table": self.primary_table,
                    "strategy": self.primary_strategy,
                    "collection_id": self.primary_collection_id,
                },
                "shadow": {
                    "table": self.shadow_table,
                    "strategy": self.shadow_strategy,
                    "collection_id": self.shadow_collection_id,
                },
            },
            "metrics": {
                "overlap_ratio": round(self.overlap_ratio, 4),
                "primary_count": self.primary_count,
                "shadow_count": self.shadow_count,
                "common_count": self.common_count,
                "only_primary_count": len(self.only_primary),
                "only_shadow_count": len(self.only_shadow),
                "score_diff_mean": round(self.score_diff_mean, 4),
                "score_diff_max": round(self.score_diff_max, 4),
            },
            "latency": {
                "primary_ms": round(self.primary_latency_ms, 2),
                "shadow_ms": round(self.shadow_latency_ms, 2),
            },
            "only_primary": only_primary_truncated,
            "only_shadow": only_shadow_truncated,
        }
        
        # 添加截断提示
        if len(self.only_primary) > max_list_items:
            result["only_primary_truncated"] = True
            result["only_primary_total"] = len(self.only_primary)
        if len(self.only_shadow) > max_list_items:
            result["only_shadow_truncated"] = True
            result["only_shadow_total"] = len(self.only_shadow)
        
        # 添加错误信息
        if self.shadow_error:
            result["shadow_error"] = self.shadow_error
            result["shadow_timed_out"] = self.shadow_timed_out
        
        # 添加门禁检查结果
        if self.gate is not None:
            result["gate"] = self.gate.to_dict()
        
        return result


def compute_dual_read_stats(
    primary_results: List[EvidenceResult],
    shadow_results: List[EvidenceResult],
    primary_backend: Optional[IndexBackend] = None,
    shadow_backend: Optional[IndexBackend] = None,
    primary_latency_ms: float = 0.0,
    shadow_latency_ms: float = 0.0,
    shadow_error: Optional[str] = None,
    shadow_timed_out: bool = False,
) -> DualReadStats:
    """
    计算双读统计信息
    
    Args:
        primary_results: primary 后端结果
        shadow_results: shadow 后端结果
        primary_backend: primary 后端实例（用于获取健康信息）
        shadow_backend: shadow 后端实例（用于获取健康信息）
        primary_latency_ms: primary 查询延迟
        shadow_latency_ms: shadow 查询延迟
        shadow_error: shadow 查询错误信息
        shadow_timed_out: shadow 查询是否因超时失败
    
    Returns:
        DualReadStats 实例
    """
    stats = DualReadStats(
        primary_latency_ms=primary_latency_ms,
        shadow_latency_ms=shadow_latency_ms,
        shadow_error=shadow_error,
        shadow_timed_out=shadow_timed_out,
    )
    
    # 提取健康信息
    if primary_backend is not None:
        stats.primary_table = getattr(primary_backend, 'table_name', '') or getattr(primary_backend, '_table_name', '')
        stats.primary_strategy = getattr(primary_backend, 'collection_strategy_name', 'unknown')
        stats.primary_collection_id = getattr(primary_backend, 'canonical_id', '') or getattr(primary_backend, 'collection_id', '')
    
    if shadow_backend is not None:
        stats.shadow_table = getattr(shadow_backend, 'table_name', '') or getattr(shadow_backend, '_table_name', '')
        stats.shadow_strategy = getattr(shadow_backend, 'collection_strategy_name', 'unknown')
        stats.shadow_collection_id = getattr(shadow_backend, 'canonical_id', '') or getattr(shadow_backend, 'collection_id', '')
    
    # 如果 shadow 查询失败，仅返回基础信息
    if shadow_error or not shadow_results:
        stats.primary_count = len(primary_results)
        return stats
    
    # 计算结果集合
    primary_map = {r.chunk_id: r.relevance_score for r in primary_results}
    shadow_map = {r.chunk_id: r.relevance_score for r in shadow_results}
    
    primary_ids = set(primary_map.keys())
    shadow_ids = set(shadow_map.keys())
    
    common_ids = primary_ids & shadow_ids
    union_ids = primary_ids | shadow_ids
    
    stats.primary_count = len(primary_results)
    stats.shadow_count = len(shadow_results)
    stats.common_count = len(common_ids)
    stats.only_primary = sorted(list(primary_ids - shadow_ids))
    stats.only_shadow = sorted(list(shadow_ids - primary_ids))
    
    # 计算 overlap_ratio（Jaccard 指数）
    if union_ids:
        stats.overlap_ratio = len(common_ids) / len(union_ids)
    else:
        stats.overlap_ratio = 1.0  # 两者都为空视为完全重叠
    
    # 计算分数差异统计（仅对共同 chunk_id）
    if common_ids:
        score_diffs = []
        for chunk_id in common_ids:
            diff = abs(primary_map[chunk_id] - shadow_map[chunk_id])
            score_diffs.append(diff)
        
        stats.score_diff_mean = sum(score_diffs) / len(score_diffs)
        stats.score_diff_max = max(score_diffs)
    
    return stats


def generate_compare_report(
    primary_results: List[EvidenceResult],
    shadow_results: List[EvidenceResult],
    primary_latency_ms: float = 0.0,
    shadow_latency_ms: float = 0.0,
    thresholds: Optional[CompareThresholds] = None,
    request_id: str = "",
    primary_backend_name: str = "primary",
    shadow_backend_name: str = "shadow",
    compare_mode: str = "summary",
) -> CompareReport:
    """
    生成双读比较报告
    
    基于 primary 和 shadow 结果生成完整的 CompareReport，
    包含指标计算、阈值校验和决策判定。
    
    内部调用 dual_read_compare.evaluate_with_report() 完成评估。
    
    Args:
        primary_results: primary 后端结果
        shadow_results: shadow 后端结果
        primary_latency_ms: primary 查询延迟（毫秒）
        shadow_latency_ms: shadow 查询延迟（毫秒）
        thresholds: 比较阈值配置，None 则从环境变量加载
        request_id: 请求标识
        primary_backend_name: primary 后端名称
        shadow_backend_name: shadow 后端名称
        compare_mode: 比较模式（summary/detailed）
    
    Returns:
        CompareReport 实例
    """
    if thresholds is None:
        thresholds = CompareThresholds.from_env()
    
    # 构建排名列表 (chunk_id, score)
    primary_ranked = [(r.chunk_id, r.relevance_score) for r in primary_results]
    shadow_ranked = [(r.chunk_id, r.relevance_score) for r in shadow_results]
    
    # 计算排名漂移指标
    ranking_metrics = compute_ranking_drift(primary_ranked, shadow_ranked, stabilize=True)
    
    # 计算分数漂移指标
    score_drift_metrics = compute_score_drift(primary_ranked, shadow_ranked)
    
    # 计算命中重叠
    primary_ids = set(r.chunk_id for r in primary_results)
    shadow_ids = set(r.chunk_id for r in shadow_results)
    common_ids = primary_ids & shadow_ids
    union_ids = primary_ids | shadow_ids
    hit_overlap_ratio = len(common_ids) / len(union_ids) if union_ids else 1.0
    
    # 计算延迟比率
    latency_ratio = (shadow_latency_ms / primary_latency_ms) if primary_latency_ms > 0 else 0.0
    
    # 构建 CompareMetrics（使用 score_drift_metrics 中的精确统计值）
    metrics = CompareMetrics(
        avg_score_diff=score_drift_metrics.avg_abs_score_diff,
        max_score_diff=score_drift_metrics.max_abs_score_diff,
        p95_score_diff=score_drift_metrics.p95_abs_score_diff,
        std_score_diff=score_drift_metrics.std_score_diff,
        avg_rank_drift=ranking_metrics.avg_abs_rank_diff,
        max_rank_drift=int(ranking_metrics.p95_abs_rank_diff),
        hit_overlap_ratio=hit_overlap_ratio,
        common_hit_count=len(common_ids),
        primary_latency_ms=primary_latency_ms,
        secondary_latency_ms=shadow_latency_ms,
        latency_ratio=latency_ratio,
        primary_hit_count=len(primary_results),
        secondary_hit_count=len(shadow_results),
    )
    
    # 构建元数据
    metadata: Dict[str, Any] = {
        "compare_mode": compare_mode,
        "ranking_drift": ranking_metrics.to_dict(),
        "score_drift": score_drift_metrics.to_dict(),
    }
    
    # 使用 evaluate_with_report() 生成完整报告
    report = evaluate_with_report(
        metrics=metrics,
        thresholds=thresholds,
        ranking_metrics=ranking_metrics,
        score_drift_metrics=score_drift_metrics,
        request_id=request_id,
        primary_backend=primary_backend_name,
        secondary_backend=shadow_backend_name,
        metadata=metadata if compare_mode == "detailed" else {},
    )
    
    # 根据 compare_mode 调整报告内容
    if compare_mode != "detailed":
        # summary 模式不输出 thresholds
        report.thresholds = None
    
    return report


def query_evidence_dual_read(
    query_text: str,
    filters: Optional[QueryFilters] = None,
    top_k: int = 10,
    primary_backend: Optional[IndexBackend] = None,
    shadow_backend: Optional[IndexBackend] = None,
    dual_read_config: Optional[DualReadConfig] = None,
    embedding_provider: Optional[EmbeddingProvider] = None,
) -> List[EvidenceResult]:
    """
    带双读支持的证据检索
    
    根据 DualReadConfig 的策略执行检索：
    - 如果双读未启用或无 shadow_backend，直接调用 query_evidence
    - compare: 同时查询两个后端并比较结果，返回 primary 结果
    - fallback: 优先使用 primary，失败或无结果时 fallback 到 shadow
    - shadow_only: 仅使用 shadow 后端
    
    默认不开启时无额外开销，直接调用 query_evidence。
    
    ========================================================================
    DualReadConfig 行为真值表
    ========================================================================
    
    输入条件:
      - config: DualReadConfig 实例（可为 None）
      - enabled: config.enabled（双读总开关）
      - shadow: shadow_backend 是否可用（非 None）
      - strategy: config.strategy (compare/fallback/shadow_only/shadow_only_compare)
    
    +--------+--------+--------------------+------------------------------------------+
    | enabled| shadow | strategy           | 行为                                     |
    +--------+--------+--------------------+------------------------------------------+
    | None/F | -      | -                  | → primary_only (直接调用 query_evidence) |
    | True   | None   | -                  | → primary_only (无 shadow 可用)          |
    | True   | 有     | shadow_only        | → 仅查询 shadow，返回 shadow 结果        |
    | True   | 有     | shadow_only_compare| → 返回 shadow 结果，但同时查询 primary 做对比记录 |
    | True   | 有     | fallback           | → primary 优先；primary 失败/空 → shadow |
    | True   | 有     | compare            | → 同时查询 primary + shadow，返回 primary |
    +--------+--------+--------------------+------------------------------------------+
    
    shadow_only_compare 使用场景:
      - 已完成切主（流量切到 shadow），但仍需记录与 primary 差异便于回滚决策
      - preflight 验证阶段：使用 shadow 结果，但通过对比日志评估数据一致性
    
    异常处理（compare/fallback 模式）:
    +-------------------+-------------------+--------------------------------+
    | primary 状态      | shadow 状态       | 返回结果                       |
    +-------------------+-------------------+--------------------------------+
    | 成功              | 成功              | primary（compare 记录差异日志） |
    | 成功              | 失败              | primary（shadow 错误被吞掉）    |
    | 失败              | 成功              | shadow（自动 fallback）         |
    | 失败              | 失败              | 抛出 primary 的异常             |
    +-------------------+-------------------+--------------------------------+
    
    fallback 触发条件（strategy=fallback 时）:
      1. primary 查询抛异常
      2. primary 返回空结果列表
    
    compare 模式差异记录（strategy=compare 且 log_diff=True）:
      - 当 primary/shadow 结果不匹配时记录 WARNING
      - 差异判定: chunk_id 集合不同，或分数差异超过 diff_threshold
    ========================================================================
    
    Args:
        query_text: 查询文本
        filters: 过滤条件
        top_k: 返回结果数量
        primary_backend: Primary 索引后端（可选，默认使用全局实例）
        shadow_backend: Shadow 索引后端（可选，默认使用全局实例）
        dual_read_config: 双读配置（可选，默认使用全局配置）
        embedding_provider: Embedding Provider
    
    Returns:
        EvidenceResult 列表
    """
    # 获取配置和后端
    config = dual_read_config or get_dual_read_config()
    primary = primary_backend or get_index_backend()
    shadow = shadow_backend or get_shadow_backend()
    
    # 默认不开启时，直接调用原始 query_evidence，无额外开销
    if config is None or not config.enabled or shadow is None:
        return query_evidence(
            query_text=query_text,
            filters=filters,
            top_k=top_k,
            backend=primary,
            embedding_provider=embedding_provider,
        )
    
    strategy = config.strategy
    
    # shadow_only 和 shadow_only_compare 策略：执行就绪性校验
    if strategy in (DualReadConfig.STRATEGY_SHADOW_ONLY, DualReadConfig.STRATEGY_SHADOW_ONLY_COMPARE):
        # 执行 Shadow 后端就绪性校验
        readiness = validate_shadow_readiness(
            shadow_backend=shadow,
            primary_backend=primary,
        )
        
        if not readiness.ready:
            # 记录警告日志并输出 remediation 提示
            logger.warning(
                f"[DualRead] Shadow 后端就绪性检查未通过: "
                f"health={readiness.health_check_passed}, "
                f"stats={readiness.stats_available}, "
                f"doc_count={readiness.doc_count_passed}"
            )
            for hint in readiness.remediation_hints:
                logger.warning(f"[DualRead] Remediation: {hint}")
            
            # 如果使用 fail_open 模式且 shadow 不就绪，回退到 primary
            if config.fail_open:
                logger.warning(
                    f"[DualRead] Shadow 后端未就绪但 fail_open=True，回退到 primary 后端。"
                    f"建议操作: {'; '.join(readiness.remediation_hints[:1]) if readiness.remediation_hints else '检查 shadow 后端配置'}"
                )
                return query_evidence(
                    query_text=query_text,
                    filters=filters,
                    top_k=top_k,
                    backend=primary,
                    embedding_provider=embedding_provider,
                )
            else:
                # fail_open=False 时抛出错误
                error_msg = (
                    f"Shadow 后端就绪性检查失败，无法使用 {strategy} 策略。\n"
                    f"检查结果: health={readiness.health_check_passed}, "
                    f"doc_count={readiness.doc_count} (阈值: {readiness.doc_count_min_threshold})\n"
                )
                if readiness.remediation_hints:
                    error_msg += "Remediation 建议:\n"
                    for hint in readiness.remediation_hints:
                        error_msg += f"  - {hint}\n"
                raise ValueError(error_msg)
        else:
            logger.info(
                f"[DualRead] Shadow 后端就绪性检查通过: "
                f"doc_count={readiness.doc_count}, strategy={strategy}"
            )
    
    # shadow_only 策略：仅使用 shadow 后端
    if strategy == DualReadConfig.STRATEGY_SHADOW_ONLY:
        logger.info(f"[DualRead] 使用 shadow_only 策略")
        return query_evidence(
            query_text=query_text,
            filters=filters,
            top_k=top_k,
            backend=shadow,
            embedding_provider=embedding_provider,
        )
    
    # shadow_only_compare 策略：返回 shadow 结果，但仍查询 primary 做对比记录
    # 用于切换验证：流量已切到 shadow，但仍需记录与 primary 差异以便回滚决策
    if strategy == DualReadConfig.STRATEGY_SHADOW_ONLY_COMPARE:
        logger.info(f"[DualRead] 使用 shadow_only_compare 策略")
        
        # 查询 shadow（作为主要结果）
        shadow_results: List[EvidenceResult] = []
        shadow_error: Optional[Exception] = None
        try:
            shadow_results = query_evidence(
                query_text=query_text,
                filters=filters,
                top_k=top_k,
                backend=shadow,
                embedding_provider=embedding_provider,
            )
        except Exception as e:
            shadow_error = e
            logger.error(f"[DualRead] Shadow 查询失败（shadow_only_compare 模式）: {e}")
        
        # 查询 primary（用于对比，不影响返回结果）
        primary_results_for_compare: List[EvidenceResult] = []
        primary_error_for_compare: Optional[Exception] = None
        try:
            primary_results_for_compare = query_evidence(
                query_text=query_text,
                filters=filters,
                top_k=top_k,
                backend=primary,
                embedding_provider=embedding_provider,
            )
        except Exception as e:
            primary_error_for_compare = e
            logger.warning(f"[DualRead] Primary 查询失败（仅用于对比）: {e}")
        
        # 比较结果并记录日志
        if config.log_diff and shadow_results and primary_results_for_compare:
            diff = _compare_results(
                primary_results_for_compare,
                shadow_results,
                config.diff_threshold
            )
            if not diff["match"]:
                logger.warning(
                    f"[DualRead/shadow_only_compare] 结果不匹配: "
                    f"primary={diff['primary_count']}, shadow={diff['shadow_count']}, "
                    f"common={diff['common_count']}, "
                    f"only_primary={len(diff['only_primary'])}, "
                    f"only_shadow={len(diff['only_shadow'])}, "
                    f"score_diffs={len(diff['score_diffs'])}"
                )
            else:
                logger.info(f"[DualRead/shadow_only_compare] 结果匹配: count={diff['shadow_count']}")
        elif config.log_diff and not primary_results_for_compare and shadow_results:
            logger.warning(
                f"[DualRead/shadow_only_compare] Primary 无结果或失败，无法对比。"
                f"Shadow 返回 {len(shadow_results)} 条结果"
            )
        
        # shadow_only_compare 模式下，shadow 失败则抛出异常
        if shadow_error is not None:
            raise shadow_error
        
        return shadow_results
    
    # 获取 shadow 超时配置
    shadow_timeout_ms = config.shadow_timeout_ms if config.shadow_timeout_ms > 0 else 5000
    
    # fallback 策略：优先使用 primary，失败或无结果时 fallback 到 shadow
    if strategy == DualReadConfig.STRATEGY_FALLBACK:
        try:
            primary_results = query_evidence(
                query_text=query_text,
                filters=filters,
                top_k=top_k,
                backend=primary,
                embedding_provider=embedding_provider,
            )
            if primary_results:
                return primary_results
            
            # primary 无结果，fallback 到 shadow（带超时）
            logger.info(f"[DualRead] Primary 无结果，fallback 到 shadow")
            return execute_with_timeout(
                query_evidence,
                shadow_timeout_ms,
                query_text=query_text,
                filters=filters,
                top_k=top_k,
                backend=shadow,
                embedding_provider=embedding_provider,
            )
        except Exception as e:
            # primary 失败，fallback 到 shadow（带超时）
            logger.warning(f"[DualRead] Primary 查询失败 ({e})，fallback 到 shadow")
            return execute_with_timeout(
                query_evidence,
                shadow_timeout_ms,
                query_text=query_text,
                filters=filters,
                top_k=top_k,
                backend=shadow,
                embedding_provider=embedding_provider,
            )
    
    # compare 策略（默认）：同时查询两个后端并比较结果
    logger.debug(f"[DualRead] 使用 compare 策略")
    
    # 查询 primary
    primary_results: List[EvidenceResult] = []
    primary_error: Optional[Exception] = None
    try:
        primary_results = query_evidence(
            query_text=query_text,
            filters=filters,
            top_k=top_k,
            backend=primary,
            embedding_provider=embedding_provider,
        )
    except Exception as e:
        primary_error = e
        logger.warning(f"[DualRead] Primary 查询失败: {e}")
    
    # 查询 shadow（带超时）
    shadow_results: List[EvidenceResult] = []
    shadow_error: Optional[Exception] = None
    try:
        shadow_results = execute_with_timeout(
            query_evidence,
            shadow_timeout_ms,
            query_text=query_text,
            filters=filters,
            top_k=top_k,
            backend=shadow,
            embedding_provider=embedding_provider,
        )
    except ShadowQueryTimeoutError as e:
        shadow_error = e
        logger.warning(f"[DualRead] Shadow 查询超时: {e}")
    except Exception as e:
        shadow_error = e
        logger.warning(f"[DualRead] Shadow 查询失败: {e}")
    
    # 比较结果并记录日志
    if config.log_diff and primary_results and shadow_results:
        diff = _compare_results(
            primary_results, 
            shadow_results, 
            config.diff_threshold
        )
        if not diff["match"]:
            logger.warning(
                f"[DualRead] 结果不匹配: "
                f"primary={diff['primary_count']}, shadow={diff['shadow_count']}, "
                f"common={diff['common_count']}, "
                f"only_primary={len(diff['only_primary'])}, "
                f"only_shadow={len(diff['only_shadow'])}, "
                f"score_diffs={len(diff['score_diffs'])}"
            )
            if diff['score_diffs']:
                for sd in diff['score_diffs'][:3]:  # 最多记录3个
                    logger.debug(
                        f"[DualRead] 分数差异: chunk_id={sd['chunk_id']}, "
                        f"primary={sd['primary_score']:.4f}, "
                        f"shadow={sd['shadow_score']:.4f}, "
                        f"diff={sd['diff']:.4f}"
                    )
        else:
            logger.debug(f"[DualRead] 结果匹配: count={diff['primary_count']}")
    
    # 返回 primary 结果（如果 primary 失败则返回 shadow 结果）
    if primary_error is not None and shadow_results:
        logger.info(f"[DualRead] Primary 失败，使用 shadow 结果")
        return shadow_results
    
    if primary_error is not None:
        # 两者都失败，抛出 primary 的错误
        raise primary_error
    
    return primary_results


def _build_retrieval_context(
    backend: Optional[IndexBackend],
    provider: Optional[EmbeddingProvider],
    filters: Optional[QueryFilters] = None,
    top_k: int = 10,
    min_score: float = 0.0,
) -> RetrievalContext:
    """
    构建检索上下文对象
    
    从后端实例和 embedding provider 中提取配置信息，
    组装成 RetrievalContext 用于追溯和调试。
    
    Args:
        backend: 索引后端实例（可能为 None，使用全局实例）
        provider: Embedding Provider 实例
        filters: 查询过滤条件
        top_k: 返回数量
        min_score: 最小分数阈值
    
    Returns:
        RetrievalContext 实例
    """
    # 如果 backend 为 None，尝试获取全局实例
    actual_backend = backend or get_index_backend()
    
    context = RetrievalContext()
    
    # 填充后端信息
    if actual_backend is not None:
        context.backend_name = getattr(actual_backend, 'backend_name', None)
        
        # 获取 collection_id（优先使用 canonical_id，回退到 collection_id）
        context.collection_id = (
            getattr(actual_backend, 'canonical_id', None) or 
            getattr(actual_backend, 'collection_id', None)
        )
        
        # 构建后端配置摘要（不泄漏密码）
        backend_config: Dict[str, Any] = {}
        
        # 通用属性
        if hasattr(actual_backend, 'backend_name'):
            backend_config["type"] = actual_backend.backend_name
        
        # PGVector 特有属性
        if hasattr(actual_backend, '_schema'):
            backend_config["schema"] = actual_backend._schema
        if hasattr(actual_backend, '_table_name'):
            backend_config["table"] = actual_backend._table_name
        if hasattr(actual_backend, '_collection_strategy'):
            strategy = actual_backend._collection_strategy
            backend_config["strategy"] = getattr(strategy, 'strategy_name', type(strategy).__name__)
        
        # SeekDB 特有属性
        if hasattr(actual_backend, '_namespace'):
            backend_config["namespace"] = actual_backend._namespace
        if hasattr(actual_backend, '_config'):
            seekdb_config = actual_backend._config
            if hasattr(seekdb_config, 'host'):
                backend_config["host"] = seekdb_config.host
            if hasattr(seekdb_config, 'port'):
                backend_config["port"] = seekdb_config.port
        
        if backend_config:
            context.backend_config = backend_config
        
        # 获取 hybrid 配置
        if hasattr(actual_backend, '_hybrid_config'):
            hybrid_cfg = actual_backend._hybrid_config
            context.hybrid_config = {
                "vector_weight": getattr(hybrid_cfg, 'vector_weight', None),
                "text_weight": getattr(hybrid_cfg, 'text_weight', None),
            }
            if hasattr(hybrid_cfg, 'normalize_scores'):
                context.hybrid_config["normalize_scores"] = hybrid_cfg.normalize_scores
            # 清理 None 值
            context.hybrid_config = {k: v for k, v in context.hybrid_config.items() if v is not None}
    
    # 填充 Embedding 模型信息
    if provider is not None:
        context.embedding_model_id = provider.model_id
        context.embedding_dim = provider.dim
        # 获取 normalize 属性（如果存在）
        context.embedding_normalize = getattr(provider, 'normalize', None)
    
    # 填充查询请求参数
    query_request: Dict[str, Any] = {
        "top_k": top_k,
    }
    if min_score > 0:
        query_request["min_score"] = min_score
    if filters is not None:
        filter_dsl = filters.to_filter_dict()
        if filter_dsl:
            query_request["filters"] = filter_dsl
    context.query_request = query_request
    
    return context


def run_query(
    query_text: str,
    filters: Optional[QueryFilters] = None,
    top_k: int = 10,
    backend: Optional[IndexBackend] = None,
    shadow_backend: Optional[IndexBackend] = None,
    dual_read_config: Optional[DualReadConfig] = None,
    embedding_provider: Optional[EmbeddingProvider] = None,
    enable_compare: bool = False,
    compare_mode: str = "summary",
    compare_thresholds: Optional[CompareThresholds] = None,
    enable_dual_read: bool = False,
    dual_read_gate_thresholds: Optional[DualReadGateThresholds] = None,
    dual_read_report: bool = False,
    dual_read_report_mode: str = "summary",
) -> QueryResult:
    """
    执行单次查询
    
    自动支持双读：当 DualReadConfig 启用时，使用 query_evidence_dual_read
    执行双读策略；否则直接调用 query_evidence，无额外开销。
    
    当 enable_compare=True 时，会分别查询 primary 和 shadow 后端，
    生成 CompareReport 并附加到结果中。
    
    当 enable_dual_read=True 时，会分别查询 primary 和 shadow 后端，
    生成 DualReadStats 并附加到结果中（轻量级统计）。
    
    Args:
        query_text: 查询文本
        filters: 过滤条件
        top_k: 返回数量
        backend: Primary 索引后端
        shadow_backend: Shadow 索引后端（用于双读）
        dual_read_config: 双读配置
        embedding_provider: Embedding Provider
        enable_compare: 是否启用双读比较（生成 CompareReport）
        compare_mode: 比较模式（summary/detailed）
        compare_thresholds: 比较阈值配置
        enable_dual_read: 是否启用双读统计（生成 DualReadStats）
        dual_read_gate_thresholds: 双读门禁阈值配置（可选）
    """
    import uuid
    
    result = QueryResult(
        query=query_text,
        filters=filters,
        top_k=top_k,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    
    start_time = datetime.now(timezone.utc)
    
    # 获取 embedding provider 并记录模型信息
    provider = embedding_provider or get_embedding_provider_instance()
    if provider:
        result.embedding_model_id = provider.model_id
        result.embedding_dim = provider.dim
    
    try:
        # 当启用双读比较且 shadow 后端可用时，分别查询并生成比较报告
        if enable_compare and shadow_backend is not None:
            request_id = str(uuid.uuid4())[:8]
            
            # 查询 primary 后端
            primary_start = datetime.now(timezone.utc)
            primary_results = query_evidence(
                query_text=query_text,
                filters=filters,
                top_k=top_k,
                backend=backend,
                embedding_provider=provider,
            )
            primary_end = datetime.now(timezone.utc)
            primary_latency_ms = (primary_end - primary_start).total_seconds() * 1000
            
            # 查询 shadow 后端（带超时支持）
            shadow_start = datetime.now(timezone.utc)
            shadow_results: List[EvidenceResult] = []
            shadow_error: Optional[Exception] = None
            shadow_timed_out = False
            
            # 获取 shadow 超时配置
            shadow_timeout_ms = 5000  # 默认 5 秒
            if dual_read_config is not None and dual_read_config.shadow_timeout_ms > 0:
                shadow_timeout_ms = dual_read_config.shadow_timeout_ms
            
            try:
                # 使用线程池超时封装执行 shadow 查询
                shadow_results = execute_with_timeout(
                    query_evidence,
                    shadow_timeout_ms,
                    query_text=query_text,
                    filters=filters,
                    top_k=top_k,
                    backend=shadow_backend,
                    embedding_provider=provider,
                )
            except ShadowQueryTimeoutError as e:
                shadow_error = e
                shadow_timed_out = True
                logger.warning(f"[DualReadCompare] Shadow 查询超时: {e}")
            except Exception as e:
                shadow_error = e
                logger.warning(f"[DualReadCompare] Shadow 查询失败: {e}")
            shadow_end = datetime.now(timezone.utc)
            shadow_latency_ms = (shadow_end - shadow_start).total_seconds() * 1000
            
            # 根据策略决定使用哪个结果作为返回值
            # shadow_only_compare 策略：返回 shadow 结果（即使生成了对比报告）
            strategy = dual_read_config.strategy if dual_read_config else None
            if strategy == DUAL_READ_STRATEGY_SHADOW_ONLY_COMPARE and shadow_error is None:
                result.evidences = shadow_results
                logger.info(f"[DualReadCompare] shadow_only_compare 策略：返回 shadow 结果 ({len(shadow_results)} 条)")
            else:
                # 默认使用 primary 结果
                result.evidences = primary_results
            
            # 生成比较报告（即使 shadow 失败也生成）
            if shadow_error is None:
                # 获取后端名称
                primary_name = getattr(backend, 'backend_name', 'primary') if backend else 'primary'
                shadow_name = getattr(shadow_backend, 'backend_name', 'shadow')
                
                compare_report = generate_compare_report(
                    primary_results=primary_results,
                    shadow_results=shadow_results,
                    primary_latency_ms=primary_latency_ms,
                    shadow_latency_ms=shadow_latency_ms,
                    thresholds=compare_thresholds,
                    request_id=request_id,
                    primary_backend_name=primary_name,
                    shadow_backend_name=shadow_name,
                    compare_mode=compare_mode,
                )
                result.compare_report = compare_report
                
                # 记录比较结果日志
                if compare_report.decision:
                    if compare_report.decision.passed:
                        if compare_report.decision.has_warnings:
                            logger.warning(
                                f"[DualReadCompare] {compare_report.decision.reason}, "
                                f"recommendation={compare_report.decision.recommendation}"
                            )
                        else:
                            logger.info(
                                f"[DualReadCompare] {compare_report.decision.reason}, "
                                f"overlap={compare_report.metrics.hit_overlap_ratio:.4f}"
                            )
                    else:
                        logger.error(
                            f"[DualReadCompare] {compare_report.decision.reason}, "
                            f"violations={compare_report.decision.violated_checks}"
                        )
            else:
                # Shadow 失败，根据 fail_open 决定行为
                # fail_open=True（默认）: shadow 失败不影响 primary 返回，但记录 shadow_error
                # fail_open=False: shadow 失败导致 compare 失败（用于 Nightly/切换门禁）
                fail_open = True  # 默认值
                if dual_read_config is not None:
                    fail_open = dual_read_config.fail_open
                
                primary_name = getattr(backend, 'backend_name', 'primary') if backend else 'primary'
                shadow_name = getattr(shadow_backend, 'backend_name', 'shadow')
                
                if fail_open:
                    # fail_open=True: shadow 失败不影响，compare 视为通过但带警告
                    result.compare_report = CompareReport(
                        request_id=request_id,
                        decision=CompareDecision(
                            passed=True,
                            has_warnings=True,
                            reason=f"Shadow 查询失败 (fail_open=True): {shadow_error}",
                            recommendation="investigate_required",
                        ),
                        primary_backend=primary_name,
                        secondary_backend=shadow_name,
                        metadata={
                            "shadow_error": str(shadow_error),
                            "fail_open": True,
                        },
                    )
                    logger.warning(
                        f"[DualReadCompare] Shadow 查询失败 (fail_open=True): {shadow_error}，"
                        f"返回 primary 结果"
                    )
                else:
                    # fail_open=False: shadow 失败导致 compare 失败
                    result.compare_report = CompareReport(
                        request_id=request_id,
                        decision=CompareDecision(
                            passed=False,
                            reason=f"Shadow 查询失败 (fail_open=False): {shadow_error}",
                            recommendation="abort_switch",
                            violated_checks=["shadow_query_failed"],
                        ),
                        primary_backend=primary_name,
                        secondary_backend=shadow_name,
                        metadata={
                            "shadow_error": str(shadow_error),
                            "fail_open": False,
                        },
                    )
                    logger.error(
                        f"[DualReadCompare] Shadow 查询失败 (fail_open=False): {shadow_error}，"
                        f"compare 失败，退出码将非 0"
                    )
        # 当启用 --dual-read 且 shadow 后端可用时，分别查询并生成轻量级统计
        elif enable_dual_read and shadow_backend is not None:
            # 查询 primary 后端
            primary_start = datetime.now(timezone.utc)
            primary_results = query_evidence(
                query_text=query_text,
                filters=filters,
                top_k=top_k,
                backend=backend,
                embedding_provider=provider,
            )
            primary_end = datetime.now(timezone.utc)
            primary_latency_ms = (primary_end - primary_start).total_seconds() * 1000
            
            # 查询 shadow 后端（带超时支持）
            shadow_start = datetime.now(timezone.utc)
            shadow_results: List[EvidenceResult] = []
            shadow_error_str: Optional[str] = None
            shadow_timed_out_flag: bool = False
            
            # 获取 shadow 超时配置
            shadow_timeout_ms_cfg: int = 5000  # 默认 5 秒
            if dual_read_config is not None and dual_read_config.shadow_timeout_ms > 0:
                shadow_timeout_ms_cfg = dual_read_config.shadow_timeout_ms
            
            try:
                # 使用线程池超时封装执行 shadow 查询
                shadow_results = execute_with_timeout(
                    query_evidence,
                    shadow_timeout_ms_cfg,
                    query_text=query_text,
                    filters=filters,
                    top_k=top_k,
                    backend=shadow_backend,
                    embedding_provider=provider,
                )
            except ShadowQueryTimeoutError as e:
                shadow_error_str = str(e)
                shadow_timed_out_flag = True
                logger.warning(f"[DualRead] Shadow 查询超时: {e}")
            except Exception as e:
                shadow_error_str = str(e)
                logger.warning(f"[DualRead] Shadow 查询失败: {e}")
            shadow_end = datetime.now(timezone.utc)
            shadow_latency_ms = (shadow_end - shadow_start).total_seconds() * 1000
            
            # 根据策略决定使用哪个结果作为返回值
            # shadow_only_compare 策略：返回 shadow 结果
            strategy = dual_read_config.strategy if dual_read_config else None
            if strategy == DUAL_READ_STRATEGY_SHADOW_ONLY_COMPARE and not shadow_error_str:
                result.evidences = shadow_results
                logger.info(f"[DualRead] shadow_only_compare 策略：返回 shadow 结果 ({len(shadow_results)} 条)")
            else:
                # 默认使用 primary 结果
                result.evidences = primary_results
            
            # 计算双读统计信息
            dual_read_stats = compute_dual_read_stats(
                primary_results=primary_results,
                shadow_results=shadow_results,
                primary_backend=backend,
                shadow_backend=shadow_backend,
                primary_latency_ms=primary_latency_ms,
                shadow_latency_ms=shadow_latency_ms,
                shadow_error=shadow_error_str,
                shadow_timed_out=shadow_timed_out_flag,
            )
            
            # 执行门禁检查（如果配置了阈值）
            if dual_read_gate_thresholds is not None and dual_read_gate_thresholds.has_thresholds():
                gate_result = check_dual_read_gate(dual_read_stats, dual_read_gate_thresholds)
                dual_read_stats.gate = gate_result
                
                # 记录门禁结果
                if not gate_result.passed:
                    violation_names = [v.check_name for v in gate_result.violations]
                    logger.warning(
                        f"[DualRead] 门禁检查失败: violations={violation_names}"
                    )
                else:
                    logger.debug("[DualRead] 门禁检查通过")
            
            # 根据 fail_open 决定 shadow 失败时的行为
            # fail_open=True（默认）: shadow 失败不影响 primary 返回
            # fail_open=False: shadow 失败导致门禁失败（用于 Nightly/切换门禁）
            fail_open = True  # 默认值
            if dual_read_config is not None:
                fail_open = dual_read_config.fail_open
            
            if shadow_error_str is not None and not fail_open:
                # fail_open=False 且 shadow 失败，创建/更新门禁失败结果
                shadow_fail_violation = DualReadGateViolation(
                    check_name="shadow_query_failed",
                    threshold_value=0.0,
                    actual_value=1.0,
                    message=f"Shadow 查询失败 (fail_open=False): {shadow_error_str}",
                )
                
                if dual_read_stats.gate is None:
                    # 创建新的门禁失败结果
                    dual_read_stats.gate = DualReadGateResult(
                        passed=False,
                        violations=[shadow_fail_violation],
                        thresholds_applied=dual_read_gate_thresholds,
                    )
                else:
                    # 更新现有门禁结果，添加 shadow 失败违规
                    dual_read_stats.gate.passed = False
                    dual_read_stats.gate.violations.append(shadow_fail_violation)
                
                logger.error(
                    f"[DualRead] Shadow 查询失败 (fail_open=False): {shadow_error_str}，"
                    f"门禁失败，退出码将非 0"
                )
            
            result.dual_read_stats = dual_read_stats
            
            # 可选地生成 CompareReport（当 dual_read_report=True 时）
            if dual_read_report and shadow_error_str is None:
                request_id = str(uuid.uuid4())[:8]
                
                # 获取后端名称
                primary_name = getattr(backend, 'backend_name', 'primary') if backend else 'primary'
                shadow_name = getattr(shadow_backend, 'backend_name', 'shadow')
                
                # 使用 compare_thresholds，如果未提供则从环境变量加载
                report_thresholds = compare_thresholds
                if report_thresholds is None:
                    report_thresholds = CompareThresholds.from_env()
                
                compare_report = generate_compare_report(
                    primary_results=primary_results,
                    shadow_results=shadow_results,
                    primary_latency_ms=primary_latency_ms,
                    shadow_latency_ms=shadow_latency_ms,
                    thresholds=report_thresholds,
                    request_id=request_id,
                    primary_backend_name=primary_name,
                    shadow_backend_name=shadow_name,
                    compare_mode=dual_read_report_mode,
                )
                result.compare_report = compare_report
                
                # 记录比较结果日志
                if compare_report.decision:
                    if compare_report.decision.passed:
                        if compare_report.decision.has_warnings:
                            logger.warning(
                                f"[DualRead] 比较报告: {compare_report.decision.reason}, "
                                f"recommendation={compare_report.decision.recommendation}"
                            )
                        else:
                            logger.info(
                                f"[DualRead] 比较报告: {compare_report.decision.reason}, "
                                f"overlap={compare_report.metrics.hit_overlap_ratio:.4f}"
                            )
                    else:
                        logger.error(
                            f"[DualRead] 比较报告失败: {compare_report.decision.reason}, "
                            f"violations={compare_report.decision.violated_checks}"
                        )
            
            # 记录统计日志
            if shadow_error_str:
                logger.warning(f"[DualRead] Shadow 查询失败: {shadow_error_str}")
            else:
                logger.info(
                    f"[DualRead] overlap_ratio={dual_read_stats.overlap_ratio:.4f}, "
                    f"primary={dual_read_stats.primary_count}, shadow={dual_read_stats.shadow_count}, "
                    f"common={dual_read_stats.common_count}, "
                    f"score_diff_mean={dual_read_stats.score_diff_mean:.4f}, "
                    f"score_diff_max={dual_read_stats.score_diff_max:.4f}"
                )
        else:
            # 常规查询路径（不启用比较或无 shadow 后端）
            evidences = query_evidence_dual_read(
                query_text=query_text,
                filters=filters,
                top_k=top_k,
                primary_backend=backend,
                shadow_backend=shadow_backend,
                dual_read_config=dual_read_config,
                embedding_provider=provider,
            )
            result.evidences = evidences
        
        # 填充检索上下文（retrieval_context）
        result.retrieval_context = _build_retrieval_context(
            backend=backend,
            provider=provider,
            filters=filters,
            top_k=top_k,
        )
    except Exception as e:
        result.error = str(e)
        logger.error(f"检索失败: {e}")
    
    end_time = datetime.now(timezone.utc)
    result.completed_at = end_time.isoformat()
    result.duration_ms = (end_time - start_time).total_seconds() * 1000
    
    return result


def run_batch_query(
    queries: List[str],
    filters: Optional[QueryFilters] = None,
    top_k: int = 10,
    backend: Optional[IndexBackend] = None,
    shadow_backend: Optional[IndexBackend] = None,
    dual_read_config: Optional[DualReadConfig] = None,
    embedding_provider: Optional[EmbeddingProvider] = None,
    enable_compare: bool = False,
    compare_mode: str = "summary",
    compare_thresholds: Optional[CompareThresholds] = None,
    enable_dual_read: bool = False,
    dual_read_gate_thresholds: Optional[DualReadGateThresholds] = None,
    dual_read_report: bool = False,
    dual_read_report_mode: str = "summary",
) -> List[QueryResult]:
    """
    执行批量查询
    
    Args:
        queries: 查询文本列表
        filters: 过滤条件
        top_k: 返回数量
        backend: Primary 索引后端
        shadow_backend: Shadow 索引后端（用于双读）
        dual_read_config: 双读配置
        embedding_provider: Embedding Provider
        enable_compare: 是否启用双读比较
        compare_mode: 比较模式（summary/detailed）
        compare_thresholds: 比较阈值配置
        enable_dual_read: 是否启用双读统计
        dual_read_gate_thresholds: 双读门禁阈值配置
        dual_read_report: 是否为 dual_read 生成 CompareReport
        dual_read_report_mode: dual_read_report 的报告模式
    """
    results = []
    for i, query_text in enumerate(queries):
        logger.info(f"处理查询 {i+1}/{len(queries)}: {query_text[:50]}...")
        result = run_query(
            query_text=query_text,
            filters=filters,
            top_k=top_k,
            backend=backend,
            shadow_backend=shadow_backend,
            dual_read_config=dual_read_config,
            embedding_provider=embedding_provider,
            enable_compare=enable_compare,
            compare_mode=compare_mode,
            compare_thresholds=compare_thresholds,
            enable_dual_read=enable_dual_read,
            dual_read_gate_thresholds=dual_read_gate_thresholds,
            dual_read_report=dual_read_report,
            dual_read_report_mode=dual_read_report_mode,
        )
        results.append(result)
    return results


# ============ CLI 部分 ============


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="Step3 证据检索工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # Makefile 入口（推荐）
    make step3-query QUERY='修复登录页面 XSS 漏洞'
    make step3-query QUERY='数据库优化' PROJECT_KEY=webapp SOURCE_TYPE=git
    make step3-query QUERY='内存泄漏修复' JSON_OUTPUT=1
    make step3-query QUERY_FILE=queries.txt JSON_OUTPUT=1

    # 直接调用
    python -m seek_query --query "修复登录页面 XSS 漏洞"
    python -m seek_query --query "数据库优化" --project-key webapp --source-type git
    python -m seek_query --query "内存泄漏" --top-k 20
    python -m seek_query --query "性能优化" --json
    python -m seek_query --query-file queries.txt --json
    python -m seek_query --query "bug fix" --json --output-format packet

    # 使用内置查询集（CI/CD 场景）
    python -m seek_query --query-set nightly_default --dual-read --json

环境变量:
    PROJECT_KEY     默认项目标识
    TOP_K           默认返回数量（默认 10）
        """,
    )
    
    add_config_argument(parser)
    
    # 查询参数（互斥组）
    query_group = parser.add_mutually_exclusive_group(required=True)
    query_group.add_argument(
        "--query", "-q",
        type=str,
        help="查询文本",
    )
    query_group.add_argument(
        "--query-file",
        type=str,
        help="从文件读取查询（每行一个）",
    )
    query_group.add_argument(
        "--query-set",
        type=str,
        choices=list(BUILTIN_QUERY_SETS.keys()),
        help=f"使用内置查询集（可选: {', '.join(BUILTIN_QUERY_SETS.keys())}）",
    )
    
    # 过滤参数
    parser.add_argument(
        "--project-key",
        type=str,
        default=os.environ.get("PROJECT_KEY"),
        help="项目标识过滤",
    )
    parser.add_argument(
        "--source-type",
        type=str,
        choices=["svn", "git", "logbook"],
        default=None,
        help="来源类型过滤",
    )
    parser.add_argument(
        "--owner",
        type=str,
        default=None,
        help="所有者用户 ID 过滤",
    )
    parser.add_argument(
        "--module",
        type=str,
        default=None,
        help="模块/路径前缀过滤",
    )
    parser.add_argument(
        "--time-start",
        type=str,
        default=None,
        help="时间范围起始（ISO 格式）",
    )
    parser.add_argument(
        "--time-end",
        type=str,
        default=None,
        help="时间范围结束（ISO 格式）",
    )
    
    # 结果参数
    parser.add_argument(
        "--top-k", "-k",
        type=int,
        default=int(os.environ.get("TOP_K", "10")),
        help="返回结果数量（默认 10）",
    )
    
    # Collection 参数
    parser.add_argument(
        "--collection",
        type=str,
        default=os.environ.get("COLLECTION"),
        help="指定 collection 名称（不指定则读取 active_collection）",
    )
    
    # 输出选项
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果",
    )
    parser.add_argument(
        "--output-format",
        type=str,
        choices=["full", "packet"],
        default="full",
        help="输出格式: full(完整结果)/packet(仅Evidence Packet)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="显示详细输出",
    )
    
    # 双读比较选项
    dual_read_group = parser.add_argument_group("双读比较选项")
    dual_read_group.add_argument(
        "--dual-read-compare",
        action="store_true",
        help="启用双读比较功能：同时查询 primary 和 shadow 后端并对比结果",
    )
    dual_read_group.add_argument(
        "--compare-mode",
        type=str,
        choices=["off", "summary", "detailed"],
        default="summary",
        help="比较模式: off(关闭)/summary(摘要)/detailed(详细)，默认 summary",
    )
    dual_read_group.add_argument(
        "--compare-thresholds",
        type=str,
        default=None,
        help="比较阈值配置（JSON 格式），例如: '{\"hit_overlap_min_fail\": 0.5}'",
    )
    
    # 双读开关（新增，用于轻量级 dual-read 对比）
    dual_read_group.add_argument(
        "--dual-read",
        action="store_true",
        help="启用双读模式：同时查询 primary 和 shadow 后端，输出 overlap/diff 统计（仅 pgvector）",
    )
    dual_read_group.add_argument(
        "--dual-read-strategy",
        type=str,
        choices=["compare", "fallback", "shadow_only", "shadow_only_compare"],
        default=None,
        help=(
            "双读策略: compare(对比模式,返回primary)/fallback(主备模式)/"
            "shadow_only(仅shadow)/shadow_only_compare(返回shadow但仍对比)。"
            "默认从 STEP3_PGVECTOR_DUAL_READ_STRATEGY 读取"
        ),
    )
    dual_read_group.add_argument(
        "--shadow-strategy",
        type=str,
        choices=["per_table", "single_table"],
        default=os.environ.get("STEP3_PGVECTOR_SHADOW_STRATEGY"),
        help="Shadow 后端的 collection 策略（默认从 STEP3_PGVECTOR_SHADOW_STRATEGY 读取）",
    )
    dual_read_group.add_argument(
        "--shadow-table",
        type=str,
        default=os.environ.get("STEP3_PGVECTOR_SHADOW_TABLE", "chunks_shadow"),
        help="Shadow 后端的表名（默认从 STEP3_PGVECTOR_SHADOW_TABLE 读取，默认 chunks_shadow）",
    )
    
    # 双读门禁阈值参数
    dual_read_gate_group = parser.add_argument_group("双读门禁阈值选项（与 --dual-read 配合使用）")
    dual_read_gate_group.add_argument(
        "--dual-read-min-overlap",
        type=float,
        default=None,
        metavar="RATIO",
        help="门禁：TopK 重叠率（Jaccard）最小阈值，范围 [0.0, 1.0]，低于此值视为失败",
    )
    dual_read_gate_group.add_argument(
        "--dual-read-max-only-primary",
        type=int,
        default=None,
        metavar="COUNT",
        help="门禁：仅在 primary 中出现的 chunk 数量上限，超过此值视为失败",
    )
    dual_read_gate_group.add_argument(
        "--dual-read-max-only-shadow",
        type=int,
        default=None,
        metavar="COUNT",
        help="门禁：仅在 shadow 中出现的 chunk 数量上限，超过此值视为失败",
    )
    dual_read_gate_group.add_argument(
        "--dual-read-max-score-drift",
        type=float,
        default=None,
        metavar="DRIFT",
        help="门禁：最大分数漂移阈值，超过此值视为失败",
    )
    dual_read_gate_group.add_argument(
        "--dual-read-report",
        action="store_true",
        help="为 --dual-read 生成 CompareReport（复用 --dual-read-compare 的报告生成逻辑）",
    )
    dual_read_gate_group.add_argument(
        "--dual-read-report-mode",
        type=str,
        choices=["summary", "detailed"],
        default="summary",
        help="--dual-read-report 的报告模式: summary(摘要)/detailed(详细)，默认 summary",
    )
    
    # fail_open 参数（互斥组）
    fail_open_group = dual_read_gate_group.add_mutually_exclusive_group()
    fail_open_group.add_argument(
        "--fail-open",
        dest="fail_open",
        action="store_true",
        default=None,
        help="Shadow 失败时不影响 primary 返回（默认行为，用于生产环境灰度）",
    )
    fail_open_group.add_argument(
        "--no-fail-open",
        dest="fail_open",
        action="store_false",
        help="Shadow 失败时导致 compare/dual-read 失败（用于 Nightly/切换门禁）",
    )
    
    # Logbook 集成选项
    logbook_group = parser.add_argument_group("Logbook 集成选项（用于批量查询/query-set 输出保存）")
    logbook_group.add_argument(
        "--log-to-logbook",
        action="store_true",
        help="将查询结果保存到 logbook.attachments（kind='report'）",
    )
    logbook_group.add_argument(
        "--save-attachment",
        action="store_true",
        help="将查询报告保存为 logbook.attachments（与 --log-to-logbook 等效）",
    )
    logbook_group.add_argument(
        "--item-id",
        type=int,
        default=None,
        help="用于关联的 item_id（需要 --log-to-logbook 或 --save-attachment）",
    )
    logbook_group.add_argument(
        "--actor",
        type=str,
        default=None,
        help="操作者用户 ID（用于 logbook 记录）",
    )
    
    # 添加后端选项
    add_backend_arguments(parser)
    
    return parser.parse_args()


def print_result(result: QueryResult):
    """打印检索结果（文本格式）"""
    print("\n" + "=" * 60)
    print("Step3 证据检索结果")
    print("=" * 60)
    
    print(f"\n【查询】")
    print(f"  {result.query}")
    
    if result.filters:
        filters = result.filters.to_dict()
        active_filters = {k: v for k, v in filters.items() if v is not None}
        if active_filters:
            print(f"\n【过滤条件】")
            for k, v in active_filters.items():
                print(f"  {k}: {v}")
    
    # Embedding 模型信息
    if result.embedding_model_id:
        print(f"\n【Embedding 模型】")
        print(f"  模型: {result.embedding_model_id}")
        print(f"  维度: {result.embedding_dim}")
    
    print(f"\n【结果统计】")
    print(f"  返回数量: {len(result.evidences)}/{result.top_k}")
    print(f"  耗时: {result.duration_ms:.2f} ms")
    
    if result.error:
        print(f"\n【错误】")
        print(f"  {result.error}")
    elif result.evidences:
        print(f"\n【检索结果】")
        for i, ev in enumerate(result.evidences, 1):
            print(f"\n  [{i}] {ev.source_type}:{ev.source_id} (score={ev.relevance_score:.3f})")
            print(f"      chunk_id: {ev.chunk_id}")
            print(f"      artifact_uri: {ev.artifact_uri}")
            if ev.excerpt:
                excerpt = ev.excerpt[:100] + "..." if len(ev.excerpt) > 100 else ev.excerpt
                print(f"      摘要: {excerpt}")
            if ev.metadata.get("project_key"):
                print(f"      project: {ev.metadata['project_key']}")
    else:
        print(f"\n  未找到相关结果")
    
    print("\n" + "=" * 60 + "\n")


def _print_compare_report(report: CompareReport) -> None:
    """打印双读比较报告（文本格式）"""
    print("\n" + "-" * 60)
    print("双读比较报告")
    print("-" * 60)
    
    if report.request_id:
        print(f"  请求 ID: {report.request_id}")
    
    print(f"  Primary: {report.primary_backend}")
    print(f"  Shadow:  {report.secondary_backend}")
    
    if report.metrics:
        m = report.metrics
        print(f"\n  【指标】")
        print(f"    命中重叠率: {m.hit_overlap_ratio:.4f} ({m.common_hit_count} 共同命中)")
        print(f"    Primary 命中: {m.primary_hit_count}, Shadow 命中: {m.secondary_hit_count}")
        print(f"    平均分数差异: {m.avg_score_diff:.4f}, 最大: {m.max_score_diff:.4f}")
        print(f"    平均排名漂移: {m.avg_rank_drift:.2f}, 最大: {m.max_rank_drift}")
        print(f"    延迟: primary={m.primary_latency_ms:.1f}ms, shadow={m.secondary_latency_ms:.1f}ms (比率={m.latency_ratio:.2f})")
    
    if report.decision:
        d = report.decision
        status = "通过" if d.passed else "失败"
        if d.has_warnings:
            status += " (有警告)"
        print(f"\n  【决策】")
        print(f"    状态: {status}")
        print(f"    原因: {d.reason}")
        if d.recommendation:
            print(f"    建议: {d.recommendation}")
        
        if d.violation_details:
            print(f"\n  【违规详情】")
            for v in d.violation_details:
                print(f"    [{v.level.upper()}] {v.check_name}: {v.actual_value:.4f} (阈值: {v.threshold_value:.4f})")
                print(f"           {v.reason}")
    
    print("-" * 60 + "\n")


def _print_dual_read_stats(stats: DualReadStats) -> None:
    """打印双读统计信息（文本格式）"""
    print("\n" + "-" * 60)
    print("双读统计信息")
    print("-" * 60)
    
    print(f"\n  【健康信息】")
    print(f"    Primary: table={stats.primary_table}, strategy={stats.primary_strategy}")
    if stats.primary_collection_id:
        print(f"             collection_id={stats.primary_collection_id}")
    print(f"    Shadow:  table={stats.shadow_table}, strategy={stats.shadow_strategy}")
    if stats.shadow_collection_id:
        print(f"             collection_id={stats.shadow_collection_id}")
    
    if stats.shadow_error:
        print(f"\n  【Shadow 错误】")
        print(f"    {stats.shadow_error}")
    else:
        print(f"\n  【重叠统计】")
        print(f"    overlap_ratio: {stats.overlap_ratio:.4f}")
        print(f"    primary_count: {stats.primary_count}")
        print(f"    shadow_count:  {stats.shadow_count}")
        print(f"    common_count:  {stats.common_count}")
        print(f"    only_primary:  {len(stats.only_primary)} 条")
        print(f"    only_shadow:   {len(stats.only_shadow)} 条")
        
        if stats.only_primary:
            display_list = stats.only_primary[:5]
            print(f"    only_primary 列表: {display_list}")
            if len(stats.only_primary) > 5:
                print(f"                       ...共 {len(stats.only_primary)} 条")
        
        if stats.only_shadow:
            display_list = stats.only_shadow[:5]
            print(f"    only_shadow 列表:  {display_list}")
            if len(stats.only_shadow) > 5:
                print(f"                       ...共 {len(stats.only_shadow)} 条")
        
        print(f"\n  【分数差异统计】")
        print(f"    score_diff_mean: {stats.score_diff_mean:.4f}")
        print(f"    score_diff_max:  {stats.score_diff_max:.4f}")
    
    print(f"\n  【延迟信息】")
    print(f"    primary: {stats.primary_latency_ms:.2f} ms")
    print(f"    shadow:  {stats.shadow_latency_ms:.2f} ms")
    
    print("-" * 60 + "\n")


def main() -> int:
    """主入口"""
    args = parse_args()
    
    # 设置日志级别
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    elif args.json:
        logging.getLogger().setLevel(logging.WARNING)
    
    # 验证 logbook 参数
    if (args.log_to_logbook or args.save_attachment) and not args.item_id:
        logger.error("使用 --log-to-logbook 或 --save-attachment 时必须指定 --item-id")
        if args.json:
            print(json.dumps({"success": False, "error": "missing --item-id for logbook integration"}))
        return 1
    
    # 构建过滤条件
    filters = None
    if any([args.project_key, args.source_type, args.owner, args.module, 
            args.time_start, args.time_end]):
        filters = QueryFilters(
            project_key=args.project_key,
            source_type=args.source_type,
            owner_user_id=args.owner,
            module=args.module,
            time_range_start=args.time_start,
            time_range_end=args.time_end,
        )
    
    # 加载配置并获取数据库连接（用于读取 active_collection）
    conn = None
    config = None
    try:
        config = get_config(args.config_path)
        config.load()
        conn = get_connection(config=config)
    except Exception as e:
        logger.debug(f"无法获取数据库连接，跳过 active_collection 读取: {e}")
    
    # 初始化索引后端（从环境变量/CLI 参数）
    backend = None
    try:
        backend = create_backend_from_args(args)
        set_index_backend(backend)
        logger.info(f"索引后端已初始化: {backend.backend_name}")
        
        # 初始化 pgvector 后端（若适用）
        if not try_initialize_pgvector_backend(backend):
            logger.warning("pgvector 初始化失败，继续运行但可能影响功能")
    except Exception as e:
        logger.warning(f"初始化索引后端失败，使用 stub 模式: {e}")
        backend = None
    
    # 解析要使用的 collection 并确保后端使用正确的 collection
    resolved_collection = None
    shadow_backend = None
    dual_read_config = None
    
    if backend is not None:
        # 获取 embedding provider
        provider = get_embedding_provider_instance()
        embedding_model_id = provider.model_id if provider else None
        
        # 解析 collection（优先显式指定 > active_collection > 默认）
        resolved_collection = resolve_collection_id(
            conn=conn,
            backend_name=backend.backend_name,
            project_key=args.project_key,
            embedding_model_id=embedding_model_id,
            explicit_collection_id=getattr(args, 'collection', None),
        )
        logger.info(f"解析 collection: {resolved_collection}")
        
        # 检查后端的 collection_id 是否与解析的 collection 一致
        backend_collection_id = getattr(backend, 'canonical_id', None) or getattr(backend, 'collection_id', None)
        if backend_collection_id != resolved_collection:
            logger.info(
                f"后端 collection ({backend_collection_id}) 与目标 collection ({resolved_collection}) 不一致，"
                f"重建后端实例"
            )
            # 重建后端，使用正确的 collection_id
            backend = create_backend_from_env(
                embedding_model_id=embedding_model_id,
                embedding_provider=provider,
                collection_id=resolved_collection,
            )
            set_index_backend(backend)
            logger.info(f"后端已重建，使用 collection: {resolved_collection}")
            # 初始化 pgvector 后端（若适用）
            if not try_initialize_pgvector_backend(backend):
                logger.warning("pgvector 初始化失败，继续运行但可能影响功能")
        
        # 按 DualReadConfig 决定是否创建 shadow_backend
        # 同时处理 --dual-read CLI 参数
        try:
            primary_config = PGVectorConfig.from_env()
            dual_read_config = DualReadConfig.from_env(primary_config.collection_strategy)
            
            # CLI 参数覆盖环境变量配置
            if hasattr(args, 'dual_read') and args.dual_read:
                # 检查是否为 pgvector 后端
                if backend.backend_name != 'pgvector':
                    logger.warning(
                        f"--dual-read 仅支持 pgvector 后端，当前后端为 '{backend.backend_name}'。"
                        f"请设置 STEP3_INDEX_BACKEND=pgvector 或使用 --backend pgvector"
                    )
                else:
                    dual_read_config.enabled = True
                    # CLI 参数覆盖 shadow_strategy
                    if hasattr(args, 'shadow_strategy') and args.shadow_strategy:
                        dual_read_config.shadow_strategy = args.shadow_strategy
                    # CLI 参数覆盖 shadow_table
                    if hasattr(args, 'shadow_table') and args.shadow_table:
                        dual_read_config.shadow_table = args.shadow_table
            
            # CLI 参数覆盖双读策略（--dual-read-strategy）
            if hasattr(args, 'dual_read_strategy') and args.dual_read_strategy:
                dual_read_config.strategy = args.dual_read_strategy
                logger.info(f"CLI 参数覆盖双读策略: {args.dual_read_strategy}")
            
            # CLI 参数覆盖 fail_open（适用于 --dual-read 和 --dual-read-compare）
            if hasattr(args, 'fail_open') and args.fail_open is not None:
                dual_read_config.fail_open = args.fail_open
                logger.info(f"CLI 参数覆盖 fail_open={args.fail_open}")
            
            set_dual_read_config(dual_read_config)
            
            if dual_read_config.enabled:
                logger.info(
                    f"双读已启用: strategy={dual_read_config.strategy}, "
                    f"shadow_strategy={dual_read_config.shadow_strategy}, "
                    f"shadow_table={dual_read_config.shadow_table}"
                )
                shadow_backend = create_shadow_backend_for_read(
                    primary_config=primary_config,
                    dual_read_config=dual_read_config,
                    embedding_model_id=embedding_model_id,
                    embedding_provider=provider,
                    collection_id=resolved_collection,
                )
                if shadow_backend is not None:
                    set_shadow_backend(shadow_backend)
                    # 初始化 shadow pgvector 后端（若适用）
                    if not try_initialize_pgvector_backend(shadow_backend):
                        logger.warning("Shadow pgvector 初始化失败，继续运行但可能影响双读功能")
                else:
                    logger.warning("双读配置已启用但 shadow 后端创建失败")
        except Exception as e:
            logger.debug(f"双读配置加载失败，跳过: {e}")
    
    try:
        # 读取查询
        if args.query:
            queries = [args.query]
        elif args.query_file:
            with open(args.query_file, "r", encoding="utf-8") as f:
                queries = [line.strip() for line in f if line.strip()]
        elif args.query_set:
            # 使用内置查询集
            queries = BUILTIN_QUERY_SETS[args.query_set]
            logger.info(f"使用内置查询集 '{args.query_set}'，包含 {len(queries)} 个查询")
        else:
            queries = []
        
        if not queries:
            logger.error("没有有效的查询")
            if args.json:
                print(json.dumps({"success": False, "error": "no valid queries"}))
            return 1
        
        # 处理双读比较参数
        enable_compare = getattr(args, 'dual_read_compare', False)
        compare_mode = getattr(args, 'compare_mode', 'summary')
        compare_thresholds = None
        
        # 如果 compare_mode 是 off，禁用比较
        if compare_mode == "off":
            enable_compare = False
        
        # 解析比较阈值 JSON
        if getattr(args, 'compare_thresholds', None):
            try:
                thresholds_dict = json.loads(args.compare_thresholds)
                compare_thresholds = CompareThresholds.from_dict(thresholds_dict)
                logger.info(f"使用自定义比较阈值: {thresholds_dict}")
            except json.JSONDecodeError as e:
                logger.warning(f"比较阈值 JSON 解析失败，使用默认值: {e}")
        
        # 如果启用比较但 shadow 后端未创建，尝试创建
        if enable_compare and shadow_backend is None and backend is not None:
            logger.info("双读比较已启用，尝试创建 shadow 后端...")
            try:
                provider = get_embedding_provider_instance()
                embedding_model_id = provider.model_id if provider else None
                primary_config = PGVectorConfig.from_env()
                
                # 创建或更新 dual_read_config
                if dual_read_config is None:
                    dual_read_config = DualReadConfig.from_env(primary_config.collection_strategy)
                
                # 强制启用双读以创建 shadow 后端
                dual_read_config.enabled = True
                
                shadow_backend = create_shadow_backend_for_read(
                    primary_config=primary_config,
                    dual_read_config=dual_read_config,
                    embedding_model_id=embedding_model_id,
                    embedding_provider=provider,
                    collection_id=resolved_collection,
                )
                
                if shadow_backend is not None:
                    set_shadow_backend(shadow_backend)
                    if not try_initialize_pgvector_backend(shadow_backend):
                        logger.warning("Shadow pgvector 初始化失败")
                    logger.info(f"双读比较 shadow 后端已创建: {shadow_backend.backend_name}")
                else:
                    logger.warning("双读比较已启用但 shadow 后端创建失败，比较功能将不可用")
                    enable_compare = False
            except Exception as e:
                logger.warning(f"创建双读比较 shadow 后端失败: {e}")
                enable_compare = False
        
        # 确定是否启用双读统计（--dual-read 开关）
        enable_dual_read = (
            hasattr(args, 'dual_read') and args.dual_read and 
            shadow_backend is not None and
            not enable_compare  # 如果启用了 --dual-read-compare，则不再重复启用 dual_read
        )
        
        # 处理 --dual-read-report 参数
        dual_read_report = getattr(args, 'dual_read_report', False)
        dual_read_report_mode = getattr(args, 'dual_read_report_mode', 'summary')
        
        # 如果 --dual-read-report 启用但 --compare-thresholds 未提供，从环境变量加载
        if enable_dual_read and dual_read_report and compare_thresholds is None:
            compare_thresholds = CompareThresholds.from_env()
            logger.info("双读报告已启用，从环境变量加载比较阈值")
        
        # 构建双读门禁阈值配置
        dual_read_gate_thresholds: Optional[DualReadGateThresholds] = None
        if enable_dual_read:
            # 解析门禁阈值参数
            min_overlap = getattr(args, 'dual_read_min_overlap', None)
            max_only_primary = getattr(args, 'dual_read_max_only_primary', None)
            max_only_shadow = getattr(args, 'dual_read_max_only_shadow', None)
            max_score_drift = getattr(args, 'dual_read_max_score_drift', None)
            
            # 如果配置了任何阈值，创建阈值配置
            if any([min_overlap, max_only_primary, max_only_shadow, max_score_drift]):
                dual_read_gate_thresholds = DualReadGateThresholds(
                    min_overlap=min_overlap,
                    max_only_primary=max_only_primary,
                    max_only_shadow=max_only_shadow,
                    max_score_drift=max_score_drift,
                    profile=GateProfile(
                        name="dual_read_gate",
                        version="1.0",
                        source=THRESHOLD_SOURCE_CLI,  # 来自 CLI 参数
                    ),
                )
                logger.info(
                    f"双读门禁已配置: min_overlap={min_overlap}, "
                    f"max_only_primary={max_only_primary}, "
                    f"max_only_shadow={max_only_shadow}, "
                    f"max_score_drift={max_score_drift}"
                )
        
        # 执行查询
        if len(queries) == 1:
            result = run_query(
                queries[0], 
                filters, 
                args.top_k,
                backend=backend,
                shadow_backend=shadow_backend,
                dual_read_config=dual_read_config,
                enable_compare=enable_compare,
                compare_mode=compare_mode,
                compare_thresholds=compare_thresholds,
                enable_dual_read=enable_dual_read,
                dual_read_gate_thresholds=dual_read_gate_thresholds,
                dual_read_report=dual_read_report,
                dual_read_report_mode=dual_read_report_mode,
            )
            
            # 输出结果
            include_compare = (enable_compare and compare_mode != "off") or (enable_dual_read and dual_read_report)
            include_dual_read = enable_dual_read and result.dual_read_stats is not None
            if args.json:
                if args.output_format == "packet":
                    output = result.to_evidence_packet(
                        include_compare=include_compare,
                        include_dual_read=include_dual_read,
                    )
                else:
                    output = result.to_dict(
                        include_compare=include_compare,
                        include_dual_read=include_dual_read,
                    )
                print(json.dumps(output, default=str, ensure_ascii=False, indent=2))
            else:
                print_result(result)
                # 额外输出比较报告（文本格式）
                if include_compare and result.compare_report:
                    _print_compare_report(result.compare_report)
                # 额外输出双读统计信息（文本格式）
                if include_dual_read:
                    _print_dual_read_stats(result.dual_read_stats)
            
            # 确定退出码
            # 1. 查询错误: 退出码=1
            # 2. 门禁失败: 退出码=1
            # 3. CompareReport 失败: 退出码=1
            if result.error is not None:
                return 1
            if (result.dual_read_stats is not None and 
                result.dual_read_stats.gate is not None and 
                not result.dual_read_stats.gate.passed):
                logger.error("[DualRead] 门禁检查失败，退出码=1")
                return 1
            if (result.compare_report is not None and
                result.compare_report.decision is not None and
                not result.compare_report.decision.passed):
                logger.error(f"[DualRead] 比较报告失败: {result.compare_report.decision.reason}，退出码=1")
                return 1
            return 0
        else:
            # 批量查询
            results = run_batch_query(
                queries, 
                filters, 
                args.top_k,
                backend=backend,
                shadow_backend=shadow_backend,
                dual_read_config=dual_read_config,
                enable_compare=enable_compare,
                compare_mode=compare_mode,
                compare_thresholds=compare_thresholds,
                enable_dual_read=enable_dual_read,
                dual_read_gate_thresholds=dual_read_gate_thresholds,
                dual_read_report=dual_read_report,
                dual_read_report_mode=dual_read_report_mode,
            )
            
            include_compare = (enable_compare and compare_mode != "off") or (enable_dual_read and dual_read_report)
            
            # 计算聚合门禁结果
            aggregate_gate = aggregate_gate_results(results)
            
            # 保存到 logbook（如果启用）
            attachment_id = None
            if (args.log_to_logbook or args.save_attachment) and args.item_id and conn is not None:
                # 获取查询集名称和 embedding 模型信息
                query_set_name = getattr(args, 'query_set', None)
                try:
                    provider = get_embedding_provider_instance()
                    embedding_model_id = provider.model_id if provider else None
                except Exception:
                    embedding_model_id = None
                
                attachment_id = add_to_attachments(
                    conn=conn,
                    item_id=args.item_id,
                    aggregate_gate=aggregate_gate,
                    results=results,
                    query_set_name=query_set_name,
                    collection_id=resolved_collection,
                    embedding_model_id=embedding_model_id,
                )
                conn.commit()
            
            # 构建阈值来源元数据
            thresholds_metadata: Dict[str, Any] = {}
            if compare_thresholds is not None:
                thresholds_metadata["compare_thresholds"] = compare_thresholds.to_dict()
            if dual_read_gate_thresholds is not None:
                # 使用 GateProfile.to_dict() 构建（保留兼容字段，新增 profile）
                thresholds_metadata["gate_thresholds"] = dual_read_gate_thresholds.to_dict()
            
            if args.json:
                if args.output_format == "packet":
                    output = [r.to_evidence_packet(
                        include_compare=include_compare,
                        include_dual_read=enable_dual_read,
                    ) for r in results]
                else:
                    output = {
                        "success": aggregate_gate.passed,
                        "total_queries": len(results),
                        "aggregate_gate": aggregate_gate.to_dict(),
                        "thresholds_metadata": thresholds_metadata if thresholds_metadata else None,
                        "results": [r.to_dict(
                            include_compare=include_compare,
                            include_dual_read=enable_dual_read,
                        ) for r in results],
                    }
                    if attachment_id is not None:
                        output["attachment_id"] = attachment_id
                print(json.dumps(output, default=str, ensure_ascii=False, indent=2))
            else:
                for result in results:
                    print_result(result)
                    if include_compare and result.compare_report:
                        _print_compare_report(result.compare_report)
                    if enable_dual_read and result.dual_read_stats is not None:
                        _print_dual_read_stats(result.dual_read_stats)
                
                # 输出保存到 logbook 的信息
                if attachment_id is not None:
                    print(f"已保存到 logbook.attachments, attachment_id={attachment_id}")
                
                # 输出聚合门禁结果摘要
                if aggregate_gate.total_queries > 1:
                    print("\n" + "-" * 60)
                    print("聚合门禁结果")
                    print("-" * 60)
                    print(f"  总查询数: {aggregate_gate.total_queries}")
                    print(f"  通过数: {aggregate_gate.pass_count}")
                    print(f"  警告数: {aggregate_gate.warn_count}")
                    print(f"  失败数: {aggregate_gate.fail_count}")
                    print(f"  错误数: {aggregate_gate.error_count}")
                    print(f"  最差建议: {aggregate_gate.worst_recommendation}")
                    print(f"  聚合结果: {'通过' if aggregate_gate.passed else '失败'}")
                    if aggregate_gate.failed_query_indices:
                        print(f"  失败查询索引: {aggregate_gate.failed_query_indices}")
                    if aggregate_gate.warned_query_indices:
                        print(f"  警告查询索引: {aggregate_gate.warned_query_indices}")
                    
                    # 显示触发项统计
                    if aggregate_gate.violation_summary is not None:
                        vs = aggregate_gate.violation_summary
                        print(f"\n  【触发项统计】")
                        if vs.by_level:
                            print(f"    按级别: {vs.by_level}")
                        if vs.by_check:
                            print(f"    按检查项: {vs.by_check}")
                        if vs.details:
                            print(f"    详情（前 {len(vs.details)} 条）:")
                            for d in vs.details[:5]:  # 文本输出只显示前 5 条
                                q_idx = d.get("query_index", "?")
                                check = d.get("check_name", "?")
                                level = d.get("level", "?")
                                actual = d.get("actual_value", 0)
                                threshold = d.get("threshold_value", 0)
                                print(f"      [{level.upper()}] query[{q_idx}] {check}: {actual:.4f} (阈值: {threshold:.4f})")
                    
                    print("-" * 60 + "\n")
            
            # 使用聚合门禁结果确定退出码
            if not aggregate_gate.passed:
                if aggregate_gate.error_count > 0:
                    logger.error(f"[DualRead] 存在 {aggregate_gate.error_count} 个查询错误，退出码=1")
                elif aggregate_gate.fail_count > 0:
                    logger.error(
                        f"[DualRead] 聚合门禁失败: {aggregate_gate.fail_count} 个查询失败, "
                        f"worst_recommendation={aggregate_gate.worst_recommendation}，退出码=1"
                    )
                return 1
            return 0
    
    except EngramError as e:
        if args.json:
            print(json.dumps({"success": False, "error": e.to_dict()}, default=str, ensure_ascii=False))
        else:
            logger.error(f"{e.error_type}: {e.message}")
        return e.exit_code
    
    except FileNotFoundError as e:
        logger.error(f"文件不存在: {e}")
        if args.json:
            print(json.dumps({"success": False, "error": f"file not found: {e}"}))
        return 1
    
    except Exception as e:
        logger.exception(f"未预期的错误: {e}")
        if args.json:
            print(json.dumps({
                "success": False,
                "error": {"type": "UNEXPECTED_ERROR", "message": str(e)},
            }, default=str, ensure_ascii=False))
        return 1
    
    finally:
        # 关闭数据库连接
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
