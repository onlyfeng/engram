#!/usr/bin/env python3
"""
tests/ci/test_workflow_contract_coupling_map_sync.py

单元测试：check_workflow_contract_coupling_map_sync.py 的检查功能

测试范围：
1. job_ids 检查：验证 job_id 必须出现在 coupling_map.md 中
2. artifact 路径检查：验证 artifact 路径必须出现在 coupling_map.md 中
3. make targets 检查：验证关键 make targets 必须出现在 coupling_map.md 中
4. 受控块检查（markers 模式）：验证 begin/end markers 和块内容比对
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

# 导入被测模块
from scripts.ci.check_workflow_contract_coupling_map_sync import (
    COUPLING_MAP_SYNC_ERROR_TYPES,
    CRITICAL_MAKE_TARGETS,
    CouplingMapSyncErrorTypes,
    WorkflowContractCouplingMapSyncChecker,
)
from scripts.ci.render_workflow_contract_docs import (
    WorkflowContractDocsRenderer,
)
from scripts.ci.workflow_contract_common import artifact_path_lookup_tokens

# ============================================================================
# Fixtures
# ============================================================================


def create_temp_files(
    contract_data: dict[str, Any], coupling_map_content: str
) -> tuple[Path, Path]:
    """创建临时 contract JSON 和 coupling_map Markdown 文件"""
    # 创建临时目录
    temp_dir = Path(tempfile.mkdtemp())

    # 写入 contract JSON
    contract_path = temp_dir / "workflow_contract.v1.json"
    with open(contract_path, "w", encoding="utf-8") as f:
        json.dump(contract_data, f, indent=2)

    # 写入 coupling_map Markdown
    coupling_map_path = temp_dir / "coupling_map.md"
    with open(coupling_map_path, "w", encoding="utf-8") as f:
        f.write(coupling_map_content)

    return contract_path, coupling_map_path


# ============================================================================
# Test: job_ids 检查
# ============================================================================


class TestJobIdsCheck:
    """测试 job_ids 检查"""

    def test_job_ids_all_present_passes(self) -> None:
        """当所有 job_ids 都出现在 coupling_map 时，应通过"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint", "test", "schema-validate"],
                "job_names": [],
            },
        }
        coupling_map = """
# CI/Nightly Workflow 耦合映射

## 1. CI Workflow Jobs 与产物映射

### 1.1 test job

| Job ID | `test` |

### 1.2 lint job

| Job ID | `lint` |

### 1.3 schema-validate job

| Job ID | `schema-validate` |
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 验证无 job_id 相关错误
        job_id_errors = [e for e in result.errors if e.category == "job_id"]
        assert len(job_id_errors) == 0, f"Unexpected errors: {job_id_errors}"
        assert len(result.checked_job_ids) == 3

    def test_job_id_missing_fails(self) -> None:
        """当某个 job_id 未出现在 coupling_map 时，应报错"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint", "missing-job"],
                "job_names": [],
            },
        }
        coupling_map = """
# CI/Nightly Workflow 耦合映射

## 1. CI Workflow Jobs

| Job ID | `lint` |
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 验证有 job_id_not_in_coupling_map 错误
        job_id_errors = [
            e
            for e in result.errors
            if e.error_type == CouplingMapSyncErrorTypes.JOB_ID_NOT_IN_COUPLING_MAP
        ]
        assert len(job_id_errors) == 1
        assert job_id_errors[0].value == "missing-job"
        assert "coupling_map.md" in job_id_errors[0].message

    def test_multiple_workflows_job_ids(self) -> None:
        """当多个 workflow 都有 job_ids 时，应全部检查"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
                "job_names": [],
            },
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": ["unified-stack-full"],
                "job_names": [],
            },
        }
        coupling_map = """
# CI/Nightly Workflow 耦合映射

## 1. CI Workflow Jobs

| Job ID | `lint` |

## 2. Nightly Workflow Jobs

| Job ID | `unified-stack-full` |
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 验证无错误，且检查了两个 job_ids
        job_id_errors = [e for e in result.errors if e.category == "job_id"]
        assert len(job_id_errors) == 0
        assert len(result.checked_job_ids) == 2


# ============================================================================
# Test: artifact 路径检查
# ============================================================================


class TestArtifactPathsCheck:
    """测试 artifact 路径检查"""

    def test_artifacts_all_present_passes(self) -> None:
        """当所有 artifact 路径都出现在 coupling_map 时，应通过"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": [],
                "job_names": [],
                "artifact_archive": {
                    "required_artifact_paths": [
                        "schema-validation-results.json",
                        "artifacts/workflow_contract_validation.json",
                    ],
                },
            },
        }
        coupling_map = """
# CI/Nightly Workflow 耦合映射

## Artifacts

| Artifact | Path |
|----------|------|
| schema | `schema-validation-results.json` |
| validation | `artifacts/workflow_contract_validation.json` |
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 验证无 artifact 相关错误
        artifact_errors = [e for e in result.errors if e.category == "artifact"]
        assert len(artifact_errors) == 0, f"Unexpected errors: {artifact_errors}"
        assert len(result.checked_artifacts) == 2

    def test_artifact_missing_fails(self) -> None:
        """当某个 artifact 路径未出现在 coupling_map 时，应报错"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": [],
                "job_names": [],
                "artifact_archive": {
                    "required_artifact_paths": [
                        "existing-artifact.json",
                        "missing-artifact.json",
                    ],
                },
            },
        }
        coupling_map = """
# CI/Nightly Workflow 耦合映射

## Artifacts

| Artifact | Path |
|----------|------|
| existing | `existing-artifact.json` |
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 验证有 artifact_not_in_coupling_map 错误
        artifact_errors = [
            e
            for e in result.errors
            if e.error_type == CouplingMapSyncErrorTypes.ARTIFACT_NOT_IN_COUPLING_MAP
        ]
        assert len(artifact_errors) == 1
        assert artifact_errors[0].value == "missing-artifact.json"

    def test_wildcard_artifact_paths_normalized(self) -> None:
        """测试通配符 artifact 路径能正确标准化匹配"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": [],
                "job_names": [],
                "artifact_archive": {
                    "required_artifact_paths": [
                        "test-results-*.xml",
                    ],
                },
            },
        }
        # 使用标准化后的前缀进行匹配
        coupling_map = """
# CI/Nightly Workflow 耦合映射

## Artifacts

| Artifact | Path |
|----------|------|
| test results | `test-results-{python-version}.xml` |
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 验证无 artifact 相关错误（固定片段匹配成功）
        artifact_errors = [e for e in result.errors if e.category == "artifact"]
        assert len(artifact_errors) == 0

    def test_glob_path_requires_literal_when_no_fixed_fragments(self) -> None:
        """glob 模式只有通配符时，应要求文档包含原始模式"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": [],
                "job_names": [],
                "artifact_archive": {
                    "required_artifact_paths": [
                        "**/*.json",
                    ],
                },
            },
        }
        coupling_map = """
# CI/Nightly Workflow 耦合映射

## Artifacts

| Artifact | Path |
|----------|------|
| report | `artifacts/report.json` |
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        artifact_errors = [
            e
            for e in result.errors
            if e.error_type == CouplingMapSyncErrorTypes.ARTIFACT_NOT_IN_COUPLING_MAP
        ]
        assert len(artifact_errors) == 1
        assert artifact_errors[0].value == "**/*.json"

    def test_directory_artifact_path_requires_trailing_slash(self) -> None:
        """目录路径必须保留末尾斜杠"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": [],
                "job_names": [],
                "artifact_archive": {
                    "required_artifact_paths": [
                        ".artifacts/acceptance-runs/",
                    ],
                },
            },
        }
        coupling_map = """
# CI/Nightly Workflow 耦合映射

## Artifacts

| Artifact | Path |
|----------|------|
| acceptance runs | `.artifacts/acceptance-runs` |
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        artifact_errors = [
            e
            for e in result.errors
            if e.error_type == CouplingMapSyncErrorTypes.ARTIFACT_NOT_IN_COUPLING_MAP
        ]
        assert len(artifact_errors) == 1
        assert artifact_errors[0].value == ".artifacts/acceptance-runs/"

    def test_directory_artifact_path_with_trailing_slash_passes(self) -> None:
        """目录路径包含末尾斜杠时应通过"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": [],
                "job_names": [],
                "artifact_archive": {
                    "required_artifact_paths": [
                        ".artifacts/acceptance-runs/",
                    ],
                },
            },
        }
        coupling_map = """
# CI/Nightly Workflow 耦合映射

## Artifacts

| Artifact | Path |
|----------|------|
| acceptance runs | `.artifacts/acceptance-runs/` |
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        artifact_errors = [e for e in result.errors if e.category == "artifact"]
        assert len(artifact_errors) == 0


# ============================================================================
# Test: make targets 检查
# ============================================================================


class TestMakeTargetsCheck:
    """测试 make targets 检查"""

    def test_critical_make_targets_present_passes(self) -> None:
        """当所有关键 make targets 都出现在 coupling_map 时，应通过"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": [],
                "job_names": [],
            },
            "make": {
                "targets_required": ["ci", "lint", "typecheck"],
            },
        }
        coupling_map = """
# CI/Nightly Workflow 耦合映射

## 3. Makefile Targets 清单

| Target | 说明 |
|--------|------|
| `ci` | CI 聚合目标 |
| `lint` | 代码风格检查 |
| `typecheck` | 类型检查 |
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 验证无 make_target 相关错误
        make_errors = [e for e in result.errors if e.category == "make_target"]
        assert len(make_errors) == 0, f"Unexpected errors: {make_errors}"
        # 只检查关键 targets
        assert all(t in CRITICAL_MAKE_TARGETS for t in result.checked_make_targets)

    def test_critical_make_target_missing_fails(self) -> None:
        """当某个关键 make target 未出现在 coupling_map 时，应报错"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": [],
                "job_names": [],
            },
            "make": {
                "targets_required": ["ci", "lint"],
            },
        }
        coupling_map = """
# CI/Nightly Workflow 耦合映射

## 3. Makefile Targets 清单

| Target | 说明 |
|--------|------|
| `lint` | 代码风格检查 |
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 验证有 make_target_not_in_coupling_map 错误
        make_errors = [
            e
            for e in result.errors
            if e.error_type == CouplingMapSyncErrorTypes.MAKE_TARGET_NOT_IN_COUPLING_MAP
        ]
        assert len(make_errors) == 1
        assert make_errors[0].value == "ci"
        assert "section 3" in make_errors[0].message

    def test_non_critical_make_target_not_checked(self) -> None:
        """非关键 make targets 不应被检查"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": [],
                "job_names": [],
            },
            "make": {
                "targets_required": ["some-custom-target"],
            },
        }
        coupling_map = """
# CI/Nightly Workflow 耦合映射

没有 some-custom-target
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 非关键 target 不应被检查，所以无错误
        make_errors = [e for e in result.errors if e.category == "make_target"]
        assert len(make_errors) == 0
        assert len(result.checked_make_targets) == 0


# ============================================================================
# Test: 文件错误处理
# ============================================================================


class TestFileErrorHandling:
    """测试文件错误处理"""

    def test_contract_not_found_error(self) -> None:
        """当 contract 文件不存在时，应报错"""
        temp_dir = Path(tempfile.mkdtemp())
        contract_path = temp_dir / "nonexistent.json"
        coupling_map_path = temp_dir / "coupling_map.md"
        coupling_map_path.write_text("# Coupling Map")

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 验证有 contract_not_found 错误
        file_errors = [
            e for e in result.errors if e.error_type == CouplingMapSyncErrorTypes.CONTRACT_NOT_FOUND
        ]
        assert len(file_errors) == 1
        assert result.success is False

    def test_coupling_map_not_found_error(self) -> None:
        """当 coupling_map 文件不存在时，应报错"""
        contract = {"version": "1.0.0", "ci": {"file": "ci.yml", "job_ids": [], "job_names": []}}
        temp_dir = Path(tempfile.mkdtemp())
        contract_path = temp_dir / "contract.json"
        contract_path.write_text(json.dumps(contract))
        coupling_map_path = temp_dir / "nonexistent.md"

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 验证有 coupling_map_not_found 错误
        file_errors = [
            e
            for e in result.errors
            if e.error_type == CouplingMapSyncErrorTypes.COUPLING_MAP_NOT_FOUND
        ]
        assert len(file_errors) == 1
        assert result.success is False

    def test_invalid_json_error(self) -> None:
        """当 contract 文件不是有效 JSON 时，应报错"""
        temp_dir = Path(tempfile.mkdtemp())
        contract_path = temp_dir / "invalid.json"
        contract_path.write_text("{ invalid json }")
        coupling_map_path = temp_dir / "coupling_map.md"
        coupling_map_path.write_text("# Coupling Map")

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 验证有 contract_parse_error 错误
        file_errors = [
            e
            for e in result.errors
            if e.error_type == CouplingMapSyncErrorTypes.CONTRACT_PARSE_ERROR
        ]
        assert len(file_errors) == 1
        assert result.success is False


# ============================================================================
# Test: 错误类型常量
# ============================================================================


class TestErrorTypeConstants:
    """测试错误类型常量定义"""

    def test_all_error_types_defined(self) -> None:
        """验证所有 error_type 都已定义在常量集合中"""
        expected_types = {
            "contract_not_found",
            "contract_parse_error",
            "coupling_map_not_found",
            "coupling_map_read_error",
            "job_id_not_in_coupling_map",
            "artifact_not_in_coupling_map",
            "make_target_not_in_coupling_map",
            # 受控块错误（markers 模式）
            "block_marker_missing",
            "block_marker_duplicate",
            "block_marker_unpaired",
            "block_content_mismatch",
            "unknown_block_marker",
        }
        assert COUPLING_MAP_SYNC_ERROR_TYPES == expected_types

    def test_error_type_class_attributes_match(self) -> None:
        """验证 CouplingMapSyncErrorTypes 类属性与集合一致"""
        class_attrs = {
            v
            for k, v in CouplingMapSyncErrorTypes.__dict__.items()
            if not k.startswith("_") and isinstance(v, str)
        }
        assert class_attrs == COUPLING_MAP_SYNC_ERROR_TYPES


# ============================================================================
# Test: 集成测试
# ============================================================================


class TestArtifactPathLookupTokens:
    """测试 artifact 路径查找 token 规则"""

    def test_artifact_path_lookup_tokens_basic(self) -> None:
        """测试基本路径的 token 生成"""
        assert ("artifacts/",) in artifact_path_lookup_tokens("./artifacts/")
        assert artifact_path_lookup_tokens("a\\b\\c") == [("a/b/c",)]
        assert artifact_path_lookup_tokens("a//b//") == [("a/b/",)]

    def test_artifact_path_lookup_tokens_glob(self) -> None:
        """测试通配符路径的 token 生成"""
        tokens = artifact_path_lookup_tokens("test-results-*.xml")
        assert ("test-results-*.xml",) in tokens
        assert ("test-results-", ".xml") in tokens

    def test_artifact_check_with_normalized_paths(self) -> None:
        """测试标准化路径在 artifact 检查中的使用"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": [],
                "job_names": [],
                "artifact_archive": {
                    "required_artifact_paths": [
                        "./artifacts/results.json",  # 使用 ./ 前缀
                    ],
                },
            },
        }
        coupling_map = """
# Coupling Map

## Artifacts

| Artifact | Path |
|----------|------|
| results | `artifacts/results.json` |
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 标准化后的路径应该能在 coupling_map 中找到
        artifact_errors = [e for e in result.errors if e.category == "artifact"]
        assert len(artifact_errors) == 0


class TestIntegration:
    """集成测试：模拟真实文件格式"""

    def test_real_format_all_present(self) -> None:
        """测试与真实 coupling_map.md 格式兼容的全量检查"""
        contract = {
            "version": "2.17.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test", "lint", "workflow-contract"],
                "job_names": [],
                "artifact_archive": {
                    "required_artifact_paths": [
                        "schema-validation-results.json",
                        "artifacts/workflow_contract_validation.json",
                    ],
                },
            },
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": ["unified-stack-full"],
                "job_names": [],
            },
            "make": {
                "targets_required": ["ci", "lint", "verify-unified"],
            },
        }
        coupling_map = """
# CI/Nightly Workflow 耦合映射

## 1. CI Workflow Jobs 与产物映射

### 1.1 test job

| 属性 | 值 |
|------|-----|
| Job ID | `test` |

### 1.2 lint job

| 属性 | 值 |
|------|-----|
| Job ID | `lint` |

### 1.4 workflow-contract job

| 属性 | 值 |
|------|-----|
| Job ID | `workflow-contract` |

**产物上传**：

| Artifact 名称 | 文件路径 |
|--------------|----------|
| validation | `artifacts/workflow_contract_validation.json` |

### 1.3 schema-validate job

**产物上传**：

| Artifact 名称 | 文件路径 |
|--------------|----------|
| schema | `schema-validation-results.json` |

## 2. Nightly Workflow Jobs 与产物映射

### 2.1 unified-stack-full job

| 属性 | 值 |
|------|-----|
| Job ID | `unified-stack-full` |

## 3. Makefile Targets 清单

| Target | 说明 |
|--------|------|
| `ci` | CI 聚合目标 |
| `lint` | 代码风格检查 |
| `verify-unified` | 统一栈验证 |
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 应该通过
        assert result.success is True, f"Unexpected errors: {result.errors}"
        assert len(result.errors) == 0
        assert len(result.checked_job_ids) == 4
        assert len(result.checked_artifacts) == 2

    def test_real_format_missing_items(self) -> None:
        """测试真实格式下缺失项的检测"""
        contract = {
            "version": "2.17.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test", "missing-job"],
                "job_names": [],
            },
            "make": {
                "targets_required": ["lint"],  # 使用 lint 而不是 ci，避免意外匹配
            },
        }
        coupling_map = """
# Workflow 耦合映射

## 1. Workflow Jobs

| Job ID | `test` |

## 3. Makefile Targets

这里没有 makefile 目标记录
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 应该失败
        assert result.success is False
        # 应该有 2 个错误：missing-job 和 lint target
        assert len(result.errors) == 2
        error_values = {e.value for e in result.errors}
        assert "missing-job" in error_values
        assert "lint" in error_values


# ============================================================================
# Test: 受控块检查（Markers 模式）
# ============================================================================


class TestControlledBlocksMarkerMode:
    """测试受控块检查（markers 模式）"""

    def test_no_markers_uses_fallback_mode(self) -> None:
        """当文档没有 markers 时，应使用回退模式（字符串匹配）"""
        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": ["test"], "job_names": []},
        }
        coupling_map = """
# CI/Nightly Workflow 耦合映射

## 1. CI Jobs

| Job ID | `test` |
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 没有 markers，不使用 block mode
        assert result.block_mode_used is False
        assert len(result.checked_blocks) == 0

    def test_markers_present_enables_block_mode(self) -> None:
        """当文档有 markers 时，应启用 block mode"""
        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": ["test"], "job_names": ["Test"]},
        }
        # 只有一个 marker，其他预期块会产生 BLOCK_MARKER_MISSING error
        coupling_map = """
# CI/Nightly Workflow 耦合映射

<!-- BEGIN:CI_JOBS_LIST -->
| Job ID | Job Name |
|--------|----------|
| `test` | Test |
<!-- END:CI_JOBS_LIST -->
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 有 markers，使用 block mode
        assert result.block_mode_used is True
        assert len(result.checked_blocks) > 0
        # 其他预期块缺失 markers 会产生 error（3 个块中只有 1 个有 marker）
        missing_marker_errors = [
            e
            for e in result.errors
            if e.error_type == CouplingMapSyncErrorTypes.BLOCK_MARKER_MISSING
        ]
        assert len(missing_marker_errors) == 2  # NIGHTLY_JOBS_LIST, MAKE_TARGETS_LIST

    def test_missing_expected_block_markers_error(self) -> None:
        """当预期块的 markers 缺失时，应报 BLOCK_MARKER_MISSING error"""
        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": ["test"], "job_names": ["Test"]},
        }
        # 有任意 marker 触发 block mode，但 CI_JOBS_LIST 缺失 markers
        coupling_map = """
# CI/Nightly Workflow 耦合映射

<!-- BEGIN:OTHER_BLOCK -->
some content
<!-- END:OTHER_BLOCK -->

## Some Section
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 应该有 BLOCK_MARKER_MISSING error（所有 3 个预期块都缺失）
        missing_errors = [
            e
            for e in result.errors
            if e.error_type == CouplingMapSyncErrorTypes.BLOCK_MARKER_MISSING
        ]
        assert len(missing_errors) == 3
        # 验证错误信息包含修复指引
        for error in missing_errors:
            assert "BEGIN:" in error.message
            assert "END:" in error.message
            assert error.category == "block"

    def test_duplicate_begin_marker_error(self) -> None:
        """当存在重复的 BEGIN marker 时，应报错"""
        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": ["test"], "job_names": ["Test"]},
        }
        coupling_map = """
# CI/Nightly Workflow 耦合映射

<!-- BEGIN:CI_JOBS_LIST -->
| Job ID | Job Name |
<!-- BEGIN:CI_JOBS_LIST -->
| `test` | Test |
<!-- END:CI_JOBS_LIST -->
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 应该有重复 marker 错误
        dup_errors = [
            e
            for e in result.errors
            if e.error_type == CouplingMapSyncErrorTypes.BLOCK_MARKER_DUPLICATE
        ]
        assert len(dup_errors) >= 1

    def test_missing_end_marker_error(self) -> None:
        """当缺少 END marker 时，应报错"""
        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": ["test"], "job_names": ["Test"]},
        }
        coupling_map = """
# CI/Nightly Workflow 耦合映射

<!-- BEGIN:CI_JOBS_LIST -->
| Job ID | Job Name |
|--------|----------|
| `test` | Test |
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 应该有 unpaired marker 错误
        unpaired_errors = [
            e
            for e in result.errors
            if e.error_type == CouplingMapSyncErrorTypes.BLOCK_MARKER_UNPAIRED
        ]
        assert len(unpaired_errors) >= 1

    def test_block_content_mismatch_provides_diff(self) -> None:
        """当块内容不匹配时，应提供 diff 输出"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test", "lint"],
                "job_names": ["Test Job", "Lint Job"],
            },
        }
        # 文档中的表格与渲染结果不匹配
        coupling_map = """
# CI/Nightly Workflow 耦合映射

<!-- BEGIN:CI_JOBS_LIST -->
| Job ID | Job Name |
|--------|----------|
| `test` | Wrong Name |
<!-- END:CI_JOBS_LIST -->
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 应该有内容不匹配错误
        mismatch_errors = [
            e
            for e in result.errors
            if e.error_type == CouplingMapSyncErrorTypes.BLOCK_CONTENT_MISMATCH
        ]
        assert len(mismatch_errors) >= 1
        # 应该包含 diff
        assert mismatch_errors[0].diff is not None
        assert "---" in mismatch_errors[0].diff  # unified diff 格式
        # 应该包含期望块
        assert mismatch_errors[0].expected_block is not None
        assert "BEGIN:CI_JOBS_LIST" in mismatch_errors[0].expected_block

    def test_unknown_block_marker_reports_error(self) -> None:
        """当存在未知 marker 时，应报错并提示修复步骤"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["ci-job"],
                "job_names": ["CI Job"],
            },
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": ["nightly-job"],
                "job_names": ["Nightly Job"],
            },
            "make": {"targets_required": ["ci"]},
        }

        temp_dir = Path(tempfile.mkdtemp())
        contract_path = temp_dir / "contract.json"
        with open(contract_path, "w", encoding="utf-8") as f:
            json.dump(contract, f)

        renderer = WorkflowContractDocsRenderer(contract_path)
        renderer.load_contract()
        blocks = renderer.render_coupling_map_blocks()

        coupling_map = f"""# Coupling Map

<!-- BEGIN:UNKNOWN_BLOCK -->
unexpected content
<!-- END:UNKNOWN_BLOCK -->

## CI Jobs

<!-- BEGIN:CI_JOBS_LIST -->
{blocks["CI_JOBS_LIST"].content}
<!-- END:CI_JOBS_LIST -->

## Nightly Jobs

<!-- BEGIN:NIGHTLY_JOBS_LIST -->
{blocks["NIGHTLY_JOBS_LIST"].content}
<!-- END:NIGHTLY_JOBS_LIST -->

## Make Targets

<!-- BEGIN:MAKE_TARGETS_LIST -->
{blocks["MAKE_TARGETS_LIST"].content}
<!-- END:MAKE_TARGETS_LIST -->
"""
        coupling_map_path = temp_dir / "coupling_map.md"
        coupling_map_path.write_text(coupling_map, encoding="utf-8")

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        unknown_errors = [
            e
            for e in result.errors
            if e.error_type == CouplingMapSyncErrorTypes.UNKNOWN_BLOCK_MARKER
        ]
        assert len(unknown_errors) == 1
        assert unknown_errors[0].value == "UNKNOWN_BLOCK"
        assert "Remove the markers" in unknown_errors[0].message
        assert "renderer support" in unknown_errors[0].message


# ============================================================================
# Test: 渲染稳定性（Coupling Map）
# ============================================================================


class TestCouplingMapRenderingStability:
    """测试 coupling_map 渲染稳定性"""

    def test_ci_jobs_list_matches_contract_order(self) -> None:
        """CI Jobs 列表应按 contract 中的顺序渲染"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test", "lint", "build"],
                "job_names": ["Test Job", "Lint Job", "Build Job"],
            },
        }
        temp_dir = Path(tempfile.mkdtemp())
        contract_path = temp_dir / "contract.json"
        with open(contract_path, "w", encoding="utf-8") as f:
            json.dump(contract, f)

        renderer = WorkflowContractDocsRenderer(contract_path)
        renderer.load_contract()
        block = renderer.render_ci_jobs_list()

        # 验证顺序与 contract 一致
        lines = block.content.split("\n")
        data_lines = [line for line in lines if line.startswith("| `")]
        assert "`test`" in data_lines[0]
        assert "`lint`" in data_lines[1]
        assert "`build`" in data_lines[2]

    def test_nightly_jobs_list_matches_contract_order(self) -> None:
        """Nightly Jobs 列表应按 contract 中的顺序渲染"""
        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": [], "job_names": []},
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": ["verify", "notify"],
                "job_names": ["Verify Job", "Notify Job"],
            },
        }
        temp_dir = Path(tempfile.mkdtemp())
        contract_path = temp_dir / "contract.json"
        with open(contract_path, "w", encoding="utf-8") as f:
            json.dump(contract, f)

        renderer = WorkflowContractDocsRenderer(contract_path)
        renderer.load_contract()
        block = renderer.render_nightly_jobs_list()

        # 验证顺序与 contract 一致
        lines = block.content.split("\n")
        data_lines = [line for line in lines if line.startswith("| `")]
        assert "`verify`" in data_lines[0]
        assert "`notify`" in data_lines[1]

    def test_make_targets_list_sorted_alphabetically(self) -> None:
        """Make Targets 列表应按字母序排序"""
        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": [], "job_names": []},
            "make": {
                "targets_required": ["typecheck", "lint", "format", "ci"],
            },
        }
        temp_dir = Path(tempfile.mkdtemp())
        contract_path = temp_dir / "contract.json"
        with open(contract_path, "w", encoding="utf-8") as f:
            json.dump(contract, f)

        renderer = WorkflowContractDocsRenderer(contract_path)
        renderer.load_contract()
        block = renderer.render_make_targets_list()

        # 验证按字母序排序
        lines = block.content.split("\n")
        data_lines = [line for line in lines if line.startswith("| `")]
        assert "`ci`" in data_lines[0]
        assert "`format`" in data_lines[1]
        assert "`lint`" in data_lines[2]
        assert "`typecheck`" in data_lines[3]


# ============================================================================
# Test: 新增 Error Types 完整性
# ============================================================================


class TestNewErrorTypesCompleteness:
    """测试新增 error types 的完整性"""

    def test_block_error_types_in_set(self) -> None:
        """新增的块错误类型应在 COUPLING_MAP_SYNC_ERROR_TYPES 集合中"""
        assert CouplingMapSyncErrorTypes.BLOCK_MARKER_MISSING in COUPLING_MAP_SYNC_ERROR_TYPES
        assert CouplingMapSyncErrorTypes.BLOCK_MARKER_DUPLICATE in COUPLING_MAP_SYNC_ERROR_TYPES
        assert CouplingMapSyncErrorTypes.BLOCK_MARKER_UNPAIRED in COUPLING_MAP_SYNC_ERROR_TYPES
        assert CouplingMapSyncErrorTypes.BLOCK_CONTENT_MISMATCH in COUPLING_MAP_SYNC_ERROR_TYPES
        assert CouplingMapSyncErrorTypes.UNKNOWN_BLOCK_MARKER in COUPLING_MAP_SYNC_ERROR_TYPES

    def test_all_class_attrs_in_set(self) -> None:
        """CouplingMapSyncErrorTypes 类的所有属性应在集合中"""
        class_attrs = {
            v
            for k, v in CouplingMapSyncErrorTypes.__dict__.items()
            if not k.startswith("_") and isinstance(v, str)
        }
        assert class_attrs == COUPLING_MAP_SYNC_ERROR_TYPES


# ============================================================================
# Test: --write 功能（update_document for coupling_map）
# ============================================================================


class TestUpdateCouplingMapWriteMode:
    """测试 --write 功能：update_document 函数对 coupling_map.md 的更新能力"""

    def test_write_updates_coupling_map_block_correctly(self) -> None:
        """--write 后 coupling_map 块内容应与 renderer 输出一致"""
        from scripts.ci.render_workflow_contract_docs import (
            extract_block_from_content,
            update_document,
        )

        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test", "lint", "build"],
                "job_names": ["Test Job", "Lint Job", "Build Job"],
            },
        }
        # 创建带有 markers 但内容过时的 coupling_map
        coupling_map = """# Coupling Map

## CI Jobs

<!-- BEGIN:CI_JOBS_LIST -->
| Job ID | Job Name |
|--------|----------|
| `old-job` | Old Job |
<!-- END:CI_JOBS_LIST -->

## Other Section
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        # 使用渲染器生成期望的块内容
        renderer = WorkflowContractDocsRenderer(contract_path)
        renderer.load_contract()
        blocks = renderer.render_coupling_map_blocks()

        # 只更新 CI_JOBS_LIST 块
        ci_jobs_block = {"CI_JOBS_LIST": blocks["CI_JOBS_LIST"]}

        # 执行 update_document（非 dry_run）
        result = update_document(coupling_map_path, ci_jobs_block, dry_run=False)

        # 验证更新成功
        assert result.success is True
        assert "CI_JOBS_LIST" in result.updated_blocks
        assert len(result.missing_markers) == 0

        # 验证文件内容已更新
        updated_content = coupling_map_path.read_text(encoding="utf-8")

        # 提取更新后的块内容
        actual_content, _, _ = extract_block_from_content(updated_content, "CI_JOBS_LIST")

        # 验证块内容与渲染器输出一致
        expected_content = blocks["CI_JOBS_LIST"].content
        assert actual_content is not None
        assert actual_content.strip() == expected_content.strip()

    def test_write_multiple_coupling_map_blocks(self) -> None:
        """同时更新多个 coupling_map 块"""
        from scripts.ci.render_workflow_contract_docs import (
            extract_block_from_content,
            update_document,
        )

        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test"],
                "job_names": ["Test"],
            },
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": ["nightly-test"],
                "job_names": ["Nightly Test"],
            },
            "make": {
                "targets_required": ["ci", "lint"],
            },
        }
        coupling_map = """# Coupling Map

## CI Jobs

<!-- BEGIN:CI_JOBS_LIST -->
| Old CI content |
<!-- END:CI_JOBS_LIST -->

## Nightly Jobs

<!-- BEGIN:NIGHTLY_JOBS_LIST -->
| Old Nightly content |
<!-- END:NIGHTLY_JOBS_LIST -->

## Make Targets

<!-- BEGIN:MAKE_TARGETS_LIST -->
| Old Make content |
<!-- END:MAKE_TARGETS_LIST -->
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        renderer = WorkflowContractDocsRenderer(contract_path)
        renderer.load_contract()
        blocks = renderer.render_coupling_map_blocks()

        # 更新所有 coupling_map 块
        result = update_document(coupling_map_path, blocks, dry_run=False)

        # 验证所有块都已更新
        assert result.success is True
        assert "CI_JOBS_LIST" in result.updated_blocks
        assert "NIGHTLY_JOBS_LIST" in result.updated_blocks
        assert "MAKE_TARGETS_LIST" in result.updated_blocks

        # 验证内容正确
        updated_content = coupling_map_path.read_text(encoding="utf-8")
        for block_name, expected_block in blocks.items():
            actual_content, _, _ = extract_block_from_content(updated_content, block_name)
            assert actual_content is not None
            assert actual_content.strip() == expected_block.content.strip()

    def test_checker_passes_after_write(self) -> None:
        """update_document 更新后，Checker 应通过"""
        from scripts.ci.render_workflow_contract_docs import update_document

        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test", "lint"],
                "job_names": ["Test Job", "Lint Job"],
            },
        }
        # 创建带有 markers 但内容错误的 coupling_map
        coupling_map = """# Coupling Map

## CI Jobs

<!-- BEGIN:CI_JOBS_LIST -->
| Job ID | Job Name |
|--------|----------|
| `wrong` | Wrong |
<!-- END:CI_JOBS_LIST -->
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        # 先验证 checker 会失败
        checker_before = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result_before = checker_before.check()
        mismatch_before = [
            e
            for e in result_before.errors
            if e.error_type == CouplingMapSyncErrorTypes.BLOCK_CONTENT_MISMATCH
        ]
        assert len(mismatch_before) >= 1, "Should have content mismatch before update"

        # 执行 update_document
        renderer = WorkflowContractDocsRenderer(contract_path)
        renderer.load_contract()
        blocks = renderer.render_coupling_map_blocks()
        update_document(coupling_map_path, {"CI_JOBS_LIST": blocks["CI_JOBS_LIST"]}, dry_run=False)

        # 验证 checker 现在通过
        checker_after = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result_after = checker_after.check()
        mismatch_after = [
            e
            for e in result_after.errors
            if e.error_type == CouplingMapSyncErrorTypes.BLOCK_CONTENT_MISMATCH
        ]
        assert len(mismatch_after) == 0, "Should have no content mismatch after update"


# ============================================================================
# Test: 异常路径（Marker 错误场景补充）
# ============================================================================


class TestCouplingMapMarkerErrorScenarios:
    """测试 coupling_map marker 错误场景的完整覆盖"""

    def test_missing_begin_marker_only(self) -> None:
        """当只有 END marker 缺少 BEGIN marker 时，应报 unpaired 错误"""
        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": ["test"], "job_names": ["Test"]},
        }
        # 只有 END marker，没有 BEGIN marker
        coupling_map = """# Coupling Map

Some content

<!-- END:CI_JOBS_LIST -->

## Other Section
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 应该有 unpaired marker 错误
        unpaired_errors = [
            e
            for e in result.errors
            if e.error_type == CouplingMapSyncErrorTypes.BLOCK_MARKER_UNPAIRED
        ]
        assert len(unpaired_errors) >= 1
        # 错误应该指出缺少 BEGIN marker
        assert any("BEGIN" in e.message for e in unpaired_errors)

    def test_duplicate_end_marker_in_coupling_map(self) -> None:
        """coupling_map 中存在重复的 END marker 时，应报错"""
        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": ["test"], "job_names": ["Test"]},
        }
        coupling_map = """# Coupling Map

<!-- BEGIN:CI_JOBS_LIST -->
| Job ID | Job Name |
|--------|----------|
<!-- END:CI_JOBS_LIST -->
<!-- END:CI_JOBS_LIST -->
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 应该有重复 marker 错误
        dup_errors = [
            e
            for e in result.errors
            if e.error_type == CouplingMapSyncErrorTypes.BLOCK_MARKER_DUPLICATE
        ]
        assert len(dup_errors) >= 1

    def test_all_three_coupling_map_blocks_with_markers(self) -> None:
        """测试所有三个 coupling_map 块都有正确的 markers 时通过"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test"],
                "job_names": ["Test"],
            },
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": ["nightly-job"],
                "job_names": ["Nightly Job"],
            },
            "make": {
                "targets_required": ["ci"],
            },
        }

        temp_dir = Path(tempfile.mkdtemp())
        contract_path = temp_dir / "contract.json"
        with open(contract_path, "w", encoding="utf-8") as f:
            json.dump(contract, f)

        # 使用渲染器生成正确的块内容
        renderer = WorkflowContractDocsRenderer(contract_path)
        renderer.load_contract()
        blocks = renderer.render_coupling_map_blocks()

        # 创建包含所有正确 markers 的 coupling_map
        coupling_map = f"""# Coupling Map

## CI Jobs

<!-- BEGIN:CI_JOBS_LIST -->
{blocks["CI_JOBS_LIST"].content}
<!-- END:CI_JOBS_LIST -->

## Nightly Jobs

<!-- BEGIN:NIGHTLY_JOBS_LIST -->
{blocks["NIGHTLY_JOBS_LIST"].content}
<!-- END:NIGHTLY_JOBS_LIST -->

## Make Targets

<!-- BEGIN:MAKE_TARGETS_LIST -->
{blocks["MAKE_TARGETS_LIST"].content}
<!-- END:MAKE_TARGETS_LIST -->
"""
        coupling_map_path = temp_dir / "coupling_map.md"
        coupling_map_path.write_text(coupling_map, encoding="utf-8")

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 验证 block mode 已使用
        assert result.block_mode_used is True

        # 验证所有三个块都被检查
        assert "CI_JOBS_LIST" in result.checked_blocks
        assert "NIGHTLY_JOBS_LIST" in result.checked_blocks
        assert "MAKE_TARGETS_LIST" in result.checked_blocks

        # 验证无 block 相关错误
        block_errors = [e for e in result.errors if e.category == "block"]
        assert len(block_errors) == 0


# ============================================================================
# Test: 同时跑两个 Checker（从 coupling_map 角度）
# ============================================================================


class TestBothCheckersFromCouplingMapPerspective:
    """从 coupling_map 角度测试同时跑两个 Checker"""

    def test_checkers_share_same_renderer_output(self) -> None:
        """验证两个 Checker 使用相同的渲染器产生一致的输出"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test", "lint"],
                "job_names": ["Test Job", "Lint Job"],
            },
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": ["nightly-verify"],
                "job_names": ["Nightly Verify"],
            },
            "make": {
                "targets_required": ["ci", "lint", "test"],
            },
        }

        temp_dir = Path(tempfile.mkdtemp())
        contract_path = temp_dir / "contract.json"
        with open(contract_path, "w", encoding="utf-8") as f:
            json.dump(contract, f)

        # 创建两个独立的渲染器实例
        renderer1 = WorkflowContractDocsRenderer(contract_path)
        renderer1.load_contract()

        renderer2 = WorkflowContractDocsRenderer(contract_path)
        renderer2.load_contract()

        # 渲染 contract blocks
        contract_blocks1 = renderer1.render_contract_blocks()
        contract_blocks2 = renderer2.render_contract_blocks()

        # 渲染 coupling_map blocks
        coupling_blocks1 = renderer1.render_coupling_map_blocks()
        coupling_blocks2 = renderer2.render_coupling_map_blocks()

        # 验证两次渲染结果一致（确定性）
        for block_name in contract_blocks1:
            assert contract_blocks1[block_name].content == contract_blocks2[block_name].content

        for block_name in coupling_blocks1:
            assert coupling_blocks1[block_name].content == coupling_blocks2[block_name].content

    def test_both_checkers_detect_same_block_errors(self) -> None:
        """两个 Checker 对相同类型的 marker 错误应产生一致的检测结果"""
        from scripts.ci.check_workflow_contract_docs_sync import (
            DocsSyncErrorTypes,
            WorkflowContractDocsSyncChecker,
        )

        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": ["test"], "job_names": ["Test"]},
        }

        temp_dir = Path(tempfile.mkdtemp())
        contract_path = temp_dir / "contract.json"
        with open(contract_path, "w", encoding="utf-8") as f:
            json.dump(contract, f)

        # 创建缺少 END marker 的 contract.md
        contract_doc = """# Workflow Contract

Version: 1.0.0

### 2.1 CI Workflow (`ci.yml`)

<!-- BEGIN:CI_JOB_TABLE -->
| Job ID | Job Name |
|--------|----------|

## 冻结的 Step 文本

无

## Make Targets

targets_required

## SemVer Policy

版本策略
"""
        contract_doc_path = temp_dir / "contract.md"
        contract_doc_path.write_text(contract_doc, encoding="utf-8")

        # 创建缺少 END marker 的 coupling_map.md
        coupling_map = """# Coupling Map

<!-- BEGIN:CI_JOBS_LIST -->
| Job ID | Job Name |
|--------|----------|
"""
        coupling_map_path = temp_dir / "coupling_map.md"
        coupling_map_path.write_text(coupling_map, encoding="utf-8")

        # 运行两个 checker
        docs_checker = WorkflowContractDocsSyncChecker(contract_path, contract_doc_path)
        docs_result = docs_checker.check()

        coupling_checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        coupling_result = coupling_checker.check()

        # 验证两个 checker 都检测到 unpaired marker 错误
        docs_unpaired = [
            e
            for e in docs_result.errors
            if e.error_type == DocsSyncErrorTypes.BLOCK_MARKER_UNPAIRED
        ]
        coupling_unpaired = [
            e
            for e in coupling_result.errors
            if e.error_type == CouplingMapSyncErrorTypes.BLOCK_MARKER_UNPAIRED
        ]

        assert len(docs_unpaired) >= 1, "Docs checker should detect unpaired marker"
        assert len(coupling_unpaired) >= 1, "Coupling checker should detect unpaired marker"
