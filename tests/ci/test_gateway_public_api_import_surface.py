"""
Gateway Public API Import Surface 门禁测试

测试 scripts/ci/check_gateway_public_api_import_surface.py 的功能，确保：
1. 正例：实际的 src/engram/gateway/public_api.py 通过检查
2. 负例：包含非 allowlist 模块导入的代码会被正确识别
3. 负例：包含 Tier B 模块 eager-import 的代码会被正确识别

与 CI job 对应关系：
- 本测试对应 CI workflow 中的 gateway-public-api-surface job
- 门禁脚本路径: scripts/ci/check_gateway_public_api_import_surface.py
- 被检查文件: src/engram/gateway/public_api.py

运行方式：
    pytest tests/ci/test_gateway_public_api_import_surface.py -v
    # 或作为 tests/ci 整体运行
    pytest tests/ci -v
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from scripts.ci.check_gateway_public_api_import_surface import (
    ALLOWED_RELATIVE_IMPORTS,
    PUBLIC_API_PATH,
    TIER_B_MODULES,
    TIER_B_SUBMODULE_PATHS,
    check_public_api_import_surface,
)
from tests.ci.helpers.subprocess_env import get_subprocess_env

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
# 正例测试：验证实际的 public_api.py 通过检查
# ============================================================================


class TestPublicApiPassesCheck:
    """验证实际的 src/engram/gateway/public_api.py 通过检查"""

    def test_real_public_api_passes(self) -> None:
        """实际的 public_api.py 应通过检查（无 Tier B eager-import 违规）"""
        project_root = get_project_root()
        file_path = project_root / PUBLIC_API_PATH

        assert file_path.exists(), f"public_api.py 不存在: {file_path}"

        result = check_public_api_import_surface(file_path)

        assert not result.has_violations(), (
            f"public_api.py 不应有 Tier B eager-import 违规，"
            f"但发现 {len(result.violations)} 处违规: "
            f"{[v.message for v in result.violations]}"
        )

    def test_real_public_api_has_getattr_lazy_load(self) -> None:
        """实际的 public_api.py 应包含 __getattr__ 懒加载函数"""
        project_root = get_project_root()
        file_path = project_root / PUBLIC_API_PATH

        result = check_public_api_import_surface(file_path)

        assert result.has_getattr_lazy_load, "public_api.py 应包含 __getattr__ 懒加载函数"

    def test_real_public_api_has_type_checking_guard(self) -> None:
        """实际的 public_api.py 应包含 TYPE_CHECKING 块"""
        project_root = get_project_root()
        file_path = project_root / PUBLIC_API_PATH

        result = check_public_api_import_surface(file_path)

        assert result.has_type_checking_guard, "public_api.py 应包含 TYPE_CHECKING 块"

    def test_real_public_api_has_tier_b_mapping(self) -> None:
        """实际的 public_api.py 应包含 _TIER_B_LAZY_IMPORTS 映射表"""
        project_root = get_project_root()
        file_path = project_root / PUBLIC_API_PATH

        result = check_public_api_import_surface(file_path)

        assert result.has_tier_b_lazy_imports_mapping, (
            "public_api.py 应包含 _TIER_B_LAZY_IMPORTS 映射表"
        )

    def test_real_public_api_all_consistency(self) -> None:
        """实际的 public_api.py 的 __all__ 与 _TIER_B_LAZY_IMPORTS 应保持一致"""
        project_root = get_project_root()
        file_path = project_root / PUBLIC_API_PATH

        result = check_public_api_import_surface(file_path)

        assert result.all_consistency.is_consistent(), (
            f"_TIER_B_LAZY_IMPORTS 中以下 key 不在 __all__ 中: "
            f"{result.all_consistency.missing_in_all}"
        )
        # 确保提取到了数据
        assert len(result.all_consistency.all_symbols) > 0, "__all__ 应包含符号"
        assert len(result.all_consistency.tier_b_keys) > 0, "_TIER_B_LAZY_IMPORTS 应包含 key"


class TestScriptSubprocess:
    """测试通过子进程运行脚本"""

    def test_script_exits_zero_for_real_public_api(self) -> None:
        """脚本检查实际 public_api.py 应返回 0（成功）"""
        project_root = get_project_root()
        script_path = project_root / "scripts" / "ci" / "check_gateway_public_api_import_surface.py"

        result = subprocess.run(
            [sys.executable, str(script_path), "--project-root", str(project_root)],
            capture_output=True,
            text=True,
            env=get_subprocess_env(project_root),
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
        script_path = project_root / "scripts" / "ci" / "check_gateway_public_api_import_surface.py"

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
            env=get_subprocess_env(project_root),
            cwd=str(project_root),
        )

        assert result.returncode == 0

        import json

        output = json.loads(result.stdout)
        assert output["ok"] is True, f"JSON 输出应显示 ok=true: {output}"
        assert output["violation_count"] == 0


# ============================================================================
# 负例测试：验证 Tier B eager-import 会被检测
# ============================================================================


class TestTierBEagerImportDetection:
    """验证 Tier B 模块 eager-import 会被正确检测（负例测试）"""

    def test_from_import_logbook_adapter_detected(self) -> None:
        """from . import logbook_adapter 应被检测为违规"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 违规：eager-import Tier B 模块
from . import logbook_adapter

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert result.has_violations()
            assert len(result.violations) == 1
            assert result.violations[0].module_name == "logbook_adapter"
            assert result.violations[0].import_type == "from_import"
            assert result.violations[0].violation_type == "tier_b_eager_import"
        finally:
            file_path.unlink()

    def test_from_logbook_adapter_import_detected(self) -> None:
        """from .logbook_adapter import ... 应被检测为违规"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 违规：从 Tier B 模块直接导入
from .logbook_adapter import LogbookAdapter

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert result.has_violations()
            assert len(result.violations) == 1
            assert result.violations[0].module_name == "logbook_adapter"
            assert result.violations[0].import_type == "from_import"
            assert result.violations[0].violation_type == "tier_b_eager_import"
        finally:
            file_path.unlink()

    def test_from_mcp_rpc_import_detected(self) -> None:
        """from .mcp_rpc import ... 应被检测为违规"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 违规：从 Tier B 模块直接导入
from .mcp_rpc import dispatch_jsonrpc_request

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert result.has_violations()
            assert len(result.violations) == 1
            assert result.violations[0].module_name == "mcp_rpc"
            assert result.violations[0].import_type == "from_import"
            assert result.violations[0].violation_type == "tier_b_eager_import"
        finally:
            file_path.unlink()

    def test_from_entrypoints_tool_executor_import_detected(self) -> None:
        """from .entrypoints.tool_executor import ... 应被检测为违规"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 违规：从 Tier B 子模块直接导入
from .entrypoints.tool_executor import execute_tool

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert result.has_violations()
            assert len(result.violations) == 1
            assert result.violations[0].module_name == "entrypoints.tool_executor"
            assert result.violations[0].import_type == "from_import"
            assert result.violations[0].violation_type == "tier_b_eager_import"
        finally:
            file_path.unlink()

    def test_absolute_import_detected(self) -> None:
        """import engram.gateway.logbook_adapter 应被检测为违规"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 违规：绝对导入 Tier B 模块
import engram.gateway.logbook_adapter

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert result.has_violations()
            assert len(result.violations) == 1
            assert result.violations[0].module_name == "logbook_adapter"
            assert result.violations[0].import_type == "import"
            assert result.violations[0].violation_type == "tier_b_eager_import"
        finally:
            file_path.unlink()

    def test_multiple_tier_b_eager_imports_detected(self) -> None:
        """多个 Tier B eager-import 应全部被检测"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 违规：多个 Tier B eager-import
from . import logbook_adapter
from . import mcp_rpc
from . import entrypoints

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert result.has_violations()
            assert len(result.violations) == 3

            modules = {v.module_name for v in result.violations}
            assert modules == TIER_B_MODULES
            # 所有违规都应为 tier_b_eager_import 类型
            for v in result.violations:
                assert v.violation_type == "tier_b_eager_import"
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
    from .logbook_adapter import LogbookAdapter as LogbookAdapter
    from .mcp_rpc import dispatch_jsonrpc_request as dispatch_jsonrpc_request
    from .entrypoints.tool_executor import execute_tool as execute_tool

__version__ = "1.0.0"

__all__ = ["LogbookAdapter"]

_TIER_B_LAZY_IMPORTS = {
    "LogbookAdapter": (".logbook_adapter", "LogbookAdapter"),
}

_TIER_B_INSTALL_HINTS = {
    ".logbook_adapter": "请安装 engram-logbook",
}

def __getattr__(name: str):
    import importlib
    if name in _TIER_B_LAZY_IMPORTS:
        module_path, attr_name = _TIER_B_LAZY_IMPORTS[name]
        module = importlib.import_module(module_path, __package__)
        obj = getattr(module, attr_name)
        globals()[name] = obj
        return obj
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert not result.has_violations(), (
                f"TYPE_CHECKING 块内的导入不应触发违规: {result.violations}"
            )
            assert result.has_type_checking_guard
            assert result.has_getattr_lazy_load
            assert result.has_tier_b_lazy_imports_mapping
            assert not result.has_consistency_errors(), (
                f"不应有一致性错误: all_consistency={result.all_consistency}, "
                f"install_hint_consistency={result.install_hint_consistency}"
            )
        finally:
            file_path.unlink()

    def test_import_outside_type_checking_block_detected(self) -> None:
        """TYPE_CHECKING 块外的导入应被检测"""
        content = '''\
"""Test module"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .logbook_adapter import LogbookAdapter as LogbookAdapter

# 违规：在 TYPE_CHECKING 块外
from .mcp_rpc import JsonRpcDispatchResult

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert result.has_violations()
            assert len(result.violations) == 1
            assert result.violations[0].module_name == "mcp_rpc"
            assert result.violations[0].violation_type == "tier_b_eager_import"
        finally:
            file_path.unlink()


# ============================================================================
# 非 allowlist 模块导入检测（负例测试）
# ============================================================================


class TestNotInAllowlistDetection:
    """验证非 allowlist 模块的导入会被正确检测（负例测试）"""

    def test_from_container_import_detected(self) -> None:
        """from .container import ... 应被检测为违规（container 不在 allowlist）"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 违规：container 不在 allowlist 中
from .container import SomeContainer

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert result.has_violations()
            assert len(result.violations) == 1
            assert result.violations[0].module_name == "container"
            assert result.violations[0].import_type == "from_import"
            assert result.violations[0].violation_type == "not_in_allowlist"
        finally:
            file_path.unlink()

    def test_from_config_import_detected(self) -> None:
        """from .config import ... 应被检测为违规（config 不在 allowlist）"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 违规：config 不在 allowlist 中
from .config import GatewayConfig

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert result.has_violations()
            assert len(result.violations) == 1
            assert result.violations[0].module_name == "config"
            assert result.violations[0].import_type == "from_import"
            assert result.violations[0].violation_type == "not_in_allowlist"
        finally:
            file_path.unlink()

    def test_from_middleware_import_detected(self) -> None:
        """from .middleware import ... 应被检测为违规（middleware 不在 allowlist）"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 违规：middleware 不在 allowlist 中
from .middleware import some_middleware

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert result.has_violations()
            assert len(result.violations) == 1
            assert result.violations[0].module_name == "middleware"
            assert result.violations[0].import_type == "from_import"
            assert result.violations[0].violation_type == "not_in_allowlist"
        finally:
            file_path.unlink()

    def test_from_app_import_detected(self) -> None:
        """from .app import ... 应被检测为违规（app 不在 allowlist）"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 违规：app 不在 allowlist 中
from .app import create_app

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert result.has_violations()
            assert len(result.violations) == 1
            assert result.violations[0].module_name == "app"
            assert result.violations[0].import_type == "from_import"
            assert result.violations[0].violation_type == "not_in_allowlist"
        finally:
            file_path.unlink()

    def test_from_import_module_not_in_allowlist(self) -> None:
        """from . import <非 allowlist 模块> 应被检测"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 违规：直接导入非 allowlist 模块
from . import container
from . import config

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert result.has_violations()
            assert len(result.violations) == 2

            modules = {v.module_name for v in result.violations}
            assert modules == {"container", "config"}
            for v in result.violations:
                assert v.violation_type == "not_in_allowlist"
        finally:
            file_path.unlink()

    def test_absolute_import_not_in_allowlist_detected(self) -> None:
        """import engram.gateway.container 应被检测为违规"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 违规：绝对导入非 allowlist 模块
import engram.gateway.container

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert result.has_violations()
            assert len(result.violations) == 1
            assert result.violations[0].module_name == "container"
            assert result.violations[0].import_type == "import"
            assert result.violations[0].violation_type == "not_in_allowlist"
        finally:
            file_path.unlink()

    def test_from_absolute_import_not_in_allowlist_detected(self) -> None:
        """from engram.gateway.config import ... 应被检测为违规"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 违规：绝对导入非 allowlist 模块
from engram.gateway.config import GatewayConfig

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert result.has_violations()
            assert len(result.violations) == 1
            assert result.violations[0].module_name == "config"
            assert result.violations[0].import_type == "from_import"
            assert result.violations[0].violation_type == "not_in_allowlist"
        finally:
            file_path.unlink()

    def test_type_checking_block_allows_non_allowlist_import(self) -> None:
        """TYPE_CHECKING 块内允许非 allowlist 模块导入"""
        content = '''\
"""Test module"""
from __future__ import annotations

from typing import TYPE_CHECKING

# TYPE_CHECKING 块内允许
if TYPE_CHECKING:
    from .container import SomeContainer
    from .config import GatewayConfig

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert not result.has_violations(), (
                f"TYPE_CHECKING 块内的非 allowlist 导入不应触发违规: {result.violations}"
            )
        finally:
            file_path.unlink()

    def test_multiple_non_allowlist_imports_detected(self) -> None:
        """多个非 allowlist 模块导入应全部被检测"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 违规：多个非 allowlist 导入
from .container import Container
from .config import Config
from .middleware import Middleware
from .app import App

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert result.has_violations()
            assert len(result.violations) == 4

            modules = {v.module_name for v in result.violations}
            assert modules == {"container", "config", "middleware", "app"}
            for v in result.violations:
                assert v.violation_type == "not_in_allowlist"
        finally:
            file_path.unlink()

    def test_mixed_violations_detected(self) -> None:
        """混合违规（非 allowlist + Tier B eager-import）应全部被检测"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 违规：非 allowlist 模块
from .container import Container
from .config import Config

# 违规：Tier B eager-import
from .logbook_adapter import LogbookAdapter

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert result.has_violations()
            assert len(result.violations) == 3

            allowlist_violations = result.get_allowlist_violations()
            tier_b_violations = result.get_tier_b_violations()

            assert len(allowlist_violations) == 2
            assert len(tier_b_violations) == 1

            assert {v.module_name for v in allowlist_violations} == {"container", "config"}
            assert tier_b_violations[0].module_name == "logbook_adapter"
        finally:
            file_path.unlink()


# ============================================================================
# Tier A 模块允许测试
# ============================================================================


class TestTierAModulesAllowed:
    """验证 Tier A 模块的直接导入是允许的"""

    def test_tier_a_di_module_allowed(self) -> None:
        """from .di import ... 应被允许（Tier A）"""
        content = '''\
"""Test module"""
from __future__ import annotations

# Tier A：核心类型允许直接导入
from .di import (
    GatewayDeps,
    GatewayDepsProtocol,
    RequestContext,
)

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert not result.has_violations()
        finally:
            file_path.unlink()

    def test_tier_a_error_codes_allowed(self) -> None:
        """from .error_codes import ... 应被允许（Tier A）"""
        content = '''\
"""Test module"""
from __future__ import annotations

# Tier A：错误码允许直接导入
from .error_codes import McpErrorCode, McpErrorCategory

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert not result.has_violations()
        finally:
            file_path.unlink()

    def test_tier_a_services_ports_allowed(self) -> None:
        """from .services.ports import ... 应被允许（Tier A）"""
        content = '''\
"""Test module"""
from __future__ import annotations

# Tier A：服务端口 Protocol 允许直接导入
from .services.ports import WriteAuditPort, UserDirectoryPort

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert not result.has_violations()
        finally:
            file_path.unlink()


# ============================================================================
# 边界情况测试
# ============================================================================


class TestEdgeCases:
    """边界情况测试"""

    def test_empty_file(self) -> None:
        """空文件不应报错"""
        content = ""
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

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
            result = check_public_api_import_surface(file_path)

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
    from .logbook_adapter import LogbookAdapter
    return LogbookAdapter
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert not result.has_violations(), "函数内的导入不应触发违规"
        finally:
            file_path.unlink()

    def test_nonexistent_file(self) -> None:
        """不存在的文件应返回空结果"""
        file_path = Path("/nonexistent/path/to/file.py")

        result = check_public_api_import_surface(file_path)

        assert not result.has_violations()

    def test_result_to_dict(self) -> None:
        """CheckResult.to_dict() 应返回正确的格式"""
        content = '''\
"""Test module"""
from __future__ import annotations

from .logbook_adapter import LogbookAdapter

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)
            output = result.to_dict()

            assert "ok" in output
            assert output["ok"] is False
            assert "violation_count" in output
            assert output["violation_count"] == 1
            assert "allowlist_violation_count" in output
            assert "tier_b_violation_count" in output
            assert output["tier_b_violation_count"] == 1
            assert "violations" in output
            assert len(output["violations"]) == 1
            assert "line_number" in output["violations"][0]
            assert "module_name" in output["violations"][0]
            assert "violation_type" in output["violations"][0]
            assert output["violations"][0]["violation_type"] == "tier_b_eager_import"
        finally:
            file_path.unlink()


# ============================================================================
# 配置验证测试
# ============================================================================


class TestConfiguration:
    """
    验证配置正确性

    注意：脚本配置（ALLOWED_RELATIVE_IMPORTS, TIER_B_MODULES 等）与文档/注释的一致性检查
    由 docs sync gate (check_gateway_public_api_docs_sync.py) 承担，本测试类专注于：
    1. 配置的逻辑正确性（allowlist 和 Tier B 不相交）
    2. 路径有效性
    3. 脚本能正确识别配置中定义的 Tier B 模块 eager-import（负例）
    4. 真实 public_api.py 无违规（正例）
    """

    def test_allowlist_and_tier_b_are_disjoint(self) -> None:
        """ALLOWED_RELATIVE_IMPORTS 和 TIER_B_MODULES 应该不相交"""
        intersection = ALLOWED_RELATIVE_IMPORTS & TIER_B_MODULES
        assert len(intersection) == 0, (
            f"ALLOWED_RELATIVE_IMPORTS 和 TIER_B_MODULES 不应有交集: {intersection}"
        )

    def test_public_api_path_valid(self) -> None:
        """PUBLIC_API_PATH 配置应指向存在的文件"""
        project_root = get_project_root()
        file_path = project_root / PUBLIC_API_PATH

        assert file_path.exists(), f"PUBLIC_API_PATH 配置无效: {PUBLIC_API_PATH}"

    def test_script_detects_configured_tier_b_modules_eager_import(self) -> None:
        """
        负例：验证脚本能识别配置中定义的所有 Tier B 模块的 eager-import

        这确保 TIER_B_MODULES 配置的每个模块都能被脚本正确检测。
        """
        # 遍历配置中的每个 Tier B 模块
        for tier_b_module in TIER_B_MODULES:
            # 构造临时文件，包含该 Tier B 模块的 eager-import
            content = f'''\
"""Test module for {tier_b_module}"""
from __future__ import annotations

# 违规：eager-import 配置中的 Tier B 模块
from . import {tier_b_module}

__version__ = "1.0.0"
'''
            file_path = create_temp_file(content)
            try:
                result = check_public_api_import_surface(file_path)

                assert result.has_violations(), (
                    f"脚本未能检测到 TIER_B_MODULES 中 '{tier_b_module}' 的 eager-import 违规"
                )
                assert len(result.violations) == 1, (
                    f"应只有 1 处违规，但发现 {len(result.violations)} 处"
                )
                assert result.violations[0].module_name == tier_b_module, (
                    f"违规模块名应为 '{tier_b_module}'，但实际为 '{result.violations[0].module_name}'"
                )
                assert result.violations[0].violation_type == "tier_b_eager_import", (
                    f"违规类型应为 'tier_b_eager_import'，但实际为 '{result.violations[0].violation_type}'"
                )
            finally:
                file_path.unlink()

    def test_script_detects_configured_tier_b_submodule_paths_eager_import(self) -> None:
        """
        负例：验证脚本能识别配置中定义的所有 Tier B 子模块路径的 eager-import

        这确保 TIER_B_SUBMODULE_PATHS 配置的每个路径都能被脚本正确检测。
        """
        # 遍历配置中的每个 Tier B 子模块路径
        for submodule_path in TIER_B_SUBMODULE_PATHS:
            # 构造临时文件，包含该子模块路径的 eager-import
            content = f'''\
"""Test module for {submodule_path}"""
from __future__ import annotations

# 违规：eager-import 配置中的 Tier B 子模块路径
from .{submodule_path} import some_symbol

__version__ = "1.0.0"
'''
            file_path = create_temp_file(content)
            try:
                result = check_public_api_import_surface(file_path)

                assert result.has_violations(), (
                    f"脚本未能检测到 TIER_B_SUBMODULE_PATHS 中 '{submodule_path}' 的 eager-import 违规"
                )
                assert len(result.violations) == 1, (
                    f"应只有 1 处违规，但发现 {len(result.violations)} 处"
                )
                assert result.violations[0].module_name == submodule_path, (
                    f"违规模块名应为 '{submodule_path}'，但实际为 '{result.violations[0].module_name}'"
                )
                assert result.violations[0].violation_type == "tier_b_eager_import", (
                    f"违规类型应为 'tier_b_eager_import'，但实际为 '{result.violations[0].violation_type}'"
                )
            finally:
                file_path.unlink()

    def test_real_public_api_passes_with_current_config(self) -> None:
        """
        正例：验证真实 public_api.py 在当前配置下无违规

        这确保脚本配置与实际代码的一致性：
        - 如果 public_api.py 有 eager-import 某个模块，该模块必须在 allowlist 中
        - 如果 public_api.py 使用了 Tier B 模块，必须通过 TYPE_CHECKING 或 __getattr__ 懒加载
        """
        project_root = get_project_root()
        file_path = project_root / PUBLIC_API_PATH

        result = check_public_api_import_surface(file_path)

        # 验证无 Tier B eager-import 违规
        tier_b_violations = result.get_tier_b_violations()
        assert len(tier_b_violations) == 0, (
            f"真实 public_api.py 不应有 Tier B eager-import 违规，"
            f"但发现 {len(tier_b_violations)} 处: "
            f"{[v.module_name for v in tier_b_violations]}"
        )

        # 验证无 allowlist 违规
        allowlist_violations = result.get_allowlist_violations()
        assert len(allowlist_violations) == 0, (
            f"真实 public_api.py 不应有 allowlist 违规，"
            f"但发现 {len(allowlist_violations)} 处: "
            f"{[v.module_name for v in allowlist_violations]}"
        )

        # 整体无违规
        assert not result.has_violations(), (
            f"真实 public_api.py 应通过检查，但发现 {len(result.violations)} 处违规"
        )


# ============================================================================
# __all__ 与 _TIER_B_LAZY_IMPORTS 一致性校验测试
# ============================================================================


class TestAllConsistency:
    """测试 __all__ 与 _TIER_B_LAZY_IMPORTS 一致性校验功能"""

    def test_consistent_all_and_tier_b(self) -> None:
        """正例：_TIER_B_LAZY_IMPORTS 所有 key 都在 __all__ 中且有 install_hint 时应通过"""
        content = '''\
"""Test module"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .logbook_adapter import LogbookAdapter as LogbookAdapter

__all__ = [
    "RequestContext",
    "LogbookAdapter",
]

_TIER_B_LAZY_IMPORTS = {
    "LogbookAdapter": (".logbook_adapter", "LogbookAdapter"),
}

_TIER_B_INSTALL_HINTS = {
    ".logbook_adapter": "请安装 engram-logbook",
}

def __getattr__(name: str):
    import importlib
    if name in _TIER_B_LAZY_IMPORTS:
        module_path, attr_name = _TIER_B_LAZY_IMPORTS[name]
        module = importlib.import_module(module_path, __package__)
        return getattr(module, attr_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert result.all_consistency.is_consistent()
            assert result.all_consistency.all_symbols == ["RequestContext", "LogbookAdapter"]
            assert result.all_consistency.tier_b_keys == ["LogbookAdapter"]
            assert result.all_consistency.missing_in_all == []
            assert result.install_hint_consistency.is_consistent()
            assert not result.has_consistency_errors()
        finally:
            file_path.unlink()

    def test_inconsistent_missing_in_all(self) -> None:
        """负例：_TIER_B_LAZY_IMPORTS 的 key 不在 __all__ 中应报错"""
        content = '''\
"""Test module"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .logbook_adapter import LogbookAdapter as LogbookAdapter

__all__ = [
    "RequestContext",
    # 缺少 LogbookAdapter
]

_TIER_B_LAZY_IMPORTS = {
    "LogbookAdapter": (".logbook_adapter", "LogbookAdapter"),
    "get_adapter": (".logbook_adapter", "get_adapter"),
}

_TIER_B_INSTALL_HINTS = {
    ".logbook_adapter": "请安装 engram-logbook",
}

def __getattr__(name: str):
    import importlib
    if name in _TIER_B_LAZY_IMPORTS:
        module_path, attr_name = _TIER_B_LAZY_IMPORTS[name]
        module = importlib.import_module(module_path, __package__)
        return getattr(module, attr_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            # install_hint 应该一致（提供了 _TIER_B_INSTALL_HINTS）
            assert result.install_hint_consistency.is_consistent(), (
                f"install_hint 应一致: {result.install_hint_consistency}"
            )
            # all_consistency 应该不一致（缺少 key 在 __all__ 中）
            assert not result.all_consistency.is_consistent()
            assert result.has_consistency_errors()
            assert "LogbookAdapter" in result.all_consistency.missing_in_all
            assert "get_adapter" in result.all_consistency.missing_in_all
            assert len(result.all_consistency.missing_in_all) == 2
        finally:
            file_path.unlink()

    def test_partial_inconsistency(self) -> None:
        """负例：部分 _TIER_B_LAZY_IMPORTS key 不在 __all__ 中应报错"""
        content = '''\
"""Test module"""
from __future__ import annotations

__all__ = [
    "LogbookAdapter",
    # 缺少 get_adapter
]

_TIER_B_LAZY_IMPORTS = {
    "LogbookAdapter": (".logbook_adapter", "LogbookAdapter"),
    "get_adapter": (".logbook_adapter", "get_adapter"),
}

_TIER_B_INSTALL_HINTS = {
    ".logbook_adapter": "请安装 engram-logbook",
}

def __getattr__(name: str):
    pass
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            # install_hint 应该一致（提供了 _TIER_B_INSTALL_HINTS）
            assert result.install_hint_consistency.is_consistent(), (
                f"install_hint 应一致: {result.install_hint_consistency}"
            )
            # all_consistency 应该不一致（缺少 get_adapter 在 __all__ 中）
            assert not result.all_consistency.is_consistent()
            assert result.has_consistency_errors()
            assert result.all_consistency.missing_in_all == ["get_adapter"]
        finally:
            file_path.unlink()

    def test_empty_tier_b_is_consistent(self) -> None:
        """正例：空的 _TIER_B_LAZY_IMPORTS 应被视为一致"""
        content = '''\
"""Test module"""
from __future__ import annotations

__all__ = ["RequestContext"]

_TIER_B_LAZY_IMPORTS = {}

def __getattr__(name: str):
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert result.all_consistency.is_consistent()
            assert result.all_consistency.tier_b_keys == []
            assert result.all_consistency.missing_in_all == []
        finally:
            file_path.unlink()

    def test_no_all_defined(self) -> None:
        """边界：没有定义 __all__ 时应返回空列表"""
        content = '''\
"""Test module without __all__"""
from __future__ import annotations

_TIER_B_LAZY_IMPORTS = {
    "LogbookAdapter": (".logbook_adapter", "LogbookAdapter"),
}

_TIER_B_INSTALL_HINTS = {
    ".logbook_adapter": "请安装 engram-logbook",
}

def __getattr__(name: str):
    pass
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            # install_hint 应该一致（提供了 _TIER_B_INSTALL_HINTS）
            assert result.install_hint_consistency.is_consistent(), (
                f"install_hint 应一致: {result.install_hint_consistency}"
            )
            # 没有 __all__ 时，tier_b_keys 都会被视为 missing
            assert result.all_consistency.all_symbols == []
            assert result.all_consistency.tier_b_keys == ["LogbookAdapter"]
            assert result.all_consistency.missing_in_all == ["LogbookAdapter"]
            assert not result.all_consistency.is_consistent()
        finally:
            file_path.unlink()

    def test_no_tier_b_defined(self) -> None:
        """边界：没有定义 _TIER_B_LAZY_IMPORTS 时应返回空列表"""
        content = '''\
"""Test module without _TIER_B_LAZY_IMPORTS"""
from __future__ import annotations

__all__ = ["RequestContext"]
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert result.all_consistency.all_symbols == ["RequestContext"]
            assert result.all_consistency.tier_b_keys == []
            assert result.all_consistency.missing_in_all == []
            assert result.all_consistency.is_consistent()
        finally:
            file_path.unlink()

    def test_to_dict_includes_consistency(self) -> None:
        """to_dict() 应包含 all_consistency 字段"""
        content = '''\
"""Test module"""
from __future__ import annotations

__all__ = ["RequestContext"]

_TIER_B_LAZY_IMPORTS = {
    "LogbookAdapter": (".logbook_adapter", "LogbookAdapter"),
}

_TIER_B_INSTALL_HINTS = {
    ".logbook_adapter": "请安装 engram-logbook",
}

def __getattr__(name: str):
    pass
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)
            output = result.to_dict()

            # install_hint 应该一致（提供了 _TIER_B_INSTALL_HINTS）
            assert output["install_hint_consistency"]["is_consistent"] is True

            assert "all_consistency" in output
            assert output["all_consistency"]["is_consistent"] is False
            assert output["all_consistency"]["all_symbols"] == ["RequestContext"]
            assert output["all_consistency"]["tier_b_keys"] == ["LogbookAdapter"]
            assert output["all_consistency"]["missing_in_all"] == ["LogbookAdapter"]
            assert output["ok"] is False  # 一致性错误导致 ok 为 False（all_consistency 不一致）
        finally:
            file_path.unlink()

    def test_json_output_with_consistency(self) -> None:
        """JSON 输出应包含 all_consistency 信息"""
        project_root = get_project_root()
        script_path = project_root / "scripts" / "ci" / "check_gateway_public_api_import_surface.py"

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
            env=get_subprocess_env(project_root),
            cwd=str(project_root),
        )

        import json

        output = json.loads(result.stdout)
        assert "all_consistency" in output
        assert "is_consistent" in output["all_consistency"]
        assert "all_symbols" in output["all_consistency"]
        assert "tier_b_keys" in output["all_consistency"]
        assert "missing_in_all" in output["all_consistency"]


# ============================================================================
# 直接导入表面检查测试（Tier C 来源验证）
# ============================================================================


class TestDirectImportSurface:
    """
    测试直接导入表面检查功能

    确保 public_api.py 的直接导入模块集合：
    1. 全部来自 Tier A allowlist
    2. 不包含任何 Tier B 模块或其子模块
    """

    def test_real_public_api_direct_import_surface_valid(self) -> None:
        """实际的 public_api.py 的直接导入表面应有效（Tier C 来源均在 allowlist）"""
        project_root = get_project_root()
        file_path = project_root / PUBLIC_API_PATH

        result = check_public_api_import_surface(file_path)

        assert result.direct_import_surface.is_valid(), (
            f"public_api.py 的直接导入表面应有效，"
            f"但发现 Tier B 模块: {result.direct_import_surface.tier_b_in_direct_imports}, "
            f"非 allowlist 模块: {result.direct_import_surface.non_allowlist_in_direct_imports}"
        )
        # 确保直接导入了 Tier A 模块
        assert len(result.direct_import_surface.direct_import_modules) > 0, (
            "public_api.py 应有直接导入的模块"
        )

    def test_tier_b_in_direct_import_detected(self) -> None:
        """负例：直接导入 Tier B 模块应被检测并标记在 direct_import_surface 中"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 违规：直接导入 Tier B 模块
from .logbook_adapter import LogbookAdapter

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            # 应有违规
            assert result.has_violations()
            assert result.violations[0].violation_type == "tier_b_eager_import"

            # direct_import_surface 应标记 Tier B 模块
            assert not result.direct_import_surface.is_valid()
            assert "logbook_adapter" in result.direct_import_surface.tier_b_in_direct_imports
            assert "logbook_adapter" in result.direct_import_surface.direct_import_modules

            # has_direct_import_surface_errors 应返回 True
            assert result.has_direct_import_surface_errors()
        finally:
            file_path.unlink()

    def test_tier_b_submodule_in_direct_import_detected(self) -> None:
        """负例：直接导入 Tier B 子模块应被检测"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 违规：直接导入 Tier B 子模块
from .entrypoints.tool_executor import execute_tool

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            # 应有违规
            assert result.has_violations()
            assert result.violations[0].violation_type == "tier_b_eager_import"

            # direct_import_surface 应标记 Tier B 子模块
            assert not result.direct_import_surface.is_valid()
            assert (
                "entrypoints.tool_executor" in result.direct_import_surface.tier_b_in_direct_imports
            )
        finally:
            file_path.unlink()

    def test_multiple_tier_b_in_direct_import_all_detected(self) -> None:
        """负例：多个 Tier B 模块直接导入应全部被检测"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 违规：多个 Tier B 模块直接导入
from . import logbook_adapter
from . import mcp_rpc
from . import entrypoints

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            # 应有 3 处违规
            assert len(result.violations) == 3

            # direct_import_surface 应标记所有 Tier B 模块
            assert not result.direct_import_surface.is_valid()
            assert set(result.direct_import_surface.tier_b_in_direct_imports) == TIER_B_MODULES
        finally:
            file_path.unlink()

    def test_tier_a_only_direct_import_valid(self) -> None:
        """正例：仅直接导入 Tier A 模块应通过"""
        content = '''\
"""Test module"""
from __future__ import annotations

# Tier A 模块直接导入
from .di import GatewayDeps, RequestContext
from .error_codes import McpErrorCode
from .services.ports import WriteAuditPort

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            # 应无违规
            assert not result.has_violations()

            # direct_import_surface 应有效
            assert result.direct_import_surface.is_valid()
            assert len(result.direct_import_surface.direct_import_modules) == 3
            assert "di" in result.direct_import_surface.direct_import_modules
            assert "error_codes" in result.direct_import_surface.direct_import_modules
            assert "services.ports" in result.direct_import_surface.direct_import_modules

            # 无 Tier B 模块
            assert len(result.direct_import_surface.tier_b_in_direct_imports) == 0
            # 无非 allowlist 模块
            assert len(result.direct_import_surface.non_allowlist_in_direct_imports) == 0
        finally:
            file_path.unlink()

    def test_non_allowlist_in_direct_import_detected(self) -> None:
        """负例：直接导入非 allowlist 模块应被检测"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 违规：非 allowlist 模块
from .container import SomeContainer
from .config import GatewayConfig

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            # 应有违规
            assert result.has_violations()
            assert len(result.violations) == 2

            # direct_import_surface 应标记非 allowlist 模块
            assert not result.direct_import_surface.is_valid()
            assert "container" in result.direct_import_surface.non_allowlist_in_direct_imports
            assert "config" in result.direct_import_surface.non_allowlist_in_direct_imports
        finally:
            file_path.unlink()

    def test_mixed_tier_b_and_non_allowlist_detected(self) -> None:
        """负例：混合违规（Tier B + 非 allowlist）应全部被检测"""
        content = '''\
"""Test module"""
from __future__ import annotations

# Tier A 允许
from .di import RequestContext

# 违规：Tier B 模块
from .logbook_adapter import LogbookAdapter

# 违规：非 allowlist 模块
from .container import Container

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            # 应有 2 处违规
            assert len(result.violations) == 2

            # direct_import_surface 应标记所有问题
            assert not result.direct_import_surface.is_valid()
            assert "logbook_adapter" in result.direct_import_surface.tier_b_in_direct_imports
            assert "container" in result.direct_import_surface.non_allowlist_in_direct_imports

            # di 模块应在直接导入列表中但不在违规列表中
            assert "di" in result.direct_import_surface.direct_import_modules
            assert "di" not in result.direct_import_surface.tier_b_in_direct_imports
            assert "di" not in result.direct_import_surface.non_allowlist_in_direct_imports
        finally:
            file_path.unlink()

    def test_to_dict_includes_direct_import_surface(self) -> None:
        """to_dict() 应包含 direct_import_surface 字段"""
        content = '''\
"""Test module"""
from __future__ import annotations

from .di import RequestContext
from .logbook_adapter import LogbookAdapter

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)
            output = result.to_dict()

            assert "direct_import_surface" in output
            assert "is_valid" in output["direct_import_surface"]
            assert output["direct_import_surface"]["is_valid"] is False
            assert "direct_import_modules" in output["direct_import_surface"]
            assert "tier_b_in_direct_imports" in output["direct_import_surface"]
            assert "non_allowlist_in_direct_imports" in output["direct_import_surface"]
            assert "logbook_adapter" in output["direct_import_surface"]["tier_b_in_direct_imports"]
            # ok 应为 False（有违规和直接导入表面错误）
            assert output["ok"] is False
        finally:
            file_path.unlink()

    def test_json_output_includes_direct_import_surface(self) -> None:
        """JSON 输出应包含 direct_import_surface 信息"""
        project_root = get_project_root()
        script_path = project_root / "scripts" / "ci" / "check_gateway_public_api_import_surface.py"

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
            env=get_subprocess_env(project_root),
            cwd=str(project_root),
        )

        import json

        output = json.loads(result.stdout)
        assert "direct_import_surface" in output
        assert "is_valid" in output["direct_import_surface"]
        assert "direct_import_modules" in output["direct_import_surface"]
        assert "tier_b_in_direct_imports" in output["direct_import_surface"]
        assert "non_allowlist_in_direct_imports" in output["direct_import_surface"]

    def test_empty_file_has_empty_direct_import_surface(self) -> None:
        """空文件的直接导入表面应为空且有效"""
        content = ""
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            assert result.direct_import_surface.is_valid()
            assert len(result.direct_import_surface.direct_import_modules) == 0
            assert len(result.direct_import_surface.tier_b_in_direct_imports) == 0
            assert len(result.direct_import_surface.non_allowlist_in_direct_imports) == 0
        finally:
            file_path.unlink()

    def test_type_checking_imports_not_in_direct_import_surface(self) -> None:
        """TYPE_CHECKING 块内的导入不应出现在直接导入表面中"""
        content = '''\
"""Test module"""
from __future__ import annotations

from typing import TYPE_CHECKING

# Tier A 直接导入
from .di import RequestContext

# TYPE_CHECKING 块内导入不算直接导入
if TYPE_CHECKING:
    from .logbook_adapter import LogbookAdapter

__version__ = "1.0.0"
'''
        file_path = create_temp_file(content)
        try:
            result = check_public_api_import_surface(file_path)

            # 应无违规（TYPE_CHECKING 内的导入不检查）
            assert not result.has_violations()

            # direct_import_surface 应只包含 di，不包含 logbook_adapter
            assert result.direct_import_surface.is_valid()
            assert "di" in result.direct_import_surface.direct_import_modules
            assert "logbook_adapter" not in result.direct_import_surface.direct_import_modules
        finally:
            file_path.unlink()
