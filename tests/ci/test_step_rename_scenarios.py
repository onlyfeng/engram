#!/usr/bin/env python3
"""
Step Name 改名场景测试

专门测试 workflow contract 中 step name 改名的各种场景：
1. EXACT 精确匹配 - 无 warning/error
2. ALIAS 别名匹配 - step_name_alias_matched WARNING
3. FUZZY 模糊匹配 - step_name_changed WARNING 或 frozen_step_name_changed ERROR
4. MISSING 未匹配 - missing_step ERROR

覆盖的关键场景：
- 匹配优先级：exact > alias > fuzzy
- 冻结项与非冻结项的差异处理
- 同分冲突处理（多个 candidate 匹配时的行为）
- 错误信息中的文档锚点

文档锚点测试:
- contract.md#53-模糊匹配策略
- contract.md#54-同分冲突处理
- contract.md#56-step_name_aliases-别名映射
- maintenance.md#62-冻结-step-rename-标准流程
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from scripts.ci.validate_workflows import (
    ErrorTypes,
    WarningTypes,
    WorkflowContractValidator,
)
from scripts.ci.workflow_contract_common import (
    FUZZY_MATCH_WORD_OVERLAP_THRESHOLD,
    MatchPriority,
    find_fuzzy_match,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_workspace():
    """创建临时工作空间"""
    with tempfile.TemporaryDirectory(prefix="test_step_rename_") as tmpdir:
        workspace = Path(tmpdir)

        # 创建 .github/workflows 目录
        (workspace / ".github" / "workflows").mkdir(parents=True)

        yield workspace


def write_contract(workspace: Path, contract: dict[str, Any]) -> Path:
    """写入 contract JSON 文件"""
    contract_path = workspace / "contract.json"
    with open(contract_path, "w", encoding="utf-8") as f:
        json.dump(contract, f, indent=2)
    return contract_path


def write_workflow(workspace: Path, name: str, content: str) -> Path:
    """写入 workflow YAML 文件"""
    workflow_path = workspace / ".github" / "workflows" / f"{name}.yml"
    with open(workflow_path, "w", encoding="utf-8") as f:
        f.write(content)
    return workflow_path


# ============================================================================
# Test: Match Priority Constants
# ============================================================================


class TestMatchPriorityConstants:
    """匹配优先级常量测试"""

    def test_priority_order(self) -> None:
        """验证优先级顺序: EXACT < ALIAS < FUZZY < NONE"""
        assert MatchPriority.EXACT < MatchPriority.ALIAS
        assert MatchPriority.ALIAS < MatchPriority.FUZZY
        assert MatchPriority.FUZZY < MatchPriority.NONE

    def test_exact_is_highest_priority(self) -> None:
        """验证 EXACT 是最高优先级"""
        assert MatchPriority.EXACT == 1

    def test_none_is_lowest_priority(self) -> None:
        """验证 NONE 是最低优先级"""
        assert MatchPriority.NONE == 99


# ============================================================================
# Test: EXACT Match Scenario
# ============================================================================


class TestExactMatchScenario:
    """EXACT 精确匹配场景测试"""

    def test_exact_match_no_warning_no_error(self, temp_workspace: Path) -> None:
        """测试：精确匹配时无 warning 无 error"""
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {"allowlist": []},
            "frozen_job_names": {"allowlist": ["Test Job"]},
            "workflows": {
                "ci": {
                    "file": ".github/workflows/ci.yml",
                    "required_jobs": [
                        {
                            "id": "test",
                            "name": "Test Job",
                            "required_steps": ["Checkout repository", "Run tests"],
                        }
                    ],
                }
            },
        }
        write_contract(temp_workspace, contract)

        workflow_content = """
name: CI
on: [push]
jobs:
  test:
    name: Test Job
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
      - name: Run tests
        run: echo "Running tests"
"""
        write_workflow(temp_workspace, "ci", workflow_content)

        validator = WorkflowContractValidator(temp_workspace / "contract.json", temp_workspace)
        result = validator.validate()

        # 应该成功，无错误无警告
        assert result.success is True
        assert len(result.errors) == 0
        assert len(result.warnings) == 0

    def test_exact_match_case_sensitive(self, temp_workspace: Path) -> None:
        """测试：精确匹配是大小写敏感的"""
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {"allowlist": []},
            "frozen_job_names": {"allowlist": ["Test Job"]},
            "workflows": {
                "ci": {
                    "file": ".github/workflows/ci.yml",
                    "required_jobs": [
                        {
                            "id": "test",
                            "name": "Test Job",
                            "required_steps": ["Run tests"],
                        }
                    ],
                }
            },
        }
        write_contract(temp_workspace, contract)

        # 使用不同大小写的 step name
        workflow_content = """
name: CI
on: [push]
jobs:
  test:
    name: Test Job
    runs-on: ubuntu-latest
    steps:
      - name: run tests
        run: echo "Running tests"
"""
        write_workflow(temp_workspace, "ci", workflow_content)

        validator = WorkflowContractValidator(temp_workspace / "contract.json", temp_workspace)
        result = validator.validate()

        # 应该通过 fuzzy 匹配（case_insensitive_exact 策略）
        # 非冻结项，产生 step_name_changed WARNING
        assert result.success is True
        step_warnings = [
            w for w in result.warnings if w.warning_type == WarningTypes.STEP_NAME_CHANGED
        ]
        assert len(step_warnings) == 1
        assert step_warnings[0].old_value == "Run tests"
        assert step_warnings[0].new_value == "run tests"


# ============================================================================
# Test: ALIAS Match Scenario
# ============================================================================


class TestAliasMatchScenario:
    """ALIAS 别名匹配场景测试"""

    def test_alias_match_produces_warning(self, temp_workspace: Path) -> None:
        """测试：alias 匹配产生 step_name_alias_matched WARNING"""
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {"allowlist": []},
            "frozen_job_names": {"allowlist": ["Test Job"]},
            "step_name_aliases": {
                "Run unit tests": ["Run tests", "Execute tests"],
            },
            "workflows": {
                "ci": {
                    "file": ".github/workflows/ci.yml",
                    "required_jobs": [
                        {
                            "id": "test",
                            "name": "Test Job",
                            "required_steps": ["Run unit tests"],
                        }
                    ],
                }
            },
        }
        write_contract(temp_workspace, contract)

        # workflow 使用 alias "Run tests"
        workflow_content = """
name: CI
on: [push]
jobs:
  test:
    name: Test Job
    runs-on: ubuntu-latest
    steps:
      - name: Run tests
        run: echo "Running tests"
"""
        write_workflow(temp_workspace, "ci", workflow_content)

        validator = WorkflowContractValidator(temp_workspace / "contract.json", temp_workspace)
        result = validator.validate()

        # 应该成功（alias 匹配不阻断）
        assert result.success is True
        assert len(result.errors) == 0

        # 应该有 step_name_alias_matched 警告
        alias_warnings = [
            w for w in result.warnings if w.warning_type == WarningTypes.STEP_NAME_ALIAS_MATCHED
        ]
        assert len(alias_warnings) == 1
        assert alias_warnings[0].key == "Run unit tests"
        assert alias_warnings[0].old_value == "Run unit tests"
        assert alias_warnings[0].new_value == "Run tests"

    def test_alias_priority_over_fuzzy(self, temp_workspace: Path) -> None:
        """测试：alias 匹配优先于 fuzzy 匹配"""
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {"allowlist": []},
            "frozen_job_names": {"allowlist": ["Test Job"]},
            "step_name_aliases": {
                "Run unit tests": ["Run tests"],  # 精确 alias
            },
            "workflows": {
                "ci": {
                    "file": ".github/workflows/ci.yml",
                    "required_jobs": [
                        {
                            "id": "test",
                            "name": "Test Job",
                            "required_steps": ["Run unit tests"],
                        }
                    ],
                }
            },
        }
        write_contract(temp_workspace, contract)

        # workflow 中有 "Run tests"（alias）和 "Run unit tests (v2)"（fuzzy match）
        workflow_content = """
name: CI
on: [push]
jobs:
  test:
    name: Test Job
    runs-on: ubuntu-latest
    steps:
      - name: Run tests
        run: echo "Running tests"
      - name: Run unit tests (v2)
        run: echo "Running unit tests v2"
"""
        write_workflow(temp_workspace, "ci", workflow_content)

        validator = WorkflowContractValidator(temp_workspace / "contract.json", temp_workspace)
        result = validator.validate()

        # 应该成功
        assert result.success is True

        # 应该是 alias 匹配，不是 fuzzy 匹配
        alias_warnings = [
            w for w in result.warnings if w.warning_type == WarningTypes.STEP_NAME_ALIAS_MATCHED
        ]
        fuzzy_warnings = [
            w for w in result.warnings if w.warning_type == WarningTypes.STEP_NAME_CHANGED
        ]
        assert len(alias_warnings) == 1
        assert len(fuzzy_warnings) == 0
        assert alias_warnings[0].new_value == "Run tests"  # alias match

    def test_alias_with_frozen_step_still_warning(self, temp_workspace: Path) -> None:
        """测试：冻结项通过 alias 匹配时仍产生 WARNING（不是 ERROR）"""
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {"allowlist": ["Run unit tests"]},  # 冻结
            "frozen_job_names": {"allowlist": ["Test Job"]},
            "step_name_aliases": {
                "Run unit tests": ["Run tests"],
            },
            "workflows": {
                "ci": {
                    "file": ".github/workflows/ci.yml",
                    "required_jobs": [
                        {
                            "id": "test",
                            "name": "Test Job",
                            "required_steps": ["Run unit tests"],
                        }
                    ],
                }
            },
        }
        write_contract(temp_workspace, contract)

        workflow_content = """
name: CI
on: [push]
jobs:
  test:
    name: Test Job
    runs-on: ubuntu-latest
    steps:
      - name: Run tests
        run: echo "Running tests"
"""
        write_workflow(temp_workspace, "ci", workflow_content)

        validator = WorkflowContractValidator(temp_workspace / "contract.json", temp_workspace)
        result = validator.validate()

        # 应该成功（alias 匹配不阻断，即使是冻结项）
        assert result.success is True

        # 应该有 alias 警告，不是 frozen error
        alias_warnings = [
            w for w in result.warnings if w.warning_type == WarningTypes.STEP_NAME_ALIAS_MATCHED
        ]
        frozen_errors = [
            e for e in result.errors if e.error_type == ErrorTypes.FROZEN_STEP_NAME_CHANGED
        ]
        assert len(alias_warnings) == 1
        assert len(frozen_errors) == 0


# ============================================================================
# Test: FUZZY Match Scenario
# ============================================================================


class TestFuzzyMatchScenario:
    """FUZZY 模糊匹配场景测试"""

    def test_fuzzy_match_non_frozen_produces_warning(self, temp_workspace: Path) -> None:
        """测试：非冻结项的 fuzzy 匹配产生 WARNING"""
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {"allowlist": []},  # 不冻结
            "frozen_job_names": {"allowlist": ["Test Job"]},
            "workflows": {
                "ci": {
                    "file": ".github/workflows/ci.yml",
                    "required_jobs": [
                        {
                            "id": "test",
                            "name": "Test Job",
                            "required_steps": ["Run lint check"],
                        }
                    ],
                }
            },
        }
        write_contract(temp_workspace, contract)

        # step name 变化但可以 fuzzy 匹配
        workflow_content = """
name: CI
on: [push]
jobs:
  test:
    name: Test Job
    runs-on: ubuntu-latest
    steps:
      - name: Run lint check (v2)
        run: echo "Running lint"
"""
        write_workflow(temp_workspace, "ci", workflow_content)

        validator = WorkflowContractValidator(temp_workspace / "contract.json", temp_workspace)
        result = validator.validate()

        # 应该成功（非冻结项 fuzzy 匹配不阻断）
        assert result.success is True

        # 应该有 step_name_changed WARNING
        step_warnings = [
            w for w in result.warnings if w.warning_type == WarningTypes.STEP_NAME_CHANGED
        ]
        assert len(step_warnings) == 1
        assert step_warnings[0].old_value == "Run lint check"
        assert step_warnings[0].new_value == "Run lint check (v2)"

    def test_fuzzy_match_frozen_produces_error(self, temp_workspace: Path) -> None:
        """测试：冻结项的 fuzzy 匹配产生 ERROR"""
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {"allowlist": ["Run lint check"]},  # 冻结
            "frozen_job_names": {"allowlist": ["Test Job"]},
            "workflows": {
                "ci": {
                    "file": ".github/workflows/ci.yml",
                    "required_jobs": [
                        {
                            "id": "test",
                            "name": "Test Job",
                            "required_steps": ["Run lint check"],
                        }
                    ],
                }
            },
        }
        write_contract(temp_workspace, contract)

        # step name 变化
        workflow_content = """
name: CI
on: [push]
jobs:
  test:
    name: Test Job
    runs-on: ubuntu-latest
    steps:
      - name: Run lint check (v2)
        run: echo "Running lint"
"""
        write_workflow(temp_workspace, "ci", workflow_content)

        validator = WorkflowContractValidator(temp_workspace / "contract.json", temp_workspace)
        result = validator.validate()

        # 应该失败（冻结项 fuzzy 匹配阻断）
        assert result.success is False

        # 应该有 frozen_step_name_changed ERROR
        frozen_errors = [
            e for e in result.errors if e.error_type == ErrorTypes.FROZEN_STEP_NAME_CHANGED
        ]
        assert len(frozen_errors) == 1
        assert frozen_errors[0].expected == "Run lint check"
        assert frozen_errors[0].actual == "Run lint check (v2)"

    def test_fuzzy_match_error_contains_anchor(self, temp_workspace: Path) -> None:
        """测试：frozen_step_name_changed ERROR 消息包含文档锚点"""
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {"allowlist": ["Run lint check"]},
            "frozen_job_names": {"allowlist": ["Test Job"]},
            "workflows": {
                "ci": {
                    "file": ".github/workflows/ci.yml",
                    "required_jobs": [
                        {
                            "id": "test",
                            "name": "Test Job",
                            "required_steps": ["Run lint check"],
                        }
                    ],
                }
            },
        }
        write_contract(temp_workspace, contract)

        workflow_content = """
name: CI
on: [push]
jobs:
  test:
    name: Test Job
    runs-on: ubuntu-latest
    steps:
      - name: Run lint check (v2)
        run: echo "Running lint"
"""
        write_workflow(temp_workspace, "ci", workflow_content)

        validator = WorkflowContractValidator(temp_workspace / "contract.json", temp_workspace)
        result = validator.validate()

        # 应该失败
        assert result.success is False

        # 错误消息应包含文档锚点
        frozen_errors = [
            e for e in result.errors if e.error_type == ErrorTypes.FROZEN_STEP_NAME_CHANGED
        ]
        assert len(frozen_errors) == 1
        # 验证消息中包含 maintenance.md 锚点
        assert "maintenance.md#62" in frozen_errors[0].message
        # 验证消息中包含 contract.md 锚点
        assert "contract.md#52" in frozen_errors[0].message


# ============================================================================
# Test: MISSING (No Match) Scenario
# ============================================================================


class TestMissingStepScenario:
    """MISSING 未匹配场景测试"""

    def test_missing_step_produces_error(self, temp_workspace: Path) -> None:
        """测试：完全无法匹配时产生 missing_step ERROR"""
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {"allowlist": []},
            "frozen_job_names": {"allowlist": ["Test Job"]},
            "workflows": {
                "ci": {
                    "file": ".github/workflows/ci.yml",
                    "required_jobs": [
                        {
                            "id": "test",
                            "name": "Test Job",
                            "required_steps": ["Run special validation"],
                        }
                    ],
                }
            },
        }
        write_contract(temp_workspace, contract)

        # workflow 中没有任何匹配的 step
        workflow_content = """
name: CI
on: [push]
jobs:
  test:
    name: Test Job
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - name: Build
        run: echo "Building"
"""
        write_workflow(temp_workspace, "ci", workflow_content)

        validator = WorkflowContractValidator(temp_workspace / "contract.json", temp_workspace)
        result = validator.validate()

        # 应该失败
        assert result.success is False

        # 应该有 missing_step ERROR
        missing_errors = [e for e in result.errors if e.error_type == ErrorTypes.MISSING_STEP]
        assert len(missing_errors) == 1
        assert missing_errors[0].key == "Run special validation"

    def test_missing_step_error_contains_anchor(self, temp_workspace: Path) -> None:
        """测试：missing_step ERROR 消息包含文档锚点"""
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {"allowlist": []},
            "frozen_job_names": {"allowlist": ["Test Job"]},
            "workflows": {
                "ci": {
                    "file": ".github/workflows/ci.yml",
                    "required_jobs": [
                        {
                            "id": "test",
                            "name": "Test Job",
                            "required_steps": ["Run special validation"],
                        }
                    ],
                }
            },
        }
        write_contract(temp_workspace, contract)

        workflow_content = """
name: CI
on: [push]
jobs:
  test:
    name: Test Job
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
"""
        write_workflow(temp_workspace, "ci", workflow_content)

        validator = WorkflowContractValidator(temp_workspace / "contract.json", temp_workspace)
        result = validator.validate()

        assert result.success is False

        missing_errors = [e for e in result.errors if e.error_type == ErrorTypes.MISSING_STEP]
        assert len(missing_errors) == 1
        # 验证消息中包含 contract.md 锚点
        assert "contract.md#55" in missing_errors[0].message


# ============================================================================
# Test: Fuzzy Match Strategies and Threshold
# ============================================================================


class TestFuzzyMatchStrategies:
    """Fuzzy 匹配策略测试"""

    def test_case_insensitive_exact_strategy(self) -> None:
        """测试：case_insensitive_exact 策略"""
        # 完全相同但大小写不同
        result = find_fuzzy_match("Run Lint", ["run lint", "Run Tests"])
        assert result == "run lint"

    def test_substring_contains_strategy(self) -> None:
        """测试：substring_contains 策略"""
        # target 包含在 candidate 中
        result = find_fuzzy_match("Run lint", ["Run lint (v2)", "Build"])
        assert result == "Run lint (v2)"

        # candidate 包含在 target 中
        result = find_fuzzy_match("Run lint check", ["lint", "Build"])
        assert result == "lint"

    def test_word_overlap_strategy(self) -> None:
        """测试：word_overlap 策略"""
        # 3 个词中有 2 个重叠 (67% > 50%)
        result = find_fuzzy_match("Run unit tests", ["Execute unit tests", "Build"])
        assert result == "Execute unit tests"

    def test_word_overlap_threshold(self) -> None:
        """测试：词语重叠阈值（默认 50%）"""
        assert FUZZY_MATCH_WORD_OVERLAP_THRESHOLD == 0.5

        # 3 个词中只有 1 个重叠 (33% < 50%) - 不应匹配
        result = find_fuzzy_match("Run unit tests", ["Execute some code", "Build"])
        assert result is None  # 不匹配

        # 2 个词中有 1 个重叠 (50% == 50%) - 应该匹配
        result = find_fuzzy_match("Run tests", ["Execute tests", "Build"])
        assert result == "Execute tests"

    def test_first_match_conflict_resolution(self) -> None:
        """测试：同分冲突时返回第一个匹配（FIRST_MATCH 策略）"""
        # 两个 candidate 都可以通过 substring 匹配
        result = find_fuzzy_match(
            "lint",
            ["Run lint", "Check lint", "Build"],  # 顺序: Run lint 在前
        )
        assert result == "Run lint"  # 返回第一个匹配

        # 调换顺序
        result = find_fuzzy_match(
            "lint",
            ["Check lint", "Run lint", "Build"],  # 顺序: Check lint 在前
        )
        assert result == "Check lint"  # 返回第一个匹配


# ============================================================================
# Test: Warning/Error Message Format
# ============================================================================


class TestMessageFormat:
    """警告/错误消息格式测试"""

    def test_alias_warning_message_format(self, temp_workspace: Path) -> None:
        """测试：alias 警告消息格式"""
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {"allowlist": []},
            "frozen_job_names": {"allowlist": ["Test Job"]},
            "step_name_aliases": {
                "Run unit tests": ["Run tests"],
            },
            "workflows": {
                "ci": {
                    "file": ".github/workflows/ci.yml",
                    "required_jobs": [
                        {
                            "id": "test",
                            "name": "Test Job",
                            "required_steps": ["Run unit tests"],
                        }
                    ],
                }
            },
        }
        write_contract(temp_workspace, contract)

        workflow_content = """
name: CI
on: [push]
jobs:
  test:
    name: Test Job
    runs-on: ubuntu-latest
    steps:
      - name: Run tests
        run: echo "Running tests"
"""
        write_workflow(temp_workspace, "ci", workflow_content)

        validator = WorkflowContractValidator(temp_workspace / "contract.json", temp_workspace)
        result = validator.validate()

        alias_warnings = [
            w for w in result.warnings if w.warning_type == WarningTypes.STEP_NAME_ALIAS_MATCHED
        ]
        assert len(alias_warnings) == 1

        # 验证消息包含关键信息
        warning = alias_warnings[0]
        assert "alias" in warning.message.lower()
        assert warning.location is not None
        assert "steps" in warning.location

    def test_fuzzy_warning_includes_diff(self, temp_workspace: Path) -> None:
        """测试：fuzzy 警告消息包含 old/new 值"""
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {"allowlist": []},
            "frozen_job_names": {"allowlist": ["Test Job"]},
            "workflows": {
                "ci": {
                    "file": ".github/workflows/ci.yml",
                    "required_jobs": [
                        {
                            "id": "test",
                            "name": "Test Job",
                            "required_steps": ["Run tests"],
                        }
                    ],
                }
            },
        }
        write_contract(temp_workspace, contract)

        workflow_content = """
name: CI
on: [push]
jobs:
  test:
    name: Test Job
    runs-on: ubuntu-latest
    steps:
      - name: Run tests (v2)
        run: echo "Running tests"
"""
        write_workflow(temp_workspace, "ci", workflow_content)

        validator = WorkflowContractValidator(temp_workspace / "contract.json", temp_workspace)
        result = validator.validate()

        step_warnings = [
            w for w in result.warnings if w.warning_type == WarningTypes.STEP_NAME_CHANGED
        ]
        assert len(step_warnings) == 1

        warning = step_warnings[0]
        assert warning.old_value == "Run tests"
        assert warning.new_value == "Run tests (v2)"


# ============================================================================
# Test: Multiple Steps and Complex Scenarios
# ============================================================================


class TestComplexScenarios:
    """复杂场景测试"""

    def test_multiple_steps_different_match_types(self, temp_workspace: Path) -> None:
        """测试：多个 step 使用不同的匹配类型"""
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {"allowlist": ["Checkout repository"]},  # 仅冻结这个
            "frozen_job_names": {"allowlist": ["Test Job"]},
            "step_name_aliases": {
                "Run unit tests": ["Run tests"],
            },
            "workflows": {
                "ci": {
                    "file": ".github/workflows/ci.yml",
                    "required_jobs": [
                        {
                            "id": "test",
                            "name": "Test Job",
                            "required_steps": [
                                "Checkout repository",  # exact match
                                "Run unit tests",  # alias match
                                "Run lint check",  # fuzzy match (non-frozen)
                            ],
                        }
                    ],
                }
            },
        }
        write_contract(temp_workspace, contract)

        workflow_content = """
name: CI
on: [push]
jobs:
  test:
    name: Test Job
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
      - name: Run tests
        run: echo "Running tests"
      - name: Run lint check (v2)
        run: echo "Running lint"
"""
        write_workflow(temp_workspace, "ci", workflow_content)

        validator = WorkflowContractValidator(temp_workspace / "contract.json", temp_workspace)
        result = validator.validate()

        # 应该成功（所有匹配都成功）
        assert result.success is True
        assert len(result.errors) == 0

        # 应该有 2 个警告：1 个 alias，1 个 fuzzy
        alias_warnings = [
            w for w in result.warnings if w.warning_type == WarningTypes.STEP_NAME_ALIAS_MATCHED
        ]
        fuzzy_warnings = [
            w for w in result.warnings if w.warning_type == WarningTypes.STEP_NAME_CHANGED
        ]
        assert len(alias_warnings) == 1
        assert len(fuzzy_warnings) == 1

    def test_alias_prevents_missing_step_error(self, temp_workspace: Path) -> None:
        """测试：alias 匹配阻止 missing_step ERROR"""
        contract = {
            "version": "1.0.0",
            "frozen_step_text": {"allowlist": []},
            "frozen_job_names": {"allowlist": ["Test Job"]},
            "step_name_aliases": {
                "Run unit tests": ["Run tests"],  # 定义 alias
            },
            "workflows": {
                "ci": {
                    "file": ".github/workflows/ci.yml",
                    "required_jobs": [
                        {
                            "id": "test",
                            "name": "Test Job",
                            "required_steps": ["Run unit tests"],
                        }
                    ],
                }
            },
        }
        write_contract(temp_workspace, contract)

        # workflow 只有 alias，没有 canonical name
        workflow_content = """
name: CI
on: [push]
jobs:
  test:
    name: Test Job
    runs-on: ubuntu-latest
    steps:
      - name: Run tests
        run: echo "Running tests"
"""
        write_workflow(temp_workspace, "ci", workflow_content)

        validator = WorkflowContractValidator(temp_workspace / "contract.json", temp_workspace)
        result = validator.validate()

        # 应该成功（alias 匹配成功）
        assert result.success is True

        # 不应有 missing_step ERROR
        missing_errors = [e for e in result.errors if e.error_type == ErrorTypes.MISSING_STEP]
        assert len(missing_errors) == 0
