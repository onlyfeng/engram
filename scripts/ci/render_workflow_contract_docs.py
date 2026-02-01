#!/usr/bin/env python3
"""
Workflow Contract 受控块渲染器

从 workflow_contract.v1.json 读取数据，生成 Markdown 受控块内容。
输出用于 contract.md 和 coupling_map.md 中的受控区域。

受控块使用 HTML 注释作为 markers：
    <!-- BEGIN:BLOCK_NAME -->
    ... 渲染内容 ...
    <!-- END:BLOCK_NAME -->

使用方式：
    # 渲染 contract.md 受控块
    python scripts/ci/render_workflow_contract_docs.py --target contract

    # 渲染 coupling_map.md 受控块
    python scripts/ci/render_workflow_contract_docs.py --target coupling_map

    # 输出 JSON 格式（包含所有块）
    python scripts/ci/render_workflow_contract_docs.py --json

    # 渲染指定块
    python scripts/ci/render_workflow_contract_docs.py --block CI_JOB_TABLE

    # 更新文档中的受控块（就地写入）
    python scripts/ci/render_workflow_contract_docs.py --write --target contract

    # 更新所有文档
    python scripts/ci/render_workflow_contract_docs.py --write --target all
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.ci.workflow_contract_common import discover_workflow_keys

# ============================================================================
# Constants
# ============================================================================

DEFAULT_CONTRACT_PATH = "scripts/ci/workflow_contract.v1.json"

# 文档路径
DEFAULT_CONTRACT_DOC_PATH = "docs/ci_nightly_workflow_refactor/contract.md"
DEFAULT_COUPLING_MAP_DOC_PATH = "docs/ci_nightly_workflow_refactor/coupling_map.md"

# Marker 格式
MARKER_BEGIN_FMT = "<!-- BEGIN:{block_name} -->"
MARKER_END_FMT = "<!-- END:{block_name} -->"


# ============================================================================
# Block Names - 受控块名称定义
# ============================================================================


class ContractBlockNames:
    """contract.md 中的受控块名称"""

    CI_JOB_TABLE = "CI_JOB_TABLE"
    NIGHTLY_JOB_TABLE = "NIGHTLY_JOB_TABLE"
    FROZEN_JOB_NAMES_TABLE = "FROZEN_JOB_NAMES_TABLE"
    FROZEN_STEP_NAMES_TABLE = "FROZEN_STEP_NAMES_TABLE"
    MAKE_TARGETS_TABLE = "MAKE_TARGETS_TABLE"
    LABELS_TABLE = "LABELS_TABLE"


class CouplingMapBlockNames:
    """coupling_map.md 中的受控块名称"""

    CI_JOBS_LIST = "CI_JOBS_LIST"
    NIGHTLY_JOBS_LIST = "NIGHTLY_JOBS_LIST"
    MAKE_TARGETS_LIST = "MAKE_TARGETS_LIST"


# 所有 contract.md 块名称
CONTRACT_BLOCK_NAMES = frozenset({
    ContractBlockNames.CI_JOB_TABLE,
    ContractBlockNames.NIGHTLY_JOB_TABLE,
    ContractBlockNames.FROZEN_JOB_NAMES_TABLE,
    ContractBlockNames.FROZEN_STEP_NAMES_TABLE,
    ContractBlockNames.MAKE_TARGETS_TABLE,
    ContractBlockNames.LABELS_TABLE,
})

# 所有 coupling_map.md 块名称
COUPLING_MAP_BLOCK_NAMES = frozenset({
    CouplingMapBlockNames.CI_JOBS_LIST,
    CouplingMapBlockNames.NIGHTLY_JOBS_LIST,
    CouplingMapBlockNames.MAKE_TARGETS_LIST,
})


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class RenderedBlock:
    """渲染后的受控块"""

    name: str
    content: str
    begin_marker: str
    end_marker: str

    def full_block(self) -> str:
        """返回包含 markers 的完整块"""
        return f"{self.begin_marker}\n{self.content}\n{self.end_marker}"


# ============================================================================
# Renderer Class
# ============================================================================


class WorkflowContractDocsRenderer:
    """Workflow Contract 文档渲染器"""

    def __init__(self, contract_path: Path) -> None:
        self.contract_path = contract_path
        self.contract: dict[str, Any] = {}
        self.workflow_keys: list[str] = []

    def load_contract(self) -> bool:
        """加载 contract JSON 文件"""
        if not self.contract_path.exists():
            print(f"Error: Contract file not found: {self.contract_path}", file=sys.stderr)
            return False

        try:
            with open(self.contract_path, "r", encoding="utf-8") as f:
                self.contract = json.load(f)
            self.workflow_keys = discover_workflow_keys(self.contract)
            return True
        except json.JSONDecodeError as e:
            print(f"Error: Failed to parse contract JSON: {e}", file=sys.stderr)
            return False

    def _make_marker(self, block_name: str, marker_type: str) -> str:
        """生成 marker 字符串"""
        if marker_type == "begin":
            return MARKER_BEGIN_FMT.format(block_name=block_name)
        return MARKER_END_FMT.format(block_name=block_name)

    # ========================================================================
    # contract.md 块渲染方法
    # ========================================================================

    def render_ci_job_table(self) -> RenderedBlock:
        """渲染 CI Workflow Job ID/Name 对照表"""
        block_name = ContractBlockNames.CI_JOB_TABLE
        ci_config = self.contract.get("ci", {})
        job_ids = ci_config.get("job_ids", [])
        job_names = ci_config.get("job_names", [])
        required_jobs = ci_config.get("required_jobs", [])

        # 构建 job_id -> description 映射（从 required_jobs 获取描述）
        job_descriptions: dict[str, str] = {}
        for job in required_jobs:
            job_id = job.get("id", "")
            # 使用 job 的 _comment 或 required_steps 的第一条作为说明
            desc = job.get("_comment", "")
            if not desc and job.get("required_steps"):
                # 简单描述：显示 steps 数量
                step_count = len(job.get("required_steps", []))
                desc = f"{step_count} 个必需步骤"
            job_descriptions[job_id] = desc

        lines: list[str] = []
        lines.append("| Job ID | Job Name | 说明 |")
        lines.append("|--------|----------|------|")

        for i, job_id in enumerate(job_ids):
            job_name = job_names[i] if i < len(job_names) else ""
            desc = job_descriptions.get(job_id, "")
            lines.append(f"| `{job_id}` | {job_name} | {desc} |")

        return RenderedBlock(
            name=block_name,
            content="\n".join(lines),
            begin_marker=self._make_marker(block_name, "begin"),
            end_marker=self._make_marker(block_name, "end"),
        )

    def render_nightly_job_table(self) -> RenderedBlock:
        """渲染 Nightly Workflow Job ID/Name 对照表"""
        block_name = ContractBlockNames.NIGHTLY_JOB_TABLE
        nightly_config = self.contract.get("nightly", {})
        job_ids = nightly_config.get("job_ids", [])
        job_names = nightly_config.get("job_names", [])
        required_jobs = nightly_config.get("required_jobs", [])

        # 构建 job_id -> description 映射
        job_descriptions: dict[str, str] = {}
        for job in required_jobs:
            job_id = job.get("id", "")
            desc = job.get("_comment", "")
            if not desc and job.get("required_steps"):
                step_count = len(job.get("required_steps", []))
                desc = f"{step_count} 个必需步骤"
            job_descriptions[job_id] = desc

        lines: list[str] = []
        lines.append("| Job ID | Job Name | 说明 |")
        lines.append("|--------|----------|------|")

        for i, job_id in enumerate(job_ids):
            job_name = job_names[i] if i < len(job_names) else ""
            desc = job_descriptions.get(job_id, "")
            lines.append(f"| `{job_id}` | {job_name} | {desc} |")

        return RenderedBlock(
            name=block_name,
            content="\n".join(lines),
            begin_marker=self._make_marker(block_name, "begin"),
            end_marker=self._make_marker(block_name, "end"),
        )

    def render_frozen_job_names_table(self) -> RenderedBlock:
        """渲染 Frozen Job Names 表"""
        block_name = ContractBlockNames.FROZEN_JOB_NAMES_TABLE
        frozen_job_names = self.contract.get("frozen_job_names", {})
        allowlist = frozen_job_names.get("allowlist", [])

        lines: list[str] = []
        lines.append("| Job Name | 原因 |")
        lines.append("|----------|------|")

        # 按字母序排序以保证渲染稳定性
        for job_name in sorted(allowlist):
            # 原因可以从注释或固定描述获取
            lines.append(f"| `{job_name}` | Required Check |")

        return RenderedBlock(
            name=block_name,
            content="\n".join(lines),
            begin_marker=self._make_marker(block_name, "begin"),
            end_marker=self._make_marker(block_name, "end"),
        )

    def render_frozen_step_names_table(self) -> RenderedBlock:
        """渲染 Frozen Step Names 表"""
        block_name = ContractBlockNames.FROZEN_STEP_NAMES_TABLE
        frozen_step_text = self.contract.get("frozen_step_text", {})
        allowlist = frozen_step_text.get("allowlist", [])

        lines: list[str] = []
        lines.append("| Step Name | 冻结原因 |")
        lines.append("|-----------|----------|")

        # 按字母序排序以保证渲染稳定性
        for step_name in sorted(allowlist):
            lines.append(f"| `{step_name}` | 核心步骤 |")

        return RenderedBlock(
            name=block_name,
            content="\n".join(lines),
            begin_marker=self._make_marker(block_name, "begin"),
            end_marker=self._make_marker(block_name, "end"),
        )

    def render_make_targets_table(self) -> RenderedBlock:
        """渲染 Make Targets 表"""
        block_name = ContractBlockNames.MAKE_TARGETS_TABLE
        make_config = self.contract.get("make", {})
        targets_required = make_config.get("targets_required", [])

        lines: list[str] = []
        lines.append("| Make Target | 用途 |")
        lines.append("|-------------|------|")

        # 按字母序排序以保证渲染稳定性
        for target in sorted(targets_required):
            lines.append(f"| `{target}` | CI 必需目标 |")

        return RenderedBlock(
            name=block_name,
            content="\n".join(lines),
            begin_marker=self._make_marker(block_name, "begin"),
            end_marker=self._make_marker(block_name, "end"),
        )

    def render_labels_table(self) -> RenderedBlock:
        """渲染 Labels 表"""
        block_name = ContractBlockNames.LABELS_TABLE
        all_labels: list[tuple[str, str]] = []  # (label, workflow)

        for workflow_key in self.workflow_keys:
            workflow_config = self.contract.get(workflow_key, {})
            labels = workflow_config.get("labels", [])
            for label in labels:
                all_labels.append((label, workflow_key))

        lines: list[str] = []
        lines.append("| Label | Workflow | 语义 |")
        lines.append("|-------|----------|------|")

        # 按 label 名称排序以保证渲染稳定性
        for label, workflow in sorted(all_labels, key=lambda x: x[0]):
            lines.append(f"| `{label}` | {workflow} | PR label |")

        return RenderedBlock(
            name=block_name,
            content="\n".join(lines),
            begin_marker=self._make_marker(block_name, "begin"),
            end_marker=self._make_marker(block_name, "end"),
        )

    # ========================================================================
    # coupling_map.md 块渲染方法
    # ========================================================================

    def render_ci_jobs_list(self) -> RenderedBlock:
        """渲染 CI Jobs 列表"""
        block_name = CouplingMapBlockNames.CI_JOBS_LIST
        ci_config = self.contract.get("ci", {})
        job_ids = ci_config.get("job_ids", [])
        job_names = ci_config.get("job_names", [])

        lines: list[str] = []
        lines.append("| Job ID | Job Name |")
        lines.append("|--------|----------|")

        for i, job_id in enumerate(job_ids):
            job_name = job_names[i] if i < len(job_names) else ""
            lines.append(f"| `{job_id}` | {job_name} |")

        return RenderedBlock(
            name=block_name,
            content="\n".join(lines),
            begin_marker=self._make_marker(block_name, "begin"),
            end_marker=self._make_marker(block_name, "end"),
        )

    def render_nightly_jobs_list(self) -> RenderedBlock:
        """渲染 Nightly Jobs 列表"""
        block_name = CouplingMapBlockNames.NIGHTLY_JOBS_LIST
        nightly_config = self.contract.get("nightly", {})
        job_ids = nightly_config.get("job_ids", [])
        job_names = nightly_config.get("job_names", [])

        lines: list[str] = []
        lines.append("| Job ID | Job Name |")
        lines.append("|--------|----------|")

        for i, job_id in enumerate(job_ids):
            job_name = job_names[i] if i < len(job_names) else ""
            lines.append(f"| `{job_id}` | {job_name} |")

        return RenderedBlock(
            name=block_name,
            content="\n".join(lines),
            begin_marker=self._make_marker(block_name, "begin"),
            end_marker=self._make_marker(block_name, "end"),
        )

    def render_make_targets_list(self) -> RenderedBlock:
        """渲染 Make Targets 列表"""
        block_name = CouplingMapBlockNames.MAKE_TARGETS_LIST
        make_config = self.contract.get("make", {})
        targets_required = make_config.get("targets_required", [])

        lines: list[str] = []
        lines.append("| Target | 说明 |")
        lines.append("|--------|------|")

        # 按字母序排序
        for target in sorted(targets_required):
            lines.append(f"| `{target}` | CI/workflow 必需 |")

        return RenderedBlock(
            name=block_name,
            content="\n".join(lines),
            begin_marker=self._make_marker(block_name, "begin"),
            end_marker=self._make_marker(block_name, "end"),
        )

    # ========================================================================
    # 公共方法
    # ========================================================================

    def render_contract_blocks(self) -> dict[str, RenderedBlock]:
        """渲染所有 contract.md 受控块"""
        return {
            ContractBlockNames.CI_JOB_TABLE: self.render_ci_job_table(),
            ContractBlockNames.NIGHTLY_JOB_TABLE: self.render_nightly_job_table(),
            ContractBlockNames.FROZEN_JOB_NAMES_TABLE: self.render_frozen_job_names_table(),
            ContractBlockNames.FROZEN_STEP_NAMES_TABLE: self.render_frozen_step_names_table(),
            ContractBlockNames.MAKE_TARGETS_TABLE: self.render_make_targets_table(),
            ContractBlockNames.LABELS_TABLE: self.render_labels_table(),
        }

    def render_coupling_map_blocks(self) -> dict[str, RenderedBlock]:
        """渲染所有 coupling_map.md 受控块"""
        return {
            CouplingMapBlockNames.CI_JOBS_LIST: self.render_ci_jobs_list(),
            CouplingMapBlockNames.NIGHTLY_JOBS_LIST: self.render_nightly_jobs_list(),
            CouplingMapBlockNames.MAKE_TARGETS_LIST: self.render_make_targets_list(),
        }

    def render_all_blocks(self) -> dict[str, RenderedBlock]:
        """渲染所有受控块"""
        blocks = {}
        blocks.update(self.render_contract_blocks())
        blocks.update(self.render_coupling_map_blocks())
        return blocks

    def render_block(self, block_name: str) -> RenderedBlock | None:
        """渲染指定名称的块"""
        all_blocks = self.render_all_blocks()
        return all_blocks.get(block_name)


# ============================================================================
# Block Extraction Utilities
# ============================================================================


def extract_block_from_content(
    content: str,
    block_name: str,
) -> tuple[str | None, int, int]:
    """从文档内容中提取指定块

    Args:
        content: 文档内容
        block_name: 块名称

    Returns:
        (块内容, 开始行号, 结束行号)，如果未找到则返回 (None, -1, -1)
    """
    begin_marker = MARKER_BEGIN_FMT.format(block_name=block_name)
    end_marker = MARKER_END_FMT.format(block_name=block_name)

    lines = content.split("\n")
    begin_line = -1
    end_line = -1

    for i, line in enumerate(lines):
        if begin_marker in line:
            if begin_line != -1:
                # 重复的 begin marker
                return None, -2, -2
            begin_line = i
        elif end_marker in line:
            if end_line != -1:
                # 重复的 end marker
                return None, -3, -3
            end_line = i

    if begin_line == -1:
        return None, -1, -1

    if end_line == -1:
        return None, begin_line, -1

    if end_line <= begin_line:
        return None, begin_line, end_line

    # 提取 begin 和 end 之间的内容（不包含 markers 所在行）
    block_content = "\n".join(lines[begin_line + 1 : end_line])
    return block_content, begin_line, end_line


def find_all_markers(content: str) -> list[tuple[str, str, int]]:
    """查找文档中所有的 markers

    Returns:
        [(block_name, marker_type, line_number), ...]
        marker_type: "begin" | "end"
    """
    import re

    markers: list[tuple[str, str, int]] = []
    lines = content.split("\n")

    begin_pattern = re.compile(r"<!--\s*BEGIN:(\w+)\s*-->")
    end_pattern = re.compile(r"<!--\s*END:(\w+)\s*-->")

    for i, line in enumerate(lines):
        begin_match = begin_pattern.search(line)
        if begin_match:
            markers.append((begin_match.group(1), "begin", i))

        end_match = end_pattern.search(line)
        if end_match:
            markers.append((end_match.group(1), "end", i))

    return markers


# ============================================================================
# Document Update Utilities
# ============================================================================


@dataclass
class UpdateResult:
    """文档更新结果"""

    doc_path: str
    updated_blocks: list[str]
    missing_markers: list[str]
    unchanged_blocks: list[str]
    success: bool
    error_message: str | None = None


def replace_block_in_content(
    content: str,
    block_name: str,
    new_content: str,
) -> tuple[str | None, str | None]:
    """替换文档中指定块的内容

    Args:
        content: 原文档内容
        block_name: 块名称
        new_content: 新的块内容（不包含 markers）

    Returns:
        (更新后的内容, 错误信息)
        如果成功，错误信息为 None
        如果失败，更新后的内容为 None
    """
    begin_marker = MARKER_BEGIN_FMT.format(block_name=block_name)
    end_marker = MARKER_END_FMT.format(block_name=block_name)

    lines = content.split("\n")
    begin_line = -1
    end_line = -1

    for i, line in enumerate(lines):
        if begin_marker in line:
            if begin_line != -1:
                return None, f"Duplicate BEGIN marker for block '{block_name}'"
            begin_line = i
        elif end_marker in line:
            if end_line != -1:
                return None, f"Duplicate END marker for block '{block_name}'"
            end_line = i

    if begin_line == -1 and end_line == -1:
        return None, f"Missing markers for block '{block_name}'"

    if begin_line == -1:
        return None, f"Missing BEGIN marker for block '{block_name}'"

    if end_line == -1:
        return None, f"Missing END marker for block '{block_name}'"

    if end_line <= begin_line:
        return None, f"END marker appears before BEGIN marker for block '{block_name}'"

    # 构建新内容：保留 markers 所在行，替换中间内容
    new_lines = (
        lines[: begin_line + 1]  # 包含 BEGIN marker
        + [new_content]  # 新内容
        + lines[end_line:]  # 包含 END marker 及之后
    )

    return "\n".join(new_lines), None


def update_document(
    doc_path: Path,
    blocks: dict[str, RenderedBlock],
    dry_run: bool = False,
) -> UpdateResult:
    """更新文档中的受控块

    Args:
        doc_path: 文档路径
        blocks: 要更新的块 {block_name: RenderedBlock}
        dry_run: 是否只检查不写入

    Returns:
        UpdateResult
    """
    if not doc_path.exists():
        return UpdateResult(
            doc_path=str(doc_path),
            updated_blocks=[],
            missing_markers=[],
            unchanged_blocks=[],
            success=False,
            error_message=f"Document not found: {doc_path}",
        )

    try:
        content = doc_path.read_text(encoding="utf-8")
    except Exception as e:
        return UpdateResult(
            doc_path=str(doc_path),
            updated_blocks=[],
            missing_markers=[],
            unchanged_blocks=[],
            success=False,
            error_message=f"Failed to read document: {e}",
        )

    updated_blocks: list[str] = []
    missing_markers: list[str] = []
    unchanged_blocks: list[str] = []
    current_content = content

    for block_name, rendered_block in blocks.items():
        # 检查当前块内容
        existing_content, begin_line, end_line = extract_block_from_content(
            current_content, block_name
        )

        if existing_content is None:
            if begin_line == -1:
                # markers 不存在
                missing_markers.append(block_name)
                continue
            else:
                # 其他错误（重复 marker 等）
                missing_markers.append(block_name)
                continue

        # 比较内容（规范化换行符后比较）
        existing_normalized = existing_content.strip()
        new_normalized = rendered_block.content.strip()

        if existing_normalized == new_normalized:
            unchanged_blocks.append(block_name)
            continue

        # 执行替换
        new_content, error = replace_block_in_content(
            current_content,
            block_name,
            rendered_block.content,
        )

        if error:
            missing_markers.append(block_name)
            continue

        current_content = new_content  # type: ignore
        updated_blocks.append(block_name)

    # 如果有缺失的 markers，返回失败
    if missing_markers:
        return UpdateResult(
            doc_path=str(doc_path),
            updated_blocks=updated_blocks,
            missing_markers=missing_markers,
            unchanged_blocks=unchanged_blocks,
            success=False,
            error_message=f"Missing markers for blocks: {', '.join(missing_markers)}",
        )

    # 写入文件（如果不是 dry_run 且有更新）
    if updated_blocks and not dry_run:
        try:
            doc_path.write_text(current_content, encoding="utf-8")
        except Exception as e:
            return UpdateResult(
                doc_path=str(doc_path),
                updated_blocks=[],
                missing_markers=[],
                unchanged_blocks=unchanged_blocks,
                success=False,
                error_message=f"Failed to write document: {e}",
            )

    return UpdateResult(
        doc_path=str(doc_path),
        updated_blocks=updated_blocks,
        missing_markers=missing_markers,
        unchanged_blocks=unchanged_blocks,
        success=True,
    )


def generate_marker_insertion_hint(block_name: str) -> str:
    """生成 marker 插入提示"""
    begin_marker = MARKER_BEGIN_FMT.format(block_name=block_name)
    end_marker = MARKER_END_FMT.format(block_name=block_name)
    return f"""
To add markers for block '{block_name}', insert the following at the appropriate location in the document:

{begin_marker}
<!-- Content will be auto-generated here -->
{end_marker}
"""


# ============================================================================
# Main
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="渲染 workflow_contract.v1.json 到 Markdown 受控块",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--contract",
        type=str,
        default=DEFAULT_CONTRACT_PATH,
        help=f"Contract JSON 文件路径 (default: {DEFAULT_CONTRACT_PATH})",
    )
    parser.add_argument(
        "--target",
        type=str,
        choices=["contract", "coupling_map", "all"],
        default="all",
        help="渲染目标: contract (contract.md), coupling_map (coupling_map.md), all (全部)",
    )
    parser.add_argument(
        "--block",
        type=str,
        help="只渲染指定的块名称",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出",
    )
    parser.add_argument(
        "--with-markers",
        action="store_true",
        help="输出包含 markers 的完整块",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="更新文档中的受控块（就地写入）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="与 --write 配合使用，只检查不写入",
    )
    parser.add_argument(
        "--project-root",
        type=str,
        default=None,
        help="项目根目录（默认从脚本位置推断）",
    )

    args = parser.parse_args()

    # 确定项目根目录
    if args.project_root:
        project_root = Path(args.project_root)
    else:
        script_path = Path(__file__).resolve()
        project_root = script_path.parent.parent.parent
        if not (project_root / "scripts" / "ci").exists():
            project_root = Path.cwd()

    contract_path = project_root / args.contract

    renderer = WorkflowContractDocsRenderer(contract_path)
    if not renderer.load_contract():
        return 2

    # --write 模式：更新文档
    if args.write:
        return _handle_write_mode(args, project_root, renderer)

    # 渲染块（非 --write 模式）
    if args.block:
        block = renderer.render_block(args.block)
        if block is None:
            print(f"Error: Unknown block name: {args.block}", file=sys.stderr)
            return 1

        if args.json:
            output = {
                "name": block.name,
                "content": block.content,
                "begin_marker": block.begin_marker,
                "end_marker": block.end_marker,
            }
            print(json.dumps(output, indent=2, ensure_ascii=False))
        elif args.with_markers:
            print(block.full_block())
        else:
            print(block.content)

    elif args.target == "contract":
        blocks = renderer.render_contract_blocks()
        if args.json:
            output = {
                name: {
                    "content": block.content,
                    "begin_marker": block.begin_marker,
                    "end_marker": block.end_marker,
                }
                for name, block in blocks.items()
            }
            print(json.dumps(output, indent=2, ensure_ascii=False))
        else:
            for name, block in blocks.items():
                print(f"=== {name} ===")
                if args.with_markers:
                    print(block.full_block())
                else:
                    print(block.content)
                print()

    elif args.target == "coupling_map":
        blocks = renderer.render_coupling_map_blocks()
        if args.json:
            output = {
                name: {
                    "content": block.content,
                    "begin_marker": block.begin_marker,
                    "end_marker": block.end_marker,
                }
                for name, block in blocks.items()
            }
            print(json.dumps(output, indent=2, ensure_ascii=False))
        else:
            for name, block in blocks.items():
                print(f"=== {name} ===")
                if args.with_markers:
                    print(block.full_block())
                else:
                    print(block.content)
                print()

    else:  # all
        blocks = renderer.render_all_blocks()
        if args.json:
            output = {
                name: {
                    "content": block.content,
                    "begin_marker": block.begin_marker,
                    "end_marker": block.end_marker,
                }
                for name, block in blocks.items()
            }
            print(json.dumps(output, indent=2, ensure_ascii=False))
        else:
            for name, block in blocks.items():
                print(f"=== {name} ===")
                if args.with_markers:
                    print(block.full_block())
                else:
                    print(block.content)
                print()

    return 0


def _handle_write_mode(
    args: argparse.Namespace,
    project_root: Path,
    renderer: WorkflowContractDocsRenderer,
) -> int:
    """处理 --write 模式

    Args:
        args: 命令行参数
        project_root: 项目根目录
        renderer: 渲染器实例

    Returns:
        退出码
    """
    # 确定要更新的文档和块
    docs_to_update: list[tuple[Path, dict[str, RenderedBlock]]] = []

    if args.target in ("contract", "all"):
        contract_doc_path = project_root / DEFAULT_CONTRACT_DOC_PATH
        contract_blocks = renderer.render_contract_blocks()
        docs_to_update.append((contract_doc_path, contract_blocks))

    if args.target in ("coupling_map", "all"):
        coupling_map_doc_path = project_root / DEFAULT_COUPLING_MAP_DOC_PATH
        coupling_map_blocks = renderer.render_coupling_map_blocks()
        docs_to_update.append((coupling_map_doc_path, coupling_map_blocks))

    # 执行更新
    all_results: list[UpdateResult] = []
    has_error = False

    for doc_path, blocks in docs_to_update:
        result = update_document(doc_path, blocks, dry_run=args.dry_run)
        all_results.append(result)

        if not result.success:
            has_error = True

    # 输出结果
    if args.json:
        output = {
            "success": not has_error,
            "dry_run": args.dry_run,
            "results": [
                {
                    "doc_path": r.doc_path,
                    "success": r.success,
                    "updated_blocks": r.updated_blocks,
                    "unchanged_blocks": r.unchanged_blocks,
                    "missing_markers": r.missing_markers,
                    "error_message": r.error_message,
                }
                for r in all_results
            ],
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        mode_str = "(dry-run)" if args.dry_run else ""
        for result in all_results:
            print(f"\n{'='*60}")
            print(f"Document: {result.doc_path} {mode_str}")
            print(f"{'='*60}")

            if result.success:
                if result.updated_blocks:
                    print(f"Updated blocks: {', '.join(result.updated_blocks)}")
                if result.unchanged_blocks:
                    print(f"Unchanged blocks: {', '.join(result.unchanged_blocks)}")
                if not result.updated_blocks and not result.unchanged_blocks:
                    print("No blocks found to update.")
            else:
                print(f"ERROR: {result.error_message}", file=sys.stderr)
                if result.missing_markers:
                    print("\nMissing markers for the following blocks:", file=sys.stderr)
                    for block_name in result.missing_markers:
                        hint = generate_marker_insertion_hint(block_name)
                        print(hint, file=sys.stderr)

    return 1 if has_error else 0


if __name__ == "__main__":
    sys.exit(main())
