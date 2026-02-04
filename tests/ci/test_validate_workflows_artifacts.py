#!/usr/bin/env python3
"""
Workflow Contract Validator - Artifact Archive 单元测试

覆盖功能:
1. extract_upload_artifact_paths: 从 workflow 中提取 upload-artifact 步骤
2. check_artifact_path_coverage: 验证 required paths 覆盖情况
3. validate_artifact_archive: 集成到 WorkflowContractValidator 的验证
4. normalize_artifact_path / normalize_artifact_paths: 路径标准化函数

Phase 0 说明：
- 仅测试 artifact 相关的提取和校验逻辑
- 不依赖 nightly/release workflow
"""

import json
import tempfile
from pathlib import Path

import pytest

from scripts.ci.validate_workflows import (
    WorkflowContractValidator,
    check_artifact_path_coverage,
    extract_upload_artifact_paths,
)
from scripts.ci.workflow_contract_common import (
    ArtifactPathError,
    is_valid_artifact_path,
    normalize_artifact_path,
    normalize_artifact_paths,
    normalize_glob_pattern,
    paths_are_equivalent,
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


# ============================================================================
# Glob/Directory Matching Edge Cases (边界用例测试)
# ============================================================================


class TestPathMatchingEdgeCases:
    """路径匹配边界用例测试

    测试 _path_matches 和 check_artifact_path_coverage 的边界情况：
    - 目录本身（带/不带末尾斜杠）
    - 混合多行 path
    - 大小写敏感性
    - glob 模式边界
    """

    def test_directory_itself_with_trailing_slash(self):
        """测试目录本身（上传路径不带斜杠，required 带斜杠）"""
        upload_steps = [
            {"step_name": "Upload", "paths": [".artifacts/runs"]}  # 不带斜杠
        ]
        required_paths = [".artifacts/runs/"]  # 带斜杠

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        # 应该匹配：上传的目录本身等于 required 目录
        assert ".artifacts/runs/" in covered
        assert missing == []

    def test_directory_itself_without_trailing_slash(self):
        """测试目录本身（上传路径带斜杠，required 带斜杠）"""
        upload_steps = [{"step_name": "Upload", "paths": [".artifacts/runs/"]}]
        required_paths = [".artifacts/runs/"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert ".artifacts/runs/" in covered
        assert missing == []

    def test_file_under_directory(self):
        """测试目录下的文件"""
        upload_steps = [{"step_name": "Upload", "paths": [".artifacts/runs/run_001.json"]}]
        required_paths = [".artifacts/runs/"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        # 上传的文件在 required 目录下，应该覆盖
        assert ".artifacts/runs/" in covered
        assert missing == []

    def test_nested_directory_file(self):
        """测试嵌套目录下的文件"""
        upload_steps = [{"step_name": "Upload", "paths": [".artifacts/runs/2026/01/run.json"]}]
        required_paths = [".artifacts/runs/"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        # 深层嵌套文件也应该覆盖
        assert ".artifacts/runs/" in covered
        assert missing == []

    def test_similar_directory_name_no_match(self):
        """测试相似目录名不应匹配"""
        upload_steps = [{"step_name": "Upload", "paths": [".artifacts/runs-backup/data.json"]}]
        required_paths = [".artifacts/runs/"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        # runs-backup 不是 runs/ 的子路径
        assert covered == []
        assert ".artifacts/runs/" in missing

    def test_glob_pattern_simple(self):
        """测试简单 glob 模式"""
        upload_steps = [{"step_name": "Upload", "paths": ["test-results-3.11.xml"]}]
        required_paths = ["test-results-*.xml"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert "test-results-*.xml" in covered
        assert missing == []

    def test_glob_pattern_with_directory(self):
        """测试带目录的 glob 模式"""
        upload_steps = [{"step_name": "Upload", "paths": ["artifacts/report-v2.json"]}]
        required_paths = ["artifacts/*.json"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert "artifacts/*.json" in covered
        assert missing == []

    def test_glob_pattern_no_match(self):
        """测试 glob 模式不匹配"""
        upload_steps = [
            {"step_name": "Upload", "paths": ["test-results-3.11.txt"]}  # .txt 不匹配 .xml
        ]
        required_paths = ["test-results-*.xml"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert covered == []
        assert "test-results-*.xml" in missing

    def test_case_sensitivity(self):
        """测试大小写敏感性（应区分大小写）"""
        upload_steps = [{"step_name": "Upload", "paths": ["TEST-RESULTS.xml"]}]
        required_paths = ["test-results.xml"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        # 大小写不同，不应匹配
        assert covered == []
        assert "test-results.xml" in missing

    def test_case_sensitivity_glob(self):
        """测试 glob 模式的大小写敏感性"""
        upload_steps = [{"step_name": "Upload", "paths": ["TEST-RESULTS-3.11.xml"]}]
        required_paths = ["test-results-*.xml"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        # glob 也区分大小写
        assert covered == []
        assert "test-results-*.xml" in missing

    def test_multiple_paths_partial_coverage(self):
        """测试多路径部分覆盖"""
        upload_steps = [
            {"step_name": "Upload A", "paths": ["test-results.xml"]},
            {"step_name": "Upload B", "paths": [".artifacts/logs/"]},
        ]
        required_paths = ["test-results.xml", ".artifacts/logs/", "missing-file.json"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert "test-results.xml" in covered
        assert ".artifacts/logs/" in covered
        assert missing == ["missing-file.json"]

    def test_multiline_paths_mixed(self):
        """测试混合多行路径（文件+目录+glob）"""
        upload_steps = [
            {
                "step_name": "Upload All",
                "paths": ["results.xml", ".artifacts/data/", "logs/app-2026-01-01.log"],
            }
        ]
        required_paths = [
            "results.xml",  # 精确匹配
            ".artifacts/data/",  # 目录匹配
            "logs/*.log",  # glob 模式
        ]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert "results.xml" in covered
        assert ".artifacts/data/" in covered
        assert "logs/*.log" in covered
        assert missing == []

    def test_glob_question_mark(self):
        """测试 glob 问号匹配单字符"""
        upload_steps = [{"step_name": "Upload", "paths": ["test-results-3.xml"]}]
        required_paths = ["test-results-?.xml"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert "test-results-?.xml" in covered
        assert missing == []

    def test_glob_bracket_pattern(self):
        """测试 glob 方括号模式"""
        upload_steps = [{"step_name": "Upload", "paths": ["log-a.txt"]}]
        required_paths = ["log-[abc].txt"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert "log-[abc].txt" in covered
        assert missing == []

    def test_glob_bracket_no_match(self):
        """测试 glob 方括号不匹配"""
        upload_steps = [{"step_name": "Upload", "paths": ["log-x.txt"]}]
        required_paths = ["log-[abc].txt"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert covered == []
        assert "log-[abc].txt" in missing

    def test_exact_match_with_special_chars_escaped(self):
        """测试精确匹配（无 glob 字符）"""
        upload_steps = [{"step_name": "Upload", "paths": ["report_v1.2.3.json"]}]
        required_paths = ["report_v1.2.3.json"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        # 没有 glob 字符，使用精确匹配
        assert "report_v1.2.3.json" in covered
        assert missing == []

    def test_empty_path_in_upload(self):
        """测试上传路径中的空路径被正确处理"""
        upload_steps = [{"step_name": "Upload", "paths": ["", "valid.xml", ""]}]
        required_paths = ["valid.xml"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert "valid.xml" in covered
        assert missing == []

    def test_directory_without_content(self):
        """测试 required 是精确文件路径，uploaded 是目录"""
        upload_steps = [{"step_name": "Upload", "paths": [".artifacts/"]}]
        required_paths = [".artifacts/report.json"]  # 精确文件

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        # 目录不应匹配精确文件路径
        assert covered == []
        assert ".artifacts/report.json" in missing


# ============================================================================
# normalize_artifact_path Tests
# ============================================================================


class TestNormalizeArtifactPath:
    """normalize_artifact_path 函数测试"""

    def test_basic_normalization(self):
        """测试基本标准化"""
        assert normalize_artifact_path("artifacts/results.json") == "artifacts/results.json"

    def test_strip_whitespace(self):
        """测试去除首尾空白"""
        assert normalize_artifact_path("  artifacts/file.json  ") == "artifacts/file.json"
        assert normalize_artifact_path("\tartifacts/\n") == "artifacts/"

    def test_windows_separator(self):
        """测试 Windows 分隔符转换"""
        assert (
            normalize_artifact_path("artifacts\\results\\file.json")
            == "artifacts/results/file.json"
        )
        assert normalize_artifact_path("a\\b\\c") == "a/b/c"

    def test_remove_dot_slash_prefix(self):
        """测试移除 ./ 前缀"""
        assert normalize_artifact_path("./artifacts/") == "artifacts/"
        assert normalize_artifact_path("./artifacts/file.json") == "artifacts/file.json"
        assert normalize_artifact_path("././artifacts/") == "artifacts/"

    def test_remove_duplicate_slashes(self):
        """测试移除重复斜杠"""
        assert normalize_artifact_path("artifacts//results//") == "artifacts/results/"
        assert normalize_artifact_path("a///b////c") == "a/b/c"

    def test_preserve_trailing_slash(self):
        """测试保留目录末尾斜杠"""
        assert normalize_artifact_path("artifacts/") == "artifacts/"
        assert normalize_artifact_path("artifacts/results/") == "artifacts/results/"

    def test_glob_patterns_preserved(self):
        """测试 glob 模式保持不变"""
        assert normalize_artifact_path("**/*.xml") == "**/*.xml"
        assert normalize_artifact_path("./**/*.xml") == "**/*.xml"
        assert normalize_artifact_path("test-results-*.xml") == "test-results-*.xml"
        assert normalize_artifact_path("logs/[abc]*.log") == "logs/[abc]*.log"
        assert normalize_artifact_path("file?.txt") == "file?.txt"

    def test_empty_path_error(self):
        """测试空路径报错"""
        with pytest.raises(ArtifactPathError) as exc_info:
            normalize_artifact_path("")
        assert "cannot be empty" in str(exc_info.value)

        with pytest.raises(ArtifactPathError) as exc_info:
            normalize_artifact_path("   ")
        assert "cannot be empty" in str(exc_info.value)

    def test_empty_path_allowed(self):
        """测试允许空路径"""
        assert normalize_artifact_path("", allow_empty=True) == ""
        assert normalize_artifact_path("   ", allow_empty=True) == ""

    def test_combined_normalization(self):
        """测试组合标准化场景"""
        # 同时包含多种需要标准化的情况
        assert (
            normalize_artifact_path("  ./artifacts\\\\results//file.json  ")
            == "artifacts/results/file.json"
        )
        assert normalize_artifact_path("./a\\b//c\\d/") == "a/b/c/d/"


# ============================================================================
# normalize_artifact_paths Tests
# ============================================================================


class TestNormalizeArtifactPaths:
    """normalize_artifact_paths 函数测试"""

    def test_basic_list_normalization(self):
        """测试基本列表标准化"""
        result = normalize_artifact_paths(["a/", "b/c", "d.txt"])
        assert result == ["a/", "b/c", "d.txt"]

    def test_deduplication(self):
        """测试去重功能"""
        # 同一路径多种写法应去重
        result = normalize_artifact_paths(
            [
                "./a/",
                "a/",
                ".\\a\\",
                "a//",
            ]
        )
        assert result == ["a/"]

    def test_deduplication_preserves_first(self):
        """测试去重保留首次出现（排序前）"""
        # 关闭排序，验证保留首次出现
        result = normalize_artifact_paths(
            [
                "./z/",
                "a/",
                "z/",  # 与 ./z/ 等价，应被去重
                "b/",
            ],
            sort=False,
        )
        assert result == ["z/", "a/", "b/"]

    def test_sorting(self):
        """测试排序功能"""
        result = normalize_artifact_paths(["z", "a", "m"])
        assert result == ["a", "m", "z"]

    def test_sorting_stability(self):
        """测试排序稳定性"""
        # 多次调用应返回相同结果
        paths = ["z/a", "a/b", "m/c", "a/a", "z/b"]
        result1 = normalize_artifact_paths(paths)
        result2 = normalize_artifact_paths(paths)
        result3 = normalize_artifact_paths(paths)
        assert result1 == result2 == result3
        assert result1 == ["a/a", "a/b", "m/c", "z/a", "z/b"]

    def test_no_sort(self):
        """测试禁用排序"""
        result = normalize_artifact_paths(["z", "a", "m"], sort=False)
        assert result == ["z", "a", "m"]

    def test_no_deduplicate(self):
        """测试禁用去重"""
        result = normalize_artifact_paths(["a/", "./a/"], deduplicate=False, sort=False)
        assert result == ["a/", "a/"]

    def test_empty_path_error(self):
        """测试空路径报错"""
        with pytest.raises(ArtifactPathError) as exc_info:
            normalize_artifact_paths(["valid.txt", "", "also-valid.txt"])
        assert "Invalid artifact path" in str(exc_info.value)

    def test_empty_path_allowed(self):
        """测试允许空路径"""
        result = normalize_artifact_paths(["valid.txt", "", "also-valid.txt"], allow_empty=True)
        # 空路径被跳过
        assert result == ["also-valid.txt", "valid.txt"]

    def test_empty_list(self):
        """测试空列表"""
        result = normalize_artifact_paths([])
        assert result == []

    def test_equivalent_paths_deduplication(self):
        """测试等价路径去重（同一路径多种写法）"""
        paths = [
            "artifacts/results.json",
            "./artifacts/results.json",
            "artifacts\\results.json",
            ".\\artifacts\\results.json",
            "artifacts//results.json",
        ]
        result = normalize_artifact_paths(paths)
        assert len(result) == 1
        assert result[0] == "artifacts/results.json"

    def test_mixed_paths_with_glob(self):
        """测试混合路径（包含 glob 模式）"""
        paths = [
            "./artifacts/",
            "**/*.xml",
            "./**/*.json",
            "artifacts/",  # 与 ./artifacts/ 等价
        ]
        result = normalize_artifact_paths(paths)
        assert len(result) == 3
        assert "**/*.json" in result
        assert "**/*.xml" in result
        assert "artifacts/" in result


# ============================================================================
# paths_are_equivalent Tests
# ============================================================================


class TestPathsAreEquivalent:
    """paths_are_equivalent 函数测试"""

    def test_identical_paths(self):
        """测试完全相同的路径"""
        assert paths_are_equivalent("a/b/c", "a/b/c") is True

    def test_dot_slash_equivalence(self):
        """测试 ./ 前缀等价"""
        assert paths_are_equivalent("./artifacts/", "artifacts/") is True
        assert paths_are_equivalent("././a/", "a/") is True

    def test_separator_equivalence(self):
        """测试分隔符等价"""
        assert paths_are_equivalent("a\\b", "a/b") is True
        assert paths_are_equivalent("a\\b\\c", "a/b/c") is True

    def test_duplicate_slash_equivalence(self):
        """测试重复斜杠等价"""
        assert paths_are_equivalent("a//b", "a/b") is True

    def test_combined_equivalence(self):
        """测试组合等价"""
        assert paths_are_equivalent("./a\\\\b//c", "a/b/c") is True

    def test_not_equivalent(self):
        """测试不等价的路径"""
        assert paths_are_equivalent("a/b", "a/c") is False
        assert paths_are_equivalent("a/", "a") is False  # 目录 vs 文件

    def test_empty_paths(self):
        """测试空路径"""
        assert paths_are_equivalent("", "") is True
        assert paths_are_equivalent("", "a") is False


# ============================================================================
# is_valid_artifact_path Tests
# ============================================================================


class TestIsValidArtifactPath:
    """is_valid_artifact_path 函数测试"""

    def test_valid_paths(self):
        """测试有效路径"""
        assert is_valid_artifact_path("artifacts/results.json") is True
        assert is_valid_artifact_path("./a/") is True
        assert is_valid_artifact_path("**/*.xml") is True

    def test_invalid_paths(self):
        """测试无效路径"""
        assert is_valid_artifact_path("") is False
        assert is_valid_artifact_path("   ") is False


# ============================================================================
# normalize_glob_pattern Tests
# ============================================================================


class TestNormalizeGlobPattern:
    """normalize_glob_pattern 函数测试"""

    def test_basic_glob_normalization(self):
        """测试基本 glob 模式标准化"""
        assert normalize_glob_pattern("./**/*.xml") == "**/*.xml"
        assert normalize_glob_pattern("./logs/[abc]*.log") == "logs/[abc]*.log"

    def test_preserve_glob_characters(self):
        """测试保留 glob 特殊字符"""
        assert normalize_glob_pattern("test-*.xml") == "test-*.xml"
        assert normalize_glob_pattern("file?.txt") == "file?.txt"
        assert normalize_glob_pattern("[abc].txt") == "[abc].txt"
        assert normalize_glob_pattern("**/*.json") == "**/*.json"


# ============================================================================
# Path Normalization Integration with check_artifact_path_coverage Tests
# ============================================================================


class TestNormalizationIntegrationWithCoverage:
    """测试路径标准化与 check_artifact_path_coverage 的集成"""

    def test_normalized_paths_match(self):
        """测试标准化后的路径能正确匹配"""
        # uploaded 使用 Windows 分隔符，required 使用 Unix 分隔符
        upload_steps = [{"step_name": "Upload", "paths": ["artifacts\\results.json"]}]
        required_paths = ["artifacts/results.json"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert "artifacts/results.json" in covered
        assert missing == []

    def test_dot_slash_paths_match(self):
        """测试 ./ 前缀路径能正确匹配"""
        upload_steps = [{"step_name": "Upload", "paths": ["./artifacts/"]}]
        required_paths = ["artifacts/"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert "artifacts/" in covered
        assert missing == []

    def test_duplicate_slashes_match(self):
        """测试重复斜杠路径能正确匹配"""
        upload_steps = [{"step_name": "Upload", "paths": ["artifacts//results//"]}]
        required_paths = ["artifacts/results/"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert "artifacts/results/" in covered
        assert missing == []

    def test_empty_uploaded_path_skipped(self):
        """测试空的上传路径被跳过"""
        upload_steps = [{"step_name": "Upload", "paths": ["", "valid.xml", "  "]}]
        required_paths = ["valid.xml"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert "valid.xml" in covered
        assert missing == []

    def test_multiple_equivalent_uploads_deduplicated(self):
        """测试多个等价的上传路径被正确处理"""
        upload_steps = [
            {"step_name": "Upload 1", "paths": ["./artifacts/results.json"]},
            {"step_name": "Upload 2", "paths": ["artifacts\\results.json"]},
        ]
        required_paths = ["artifacts/results.json"]

        covered, missing = check_artifact_path_coverage(upload_steps, required_paths)

        assert "artifacts/results.json" in covered
        assert missing == []
