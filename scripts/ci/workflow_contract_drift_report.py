#!/usr/bin/env python3
"""
Workflow Contract Drift Report Generator

读取 workflow 快照与 workflow_contract.v1.json，比较差异，
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
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

# 复用 validate_workflows.py 中的 artifact path 和 make target 提取逻辑
from validate_workflows import (
    check_artifact_path_coverage,
    extract_upload_artifact_paths,
    extract_workflow_make_calls,
    parse_makefile_targets,
)

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
        self.report.report_generated_at = datetime.now().isoformat()

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

    def discover_workflow_keys(self) -> list[str]:
        """动态发现 contract 中的 workflow 定义 key"""
        metadata_keys = {
            "$schema",
            "version",
            "description",
            "last_updated",
            "make",
            "frozen_step_text",
            "frozen_job_names",
        }

        workflow_keys: list[str] = []
        for key, value in self.contract.items():
            if key.startswith("_"):
                continue
            if key in metadata_keys:
                continue
            if isinstance(value, dict) and "file" in value:
                workflow_keys.append(key)

        return sorted(workflow_keys)

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
                DriftItem(
                    drift_type="removed",
                    category="job_id",
                    workflow=workflow_key,
                    key=job_id,
                    contract_value=job_id,
                    actual_value=None,
                    location=f"jobs.{job_id}",
                    severity="error",
                )
            )

        # 检查实际存在但 contract 中没有的 job_ids（added）
        for job_id in actual_job_ids - contract_job_ids_set:
            self.report.add_drift(
                DriftItem(
                    drift_type="added",
                    category="job_id",
                    workflow=workflow_key,
                    key=job_id,
                    contract_value=None,
                    actual_value=job_id,
                    location=f"jobs.{job_id}",
                    severity="warning",
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
                    DriftItem(
                        drift_type="changed",
                        category="job_name",
                        workflow=workflow_key,
                        key=job_id,
                        contract_value=expected_name,
                        actual_value=actual_name,
                        location=f"jobs.{job_id}.name",
                        severity="warning",
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
                            DriftItem(
                                drift_type="changed",
                                category="step",
                                workflow=workflow_key,
                                key=f"{job_id}/{required_step}",
                                contract_value=required_step,
                                actual_value=fuzzy_match,
                                location=f"jobs.{job_id}.steps",
                                severity="warning",
                            )
                        )
                    else:
                        self.report.add_drift(
                            DriftItem(
                                drift_type="removed",
                                category="step",
                                workflow=workflow_key,
                                key=f"{job_id}/{required_step}",
                                contract_value=required_step,
                                actual_value=None,
                                location=f"jobs.{job_id}.steps",
                                severity="error",
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
                    DriftItem(
                        drift_type="removed",
                        category="env_var",
                        workflow=workflow_key,
                        key=required_var,
                        contract_value=required_var,
                        actual_value=None,
                        location="env",
                        severity="error",
                    )
                )

    def analyze_artifact_paths(
        self,
        workflow_key: str,
        artifact_contract: dict[str, Any],
        workflow_data: dict[str, Any],
    ) -> None:
        """分析 artifact_path 的 drift

        比较 contract 中定义的 required_artifact_paths 与实际 workflow 中
        upload-artifact 步骤上传的路径。

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

        # 收集实际上传的路径
        actual_paths: set[str] = set()
        for step in upload_steps:
            # 如果有 step name 过滤器，只检查匹配的 step
            if step_name_filter:
                step_name = step.get("step_name", "")
                if not any(
                    filter_name.lower() in step_name.lower() for filter_name in step_name_filter
                ):
                    continue
            for path in step.get("paths", []):
                actual_paths.add(path)

        # 检查覆盖情况
        covered, missing = check_artifact_path_coverage(
            upload_steps, required_paths, step_name_filter
        )

        # 报告缺失的 artifact paths (removed)
        for missing_path in missing:
            self.report.add_drift(
                DriftItem(
                    drift_type="removed",
                    category="artifact_path",
                    workflow=workflow_key,
                    key=missing_path,
                    contract_value=missing_path,
                    actual_value=None,
                    location="artifact_archive.required_artifact_paths",
                    severity="error",
                )
            )

        # 检查实际上传但 contract 中未要求的路径 (added)
        required_set = set(required_paths)
        for actual_path in actual_paths:
            # 只报告完全不在 required_paths 中的路径
            if actual_path not in required_set and actual_path not in covered:
                self.report.add_drift(
                    DriftItem(
                        drift_type="added",
                        category="artifact_path",
                        workflow=workflow_key,
                        key=actual_path,
                        contract_value=None,
                        actual_value=actual_path,
                        location="upload-artifact.with.path",
                        severity="info",
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
            actual_targets.add(call["target"])

        # 检查 contract 中要求但 workflow 未调用的 targets (removed)
        for target in targets_required - actual_targets:
            self.report.add_drift(
                DriftItem(
                    drift_type="removed",
                    category="make_target",
                    workflow=workflow_key,
                    key=target,
                    contract_value=target,
                    actual_value=None,
                    location="make.targets_required",
                    severity="warning",
                )
            )

        # 检查 workflow 中调用但 contract 未要求的 targets (added)
        for target in actual_targets - targets_required:
            self.report.add_drift(
                DriftItem(
                    drift_type="added",
                    category="make_target",
                    workflow=workflow_key,
                    key=target,
                    contract_value=None,
                    actual_value=target,
                    location=f"jobs.*.steps.run (make {target})",
                    severity="warning",
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
                DriftItem(
                    drift_type="removed",
                    category="label",
                    workflow=workflow_key,
                    key=label,
                    contract_value=label,
                    actual_value=None,
                    location="gh_pr_labels_to_outputs.py LABEL_*",
                    severity="error",
                )
            )

        # 检查脚本中有但 contract 中没有的 labels (added in script)
        for label in script_labels - contract_labels_set:
            self.report.add_drift(
                DriftItem(
                    drift_type="added",
                    category="label",
                    workflow=workflow_key,
                    key=label,
                    contract_value=None,
                    actual_value=label,
                    location="gh_pr_labels_to_outputs.py LABEL_*",
                    severity="warning",
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
        """模糊匹配 step name"""
        target_lower = target.lower()

        # 包含匹配
        for candidate in candidates:
            if target_lower in candidate.lower() or candidate.lower() in target_lower:
                return candidate

        # 词语匹配
        target_words = set(target_lower.split())
        for candidate in candidates:
            candidate_words = set(candidate.lower().split())
            overlap = len(target_words & candidate_words)
            if overlap >= len(target_words) * 0.5:
                return candidate

        return None

    def analyze_workflow(self, workflow_key: str, workflow_contract: dict[str, Any]) -> None:
        """分析单个 workflow 的 drift"""
        workflow_file = workflow_contract.get("file", "")
        if not workflow_file:
            return

        workflow_path = self.workspace_root / workflow_file
        workflow_data = self.load_workflow(workflow_path)

        if workflow_data is None:
            self.report.add_drift(
                DriftItem(
                    drift_type="removed",
                    category="workflow",
                    workflow=workflow_key,
                    key=workflow_file,
                    contract_value=workflow_file,
                    actual_value=None,
                    location=workflow_file,
                    severity="error",
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

        workflow_keys = self.discover_workflow_keys()

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
                    DriftItem(
                        drift_type="removed",
                        category="make_target",
                        workflow="(global)",
                        key=target,
                        contract_value=target,
                        actual_value=None,
                        location="Makefile",
                        severity="error",
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
                all_workflow_targets.add(call["target"])

        # 检查 workflow 中调用但 contract 未要求的 targets (added)
        for target in all_workflow_targets - targets_required:
            self.report.add_drift(
                DriftItem(
                    drift_type="added",
                    category="make_target",
                    workflow="(global)",
                    key=target,
                    contract_value=None,
                    actual_value=target,
                    location="workflows/*.yml",
                    severity="warning",
                )
            )


# ============================================================================
# Output Formatters
# ============================================================================


def format_json_output(report: DriftReport) -> str:
    """格式化 JSON 输出"""
    output = {
        "has_drift": report.has_drift,
        "contract_version": report.contract_version,
        "contract_last_updated": report.contract_last_updated,
        "report_generated_at": report.report_generated_at,
        "workflows_checked": report.workflows_checked,
        "summary": report.summary,
        "drift_count": len(report.drift_items),
        "drift_items": [
            {
                "drift_type": item.drift_type,
                "category": item.category,
                "workflow": item.workflow,
                "key": item.key,
                "contract_value": item.contract_value,
                "actual_value": item.actual_value,
                "location": item.location,
                "severity": item.severity,
            }
            for item in report.drift_items
        ],
    }
    return json.dumps(output, indent=2, ensure_ascii=False)


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
        default="scripts/ci/workflow_contract.v1.json",
        help="Contract JSON 文件路径 (default: scripts/ci/workflow_contract.v1.json)",
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
