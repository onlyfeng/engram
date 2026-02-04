#!/usr/bin/env python3
"""
Suggest Workflow Contract Updates

读取 workflow_contract.v2.json 与当前 workflow YAML，对比并建议需要更新的内容。

功能:
- 缺失的 job_id（workflow 中有但 contract 未声明）
- job_name 不匹配
- required_steps 缺失项（workflow 中存在但 contract 未记录的 step）
- extra jobs（contract 中声明但 workflow 中没有的 job）
- 可能需要更新的 frozen allowlist（仅提示）

输出格式:
- JSON（机器可读）: --json 或 --output xxx.json
- Markdown（人类可读）: --markdown 或 --output xxx.md
  - 输出包含 summary counts，便于粘贴到 PR 描述

用法:
    # 输出 JSON 到 stdout
    python scripts/ci/suggest_workflow_contract_updates.py --json

    # 输出 Markdown 到 stdout
    python scripts/ci/suggest_workflow_contract_updates.py --markdown

    # 输出到文件（根据扩展名自动选择格式）
    python scripts/ci/suggest_workflow_contract_updates.py --output suggestions.json
    python scripts/ci/suggest_workflow_contract_updates.py --output suggestions.md

    # 输出到 artifacts（便于 PR 评审/上传）
    python scripts/ci/suggest_workflow_contract_updates.py --json --output artifacts/workflow_contract_suggestions.json
    python scripts/ci/suggest_workflow_contract_updates.py --markdown --output artifacts/workflow_contract_suggestions.md

    # 只分析特定 workflow
    python scripts/ci/suggest_workflow_contract_updates.py --workflow ci --json

    # 应用建议的更新（修改 contract 文件）
    python scripts/ci/suggest_workflow_contract_updates.py --apply

    # 只应用特定范围的更新
    python scripts/ci/suggest_workflow_contract_updates.py --apply --apply-scope jobs
    python scripts/ci/suggest_workflow_contract_updates.py --apply --apply-scope steps
    python scripts/ci/suggest_workflow_contract_updates.py --apply --apply-scope jobs,steps

    # 预览将要应用的更改（不实际修改文件）
    python scripts/ci/suggest_workflow_contract_updates.py --apply --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts.ci.workflow_contract_common import (
    discover_workflow_keys,
    find_fuzzy_match,
    is_string_similar,
)

# ============================================================================
# Constants
# ============================================================================

# 建议类型
SUGGESTION_TYPE_MISSING_JOB_ID = "missing_job_id"
SUGGESTION_TYPE_JOB_NAME_MISMATCH = "job_name_mismatch"
SUGGESTION_TYPE_MISSING_STEP = "missing_step"
SUGGESTION_TYPE_EXTRA_JOB = "extra_job"
SUGGESTION_TYPE_FROZEN_ALLOWLIST_UPDATE = "frozen_allowlist_update"
SUGGESTION_TYPE_NEW_STEP_IN_WORKFLOW = "new_step_in_workflow"

# 建议优先级
PRIORITY_HIGH = "high"
PRIORITY_MEDIUM = "medium"
PRIORITY_LOW = "low"
PRIORITY_INFO = "info"

# Apply Scope 选项
APPLY_SCOPE_JOBS = "jobs"
APPLY_SCOPE_STEPS = "steps"
APPLY_SCOPE_ARTIFACTS = "artifacts"
APPLY_SCOPE_LABELS = "labels"
APPLY_SCOPE_FROZEN_ALLOWLIST = "frozen_allowlist"

VALID_APPLY_SCOPES = frozenset(
    [
        APPLY_SCOPE_JOBS,
        APPLY_SCOPE_STEPS,
        APPLY_SCOPE_ARTIFACTS,
        APPLY_SCOPE_LABELS,
        APPLY_SCOPE_FROZEN_ALLOWLIST,
    ]
)


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class Suggestion:
    """表示一条更新建议"""

    suggestion_type: str
    workflow: str
    key: str
    message: str
    priority: str = PRIORITY_MEDIUM
    contract_value: str | None = None
    actual_value: str | None = None
    location: str | None = None
    action: str | None = None


@dataclass
class SuggestionReport:
    """建议报告"""

    suggestions: list[Suggestion] = field(default_factory=list)
    contract_version: str = ""
    contract_last_updated: str = ""
    report_generated_at: str = ""
    workflows_checked: list[str] = field(default_factory=list)

    @property
    def has_suggestions(self) -> bool:
        return len(self.suggestions) > 0

    @property
    def summary(self) -> dict[str, int]:
        """按类型统计建议数量"""
        counts: dict[str, int] = {}
        for s in self.suggestions:
            key = s.suggestion_type
            counts[key] = counts.get(key, 0) + 1
        return counts

    def add_suggestion(self, suggestion: Suggestion) -> None:
        self.suggestions.append(suggestion)


@dataclass
class ApplyResult:
    """应用建议的结果"""

    applied_count: int = 0
    skipped_count: int = 0
    applied_suggestions: list[Suggestion] = field(default_factory=list)
    skipped_suggestions: list[Suggestion] = field(default_factory=list)
    contract_before: str = ""
    contract_after: str = ""
    diff: str = ""

    @property
    def has_changes(self) -> bool:
        return self.applied_count > 0


# ============================================================================
# YAML Loader
# ============================================================================


def load_yaml():
    """尝试加载 yaml 模块，提供友好的错误提示。"""
    try:
        import yaml

        return yaml
    except ImportError:
        print("错误: 需要安装 pyyaml 模块", file=sys.stderr)
        print("  pip install pyyaml", file=sys.stderr)
        sys.exit(1)


# ============================================================================
# Path Utilities
# ============================================================================


def find_project_root() -> Path:
    """查找项目根目录（包含 .github 的目录）"""
    script_dir = Path(__file__).resolve().parent

    # 从 scripts/ci 向上两级找到项目根目录
    for parent in [script_dir.parent.parent, Path.cwd()]:
        if (parent / ".github" / "workflows").is_dir():
            return parent

    raise FileNotFoundError(
        "无法找到项目根目录。请在项目根目录运行此脚本，或确保 .github/workflows 存在。"
    )


def find_contract_path(project_root: Path) -> Path:
    """查找合约文件路径"""
    contract_path = project_root / "scripts" / "ci" / "workflow_contract.v2.json"
    if contract_path.exists():
        return contract_path
    raise FileNotFoundError(f"找不到合约文件: {contract_path}")


# ============================================================================
# Analyzer
# ============================================================================


class WorkflowContractSuggestionAnalyzer:
    """分析 workflow 与 contract 的差异，生成更新建议"""

    def __init__(
        self,
        contract_path: Path,
        workspace_root: Path,
        workflow_filter: str | None = None,
    ):
        self.contract_path = contract_path
        self.workspace_root = workspace_root
        self.workflow_filter = workflow_filter
        self.yaml = load_yaml()
        self.contract: dict[str, Any] = {}
        self.workflows: dict[str, dict[str, Any]] = {}

    def load_contract(self) -> None:
        """加载合约文件"""
        with open(self.contract_path, encoding="utf-8") as f:
            self.contract = json.load(f)

    def load_workflow(self, workflow_key: str, workflow_file: str) -> dict[str, Any] | None:
        """加载单个 workflow 文件"""
        workflow_path = self.workspace_root / workflow_file
        if not workflow_path.exists():
            return None
        with open(workflow_path, encoding="utf-8") as f:
            return self.yaml.safe_load(f)

    def analyze(self) -> SuggestionReport:
        """执行分析并生成建议报告"""
        report = SuggestionReport()
        report.report_generated_at = datetime.now().isoformat()

        # 加载合约
        self.load_contract()
        report.contract_version = self.contract.get("version", "unknown")
        report.contract_last_updated = self.contract.get("last_updated", "unknown")

        # 确定要分析的 workflow
        workflow_keys = self._get_workflow_keys()

        for wf_key in workflow_keys:
            wf_config = self.contract.get(wf_key)
            if not wf_config or not isinstance(wf_config, dict):
                continue

            wf_file = wf_config.get("file", "")
            if not wf_file:
                continue

            workflow_data = self.load_workflow(wf_key, wf_file)
            if workflow_data is None:
                continue

            report.workflows_checked.append(wf_key)
            self.workflows[wf_key] = workflow_data

            # 分析各项差异
            self._analyze_job_ids(report, wf_key, wf_config, workflow_data)
            self._analyze_job_names(report, wf_key, wf_config, workflow_data)
            self._analyze_required_steps(report, wf_key, wf_config, workflow_data)
            self._analyze_frozen_allowlist(report, wf_key, wf_config, workflow_data)

        return report

    def _get_workflow_keys(self) -> list[str]:
        """获取要分析的 workflow key 列表

        使用 discover_workflow_keys() 动态发现 contract 中的 workflow 定义，
        自动排除 METADATA_KEYS 和下划线前缀字段。

        ============================================================================
        Phase 2 扩展点：纳入 release.yml
        ============================================================================

        本脚本使用 discover_workflow_keys() 动态发现 workflow 定义，无需硬编码。
        当 release.yml 纳入合约时，只需在 workflow_contract.v2.json 中添加 release
        字段定义即可自动被本脚本发现和分析。

        纳入 release.yml 时的同步 Checklist（本脚本无需代码修改）：

        1. [workflow_contract.v2.json] 添加 release 字段：
           - file: ".github/workflows/release.yml"
           - job_ids: release workflow 的所有 job ID
           - job_names: 与 job_ids 位置对应的 job name
           - required_jobs: 核心 job 的 required_steps 定义
           - artifact_archive: release 产物路径（如 dist/*.whl）

        2. [本脚本] 无需修改 - 自动发现 release workflow

        3. [输出验证] 运行以下命令确认 release 被正确发现：
           python scripts/ci/suggest_workflow_contract_updates.py --json | jq '.workflows_checked'
           # 预期输出应包含 "release"

        详见 contract.md 2.4.3 节迁移 Checklist
        ============================================================================
        """
        # 动态发现 workflow keys（自动排除 metadata 字段）
        discovered_keys = discover_workflow_keys(self.contract)

        if self.workflow_filter:
            return [self.workflow_filter] if self.workflow_filter in discovered_keys else []

        return discovered_keys

    def _analyze_job_ids(
        self,
        report: SuggestionReport,
        wf_key: str,
        wf_config: dict[str, Any],
        workflow_data: dict[str, Any],
    ) -> None:
        """分析 job_ids 差异"""
        contract_job_ids = set(wf_config.get("job_ids", []))
        actual_jobs = workflow_data.get("jobs", {})
        actual_job_ids = set(actual_jobs.keys())

        # 缺失的 job_id（workflow 中有但 contract 未声明）
        missing_in_contract = actual_job_ids - contract_job_ids
        for job_id in sorted(missing_in_contract):
            job_data = actual_jobs.get(job_id, {})
            job_name = job_data.get("name", job_id)
            report.add_suggestion(
                Suggestion(
                    suggestion_type=SUGGESTION_TYPE_MISSING_JOB_ID,
                    workflow=wf_key,
                    key=job_id,
                    message=f"Workflow 中存在 job '{job_id}'，但 contract 的 job_ids 中未声明",
                    priority=PRIORITY_HIGH,
                    actual_value=job_name,
                    location=f"jobs.{job_id}",
                    action=f'将 "{job_id}" 添加到 {wf_key}.job_ids 数组中',
                )
            )

        # extra jobs（contract 中声明但 workflow 中没有）
        extra_in_contract = contract_job_ids - actual_job_ids
        for job_id in sorted(extra_in_contract):
            report.add_suggestion(
                Suggestion(
                    suggestion_type=SUGGESTION_TYPE_EXTRA_JOB,
                    workflow=wf_key,
                    key=job_id,
                    message=f"Contract 中声明了 job '{job_id}'，但 workflow 中不存在",
                    priority=PRIORITY_HIGH,
                    contract_value=job_id,
                    location=f"{wf_key}.job_ids",
                    action=f'从 {wf_key}.job_ids 中移除 "{job_id}"',
                )
            )

    def _analyze_job_names(
        self,
        report: SuggestionReport,
        wf_key: str,
        wf_config: dict[str, Any],
        workflow_data: dict[str, Any],
    ) -> None:
        """分析 job_names 差异"""
        contract_job_ids = wf_config.get("job_ids", [])
        contract_job_names = wf_config.get("job_names", [])
        actual_jobs = workflow_data.get("jobs", {})

        # 按位置对应检查 job_id -> job_name
        for i, job_id in enumerate(contract_job_ids):
            if job_id not in actual_jobs:
                continue

            actual_name = actual_jobs[job_id].get("name", job_id)
            expected_name = contract_job_names[i] if i < len(contract_job_names) else None

            if expected_name and expected_name != actual_name:
                report.add_suggestion(
                    Suggestion(
                        suggestion_type=SUGGESTION_TYPE_JOB_NAME_MISMATCH,
                        workflow=wf_key,
                        key=job_id,
                        message=f"Job '{job_id}' 的 name 不匹配",
                        priority=PRIORITY_MEDIUM,
                        contract_value=expected_name,
                        actual_value=actual_name,
                        location=f"jobs.{job_id}.name",
                        action=f'将 {wf_key}.job_names[{i}] 更新为 "{actual_name}"',
                    )
                )

        # 检查是否有新的 job 需要添加对应的 job_name
        actual_job_ids = list(actual_jobs.keys())
        for job_id in actual_job_ids:
            if job_id not in contract_job_ids:
                actual_name = actual_jobs[job_id].get("name", job_id)
                report.add_suggestion(
                    Suggestion(
                        suggestion_type=SUGGESTION_TYPE_JOB_NAME_MISMATCH,
                        workflow=wf_key,
                        key=job_id,
                        message=f"新 job '{job_id}' 需要在 job_names 中添加对应的 name",
                        priority=PRIORITY_MEDIUM,
                        actual_value=actual_name,
                        location=f"jobs.{job_id}.name",
                        action=f'将 "{actual_name}" 添加到 {wf_key}.job_names（与 job_ids 中 "{job_id}" 位置对应）',
                    )
                )

    def _analyze_required_steps(
        self,
        report: SuggestionReport,
        wf_key: str,
        wf_config: dict[str, Any],
        workflow_data: dict[str, Any],
    ) -> None:
        """分析 required_steps 差异"""
        required_jobs = wf_config.get("required_jobs", [])
        actual_jobs = workflow_data.get("jobs", {})

        # 创建 required_jobs 的 id -> config 映射
        required_jobs_map = {rj["id"]: rj for rj in required_jobs if "id" in rj}

        for job_id, job_data in actual_jobs.items():
            actual_steps = job_data.get("steps", [])
            actual_step_names = [s.get("name", "") for s in actual_steps if s.get("name")]

            if job_id in required_jobs_map:
                # Job 在 required_jobs 中，检查 required_steps
                required_steps = required_jobs_map[job_id].get("required_steps", [])
                required_steps_set = set(required_steps)
                actual_steps_set = set(actual_step_names)

                # Workflow 中有但 contract 未记录的 step
                new_steps = actual_steps_set - required_steps_set
                for step_name in sorted(new_steps):
                    report.add_suggestion(
                        Suggestion(
                            suggestion_type=SUGGESTION_TYPE_NEW_STEP_IN_WORKFLOW,
                            workflow=wf_key,
                            key=f"{job_id}/{step_name}",
                            message=f"Workflow job '{job_id}' 中存在 step '{step_name}'，但 required_steps 中未记录",
                            priority=PRIORITY_LOW,
                            actual_value=step_name,
                            location=f"jobs.{job_id}.steps",
                            action=f'将 "{step_name}" 添加到 {wf_key}.required_jobs[id={job_id}].required_steps',
                        )
                    )

                # Contract 中声明但 workflow 中不存在的 step
                missing_steps = required_steps_set - actual_steps_set
                for step_name in sorted(missing_steps):
                    # 尝试模糊匹配
                    fuzzy_match = self._find_fuzzy_match(step_name, actual_step_names)
                    if fuzzy_match:
                        report.add_suggestion(
                            Suggestion(
                                suggestion_type=SUGGESTION_TYPE_MISSING_STEP,
                                workflow=wf_key,
                                key=f"{job_id}/{step_name}",
                                message=f"Step '{step_name}' 可能被重命名为 '{fuzzy_match}'",
                                priority=PRIORITY_MEDIUM,
                                contract_value=step_name,
                                actual_value=fuzzy_match,
                                location=f"jobs.{job_id}.steps",
                                action=f'将 required_steps 中的 "{step_name}" 更新为 "{fuzzy_match}"',
                            )
                        )
                    else:
                        report.add_suggestion(
                            Suggestion(
                                suggestion_type=SUGGESTION_TYPE_MISSING_STEP,
                                workflow=wf_key,
                                key=f"{job_id}/{step_name}",
                                message=f"Contract 中声明的 step '{step_name}' 在 workflow 中不存在",
                                priority=PRIORITY_HIGH,
                                contract_value=step_name,
                                location=f"jobs.{job_id}.steps",
                                action=f'从 required_steps 中移除 "{step_name}"，或确认 workflow 是否需要此步骤',
                            )
                        )
            else:
                # Job 不在 required_jobs 中，建议添加
                if actual_step_names:
                    report.add_suggestion(
                        Suggestion(
                            suggestion_type=SUGGESTION_TYPE_MISSING_STEP,
                            workflow=wf_key,
                            key=job_id,
                            message=f"Job '{job_id}' 有 {len(actual_step_names)} 个 steps，但未在 required_jobs 中定义",
                            priority=PRIORITY_LOW,
                            actual_value=", ".join(actual_step_names[:3])
                            + ("..." if len(actual_step_names) > 3 else ""),
                            location=f"{wf_key}.required_jobs",
                            action=f"考虑将 job '{job_id}' 添加到 required_jobs 以进行 step 合约校验",
                        )
                    )

    def _analyze_frozen_allowlist(
        self,
        report: SuggestionReport,
        wf_key: str,
        wf_config: dict[str, Any],
        workflow_data: dict[str, Any],
    ) -> None:
        """分析是否需要更新 frozen allowlist"""
        frozen_job_names = set(self.contract.get("frozen_job_names", {}).get("allowlist", []))
        frozen_step_text = set(self.contract.get("frozen_step_text", {}).get("allowlist", []))

        actual_jobs = workflow_data.get("jobs", {})

        # 检查实际的 job names 是否在 frozen_job_names 中有对应
        for job_id, job_data in actual_jobs.items():
            job_name = job_data.get("name", job_id)

            # 检查 job_name 是否与 frozen 列表中的某项相似但不完全匹配
            for frozen_name in frozen_job_names:
                if self._is_similar(job_name, frozen_name) and job_name != frozen_name:
                    report.add_suggestion(
                        Suggestion(
                            suggestion_type=SUGGESTION_TYPE_FROZEN_ALLOWLIST_UPDATE,
                            workflow=wf_key,
                            key=f"frozen_job_names/{job_id}",
                            message=f"Job name '{job_name}' 与 frozen_job_names 中的 '{frozen_name}' 相似但不完全匹配",
                            priority=PRIORITY_INFO,
                            contract_value=frozen_name,
                            actual_value=job_name,
                            location="frozen_job_names.allowlist",
                            action=f"如需冻结此 job name，请将 frozen_job_names 中的 '{frozen_name}' 更新为 '{job_name}'",
                        )
                    )
                    break

            # 检查 steps
            steps = job_data.get("steps", [])
            for step in steps:
                step_name = step.get("name", "")
                if not step_name:
                    continue

                for frozen_step in frozen_step_text:
                    if self._is_similar(step_name, frozen_step) and step_name != frozen_step:
                        report.add_suggestion(
                            Suggestion(
                                suggestion_type=SUGGESTION_TYPE_FROZEN_ALLOWLIST_UPDATE,
                                workflow=wf_key,
                                key=f"frozen_step_text/{job_id}/{step_name}",
                                message=f"Step name '{step_name}' 与 frozen_step_text 中的 '{frozen_step}' 相似但不完全匹配",
                                priority=PRIORITY_INFO,
                                contract_value=frozen_step,
                                actual_value=step_name,
                                location="frozen_step_text.allowlist",
                                action=f"如需冻结此 step name，请将 frozen_step_text 中的 '{frozen_step}' 更新为 '{step_name}'",
                            )
                        )
                        break

    def _find_fuzzy_match(self, target: str, candidates: list[str]) -> str | None:
        """尝试在 candidates 中找到与 target 模糊匹配的项

        委托给 workflow_contract_common.find_fuzzy_match() 实现。
        """
        return find_fuzzy_match(target, candidates)

    def _is_similar(self, s1: str, s2: str) -> bool:
        """判断两个字符串是否相似（用于 frozen allowlist 提示）

        委托给 workflow_contract_common.is_string_similar() 实现。
        """
        return is_string_similar(s1, s2)


# ============================================================================
# Contract Applier
# ============================================================================


class ContractApplier:
    """应用建议的更新到 contract 文件

    设计原则:
    1. 保持 JSON 键顺序稳定（使用原始顺序）
    2. 保持下划线前缀字段（如 _changelog_*, _comment）的稳定
    3. 只应用确定性的更新（不处理需要人工判断的建议）
    4. 支持按 scope 过滤应用范围
    """

    def __init__(
        self,
        contract_path: Path,
        report: SuggestionReport,
        scopes: set[str] | None = None,
    ):
        self.contract_path = contract_path
        self.report = report
        self.scopes = scopes or VALID_APPLY_SCOPES
        self.contract: dict[str, Any] = {}
        self.contract_before: str = ""

    def load_contract(self) -> None:
        """加载合约文件（保留原始内容用于 diff）"""
        with open(self.contract_path, encoding="utf-8") as f:
            self.contract_before = f.read()
            f.seek(0)
            self.contract = json.load(f)

    def apply(self) -> ApplyResult:
        """应用建议并返回结果"""
        result = ApplyResult()
        result.contract_before = self.contract_before

        self.load_contract()

        for suggestion in self.report.suggestions:
            if self._should_apply(suggestion):
                if self._apply_suggestion(suggestion):
                    result.applied_count += 1
                    result.applied_suggestions.append(suggestion)
                else:
                    result.skipped_count += 1
                    result.skipped_suggestions.append(suggestion)
            else:
                result.skipped_count += 1
                result.skipped_suggestions.append(suggestion)

        # 生成修改后的内容
        result.contract_after = json.dumps(self.contract, indent=2, ensure_ascii=False)

        # 生成 diff
        result.diff = self._generate_diff(result.contract_before, result.contract_after)

        return result

    def save(self, result: ApplyResult) -> None:
        """保存修改后的 contract 文件"""
        with open(self.contract_path, "w", encoding="utf-8") as f:
            f.write(result.contract_after)
            f.write("\n")

    def _should_apply(self, suggestion: Suggestion) -> bool:
        """判断是否应该应用此建议"""
        # 只应用 HIGH 和 MEDIUM 优先级的建议
        if suggestion.priority not in (PRIORITY_HIGH, PRIORITY_MEDIUM):
            return False

        # 根据 scope 过滤
        suggestion_scope = self._get_suggestion_scope(suggestion)
        return suggestion_scope in self.scopes

    def _get_suggestion_scope(self, suggestion: Suggestion) -> str:
        """获取建议所属的 scope"""
        if suggestion.suggestion_type in (
            SUGGESTION_TYPE_MISSING_JOB_ID,
            SUGGESTION_TYPE_EXTRA_JOB,
            SUGGESTION_TYPE_JOB_NAME_MISMATCH,
        ):
            return APPLY_SCOPE_JOBS
        elif suggestion.suggestion_type in (
            SUGGESTION_TYPE_MISSING_STEP,
            SUGGESTION_TYPE_NEW_STEP_IN_WORKFLOW,
        ):
            return APPLY_SCOPE_STEPS
        elif suggestion.suggestion_type == SUGGESTION_TYPE_FROZEN_ALLOWLIST_UPDATE:
            return APPLY_SCOPE_FROZEN_ALLOWLIST
        else:
            return ""

    def _apply_suggestion(self, suggestion: Suggestion) -> bool:
        """应用单个建议，返回是否成功"""
        try:
            if suggestion.suggestion_type == SUGGESTION_TYPE_MISSING_JOB_ID:
                return self._apply_missing_job_id(suggestion)
            elif suggestion.suggestion_type == SUGGESTION_TYPE_EXTRA_JOB:
                return self._apply_extra_job(suggestion)
            elif suggestion.suggestion_type == SUGGESTION_TYPE_JOB_NAME_MISMATCH:
                return self._apply_job_name_mismatch(suggestion)
            elif suggestion.suggestion_type == SUGGESTION_TYPE_MISSING_STEP:
                return self._apply_missing_step(suggestion)
            elif suggestion.suggestion_type == SUGGESTION_TYPE_NEW_STEP_IN_WORKFLOW:
                return self._apply_new_step(suggestion)
            else:
                return False
        except Exception:
            return False

    def _apply_missing_job_id(self, suggestion: Suggestion) -> bool:
        """应用: 添加缺失的 job_id"""
        wf_key = suggestion.workflow
        job_id = suggestion.key
        job_name = suggestion.actual_value or job_id

        if wf_key not in self.contract:
            return False

        wf_config = self.contract[wf_key]

        # 添加 job_id
        job_ids = wf_config.get("job_ids", [])
        if job_id not in job_ids:
            job_ids.append(job_id)
            wf_config["job_ids"] = job_ids

        # 添加 job_name
        job_names = wf_config.get("job_names", [])
        job_names.append(job_name)
        wf_config["job_names"] = job_names

        return True

    def _apply_extra_job(self, suggestion: Suggestion) -> bool:
        """应用: 移除 contract 中多余的 job"""
        wf_key = suggestion.workflow
        job_id = suggestion.key

        if wf_key not in self.contract:
            return False

        wf_config = self.contract[wf_key]

        # 获取 job 在 job_ids 中的索引
        job_ids = wf_config.get("job_ids", [])
        if job_id not in job_ids:
            return False

        idx = job_ids.index(job_id)

        # 移除 job_id
        job_ids.remove(job_id)
        wf_config["job_ids"] = job_ids

        # 移除对应的 job_name
        job_names = wf_config.get("job_names", [])
        if idx < len(job_names):
            job_names.pop(idx)
            wf_config["job_names"] = job_names

        # 移除 required_jobs 中的对应项
        required_jobs = wf_config.get("required_jobs", [])
        wf_config["required_jobs"] = [rj for rj in required_jobs if rj.get("id") != job_id]

        return True

    def _apply_job_name_mismatch(self, suggestion: Suggestion) -> bool:
        """应用: 更新 job_name"""
        wf_key = suggestion.workflow
        job_id = suggestion.key
        actual_name = suggestion.actual_value

        if not actual_name or wf_key not in self.contract:
            return False

        wf_config = self.contract[wf_key]

        # 找到 job_id 在 job_ids 中的索引
        job_ids = wf_config.get("job_ids", [])
        if job_id not in job_ids:
            return False

        idx = job_ids.index(job_id)

        # 更新 job_name
        job_names = wf_config.get("job_names", [])
        if idx < len(job_names):
            job_names[idx] = actual_name
        else:
            # 如果 job_names 不够长，扩展它
            while len(job_names) < idx:
                job_names.append("")
            job_names.append(actual_name)
        wf_config["job_names"] = job_names

        # 同时更新 required_jobs 中的 name（如果有）
        required_jobs = wf_config.get("required_jobs", [])
        for rj in required_jobs:
            if rj.get("id") == job_id:
                rj["name"] = actual_name
                break

        return True

    def _apply_missing_step(self, suggestion: Suggestion) -> bool:
        """应用: 更新或移除缺失的 step

        根据是否有 fuzzy match 决定操作:
        - 有 actual_value: 更新 step name（重命名）
        - 无 actual_value: 移除 step
        """
        wf_key = suggestion.workflow
        key_parts = suggestion.key.split("/", 1)
        if len(key_parts) != 2:
            return False

        job_id, step_name = key_parts
        actual_name = suggestion.actual_value

        if wf_key not in self.contract:
            return False

        wf_config = self.contract[wf_key]
        required_jobs = wf_config.get("required_jobs", [])

        for rj in required_jobs:
            if rj.get("id") != job_id:
                continue

            required_steps = rj.get("required_steps", [])
            if step_name not in required_steps:
                continue

            if actual_name:
                # 重命名 step
                idx = required_steps.index(step_name)
                required_steps[idx] = actual_name
            else:
                # 移除 step
                required_steps.remove(step_name)

            rj["required_steps"] = required_steps
            return True

        return False

    def _apply_new_step(self, suggestion: Suggestion) -> bool:
        """应用: 添加新的 step 到 required_steps"""
        wf_key = suggestion.workflow
        key_parts = suggestion.key.split("/", 1)
        if len(key_parts) != 2:
            return False

        job_id, step_name = key_parts

        if wf_key not in self.contract:
            return False

        wf_config = self.contract[wf_key]
        required_jobs = wf_config.get("required_jobs", [])

        for rj in required_jobs:
            if rj.get("id") != job_id:
                continue

            required_steps = rj.get("required_steps", [])
            if step_name not in required_steps:
                required_steps.append(step_name)
                rj["required_steps"] = required_steps
            return True

        return False

    def _generate_diff(self, before: str, after: str) -> str:
        """生成统一格式的 diff"""
        import difflib

        before_lines = before.splitlines(keepends=True)
        after_lines = after.splitlines(keepends=True)

        diff = difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile="workflow_contract.v2.json (before)",
            tofile="workflow_contract.v2.json (after)",
        )
        return "".join(diff)


# ============================================================================
# Output Formatters
# ============================================================================


def format_json_output(report: SuggestionReport) -> str:
    """将报告格式化为 JSON"""
    data = {
        "has_suggestions": report.has_suggestions,
        "contract_version": report.contract_version,
        "contract_last_updated": report.contract_last_updated,
        "report_generated_at": report.report_generated_at,
        "workflows_checked": report.workflows_checked,
        "summary": report.summary,
        "suggestion_count": len(report.suggestions),
        "suggestions": [
            {
                "suggestion_type": s.suggestion_type,
                "workflow": s.workflow,
                "key": s.key,
                "message": s.message,
                "priority": s.priority,
                "contract_value": s.contract_value,
                "actual_value": s.actual_value,
                "location": s.location,
                "action": s.action,
            }
            for s in report.suggestions
        ],
    }
    return json.dumps(data, indent=2, ensure_ascii=False)


def format_apply_result(result: ApplyResult) -> str:
    """格式化 apply 结果为可读文本"""
    lines: list[str] = []

    lines.append("=" * 60)
    lines.append("Contract Update Apply Result")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"Applied: {result.applied_count}")
    lines.append(f"Skipped: {result.skipped_count}")
    lines.append("")

    if result.applied_suggestions:
        lines.append("Applied Changes:")
        lines.append("-" * 40)
        for s in result.applied_suggestions:
            lines.append(f"  [{s.workflow}] {s.suggestion_type}: {s.key}")
        lines.append("")

    if result.skipped_suggestions:
        lines.append("Skipped (out of scope or low priority):")
        lines.append("-" * 40)
        for s in result.skipped_suggestions:
            lines.append(f"  [{s.workflow}] {s.suggestion_type}: {s.key} (priority={s.priority})")
        lines.append("")

    if result.diff:
        lines.append("Diff:")
        lines.append("-" * 40)
        lines.append(result.diff)

    return "\n".join(lines)


def format_markdown_output(report: SuggestionReport) -> str:
    """将报告格式化为 Markdown"""
    lines: list[str] = []

    # Header
    lines.append("# Workflow Contract Update Suggestions")
    lines.append("")
    lines.append("## Overview")
    lines.append("")
    lines.append(f"- **Contract Version**: {report.contract_version}")
    lines.append(f"- **Contract Last Updated**: {report.contract_last_updated}")
    lines.append(f"- **Report Generated**: {report.report_generated_at}")
    lines.append(f"- **Workflows Checked**: {', '.join(report.workflows_checked)}")
    lines.append(f"- **Has Suggestions**: {'Yes' if report.has_suggestions else 'No'}")
    lines.append(f"- **Total Suggestions**: {len(report.suggestions)}")
    lines.append("")

    if not report.has_suggestions:
        lines.append("> ✅ No suggestions - contract is in sync with workflows!")
        return "\n".join(lines)

    # Summary
    lines.append("## Summary")
    lines.append("")
    lines.append("| Category | Count |")
    lines.append("|----------|-------|")
    for cat, count in sorted(report.summary.items()):
        lines.append(f"| {cat} | {count} |")
    lines.append("")

    # Group by priority
    priority_order = [PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW, PRIORITY_INFO]
    priority_labels = {
        PRIORITY_HIGH: "🔴 High Priority",
        PRIORITY_MEDIUM: "🟡 Medium Priority",
        PRIORITY_LOW: "🟢 Low Priority",
        PRIORITY_INFO: "ℹ️ Info",
    }

    for priority in priority_order:
        items = [s for s in report.suggestions if s.priority == priority]
        if not items:
            continue

        lines.append(f"## {priority_labels[priority]}")
        lines.append("")

        # Group by workflow
        workflows = sorted(set(s.workflow for s in items))
        for wf in workflows:
            wf_items = [s for s in items if s.workflow == wf]
            lines.append(f"### {wf}")
            lines.append("")
            lines.append("| Type | Key | Message | Action |")
            lines.append("|------|-----|---------|--------|")

            for s in wf_items:
                type_short = s.suggestion_type.replace("_", " ").title()
                key_truncated = s.key[:30] + "..." if len(s.key) > 30 else s.key
                msg_truncated = s.message[:50] + "..." if len(s.message) > 50 else s.message
                action_truncated = (
                    (s.action[:40] + "..." if len(s.action) > 40 else s.action) if s.action else "-"
                )
                lines.append(
                    f"| {type_short} | `{key_truncated}` | {msg_truncated} | {action_truncated} |"
                )

            lines.append("")

    # Detailed Actions
    lines.append("## Detailed Actions")
    lines.append("")
    lines.append("以下是具体的修改建议（按优先级排序）：")
    lines.append("")

    for priority in priority_order:
        items = [s for s in report.suggestions if s.priority == priority]
        if not items:
            continue

        for i, s in enumerate(items, 1):
            lines.append(f"### {i}. [{s.workflow}] {s.key}")
            lines.append("")
            lines.append(f"- **类型**: {s.suggestion_type}")
            lines.append(f"- **优先级**: {priority}")
            lines.append(f"- **消息**: {s.message}")
            if s.contract_value:
                lines.append(f"- **Contract 值**: `{s.contract_value}`")
            if s.actual_value:
                lines.append(f"- **实际值**: `{s.actual_value}`")
            if s.location:
                lines.append(f"- **位置**: `{s.location}`")
            if s.action:
                lines.append(f"- **建议操作**: {s.action}")
            lines.append("")

    return "\n".join(lines)


# ============================================================================
# Main
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="分析 workflow 与 contract 的差异，生成更新建议",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 输出 JSON 到 stdout
  python scripts/ci/suggest_workflow_contract_updates.py --json

  # 输出 Markdown 到 stdout
  python scripts/ci/suggest_workflow_contract_updates.py --markdown

  # 输出到文件
  python scripts/ci/suggest_workflow_contract_updates.py --output suggestions.json
  python scripts/ci/suggest_workflow_contract_updates.py --output suggestions.md

  # 输出到 artifacts
  python scripts/ci/suggest_workflow_contract_updates.py --json --output artifacts/workflow_contract_suggestions.json
  python scripts/ci/suggest_workflow_contract_updates.py --markdown --output artifacts/workflow_contract_suggestions.md

  # 只分析 ci workflow
  python scripts/ci/suggest_workflow_contract_updates.py --workflow ci --json

  # 应用建议的更新
  python scripts/ci/suggest_workflow_contract_updates.py --apply

  # 只应用特定范围的更新
  python scripts/ci/suggest_workflow_contract_updates.py --apply --apply-scope jobs
  python scripts/ci/suggest_workflow_contract_updates.py --apply --apply-scope jobs,steps

  # 预览更改（不实际修改）
  python scripts/ci/suggest_workflow_contract_updates.py --apply --dry-run
""",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="输出 JSON 格式到 stdout",
    )

    parser.add_argument(
        "--markdown",
        action="store_true",
        default=False,
        help="输出 Markdown 格式到 stdout",
    )

    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=None,
        help="输出到指定文件（根据扩展名自动选择格式：.json 或 .md）",
    )

    parser.add_argument(
        "--workflow",
        "-w",
        type=str,
        default=None,
        help="只分析指定 workflow（如: ci, nightly）",
    )

    parser.add_argument(
        "--contract-path",
        type=str,
        default=None,
        help="指定合约文件路径（默认自动查找）",
    )

    parser.add_argument(
        "--workspace-root",
        type=str,
        default=None,
        help="指定工作区根目录（默认自动查找）",
    )

    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="应用建议的更新到 contract 文件（注意：此选项不应在 CI 中默认启用）",
    )

    parser.add_argument(
        "--apply-scope",
        type=str,
        default=None,
        help=(
            f"限制应用更新的范围（逗号分隔）。"
            f"可选值: {', '.join(sorted(VALID_APPLY_SCOPES))}。"
            f"默认: 全部"
        ),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="预览将要应用的更改，不实际修改文件（需配合 --apply 使用）",
    )

    args = parser.parse_args()

    # 验证 --apply-scope 参数
    apply_scopes: set[str] | None = None
    if args.apply_scope:
        apply_scopes = set(s.strip() for s in args.apply_scope.split(","))
        invalid_scopes = apply_scopes - VALID_APPLY_SCOPES
        if invalid_scopes:
            print(
                f"错误: 无效的 --apply-scope 值: {', '.join(invalid_scopes)}",
                file=sys.stderr,
            )
            print(f"有效值: {', '.join(sorted(VALID_APPLY_SCOPES))}", file=sys.stderr)
            return 2

    # 确定输出格式
    output_json = args.json
    output_markdown = args.markdown

    if args.output:
        if args.output.endswith(".json"):
            output_json = True
        elif args.output.endswith(".md"):
            output_markdown = True
        else:
            # 默认 JSON
            output_json = True

    # 如果没有指定格式，默认 JSON
    if not output_json and not output_markdown:
        output_json = True

    try:
        # 确定路径
        workspace_root = Path(args.workspace_root) if args.workspace_root else find_project_root()
        contract_path = (
            Path(args.contract_path) if args.contract_path else find_contract_path(workspace_root)
        )

        # 执行分析
        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=contract_path,
            workspace_root=workspace_root,
            workflow_filter=args.workflow,
        )
        report = analyzer.analyze()

        # 如果是 --apply 模式
        if args.apply:
            applier = ContractApplier(
                contract_path=contract_path,
                report=report,
                scopes=apply_scopes,
            )
            apply_result = applier.apply()

            # 输出结果
            print(format_apply_result(apply_result))

            # 如果不是 dry-run 且有更改，保存文件
            if not args.dry_run and apply_result.has_changes:
                applier.save(apply_result)
                print(f"\n合约文件已更新: {contract_path}", file=sys.stderr)
            elif args.dry_run:
                print("\n[dry-run] 未修改任何文件", file=sys.stderr)
            else:
                print("\n无需更新（没有可应用的更改）", file=sys.stderr)

            return 0 if apply_result.has_changes or not report.has_suggestions else 1

        # 格式化输出
        if output_json:
            output_content = format_json_output(report)
        else:
            output_content = format_markdown_output(report)

        # 输出
        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(output_content)
                f.write("\n")
            print(f"报告已保存到: {output_path}", file=sys.stderr)
        else:
            print(output_content)

        # 返回值：有高优先级建议时返回 1
        high_priority_count = sum(1 for s in report.suggestions if s.priority == PRIORITY_HIGH)
        return 1 if high_priority_count > 0 else 0

    except FileNotFoundError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
