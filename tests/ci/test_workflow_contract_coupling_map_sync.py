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
| test results | `test-results-{python-version}` |
"""
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)
        result = checker.check()

        # 验证无 artifact 相关错误（因为标准化后的前缀匹配成功）
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


class TestArtifactPathNormalization:
    """测试 artifact 路径标准化"""

    def test_normalize_artifact_path_for_lookup_basic(self) -> None:
        """测试基本的路径标准化"""
        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": [], "job_names": []},
        }
        coupling_map = "# Coupling Map"
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)

        # 测试基本标准化
        assert checker.normalize_artifact_path_for_lookup("./artifacts/") == "artifacts/"
        assert checker.normalize_artifact_path_for_lookup("a\\b\\c") == "a/b/c"
        assert checker.normalize_artifact_path_for_lookup("a//b//") == "a/b/"

    def test_normalize_artifact_path_for_lookup_wildcard(self) -> None:
        """测试通配符路径标准化"""
        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": [], "job_names": []},
        }
        coupling_map = "# Coupling Map"
        contract_path, coupling_map_path = create_temp_files(contract, coupling_map)

        checker = WorkflowContractCouplingMapSyncChecker(contract_path, coupling_map_path)

        # 测试通配符路径（ARTIFACT_PATH_NORMALIZATIONS 中的模式）
        assert checker.normalize_artifact_path_for_lookup("test-results-*.xml") == "test-results-"
        assert (
            checker.normalize_artifact_path_for_lookup("acceptance-results-*.xml")
            == "acceptance-results-"
        )

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

    def test_all_class_attrs_in_set(self) -> None:
        """CouplingMapSyncErrorTypes 类的所有属性应在集合中"""
        class_attrs = {
            v
            for k, v in CouplingMapSyncErrorTypes.__dict__.items()
            if not k.startswith("_") and isinstance(v, str)
        }
        assert class_attrs == COUPLING_MAP_SYNC_ERROR_TYPES
