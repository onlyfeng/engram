#!/usr/bin/env python3
"""
Workflow Contract 与 Coupling Map 同步校验脚本

校验 workflow_contract.v2.json 中的关键元素是否在 coupling_map.md 文档中有对应记录。

校验模式：
1. **受控块模式**（推荐）：当文档包含 markers 时，渲染期望块并逐字比对
2. **字符串匹配模式**（回退）：当文档无 markers 时，检查值是否在文档中出现

校验范围：
1. ci.job_ids: 每个 CI job id 在 coupling_map.md 可被找到
2. nightly.job_ids: 每个 Nightly job id 在 coupling_map.md 可被找到
3. artifact_archive.required_artifact_paths: 关键 artifact 路径在 coupling_map.md 可被找到
4. make.targets_required: 关键 Make targets 在 coupling_map.md 可被找到

使用方式（推荐使用 -m 方式运行，确保导入路径正确）：
    python -m scripts.ci.check_workflow_contract_coupling_map_sync
    python -m scripts.ci.check_workflow_contract_coupling_map_sync --json
    python -m scripts.ci.check_workflow_contract_coupling_map_sync --verbose

    # 也支持直接运行（需从项目根目录执行）：
    python scripts/ci/check_workflow_contract_coupling_map_sync.py

退出码：
    0: 校验通过
    1: 校验失败（存在未同步项）
    2: 文件读取/解析错误
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts.ci.render_workflow_contract_docs import (
    WorkflowContractDocsRenderer,
    extract_block_from_content,
    find_all_markers,
    get_coupling_map_block_names,
)
from scripts.ci.workflow_contract_common import (
    artifact_path_lookup_tokens,
    discover_workflow_keys,
)

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


class CouplingMapSyncErrorTypes:
    """Coupling Map 同步校验的 error_type 常量定义"""

    # 文件/解析错误
    CONTRACT_NOT_FOUND = "contract_not_found"
    CONTRACT_PARSE_ERROR = "contract_parse_error"
    COUPLING_MAP_NOT_FOUND = "coupling_map_not_found"
    COUPLING_MAP_READ_ERROR = "coupling_map_read_error"

    # 内容缺失错误
    JOB_ID_NOT_IN_COUPLING_MAP = "job_id_not_in_coupling_map"
    ARTIFACT_NOT_IN_COUPLING_MAP = "artifact_not_in_coupling_map"
    MAKE_TARGET_NOT_IN_COUPLING_MAP = "make_target_not_in_coupling_map"

    # 受控块错误（markers 模式）
    BLOCK_MARKER_MISSING = "block_marker_missing"
    BLOCK_MARKER_DUPLICATE = "block_marker_duplicate"
    BLOCK_MARKER_UNPAIRED = "block_marker_unpaired"
    BLOCK_CONTENT_MISMATCH = "block_content_mismatch"
    UNKNOWN_BLOCK_MARKER = "unknown_block_marker"


# 导出所有 error_type 的集合（用于测试覆盖检查）
COUPLING_MAP_SYNC_ERROR_TYPES = frozenset({
    CouplingMapSyncErrorTypes.CONTRACT_NOT_FOUND,
    CouplingMapSyncErrorTypes.CONTRACT_PARSE_ERROR,
    CouplingMapSyncErrorTypes.COUPLING_MAP_NOT_FOUND,
    CouplingMapSyncErrorTypes.COUPLING_MAP_READ_ERROR,
    CouplingMapSyncErrorTypes.JOB_ID_NOT_IN_COUPLING_MAP,
    CouplingMapSyncErrorTypes.ARTIFACT_NOT_IN_COUPLING_MAP,
    CouplingMapSyncErrorTypes.MAKE_TARGET_NOT_IN_COUPLING_MAP,
    CouplingMapSyncErrorTypes.BLOCK_MARKER_MISSING,
    CouplingMapSyncErrorTypes.BLOCK_MARKER_DUPLICATE,
    CouplingMapSyncErrorTypes.BLOCK_MARKER_UNPAIRED,
    CouplingMapSyncErrorTypes.BLOCK_CONTENT_MISMATCH,
    CouplingMapSyncErrorTypes.UNKNOWN_BLOCK_MARKER,
})


# ============================================================================
# Constants
# ============================================================================

DEFAULT_CONTRACT_PATH = "scripts/ci/workflow_contract.v2.json"
DEFAULT_COUPLING_MAP_PATH = "docs/ci_nightly_workflow_refactor/coupling_map.md"

# 需要检查的核心 Make targets（子集）
# 这些是 CI/Nightly workflow 直接使用的关键 targets
CRITICAL_MAKE_TARGETS = frozenset({
    "ci",
    "lint",
    "format-check",
    "typecheck",
    "check-env-consistency",
    "check-logbook-consistency",
    "check-schemas",
    "check-migration-sanity",
    "verify-unified",
    "verify-permissions",
})

# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class SyncError:
    """同步错误"""

    error_type: str
    category: str  # "job_id", "artifact", "make_target", "block"
    value: str
    message: str
    diff: str | None = None  # unified diff for block mismatches
    expected_block: str | None = None  # expected content for easy copy-paste


@dataclass
class SyncResult:
    """同步校验结果"""

    success: bool = True
    errors: list[SyncError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked_job_ids: list[str] = field(default_factory=list)
    checked_artifacts: list[str] = field(default_factory=list)
    checked_make_targets: list[str] = field(default_factory=list)
    checked_blocks: list[str] = field(default_factory=list)  # blocks checked in marker mode
    block_mode_used: bool = False  # whether marker mode was used

    def add_error(self, error: SyncError) -> None:
        """添加错误"""
        self.errors.append(error)
        self.success = False

    def add_warning(self, warning: str) -> None:
        """添加警告"""
        self.warnings.append(warning)


# ============================================================================
# Core Logic
# ============================================================================


class WorkflowContractCouplingMapSyncChecker:
    """Workflow Contract 与 Coupling Map 同步校验器"""

    def __init__(
        self,
        contract_path: Path,
        coupling_map_path: Path,
        verbose: bool = False,
    ) -> None:
        self.contract_path = contract_path
        self.coupling_map_path = coupling_map_path
        self.verbose = verbose
        self.result = SyncResult()
        self.contract: dict[str, Any] = {}
        self.coupling_map_content: str = ""
        self.workflow_keys: list[str] = []

    def load_contract(self) -> bool:
        """加载 contract JSON 文件"""
        if not self.contract_path.exists():
            self.result.add_error(
                SyncError(
                    error_type=CouplingMapSyncErrorTypes.CONTRACT_NOT_FOUND,
                    category="file",
                    value=str(self.contract_path),
                    message=f"Contract file not found: {self.contract_path}",
                )
            )
            return False

        try:
            with open(self.contract_path, "r", encoding="utf-8") as f:
                self.contract = json.load(f)
            # 动态发现 workflow keys
            self.workflow_keys = discover_workflow_keys(self.contract)
            return True
        except json.JSONDecodeError as e:
            self.result.add_error(
                SyncError(
                    error_type=CouplingMapSyncErrorTypes.CONTRACT_PARSE_ERROR,
                    category="file",
                    value=str(self.contract_path),
                    message=f"Failed to parse contract JSON: {e}",
                )
            )
            return False

    def load_coupling_map(self) -> bool:
        """加载 coupling_map.md 文档"""
        if not self.coupling_map_path.exists():
            self.result.add_error(
                SyncError(
                    error_type=CouplingMapSyncErrorTypes.COUPLING_MAP_NOT_FOUND,
                    category="file",
                    value=str(self.coupling_map_path),
                    message=f"Coupling map file not found: {self.coupling_map_path}",
                )
            )
            return False

        try:
            with open(self.coupling_map_path, "r", encoding="utf-8") as f:
                self.coupling_map_content = f.read()
            return True
        except Exception as e:
            self.result.add_error(
                SyncError(
                    error_type=CouplingMapSyncErrorTypes.COUPLING_MAP_READ_ERROR,
                    category="file",
                    value=str(self.coupling_map_path),
                    message=f"Failed to read coupling map file: {e}",
                )
            )
            return False

    def check_value_in_coupling_map(self, value: str) -> bool:
        """检查值是否在 coupling_map 中可被找到"""
        return value in self.coupling_map_content

    def check_job_ids(self) -> None:
        """校验所有 workflow 的 job_ids"""
        for workflow_key in self.workflow_keys:
            workflow_config = self.contract.get(workflow_key, {})
            job_ids = workflow_config.get("job_ids", [])

            if not job_ids:
                continue

            for job_id in job_ids:
                self.result.checked_job_ids.append(job_id)
                if not self.check_value_in_coupling_map(job_id):
                    self.result.add_error(
                        SyncError(
                            error_type=CouplingMapSyncErrorTypes.JOB_ID_NOT_IN_COUPLING_MAP,
                            category="job_id",
                            value=job_id,
                            message=(
                                f"Job ID '{job_id}' ({workflow_key}) not found in coupling_map.md. "
                                f"Please add it to the appropriate section in coupling_map.md."
                            ),
                        )
                    )
                elif self.verbose:
                    print(f"  [OK] job_id ({workflow_key}): {job_id}")

    def check_artifacts(self) -> None:
        """校验关键 artifact 路径"""
        for workflow_key in self.workflow_keys:
            workflow_config = self.contract.get(workflow_key, {})
            artifact_archive = workflow_config.get("artifact_archive", {})
            required_paths = artifact_archive.get("required_artifact_paths", [])

            if not required_paths:
                continue

            for path in required_paths:
                self.result.checked_artifacts.append(path)
                lookup_groups = artifact_path_lookup_tokens(path)
                if not lookup_groups:
                    lookup_groups = [(path,)]

                matched = any(
                    all(token in self.coupling_map_content for token in group)
                    for group in lookup_groups
                )
                if not matched:
                    self.result.add_error(
                        SyncError(
                            error_type=CouplingMapSyncErrorTypes.ARTIFACT_NOT_IN_COUPLING_MAP,
                            category="artifact",
                            value=path,
                            message=(
                                f"Artifact path '{path}' ({workflow_key}) not found in coupling_map.md. "
                                f"Please document this artifact in the relevant job section."
                            ),
                        )
                    )
                elif self.verbose:
                    print(f"  [OK] artifact ({workflow_key}): {path}")

    def check_make_targets(self) -> None:
        """校验关键 Make targets"""
        make_config = self.contract.get("make", {})
        targets_required = make_config.get("targets_required", [])

        if not targets_required:
            self.result.add_warning("No make.targets_required found in contract")
            return

        for target in targets_required:
            # 只检查核心 targets
            if target not in CRITICAL_MAKE_TARGETS:
                continue

            self.result.checked_make_targets.append(target)
            if not self.check_value_in_coupling_map(target):
                self.result.add_error(
                    SyncError(
                        error_type=CouplingMapSyncErrorTypes.MAKE_TARGET_NOT_IN_COUPLING_MAP,
                        category="make_target",
                        value=target,
                        message=(
                            f"Make target '{target}' not found in coupling_map.md. "
                            f"Please add it to section 3 (Makefile Targets)."
                        ),
                    )
                )
            elif self.verbose:
                print(f"  [OK] make_target: {target}")

    # ========================================================================
    # 受控块检查（Marker Mode）
    # ========================================================================

    def has_any_markers(self) -> bool:
        """检查文档中是否存在任何受控块 markers"""
        markers = find_all_markers(self.coupling_map_content)
        return len(markers) > 0

    def check_controlled_blocks(self) -> bool:
        """检查受控块内容是否与渲染结果一致

        Returns:
            True 如果使用了 marker 模式（找到了 markers），False 否则
        """
        # 检查是否有 markers
        markers = find_all_markers(self.coupling_map_content)
        if not markers:
            return False

        self.result.block_mode_used = True

        if self.verbose:
            print("\nUsing marker mode for controlled blocks...")

        # 创建渲染器
        renderer = WorkflowContractDocsRenderer(self.contract_path)
        if not renderer.load_contract():
            return True  # 仍然标记为使用了 marker 模式

        # 只检查 coupling_map.md 相关的块
        rendered_blocks = renderer.render_coupling_map_blocks()
        expected_block_names = get_coupling_map_block_names()

        # 检查 marker 完整性
        marker_map: dict[str, list[tuple[str, int]]] = {}  # block_name -> [(type, line)]
        for block_name, marker_type, line_num in markers:
            if block_name not in marker_map:
                marker_map[block_name] = []
            marker_map[block_name].append((marker_type, line_num))

        unknown_blocks = sorted(set(marker_map.keys()) - set(expected_block_names))
        for block_name in unknown_blocks:
            self.result.add_error(
                SyncError(
                    error_type=CouplingMapSyncErrorTypes.UNKNOWN_BLOCK_MARKER,
                    category="block",
                    value=block_name,
                    message=(
                        f"Unknown block marker '{block_name}' found in document. "
                        "Remove the markers or add renderer support in "
                        "scripts/ci/render_workflow_contract_docs.py and update the expected block list."
                    ),
                )
            )

        # 检查每个预期的块
        for block_name, rendered_block in rendered_blocks.items():
            self.result.checked_blocks.append(block_name)

            # 检查 markers 是否存在
            if block_name not in marker_map:
                # Marker 缺失 - 预期块必须存在 markers
                self.result.add_error(
                    SyncError(
                        error_type=CouplingMapSyncErrorTypes.BLOCK_MARKER_MISSING,
                        category="block",
                        value=block_name,
                        message=(
                            f"Block '{block_name}' markers not found in document. "
                            f"Add <!-- BEGIN:{block_name} --> and <!-- END:{block_name} --> markers."
                        ),
                    )
                )
                continue

            block_markers = marker_map[block_name]

            # 检查 marker 配对
            begin_markers = [m for m in block_markers if m[0] == "begin"]
            end_markers = [m for m in block_markers if m[0] == "end"]

            if len(begin_markers) > 1:
                self.result.add_error(
                    SyncError(
                        error_type=CouplingMapSyncErrorTypes.BLOCK_MARKER_DUPLICATE,
                        category="block",
                        value=block_name,
                        message=f"Duplicate BEGIN marker for block '{block_name}' at lines {[m[1]+1 for m in begin_markers]}",
                    )
                )
                continue

            if len(end_markers) > 1:
                self.result.add_error(
                    SyncError(
                        error_type=CouplingMapSyncErrorTypes.BLOCK_MARKER_DUPLICATE,
                        category="block",
                        value=block_name,
                        message=f"Duplicate END marker for block '{block_name}' at lines {[m[1]+1 for m in end_markers]}",
                    )
                )
                continue

            if len(begin_markers) == 0:
                self.result.add_error(
                    SyncError(
                        error_type=CouplingMapSyncErrorTypes.BLOCK_MARKER_UNPAIRED,
                        category="block",
                        value=block_name,
                        message=f"Missing BEGIN marker for block '{block_name}'",
                    )
                )
                continue

            if len(end_markers) == 0:
                self.result.add_error(
                    SyncError(
                        error_type=CouplingMapSyncErrorTypes.BLOCK_MARKER_UNPAIRED,
                        category="block",
                        value=block_name,
                        message=f"Missing END marker for block '{block_name}'",
                    )
                )
                continue

            # 提取实际块内容
            actual_content, begin_line, end_line = extract_block_from_content(
                self.coupling_map_content, block_name
            )

            if actual_content is None:
                if begin_line == -2:
                    self.result.add_error(
                        SyncError(
                            error_type=CouplingMapSyncErrorTypes.BLOCK_MARKER_DUPLICATE,
                            category="block",
                            value=block_name,
                            message=f"Duplicate BEGIN marker for block '{block_name}'",
                        )
                    )
                elif begin_line == -3:
                    self.result.add_error(
                        SyncError(
                            error_type=CouplingMapSyncErrorTypes.BLOCK_MARKER_DUPLICATE,
                            category="block",
                            value=block_name,
                            message=f"Duplicate END marker for block '{block_name}'",
                        )
                    )
                continue

            # 比较内容
            expected_content = rendered_block.content
            if actual_content.strip() != expected_content.strip():
                # 生成 unified diff
                actual_lines = actual_content.strip().split("\n")
                expected_lines = expected_content.strip().split("\n")
                diff = "\n".join(
                    difflib.unified_diff(
                        actual_lines,
                        expected_lines,
                        fromfile=f"actual:{block_name}",
                        tofile=f"expected:{block_name}",
                        lineterm="",
                    )
                )

                self.result.add_error(
                    SyncError(
                        error_type=CouplingMapSyncErrorTypes.BLOCK_CONTENT_MISMATCH,
                        category="block",
                        value=block_name,
                        message=f"Block '{block_name}' content mismatch (lines {begin_line+2}-{end_line})",
                        diff=diff,
                        expected_block=rendered_block.full_block(),
                    )
                )
            elif self.verbose:
                print(f"  [OK] block: {block_name}")

        return True

    def check(self) -> SyncResult:
        """执行完整校验

        校验流程：
        1. 加载 contract 和 coupling_map
        2. 尝试使用 marker 模式检查受控块
        3. 无论是否使用 marker 模式，都执行传统的字符串匹配检查
           （传统检查作为基线保障，确保关键内容存在）
        """
        if self.verbose:
            print(f"Loading contract: {self.contract_path}")

        if not self.load_contract():
            return self.result

        if self.verbose:
            print(f"Loading coupling map: {self.coupling_map_path}")

        if not self.load_coupling_map():
            return self.result

        # 尝试使用 marker 模式检查受控块
        if self.verbose:
            print("\nChecking controlled blocks (marker mode)...")
        self.check_controlled_blocks()

        if self.verbose:
            print(f"\nChecking job_ids across workflows: {self.workflow_keys}...")
        self.check_job_ids()

        if self.verbose:
            print("\nChecking artifact paths...")
        self.check_artifacts()

        if self.verbose:
            print("\nChecking critical make targets...")
        self.check_make_targets()

        return self.result


# ============================================================================
# Output Formatting
# ============================================================================


def format_text_output(result: SyncResult) -> str:
    """格式化文本输出"""
    lines: list[str] = []

    if result.success:
        lines.append("=" * 60)
        lines.append("Workflow Contract Coupling Map Sync Check: PASSED")
        lines.append("=" * 60)
    else:
        lines.append("=" * 60)
        lines.append("Workflow Contract Coupling Map Sync Check: FAILED")
        lines.append("=" * 60)

    lines.append("")
    lines.append("Summary:")
    lines.append(f"  - Block mode used: {result.block_mode_used}")
    lines.append(f"  - Checked blocks: {len(result.checked_blocks)}")
    lines.append(f"  - Checked job_ids: {len(result.checked_job_ids)}")
    lines.append(f"  - Checked artifacts: {len(result.checked_artifacts)}")
    lines.append(f"  - Checked make_targets: {len(result.checked_make_targets)}")
    lines.append(f"  - Errors: {len(result.errors)}")
    lines.append(f"  - Warnings: {len(result.warnings)}")

    if result.errors:
        lines.append("")
        lines.append("ERRORS:")
        for error in result.errors:
            lines.append(f"  [{error.error_type}] {error.category}: {error.value}")
            lines.append(f"    {error.message}")
            # 显示 diff（如果有）
            if error.diff:
                lines.append("")
                lines.append("    --- Diff ---")
                for diff_line in error.diff.split("\n"):
                    lines.append(f"    {diff_line}")
            # 显示期望块（如果有）
            if error.expected_block:
                lines.append("")
                lines.append("    --- Expected block (copy-paste ready) ---")
                for block_line in error.expected_block.split("\n"):
                    lines.append(f"    {block_line}")

    if result.warnings:
        lines.append("")
        lines.append("WARNINGS:")
        for warning in result.warnings:
            lines.append(f"  {warning}")

    return "\n".join(lines)


def format_json_output(result: SyncResult) -> str:
    """格式化 JSON 输出"""
    output = {
        "success": result.success,
        "block_mode_used": result.block_mode_used,
        "error_count": len(result.errors),
        "warning_count": len(result.warnings),
        "checked_blocks": result.checked_blocks,
        "checked_job_ids": result.checked_job_ids,
        "checked_artifacts": result.checked_artifacts,
        "checked_make_targets": result.checked_make_targets,
        "errors": [
            {
                "error_type": e.error_type,
                "category": e.category,
                "value": e.value,
                "message": e.message,
                "diff": e.diff,
                "expected_block": e.expected_block,
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
        description="校验 workflow_contract.v2.json 与 coupling_map.md 的同步一致性",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--contract",
        type=str,
        default=DEFAULT_CONTRACT_PATH,
        help=f"Contract JSON 文件路径 (default: {DEFAULT_CONTRACT_PATH})",
    )
    parser.add_argument(
        "--coupling-map",
        type=str,
        default=DEFAULT_COUPLING_MAP_PATH,
        help=f"Coupling Map Markdown 文件路径 (default: {DEFAULT_COUPLING_MAP_PATH})",
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
        # scripts/ci/check_workflow_contract_coupling_map_sync.py -> project_root
        project_root = script_path.parent.parent.parent
        if not (project_root / "scripts" / "ci").exists():
            # 回退到当前工作目录
            project_root = Path.cwd()

    contract_path = project_root / args.contract
    coupling_map_path = project_root / args.coupling_map

    if args.verbose and not args.json:
        print(f"Project root: {project_root}")
        print(f"Contract path: {contract_path}")
        print(f"Coupling map path: {coupling_map_path}")
        print()

    checker = WorkflowContractCouplingMapSyncChecker(
        contract_path=contract_path,
        coupling_map_path=coupling_map_path,
        verbose=args.verbose and not args.json,
    )

    result = checker.check()

    if args.json:
        print(format_json_output(result))
    else:
        print(format_text_output(result))

    # 返回退出码
    if result.errors:
        # 区分文件错误和同步错误
        file_errors = [e for e in result.errors if e.category == "file"]
        if file_errors:
            return 2
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
