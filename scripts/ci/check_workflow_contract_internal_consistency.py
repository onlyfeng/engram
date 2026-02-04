#!/usr/bin/env python3
"""
check_workflow_contract_internal_consistency.py

校验 workflow_contract.v2.json 的内部不变量。

校验规则（参见 _changelog_v2.10.0）：
1. job_ids/job_names 长度一致（位置匹配）
2. job_ids 无重复
3. required_jobs id 无重复
4. required_jobs 的 id 必须在 job_ids 中定义

用法:
    python scripts/ci/check_workflow_contract_internal_consistency.py [--json] [--verbose]

退出码:
    0: 校验通过
    1: 校验失败（发现不一致）
    2: 文件加载失败

产物:
    --json 输出 JSON 格式结果
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Add project root to path for imports when run as script
_SCRIPT_DIR = Path(__file__).parent.resolve()
_PROJECT_ROOT = _SCRIPT_DIR.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Import common utilities
from scripts.ci.workflow_contract_common import (
    build_workflows_view,
    discover_workflow_keys,
)

# ============================================================================
# Constants
# ============================================================================

# 默认合约文件路径
DEFAULT_CONTRACT_PATH = Path(__file__).parent / "workflow_contract.v2.json"

# Error types (align with validate_workflows.py / contract.md)
CONTRACT_JOB_IDS_NAMES_LENGTH_MISMATCH = "contract_job_ids_names_length_mismatch"
CONTRACT_JOB_IDS_DUPLICATE = "contract_job_ids_duplicate"
CONTRACT_REQUIRED_JOB_ID_DUPLICATE = "contract_required_job_id_duplicate"
CONTRACT_REQUIRED_JOB_NOT_IN_JOB_IDS = "contract_required_job_not_in_job_ids"


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class ConsistencyError:
    """内部一致性错误"""

    error_type: str
    workflow: str
    message: str
    key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_type": self.error_type,
            "workflow": self.workflow,
            "message": self.message,
            "key": self.key,
        }


@dataclass
class ConsistencyResult:
    """校验结果"""

    success: bool = True
    errors: list[ConsistencyError] = field(default_factory=list)
    workflows_checked: list[str] = field(default_factory=list)
    contract_version: str = ""

    def add_error(self, error: ConsistencyError) -> None:
        self.errors.append(error)
        self.success = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "contract_version": self.contract_version,
            "workflows_checked": self.workflows_checked,
            "error_count": len(self.errors),
            "errors": [e.to_dict() for e in self.errors],
        }


# ============================================================================
# Checker Class
# ============================================================================


class WorkflowContractInternalConsistencyChecker:
    """Workflow Contract 内部一致性检查器"""

    def __init__(self, contract_path: Path | None = None):
        self.contract_path = contract_path or DEFAULT_CONTRACT_PATH
        self.contract: dict[str, Any] = {}
        self.result = ConsistencyResult()

    def load_contract(self) -> bool:
        """加载合约文件

        Returns:
            加载是否成功
        """
        try:
            with open(self.contract_path, "r", encoding="utf-8") as f:
                self.contract = json.load(f)
            self.result.contract_version = self.contract.get("version", "unknown")
            return True
        except (OSError, json.JSONDecodeError) as e:
            print(f"ERROR: Failed to load contract: {e}", file=sys.stderr)
            return False

    def check(self) -> ConsistencyResult:
        """执行内部一致性检查

        Returns:
            检查结果
        """
        if not self.contract:
            if not self.load_contract():
                self.result.success = False
                return self.result

        # 获取所有 workflow 定义
        workflows = build_workflows_view(self.contract)
        workflow_keys = discover_workflow_keys(self.contract)
        self.result.workflows_checked = workflow_keys

        for workflow_key in workflow_keys:
            workflow_config = workflows.get(workflow_key, {})
            self._check_workflow(workflow_key, workflow_config)

        return self.result

    def _check_workflow(self, workflow_key: str, config: dict[str, Any]) -> None:
        """检查单个 workflow 的内部一致性

        Args:
            workflow_key: workflow 名称（如 "ci", "nightly"）
            config: workflow 配置
        """
        job_ids = config.get("job_ids", [])
        job_names = config.get("job_names", [])
        required_jobs = config.get("required_jobs", [])

        # 规则 1: job_ids 和 job_names 长度必须一致
        self._check_job_ids_names_length(workflow_key, job_ids, job_names)

        # 规则 2: job_ids 无重复
        self._check_no_duplicate_job_ids(workflow_key, job_ids)

        # 规则 3: required_jobs 的 id 无重复
        self._check_no_duplicate_required_job_ids(workflow_key, required_jobs)

        # 规则 4: required_jobs 的 id 必须在 job_ids 中
        self._check_required_jobs_in_job_ids(workflow_key, required_jobs, job_ids)

    def _check_job_ids_names_length(
        self, workflow: str, job_ids: list[str], job_names: list[str]
    ) -> None:
        """规则 1: job_ids 和 job_names 长度必须一致（位置匹配）"""
        if len(job_ids) != len(job_names):
            self.result.add_error(
                ConsistencyError(
                    error_type=CONTRACT_JOB_IDS_NAMES_LENGTH_MISMATCH,
                    workflow=workflow,
                    message=(
                        f"Contract error: {workflow}.job_ids ({len(job_ids)}) and "
                        f"{workflow}.job_names ({len(job_names)}) must have the same length "
                        f"for position matching. Fix scripts/ci/workflow_contract.v2.json "
                        f"fields: {workflow}.job_ids / {workflow}.job_names."
                    ),
                    key=f"job_ids={len(job_ids)}, job_names={len(job_names)}",
                )
            )

    def _check_no_duplicate_job_ids(self, workflow: str, job_ids: list[str]) -> None:
        """规则 2: job_ids 无重复"""
        seen: set[str] = set()
        for job_id in job_ids:
            if job_id in seen:
                self.result.add_error(
                    ConsistencyError(
                        error_type=CONTRACT_JOB_IDS_DUPLICATE,
                        workflow=workflow,
                        message=(
                            f"Contract error: Duplicate job_id '{job_id}' in {workflow}.job_ids. "
                            f"Fix scripts/ci/workflow_contract.v2.json field: {workflow}.job_ids."
                        ),
                        key=job_id,
                    )
                )
            seen.add(job_id)

    def _check_no_duplicate_required_job_ids(
        self, workflow: str, required_jobs: list[dict[str, Any]]
    ) -> None:
        """规则 3: required_jobs 的 id 无重复"""
        seen: set[str] = set()
        for job in required_jobs:
            job_id = job.get("id", "")
            if not job_id:
                continue
            if job_id in seen:
                self.result.add_error(
                    ConsistencyError(
                        error_type=CONTRACT_REQUIRED_JOB_ID_DUPLICATE,
                        workflow=workflow,
                        message=(
                            f"Contract error: Duplicate required_job id '{job_id}' in "
                            f"{workflow}.required_jobs. Fix scripts/ci/workflow_contract.v2.json "
                            f"field: {workflow}.required_jobs[*].id."
                        ),
                        key=job_id,
                    )
                )
            seen.add(job_id)

    def _check_required_jobs_in_job_ids(
        self, workflow: str, required_jobs: list[dict[str, Any]], job_ids: list[str]
    ) -> None:
        """规则 4: required_jobs 的 id 必须在 job_ids 中"""
        job_ids_set = set(job_ids)
        for job in required_jobs:
            job_id = job.get("id", "")
            if not job_id:
                continue
            if job_id not in job_ids_set:
                self.result.add_error(
                    ConsistencyError(
                        error_type=CONTRACT_REQUIRED_JOB_NOT_IN_JOB_IDS,
                        workflow=workflow,
                        message=(
                            f"Contract error: required_job id '{job_id}' is not defined in "
                            f"{workflow}.job_ids. Fix scripts/ci/workflow_contract.v2.json: "
                            f"add '{job_id}' to {workflow}.job_ids or remove it from "
                            f"{workflow}.required_jobs."
                        ),
                        key=job_id,
                    )
                )


# ============================================================================
# Output Formatting
# ============================================================================


def format_text_output(result: ConsistencyResult, verbose: bool = False) -> str:
    """格式化文本输出

    Args:
        result: 检查结果
        verbose: 是否详细输出

    Returns:
        格式化的文本
    """
    lines: list[str] = []

    lines.append("=" * 60)
    lines.append("Workflow Contract Internal Consistency Check")
    lines.append("=" * 60)
    lines.append(f"Contract version: {result.contract_version}")
    lines.append(f"Workflows checked: {', '.join(result.workflows_checked)}")
    lines.append("")

    if result.success:
        lines.append("[PASS] All internal consistency checks passed")
    else:
        lines.append(f"[FAIL] Found {len(result.errors)} consistency error(s)")
        lines.append("")
        for error in result.errors:
            lines.append(f"  [{error.error_type}] {error.workflow}: {error.message}")
            if verbose and error.key:
                lines.append(f"    Key: {error.key}")

    return "\n".join(lines)


def format_json_output(result: ConsistencyResult) -> str:
    """格式化 JSON 输出

    Args:
        result: 检查结果

    Returns:
        JSON 字符串
    """
    return json.dumps(result.to_dict(), indent=2, ensure_ascii=False)


# ============================================================================
# Main
# ============================================================================


def main() -> int:
    """主函数

    Returns:
        退出码
    """
    parser = argparse.ArgumentParser(
        description="Check workflow contract internal consistency"
    )
    parser.add_argument(
        "--contract",
        type=Path,
        default=DEFAULT_CONTRACT_PATH,
        help=f"Path to contract file (default: {DEFAULT_CONTRACT_PATH})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose output",
    )
    args = parser.parse_args()

    # 执行检查
    checker = WorkflowContractInternalConsistencyChecker(args.contract)

    if not checker.load_contract():
        return 2

    result = checker.check()

    # 输出结果
    if args.json:
        print(format_json_output(result))
    else:
        print(format_text_output(result, args.verbose))

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
