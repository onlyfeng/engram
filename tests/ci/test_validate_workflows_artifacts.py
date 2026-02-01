#!/usr/bin/env python3
"""
Workflow Contract Validator - Artifact Archive 单元测试

覆盖功能:
1. extract_upload_artifact_paths: 从 workflow 中提取 upload-artifact 步骤
2. check_artifact_path_coverage: 验证 required paths 覆盖情况
3. validate_artifact_archive: 集成到 WorkflowContractValidator 的验证

Phase 0 说明：
- 仅测试 artifact 相关的提取和校验逻辑
- 不依赖 nightly/release workflow
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

# 导入被测模块
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))

from validate_workflows import (
    WorkflowContractValidator,
    check_artifact_path_coverage,
    extract_upload_artifact_paths,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_workspace():
    """创建临时工作空间"""
    with tempfile.TemporaryDirectory(prefix="test_artifact_") as tmpdir:
        workspace = Path(tmpdir)
        (workspace / ".github" / "workflows").mkdir(parents=True)
        yield workspace


@pytest.fixture
def sample_workflow_with_artifacts():
    """包含 upload-artifact 步骤的示例 workflow"""
    return {
        "name": "CI",
        "on": ["push"],
        "jobs": {
            "test-job": {
                "name": "Test Job",
                "runs-on": "ubuntu-latest",
                "steps": [
                    {"name": "Checkout repository", "uses": "actions/checkout@v4"},
                    {"name": "Run tests", "run": "make test"},
                    {
                        "name": "Upload verification results",
                        "uses": "actions/upload-artifact@v4",
                        "with": {
                            "name": "verification-results",
                            "path": ".artifacts/verify-results.json",
                        },
                    },
                    {
                        "name": "Upload acceptance run records",
                        "uses": "actions/upload-artifact@v4",
                        "with": {"name": "acceptance-runs", "path": ".artifacts/acceptance-runs/"},
                    },
                ],
            },
            "build-job": {
                "name": "Build Job",
                "runs-on": "ubuntu-latest",
                "steps": [
                    {"name": "Build", "run": "make build"},
                    {
                        "name": "Upload build artifacts",
                        "uses": "actions/upload-artifact@v4",
                        "with": {"name": "build-artifacts", "path": ".artifacts/build/"},
                    },
                ],
            },
        },
    }


@pytest.fixture
def sample_workflow_with_multiline_paths():
    """包含多行 path 的 upload-artifact 步骤"""
    return {
        "name": "CI",
        "on": ["push"],
        "jobs": {
            "test-job": {
                "name": "Test Job",
                "runs-on": "ubuntu-latest",
                "steps": [
                    {
                        "name": "Upload multiple artifacts",
                        "uses": "actions/upload-artifact@v4",
                        "with": {
                            "name": "all-artifacts",
                            "path": """.artifacts/verify-results.json
.artifacts/acceptance-runs/
.artifacts/test-results/""",
                        },
                    }
                ],
            }
        },
    }


# ============================================================================
# extract_upload_artifact_paths Tests
# ============================================================================


class TestExtractUploadArtifactPaths:
    """extract_upload_artifact_paths 函数测试"""

    def test_extract_single_path(self, sample_workflow_with_artifacts):
        """测试提取单个路径"""
        results = extract_upload_artifact_paths(sample_workflow_with_artifacts)

        assert len(results) == 3

        # 检查第一个 upload step
        step1 = next(r for r in results if r["step_name"] == "Upload verification results")
        assert step1["job_id"] == "test-job"
        assert step1["paths"] == [".artifacts/verify-results.json"]

        # 检查第二个 upload step
        step2 = next(r for r in results if r["step_name"] == "Upload acceptance run records")
        assert step2["paths"] == [".artifacts/acceptance-runs/"]

    def test_extract_multiline_paths(self, sample_workflow_with_multiline_paths):
        """测试提取多行路径"""
        results = extract_upload_artifact_paths(sample_workflow_with_multiline_paths)

        assert len(results) == 1

        step = results[0]
        assert step["step_name"] == "Upload multiple artifacts"
        assert len(step["paths"]) == 3
        assert ".artifacts/verify-results.json" in step["paths"]
        assert ".artifacts/acceptance-runs/" in step["paths"]
        assert ".artifacts/test-results/" in step["paths"]

    def test_extract_empty_workflow(self):
        """测试空 workflow"""
        results = extract_upload_artifact_paths({})
        assert results == []

        results = extract_upload_artifact_paths({"jobs": {}})
        assert results == []

    def test_ignore_non_upload_artifact_steps(self):
        """测试忽略非 upload-artifact 步骤"""
        workflow = {
            "jobs": {
                "job1": {
                    "steps": [
                        {"name": "Checkout", "uses": "actions/checkout@v4"},
                        {"name": "Setup", "uses": "actions/setup-python@v5"},
                        {"name": "Run", "run": "echo hello"},
                    ]
                }
            }
        }

        results = extract_upload_artifact_paths(workflow)
        assert len(results) == 0

    def test_extract_different_artifact_versions(self):
        """测试提取不同版本的 upload-artifact"""
        workflow = {
            "jobs": {
                "job1": {
                    "steps": [
                        {
                            "name": "Upload v3",
                            "uses": "actions/upload-artifact@v3",
                            "with": {"path": ".artifacts/v3/"},
                        },
                        {
                            "name": "Upload v4",
                            "uses": "actions/upload-artifact@v4",
                            "with": {"path": ".artifacts/v4/"},
                        },
                    ]
                }
            }
        }

        results = extract_upload_artifact_paths(workflow)
        assert len(results) == 2

    def test_skip_step_without_path(self):
        """测试跳过没有 path 的 upload-artifact 步骤"""
        workflow = {
            "jobs": {
                "job1": {
                    "steps": [
                        {
                            "name": "Upload without path",
                            "uses": "actions/upload-artifact@v4",
                            "with": {"name": "no-path"},
                        }
                    ]
                }
            }
        }

        results = extract_upload_artifact_paths(workflow)
        assert len(results) == 0


# ============================================================================
# check_artifact_path_coverage Tests
# ============================================================================


class TestCheckArtifactPathCoverage:
    """check_artifact_path_coverage 函数测试"""

    def test_all_paths_covered(self):
        """测试所有路径都被覆盖"""
        upload_steps = [
            {"step_name": "Upload A", "paths": [".artifacts/verify-results.json"]},
            {"step_name": "Upload B", "paths": [".artifacts/acceptance-runs/"]},
        ]
        required_paths = [".artifacts/verify-results.json", ".artifacts/acceptance-runs/"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert covered == required_paths
        assert missing == []

    def test_missing_paths(self):
        """测试缺失路径检测"""
        upload_steps = [{"step_name": "Upload A", "paths": [".artifacts/verify-results.json"]}]
        required_paths = [".artifacts/verify-results.json", ".artifacts/acceptance-runs/"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert covered == [".artifacts/verify-results.json"]
        assert missing == [".artifacts/acceptance-runs/"]

    def test_path_prefix_matching(self):
        """测试路径前缀匹配"""
        upload_steps = [{"step_name": "Upload", "paths": [".artifacts/acceptance-runs/run1.json"]}]
        required_paths = [".artifacts/acceptance-runs/"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        # 上传的路径包含在 required 目录下，应该被视为覆盖
        assert ".artifacts/acceptance-runs/" in covered
        assert missing == []

    def test_step_name_filter(self):
        """测试 step name 过滤器"""
        upload_steps = [
            {
                "step_name": "Upload verification results",
                "paths": [".artifacts/verify-results.json"],
            },
            {"step_name": "Upload other stuff", "paths": [".artifacts/other/"]},
        ]
        required_paths = [".artifacts/verify-results.json", ".artifacts/other/"]

        # 仅检查包含 "verification" 的步骤
        covered, missing = check_artifact_path_coverage(
            upload_steps, required_paths, step_name_filter=["verification"]
        )

        assert ".artifacts/verify-results.json" in covered
        assert ".artifacts/other/" in missing

    def test_empty_upload_steps(self):
        """测试没有 upload 步骤的情况"""
        upload_steps = []
        required_paths = [".artifacts/verify-results.json"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert covered == []
        assert missing == [".artifacts/verify-results.json"]

    def test_empty_required_paths(self):
        """测试没有 required paths 的情况"""
        upload_steps = [{"step_name": "Upload", "paths": [".artifacts/something/"]}]
        required_paths = []

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert covered == []
        assert missing == []


# ============================================================================
# WorkflowContractValidator.validate_artifact_archive Tests
# ============================================================================


class TestValidateArtifactArchive:
    """validate_artifact_archive 方法集成测试"""

    def test_valid_artifact_archive(self, temp_workspace, sample_workflow_with_artifacts):
        """测试有效的 artifact archive 配置"""
        import yaml

        # 写入 workflow 文件
        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        with open(workflow_path, "w") as f:
            yaml.dump(sample_workflow_with_artifacts, f)

        # 创建合约
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "artifact_archive": {
                    "required_artifact_paths": [
                        ".artifacts/verify-results.json",
                        ".artifacts/acceptance-runs/",
                    ]
                },
            },
        }

        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 验证
        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该没有 artifact 相关错误
        artifact_errors = [e for e in result.errors if e.error_type == "missing_artifact_path"]
        assert len(artifact_errors) == 0

    def test_missing_artifact_path_error(self, temp_workspace):
        """测试缺失 artifact path 报错"""
        import yaml

        # 创建只有一个 upload step 的 workflow
        workflow = {
            "name": "CI",
            "on": ["push"],
            "jobs": {
                "test": {
                    "runs-on": "ubuntu-latest",
                    "steps": [
                        {
                            "name": "Upload something",
                            "uses": "actions/upload-artifact@v4",
                            "with": {"path": ".artifacts/something/"},
                        }
                    ],
                }
            },
        }

        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        with open(workflow_path, "w") as f:
            yaml.dump(workflow, f)

        # 创建合约，要求不存在的路径
        contract = {
            "version": "1.0.0",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "artifact_archive": {
                    "required_artifact_paths": [
                        ".artifacts/verify-results.json",
                        ".artifacts/acceptance-runs/",
                    ]
                },
            },
        }

        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 验证
        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        # 应该有 artifact 相关错误
        artifact_errors = [e for e in result.errors if e.error_type == "missing_artifact_path"]
        assert len(artifact_errors) == 2

        # 检查错误消息
        missing_keys = {e.key for e in artifact_errors}
        assert ".artifacts/verify-results.json" in missing_keys
        assert ".artifacts/acceptance-runs/" in missing_keys

    def test_no_artifact_archive_in_contract(self, temp_workspace):
        """测试合约中没有 artifact_archive 时不报错"""
        import yaml

        workflow = {
            "name": "CI",
            "on": ["push"],
            "jobs": {
                "test": {
                    "runs-on": "ubuntu-latest",
                    "steps": [{"name": "Run", "run": "echo hello"}],
                }
            },
        }

        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        with open(workflow_path, "w") as f:
            yaml.dump(workflow, f)

        # 合约没有 artifact_archive
        contract = {"version": "1.0.0", "ci": {"file": ".github/workflows/ci.yml"}}

        contract_path = temp_workspace / "contract.json"
        with open(contract_path, "w") as f:
            json.dump(contract, f)

        # 验证应该通过
        validator = WorkflowContractValidator(contract_path, temp_workspace)
        result = validator.validate()

        artifact_errors = [e for e in result.errors if e.error_type == "missing_artifact_path"]
        assert len(artifact_errors) == 0


# ============================================================================
# Integration Tests with Real Workflow Structure
# ============================================================================


class TestRealWorkflowStructure:
    """使用真实 workflow 结构的集成测试"""

    def test_multiline_path_parsing(self, temp_workspace):
        """测试多行 path 解析（模拟真实 CI workflow 格式）"""
        import yaml

        # 模拟真实的多行 path 配置
        workflow_yaml = """
name: CI

on: [push]

jobs:
  unified-standard:
    name: "[Standard] Unified Stack Integration Test"
    runs-on: ubuntu-latest
    steps:
      - name: Upload verification results
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: unified-verification-results
          path: |
            .artifacts/verify-results.json
            .artifacts/openmemory-artifact-audit.json
            .artifacts/seek-smoke/
            .artifacts/test-results/seek-pgvector.xml
          retention-days: 14
          if-no-files-found: ignore
"""

        workflow_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        with open(workflow_path, "w") as f:
            f.write(workflow_yaml)

        # 加载并解析
        with open(workflow_path, "r") as f:
            workflow_data = yaml.safe_load(f)

        results = extract_upload_artifact_paths(workflow_data)

        assert len(results) == 1
        step = results[0]
        assert step["job_id"] == "unified-standard"
        assert len(step["paths"]) == 4
        assert ".artifacts/verify-results.json" in step["paths"]
