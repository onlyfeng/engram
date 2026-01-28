"""
base.py - 索引后端抽象基类与 DSL 校验

定义索引后端的统一接口协议，所有后端实现必须遵循此协议。
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Set

try:
    from step3_seekdb_rag_hybrid.index_backend.types import (
        ChunkDoc,
        QueryRequest,
        QueryHit,
        FilterDSL,
        FILTER_FIELDS,
        FILTER_OPERATORS,
    )
except ImportError:
    from .types import (
        ChunkDoc,
        QueryRequest,
        QueryHit,
        FilterDSL,
        FILTER_FIELDS,
        FILTER_OPERATORS,
    )


# ============ Filter DSL 校验 ============


class FilterValidationError(Exception):
    """过滤条件校验错误"""
    
    def __init__(self, message: str, field: Optional[str] = None, operator: Optional[str] = None):
        self.message = message
        self.field = field
        self.operator = operator
        super().__init__(message)
    
    def to_dict(self) -> Dict[str, Any]:
        result = {"error": "FilterValidationError", "message": self.message}
        if self.field:
            result["field"] = self.field
        if self.operator:
            result["operator"] = self.operator
        return result


def validate_filter_dsl(
    filters: FilterDSL,
    allowed_fields: Optional[Set[str]] = None,
    strict: bool = True,
) -> List[str]:
    """
    校验 Filter DSL 格式
    
    Args:
        filters: 过滤条件字典
        allowed_fields: 允许的字段集合（None 表示使用默认字段）
        strict: 严格模式，遇到未知字段时抛出异常
    
    Returns:
        警告信息列表
    
    Raises:
        FilterValidationError: 校验失败时抛出
    
    DSL 格式规范:
        - 直接值: {"field": "value"}  等同于 {"field": {"$eq": "value"}}
        - 操作符: {"field": {"$op": "value"}}
        - 范围: {"commit_ts": {"$gte": "2024-01-01", "$lte": "2024-12-31"}}
        
    支持的操作符:
        - $eq: 精确匹配（默认）
        - $prefix: 前缀匹配（仅 module 字段支持）
        - $gte/$lte/$gt/$lt: 范围比较（仅 commit_ts 字段支持）
        - $in: 列表包含
    """
    warnings: List[str] = []
    valid_fields = allowed_fields or set(FILTER_FIELDS.keys())
    valid_operators = set(FILTER_OPERATORS.keys())
    
    for field_name, field_value in filters.items():
        # 1. 检查字段名是否有效
        if field_name not in valid_fields:
            if strict:
                raise FilterValidationError(
                    f"未知的过滤字段: {field_name}，允许的字段: {sorted(valid_fields)}",
                    field=field_name,
                )
            else:
                warnings.append(f"未知字段: {field_name}")
                continue
        
        # 2. 检查字段值格式
        if isinstance(field_value, dict):
            # 操作符格式
            for op, op_value in field_value.items():
                if op not in valid_operators:
                    raise FilterValidationError(
                        f"未知的操作符: {op}，允许的操作符: {sorted(valid_operators)}",
                        field=field_name,
                        operator=op,
                    )
                
                # 3. 检查操作符与字段的兼容性
                _validate_operator_field_compatibility(field_name, op, op_value)
        
        elif isinstance(field_value, (str, int, float)):
            # 直接值（隐式 $eq）
            pass
        
        elif isinstance(field_value, list):
            # 列表值（隐式 $in）
            if not field_value:
                warnings.append(f"字段 {field_name} 的列表值为空")
        
        else:
            raise FilterValidationError(
                f"字段 {field_name} 的值类型无效: {type(field_value).__name__}",
                field=field_name,
            )
    
    return warnings


def _validate_operator_field_compatibility(field: str, operator: str, value: Any) -> None:
    """校验操作符与字段的兼容性"""
    
    # $prefix 仅支持 module 字段
    if operator == "$prefix":
        if field != "module":
            raise FilterValidationError(
                f"操作符 $prefix 仅支持 module 字段，当前字段: {field}",
                field=field,
                operator=operator,
            )
        if not isinstance(value, str):
            raise FilterValidationError(
                f"$prefix 操作符的值必须是字符串，当前类型: {type(value).__name__}",
                field=field,
                operator=operator,
            )
    
    # 范围操作符仅支持 commit_ts 字段
    if operator in ("$gte", "$lte", "$gt", "$lt"):
        if field != "commit_ts":
            raise FilterValidationError(
                f"范围操作符 {operator} 仅支持 commit_ts 字段，当前字段: {field}",
                field=field,
                operator=operator,
            )
        if not isinstance(value, str):
            raise FilterValidationError(
                f"范围操作符的值必须是 ISO 时间字符串，当前类型: {type(value).__name__}",
                field=field,
                operator=operator,
            )
    
    # $in 的值必须是列表
    if operator == "$in":
        if not isinstance(value, list):
            raise FilterValidationError(
                f"$in 操作符的值必须是列表，当前类型: {type(value).__name__}",
                field=field,
                operator=operator,
            )


def normalize_filter_dsl(filters: FilterDSL) -> FilterDSL:
    """
    规范化 Filter DSL
    
    将简写格式转换为完整的操作符格式，便于后端实现统一处理。
    
    Args:
        filters: 原始过滤条件
    
    Returns:
        规范化后的过滤条件
    
    Example:
        输入: {"project_key": "webapp", "source_type": ["git", "svn"]}
        输出: {"project_key": {"$eq": "webapp"}, "source_type": {"$in": ["git", "svn"]}}
    """
    normalized: FilterDSL = {}
    
    for field, value in filters.items():
        if isinstance(value, dict):
            # 已经是操作符格式
            normalized[field] = value
        elif isinstance(value, list):
            # 列表转换为 $in
            normalized[field] = {"$in": value}
        else:
            # 标量值转换为 $eq
            normalized[field] = {"$eq": value}
    
    return normalized


# ============ 索引后端抽象基类 ============


class IndexBackend(ABC):
    """
    索引后端抽象基类
    
    定义所有索引后端必须实现的接口，包括：
    - 文档索引（upsert/delete）
    - 向量检索（query）
    - 状态查询（health/stats）
    """
    
    @property
    @abstractmethod
    def backend_name(self) -> str:
        """后端名称标识"""
        pass
    
    @property
    @abstractmethod
    def supports_vector_search(self) -> bool:
        """是否支持向量检索"""
        pass
    
    # ============ 文档操作 ============
    
    @abstractmethod
    def upsert(self, docs: List[ChunkDoc]) -> int:
        """
        批量插入或更新文档
        
        Args:
            docs: 文档列表
        
        Returns:
            成功处理的文档数量
        
        Raises:
            IndexBackendError: 操作失败时抛出
        """
        pass
    
    @abstractmethod
    def delete(self, chunk_ids: List[str]) -> int:
        """
        批量删除文档
        
        Args:
            chunk_ids: 要删除的 chunk_id 列表
        
        Returns:
            成功删除的文档数量
        """
        pass
    
    @abstractmethod
    def delete_by_filter(self, filters: FilterDSL) -> int:
        """
        根据过滤条件删除文档
        
        Args:
            filters: 过滤条件（DSL 格式）
        
        Returns:
            删除的文档数量
        """
        pass
    
    # ============ 检索操作 ============
    
    @abstractmethod
    def query(self, request: QueryRequest) -> List[QueryHit]:
        """
        执行向量检索
        
        Args:
            request: 查询请求
        
        Returns:
            命中结果列表，按相似度降序排列
        
        Raises:
            IndexBackendError: 检索失败时抛出
        """
        pass
    
    def query_simple(
        self,
        query_text: str,
        filters: Optional[FilterDSL] = None,
        top_k: int = 10,
    ) -> List[QueryHit]:
        """
        简化的检索接口
        
        Args:
            query_text: 查询文本
            filters: 过滤条件
            top_k: 返回数量
        
        Returns:
            命中结果列表
        """
        request = QueryRequest(
            query_text=query_text,
            filters=filters or {},
            top_k=top_k,
        )
        return self.query(request)
    
    # ============ 一致性检查支持 ============
    
    def get_by_ids(self, chunk_ids: List[str]) -> List[ChunkDoc]:
        """
        批量获取文档
        
        Args:
            chunk_ids: chunk_id 列表
        
        Returns:
            存在的文档列表
        
        Note:
            默认实现返回空列表，需要后端具体实现。
            此方法用于一致性检查，不要求所有后端实现。
        """
        return []
    
    def exists(self, chunk_ids: List[str]) -> Dict[str, bool]:
        """
        批量检查文档是否存在
        
        Args:
            chunk_ids: chunk_id 列表
        
        Returns:
            {chunk_id: exists} 的字典
        
        Note:
            默认实现基于 get_by_ids，子类可覆写以提供更高效实现。
        """
        found_docs = self.get_by_ids(chunk_ids)
        found_ids = {doc.chunk_id for doc in found_docs}
        return {cid: cid in found_ids for cid in chunk_ids}
    
    def get_chunk_metadata(self, chunk_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        批量获取文档元数据（用于一致性校验）
        
        Args:
            chunk_ids: chunk_id 列表
        
        Returns:
            {chunk_id: metadata_dict} 的字典
            metadata_dict 包含: sha256, source_id, artifact_uri, project_key 等
        
        Note:
            默认实现基于 get_by_ids，子类可覆写以提供更高效实现。
        """
        found_docs = self.get_by_ids(chunk_ids)
        return {
            doc.chunk_id: {
                "sha256": doc.sha256,
                "source_id": doc.source_id,
                "source_type": doc.source_type,
                "artifact_uri": doc.artifact_uri,
                "project_key": doc.project_key,
                "module": doc.module,
                "chunk_idx": doc.chunk_idx,
            }
            for doc in found_docs
        }
    
    def count_by_source(self, source_type: str, source_id: str) -> int:
        """
        统计指定来源的文档数量
        
        Args:
            source_type: 来源类型
            source_id: 来源标识
        
        Returns:
            文档数量，-1 表示不支持此查询
        
        Note:
            默认返回 -1 表示不支持。子类可覆写提供具体实现。
        """
        return -1
    
    # ============ 状态查询 ============
    
    @abstractmethod
    def health_check(self) -> Dict[str, Any]:
        """
        健康检查
        
        Returns:
            健康状态信息，包含:
            - status: "healthy" | "degraded" | "unhealthy"
            - backend: 后端名称
            - details: 详细信息
        """
        pass
    
    @abstractmethod
    def get_stats(self) -> Dict[str, Any]:
        """
        获取统计信息
        
        Returns:
            统计信息，包含:
            - total_docs: 文档总数
            - index_size_bytes: 索引大小
            - ... 其他后端特定统计
        """
        pass
    
    # ============ 生命周期 ============
    
    def initialize(self) -> None:
        """初始化后端（可选实现）"""
        pass
    
    def close(self) -> None:
        """关闭后端连接（可选实现）"""
        pass
    
    def __enter__(self) -> "IndexBackend":
        self.initialize()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


# ============ 工具函数 ============


def build_filter_dsl(
    project_key: Optional[str] = None,
    module: Optional[str] = None,
    source_type: Optional[str] = None,
    source_id: Optional[str] = None,
    owner_user_id: Optional[str] = None,
    commit_ts_gte: Optional[str] = None,
    commit_ts_lte: Optional[str] = None,
    collection_id: Optional[str] = None,
) -> FilterDSL:
    """
    构建 Filter DSL 的便捷函数
    
    将常用的过滤参数转换为标准 DSL 格式。
    
    Args:
        project_key: 项目标识（精确匹配）
        module: 模块/路径（前缀匹配）
        source_type: 来源类型（精确匹配）
        source_id: 来源ID（精确匹配）
        owner_user_id: 所有者（精确匹配）
        commit_ts_gte: 时间范围起始（>=）
        commit_ts_lte: 时间范围结束（<=）
        collection_id: Collection 标识（用于诊断/管理，通常由后端自动注入）
    
    Returns:
        Filter DSL 字典
    """
    filters: FilterDSL = {}
    
    if project_key:
        filters["project_key"] = project_key
    
    if module:
        filters["module"] = {"$prefix": module}
    
    if source_type:
        filters["source_type"] = source_type
    
    if source_id:
        filters["source_id"] = source_id
    
    if owner_user_id:
        filters["owner_user_id"] = owner_user_id
    
    if commit_ts_gte or commit_ts_lte:
        filters["commit_ts"] = {}
        if commit_ts_gte:
            filters["commit_ts"]["$gte"] = commit_ts_gte
        if commit_ts_lte:
            filters["commit_ts"]["$lte"] = commit_ts_lte
    
    if collection_id:
        filters["collection_id"] = collection_id
    
    return filters
