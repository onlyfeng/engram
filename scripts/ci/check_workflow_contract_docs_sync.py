#!/usr/bin/env python3
"""
Workflow Contract 与文档同步校验脚本

校验 workflow_contract.v1.json 中的关键元素是否在 contract.md 文档中有对应描述。

校验模式：
1. **受控块模式**（推荐）：当文档包含 markers 时，渲染期望块并逐字比对
2. **字符串匹配模式**（回退）：当文档无 markers 时，检查值是否在文档中出现

校验范围：
1. <workflow>.job_ids: 每个 workflow（ci/nightly）的 job id 在对应章节可被找到
2. <workflow>.job_names: 每个 workflow 的 job name 在对应章节可被找到
3. frozen_step_text.allowlist: 每个 frozen step 在冻结 Step 章节可被找到
4. frozen_job_names.allowlist: 每个 frozen job name 在 Frozen Job Names 章节可被找到
5. <workflow>.labels: 每个 label 在 PR Labels 章节可被找到

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
)
from scripts.ci.workflow_contract_common import discover_workflow_keys

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


class DocsSyncErrorTypes:
    """文档同步校验的 error_type 常量定义"""

    # 文件/解析错误
    CONTRACT_NOT_FOUND = "contract_not_found"
    CONTRACT_PARSE_ERROR = "contract_parse_error"
    DOC_NOT_FOUND = "doc_not_found"
    DOC_READ_ERROR = "doc_read_error"

    # 章节缺失错误
    WORKFLOW_SECTION_MISSING = "workflow_section_missing"
    FROZEN_STEP_SECTION_MISSING = "frozen_step_section_missing"
    FROZEN_JOB_NAMES_SECTION_MISSING = "frozen_job_names_section_missing"
    LABELS_SECTION_MISSING = "labels_section_missing"
    MAKE_TARGETS_SECTION_MISSING = "make_targets_section_missing"
    SEMVER_POLICY_SECTION_MISSING = "semver_policy_section_missing"

    # 内容缺失错误
    JOB_ID_NOT_IN_DOC = "job_id_not_in_doc"
    JOB_NAME_NOT_IN_DOC = "job_name_not_in_doc"
    FROZEN_STEP_NOT_IN_DOC = "frozen_step_not_in_doc"
    FROZEN_JOB_NAME_NOT_IN_DOC = "frozen_job_name_not_in_doc"
    LABEL_NOT_IN_DOC = "label_not_in_doc"
    VERSION_NOT_IN_DOC = "version_not_in_doc"
    MAKE_TARGET_NOT_IN_DOC = "make_target_not_in_doc"

    # 受控块错误（markers 模式）
    BLOCK_MARKER_MISSING = "block_marker_missing"
    BLOCK_MARKER_DUPLICATE = "block_marker_duplicate"
    BLOCK_MARKER_UNPAIRED = "block_marker_unpaired"
    BLOCK_CONTENT_MISMATCH = "block_content_mismatch"


# 导出所有 error_type 的集合（用于测试覆盖检查）
DOCS_SYNC_ERROR_TYPES = frozenset({
    DocsSyncErrorTypes.CONTRACT_NOT_FOUND,
    DocsSyncErrorTypes.CONTRACT_PARSE_ERROR,
    DocsSyncErrorTypes.DOC_NOT_FOUND,
    DocsSyncErrorTypes.DOC_READ_ERROR,
    DocsSyncErrorTypes.WORKFLOW_SECTION_MISSING,
    DocsSyncErrorTypes.FROZEN_STEP_SECTION_MISSING,
    DocsSyncErrorTypes.FROZEN_JOB_NAMES_SECTION_MISSING,
    DocsSyncErrorTypes.LABELS_SECTION_MISSING,
    DocsSyncErrorTypes.MAKE_TARGETS_SECTION_MISSING,
    DocsSyncErrorTypes.SEMVER_POLICY_SECTION_MISSING,
    DocsSyncErrorTypes.JOB_ID_NOT_IN_DOC,
    DocsSyncErrorTypes.JOB_NAME_NOT_IN_DOC,
    DocsSyncErrorTypes.FROZEN_STEP_NOT_IN_DOC,
    DocsSyncErrorTypes.FROZEN_JOB_NAME_NOT_IN_DOC,
    DocsSyncErrorTypes.LABEL_NOT_IN_DOC,
    DocsSyncErrorTypes.VERSION_NOT_IN_DOC,
    DocsSyncErrorTypes.MAKE_TARGET_NOT_IN_DOC,
    DocsSyncErrorTypes.BLOCK_MARKER_MISSING,
    DocsSyncErrorTypes.BLOCK_MARKER_DUPLICATE,
    DocsSyncErrorTypes.BLOCK_MARKER_UNPAIRED,
    DocsSyncErrorTypes.BLOCK_CONTENT_MISMATCH,
})


# ============================================================================
# Constants
# ============================================================================

DEFAULT_CONTRACT_PATH = "scripts/ci/workflow_contract.v1.json"
DEFAULT_DOC_PATH = "docs/ci_nightly_workflow_refactor/contract.md"

# 文档中各 workflow 章节的锚点关键字（用于章节定位和切片）
# 检查 job_ids/job_names 时，只在对应 workflow 章节内匹配
# 注意：锚点必须足够精确，避免匹配到其他章节（如关键文件清单中的路径引用）
#
# ============================================================================
# Phase 2 扩展点：纳入 release.yml
# ============================================================================
#
# 当前 release 的锚点指向 "Phase 2 预留" 章节。当 release.yml 正式纳入合约时：
#
# 纳入 release.yml 时的同步 Checklist：
#
# 1. [contract.md] 更新 2.3 节为正式章节：
#    - 标题改为 "### 2.3 Release Workflow (`release.yml`)"
#    - 添加 Job ID / Job Name 对照表
#
# 2. [本脚本] 更新下方 release 锚点（如需）：
#    - 确保锚点与 contract.md 中的实际章节标题匹配
#    - 示例: ["### 2.3 Release Workflow", "Release Workflow (`release.yml`)"]
#
# 3. [workflow_contract.v1.json] 添加 release 字段定义
#
# 4. [验证] 运行以下命令确认 release 文档同步正确：
#    python scripts/ci/check_workflow_contract_docs_sync.py --verbose
#
# 详见 contract.md 2.4.3 节迁移 Checklist
# ============================================================================
WORKFLOW_DOC_ANCHORS = {
    "ci": ["### 2.1 CI Workflow", "CI Workflow (`ci.yml`)"],
    "nightly": ["### 2.2 Nightly Workflow", "Nightly Workflow (`nightly.yml`)"],
    "release": ["### 2.3 Release Workflow", "Release Workflow (`release.yml`)"],
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

# Frozen Job Names 章节的锚点关键字（用于 frozen_job_names.allowlist 匹配）
# frozen job name 必须在此章节内出现才算有效
FROZEN_JOB_DOC_ANCHORS = [
    "Frozen Job Names",  # 文档 5.1 节标题
    "frozen_job_names.allowlist",  # 合约字段引用
    "冻结的 Job Names",  # 中文标题（如有）
]

# PR Labels 章节的锚点关键字（用于 ci.labels / nightly.labels 匹配）
# labels 必须在此章节内出现才算有效
LABELS_DOC_ANCHORS = [
    "PR Label 列表与语义",  # 文档 3 节标题
    "PR Labels",  # 英文简称
    "Label 列表",  # 中文简称
    "ci.labels",  # 合约字段引用
]


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class SyncError:
    """同步错误"""

    error_type: str
    category: str  # "job_id", "job_name", "frozen_step", "block"
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
    checked_job_names: list[str] = field(default_factory=list)
    checked_frozen_steps: list[str] = field(default_factory=list)
    checked_frozen_job_names: list[str] = field(default_factory=list)
    checked_labels: list[str] = field(default_factory=list)
    checked_version: str = ""
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

    def extract_frozen_job_names_section(self) -> str | None:
        """提取 Frozen Job Names 章节的文本

        Returns:
            Frozen Job Names 章节的文本，如果找不到则返回 None
        """
        return self.extract_section_text(FROZEN_JOB_DOC_ANCHORS)

    def extract_labels_section(self) -> str | None:
        """提取 PR Labels 章节的文本

        Returns:
            PR Labels 章节的文本，如果找不到则返回 None
        """
        return self.extract_section_text(LABELS_DOC_ANCHORS)

    def check_frozen_job_names(self) -> None:
        """校验 frozen_job_names.allowlist

        使用章节切片确保 frozen job name 出现在 Frozen Job Names 章节中。
        """
        frozen_job_names = self.contract.get("frozen_job_names", {})
        allowlist = frozen_job_names.get("allowlist", [])

        if not allowlist:
            self.result.add_warning("No frozen_job_names.allowlist found in contract")
            return

        # 提取 Frozen Job Names 章节文本
        section_text = self.extract_frozen_job_names_section()

        if section_text is None:
            self.result.add_error(
                SyncError(
                    error_type="frozen_job_names_section_missing",
                    category="frozen_job_name",
                    value="frozen_job_names",
                    message=(
                        "Documentation missing 'Frozen Job Names' section. "
                        "Please add section 5.1 with title containing 'Frozen Job Names'."
                    ),
                )
            )
            # 章节不存在时，所有 frozen job name 都会报错
            for job_name in allowlist:
                self.result.checked_frozen_job_names.append(job_name)
                self.result.add_error(
                    SyncError(
                        error_type="frozen_job_name_not_in_doc",
                        category="frozen_job_name",
                        value=job_name,
                        message=f"Frozen job name '{job_name}' not found in documentation (section missing)",
                    )
                )
            return

        for job_name in allowlist:
            self.result.checked_frozen_job_names.append(job_name)
            # 使用章节切片匹配，而不是全文匹配
            if not self.check_value_in_section(job_name, section_text):
                self.result.add_error(
                    SyncError(
                        error_type="frozen_job_name_not_in_doc",
                        category="frozen_job_name",
                        value=job_name,
                        message=f"Frozen job name '{job_name}' not found in 'Frozen Job Names' section (5.1)",
                    )
                )
            elif self.verbose:
                print(f"  [OK] frozen_job_name: {job_name}")

    def check_labels(self) -> None:
        """校验所有 workflow 的 labels

        遍历动态发现的每个 workflow，检查其 labels 是否在 PR Labels 章节内出现。
        使用章节切片确保 label 出现在正确的章节中。
        """
        # 提取 PR Labels 章节文本
        section_text = self.extract_labels_section()

        all_labels: list[tuple[str, str]] = []  # (label, workflow_key)

        for workflow_key in self.workflow_keys:
            workflow_config = self.contract.get(workflow_key, {})
            labels = workflow_config.get("labels", [])

            for label in labels:
                all_labels.append((label, workflow_key))

        if not all_labels:
            # labels 是可选的，不产生警告
            return

        if section_text is None:
            self.result.add_error(
                SyncError(
                    error_type="labels_section_missing",
                    category="label",
                    value="PR Labels",
                    message=(
                        "Documentation missing 'PR Label' section. "
                        "Please add section 3 with title containing 'PR Label 列表与语义' or 'PR Labels'."
                    ),
                )
            )
            # 章节不存在时，所有 labels 都会报错
            for label, workflow_key in all_labels:
                self.result.checked_labels.append(label)
                self.result.add_error(
                    SyncError(
                        error_type="label_not_in_doc",
                        category="label",
                        value=label,
                        message=f"Label '{label}' ({workflow_key}) not found in documentation (section missing)",
                    )
                )
            return

        for label, workflow_key in all_labels:
            self.result.checked_labels.append(label)
            # 使用章节切片匹配，而不是全文匹配
            if not self.check_value_in_section(label, section_text):
                self.result.add_error(
                    SyncError(
                        error_type="label_not_in_doc",
                        category="label",
                        value=label,
                        message=f"Label '{label}' ({workflow_key}) not found in 'PR Labels' section (section 3)",
                    )
                )
            elif self.verbose:
                print(f"  [OK] label ({workflow_key}): {label}")

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

    # ========================================================================
    # 受控块检查（Marker Mode）
    # ========================================================================

    def has_any_markers(self) -> bool:
        """检查文档中是否存在任何受控块 markers"""
        markers = find_all_markers(self.doc_content)
        return len(markers) > 0

    def check_controlled_blocks(self) -> bool:
        """检查受控块内容是否与渲染结果一致

        Returns:
            True 如果使用了 marker 模式（找到了 markers），False 否则
        """
        # 检查是否有 markers
        markers = find_all_markers(self.doc_content)
        if not markers:
            return False

        self.result.block_mode_used = True

        if self.verbose:
            print("\nUsing marker mode for controlled blocks...")

        # 创建渲染器
        renderer = WorkflowContractDocsRenderer(self.contract_path)
        if not renderer.load_contract():
            return True  # 仍然标记为使用了 marker 模式

        # 只检查 contract.md 相关的块
        rendered_blocks = renderer.render_contract_blocks()

        # 检查 marker 完整性
        marker_map: dict[str, list[tuple[str, int]]] = {}  # block_name -> [(type, line)]
        for block_name, marker_type, line_num in markers:
            if block_name not in marker_map:
                marker_map[block_name] = []
            marker_map[block_name].append((marker_type, line_num))

        # 检查每个预期的块
        for block_name, rendered_block in rendered_blocks.items():
            self.result.checked_blocks.append(block_name)

            # 检查 markers 是否存在
            if block_name not in marker_map:
                # Marker 缺失 - 预期块必须存在 markers
                self.result.add_error(
                    SyncError(
                        error_type=DocsSyncErrorTypes.BLOCK_MARKER_MISSING,
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
                        error_type=DocsSyncErrorTypes.BLOCK_MARKER_DUPLICATE,
                        category="block",
                        value=block_name,
                        message=f"Duplicate BEGIN marker for block '{block_name}' at lines {[m[1]+1 for m in begin_markers]}",
                    )
                )
                continue

            if len(end_markers) > 1:
                self.result.add_error(
                    SyncError(
                        error_type=DocsSyncErrorTypes.BLOCK_MARKER_DUPLICATE,
                        category="block",
                        value=block_name,
                        message=f"Duplicate END marker for block '{block_name}' at lines {[m[1]+1 for m in end_markers]}",
                    )
                )
                continue

            if len(begin_markers) == 0:
                self.result.add_error(
                    SyncError(
                        error_type=DocsSyncErrorTypes.BLOCK_MARKER_UNPAIRED,
                        category="block",
                        value=block_name,
                        message=f"Missing BEGIN marker for block '{block_name}'",
                    )
                )
                continue

            if len(end_markers) == 0:
                self.result.add_error(
                    SyncError(
                        error_type=DocsSyncErrorTypes.BLOCK_MARKER_UNPAIRED,
                        category="block",
                        value=block_name,
                        message=f"Missing END marker for block '{block_name}'",
                    )
                )
                continue

            # 提取实际块内容
            actual_content, begin_line, end_line = extract_block_from_content(
                self.doc_content, block_name
            )

            if actual_content is None:
                if begin_line == -2:
                    self.result.add_error(
                        SyncError(
                            error_type=DocsSyncErrorTypes.BLOCK_MARKER_DUPLICATE,
                            category="block",
                            value=block_name,
                            message=f"Duplicate BEGIN marker for block '{block_name}'",
                        )
                    )
                elif begin_line == -3:
                    self.result.add_error(
                        SyncError(
                            error_type=DocsSyncErrorTypes.BLOCK_MARKER_DUPLICATE,
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
                        error_type=DocsSyncErrorTypes.BLOCK_CONTENT_MISMATCH,
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
        1. 加载 contract 和文档
        2. 尝试使用 marker 模式检查受控块
        3. 无论是否使用 marker 模式，都执行传统的字符串匹配检查
           （传统检查作为基线保障，确保关键内容存在）
        """
        if self.verbose:
            print(f"Loading contract: {self.contract_path}")

        if not self.load_contract():
            return self.result

        if self.verbose:
            print(f"Loading documentation: {self.doc_path}")

        if not self.load_doc():
            return self.result

        # 尝试使用 marker 模式检查受控块
        if self.verbose:
            print("\nChecking controlled blocks (marker mode)...")
        self.check_controlled_blocks()

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
            print("\nChecking frozen_job_names.allowlist...")
        self.check_frozen_job_names()

        if self.verbose:
            print(f"\nChecking labels across workflows: {self.workflow_keys}...")
        self.check_labels()

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
    lines.append(f"  - Block mode used: {result.block_mode_used}")
    lines.append(f"  - Checked blocks: {len(result.checked_blocks)}")
    lines.append(f"  - Checked version: {result.checked_version or '(not checked)'}")
    lines.append(f"  - Checked job_ids: {len(result.checked_job_ids)}")
    lines.append(f"  - Checked job_names: {len(result.checked_job_names)}")
    lines.append(f"  - Checked frozen_steps: {len(result.checked_frozen_steps)}")
    lines.append(f"  - Checked frozen_job_names: {len(result.checked_frozen_job_names)}")
    lines.append(f"  - Checked labels: {len(result.checked_labels)}")
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
        "checked_version": result.checked_version,
        "checked_job_ids": result.checked_job_ids,
        "checked_job_names": result.checked_job_names,
        "checked_frozen_steps": result.checked_frozen_steps,
        "checked_frozen_job_names": result.checked_frozen_job_names,
        "checked_labels": result.checked_labels,
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
