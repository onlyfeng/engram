#!/usr/bin/env python3
"""
CI 脚本导入顺序回归测试

测试 scripts/ci/ 下的模块在不同导入顺序下的行为一致性。
确保 dual-mode import（相对导入 + 顶层导入回退）不会导致：
1. 模块重复加载
2. 顶层别名污染 sys.modules
3. __file__ 指向错误路径

测试范围：
- validate_workflows.py
- workflow_contract_drift_report.py
- check_workflow_contract_docs_sync.py

设计说明：
- 使用 clean_ci_modules fixture 确保每次测试从干净状态开始
- 测试两种导入顺序（A: validate_workflows 先；B: drift_report 先）
- 使用 importlib.reload 模拟重复导入场景
- 断言 __file__ 指向正确的脚本路径
- 断言 sys.modules 中不存在顶层别名模块
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

# ============================================================================
# 配置常量
# ============================================================================

# 测试目标模块（命名空间路径）
TARGET_MODULES = [
    "scripts.ci.validate_workflows",
    "scripts.ci.workflow_contract_drift_report",
    "scripts.ci.check_workflow_contract_docs_sync",
    "scripts.ci.workflow_contract_common",  # 共享依赖
    "scripts.ci.check_ci_test_isolation",
    "scripts.ci.check_gateway_di_boundaries",
    "scripts.ci.check_mypy_gate",
    "scripts.ci._date_utils",
]

# 禁止的顶层模块键（这些不应该出现在 sys.modules 顶层）
FORBIDDEN_TOPLEVEL_KEYS = frozenset(
    {
        "validate_workflows",
        "workflow_contract_drift_report",
        "check_workflow_contract_docs_sync",
        "workflow_contract_common",
        "check_ci_test_isolation",
        "check_gateway_di_boundaries",
        "check_mypy_gate",
        "_date_utils",
    }
)

# 项目根目录（从测试文件位置推断）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def clean_import_state() -> Generator[None, None, None]:
    """
    清理导入状态的 fixture

    确保测试前后 sys.modules 中不包含目标模块和顶层别名。
    用于隔离不同导入顺序测试之间的状态。
    """
    # Setup: 清理所有目标模块和顶层别名，确保从干净状态开始
    modules_to_clean_setup = []
    for mod_name in list(sys.modules.keys()):
        if mod_name in FORBIDDEN_TOPLEVEL_KEYS:
            modules_to_clean_setup.append(mod_name)
        elif mod_name.startswith("scripts.ci."):
            modules_to_clean_setup.append(mod_name)

    for mod_name in modules_to_clean_setup:
        if mod_name in sys.modules:
            del sys.modules[mod_name]

    # 同时清理 scripts.ci 包本身（如果存在）
    if "scripts.ci" in sys.modules:
        del sys.modules["scripts.ci"]
    if "scripts" in sys.modules:
        del sys.modules["scripts"]

    yield

    # Teardown: 清理测试期间加载的模块（恢复干净状态）
    modules_to_remove = []
    for mod_name in list(sys.modules.keys()):
        # 清理顶层别名
        if mod_name in FORBIDDEN_TOPLEVEL_KEYS:
            modules_to_remove.append(mod_name)
        # 清理所有 scripts.ci 下的模块
        elif mod_name.startswith("scripts.ci."):
            modules_to_remove.append(mod_name)

    for mod_name in modules_to_remove:
        if mod_name in sys.modules:
            del sys.modules[mod_name]

    # 同时清理 scripts.ci 包本身
    if "scripts.ci" in sys.modules:
        del sys.modules["scripts.ci"]
    if "scripts" in sys.modules:
        del sys.modules["scripts"]


def _assert_no_toplevel_aliases() -> None:
    """断言 sys.modules 中不存在禁止的顶层别名"""
    toplevel_found = []
    for mod_name in sys.modules:
        # 只检查顶层键（不包含 . 的）
        if "." not in mod_name and mod_name in FORBIDDEN_TOPLEVEL_KEYS:
            toplevel_found.append(mod_name)

    if toplevel_found:
        pytest.fail(
            f"Forbidden top-level module aliases found in sys.modules: {sorted(toplevel_found)}\n"
            f"These modules should only exist under 'scripts.ci.*' namespace."
        )


def _assert_module_file_path(module: object, expected_name: str) -> None:
    """断言模块的 __file__ 指向正确的脚本路径"""
    module_file = getattr(module, "__file__", None)
    assert module_file is not None, f"Module {expected_name} has no __file__ attribute"

    expected_path = PROJECT_ROOT / "scripts" / "ci" / f"{expected_name}.py"
    actual_path = Path(module_file).resolve()

    assert actual_path == expected_path, (
        f"Module {expected_name}.__file__ mismatch:\n"
        f"  Expected: {expected_path}\n"
        f"  Actual: {actual_path}"
    )


# ============================================================================
# 导入顺序测试：顺序 A（validate_workflows 先）
# ============================================================================


@pytest.mark.allow_toplevel_ci_module([])  # 不允许任何顶层模块
class TestImportOrderA:
    """
    导入顺序 A：validate_workflows -> drift_report -> docs_sync

    测试常规导入顺序，validate_workflows 作为基础模块先加载。
    """

    def test_import_order_a_no_exception(self, clean_import_state: None) -> None:
        """测试顺序 A 导入无异常"""
        # Step 1: 导入 validate_workflows
        from scripts.ci import validate_workflows

        assert validate_workflows is not None

        # Step 2: 导入 workflow_contract_drift_report（依赖 validate_workflows）
        from scripts.ci import workflow_contract_drift_report

        assert workflow_contract_drift_report is not None

        # Step 3: 导入 check_workflow_contract_docs_sync
        from scripts.ci import check_workflow_contract_docs_sync

        assert check_workflow_contract_docs_sync is not None

        # 断言：无顶层别名
        _assert_no_toplevel_aliases()

    def test_import_order_a_file_paths(self, clean_import_state: None) -> None:
        """测试顺序 A 导入后 __file__ 路径正确"""
        # noqa: I001 - 故意使用特定导入顺序进行测试
        from scripts.ci import check_workflow_contract_docs_sync  # noqa: I001
        from scripts.ci import validate_workflows  # noqa: I001
        from scripts.ci import workflow_contract_drift_report  # noqa: I001

        # 验证 __file__ 路径
        _assert_module_file_path(validate_workflows, "validate_workflows")
        _assert_module_file_path(workflow_contract_drift_report, "workflow_contract_drift_report")
        _assert_module_file_path(
            check_workflow_contract_docs_sync, "check_workflow_contract_docs_sync"
        )

        # 断言：无顶层别名
        _assert_no_toplevel_aliases()

    def test_import_order_a_module_identity(self, clean_import_state: None) -> None:
        """测试顺序 A 导入的模块身份一致性"""
        # noqa: I001 - 故意使用特定导入顺序进行测试
        from scripts.ci import validate_workflows  # noqa: I001
        from scripts.ci import workflow_contract_drift_report  # noqa: I001

        # 验证 drift_report 内部引用的 validate_workflows 函数来自同一模块
        # workflow_contract_drift_report 导入了 validate_workflows 的几个函数
        assert hasattr(workflow_contract_drift_report, "extract_upload_artifact_paths")

        # 验证是同一函数对象（非重复加载）
        assert (
            workflow_contract_drift_report.extract_upload_artifact_paths
            is validate_workflows.extract_upload_artifact_paths
        ), "drift_report should reference the same function object from validate_workflows"

        # 断言：无顶层别名
        _assert_no_toplevel_aliases()


# ============================================================================
# 导入顺序测试：顺序 B（drift_report 先）
# ============================================================================


@pytest.mark.allow_toplevel_ci_module([])  # 不允许任何顶层模块
class TestImportOrderB:
    """
    导入顺序 B：drift_report -> validate_workflows -> docs_sync

    测试反向导入顺序，drift_report 先加载（触发其内部对 validate_workflows 的导入）。
    """

    def test_import_order_b_no_exception(self, clean_import_state: None) -> None:
        """测试顺序 B 导入无异常"""
        # Step 1: 导入 workflow_contract_drift_report（会内部导入 validate_workflows）
        from scripts.ci import workflow_contract_drift_report

        assert workflow_contract_drift_report is not None

        # Step 2: 导入 validate_workflows（应该复用已加载的模块）
        from scripts.ci import validate_workflows

        assert validate_workflows is not None

        # Step 3: 导入 check_workflow_contract_docs_sync
        from scripts.ci import check_workflow_contract_docs_sync

        assert check_workflow_contract_docs_sync is not None

        # 断言：无顶层别名
        _assert_no_toplevel_aliases()

    def test_import_order_b_file_paths(self, clean_import_state: None) -> None:
        """测试顺序 B 导入后 __file__ 路径正确"""
        # noqa: I001 - 故意使用特定导入顺序进行测试
        from scripts.ci import workflow_contract_drift_report  # noqa: I001
        from scripts.ci import validate_workflows  # noqa: I001
        from scripts.ci import check_workflow_contract_docs_sync  # noqa: I001

        # 验证 __file__ 路径
        _assert_module_file_path(workflow_contract_drift_report, "workflow_contract_drift_report")
        _assert_module_file_path(validate_workflows, "validate_workflows")
        _assert_module_file_path(
            check_workflow_contract_docs_sync, "check_workflow_contract_docs_sync"
        )

        # 断言：无顶层别名
        _assert_no_toplevel_aliases()

    def test_import_order_b_module_identity(self, clean_import_state: None) -> None:
        """测试顺序 B 导入的模块身份一致性"""
        # noqa: I001 - 故意使用特定导入顺序进行测试
        from scripts.ci import workflow_contract_drift_report  # noqa: I001
        from scripts.ci import validate_workflows  # noqa: I001

        # 验证 drift_report 内部引用的 validate_workflows 函数来自同一模块
        assert (
            workflow_contract_drift_report.extract_upload_artifact_paths
            is validate_workflows.extract_upload_artifact_paths
        ), "drift_report should reference the same function object from validate_workflows"

        # 断言：无顶层别名
        _assert_no_toplevel_aliases()


# ============================================================================
# 重复导入（reload）测试
# ============================================================================


@pytest.mark.allow_toplevel_ci_module([])  # 不允许任何顶层模块
class TestReloadBehavior:
    """
    测试 importlib.reload 行为

    确保 reload 后模块状态正确，不会产生顶层别名。
    """

    def test_reload_validate_workflows(self, clean_import_state: None) -> None:
        """测试 reload validate_workflows 后状态正确"""
        from scripts.ci import validate_workflows

        # 记录 reload 前的函数对象
        original_format_text = validate_workflows.format_text_output

        # 执行 reload
        reloaded = importlib.reload(validate_workflows)

        # 验证 reload 返回同一模块对象
        assert reloaded is validate_workflows

        # 验证 __file__ 路径仍然正确
        _assert_module_file_path(reloaded, "validate_workflows")

        # 验证函数对象被替换（reload 的正常行为）
        assert reloaded.format_text_output is not original_format_text

        # 断言：无顶层别名
        _assert_no_toplevel_aliases()

    def test_reload_drift_report_preserves_imports(self, clean_import_state: None) -> None:
        """测试 reload drift_report 后仍正确引用 validate_workflows"""
        # noqa: I001 - 故意使用特定导入顺序进行测试
        from scripts.ci import validate_workflows  # noqa: I001, F401
        from scripts.ci import workflow_contract_drift_report  # noqa: I001

        # validate_workflows 导入用于确保 drift_report reload 时引用正确的模块
        _ = validate_workflows  # 显式使用以避免 F401

        # 执行 reload
        reloaded = importlib.reload(workflow_contract_drift_report)

        # 验证 __file__ 路径仍然正确
        _assert_module_file_path(reloaded, "workflow_contract_drift_report")

        # 验证 reload 后仍正确引用 validate_workflows 的函数
        # 注意：reload 后 drift_report 会重新执行其导入语句
        assert hasattr(reloaded, "extract_upload_artifact_paths")

        # 断言：无顶层别名
        _assert_no_toplevel_aliases()

    def test_reload_docs_sync(self, clean_import_state: None) -> None:
        """测试 reload check_workflow_contract_docs_sync 后状态正确"""
        from scripts.ci import check_workflow_contract_docs_sync

        # 执行 reload
        reloaded = importlib.reload(check_workflow_contract_docs_sync)

        # 验证 __file__ 路径仍然正确
        _assert_module_file_path(reloaded, "check_workflow_contract_docs_sync")

        # 验证关键类仍然存在
        assert hasattr(reloaded, "WorkflowContractDocsSyncChecker")

        # 断言：无顶层别名
        _assert_no_toplevel_aliases()


# ============================================================================
# sys.modules 状态测试
# ============================================================================


@pytest.mark.allow_toplevel_ci_module([])  # 不允许任何顶层模块
class TestSysModulesState:
    """
    测试 sys.modules 状态

    确保模块只通过命名空间路径注册，不产生顶层别名。
    """

    def test_modules_registered_under_namespace(self, clean_import_state: None) -> None:
        """测试模块只注册在 scripts.ci 命名空间下"""
        # noqa: I001 - 故意使用特定导入顺序进行测试
        from scripts.ci import validate_workflows  # noqa: I001
        from scripts.ci import workflow_contract_drift_report  # noqa: I001
        from scripts.ci import check_workflow_contract_docs_sync  # noqa: I001

        # 验证命名空间路径存在
        assert "scripts.ci.validate_workflows" in sys.modules
        assert "scripts.ci.workflow_contract_drift_report" in sys.modules
        assert "scripts.ci.check_workflow_contract_docs_sync" in sys.modules

        # 验证顶层别名不存在
        _assert_no_toplevel_aliases()

        # 验证 sys.modules 中的对象与导入的对象相同
        assert sys.modules["scripts.ci.validate_workflows"] is validate_workflows
        assert (
            sys.modules["scripts.ci.workflow_contract_drift_report"]
            is workflow_contract_drift_report
        )
        assert (
            sys.modules["scripts.ci.check_workflow_contract_docs_sync"]
            is check_workflow_contract_docs_sync
        )

    def test_common_module_shared(self, clean_import_state: None) -> None:
        """测试 workflow_contract_common 模块在多个导入者间共享"""
        # noqa: I001 - 故意使用特定导入顺序进行测试
        from scripts.ci import validate_workflows  # noqa: I001
        from scripts.ci import workflow_contract_drift_report  # noqa: I001
        from scripts.ci import check_workflow_contract_docs_sync  # noqa: I001

        # 验证 common 模块只加载一次
        assert "scripts.ci.workflow_contract_common" in sys.modules

        # 获取 common 模块
        common_module = sys.modules["scripts.ci.workflow_contract_common"]

        # 验证三个模块引用的 discover_workflow_keys 来自同一 common 模块
        # validate_workflows 使用 discover_workflow_keys
        assert hasattr(validate_workflows, "discover_workflow_keys")

        # drift_report 使用 discover_workflow_keys
        assert hasattr(workflow_contract_drift_report, "discover_workflow_keys")

        # docs_sync 使用 discover_workflow_keys
        assert hasattr(check_workflow_contract_docs_sync, "discover_workflow_keys")

        # 验证都是同一函数对象
        assert validate_workflows.discover_workflow_keys is common_module.discover_workflow_keys
        assert (
            workflow_contract_drift_report.discover_workflow_keys
            is common_module.discover_workflow_keys
        )
        assert (
            check_workflow_contract_docs_sync.discover_workflow_keys
            is common_module.discover_workflow_keys
        )

        # 断言：无顶层别名
        _assert_no_toplevel_aliases()


# ============================================================================
# import -> reload -> import 全流程测试
# ============================================================================


@pytest.mark.allow_toplevel_ci_module([])  # 不允许任何顶层模块
class TestImportReloadImportCycle:
    """
    测试 import -> reload -> import 循环

    验证在完整的导入-重载-再导入循环后，sys.modules 状态正确：
    1. 无顶层别名污染
    2. 模块身份保持一致
    3. __file__ 路径正确
    """

    def test_validate_workflows_import_reload_import(self, clean_import_state: None) -> None:
        """测试 validate_workflows 的 import -> reload -> import 循环"""
        # Step 1: 首次导入
        from scripts.ci import validate_workflows

        assert validate_workflows is not None
        original_id = id(validate_workflows)
        _assert_no_toplevel_aliases()

        # Step 2: reload
        reloaded = importlib.reload(validate_workflows)
        assert reloaded is validate_workflows
        _assert_no_toplevel_aliases()

        # Step 3: 再次导入（应返回相同模块）
        from scripts.ci import validate_workflows as vw2

        assert vw2 is validate_workflows
        assert id(vw2) == original_id
        _assert_no_toplevel_aliases()

        # 最终验证
        _assert_module_file_path(validate_workflows, "validate_workflows")

    def test_workflow_contract_common_import_reload_import(self, clean_import_state: None) -> None:
        """测试 workflow_contract_common 的 import -> reload -> import 循环"""
        # Step 1: 首次导入
        from scripts.ci import workflow_contract_common

        assert workflow_contract_common is not None
        original_id = id(workflow_contract_common)
        _assert_no_toplevel_aliases()

        # Step 2: reload
        reloaded = importlib.reload(workflow_contract_common)
        assert reloaded is workflow_contract_common
        _assert_no_toplevel_aliases()

        # Step 3: 再次导入
        from scripts.ci import workflow_contract_common as wcc2

        assert wcc2 is workflow_contract_common
        assert id(wcc2) == original_id
        _assert_no_toplevel_aliases()

        # 最终验证
        _assert_module_file_path(workflow_contract_common, "workflow_contract_common")

    def test_check_ci_test_isolation_import_reload_import(self, clean_import_state: None) -> None:
        """测试 check_ci_test_isolation 的 import -> reload -> import 循环"""
        # Step 1: 首次导入
        from scripts.ci import check_ci_test_isolation

        assert check_ci_test_isolation is not None
        original_id = id(check_ci_test_isolation)
        _assert_no_toplevel_aliases()

        # Step 2: reload
        reloaded = importlib.reload(check_ci_test_isolation)
        assert reloaded is check_ci_test_isolation
        _assert_no_toplevel_aliases()

        # Step 3: 再次导入
        from scripts.ci import check_ci_test_isolation as ccti2

        assert ccti2 is check_ci_test_isolation
        assert id(ccti2) == original_id
        _assert_no_toplevel_aliases()

        # 最终验证
        _assert_module_file_path(check_ci_test_isolation, "check_ci_test_isolation")

    def test_check_gateway_di_boundaries_import_reload_import(
        self, clean_import_state: None
    ) -> None:
        """测试 check_gateway_di_boundaries 的 import -> reload -> import 循环"""
        # Step 1: 首次导入
        from scripts.ci import check_gateway_di_boundaries

        assert check_gateway_di_boundaries is not None
        original_id = id(check_gateway_di_boundaries)
        _assert_no_toplevel_aliases()

        # Step 2: reload
        reloaded = importlib.reload(check_gateway_di_boundaries)
        assert reloaded is check_gateway_di_boundaries
        _assert_no_toplevel_aliases()

        # Step 3: 再次导入
        from scripts.ci import check_gateway_di_boundaries as cgdb2

        assert cgdb2 is check_gateway_di_boundaries
        assert id(cgdb2) == original_id
        _assert_no_toplevel_aliases()

        # 最终验证
        _assert_module_file_path(check_gateway_di_boundaries, "check_gateway_di_boundaries")

    def test_multiple_modules_import_reload_import(self, clean_import_state: None) -> None:
        """测试多个模块同时进行 import -> reload -> import 循环"""
        # Step 1: 首次导入所有模块
        from scripts.ci import validate_workflows  # noqa: I001
        from scripts.ci import workflow_contract_common  # noqa: I001
        from scripts.ci import check_ci_test_isolation  # noqa: I001

        original_ids = {
            "validate_workflows": id(validate_workflows),
            "workflow_contract_common": id(workflow_contract_common),
            "check_ci_test_isolation": id(check_ci_test_isolation),
        }
        _assert_no_toplevel_aliases()

        # Step 2: reload 所有模块
        importlib.reload(validate_workflows)
        importlib.reload(workflow_contract_common)
        importlib.reload(check_ci_test_isolation)
        _assert_no_toplevel_aliases()

        # Step 3: 再次导入
        from scripts.ci import validate_workflows as vw2  # noqa: I001
        from scripts.ci import workflow_contract_common as wcc2  # noqa: I001
        from scripts.ci import check_ci_test_isolation as ccti2  # noqa: I001

        # 验证模块身份
        assert id(vw2) == original_ids["validate_workflows"]
        assert id(wcc2) == original_ids["workflow_contract_common"]
        assert id(ccti2) == original_ids["check_ci_test_isolation"]

        # 最终验证无顶层别名
        _assert_no_toplevel_aliases()


# ============================================================================
# 扩展模块测试
# ============================================================================


@pytest.mark.allow_toplevel_ci_module([])  # 不允许任何顶层模块
class TestExtendedModuleImports:
    """
    测试扩展模块列表的导入行为

    确保所有 TARGET_MODULES 中的模块都能正确导入且无顶层别名。
    """

    def test_date_utils_import(self, clean_import_state: None) -> None:
        """测试 _date_utils 模块导入"""
        from scripts.ci import _date_utils

        assert _date_utils is not None
        assert "scripts.ci._date_utils" in sys.modules
        _assert_no_toplevel_aliases()

    def test_check_mypy_gate_import(self, clean_import_state: None) -> None:
        """测试 check_mypy_gate 模块导入"""
        from scripts.ci import check_mypy_gate

        assert check_mypy_gate is not None
        assert "scripts.ci.check_mypy_gate" in sys.modules
        _assert_no_toplevel_aliases()

    def test_all_target_modules_importable(self, clean_import_state: None) -> None:
        """测试所有 TARGET_MODULES 都能正确导入"""
        for module_path in TARGET_MODULES:
            module = importlib.import_module(module_path)
            assert module is not None, f"Failed to import {module_path}"
            assert module_path in sys.modules, f"{module_path} not in sys.modules"

        # 所有模块导入后，验证无顶层别名
        _assert_no_toplevel_aliases()

    def test_all_target_modules_reload_cycle(self, clean_import_state: None) -> None:
        """测试所有 TARGET_MODULES 的 reload 循环"""
        # 首次导入所有模块
        modules: dict[str, object] = {}
        for module_path in TARGET_MODULES:
            modules[module_path] = importlib.import_module(module_path)

        _assert_no_toplevel_aliases()

        # reload 所有模块
        for module_path, module in modules.items():
            reloaded = importlib.reload(module)  # type: ignore[arg-type]
            assert reloaded is module, f"reload() should return same object for {module_path}"

        _assert_no_toplevel_aliases()

        # 再次导入，验证返回相同模块
        for module_path in TARGET_MODULES:
            module2 = importlib.import_module(module_path)
            assert module2 is modules[module_path], (
                f"Re-import should return same module for {module_path}"
            )

        # 最终验证
        _assert_no_toplevel_aliases()
