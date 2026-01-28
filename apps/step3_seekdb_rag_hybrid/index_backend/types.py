"""
types.py - 索引后端数据结构定义

定义统一的数据结构，供所有索引后端实现使用。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union


# ============ Filter DSL 类型定义 ============
# 定义统一的过滤条件 DSL 格式，所有后端实现必须支持

# 支持的操作符
FILTER_OPERATORS = {
    "$eq": "等于（默认）",
    "$prefix": "前缀匹配",
    "$gte": "大于等于",
    "$lte": "小于等于",
    "$gt": "大于",
    "$lt": "小于",
    "$in": "包含于列表",
}

# 支持的过滤字段
FILTER_FIELDS = {
    "project_key": "项目标识",
    "module": "模块/路径（支持 $prefix）",
    "source_type": "来源类型（svn/git/logbook）",
    "source_id": "来源标识",
    "owner_user_id": "所有者用户ID",
    "commit_ts": "提交时间戳（支持 $gte/$lte/$gt/$lt 范围查询）",
    "collection_id": "Collection 标识（用于诊断/管理，通常由后端自动注入）",
}


@dataclass
class FilterOperator:
    """过滤操作符值"""
    operator: str  # $eq, $prefix, $gte, $lte, $gt, $lt, $in
    value: Any

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {self.operator: self.value}


# Filter DSL 类型定义
# 字段值可以是：直接值（等同于 $eq）、操作符字典、或操作符列表（用于范围查询）
FilterValue = Union[str, int, float, Dict[str, Any], List[Any]]
FilterDSL = Dict[str, FilterValue]


# ============ 核心数据结构 ============


@dataclass
class ChunkDoc:
    """
    分块文档结构 - 用于索引存储
    
    表示一个可索引的分块文档，包含内容、向量和元数据。
    """
    # 必需字段
    chunk_id: str  # 唯一标识，格式: {project}:{source_type}:{source_id}:{sha256}:{version}:{chunk_idx}
    content: str   # 分块文本内容
    
    # 向量（可选，由 embedding 服务生成）
    vector: Optional[List[float]] = None
    
    # 核心元数据
    project_key: str = ""
    module: str = ""           # 模块/路径，用于前缀过滤
    source_type: str = ""      # svn/git/logbook
    source_id: str = ""        # 来源唯一标识
    owner_user_id: str = ""    # 所有者用户ID
    commit_ts: Optional[str] = None  # 提交时间戳 (ISO 格式)
    
    # 辅助字段
    artifact_uri: str = ""     # 关联的 artifact URI
    sha256: str = ""           # 内容哈希
    chunk_idx: int = 0         # 分块索引
    excerpt: str = ""          # 摘要/摘录
    
    # Collection 标识（可选）
    # 通常由 backend 自动注入，但允许 doc 级显式指定（需与 backend.collection_id 一致或用于无 backend collection_id 的管理场景）
    collection_id: Optional[str] = None
    
    # 扩展元数据
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_index_doc(self) -> Dict[str, Any]:
        """转换为索引后端文档格式"""
        doc = {
            "chunk_id": self.chunk_id,
            "content": self.content,
            "project_key": self.project_key,
            "module": self.module,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "owner_user_id": self.owner_user_id,
            "artifact_uri": self.artifact_uri,
            "sha256": self.sha256,
            "chunk_idx": self.chunk_idx,
            "excerpt": self.excerpt,
        }
        if self.commit_ts:
            doc["commit_ts"] = self.commit_ts
        if self.vector:
            doc["vector"] = self.vector
        if self.metadata:
            doc.update(self.metadata)
        return doc


@dataclass
class QueryRequest:
    """
    查询请求结构
    
    封装一次向量检索请求的所有参数。
    """
    # 查询内容（至少提供一个）
    query_text: Optional[str] = None   # 原始查询文本（将由后端生成向量）
    query_vector: Optional[List[float]] = None  # 预计算的查询向量
    
    # 过滤条件（DSL 格式）
    filters: FilterDSL = field(default_factory=dict)
    
    # 检索参数
    top_k: int = 10            # 返回结果数量
    min_score: float = 0.0     # 最小相似度阈值
    
    # 请求元数据
    request_id: Optional[str] = None  # 请求ID（用于追踪）
    
    def validate(self) -> bool:
        """验证请求是否有效"""
        if not self.query_text and not self.query_vector:
            return False
        if self.top_k <= 0:
            return False
        return True


@dataclass
class QueryHit:
    """
    查询命中结果
    
    表示单个检索结果，包含分块信息和相似度分数。
    """
    # 核心信息
    chunk_id: str
    content: str
    score: float  # 相似度分数 (0-1)
    
    # 来源信息
    source_type: str = ""
    source_id: str = ""
    artifact_uri: str = ""
    
    # 分块信息
    chunk_idx: int = 0
    sha256: str = ""
    excerpt: str = ""
    
    # 所有元数据
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        result = {
            "chunk_id": self.chunk_id,
            "content": self.content,
            "score": self.score,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "artifact_uri": self.artifact_uri,
            "chunk_idx": self.chunk_idx,
            "sha256": self.sha256,
            "excerpt": self.excerpt,
        }
        if self.metadata:
            result["metadata"] = self.metadata
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QueryHit":
        """从字典构建 QueryHit"""
        # 提取已知字段
        known_fields = {
            "chunk_id", "content", "score", "source_type", "source_id",
            "artifact_uri", "chunk_idx", "sha256", "excerpt", "metadata"
        }
        
        # 分离元数据
        metadata = data.get("metadata", {})
        extra_fields = {k: v for k, v in data.items() if k not in known_fields}
        if extra_fields:
            metadata = {**metadata, **extra_fields}
        
        return cls(
            chunk_id=data.get("chunk_id", ""),
            content=data.get("content", ""),
            score=data.get("score", 0.0),
            source_type=data.get("source_type", ""),
            source_id=data.get("source_id", ""),
            artifact_uri=data.get("artifact_uri", ""),
            chunk_idx=data.get("chunk_idx", 0),
            sha256=data.get("sha256", ""),
            excerpt=data.get("excerpt", ""),
            metadata=metadata,
        )
