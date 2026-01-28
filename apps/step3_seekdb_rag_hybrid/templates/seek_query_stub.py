# 证据检索脚本（模板）
#
# 输入：
# - query_text
# - filters（project/module/source_id/owner）
# 输出：
# - chunks（含 artifact_uri + sha256 + excerpt + metadata）
#
# 用于给 Agent 组装 Evidence Packet。
#
# 使用 step3_chunking 模块的共用字段定义

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# 引入共用 chunking 模块
from step3_seekdb_rag_hybrid.step3_chunking import (
    CHUNKING_VERSION,
    CHUNK_ID_NAMESPACE,
    ChunkResult,
    parse_chunk_id,
    generate_excerpt,
)


# ============================================================
# 查询结果数据结构
# ============================================================

@dataclass
class EvidenceResult:
    """
    证据检索结果

    包含 chunk 内容和相关元数据，用于组装 Evidence Packet
    
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
    # 核心字段（与 ChunkResult 对齐）
    chunk_id: str
    chunk_idx: int
    content: str

    # 必须的可验证字段
    artifact_uri: str              # memory:// 协议优先
    sha256: str                    # 原始内容哈希
    source_id: str                 # 来源标识
    source_type: str               # svn/git/logbook

    # 摘要字段
    excerpt: str                   # 内容摘要（index 阶段预生成）

    # 检索相关
    relevance_score: float = 0.0   # 相似度分数

    # 扩展元数据
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}

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
        """
        从索引后端返回的结果构建 EvidenceResult

        Args:
            index_result: 索引后端返回的字典

        Returns:
            EvidenceResult 实例
        """
        # 提取核心字段
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


# ============================================================
# 查询过滤器
# ============================================================

@dataclass
class QueryFilters:
    """查询过滤条件"""
    project_key: Optional[str] = None      # 项目标识
    module: Optional[str] = None           # 模块/路径前缀
    source_type: Optional[str] = None      # svn/git/logbook
    source_id: Optional[str] = None        # 具体的 source_id
    owner_user_id: Optional[str] = None    # 所有者用户 ID
    time_range_start: Optional[str] = None # 时间范围起始（ISO 格式）
    time_range_end: Optional[str] = None   # 时间范围结束

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


# ============================================================
# 查询函数
# ============================================================

def query_evidence(
    query_text: str,
    filters: Optional[QueryFilters] = None,
    top_k: int = 10,
) -> List[EvidenceResult]:
    """
    执行证据检索

    Args:
        query_text: 查询文本
        filters: 过滤条件（可选）
        top_k: 返回结果数量

    Returns:
        EvidenceResult 列表，按相关度排序

    Notes:
        返回的每个 EvidenceResult 包含：
        - chunk_id: 稳定唯一标识
        - artifact_uri: 规范化的 memory:// URI（可用于验证/获取原文）
        - sha256: 原始内容哈希（可用于完整性验证）
        - source_id: 来源标识（可追溯到 Step1 数据）
        - excerpt: 内容摘要（用于快速预览，index 阶段预生成）
        - relevance_score: 相似度分数

    Example:
        >>> results = query_evidence(
        ...     query_text="修复登录页面的 XSS 漏洞",
        ...     filters=QueryFilters(project_key="webapp", source_type="git"),
        ...     top_k=5
        ... )
        >>> for r in results:
        ...     print(f"{r.source_id}: {r.excerpt[:50]}... (score={r.relevance_score:.2f})")
    """
    # 注意：这是模板，具体实现取决于选择的索引后端
    # - seekdb: 使用 seekdb client 的 search API
    # - pgvector: 使用 SQL + pgvector 扩展
    #
    # 示例伪代码：
    # query_vector = embedding_model.encode(query_text)
    # filter_dict = filters.to_filter_dict() if filters else {}
    # raw_results = index_client.search(
    #     vector=query_vector,
    #     filter=filter_dict,
    #     top_k=top_k,
    # )
    # return [EvidenceResult.from_index_result(r) for r in raw_results]
    raise NotImplementedError(
        "Fill in query logic against seekdb/pgvector backend.\n"
        f"当前 CHUNKING_VERSION: {CHUNKING_VERSION}"
    )


def build_evidence_packet(
    query_text: str,
    results: List[EvidenceResult],
) -> Dict[str, Any]:
    """
    将检索结果组装为 Evidence Packet

    Args:
        query_text: 原始查询文本
        results: 检索结果列表

    Returns:
        Evidence Packet 字典

    Example:
        >>> results = query_evidence("修复 XSS 漏洞")
        >>> packet = build_evidence_packet("修复 XSS 漏洞", results)
        >>> print(packet["evidences"][0]["artifact_uri"])
        'memory://engram/git/1.abc123/deadbeef...'
    """
    from datetime import datetime

    return {
        "query": query_text,
        "evidences": [r.to_evidence_dict() for r in results],
        "chunking_version": CHUNKING_VERSION,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }


# ============================================================
# 模块导出
# ============================================================

__all__ = [
    "EvidenceResult",
    "QueryFilters",
    "query_evidence",
    "build_evidence_packet",
    "CHUNKING_VERSION",
]
