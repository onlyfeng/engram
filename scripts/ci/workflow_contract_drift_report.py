#!/usr/bin/env python3
"""
Workflow Contract Drift Report Generator

读取 workflow 快照与 workflow_contract.v2.json，比较差异，
输出 drift 的 JSON 报告（可选渲染 Markdown）。

功能:
- 生成实时 workflow 快照
- 与 contract 中定义的 job_ids, job_names, required_steps 等进行比较
- 输出结构化的 drift 报告（JSON 或 Markdown 格式）

用法:
    # 生成 drift report（JSON 格式，输出到 stdout）
    python scripts/ci/workflow_contract_drift_report.py

    # 生成 drift report（Markdown 格式）
    python scripts/ci/workflow_contract_drift_report.py --markdown

    # 输出到文件
    python scripts/ci/workflow_contract_drift_report.py --output drift_report.json

    # 输出到 artifacts（便于 PR 评审/上传）
    python scripts/ci/workflow_contract_drift_report.py --output artifacts/workflow_contract_drift.json
    python scripts/ci/workflow_contract_drift_report.py --markdown --output artifacts/workflow_contract_drift.md

    # 或使用 Make target（同时生成 JSON + Markdown）
    make workflow-contract-drift-report-all

    # 只检查 ci workflow
    python scripts/ci/workflow_contract_drift_report.py --workflow ci

退出码:
    0: 无 drift（合约与实际 workflow 一致）
    1: 存在 drift（合约与实际 workflow 不一致）
    2: 文件读取/解析错误
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# ============================================================================
# Output Schema Definition
# ============================================================================
#
# Drift Report 输出 schema 定义
#
# 版本策略：
#   - DRIFT_REPORT_SCHEMA_VERSION: 输出 schema 版本号
#   - 新增字段: Minor 版本升级 (1.x.0)
#   - 移除字段: Major 版本升级 (x.0.0)，需提供迁移路径
#   - 字段语义变更: Major 版本升级 (x.0.0)
#
# 字段稳定性保证：
#   - 所有输出字段按字母序排序，确保相同输入产生相同输出
#   - drift_items 按 (workflow, category, drift_type, key) 排序
#   - summary keys 按字母序排序
#
# 依赖此输出的下游消费者：
#   - suggest_workflow_contract_updates.py (建议工具)
#   - CI 报告生成脚本
#   - 外部监控/分析工具
#

DRIFT_REPORT_SCHEMA_VERSION = "2.0.0"
"""
Drift Report 输出 schema 版本号

输出字段（按字母序）：
- contract_last_updated: str - 合约最后更新时间
- contract_version: str - 合约版本号
- drift_count: int - drift 项数量
- drift_items: list[dict] - drift 项列表（按 workflow/category/drift_type/key 排序）
- has_drift: bool - 是否存在 drift
- report_generated_at: str - 报告生成时间（ISO 8601 UTC）
- schema_version: str - 输出 schema 版本号
- summary: dict[str, int] - 按 category_drift_type 分组的计数（keys 按字母序）
- workflows_checked: list[str] - 已检查的 workflow 列表（按字母序）

drift_items 每项字段（按字母序）：
- actual_value: str | None - 实际值
- category: str - drift 分类
- contract_value: str | None - 合约值
- drift_type: str - drift 类型
- key: str - drift 项标识
- location: str | None - 位置信息
- severity: str - 严重程度
- workflow: str - workflow 名称
"""

# 复用 validate_workflows.py 中的 artifact path 和 make target 提取逻辑
from scripts.ci.validate_workflows import (
    check_artifact_path_coverage,
    extract_upload_artifact_paths,
    extract_workflow_make_calls,
    parse_makefile_targets,
)
from scripts.ci.workflow_contract_common import (
    discover_workflow_keys,
    find_fuzzy_match,
    normalize_artifact_path,
)

# ============================================================================
# Drift Types, Categories, Severities - 统一定义
# ============================================================================
#
# 所有 drift 相关常量的统一定义，便于维护和测试覆盖。
#
# 版本策略：
#   - 新增 drift_type/category: Minor (0.X.0)
#   - 弃用 drift_type/category: Major (X.0.0) - 需提供迁移路径
#   - 修改 drift_type/category 含义: Major (X.0.0)
#
# 详见 docs/ci_nightly_workflow_refactor/contract.md 第 13 章
#


class DriftTypes:
    """Drift 类型常量定义"""

    ADDED = "added"      # 实际存在但合约未声明
    REMOVED = "removed"  # 合约声明但实际不存在
    CHANGED = "changed"  # 存在但值/名称不同


class DriftCategories:
    """Drift 分类常量定义"""

    WORKFLOW = "workflow"        # workflow 文件级别
    JOB_ID = "job_id"            # Job ID
    JOB_NAME = "job_name"        # Job Name
    STEP = "step"                # Step Name
    ENV_VAR = "env_var"          # 环境变量
    ARTIFACT_PATH = "artifact_path"  # Artifact 路径
    MAKE_TARGET = "make_target"  # Makefile Target
    LABEL = "label"              # PR Label


class DriftSeverities:
    """Drift 严重程度常量定义"""

    INFO = "info"        # 信息性提示（如新增了合约未要求的项）
    WARNING = "warning"  # 警告（如名称变更）
    ERROR = "error"      # 错误（如必需项缺失）


# 导出所有常量的集合（用于测试覆盖检查）
DRIFT_TYPES = frozenset({
    DriftTypes.ADDED,
    DriftTypes.REMOVED,
    DriftTypes.CHANGED,
})

DRIFT_CATEGORIES = frozenset({
    DriftCategories.WORKFLOW,
    DriftCategories.JOB_ID,
    DriftCategories.JOB_NAME,
    DriftCategories.STEP,
    DriftCategories.ENV_VAR,
    DriftCategories.ARTIFACT_PATH,
    DriftCategories.MAKE_TARGET,
    DriftCategories.LABEL,
})

DRIFT_SEVERITIES = frozenset({
    DriftSeverities.INFO,
    DriftSeverities.WARNING,
    DriftSeverities.ERROR,
})

# Severity 映射：各 category + drift_type 组合的默认严重程度
# 格式: (category, drift_type) -> severity
DRIFT_SEVERITY_MAP = {
    # job_id: removed 是错误，added 是警告
    (DriftCategories.JOB_ID, DriftTypes.REMOVED): DriftSeverities.ERROR,
    (DriftCategories.JOB_ID, DriftTypes.ADDED): DriftSeverities.WARNING,
    # job_name: changed 是警告
    (DriftCategories.JOB_NAME, DriftTypes.CHANGED): DriftSeverities.WARNING,
    # step: removed 是错误，changed 是警告
    (DriftCategories.STEP, DriftTypes.REMOVED): DriftSeverities.ERROR,
    (DriftCategories.STEP, DriftTypes.CHANGED): DriftSeverities.WARNING,
    # env_var: removed 是错误
    (DriftCategories.ENV_VAR, DriftTypes.REMOVED): DriftSeverities.ERROR,
    # artifact_path: removed 是错误，added 是信息
    (DriftCategories.ARTIFACT_PATH, DriftTypes.REMOVED): DriftSeverities.ERROR,
    (DriftCategories.ARTIFACT_PATH, DriftTypes.ADDED): DriftSeverities.INFO,
    # make_target: removed 是错误，added 是警告
    (DriftCategories.MAKE_TARGET, DriftTypes.REMOVED): DriftSeverities.ERROR,
    (DriftCategories.MAKE_TARGET, DriftTypes.ADDED): DriftSeverities.WARNING,
    # label: removed 是错误，added 是警告
    (DriftCategories.LABEL, DriftTypes.REMOVED): DriftSeverities.ERROR,
    (DriftCategories.LABEL, DriftTypes.ADDED): DriftSeverities.WARNING,
    # workflow: removed 是错误
    (DriftCategories.WORKFLOW, DriftTypes.REMOVED): DriftSeverities.ERROR,
}


# 默认 severity（当 category + drift_type 组合不在 DRIFT_SEVERITY_MAP 中时）
DEFAULT_DRIFT_SEVERITY = DriftSeverities.WARNING


def get_drift_severity(category: str, drift_type: str) -> str:
    """从 DRIFT_SEVERITY_MAP 获取 severity，未定义时返回默认值

    Args:
        category: drift 分类（如 job_id, step 等）
        drift_type: drift 类型（added, removed, changed）

    Returns:
        severity 字符串（info, warning, error）
    """
    return DRIFT_SEVERITY_MAP.get((category, drift_type), DEFAULT_DRIFT_SEVERITY)


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class DriftItem:
    """单个 drift 项"""

    drift_type: str  # added, removed, changed
    category: str  # job_id, job_name, step, env_var, artifact_path, make_target, label
    workflow: str
    key: str
    contract_value: str | None = None
    actual_value: str | None = None
    location: str | None = None
    severity: str = "warning"  # info, warning, error


@dataclass
class DriftReport:
    """Drift 报告"""

    has_drift: bool = False
    drift_items: list[DriftItem] = field(default_factory=list)
    contract_version: str = ""
    contract_last_updated: str = ""
    report_generated_at: str = ""
    workflows_checked: list[str] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)

    def add_drift(self, item: DriftItem) -> None:
        """添加 drift 项"""
        self.drift_items.append(item)
        self.has_drift = True

        # 更新 summary
        key = f"{item.category}_{item.drift_type}"
        self.summary[key] = self.summary.get(key, 0) + 1


# ============================================================================
# Core Logic
# ============================================================================


class WorkflowContractDriftAnalyzer:
    """Workflow Contract Drift 分析器"""

    def __init__(
        self,
        contract_path: Path,
        workspace_root: Path,
        workflow_filter: str | None = None,
    ) -> None:
        self.contract_path = contract_path
        self.workspace_root = workspace_root
        self.workflow_filter = workflow_filter
        self.contract: dict[str, Any] = {}
        self.report = DriftReport()
        # 使用 UTC 时间，格式为 ISO 8601 带 Z 后缀
        self.report.report_generated_at = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    def _make_drift_item(
        self,
        category: str,
        drift_type: str,
        workflow: str,
        key: str,
        *,
        contract_value: str | None = None,
        actual_value: str | None = None,
        location: str | None = None,
        severity: str | None = None,
    ) -> DriftItem:
        """统一创建 DriftItem，自动从 DRIFT_SEVERITY_MAP 推导 severity

        Args:
            category: drift 分类（如 job_id, step 等）
            drift_type: drift 类型（added, removed, changed）
            workflow: workflow 名称
            key: drift 项的 key
            contract_value: 合约中的值（可选）
            actual_value: 实际值（可选）
            location: 位置信息（可选）
            severity: 显式覆盖 severity（可选，不指定则从 map 推导）

        Returns:
            DriftItem 实例
        """
        resolved_severity = severity if severity is not None else get_drift_severity(
            category, drift_type
        )
        return DriftItem(
            drift_type=drift_type,
            category=category,
            workflow=workflow,
            key=key,
            contract_value=contract_value,
            actual_value=actual_value,
            location=location,
            severity=resolved_severity,
        )

    def load_contract(self) -> bool:
        """加载 contract JSON 文件"""
        if not self.contract_path.exists():
            print(f"错误: Contract 文件不存在: {self.contract_path}", file=sys.stderr)
            return False

        try:
            with open(self.contract_path, "r", encoding="utf-8") as f:
                self.contract = json.load(f)
            self.report.contract_version = self.contract.get("version", "unknown")
            self.report.contract_last_updated = self.contract.get("last_updated", "unknown")
            return True
        except json.JSONDecodeError as e:
            print(f"错误: 解析 contract JSON 失败: {e}", file=sys.stderr)
            return False

    def load_workflow(self, workflow_file: Path) -> dict[str, Any] | None:
        """加载 workflow YAML 文件"""
        if not workflow_file.exists():
            return None

        try:
            with open(workflow_file, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        except yaml.YAMLError as e:
            print(f"警告: 解析 workflow YAML 失败 ({workflow_file}): {e}", file=sys.stderr)
            return None

    def analyze_job_ids(
        self,
        workflow_key: str,
        contract_job_ids: list[str],
        actual_jobs: dict[str, Any],
    ) -> None:
        """分析 job_ids 的 drift"""
        actual_job_ids = set(actual_jobs.keys())
        contract_job_ids_set = set(contract_job_ids)

        # 检查 contract 中有但实际不存在的 job_ids（removed）
        for job_id in contract_job_ids_set - actual_job_ids:
            self.report.add_drift(
                self._make_drift_item(
                    category=DriftCategories.JOB_ID,
                    drift_type=DriftTypes.REMOVED,
                    workflow=workflow_key,
                    key=job_id,
                    contract_value=job_id,
                    location=f"jobs.{job_id}",
                )
            )

        # 检查实际存在但 contract 中没有的 job_ids（added）
        for job_id in actual_job_ids - contract_job_ids_set:
            self.report.add_drift(
                self._make_drift_item(
                    category=DriftCategories.JOB_ID,
                    drift_type=DriftTypes.ADDED,
                    workflow=workflow_key,
                    key=job_id,
                    actual_value=job_id,
                    location=f"jobs.{job_id}",
                )
            )

    def analyze_job_names(
        self,
        workflow_key: str,
        contract_job_ids: list[str],
        contract_job_names: list[str],
        actual_jobs: dict[str, Any],
    ) -> None:
        """分析 job_names 的 drift"""
        # 建立 job_id -> contract_job_name 映射
        id_to_name = {}
        for i, job_id in enumerate(contract_job_ids):
            if i < len(contract_job_names):
                id_to_name[job_id] = contract_job_names[i]

        for job_id, expected_name in id_to_name.items():
            if job_id not in actual_jobs:
                continue  # job_ids 分析已经报告了

            actual_name = actual_jobs[job_id].get("name", "")
            if actual_name != expected_name:
                self.report.add_drift(
                    self._make_drift_item(
                        category=DriftCategories.JOB_NAME,
                        drift_type=DriftTypes.CHANGED,
                        workflow=workflow_key,
                        key=job_id,
                        contract_value=expected_name,
                        actual_value=actual_name,
                        location=f"jobs.{job_id}.name",
                    )
                )

    def analyze_required_steps(
        self,
        workflow_key: str,
        required_jobs: list[dict[str, Any]],
        actual_jobs: dict[str, Any],
    ) -> None:
        """分析 required_steps 的 drift"""
        for job_contract in required_jobs:
            job_id = job_contract.get("id", "")
            required_steps = job_contract.get("required_steps", [])

            if not job_id or job_id not in actual_jobs:
                continue

            actual_steps = actual_jobs[job_id].get("steps", [])
            actual_step_names = {step.get("name", "") for step in actual_steps}

            for required_step in required_steps:
                if required_step not in actual_step_names:
                    # 尝试模糊匹配
                    fuzzy_match = self._find_fuzzy_match(required_step, list(actual_step_names))

                    if fuzzy_match:
                        self.report.add_drift(
                            self._make_drift_item(
                                category=DriftCategories.STEP,
                                drift_type=DriftTypes.CHANGED,
                                workflow=workflow_key,
                                key=f"{job_id}/{required_step}",
                                contract_value=required_step,
                                actual_value=fuzzy_match,
                                location=f"jobs.{job_id}.steps",
                            )
                        )
                    else:
                        self.report.add_drift(
                            self._make_drift_item(
                                category=DriftCategories.STEP,
                                drift_type=DriftTypes.REMOVED,
                                workflow=workflow_key,
                                key=f"{job_id}/{required_step}",
                                contract_value=required_step,
                                location=f"jobs.{job_id}.steps",
                            )
                        )

    def analyze_env_vars(
        self,
        workflow_key: str,
        required_env_vars: list[str],
        workflow_data: dict[str, Any],
    ) -> None:
        """分析 required_env_vars 的 drift"""
        actual_env = workflow_data.get("env", {})

        for required_var in required_env_vars:
            if required_var not in actual_env:
                self.report.add_drift(
                    self._make_drift_item(
                        category=DriftCategories.ENV_VAR,
                        drift_type=DriftTypes.REMOVED,
                        workflow=workflow_key,
                        key=required_var,
                        contract_value=required_var,
                        location="env",
                    )
                )

    def _normalize_path_for_comparison(self, path: str) -> str:
        """标准化路径用于比较

        Args:
            path: 要标准化的路径

        Returns:
            标准化后的路径，如果出错则返回原路径
        """
        try:
            return normalize_artifact_path(path, allow_empty=True)
        except Exception:
            return path

    def analyze_artifact_paths(
        self,
        workflow_key: str,
        artifact_contract: dict[str, Any],
        workflow_data: dict[str, Any],
    ) -> None:
        """分析 artifact_path 的 drift

        比较 contract 中定义的 required_artifact_paths 与实际 workflow 中
        upload-artifact 步骤上传的路径。

        路径比较前会进行标准化处理：
        - 统一分隔符（反斜杠转正斜杠）
        - 处理 ./ 前缀
        - 去除重复斜杠

        Args:
            workflow_key: workflow 名称
            artifact_contract: artifact_archive 合约定义
            workflow_data: 解析后的 workflow 数据
        """
        required_paths = artifact_contract.get("required_artifact_paths", [])
        if not required_paths:
            return

        step_name_filter = artifact_contract.get("artifact_step_names")

        # 提取所有 upload-artifact 步骤的路径
        upload_steps = extract_upload_artifact_paths(workflow_data)

        # 收集实际上传的路径（标准化）
        actual_paths: set[str] = set()
        actual_paths_original: dict[str, str] = {}  # normalized -> original
        for step in upload_steps:
            # 如果有 step name 过滤器，只检查匹配的 step
            if step_name_filter:
                step_name = step.get("step_name", "")
                if not any(
                    filter_name.lower() in step_name.lower() for filter_name in step_name_filter
                ):
                    continue
            for path in step.get("paths", []):
                normalized = self._normalize_path_for_comparison(path)
                if normalized:
                    actual_paths.add(normalized)
                    actual_paths_original[normalized] = path

        # 检查覆盖情况
        covered, missing = check_artifact_path_coverage(
            upload_steps, required_paths, step_name_filter
        )

        # 报告缺失的 artifact paths (removed)
        for missing_path in missing:
            self.report.add_drift(
                self._make_drift_item(
                    category=DriftCategories.ARTIFACT_PATH,
                    drift_type=DriftTypes.REMOVED,
                    workflow=workflow_key,
                    key=missing_path,
                    contract_value=missing_path,
                    location="artifact_archive.required_artifact_paths",
                )
            )

        # 检查实际上传但 contract 中未要求的路径 (added)
        # 标准化 required_paths 用于比较
        required_set_normalized: set[str] = set()
        for rp in required_paths:
            required_set_normalized.add(self._normalize_path_for_comparison(rp))

        covered_normalized: set[str] = set()
        for cp in covered:
            covered_normalized.add(self._normalize_path_for_comparison(cp))

        for actual_path in actual_paths:
            # 只报告完全不在 required_paths 中的路径
            if (
                actual_path not in required_set_normalized
                and actual_path not in covered_normalized
            ):
                # 使用原始路径作为 key
                original_path = actual_paths_original.get(actual_path, actual_path)
                self.report.add_drift(
                    self._make_drift_item(
                        category=DriftCategories.ARTIFACT_PATH,
                        drift_type=DriftTypes.ADDED,
                        workflow=workflow_key,
                        key=original_path,
                        actual_value=original_path,
                        location="upload-artifact.with.path",
                    )
                )

    def analyze_make_targets(
        self,
        workflow_key: str,
        make_contract: dict[str, Any],
        workflow_data: dict[str, Any],
        workflow_file: str,
    ) -> None:
        """分析 make_target 的 drift

        比较 contract 中定义的 make.targets_required 与实际 workflow 中
        调用的 make targets。

        Args:
            workflow_key: workflow 名称
            make_contract: make 合约定义
            workflow_data: 解析后的 workflow 数据
            workflow_file: workflow 文件路径
        """
        targets_required = set(make_contract.get("targets_required", []))
        if not targets_required:
            return

        # 提取 workflow 中的 make 调用
        workflow_path = self.workspace_root / workflow_file
        make_calls = extract_workflow_make_calls(workflow_path)

        # 收集实际调用的 targets
        actual_targets: set[str] = set()
        for call in make_calls:
            actual_targets.add(call.target)

        # 检查 contract 中要求但 workflow 未调用的 targets (removed)
        for target in targets_required - actual_targets:
            self.report.add_drift(
                self._make_drift_item(
                    category=DriftCategories.MAKE_TARGET,
                    drift_type=DriftTypes.REMOVED,
                    workflow=workflow_key,
                    key=target,
                    contract_value=target,
                    location="make.targets_required",
                )
            )

        # 检查 workflow 中调用但 contract 未要求的 targets (added)
        for target in actual_targets - targets_required:
            self.report.add_drift(
                self._make_drift_item(
                    category=DriftCategories.MAKE_TARGET,
                    drift_type=DriftTypes.ADDED,
                    workflow=workflow_key,
                    key=target,
                    actual_value=target,
                    location=f"jobs.*.steps.run (make {target})",
                )
            )

    def analyze_labels(
        self,
        workflow_key: str,
        contract_labels: list[str],
    ) -> None:
        """分析 label 的 drift

        比较 contract 中定义的 ci.labels 与 gh_pr_labels_to_outputs.py 中的
        LABEL_* 常量。

        Args:
            workflow_key: workflow 名称
            contract_labels: contract 中定义的 labels
        """
        if not contract_labels:
            return

        # 解析脚本中的 LABEL_* 常量
        script_path = self.workspace_root / "scripts" / "ci" / "gh_pr_labels_to_outputs.py"
        script_labels = self._parse_label_constants_from_script(script_path)

        if script_labels is None:
            return

        contract_labels_set = set(contract_labels)

        # 检查 contract 中有但脚本中没有的 labels (removed from script)
        for label in contract_labels_set - script_labels:
            self.report.add_drift(
                self._make_drift_item(
                    category=DriftCategories.LABEL,
                    drift_type=DriftTypes.REMOVED,
                    workflow=workflow_key,
                    key=label,
                    contract_value=label,
                    location="gh_pr_labels_to_outputs.py LABEL_*",
                )
            )

        # 检查脚本中有但 contract 中没有的 labels (added in script)
        for label in script_labels - contract_labels_set:
            self.report.add_drift(
                self._make_drift_item(
                    category=DriftCategories.LABEL,
                    drift_type=DriftTypes.ADDED,
                    workflow=workflow_key,
                    key=label,
                    actual_value=label,
                    location="gh_pr_labels_to_outputs.py LABEL_*",
                )
            )

    def _parse_label_constants_from_script(self, script_path: Path) -> set[str] | None:
        """
        解析 Python 脚本中的 LABEL_* 常量值。

        使用 AST 解析而非 import，避免执行脚本代码。

        Args:
            script_path: 脚本文件路径

        Returns:
            LABEL_* 常量值的集合，解析失败返回 None
        """
        import ast

        if not script_path.exists():
            return None

        try:
            with open(script_path, "r", encoding="utf-8") as f:
                source = f.read()

            tree = ast.parse(source)
            labels: set[str] = set()

            for node in ast.walk(tree):
                # 查找形如 LABEL_XXX = "value" 的赋值语句
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id.startswith("LABEL_"):
                            # 获取赋值的值
                            if isinstance(node.value, ast.Constant) and isinstance(
                                node.value.value, str
                            ):
                                labels.add(node.value.value)

            return labels if labels else None

        except (SyntaxError, OSError):
            return None

    def _find_fuzzy_match(self, target: str, candidates: list[str]) -> str | None:
        """模糊匹配 step name

        委托给 workflow_contract_common.find_fuzzy_match() 实现。
        """
        return find_fuzzy_match(target, candidates)

    def analyze_workflow(self, workflow_key: str, workflow_contract: dict[str, Any]) -> None:
        """分析单个 workflow 的 drift"""
        workflow_file = workflow_contract.get("file", "")
        if not workflow_file:
            return

        workflow_path = self.workspace_root / workflow_file
        workflow_data = self.load_workflow(workflow_path)

        if workflow_data is None:
            self.report.add_drift(
                self._make_drift_item(
                    category=DriftCategories.WORKFLOW,
                    drift_type=DriftTypes.REMOVED,
                    workflow=workflow_key,
                    key=workflow_file,
                    contract_value=workflow_file,
                    location=workflow_file,
                )
            )
            return

        self.report.workflows_checked.append(workflow_key)

        actual_jobs = workflow_data.get("jobs", {})
        contract_job_ids = workflow_contract.get("job_ids", [])
        contract_job_names = workflow_contract.get("job_names", [])
        required_jobs = workflow_contract.get("required_jobs", [])
        required_env_vars = workflow_contract.get("required_env_vars", [])

        # 分析各维度的 drift
        if contract_job_ids:
            self.analyze_job_ids(workflow_key, contract_job_ids, actual_jobs)

        if contract_job_ids and contract_job_names:
            self.analyze_job_names(workflow_key, contract_job_ids, contract_job_names, actual_jobs)

        if required_jobs:
            self.analyze_required_steps(workflow_key, required_jobs, actual_jobs)

        if required_env_vars:
            self.analyze_env_vars(workflow_key, required_env_vars, workflow_data)

        # 分析 artifact_path drift
        artifact_contract = workflow_contract.get("artifact_archive")
        if artifact_contract:
            self.analyze_artifact_paths(workflow_key, artifact_contract, workflow_data)

        # 分析 label drift (仅 ci workflow)
        contract_labels = workflow_contract.get("labels", [])
        if contract_labels:
            self.analyze_labels(workflow_key, contract_labels)

    def analyze(self) -> DriftReport:
        """执行完整分析"""
        if not self.load_contract():
            return self.report

        workflow_keys = discover_workflow_keys(self.contract)

        # 应用 workflow 过滤器
        if self.workflow_filter:
            workflow_keys = [k for k in workflow_keys if k == self.workflow_filter]

        for workflow_key in workflow_keys:
            workflow_contract = self.contract.get(workflow_key, {})
            self.analyze_workflow(workflow_key, workflow_contract)

        # 分析全局 make targets drift
        make_contract = self.contract.get("make", {})
        if make_contract:
            self.analyze_global_make_targets(make_contract, workflow_keys)

        return self.report

    def analyze_global_make_targets(
        self,
        make_contract: dict[str, Any],
        workflow_keys: list[str],
    ) -> None:
        """分析全局 make targets 的 drift

        比较 contract 中定义的 make.targets_required 与:
        1. Makefile 中实际定义的 targets
        2. 所有 workflow 中调用的 make targets

        Args:
            make_contract: make 合约定义
            workflow_keys: 要分析的 workflow keys
        """
        targets_required = set(make_contract.get("targets_required", []))
        if not targets_required:
            return

        # 检查 Makefile 中是否定义了 targets_required 中的 targets
        makefile_path = self.workspace_root / "Makefile"
        makefile_targets = parse_makefile_targets(makefile_path)

        for target in targets_required:
            if target not in makefile_targets:
                self.report.add_drift(
                    self._make_drift_item(
                        category=DriftCategories.MAKE_TARGET,
                        drift_type=DriftTypes.REMOVED,
                        workflow="(global)",
                        key=target,
                        contract_value=target,
                        location="Makefile",
                    )
                )

        # 收集所有 workflow 中调用的 make targets
        all_workflow_targets: set[str] = set()
        for workflow_key in workflow_keys:
            workflow_contract = self.contract.get(workflow_key, {})
            workflow_file = workflow_contract.get("file", "")
            if not workflow_file:
                continue

            workflow_path = self.workspace_root / workflow_file
            make_calls = extract_workflow_make_calls(workflow_path)
            for call in make_calls:
                all_workflow_targets.add(call.target)

        # 检查 workflow 中调用但 contract 未要求的 targets (added)
        for target in all_workflow_targets - targets_required:
            self.report.add_drift(
                self._make_drift_item(
                    category=DriftCategories.MAKE_TARGET,
                    drift_type=DriftTypes.ADDED,
                    workflow="(global)",
                    key=target,
                    actual_value=target,
                    location="workflows/*.yml",
                )
            )


# ============================================================================
# Output Formatters
# ============================================================================


def format_json_output(report: DriftReport) -> str:
    """格式化 JSON 输出

    输出格式遵循 DRIFT_REPORT_SCHEMA_VERSION 定义的 schema。

    字段稳定性保证：
    - 顶层字段按字母序排序
    - drift_items 按 (workflow, category, drift_type, key) 排序
    - drift_items 内部字段按字母序排序
    - summary keys 按字母序排序
    - workflows_checked 按字母序排序

    Returns:
        格式化的 JSON 字符串
    """
    # 对 drift_items 按 (workflow, category, drift_type, key) 排序
    sorted_items = sorted(
        report.drift_items,
        key=lambda item: (item.workflow, item.category, item.drift_type, item.key),
    )

    # 对 summary keys 排序
    sorted_summary = dict(sorted(report.summary.items()))

    # 对 workflows_checked 排序
    sorted_workflows = sorted(report.workflows_checked)

    # 构建输出（字段按字母序）
    output = {
        "contract_last_updated": report.contract_last_updated,
        "contract_version": report.contract_version,
        "drift_count": len(report.drift_items),
        "drift_items": [
            {
                "actual_value": item.actual_value,
                "category": item.category,
                "contract_value": item.contract_value,
                "drift_type": item.drift_type,
                "key": item.key,
                "location": item.location,
                "severity": item.severity,
                "workflow": item.workflow,
            }
            for item in sorted_items
        ],
        "has_drift": report.has_drift,
        "report_generated_at": report.report_generated_at,
        "schema_version": DRIFT_REPORT_SCHEMA_VERSION,
        "summary": sorted_summary,
        "workflows_checked": sorted_workflows,
    }
    return json.dumps(output, indent=2, ensure_ascii=False, sort_keys=False)


def format_markdown_output(report: DriftReport) -> str:
    """格式化 Markdown 输出"""
    lines: list[str] = []

    # Header
    lines.append("# Workflow Contract Drift Report")
    lines.append("")
    lines.append("## Overview")
    lines.append("")
    lines.append(f"- **Contract Version**: {report.contract_version}")
    lines.append(f"- **Contract Last Updated**: {report.contract_last_updated}")
    lines.append(f"- **Report Generated**: {report.report_generated_at}")
    lines.append(f"- **Workflows Checked**: {', '.join(report.workflows_checked) or 'none'}")
    lines.append(f"- **Has Drift**: {'Yes' if report.has_drift else 'No'}")
    lines.append(f"- **Total Drift Items**: {len(report.drift_items)}")
    lines.append("")

    # Summary
    if report.summary:
        lines.append("## Summary")
        lines.append("")
        lines.append("| Category | Type | Count |")
        lines.append("|----------|------|-------|")
        for key, count in sorted(report.summary.items()):
            parts = key.rsplit("_", 1)
            category = parts[0] if len(parts) > 1 else key
            drift_type = parts[1] if len(parts) > 1 else "unknown"
            lines.append(f"| {category} | {drift_type} | {count} |")
        lines.append("")

    # Drift Items
    if report.drift_items:
        lines.append("## Drift Details")
        lines.append("")

        # Group by workflow
        by_workflow: dict[str, list[DriftItem]] = {}
        for item in report.drift_items:
            if item.workflow not in by_workflow:
                by_workflow[item.workflow] = []
            by_workflow[item.workflow].append(item)

        for workflow, items in sorted(by_workflow.items()):
            lines.append(f"### {workflow}")
            lines.append("")
            lines.append("| Type | Category | Key | Contract | Actual | Severity |")
            lines.append("|------|----------|-----|----------|--------|----------|")
            for item in items:
                contract_val = item.contract_value or "-"
                actual_val = item.actual_value or "-"
                # Truncate long values for readability
                if len(contract_val) > 30:
                    contract_val = contract_val[:27] + "..."
                if len(actual_val) > 30:
                    actual_val = actual_val[:27] + "..."
                lines.append(
                    f"| {item.drift_type} | {item.category} | {item.key} | "
                    f"{contract_val} | {actual_val} | {item.severity} |"
                )
            lines.append("")

        # 分类表格：artifact_path
        artifact_items = [i for i in report.drift_items if i.category == "artifact_path"]
        if artifact_items:
            lines.append("## Artifact Path Drift")
            lines.append("")
            lines.append("| Type | Workflow | Path | Location | Severity |")
            lines.append("|------|----------|------|----------|----------|")
            for item in artifact_items:
                path_val = item.contract_value or item.actual_value or "-"
                if len(path_val) > 40:
                    path_val = path_val[:37] + "..."
                lines.append(
                    f"| {item.drift_type} | {item.workflow} | {path_val} | "
                    f"{item.location or '-'} | {item.severity} |"
                )
            lines.append("")

        # 分类表格：make_target
        make_items = [i for i in report.drift_items if i.category == "make_target"]
        if make_items:
            lines.append("## Make Target Drift")
            lines.append("")
            lines.append("| Type | Workflow | Target | Location | Severity |")
            lines.append("|------|----------|--------|----------|----------|")
            for item in make_items:
                target_val = item.key
                lines.append(
                    f"| {item.drift_type} | {item.workflow} | {target_val} | "
                    f"{item.location or '-'} | {item.severity} |"
                )
            lines.append("")

        # 分类表格：label
        label_items = [i for i in report.drift_items if i.category == "label"]
        if label_items:
            lines.append("## Label Drift")
            lines.append("")
            lines.append("| Type | Workflow | Label | Location | Severity |")
            lines.append("|------|----------|-------|----------|----------|")
            for item in label_items:
                label_val = item.key
                lines.append(
                    f"| {item.drift_type} | {item.workflow} | {label_val} | "
                    f"{item.location or '-'} | {item.severity} |"
                )
            lines.append("")
    else:
        lines.append("## Result")
        lines.append("")
        lines.append("No drift detected. Contract and workflows are in sync.")
        lines.append("")

    return "\n".join(lines)


# ============================================================================
# Main
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="生成 Workflow Contract Drift Report",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--contract",
        type=str,
        default="scripts/ci/workflow_contract.v2.json",
        help="Contract JSON 文件路径 (default: scripts/ci/workflow_contract.v2.json)",
    )
    parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        help="Workspace 根目录（默认从脚本位置推断）",
    )
    parser.add_argument(
        "--workflow",
        "-w",
        type=str,
        default=None,
        help="只分析指定的 workflow（如: ci, nightly）",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=None,
        help="输出到指定文件（默认输出到 stdout）",
    )
    parser.add_argument(
        "--markdown",
        "-m",
        action="store_true",
        help="以 Markdown 格式输出（默认 JSON）",
    )

    args = parser.parse_args()

    # 确定 workspace 根目录
    if args.workspace:
        workspace_root = Path(args.workspace).resolve()
    else:
        script_path = Path(__file__).resolve()
        workspace_root = script_path.parent.parent.parent
        if not (workspace_root / "scripts" / "ci").exists():
            workspace_root = Path.cwd()

    contract_path = workspace_root / args.contract

    # 执行分析
    analyzer = WorkflowContractDriftAnalyzer(
        contract_path=contract_path,
        workspace_root=workspace_root,
        workflow_filter=args.workflow,
    )
    report = analyzer.analyze()

    # 格式化输出
    if args.markdown:
        output = format_markdown_output(report)
    else:
        output = format_json_output(report)

    # 输出
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(output)
            f.write("\n")
        print(f"Drift report 已保存到: {output_path}", file=sys.stderr)
    else:
        print(output)

    # 返回退出码
    if report.has_drift:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
