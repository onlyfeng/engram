#!/usr/bin/env python3
"""
index_source_policy.py - 索引源记录过滤策略

定义索引阶段的记录过滤策略，控制哪些 patch_blobs/attachments 应该被索引。

策略维度:
- format: 允许的格式列表 (patch_blobs)
- kind: 允许的类型列表 (attachments)
- max_size_bytes: 最大文件大小限制
- skip_bulk: 是否跳过批量提交 (is_bulk=true)

优先级 (从高到低):
1. CLI 显式参数
2. 环境变量
3. 默认值

环境变量:
- STEP3_INDEX_POLICY_FORMATS: 允许的 format，逗号分隔 (默认 "diff,patch,log")
- STEP3_INDEX_POLICY_KINDS: 允许的 kind，逗号分隔 (默认 "patch,diff,log,spec,md,markdown,report,text")
- STEP3_INDEX_POLICY_MAX_SIZE: 最大文件大小(bytes)，0 表示不限制 (默认 10485760，即 10MB)
- STEP3_INDEX_POLICY_SKIP_BULK: 是否跳过批量提交 (默认 true)
- STEP3_INDEX_POLICY_VERSION: 策略版本号 (默认 "1.0")

使用示例:
    from step3_seekdb_rag_hybrid.index_source_policy import (
        IndexSourcePolicy,
        PolicyFilterResult,
        create_policy_from_env,
    )
    
    # 从环境变量创建策略
    policy = create_policy_from_env()
    
    # 过滤 patch_blob 记录
    result = policy.filter_patch_blob(row)
    if result.accepted:
        process_patch_blob(row)
    else:
        log_skipped(row, result.skip_reason)
    
    # 获取策略元信息用于 chunk metadata
    policy_meta = policy.get_metadata()
    # {'policy_version': '1.0', 'policy_hash': 'abc123...'}
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from step3_seekdb_rag_hybrid.env_compat import (
    get_str,
    get_int,
    get_bool,
    get_list,
)

__all__ = [
    "IndexSourcePolicy",
    "PolicyFilterResult",
    "create_policy_from_env",
    "SkipReason",
    # 默认值常量
    "DEFAULT_POLICY_VERSION",
    "DEFAULT_FORMATS",
    "DEFAULT_KINDS",
    "DEFAULT_MAX_SIZE_BYTES",
    "DEFAULT_SKIP_BULK",
]

# ============ 默认值常量 ============

DEFAULT_POLICY_VERSION = "1.0"

# patch_blobs 允许的 format 列表
DEFAULT_FORMATS = ["diff", "patch", "log"]

# attachments 允许的 kind 列表
DEFAULT_KINDS = ["patch", "diff", "log", "spec", "md", "markdown", "report", "text"]

# 最大文件大小限制 (10MB)
DEFAULT_MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10MB

# 是否跳过批量提交
DEFAULT_SKIP_BULK = True


# ============ 跳过原因枚举 ============

class SkipReason:
    """跳过原因常量"""
    NONE = ""  # 未跳过
    FORMAT_NOT_ALLOWED = "format_not_allowed"
    KIND_NOT_ALLOWED = "kind_not_allowed"
    SIZE_EXCEEDED = "size_exceeded"
    BULK_COMMIT = "bulk_commit"
    EMPTY_CONTENT = "empty_content"
    NO_URI = "no_uri"


# ============ 数据结构 ============

@dataclass
class PolicyFilterResult:
    """策略过滤结果"""
    accepted: bool
    skip_reason: str = ""
    skip_details: Optional[str] = None
    
    @staticmethod
    def accept() -> "PolicyFilterResult":
        """创建接受结果"""
        return PolicyFilterResult(accepted=True)
    
    @staticmethod
    def skip(reason: str, details: Optional[str] = None) -> "PolicyFilterResult":
        """创建跳过结果"""
        return PolicyFilterResult(accepted=False, skip_reason=reason, skip_details=details)


@dataclass
class IndexSourcePolicy:
    """
    索引源记录过滤策略
    
    控制哪些 patch_blobs/attachments 应该被索引。
    
    Attributes:
        version: 策略版本号
        formats: 允许的 format 列表 (用于 patch_blobs)
        kinds: 允许的 kind 列表 (用于 attachments)
        max_size_bytes: 最大文件大小限制，0 表示不限制
        skip_bulk: 是否跳过批量提交
    """
    version: str = DEFAULT_POLICY_VERSION
    formats: List[str] = field(default_factory=lambda: list(DEFAULT_FORMATS))
    kinds: List[str] = field(default_factory=lambda: list(DEFAULT_KINDS))
    max_size_bytes: int = DEFAULT_MAX_SIZE_BYTES
    skip_bulk: bool = DEFAULT_SKIP_BULK
    
    # 内部缓存
    _hash: Optional[str] = field(default=None, repr=False, compare=False)
    _formats_set: Optional[Set[str]] = field(default=None, repr=False, compare=False)
    _kinds_set: Optional[Set[str]] = field(default=None, repr=False, compare=False)
    
    def __post_init__(self):
        """初始化后处理"""
        # 清除缓存，确保后续懒加载
        object.__setattr__(self, '_hash', None)
        object.__setattr__(self, '_formats_set', None)
        object.__setattr__(self, '_kinds_set', None)
    
    @property
    def formats_set(self) -> Set[str]:
        """获取 formats 集合（懒加载）"""
        if self._formats_set is None:
            object.__setattr__(self, '_formats_set', set(f.lower() for f in self.formats))
        return self._formats_set
    
    @property
    def kinds_set(self) -> Set[str]:
        """获取 kinds 集合（懒加载）"""
        if self._kinds_set is None:
            object.__setattr__(self, '_kinds_set', set(k.lower() for k in self.kinds))
        return self._kinds_set
    
    def compute_hash(self) -> str:
        """
        计算策略配置的 SHA256 哈希值
        
        哈希计算基于策略的所有配置项，用于检测策略变更。
        """
        if self._hash is not None:
            return self._hash
        
        # 构建可哈希的配置字典
        config = {
            "version": self.version,
            "formats": sorted(self.formats),
            "kinds": sorted(self.kinds),
            "max_size_bytes": self.max_size_bytes,
            "skip_bulk": self.skip_bulk,
        }
        
        # 计算 SHA256
        config_str = json.dumps(config, sort_keys=True, ensure_ascii=False)
        hash_value = hashlib.sha256(config_str.encode("utf-8")).hexdigest()[:16]
        
        object.__setattr__(self, '_hash', hash_value)
        return hash_value
    
    def get_metadata(self) -> Dict[str, Any]:
        """
        获取策略元信息，用于写入 chunk metadata
        
        Returns:
            包含 policy_version 和 policy_hash 的字典
        """
        return {
            "policy_version": self.version,
            "policy_hash": self.compute_hash(),
        }
    
    def filter_patch_blob(self, row: Dict[str, Any]) -> PolicyFilterResult:
        """
        过滤 patch_blob 记录
        
        Args:
            row: patch_blob 记录字典，应包含 format, size_bytes, is_bulk, uri 等字段
        
        Returns:
            PolicyFilterResult: 过滤结果
        """
        # 检查 uri
        uri = row.get("uri")
        if not uri:
            return PolicyFilterResult.skip(SkipReason.NO_URI, "uri is empty or missing")
        
        # 检查 format
        format_val = row.get("format", "").lower()
        if format_val and format_val not in self.formats_set:
            return PolicyFilterResult.skip(
                SkipReason.FORMAT_NOT_ALLOWED,
                f"format '{format_val}' not in allowed list: {self.formats}"
            )
        
        # 检查 size_bytes
        if self.max_size_bytes > 0:
            size_bytes = row.get("size_bytes", 0) or 0
            if size_bytes > self.max_size_bytes:
                return PolicyFilterResult.skip(
                    SkipReason.SIZE_EXCEEDED,
                    f"size {size_bytes} bytes exceeds limit {self.max_size_bytes} bytes"
                )
        
        # 检查 is_bulk
        if self.skip_bulk:
            is_bulk = row.get("is_bulk", False)
            if is_bulk:
                return PolicyFilterResult.skip(
                    SkipReason.BULK_COMMIT,
                    "bulk commit skipped by policy"
                )
        
        return PolicyFilterResult.accept()
    
    def filter_attachment(self, row: Dict[str, Any]) -> PolicyFilterResult:
        """
        过滤 attachment 记录
        
        Args:
            row: attachment 记录字典，应包含 kind, size_bytes, uri 等字段
        
        Returns:
            PolicyFilterResult: 过滤结果
        """
        # 检查 uri
        uri = row.get("uri")
        if not uri:
            return PolicyFilterResult.skip(SkipReason.NO_URI, "uri is empty or missing")
        
        # 检查 kind
        kind_val = row.get("kind", "").lower()
        if kind_val and kind_val not in self.kinds_set:
            return PolicyFilterResult.skip(
                SkipReason.KIND_NOT_ALLOWED,
                f"kind '{kind_val}' not in allowed list: {self.kinds}"
            )
        
        # 检查 size_bytes
        if self.max_size_bytes > 0:
            size_bytes = row.get("size_bytes", 0) or 0
            if size_bytes > self.max_size_bytes:
                return PolicyFilterResult.skip(
                    SkipReason.SIZE_EXCEEDED,
                    f"size {size_bytes} bytes exceeds limit {self.max_size_bytes} bytes"
                )
        
        return PolicyFilterResult.accept()
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "version": self.version,
            "formats": self.formats,
            "kinds": self.kinds,
            "max_size_bytes": self.max_size_bytes,
            "skip_bulk": self.skip_bulk,
            "hash": self.compute_hash(),
        }
    
    def __str__(self) -> str:
        return (
            f"IndexSourcePolicy(version={self.version}, "
            f"formats={self.formats}, kinds={self.kinds}, "
            f"max_size={self.max_size_bytes}, skip_bulk={self.skip_bulk})"
        )


# ============ 工厂函数 ============

def create_policy_from_env(
    *,
    cli_version: Optional[str] = None,
    cli_formats: Optional[List[str]] = None,
    cli_kinds: Optional[List[str]] = None,
    cli_max_size: Optional[int] = None,
    cli_skip_bulk: Optional[bool] = None,
) -> IndexSourcePolicy:
    """
    从环境变量创建策略（支持 CLI 覆盖）
    
    Args:
        cli_version: CLI 传入的策略版本号
        cli_formats: CLI 传入的允许 format 列表
        cli_kinds: CLI 传入的允许 kind 列表
        cli_max_size: CLI 传入的最大文件大小
        cli_skip_bulk: CLI 传入的是否跳过批量提交
    
    Returns:
        IndexSourcePolicy 实例
    """
    # 读取策略版本
    version = get_str(
        "STEP3_INDEX_POLICY_VERSION",
        cli_value=cli_version,
        default=DEFAULT_POLICY_VERSION,
    )
    
    # 读取允许的 formats
    formats = get_list(
        "STEP3_INDEX_POLICY_FORMATS",
        cli_value=cli_formats,
        default=DEFAULT_FORMATS,
    )
    
    # 读取允许的 kinds
    kinds = get_list(
        "STEP3_INDEX_POLICY_KINDS",
        cli_value=cli_kinds,
        default=DEFAULT_KINDS,
    )
    
    # 读取最大文件大小
    max_size_bytes = get_int(
        "STEP3_INDEX_POLICY_MAX_SIZE",
        cli_value=cli_max_size,
        default=DEFAULT_MAX_SIZE_BYTES,
    )
    
    # 读取是否跳过批量提交
    skip_bulk = get_bool(
        "STEP3_INDEX_POLICY_SKIP_BULK",
        cli_value=cli_skip_bulk,
        default=DEFAULT_SKIP_BULK,
    )
    
    return IndexSourcePolicy(
        version=version,
        formats=formats,
        kinds=kinds,
        max_size_bytes=max_size_bytes,
        skip_bulk=skip_bulk,
    )


# ============ 便捷函数 ============

def get_default_policy() -> IndexSourcePolicy:
    """
    获取默认策略实例
    
    注意：此函数不读取环境变量，仅使用硬编码默认值。
    如需环境变量支持，请使用 create_policy_from_env()。
    """
    return IndexSourcePolicy()
