#!/usr/bin/env python3
"""
Workflow Contract Common Utilities

提供 workflow contract 处理的公共函数和常量，供以下脚本复用：
- check_workflow_contract_docs_sync.py
- workflow_contract_drift_report.py
- validate_workflows.py

主要功能：
1. METADATA_KEYS: 定义 contract 中的元数据字段（非 workflow 定义）
2. discover_workflow_keys(): 动态发现 contract 中的 workflow 定义 key
"""

from __future__ import annotations

from typing import Any

# ============================================================================
# Constants
# ============================================================================

# Metadata/legacy 字段排除列表 - 这些 key 不是 workflow 定义
#
# 包含以下类型：
# 1. JSON Schema 相关: $schema
# 2. 版本信息: version, description, last_updated
# 3. 全局配置: make (make targets 配置)
# 4. 冻结配置: frozen_step_text, frozen_job_names
# 5. 别名配置: step_name_aliases
#
# 注意：以下划线 (_) 开头的字段通过前缀检查排除：
# - _changelog_* (版本变更记录)
# - _*_note (注释字段)
# - _comment (注释字段)
METADATA_KEYS: frozenset[str] = frozenset(
    [
        "$schema",
        "version",
        "description",
        "last_updated",
        "make",
        "frozen_step_text",
        "frozen_job_names",
        "step_name_aliases",
    ]
)


# ============================================================================
# Helper Functions
# ============================================================================


def is_metadata_key(key: str) -> bool:
    """判断 key 是否为 metadata/非 workflow 字段

    Args:
        key: contract 的顶层 key

    Returns:
        如果是 metadata key 或下划线前缀字段返回 True
    """
    # 规则 1: 下划线前缀（changelog, notes, comments 等）
    if key.startswith("_"):
        return True

    # 规则 2: 已知 metadata 字段
    if key in METADATA_KEYS:
        return True

    return False


def discover_workflow_keys(contract: dict[str, Any]) -> list[str]:
    """动态发现 contract 中的 workflow 定义 key

    通过扫描顶层 dict，筛选符合 workflow 结构特征的 key：
    1. value 是 dict 类型
    2. value 包含 "file" 字段（workflow 定义的必需字段）
    3. key 不在 METADATA_KEYS 排除列表中
    4. key 不以下划线开头（排除 _changelog_*, _*_note 等注释字段）

    设计原则：
    - 使用 "file" 字段作为 workflow 定义的结构特征判断
    - 新增 metadata key 时只需更新 METADATA_KEYS，不影响 workflow 发现逻辑
    - 新增 workflow 时只需添加包含 "file" 字段的定义，自动被发现

    Args:
        contract: 加载的 contract JSON dict

    Returns:
        发现的 workflow key 列表，按字母序排序

    Example:
        >>> contract = {
        ...     "$schema": "...",
        ...     "version": "2.14.0",
        ...     "_changelog_v2.14.0": "...",
        ...     "ci": {"file": ".github/workflows/ci.yml", ...},
        ...     "nightly": {"file": ".github/workflows/nightly.yml", ...},
        ... }
        >>> discover_workflow_keys(contract)
        ['ci', 'nightly']
    """
    workflow_keys: list[str] = []

    for key, value in contract.items():
        # 排除 metadata 字段（含下划线前缀）
        if is_metadata_key(key):
            continue

        # 检查是否符合 workflow 结构特征：dict 且包含 "file" 字段
        if isinstance(value, dict) and "file" in value:
            workflow_keys.append(key)

    return sorted(workflow_keys)
