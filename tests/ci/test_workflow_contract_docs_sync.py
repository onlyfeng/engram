#!/usr/bin/env python3
"""
tests/ci/test_workflow_contract_docs_sync.py

单元测试：check_workflow_contract_docs_sync.py 的新增检查功能

测试范围：
1. frozen_job_names.allowlist 检查：验证 frozen job name 必须出现在文档的 Frozen Job Names 章节
2. labels 检查：验证 ci.labels / nightly.labels 必须出现在文档的 PR Labels 章节
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

# 导入被测模块
from scripts.ci.check_workflow_contract_docs_sync import (
    FROZEN_JOB_DOC_ANCHORS,
    LABELS_DOC_ANCHORS,
    WorkflowContractDocsSyncChecker,
)

# ============================================================================
# Fixtures
# ============================================================================


def create_temp_files(contract_data: dict[str, Any], doc_content: str) -> tuple[Path, Path]:
    """创建临时 contract JSON 和 doc Markdown 文件"""
    # 创建临时目录
    temp_dir = Path(tempfile.mkdtemp())

    # 写入 contract JSON
    contract_path = temp_dir / "workflow_contract.v1.json"
    with open(contract_path, "w", encoding="utf-8") as f:
        json.dump(contract_data, f, indent=2)

    # 写入 doc Markdown
    doc_path = temp_dir / "contract.md"
    with open(doc_path, "w", encoding="utf-8") as f:
        f.write(doc_content)

    return contract_path, doc_path


# ============================================================================
# Test: frozen_job_names.allowlist 检查
# ============================================================================


class TestFrozenJobNamesCheck:
    """测试 frozen_job_names.allowlist 检查"""

    def test_frozen_job_names_all_present_in_section(self) -> None:
        """当所有 frozen job names 都出现在 Frozen Job Names 章节时，应通过"""
        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": [], "job_names": []},
            "frozen_job_names": {
                "allowlist": [
                    "Test (Python ${{ matrix.python-version }})",
                    "Lint",
                ]
            },
        }
        doc = """
# Workflow Contract

## 5. "禁止回归"的 Step 文本范围

### 5.1 Frozen Job Names

| Job Name | 原因 |
|----------|------|
| `Test (Python ${{ matrix.python-version }})` | Required Check |
| `Lint` | Required Check |

## 6. 其他章节
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # 验证无 frozen_job_name 相关错误
        frozen_errors = [e for e in result.errors if e.category == "frozen_job_name"]
        assert len(frozen_errors) == 0, f"Unexpected errors: {frozen_errors}"
        assert len(result.checked_frozen_job_names) == 2

    def test_frozen_job_name_missing_from_section(self) -> None:
        """当某个 frozen job name 未出现在 Frozen Job Names 章节时，应报错"""
        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": [], "job_names": []},
            "frozen_job_names": {
                "allowlist": [
                    "Test Job",
                    "Missing Job",  # 这个不在文档中
                ]
            },
        }
        doc = """
# Workflow Contract

### 5.1 Frozen Job Names

| Job Name | 原因 |
|----------|------|
| `Test Job` | Required Check |

## 6. 其他章节
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # 验证有 frozen_job_name_not_in_doc 错误
        frozen_errors = [e for e in result.errors if e.error_type == "frozen_job_name_not_in_doc"]
        assert len(frozen_errors) == 1
        assert frozen_errors[0].value == "Missing Job"
        assert "5.1" in frozen_errors[0].message

    def test_frozen_job_names_section_missing(self) -> None:
        """当文档缺少 Frozen Job Names 章节时，应报错"""
        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": [], "job_names": []},
            "frozen_job_names": {"allowlist": ["Test Job"]},
        }
        # 注意：文档内容不能包含任何锚点关键字
        doc = """
# Workflow Contract

## 5. 其他章节

一些内容，但没有冻结作业名称的章节。
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # 验证有 section_missing 错误
        section_errors = [
            e for e in result.errors if e.error_type == "frozen_job_names_section_missing"
        ]
        assert len(section_errors) == 1
        assert "Frozen Job Names" in section_errors[0].message

    def test_frozen_job_name_in_wrong_section(self) -> None:
        """当 frozen job name 出现在错误的章节时，应报错"""
        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": [], "job_names": []},
            "frozen_job_names": {"allowlist": ["Missing Job Name"]},
        }
        # 注意：文档中 "Missing Job Name" 只出现在 section 6（错误位置），不在 section 5.1
        doc = """
# Workflow Contract

### 5.1 Frozen Job Names

这里列出冻结的作业

| Job Name | 原因 |
|----------|------|
| `Other Job` | 其他 |

## 6. 其他章节

Missing Job Name 出现在这里（错误的位置）
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # 验证有错误（因为 Missing Job Name 不在正确的章节）
        frozen_errors = [e for e in result.errors if e.error_type == "frozen_job_name_not_in_doc"]
        assert len(frozen_errors) == 1


# ============================================================================
# Test: labels 检查
# ============================================================================


class TestLabelsCheck:
    """测试 <workflow>.labels 检查"""

    def test_labels_all_present_in_section(self) -> None:
        """当所有 labels 都出现在 PR Labels 章节时，应通过"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": [],
                "job_names": [],
                "labels": ["openmemory:freeze-override"],
            },
        }
        doc = """
# Workflow Contract

## 3. PR Label 列表与语义

| Label | 语义 |
|-------|------|
| `openmemory:freeze-override` | 绕过冻结 |

## 4. 其他章节
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # 验证无 label 相关错误
        label_errors = [e for e in result.errors if e.category == "label"]
        assert len(label_errors) == 0, f"Unexpected errors: {label_errors}"
        assert len(result.checked_labels) == 1

    def test_label_missing_from_section(self) -> None:
        """当某个 label 未出现在 PR Labels 章节时，应报错"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": [],
                "job_names": [],
                "labels": ["documented-label", "missing-label"],
            },
        }
        doc = """
# Workflow Contract

## 3. PR Label 列表与语义

| Label | 语义 |
|-------|------|
| `documented-label` | 已记录的标签 |

## 4. 其他章节
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # 验证有 label_not_in_doc 错误
        label_errors = [e for e in result.errors if e.error_type == "label_not_in_doc"]
        assert len(label_errors) == 1
        assert label_errors[0].value == "missing-label"
        assert "section 3" in label_errors[0].message

    def test_labels_section_missing(self) -> None:
        """当文档缺少 PR Labels 章节时，应报错"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": [],
                "job_names": [],
                "labels": ["some-label"],
            },
        }
        # 注意：文档内容不能包含任何锚点关键字（避免意外匹配）
        doc = """
# Workflow Contract

## 2. Job 定义

## 4. 其他章节

没有标签相关的章节
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # 验证有 section_missing 错误
        section_errors = [e for e in result.errors if e.error_type == "labels_section_missing"]
        assert len(section_errors) == 1
        assert "PR Label" in section_errors[0].message

    def test_label_in_wrong_section(self) -> None:
        """当 label 出现在错误的章节时，应报错"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": [],
                "job_names": [],
                "labels": ["missing-label-xyz"],
            },
        }
        # 注意：文档中 "missing-label-xyz" 只出现在 section 4（错误位置），不在 section 3
        doc = """
# Workflow Contract

## 3. PR Label 列表与语义

| Label | 语义 |
|-------|------|
| `other-label` | 其他标签 |

## 4. 其他章节

missing-label-xyz 出现在这里（错误的位置）
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # 验证有错误（因为 missing-label-xyz 不在正确的章节）
        label_errors = [e for e in result.errors if e.error_type == "label_not_in_doc"]
        assert len(label_errors) == 1

    def test_multiple_workflows_with_labels(self) -> None:
        """当多个 workflow 都有 labels 时，应全部检查"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": [],
                "job_names": [],
                "labels": ["ci-label"],
            },
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": [],
                "job_names": [],
                "labels": ["nightly-label"],
            },
        }
        doc = """
# Workflow Contract

## 3. PR Label 列表与语义

| Label | 语义 |
|-------|------|
| `ci-label` | CI 标签 |
| `nightly-label` | Nightly 标签 |

## 4. 其他章节
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # 验证无错误，且检查了两个 labels
        label_errors = [e for e in result.errors if e.category == "label"]
        assert len(label_errors) == 0
        assert len(result.checked_labels) == 2

    def test_no_labels_is_ok(self) -> None:
        """当 workflow 没有定义 labels 时，不应产生警告或错误"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": [],
                "job_names": [],
                # 没有 labels 字段
            },
        }
        doc = """
# Workflow Contract

## 3. PR Label 列表与语义

没有定义任何 label

## 4. 其他章节
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # 验证无 label 相关错误或警告
        label_errors = [e for e in result.errors if e.category == "label"]
        assert len(label_errors) == 0
        assert len(result.checked_labels) == 0


# ============================================================================
# Test: 锚点常量定义
# ============================================================================


class TestAnchorConstants:
    """测试锚点常量定义"""

    def test_frozen_job_doc_anchors_defined(self) -> None:
        """验证 FROZEN_JOB_DOC_ANCHORS 已正确定义"""
        assert len(FROZEN_JOB_DOC_ANCHORS) >= 1
        assert "Frozen Job Names" in FROZEN_JOB_DOC_ANCHORS

    def test_labels_doc_anchors_defined(self) -> None:
        """验证 LABELS_DOC_ANCHORS 已正确定义"""
        assert len(LABELS_DOC_ANCHORS) >= 1
        assert "PR Label 列表与语义" in LABELS_DOC_ANCHORS


# ============================================================================
# Test: 章节切片逻辑（Section Slicing）
# ============================================================================


class TestSectionSlicingLogic:
    """测试章节切片逻辑

    验证 job_ids/job_names 只能在对应 workflow 章节出现才算通过，
    而不是全文匹配。
    """

    def test_ci_job_id_in_nightly_section_fails(self) -> None:
        """测试 CI 的 job_id 出现在 nightly 章节而非 CI 章节时，应报错"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["ci-test-job"],
                "job_names": ["CI Test Job"],
            },
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": ["nightly-job"],
                "job_names": ["Nightly Job"],
            },
        }
        # ci-test-job 只在 nightly 章节出现，不在 CI 章节
        doc = """
# Workflow Contract

## CI Workflow (ci.yml)

| Job ID | Job Name |
|--------|----------|
| `other-job` | Other Job |

## Nightly Workflow (nightly.yml)

| Job ID | Job Name |
|--------|----------|
| `nightly-job` | Nightly Job |
| `ci-test-job` | CI Test Job |

## 冻结的 Step 文本

无

## Make Targets

targets_required 说明
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # 应该失败
        assert result.success is False

        # 应该有 job_id_not_in_doc 错误，指向 ci-test-job
        job_id_errors = [
            e
            for e in result.errors
            if e.error_type == "job_id_not_in_doc" and e.value == "ci-test-job"
        ]
        assert len(job_id_errors) == 1
        # 错误消息应该说明是在章节内找不到
        assert "section" in job_id_errors[0].message.lower()

    def test_nightly_job_name_in_ci_section_fails(self) -> None:
        """测试 nightly 的 job_name 出现在 CI 章节而非 nightly 章节时，应报错"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["ci-job"],
                "job_names": ["CI Job"],
            },
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": ["nightly-verify"],
                "job_names": ["Nightly Verification"],
            },
        }
        # Nightly Verification 只在 CI 章节出现，不在 nightly 章节
        doc = """
# Workflow Contract

## CI Workflow (ci.yml)

| Job ID | Job Name |
|--------|----------|
| `ci-job` | CI Job |
| `wrong` | Nightly Verification |

## Nightly Workflow (nightly.yml)

| Job ID | Job Name |
|--------|----------|
| `nightly-verify` | Other Name |

## 冻结的 Step 文本

无

## Make Targets

targets_required 说明
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # 应该失败
        assert result.success is False

        # 应该有 job_name_not_in_doc 错误，指向 Nightly Verification
        job_name_errors = [
            e
            for e in result.errors
            if e.error_type == "job_name_not_in_doc" and e.value == "Nightly Verification"
        ]
        assert len(job_name_errors) == 1

    def test_frozen_step_outside_frozen_section_fails(self) -> None:
        """测试 frozen step 出现在非冻结章节时，应报错"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test"],
                "job_names": ["Test"],
            },
            "frozen_step_text": {
                "allowlist": ["Run CI Gate", "Checkout Repository"],
            },
        }
        # "Run CI Gate" 只在 CI 章节出现，不在冻结 Step 章节
        doc = """
# Workflow Contract

## CI Workflow (ci.yml)

| Job ID | Job Name |
|--------|----------|
| `test` | Test |

步骤包括：
- Run CI Gate

## 冻结的 Step 文本

| Step Name |
|-----------|
| `Checkout Repository` |

## Make Targets

targets_required 说明
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # 应该失败
        assert result.success is False

        # 应该有 frozen_step_not_in_doc 错误
        step_errors = [
            e
            for e in result.errors
            if e.error_type == "frozen_step_not_in_doc" and e.value == "Run CI Gate"
        ]
        assert len(step_errors) == 1

    def test_correct_section_placement_passes(self) -> None:
        """测试所有元素在正确章节时通过"""
        contract = {
            "version": "2.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint", "test"],
                "job_names": ["Lint Check", "Unit Tests"],
            },
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": ["full-verify"],
                "job_names": ["Full Verification"],
            },
            "frozen_step_text": {
                "allowlist": ["Checkout code", "Run tests"],
            },
            "frozen_job_names": {
                "allowlist": ["Lint Check", "Full Verification"],
            },
            "make": {
                "targets_required": ["lint", "test"],
            },
        }
        doc = """
# Workflow Contract

Version: 2.0.0

## CI Workflow (ci.yml)

| Job ID | Job Name |
|--------|----------|
| `lint` | Lint Check |
| `test` | Unit Tests |

## Nightly Workflow (nightly.yml)

| Job ID | Job Name |
|--------|----------|
| `full-verify` | Full Verification |

## 冻结的 Step 文本

| Step Name |
|-----------|
| `Checkout code` |
| `Run tests` |

### Frozen Job Names

| Job Name |
|----------|
| `Lint Check` |
| `Full Verification` |

## Make Targets

targets_required:
- lint
- test

## SemVer Policy

版本策略说明...
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # 应该通过
        assert result.success is True
        assert len(result.errors) == 0


# ============================================================================
# Test: version 字符串检查
# ============================================================================


class TestVersionStringCheck:
    """测试 version 字符串检查"""

    def test_version_present_in_doc_passes(self) -> None:
        """测试 version 在文档中存在时通过"""
        contract = {
            "version": "2.5.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": [], "job_names": []},
        }
        doc = """
# Workflow Contract

Version: **2.5.0**

## 其他内容
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # 版本检查应该通过
        version_errors = [e for e in result.errors if e.error_type == "version_not_in_doc"]
        assert len(version_errors) == 0
        assert result.checked_version == "2.5.0"

    def test_version_missing_from_doc_fails(self) -> None:
        """测试 version 不在文档中时报错"""
        contract = {
            "version": "3.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": [], "job_names": []},
        }
        doc = """
# Workflow Contract

## 其他内容

没有版本号
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # 版本检查应该失败
        version_errors = [e for e in result.errors if e.error_type == "version_not_in_doc"]
        assert len(version_errors) == 1
        assert version_errors[0].value == "3.0.0"
        assert version_errors[0].category == "version"

    def test_version_partial_match_not_enough(self) -> None:
        """测试版本号部分匹配不够（必须完全匹配）"""
        contract = {
            "version": "2.5.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": [], "job_names": []},
        }
        # 文档包含 2.5 但不包含 2.5.0
        doc = """
# Workflow Contract

Version: 2.5

## 其他内容
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # 版本检查应该失败（2.5 != 2.5.0）
        version_errors = [e for e in result.errors if e.error_type == "version_not_in_doc"]
        assert len(version_errors) == 1

    def test_no_version_in_contract_produces_warning(self) -> None:
        """测试 contract 没有 version 字段时产生警告"""
        contract = {
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": [], "job_names": []},
            # 没有 version 字段
        }
        doc = """
# Workflow Contract

## 其他内容
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # 应该产生警告
        assert "No version found in contract" in " ".join(result.warnings)


# ============================================================================
# Test: SemVer Policy 章节存在性检查
# ============================================================================


class TestSemVerPolicySectionCheck:
    """测试 SemVer Policy / 版本策略章节存在性检查"""

    def test_semver_policy_section_present_passes(self) -> None:
        """测试 SemVer Policy 章节存在时通过"""
        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": [], "job_names": []},
        }
        doc = """
# Workflow Contract

Version: 1.0.0

## SemVer Policy

版本变更规则说明...
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # SemVer 章节检查应该通过
        semver_errors = [
            e for e in result.errors if e.error_type == "semver_policy_section_missing"
        ]
        assert len(semver_errors) == 0

    def test_version_policy_chinese_title_passes(self) -> None:
        """测试中文'版本策略'标题也能通过"""
        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": [], "job_names": []},
        }
        doc = """
# Workflow Contract

Version: 1.0.0

## 版本策略

版本变更规则说明...
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # SemVer 章节检查应该通过（因为包含"版本策略"）
        semver_errors = [
            e for e in result.errors if e.error_type == "semver_policy_section_missing"
        ]
        assert len(semver_errors) == 0

    def test_semver_keyword_in_content_passes(self) -> None:
        """测试内容中包含 SemVer 关键字也能通过"""
        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": [], "job_names": []},
        }
        doc = """
# Workflow Contract

Version: 1.0.0

## 版本管理

本合约遵循 SemVer 语义化版本规范。
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # SemVer 章节检查应该通过（因为包含"SemVer"关键字）
        semver_errors = [
            e for e in result.errors if e.error_type == "semver_policy_section_missing"
        ]
        assert len(semver_errors) == 0

    def test_semver_policy_section_missing_fails(self) -> None:
        """测试 SemVer Policy 章节缺失时报错"""
        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": [], "job_names": []},
        }
        # 注意：文档内容不能包含任何 SEMVER_POLICY_DOC_ANCHORS 中的关键字
        # 包括 "SemVer Policy", "版本策略", "SemVer"
        doc = """
# Workflow Contract

Version: 1.0.0

## 其他章节

这里没有版本管理相关的内容说明
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # SemVer 章节检查应该失败
        semver_errors = [
            e for e in result.errors if e.error_type == "semver_policy_section_missing"
        ]
        assert len(semver_errors) == 1
        assert semver_errors[0].category == "doc_structure"


# ============================================================================
# Test: make.targets_required 文档同步检查
# ============================================================================


class TestMakeTargetsDocsSync:
    """测试 make.targets_required 文档同步检查"""

    def test_all_make_targets_in_doc_passes(self) -> None:
        """测试所有 make targets 都在文档中时通过"""
        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": [], "job_names": []},
            "make": {
                "targets_required": ["lint", "test", "format-check"],
            },
        }
        doc = """
# Workflow Contract

Version: 1.0.0

## Make Targets

targets_required 包含:
- lint
- test
- format-check

## SemVer Policy

版本策略说明
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # make target 检查应该通过
        target_errors = [e for e in result.errors if e.error_type == "make_target_not_in_doc"]
        assert len(target_errors) == 0
        assert len(result.checked_make_targets) == 3

    def test_make_target_missing_from_doc_fails(self) -> None:
        """测试 make target 未在文档中时报错"""
        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": [], "job_names": []},
            "make": {
                "targets_required": ["lint", "test", "undocumented-target"],
            },
        }
        doc = """
# Workflow Contract

Version: 1.0.0

## Make Targets

targets_required 包含:
- lint
- test

## SemVer Policy

版本策略说明
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # 应该失败
        target_errors = [e for e in result.errors if e.error_type == "make_target_not_in_doc"]
        assert len(target_errors) == 1
        assert target_errors[0].value == "undocumented-target"
        assert target_errors[0].category == "make_target"

    def test_make_targets_section_missing_fails(self) -> None:
        """测试文档缺少 Make Targets 章节时报错"""
        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": [], "job_names": []},
            "make": {
                "targets_required": ["lint"],
            },
        }
        # 注意：文档内容不能包含 "targets_required", "Make Targets", "make targets" 关键字
        doc = """
# Workflow Contract

Version: 1.0.0

## 其他章节

这里没有构建命令相关的说明
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # 应该有 make_targets_section_missing 错误
        section_errors = [
            e for e in result.errors if e.error_type == "make_targets_section_missing"
        ]
        assert len(section_errors) == 1
        assert section_errors[0].category == "make"


# ============================================================================
# Test: 错误信息清晰度
# ============================================================================


class TestErrorMessageClarity:
    """测试错误信息的清晰度和可定位性"""

    def test_frozen_job_name_error_contains_section_hint(self) -> None:
        """frozen job name 错误应包含章节提示"""
        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": [], "job_names": []},
            "frozen_job_names": {"allowlist": ["Missing Job"]},
        }
        doc = """
# Workflow Contract

### 5.1 Frozen Job Names

只有这个 job

## 6. 其他章节
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        errors = [e for e in result.errors if e.error_type == "frozen_job_name_not_in_doc"]
        assert len(errors) == 1
        # 错误信息应包含章节编号提示
        assert "5.1" in errors[0].message or "Frozen Job Names" in errors[0].message

    def test_label_error_contains_workflow_info(self) -> None:
        """label 错误应包含 workflow 信息"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": [],
                "job_names": [],
                "labels": ["missing-label"],
            },
        }
        doc = """
# Workflow Contract

## 3. PR Label 列表与语义

没有定义的标签

## 4. 其他章节
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        errors = [e for e in result.errors if e.error_type == "label_not_in_doc"]
        assert len(errors) == 1
        # 错误信息应包含 workflow 信息
        assert "ci" in errors[0].message or "section 3" in errors[0].message


# ============================================================================
# Test: 集成测试（与真实文件格式兼容）
# ============================================================================


class TestIntegrationWithRealFormat:
    """测试与真实文件格式的兼容性"""

    def test_real_frozen_job_names_format(self) -> None:
        """测试与真实 contract.md 格式兼容的 frozen job names 检查"""
        contract = {
            "version": "2.5.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": [], "job_names": []},
            "frozen_job_names": {
                "allowlist": [
                    "Test (Python ${{ matrix.python-version }})",
                    "Lint",
                    "Workflow Contract Validation",
                    "Unified Stack Full Verification",
                ]
            },
        }
        # 模拟真实的 contract.md 格式
        doc = """
# CI/Nightly Workflow Contract

## 5. "禁止回归"的 Step 文本范围

### 5.1 Frozen Job Names

以下 Job Name 为"禁止回归"基准，在 `workflow_contract.v1.json` 的 `frozen_job_names.allowlist` 中定义。

**仅冻结被 GitHub Required Checks 引用的核心 Jobs（共 4 个）：**

| Job Name | 原因 |
|----------|------|
| `Test (Python ${{ matrix.python-version }})` | Required Check，单元测试门禁 |
| `Lint` | Required Check，代码质量门禁 |
| `Workflow Contract Validation` | Required Check，合约校验门禁 |
| `Unified Stack Full Verification` | Nightly 核心验证 |

### 5.2 Frozen Step Names

## 10. SemVer Policy / 版本策略
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # 验证无 frozen_job_name 相关错误
        frozen_errors = [e for e in result.errors if e.category == "frozen_job_name"]
        assert len(frozen_errors) == 0, f"Unexpected errors: {frozen_errors}"
        assert len(result.checked_frozen_job_names) == 4

    def test_real_labels_format(self) -> None:
        """测试与真实 contract.md 格式兼容的 labels 检查"""
        contract = {
            "version": "2.5.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": [],
                "job_names": [],
                "labels": ["openmemory:freeze-override"],
            },
        }
        # 模拟真实的 contract.md 格式
        doc = """
# CI/Nightly Workflow Contract

## 3. PR Label 列表与语义

> **SSOT 说明**: `scripts/ci/workflow_contract.v1.json` 的 `ci.labels` 字段是 PR Labels 的唯一真实来源（SSOT）。

| Label | 语义 | 使用场景 |
|-------|------|----------|
| `openmemory:freeze-override` | 绕过 OpenMemory 升级冻结 | 冻结期间的紧急修复 |

### 3.1 Override Reason 要求

## 4. Workflow 环境变量基线
"""
        contract_path, doc_path = create_temp_files(contract, doc)

        checker = WorkflowContractDocsSyncChecker(contract_path, doc_path)
        result = checker.check()

        # 验证无 label 相关错误
        label_errors = [e for e in result.errors if e.category == "label"]
        assert len(label_errors) == 0, f"Unexpected errors: {label_errors}"
        assert len(result.checked_labels) == 1
