#!/usr/bin/env python3
"""
Workflow Make Targets 一致性检查 - 单元测试

覆盖功能:
1. extract_make_targets_from_run: 从 run 命令解析 make targets
2. parse_makefile_targets: 从 Makefile 解析 .PHONY 和规则目标
3. WorkflowMakeTargetsChecker: 集成检查
4. 边界情况: 多命令、重复 target、未知 target、contract 缺失 target
"""

import tempfile
from pathlib import Path

import pytest

from scripts.ci.check_workflow_make_targets_consistency import (
    CheckResult,
    ErrorTypes,
    WorkflowMakeTargetsChecker,
    format_result_json,
    format_result_text,
    load_contract_make_targets,
    parse_makefile_targets,
)
from scripts.ci.workflow_contract_common import (
    MakeTargetUsage,
    extract_make_targets_from_run,
    extract_make_targets_from_workflow,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_workspace():
    """创建临时工作空间"""
    with tempfile.TemporaryDirectory(prefix="test_make_targets_") as tmpdir:
        workspace = Path(tmpdir)
        (workspace / ".github" / "workflows").mkdir(parents=True)
        (workspace / "scripts" / "ci").mkdir(parents=True)
        yield workspace


@pytest.fixture
def sample_makefile_content():
    """示例 Makefile 内容"""
    return """.PHONY: install test lint format ci clean help

# Variables
PYTHON := python3

install:  ## Install dependencies
\t$(PIP) install -e .

test:  ## Run tests
\tpytest tests/

lint:  ## Run linter
\truff check src/

format:  ## Format code
\truff format src/

ci: lint format test  ## Run all CI checks
\t@echo "CI passed"

clean:  ## Clean up
\trm -rf __pycache__

help:
\t@echo "Available targets"
"""


@pytest.fixture
def sample_workflow_content():
    """示例 workflow 内容"""
    return """name: CI

on: [push]

jobs:
  test:
    name: Test
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - name: Install
        run: make install
      - name: Run tests
        run: make test

  lint:
    name: Lint
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - name: Run lint
        run: make lint
      - name: Run format check
        run: make format
"""


@pytest.fixture
def sample_contract_content():
    """示例 contract 内容"""
    return """{
  "version": "1.0.0",
  "make": {
    "targets_required": [
      "ci",
      "lint",
      "format",
      "test",
      "clean"
    ]
  }
}
"""


# ============================================================================
# extract_make_targets_from_run Tests
# ============================================================================


class TestExtractMakeTargetsFromRun:
    """extract_make_targets_from_run 函数测试"""

    def test_simple_make_target(self):
        """测试简单 make target"""
        targets = extract_make_targets_from_run("make test")
        assert len(targets) == 1
        assert targets[0][0] == "test"

    def test_multiple_targets(self):
        """测试多个 targets"""
        targets = extract_make_targets_from_run("make lint format test")
        assert len(targets) == 3
        target_names = [t[0] for t in targets]
        assert "lint" in target_names
        assert "format" in target_names
        assert "test" in target_names

    def test_make_with_directory(self):
        """测试 make -C dir"""
        targets = extract_make_targets_from_run("make -C subdir test")
        assert len(targets) == 1
        assert targets[0][0] == "test"

    def test_make_with_variable_assignment(self):
        """测试 make VAR=value target"""
        targets = extract_make_targets_from_run("make N=13 iteration-init")
        assert len(targets) == 1
        assert targets[0][0] == "iteration-init"

    def test_chained_commands(self):
        """测试链式命令"""
        targets = extract_make_targets_from_run("echo hello && make test && make lint")
        assert len(targets) == 2
        target_names = [t[0] for t in targets]
        assert "test" in target_names
        assert "lint" in target_names

    def test_semicolon_separated(self):
        """测试分号分隔的命令"""
        targets = extract_make_targets_from_run("make test ; make lint")
        assert len(targets) == 2

    def test_multiline_run(self):
        """测试多行 run"""
        run_content = """echo "Starting..."
make install
make test
echo "Done"
"""
        targets = extract_make_targets_from_run(run_content)
        assert len(targets) == 2
        target_names = [t[0] for t in targets]
        assert "install" in target_names
        assert "test" in target_names

    def test_make_variable_target(self):
        """测试变量作为 target"""
        targets = extract_make_targets_from_run("make ${{ env.TARGET }}")
        # 变量 target 应被识别但标记为含变量
        assert len(targets) == 0  # 变量 target 不提取

    def test_dollar_make(self):
        """测试 $(MAKE) 调用"""
        targets = extract_make_targets_from_run("$(MAKE) test")
        assert len(targets) == 1
        assert targets[0][0] == "test"

    def test_make_with_options(self):
        """测试带选项的 make"""
        targets = extract_make_targets_from_run("make -j4 -k test")
        assert len(targets) == 1
        assert targets[0][0] == "test"

    def test_empty_run(self):
        """测试空 run"""
        targets = extract_make_targets_from_run("")
        assert len(targets) == 0

    def test_no_make_command(self):
        """测试没有 make 的命令"""
        targets = extract_make_targets_from_run("echo hello && python test.py")
        assert len(targets) == 0

    def test_comment_in_run(self):
        """测试包含注释的 run"""
        targets = extract_make_targets_from_run("make test # run tests")
        assert len(targets) == 1
        assert targets[0][0] == "test"

    def test_hyphenated_target(self):
        """测试带连字符的 target"""
        targets = extract_make_targets_from_run("make check-env-consistency")
        assert len(targets) == 1
        assert targets[0][0] == "check-env-consistency"

    def test_underscored_target(self):
        """测试带下划线的 target"""
        targets = extract_make_targets_from_run("make install_dev")
        assert len(targets) == 1
        assert targets[0][0] == "install_dev"


# ============================================================================
# parse_makefile_targets Tests
# ============================================================================


class TestParseMakefileTargets:
    """parse_makefile_targets 函数测试"""

    def test_phony_targets(self, temp_workspace, sample_makefile_content):
        """测试 .PHONY 目标解析"""
        makefile = temp_workspace / "Makefile"
        makefile.write_text(sample_makefile_content)

        targets = parse_makefile_targets(makefile)

        assert "install" in targets
        assert "test" in targets
        assert "lint" in targets
        assert "format" in targets
        assert "ci" in targets
        assert "clean" in targets
        assert "help" in targets

    def test_rule_targets(self, temp_workspace):
        """测试规则目标解析"""
        makefile_content = """
target1:
\techo "target1"

target2: target1
\techo "target2"

target-with-hyphen: target2
\techo "hyphen"
"""
        makefile = temp_workspace / "Makefile"
        makefile.write_text(makefile_content)

        targets = parse_makefile_targets(makefile)

        assert "target1" in targets
        assert "target2" in targets
        assert "target-with-hyphen" in targets

    def test_multiline_phony(self, temp_workspace):
        """测试多行 .PHONY"""
        makefile_content = """.PHONY: target1 target2 \\
    target3 target4

target1:
\techo "1"
"""
        makefile = temp_workspace / "Makefile"
        makefile.write_text(makefile_content)

        targets = parse_makefile_targets(makefile)

        assert "target1" in targets
        assert "target2" in targets

    def test_nonexistent_makefile(self, temp_workspace):
        """测试不存在的 Makefile"""
        makefile = temp_workspace / "NonExistent"
        targets = parse_makefile_targets(makefile)
        assert len(targets) == 0

    def test_excludes_special_targets(self, temp_workspace):
        """测试排除特殊目标"""
        makefile_content = """
.DEFAULT_GOAL := help
.PHONY: test

test:
\techo "test"
"""
        makefile = temp_workspace / "Makefile"
        makefile.write_text(makefile_content)

        targets = parse_makefile_targets(makefile)

        assert "test" in targets
        assert ".DEFAULT_GOAL" not in targets


# ============================================================================
# extract_make_targets_from_workflow Tests
# ============================================================================


class TestExtractMakeTargetsFromWorkflow:
    """extract_make_targets_from_workflow 函数测试"""

    def test_extract_from_workflow(self, temp_workspace, sample_workflow_content):
        """测试从 workflow 提取 make targets"""
        workflow_file = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_file.write_text(sample_workflow_content)

        usages = extract_make_targets_from_workflow(workflow_file)

        assert len(usages) == 4
        target_names = [u.target for u in usages]
        assert "install" in target_names
        assert "test" in target_names
        assert "lint" in target_names
        assert "format" in target_names

    def test_usage_metadata(self, temp_workspace, sample_workflow_content):
        """测试使用位置元数据"""
        workflow_file = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_file.write_text(sample_workflow_content)

        usages = extract_make_targets_from_workflow(workflow_file)

        # 检查第一个 usage
        install_usage = next(u for u in usages if u.target == "install")
        assert install_usage.workflow_file == "ci.yml"
        assert install_usage.job_id == "test"
        assert install_usage.step_name == "Install"

    def test_extract_ci_nightly_make_patterns(self, temp_workspace):
        """覆盖 ci/nightly 典型 make 调用形式"""
        workflow_content = """name: CI
on: [push]
jobs:
  ci:
    runs-on: ubuntu-latest
    steps:
      - name: Contract checks
        run: |
          echo "start"
          make check-iteration-fixtures-freshness
          make validate-workflows-strict
          make check-workflow-contract-docs-sync
      - name: Multi targets
        run: make lint format test
  nightly:
    runs-on: ubuntu-latest
    steps:
      - name: Run make verify-unified (full mode)
        run: |
          make verify-unified
          make iteration-audit
      - name: Mixed forms
        run: |
          make -C scripts ci
          make VAR=1 lint
          make ${{ env.TARGET }}
          echo "done" && $(MAKE) test; make format
"""
        workflow_file = temp_workspace / ".github" / "workflows" / "ci.yml"
        workflow_file.write_text(workflow_content)

        usages = extract_make_targets_from_workflow(workflow_file)

        target_names = {u.target for u in usages}
        assert "check-iteration-fixtures-freshness" in target_names
        assert "validate-workflows-strict" in target_names
        assert "check-workflow-contract-docs-sync" in target_names
        assert "verify-unified" in target_names
        assert "iteration-audit" in target_names
        assert "ci" in target_names
        assert "lint" in target_names
        assert "format" in target_names
        assert "test" in target_names
        assert "TARGET" not in target_names

    def test_nonexistent_workflow(self, temp_workspace):
        """测试不存在的 workflow"""
        workflow_file = temp_workspace / "nonexistent.yml"
        usages = extract_make_targets_from_workflow(workflow_file)
        assert len(usages) == 0

    def test_invalid_yaml(self, temp_workspace):
        """测试无效 YAML"""
        workflow_file = temp_workspace / ".github" / "workflows" / "invalid.yml"
        workflow_file.write_text("invalid: [yaml: content")

        usages = extract_make_targets_from_workflow(workflow_file)
        assert len(usages) == 0


# ============================================================================
# load_contract_make_targets Tests
# ============================================================================


class TestLoadContractMakeTargets:
    """load_contract_make_targets 函数测试"""

    def test_load_targets(self, temp_workspace, sample_contract_content):
        """测试加载 contract targets"""
        contract_file = temp_workspace / "scripts" / "ci" / "contract.json"
        contract_file.write_text(sample_contract_content)

        targets = load_contract_make_targets(contract_file)

        assert len(targets) == 5
        assert "ci" in targets
        assert "lint" in targets
        assert "format" in targets
        assert "test" in targets
        assert "clean" in targets

    def test_nonexistent_contract(self, temp_workspace):
        """测试不存在的 contract"""
        contract_file = temp_workspace / "nonexistent.json"
        targets = load_contract_make_targets(contract_file)
        assert len(targets) == 0

    def test_invalid_json(self, temp_workspace):
        """测试无效 JSON"""
        contract_file = temp_workspace / "invalid.json"
        contract_file.write_text("invalid json {")
        targets = load_contract_make_targets(contract_file)
        assert len(targets) == 0

    def test_no_make_section(self, temp_workspace):
        """测试没有 make section"""
        contract_file = temp_workspace / "contract.json"
        contract_file.write_text('{"version": "1.0.0"}')
        targets = load_contract_make_targets(contract_file)
        assert len(targets) == 0


# ============================================================================
# WorkflowMakeTargetsChecker Integration Tests
# ============================================================================


class TestWorkflowMakeTargetsChecker:
    """WorkflowMakeTargetsChecker 集成测试"""

    def test_all_checks_pass(
        self,
        temp_workspace,
        sample_makefile_content,
        sample_workflow_content,
        sample_contract_content,
    ):
        """测试所有检查通过"""
        # Setup
        (temp_workspace / "Makefile").write_text(sample_makefile_content)
        (temp_workspace / ".github" / "workflows" / "ci.yml").write_text(sample_workflow_content)
        (temp_workspace / "scripts" / "ci" / "workflow_contract.v2.json").write_text(
            sample_contract_content
        )

        checker = WorkflowMakeTargetsChecker(workspace=temp_workspace)
        result = checker.check()

        assert result.passed
        assert len(result.errors) == 0

    def test_unknown_make_target(self, temp_workspace, sample_makefile_content):
        """测试未知 make target 报错"""
        # Makefile 没有 unknown-target
        (temp_workspace / "Makefile").write_text(sample_makefile_content)

        # Workflow 使用 unknown-target
        workflow_content = """name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Unknown
        run: make unknown-target
"""
        (temp_workspace / ".github" / "workflows" / "ci.yml").write_text(workflow_content)
        (temp_workspace / "scripts" / "ci" / "workflow_contract.v2.json").write_text(
            '{"version": "1.0.0"}'
        )

        checker = WorkflowMakeTargetsChecker(workspace=temp_workspace)
        result = checker.check()

        assert not result.passed
        assert len(result.errors) == 1
        assert result.errors[0].error_type == ErrorTypes.UNKNOWN_MAKE_TARGET
        assert result.errors[0].target == "unknown-target"

    def test_contract_target_not_in_makefile(self, temp_workspace, sample_makefile_content):
        """测试 contract target 不在 Makefile 中"""
        (temp_workspace / "Makefile").write_text(sample_makefile_content)
        (temp_workspace / ".github" / "workflows" / "ci.yml").write_text(
            "name: CI\non: [push]\njobs: {}"
        )

        # Contract 要求一个不存在的 target
        contract_content = """{
  "version": "1.0.0",
  "make": {
    "targets_required": ["nonexistent-target"]
  }
}
"""
        (temp_workspace / "scripts" / "ci" / "workflow_contract.v2.json").write_text(
            contract_content
        )

        checker = WorkflowMakeTargetsChecker(workspace=temp_workspace)
        result = checker.check()

        assert not result.passed
        assert len(result.errors) == 1
        assert result.errors[0].error_type == ErrorTypes.CONTRACT_TARGET_NOT_IN_MAKEFILE
        assert result.errors[0].target == "nonexistent-target"

    def test_workflow_target_not_in_contract_warning(
        self, temp_workspace, sample_makefile_content, sample_workflow_content
    ):
        """测试 workflow target 不在 contract 中产生警告"""
        (temp_workspace / "Makefile").write_text(sample_makefile_content)
        (temp_workspace / ".github" / "workflows" / "ci.yml").write_text(sample_workflow_content)

        # Contract 只要求 ci，不包含 install, test, lint, format
        contract_content = """{
  "version": "1.0.0",
  "make": {
    "targets_required": ["ci"]
  }
}
"""
        (temp_workspace / "scripts" / "ci" / "workflow_contract.v2.json").write_text(
            contract_content
        )

        checker = WorkflowMakeTargetsChecker(workspace=temp_workspace)
        result = checker.check()

        assert result.passed  # 警告不阻断
        # 应该有警告：install, test, lint, format 不在 contract 中
        warning_targets = {w.target for w in result.warnings}
        assert "install" in warning_targets
        assert "test" in warning_targets
        assert "lint" in warning_targets
        assert "format" in warning_targets

    def test_duplicate_targets_deduplicated(self, temp_workspace, sample_makefile_content):
        """测试重复 targets 去重"""
        (temp_workspace / "Makefile").write_text(sample_makefile_content)

        # Workflow 多次使用同一个 target
        workflow_content = """name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Test 1
        run: make test
      - name: Test 2
        run: make test
      - name: Test 3
        run: make test && make test
"""
        (temp_workspace / ".github" / "workflows" / "ci.yml").write_text(workflow_content)
        (temp_workspace / "scripts" / "ci" / "workflow_contract.v2.json").write_text(
            '{"version": "1.0.0"}'
        )

        checker = WorkflowMakeTargetsChecker(workspace=temp_workspace)
        result = checker.check()

        # 应该只报告一次警告（target 不在 contract 中）
        test_warnings = [w for w in result.warnings if w.target == "test"]
        assert len(test_warnings) == 1

    def test_multiple_workflows(self, temp_workspace, sample_makefile_content):
        """测试多个 workflow 文件"""
        (temp_workspace / "Makefile").write_text(sample_makefile_content)

        # ci.yml
        (temp_workspace / ".github" / "workflows" / "ci.yml").write_text(
            """name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: make test
"""
        )

        # nightly.yml
        (temp_workspace / ".github" / "workflows" / "nightly.yml").write_text(
            """name: Nightly
on:
  schedule:
    - cron: '0 0 * * *'
jobs:
  full:
    runs-on: ubuntu-latest
    steps:
      - run: make ci
"""
        )

        (temp_workspace / "scripts" / "ci" / "workflow_contract.v2.json").write_text(
            '{"version": "1.0.0"}'
        )

        checker = WorkflowMakeTargetsChecker(workspace=temp_workspace)
        result = checker.check()

        # 应该从两个 workflow 提取 targets
        workflow_files = {u.workflow_file for u in result.workflow_targets}
        assert "ci.yml" in workflow_files
        assert "nightly.yml" in workflow_files


# ============================================================================
# Output Formatting Tests
# ============================================================================


class TestOutputFormatting:
    """输出格式化测试"""

    def test_format_result_text_passed(self):
        """测试通过时的文本输出"""
        result = CheckResult()
        result.makefile_targets = {"test", "lint", "ci"}
        result.contract_targets = ["test", "lint"]

        output = format_result_text(result)

        assert "[OK] All checks passed" in output
        assert "Makefile targets: 3" in output

    def test_format_result_text_failed(self):
        """测试失败时的文本输出"""
        from scripts.ci.check_workflow_make_targets_consistency import CheckError

        result = CheckResult()
        result.errors.append(
            CheckError(
                error_type=ErrorTypes.UNKNOWN_MAKE_TARGET,
                message="Unknown target 'bad'",
                target="bad",
            )
        )

        output = format_result_text(result)

        assert "[FAILED]" in output
        assert "unknown_make_target" in output

    def test_format_result_json(self):
        """测试 JSON 输出"""
        import json

        result = CheckResult()
        result.makefile_targets = {"test", "lint"}
        result.contract_targets = ["test"]
        result.workflow_targets = [
            MakeTargetUsage(
                target="test",
                workflow_file="ci.yml",
                job_id="test",
                step_name="Run tests",
                line_content="make test",
            )
        ]

        output = format_result_json(result)
        data = json.loads(output)

        assert data["passed"] is True
        assert data["summary"]["makefile_targets_count"] == 2
        assert data["summary"]["contract_targets_count"] == 1
        assert len(data["workflow_usages"]) == 1


# ============================================================================
# Edge Cases
# ============================================================================


class TestEdgeCases:
    """边界情况测试"""

    def test_empty_workspace(self, temp_workspace):
        """测试空工作空间"""
        checker = WorkflowMakeTargetsChecker(workspace=temp_workspace)
        result = checker.check()

        # 应该通过（没有东西可检查）
        assert result.passed
        assert len(result.makefile_targets) == 0
        assert len(result.workflow_targets) == 0

    def test_complex_make_command(self, temp_workspace):
        """测试复杂 make 命令"""
        makefile_content = """.PHONY: test ci

test:
\techo "test"

ci:
\techo "ci"
"""
        (temp_workspace / "Makefile").write_text(makefile_content)

        workflow_content = """name: CI
on: [push]
jobs:
  complex:
    runs-on: ubuntu-latest
    steps:
      - name: Complex
        run: |
          echo "Starting"
          make -C . -j4 test ci
          echo "Done"
"""
        (temp_workspace / ".github" / "workflows" / "ci.yml").write_text(workflow_content)
        (temp_workspace / "scripts" / "ci" / "workflow_contract.v2.json").write_text(
            '{"make": {"targets_required": ["test", "ci"]}}'
        )

        checker = WorkflowMakeTargetsChecker(workspace=temp_workspace)
        result = checker.check()

        assert result.passed
        target_names = [u.target for u in result.workflow_targets]
        assert "test" in target_names
        assert "ci" in target_names

    def test_workflow_without_make(self, temp_workspace, sample_makefile_content):
        """测试没有 make 命令的 workflow"""
        (temp_workspace / "Makefile").write_text(sample_makefile_content)

        workflow_content = """name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - name: Run
        run: echo "Hello"
"""
        (temp_workspace / ".github" / "workflows" / "ci.yml").write_text(workflow_content)
        (temp_workspace / "scripts" / "ci" / "workflow_contract.v2.json").write_text(
            '{"version": "1.0.0"}'
        )

        checker = WorkflowMakeTargetsChecker(workspace=temp_workspace)
        result = checker.check()

        assert result.passed
        assert len(result.workflow_targets) == 0
