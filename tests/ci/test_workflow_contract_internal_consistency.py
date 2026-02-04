#!/usr/bin/env python3
"""
tests/ci/test_workflow_contract_internal_consistency.py

单元测试：check_workflow_contract_internal_consistency.py 的内部一致性检查功能

测试范围：
1. job_ids/job_names 长度一致性检查
2. job_ids 无重复检查
3. required_jobs id 无重复检查
4. required_jobs id 在 job_ids 中检查
5. 真实合约文件的集成测试
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from scripts.ci.check_workflow_contract_internal_consistency import (
    CONTRACT_JOB_IDS_DUPLICATE,
    CONTRACT_JOB_IDS_NAMES_LENGTH_MISMATCH,
    CONTRACT_REQUIRED_JOB_ID_DUPLICATE,
    CONTRACT_REQUIRED_JOB_NOT_IN_JOB_IDS,
    ConsistencyError,
    ConsistencyResult,
    WorkflowContractInternalConsistencyChecker,
    format_json_output,
    format_text_output,
)

# ============================================================================
# Fixtures
# ============================================================================


def create_temp_contract(contract_data: dict[str, Any]) -> Path:
    """创建临时合约文件

    Args:
        contract_data: 合约数据

    Returns:
        临时文件路径
    """
    temp_file = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        delete=False,
        encoding="utf-8",
    )
    json.dump(contract_data, temp_file)
    temp_file.close()
    return Path(temp_file.name)


def make_minimal_contract(
    job_ids: list[str] | None = None,
    job_names: list[str] | None = None,
    required_jobs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """创建最小的合约数据用于测试

    Args:
        job_ids: job_ids 列表（默认为空列表）
        job_names: job_names 列表（默认为空列表）
        required_jobs: required_jobs 列表（默认为空列表）

    Returns:
        合约数据字典
    """
    return {
        "version": "1.0.0",
        "frozen_step_text": {"allowlist": []},
        "frozen_job_names": {"allowlist": []},
        "ci": {
            "file": ".github/workflows/ci.yml",
            "job_ids": job_ids if job_ids is not None else [],
            "job_names": job_names if job_names is not None else [],
            "required_jobs": required_jobs if required_jobs is not None else [],
        },
        "nightly": {
            "file": ".github/workflows/nightly.yml",
            "job_ids": [],
            "job_names": [],
            "required_jobs": [],
            "required_env_vars": ["TEST_VAR"],
        },
        "make": {"targets_required": ["ci"]},
    }


# ============================================================================
# Test: job_ids/job_names 长度一致性
# ============================================================================


class TestJobIdsNamesLengthConsistency:
    """测试 job_ids 和 job_names 长度一致性检查"""

    def test_equal_length_passes(self) -> None:
        """当 job_ids 和 job_names 长度相同时应通过"""
        contract = make_minimal_contract(
            job_ids=["test", "lint"],
            job_names=["Test Job", "Lint Job"],
        )
        contract_path = create_temp_contract(contract)

        checker = WorkflowContractInternalConsistencyChecker(contract_path)
        result = checker.check()

        assert result.success is True
        length_errors = [
            e for e in result.errors if e.error_type == CONTRACT_JOB_IDS_NAMES_LENGTH_MISMATCH
        ]
        assert len(length_errors) == 0

    def test_more_job_ids_than_names_fails(self) -> None:
        """当 job_ids 比 job_names 多时应报错"""
        contract = make_minimal_contract(
            job_ids=["test", "lint", "build"],
            job_names=["Test Job", "Lint Job"],  # 缺少一个
        )
        contract_path = create_temp_contract(contract)

        checker = WorkflowContractInternalConsistencyChecker(contract_path)
        result = checker.check()

        assert result.success is False
        length_errors = [
            e for e in result.errors if e.error_type == CONTRACT_JOB_IDS_NAMES_LENGTH_MISMATCH
        ]
        assert len(length_errors) == 1
        assert "3" in length_errors[0].message  # job_ids count
        assert "2" in length_errors[0].message  # job_names count

    def test_more_job_names_than_ids_fails(self) -> None:
        """当 job_names 比 job_ids 多时应报错"""
        contract = make_minimal_contract(
            job_ids=["test"],
            job_names=["Test Job", "Lint Job"],  # 多一个
        )
        contract_path = create_temp_contract(contract)

        checker = WorkflowContractInternalConsistencyChecker(contract_path)
        result = checker.check()

        assert result.success is False
        length_errors = [
            e for e in result.errors if e.error_type == CONTRACT_JOB_IDS_NAMES_LENGTH_MISMATCH
        ]
        assert len(length_errors) == 1

    def test_empty_arrays_pass(self) -> None:
        """空数组应通过（长度都是 0）"""
        contract = make_minimal_contract(
            job_ids=[],
            job_names=[],
        )
        contract_path = create_temp_contract(contract)

        checker = WorkflowContractInternalConsistencyChecker(contract_path)
        result = checker.check()

        # 空数组长度相等，不会触发长度不匹配错误
        length_errors = [
            e for e in result.errors if e.error_type == CONTRACT_JOB_IDS_NAMES_LENGTH_MISMATCH
        ]
        assert len(length_errors) == 0


# ============================================================================
# Test: job_ids 无重复
# ============================================================================


class TestNoDuplicateJobIds:
    """测试 job_ids 无重复检查"""

    def test_unique_job_ids_passes(self) -> None:
        """当 job_ids 无重复时应通过"""
        contract = make_minimal_contract(
            job_ids=["test", "lint", "build"],
            job_names=["Test Job", "Lint Job", "Build Job"],
        )
        contract_path = create_temp_contract(contract)

        checker = WorkflowContractInternalConsistencyChecker(contract_path)
        result = checker.check()

        duplicate_errors = [e for e in result.errors if e.error_type == CONTRACT_JOB_IDS_DUPLICATE]
        assert len(duplicate_errors) == 0

    def test_duplicate_job_id_fails(self) -> None:
        """当 job_ids 有重复时应报错"""
        contract = make_minimal_contract(
            job_ids=["test", "lint", "test"],  # "test" 重复
            job_names=["Test Job", "Lint Job", "Test Job 2"],
        )
        contract_path = create_temp_contract(contract)

        checker = WorkflowContractInternalConsistencyChecker(contract_path)
        result = checker.check()

        assert result.success is False
        duplicate_errors = [e for e in result.errors if e.error_type == CONTRACT_JOB_IDS_DUPLICATE]
        assert len(duplicate_errors) == 1
        assert duplicate_errors[0].key == "test"

    def test_multiple_duplicates_reported(self) -> None:
        """多个重复的 job_id 应各自报错"""
        contract = make_minimal_contract(
            job_ids=["test", "lint", "test", "lint"],  # "test" 和 "lint" 都重复
            job_names=["Test 1", "Lint 1", "Test 2", "Lint 2"],
        )
        contract_path = create_temp_contract(contract)

        checker = WorkflowContractInternalConsistencyChecker(contract_path)
        result = checker.check()

        assert result.success is False
        duplicate_errors = [e for e in result.errors if e.error_type == CONTRACT_JOB_IDS_DUPLICATE]
        assert len(duplicate_errors) == 2
        duplicate_keys = {e.key for e in duplicate_errors}
        assert "test" in duplicate_keys
        assert "lint" in duplicate_keys


# ============================================================================
# Test: required_jobs id 无重复
# ============================================================================


class TestNoDuplicateRequiredJobIds:
    """测试 required_jobs id 无重复检查"""

    def test_unique_required_job_ids_passes(self) -> None:
        """当 required_jobs id 无重复时应通过"""
        contract = make_minimal_contract(
            job_ids=["test", "lint"],
            job_names=["Test Job", "Lint Job"],
            required_jobs=[
                {"id": "test", "name": "Test Job", "required_steps": []},
                {"id": "lint", "name": "Lint Job", "required_steps": []},
            ],
        )
        contract_path = create_temp_contract(contract)

        checker = WorkflowContractInternalConsistencyChecker(contract_path)
        result = checker.check()

        duplicate_errors = [
            e for e in result.errors if e.error_type == CONTRACT_REQUIRED_JOB_ID_DUPLICATE
        ]
        assert len(duplicate_errors) == 0

    def test_duplicate_required_job_id_fails(self) -> None:
        """当 required_jobs id 有重复时应报错"""
        contract = make_minimal_contract(
            job_ids=["test", "lint"],
            job_names=["Test Job", "Lint Job"],
            required_jobs=[
                {"id": "test", "name": "Test Job", "required_steps": []},
                {"id": "test", "name": "Test Job 2", "required_steps": []},  # 重复
            ],
        )
        contract_path = create_temp_contract(contract)

        checker = WorkflowContractInternalConsistencyChecker(contract_path)
        result = checker.check()

        assert result.success is False
        duplicate_errors = [
            e for e in result.errors if e.error_type == CONTRACT_REQUIRED_JOB_ID_DUPLICATE
        ]
        assert len(duplicate_errors) == 1
        assert duplicate_errors[0].key == "test"


# ============================================================================
# Test: required_jobs id 必须在 job_ids 中
# ============================================================================


class TestRequiredJobsInJobIds:
    """测试 required_jobs id 必须在 job_ids 中"""

    def test_all_required_jobs_in_job_ids_passes(self) -> None:
        """当所有 required_jobs id 都在 job_ids 中时应通过"""
        contract = make_minimal_contract(
            job_ids=["test", "lint", "build"],
            job_names=["Test Job", "Lint Job", "Build Job"],
            required_jobs=[
                {"id": "test", "name": "Test Job", "required_steps": []},
                {"id": "lint", "name": "Lint Job", "required_steps": []},
            ],
        )
        contract_path = create_temp_contract(contract)

        checker = WorkflowContractInternalConsistencyChecker(contract_path)
        result = checker.check()

        not_in_errors = [
            e for e in result.errors if e.error_type == CONTRACT_REQUIRED_JOB_NOT_IN_JOB_IDS
        ]
        assert len(not_in_errors) == 0

    def test_required_job_not_in_job_ids_fails(self) -> None:
        """当 required_jobs id 不在 job_ids 中时应报错"""
        contract = make_minimal_contract(
            job_ids=["test", "lint"],
            job_names=["Test Job", "Lint Job"],
            required_jobs=[
                {"id": "test", "name": "Test Job", "required_steps": []},
                {"id": "build", "name": "Build Job", "required_steps": []},  # 不在 job_ids 中
            ],
        )
        contract_path = create_temp_contract(contract)

        checker = WorkflowContractInternalConsistencyChecker(contract_path)
        result = checker.check()

        assert result.success is False
        not_in_errors = [
            e for e in result.errors if e.error_type == CONTRACT_REQUIRED_JOB_NOT_IN_JOB_IDS
        ]
        assert len(not_in_errors) == 1
        assert not_in_errors[0].key == "build"

    def test_multiple_missing_reported(self) -> None:
        """多个不在 job_ids 中的 required_jobs 应各自报错"""
        contract = make_minimal_contract(
            job_ids=["test"],
            job_names=["Test Job"],
            required_jobs=[
                {"id": "lint", "name": "Lint Job", "required_steps": []},
                {"id": "build", "name": "Build Job", "required_steps": []},
            ],
        )
        contract_path = create_temp_contract(contract)

        checker = WorkflowContractInternalConsistencyChecker(contract_path)
        result = checker.check()

        assert result.success is False
        not_in_errors = [
            e for e in result.errors if e.error_type == CONTRACT_REQUIRED_JOB_NOT_IN_JOB_IDS
        ]
        assert len(not_in_errors) == 2


# ============================================================================
# Test: 多 workflow 支持
# ============================================================================


class TestMultipleWorkflows:
    """测试多 workflow 支持"""

    def test_checks_all_workflows(self) -> None:
        """应检查所有 workflow 定义"""
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {"allowlist": []},
            "frozen_job_names": {"allowlist": []},
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test"],
                "job_names": ["Test Job"],
                "required_jobs": [],
            },
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": ["nightly-test", "nightly-test"],  # 重复
                "job_names": ["Nightly Test 1", "Nightly Test 2"],
                "required_jobs": [],
                "required_env_vars": ["TEST_VAR"],
            },
            "make": {"targets_required": ["ci"]},
        }
        contract_path = create_temp_contract(contract)

        checker = WorkflowContractInternalConsistencyChecker(contract_path)
        result = checker.check()

        # 应该检查 ci 和 nightly 两个 workflow
        assert "ci" in result.workflows_checked
        assert "nightly" in result.workflows_checked

        # nightly 中的重复 job_id 应被检测到
        assert result.success is False
        duplicate_errors = [e for e in result.errors if e.error_type == CONTRACT_JOB_IDS_DUPLICATE]
        assert len(duplicate_errors) == 1
        assert duplicate_errors[0].workflow == "nightly"


# ============================================================================
# Test: 输出格式
# ============================================================================


class TestOutputFormatting:
    """测试输出格式"""

    def test_text_output_success(self) -> None:
        """成功时的文本输出"""
        result = ConsistencyResult(
            success=True,
            contract_version="1.0.0",
            workflows_checked=["ci", "nightly"],
        )

        output = format_text_output(result)

        assert "[PASS]" in output
        assert "1.0.0" in output
        assert "ci" in output
        assert "nightly" in output

    def test_text_output_failure(self) -> None:
        """失败时的文本输出"""
        result = ConsistencyResult(
            success=False,
            contract_version="1.0.0",
            workflows_checked=["ci"],
            errors=[
                ConsistencyError(
                    error_type=CONTRACT_JOB_IDS_DUPLICATE,
                    workflow="ci",
                    message="Duplicate job_id 'test'",
                    key="test",
                )
            ],
        )

        output = format_text_output(result, verbose=True)

        assert "[FAIL]" in output
        assert "1 consistency error" in output
        assert "test" in output

    def test_json_output(self) -> None:
        """JSON 输出格式"""
        result = ConsistencyResult(
            success=True,
            contract_version="1.0.0",
            workflows_checked=["ci"],
        )

        output = format_json_output(result)
        data = json.loads(output)

        assert data["success"] is True
        assert data["contract_version"] == "1.0.0"
        assert "ci" in data["workflows_checked"]


# ============================================================================
# Test: 真实合约文件集成测试
# ============================================================================


class TestRealContractIntegration:
    """真实合约文件的集成测试"""

    def test_real_contract_internal_consistency(self) -> None:
        """验证真实 contract 的内部一致性"""
        workspace = Path(__file__).parent.parent.parent
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v2.json"

        if not contract_path.exists():
            pytest.skip(f"Contract file not found: {contract_path}")

        checker = WorkflowContractInternalConsistencyChecker(contract_path)
        result = checker.check()

        # 输出详细信息用于调试
        if not result.success:
            print("\nInternal consistency errors found:")
            for e in result.errors:
                print(f"  - [{e.error_type}] {e.workflow}: {e.message}")

        # 真实 contract 应该保持内部一致性
        assert result.success, (
            f"Real contract should be internally consistent. Found errors: "
            f"{[(e.error_type, e.key, e.workflow) for e in result.errors]}"
        )

    def test_real_contract_has_expected_workflows(self) -> None:
        """验证真实 contract 包含预期的 workflow"""
        workspace = Path(__file__).parent.parent.parent
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v2.json"

        if not contract_path.exists():
            pytest.skip(f"Contract file not found: {contract_path}")

        checker = WorkflowContractInternalConsistencyChecker(contract_path)
        checker.load_contract()
        result = checker.check()

        # 应该至少包含 ci 和 nightly
        assert "ci" in result.workflows_checked
        assert "nightly" in result.workflows_checked


# ============================================================================
# Test: 边界情况
# ============================================================================


class TestEdgeCases:
    """边界情况测试"""

    def test_missing_job_ids_field(self) -> None:
        """当 job_ids 字段缺失时应正常处理"""
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {"allowlist": []},
            "frozen_job_names": {"allowlist": []},
            "ci": {
                "file": ".github/workflows/ci.yml",
                # 缺少 job_ids
                "job_names": ["Test Job"],
                "required_jobs": [],
            },
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": [],
                "job_names": [],
                "required_jobs": [],
                "required_env_vars": ["TEST_VAR"],
            },
            "make": {"targets_required": ["ci"]},
        }
        contract_path = create_temp_contract(contract)

        checker = WorkflowContractInternalConsistencyChecker(contract_path)
        result = checker.check()

        # 应该报告长度不匹配（0 vs 1）
        length_errors = [
            e for e in result.errors if e.error_type == CONTRACT_JOB_IDS_NAMES_LENGTH_MISMATCH
        ]
        assert len(length_errors) == 1

    def test_required_job_without_id(self) -> None:
        """当 required_job 缺少 id 字段时应正常处理"""
        contract = make_minimal_contract(
            job_ids=["test"],
            job_names=["Test Job"],
            required_jobs=[
                {"name": "Test Job", "required_steps": []},  # 缺少 id
            ],
        )
        contract_path = create_temp_contract(contract)

        checker = WorkflowContractInternalConsistencyChecker(contract_path)
        checker.check()

        # 应该正常处理，不崩溃
        # 缺少 id 的 required_job 会被跳过
        assert True  # 只要不崩溃就通过

    def test_contract_load_failure(self) -> None:
        """当合约文件加载失败时应正确处理"""
        checker = WorkflowContractInternalConsistencyChecker(
            Path("/nonexistent/path/contract.json")
        )

        success = checker.load_contract()

        assert success is False
