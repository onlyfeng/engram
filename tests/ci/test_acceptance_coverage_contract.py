#!/usr/bin/env python3
"""
Acceptance Coverage Contract Checker 单元测试

覆盖功能:
1. Workflow YAML 解析和信息提取
2. 等价性规则匹配器（ContainsMatcher, RegexMatcher, StepNameAnchorMatcher）
3. Acceptance 脚本覆盖检查
4. JSON 输出格式验证
5. 集成测试（真实 workflow 文件）
"""

import json
import tempfile
from pathlib import Path

import pytest

from scripts.ci.check_acceptance_coverage_contract import (
    AcceptanceCoverageChecker,
    ContainsMatcher,
    CoverageCheckResult,
    CoverageError,
    RegexMatcher,
    StepNameAnchorMatcher,
    extract_workflow_info,
    format_json_output,
    format_text_output,
    load_workflow,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_workspace():
    """创建临时工作空间"""
    with tempfile.TemporaryDirectory(prefix="test_acceptance_coverage_") as tmpdir:
        workspace = Path(tmpdir)
        (workspace / ".github" / "workflows").mkdir(parents=True)
        yield workspace


@pytest.fixture
def sample_ci_workflow():
    """包含 acceptance 脚本调用的 CI workflow"""
    return """
name: CI

on: [push]

env:
  CI_VAR: "test"

jobs:
  test:
    name: Test Job
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
      - name: Run tests
        run: make test
      - name: Record acceptance run
        run: python scripts/acceptance/record_acceptance_run.py --name test
      - name: Render acceptance matrix
        run: python scripts/acceptance/render_acceptance_matrix.py --output matrix.md
"""


@pytest.fixture
def sample_nightly_workflow():
    """包含 make target 调用的 Nightly workflow"""
    return """
name: Nightly

on:
  schedule:
    - cron: '0 2 * * *'

env:
  NIGHTLY_VAR: "test"

jobs:
  verify:
    name: Verification
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - name: Run verification
        run: make verify-unified
      - name: Upload artifacts
        uses: actions/upload-artifact@v4
        with:
          name: results
          path: |
            .artifacts/verify-results.json
            .artifacts/acceptance-runs/
"""


@pytest.fixture
def ci_workflow_with_make_targets():
    """使用 make target 的 CI workflow"""
    return """
name: CI

on: [push]

jobs:
  acceptance:
    name: Acceptance
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - name: Record acceptance
        run: make acceptance-record NAME=test RESULT=PASS
      - name: Generate matrix
        run: make acceptance-matrix
"""


@pytest.fixture
def ci_workflow_without_acceptance():
    """不包含 acceptance 脚本的 CI workflow"""
    return """
name: CI

on: [push]

jobs:
  test:
    name: Test Job
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - name: Run tests
        run: make test
      - name: Lint
        run: make lint
"""


# ============================================================================
# Matcher Tests
# ============================================================================


class TestContainsMatcher:
    """ContainsMatcher 测试"""

    def test_matches_exact_string(self):
        """测试精确字符串匹配"""
        matcher = ContainsMatcher(
            name="test",
            description="Test matcher",
            pattern="record_acceptance_run.py",
        )
        assert matcher.matches("python scripts/acceptance/record_acceptance_run.py")
        assert matcher.matches("record_acceptance_run.py --name test")

    def test_no_match(self):
        """测试不匹配的情况"""
        matcher = ContainsMatcher(
            name="test",
            description="Test matcher",
            pattern="record_acceptance_run.py",
        )
        assert not matcher.matches("render_acceptance_matrix.py")
        assert not matcher.matches("some other content")

    def test_partial_match(self):
        """测试部分匹配"""
        matcher = ContainsMatcher(
            name="test",
            description="Test matcher",
            pattern="acceptance",
        )
        assert matcher.matches("acceptance-record")
        assert matcher.matches("make acceptance-matrix")


class TestRegexMatcher:
    """RegexMatcher 测试"""

    def test_matches_simple_pattern(self):
        """测试简单正则匹配"""
        matcher = RegexMatcher(
            name="test",
            description="Test matcher",
            pattern=r"record_acceptance_run\.py",
        )
        assert matcher.matches("python record_acceptance_run.py")

    def test_matches_complex_pattern(self):
        """测试复杂正则匹配"""
        matcher = RegexMatcher(
            name="test",
            description="Test matcher",
            pattern=r"make\s+acceptance-(record|matrix)",
        )
        assert matcher.matches("make acceptance-record")
        assert matcher.matches("make acceptance-matrix")
        assert not matcher.matches("make test")

    def test_case_insensitive(self):
        """测试大小写不敏感匹配"""
        import re

        matcher = RegexMatcher(
            name="test",
            description="Test matcher",
            pattern=r"acceptance",
            flags=re.IGNORECASE,
        )
        assert matcher.matches("ACCEPTANCE")
        assert matcher.matches("Acceptance")


class TestStepNameAnchorMatcher:
    """StepNameAnchorMatcher 测试"""

    def test_matches_step_name(self):
        """测试 step name 匹配"""
        matcher = StepNameAnchorMatcher(
            name="test",
            description="Test matcher",
            step_name="Record acceptance",
        )
        step = {"name": "Record acceptance run", "run": "python script.py"}
        assert matcher.matches_step(step)

    def test_matches_with_content_pattern(self):
        """测试带内容模式的匹配"""
        matcher = StepNameAnchorMatcher(
            name="test",
            description="Test matcher",
            step_name="Record",
            content_pattern=r"record_acceptance_run\.py",
        )
        step = {
            "name": "Record acceptance",
            "run": "python scripts/acceptance/record_acceptance_run.py",
        }
        assert matcher.matches_step(step)

    def test_no_match_wrong_step_name(self):
        """测试 step name 不匹配"""
        matcher = StepNameAnchorMatcher(
            name="test",
            description="Test matcher",
            step_name="Record acceptance",
        )
        step = {"name": "Checkout", "run": "echo hello"}
        assert not matcher.matches_step(step)


# ============================================================================
# Workflow Extraction Tests
# ============================================================================


class TestExtractWorkflowInfo:
    """extract_workflow_info 函数测试"""

    def test_extract_run_commands(self, sample_ci_workflow):
        """测试提取 run 命令"""
        import yaml

        workflow_data = yaml.safe_load(sample_ci_workflow)
        extraction = extract_workflow_info(workflow_data, "ci.yml")

        assert len(extraction.run_commands) == 3
        run_contents = [cmd["content"] for cmd in extraction.run_commands]
        assert any("make test" in content for content in run_contents)
        assert any("record_acceptance_run.py" in content for content in run_contents)

    def test_extract_env_vars(self, sample_ci_workflow):
        """测试提取环境变量"""
        import yaml

        workflow_data = yaml.safe_load(sample_ci_workflow)
        extraction = extract_workflow_info(workflow_data, "ci.yml")

        assert "CI_VAR" in extraction.env_vars
        assert extraction.env_vars["CI_VAR"] == "test"

    def test_extract_uses_actions(self, sample_nightly_workflow):
        """测试提取 uses actions"""
        import yaml

        workflow_data = yaml.safe_load(sample_nightly_workflow)
        extraction = extract_workflow_info(workflow_data, "nightly.yml")

        assert len(extraction.uses_actions) == 2
        actions = [action["action"] for action in extraction.uses_actions]
        assert "actions/checkout@v4" in actions
        assert "actions/upload-artifact@v4" in actions

    def test_extract_with_paths(self, sample_nightly_workflow):
        """测试提取 with.path"""
        import yaml

        workflow_data = yaml.safe_load(sample_nightly_workflow)
        extraction = extract_workflow_info(workflow_data, "nightly.yml")

        assert len(extraction.with_paths) == 1
        paths = extraction.with_paths[0]["paths"]
        assert ".artifacts/verify-results.json" in paths
        assert ".artifacts/acceptance-runs/" in paths

    def test_extract_make_calls(self, ci_workflow_with_make_targets):
        """测试提取 make 调用"""
        import yaml

        workflow_data = yaml.safe_load(ci_workflow_with_make_targets)
        extraction = extract_workflow_info(workflow_data, "ci.yml")

        targets = [call["target"] for call in extraction.make_calls]
        assert "acceptance-record" in targets
        assert "acceptance-matrix" in targets

    def test_extract_python_scripts(self, sample_ci_workflow):
        """测试提取 Python 脚本调用"""
        import yaml

        workflow_data = yaml.safe_load(sample_ci_workflow)
        extraction = extract_workflow_info(workflow_data, "ci.yml")

        scripts = [call["script"] for call in extraction.python_scripts]
        assert any("record_acceptance_run.py" in s for s in scripts)
        assert any("render_acceptance_matrix.py" in s for s in scripts)

    def test_extract_empty_workflow(self):
        """测试空 workflow"""
        extraction = extract_workflow_info({}, "empty.yml")
        assert len(extraction.run_commands) == 0
        assert len(extraction.uses_actions) == 0


# ============================================================================
# Coverage Checker Tests
# ============================================================================


class TestAcceptanceCoverageChecker:
    """AcceptanceCoverageChecker 测试"""

    def test_check_with_script_calls(self, temp_workspace, sample_ci_workflow):
        """测试包含脚本调用的 workflow"""
        # 写入 CI workflow
        ci_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        ci_path.write_text(sample_ci_workflow)

        checker = AcceptanceCoverageChecker(temp_workspace)
        result = checker.check()

        assert result.success is True
        assert result.acceptance_record_found is True
        assert result.acceptance_matrix_found is True
        assert "ci.yml" in result.checked_workflows

    def test_check_with_make_targets(self, temp_workspace, ci_workflow_with_make_targets):
        """测试包含 make target 调用的 workflow"""
        ci_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        ci_path.write_text(ci_workflow_with_make_targets)

        checker = AcceptanceCoverageChecker(temp_workspace)
        result = checker.check()

        assert result.success is True
        assert result.acceptance_record_found is True
        assert result.acceptance_matrix_found is True

    def test_check_missing_acceptance_scripts(self, temp_workspace, ci_workflow_without_acceptance):
        """测试缺少 acceptance 脚本的 workflow"""
        ci_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        ci_path.write_text(ci_workflow_without_acceptance)

        checker = AcceptanceCoverageChecker(temp_workspace)
        result = checker.check()

        assert result.success is False
        assert result.acceptance_record_found is False
        assert result.acceptance_matrix_found is False
        assert len(result.errors) == 2

        error_categories = {e.category for e in result.errors}
        assert "acceptance_record" in error_categories
        assert "acceptance_matrix" in error_categories

    def test_check_partial_coverage(self, temp_workspace):
        """测试部分覆盖的情况"""
        ci_workflow = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Record
        run: python scripts/acceptance/record_acceptance_run.py
"""
        ci_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        ci_path.write_text(ci_workflow)

        checker = AcceptanceCoverageChecker(temp_workspace)
        result = checker.check()

        assert result.success is False
        assert result.acceptance_record_found is True
        assert result.acceptance_matrix_found is False
        assert len(result.errors) == 1
        assert result.errors[0].category == "acceptance_matrix"

    def test_check_no_workflow_files(self, temp_workspace):
        """测试没有 workflow 文件的情况"""
        checker = AcceptanceCoverageChecker(temp_workspace)
        result = checker.check()

        assert result.success is False
        assert len(result.warnings) == 2  # ci.yml and nightly.yml not found

    def test_equivalence_checks_recorded(self, temp_workspace, sample_ci_workflow):
        """测试等价性检查结果被记录"""
        ci_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        ci_path.write_text(sample_ci_workflow)

        checker = AcceptanceCoverageChecker(temp_workspace)
        result = checker.check()

        assert len(result.equivalence_checks) > 0
        for check in result.equivalence_checks:
            assert "rule" in check
            assert "matched" in check


# ============================================================================
# Output Formatting Tests
# ============================================================================


class TestOutputFormatting:
    """输出格式化测试"""

    def test_text_output_format(self):
        """测试文本输出格式"""
        result = CoverageCheckResult(
            success=False,
            checked_workflows=["ci.yml"],
            acceptance_record_found=False,
            acceptance_matrix_found=True,
            errors=[
                CoverageError(
                    error_type="missing_acceptance_coverage",
                    category="acceptance_record",
                    key="record_acceptance_run.py",
                    message="Script not found",
                )
            ],
        )

        output = format_text_output(result)

        assert "FAILED" in output
        assert "ci.yml" in output
        assert "acceptance_record" in output
        assert "record_acceptance_run.py" in output

    def test_json_output_format(self):
        """测试 JSON 输出格式"""
        result = CoverageCheckResult(
            success=True,
            checked_workflows=["ci.yml", "nightly.yml"],
            acceptance_record_found=True,
            acceptance_matrix_found=True,
        )

        output = format_json_output(result)
        parsed = json.loads(output)

        assert parsed["success"] is True
        assert "ci.yml" in parsed["checked_workflows"]
        assert parsed["acceptance_record_found"] is True
        assert parsed["acceptance_matrix_found"] is True

    def test_json_output_with_errors(self):
        """测试带错误的 JSON 输出"""
        result = CoverageCheckResult(
            success=False,
            errors=[
                CoverageError(
                    error_type="missing_acceptance_coverage",
                    category="acceptance_record",
                    key="record_acceptance_run.py",
                    message="Not found",
                    expected="script call",
                    actual="(not found)",
                )
            ],
        )

        output = format_json_output(result)
        parsed = json.loads(output)

        assert parsed["success"] is False
        assert parsed["error_count"] == 1
        assert len(parsed["errors"]) == 1
        error = parsed["errors"][0]
        assert error["error_type"] == "missing_acceptance_coverage"
        assert error["category"] == "acceptance_record"


# ============================================================================
# Load Workflow Tests
# ============================================================================


class TestLoadWorkflow:
    """load_workflow 函数测试"""

    def test_load_valid_workflow(self, temp_workspace, sample_ci_workflow):
        """测试加载有效 workflow"""
        workflow_path = temp_workspace / "test.yml"
        workflow_path.write_text(sample_ci_workflow)

        data = load_workflow(workflow_path)

        assert data is not None
        assert data["name"] == "CI"
        assert "jobs" in data

    def test_load_missing_workflow(self, temp_workspace):
        """测试加载不存在的 workflow"""
        workflow_path = temp_workspace / "missing.yml"

        data = load_workflow(workflow_path)

        assert data is None

    def test_load_invalid_yaml(self, temp_workspace):
        """测试加载无效 YAML"""
        workflow_path = temp_workspace / "invalid.yml"
        workflow_path.write_text("{ invalid yaml content :")

        data = load_workflow(workflow_path)

        assert data is None


# ============================================================================
# Integration Tests
# ============================================================================


class TestRealWorkflowValidation:
    """真实 workflow 文件集成测试"""

    @pytest.fixture
    def real_workspace(self):
        """获取真实工作空间路径"""
        workspace = Path(__file__).parent.parent.parent
        return workspace

    def test_real_ci_workflow_exists(self, real_workspace):
        """测试 CI workflow 文件存在"""
        ci_path = real_workspace / ".github" / "workflows" / "ci.yml"
        assert ci_path.exists(), "CI workflow should exist"

    def test_real_workflows_extraction(self, real_workspace):
        """测试从真实 workflow 提取信息"""
        ci_path = real_workspace / ".github" / "workflows" / "ci.yml"

        if not ci_path.exists():
            pytest.skip("CI workflow not found")

        data = load_workflow(ci_path)
        assert data is not None

        extraction = extract_workflow_info(data, str(ci_path))

        # 验证提取结果合理
        assert len(extraction.run_commands) > 0
        assert len(extraction.uses_actions) > 0

        # 输出统计信息
        print("\nCI workflow extraction:")
        print(f"  Run commands: {len(extraction.run_commands)}")
        print(f"  Uses actions: {len(extraction.uses_actions)}")
        print(f"  Env vars: {len(extraction.env_vars)}")
        print(f"  Make calls: {len(extraction.make_calls)}")
        print(f"  Python scripts: {len(extraction.python_scripts)}")

    def test_real_coverage_check(self, real_workspace):
        """测试真实 workflow 的覆盖检查

        注意: 此测试会检查 CI/Nightly workflow 中是否包含 acceptance 脚本调用。
        如果项目尚未添加这些调用，测试会失败并提供详细的诊断信息。
        """
        ci_path = real_workspace / ".github" / "workflows" / "ci.yml"

        if not ci_path.exists():
            pytest.skip("CI workflow not found")

        checker = AcceptanceCoverageChecker(real_workspace, verbose=True)
        result = checker.check()

        # 输出详细结果
        print("\n" + "=" * 60)
        print("Real Workflow Coverage Check Results")
        print("=" * 60)
        print(format_text_output(result))

        # 验证基本检查完成
        assert len(result.checked_workflows) > 0, "Should check at least one workflow"

        # 输出诊断信息
        if result.ci_extraction:
            print("\nCI Workflow Make Calls:")
            for call in result.ci_extraction.make_calls:
                print(f"  - {call['target']}")

            print("\nCI Workflow Python Scripts:")
            for script in result.ci_extraction.python_scripts:
                print(f"  - {script['script']}")

        # 注意: 不强制断言 success，因为项目可能尚未添加 acceptance 脚本调用
        # 此测试主要用于诊断和验证提取逻辑正确


class TestEquivalenceRules:
    """等价性规则测试"""

    def test_script_call_equivalence(self, temp_workspace):
        """测试脚本调用等价性"""
        ci_workflow = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Record
        run: python scripts/acceptance/record_acceptance_run.py
      - name: Matrix
        run: python scripts/acceptance/render_acceptance_matrix.py
"""
        ci_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        ci_path.write_text(ci_workflow)

        checker = AcceptanceCoverageChecker(temp_workspace)
        result = checker.check()

        # 应该通过等价性检查
        assert result.success is True
        for check in result.equivalence_checks:
            if "record" in check["rule"]:
                assert check["matched"] is True
            if "matrix" in check["rule"]:
                assert check["matched"] is True

    def test_make_target_equivalence(self, temp_workspace):
        """测试 make target 等价性"""
        ci_workflow = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Record
        run: make acceptance-record
      - name: Matrix
        run: make acceptance-matrix
"""
        ci_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        ci_path.write_text(ci_workflow)

        checker = AcceptanceCoverageChecker(temp_workspace)
        result = checker.check()

        # 应该通过等价性检查
        assert result.success is True

    def test_mixed_equivalence(self, temp_workspace):
        """测试混合等价性（脚本 + make target）"""
        ci_workflow = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Record
        run: python scripts/acceptance/record_acceptance_run.py
      - name: Matrix
        run: make acceptance-matrix
"""
        ci_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        ci_path.write_text(ci_workflow)

        checker = AcceptanceCoverageChecker(temp_workspace)
        result = checker.check()

        # 应该通过等价性检查
        assert result.success is True


# ============================================================================
# Direct vs Composed Mode Tests
# ============================================================================


class TestDirectVsComposedCoverage:
    """Direct 模式（直接脚本调用）vs Composed 模式（make target）测试

    验证 check_acceptance_coverage_contract.py 对两种调用模式的正确判定：
    1. Direct 模式：直接调用 Python 脚本（如 python scripts/acceptance/record_acceptance_run.py）
    2. Composed 模式：通过 make target 调用（如 make acceptance-record）

    两种模式应被视为等价，只要覆盖了 acceptance 功能即可。
    """

    @pytest.fixture
    def temp_workspace(self):
        """创建临时工作空间"""
        with tempfile.TemporaryDirectory(prefix="test_direct_composed_") as tmpdir:
            workspace = Path(tmpdir)
            (workspace / ".github" / "workflows").mkdir(parents=True)
            yield workspace

    def test_direct_mode_only(self, temp_workspace):
        """测试仅使用 direct 模式（直接脚本调用）"""
        ci_workflow = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Record acceptance run
        run: python scripts/acceptance/record_acceptance_run.py --name test
      - name: Render acceptance matrix
        run: python scripts/acceptance/render_acceptance_matrix.py --output matrix.md
"""
        ci_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        ci_path.write_text(ci_workflow)

        checker = AcceptanceCoverageChecker(temp_workspace)
        result = checker.check()

        # 应该通过
        assert result.success is True
        assert result.acceptance_record_found is True
        assert result.acceptance_matrix_found is True

        # 验证等价性检查记录了 direct 模式
        for check in result.equivalence_checks:
            if "record" in check["rule"]:
                assert check["matched"] is True
                assert check["matched_by"] == "script_call"
            if "matrix" in check["rule"]:
                assert check["matched"] is True
                assert check["matched_by"] == "script_call"

    def test_composed_mode_only(self, temp_workspace):
        """测试仅使用 composed 模式（make target）"""
        ci_workflow = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Record acceptance
        run: make acceptance-record NAME=test RESULT=PASS
      - name: Generate matrix
        run: make acceptance-matrix
"""
        ci_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        ci_path.write_text(ci_workflow)

        checker = AcceptanceCoverageChecker(temp_workspace)
        result = checker.check()

        # 应该通过
        assert result.success is True
        assert result.acceptance_record_found is True
        assert result.acceptance_matrix_found is True

        # 验证等价性检查记录了 composed 模式
        for check in result.equivalence_checks:
            if "record" in check["rule"]:
                assert check["matched"] is True
                assert check["matched_by"] == "make_target"
            if "matrix" in check["rule"]:
                assert check["matched"] is True
                assert check["matched_by"] == "make_target"

    def test_mixed_mode_direct_record_composed_matrix(self, temp_workspace):
        """测试混合模式：direct record + composed matrix"""
        ci_workflow = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Record acceptance run
        run: python scripts/acceptance/record_acceptance_run.py --name test
      - name: Generate matrix via make
        run: make acceptance-matrix
"""
        ci_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        ci_path.write_text(ci_workflow)

        checker = AcceptanceCoverageChecker(temp_workspace)
        result = checker.check()

        # 应该通过
        assert result.success is True
        assert result.acceptance_record_found is True
        assert result.acceptance_matrix_found is True

    def test_mixed_mode_composed_record_direct_matrix(self, temp_workspace):
        """测试混合模式：composed record + direct matrix"""
        ci_workflow = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Record via make
        run: make acceptance-record
      - name: Render matrix
        run: python scripts/acceptance/render_acceptance_matrix.py
"""
        ci_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        ci_path.write_text(ci_workflow)

        checker = AcceptanceCoverageChecker(temp_workspace)
        result = checker.check()

        # 应该通过
        assert result.success is True
        assert result.acceptance_record_found is True
        assert result.acceptance_matrix_found is True

    def test_neither_mode_fails(self, temp_workspace):
        """测试既不使用 direct 也不使用 composed 模式时失败"""
        ci_workflow = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Run tests
        run: pytest tests/
      - name: Build
        run: make build
"""
        ci_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        ci_path.write_text(ci_workflow)

        checker = AcceptanceCoverageChecker(temp_workspace)
        result = checker.check()

        # 应该失败
        assert result.success is False
        assert result.acceptance_record_found is False
        assert result.acceptance_matrix_found is False
        assert len(result.errors) == 2

    def test_split_across_workflows_ci_and_nightly(self, temp_workspace):
        """测试 acceptance 功能分布在 CI 和 Nightly 两个 workflow 中"""
        # CI workflow 只有 record
        ci_workflow = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Record acceptance
        run: python scripts/acceptance/record_acceptance_run.py
"""
        ci_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        ci_path.write_text(ci_workflow)

        # Nightly workflow 只有 matrix
        nightly_workflow = """
name: Nightly
on:
  schedule:
    - cron: '0 2 * * *'
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - name: Render matrix
        run: python scripts/acceptance/render_acceptance_matrix.py
"""
        nightly_path = temp_workspace / ".github" / "workflows" / "nightly.yml"
        nightly_path.write_text(nightly_workflow)

        checker = AcceptanceCoverageChecker(temp_workspace)
        result = checker.check()

        # 应该通过（两个 workflow 合起来覆盖了 acceptance 功能）
        assert result.success is True
        assert result.acceptance_record_found is True
        assert result.acceptance_matrix_found is True

    def test_nightly_only_with_both_scripts(self, temp_workspace):
        """测试只有 nightly workflow 但包含两个脚本"""
        nightly_workflow = """
name: Nightly
on:
  schedule:
    - cron: '0 2 * * *'
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - name: Record acceptance run
        run: python scripts/acceptance/record_acceptance_run.py
      - name: Render acceptance matrix
        run: python scripts/acceptance/render_acceptance_matrix.py
"""
        nightly_path = temp_workspace / ".github" / "workflows" / "nightly.yml"
        nightly_path.write_text(nightly_workflow)

        checker = AcceptanceCoverageChecker(temp_workspace)
        result = checker.check()

        # 应该通过
        assert result.success is True
        assert "nightly.yml" in result.checked_workflows

    def test_extraction_records_both_modes(self, temp_workspace):
        """测试提取功能正确记录两种模式的调用"""
        ci_workflow = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Direct call
        run: python scripts/acceptance/record_acceptance_run.py
      - name: Composed call
        run: make acceptance-matrix
"""
        ci_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        ci_path.write_text(ci_workflow)

        checker = AcceptanceCoverageChecker(temp_workspace)
        result = checker.check()

        # 验证提取结果
        assert result.ci_extraction is not None

        # 应该记录 make 调用
        make_targets = [call["target"] for call in result.ci_extraction.make_calls]
        assert "acceptance-matrix" in make_targets

        # 应该记录 Python 脚本调用
        scripts = [call["script"] for call in result.ci_extraction.python_scripts]
        assert any("record_acceptance_run.py" in s for s in scripts)


# ============================================================================
# Nightly Workflow Coverage Tests
# ============================================================================


class TestNightlyWorkflowCoverage:
    """Nightly Workflow 覆盖测试

    验证 check_acceptance_coverage_contract.py 正确处理 nightly.yml：
    1. 正确加载和解析 nightly workflow
    2. 正确提取 nightly 中的 acceptance 脚本调用
    3. 正确合并 ci 和 nightly 的覆盖
    """

    @pytest.fixture
    def temp_workspace(self):
        """创建临时工作空间"""
        with tempfile.TemporaryDirectory(prefix="test_nightly_coverage_") as tmpdir:
            workspace = Path(tmpdir)
            (workspace / ".github" / "workflows").mkdir(parents=True)
            yield workspace

    def test_nightly_extraction(self, temp_workspace):
        """测试从 nightly workflow 提取信息"""
        nightly_workflow = """
name: Nightly
on:
  schedule:
    - cron: '0 2 * * *'
env:
  NIGHTLY_VAR: test
jobs:
  unified-stack-full:
    name: Unified Stack Full Verification
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - name: Run verification
        run: |
          python scripts/verify_unified_stack.py
          make verify-unified
      - name: Record acceptance run
        run: python scripts/acceptance/record_acceptance_run.py --name nightly
      - name: Render acceptance matrix
        run: python scripts/acceptance/render_acceptance_matrix.py
      - name: Upload artifacts
        uses: actions/upload-artifact@v4
        with:
          name: nightly-results
          path: |
            .artifacts/verify-results.json
            .artifacts/acceptance-runs/
"""
        nightly_path = temp_workspace / ".github" / "workflows" / "nightly.yml"
        nightly_path.write_text(nightly_workflow)

        checker = AcceptanceCoverageChecker(temp_workspace)
        result = checker.check()

        # 验证 nightly 被检查
        assert "nightly.yml" in result.checked_workflows

        # 验证提取结果
        assert result.nightly_extraction is not None
        assert len(result.nightly_extraction.run_commands) > 0
        assert len(result.nightly_extraction.uses_actions) >= 2  # checkout + upload-artifact
        assert "NIGHTLY_VAR" in result.nightly_extraction.env_vars

        # 验证 make 调用提取
        make_targets = [call["target"] for call in result.nightly_extraction.make_calls]
        assert "verify-unified" in make_targets

        # 验证 Python 脚本提取
        scripts = [call["script"] for call in result.nightly_extraction.python_scripts]
        assert any("record_acceptance_run.py" in s for s in scripts)
        assert any("render_acceptance_matrix.py" in s for s in scripts)

        # 验证 with.path 提取
        assert len(result.nightly_extraction.with_paths) >= 1

    def test_nightly_only_coverage(self, temp_workspace):
        """测试 nightly 独立提供完整 acceptance 覆盖"""
        nightly_workflow = """
name: Nightly
on:
  schedule:
    - cron: '0 2 * * *'
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - name: Record
        run: make acceptance-record
      - name: Matrix
        run: make acceptance-matrix
"""
        nightly_path = temp_workspace / ".github" / "workflows" / "nightly.yml"
        nightly_path.write_text(nightly_workflow)

        checker = AcceptanceCoverageChecker(temp_workspace)
        result = checker.check()

        # 应该通过
        assert result.success is True
        assert result.acceptance_record_found is True
        assert result.acceptance_matrix_found is True

    def test_ci_and_nightly_combined_coverage(self, temp_workspace):
        """测试 CI 和 Nightly 合并覆盖"""
        # CI 只有 record（通过 make）
        ci_workflow = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Record
        run: make acceptance-record
"""
        ci_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        ci_path.write_text(ci_workflow)

        # Nightly 只有 matrix（通过脚本）
        nightly_workflow = """
name: Nightly
on:
  schedule:
    - cron: '0 2 * * *'
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - name: Matrix
        run: python scripts/acceptance/render_acceptance_matrix.py
"""
        nightly_path = temp_workspace / ".github" / "workflows" / "nightly.yml"
        nightly_path.write_text(nightly_workflow)

        checker = AcceptanceCoverageChecker(temp_workspace)
        result = checker.check()

        # 应该通过
        assert result.success is True
        assert result.acceptance_record_found is True
        assert result.acceptance_matrix_found is True
        assert len(result.checked_workflows) == 2

    def test_nightly_missing_produces_warning(self, temp_workspace):
        """测试 nightly workflow 不存在时产生警告"""
        # 只创建 CI workflow
        ci_workflow = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Record
        run: python scripts/acceptance/record_acceptance_run.py
      - name: Matrix
        run: python scripts/acceptance/render_acceptance_matrix.py
"""
        ci_path = temp_workspace / ".github" / "workflows" / "ci.yml"
        ci_path.write_text(ci_workflow)

        checker = AcceptanceCoverageChecker(temp_workspace)
        result = checker.check()

        # 应该通过（CI 提供完整覆盖）
        assert result.success is True

        # 应该有 nightly not found 的警告
        nightly_warnings = [
            w
            for w in result.warnings
            if w.warning_type == "workflow_not_found" and "nightly" in w.key.lower()
        ]
        assert len(nightly_warnings) == 1

    def test_real_workflows_coverage(self):
        """集成测试：验证真实 CI 和 Nightly workflow 的 acceptance 覆盖"""
        workspace = Path(__file__).parent.parent.parent
        ci_path = workspace / ".github" / "workflows" / "ci.yml"
        nightly_path = workspace / ".github" / "workflows" / "nightly.yml"

        if not ci_path.exists():
            pytest.skip("CI workflow not found")

        checker = AcceptanceCoverageChecker(workspace, verbose=True)
        result = checker.check()

        # 输出详细结果
        print("\n" + "=" * 60)
        print("Real Workflows Acceptance Coverage Check")
        print("=" * 60)
        print(format_text_output(result))

        # 验证检查了至少一个 workflow
        assert len(result.checked_workflows) >= 1

        # 如果 nightly 存在，验证它被检查
        if nightly_path.exists():
            assert "nightly.yml" in result.checked_workflows

        # 输出提取统计
        if result.ci_extraction:
            print("\nCI Extraction Summary:")
            print(f"  Make calls: {[c['target'] for c in result.ci_extraction.make_calls]}")
            print(
                f"  Python scripts: {[c['script'] for c in result.ci_extraction.python_scripts][:5]}"
            )

        if result.nightly_extraction:
            print("\nNightly Extraction Summary:")
            print(f"  Make calls: {[c['target'] for c in result.nightly_extraction.make_calls]}")
            print(
                f"  Python scripts: {[c['script'] for c in result.nightly_extraction.python_scripts][:5]}"
            )

        # 验证真实 workflow 覆盖了 acceptance 功能
        assert result.success is True, (
            f"Real workflows should cover acceptance. "
            f"Record found: {result.acceptance_record_found}, "
            f"Matrix found: {result.acceptance_matrix_found}"
        )
