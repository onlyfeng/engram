#!/usr/bin/env python3
"""
Workflow Contract 与文档同步校验脚本

校验 workflow_contract.v1.json 中的关键元素是否在 contract.md 文档中有对应描述。

校验范围：
1. <workflow>.job_ids: 每个 workflow（ci/nightly）的 job id 在文档中可被找到
2. <workflow>.job_names: 每个 workflow 的 job name 在文档中可被找到
3. frozen_step_text.allowlist: 每个 frozen step 在文档中可被找到

使用方式：
    python scripts/ci/check_workflow_contract_docs_sync.py
    python scripts/ci/check_workflow_contract_docs_sync.py --json
    python scripts/ci/check_workflow_contract_docs_sync.py --verbose

退出码：
    0: 校验通过
    1: 校验失败（存在未同步项）
    2: 文件读取/解析错误
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ============================================================================
# Constants
# ============================================================================

DEFAULT_CONTRACT_PATH = "scripts/ci/workflow_contract.v1.json"
DEFAULT_DOC_PATH = "docs/ci_nightly_workflow_refactor/contract.md"

# Metadata/legacy 字段排除列表 - 这些 key 不是 workflow 定义
METADATA_KEYS = frozenset(
    [
        "$schema",
        "version",
        "description",
        "last_updated",
        "make",
        "frozen_step_text",
        "frozen_job_names",
        # _changelog_* 和 _*_note 等下划线前缀字段通过前缀检查排除
    ]
)

# 文档中各 workflow 章节的锚点关键字（用于章节定位和切片）
# 检查 job_ids/job_names 时，只在对应 workflow 章节内匹配
WORKFLOW_DOC_ANCHORS = {
    "ci": ["ci.yml", "CI Workflow"],
    "nightly": ["nightly.yml", "Nightly Workflow"],
    "release": ["release.yml", "Release Workflow"],
}

# 冻结 Step 章节的锚点关键字（用于 frozen_step_text 匹配）
# frozen step 必须在此章节内出现才算有效
# 匹配文档中第 5 节 "禁止回归"的 Step 文本范围 或第 4 节 冻结的 Step 文本范围
FROZEN_STEP_DOC_ANCHORS = [
    '"禁止回归"的 Step',  # 文档 v1.x 章节标题: ## 5. "禁止回归"的 Step 文本范围
    "冻结的 Step 文本范围",  # 文档 v3.x 章节标题: ## 4. 冻结的 Step 文本范围
    "冻结的 Step 文本",  # 测试用简短格式: ## 冻结的 Step 文本
    "Frozen Step",
    "frozen_step_text.allowlist",
]

# SemVer Policy 章节的锚点关键字
# 文档必须包含版本策略说明，用于指导 workflow/contract 变更时的版本处理
SEMVER_POLICY_DOC_ANCHORS = [
    "SemVer Policy",  # 英文标题
    "版本策略",  # 中文标题
    "SemVer",  # 简短关键字
]


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class SyncError:
    """同步错误"""

    error_type: str
    category: str  # "job_id", "job_name", "frozen_step"
    value: str
    message: str


@dataclass
class SyncResult:
    """同步校验结果"""

    success: bool = True
    errors: list[SyncError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked_job_ids: list[str] = field(default_factory=list)
    checked_job_names: list[str] = field(default_factory=list)
    checked_frozen_steps: list[str] = field(default_factory=list)
    checked_version: str = ""
    checked_make_targets: list[str] = field(default_factory=list)

    def add_error(self, error: SyncError) -> None:
        """添加错误"""
        self.errors.append(error)
        self.success = False

    def add_warning(self, warning: str) -> None:
        """添加警告"""
        self.warnings.append(warning)


# ============================================================================
# Helper Functions
# ============================================================================


def discover_workflow_keys(contract: dict[str, Any]) -> list[str]:
    """动态发现 contract 中的 workflow 定义 key

    通过扫描顶层 dict，筛选符合 workflow 结构特征的 key：
    1. value 是 dict 类型
    2. value 包含 "file" 字段（workflow 定义的必需字段）
    3. key 不在 METADATA_KEYS 排除列表中
    4. key 不以下划线开头（排除 _changelog_*, _*_note 等注释字段）

    Args:
        contract: 加载的 contract JSON dict

    Returns:
        发现的 workflow key 列表，按字母序排序
    """
    workflow_keys: list[str] = []

    for key, value in contract.items():
        # 排除下划线前缀字段（changelog, notes 等）
        if key.startswith("_"):
            continue

        # 排除已知 metadata 字段
        if key in METADATA_KEYS:
            continue

        # 检查是否符合 workflow 结构特征：dict 且包含 "file" 字段
        if isinstance(value, dict) and "file" in value:
            workflow_keys.append(key)

    return sorted(workflow_keys)


# ============================================================================
# Core Logic
# ============================================================================


class WorkflowContractDocsSyncChecker:
    """Workflow Contract 与文档同步校验器"""

    def __init__(
        self,
        contract_path: Path,
        doc_path: Path,
        verbose: bool = False,
    ) -> None:
        self.contract_path = contract_path
        self.doc_path = doc_path
        self.verbose = verbose
        self.result = SyncResult()
        self.contract: dict[str, Any] = {}
        self.doc_content: str = ""
        self.workflow_keys: list[str] = []  # 动态发现的 workflow keys

    def load_contract(self) -> bool:
        """加载 contract JSON 文件"""
        if not self.contract_path.exists():
            self.result.add_error(
                SyncError(
                    error_type="contract_not_found",
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
                    error_type="contract_parse_error",
                    category="file",
                    value=str(self.contract_path),
                    message=f"Failed to parse contract JSON: {e}",
                )
            )
            return False

    def load_doc(self) -> bool:
        """加载文档文件"""
        if not self.doc_path.exists():
            self.result.add_error(
                SyncError(
                    error_type="doc_not_found",
                    category="file",
                    value=str(self.doc_path),
                    message=f"Documentation file not found: {self.doc_path}",
                )
            )
            return False

        try:
            with open(self.doc_path, "r", encoding="utf-8") as f:
                self.doc_content = f.read()
            return True
        except Exception as e:
            self.result.add_error(
                SyncError(
                    error_type="doc_read_error",
                    category="file",
                    value=str(self.doc_path),
                    message=f"Failed to read documentation file: {e}",
                )
            )
            return False

    def check_value_in_doc(self, value: str) -> bool:
        """检查值是否在文档中可被找到（字符串包含匹配）"""
        return value in self.doc_content

    def check_value_in_section(self, value: str, section_text: str) -> bool:
        """检查值是否在指定章节文本内可被找到"""
        return value in section_text

    def extract_section_text(
        self, anchors: list[str], *, use_subsection_boundary: bool = False
    ) -> str | None:
        """提取从 anchor 首次出现位置到下一个标题的章节文本

        Args:
            anchors: 锚点关键字列表，按顺序尝试匹配
            use_subsection_boundary: 是否使用子章节边界（### 或 ##），
                                     默认为 False（只使用 ## 边界）

        Returns:
            章节文本，如果找不到 anchor 则返回 None
        """
        # 找到 anchor 首次出现的位置
        anchor_pos = -1
        for anchor in anchors:
            pos = self.doc_content.find(anchor)
            if pos != -1:
                if anchor_pos == -1 or pos < anchor_pos:
                    anchor_pos = pos
                break  # 使用第一个匹配的 anchor

        if anchor_pos == -1:
            return None

        # 找到 anchor 所在行的行首，确保我们从完整的行开始
        line_start = self.doc_content.rfind("\n", 0, anchor_pos)
        if line_start == -1:
            line_start = 0
        else:
            line_start += 1  # 跳过换行符

        # 确定章节结束位置
        if use_subsection_boundary:
            # 对于 workflow 章节（在 ### 子标题下），找下一个 ### 或 ## 标题
            next_h3_pos = self.doc_content.find("\n### ", anchor_pos)
            next_h2_pos = self.doc_content.find("\n## ", anchor_pos)

            # 取最近的标题位置
            if next_h3_pos == -1 and next_h2_pos == -1:
                next_section_pos = -1
            elif next_h3_pos == -1:
                next_section_pos = next_h2_pos
            elif next_h2_pos == -1:
                next_section_pos = next_h3_pos
            else:
                next_section_pos = min(next_h3_pos, next_h2_pos)
        else:
            # 默认行为：只找下一个 ## 标题（二级标题）
            next_section_pos = self.doc_content.find("\n## ", anchor_pos)

        if next_section_pos == -1:
            # 没有下一个标题，取到文档结尾
            return self.doc_content[line_start:]
        else:
            return self.doc_content[line_start:next_section_pos]

    def extract_workflow_section(self, workflow_key: str) -> str | None:
        """提取指定 workflow 的章节文本

        使用子章节边界（### 或 ##），因为 workflow 章节通常在 ### 子标题下。

        Args:
            workflow_key: workflow 键名（如 "ci", "nightly"）

        Returns:
            该 workflow 章节的文本，如果找不到则返回 None
        """
        anchors = WORKFLOW_DOC_ANCHORS.get(workflow_key, [])
        if not anchors:
            return None
        return self.extract_section_text(anchors, use_subsection_boundary=True)

    def extract_frozen_step_section(self) -> str | None:
        """提取冻结 Step 章节的文本

        Returns:
            冻结 Step 章节的文本，如果找不到则返回 None
        """
        return self.extract_section_text(FROZEN_STEP_DOC_ANCHORS)

    def check_workflow_section_exists(self, workflow_key: str) -> bool:
        """检查文档中是否存在指定 workflow 的章节

        通过检查预定义的锚点关键字判断文档中是否有对应 workflow 的章节描述。
        """
        anchors = WORKFLOW_DOC_ANCHORS.get(workflow_key, [])
        for anchor in anchors:
            if anchor in self.doc_content:
                return True
        return False

    def check_job_ids(self) -> None:
        """校验所有 workflow 的 job_ids

        遍历动态发现的每个 workflow，检查其 job_ids 是否在对应章节内出现。
        使用章节切片确保 job_id 出现在正确的 workflow 章节中。
        """
        for workflow_key in self.workflow_keys:
            workflow_config = self.contract.get(workflow_key, {})
            job_ids = workflow_config.get("job_ids", [])

            if not job_ids:
                self.result.add_warning(f"No job_ids found in contract.{workflow_key}")
                continue

            # 提取 workflow 章节文本
            section_text = self.extract_workflow_section(workflow_key)

            # 检查文档中是否有该 workflow 的章节
            if section_text is None:
                self.result.add_error(
                    SyncError(
                        error_type="workflow_section_missing",
                        category="workflow",
                        value=workflow_key,
                        message=f"Documentation missing section for workflow '{workflow_key}'",
                    )
                )
                # 章节不存在时，所有 job_id 都会报错
                for job_id in job_ids:
                    self.result.checked_job_ids.append(job_id)
                    self.result.add_error(
                        SyncError(
                            error_type="job_id_not_in_doc",
                            category="job_id",
                            value=job_id,
                            message=f"Job ID '{job_id}' ({workflow_key}) not found in documentation (section missing)",
                        )
                    )
                continue

            for job_id in job_ids:
                self.result.checked_job_ids.append(job_id)
                # 使用章节切片匹配，而不是全文匹配
                if not self.check_value_in_section(job_id, section_text):
                    self.result.add_error(
                        SyncError(
                            error_type="job_id_not_in_doc",
                            category="job_id",
                            value=job_id,
                            message=f"Job ID '{job_id}' ({workflow_key}) not found in its workflow section",
                        )
                    )
                elif self.verbose:
                    print(f"  [OK] job_id ({workflow_key}): {job_id}")

    def check_job_names(self) -> None:
        """校验所有 workflow 的 job_names

        遍历动态发现的每个 workflow，检查其 job_names 是否在对应章节内出现。
        使用章节切片确保 job_name 出现在正确的 workflow 章节中。
        """
        for workflow_key in self.workflow_keys:
            workflow_config = self.contract.get(workflow_key, {})
            job_names = workflow_config.get("job_names", [])

            if not job_names:
                self.result.add_warning(f"No job_names found in contract.{workflow_key}")
                continue

            # 提取 workflow 章节文本
            section_text = self.extract_workflow_section(workflow_key)

            # 如果章节不存在，job_ids 检查已经报错了，这里跳过
            if section_text is None:
                for job_name in job_names:
                    self.result.checked_job_names.append(job_name)
                    self.result.add_error(
                        SyncError(
                            error_type="job_name_not_in_doc",
                            category="job_name",
                            value=job_name,
                            message=f"Job name '{job_name}' ({workflow_key}) not found in documentation (section missing)",
                        )
                    )
                continue

            for job_name in job_names:
                self.result.checked_job_names.append(job_name)
                # 使用章节切片匹配，而不是全文匹配
                if not self.check_value_in_section(job_name, section_text):
                    self.result.add_error(
                        SyncError(
                            error_type="job_name_not_in_doc",
                            category="job_name",
                            value=job_name,
                            message=f"Job name '{job_name}' ({workflow_key}) not found in its workflow section",
                        )
                    )
                elif self.verbose:
                    print(f"  [OK] job_name ({workflow_key}): {job_name}")

    def check_frozen_steps(self) -> None:
        """校验 frozen_step_text.allowlist

        使用章节切片确保 frozen step 出现在冻结 Step 章节中。
        """
        frozen_step_text = self.contract.get("frozen_step_text", {})
        allowlist = frozen_step_text.get("allowlist", [])

        if not allowlist:
            self.result.add_warning("No frozen_step_text.allowlist found in contract")
            return

        # 提取冻结 Step 章节文本
        section_text = self.extract_frozen_step_section()

        if section_text is None:
            self.result.add_error(
                SyncError(
                    error_type="frozen_step_section_missing",
                    category="frozen_step",
                    value="frozen_step_text",
                    message="Documentation missing section for frozen steps",
                )
            )
            # 章节不存在时，所有 frozen step 都会报错
            for step in allowlist:
                self.result.checked_frozen_steps.append(step)
                self.result.add_error(
                    SyncError(
                        error_type="frozen_step_not_in_doc",
                        category="frozen_step",
                        value=step,
                        message=f"Frozen step '{step}' not found in documentation (section missing)",
                    )
                )
            return

        for step in allowlist:
            self.result.checked_frozen_steps.append(step)
            # 使用章节切片匹配，而不是全文匹配
            if not self.check_value_in_section(step, section_text):
                self.result.add_error(
                    SyncError(
                        error_type="frozen_step_not_in_doc",
                        category="frozen_step",
                        value=step,
                        message=f"Frozen step '{step}' not found in frozen steps section",
                    )
                )
            elif self.verbose:
                print(f"  [OK] frozen_step: {step}")

    def check_version(self) -> None:
        """校验 version 字段是否在文档中

        文档应包含 contract 的 version 字符串，确保文档与 JSON 版本一致。
        """
        version = self.contract.get("version", "")

        if not version:
            self.result.add_warning("No version found in contract")
            return

        self.result.checked_version = version
        if not self.check_value_in_doc(version):
            self.result.add_error(
                SyncError(
                    error_type="version_not_in_doc",
                    category="version",
                    value=version,
                    message=f"Contract version '{version}' not found in documentation",
                )
            )
        elif self.verbose:
            print(f"  [OK] version: {version}")

    def check_make_targets(self) -> None:
        """校验 make.targets_required 列表

        文档应包含：
        1. 关于 make.targets_required 的说明段落（标题/关键字）
        2. 每个 required target 名称
        """
        make_config = self.contract.get("make", {})
        targets_required = make_config.get("targets_required", [])

        # 检查文档是否包含 make.targets_required 的说明
        # 需要包含 "targets_required" 关键字或 "Make Targets" 标题
        has_make_section = (
            "targets_required" in self.doc_content
            or "Make Targets" in self.doc_content
            or "make targets" in self.doc_content.lower()
        )

        if not has_make_section:
            self.result.add_error(
                SyncError(
                    error_type="make_targets_section_missing",
                    category="make",
                    value="make.targets_required",
                    message="Documentation missing section about make.targets_required",
                )
            )

        if not targets_required:
            self.result.add_warning("No make.targets_required found in contract")
            return

        for target in targets_required:
            self.result.checked_make_targets.append(target)
            if not self.check_value_in_doc(target):
                self.result.add_error(
                    SyncError(
                        error_type="make_target_not_in_doc",
                        category="make_target",
                        value=target,
                        message=f"Make target '{target}' not found in documentation",
                    )
                )
            elif self.verbose:
                print(f"  [OK] make_target: {target}")

    def check_semver_policy_section(self) -> None:
        """校验文档是否包含 SemVer Policy / 版本策略章节

        文档应包含版本策略说明，用于指导 workflow/contract 变更时的版本处理。
        至少需要包含 SEMVER_POLICY_DOC_ANCHORS 中的一个关键字。
        """
        has_semver_section = False
        for anchor in SEMVER_POLICY_DOC_ANCHORS:
            if anchor in self.doc_content:
                has_semver_section = True
                if self.verbose:
                    print(f"  [OK] semver_policy: found anchor '{anchor}'")
                break

        if not has_semver_section:
            self.result.add_error(
                SyncError(
                    error_type="semver_policy_section_missing",
                    category="doc_structure",
                    value="SemVer Policy / 版本策略",
                    message=(
                        "Documentation missing SemVer Policy section. "
                        "Please add a section with 'SemVer Policy' or '版本策略' in the title. "
                        "See contract.md section 10 for reference."
                    ),
                )
            )

    def check(self) -> SyncResult:
        """执行完整校验"""
        if self.verbose:
            print(f"Loading contract: {self.contract_path}")

        if not self.load_contract():
            return self.result

        if self.verbose:
            print(f"Loading documentation: {self.doc_path}")

        if not self.load_doc():
            return self.result

        if self.verbose:
            print("\nChecking version...")
        self.check_version()

        if self.verbose:
            print(f"\nChecking job_ids across workflows: {self.workflow_keys}...")
        self.check_job_ids()

        if self.verbose:
            print(f"\nChecking job_names across workflows: {self.workflow_keys}...")
        self.check_job_names()

        if self.verbose:
            print("\nChecking frozen_step_text.allowlist...")
        self.check_frozen_steps()

        if self.verbose:
            print("\nChecking make.targets_required...")
        self.check_make_targets()

        if self.verbose:
            print("\nChecking SemVer Policy section...")
        self.check_semver_policy_section()

        return self.result


# ============================================================================
# Output Formatting
# ============================================================================


def format_text_output(result: SyncResult) -> str:
    """格式化文本输出"""
    lines: list[str] = []

    if result.success:
        lines.append("=" * 60)
        lines.append("Workflow Contract Docs Sync Check: PASSED")
        lines.append("=" * 60)
    else:
        lines.append("=" * 60)
        lines.append("Workflow Contract Docs Sync Check: FAILED")
        lines.append("=" * 60)

    lines.append("")
    lines.append("Summary:")
    lines.append(f"  - Checked version: {result.checked_version or '(not checked)'}")
    lines.append(f"  - Checked job_ids: {len(result.checked_job_ids)}")
    lines.append(f"  - Checked job_names: {len(result.checked_job_names)}")
    lines.append(f"  - Checked frozen_steps: {len(result.checked_frozen_steps)}")
    lines.append(f"  - Checked make_targets: {len(result.checked_make_targets)}")
    lines.append(f"  - Errors: {len(result.errors)}")
    lines.append(f"  - Warnings: {len(result.warnings)}")

    if result.errors:
        lines.append("")
        lines.append("ERRORS:")
        for error in result.errors:
            lines.append(f"  [{error.error_type}] {error.category}: {error.value}")
            lines.append(f"    {error.message}")

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
        "error_count": len(result.errors),
        "warning_count": len(result.warnings),
        "checked_version": result.checked_version,
        "checked_job_ids": result.checked_job_ids,
        "checked_job_names": result.checked_job_names,
        "checked_frozen_steps": result.checked_frozen_steps,
        "checked_make_targets": result.checked_make_targets,
        "errors": [
            {
                "error_type": e.error_type,
                "category": e.category,
                "value": e.value,
                "message": e.message,
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
        description="校验 workflow_contract.v1.json 与 contract.md 的同步一致性",
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
        # scripts/ci/check_workflow_contract_docs_sync.py -> project_root
        project_root = script_path.parent.parent.parent
        if not (project_root / "scripts" / "ci").exists():
            # 回退到当前工作目录
            project_root = Path.cwd()

    contract_path = project_root / args.contract
    doc_path = project_root / args.doc

    if args.verbose and not args.json:
        print(f"Project root: {project_root}")
        print(f"Contract path: {contract_path}")
        print(f"Doc path: {doc_path}")
        print()

    checker = WorkflowContractDocsSyncChecker(
        contract_path=contract_path,
        doc_path=doc_path,
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
