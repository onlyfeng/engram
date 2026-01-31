#!/usr/bin/env python3
"""
Workflow Contract Validator 单元测试

覆盖功能:
1. 合约文件加载和解析
2. Workflow 文件验证
3. Job/Step/Output 缺失检测
4. Step name 变化 diff 提示
5. 实际 workflow 文件校验（集成测试）
"""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# 将 scripts/ci 目录添加到 path
sys.path.insert(0, str(Path(__file__).parent.parent / "ci"))

from validate_workflows import (
    WorkflowContractValidator,
    ValidationResult,
    ValidationError,
    ValidationWarning,
    format_text_output,
    format_json_output,
    HAS_JSONSCHEMA,
    parse_makefile_targets,
    extract_workflow_make_calls,
)


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
        "workflows": {
            "ci": {
                "file": ".github/workflows/ci.yml",
                "required_jobs": [
                    {
                        "id": "test-job",
                        "name": "Test Job",
                        "required_steps": [
                            "Checkout repository",
                            "Run tests"
                        ],
                        "required_outputs": [
                            "test_result"
                        ]
                    }
                ],
                "required_env_vars": [
                    "CI_VAR"
                ]
            }
        }
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
        with open(contract_path, 'w') as f:
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
        with open(contract_path, 'w') as f:
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
        with open(contract_path, 'w') as f:
            json.dump(sample_contract, f)

        # 写入 workflow
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        with open(workflow_path, 'w') as f:
            f.write(sample_workflow)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        assert result.success is True
        assert len(result.errors) == 0
        assert "ci" in result.validated_workflows

    def test_validate_missing_workflow_file(self, temp_workspace, sample_contract):
        """测试 workflow 文件缺失"""
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
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
        with open(contract_path, 'w') as f:
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
        with open(workflow_path, 'w') as f:
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
        with open(contract_path, 'w') as f:
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
        with open(workflow_path, 'w') as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        assert result.success is False
        errors = [e for e in result.errors if e.error_type == "missing_step"]
        assert len(errors) == 1
        assert errors[0].key == "Run tests"

    def test_validate_missing_output(self, temp_workspace, sample_contract):
        """测试缺少 output"""
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
            json.dump(sample_contract, f)

        # 写入缺少 output 的 workflow
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
    # Missing outputs
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
      - name: Run tests
        run: echo "test"
"""
        with open(workflow_path, 'w') as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        assert result.success is False
        errors = [e for e in result.errors if e.error_type == "missing_output"]
        assert len(errors) == 1
        assert errors[0].key == "test_result"

    def test_validate_missing_env_var(self, temp_workspace, sample_contract):
        """测试缺少环境变量"""
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
            json.dump(sample_contract, f)

        # 写入缺少 env var 的 workflow
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
# Missing env section
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
        with open(workflow_path, 'w') as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        assert result.success is False
        errors = [e for e in result.errors if e.error_type == "missing_env_var"]
        assert len(errors) == 1
        assert errors[0].key == "CI_VAR"


# ============================================================================
# Step Name Change Detection Tests
# ============================================================================

class TestStepNameChangeDetection:
    """Step name 变化检测测试"""

    def test_detect_step_name_change(self, temp_workspace, sample_contract):
        """测试检测 step name 变化"""
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
            json.dump(sample_contract, f)

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
        with open(workflow_path, 'w') as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该产生警告而不是错误（因为 "Run tests" 和 "Run unit tests" 部分匹配）
        warnings = [w for w in result.warnings if w.warning_type == "step_name_changed"]
        assert len(warnings) == 1
        assert warnings[0].old_value == "Run tests"
        assert warnings[0].new_value == "Run unit tests"

    def test_detect_job_name_change(self, temp_workspace, sample_contract):
        """测试检测 job name 变化"""
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
            json.dump(sample_contract, f)

        # 写入 job name 变化的 workflow
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
env:
  CI_VAR: "value"
jobs:
  test-job:
    name: Test Job (Updated)
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
        with open(workflow_path, 'w') as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该产生警告
        warnings = [w for w in result.warnings if w.warning_type == "job_name_changed"]
        assert len(warnings) == 1
        assert warnings[0].old_value == "Test Job"
        assert warnings[0].new_value == "Test Job (Updated)"


# ============================================================================
# Job ID and Job Name Validation Tests
# ============================================================================

class TestJobIdAndNameValidation:
    """Job ID 和 Job Name 校验测试"""

    @pytest.fixture
    def contract_with_job_ids(self):
        """带有 job_ids/job_names 的合约"""
        return {
            "version": "1.0.0",
            "workflows": {
                "ci": {
                    "file": ".github/workflows/ci.yml",
                    "job_ids": ["build", "test", "deploy"],
                    "job_names": ["Build Project", "Run Tests", "Deploy App"],
                    "required_jobs": [],
                    "required_env_vars": []
                }
            }
        }

    def test_validate_all_job_ids_exist(self, temp_workspace, contract_with_job_ids):
        """测试所有 job_ids 都存在于 workflow"""
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
            json.dump(contract_with_job_ids, f)

        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
jobs:
  build:
    name: Build Project
    runs-on: ubuntu-latest
    steps:
      - run: echo "build"
  test:
    name: Run Tests
    runs-on: ubuntu-latest
    steps:
      - run: echo "test"
  deploy:
    name: Deploy App
    runs-on: ubuntu-latest
    steps:
      - run: echo "deploy"
"""
        with open(workflow_path, 'w') as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        assert result.success is True
        missing_job_id_errors = [e for e in result.errors if e.error_type == "missing_job_id"]
        assert len(missing_job_id_errors) == 0

    def test_validate_missing_job_id(self, temp_workspace, contract_with_job_ids):
        """测试检测缺失的 job_id"""
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
            json.dump(contract_with_job_ids, f)

        # workflow 缺少 deploy job
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
jobs:
  build:
    name: Build Project
    runs-on: ubuntu-latest
    steps:
      - run: echo "build"
  test:
    name: Run Tests
    runs-on: ubuntu-latest
    steps:
      - run: echo "test"
"""
        with open(workflow_path, 'w') as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        assert result.success is False
        missing_job_id_errors = [e for e in result.errors if e.error_type == "missing_job_id"]
        assert len(missing_job_id_errors) == 1
        assert missing_job_id_errors[0].key == "deploy"
        assert missing_job_id_errors[0].expected == "deploy"

    def test_validate_job_name_mismatch_warning(self, temp_workspace):
        """测试非冻结 job name 不一致时产生 WARNING"""
        contract = {
            "version": "1.0.0",
            "frozen_job_names": {
                "allowlist": []  # 没有冻结的 job name
            },
            "workflows": {
                "ci": {
                    "file": ".github/workflows/ci.yml",
                    "job_ids": ["build"],
                    "job_names": ["Build Project"],
                    "required_jobs": [],
                    "required_env_vars": []
                }
            }
        }

        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
            json.dump(contract, f)

        # workflow 的 job name 与 contract 不一致
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
jobs:
  build:
    name: Build Application
    runs-on: ubuntu-latest
    steps:
      - run: echo "build"
"""
        with open(workflow_path, 'w') as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该成功（非冻结 job name 改名只是 warning）
        assert result.success is True
        
        # 应该有 job_name_mismatch 警告
        warnings = [w for w in result.warnings if w.warning_type == "job_name_mismatch"]
        assert len(warnings) == 1
        assert warnings[0].old_value == "Build Project"
        assert warnings[0].new_value == "Build Application"

    def test_validate_frozen_job_name_changed_error(self, temp_workspace):
        """测试冻结 job name 改名时产生 ERROR"""
        contract = {
            "version": "1.0.0",
            "frozen_job_names": {
                "allowlist": ["Build Project"]  # 冻结此 job name
            },
            "workflows": {
                "ci": {
                    "file": ".github/workflows/ci.yml",
                    "job_ids": ["build"],
                    "job_names": ["Build Project"],
                    "required_jobs": [],
                    "required_env_vars": []
                }
            }
        }

        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
            json.dump(contract, f)

        # workflow 的 job name 与 contract 不一致
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
jobs:
  build:
    name: Build Application
    runs-on: ubuntu-latest
    steps:
      - run: echo "build"
"""
        with open(workflow_path, 'w') as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该失败（冻结 job name 改名是 error）
        assert result.success is False
        
        # 应该有 frozen_job_name_changed 错误
        frozen_errors = [e for e in result.errors if e.error_type == "frozen_job_name_changed"]
        assert len(frozen_errors) == 1
        assert frozen_errors[0].expected == "Build Project"
        assert frozen_errors[0].actual == "Build Application"
        assert "冻结文案" in frozen_errors[0].message
        # 验证错误消息包含修复指引
        assert "workflow_contract.v1.json" in frozen_errors[0].message

    def test_validate_job_name_exact_match_pass(self, temp_workspace):
        """测试 job name 精确匹配通过"""
        contract = {
            "version": "1.0.0",
            "frozen_job_names": {
                "allowlist": ["Build Project"]
            },
            "workflows": {
                "ci": {
                    "file": ".github/workflows/ci.yml",
                    "job_ids": ["build"],
                    "job_names": ["Build Project"],
                    "required_jobs": [],
                    "required_env_vars": []
                }
            }
        }

        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
            json.dump(contract, f)

        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
jobs:
  build:
    name: Build Project
    runs-on: ubuntu-latest
    steps:
      - run: echo "build"
"""
        with open(workflow_path, 'w') as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        assert result.success is True
        assert len(result.errors) == 0

    def test_validate_multiple_missing_job_ids(self, temp_workspace, contract_with_job_ids):
        """测试检测多个缺失的 job_id"""
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
            json.dump(contract_with_job_ids, f)

        # workflow 只有 build job
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
jobs:
  build:
    name: Build Project
    runs-on: ubuntu-latest
    steps:
      - run: echo "build"
"""
        with open(workflow_path, 'w') as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        assert result.success is False
        missing_job_id_errors = [e for e in result.errors if e.error_type == "missing_job_id"]
        assert len(missing_job_id_errors) == 2
        
        missing_ids = {e.key for e in missing_job_id_errors}
        assert missing_ids == {"test", "deploy"}

    def test_validate_empty_job_ids_skips_validation(self, temp_workspace):
        """测试空 job_ids 跳过校验"""
        contract = {
            "version": "1.0.0",
            "workflows": {
                "ci": {
                    "file": ".github/workflows/ci.yml",
                    "job_ids": [],
                    "job_names": [],
                    "required_jobs": [],
                    "required_env_vars": []
                }
            }
        }

        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
            json.dump(contract, f)

        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
jobs:
  some-job:
    runs-on: ubuntu-latest
    steps:
      - run: echo "test"
"""
        with open(workflow_path, 'w') as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        assert result.success is True

    def test_validate_job_ids_without_job_names(self, temp_workspace):
        """测试只有 job_ids 没有 job_names 的情况"""
        contract = {
            "version": "1.0.0",
            "workflows": {
                "ci": {
                    "file": ".github/workflows/ci.yml",
                    "job_ids": ["build", "test"],
                    # 没有 job_names
                    "required_jobs": [],
                    "required_env_vars": []
                }
            }
        }

        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
            json.dump(contract, f)

        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
jobs:
  build:
    name: Any Name
    runs-on: ubuntu-latest
    steps:
      - run: echo "build"
  test:
    name: Another Name
    runs-on: ubuntu-latest
    steps:
      - run: echo "test"
"""
        with open(workflow_path, 'w') as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该成功（只校验 job_id 存在，不校验 name）
        assert result.success is True


# ============================================================================
# Frozen Step Name Tests
# ============================================================================

class TestFrozenStepNameDetection:
    """冻结 step name 变化检测测试"""

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
                    "Verify build static (Dockerfile/compose config check)"
                ]
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
                                "Run tests"
                            ],
                            "required_outputs": []
                        }
                    ],
                    "required_env_vars": []
                }
            }
        }

    def test_frozen_step_name_change_should_fail(self, temp_workspace, contract_with_frozen_steps):
        """测试冻结的 step name 改名应报告 ERROR"""
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
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
        with open(workflow_path, 'w') as f:
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
        assert "冻结文案" in frozen_errors[0].message
        # 验证错误消息包含修复指引
        assert "workflow_contract.v1.json" in frozen_errors[0].message

    def test_non_frozen_step_name_change_should_warn(self, temp_workspace):
        """测试非冻结的 step name 改名应仅报告 WARNING"""
        # 使用自定义合约，确保 "Run tests" 不在冻结列表中
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {
                "allowlist": [
                    "Checkout repository"
                    # "Run tests" 不在冻结列表中
                ]
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
                                "Run tests"
                            ],
                            "required_outputs": []
                        }
                    ],
                    "required_env_vars": []
                }
            }
        }
        
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
            json.dump(contract, f)

        # 写入将非冻结 step name 改名的 workflow
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
      - name: Run unit tests
        run: echo "test"
"""
        with open(workflow_path, 'w') as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该成功（非冻结 step 改名只是 warning）
        assert result.success is True
        
        # 不应该有 frozen_step_name_changed 错误
        frozen_errors = [e for e in result.errors if e.error_type == "frozen_step_name_changed"]
        assert len(frozen_errors) == 0
        
        # 应该有 step_name_changed 警告
        warnings = [w for w in result.warnings if w.warning_type == "step_name_changed"]
        assert len(warnings) == 1
        assert warnings[0].old_value == "Run tests"
        assert warnings[0].new_value == "Run unit tests"

    def test_frozen_step_missing_completely_should_fail(self, temp_workspace, contract_with_frozen_steps):
        """测试冻结的 step 完全缺失应报告 missing_step ERROR"""
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
            json.dump(contract_with_frozen_steps, f)

        # 写入缺少冻结 step 的 workflow（无法模糊匹配）
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
      - name: Completely different step
        run: echo "different"
      - name: Run tests
        run: echo "test"
"""
        with open(workflow_path, 'w') as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该失败
        assert result.success is False
        
        # 应该有 missing_step 错误
        missing_errors = [e for e in result.errors if e.error_type == "missing_step"]
        assert len(missing_errors) == 1
        assert missing_errors[0].key == "Run CI precheck"

    def test_frozen_step_exact_match_should_pass(self, temp_workspace, contract_with_frozen_steps):
        """测试冻结的 step name 精确匹配应通过"""
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
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
        with open(workflow_path, 'w') as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该通过
        assert result.success is True
        assert len(result.errors) == 0

    def test_contract_without_frozen_steps_should_work(self, temp_workspace, sample_contract):
        """测试没有 frozen_step_text 的合约应正常工作"""
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
            json.dump(sample_contract, f)

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
        with open(workflow_path, 'w') as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该成功（因为没有 frozen_step_text 配置，所有 step 改名都是 warning）
        assert result.success is True
        
        # 应该有 step_name_changed 警告
        warnings = [w for w in result.warnings if w.warning_type == "step_name_changed"]
        assert len(warnings) == 1

    def test_multiple_frozen_steps_renamed_should_report_all(self, temp_workspace, contract_with_frozen_steps):
        """测试多个冻结 step 改名应报告所有错误"""
        contract_path = temp_workspace / "contract.json"
        
        # 修改合约，添加更多 required steps
        contract_with_frozen_steps["workflows"]["ci"]["required_jobs"][0]["required_steps"] = [
            "Checkout repository",
            "Run CI precheck",
            "Verify build static (Dockerfile/compose config check)"
        ]
        
        with open(contract_path, 'w') as f:
            json.dump(contract_with_frozen_steps, f)

        # 写入多个冻结 step 都被改名的 workflow
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_content = """
name: CI
on: [push]
jobs:
  test-job:
    name: Test Job
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repo
        uses: actions/checkout@v4
      - name: Run precheck
        run: echo "precheck"
      - name: Verify build
        run: echo "verify"
"""
        with open(workflow_path, 'w') as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该失败
        assert result.success is False
        
        # 应该有多个 frozen_step_name_changed 错误
        frozen_errors = [e for e in result.errors if e.error_type == "frozen_step_name_changed"]
        assert len(frozen_errors) == 3  # 所有三个冻结 step 都被改名


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
                    location="jobs.test.steps"
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
                    new_value="Build artifacts"
                )
            ],
            validated_workflows=["ci"]
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
        result = ValidationResult(
            success=True,
            validated_workflows=["ci", "nightly"],
            errors=[],
            warnings=[]
        )

        output = format_json_output(result)
        parsed = json.loads(output)

        assert parsed["success"] is True
        assert parsed["validated_workflows"] == ["ci", "nightly"]
        assert parsed["error_count"] == 0
        assert parsed["warning_count"] == 0


# ============================================================================
# Schema Validation Tests
# ============================================================================

class TestSchemaValidation:
    """JSON Schema 校验测试"""

    @pytest.fixture
    def valid_schema(self):
        """有效的 schema 文件"""
        return {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "required": ["version"],
            "properties": {
                "version": {
                    "type": "string",
                    "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+$"
                },
                "ci": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {
                        "file": {"type": "string"}
                    }
                }
            }
        }

    @pytest.fixture
    def valid_contract_with_schema(self):
        """符合 schema 的有效合约"""
        return {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "required_jobs": []
            }
        }

    @pytest.fixture
    def invalid_contract_wrong_version(self):
        """版本号格式错误的合约"""
        return {
            "version": "invalid-version",  # 不符合 semver 格式
            "ci": {
                "file": ".github/workflows/ci.yml"
            }
        }

    @pytest.fixture
    def invalid_contract_missing_required(self):
        """缺少必需字段的合约"""
        return {
            # 缺少 version 字段
            "ci": {
                "file": ".github/workflows/ci.yml"
            }
        }

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_schema_validation_pass(self, temp_workspace, valid_schema, valid_contract_with_schema):
        """测试 schema 校验通过"""
        # 写入 schema
        schema_path = temp_workspace / "workflow_contract.v1.schema.json"
        with open(schema_path, 'w') as f:
            json.dump(valid_schema, f)

        # 写入合约
        contract_path = temp_workspace / "workflow_contract.v1.json"
        with open(contract_path, 'w') as f:
            json.dump(valid_contract_with_schema, f)

        # 创建 workflow 文件
        (temp_workspace / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        with open(workflow_path, 'w') as f:
            f.write("name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps: []\n")

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        validator.load_contract()
        result = validator.validate_schema()

        assert result is True
        schema_errors = [e for e in validator.result.errors if e.error_type == "schema_error"]
        assert len(schema_errors) == 0

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_schema_validation_fail_wrong_version_pattern(self, temp_workspace, valid_schema, invalid_contract_wrong_version):
        """测试 schema 校验失败 - 版本号格式错误"""
        # 写入 schema
        schema_path = temp_workspace / "workflow_contract.v1.schema.json"
        with open(schema_path, 'w') as f:
            json.dump(valid_schema, f)

        # 写入无效合约
        contract_path = temp_workspace / "workflow_contract.v1.json"
        with open(contract_path, 'w') as f:
            json.dump(invalid_contract_wrong_version, f)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        validator.load_contract()
        result = validator.validate_schema()

        assert result is False
        schema_errors = [e for e in validator.result.errors if e.error_type == "schema_error"]
        assert len(schema_errors) >= 1
        # 检查错误包含字段路径
        assert any("version" in e.key for e in schema_errors)

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_schema_validation_fail_missing_required(self, temp_workspace, valid_schema, invalid_contract_missing_required):
        """测试 schema 校验失败 - 缺少必需字段"""
        # 写入 schema
        schema_path = temp_workspace / "workflow_contract.v1.schema.json"
        with open(schema_path, 'w') as f:
            json.dump(valid_schema, f)

        # 写入无效合约
        contract_path = temp_workspace / "workflow_contract.v1.json"
        with open(contract_path, 'w') as f:
            json.dump(invalid_contract_missing_required, f)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        validator.load_contract()
        result = validator.validate_schema()

        assert result is False
        schema_errors = [e for e in validator.result.errors if e.error_type == "schema_error"]
        assert len(schema_errors) >= 1
        # 检查错误信息包含 required
        assert any("required" in e.message.lower() or "version" in e.message.lower() for e in schema_errors)

    def test_schema_validation_skip_when_no_schema_file(self, temp_workspace):
        """测试无 schema 文件时跳过校验"""
        # 只写入合约，不写入 schema
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
            json.dump({"version": "1.0.0"}, f)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        validator.load_contract()
        result = validator.validate_schema()

        # 无 schema 文件时应该返回 True（跳过校验）
        assert result is True
        schema_errors = [e for e in validator.result.errors if e.error_type == "schema_error"]
        assert len(schema_errors) == 0

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_schema_validation_error_output_contains_path(self, temp_workspace, valid_schema):
        """测试 schema 校验错误输出包含字段路径"""
        # 写入 schema（要求 ci.file 是字符串）
        schema_path = temp_workspace / "workflow_contract.v1.schema.json"
        with open(schema_path, 'w') as f:
            json.dump(valid_schema, f)

        # 写入合约（ci.file 是数字而非字符串）
        contract_path = temp_workspace / "workflow_contract.v1.json"
        with open(contract_path, 'w') as f:
            json.dump({
                "version": "1.0.0",
                "ci": {
                    "file": 12345  # 应该是字符串
                }
            }, f)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        validator.load_contract()
        result = validator.validate_schema()

        assert result is False
        schema_errors = [e for e in validator.result.errors if e.error_type == "schema_error"]
        assert len(schema_errors) >= 1
        # 检查错误包含 location（字段路径）
        assert any(e.location is not None and "ci" in e.location for e in schema_errors)

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_schema_validation_with_full_workflow(self, temp_workspace, sample_contract, sample_workflow):
        """测试完整流程中的 schema 校验"""
        # 创建包含 schema 的完整测试环境
        schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "required": ["version", "workflows"],
            "properties": {
                "version": {"type": "string"},
                "workflows": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "object",
                        "required": ["file"]
                    }
                }
            }
        }

        # 写入 schema
        schema_path = temp_workspace / "workflow_contract.v1.schema.json"
        with open(schema_path, 'w') as f:
            json.dump(schema, f)

        # 写入合约
        contract_path = temp_workspace / "workflow_contract.v1.json"
        with open(contract_path, 'w') as f:
            json.dump(sample_contract, f)

        # 写入 workflow
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        with open(workflow_path, 'w') as f:
            f.write(sample_workflow)

        # 执行完整验证
        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 检查 schema 校验被执行
        # sample_contract 符合 schema，所以不应有 schema_error
        schema_errors = [e for e in result.errors if e.error_type == "schema_error"]
        assert len(schema_errors) == 0


class TestSchemaValidationRealContract:
    """使用真实 schema 文件的测试"""

    @pytest.fixture
    def real_workspace(self):
        """获取真实工作空间路径"""
        workspace = Path(__file__).parent.parent.parent
        return workspace

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_real_contract_passes_schema(self, real_workspace):
        """测试真实合约文件通过 schema 校验"""
        contract_path = real_workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        schema_path = real_workspace / "scripts" / "ci" / "workflow_contract.v1.schema.json"

        if not contract_path.exists():
            pytest.skip("Contract file not found - run from project root")
        if not schema_path.exists():
            pytest.skip("Schema file not found")

        validator = WorkflowContractValidator(contract_path, real_workspace)
        validator.load_contract()
        result = validator.validate_schema()

        # 输出校验结果供调试
        if not result:
            for error in validator.result.errors:
                if error.error_type == "schema_error":
                    print(f"Schema error at {error.location}: {error.message}")
                    print(f"  Expected: {error.expected}")
                    print(f"  Actual: {error.actual}")

        assert result is True, "Real contract should pass schema validation"


# ============================================================================
# Integration Test - Real Workflow Files
# ============================================================================

class TestRealWorkflowValidation:
    """真实 workflow 文件验证测试（集成测试）"""

    @pytest.fixture
    def real_workspace(self):
        """获取真实工作空间路径"""
        # 假设测试在项目根目录运行
        workspace = Path(__file__).parent.parent.parent
        return workspace

    def test_validate_real_workflows(self, real_workspace):
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

        # 关键治理项错误类型（必须无 ERROR）
        critical_error_types = {
            "workflow_not_found",
            "missing_job",
            "missing_job_id",
            "frozen_step_name_changed",
            "frozen_job_name_changed",
            "schema_error",
        }

        critical_errors = [e for e in result.errors if e.error_type in critical_error_types]

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

    def test_validate_ci_workflow_exists(self, real_workspace):
        """测试 CI workflow 文件存在"""
        ci_workflow = real_workspace / ".github" / "workflows" / "ci.yml"
        assert ci_workflow.exists(), "CI workflow should exist"

    def test_validate_nightly_workflow_exists(self, real_workspace):
        """测试 Nightly workflow 文件存在"""
        nightly_workflow = real_workspace / ".github" / "workflows" / "nightly.yml"
        assert nightly_workflow.exists(), "Nightly workflow should exist"

    def test_validate_release_workflow_exists(self, real_workspace):
        """测试 Release workflow 文件存在"""
        release_workflow = real_workspace / ".github" / "workflows" / "release.yml"
        assert release_workflow.exists(), "Release workflow should exist"

    def test_frozen_step_validation_logic_works(self, real_workspace):
        """集成测试：验证 frozen step 校验逻辑正常工作"""
        contract_path = real_workspace / "scripts" / "ci" / "workflow_contract.v1.json"

        # 如果合约文件不存在，跳过测试
        if not contract_path.exists():
            pytest.skip("Contract file not found - run from project root")

        validator = WorkflowContractValidator(contract_path, real_workspace)
        result = validator.validate()

        # 输出结果供调试
        print(format_text_output(result))

        # 验证 frozen_steps 被正确加载
        assert len(validator.frozen_steps) > 0, "Should load frozen steps from contract"
        
        # 检查是否有 frozen_step_name_changed 错误
        frozen_errors = [e for e in result.errors if e.error_type == "frozen_step_name_changed"]
        
        if frozen_errors:
            # 输出警告信息，但不阻止测试
            # 在 CI 环境中，validate_workflows.py 会正确报告这些错误
            error_details = "\n".join([
                f"  - {e.key}: expected='{e.expected}', actual='{e.actual}'"
                for e in frozen_errors
            ])
            print(
                f"\nINFO: Frozen step name changes detected (validation working correctly):\n"
                f"{error_details}\n"
                f"如需改名，请同步更新 workflow_contract.v1.json 和 docs/ci_nightly_workflow_refactor/contract.md"
            )
            
        # 验证错误类型格式正确
        for error in frozen_errors:
            assert error.expected is not None, "frozen_step_name_changed error should have expected value"
            assert error.actual is not None, "frozen_step_name_changed error should have actual value"
            assert "冻结文案" in error.message, "Error message should mention '冻结文案'"
            assert "contract+docs" in error.message, "Error message should mention 'contract+docs'"

    def test_frozen_step_allowlist_consistency(self, real_workspace):
        """集成测试：验证 frozen_step_text.allowlist 与 required_steps 的一致性"""
        contract_path = real_workspace / "scripts" / "ci" / "workflow_contract.v1.json"

        if not contract_path.exists():
            pytest.skip("Contract file not found - run from project root")

        with open(contract_path, 'r') as f:
            contract = json.load(f)

        frozen_steps = set(contract.get("frozen_step_text", {}).get("allowlist", []))
        
        # 收集所有 required_steps
        all_required_steps = set()
        
        # 检查所有 workflow 定义
        for workflow_name, workflow_def in contract.items():
            if isinstance(workflow_def, dict) and "required_jobs" in workflow_def:
                for job in workflow_def["required_jobs"]:
                    all_required_steps.update(job.get("required_steps", []))
        
        # 检查是否有 required_step 不在 frozen_steps 中的关键 step
        # （这只是一个建议性检查，不强制要求所有 step 都冻结）
        critical_steps = {
            "Checkout repository",
            "Run CI precheck",
            "Verify build static (Dockerfile/compose config check)",
            "Check OpenMemory freeze status",
            "Run OpenMemory sync check",
            "Run OpenMemory sync verify",
        }
        
        missing_critical_frozen = critical_steps - frozen_steps
        if missing_critical_frozen:
            print(f"Warning: Critical steps not in frozen allowlist: {missing_critical_frozen}")
        
        # 确保 frozen_steps 中的所有 step 都在某个 required_steps 中
        # （除非是为将来预留的）
        print(f"Total frozen steps: {len(frozen_steps)}")
        print(f"Total required steps: {len(all_required_steps)}")

    def test_job_ids_and_names_validation(self, real_workspace):
        """集成测试：验证 job_ids 和 job_names 校验正常工作"""
        contract_path = real_workspace / "scripts" / "ci" / "workflow_contract.v1.json"

        if not contract_path.exists():
            pytest.skip("Contract file not found - run from project root")

        validator = WorkflowContractValidator(contract_path, real_workspace)
        result = validator.validate()

        # 输出结果供调试
        print(format_text_output(result))

        # 验证 frozen_job_names 被正确加载
        assert len(validator.frozen_job_names) > 0, "Should load frozen job names from contract"
        
        # 检查是否有 missing_job_id 错误
        missing_job_id_errors = [e for e in result.errors if e.error_type == "missing_job_id"]
        
        if missing_job_id_errors:
            error_details = "\n".join([
                f"  - {e.key}: in workflow {e.workflow}"
                for e in missing_job_id_errors
            ])
            print(f"\nMissing job IDs detected:\n{error_details}")
        
        # 检查是否有 frozen_job_name_changed 错误
        frozen_job_errors = [e for e in result.errors if e.error_type == "frozen_job_name_changed"]
        
        if frozen_job_errors:
            error_details = "\n".join([
                f"  - {e.key}: expected='{e.expected}', actual='{e.actual}'"
                for e in frozen_job_errors
            ])
            print(
                f"\nINFO: Frozen job name changes detected (validation working correctly):\n"
                f"{error_details}\n"
                f"如需改名，请同步更新 workflow_contract.v1.json 和 docs/ci_nightly_workflow_refactor/contract.md"
            )
        
        # 验证错误类型格式正确
        for error in frozen_job_errors:
            assert error.expected is not None, "frozen_job_name_changed error should have expected value"
            assert error.actual is not None, "frozen_job_name_changed error should have actual value"
            assert "冻结文案" in error.message, "Error message should mention '冻结文案'"
            assert "contract+docs" in error.message, "Error message should mention 'contract+docs'"

    def test_frozen_job_names_consistency(self, real_workspace):
        """集成测试：验证 frozen_job_names.allowlist 与 job_names 的一致性"""
        contract_path = real_workspace / "scripts" / "ci" / "workflow_contract.v1.json"

        if not contract_path.exists():
            pytest.skip("Contract file not found - run from project root")

        with open(contract_path, 'r') as f:
            contract = json.load(f)

        frozen_job_names = set(contract.get("frozen_job_names", {}).get("allowlist", []))
        
        # 收集所有 job_names
        all_job_names = set()
        
        # 检查所有 workflow 定义
        for workflow_name, workflow_def in contract.items():
            if isinstance(workflow_def, dict) and "job_names" in workflow_def:
                all_job_names.update(workflow_def.get("job_names", []))
        
        # 验证 frozen_job_names 中的 job name 都在某个 workflow 的 job_names 中
        orphan_frozen = frozen_job_names - all_job_names
        if orphan_frozen:
            print(f"Warning: Frozen job names not in any job_names: {orphan_frozen}")
        
        print(f"Total frozen job names: {len(frozen_job_names)}")
        print(f"Total job names: {len(all_job_names)}")


# ============================================================================
# CLI Tests
# ============================================================================

class TestCLI:
    """命令行接口测试"""

    def test_main_with_missing_contract(self, temp_workspace):
        """测试缺少合约文件时的命令行行为"""
        import subprocess

        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).parent.parent / "ci" / "validate_workflows.py"),
                "--contract", str(temp_workspace / "missing.json"),
                "--workspace", str(temp_workspace),
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 1
        assert "contract_not_found" in result.stdout or "not found" in result.stdout.lower()

    def test_main_json_output(self, temp_workspace, sample_contract, sample_workflow):
        """测试 JSON 输出模式"""
        import subprocess

        # 写入合约
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
            json.dump(sample_contract, f)

        # 写入 workflow
        (temp_workspace / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        with open(workflow_path, 'w') as f:
            f.write(sample_workflow)

        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).parent.parent / "ci" / "validate_workflows.py"),
                "--contract", str(contract_path),
                "--workspace", str(temp_workspace),
                "--json",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["success"] is True


# ============================================================================
# Makefile Parsing Tests
# ============================================================================

class TestMakefileParsing:
    """Makefile 解析测试"""

    def test_parse_empty_makefile(self, temp_workspace):
        """测试解析空 Makefile"""
        makefile = temp_workspace / "Makefile"
        makefile.write_text("")
        
        targets = parse_makefile_targets(makefile)
        assert targets == set()

    def test_parse_nonexistent_makefile(self, temp_workspace):
        """测试解析不存在的 Makefile"""
        makefile = temp_workspace / "nonexistent_Makefile"
        
        targets = parse_makefile_targets(makefile)
        assert targets == set()

    def test_parse_simple_targets(self, temp_workspace):
        """测试解析简单 target 定义"""
        makefile = temp_workspace / "Makefile"
        makefile.write_text("""
build:
	echo "building"

test:
	echo "testing"

deploy: build test
	echo "deploying"
""")
        
        targets = parse_makefile_targets(makefile)
        assert "build" in targets
        assert "test" in targets
        assert "deploy" in targets

    def test_parse_phony_targets(self, temp_workspace):
        """测试解析 .PHONY 声明"""
        makefile = temp_workspace / "Makefile"
        makefile.write_text("""
.PHONY: build test deploy clean

build:
	echo "building"

test:
	echo "testing"
""")
        
        targets = parse_makefile_targets(makefile)
        assert "build" in targets
        assert "test" in targets
        assert "deploy" in targets
        assert "clean" in targets

    def test_parse_multiline_phony(self, temp_workspace):
        """测试解析多行 .PHONY 声明"""
        makefile = temp_workspace / "Makefile"
        makefile.write_text(r"""
.PHONY: build test deploy \
        clean lint format

build:
	echo "building"
""")
        
        targets = parse_makefile_targets(makefile)
        assert "build" in targets
        assert "test" in targets
        assert "deploy" in targets
        assert "clean" in targets
        assert "lint" in targets
        assert "format" in targets

    def test_parse_targets_with_dashes_and_underscores(self, temp_workspace):
        """测试解析带连字符和下划线的 target"""
        makefile = temp_workspace / "Makefile"
        makefile.write_text("""
.PHONY: test-unit test_integration ci-precheck verify-build-static

test-unit:
	echo "unit tests"

test_integration:
	echo "integration tests"

ci-precheck:
	echo "precheck"

verify-build-static:
	echo "verify"
""")
        
        targets = parse_makefile_targets(makefile)
        assert "test-unit" in targets
        assert "test_integration" in targets
        assert "ci-precheck" in targets
        assert "verify-build-static" in targets

    def test_parse_ignores_comments(self, temp_workspace):
        """测试忽略注释行"""
        makefile = temp_workspace / "Makefile"
        makefile.write_text("""
# This is a comment
.PHONY: build

# build target
build:
	# This is a comment in recipe
	echo "building"
""")
        
        targets = parse_makefile_targets(makefile)
        assert "build" in targets
        # Comments should not be parsed as targets
        assert "This" not in targets

    def test_parse_real_makefile(self):
        """测试解析真实 Makefile（集成测试）"""
        workspace = Path(__file__).parent.parent.parent
        makefile = workspace / "Makefile"
        
        if not makefile.exists():
            pytest.skip("Makefile not found - run from project root")
        
        targets = parse_makefile_targets(makefile)
        
        # 验证一些已知的 targets
        expected_targets = [
            "ci-precheck",
            "deploy",
            "verify-build-static",
            "test-logbook-unit",
            "test-gateway-integration",
        ]
        
        for target in expected_targets:
            assert target in targets, f"Expected target '{target}' not found in Makefile"


# ============================================================================
# Workflow Make Call Extraction Tests
# ============================================================================

class TestWorkflowMakeCallExtraction:
    """Workflow make 调用提取测试"""

    def test_extract_no_make_calls(self, temp_workspace):
        """测试无 make 调用的 workflow"""
        workflow_path = temp_workspace / "workflow.yml"
        workflow_path.write_text("""
name: Test
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - name: Run script
        run: python test.py
""")
        
        calls = extract_workflow_make_calls(workflow_path)
        assert calls == []

    def test_extract_simple_make_call(self, temp_workspace):
        """测试提取简单 make 调用"""
        workflow_path = temp_workspace / "workflow.yml"
        workflow_path.write_text("""
name: Test
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Run tests
        run: make test
""")
        
        calls = extract_workflow_make_calls(workflow_path)
        assert len(calls) == 1
        assert calls[0]['target'] == 'test'
        assert calls[0]['job'] == 'test'
        assert calls[0]['step'] == 'Run tests'

    def test_extract_multiple_make_calls(self, temp_workspace):
        """测试提取多个 make 调用"""
        workflow_path = temp_workspace / "workflow.yml"
        workflow_path.write_text("""
name: Test
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Build
        run: make build
      - name: Test
        run: make test-unit
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Deploy
        run: make deploy
""")
        
        calls = extract_workflow_make_calls(workflow_path)
        assert len(calls) == 3
        
        targets = {c['target'] for c in calls}
        assert targets == {'build', 'test-unit', 'deploy'}

    def test_extract_make_call_with_flags(self, temp_workspace):
        """测试提取带 flag 的 make 调用"""
        workflow_path = temp_workspace / "workflow.yml"
        workflow_path.write_text("""
name: Test
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Run tests
        run: make -j4 test
      - name: Build with directory
        run: make -C subdir build
""")
        
        calls = extract_workflow_make_calls(workflow_path)
        targets = {c['target'] for c in calls}
        # -C subdir build should extract 'build'
        assert 'test' in targets or 'build' in targets

    def test_extract_make_call_in_multiline_run(self, temp_workspace):
        """测试在多行 run 中提取 make 调用"""
        workflow_path = temp_workspace / "workflow.yml"
        workflow_path.write_text("""
name: Test
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Setup and test
        run: |
          echo "Setting up..."
          make setup
          echo "Testing..."
          make test-unit
""")
        
        calls = extract_workflow_make_calls(workflow_path)
        targets = {c['target'] for c in calls}
        assert 'setup' in targets
        assert 'test-unit' in targets

    def test_skip_variable_targets(self, temp_workspace):
        """测试跳过变量形式的 target"""
        workflow_path = temp_workspace / "workflow.yml"
        workflow_path.write_text("""
name: Test
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Run with variable
        run: make ${{ env.TARGET }}
      - name: Run normal
        run: make test
""")
        
        calls = extract_workflow_make_calls(workflow_path)
        # 应该只提取 'test'，跳过变量形式的调用
        targets = {c['target'] for c in calls}
        assert 'test' in targets
        # 变量不应该被当作 target
        assert '${{' not in str(targets)

    def test_extract_nonexistent_workflow(self, temp_workspace):
        """测试处理不存在的 workflow 文件"""
        workflow_path = temp_workspace / "nonexistent.yml"
        
        calls = extract_workflow_make_calls(workflow_path)
        assert calls == []

    def test_extract_invalid_yaml(self, temp_workspace):
        """测试处理无效 YAML"""
        workflow_path = temp_workspace / "invalid.yml"
        workflow_path.write_text("{ invalid yaml: [")
        
        calls = extract_workflow_make_calls(workflow_path)
        assert calls == []


# ============================================================================
# Makefile Target Validation Tests (contract -> Makefile)
# ============================================================================

class TestMakefileTargetValidation:
    """Makefile target 校验测试 (contract -> Makefile)"""

    def test_validate_all_targets_exist(self, temp_workspace):
        """测试所有 targets 都存在于 Makefile"""
        # 创建 Makefile
        makefile = temp_workspace / "Makefile"
        makefile.write_text("""
.PHONY: build test deploy

build:
	echo "building"

test:
	echo "testing"

deploy:
	echo "deploying"
""")
        
        # 创建 contract
        contract_path = temp_workspace / "contract.json"
        contract = {
            "version": "1.0.0",
            "make": {
                "targets_required": ["build", "test", "deploy"]
            }
        }
        with open(contract_path, 'w') as f:
            json.dump(contract, f)
        
        validator = WorkflowContractValidator(contract_path, temp_workspace)
        validator.load_contract()
        result = validator.validate_makefile_targets()
        
        assert result is True
        missing_errors = [e for e in validator.result.errors if e.error_type == "missing_makefile_target"]
        assert len(missing_errors) == 0

    def test_validate_missing_target(self, temp_workspace):
        """测试检测缺失的 target"""
        # 创建 Makefile（缺少 deploy）
        makefile = temp_workspace / "Makefile"
        makefile.write_text("""
.PHONY: build test

build:
	echo "building"

test:
	echo "testing"
""")
        
        # 创建 contract（要求 deploy）
        contract_path = temp_workspace / "contract.json"
        contract = {
            "version": "1.0.0",
            "make": {
                "targets_required": ["build", "test", "deploy"]
            }
        }
        with open(contract_path, 'w') as f:
            json.dump(contract, f)
        
        validator = WorkflowContractValidator(contract_path, temp_workspace)
        validator.load_contract()
        result = validator.validate_makefile_targets()
        
        assert result is False
        missing_errors = [e for e in validator.result.errors if e.error_type == "missing_makefile_target"]
        assert len(missing_errors) == 1
        assert missing_errors[0].key == "deploy"
        assert "ERROR" in missing_errors[0].message

    def test_validate_no_targets_required(self, temp_workspace):
        """测试没有 targets_required 时跳过校验"""
        makefile = temp_workspace / "Makefile"
        makefile.write_text("build:\n\techo building")
        
        contract_path = temp_workspace / "contract.json"
        contract = {
            "version": "1.0.0",
            "make": {}
        }
        with open(contract_path, 'w') as f:
            json.dump(contract, f)
        
        validator = WorkflowContractValidator(contract_path, temp_workspace)
        validator.load_contract()
        result = validator.validate_makefile_targets()
        
        assert result is True


# ============================================================================
# Workflow Make Call Validation Tests (workflow -> contract)
# ============================================================================

class TestWorkflowMakeCallValidation:
    """Workflow make 调用校验测试 (workflow -> contract)"""

    def test_validate_all_calls_declared(self, temp_workspace):
        """测试所有 make 调用都在 contract 中声明"""
        # 创建 workflow
        (temp_workspace / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_path.write_text("""
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Build
        run: make build
      - name: Test
        run: make test
""")
        
        # 创建 contract（声明了所有 targets）
        contract_path = temp_workspace / "contract.json"
        contract = {
            "version": "1.0.0",
            "make": {
                "targets_required": ["build", "test"]
            }
        }
        with open(contract_path, 'w') as f:
            json.dump(contract, f)
        
        validator = WorkflowContractValidator(contract_path, temp_workspace)
        validator.load_contract()
        result = validator.validate_workflow_make_calls(
            workflow_files=[".github/workflows/ci.yml"]
        )
        
        assert result is True
        undeclared_errors = [e for e in validator.result.errors if e.error_type == "undeclared_make_target"]
        assert len(undeclared_errors) == 0

    def test_validate_undeclared_make_call(self, temp_workspace):
        """测试检测未声明的 make 调用"""
        # 创建 workflow
        (temp_workspace / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_path.write_text("""
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Build
        run: make build
      - name: Undeclared target
        run: make secret-target
""")
        
        # 创建 contract（只声明了 build）
        contract_path = temp_workspace / "contract.json"
        contract = {
            "version": "1.0.0",
            "make": {
                "targets_required": ["build"]
            }
        }
        with open(contract_path, 'w') as f:
            json.dump(contract, f)
        
        validator = WorkflowContractValidator(contract_path, temp_workspace)
        validator.load_contract()
        result = validator.validate_workflow_make_calls(
            workflow_files=[".github/workflows/ci.yml"]
        )
        
        assert result is False
        undeclared_errors = [e for e in validator.result.errors if e.error_type == "undeclared_make_target"]
        assert len(undeclared_errors) == 1
        assert undeclared_errors[0].key == "secret-target"
        assert "ERROR" in undeclared_errors[0].message

    def test_validate_with_ignore_list(self, temp_workspace):
        """测试 ignore list 功能"""
        # 创建 workflow
        (temp_workspace / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_path.write_text("""
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Deploy
        run: make deploy
""")
        
        # 创建 contract（没有声明 deploy）
        contract_path = temp_workspace / "contract.json"
        contract = {
            "version": "1.0.0",
            "make": {
                "targets_required": []
            }
        }
        with open(contract_path, 'w') as f:
            json.dump(contract, f)
        
        validator = WorkflowContractValidator(contract_path, temp_workspace)
        validator.load_contract()
        
        # 使用 ignore list 忽略 deploy
        result = validator.validate_workflow_make_calls(
            workflow_files=[".github/workflows/ci.yml"],
            ignore_list={"deploy"}
        )
        
        assert result is True
        undeclared_errors = [e for e in validator.result.errors if e.error_type == "undeclared_make_target"]
        assert len(undeclared_errors) == 0


# ============================================================================
# Integration Tests - Real Files
# ============================================================================

class TestRealMakefileAndWorkflowValidation:
    """真实文件校验集成测试"""

    @pytest.fixture
    def real_workspace(self):
        """获取真实工作空间路径"""
        workspace = Path(__file__).parent.parent.parent
        return workspace

    def test_real_contract_makefile_targets(self, real_workspace):
        """测试真实 contract 的 make targets 都存在于 Makefile（无 ERROR 断言）"""
        contract_path = real_workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        makefile_path = real_workspace / "Makefile"

        if not contract_path.exists():
            pytest.skip("Contract file not found - run from project root")
        if not makefile_path.exists():
            pytest.skip("Makefile not found - run from project root")

        validator = WorkflowContractValidator(contract_path, real_workspace)
        validator.load_contract()
        result = validator.validate_makefile_targets()

        # 输出结果供调试
        missing_errors = [e for e in validator.result.errors if e.error_type == "missing_makefile_target"]
        if missing_errors:
            for error in missing_errors:
                print(f"Missing target: {error.key}")

        # 关键治理项：make.targets_required 中的 target 必须存在于 Makefile
        assert result is True, (
            f"All contract make.targets_required should exist in Makefile. "
            f"Missing: {[e.key for e in missing_errors]}"
        )

    def test_real_workflow_make_calls(self, real_workspace):
        """测试真实 workflow 的 make 调用都在 contract 中声明（无 ERROR 断言）"""
        contract_path = real_workspace / "scripts" / "ci" / "workflow_contract.v1.json"

        if not contract_path.exists():
            pytest.skip("Contract file not found - run from project root")

        validator = WorkflowContractValidator(contract_path, real_workspace)
        validator.load_contract()
        result = validator.validate_workflow_make_calls()

        # 输出结果供调试
        undeclared_errors = [e for e in validator.result.errors if e.error_type == "undeclared_make_target"]
        if undeclared_errors:
            for error in undeclared_errors:
                print(f"Undeclared target: {error.key} in {error.workflow}")

        # 关键治理项：workflow 中调用的 make target 必须在 contract 中声明
        assert result is True, (
            f"All workflow make calls should be declared in contract make.targets_required. "
            f"Undeclared: {[e.key for e in undeclared_errors]}"
        )


# ============================================================================
# Schema File Existence and Validation Tests
# ============================================================================

class TestSchemaFileExistsAndValidatesContract:
    """测试 schema 文件存在且可校验当前 contract"""

    @pytest.fixture
    def real_workspace(self):
        """获取真实工作空间路径"""
        workspace = Path(__file__).parent.parent.parent
        return workspace

    def test_schema_file_exists(self, real_workspace):
        """测试 schema 文件存在"""
        schema_path = real_workspace / "scripts" / "ci" / "workflow_contract.v1.schema.json"
        assert schema_path.exists(), f"Schema file should exist at {schema_path}"

    def test_contract_file_exists(self, real_workspace):
        """测试 contract 文件存在"""
        contract_path = real_workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        assert contract_path.exists(), f"Contract file should exist at {contract_path}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_real_contract_passes_real_schema(self, real_workspace):
        """测试真实 contract 通过真实 schema 校验（关键治理项）"""
        contract_path = real_workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        schema_path = real_workspace / "scripts" / "ci" / "workflow_contract.v1.schema.json"

        if not contract_path.exists():
            pytest.skip("Contract file not found")
        if not schema_path.exists():
            pytest.skip("Schema file not found")

        validator = WorkflowContractValidator(contract_path, real_workspace)
        assert validator.load_contract() is True, "Contract should load successfully"
        
        result = validator.validate_schema()

        # 输出 schema 校验错误供调试
        schema_errors = [e for e in validator.result.errors if e.error_type == "schema_error"]
        if schema_errors:
            for error in schema_errors:
                print(f"Schema error at {error.location}: {error.message}")
                print(f"  Expected: {error.expected}")
                print(f"  Actual: {error.actual}")

        # 关键治理项：contract 必须通过 schema 校验
        assert result is True, (
            f"Real contract must pass schema validation. "
            f"Errors: {[f'{e.location}: {e.message}' for e in schema_errors]}"
        )

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_schema_validates_version_field(self, real_workspace):
        """测试 schema 能校验 version 字段"""
        contract_path = real_workspace / "scripts" / "ci" / "workflow_contract.v1.json"

        if not contract_path.exists():
            pytest.skip("Contract file not found")

        with open(contract_path, 'r') as f:
            contract = json.load(f)

        # 验证 version 字段存在且格式正确
        assert "version" in contract, "Contract must have version field"
        version = contract["version"]
        assert isinstance(version, str), "Version must be a string"
        # 验证 semver 格式 (X.Y.Z)
        import re
        assert re.match(r'^\d+\.\d+\.\d+$', version), f"Version '{version}' should be semver format (X.Y.Z)"


# ============================================================================
# Frozen Step Rename Failure Tests (Integration)
# ============================================================================

class TestFrozenStepRenameFailure:
    """测试 frozen step 改名会导致失败（集成测试）"""

    @pytest.fixture
    def real_workspace(self):
        """获取真实工作空间路径"""
        workspace = Path(__file__).parent.parent.parent
        return workspace

    def test_frozen_step_list_not_empty(self, real_workspace):
        """测试 frozen_step_text.allowlist 非空"""
        contract_path = real_workspace / "scripts" / "ci" / "workflow_contract.v1.json"

        if not contract_path.exists():
            pytest.skip("Contract file not found")

        with open(contract_path, 'r') as f:
            contract = json.load(f)

        frozen_steps = contract.get("frozen_step_text", {}).get("allowlist", [])
        assert len(frozen_steps) > 0, "frozen_step_text.allowlist should not be empty"

    def test_frozen_step_in_workflow_exact_match(self, real_workspace):
        """测试 frozen step 在 workflow 中精确匹配（无 frozen_step_name_changed 错误）"""
        contract_path = real_workspace / "scripts" / "ci" / "workflow_contract.v1.json"

        if not contract_path.exists():
            pytest.skip("Contract file not found")

        validator = WorkflowContractValidator(contract_path, real_workspace)
        result = validator.validate()

        # 检查是否有 frozen_step_name_changed 错误
        frozen_step_errors = [e for e in result.errors if e.error_type == "frozen_step_name_changed"]

        if frozen_step_errors:
            error_details = "\n".join([
                f"  - {e.key}: expected='{e.expected}', actual='{e.actual}' in {e.workflow}"
                for e in frozen_step_errors
            ])
            print(f"\nFrozen step name changes detected:\n{error_details}")

        # 关键治理项：不应有 frozen step 改名错误
        assert len(frozen_step_errors) == 0, (
            f"Frozen step names must match exactly. "
            f"Found {len(frozen_step_errors)} frozen step name changes. "
            f"If intentional, update workflow_contract.v1.json and docs accordingly."
        )

    def test_simulated_frozen_step_rename_fails(self, temp_workspace):
        """模拟测试：frozen step 改名应导致 ERROR"""
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {
                "allowlist": ["Checkout repository", "Run tests"]
            },
            "workflows": {
                "ci": {
                    "file": ".github/workflows/ci.yml",
                    "required_jobs": [
                        {
                            "id": "test-job",
                            "name": "Test Job",
                            "required_steps": ["Checkout repository", "Run tests"],
                            "required_outputs": []
                        }
                    ],
                    "required_env_vars": []
                }
            }
        }

        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
            json.dump(contract, f)

        # 创建 workflow，将 frozen step "Run tests" 改名为 "Execute tests"
        (temp_workspace / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
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
      - name: Execute tests
        run: echo "testing"
"""
        with open(workflow_path, 'w') as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该失败
        assert result.success is False, "Should fail when frozen step is renamed"

        # 应该有 frozen_step_name_changed 错误
        frozen_errors = [e for e in result.errors if e.error_type == "frozen_step_name_changed"]
        assert len(frozen_errors) == 1, "Should have exactly one frozen step name change error"
        assert frozen_errors[0].key == "Run tests"
        assert frozen_errors[0].expected == "Run tests"
        assert frozen_errors[0].actual == "Execute tests"


# ============================================================================
# Make Target Required Validation Tests
# ============================================================================

class TestMakeTargetRequiredValidation:
    """测试 make.targets_required 中任意 target 缺失会失败"""

    def test_missing_single_target_fails(self, temp_workspace):
        """测试缺失单个 target 导致失败"""
        # 创建 Makefile（缺少 build）
        makefile = temp_workspace / "Makefile"
        makefile.write_text("""
.PHONY: test deploy

test:
\techo "testing"

deploy:
\techo "deploying"
""")

        # 创建 contract（要求 build, test, deploy）
        contract_path = temp_workspace / "contract.json"
        contract = {
            "version": "1.0.0",
            "make": {
                "targets_required": ["build", "test", "deploy"]
            }
        }
        with open(contract_path, 'w') as f:
            json.dump(contract, f)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        validator.load_contract()
        result = validator.validate_makefile_targets()

        # 应该失败
        assert result is False, "Should fail when a required target is missing"

        # 应该有 missing_makefile_target 错误
        missing_errors = [e for e in validator.result.errors if e.error_type == "missing_makefile_target"]
        assert len(missing_errors) == 1
        assert missing_errors[0].key == "build"

    def test_missing_multiple_targets_fails(self, temp_workspace):
        """测试缺失多个 target 导致失败，报告所有缺失"""
        # 创建 Makefile（只有 test）
        makefile = temp_workspace / "Makefile"
        makefile.write_text("""
.PHONY: test

test:
\techo "testing"
""")

        # 创建 contract（要求 build, test, deploy, lint）
        contract_path = temp_workspace / "contract.json"
        contract = {
            "version": "1.0.0",
            "make": {
                "targets_required": ["build", "test", "deploy", "lint"]
            }
        }
        with open(contract_path, 'w') as f:
            json.dump(contract, f)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        validator.load_contract()
        result = validator.validate_makefile_targets()

        # 应该失败
        assert result is False

        # 应该报告所有缺失的 targets
        missing_errors = [e for e in validator.result.errors if e.error_type == "missing_makefile_target"]
        assert len(missing_errors) == 3
        missing_targets = {e.key for e in missing_errors}
        assert missing_targets == {"build", "deploy", "lint"}


# ============================================================================
# Workflow Make Call Undeclared Tests
# ============================================================================

class TestWorkflowMakeCallUndeclared:
    """测试 workflow 中新增 make target 未加入 contract 会失败"""

    def test_undeclared_make_target_fails(self, temp_workspace):
        """测试未声明的 make target 导致失败"""
        # 创建 workflow
        (temp_workspace / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_path.write_text("""
name: CI
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Build
        run: make build
      - name: Secret operation
        run: make secret-internal-target
""")

        # 创建 contract（只声明了 build）
        contract_path = temp_workspace / "contract.json"
        contract = {
            "version": "1.0.0",
            "make": {
                "targets_required": ["build"]
            }
        }
        with open(contract_path, 'w') as f:
            json.dump(contract, f)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        validator.load_contract()
        result = validator.validate_workflow_make_calls(
            workflow_files=[".github/workflows/ci.yml"]
        )

        # 应该失败
        assert result is False, "Should fail when workflow uses undeclared make target"

        # 应该有 undeclared_make_target 错误
        undeclared_errors = [e for e in validator.result.errors if e.error_type == "undeclared_make_target"]
        assert len(undeclared_errors) == 1
        assert undeclared_errors[0].key == "secret-internal-target"
        assert "ERROR" in undeclared_errors[0].message

    def test_multiple_undeclared_targets_all_reported(self, temp_workspace):
        """测试多个未声明 target 都被报告"""
        # 创建 workflow
        (temp_workspace / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_path.write_text("""
name: CI
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Build
        run: make build
      - name: Step A
        run: make undeclared-a
      - name: Step B
        run: make undeclared-b
""")

        # 创建 contract（只声明了 build）
        contract_path = temp_workspace / "contract.json"
        contract = {
            "version": "1.0.0",
            "make": {
                "targets_required": ["build"]
            }
        }
        with open(contract_path, 'w') as f:
            json.dump(contract, f)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        validator.load_contract()
        result = validator.validate_workflow_make_calls(
            workflow_files=[".github/workflows/ci.yml"]
        )

        # 应该失败
        assert result is False

        # 应该报告所有未声明的 targets
        undeclared_errors = [e for e in validator.result.errors if e.error_type == "undeclared_make_target"]
        assert len(undeclared_errors) == 2
        undeclared_targets = {e.key for e in undeclared_errors}
        assert undeclared_targets == {"undeclared-a", "undeclared-b"}


# ============================================================================
# Real Workflow Validation Integration Tests (Enhanced)
# ============================================================================

class TestRealWorkflowValidationNoErrors:
    """真实 workflow 校验集成测试 - 关键治理项无 ERROR 断言"""

    @pytest.fixture
    def real_workspace(self):
        """获取真实工作空间路径"""
        workspace = Path(__file__).parent.parent.parent
        return workspace

    def test_real_workflow_validation_no_critical_errors(self, real_workspace):
        """测试真实 workflow 校验无关键治理项 ERROR（warning 允许）"""
        contract_path = real_workspace / "scripts" / "ci" / "workflow_contract.v1.json"

        if not contract_path.exists():
            pytest.skip("Contract file not found - run from project root")

        validator = WorkflowContractValidator(contract_path, real_workspace)
        result = validator.validate()

        # 输出完整结果供调试
        print(format_text_output(result))

        # 关键治理项错误类型（必须无 ERROR）
        critical_error_types = {
            "frozen_step_name_changed",
            "frozen_job_name_changed",
            "missing_makefile_target",
            "undeclared_make_target",
            "schema_error",
            "missing_job_id",
        }

        critical_errors = [e for e in result.errors if e.error_type in critical_error_types]

        if critical_errors:
            error_details = "\n".join([
                f"  [{e.error_type}] {e.key}: {e.message}"
                for e in critical_errors
            ])
            print(f"\nCritical errors found:\n{error_details}")

        # 断言：关键治理项无 ERROR
        assert len(critical_errors) == 0, (
            f"Real workflow validation should have no critical errors. "
            f"Found {len(critical_errors)} critical errors: "
            f"{[e.error_type + ':' + e.key for e in critical_errors]}"
        )

        # Warnings 允许存在，但输出供审查
        if result.warnings:
            print(f"\nWarnings (allowed, for review): {len(result.warnings)}")
            for w in result.warnings:
                print(f"  [{w.warning_type}] {w.key}: {w.message}")

    def test_all_workflow_files_exist(self, real_workspace):
        """测试所有 contract 中定义的 workflow 文件都存在"""
        contract_path = real_workspace / "scripts" / "ci" / "workflow_contract.v1.json"

        if not contract_path.exists():
            pytest.skip("Contract file not found")

        with open(contract_path, 'r') as f:
            contract = json.load(f)

        # 获取所有 workflow 定义
        workflow_files = []
        for key, value in contract.items():
            if isinstance(value, dict) and "file" in value:
                workflow_files.append(value["file"])

        # 验证每个 workflow 文件存在
        missing_files = []
        for workflow_file in workflow_files:
            full_path = real_workspace / workflow_file
            if not full_path.exists():
                missing_files.append(workflow_file)

        assert len(missing_files) == 0, (
            f"All workflow files in contract should exist. "
            f"Missing: {missing_files}"
        )

    def test_all_required_jobs_exist(self, real_workspace):
        """测试所有 contract 中定义的 required_jobs 都存在于 workflow"""
        contract_path = real_workspace / "scripts" / "ci" / "workflow_contract.v1.json"

        if not contract_path.exists():
            pytest.skip("Contract file not found")

        validator = WorkflowContractValidator(contract_path, real_workspace)
        result = validator.validate()

        # 检查 missing_job 和 missing_job_id 错误
        missing_job_errors = [
            e for e in result.errors
            if e.error_type in ("missing_job", "missing_job_id")
        ]

        if missing_job_errors:
            error_details = "\n".join([
                f"  {e.workflow}: {e.key} ({e.error_type})"
                for e in missing_job_errors
            ])
            print(f"\nMissing jobs:\n{error_details}")

        # 断言：所有 required_jobs 都存在
        assert len(missing_job_errors) == 0, (
            f"All required jobs should exist in workflows. "
            f"Missing: {[e.key for e in missing_job_errors]}"
        )


# ============================================================================
# CI Labels Validation Tests
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
                    "test:freeze-override"
                ],
                "required_jobs": [],
                "required_env_vars": []
            }
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

    def test_labels_match_success(self, temp_workspace, contract_with_labels, matching_label_script):
        """测试 labels 匹配成功的情况"""
        # 写入合约
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
            json.dump(contract_with_labels, f)

        # 创建 scripts/ci 目录并写入脚本
        scripts_ci_dir = temp_workspace / "scripts" / "ci"
        scripts_ci_dir.mkdir(parents=True)
        
        script_path = scripts_ci_dir / "gh_pr_labels_to_outputs.py"
        with open(script_path, 'w') as f:
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
        with open(workflow_path, 'w') as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 不应该有 label 相关的错误
        label_errors = [e for e in result.errors if e.error_type.startswith("label_")]
        assert len(label_errors) == 0, f"Expected no label errors, got: {[e.message for e in label_errors]}"

    def test_labels_missing_in_script(self, temp_workspace, contract_with_labels, mismatched_label_script_missing):
        """测试 contract 中有但脚本中没有的 label"""
        # 写入合约
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
            json.dump(contract_with_labels, f)

        # 创建 scripts/ci 目录并写入脚本
        scripts_ci_dir = temp_workspace / "scripts" / "ci"
        scripts_ci_dir.mkdir(parents=True)
        
        script_path = scripts_ci_dir / "gh_pr_labels_to_outputs.py"
        with open(script_path, 'w') as f:
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
        with open(workflow_path, 'w') as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该有 label_missing_in_script 错误
        label_errors = [e for e in result.errors if e.error_type == "label_missing_in_script"]
        assert len(label_errors) == 1
        assert label_errors[0].key == "ci:test-label-2"
        assert "not found as a LABEL_* constant" in label_errors[0].message

    def test_labels_missing_in_contract(self, temp_workspace, contract_with_labels, mismatched_label_script_extra):
        """测试脚本中有但 contract 中没有的 label"""
        # 写入合约
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
            json.dump(contract_with_labels, f)

        # 创建 scripts/ci 目录并写入脚本
        scripts_ci_dir = temp_workspace / "scripts" / "ci"
        scripts_ci_dir.mkdir(parents=True)
        
        script_path = scripts_ci_dir / "gh_pr_labels_to_outputs.py"
        with open(script_path, 'w') as f:
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
        with open(workflow_path, 'w') as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该有 label_missing_in_contract 错误
        label_errors = [e for e in result.errors if e.error_type == "label_missing_in_contract"]
        assert len(label_errors) == 1
        assert label_errors[0].key == "ci:extra-label"
        assert "not found in contract.ci.labels" in label_errors[0].message

    def test_labels_script_not_found(self, temp_workspace, contract_with_labels):
        """测试脚本不存在时的处理（应该只是警告）"""
        # 写入合约
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
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
        with open(workflow_path, 'w') as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该有警告而不是错误
        label_warnings = [w for w in result.warnings if w.warning_type == "label_script_parse_warning"]
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
                "required_env_vars": []
            }
        }
        
        contract_path = temp_workspace / "contract.json"
        with open(contract_path, 'w') as f:
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
        with open(workflow_path, 'w') as f:
            f.write(workflow_content)

        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 不应该有 label 相关的错误或警告
        label_errors = [e for e in result.errors if e.error_type.startswith("label_")]
        label_warnings = [w for w in result.warnings if w.warning_type.startswith("label_")]
        assert len(label_errors) == 0
        assert len(label_warnings) == 0


class TestCILabelsRealFileValidation:
    """真实文件的 CI Labels 一致性校验测试"""

    def test_real_contract_and_script_labels_match(self):
        """测试真实 contract 和脚本中的 labels 是否一致"""
        # 获取项目根目录
        workspace_root = Path(__file__).parent.parent.parent
        contract_path = workspace_root / "scripts" / "ci" / "workflow_contract.v1.json"
        script_path = workspace_root / "scripts" / "ci" / "gh_pr_labels_to_outputs.py"
        
        if not contract_path.exists() or not script_path.exists():
            pytest.skip("Required files not found in workspace")
        
        # 加载 contract
        with open(contract_path, 'r') as f:
            contract = json.load(f)
        
        contract_labels = set(contract.get("ci", {}).get("labels", []))
        
        # 解析脚本中的 LABEL_* 常量
        import ast
        with open(script_path, 'r') as f:
            source = f.read()
        
        tree = ast.parse(source)
        script_labels = set()
        
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.startswith("LABEL_"):
                        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                            script_labels.add(node.value.value)
        
        # 验证一致性
        missing_in_script = contract_labels - script_labels
        missing_in_contract = script_labels - contract_labels
        
        error_msgs = []
        if missing_in_script:
            error_msgs.append(f"Labels in contract but not in script: {missing_in_script}")
        if missing_in_contract:
            error_msgs.append(f"Labels in script but not in contract: {missing_in_contract}")
        
        assert len(error_msgs) == 0, "\n".join(error_msgs)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
