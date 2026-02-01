"""
Gateway Import Surface 门禁测试

测试 scripts/ci/check_gateway_import_surface.py 的功能，确保：
1. 正例：实际的 src/engram/gateway/__init__.py 通过检查
2. 负例：包含 eager-import 的代码会被正确识别

与 CI job 对应关系：
- 本测试对应 CI workflow 中的 gateway-import-surface-check job
- 门禁脚本路径: scripts/ci/check_gateway_import_surface.py
- 被检查文件: src/engram/gateway/__init__.py

运行方式：
    pytest tests/ci/test_gateway_import_surface_gate.py -v
    # 或作为 tests/ci 整体运行
    pytest tests/ci -v
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

# 将 scripts/ci 加入 path 以便导入
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))

from scripts.ci.check_gateway_import_surface import (  # noqa: E402
    GATEWAY_INIT_PATH,
    LAZY_SUBMODULES,
    check_gateway_import_surface,
)

# ============================================================================
# 辅助函数
# ============================================================================


def create_temp_file(content: str) -> Path:
    """创建临时 Python 文件并返回路径"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(content)
        return Path(f.name)


def get_project_root() -> Path:
    """获取项目根目录"""
    return Path(__file__).parent.parent.parent.resolve()


# ============================================================================
# 正例测试：验证实际的 Gateway __init__.py 通过检查
# ============================================================================


class TestGatewayInitPassesCheck:
    """验证实际的 src/engram/gateway/__init__.py 通过检查"""

    def test_real_gateway_init_passes(self) -> None:
        """实际的 Gateway __init__.py 应通过检查（无 eager-import 违规）"""
        project_root = get_project_root()
        file_path = project_root / GATEWAY_INIT_PATH

        assert file_path.exists(), f"Gateway __init__.py 不存在: {file_path}"

        result = check_gateway_import_surface(file_path)

        assert not result.has_violations(), (
            f"Gateway __init__.py 不应有 eager-import 违规，"
            f"但发现 {len(result.violations)} 处违规: "
            f"{[v.message for v in result.violations]}"
        )

    def test_real_gateway_init_has_getattr_lazy_load(self) -> None:
        """实际的 Gateway __init__.py 应包含 __getattr__ 懒加载函数"""
        project_root = get_project_root()
        file_path = project_root / GATEWAY_INIT_PATH

        result = check_gateway_import_surface(file_path)

        assert result.has_getattr_lazy_load, "Gateway __init__.py 应包含 __getattr__ 懒加载函数"

    def test_real_gateway_init_has_type_checking_guard(self) -> None:
        """实际的 Gateway __init__.py 应包含 TYPE_CHECKING 块"""
        project_root = get_project_root()
        file_path = project_root / GATEWAY_INIT_PATH

        result = check_gateway_import_surface(file_path)

        assert result.has_type_checking_guard, "Gateway __init__.py 应包含 TYPE_CHECKING 块"


class TestScriptSubprocess:
    """测试通过子进程运行脚本"""

    def test_script_exits_zero_for_real_gateway_init(self) -> None:
        """脚本检查实际 Gateway __init__.py 应返回 0（成功）"""
        project_root = get_project_root()
        script_path = project_root / "scripts" / "ci" / "check_gateway_import_surface.py"

        result = subprocess.run(
            [sys.executable, str(script_path), "--project-root", str(project_root)],
            capture_output=True,
            text=True,
            cwd=str(project_root),
        )

        assert result.returncode == 0, (
            f"脚本应返回 0，但返回 {result.returncode}\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

    def test_script_json_output_ok_true(self) -> None:
        """脚本 JSON 输出应显示 ok=true"""
        project_root = get_project_root()
        script_path = project_root / "scripts" / "ci" / "check_gateway_import_surface.py"

        result = subprocess.run(
            [
                sys.executable,
                str(script_path),
                "--project-root",
                str(project_root),
                "--json",
            ],
            capture_output=True,
            text=True,
            cwd=str(project_root),
        )

        assert result.returncode == 0

        import json

        output = json.loads(result.stdout)
        assert output["ok"] is True, f"JSON 输出应显示 ok=true: {output}"
        assert output["violation_count"] == 0


# ============================================================================
# 负例测试：验证 eager-import 会被检测
# ============================================================================


class TestEagerImportDetection:
    """验证 eager-import 会被正确检测（负例测试）"""

    def test_from_import_submodule_detected(self) -> None:
        """from . import logbook_adapter 应被检测为违规"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 违规：eager-import 懒加载子模块
from . import logbook_adapter

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_gateway_import_surface(file_path)

            assert result.has_violations()
            assert len(result.violations) == 1
            assert result.violations[0].submodule == "logbook_adapter"
            assert result.violations[0].import_type == "from_import"
        finally:
            file_path.unlink()

    def test_from_submodule_import_detected(self) -> None:
        """from .logbook_adapter import ... 应被检测为违规"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 违规：从懒加载子模块导入
from .logbook_adapter import LogbookAdapter

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_gateway_import_surface(file_path)

            assert result.has_violations()
            assert len(result.violations) == 1
            assert result.violations[0].submodule == "logbook_adapter"
            assert result.violations[0].import_type == "from_import"
        finally:
            file_path.unlink()

    def test_absolute_import_detected(self) -> None:
        """import engram.gateway.logbook_adapter 应被检测为违规"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 违规：绝对导入懒加载子模块
import engram.gateway.logbook_adapter

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_gateway_import_surface(file_path)

            assert result.has_violations()
            assert len(result.violations) == 1
            assert result.violations[0].submodule == "logbook_adapter"
            assert result.violations[0].import_type == "import"
        finally:
            file_path.unlink()

    def test_multiple_eager_imports_detected(self) -> None:
        """多个 eager-import 应全部被检测"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 违规：多个 eager-import
from . import logbook_adapter
from . import openmemory_client
from . import outbox_worker

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_gateway_import_surface(file_path)

            assert result.has_violations()
            assert len(result.violations) == 3

            submodules = {v.submodule for v in result.violations}
            assert submodules == LAZY_SUBMODULES
        finally:
            file_path.unlink()

    def test_type_checking_block_allows_import(self) -> None:
        """TYPE_CHECKING 块内的导入不应被检测为违规"""
        content = '''\
"""Test module"""
from __future__ import annotations

from typing import TYPE_CHECKING

# TYPE_CHECKING 块内允许
if TYPE_CHECKING:
    from . import logbook_adapter as logbook_adapter
    from . import openmemory_client as openmemory_client
    from . import outbox_worker as outbox_worker

__version__ = "1.0.0"

def __getattr__(name: str):
    import importlib
    _lazy_submodules = {"logbook_adapter", "openmemory_client", "outbox_worker"}
    if name in _lazy_submodules:
        module = importlib.import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
'''
        file_path = create_temp_file(content)
        try:
            result = check_gateway_import_surface(file_path)

            assert not result.has_violations(), (
                f"TYPE_CHECKING 块内的导入不应触发违规: {result.violations}"
            )
            assert result.has_type_checking_guard
            assert result.has_getattr_lazy_load
        finally:
            file_path.unlink()

    def test_import_outside_type_checking_block_detected(self) -> None:
        """TYPE_CHECKING 块外的导入应被检测"""
        content = '''\
"""Test module"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import logbook_adapter as logbook_adapter

# 违规：在 TYPE_CHECKING 块外
from . import openmemory_client

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_gateway_import_surface(file_path)

            assert result.has_violations()
            assert len(result.violations) == 1
            assert result.violations[0].submodule == "openmemory_client"
        finally:
            file_path.unlink()


class TestEdgeCases:
    """边界情况测试"""

    def test_empty_file(self) -> None:
        """空文件不应报错"""
        content = ""
        file_path = create_temp_file(content)
        try:
            result = check_gateway_import_surface(file_path)

            assert not result.has_violations()
        finally:
            file_path.unlink()

    def test_file_without_imports(self) -> None:
        """没有导入语句的文件不应报错"""
        content = '''\
"""Test module without imports"""

__version__ = "1.0.0"
__all__ = []

def some_function():
    pass
'''
        file_path = create_temp_file(content)
        try:
            result = check_gateway_import_surface(file_path)

            assert not result.has_violations()
        finally:
            file_path.unlink()

    def test_import_inside_function_allowed(self) -> None:
        """函数内部的导入不应被检测（延迟执行）"""
        content = '''\
"""Test module"""
from __future__ import annotations

__version__ = "1.0.0"

def get_adapter():
    # 函数内导入是延迟执行的，不应触发
    from . import logbook_adapter
    return logbook_adapter
'''
        file_path = create_temp_file(content)
        try:
            result = check_gateway_import_surface(file_path)

            assert not result.has_violations(), "函数内的导入不应触发违规"
        finally:
            file_path.unlink()

    def test_non_lazy_submodule_allowed(self) -> None:
        """非懒加载子模块的导入应被允许"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 不在 LAZY_SUBMODULES 列表中的子模块允许 eager-import
from . import mcp_rpc
from . import policy

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_gateway_import_surface(file_path)

            assert not result.has_violations()
        finally:
            file_path.unlink()

    def test_nonexistent_file(self) -> None:
        """不存在的文件应返回空结果"""
        file_path = Path("/nonexistent/path/to/file.py")

        result = check_gateway_import_surface(file_path)

        assert not result.has_violations()

    def test_result_to_dict(self) -> None:
        """CheckResult.to_dict() 应返回正确的格式"""
        content = '''\
"""Test module"""
from __future__ import annotations

from . import logbook_adapter

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_gateway_import_surface(file_path)
            output = result.to_dict()

            assert "ok" in output
            assert output["ok"] is False
            assert "violation_count" in output
            assert output["violation_count"] == 1
            assert "violations" in output
            assert len(output["violations"]) == 1
            assert "line_number" in output["violations"][0]
            assert "submodule" in output["violations"][0]
        finally:
            file_path.unlink()


# ============================================================================
# 配置验证测试
# ============================================================================


class TestConfiguration:
    """验证配置正确性"""

    def test_lazy_submodules_match_real_init(self) -> None:
        """LAZY_SUBMODULES 配置应与实际 __init__.py 中的懒加载模块一致"""
        project_root = get_project_root()
        file_path = project_root / GATEWAY_INIT_PATH

        content = file_path.read_text()

        # 检查所有配置的懒加载子模块都在 __init__.py 中被引用
        for submodule in LAZY_SUBMODULES:
            assert submodule in content, (
                f"LAZY_SUBMODULES 中的 '{submodule}' 未在 Gateway __init__.py 中使用"
            )

    def test_gateway_init_path_valid(self) -> None:
        """GATEWAY_INIT_PATH 配置应指向存在的文件"""
        project_root = get_project_root()
        file_path = project_root / GATEWAY_INIT_PATH

        assert file_path.exists(), f"GATEWAY_INIT_PATH 配置无效: {GATEWAY_INIT_PATH}"
