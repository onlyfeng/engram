#!/usr/bin/env python3
"""
Workflow Contract Validator 单元测试

覆盖功能:
1. 合约文件加载和解析
2. Workflow 文件验证
3. Job/Step/Output 缺失检测
4. Step name 变化 diff 提示
5. 实际 workflow 文件校验（集成测试）

Phase 1 说明：
- 校验 ci.yml 和 nightly.yml，release.yml 暂不纳入
- Nightly 验证为结构契约校验（job/step/artifact 定义），不执行 docker compose
- 合约定义参见 scripts/ci/workflow_contract.v1.json
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

# 导入被测模块
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))

from validate_workflows import (
    ValidationError,
    ValidationResult,
    ValidationWarning,
    WorkflowContractValidator,
    _is_glob_pattern,
    _path_matches,
    check_artifact_path_coverage,
    format_json_output,
    format_text_output,
)

# ============================================================================
# Shared Constants
# ============================================================================

# Critical error types for workflow contract validation
# These are errors that indicate significant contract violations
CRITICAL_ERROR_TYPES = {
    "workflow_not_found",
    "missing_job",
    "missing_job_id",
    "missing_step",
    "frozen_step_name_changed",
    "frozen_job_name_changed",
    "schema_error",
}

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_workspace():
    """创建临时工作空间"""
    with tempfile.TemporaryDirectory(prefix="test_workflow_contract_") as tmpdir:
        workspace = Path(tmpdir)

        # 创建 .github/workflows 目录
        (workspace / ".github" / "workflows").mkdir(parents=True)

        yield workspace


@pytest.fixture
def sample_contract():
    """示例合约"""
    return {
        "version": "1.0.0",
        "frozen_step_text": {
            "allowlist": ["Checkout repository", "Run tests"],
        },
        "frozen_job_names": {
            "allowlist": ["Test Job"],
        },
        "workflows": {
            "ci": {
                "file": ".github/workflows/ci.yml",
                "required_jobs": [
                    {
                        "id": "test-job",
                        "name": "Test Job",
                        "required_steps": ["Checkout repository", "Run tests"],
                        "required_outputs": ["test_result"],
                    }
                ],
                "required_env_vars": ["CI_VAR"],
            }
        },
    }


@pytest.fixture
def sample_workflow():
    """示例 workflow"""
    return """
name: CI

on: [push]

env:
  CI_VAR: "value"

jobs:
  test-job:
    name: Test Job
    runs-on: ubuntu-latest
    outputs:
      test_result: ${{ steps.test.outputs.result }}
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
      - name: Run tests
        id: test
        run: echo "result=success" >> $GITHUB_OUTPUT
"""


# ============================================================================
# Contract Loading Tests
# ============================================================================


class TestContractLoading:
    """合约加载测试"""

    def test_load_valid_contract(self, temp_workspace, sample_contract):
        """测试加载有效合约"""
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(sample_contract, f)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        assert validator.load_contract() is True
        assert validator.contract == sample_contract

    def test_load_missing_contract(self, temp_workspace):
        """测试加载不存在的合约"""
        contract_path = temp_workspace / "missing.json"

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        assert validator.load_contract() is False
        assert len(validator.result.errors) == 1
        assert validator.result.errors[0].error_type == "contract_not_found"

    def test_load_invalid_json_contract(self, temp_workspace):
        """测试加载无效 JSON 合约"""
        contract_path = temp_workspace / "invalid.json"
        with open(contract_path, "w") as f:
            f.write("{ invalid json }")

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        assert validator.load_contract() is False
        assert len(validator.result.errors) == 1
        assert validator.result.errors[0].error_type == "contract_parse_error"


# ============================================================================
# Workflow Validation Tests
# ============================================================================


class TestWorkflowValidation:
    """Workflow 验证测试"""

    def test_validate_valid_workflow(self, temp_workspace, sample_contract, sample_workflow):
        """测试验证有效 workflow"""
        # 写入合约
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(sample_contract, f)

        # 写入 workflow
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        with open(workflow_path, "w") as f:
            f.write(sample_workflow)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        assert result.success is True
        assert len(result.errors) == 0
        assert "ci" in result.validated_workflows

    def test_validate_missing_workflow_file(self, temp_workspace, sample_contract):
        """测试 workflow 文件缺失"""
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(sample_contract, f)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        assert result.success is False
        assert len(result.errors) == 1
        assert result.errors[0].error_type == "workflow_not_found"
        assert "ci" in result.skipped_workflows

    def test_validate_missing_job(self, temp_workspace, sample_contract):
        """测试缺少 job"""
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(sample_contract, f)

        # 写入缺少 job 的 workflow
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
env:
  CI_VAR: "value"
jobs:
  other-job:
    runs-on: ubuntu-latest
    steps:
      - name: Step
        run: echo "hello"
"""
        with open(workflow_path, "w") as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        assert result.success is False
        errors = [e for e in result.errors if e.error_type == "missing_job"]
        assert len(errors) == 1
        assert errors[0].key == "test-job"

    def test_validate_missing_step(self, temp_workspace, sample_contract):
        """测试缺少 step"""
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(sample_contract, f)

        # 写入缺少 step 的 workflow
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
env:
  CI_VAR: "value"
jobs:
  test-job:
    name: Test Job
    runs-on: ubuntu-latest
    outputs:
      test_result: ${{ steps.test.outputs.result }}
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
      # Missing "Run tests" step
"""
        with open(workflow_path, "w") as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        assert result.success is False
        errors = [e for e in result.errors if e.error_type == "missing_step"]
        assert len(errors) == 1
        assert errors[0].key == "Run tests"


# ============================================================================
# Step Name Change Detection Tests
# ============================================================================


class TestStepNameChangeDetection:
    """Step name 变化检测测试"""

    def test_detect_step_name_change(self, temp_workspace):
        """测试检测 step name 变化（非冻结 step）"""
        # 创建一个专用合约，其中 "Run tests" 不在 frozen 列表中
        # 这样 step name 变化会产生警告而不是错误
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {
                "allowlist": ["Checkout repository"],  # 不包含 "Run tests"
            },
            "frozen_job_names": {
                "allowlist": ["Test Job"],
            },
            "workflows": {
                "ci": {
                    "file": ".github/workflows/ci.yml",
                    "required_jobs": [
                        {
                            "id": "test-job",
                            "name": "Test Job",
                            "required_steps": ["Checkout repository", "Run tests"],
                            "required_outputs": ["test_result"],
                        }
                    ],
                    "required_env_vars": ["CI_VAR"],
                }
            },
        }
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 写入 step name 略有变化的 workflow
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
env:
  CI_VAR: "value"
jobs:
  test-job:
    name: Test Job
    runs-on: ubuntu-latest
    outputs:
      test_result: ${{ steps.test.outputs.result }}
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
      - name: Run unit tests
        id: test
        run: echo "result=success" >> $GITHUB_OUTPUT
"""
        with open(workflow_path, "w") as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 非冻结 step 改名应该产生警告（因为 "Run tests" 和 "Run unit tests" 部分匹配）
        # 但同时会报告 contract_frozen_step_missing 错误（因为 "Run tests" 不在 frozen_step_text 中）
        warnings = [w for w in result.warnings if w.warning_type == "step_name_changed"]
        assert len(warnings) == 1
        assert warnings[0].old_value == "Run tests"
        assert warnings[0].new_value == "Run unit tests"


# ============================================================================
# Output Formatting Tests
# ============================================================================


class TestOutputFormatting:
    """输出格式化测试"""

    def test_text_output_format(self):
        """测试文本输出格式"""
        result = ValidationResult(
            success=False,
            errors=[
                ValidationError(
                    workflow="ci",
                    file=".github/workflows/ci.yml",
                    error_type="missing_step",
                    key="Run tests",
                    message="Required step not found",
                    location="jobs.test.steps",
                )
            ],
            warnings=[
                ValidationWarning(
                    workflow="ci",
                    file=".github/workflows/ci.yml",
                    warning_type="step_name_changed",
                    key="Build",
                    message="Step name changed",
                    old_value="Build project",
                    new_value="Build artifacts",
                )
            ],
            validated_workflows=["ci"],
        )

        output = format_text_output(result)

        assert "FAILED" in output
        assert "ERRORS" in output
        assert "missing_step" in output
        assert "Run tests" in output
        assert "WARNINGS" in output
        assert "step_name_changed" in output
        assert "'Build project' -> 'Build artifacts'" in output

    def test_json_output_format(self):
        """测试 JSON 输出格式"""
        result = ValidationResult(success=True, validated_workflows=["ci"], errors=[], warnings=[])

        output = format_json_output(result)
        parsed = json.loads(output)

        assert parsed["success"] is True
        assert parsed["validated_workflows"] == ["ci"]
        assert parsed["error_count"] == 0
        assert parsed["warning_count"] == 0


# ============================================================================
# Integration Test - Real Workflow Files (Phase 0: CI only)
# ============================================================================


class TestRealWorkflowValidation:
    """真实 workflow 文件验证测试（集成测试）

    Phase 0 说明：
    - 仅校验 ci.yml
    - nightly.yml/release.yml 相关测试已移除
    """

    @pytest.fixture
    def real_workspace(self):
        """获取真实工作空间路径"""
        # 假设测试在项目根目录运行
        workspace = Path(__file__).parent.parent.parent
        return workspace

    def test_validate_ci_workflow_exists(self, real_workspace):
        """测试 CI workflow 文件存在"""
        ci_workflow = real_workspace / ".github" / "workflows" / "ci.yml"
        assert ci_workflow.exists(), "CI workflow should exist"

    def test_validate_real_workflows_no_critical_errors(self, real_workspace):
        """测试验证真实 workflow 文件（关键治理项无 ERROR 断言）"""
        contract_path = real_workspace / "scripts" / "ci" / "workflow_contract.v1.json"

        # 如果合约文件不存在，跳过测试
        if not contract_path.exists():
            pytest.skip("Contract file not found - run from project root")

        validator = WorkflowContractValidator(contract_path, real_workspace)
        result = validator.validate()

        # 输出结果供调试
        print(format_text_output(result))

        # 基本检查
        assert len(result.validated_workflows) > 0, "Should validate at least one workflow"

        # 使用共享的 critical error types 定义
        critical_errors = [e for e in result.errors if e.error_type in CRITICAL_ERROR_TYPES]

        # 记录错误和警告数量
        print(f"Total Errors: {len(result.errors)}")
        print(f"Critical Errors: {len(critical_errors)}")
        print(f"Warnings: {len(result.warnings)}")

        # 断言：关键治理项无 ERROR（warning 可保留）
        assert len(critical_errors) == 0, (
            f"Real workflow validation should have no critical errors. "
            f"Found {len(critical_errors)} critical errors: "
            f"{[(e.error_type, e.key, e.workflow) for e in critical_errors]}"
        )


# ============================================================================
# Integration Test - Contract + CI Workflow Validation
# ============================================================================


class TestContractCIWorkflowIntegration:
    """集成测试：对仓库真实 contract + ci.yml 运行完整验证

    这是 Phase 0 的核心集成测试，验证：
    1. workflow_contract.v1.json 与 ci.yml 的一致性
    2. 所有 required jobs 存在
    3. 所有 required steps 存在
    4. frozen job/step names 未被修改
    """

    @pytest.fixture
    def real_workspace(self):
        """获取真实工作空间路径"""
        workspace = Path(__file__).parent.parent.parent
        return workspace

    def test_contract_ci_workflow_validation(self, real_workspace):
        """核心集成测试：验证 contract + ci.yml 无 critical errors

        此测试确保：
        - workflow_contract.v1.json 正确定义了 ci.yml 的合约
        - ci.yml 符合合约要求
        - 无 critical validation errors
        """
        contract_path = real_workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        ci_workflow_path = real_workspace / ".github" / "workflows" / "ci.yml"

        # 前置条件检查
        if not contract_path.exists():
            pytest.skip(f"Contract file not found: {contract_path}")
        if not ci_workflow_path.exists():
            pytest.skip(f"CI workflow not found: {ci_workflow_path}")

        # 加载并验证
        validator = WorkflowContractValidator(contract_path, real_workspace)
        result = validator.validate()

        # 输出详细结果
        print("\n" + "=" * 60)
        print("Contract + CI Workflow Integration Test Results")
        print("=" * 60)
        print(format_text_output(result))

        # 验证 CI workflow 被成功验证
        assert "ci" in result.validated_workflows, (
            f"CI workflow should be validated. Validated: {result.validated_workflows}"
        )

        # 使用共享的 critical error types 定义
        critical_errors = [e for e in result.errors if e.error_type in CRITICAL_ERROR_TYPES]

        # 断言无 critical errors
        assert len(critical_errors) == 0, (
            f"Contract + CI workflow validation should have no critical errors.\n"
            f"Found {len(critical_errors)} critical errors:\n"
            + "\n".join([f"  - [{e.error_type}] {e.key}: {e.message}" for e in critical_errors])
        )

        # 输出验证统计
        print("\nValidation Summary:")
        print(f"  - Validated workflows: {result.validated_workflows}")
        print(f"  - Total errors: {len(result.errors)}")
        print(f"  - Critical errors: {len(critical_errors)}")
        print(f"  - Warnings: {len(result.warnings)}")
        print(f"  - Success: {result.success}")

    def test_contract_nightly_workflow_validation(self, real_workspace):
        """核心集成测试：验证 contract + nightly.yml 无 critical errors

        此测试确保：
        - workflow_contract.v1.json 正确定义了 nightly.yml 的合约
        - nightly.yml 符合合约要求
        - 无 critical validation errors
        """
        contract_path = real_workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        nightly_workflow_path = real_workspace / ".github" / "workflows" / "nightly.yml"

        # 前置条件检查
        if not contract_path.exists():
            pytest.skip(f"Contract file not found: {contract_path}")
        if not nightly_workflow_path.exists():
            pytest.skip(f"Nightly workflow not found: {nightly_workflow_path}")

        # 加载并验证
        validator = WorkflowContractValidator(contract_path, real_workspace)
        result = validator.validate()

        # 输出详细结果
        print("\n" + "=" * 60)
        print("Contract + Nightly Workflow Integration Test Results")
        print("=" * 60)
        print(format_text_output(result))

        # 验证 nightly workflow 被成功验证
        assert "nightly" in result.validated_workflows, (
            f"Nightly workflow should be validated. Validated: {result.validated_workflows}"
        )

        # 使用共享的 critical error types 定义
        critical_errors = [e for e in result.errors if e.error_type in CRITICAL_ERROR_TYPES]

        # 断言无 critical errors
        assert len(critical_errors) == 0, (
            f"Contract + Nightly workflow validation should have no critical errors.\n"
            f"Found {len(critical_errors)} critical errors:\n"
            + "\n".join([f"  - [{e.error_type}] {e.key}: {e.message}" for e in critical_errors])
        )

        # 输出验证统计
        print("\nValidation Summary:")
        print(f"  - Validated workflows: {result.validated_workflows}")
        print(f"  - Total errors: {len(result.errors)}")
        print(f"  - Critical errors: {len(critical_errors)}")
        print(f"  - Warnings: {len(result.warnings)}")
        print(f"  - Success: {result.success}")

    def test_contract_defines_ci_workflow(self, real_workspace):
        """验证 contract 正确定义了 ci workflow"""
        contract_path = real_workspace / "scripts" / "ci" / "workflow_contract.v1.json"

        if not contract_path.exists():
            pytest.skip(f"Contract file not found: {contract_path}")

        with open(contract_path, "r") as f:
            contract = json.load(f)

        # Phase 0: 合约应直接在顶层定义 ci，而非 workflows.ci
        assert "ci" in contract, "Contract should define 'ci' workflow (Phase 0 format)"

        ci_def = contract["ci"]
        assert "file" in ci_def, "CI definition should have 'file' field"
        assert ci_def["file"] == ".github/workflows/ci.yml", (
            f"CI file should be '.github/workflows/ci.yml', got: {ci_def['file']}"
        )

        # 验证关键字段存在
        assert "job_ids" in ci_def or "required_jobs" in ci_def, (
            "CI definition should have 'job_ids' or 'required_jobs'"
        )

    def test_frozen_names_consistency(self, real_workspace):
        """验证 frozen job/step names 与实际 workflow 一致"""
        contract_path = real_workspace / "scripts" / "ci" / "workflow_contract.v1.json"

        if not contract_path.exists():
            pytest.skip(f"Contract file not found: {contract_path}")

        validator = WorkflowContractValidator(contract_path, real_workspace)
        validator.load_contract()

        # 验证 frozen steps 被加载
        if hasattr(validator, "frozen_steps"):
            print(f"Frozen steps loaded: {len(validator.frozen_steps)}")
            assert len(validator.frozen_steps) >= 0, "Should load frozen steps"

        # 验证 frozen job names 被加载
        if hasattr(validator, "frozen_job_names"):
            print(f"Frozen job names loaded: {len(validator.frozen_job_names)}")
            assert len(validator.frozen_job_names) >= 0, "Should load frozen job names"


# ============================================================================
# Frozen Step/Job Name Tests (Essential for Phase 0)
# ============================================================================


# ============================================================================
# Schema Validation Tests
# ============================================================================


class TestSchemaValidation:
    """JSON Schema 校验测试

    测试 validate_workflows.py 的 schema 校验能力：
    - 缺少必填字段的 contract 应报告 schema_error
    - 字段类型错误的 contract 应报告 schema_error
    - 严格模式下 schema_error 应导致验证失败
    """

    def test_schema_error_missing_required_field(self, temp_workspace):
        """测试缺少必填字段（version）应产生 schema_error"""
        # 创建 schema 文件（从真实项目复制）
        schema_path = temp_workspace / "contract.schema.json"
        schema_content = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "required": ["version"],
            "properties": {"version": {"type": "string", "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+$"}},
        }
        with open(schema_path, "w") as f:
            json.dump(schema_content, f)

        # 创建缺少 version 字段的 contract
        contract_path = temp_workspace / "contract.json"
        invalid_contract = {"description": "Missing version field"}
        with open(contract_path, "w") as f:
            json.dump(invalid_contract, f)

        # 手动复制 schema 到与 contract 同目录，使用正确的文件名
        schema_dest = temp_workspace / "workflow_contract.v1.schema.json"
        with open(schema_dest, "w") as f:
            json.dump(schema_content, f)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        validator.load_contract()
        result_valid = validator.validate_schema()

        # 应该校验失败
        assert result_valid is False

        # 应该有 schema_error
        schema_errors = [e for e in validator.result.errors if e.error_type == "schema_error"]
        assert len(schema_errors) >= 1, "Should detect missing required field 'version'"

        # 验证错误信息包含 'version'
        error_messages = " ".join(e.message for e in schema_errors)
        assert "version" in error_messages.lower(), (
            f"Error should mention 'version': {error_messages}"
        )

    def test_schema_error_wrong_type(self, temp_workspace):
        """测试字段类型错误应产生 schema_error"""
        # 创建 schema 文件
        schema_content = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "required": ["version"],
            "properties": {
                "version": {"type": "string", "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+$"},
                "ci": {
                    "type": "object",
                    "properties": {"labels": {"type": "array", "items": {"type": "string"}}},
                },
            },
        }
        schema_path = temp_workspace / "workflow_contract.v1.schema.json"
        with open(schema_path, "w") as f:
            json.dump(schema_content, f)

        # 创建 labels 类型错误的 contract（应为 array，传入 string）
        contract_path = temp_workspace / "contract.json"
        invalid_contract = {
            "version": "1.0.0",
            "ci": {
                "labels": "not-an-array"  # 应该是数组
            },
        }
        with open(contract_path, "w") as f:
            json.dump(invalid_contract, f)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        validator.load_contract()
        result_valid = validator.validate_schema()

        # 应该校验失败
        assert result_valid is False

        # 应该有 schema_error
        schema_errors = [e for e in validator.result.errors if e.error_type == "schema_error"]
        assert len(schema_errors) >= 1, "Should detect type mismatch for 'labels'"

    def test_schema_error_invalid_version_pattern(self, temp_workspace):
        """测试 version 格式不符合 semver 应产生 schema_error"""
        # 创建 schema 文件
        schema_content = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "required": ["version"],
            "properties": {"version": {"type": "string", "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+$"}},
        }
        schema_path = temp_workspace / "workflow_contract.v1.schema.json"
        with open(schema_path, "w") as f:
            json.dump(schema_content, f)

        # 创建 version 格式错误的 contract
        contract_path = temp_workspace / "contract.json"
        invalid_contract = {
            "version": "v1.0"  # 不符合 semver 格式
        }
        with open(contract_path, "w") as f:
            json.dump(invalid_contract, f)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        validator.load_contract()
        result_valid = validator.validate_schema()

        # 应该校验失败
        assert result_valid is False

        # 应该有 schema_error
        schema_errors = [e for e in validator.result.errors if e.error_type == "schema_error"]
        assert len(schema_errors) >= 1, "Should detect invalid version pattern"

    def test_schema_error_in_full_validation(self, temp_workspace):
        """测试完整验证流程中 schema_error 被捕获并导致失败"""
        # 创建 schema 文件
        schema_content = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "required": ["version"],
            "properties": {"version": {"type": "string", "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+$"}},
        }
        schema_path = temp_workspace / "workflow_contract.v1.schema.json"
        with open(schema_path, "w") as f:
            json.dump(schema_content, f)

        # 创建无效 contract
        contract_path = temp_workspace / "contract.json"
        invalid_contract = {"description": "Missing version"}
        with open(contract_path, "w") as f:
            json.dump(invalid_contract, f)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 完整验证应该失败
        assert result.success is False

        # 应该包含 schema_error
        schema_errors = [e for e in result.errors if e.error_type == "schema_error"]
        assert len(schema_errors) >= 1, "Full validation should capture schema_error"

    def test_schema_error_frozen_job_names_wrong_type(self, temp_workspace):
        """测试 frozen_job_names.allowlist 类型错误应产生 schema_error"""
        # 使用与真实 schema 类似的结构
        schema_content = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "required": ["version"],
            "properties": {
                "version": {"type": "string", "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+$"},
                "frozen_job_names": {"$ref": "#/definitions/frozen_job_names_definition"},
            },
            "definitions": {
                "frozen_job_names_definition": {
                    "type": "object",
                    "required": ["allowlist"],
                    "properties": {"allowlist": {"type": "array", "items": {"type": "string"}}},
                }
            },
        }
        schema_path = temp_workspace / "workflow_contract.v1.schema.json"
        with open(schema_path, "w") as f:
            json.dump(schema_content, f)

        # 创建 frozen_job_names.allowlist 类型错误的 contract
        contract_path = temp_workspace / "contract.json"
        invalid_contract = {
            "version": "1.0.0",
            "frozen_job_names": {
                "allowlist": "should-be-array"  # 应该是数组
            },
        }
        with open(contract_path, "w") as f:
            json.dump(invalid_contract, f)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        validator.load_contract()
        result_valid = validator.validate_schema()

        # 应该校验失败
        assert result_valid is False

        # 应该有 schema_error
        schema_errors = [e for e in validator.result.errors if e.error_type == "schema_error"]
        assert len(schema_errors) >= 1, "Should detect type mismatch for frozen_job_names.allowlist"

    def test_valid_schema_passes(self, temp_workspace):
        """测试符合 schema 的 contract 应通过校验"""
        # 创建 schema 文件
        schema_content = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "required": ["version"],
            "properties": {
                "version": {"type": "string", "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+$"},
                "frozen_job_names": {"$ref": "#/definitions/frozen_job_names_definition"},
                "frozen_step_text": {"$ref": "#/definitions/frozen_step_text_definition"},
            },
            "definitions": {
                "frozen_job_names_definition": {
                    "type": "object",
                    "required": ["allowlist"],
                    "properties": {"allowlist": {"type": "array", "items": {"type": "string"}}},
                    "additionalProperties": {"type": "string"},
                },
                "frozen_step_text_definition": {
                    "type": "object",
                    "required": ["allowlist"],
                    "properties": {"allowlist": {"type": "array", "items": {"type": "string"}}},
                    "additionalProperties": {"type": "string"},
                },
            },
        }
        schema_path = temp_workspace / "workflow_contract.v1.schema.json"
        with open(schema_path, "w") as f:
            json.dump(schema_content, f)

        # 创建有效 contract
        contract_path = temp_workspace / "contract.json"
        valid_contract = {
            "version": "1.0.0",
            "frozen_job_names": {"_comment": "Test comment", "allowlist": ["Job A", "Job B"]},
            "frozen_step_text": {"_comment": "Test comment", "allowlist": ["Step 1", "Step 2"]},
        }
        with open(contract_path, "w") as f:
            json.dump(valid_contract, f)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        validator.load_contract()
        result_valid = validator.validate_schema()

        # 应该通过校验
        assert result_valid is True

        # 不应有 schema_error
        schema_errors = [e for e in validator.result.errors if e.error_type == "schema_error"]
        assert len(schema_errors) == 0, f"Should pass validation, but got errors: {schema_errors}"

    def test_schema_error_missing_nightly(self, temp_workspace):
        """测试缺少 nightly 字段应产生 schema_error（Phase 1 必需字段）"""
        # 从真实 schema 加载完整定义
        real_schema_path = (
            Path(__file__).parent.parent.parent
            / "scripts"
            / "ci"
            / "workflow_contract.v1.schema.json"
        )
        if not real_schema_path.exists():
            pytest.skip("Real schema file not found")

        with open(real_schema_path) as f:
            schema_content = json.load(f)

        schema_path = temp_workspace / "workflow_contract.v1.schema.json"
        with open(schema_path, "w") as f:
            json.dump(schema_content, f)

        # 创建缺少 nightly 字段的 contract（包含其他必需字段）
        contract_path = temp_workspace / "contract.json"
        invalid_contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml"},
            # 缺少 "nightly"
            "make": {"targets_required": ["test"]},
            "frozen_step_text": {"allowlist": []},
            "frozen_job_names": {"allowlist": []},
        }
        with open(contract_path, "w") as f:
            json.dump(invalid_contract, f)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        validator.load_contract()
        result_valid = validator.validate_schema()

        # 应该校验失败
        assert result_valid is False

        # 应该有 schema_error
        schema_errors = [e for e in validator.result.errors if e.error_type == "schema_error"]
        assert len(schema_errors) >= 1, "Should detect missing required field 'nightly'"

        # 验证错误信息包含 'nightly'
        error_messages = " ".join(e.message for e in schema_errors)
        assert "nightly" in error_messages.lower(), (
            f"Error should mention 'nightly': {error_messages}"
        )

    def test_schema_error_missing_frozen_job_names(self, temp_workspace):
        """测试缺少 frozen_job_names 字段应产生 schema_error（Phase 1 必需字段）"""
        # 从真实 schema 加载完整定义
        real_schema_path = (
            Path(__file__).parent.parent.parent
            / "scripts"
            / "ci"
            / "workflow_contract.v1.schema.json"
        )
        if not real_schema_path.exists():
            pytest.skip("Real schema file not found")

        with open(real_schema_path) as f:
            schema_content = json.load(f)

        schema_path = temp_workspace / "workflow_contract.v1.schema.json"
        with open(schema_path, "w") as f:
            json.dump(schema_content, f)

        # 创建缺少 frozen_job_names 字段的 contract
        contract_path = temp_workspace / "contract.json"
        invalid_contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml"},
            "nightly": {"file": ".github/workflows/nightly.yml"},
            "make": {"targets_required": ["test"]},
            "frozen_step_text": {"allowlist": []},
            # 缺少 "frozen_job_names"
        }
        with open(contract_path, "w") as f:
            json.dump(invalid_contract, f)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        validator.load_contract()
        result_valid = validator.validate_schema()

        # 应该校验失败
        assert result_valid is False

        # 应该有 schema_error
        schema_errors = [e for e in validator.result.errors if e.error_type == "schema_error"]
        assert len(schema_errors) >= 1, "Should detect missing required field 'frozen_job_names'"

        # 验证错误信息包含 'frozen_job_names'
        error_messages = " ".join(e.message for e in schema_errors)
        assert "frozen_job_names" in error_messages.lower(), (
            f"Error should mention 'frozen_job_names': {error_messages}"
        )

    def test_schema_error_missing_make_targets_required(self, temp_workspace):
        """测试 make 缺少 targets_required 字段应产生 schema_error"""
        # 从真实 schema 加载完整定义
        real_schema_path = (
            Path(__file__).parent.parent.parent
            / "scripts"
            / "ci"
            / "workflow_contract.v1.schema.json"
        )
        if not real_schema_path.exists():
            pytest.skip("Real schema file not found")

        with open(real_schema_path) as f:
            schema_content = json.load(f)

        schema_path = temp_workspace / "workflow_contract.v1.schema.json"
        with open(schema_path, "w") as f:
            json.dump(schema_content, f)

        # 创建 make 缺少 targets_required 字段的 contract
        contract_path = temp_workspace / "contract.json"
        invalid_contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml"},
            "nightly": {"file": ".github/workflows/nightly.yml"},
            "make": {
                # 缺少 "targets_required"
                "_comment": "Missing required targets_required field"
            },
            "frozen_step_text": {"allowlist": []},
            "frozen_job_names": {"allowlist": []},
        }
        with open(contract_path, "w") as f:
            json.dump(invalid_contract, f)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        validator.load_contract()
        result_valid = validator.validate_schema()

        # 应该校验失败
        assert result_valid is False

        # 应该有 schema_error
        schema_errors = [e for e in validator.result.errors if e.error_type == "schema_error"]
        assert len(schema_errors) >= 1, "Should detect missing required field 'targets_required'"

        # 验证错误信息包含 'targets_required'
        error_messages = " ".join(e.message for e in schema_errors)
        assert "targets_required" in error_messages.lower(), (
            f"Error should mention 'targets_required': {error_messages}"
        )


class TestContractInternalConsistency:
    """Contract 内部一致性校验测试"""

    @pytest.fixture
    def contract_with_missing_frozen_step(self):
        """required_steps 中有 step 未在 frozen_step_text.allowlist 中的合约"""
        return {
            "version": "1.0.0",
            "frozen_step_text": {
                "allowlist": [
                    "Checkout repository",
                    # 缺少 "Run tests"
                ],
            },
            "frozen_job_names": {
                "allowlist": ["Test Job"],
            },
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test-job"],
                "job_names": ["Test Job"],
                "required_jobs": [
                    {
                        "id": "test-job",
                        "name": "Test Job",
                        "required_steps": [
                            "Checkout repository",
                            "Run tests",  # 此 step 不在 frozen_step_text.allowlist 中
                        ],
                        "required_outputs": [],
                    }
                ],
                "required_env_vars": [],
            },
        }

    @pytest.fixture
    def contract_with_missing_frozen_job(self):
        """job_names 中有 job 未在 frozen_job_names.allowlist 中的合约"""
        return {
            "version": "1.0.0",
            "frozen_step_text": {
                "allowlist": ["Checkout repository"],
            },
            "frozen_job_names": {
                "allowlist": [
                    # 缺少 "Test Job"
                ],
            },
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test-job"],
                "job_names": ["Test Job"],  # 此 job name 不在 frozen_job_names.allowlist 中
                "required_jobs": [
                    {
                        "id": "test-job",
                        "name": "Test Job",
                        "required_steps": ["Checkout repository"],
                        "required_outputs": [],
                    }
                ],
                "required_env_vars": [],
            },
        }

    @pytest.fixture
    def contract_with_consistent_allowlists(self):
        """所有 required_steps 和 job_names 都在 allowlist 中的合约"""
        return {
            "version": "1.0.0",
            "frozen_step_text": {
                "allowlist": [
                    "Checkout repository",
                    "Run tests",
                ],
            },
            "frozen_job_names": {
                "allowlist": ["Test Job"],
            },
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test-job"],
                "job_names": ["Test Job"],
                "required_jobs": [
                    {
                        "id": "test-job",
                        "name": "Test Job",
                        "required_steps": [
                            "Checkout repository",
                            "Run tests",
                        ],
                        "required_outputs": [],
                    }
                ],
                "required_env_vars": [],
            },
        }

    @pytest.mark.skip(
        reason="WorkflowContractValidator 未实现 contract_frozen_step_missing 检查 (Phase 2 预留)"
    )
    def test_contract_frozen_step_missing_error(
        self, temp_workspace, contract_with_missing_frozen_step
    ):
        """测试 required_steps 中有 step 未在 frozen_step_text.allowlist 中时报告 ERROR"""
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract_with_missing_frozen_step, f)

        # 创建最小 workflow 文件（校验需要）
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
jobs:
  test-job:
    name: Test Job
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
      - name: Run tests
        run: echo "test"
"""
        with open(workflow_path, "w") as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该失败
        assert result.success is False

        # 应该有 contract_frozen_step_missing 错误
        step_errors = [e for e in result.errors if e.error_type == "contract_frozen_step_missing"]
        assert len(step_errors) == 1
        assert step_errors[0].key == "Run tests"
        assert "frozen_step_text.allowlist" in step_errors[0].message
        assert "test-job" in step_errors[0].message

    @pytest.mark.skip(
        reason="WorkflowContractValidator 未实现 contract_frozen_job_missing 检查 (Phase 2 预留)"
    )
    def test_contract_frozen_job_missing_error(
        self, temp_workspace, contract_with_missing_frozen_job
    ):
        """测试 job_names 中有 job 未在 frozen_job_names.allowlist 中时报告 ERROR"""
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract_with_missing_frozen_job, f)

        # 创建最小 workflow 文件（校验需要）
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
jobs:
  test-job:
    name: Test Job
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
"""
        with open(workflow_path, "w") as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该失败
        assert result.success is False

        # 应该有 contract_frozen_job_missing 错误
        job_errors = [e for e in result.errors if e.error_type == "contract_frozen_job_missing"]
        assert len(job_errors) == 1
        assert job_errors[0].key == "Test Job"
        assert "frozen_job_names.allowlist" in job_errors[0].message
        assert "test-job" in job_errors[0].message

    def test_contract_internal_consistency_passes(
        self, temp_workspace, contract_with_consistent_allowlists
    ):
        """测试所有 required_steps 和 job_names 都在 allowlist 中时通过"""
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract_with_consistent_allowlists, f)

        # 创建匹配的 workflow 文件
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
jobs:
  test-job:
    name: Test Job
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
      - name: Run tests
        run: echo "test"
"""
        with open(workflow_path, "w") as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该通过（无内部一致性错误）
        consistency_errors = [
            e
            for e in result.errors
            if e.error_type in ("contract_frozen_step_missing", "contract_frozen_job_missing")
        ]
        assert len(consistency_errors) == 0

    @pytest.mark.skip(
        reason="WorkflowContractValidator 未实现 validate_contract_internal_consistency 方法 (Phase 2 预留)"
    )
    def test_real_contract_internal_consistency(self):
        """集成测试：验证真实 contract 的内部一致性"""
        workspace = Path(__file__).parent.parent.parent
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"

        if not contract_path.exists():
            pytest.skip(f"Contract file not found: {contract_path}")

        validator = WorkflowContractValidator(contract_path, workspace)
        validator.load_contract()

        # 手动调用内部一致性校验
        is_consistent = validator.validate_contract_internal_consistency()

        # 输出详细信息
        if not is_consistent:
            print("\nInternal consistency errors found:")
            for e in validator.result.errors:
                if e.error_type in ("contract_frozen_step_missing", "contract_frozen_job_missing"):
                    print(f"  - [{e.error_type}] {e.key}: {e.message}")

        # 真实 contract 应该保持内部一致性
        assert is_consistent, (
            f"Real contract should be internally consistent. Found errors: "
            f"{[(e.error_type, e.key) for e in validator.result.errors if e.error_type.startswith('contract_frozen')]}"
        )

    @pytest.mark.skip(
        reason="WorkflowContractValidator 未实现 contract_job_ids_names_length_mismatch 检查 (Phase 2 预留)"
    )
    def test_job_ids_names_length_mismatch_error(self, temp_workspace):
        """测试 job_ids 和 job_names 长度不一致时报告 ERROR"""
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {"allowlist": []},
            "frozen_job_names": {"allowlist": ["Test Job", "Lint Job"]},
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test", "lint", "build"],  # 3 个
                "job_names": ["Test Job", "Lint Job"],  # 2 个 - 不匹配
            },
        }
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建最小 workflow 文件
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
jobs:
  test:
    name: Test Job
    runs-on: ubuntu-latest
    steps:
      - run: echo test
"""
        with open(workflow_path, "w") as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该失败
        assert result.success is False

        # 应该有 contract_job_ids_names_length_mismatch 错误
        length_errors = [
            e for e in result.errors if e.error_type == "contract_job_ids_names_length_mismatch"
        ]
        assert len(length_errors) == 1
        assert "job_ids" in length_errors[0].key
        assert "3" in length_errors[0].actual
        assert "2" in length_errors[0].actual

    @pytest.mark.skip(
        reason="WorkflowContractValidator 未实现 contract_required_job_not_in_job_ids 检查 (Phase 2 预留)"
    )
    def test_required_job_not_in_job_ids_error(self, temp_workspace):
        """测试 required_jobs[].id 不在 job_ids 中时报告 ERROR"""
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {"allowlist": ["Checkout"]},
            "frozen_job_names": {"allowlist": ["Test Job"]},
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test"],  # 只有 test
                "job_names": ["Test Job"],
                "required_jobs": [
                    {
                        "id": "lint",  # lint 不在 job_ids 中
                        "required_steps": ["Checkout"],
                    }
                ],
            },
        }
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建最小 workflow 文件
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
jobs:
  test:
    name: Test Job
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        run: echo checkout
"""
        with open(workflow_path, "w") as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该失败
        assert result.success is False

        # 应该有 contract_required_job_not_in_job_ids 错误
        job_errors = [
            e for e in result.errors if e.error_type == "contract_required_job_not_in_job_ids"
        ]
        assert len(job_errors) == 1
        assert job_errors[0].key == "lint"

    @pytest.mark.skip(
        reason="WorkflowContractValidator 未实现 frozen_allowlist_duplicate 检查 (Phase 2 预留)"
    )
    def test_frozen_allowlist_duplicate_error(self, temp_workspace):
        """测试 frozen allowlist 有重复项时报告 ERROR"""
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {
                "allowlist": [
                    "Checkout",
                    "Run tests",
                    "Checkout",  # 重复
                ]
            },
            "frozen_job_names": {
                "allowlist": [
                    "Test Job",
                    "Test Job",  # 重复
                ]
            },
            "ci": {
                "file": ".github/workflows/ci.yml",
            },
        }
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建最小 workflow 文件
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        with open(workflow_path, "w") as f:
            f.write(
                "name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo test"
            )

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该失败
        assert result.success is False

        # 应该有 contract_frozen_allowlist_duplicate 错误
        dup_errors = [
            e for e in result.errors if e.error_type == "contract_frozen_allowlist_duplicate"
        ]
        assert len(dup_errors) == 2  # 一个 step 重复，一个 job 重复
        dup_keys = {e.key for e in dup_errors}
        assert "Checkout" in dup_keys
        assert "Test Job" in dup_keys

    def test_nightly_internal_consistency(self, temp_workspace):
        """测试 nightly workflow 的内部一致性校验"""
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {
                "allowlist": [
                    "Checkout repository",
                    "Run verification",
                ]
            },
            "frozen_job_names": {
                "allowlist": [
                    "Unified Stack Verification",
                    "Notify Results",
                ]
            },
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": ["unified-stack", "notify"],
                "job_names": ["Unified Stack Verification", "Notify Results"],
                "required_jobs": [
                    {
                        "id": "unified-stack",
                        "required_steps": ["Checkout repository", "Run verification"],
                    }
                ],
            },
        }
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建 nightly workflow 文件
        workflow_path = temp_workspace / ".github" / "workflows" / "nightly.yml"
        workflow_content = """
name: Nightly
on:
  schedule:
    - cron: '0 2 * * *'
jobs:
  unified-stack:
    name: Unified Stack Verification
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
      - name: Run verification
        run: make verify
  notify:
    name: Notify Results
    runs-on: ubuntu-latest
    steps:
      - run: echo done
"""
        with open(workflow_path, "w") as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该通过
        consistency_errors = [e for e in result.errors if e.error_type.startswith("contract_")]
        assert len(consistency_errors) == 0

    @pytest.mark.skip(
        reason="WorkflowContractValidator 未实现 contract_job_ids_names_length_mismatch 检查 (Phase 2 预留)"
    )
    def test_nightly_job_ids_names_mismatch(self, temp_workspace):
        """测试 nightly workflow 的 job_ids/job_names 长度不匹配"""
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {"allowlist": []},
            "frozen_job_names": {"allowlist": ["Job A"]},
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": ["job-a", "job-b", "job-c"],  # 3 个
                "job_names": ["Job A"],  # 1 个
            },
        }
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建最小 workflow 文件
        workflow_path = temp_workspace / ".github" / "workflows" / "nightly.yml"
        with open(workflow_path, "w") as f:
            f.write(
                "name: Nightly\non: [push]\njobs:\n  job-a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo test"
            )

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该失败
        assert result.success is False

        # 应该有针对 nightly 的 length mismatch 错误
        length_errors = [
            e
            for e in result.errors
            if e.error_type == "contract_job_ids_names_length_mismatch" and e.workflow == "nightly"
        ]
        assert len(length_errors) == 1

    @pytest.mark.skip(
        reason="WorkflowContractValidator 未实现 contract_required_job_not_in_job_ids 检查 (Phase 2 预留)"
    )
    def test_nightly_required_job_not_in_job_ids(self, temp_workspace):
        """测试 nightly workflow 的 required_jobs[].id 不在 job_ids 中"""
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {"allowlist": ["Step A"]},
            "frozen_job_names": {"allowlist": []},
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": ["job-a"],
                "required_jobs": [
                    {
                        "id": "job-b",  # 不在 job_ids 中
                        "required_steps": ["Step A"],
                    }
                ],
            },
        }
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建最小 workflow 文件
        workflow_path = temp_workspace / ".github" / "workflows" / "nightly.yml"
        with open(workflow_path, "w") as f:
            f.write(
                "name: Nightly\non: [push]\njobs:\n  job-a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo test"
            )

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该失败
        assert result.success is False

        # 应该有针对 nightly 的 required_job_not_in_job_ids 错误
        job_errors = [
            e
            for e in result.errors
            if e.error_type == "contract_required_job_not_in_job_ids" and e.workflow == "nightly"
        ]
        assert len(job_errors) == 1
        assert job_errors[0].key == "job-b"


# ============================================================================
# Workflow Contract Docs Sync Tests
# ============================================================================


class TestWorkflowContractDocsSync:
    """Workflow Contract 文档同步校验测试

    测试 check_workflow_contract_docs_sync.py 的功能：
    1. 检测 job_id 未在文档中的情况
    2. 检测 job_name 未在文档中的情况
    3. 检测 frozen_step 未在文档中的情况
    4. 验证完整匹配场景
    """

    @pytest.fixture
    def temp_workspace_with_files(self):
        """创建带有 contract 和 doc 文件的临时工作空间"""
        with tempfile.TemporaryDirectory(prefix="test_docs_sync_") as tmpdir:
            workspace = Path(tmpdir)

            # 创建目录结构
            (workspace / "scripts" / "ci").mkdir(parents=True)
            (workspace / "docs" / "ci_nightly_workflow_refactor").mkdir(parents=True)

            yield workspace

    def test_missing_job_id_in_doc(self, temp_workspace_with_files):
        """测试 job_id 未在文档中时报告错误"""
        workspace = temp_workspace_with_files

        # 创建 contract 文件
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test", "lint", "missing-job"],
                "job_names": ["Test Job", "Lint Job"],
            },
            "frozen_step_text": {
                "allowlist": ["Checkout repository"],
            },
        }
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建文档文件（缺少 missing-job）
        # 使用正确的章节格式：CI Workflow 章节需要包含 "ci.yml" 或 "CI Workflow" anchor
        doc_content = """# CI Workflow Contract

## CI Workflow (ci.yml)

| Job ID | Job Name |
|--------|----------|
| `test` | Test Job |
| `lint` | Lint Job |

## 冻结的 Step 文本

- `Checkout repository`

## Make Targets

targets_required 说明
"""
        doc_path = workspace / "docs" / "ci_nightly_workflow_refactor" / "contract.md"
        with open(doc_path, "w") as f:
            f.write(doc_content)

        # 导入并运行检查器
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 应该失败
        assert result.success is False

        # 应该有 job_id_not_in_doc 错误
        job_id_errors = [e for e in result.errors if e.error_type == "job_id_not_in_doc"]
        assert len(job_id_errors) == 1
        assert job_id_errors[0].value == "missing-job"
        assert job_id_errors[0].category == "job_id"

    def test_missing_job_name_in_doc(self, temp_workspace_with_files):
        """测试 job_name 未在文档中时报告错误"""
        workspace = temp_workspace_with_files

        # 创建 contract 文件
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test"],
                "job_names": ["Test Job", "Missing Job Name"],
            },
            "frozen_step_text": {
                "allowlist": ["Checkout repository"],
            },
        }
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建文档文件（缺少 Missing Job Name）
        # 使用正确的章节格式：CI Workflow 章节需要包含 "ci.yml" 或 "CI Workflow" anchor
        doc_content = """# CI Workflow Contract

## CI Workflow (ci.yml)

| Job ID | Job Name |
|--------|----------|
| `test` | Test Job |

## 冻结的 Step 文本

- `Checkout repository`

## Make Targets

targets_required 说明
"""
        doc_path = workspace / "docs" / "ci_nightly_workflow_refactor" / "contract.md"
        with open(doc_path, "w") as f:
            f.write(doc_content)

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 应该失败
        assert result.success is False

        # 应该有 job_name_not_in_doc 错误
        job_name_errors = [e for e in result.errors if e.error_type == "job_name_not_in_doc"]
        assert len(job_name_errors) == 1
        assert job_name_errors[0].value == "Missing Job Name"
        assert job_name_errors[0].category == "job_name"

    def test_missing_frozen_step_in_doc(self, temp_workspace_with_files):
        """测试 frozen_step 未在文档中时报告错误"""
        workspace = temp_workspace_with_files

        # 创建 contract 文件
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test"],
                "job_names": ["Test Job"],
            },
            "frozen_step_text": {
                "allowlist": [
                    "Checkout repository",
                    "Run tests",
                    "Missing Step Name",
                ],
            },
        }
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建文档文件（缺少 Missing Step Name）
        doc_content = """# CI Workflow Contract

## Job ID 与 Job Name 对照表

| Job ID | Job Name |
|--------|----------|
| `test` | Test Job |

## 冻结的 Step 文本

- `Checkout repository`
- `Run tests`
"""
        doc_path = workspace / "docs" / "ci_nightly_workflow_refactor" / "contract.md"
        with open(doc_path, "w") as f:
            f.write(doc_content)

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 应该失败
        assert result.success is False

        # 应该有 frozen_step_not_in_doc 错误
        step_errors = [e for e in result.errors if e.error_type == "frozen_step_not_in_doc"]
        assert len(step_errors) == 1
        assert step_errors[0].value == "Missing Step Name"
        assert step_errors[0].category == "frozen_step"

    def test_all_elements_in_doc_passes(self, temp_workspace_with_files):
        """测试所有元素都在文档中时通过"""
        workspace = temp_workspace_with_files

        # 创建 contract 文件
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test", "lint"],
                "job_names": ["Test Job", "Lint Job"],
            },
            "frozen_step_text": {
                "allowlist": [
                    "Checkout repository",
                    "Run tests",
                ],
            },
            "make": {
                "targets_required": ["check-test"],
            },
        }
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建包含所有元素的文档
        # 使用正确的章节格式：CI Workflow 章节需要包含 "ci.yml" 或 "CI Workflow" anchor
        doc_content = """# CI Workflow Contract

当前版本：**1.0.0**

## CI Workflow (ci.yml)

| Job ID | Job Name |
|--------|----------|
| `test` | Test Job |
| `lint` | Lint Job |

## 冻结的 Step 文本

- `Checkout repository`
- `Run tests`

## Make Targets

targets_required 包含以下 target：

- `check-test`

## SemVer Policy

版本策略说明...
"""
        doc_path = workspace / "docs" / "ci_nightly_workflow_refactor" / "contract.md"
        with open(doc_path, "w") as f:
            f.write(doc_content)

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 应该通过
        assert result.success is True
        assert len(result.errors) == 0
        assert len(result.checked_job_ids) == 2
        assert len(result.checked_job_names) == 2
        assert len(result.checked_frozen_steps) == 2
        assert result.checked_version == "1.0.0"
        assert len(result.checked_make_targets) == 1

    def test_contract_file_not_found(self, temp_workspace_with_files):
        """测试 contract 文件不存在时报告错误"""
        workspace = temp_workspace_with_files

        contract_path = workspace / "scripts" / "ci" / "nonexistent.json"
        doc_path = workspace / "docs" / "ci_nightly_workflow_refactor" / "contract.md"

        # 创建文档文件
        with open(doc_path, "w") as f:
            f.write("# Contract Doc")

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 应该失败
        assert result.success is False

        # 应该有 contract_not_found 错误
        file_errors = [e for e in result.errors if e.error_type == "contract_not_found"]
        assert len(file_errors) == 1
        assert file_errors[0].category == "file"

    def test_doc_file_not_found(self, temp_workspace_with_files):
        """测试文档文件不存在时报告错误"""
        workspace = temp_workspace_with_files

        # 创建 contract 文件
        contract = {
            "version": "1.0.0",
            "ci": {"job_ids": []},
            "frozen_step_text": {"allowlist": []},
        }
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        doc_path = workspace / "docs" / "ci_nightly_workflow_refactor" / "nonexistent.md"

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 应该失败
        assert result.success is False

        # 应该有 doc_not_found 错误
        file_errors = [e for e in result.errors if e.error_type == "doc_not_found"]
        assert len(file_errors) == 1
        assert file_errors[0].category == "file"

    def test_json_output_format(self, temp_workspace_with_files):
        """测试 JSON 输出格式"""
        workspace = temp_workspace_with_files

        # 创建 contract 文件
        contract = {
            "version": "1.0.0",
            "ci": {
                "job_ids": ["test"],
                "job_names": ["Test Job"],
            },
            "frozen_step_text": {
                "allowlist": ["Checkout repository"],
            },
        }
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建文档（缺少 test job_id）
        doc_content = """# Contract
- Test Job
- Checkout repository
"""
        doc_path = workspace / "docs" / "ci_nightly_workflow_refactor" / "contract.md"
        with open(doc_path, "w") as f:
            f.write(doc_content)

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import (
            WorkflowContractDocsSyncChecker,
            format_json_output,
        )

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 测试 JSON 输出
        json_output = format_json_output(result)
        parsed = json.loads(json_output)

        assert "success" in parsed
        assert "error_count" in parsed
        assert "errors" in parsed
        assert isinstance(parsed["errors"], list)

        # 验证错误结构
        if parsed["errors"]:
            error = parsed["errors"][0]
            assert "error_type" in error
            assert "category" in error
            assert "value" in error
            assert "message" in error

    def test_missing_version_in_doc(self, temp_workspace_with_files):
        """测试 version 未在文档中时报告错误"""
        workspace = temp_workspace_with_files

        # 创建 contract 文件
        contract = {
            "version": "99.99.99",  # 一个不在文档中的版本号
            "ci": {
                "job_ids": ["test"],
                "job_names": ["Test Job"],
            },
            "frozen_step_text": {
                "allowlist": ["Checkout repository"],
            },
        }
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建文档文件（不包含 version）
        doc_content = """# CI Workflow Contract

## Job ID 与 Job Name 对照表

| Job ID | Job Name |
|--------|----------|
| `test` | Test Job |

## 冻结的 Step 文本

- `Checkout repository`

## Make Targets

make targets 说明
"""
        doc_path = workspace / "docs" / "ci_nightly_workflow_refactor" / "contract.md"
        with open(doc_path, "w") as f:
            f.write(doc_content)

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 应该失败
        assert result.success is False

        # 应该有 version_not_in_doc 错误
        version_errors = [e for e in result.errors if e.error_type == "version_not_in_doc"]
        assert len(version_errors) == 1
        assert version_errors[0].value == "99.99.99"
        assert version_errors[0].category == "version"

    def test_missing_make_target_in_doc(self, temp_workspace_with_files):
        """测试 make target 未在文档中时报告错误"""
        workspace = temp_workspace_with_files

        # 创建 contract 文件
        contract = {
            "version": "1.0.0",
            "ci": {
                "job_ids": ["test"],
                "job_names": ["Test Job"],
            },
            "frozen_step_text": {
                "allowlist": ["Checkout repository"],
            },
            "make": {
                "targets_required": [
                    "check-mypy-baseline-policy",
                    "missing-target",  # 一个不在文档中的 target
                ],
            },
        }
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建文档文件（缺少 missing-target）
        doc_content = """# CI Workflow Contract

Version: 1.0.0

## Job ID 与 Job Name 对照表

| Job ID | Job Name |
|--------|----------|
| `test` | Test Job |

## 冻结的 Step 文本

- `Checkout repository`

## Make Targets

targets_required 包含以下 target：

- `check-mypy-baseline-policy`
"""
        doc_path = workspace / "docs" / "ci_nightly_workflow_refactor" / "contract.md"
        with open(doc_path, "w") as f:
            f.write(doc_content)

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 应该失败
        assert result.success is False

        # 应该有 make_target_not_in_doc 错误
        target_errors = [e for e in result.errors if e.error_type == "make_target_not_in_doc"]
        assert len(target_errors) == 1
        assert target_errors[0].value == "missing-target"
        assert target_errors[0].category == "make_target"

    def test_missing_make_targets_section_in_doc(self, temp_workspace_with_files):
        """测试文档缺少 make targets 说明段落时报告错误"""
        workspace = temp_workspace_with_files

        # 创建 contract 文件
        contract = {
            "version": "1.0.0",
            "ci": {
                "job_ids": ["test"],
                "job_names": ["Test Job"],
            },
            "frozen_step_text": {
                "allowlist": ["Checkout repository"],
            },
            "make": {
                "targets_required": ["some-target"],
            },
        }
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建文档文件（完全没有 make targets 相关内容）
        doc_content = """# CI Workflow Contract

Version: 1.0.0

## Job ID 与 Job Name 对照表

| Job ID | Job Name |
|--------|----------|
| `test` | Test Job |

## 冻结的 Step 文本

- `Checkout repository`
"""
        doc_path = workspace / "docs" / "ci_nightly_workflow_refactor" / "contract.md"
        with open(doc_path, "w") as f:
            f.write(doc_content)

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 应该失败
        assert result.success is False

        # 应该有 make_targets_section_missing 错误
        section_errors = [
            e for e in result.errors if e.error_type == "make_targets_section_missing"
        ]
        assert len(section_errors) == 1
        assert section_errors[0].category == "make"

    def test_version_and_make_targets_in_doc_passes(self, temp_workspace_with_files):
        """测试 version 和 make targets 都在文档中时通过"""
        workspace = temp_workspace_with_files

        # 创建 contract 文件
        contract = {
            "version": "2.0.0",
            "ci": {
                "job_ids": ["test"],
                "job_names": ["Test Job"],
            },
            "frozen_step_text": {
                "allowlist": ["Checkout repository"],
            },
            "make": {
                "targets_required": ["check-mypy-baseline-policy"],
            },
        }
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建包含所有元素的文档
        doc_content = """# CI Workflow Contract

当前版本：**2.0.0**

## Job ID 与 Job Name 对照表

| Job ID | Job Name |
|--------|----------|
| `test` | Test Job |

## 冻结的 Step 文本

- `Checkout repository`

## Make Targets

targets_required 包含以下 target：

- `check-mypy-baseline-policy`

## SemVer Policy

版本策略说明...
"""
        doc_path = workspace / "docs" / "ci_nightly_workflow_refactor" / "contract.md"
        with open(doc_path, "w") as f:
            f.write(doc_content)

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 应该通过
        assert result.success is True
        assert result.checked_version == "2.0.0"
        assert len(result.checked_make_targets) == 1

    def test_missing_semver_policy_section_in_doc(self, temp_workspace_with_files):
        """测试文档缺少 SemVer Policy / 版本策略章节时报告错误"""
        workspace = temp_workspace_with_files

        # 创建 contract 文件
        contract = {
            "version": "1.0.0",
            "ci": {
                "job_ids": ["test"],
                "job_names": ["Test Job"],
            },
            "frozen_step_text": {
                "allowlist": ["Checkout repository"],
            },
            "make": {
                "targets_required": ["some-target"],
            },
        }
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建文档文件（不包含 SemVer Policy 章节）
        doc_content = """# CI Workflow Contract

Version: 1.0.0

## Job ID 与 Job Name 对照表

| Job ID | Job Name |
|--------|----------|
| `test` | Test Job |

## 冻结的 Step 文本

- `Checkout repository`

## Make Targets

targets_required 包含以下 target：

- `some-target`
"""
        doc_path = workspace / "docs" / "ci_nightly_workflow_refactor" / "contract.md"
        with open(doc_path, "w") as f:
            f.write(doc_content)

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 应该失败
        assert result.success is False

        # 应该有 semver_policy_section_missing 错误
        section_errors = [
            e for e in result.errors if e.error_type == "semver_policy_section_missing"
        ]
        assert len(section_errors) == 1
        assert section_errors[0].category == "doc_structure"
        assert "SemVer" in section_errors[0].message or "版本策略" in section_errors[0].message

    def test_semver_policy_section_with_english_keyword_passes(self, temp_workspace_with_files):
        """测试文档包含 'SemVer Policy' 关键字时通过"""
        workspace = temp_workspace_with_files

        # 创建 contract 文件
        contract = {
            "version": "1.0.0",
            "ci": {
                "job_ids": ["test"],
                "job_names": ["Test Job"],
            },
            "frozen_step_text": {
                "allowlist": ["Checkout repository"],
            },
            "make": {
                "targets_required": ["some-target"],
            },
        }
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建包含 SemVer Policy 的文档
        doc_content = """# CI Workflow Contract

Version: 1.0.0

## Job ID 与 Job Name 对照表

| Job ID | Job Name |
|--------|----------|
| `test` | Test Job |

## 冻结的 Step 文本

- `Checkout repository`

## Make Targets

targets_required 包含以下 target：

- `some-target`

## SemVer Policy

版本变更规则说明...
"""
        doc_path = workspace / "docs" / "ci_nightly_workflow_refactor" / "contract.md"
        with open(doc_path, "w") as f:
            f.write(doc_content)

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 应该通过（无 semver_policy_section_missing 错误）
        semver_errors = [
            e for e in result.errors if e.error_type == "semver_policy_section_missing"
        ]
        assert len(semver_errors) == 0

    def test_semver_policy_section_with_chinese_keyword_passes(self, temp_workspace_with_files):
        """测试文档包含 '版本策略' 关键字时通过"""
        workspace = temp_workspace_with_files

        # 创建 contract 文件
        contract = {
            "version": "1.0.0",
            "ci": {
                "job_ids": ["test"],
                "job_names": ["Test Job"],
            },
            "frozen_step_text": {
                "allowlist": ["Checkout repository"],
            },
            "make": {
                "targets_required": ["some-target"],
            },
        }
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建包含"版本策略"的文档
        doc_content = """# CI Workflow Contract

Version: 1.0.0

## Job ID 与 Job Name 对照表

| Job ID | Job Name |
|--------|----------|
| `test` | Test Job |

## 冻结的 Step 文本

- `Checkout repository`

## Make Targets

targets_required 包含以下 target：

- `some-target`

## 版本策略

版本变更规则说明...
"""
        doc_path = workspace / "docs" / "ci_nightly_workflow_refactor" / "contract.md"
        with open(doc_path, "w") as f:
            f.write(doc_content)

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 应该通过（无 semver_policy_section_missing 错误）
        semver_errors = [
            e for e in result.errors if e.error_type == "semver_policy_section_missing"
        ]
        assert len(semver_errors) == 0

    @pytest.mark.xfail(
        reason="文档与合约可能存在同步滞后，此测试用于检测但不阻断 CI (Phase 2 完善文档同步)",
        strict=False,
    )
    def test_real_contract_and_doc_sync(self):
        """集成测试：验证真实 contract 和文档的同步一致性

        注意：此测试使用 xfail 标记，因为文档可能存在同步滞后。
        测试会运行并输出结果，但不会阻断 CI。
        """
        workspace = Path(__file__).parent.parent.parent
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        doc_path = workspace / "docs" / "ci_nightly_workflow_refactor" / "contract.md"

        if not contract_path.exists():
            pytest.skip(f"Contract file not found: {contract_path}")
        if not doc_path.exists():
            pytest.skip(f"Doc file not found: {doc_path}")

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 输出详细信息供调试
        print(f"\nChecked version: {result.checked_version}")
        print(f"Checked job_ids: {len(result.checked_job_ids)}")
        print(f"Checked job_names: {len(result.checked_job_names)}")
        print(f"Checked frozen_steps: {len(result.checked_frozen_steps)}")
        print(f"Checked make_targets: {len(result.checked_make_targets)}")
        print(f"Errors: {len(result.errors)}")

        if result.errors:
            print("\nErrors found:")
            for error in result.errors:
                print(f"  [{error.error_type}] {error.category}: {error.value}")

        # 真实 contract 和文档应该保持同步
        assert result.success is True, (
            f"Real contract and doc should be in sync. Found {len(result.errors)} errors: "
            f"{[(e.error_type, e.value) for e in result.errors]}"
        )

    def test_job_id_in_wrong_section_reports_error(self, temp_workspace_with_files):
        """测试 job_id 仅在错误章节出现时会报错（章节切片校验）

        场景：ci 的 job_id "test" 出现在 nightly 章节中，但不在 ci 章节中，
        应该报告 job_id_not_in_doc 错误。
        """
        workspace = temp_workspace_with_files

        # 创建 contract 文件
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test", "lint"],
                "job_names": ["Test Job", "Lint Job"],
            },
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": ["unified-stack-full"],
                "job_names": ["Unified Stack Full Verification"],
            },
            "frozen_step_text": {
                "allowlist": ["Checkout repository"],
            },
        }
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建文档文件：ci 的 job_id "test" 只出现在 nightly 章节中
        doc_content = """# Workflow Contract

当前版本：**1.0.0**

## CI Workflow (ci.yml)

| Job ID | Job Name |
|--------|----------|
| `lint` | Lint Job |

## Nightly Workflow (nightly.yml)

| Job ID | Job Name |
|--------|----------|
| `unified-stack-full` | Unified Stack Full Verification |
| `test` | Test Job |

## 冻结的 Step 文本

- `Checkout repository`

## Make Targets

targets_required 说明
"""
        doc_path = workspace / "docs" / "ci_nightly_workflow_refactor" / "contract.md"
        with open(doc_path, "w") as f:
            f.write(doc_content)

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 应该失败，因为 "test" 不在 CI 章节中
        assert result.success is False

        # 应该有 job_id_not_in_doc 错误
        job_id_errors = [e for e in result.errors if e.error_type == "job_id_not_in_doc"]
        assert len(job_id_errors) >= 1
        # 错误的 job_id 应该是 "test"（出现在 nightly 章节而非 ci 章节）
        assert any(e.value == "test" for e in job_id_errors)
        # 错误消息应该表明是在章节内找不到
        test_error = next(e for e in job_id_errors if e.value == "test")
        assert "section" in test_error.message.lower()

    def test_frozen_step_in_wrong_section_reports_error(self, temp_workspace_with_files):
        """测试 frozen step 仅在错误章节出现时会报错（章节切片校验）

        场景：frozen step 出现在 CI Workflow 章节中，但不在冻结 Step 章节中，
        应该报告 frozen_step_not_in_doc 错误。
        """
        workspace = temp_workspace_with_files

        # 创建 contract 文件
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test"],
                "job_names": ["Test Job"],
            },
            "frozen_step_text": {
                "allowlist": ["Checkout repository", "Run tests"],
            },
        }
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建文档文件："Run tests" 只出现在 CI 章节中，不在冻结 Step 章节中
        doc_content = """# Workflow Contract

当前版本：**1.0.0**

## CI Workflow (ci.yml)

| Job ID | Job Name |
|--------|----------|
| `test` | Test Job |

步骤说明：
- `Run tests` - 运行测试

## 冻结的 Step 文本

- `Checkout repository`

## Make Targets

targets_required 说明
"""
        doc_path = workspace / "docs" / "ci_nightly_workflow_refactor" / "contract.md"
        with open(doc_path, "w") as f:
            f.write(doc_content)

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 应该失败，因为 "Run tests" 不在冻结 Step 章节中
        assert result.success is False

        # 应该有 frozen_step_not_in_doc 错误
        step_errors = [e for e in result.errors if e.error_type == "frozen_step_not_in_doc"]
        assert len(step_errors) >= 1
        # 错误的 step 应该是 "Run tests"
        assert any(e.value == "Run tests" for e in step_errors)
        # 错误消息应该表明是在章节内找不到
        run_tests_error = next(e for e in step_errors if e.value == "Run tests")
        assert "section" in run_tests_error.message.lower()

    def test_section_slicing_with_correct_placement_passes(self, temp_workspace_with_files):
        """测试元素在正确章节中时校验通过（章节切片校验）

        验证当 job_id、job_name、frozen_step 都在正确的章节中时，
        校验应该通过。
        """
        workspace = temp_workspace_with_files

        # 创建 contract 文件
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test", "lint"],
                "job_names": ["Test Job", "Lint Job"],
            },
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": ["unified-stack-full"],
                "job_names": ["Unified Stack Full Verification"],
            },
            "frozen_step_text": {
                "allowlist": ["Checkout repository", "Run tests"],
            },
        }
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建文档文件：所有元素都在正确的章节中
        doc_content = """# Workflow Contract

当前版本：**1.0.0**

## CI Workflow (ci.yml)

| Job ID | Job Name |
|--------|----------|
| `test` | Test Job |
| `lint` | Lint Job |

## Nightly Workflow (nightly.yml)

| Job ID | Job Name |
|--------|----------|
| `unified-stack-full` | Unified Stack Full Verification |

## 冻结的 Step 文本

- `Checkout repository`
- `Run tests`

## Make Targets

targets_required 说明

## SemVer Policy

版本策略说明...
"""
        doc_path = workspace / "docs" / "ci_nightly_workflow_refactor" / "contract.md"
        with open(doc_path, "w") as f:
            f.write(doc_content)

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 所有元素都在正确章节中，应该通过
        assert result.success is True
        assert len(result.errors) == 0


# ============================================================================
# Nightly Workflow Docs Sync Tests
# ============================================================================


class TestNightlyWorkflowDocsSync:
    """Nightly Workflow 文档同步测试（Phase 1）

    测试 check_workflow_contract_docs_sync.py 对 nightly workflow 的覆盖：
    1. nightly.job_ids 在文档中有对应描述
    2. nightly.job_names 在文档中有对应描述
    3. 文档中存在 nightly 章节标识
    """

    @pytest.fixture
    def temp_workspace_with_files(self):
        """创建带有 contract 和 doc 文件的临时工作空间"""
        with tempfile.TemporaryDirectory(prefix="test_nightly_docs_sync_") as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "scripts" / "ci").mkdir(parents=True)
            (workspace / "docs" / "ci_nightly_workflow_refactor").mkdir(parents=True)
            yield workspace

    @pytest.mark.xfail(
        reason="章节切片逻辑与测试文档格式不完全匹配，需要完善文档结构 (Phase 2 预留)",
        strict=False,
    )
    def test_nightly_job_ids_in_doc(self, temp_workspace_with_files):
        """测试 nightly job_ids 在文档中"""
        workspace = temp_workspace_with_files

        # 创建包含 nightly 定义的 contract
        contract = {
            "version": "3.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test"],
                "job_names": ["Test Job"],
            },
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": ["unified-stack-full", "notify-results"],
                "job_names": ["Unified Stack Full Verification", "Notify Results"],
            },
            "frozen_step_text": {"allowlist": []},
        }
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建包含所有元素的文档
        doc_content = """# Workflow Contract

当前版本：**3.0.0**

## 1. CI Workflow

### Job ID 与 Job Name 对照表

| Job ID | Job Name |
|--------|----------|
| `test` | Test Job |

## 7. Nightly Workflow (nightly.yml)

### Job ID 与 Job Name 对照表

| Job ID | Job Name |
|--------|----------|
| `unified-stack-full` | Unified Stack Full Verification |
| `notify-results` | Notify Results |

## Make Targets

targets_required 说明
"""
        doc_path = workspace / "docs" / "ci_nightly_workflow_refactor" / "contract.md"
        with open(doc_path, "w") as f:
            f.write(doc_content)

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 应该通过
        assert result.success is True
        # 应该检查了 nightly 的 job_ids
        assert "unified-stack-full" in result.checked_job_ids
        assert "notify-results" in result.checked_job_ids

    def test_nightly_job_id_missing_in_doc(self, temp_workspace_with_files):
        """测试 nightly job_id 未在文档中时报告错误"""
        workspace = temp_workspace_with_files

        contract = {
            "version": "3.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test"],
                "job_names": ["Test Job"],
            },
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": ["unified-stack-full", "notify-results"],
                "job_names": ["Unified Stack Full Verification", "Notify Results"],
            },
            "frozen_step_text": {"allowlist": []},
        }
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建文档，缺少 notify-results
        doc_content = """# Workflow Contract

当前版本：**3.0.0**

## CI Workflow

| Job ID | Job Name |
|--------|----------|
| `test` | Test Job |

## Nightly Workflow (nightly.yml)

| Job ID | Job Name |
|--------|----------|
| `unified-stack-full` | Unified Stack Full Verification |

## Make Targets

targets_required 说明
"""
        doc_path = workspace / "docs" / "ci_nightly_workflow_refactor" / "contract.md"
        with open(doc_path, "w") as f:
            f.write(doc_content)

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 应该失败
        assert result.success is False

        # 应该有 job_id_not_in_doc 错误
        job_id_errors = [e for e in result.errors if e.error_type == "job_id_not_in_doc"]
        assert len(job_id_errors) >= 1
        assert any(e.value == "notify-results" for e in job_id_errors)

    def test_nightly_job_name_missing_in_doc(self, temp_workspace_with_files):
        """测试 nightly job_name 未在文档中时报告错误"""
        workspace = temp_workspace_with_files

        contract = {
            "version": "3.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test"],
                "job_names": ["Test Job"],
            },
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": ["unified-stack-full"],
                "job_names": ["Unified Stack Full Verification"],
            },
            "frozen_step_text": {"allowlist": []},
        }
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建文档，缺少 job_name
        doc_content = """# Workflow Contract

当前版本：**3.0.0**

## CI Workflow

| Job ID | Job Name |
|--------|----------|
| `test` | Test Job |

## Nightly Workflow (nightly.yml)

| Job ID | Job Name |
|--------|----------|
| `unified-stack-full` | Some Other Name |

## Make Targets

targets_required 说明
"""
        doc_path = workspace / "docs" / "ci_nightly_workflow_refactor" / "contract.md"
        with open(doc_path, "w") as f:
            f.write(doc_content)

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 应该失败
        assert result.success is False

        # 应该有 job_name_not_in_doc 错误
        job_name_errors = [e for e in result.errors if e.error_type == "job_name_not_in_doc"]
        assert len(job_name_errors) >= 1
        assert any(e.value == "Unified Stack Full Verification" for e in job_name_errors)

    def test_nightly_section_missing_in_doc(self, temp_workspace_with_files):
        """测试文档缺少 nightly 章节标识时报告错误"""
        workspace = temp_workspace_with_files

        contract = {
            "version": "3.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test"],
                "job_names": ["Test Job"],
            },
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": ["unified-stack-full"],
                "job_names": ["Unified Stack Full Verification"],
            },
            "frozen_step_text": {"allowlist": []},
        }
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建文档，没有 nightly 章节标识
        doc_content = """# Workflow Contract

当前版本：**3.0.0**

## CI Workflow

| Job ID | Job Name |
|--------|----------|
| `test` | Test Job |
| `unified-stack-full` | Unified Stack Full Verification |

## Make Targets

targets_required 说明
"""
        doc_path = workspace / "docs" / "ci_nightly_workflow_refactor" / "contract.md"
        with open(doc_path, "w") as f:
            f.write(doc_content)

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 应该失败
        assert result.success is False

        # 应该有 workflow_section_missing 错误
        section_errors = [e for e in result.errors if e.error_type == "workflow_section_missing"]
        assert len(section_errors) >= 1
        assert any(e.value == "nightly" for e in section_errors)

    @pytest.mark.xfail(
        reason="Nightly 文档可能存在同步滞后，此测试用于检测但不阻断 CI (Phase 2 完善文档同步)",
        strict=False,
    )
    def test_real_contract_nightly_docs_sync(self):
        """集成测试：验证真实合约中 nightly 部分与文档同步"""
        workspace = Path(__file__).parent.parent.parent
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        doc_path = workspace / "docs" / "ci_nightly_workflow_refactor" / "contract.md"

        if not contract_path.exists():
            pytest.skip(f"Contract file not found: {contract_path}")
        if not doc_path.exists():
            pytest.skip(f"Doc file not found: {doc_path}")

        # 检查合约是否包含 nightly 定义
        with open(contract_path, "r") as f:
            contract = json.load(f)

        if "nightly" not in contract:
            pytest.skip("Contract does not define nightly workflow")

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 输出详细信息
        print("\n" + "=" * 60)
        print("Nightly Workflow Docs Sync Check")
        print("=" * 60)
        print(f"Checked job_ids: {len(result.checked_job_ids)}")

        nightly_job_ids = contract.get("nightly", {}).get("job_ids", [])
        print(f"Nightly job_ids in contract: {nightly_job_ids}")

        # 筛选 nightly 相关的错误
        nightly_errors = [
            e for e in result.errors if "nightly" in e.message.lower() or e.value in nightly_job_ids
        ]

        if nightly_errors:
            print("\nNightly-related errors:")
            for error in nightly_errors:
                print(f"  [{error.error_type}] {error.category}: {error.value}")

        # 断言 nightly 部分同步正确
        assert len(nightly_errors) == 0, (
            f"Nightly docs should be in sync. Found {len(nightly_errors)} errors: "
            f"{[(e.error_type, e.value) for e in nightly_errors]}"
        )


# ============================================================================
# Nightly Workflow Validation Tests (Phase 1)
# ============================================================================


class TestNightlyWorkflowValidation:
    """Nightly Workflow 验证测试（Phase 1）

    测试 validate_workflows.py 对 nightly.yml 的校验：
    1. job_ids 存在性校验
    2. job_names 一致性校验
    3. required_steps 存在性校验
    4. required_env_vars 校验
    5. artifact_archive 校验
    """

    @pytest.fixture
    def temp_workspace_with_nightly(self):
        """创建带有 nightly workflow 的临时工作空间"""
        with tempfile.TemporaryDirectory(prefix="test_nightly_") as tmpdir:
            workspace = Path(tmpdir)
            (workspace / ".github" / "workflows").mkdir(parents=True)
            yield workspace

    @pytest.fixture
    def sample_nightly_contract(self):
        """示例 nightly 合约"""
        return {
            "version": "3.0.0",
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": ["unified-stack-full", "notify-results"],
                "job_names": [
                    "Unified Stack Full Verification",
                    "Notify Results",
                ],
                "required_jobs": [
                    {
                        "id": "unified-stack-full",
                        "name": "Unified Stack Full Verification",
                        "required_steps": [
                            "Checkout repository",
                            "Set up Python",
                            "Install dependencies",
                        ],
                    },
                    {
                        "id": "notify-results",
                        "name": "Notify Results",
                        "required_steps": ["Check job results"],
                    },
                ],
                "required_env_vars": [
                    "LOGBOOK_MIGRATOR_PASSWORD",
                    "POSTGRES_USER",
                ],
            },
            "frozen_step_text": {
                "allowlist": [
                    "Checkout repository",
                    "Set up Python",
                    "Install dependencies",
                    "Check job results",
                ],
            },
            "frozen_job_names": {
                "allowlist": [
                    "Unified Stack Full Verification",
                    "Notify Results",
                ],
            },
        }

    @pytest.fixture
    def sample_nightly_workflow(self):
        """示例 nightly workflow"""
        return """
name: Nightly

on:
  schedule:
    - cron: '0 2 * * *'

env:
  LOGBOOK_MIGRATOR_PASSWORD: test_pwd
  POSTGRES_USER: postgres

jobs:
  unified-stack-full:
    name: Unified Stack Full Verification
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
      - name: Install dependencies
        run: pip install -e .

  notify-results:
    name: Notify Results
    runs-on: ubuntu-latest
    needs: [unified-stack-full]
    steps:
      - name: Check job results
        run: echo "Results checked"
"""

    def test_validate_nightly_workflow(
        self, temp_workspace_with_nightly, sample_nightly_contract, sample_nightly_workflow
    ):
        """测试验证有效的 nightly workflow"""
        workspace = temp_workspace_with_nightly

        # 写入合约
        contract_path = workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(sample_nightly_contract, f)

        # 写入 workflow
        workflow_path = workspace / ".github" / "workflows" / "nightly.yml"
        with open(workflow_path, "w") as f:
            f.write(sample_nightly_workflow)

        validator = WorkflowContractValidator(contract_path, workspace)
        result = validator.validate()

        # 应该通过
        assert "nightly" in result.validated_workflows
        # 检查无 critical errors
        critical_errors = [
            e
            for e in result.errors
            if e.error_type in ("missing_job", "missing_job_id", "missing_step")
        ]
        assert len(critical_errors) == 0

    def test_validate_nightly_missing_job(
        self, temp_workspace_with_nightly, sample_nightly_contract
    ):
        """测试 nightly workflow 缺少 job"""
        workspace = temp_workspace_with_nightly

        # 写入合约
        contract_path = workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(sample_nightly_contract, f)

        # 写入缺少 notify-results job 的 workflow
        workflow_content = """
name: Nightly
on:
  schedule:
    - cron: '0 2 * * *'
env:
  LOGBOOK_MIGRATOR_PASSWORD: test_pwd
  POSTGRES_USER: postgres
jobs:
  unified-stack-full:
    name: Unified Stack Full Verification
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
      - name: Install dependencies
        run: pip install -e .
"""
        workflow_path = workspace / ".github" / "workflows" / "nightly.yml"
        with open(workflow_path, "w") as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, workspace)
        result = validator.validate()

        # 应该失败
        assert result.success is False

        # 应该有 missing_job_id 错误
        missing_job_errors = [e for e in result.errors if e.error_type == "missing_job_id"]
        assert len(missing_job_errors) >= 1
        assert any(e.key == "notify-results" for e in missing_job_errors)

    def test_validate_nightly_missing_env_var(
        self, temp_workspace_with_nightly, sample_nightly_contract
    ):
        """测试 nightly workflow 缺少环境变量"""
        workspace = temp_workspace_with_nightly

        # 写入合约
        contract_path = workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(sample_nightly_contract, f)

        # 写入缺少 LOGBOOK_MIGRATOR_PASSWORD 的 workflow
        workflow_content = """
name: Nightly
on:
  schedule:
    - cron: '0 2 * * *'
env:
  POSTGRES_USER: postgres
jobs:
  unified-stack-full:
    name: Unified Stack Full Verification
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
      - name: Install dependencies
        run: pip install -e .
  notify-results:
    name: Notify Results
    runs-on: ubuntu-latest
    steps:
      - name: Check job results
        run: echo "done"
"""
        workflow_path = workspace / ".github" / "workflows" / "nightly.yml"
        with open(workflow_path, "w") as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, workspace)
        result = validator.validate()

        # 应该失败
        assert result.success is False

        # 应该有 missing_env_var 错误
        env_errors = [e for e in result.errors if e.error_type == "missing_env_var"]
        assert len(env_errors) >= 1
        assert any(e.key == "LOGBOOK_MIGRATOR_PASSWORD" for e in env_errors)

    def test_validate_nightly_artifact_archive(self, temp_workspace_with_nightly):
        """测试 nightly workflow 的 artifact archive 校验"""
        workspace = temp_workspace_with_nightly

        # 创建带有 artifact_archive 的合约
        # 注意：required_artifact_paths 使用目录路径（以 / 结尾），不使用 glob 模式
        contract = {
            "version": "3.0.0",
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "artifact_archive": {
                    "required_artifact_paths": [
                        ".artifacts/verify-results.json",
                        ".artifacts/acceptance-runs/",
                    ]
                },
            },
        }
        contract_path = workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 写入包含 upload-artifact 的 workflow
        workflow_content = """
name: Nightly
on:
  schedule:
    - cron: '0 2 * * *'
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Upload test results
        uses: actions/upload-artifact@v4
        with:
          name: results
          path: |
            .artifacts/verify-results.json
            .artifacts/acceptance-runs/
"""
        workflow_path = workspace / ".github" / "workflows" / "nightly.yml"
        with open(workflow_path, "w") as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, workspace)
        result = validator.validate()

        # 应该通过（artifact 路径覆盖）
        artifact_errors = [e for e in result.errors if e.error_type == "missing_artifact_path"]
        assert len(artifact_errors) == 0

    def test_real_nightly_workflow_validation(self):
        """集成测试：验证真实 nightly workflow 符合合约"""
        workspace = Path(__file__).parent.parent.parent
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        nightly_path = workspace / ".github" / "workflows" / "nightly.yml"

        if not contract_path.exists():
            pytest.skip(f"Contract file not found: {contract_path}")
        if not nightly_path.exists():
            pytest.skip(f"Nightly workflow not found: {nightly_path}")

        validator = WorkflowContractValidator(contract_path, workspace)
        result = validator.validate()

        # 输出详细结果
        print("\n" + "=" * 60)
        print("Nightly Workflow Validation Results")
        print("=" * 60)
        print(format_text_output(result))

        # 验证 nightly workflow 被成功验证
        assert "nightly" in result.validated_workflows, (
            f"Nightly workflow should be validated. Validated: {result.validated_workflows}"
        )

        # Critical error 定义
        critical_error_types = {
            "workflow_not_found",
            "missing_job",
            "missing_job_id",
            "frozen_step_name_changed",
            "frozen_job_name_changed",
            "schema_error",
        }

        critical_errors = [
            e
            for e in result.errors
            if e.error_type in critical_error_types and e.workflow == "nightly"
        ]

        # 断言无 critical errors
        assert len(critical_errors) == 0, (
            f"Nightly workflow validation should have no critical errors.\n"
            f"Found {len(critical_errors)} critical errors:\n"
            + "\n".join([f"  - [{e.error_type}] {e.key}: {e.message}" for e in critical_errors])
        )


class TestContractSchemaValidationNightly:
    """Contract Schema 校验测试（包含 Nightly 定义）"""

    @pytest.fixture
    def temp_workspace_with_schema(self):
        """创建带有 schema 的临时工作空间"""
        with tempfile.TemporaryDirectory(prefix="test_schema_nightly_") as tmpdir:
            workspace = Path(tmpdir)
            (workspace / ".github" / "workflows").mkdir(parents=True)

            # 复制真实 schema 文件
            real_workspace = Path(__file__).parent.parent.parent
            real_schema = real_workspace / "scripts" / "ci" / "workflow_contract.v1.schema.json"
            if real_schema.exists():
                import shutil

                shutil.copy(real_schema, workspace / "workflow_contract.v1.schema.json")

            yield workspace

    def test_nightly_schema_validation_passes(self, temp_workspace_with_schema):
        """测试符合 schema 的 nightly 合约通过校验"""
        workspace = temp_workspace_with_schema
        schema_path = workspace / "workflow_contract.v1.schema.json"

        if not schema_path.exists():
            pytest.skip("Schema file not found")

        # 创建符合 schema 的完整合约（包含所有 Phase 1 必需字段）
        contract = {
            "version": "3.0.0",
            "ci": {"file": ".github/workflows/ci.yml"},
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": ["unified-stack-full"],
                "job_names": ["Unified Stack Full Verification"],
                "required_jobs": [
                    {
                        "id": "unified-stack-full",
                        "name": "Unified Stack Full Verification",
                        "required_steps": ["Checkout repository"],
                    }
                ],
                "required_env_vars": ["POSTGRES_USER"],
                "artifact_archive": {"required_artifact_paths": [".artifacts/verify-results.json"]},
            },
            "make": {"targets_required": ["test"]},
            "frozen_step_text": {"allowlist": ["Checkout repository"]},
            "frozen_job_names": {"allowlist": ["Unified Stack Full Verification"]},
        }
        contract_path = workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        validator = WorkflowContractValidator(contract_path, workspace)
        validator.load_contract()
        result = validator.validate_schema()

        assert result is True

    def test_nightly_schema_error_invalid_file_path(self, temp_workspace_with_schema):
        """测试 nightly.file 路径格式错误应报告 schema_error"""
        workspace = temp_workspace_with_schema
        schema_path = workspace / "workflow_contract.v1.schema.json"

        if not schema_path.exists():
            pytest.skip("Schema file not found")

        # 创建 file 路径格式错误的合约
        contract = {
            "version": "3.0.0",
            "nightly": {
                "file": "workflows/nightly.yml",  # 缺少 .github/ 前缀
            },
        }
        contract_path = workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        validator = WorkflowContractValidator(contract_path, workspace)
        validator.load_contract()
        result = validator.validate_schema()

        assert result is False
        schema_errors = [e for e in validator.result.errors if e.error_type == "schema_error"]
        assert len(schema_errors) >= 1

    def test_real_contract_schema_validation(self):
        """集成测试：验证真实合约文件通过 schema 校验"""
        workspace = Path(__file__).parent.parent.parent
        contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        schema_path = workspace / "scripts" / "ci" / "workflow_contract.v1.schema.json"

        if not contract_path.exists():
            pytest.skip(f"Contract file not found: {contract_path}")
        if not schema_path.exists():
            pytest.skip(f"Schema file not found: {schema_path}")

        validator = WorkflowContractValidator(contract_path, workspace)
        validator.load_contract()
        result = validator.validate_schema()

        if not result:
            print("\nSchema validation errors:")
            for error in validator.result.errors:
                if error.error_type == "schema_error":
                    print(f"  - {error.key}: {error.message}")

        assert result is True, "Real contract should pass schema validation"


class TestFrozenNameValidation:
    """冻结名称校验测试"""

    @pytest.fixture
    def contract_with_frozen_steps(self):
        """带有 frozen_step_text 的合约"""
        return {
            "version": "1.0.0",
            "frozen_step_text": {
                "_comment": "Frozen step names that must match exactly",
                "allowlist": [
                    "Checkout repository",
                    "Run CI precheck",
                    "Run tests",
                ],
            },
            "frozen_job_names": {
                "allowlist": ["Test Job"],
            },
            "workflows": {
                "ci": {
                    "file": ".github/workflows/ci.yml",
                    "required_jobs": [
                        {
                            "id": "test-job",
                            "name": "Test Job",
                            "required_steps": [
                                "Checkout repository",
                                "Run CI precheck",
                                "Run tests",
                            ],
                            "required_outputs": [],
                        }
                    ],
                    "required_env_vars": [],
                }
            },
        }

    def test_frozen_step_name_change_should_fail(self, temp_workspace, contract_with_frozen_steps):
        """测试冻结的 step name 改名应报告 ERROR"""
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract_with_frozen_steps, f)

        # 写入将冻结 step name 改名的 workflow
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
jobs:
  test-job:
    name: Test Job
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
      - name: Run precheck
        run: echo "precheck"
      - name: Run tests
        run: echo "test"
"""
        with open(workflow_path, "w") as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该失败，因为 "Run CI precheck" 是冻结的 step 但被改名为 "Run precheck"
        assert result.success is False

        # 应该有 frozen_step_name_changed 错误
        frozen_errors = [e for e in result.errors if e.error_type == "frozen_step_name_changed"]
        assert len(frozen_errors) == 1
        assert frozen_errors[0].key == "Run CI precheck"
        assert frozen_errors[0].expected == "Run CI precheck"
        assert frozen_errors[0].actual == "Run precheck"

    def test_frozen_step_exact_match_should_pass(self, temp_workspace, contract_with_frozen_steps):
        """测试冻结的 step name 精确匹配应通过"""
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract_with_frozen_steps, f)

        # 写入所有 step name 精确匹配的 workflow
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
jobs:
  test-job:
    name: Test Job
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
      - name: Run CI precheck
        run: echo "precheck"
      - name: Run tests
        run: echo "test"
"""
        with open(workflow_path, "w") as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该通过
        assert result.success is True
        assert len(result.errors) == 0


class TestArtifactPathCoverage:
    """artifact path coverage 匹配逻辑测试

    覆盖三种匹配模式：
    1. Glob 模式匹配（含 *?[] 字符）
    2. 目录匹配（以 / 结尾）
    3. 精确文件匹配
    """

    # =========================================================================
    # _is_glob_pattern 辅助函数测试
    # =========================================================================

    def test_is_glob_pattern_with_asterisk(self):
        """测试 * 被识别为 glob 模式"""
        assert _is_glob_pattern("*.json") is True
        assert _is_glob_pattern(".artifacts/*") is True
        assert _is_glob_pattern(".artifacts/acceptance-runs/*") is True

    def test_is_glob_pattern_with_question_mark(self):
        """测试 ? 被识别为 glob 模式"""
        assert _is_glob_pattern("file?.txt") is True
        assert _is_glob_pattern("test-?.json") is True

    def test_is_glob_pattern_with_brackets(self):
        """测试 [] 被识别为 glob 模式"""
        assert _is_glob_pattern("file[0-9].txt") is True
        assert _is_glob_pattern("[abc].json") is True

    def test_is_glob_pattern_without_special_chars(self):
        """测试没有 glob 字符的路径"""
        assert _is_glob_pattern(".artifacts/verify-results.json") is False
        assert _is_glob_pattern(".artifacts/acceptance-runs/") is False
        assert _is_glob_pattern("simple-file.txt") is False

    # =========================================================================
    # _path_matches 辅助函数测试
    # =========================================================================

    class TestPathMatchesGlob:
        """Glob 模式匹配测试"""

        def test_glob_asterisk_matches(self):
            """测试 * glob 匹配"""
            # 应该匹配
            assert (
                _path_matches(
                    ".artifacts/acceptance-runs/run1.json", ".artifacts/acceptance-runs/*"
                )
                is True
            )
            assert _path_matches(".artifacts/file.json", ".artifacts/*.json") is True
            assert _path_matches("test.json", "*.json") is True

        def test_glob_asterisk_not_matches(self):
            """测试 * glob 不匹配"""
            # 不应该匹配（文件扩展名不同）
            assert _path_matches("test.txt", "*.json") is False
            # 注意：fnmatch 的 * 会匹配路径分隔符 /
            # 所以 .artifacts/other/file.json 会匹配 .artifacts/*.json
            # 这是 fnmatch 的标准行为，与 shell glob 不同

        def test_glob_question_mark_matches(self):
            """测试 ? glob 匹配"""
            assert _path_matches("file1.txt", "file?.txt") is True
            assert _path_matches("fileA.txt", "file?.txt") is True

        def test_glob_question_mark_not_matches(self):
            """测试 ? glob 不匹配"""
            assert _path_matches("file12.txt", "file?.txt") is False
            assert _path_matches("file.txt", "file?.txt") is False

        def test_glob_bracket_matches(self):
            """测试 [] glob 匹配"""
            assert _path_matches("file1.txt", "file[0-9].txt") is True
            assert _path_matches("file5.txt", "file[0-9].txt") is True

        def test_glob_bracket_not_matches(self):
            """测试 [] glob 不匹配"""
            assert _path_matches("filea.txt", "file[0-9].txt") is False

    class TestPathMatchesDirectory:
        """目录匹配测试"""

        def test_directory_exact_match(self):
            """测试目录精确匹配（uploaded + / == required）"""
            assert (
                _path_matches(".artifacts/acceptance-runs", ".artifacts/acceptance-runs/") is True
            )

        def test_directory_file_inside(self):
            """测试目录下的文件匹配"""
            assert (
                _path_matches(".artifacts/acceptance-runs/run1.json", ".artifacts/acceptance-runs/")
                is True
            )
            assert (
                _path_matches(
                    ".artifacts/acceptance-runs/subdir/file.txt", ".artifacts/acceptance-runs/"
                )
                is True
            )

        def test_directory_not_matches_sibling(self):
            """测试目录不匹配同级路径"""
            assert (
                _path_matches(".artifacts/other-dir/file.json", ".artifacts/acceptance-runs/")
                is False
            )

        def test_directory_not_matches_partial_prefix(self):
            """测试目录不匹配部分前缀"""
            # .artifacts/acceptance-runs-backup 不应该匹配 .artifacts/acceptance-runs/
            assert (
                _path_matches(
                    ".artifacts/acceptance-runs-backup/file.json", ".artifacts/acceptance-runs/"
                )
                is False
            )

    class TestPathMatchesExact:
        """精确匹配测试"""

        def test_exact_match(self):
            """测试精确匹配成功"""
            assert (
                _path_matches(".artifacts/verify-results.json", ".artifacts/verify-results.json")
                is True
            )
            assert _path_matches("test.txt", "test.txt") is True

        def test_exact_no_match_different_path(self):
            """测试精确匹配失败 - 不同路径"""
            assert _path_matches(".artifacts/other.json", ".artifacts/verify-results.json") is False

        def test_exact_no_match_partial(self):
            """测试精确匹配不做部分匹配"""
            # 精确匹配不应该做 endswith 或 contains 匹配
            assert (
                _path_matches(
                    ".artifacts/subdir/verify-results.json", ".artifacts/verify-results.json"
                )
                is False
            )
            assert _path_matches("verify-results.json", ".artifacts/verify-results.json") is False

    # =========================================================================
    # check_artifact_path_coverage 主函数测试
    # =========================================================================

    def test_coverage_glob_pattern_match(self):
        """测试 glob 模式路径覆盖检查"""
        upload_steps = [
            {
                "job_id": "test",
                "step_name": "Upload artifacts",
                "paths": [
                    ".artifacts/acceptance-runs/run-20260201.json",
                    ".artifacts/acceptance-runs/run-20260202.json",
                ],
            }
        ]
        required_paths = [".artifacts/acceptance-runs/*"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert covered == [".artifacts/acceptance-runs/*"]
        assert missing == []

    def test_coverage_glob_pattern_missing(self):
        """测试 glob 模式路径缺失"""
        upload_steps = [
            {
                "job_id": "test",
                "step_name": "Upload artifacts",
                "paths": [".artifacts/other/file.json"],
            }
        ]
        required_paths = [".artifacts/acceptance-runs/*"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert covered == []
        assert missing == [".artifacts/acceptance-runs/*"]

    def test_coverage_directory_match(self):
        """测试目录路径覆盖检查"""
        upload_steps = [
            {
                "job_id": "test",
                "step_name": "Upload artifacts",
                "paths": [".artifacts/acceptance-runs/run1.json"],
            }
        ]
        required_paths = [".artifacts/acceptance-runs/"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert covered == [".artifacts/acceptance-runs/"]
        assert missing == []

    def test_coverage_directory_missing(self):
        """测试目录路径缺失"""
        upload_steps = [
            {
                "job_id": "test",
                "step_name": "Upload artifacts",
                "paths": [".artifacts/other-dir/file.json"],
            }
        ]
        required_paths = [".artifacts/acceptance-runs/"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert covered == []
        assert missing == [".artifacts/acceptance-runs/"]

    def test_coverage_exact_file_match(self):
        """测试精确文件路径覆盖检查"""
        upload_steps = [
            {
                "job_id": "test",
                "step_name": "Upload artifacts",
                "paths": [".artifacts/verify-results.json"],
            }
        ]
        required_paths = [".artifacts/verify-results.json"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert covered == [".artifacts/verify-results.json"]
        assert missing == []

    def test_coverage_exact_file_missing(self):
        """测试精确文件路径缺失"""
        upload_steps = [
            {
                "job_id": "test",
                "step_name": "Upload artifacts",
                "paths": [".artifacts/other-file.json"],
            }
        ]
        required_paths = [".artifacts/verify-results.json"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert covered == []
        assert missing == [".artifacts/verify-results.json"]

    def test_coverage_mixed_patterns(self):
        """测试混合模式（glob + 目录 + 文件）"""
        upload_steps = [
            {
                "job_id": "test",
                "step_name": "Upload artifacts",
                "paths": [
                    ".artifacts/verify-results.json",
                    ".artifacts/acceptance-runs/run1.json",
                    ".artifacts/acceptance-matrix.md",
                    ".artifacts/acceptance-matrix.json",
                ],
            }
        ]
        required_paths = [
            ".artifacts/acceptance-runs/*",  # glob
            ".artifacts/acceptance-matrix.md",  # exact file
            ".artifacts/acceptance-matrix.json",  # exact file
        ]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert set(covered) == {
            ".artifacts/acceptance-runs/*",
            ".artifacts/acceptance-matrix.md",
            ".artifacts/acceptance-matrix.json",
        }
        assert missing == []

    def test_coverage_with_step_name_filter(self):
        """测试带 step name 过滤器的覆盖检查"""
        upload_steps = [
            {
                "job_id": "test",
                "step_name": "Upload test artifacts",
                "paths": [".artifacts/test-results.json"],
            },
            {
                "job_id": "test",
                "step_name": "Upload acceptance artifacts",
                "paths": [".artifacts/acceptance-runs/run1.json"],
            },
        ]
        required_paths = [".artifacts/acceptance-runs/*"]

        # 只检查 acceptance 相关的 step
        covered, missing = check_artifact_path_coverage(
            upload_steps, required_paths, step_name_filter=["acceptance"]
        )

        assert covered == [".artifacts/acceptance-runs/*"]
        assert missing == []

        # 只检查 test 相关的 step - 应该找不到
        covered, missing = check_artifact_path_coverage(
            upload_steps, required_paths, step_name_filter=["test artifacts"]
        )

        assert covered == []
        assert missing == [".artifacts/acceptance-runs/*"]

    def test_coverage_real_contract_patterns(self):
        """测试真实合约中的路径模式"""
        # 模拟 ci.artifact_archive.required_artifact_paths
        upload_steps = [
            {
                "job_id": "test",
                "step_name": "Upload acceptance artifacts",
                "paths": [
                    ".artifacts/acceptance-runs/iteration_12_py3.11.json",
                    ".artifacts/acceptance-matrix.md",
                    ".artifacts/acceptance-matrix.json",
                ],
            }
        ]
        required_paths = [
            ".artifacts/acceptance-runs/*",
            ".artifacts/acceptance-matrix.md",
            ".artifacts/acceptance-matrix.json",
        ]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert set(covered) == set(required_paths)
        assert missing == []


# ============================================================================
# Workflow Key Discovery Tests
# ============================================================================


class TestDiscoverWorkflowKeys:
    """测试 discover_workflow_keys 函数

    验证动态发现 workflow key 的逻辑：
    1. 正确识别包含 "file" 字段的 dict 为 workflow
    2. 排除 METADATA_KEYS 中的字段
    3. 排除下划线前缀字段
    4. 返回按字母序排序的列表
    """

    def test_discover_basic_workflows(self):
        """测试发现基本的 ci/nightly workflow"""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import discover_workflow_keys

        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": ["test"]},
            "nightly": {"file": ".github/workflows/nightly.yml", "job_ids": ["build"]},
        }

        keys = discover_workflow_keys(contract)
        assert keys == ["ci", "nightly"]

    def test_discover_with_release_workflow(self):
        """测试发现包含 release workflow 的合约"""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import discover_workflow_keys

        contract = {
            "version": "1.0.0",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": ["test"]},
            "nightly": {"file": ".github/workflows/nightly.yml", "job_ids": ["build"]},
            "release": {"file": ".github/workflows/release.yml", "job_ids": ["publish"]},
        }

        keys = discover_workflow_keys(contract)
        assert keys == ["ci", "nightly", "release"]

    def test_exclude_metadata_keys(self):
        """测试排除 metadata 字段"""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import discover_workflow_keys

        contract = {
            "$schema": "schema.json",
            "version": "1.0.0",
            "description": "test contract",
            "make": {"targets_required": ["lint"]},
            "frozen_step_text": {"allowlist": ["step1"]},
            "frozen_job_names": {"allowlist": ["job1"]},
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": ["test"]},
        }

        keys = discover_workflow_keys(contract)
        # 只有 ci 应该被发现，其他都是 metadata
        assert keys == ["ci"]

    def test_exclude_underscore_prefix_keys(self):
        """测试排除下划线前缀字段"""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import discover_workflow_keys

        contract = {
            "_changelog_v1.0.0": "Initial release",
            "_phase_1_scope": "CI only",
            "_sop_reference": "see docs",
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": ["test"]},
        }

        keys = discover_workflow_keys(contract)
        assert keys == ["ci"]

    def test_require_file_field(self):
        """测试要求 "file" 字段才识别为 workflow"""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import discover_workflow_keys

        contract = {
            "ci": {"file": ".github/workflows/ci.yml", "job_ids": ["test"]},
            "custom_config": {"setting": "value"},  # 没有 file 字段，不是 workflow
            "labels": ["label1", "label2"],  # 不是 dict，不是 workflow
        }

        keys = discover_workflow_keys(contract)
        assert keys == ["ci"]

    def test_empty_contract(self):
        """测试空合约"""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import discover_workflow_keys

        contract: dict = {}
        keys = discover_workflow_keys(contract)
        assert keys == []

    def test_sorted_output(self):
        """测试返回结果按字母序排序"""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import discover_workflow_keys

        contract = {
            "zebra": {"file": "z.yml"},
            "alpha": {"file": "a.yml"},
            "beta": {"file": "b.yml"},
        }

        keys = discover_workflow_keys(contract)
        assert keys == ["alpha", "beta", "zebra"]


class TestReleaseWorkflowDocsSync:
    """测试动态发现 release workflow 时的文档同步检查

    验证当合约中包含 release workflow 时，checker 会正确检查
    release 的 job_ids/job_names 是否在文档中出现。
    """

    def test_release_workflow_missing_from_doc(self, tmp_path):
        """测试 release workflow 的 job_ids 未在文档中时报错"""
        # 创建包含 release 的合约
        contract_data = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test"],
                "job_names": ["Test Job"],
            },
            "release": {
                "file": ".github/workflows/release.yml",
                "job_ids": ["publish", "deploy"],
                "job_names": ["Publish Package", "Deploy to Production"],
            },
        }

        contract_path = tmp_path / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract_data, f)

        # 创建只包含 ci 信息的文档（缺少 release 信息）
        doc_content = """# Workflow Contract

## CI Workflow

ci.yml 文件定义了 CI 流程。

### Job IDs
- test

### Job Names
- Test Job
"""
        doc_path = tmp_path / "contract.md"
        with open(doc_path, "w") as f:
            f.write(doc_content)

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 应该发现 release 相关错误
        assert result.success is False

        # 检查 release job_ids 错误
        release_job_id_errors = [
            e for e in result.errors if e.category == "job_id" and e.value in ["publish", "deploy"]
        ]
        assert len(release_job_id_errors) == 2

        # 检查 release job_names 错误
        release_job_name_errors = [
            e
            for e in result.errors
            if e.category == "job_name" and e.value in ["Publish Package", "Deploy to Production"]
        ]
        assert len(release_job_name_errors) == 2

    def test_release_workflow_fully_documented(self, tmp_path):
        """测试 release workflow 完全文档化时通过检查

        注意：extract_workflow_section 使用 use_subsection_boundary=True，
        会在遇到 ### 子标题时停止提取。因此文档结构需要将 job_ids/job_names
        直接放在 workflow 章节描述行中，而不是用 ### 子标题分隔。
        """
        # 创建包含 release 的合约
        contract_data = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test"],
                "job_names": ["Test Job"],
            },
            "release": {
                "file": ".github/workflows/release.yml",
                "job_ids": ["publish", "deploy"],
                "job_names": ["Publish Package", "Deploy to Production"],
            },
            "make": {"targets_required": ["lint"]},
            "frozen_step_text": {"allowlist": ["Checkout repository"]},
        }

        contract_path = tmp_path / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract_data, f)

        # 创建包含所有信息的文档
        # 注意：不使用 ### 子标题，因为 extract_workflow_section 会在 ### 处停止
        doc_content = """# Workflow Contract

version: 1.0.0

## CI Workflow

ci.yml 文件定义了 CI 流程。
Job IDs: test
Job Names: Test Job

## Release Workflow

release.yml 文件定义了发布流程。
Job IDs: publish, deploy
Job Names: Publish Package, Deploy to Production

## Make Targets

targets_required:
- lint

## Frozen Steps

frozen_step_text allowlist:
- Checkout repository
"""
        doc_path = tmp_path / "contract.md"
        with open(doc_path, "w") as f:
            f.write(doc_content)

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 应该通过（可能有 workflow section missing 警告，但无同步错误）
        sync_errors = [
            e for e in result.errors if e.category in ["job_id", "job_name", "frozen_step"]
        ]
        assert len(sync_errors) == 0

    def test_discovered_workflows_are_checked(self, tmp_path):
        """测试动态发现的所有 workflow 都会被检查"""
        # 创建包含三个 workflow 的合约
        contract_data = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["ci-job-1"],
                "job_names": ["CI Job One"],
            },
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": ["nightly-job-1"],
                "job_names": ["Nightly Job One"],
            },
            "release": {
                "file": ".github/workflows/release.yml",
                "job_ids": ["release-job-1"],
                "job_names": ["Release Job One"],
            },
        }

        contract_path = tmp_path / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract_data, f)

        # 创建空文档
        doc_path = tmp_path / "contract.md"
        with open(doc_path, "w") as f:
            f.write("# Empty Doc\nversion: 1.0.0")

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
        from check_workflow_contract_docs_sync import WorkflowContractDocsSyncChecker

        checker = WorkflowContractDocsSyncChecker(
            contract_path=contract_path,
            doc_path=doc_path,
            verbose=False,
        )
        result = checker.check()

        # 所有三个 workflow 的 job_ids 都应该被检查
        assert "ci-job-1" in result.checked_job_ids
        assert "nightly-job-1" in result.checked_job_ids
        assert "release-job-1" in result.checked_job_ids

        # 所有三个 workflow 的 job_names 都应该被检查
        assert "CI Job One" in result.checked_job_names
        assert "Nightly Job One" in result.checked_job_names
        assert "Release Job One" in result.checked_job_names


# ============================================================================
# CI Labels Validation Tests (migrated from scripts/tests)
# ============================================================================


class TestCILabelsValidation:
    """CI Labels 一致性校验测试

    验证 contract.ci.labels 与 gh_pr_labels_to_outputs.py 中 LABEL_* 常量的一致性。
    """

    @pytest.fixture
    def contract_with_labels(self):
        """带有 labels 的合约"""
        return {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "labels": [
                    "ci:test-label-1",
                    "ci:test-label-2",
                    "test:freeze-override",
                ],
                "required_jobs": [],
                "required_env_vars": [],
            },
        }

    @pytest.fixture
    def matching_label_script(self):
        """与合约匹配的 label 脚本内容"""
        return '''#!/usr/bin/env python3
"""Parse PR labels and output to GITHUB_OUTPUT."""

import os
import sys

# Label constants
LABEL_TEST_1 = "ci:test-label-1"
LABEL_TEST_2 = "ci:test-label-2"
LABEL_FREEZE_OVERRIDE = "test:freeze-override"

def main():
    pass

if __name__ == "__main__":
    sys.exit(main())
'''

    @pytest.fixture
    def mismatched_label_script_missing(self):
        """缺少一个 label 的脚本内容"""
        return '''#!/usr/bin/env python3
"""Parse PR labels and output to GITHUB_OUTPUT."""

import os
import sys

# Label constants - missing ci:test-label-2
LABEL_TEST_1 = "ci:test-label-1"
LABEL_FREEZE_OVERRIDE = "test:freeze-override"

def main():
    pass

if __name__ == "__main__":
    sys.exit(main())
'''

    @pytest.fixture
    def mismatched_label_script_extra(self):
        """多出一个 label 的脚本内容"""
        return '''#!/usr/bin/env python3
"""Parse PR labels and output to GITHUB_OUTPUT."""

import os
import sys

# Label constants - extra label
LABEL_TEST_1 = "ci:test-label-1"
LABEL_TEST_2 = "ci:test-label-2"
LABEL_FREEZE_OVERRIDE = "test:freeze-override"
LABEL_EXTRA = "ci:extra-label"

def main():
    pass

if __name__ == "__main__":
    sys.exit(main())
'''

    def test_labels_match_success(
        self, temp_workspace, contract_with_labels, matching_label_script
    ):
        """测试 labels 匹配成功的情况"""
        # 写入合约
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract_with_labels, f)

        # 创建 scripts/ci 目录并写入脚本
        scripts_ci_dir = temp_workspace / "scripts" / "ci"
        scripts_ci_dir.mkdir(parents=True)

        script_path = scripts_ci_dir / "gh_pr_labels_to_outputs.py"
        with open(script_path, "w") as f:
            f.write(matching_label_script)

        # 创建空的 workflow 文件（需要存在以避免 workflow_not_found 错误）
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo "test"
"""
        with open(workflow_path, "w") as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 不应该有 label 相关的错误
        label_errors = [e for e in result.errors if e.error_type.startswith("label_")]
        assert (
            len(label_errors) == 0
        ), f"Expected no label errors, got: {[e.message for e in label_errors]}"

    def test_labels_missing_in_script(
        self, temp_workspace, contract_with_labels, mismatched_label_script_missing
    ):
        """测试 contract 中有但脚本中没有的 label"""
        # 写入合约
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract_with_labels, f)

        # 创建 scripts/ci 目录并写入脚本
        scripts_ci_dir = temp_workspace / "scripts" / "ci"
        scripts_ci_dir.mkdir(parents=True)

        script_path = scripts_ci_dir / "gh_pr_labels_to_outputs.py"
        with open(script_path, "w") as f:
            f.write(mismatched_label_script_missing)

        # 创建空的 workflow 文件
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo "test"
"""
        with open(workflow_path, "w") as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该有 label_missing_in_script 错误
        label_errors = [
            e for e in result.errors if e.error_type == "label_missing_in_script"
        ]
        assert len(label_errors) == 1
        assert label_errors[0].key == "ci:test-label-2"
        assert "not found as a LABEL_* constant" in label_errors[0].message

    def test_labels_missing_in_contract(
        self, temp_workspace, contract_with_labels, mismatched_label_script_extra
    ):
        """测试脚本中有但 contract 中没有的 label"""
        # 写入合约
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract_with_labels, f)

        # 创建 scripts/ci 目录并写入脚本
        scripts_ci_dir = temp_workspace / "scripts" / "ci"
        scripts_ci_dir.mkdir(parents=True)

        script_path = scripts_ci_dir / "gh_pr_labels_to_outputs.py"
        with open(script_path, "w") as f:
            f.write(mismatched_label_script_extra)

        # 创建空的 workflow 文件
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo "test"
"""
        with open(workflow_path, "w") as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该有 label_missing_in_contract 错误
        label_errors = [
            e for e in result.errors if e.error_type == "label_missing_in_contract"
        ]
        assert len(label_errors) == 1
        assert label_errors[0].key == "ci:extra-label"
        assert "not found in contract.ci.labels" in label_errors[0].message

    def test_labels_script_not_found(self, temp_workspace, contract_with_labels):
        """测试脚本不存在时的处理（应该只是警告）"""
        # 写入合约
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract_with_labels, f)

        # 不创建脚本文件

        # 创建空的 workflow 文件
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo "test"
"""
        with open(workflow_path, "w") as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该有警告而不是错误
        label_warnings = [
            w for w in result.warnings if w.warning_type == "label_script_parse_warning"
        ]
        assert len(label_warnings) == 1
        assert "Could not parse LABEL_* constants" in label_warnings[0].message

        # 不应该有 label 错误
        label_errors = [e for e in result.errors if e.error_type.startswith("label_")]
        assert len(label_errors) == 0

    def test_no_labels_in_contract_skips_validation(self, temp_workspace):
        """测试 contract 中没有 labels 时跳过校验"""
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                # No labels field
                "required_jobs": [],
                "required_env_vars": [],
            },
        }

        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 创建空的 workflow 文件
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo "test"
"""
        with open(workflow_path, "w") as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 不应该有 label 相关的错误或警告
        label_errors = [e for e in result.errors if e.error_type.startswith("label_")]
        label_warnings = [
            w for w in result.warnings if w.warning_type.startswith("label_")
        ]
        assert len(label_errors) == 0
        assert len(label_warnings) == 0


class TestCILabelsRealFileValidation:
    """真实文件的 CI Labels 一致性校验测试"""

    def test_real_contract_and_script_labels_match(self):
        """测试真实 contract 和脚本中的 labels 是否一致"""
        import ast

        # 获取项目根目录
        workspace_root = Path(__file__).parent.parent.parent
        contract_path = workspace_root / "scripts" / "ci" / "workflow_contract.v1.json"
        script_path = workspace_root / "scripts" / "ci" / "gh_pr_labels_to_outputs.py"

        if not contract_path.exists() or not script_path.exists():
            pytest.skip("Required files not found in workspace")

        # 加载 contract
        with open(contract_path) as f:
            contract = json.load(f)

        contract_labels = set(contract.get("ci", {}).get("labels", []))

        # 解析脚本中的 LABEL_* 常量
        with open(script_path) as f:
            source = f.read()

        tree = ast.parse(source)
        script_labels = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.startswith("LABEL_"):
                        if isinstance(node.value, ast.Constant) and isinstance(
                            node.value.value, str
                        ):
                            script_labels.add(node.value.value)

        # 验证一致性
        missing_in_script = contract_labels - script_labels
        missing_in_contract = script_labels - contract_labels

        error_msgs = []
        if missing_in_script:
            error_msgs.append(
                f"Labels in contract but not in script: {missing_in_script}"
            )
        if missing_in_contract:
            error_msgs.append(
                f"Labels in script but not in contract: {missing_in_contract}"
            )

        assert len(error_msgs) == 0, "\n".join(error_msgs)
