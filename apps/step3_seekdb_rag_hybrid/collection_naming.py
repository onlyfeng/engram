"""
collection_naming.py - Collection 命名统一模块

提供 collection_id 的规范化命名和跨后端映射。

Canonical Collection ID 格式:
    {project_key}:{chunking_version}:{embedding_model_id}[:{version_tag}]
    
    例如:
    - default:v1:bge-m3
    - proj1:v2:bge-m3:20260128T120000

这种冒号格式的优点:
    - 便于 logbook.kv 存储和审计可读
    - 各部分语义清晰，易于解析
    - 支持版本标签用于全量重建

各后端的映射:
    - SeekDB: 下划线分隔，特殊字符清理 -> default_v1_bge_m3
    - PGVector: 带前缀的表名 -> step3_chunks_default_v1_bge_m3
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple


# ============ 常量定义 ============


# Collection ID 分隔符
COLLECTION_ID_SEPARATOR = ":"

# PGVector 表名前缀
PGVECTOR_TABLE_PREFIX = "step3_chunks"

# PostgreSQL 标识符最大长度
POSTGRES_MAX_IDENTIFIER_LENGTH = 63

# SeekDB/PGVector 标识符中允许的字符（字母、数字、下划线）
SAFE_IDENTIFIER_PATTERN = re.compile(r'[^a-zA-Z0-9_]')


# ============ 数据结构 ============


@dataclass
class CollectionParts:
    """
    Collection ID 各组成部分
    
    Attributes:
        project_key: 项目标识（如 proj1, default）
        chunking_version: 分块版本（如 v1, v2）
        embedding_model_id: Embedding 模型标识（如 bge-m3, openai-ada-002）
        version_tag: 可选的版本标签（如 20260128T120000，用于全量重建）
    """
    project_key: str
    chunking_version: str
    embedding_model_id: str
    version_tag: Optional[str] = None
    
    def to_canonical_id(self) -> str:
        """
        转换为规范化的 collection_id
        
        Returns:
            冒号分隔的 collection_id 字符串
        
        Examples:
            >>> parts = CollectionParts("proj1", "v2", "bge-m3")
            >>> parts.to_canonical_id()
            'proj1:v2:bge-m3'
            >>> parts = CollectionParts("default", "v1", "bge-m3", "20260128T120000")
            >>> parts.to_canonical_id()
            'default:v1:bge-m3:20260128T120000'
        """
        parts = [self.project_key, self.chunking_version, self.embedding_model_id]
        if self.version_tag:
            parts.append(self.version_tag)
        return COLLECTION_ID_SEPARATOR.join(parts)
    
    def to_dict(self) -> Dict[str, Optional[str]]:
        """转换为字典"""
        return {
            "project_key": self.project_key,
            "chunking_version": self.chunking_version,
            "embedding_model_id": self.embedding_model_id,
            "version_tag": self.version_tag,
        }


# ============ 辅助函数 ============


def _sanitize_identifier(name: str) -> str:
    """
    清理标识符，只保留字母数字和下划线
    
    将连字符转换为下划线，移除其他特殊字符。
    
    Args:
        name: 原始名称
    
    Returns:
        清理后的安全标识符
    
    Examples:
        >>> _sanitize_identifier("bge-m3")
        'bge_m3'
        >>> _sanitize_identifier("openai:ada-002")
        'openai_ada_002'
    """
    # 先将连字符和冒号替换为下划线
    result = name.replace("-", "_").replace(":", "_")
    # 移除其他特殊字符
    result = SAFE_IDENTIFIER_PATTERN.sub('', result)
    # 确保不以数字开头（PostgreSQL 标识符要求）
    if result and result[0].isdigit():
        result = "_" + result
    return result


# ============ 核心函数 ============


def make_collection_id(
    project_key: Optional[str] = None,
    chunking_version: Optional[str] = None,
    embedding_model_id: Optional[str] = None,
    version_tag: Optional[str] = None,
) -> str:
    """
    生成规范化的 collection_id（Canonical Format）
    
    格式: {project_key}:{chunking_version}:{embedding_model_id}[:{version_tag}]
    
    Args:
        project_key: 项目标识，None 时使用 "default"
        chunking_version: 分块版本，None 时使用 "v1"
        embedding_model_id: Embedding 模型 ID，None 时使用 "nomodel"
        version_tag: 可选的版本标签（如时间戳，用于全量重建版本控制）
    
    Returns:
        规范化的 collection_id，各部分用冒号分隔
    
    Examples:
        >>> make_collection_id("proj1", "v2", "bge-m3")
        'proj1:v2:bge-m3'
        >>> make_collection_id(None, "v2", "bge-m3", "20260128T120000")
        'default:v2:bge-m3:20260128T120000'
    """
    parts = CollectionParts(
        project_key=project_key or "default",
        chunking_version=chunking_version or "v1",
        embedding_model_id=embedding_model_id or "nomodel",
        version_tag=version_tag,
    )
    return parts.to_canonical_id()


def parse_collection_id(collection_id: str) -> CollectionParts:
    """
    解析 collection_id
    
    Args:
        collection_id: 规范化的 collection_id 字符串
    
    Returns:
        CollectionParts 实例
    
    Raises:
        ValueError: 格式无效时抛出
    
    Examples:
        >>> parts = parse_collection_id("proj1:v2:bge-m3")
        >>> parts.project_key
        'proj1'
        >>> parts.embedding_model_id
        'bge-m3'
    """
    parts = collection_id.split(COLLECTION_ID_SEPARATOR)
    if len(parts) < 3:
        raise ValueError(
            f"无效的 collection_id 格式: {collection_id}, "
            f"期望格式: project_key:chunking_version:embedding_model_id[:version_tag]"
        )
    
    return CollectionParts(
        project_key=parts[0],
        chunking_version=parts[1],
        embedding_model_id=parts[2],
        version_tag=parts[3] if len(parts) > 3 else None,
    )


def make_version_tag() -> str:
    """
    生成版本标签（时间戳格式）
    
    用于全量重建时创建新版本的 collection。
    
    Returns:
        格式为 YYYYMMDDTHHmmss 的时间戳
    
    Examples:
        >>> tag = make_version_tag()  # 类似 '20260128T143025'
    """
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


# ============ 后端映射函数 ============


def to_seekdb_collection_name(collection_id: str) -> str:
    """
    将 canonical collection_id 转换为 SeekDB 的 collection 名称
    
    SeekDB 使用下划线分隔，且要求名称只含字母数字和下划线。
    
    映射规则:
        - 冒号替换为下划线
        - 连字符替换为下划线
        - 移除其他特殊字符
    
    Args:
        collection_id: 规范化的 collection_id (冒号格式)
    
    Returns:
        SeekDB collection 名称 (下划线格式)
    
    Examples:
        >>> to_seekdb_collection_name("proj1:v2:bge-m3")
        'proj1_v2_bge_m3'
        >>> to_seekdb_collection_name("default:v1:bge-m3:20260128T120000")
        'default_v1_bge_m3_20260128T120000'
    """
    return _sanitize_identifier(collection_id)


def from_seekdb_collection_name(seekdb_name: str) -> str:
    """
    从 SeekDB collection 名称反向解析为 canonical collection_id
    
    注意：由于下划线格式丢失了原始分隔信息，此函数采用启发式解析：
    假设格式为 {project}_{version}_{model}[_{tag}]
    
    Args:
        seekdb_name: SeekDB collection 名称
    
    Returns:
        尽力还原的 collection_id（可能与原始不完全一致）
    
    Raises:
        ValueError: 格式无法解析时抛出
    """
    parts = seekdb_name.split("_")
    if len(parts) < 3:
        raise ValueError(f"无法解析 SeekDB collection 名称: {seekdb_name}")
    
    # 启发式解析：
    # - 最后一部分是 embedding_model_id（或 version_tag）
    # - 倒数第二部分是 chunking_version（vX 格式）或 embedding_model_id
    # - 其余是 project_key
    
    # 查找 vX 格式的版本号位置
    version_idx = -1
    for i, part in enumerate(parts):
        if re.match(r'^v\d+$', part):
            version_idx = i
            break
    
    if version_idx == -1:
        # 没找到明确的版本号，使用默认拆分
        version_idx = 1
    
    project_key = "_".join(parts[:version_idx]) or "default"
    chunking_version = parts[version_idx] if version_idx < len(parts) else "v1"
    
    # 剩余部分确定 embedding_model_id 和 version_tag
    remaining = parts[version_idx + 1:]
    if not remaining:
        embedding_model_id = "nomodel"
        version_tag = None
    elif len(remaining) == 1:
        embedding_model_id = remaining[0]
        version_tag = None
    else:
        # 检查最后一部分是否是时间戳格式的 version_tag
        last_part = remaining[-1]
        if re.match(r'^\d{8}T\d{6}$', last_part):
            embedding_model_id = "_".join(remaining[:-1])
            version_tag = last_part
        else:
            embedding_model_id = "_".join(remaining)
            version_tag = None
    
    result_parts = [project_key, chunking_version, embedding_model_id]
    if version_tag:
        result_parts.append(version_tag)
    
    return COLLECTION_ID_SEPARATOR.join(result_parts)


def to_pgvector_table_name(collection_id: str) -> str:
    """
    将 canonical collection_id 转换为 PGVector 表名
    
    PostgreSQL 表名规则:
        - 以字母或下划线开头
        - 只含字母、数字、下划线
        - 建议小写
        - 添加统一前缀以便识别
        - 长度不超过 63 字符（PostgreSQL 限制）
    
    映射规则:
        - 添加 step3_chunks_ 前缀
        - 冒号和连字符替换为下划线
        - 转为小写
        - 超长时截断并添加 hash 后缀以保证唯一性
    
    Args:
        collection_id: 规范化的 collection_id (冒号格式)
    
    Returns:
        PGVector 表名（保证 ≤63 字符）
    
    Examples:
        >>> to_pgvector_table_name("proj1:v2:bge-m3")
        'step3_chunks_proj1_v2_bge_m3'
        >>> to_pgvector_table_name("default:v1:BGE-M3")
        'step3_chunks_default_v1_bge_m3'
    """
    sanitized = _sanitize_identifier(collection_id).lower()
    full_name = f"{PGVECTOR_TABLE_PREFIX}_{sanitized}"
    
    # 检查长度限制
    if len(full_name) <= POSTGRES_MAX_IDENTIFIER_LENGTH:
        return full_name
    
    # 超长时截断并添加 hash 后缀以保证唯一性
    # 格式: {prefix}_{truncated}_{hash8}
    # hash 使用原始 collection_id 保证唯一性
    import hashlib
    hash_suffix = hashlib.sha256(collection_id.encode()).hexdigest()[:8]
    
    # 计算可用的截断长度: 63 - len(prefix) - 1(下划线) - 8(hash) - 1(下划线)
    prefix_with_underscore = f"{PGVECTOR_TABLE_PREFIX}_"
    max_body_len = POSTGRES_MAX_IDENTIFIER_LENGTH - len(prefix_with_underscore) - 9  # _hash8
    
    truncated = sanitized[:max_body_len]
    # 确保截断后不以下划线结尾（美观考虑）
    truncated = truncated.rstrip('_')
    
    return f"{prefix_with_underscore}{truncated}_{hash_suffix}"


def from_pgvector_table_name(table_name: str) -> str:
    """
    从 PGVector 表名反向解析为 canonical collection_id
    
    Args:
        table_name: PGVector 表名
    
    Returns:
        尽力还原的 collection_id
    
    Raises:
        ValueError: 格式无法解析时抛出
    """
    # 移除前缀
    prefix = f"{PGVECTOR_TABLE_PREFIX}_"
    if not table_name.startswith(prefix):
        raise ValueError(
            f"无效的 PGVector 表名: {table_name}, "
            f"期望以 '{prefix}' 开头"
        )
    
    name_part = table_name[len(prefix):]
    return from_seekdb_collection_name(name_part)


# ============ 兼容性别名（已弃用，将在未来版本移除） ============


# [DEPRECATED] 这些别名函数已弃用，请直接使用 make_collection_id / parse_collection_id
# 弃用原因：统一使用 collection_id 概念，避免 name/id 混淆
# 迁移指南：
#   - make_collection_name(...) -> make_collection_id(...)
#   - parse_collection_name(x) -> parse_collection_id(x).to_dict()
# 计划移除版本：v2.0.0

import warnings


def make_collection_name(
    project_key: Optional[str] = None,
    chunking_version: Optional[str] = None,
    embedding_model_id: Optional[str] = None,
    version_tag: Optional[str] = None,
) -> str:
    """
    [DEPRECATED] make_collection_id 的别名（兼容 seek_indexer 原有接口）
    
    .. deprecated::
        请使用 :func:`make_collection_id` 代替，此函数将在 v2.0.0 移除。
    """
    warnings.warn(
        "make_collection_name 已弃用，请使用 make_collection_id 代替",
        DeprecationWarning,
        stacklevel=2,
    )
    return make_collection_id(
        project_key=project_key,
        chunking_version=chunking_version,
        embedding_model_id=embedding_model_id,
        version_tag=version_tag,
    )


def parse_collection_name(collection_name: str) -> Dict[str, Optional[str]]:
    """
    [DEPRECATED] parse_collection_id 的别名（兼容 seek_indexer 原有接口）
    
    .. deprecated::
        请使用 :func:`parse_collection_id` 代替，此函数将在 v2.0.0 移除。
    
    返回字典格式以保持向后兼容。
    """
    warnings.warn(
        "parse_collection_name 已弃用，请使用 parse_collection_id 代替",
        DeprecationWarning,
        stacklevel=2,
    )
    parts = parse_collection_id(collection_name)
    return parts.to_dict()


# ============ 验证函数 ============


def is_valid_collection_id(collection_id: str) -> bool:
    """
    检查 collection_id 是否有效
    
    Args:
        collection_id: 待验证的 collection_id
    
    Returns:
        是否有效
    """
    try:
        parse_collection_id(collection_id)
        return True
    except ValueError:
        return False


def get_collection_mapping(collection_id: str) -> Dict[str, str]:
    """
    获取 collection_id 到各后端名称的完整映射
    
    用于审计和调试目的。
    
    Args:
        collection_id: 规范化的 collection_id
    
    Returns:
        包含各后端映射的字典
    
    Examples:
        >>> get_collection_mapping("proj1:v2:bge-m3")
        {
            'canonical': 'proj1:v2:bge-m3',
            'seekdb': 'proj1_v2_bge_m3',
            'pgvector': 'step3_chunks_proj1_v2_bge_m3',
        }
    """
    return {
        "canonical": collection_id,
        "seekdb": to_seekdb_collection_name(collection_id),
        "pgvector": to_pgvector_table_name(collection_id),
    }
