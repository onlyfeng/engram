#!/usr/bin/env python3
"""
Workflow Contract 版本策略检查脚本

当关键文件变更时，强制要求：
1. workflow_contract.v1.json 的 version 字段已更新
2. workflow_contract.v1.json 的 last_updated 字段已更新
3. contract.md 版本控制表（第 14 章）包含该版本

关键文件规则（统一定义，详见 CRITICAL_*_RULES）：

  [workflow_core] Workflow 文件（Phase 2 已纳入 release）：
    - .github/workflows/ci.yml
    - .github/workflows/nightly.yml
    - .github/workflows/release.yml

  [contract_definition] 合约定义文件：
    - scripts/ci/workflow_contract.v*.json
    - docs/ci_nightly_workflow_refactor/*.md

  [tooling] 工具脚本（变更影响合约执行逻辑）：
    - scripts/ci/validate_workflows.py
    - scripts/ci/workflow_contract.v*.schema.json
    - scripts/ci/check_workflow_contract_docs_sync.py
    - scripts/ci/check_workflow_contract_error_types_docs_sync.py
    - scripts/ci/workflow_contract_drift_report.py
    - scripts/ci/generate_workflow_contract_snapshot.py

  [special] Makefile（仅当变更涉及 workflow/CI 相关目标时触发）

使用方式：
    # 基于 git diff 检测变更（默认比较 HEAD~1）
    python scripts/ci/check_workflow_contract_version_policy.py

    # 比较指定 commit
    python scripts/ci/check_workflow_contract_version_policy.py --base HEAD~3

    # 使用 PR 模式（检测 PR 中的所有变更）
    python scripts/ci/check_workflow_contract_version_policy.py --pr-mode

    # 指定变更文件列表（用于测试或 CI 集成）
    python scripts/ci/check_workflow_contract_version_policy.py --changed-files file1.yml file2.json

    # JSON 输出（包含 trigger_reasons 字段）
    python scripts/ci/check_workflow_contract_version_policy.py --json

退出码：
    0: 检查通过（无关键变更，或版本已正确更新）
    1: 检查失败（关键变更但版本未更新）
    2: 文件读取/解析错误
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from scripts.ci.workflow_contract_common import (
    compute_set_diff,
    is_string_similar,
    parse_makefile_targets_from_content,
)
# ============================================================================
# Constants
# ============================================================================

DEFAULT_CONTRACT_PATH = "scripts/ci/workflow_contract.v1.json"
DEFAULT_DOC_PATH = "docs/ci_nightly_workflow_refactor/contract.md"


# ============================================================================
# Error Types - 统一定义
# ============================================================================
#
# 所有 error_type 的统一定义，便于维护和测试覆盖。
#
# 版本策略：
#   - 新增 error_type: Minor (0.X.0)
#   - 弃用 error_type: Major (X.0.0) - 需提供迁移路径
#   - 修改 error_type 含义: Major (X.0.0)
#
# 详见 docs/ci_nightly_workflow_refactor/contract.md 第 13 章
#


class VersionPolicyErrorTypes:
    """版本策略校验的 error_type 常量定义"""

    # 文件/解析错误
    CONTRACT_NOT_FOUND = "contract_not_found"
    CONTRACT_PARSE_ERROR = "contract_parse_error"
    DOC_NOT_FOUND = "doc_not_found"
    DOC_READ_ERROR = "doc_read_error"

    # 版本策略错误
    VERSION_NOT_UPDATED = "version_not_updated"
    LAST_UPDATED_NOT_UPDATED = "last_updated_not_updated"
    VERSION_NOT_IN_DOC = "version_not_in_doc"


# 导出所有 error_type 的集合（用于测试覆盖检查）
VERSION_POLICY_ERROR_TYPES = frozenset(
    {
        VersionPolicyErrorTypes.CONTRACT_NOT_FOUND,
        VersionPolicyErrorTypes.CONTRACT_PARSE_ERROR,
        VersionPolicyErrorTypes.DOC_NOT_FOUND,
        VersionPolicyErrorTypes.DOC_READ_ERROR,
        VersionPolicyErrorTypes.VERSION_NOT_UPDATED,
        VersionPolicyErrorTypes.LAST_UPDATED_NOT_UPDATED,
        VersionPolicyErrorTypes.VERSION_NOT_IN_DOC,
    }
)

# 文件错误集合（exit code = 2）
VERSION_POLICY_FILE_ERROR_TYPES = frozenset(
    {
        VersionPolicyErrorTypes.CONTRACT_NOT_FOUND,
        VersionPolicyErrorTypes.CONTRACT_PARSE_ERROR,
        VersionPolicyErrorTypes.DOC_NOT_FOUND,
        VersionPolicyErrorTypes.DOC_READ_ERROR,
    }
)


# ============================================================================
# Critical File Rules - 统一定义
# ============================================================================
#
# 每条规则包含：
#   - pattern: 正则表达式模式
#   - description: 规则描述（用于 trigger_reasons）
#   - category: 分类（workflow_core | contract_definition | contract_docs | tooling）
#
# Phase 2 范围说明：
#   当前覆盖 ci.yml、nightly.yml 与 release.yml。
#   可通过扩展 CRITICAL_WORKFLOW_RULES 支持更多 workflow 文件。
#


@dataclass
class CriticalFileRule:
    """关键文件规则定义"""

    pattern: str
    description: str
    category: str

    def matches(self, file_path: str) -> bool:
        """检查文件路径是否匹配此规则"""
        return bool(re.match(self.pattern, file_path))


# Workflow 核心文件规则
# 注意：当前支持 ci/nightly/release，扩展时修改此列表的正则表达式
#
# ============================================================================
# Release workflow 已纳入合约版本策略检查（Phase 2）
# ============================================================================
CRITICAL_WORKFLOW_RULES: list[CriticalFileRule] = [
    CriticalFileRule(
        pattern=r"^\.github/workflows/(ci|nightly|release)\.yml$",
        description="Phase 2 workflow 文件（ci.yml/nightly.yml/release.yml）",
        category="workflow_core",
    ),
]

# 合约定义文件规则
CRITICAL_CONTRACT_RULES: list[CriticalFileRule] = [
    CriticalFileRule(
        pattern=r"^scripts/ci/workflow_contract\.v\d+\.json$",
        description="合约定义 JSON 文件",
        category="contract_definition",
    ),
    CriticalFileRule(
        pattern=r"^docs/ci_nightly_workflow_refactor/.*\.md$",
        description="合约文档（docs/ci_nightly_workflow_refactor/）",
        category="contract_docs",
    ),
]

# 工具脚本关键文件规则
# 版本策略：
#   Major: 校验逻辑不兼容变更（如删除校验规则、修改错误码含义）
#   Minor: 新增校验功能、新增错误类型
#   Patch: 修复 bug、优化性能、完善错误提示
CRITICAL_TOOLING_RULES: list[CriticalFileRule] = [
    CriticalFileRule(
        pattern=r"^scripts/ci/validate_workflows\.py$",
        description="合约校验器核心脚本",
        category="tooling",
    ),
    CriticalFileRule(
        pattern=r"^scripts/ci/workflow_contract\.v\d+\.schema\.json$",
        description="合约 JSON Schema",
        category="tooling",
    ),
    CriticalFileRule(
        pattern=r"^scripts/ci/check_workflow_contract_docs_sync\.py$",
        description="文档同步校验脚本",
        category="tooling",
    ),
    CriticalFileRule(
        pattern=r"^scripts/ci/check_workflow_contract_error_types_docs_sync\.py$",
        description="Error Types 文档同步校验脚本",
        category="tooling",
    ),
    CriticalFileRule(
        pattern=r"^scripts/ci/workflow_contract_drift_report\.py$",
        description="漂移报告生成脚本",
        category="tooling",
    ),
    CriticalFileRule(
        pattern=r"^scripts/ci/generate_workflow_contract_snapshot\.py$",
        description="快照生成脚本",
        category="tooling",
    ),
]

# 所有关键文件规则（合并）
ALL_CRITICAL_RULES: list[CriticalFileRule] = (
    CRITICAL_WORKFLOW_RULES + CRITICAL_CONTRACT_RULES + CRITICAL_TOOLING_RULES
)

# Makefile 中与 workflow/CI 相关的目标关键字
# 只有当 Makefile 变更涉及这些关键字时才触发版本检查
MAKEFILE_CI_KEYWORDS = [
    "validate-workflows",
    "check-workflow",
    "ci:",
    "workflow-contract",
]

# Makefile target rename detection threshold
MAKEFILE_RENAME_SIMILARITY_THRESHOLD = 0.6

# Makefile 规则描述（用于 trigger_reasons）
MAKEFILE_RULE_DESCRIPTION = "Makefile CI/workflow 相关目标变更"

# Makefile target 行匹配（用于结构化检测）
MAKEFILE_TARGET_LINE_PATTERN = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9_.%/-]*(?:\s+[A-Za-z0-9][A-Za-z0-9_.%/-]*)*)\s*:(?!\s*=)"
)


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class VersionError:
    """版本策略错误"""

    error_type: str
    message: str
    suggestion: str


@dataclass
class VersionCheckResult:
    """版本检查结果"""

    success: bool = True
    errors: list[VersionError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    changed_critical_files: list[str] = field(default_factory=list)
    trigger_reasons: dict[str, str] = field(default_factory=dict)
    contract_version: str = ""
    contract_last_updated: str = ""
    doc_versions: list[str] = field(default_factory=list)
    version_updated: bool = False
    last_updated_updated: bool = False
    version_in_doc: bool = False

    def add_error(self, error: VersionError) -> None:
        """添加错误"""
        self.errors.append(error)
        self.success = False

    def add_warning(self, warning: str) -> None:
        """添加警告"""
        self.warnings.append(warning)

    def add_trigger_reason(self, file_path: str, reason: str) -> None:
        """添加触发原因"""
        self.trigger_reasons[file_path] = reason


# ============================================================================
# Git Operations
# ============================================================================


def get_git_diff_files(base: str = "HEAD~1") -> list[str]:
    """获取 git diff 中变更的文件列表

    Args:
        base: 比较基准（默认 HEAD~1）

    Returns:
        变更文件路径列表
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", base, "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
        return files
    except subprocess.CalledProcessError:
        # 如果 git diff 失败（如首次提交），尝试列出所有已暂存文件
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "--cached"],
                capture_output=True,
                text=True,
                check=True,
            )
            files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
            return files
        except subprocess.CalledProcessError:
            return []


def get_pr_changed_files() -> list[str]:
    """获取 PR 中所有变更的文件列表（从 merge-base 开始）

    Returns:
        变更文件路径列表
    """
    try:
        # 找到与 main/master 分支的 merge-base
        for main_branch in ["origin/main", "origin/master", "main", "master"]:
            result = subprocess.run(
                ["git", "merge-base", main_branch, "HEAD"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                merge_base = result.stdout.strip()
                break
        else:
            # 找不到 main/master，回退到 HEAD~10
            merge_base = "HEAD~10"

        result = subprocess.run(
            ["git", "diff", "--name-only", merge_base, "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
        return files
    except subprocess.CalledProcessError:
        return []


def get_file_diff_content(file_path: str, base: str = "HEAD~1") -> str | None:
    """获取指定文件的 diff 内容

    Args:
        file_path: 文件路径
        base: 比较基准

    Returns:
        diff 内容，如果文件未变更则返回 None
    """
    try:
        result = subprocess.run(
            ["git", "diff", base, "HEAD", "--", file_path],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout if result.stdout.strip() else None
    except subprocess.CalledProcessError:
        return None


def get_old_file_content(file_path: str, base: str = "HEAD~1") -> str | None:
    """获取文件在 base commit 时的内容

    Args:
        file_path: 文件路径
        base: commit 引用

    Returns:
        文件内容，如果文件不存在则返回 None
    """
    try:
        result = subprocess.run(
            ["git", "show", f"{base}:{file_path}"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError:
        return None


# ============================================================================
# Rule Groups - 统一分组定义（便于测试和维护）
# ============================================================================
#
# 按关键性分组，便于单独测试每个组的触发行为：
#   - WORKFLOW_PATTERNS: workflow 文件，任何变更必须 bump
#   - TOOLING_PATTERNS: 关键校验器脚本，变更影响合约执行逻辑
#   - CONTRACT_PATTERNS: 合约定义文件（JSON 和文档）
#   - NON_CRITICAL_DOC_PATTERNS: 非关键文档（不触发 bump）
#


class RuleGroups:
    """规则分组常量定义（便于测试导入）"""

    # workflow 文件（必须触发）
    WORKFLOW_FILES = frozenset(
        {
            ".github/workflows/ci.yml",
            ".github/workflows/nightly.yml",
            ".github/workflows/release.yml",
        }
    )

    # 关键工具脚本（必须触发）
    CRITICAL_TOOLING_SCRIPTS = frozenset(
        {
            "scripts/ci/validate_workflows.py",
            "scripts/ci/check_workflow_contract_docs_sync.py",
            "scripts/ci/check_workflow_contract_error_types_docs_sync.py",
            "scripts/ci/workflow_contract_drift_report.py",
            "scripts/ci/generate_workflow_contract_snapshot.py",
        }
    )

    # 关键工具脚本模式（正则匹配）
    CRITICAL_TOOLING_PATTERNS = [
        r"^scripts/ci/workflow_contract\.v\d+\.schema\.json$",
    ]

    # 非关键文档路径前缀（不触发）
    NON_CRITICAL_DOC_PREFIXES = [
        "docs/architecture/",
        "docs/dev/",
        "docs/guides/",
        "docs/legacy/",
        "docs/logbook/",
        "docs/openmemory/",
        "docs/reference/",
        "docs/seekdb/",
        "docs/gateway/",
        "docs/acceptance/",
    ]

    # Makefile CI 相关关键字（用于识别 CI 相关变更）
    MAKEFILE_CI_TARGET_KEYWORDS = [
        "validate-workflows",
        "check-workflow",
        "ci:",
        "workflow-contract",
    ]


# ============================================================================
# Pure Functions - 可测试纯函数接口
# ============================================================================
#
# 这些函数接受输入数据（changed_files, old_content, new_content）
# 并返回结果，不依赖外部状态或 git 操作，便于单元测试。
#


@dataclass
class VersionPolicyViolation:
    """版本策略违规项"""

    error_type: str
    message: str
    suggestion: str


@dataclass
class VersionPolicyCheckInput:
    """版本策略检查输入"""

    changed_files: list[str]
    old_contract_content: str | None  # 旧版本 contract JSON 内容
    new_contract_content: str  # 新版本 contract JSON 内容
    doc_content: str  # contract.md 文档内容
    makefile_diff: str | None = None  # Makefile diff 内容（可选）
    makefile_old_content: str | None = None  # Makefile 旧内容（可选）
    makefile_new_content: str | None = None  # Makefile 新内容（可选）
    makefile_required_targets: list[str] | None = None  # 合约 targets_required（可选）
    workflow_make_targets: list[str] | None = None  # workflow 中 make 调用（可选）


@dataclass
class VersionPolicyCheckOutput:
    """版本策略检查输出"""

    critical_files: list[str]  # 触发检查的关键文件
    trigger_reasons: dict[str, str]  # 每个关键文件的触发原因
    violations: list[VersionPolicyViolation]  # 违规项列表
    version_updated: bool  # version 是否已更新
    last_updated_updated: bool  # last_updated 是否已更新
    version_in_doc: bool  # 版本是否在文档中


def check_version_policy_pure(
    input_data: VersionPolicyCheckInput,
) -> VersionPolicyCheckOutput:
    """纯函数：检查版本策略违规

    此函数不依赖 git 操作或文件系统，仅基于输入数据进行检查，
    适合单元测试直接调用。

    Args:
        input_data: 版本策略检查输入

    Returns:
        版本策略检查输出
    """
    violations: list[VersionPolicyViolation] = []

    # 1. 过滤关键文件并获取触发原因
    makefile_old_targets = None
    makefile_new_targets = None
    if input_data.makefile_new_content is not None:
        makefile_new_targets = parse_makefile_targets_from_content(
            input_data.makefile_new_content
        )
    if input_data.makefile_old_content is not None:
        makefile_old_targets = parse_makefile_targets_from_content(
            input_data.makefile_old_content
        )
    elif makefile_new_targets is not None:
        makefile_old_targets = set()

    makefile_required_targets = (
        set(input_data.makefile_required_targets) if input_data.makefile_required_targets else None
    )
    workflow_make_targets = (
        set(input_data.workflow_make_targets) if input_data.workflow_make_targets else None
    )

    critical_files, trigger_reasons = filter_critical_files_with_reasons(
        input_data.changed_files,
        makefile_diff=input_data.makefile_diff,
        makefile_old_targets=makefile_old_targets,
        makefile_new_targets=makefile_new_targets,
        makefile_required_targets=makefile_required_targets,
        workflow_make_targets=workflow_make_targets,
    )

    # 如果没有关键文件变更，直接返回空结果
    if not critical_files:
        return VersionPolicyCheckOutput(
            critical_files=[],
            trigger_reasons={},
            violations=[],
            version_updated=False,
            last_updated_updated=False,
            version_in_doc=False,
        )

    # 2. 解析 contract JSON
    try:
        new_contract = json.loads(input_data.new_contract_content)
    except json.JSONDecodeError as e:
        violations.append(
            VersionPolicyViolation(
                error_type=VersionPolicyErrorTypes.CONTRACT_PARSE_ERROR,
                message=f"Failed to parse new contract JSON: {e}",
                suggestion="Fix JSON syntax in workflow_contract.v1.json",
            )
        )
        return VersionPolicyCheckOutput(
            critical_files=critical_files,
            trigger_reasons=trigger_reasons,
            violations=violations,
            version_updated=False,
            last_updated_updated=False,
            version_in_doc=False,
        )

    new_version = new_contract.get("version", "")
    new_last_updated = new_contract.get("last_updated", "")

    # 3. 检查版本是否更新
    version_updated = False
    last_updated_updated = False

    if input_data.old_contract_content is None:
        # 新文件，视为已更新
        version_updated = True
        last_updated_updated = True
    else:
        try:
            old_contract = json.loads(input_data.old_contract_content)
            old_version = old_contract.get("version", "")
            old_last_updated = old_contract.get("last_updated", "")

            version_updated = is_version_updated(old_version, new_version)
            last_updated_updated = new_last_updated != old_last_updated
        except json.JSONDecodeError:
            # 旧文件无法解析，视为已更新
            version_updated = True
            last_updated_updated = True

    # 4. 检查版本是否在文档中
    version_in_doc = _check_version_in_doc_content(input_data.doc_content, new_version)

    # 5. 生成违规项
    if not version_updated:
        violations.append(
            VersionPolicyViolation(
                error_type=VersionPolicyErrorTypes.VERSION_NOT_UPDATED,
                message=(
                    f"Critical files changed but 'version' field not updated in "
                    f"workflow_contract.v1.json (current: {new_version})"
                ),
                suggestion=(
                    "Update 'version' field according to SemVer policy: "
                    "Major for breaking changes, Minor for new features, Patch for fixes"
                ),
            )
        )

    if not last_updated_updated:
        violations.append(
            VersionPolicyViolation(
                error_type=VersionPolicyErrorTypes.LAST_UPDATED_NOT_UPDATED,
                message=(
                    f"Critical files changed but 'last_updated' field not updated in "
                    f"workflow_contract.v1.json (current: {new_last_updated})"
                ),
                suggestion=f"Update 'last_updated' to today's date: {date.today().isoformat()}",
            )
        )

    if not version_in_doc:
        violations.append(
            VersionPolicyViolation(
                error_type=VersionPolicyErrorTypes.VERSION_NOT_IN_DOC,
                message=(
                    f"Version '{new_version}' not found in "
                    f"contract.md version control table (Section 13)"
                ),
                suggestion=(
                    "Add a new row to the version control table in contract.md:\n"
                    f"| v{new_version} | {date.today().isoformat()} | <变更说明> |"
                ),
            )
        )

    return VersionPolicyCheckOutput(
        critical_files=critical_files,
        trigger_reasons=trigger_reasons,
        violations=violations,
        version_updated=version_updated,
        last_updated_updated=last_updated_updated,
        version_in_doc=version_in_doc,
    )


def is_workflow_file(file_path: str) -> bool:
    """检查文件是否为 workflow 文件

    Args:
        file_path: 文件路径

    Returns:
        是否为 workflow 文件
    """
    return file_path in RuleGroups.WORKFLOW_FILES


def is_critical_tooling_script(file_path: str) -> bool:
    """检查文件是否为关键工具脚本

    Args:
        file_path: 文件路径

    Returns:
        是否为关键工具脚本
    """
    if file_path in RuleGroups.CRITICAL_TOOLING_SCRIPTS:
        return True
    for pattern in RuleGroups.CRITICAL_TOOLING_PATTERNS:
        if re.match(pattern, file_path):
            return True
    return False


def is_non_critical_doc(file_path: str) -> bool:
    """检查文件是否为非关键文档（不触发版本检查）

    Args:
        file_path: 文件路径

    Returns:
        是否为非关键文档
    """
    for prefix in RuleGroups.NON_CRITICAL_DOC_PREFIXES:
        if file_path.startswith(prefix):
            return True
    return False


def _check_version_in_doc_content(doc_content: str, version: str) -> bool:
    """检查版本是否在文档版本控制表中（内部辅助函数）

    Args:
        doc_content: 文档内容
        version: 版本号

    Returns:
        版本是否在文档中
    """
    # 匹配版本控制表中的版本号
    # 格式：| v2.7.1 | 或 | 2.7.1 |
    pattern = r"\|\s*v?(\d+\.\d+\.\d+)\s*\|"
    matches = re.findall(pattern, doc_content)
    # 支持 v2.7.1 和 2.7.1 两种格式
    version_normalized = version.lstrip("v")
    return version_normalized in matches


# ============================================================================
# File Pattern Matching
# ============================================================================


def get_matching_rule(file_path: str) -> CriticalFileRule | None:
    """获取匹配文件路径的规则

    Args:
        file_path: 文件路径

    Returns:
        匹配的规则，如果不匹配则返回 None
    """
    for rule in ALL_CRITICAL_RULES:
        if rule.matches(file_path):
            return rule
    return None


def is_critical_file(file_path: str) -> bool:
    """检查文件是否为关键文件

    Args:
        file_path: 文件路径

    Returns:
        是否为关键文件
    """
    return get_matching_rule(file_path) is not None


def is_makefile_ci_related_change(diff_content: str) -> bool:
    """检查 Makefile 变更是否涉及 CI/workflow 相关目标

    Args:
        diff_content: Makefile 的 diff 内容

    Returns:
        是否涉及 CI/workflow 相关变更
    """
    if not diff_content:
        return False

    def is_changed_line(line: str) -> bool:
        return line.startswith(("+", "-")) and not line.startswith(("+++", "---"))

    def is_comment_or_echo(line: str) -> bool:
        stripped = line.lstrip()
        if not stripped:
            return True
        if stripped.startswith("#"):
            return True
        if stripped.startswith("@#"):
            return True
        if stripped.startswith("@echo") or stripped.startswith("echo "):
            return True
        return False

    def extract_target_names(line: str) -> list[str]:
        if not line:
            return []
        if line[0] in "+- ":
            line = line[1:]
        if not line or line[0].isspace():
            return []
        match = MAKEFILE_TARGET_LINE_PATTERN.match(line)
        if not match:
            return []
        return [target for target in match.group(1).split() if target]

    def is_ci_target(target: str) -> bool:
        target_with_colon = f"{target}:"
        return any(
            keyword in target or keyword in target_with_colon for keyword in MAKEFILE_CI_KEYWORDS
        )

    saw_target_context = False
    current_targets: list[str] = []

    for raw_line in diff_content.splitlines():
        if raw_line.startswith(("diff --git", "index ", "---", "+++")):
            continue
        if raw_line.startswith("@@"):
            current_targets = []
            continue

        target_names = extract_target_names(raw_line)
        if target_names:
            saw_target_context = True
            current_targets = target_names
            if is_changed_line(raw_line) and any(is_ci_target(t) for t in target_names):
                return True
            continue

        if is_changed_line(raw_line) and current_targets:
            if any(is_ci_target(t) for t in current_targets):
                return True

    if saw_target_context:
        return False

    for raw_line in diff_content.splitlines():
        if not is_changed_line(raw_line):
            continue
        content = raw_line[1:] if raw_line and raw_line[0] in "+-" else raw_line
        if is_comment_or_echo(content):
            continue
        if any(keyword in content for keyword in MAKEFILE_CI_KEYWORDS):
            return True

    return False


# ============================================================================
# Makefile Target Change Detection
# ============================================================================


def _load_make_targets_required(contract_path: Path) -> set[str]:
    """从合约文件加载 make.targets_required 集合"""
    if not contract_path.exists():
        return set()

    try:
        with open(contract_path, "r", encoding="utf-8") as file:
            contract = json.load(file)
    except (OSError, json.JSONDecodeError):
        return set()

    make_config = contract.get("make", {})
    targets_required = make_config.get("targets_required", [])
    return {target for target in targets_required if isinstance(target, str)}


def _extract_ci_workflow_make_targets(project_root: Path) -> set[str]:
    """从 CI workflow 中提取 make 调用目标集合"""
    workflow_path = project_root / ".github" / "workflows" / "ci.yml"
    if not workflow_path.exists():
        return set()

    try:
        from scripts.ci.validate_workflows import extract_workflow_make_calls
    except Exception:
        return set()

    try:
        make_calls = extract_workflow_make_calls(workflow_path)
    except Exception:
        return set()

    targets = {call.target for call in make_calls}
    return {target for target in targets if target}


def _detect_makefile_target_changes(
    old_targets: set[str],
    new_targets: set[str],
) -> tuple[set[str], set[str], list[tuple[str, str]]]:
    """检测 Makefile targets 的新增、删除、重命名变化"""
    removed, added = compute_set_diff(old_targets, new_targets)
    renamed: list[tuple[str, str]] = []

    if removed and added:
        remaining_added = set(added)
        remaining_removed = set(removed)

        for old_target in sorted(removed):
            for new_target in sorted(remaining_added):
                if is_string_similar(
                    old_target, new_target, threshold=MAKEFILE_RENAME_SIMILARITY_THRESHOLD
                ):
                    renamed.append((old_target, new_target))
                    remaining_added.remove(new_target)
                    remaining_removed.remove(old_target)
                    break

        added = remaining_added
        removed = remaining_removed

    return added, removed, renamed


def _format_makefile_target_reason(targets: set[str]) -> str:
    """格式化 Makefile 触发原因说明"""
    targets_list = ", ".join(sorted(targets))
    return f"Makefile target changed: {targets_list}"


def _evaluate_makefile_criticality(
    makefile_diff: str | None,
    old_targets: set[str] | None,
    new_targets: set[str] | None,
    required_targets: set[str] | None,
    workflow_targets: set[str] | None,
) -> tuple[bool, str | None]:
    """评估 Makefile 变更是否触发版本策略检查"""
    if new_targets is not None:
        if old_targets is None:
            old_targets = set()

        added, removed, renamed = _detect_makefile_target_changes(old_targets, new_targets)
        changed_targets = set(added) | set(removed)
        for old_target, new_target in renamed:
            changed_targets.add(old_target)
            changed_targets.add(new_target)

        if changed_targets:
            relevant_targets = set()
            if required_targets:
                relevant_targets.update(required_targets)
            if workflow_targets:
                relevant_targets.update(workflow_targets)

            matched_targets = changed_targets & relevant_targets
            if matched_targets:
                return True, _format_makefile_target_reason(matched_targets)

    # Fallback: keyword-based detection
    if makefile_diff and is_makefile_ci_related_change(makefile_diff):
        return True, MAKEFILE_RULE_DESCRIPTION

    return False, None


def filter_critical_files_with_reasons(
    changed_files: list[str],
    makefile_diff: str | None = None,
    makefile_old_targets: set[str] | None = None,
    makefile_new_targets: set[str] | None = None,
    makefile_required_targets: set[str] | None = None,
    workflow_make_targets: set[str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """过滤出关键文件列表并记录触发原因

    Args:
        changed_files: 所有变更文件列表
        makefile_diff: Makefile 的 diff 内容（可选）
        makefile_old_targets: 旧 Makefile target 集合（可选）
        makefile_new_targets: 新 Makefile target 集合（可选）
        makefile_required_targets: 合约 targets_required 集合（可选）
        workflow_make_targets: workflow 中 make 调用集合（可选）

    Returns:
        (关键文件列表, 触发原因字典)
    """
    critical: list[str] = []
    reasons: dict[str, str] = {}

    for file_path in changed_files:
        # Makefile 特殊处理：只有 CI 相关变更才算关键
        if file_path == "Makefile":
            is_critical, reason = _evaluate_makefile_criticality(
                makefile_diff=makefile_diff,
                old_targets=makefile_old_targets,
                new_targets=makefile_new_targets,
                required_targets=makefile_required_targets,
                workflow_targets=workflow_make_targets,
            )
            if is_critical:
                critical.append(file_path)
                reasons[file_path] = reason or MAKEFILE_RULE_DESCRIPTION
            continue

        rule = get_matching_rule(file_path)
        if rule is not None:
            critical.append(file_path)
            reasons[file_path] = rule.description

    return critical, reasons


def filter_critical_files(
    changed_files: list[str],
    makefile_diff: str | None = None,
    makefile_old_targets: set[str] | None = None,
    makefile_new_targets: set[str] | None = None,
    makefile_required_targets: set[str] | None = None,
    workflow_make_targets: set[str] | None = None,
) -> list[str]:
    """过滤出关键文件列表（向后兼容）

    Args:
        changed_files: 所有变更文件列表
        makefile_diff: Makefile 的 diff 内容（可选）

    Returns:
        关键文件列表
    """
    critical, _ = filter_critical_files_with_reasons(
        changed_files,
        makefile_diff=makefile_diff,
        makefile_old_targets=makefile_old_targets,
        makefile_new_targets=makefile_new_targets,
        makefile_required_targets=makefile_required_targets,
        workflow_make_targets=workflow_make_targets,
    )
    return critical


# ============================================================================
# Version Comparison
# ============================================================================


def parse_semver(version: str) -> tuple[int, int, int] | None:
    """解析 SemVer 版本号

    Args:
        version: 版本字符串（如 "2.6.0"）

    Returns:
        (major, minor, patch) 元组，解析失败返回 None
    """
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)$", version)
    if match:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return None


def is_version_updated(old_version: str, new_version: str) -> bool:
    """检查版本是否已更新（新版本大于旧版本）

    Args:
        old_version: 旧版本
        new_version: 新版本

    Returns:
        新版本是否大于旧版本
    """
    old_semver = parse_semver(old_version)
    new_semver = parse_semver(new_version)

    if old_semver is None or new_semver is None:
        # 如果无法解析，使用字符串比较
        return new_version != old_version

    return new_semver > old_semver


def is_date_current(date_str: str) -> bool:
    """检查日期是否为当前日期

    Args:
        date_str: 日期字符串（格式 YYYY-MM-DD）

    Returns:
        是否为当前日期
    """
    today = date.today().isoformat()
    return date_str == today


# ============================================================================
# Core Logic
# ============================================================================


class WorkflowContractVersionChecker:
    """Workflow Contract 版本策略检查器"""

    def __init__(
        self,
        project_root: Path,
        contract_path: str = DEFAULT_CONTRACT_PATH,
        doc_path: str = DEFAULT_DOC_PATH,
        verbose: bool = False,
    ) -> None:
        self.project_root = project_root
        self.contract_path = project_root / contract_path
        self.doc_path = project_root / doc_path
        self.verbose = verbose
        self.result = VersionCheckResult()
        self.contract: dict[str, Any] = {}
        self.doc_content: str = ""

    def _get_merge_base(self) -> str:
        """获取与 main/master 分支的 merge-base commit

        Returns:
            merge-base commit hash，找不到时返回 HEAD~1
        """
        try:
            for main_branch in ["origin/main", "origin/master", "main", "master"]:
                result = subprocess.run(
                    ["git", "merge-base", main_branch, "HEAD"],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    return result.stdout.strip()
        except Exception:
            pass
        return "HEAD~1"

    def load_contract(self) -> bool:
        """加载 contract JSON 文件"""
        if not self.contract_path.exists():
            self.result.add_error(
                VersionError(
                    error_type="contract_not_found",
                    message=f"Contract file not found: {self.contract_path}",
                    suggestion="Ensure workflow_contract.v1.json exists",
                )
            )
            return False

        try:
            with open(self.contract_path, "r", encoding="utf-8") as f:
                self.contract = json.load(f)
            self.result.contract_version = self.contract.get("version", "")
            self.result.contract_last_updated = self.contract.get("last_updated", "")
            return True
        except json.JSONDecodeError as e:
            self.result.add_error(
                VersionError(
                    error_type="contract_parse_error",
                    message=f"Failed to parse contract JSON: {e}",
                    suggestion="Fix JSON syntax in workflow_contract.v1.json",
                )
            )
            return False

    def load_doc(self) -> bool:
        """加载文档文件"""
        if not self.doc_path.exists():
            self.result.add_error(
                VersionError(
                    error_type="doc_not_found",
                    message=f"Documentation file not found: {self.doc_path}",
                    suggestion="Ensure contract.md exists",
                )
            )
            return False

        try:
            with open(self.doc_path, "r", encoding="utf-8") as f:
                self.doc_content = f.read()
            return True
        except Exception as e:
            self.result.add_error(
                VersionError(
                    error_type="doc_read_error",
                    message=f"Failed to read documentation file: {e}",
                    suggestion="Check file permissions",
                )
            )
            return False

    def extract_doc_versions(self) -> list[str]:
        """从文档版本控制表中提取版本号列表

        文档第 13 章的版本控制表格式：
        | 版本 | 日期 | 变更说明 |
        |------|------|----------|
        | v2.7.1 | 2026-02-02 | ... |

        Returns:
            版本号列表（如 ["2.7.1", "2.7.0", ...]）
        """
        versions: list[str] = []

        # 匹配版本控制表中的版本号
        # 格式：| v2.7.1 | 或 | 2.7.1 |
        pattern = r"\|\s*v?(\d+\.\d+\.\d+)\s*\|"
        matches = re.findall(pattern, self.doc_content)
        versions.extend(matches)

        self.result.doc_versions = versions
        return versions

    def check_version_in_doc(self, version: str) -> bool:
        """检查版本是否在文档版本控制表中

        Args:
            version: 版本号

        Returns:
            版本是否在文档中
        """
        doc_versions = self.extract_doc_versions()
        # 支持 v2.7.1 和 2.7.1 两种格式
        version_normalized = version.lstrip("v")
        return version_normalized in doc_versions

    def check_version_updated(self, base: str) -> bool:
        """检查 version 字段是否已更新

        Args:
            base: 比较基准 commit

        Returns:
            版本是否已更新
        """
        contract_rel_path = str(self.contract_path.relative_to(self.project_root))
        old_content = get_old_file_content(contract_rel_path, base)

        if old_content is None:
            # 新文件，视为已更新
            self.result.version_updated = True
            return True

        try:
            old_contract = json.loads(old_content)
            old_version = old_contract.get("version", "")
            new_version = self.result.contract_version

            if is_version_updated(old_version, new_version):
                self.result.version_updated = True
                if self.verbose:
                    print(f"  Version updated: {old_version} -> {new_version}")
                return True
            else:
                if self.verbose:
                    print(f"  Version NOT updated: {old_version} == {new_version}")
                return False
        except json.JSONDecodeError:
            # 旧文件无法解析，视为已更新
            self.result.version_updated = True
            return True

    def check_last_updated_updated(self, base: str) -> bool:
        """检查 last_updated 字段是否已更新

        Args:
            base: 比较基准 commit

        Returns:
            last_updated 是否已更新
        """
        contract_rel_path = str(self.contract_path.relative_to(self.project_root))
        old_content = get_old_file_content(contract_rel_path, base)

        if old_content is None:
            # 新文件，视为已更新
            self.result.last_updated_updated = True
            return True

        try:
            old_contract = json.loads(old_content)
            old_date = old_contract.get("last_updated", "")
            new_date = self.result.contract_last_updated

            if new_date != old_date:
                self.result.last_updated_updated = True
                if self.verbose:
                    print(f"  last_updated updated: {old_date} -> {new_date}")
                return True
            else:
                if self.verbose:
                    print(f"  last_updated NOT updated: {old_date}")
                return False
        except json.JSONDecodeError:
            # 旧文件无法解析，视为已更新
            self.result.last_updated_updated = True
            return True

    def check(
        self,
        changed_files: list[str] | None = None,
        base: str = "HEAD~1",
        pr_mode: bool = False,
    ) -> VersionCheckResult:
        """执行版本策略检查

        Args:
            changed_files: 变更文件列表（可选，不提供则从 git diff 获取）
            base: 比较基准 commit
            pr_mode: 是否为 PR 模式（从 merge-base 开始比较）

        Returns:
            检查结果
        """
        # 1. 获取变更文件列表
        # 在 PR 模式下，同时更新 base 为 merge-base，确保版本比较一致
        if changed_files is None:
            if pr_mode:
                changed_files = get_pr_changed_files()
                # 更新 base 为 merge-base，确保版本比较与变更检测使用同一基准
                base = self._get_merge_base()
                if self.verbose:
                    print(f"Using PR mode: comparing from merge-base ({base})")
            else:
                changed_files = get_git_diff_files(base)

        if self.verbose:
            print(f"Changed files ({len(changed_files)}):")
            for f in changed_files:
                print(f"  - {f}")
            print()

        # 2. 过滤关键文件并获取触发原因
        makefile_diff = None
        makefile_old_targets = None
        makefile_new_targets = None
        makefile_required_targets = None
        workflow_make_targets = None
        if "Makefile" in changed_files:
            makefile_diff = get_file_diff_content("Makefile", base)
            makefile_path = self.project_root / "Makefile"
            if makefile_path.exists():
                try:
                    new_content = makefile_path.read_text(encoding="utf-8")
                    makefile_new_targets = parse_makefile_targets_from_content(new_content)
                except OSError:
                    makefile_new_targets = None

                old_content = get_old_file_content("Makefile", base)
                if old_content is not None and makefile_new_targets is not None:
                    makefile_old_targets = parse_makefile_targets_from_content(old_content)
                elif makefile_new_targets is not None:
                    makefile_old_targets = set()

                makefile_required_targets = _load_make_targets_required(self.contract_path)
                workflow_make_targets = _extract_ci_workflow_make_targets(self.project_root)

        critical_files, trigger_reasons = filter_critical_files_with_reasons(
            changed_files,
            makefile_diff=makefile_diff,
            makefile_old_targets=makefile_old_targets,
            makefile_new_targets=makefile_new_targets,
            makefile_required_targets=makefile_required_targets,
            workflow_make_targets=workflow_make_targets,
        )
        self.result.changed_critical_files = critical_files
        self.result.trigger_reasons = trigger_reasons

        if self.verbose:
            print(f"Critical files ({len(critical_files)}):")
            for f in critical_files:
                reason = trigger_reasons.get(f, "unknown")
                print(f"  - {f} ({reason})")
            print()

        # 3. 如果没有关键文件变更，直接通过
        if not critical_files:
            if self.verbose:
                print("No critical files changed. Version policy check SKIPPED.")
            return self.result

        # 4. 加载 contract 和文档
        if self.verbose:
            print(f"Loading contract: {self.contract_path}")

        if not self.load_contract():
            return self.result

        if self.verbose:
            print(f"Loading documentation: {self.doc_path}")

        if not self.load_doc():
            return self.result

        # 5. 检查 version 是否更新
        if self.verbose:
            print("\nChecking version field...")

        version_updated = self.check_version_updated(base)

        if not version_updated:
            self.result.add_error(
                VersionError(
                    error_type="version_not_updated",
                    message=(
                        f"Critical files changed but 'version' field not updated in "
                        f"workflow_contract.v1.json (current: {self.result.contract_version})"
                    ),
                    suggestion=(
                        "Update 'version' field according to SemVer policy: "
                        "Major for breaking changes, Minor for new features, Patch for fixes"
                    ),
                )
            )

        # 6. 检查 last_updated 是否更新
        if self.verbose:
            print("\nChecking last_updated field...")

        last_updated_updated = self.check_last_updated_updated(base)

        if not last_updated_updated:
            self.result.add_error(
                VersionError(
                    error_type="last_updated_not_updated",
                    message=(
                        f"Critical files changed but 'last_updated' field not updated in "
                        f"workflow_contract.v1.json (current: {self.result.contract_last_updated})"
                    ),
                    suggestion=f"Update 'last_updated' to today's date: {date.today().isoformat()}",
                )
            )

        # 7. 检查版本是否在文档版本控制表中
        if self.verbose:
            print("\nChecking version in documentation...")

        version_in_doc = self.check_version_in_doc(self.result.contract_version)
        self.result.version_in_doc = version_in_doc

        if not version_in_doc:
            self.result.add_error(
                VersionError(
                    error_type="version_not_in_doc",
                    message=(
                        f"Version '{self.result.contract_version}' not found in "
                        f"contract.md version control table (Section 13)"
                    ),
                    suggestion=(
                        "Add a new row to the version control table in contract.md:\n"
                        f"| v{self.result.contract_version} | {date.today().isoformat()} | <变更说明> |"
                    ),
                )
            )

        return self.result


# ============================================================================
# Output Formatting
# ============================================================================


def format_text_output(result: VersionCheckResult) -> str:
    """格式化文本输出"""
    lines: list[str] = []

    if result.success:
        lines.append("=" * 60)
        lines.append("Workflow Contract Version Policy Check: PASSED")
        lines.append("=" * 60)
    else:
        lines.append("=" * 60)
        lines.append("Workflow Contract Version Policy Check: FAILED")
        lines.append("=" * 60)

    lines.append("")
    lines.append("Summary:")
    lines.append(f"  - Critical files changed: {len(result.changed_critical_files)}")
    if result.changed_critical_files:
        for f in result.changed_critical_files:
            reason = result.trigger_reasons.get(f, "")
            if reason:
                lines.append(f"      - {f} ({reason})")
            else:
                lines.append(f"      - {f}")
    lines.append(f"  - Contract version: {result.contract_version or '(not loaded)'}")
    lines.append(f"  - Contract last_updated: {result.contract_last_updated or '(not loaded)'}")
    lines.append(f"  - Version updated: {'Yes' if result.version_updated else 'No'}")
    lines.append(f"  - last_updated updated: {'Yes' if result.last_updated_updated else 'No'}")
    lines.append(f"  - Version in doc: {'Yes' if result.version_in_doc else 'No'}")
    lines.append(f"  - Errors: {len(result.errors)}")
    lines.append(f"  - Warnings: {len(result.warnings)}")

    if result.errors:
        lines.append("")
        lines.append("ERRORS:")
        for error in result.errors:
            lines.append(f"  [{error.error_type}]")
            lines.append(f"    {error.message}")
            lines.append(f"    Suggestion: {error.suggestion}")

    if result.warnings:
        lines.append("")
        lines.append("WARNINGS:")
        for warning in result.warnings:
            lines.append(f"  {warning}")

    return "\n".join(lines)


def format_json_output(result: VersionCheckResult) -> str:
    """格式化 JSON 输出"""
    output = {
        "success": result.success,
        "error_count": len(result.errors),
        "warning_count": len(result.warnings),
        "changed_critical_files": result.changed_critical_files,
        "trigger_reasons": result.trigger_reasons,
        "contract_version": result.contract_version,
        "contract_last_updated": result.contract_last_updated,
        "doc_versions": result.doc_versions,
        "version_updated": result.version_updated,
        "last_updated_updated": result.last_updated_updated,
        "version_in_doc": result.version_in_doc,
        "errors": [
            {
                "error_type": e.error_type,
                "message": e.message,
                "suggestion": e.suggestion,
            }
            for e in result.errors
        ],
        "warnings": result.warnings,
    }
    return json.dumps(output, indent=2, ensure_ascii=False)


# ============================================================================
# Main
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查 workflow contract 版本策略：关键文件变更时强制版本更新",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
关键文件规则（按类别）：

  [workflow_core] Workflow 文件（扩展时修改 CRITICAL_WORKFLOW_RULES）：
    - .github/workflows/ci.yml
    - .github/workflows/nightly.yml
    - .github/workflows/release.yml

  [contract_definition] 合约定义文件：
    - scripts/ci/workflow_contract.v*.json
    - docs/ci_nightly_workflow_refactor/*.md

  [tooling] 工具脚本（变更影响合约执行逻辑）：
    - scripts/ci/validate_workflows.py
    - scripts/ci/workflow_contract.v*.schema.json
    - scripts/ci/check_workflow_contract_docs_sync.py
    - scripts/ci/check_workflow_contract_error_types_docs_sync.py
    - scripts/ci/workflow_contract_drift_report.py
    - scripts/ci/generate_workflow_contract_snapshot.py

  [special] Makefile（仅 CI 相关变更触发）

版本策略要求：
  1. workflow_contract.v1.json 的 version 字段必须更新
  2. workflow_contract.v1.json 的 last_updated 字段必须更新
  3. contract.md 版本控制表（第 14 章）必须包含新版本

版本升级建议：
  - Major: 不兼容变更（如删除校验规则、修改错误码含义）
  - Minor: 新增功能（新增校验规则、新增错误类型）
  - Patch: 修复/优化（修复 bug、优化性能、完善错误提示）

JSON 输出包含 trigger_reasons 字段，说明每个关键文件触发检查的原因。
        """,
    )
    parser.add_argument(
        "--base",
        type=str,
        default="HEAD~1",
        help="Git diff 比较基准 (default: HEAD~1)",
    )
    parser.add_argument(
        "--pr-mode",
        action="store_true",
        help="PR 模式：从 merge-base 开始比较（适用于 CI PR 检查）",
    )
    parser.add_argument(
        "--changed-files",
        nargs="*",
        type=str,
        default=None,
        help="指定变更文件列表（用于测试或 CI 集成）",
    )
    parser.add_argument(
        "--contract",
        type=str,
        default=DEFAULT_CONTRACT_PATH,
        help=f"Contract JSON 文件路径 (default: {DEFAULT_CONTRACT_PATH})",
    )
    parser.add_argument(
        "--doc",
        type=str,
        default=DEFAULT_DOC_PATH,
        help=f"Documentation Markdown 文件路径 (default: {DEFAULT_DOC_PATH})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="显示详细检查过程",
    )
    parser.add_argument(
        "--project-root",
        type=str,
        default=None,
        help="项目根目录（默认使用当前工作目录）",
    )

    args = parser.parse_args()

    # 确定项目根目录
    if args.project_root:
        project_root = Path(args.project_root)
    else:
        # 尝试从脚本位置推断项目根目录
        script_path = Path(__file__).resolve()
        # scripts/ci/check_workflow_contract_version_policy.py -> project_root
        project_root = script_path.parent.parent.parent
        if not (project_root / "scripts" / "ci").exists():
            # 回退到当前工作目录
            project_root = Path.cwd()

    if args.verbose and not args.json:
        print(f"Project root: {project_root}")
        print()

    checker = WorkflowContractVersionChecker(
        project_root=project_root,
        contract_path=args.contract,
        doc_path=args.doc,
        verbose=args.verbose and not args.json,
    )

    result = checker.check(
        changed_files=args.changed_files,
        base=args.base,
        pr_mode=args.pr_mode,
    )

    if args.json:
        print(format_json_output(result))
    else:
        print(format_text_output(result))

    # 返回退出码
    if result.errors:
        # 区分文件错误和版本策略错误
        file_errors = [
            e
            for e in result.errors
            if e.error_type
            in ("contract_not_found", "doc_not_found", "contract_parse_error", "doc_read_error")
        ]
        if file_errors:
            return 2
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
