"""
tests/ci/conftest.py

确保 CI 测试模块只通过 scripts.ci.* 命名空间导入，
禁止直接作为顶层模块导入（例如 import validate_workflows）。

功能:
1. session/func 级 autouse fixture 记录进入/退出时的 sys.path 快照
   并断言只允许白名单变更
2. 在 teardown 断言 sys.modules 不包含禁止的顶层模块键
3. 若确有例外，为该测试显式标记 pytest.mark.allow_toplevel_ci_module

收口期策略:
- ALLOWED_PATH_ADDITIONS 不再包含 scripts/ci 允许项
- 对仍需例外的测试，要求以子进程方式运行而不是放行顶层导入
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

# ============================================================================
# 配置常量
# ============================================================================


def _discover_ci_modules(scripts_ci_dir: Path | None = None) -> frozenset[str]:
    """
    自动发现 scripts/ci/ 目录下的 Python 模块名。

    这些模块禁止作为顶层模块导入，必须通过 scripts.ci.xxx 命名空间导入。
    自动发现避免手工列表漂移。

    Args:
        scripts_ci_dir: scripts/ci 目录路径，None 时自动检测

    Returns:
        frozenset[str]: 模块名集合（不带 .py 后缀）
    """
    if scripts_ci_dir is None:
        # 自动检测：从 tests/ci/ 向上两级到项目根，再进入 scripts/ci
        this_file = Path(__file__).resolve()
        project_root = this_file.parent.parent.parent
        scripts_ci_dir = project_root / "scripts" / "ci"

    if not scripts_ci_dir.exists():
        return frozenset()

    modules: set[str] = set()
    for py_file in scripts_ci_dir.glob("*.py"):
        # 排除 __init__.py
        if py_file.name == "__init__.py":
            continue
        # 提取模块名（去掉 .py 后缀）
        module_name = py_file.stem
        modules.add(module_name)

    return frozenset(modules)


# scripts/ci/ 目录下的模块名（不带 .py 后缀），禁止作为顶层模块导入
# 自动发现 scripts/ci/*.py，避免手工列表漂移
FORBIDDEN_TOPLEVEL_MODULES: frozenset[str] = _discover_ci_modules()

# sys.path 变更白名单（允许添加的路径模式）
# 例如 pytest 插件或临时目录
#
# 收口期策略: 不再允许 scripts/ci 路径添加
# 对仍需例外的测试，要求以子进程方式运行（subprocess.run）
ALLOWED_PATH_ADDITIONS: frozenset[str] = frozenset(
    {
        # pytest 相关
        "_pytest",
        "pluggy",
        # 临时目录（由 pytest tmp_path 等创建）
        "/var/folders/",  # macOS 临时目录
        "/tmp/",  # Linux 临时目录
        "\\Temp\\",  # Windows 临时目录
        # 收口期: 已移除 "scripts/ci" 允许项
        # 如需测试 CI 脚本的命令行行为，应使用 subprocess.run()
    }
)


# ============================================================================
# pytest marker 注册
# ============================================================================


def pytest_configure(config: pytest.Config) -> None:
    """注册自定义 markers"""
    config.addinivalue_line(
        "markers",
        "allow_toplevel_ci_module(modules): 允许测试中使用指定的顶层 CI 模块",
    )


# ============================================================================
# sys.path 快照与断言
# ============================================================================


def _is_allowed_path_addition(path: str) -> bool:
    """检查路径是否在白名单中"""
    for allowed_pattern in ALLOWED_PATH_ADDITIONS:
        if allowed_pattern in path:
            return True
    return False


@pytest.fixture(scope="session", autouse=True)
def _session_syspath_snapshot() -> Generator[set[str], None, None]:
    """Session 级别 sys.path 快照"""
    initial_paths = set(sys.path)
    yield initial_paths
    # Session 结束时不做断言，仅记录
    # 具体断言在 function 级别做


@pytest.fixture(autouse=True)
def _func_syspath_guard(
    request: pytest.FixtureRequest,
) -> Generator[None, None, None]:
    """
    Function 级别 sys.path 保护

    记录测试开始时的 sys.path，测试结束后检查：
    1. 新增的路径是否在白名单中
    2. 若不在白名单，测试失败
    """
    initial_paths = set(sys.path)
    yield

    current_paths = set(sys.path)
    added_paths = current_paths - initial_paths

    # 检查新增路径是否都在白名单中
    disallowed_additions = [p for p in added_paths if not _is_allowed_path_addition(p)]

    if disallowed_additions:
        # 获取测试名称用于错误信息
        test_name = request.node.nodeid
        msg = (
            f"Test '{test_name}' added disallowed paths to sys.path:\n"
            f"  {disallowed_additions}\n"
            "If this is intentional, add the pattern to ALLOWED_PATH_ADDITIONS "
            "in tests/ci/conftest.py"
        )
        pytest.fail(msg)


# ============================================================================
# sys.modules 顶层模块检查
# ============================================================================


def _get_allowed_toplevel_modules(request: pytest.FixtureRequest) -> set[str]:
    """获取当前测试允许的顶层模块（通过 marker 指定）"""
    allowed: set[str] = set()
    marker = request.node.get_closest_marker("allow_toplevel_ci_module")
    if marker is not None:
        # marker 参数可以是单个模块名或模块名列表
        for arg in marker.args:
            if isinstance(arg, (list, tuple, set, frozenset)):
                allowed.update(arg)
            else:
                allowed.add(str(arg))
    return allowed


@pytest.fixture(autouse=True)
def _func_sysmodules_guard(
    request: pytest.FixtureRequest,
) -> Generator[None, None, None]:
    """
    Function 级别 sys.modules 检查

    在测试开始前记录 sys.modules 快照，测试结束后检查是否有新增的禁止顶层模块。
    强制使用 scripts.ci.* 命名空间导入。

    注意：只检查测试期间**新增**的禁止模块，避免因之前测试遗留的模块导致误报。

    例外处理：
    - 若测试标记了 @pytest.mark.allow_toplevel_ci_module("module_name")
      则该模块不会被检查
    """
    # 记录测试开始前的 sys.modules 快照（只记录顶层模块名）
    initial_toplevel_modules = {k for k in sys.modules if "." not in k}

    yield  # 测试执行

    # 获取此测试允许的例外
    allowed_exceptions = _get_allowed_toplevel_modules(request)

    # 检查测试期间新增的顶层模块
    current_toplevel_modules = {k for k in sys.modules if "." not in k}
    newly_added_modules = current_toplevel_modules - initial_toplevel_modules

    # 只检查新增的禁止模块
    forbidden_found: list[str] = []
    for module_name in newly_added_modules:
        if module_name in FORBIDDEN_TOPLEVEL_MODULES:
            if module_name not in allowed_exceptions:
                forbidden_found.append(module_name)

    if forbidden_found:
        test_name = request.node.nodeid
        msg = (
            f"Test '{test_name}' added forbidden top-level CI modules to sys.modules:\n"
            f"  {sorted(forbidden_found)}\n\n"
            "These modules should be imported via 'scripts.ci.*' namespace, e.g.:\n"
            "  from scripts.ci.validate_workflows import ...\n"
            "NOT:\n"
            "  import validate_workflows\n\n"
            "If this test legitimately needs top-level imports, mark it with:\n"
            f"  @pytest.mark.allow_toplevel_ci_module({forbidden_found!r})"
        )
        pytest.fail(msg)


# ============================================================================
# 清理 fixture（可选，用于隔离测试）
# ============================================================================


@pytest.fixture
def clean_ci_modules() -> Generator[None, None, None]:
    """
    可选 fixture：清理测试前后的 CI 模块

    用于需要完全隔离的测试场景。
    注意：这不是 autouse，需要显式请求。
    """
    # 记录测试前已加载的 scripts.ci 模块
    initial_ci_modules = {
        k for k in sys.modules if k.startswith("scripts.ci") or k in FORBIDDEN_TOPLEVEL_MODULES
    }

    yield

    # 清理测试期间新加载的 scripts.ci 模块
    modules_to_remove = [
        k
        for k in list(sys.modules.keys())
        if (k.startswith("scripts.ci") or k in FORBIDDEN_TOPLEVEL_MODULES)
        and k not in initial_ci_modules
    ]
    for mod in modules_to_remove:
        del sys.modules[mod]
