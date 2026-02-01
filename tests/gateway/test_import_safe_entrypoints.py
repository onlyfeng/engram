"""
测试 Gateway 模块的 Import-Safe 特性

验证在没有设置 PROJECT_KEY/POSTGRES_DSN 环境变量的情况下：
1. 导入 engram.gateway.main, app, routes, middleware 模块不抛异常
2. 显式调用 get_container() 仍应抛出 ConfigError（对比测试）

设计原则：
- 模块导入时不应触发 get_config()/get_container()
- 仅在 lifespan startup 或首次请求时才触发配置加载
- 使用 subprocess 进行真正的进程隔离测试，避免 sys.modules 污染

详见 ADR: adr_gateway_di_and_entry_boundary.md
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


def _get_pythonpath() -> str:
    """获取 PYTHONPATH，确保可导入 src/ 目录"""
    repo_root = Path(__file__).parent.parent.parent
    src_path = repo_root / "src"
    return str(src_path)


def _run_import_script(
    script: str, env_vars: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """
    在子进程中执行 Python 脚本

    Args:
        script: 要执行的 Python 脚本内容
        env_vars: 额外的环境变量（默认只保留 PYTHONPATH，不包含 PROJECT_KEY/POSTGRES_DSN）

    Returns:
        subprocess.CompletedProcess 结果
    """
    # 构建干净的环境变量（排除 PROJECT_KEY 和 POSTGRES_DSN）
    clean_env = {k: v for k, v in os.environ.items() if k not in ("PROJECT_KEY", "POSTGRES_DSN")}
    clean_env["PYTHONPATH"] = _get_pythonpath()

    # 添加额外的环境变量
    if env_vars:
        clean_env.update(env_vars)

    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestGatewayLazyLoadSubmodules:
    """
    测试 engram.gateway 的懒加载子模块特性

    验证 import engram.gateway 后 sys.modules 中不应出现子模块，
    仅在访问属性时才触发导入。

    使用 subprocess 进行真正的进程隔离测试。
    """

    def test_import_gateway_does_not_load_submodules(self) -> None:
        """
        测试 import engram.gateway 不加载子模块

        契约：
        - import engram.gateway 后，sys.modules 中不应出现：
          - engram.gateway.logbook_adapter
          - engram.gateway.openmemory_client
          - engram.gateway.outbox_worker
        """
        script = """
        import sys

        # 仅导入 engram.gateway
        import engram.gateway

        # 验证子模块未被加载
        assert "engram.gateway.logbook_adapter" not in sys.modules, \\
            "logbook_adapter 不应在 import engram.gateway 时被加载"
        assert "engram.gateway.openmemory_client" not in sys.modules, \\
            "openmemory_client 不应在 import engram.gateway 时被加载"
        assert "engram.gateway.outbox_worker" not in sys.modules, \\
            "outbox_worker 不应在 import engram.gateway 时被加载"

        # 验证 __version__ 仍可访问
        assert engram.gateway.__version__ == "1.0.0"

        print("OK")
        """
        result = _run_import_script(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_accessing_submodule_triggers_import(self) -> None:
        """
        测试访问子模块属性时才触发导入

        契约：
        - 访问 engram.gateway.logbook_adapter 时才加载该子模块
        - 其他子模块仍保持未加载状态
        """
        script = """
        import sys

        # 仅导入 engram.gateway
        import engram.gateway

        # 验证子模块未被加载
        assert "engram.gateway.logbook_adapter" not in sys.modules

        # 访问 logbook_adapter 属性
        _ = engram.gateway.logbook_adapter

        # 验证 logbook_adapter 已被加载
        assert "engram.gateway.logbook_adapter" in sys.modules

        # 验证其他子模块仍未被加载
        assert "engram.gateway.openmemory_client" not in sys.modules
        assert "engram.gateway.outbox_worker" not in sys.modules

        print("OK")
        """
        result = _run_import_script(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_gateway_all_contains_expected_exports(self) -> None:
        """
        测试 __all__ 包含预期的导出项

        契约：
        - __all__ 应包含 __version__ 和所有子模块名
        """
        script = """
        import engram.gateway

        assert "__version__" in engram.gateway.__all__
        assert "logbook_adapter" in engram.gateway.__all__
        assert "openmemory_client" in engram.gateway.__all__
        assert "outbox_worker" in engram.gateway.__all__

        print("OK")
        """
        result = _run_import_script(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_getattr_raises_for_unknown_attribute(self) -> None:
        """
        测试访问未知属性时抛出 AttributeError

        契约：
        - 访问不存在的属性应抛出 AttributeError
        """
        script = """
        import engram.gateway

        try:
            _ = engram.gateway.nonexistent_module
            raise AssertionError("Should have raised AttributeError")
        except AttributeError as e:
            assert "nonexistent_module" in str(e)

        print("OK")
        """
        result = _run_import_script(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout


class TestImportSafeEntrypoints:
    """
    测试 Gateway 模块的 Import-Safe 特性

    验证在环境变量缺失时，模块导入不抛异常。
    使用 subprocess 进行真正的进程隔离测试。
    """

    def test_import_main_without_env_vars(self) -> None:
        """
        测试 engram.gateway.main 模块导入不依赖环境变量

        契约：
        - from engram.gateway.main import app 不依赖环境变量
        - 仅在 lifespan startup 或首次请求时才触发配置加载
        """
        script = """
        import os

        # 确保环境变量已清除
        assert os.environ.get("PROJECT_KEY") is None
        assert os.environ.get("POSTGRES_DSN") is None

        # 导入模块不应抛出异常
        from engram.gateway import main as main_module
        assert hasattr(main_module, "app")
        assert hasattr(main_module, "create_app")

        print("OK")
        """
        result = _run_import_script(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_import_app_without_env_vars(self) -> None:
        """
        测试 engram.gateway.app 模块导入不依赖环境变量

        契约：
        - create_app() 不传参时不触发 get_config()/get_container()
        """
        script = """
        import os

        # 确保环境变量已清除
        assert os.environ.get("PROJECT_KEY") is None
        assert os.environ.get("POSTGRES_DSN") is None

        # 导入模块不应抛出异常
        from engram.gateway import app as app_module
        assert hasattr(app_module, "create_app")
        assert hasattr(app_module, "GatewayContainer")

        print("OK")
        """
        result = _run_import_script(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_import_routes_without_env_vars(self) -> None:
        """
        测试 engram.gateway.routes 模块导入不依赖环境变量

        契约：
        - 模块导入时不触发 get_config()/get_container()
        - register_routes() 内部保持延迟导入策略
        """
        script = """
        import os

        # 确保环境变量已清除
        assert os.environ.get("PROJECT_KEY") is None
        assert os.environ.get("POSTGRES_DSN") is None

        # 导入模块不应抛出异常
        from engram.gateway import routes as routes_module
        assert hasattr(routes_module, "register_routes")
        assert hasattr(routes_module, "MemoryStoreRequest")

        print("OK")
        """
        result = _run_import_script(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_import_middleware_without_env_vars(self) -> None:
        """
        测试 engram.gateway.middleware 模块导入不依赖环境变量

        契约：
        - 模块导入时不触发 get_config()/get_container()
        - install_middleware() 使用延迟导入
        """
        script = """
        import os

        # 确保环境变量已清除
        assert os.environ.get("PROJECT_KEY") is None
        assert os.environ.get("POSTGRES_DSN") is None

        # 导入模块不应抛出异常
        from engram.gateway import middleware as middleware_module
        assert hasattr(middleware_module, "install_middleware")
        assert hasattr(middleware_module, "CorrelationIdMiddleware")

        print("OK")
        """
        result = _run_import_script(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout


class TestGatewayImportSurfaceContract:
    """
    契约测试：检查 engram.gateway.__init__.py 的 import surface

    验证 __init__.py 不包含 eager-import，确保懒加载策略正确实现。
    这是 CI 检查脚本 check_gateway_import_surface.py 的契约测试。
    """

    def test_no_eager_import_of_logbook_adapter(self) -> None:
        """
        契约测试：__init__.py 不应在模块级别 eager-import logbook_adapter

        规则：
        - TYPE_CHECKING 块内的导入允许（仅用于静态类型提示）
        - 模块级别的 from . import logbook_adapter 禁止
        - 应使用 __getattr__ 实现运行时懒加载
        """
        script = """
        import sys

        # 仅导入 engram.gateway
        import engram.gateway

        assert "engram.gateway.logbook_adapter" not in sys.modules, \\
            "logbook_adapter 不应在 import engram.gateway 时被 eager-import"

        print("OK")
        """
        result = _run_import_script(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_no_eager_import_of_openmemory_client(self) -> None:
        """
        契约测试：__init__.py 不应在模块级别 eager-import openmemory_client
        """
        script = """
        import sys

        import engram.gateway

        assert "engram.gateway.openmemory_client" not in sys.modules, \\
            "openmemory_client 不应在 import engram.gateway 时被 eager-import"

        print("OK")
        """
        result = _run_import_script(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_no_eager_import_of_outbox_worker(self) -> None:
        """
        契约测试：__init__.py 不应在模块级别 eager-import outbox_worker
        """
        script = """
        import sys

        import engram.gateway

        assert "engram.gateway.outbox_worker" not in sys.modules, \\
            "outbox_worker 不应在 import engram.gateway 时被 eager-import"

        print("OK")
        """
        result = _run_import_script(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_init_has_getattr_lazy_loader(self) -> None:
        """
        契约测试：__init__.py 应定义 __getattr__ 函数实现懒加载
        """
        import ast

        init_path = (
            Path(__file__).parent.parent.parent / "src" / "engram" / "gateway" / "__init__.py"
        )
        content = init_path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(init_path))

        has_getattr = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "__getattr__":
                has_getattr = True
                break

        assert has_getattr, "engram.gateway.__init__.py 应定义 __getattr__ 函数实现懒加载"

    def test_init_has_type_checking_guard(self) -> None:
        """
        契约测试：__init__.py 应有 TYPE_CHECKING 块用于静态类型提示
        """
        import ast

        init_path = (
            Path(__file__).parent.parent.parent / "src" / "engram" / "gateway" / "__init__.py"
        )
        content = init_path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(init_path))

        has_type_checking = False
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                if isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
                    has_type_checking = True
                    break

        assert has_type_checking, (
            "engram.gateway.__init__.py 应有 if TYPE_CHECKING: 块用于静态类型提示"
        )


class TestPublicApiErrorCodeExports:
    """
    测试错误码常量的稳定导入

    验证 McpErrorCode/McpErrorCategory/McpErrorReason/ToolResultErrorCode
    可从稳定入口导入，且不依赖环境变量。
    """

    def test_error_code_exports_from_error_codes_module(self) -> None:
        """
        测试错误码常量可从 error_codes 模块导入

        契约：
        - McpErrorCode, McpErrorCategory, McpErrorReason 可从 error_codes 导入
        - 导入不依赖环境变量（import-safe）
        """
        script = """
        import os

        # 确保环境变量已清除（验证 import-safe）
        assert os.environ.get("PROJECT_KEY") is None
        assert os.environ.get("POSTGRES_DSN") is None

        # 从定义模块导入错误码常量
        from engram.gateway.error_codes import (
            McpErrorCode,
            McpErrorCategory,
            McpErrorReason,
        )

        # 验证 McpErrorCode 包含预期常量
        assert hasattr(McpErrorCode, "PARSE_ERROR")
        assert hasattr(McpErrorCode, "INVALID_REQUEST")
        assert hasattr(McpErrorCode, "INTERNAL_ERROR")
        assert McpErrorCode.PARSE_ERROR == -32700

        # 验证 McpErrorCategory 包含预期常量
        assert hasattr(McpErrorCategory, "PROTOCOL")
        assert hasattr(McpErrorCategory, "VALIDATION")
        assert hasattr(McpErrorCategory, "INTERNAL")
        assert McpErrorCategory.PROTOCOL == "protocol"

        # 验证 McpErrorReason 包含预期常量
        assert hasattr(McpErrorReason, "PARSE_ERROR")
        assert hasattr(McpErrorReason, "UNKNOWN_TOOL")
        assert hasattr(McpErrorReason, "INTERNAL_ERROR")
        assert McpErrorReason.UNKNOWN_TOOL == "UNKNOWN_TOOL"

        print("OK")
        """
        result = _run_import_script(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_tool_result_error_code_from_result_error_codes_module(self) -> None:
        """
        测试 ToolResultErrorCode 可从 result_error_codes 模块导入

        契约：
        - ToolResultErrorCode 可从 result_error_codes 导入
        - 导入不依赖环境变量（import-safe）
        """
        script = """
        import os

        # 确保环境变量已清除
        assert os.environ.get("PROJECT_KEY") is None
        assert os.environ.get("POSTGRES_DSN") is None

        from engram.gateway.result_error_codes import ToolResultErrorCode

        # 验证 ToolResultErrorCode 包含预期常量
        assert hasattr(ToolResultErrorCode, "DEPENDENCY_MISSING")
        assert hasattr(ToolResultErrorCode, "MISSING_REQUIRED_PARAM")
        assert ToolResultErrorCode.DEPENDENCY_MISSING == "DEPENDENCY_MISSING"

        print("OK")
        """
        result = _run_import_script(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_error_codes_in_public_api_all(self) -> None:
        """
        测试错误码常量在 public_api.__all__ 中正确声明

        契约：
        - McpErrorCode, McpErrorCategory, McpErrorReason, ToolResultErrorCode
          应在 engram.gateway.public_api.__all__ 中
        """
        import ast

        public_api_path = (
            Path(__file__).parent.parent.parent / "src" / "engram" / "gateway" / "public_api.py"
        )
        content = public_api_path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(public_api_path))

        # 找到 __all__ 定义
        all_list: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__all__":
                        if isinstance(node.value, ast.List):
                            for elt in node.value.elts:
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                    all_list.append(elt.value)

        expected = [
            "McpErrorCode",
            "McpErrorCategory",
            "McpErrorReason",
            "ToolResultErrorCode",
        ]
        for name in expected:
            assert name in all_list, f"{name} should be in public_api.__all__"

    def test_no_circular_import_with_error_codes(self) -> None:
        """
        测试错误码导入不会导致循环导入

        契约：
        - 同时导入错误码和其他模块不应导致循环导入
        """
        script = """
        # 从不同模块导入，验证无循环依赖
        from engram.gateway.error_codes import (
            McpErrorCode,
            McpErrorCategory,
            McpErrorReason,
        )
        from engram.gateway.result_error_codes import ToolResultErrorCode
        from engram.gateway.di import RequestContext, GatewayDepsProtocol

        # 验证所有导入都可用
        assert McpErrorCode.INTERNAL_ERROR == -32603
        assert McpErrorCategory.INTERNAL == "internal"
        assert ToolResultErrorCode.DEPENDENCY_MISSING == "DEPENDENCY_MISSING"

        print("OK")
        """
        result = _run_import_script(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout


class TestPublicApiImportSafe:
    """
    测试 engram.gateway.public_api 模块的 Import-Safe 特性

    验证在无可选依赖（如 pydantic/fastapi）环境下：
    1. Tier A 符号可直接导入，不依赖可选模块
    2. Tier B 符号延迟导入，访问时才触发
    3. `python -c "import engram.gateway.public_api"` 在无可选依赖环境下可成功

    设计原则：
    - Tier A: 纯 Python 类型/Protocol，无可选依赖
    - Tier B: 需要完整依赖（如 pydantic、数据库等）的模块
    """

    def test_import_public_api_without_env_vars(self) -> None:
        """
        测试 engram.gateway.public_api 模块导入不依赖环境变量

        契约：
        - import engram.gateway.public_api 不依赖 PROJECT_KEY/POSTGRES_DSN
        - Tier A 符号可直接访问
        """
        script = """
        import os

        # 确保环境变量已清除
        assert os.environ.get("PROJECT_KEY") is None
        assert os.environ.get("POSTGRES_DSN") is None

        # 导入模块不应抛出异常
        import engram.gateway.public_api

        # 验证 Tier A 核心类型可访问
        assert hasattr(engram.gateway.public_api, "RequestContext")
        assert hasattr(engram.gateway.public_api, "GatewayDeps")
        assert hasattr(engram.gateway.public_api, "GatewayDepsProtocol")
        assert hasattr(engram.gateway.public_api, "generate_correlation_id")

        # 验证 Tier A 便捷函数可访问
        assert hasattr(engram.gateway.public_api, "create_request_context")
        assert hasattr(engram.gateway.public_api, "create_gateway_deps")

        # 验证 Tier A 服务端口 Protocol 可访问
        assert hasattr(engram.gateway.public_api, "WriteAuditPort")
        assert hasattr(engram.gateway.public_api, "UserDirectoryPort")
        assert hasattr(engram.gateway.public_api, "ActorPolicyConfigPort")
        assert hasattr(engram.gateway.public_api, "ToolExecutorPort")

        print("OK")
        """
        result = _run_import_script(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_tier_a_symbols_are_directly_available(self) -> None:
        """
        测试 Tier A 符号在导入后直接可用（非延迟加载）

        契约：
        - RequestContext, GatewayDeps 等 Tier A 符号导入后立即可用
        - 无需触发 __getattr__ 延迟加载
        """
        script = """
        import os

        # 确保环境变量已清除
        assert os.environ.get("PROJECT_KEY") is None
        assert os.environ.get("POSTGRES_DSN") is None

        # 从 public_api 导入 Tier A 符号
        from engram.gateway.public_api import (
            RequestContext,
            GatewayDeps,
            GatewayDepsProtocol,
            generate_correlation_id,
            create_request_context,
            WriteAuditPort,
            ToolCallContext,
            ToolCallResult,
        )

        # 验证这些符号可直接使用
        # generate_correlation_id 应返回格式正确的 ID
        corr_id = generate_correlation_id()
        assert corr_id.startswith("corr-")
        assert len(corr_id) == 21  # corr- (5) + 16 hex chars

        # RequestContext 应可创建测试实例
        ctx = RequestContext.for_testing()
        assert ctx.actor_user_id == "test-user"

        # ToolCallContext 应可实例化
        context = ToolCallContext(
            correlation_id="corr-0000000000000000",
            get_deps=lambda: None,
        )
        assert context.correlation_id == "corr-0000000000000000"

        print("OK")
        """
        result = _run_import_script(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_tier_b_symbols_are_lazy_loaded(self) -> None:
        """
        测试 Tier B 符号延迟加载

        契约：
        - LogbookAdapter 等 Tier B 符号在首次访问时才导入
        - 导入 public_api 后，logbook_adapter 模块不应在 sys.modules 中
        """
        script = """
        import sys

        # 导入 public_api
        import engram.gateway.public_api

        # 验证 Tier B 相关模块未被加载
        assert "engram.gateway.logbook_adapter" not in sys.modules, \\
            "logbook_adapter 不应在 import public_api 时被 eager-import"

        print("OK")
        """
        result = _run_import_script(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_di_module_does_not_import_mcp_rpc(self) -> None:
        """
        测试 di 模块不导入 mcp_rpc

        契约：
        - di.py 中的 generate_correlation_id 是内联实现
        - 导入 di 模块不应触发 mcp_rpc 的加载
        """
        script = """
        import sys

        # 导入 di 模块
        import engram.gateway.di

        # 验证 mcp_rpc 模块未被加载
        # 注意：mcp_rpc 依赖 pydantic，如果被加载则表明 di 不是 import-safe
        assert "engram.gateway.mcp_rpc" not in sys.modules, \\
            "mcp_rpc 不应在 import engram.gateway.di 时被加载"

        # 验证 generate_correlation_id 可用
        from engram.gateway.di import generate_correlation_id
        corr_id = generate_correlation_id()
        assert corr_id.startswith("corr-")

        print("OK")
        """
        result = _run_import_script(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_services_ports_import_safe(self) -> None:
        """
        测试 services.ports 模块 import-safe

        契约：
        - ports.py 只包含 Protocol 定义和简单 dataclass
        - 导入不依赖环境变量或可选模块
        """
        script = """
        import os

        # 确保环境变量已清除
        assert os.environ.get("PROJECT_KEY") is None
        assert os.environ.get("POSTGRES_DSN") is None

        # 导入 ports 模块
        from engram.gateway.services.ports import (
            WriteAuditPort,
            UserDirectoryPort,
            ActorPolicyConfigPort,
            ToolExecutorPort,
            ToolRouterPort,
            ToolDefinition,
            ToolCallContext,
            ToolCallResult,
        )

        # 验证 Protocol 可用于类型注解
        from typing import Protocol
        assert issubclass(type(WriteAuditPort), type(Protocol))
        assert issubclass(type(ToolExecutorPort), type(Protocol))

        # 验证 dataclass/class 可实例化
        tool_def = ToolDefinition(
            name="test_tool",
            description="A test tool",
            inputSchema={"type": "object"},
        )
        assert tool_def.name == "test_tool"

        context = ToolCallContext(
            correlation_id="corr-test123456789a",
            get_deps=lambda: None,
        )
        assert context.correlation_id == "corr-test123456789a"

        result = ToolCallResult(ok=True, result={"data": "test"})
        assert result.ok is True

        print("OK")
        """
        result = _run_import_script(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout


class TestGetContainerRequiresEnvVars:
    """
    对比测试：验证 get_container() 仍需要环境变量

    确保延迟初始化策略正确：
    - 模块导入时不触发配置加载（已在上面测试）
    - 显式调用 get_container() 时触发配置加载，缺少环境变量应抛出 ConfigError

    使用 subprocess 进行真正的进程隔离测试。
    """

    def test_get_container_raises_config_error_without_env_vars(self) -> None:
        """
        测试 get_container() 在缺少环境变量时抛出 ConfigError

        这是对比测试，验证延迟初始化的语义：
        - import 时不报错（上面已测试）
        - 显式调用 get_container() 时报错
        """
        script = """
        import os

        # 确保环境变量已清除
        assert os.environ.get("PROJECT_KEY") is None
        assert os.environ.get("POSTGRES_DSN") is None

        # 导入 container 模块
        from engram.gateway.config import ConfigError
        from engram.gateway.container import get_container

        # 显式调用 get_container() 应抛出 ConfigError
        try:
            get_container()
            raise AssertionError("Should have raised ConfigError")
        except ConfigError as e:
            error_message = str(e)
            assert "PROJECT_KEY" in error_message
            assert "POSTGRES_DSN" in error_message

        print("OK")
        """
        result = _run_import_script(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_get_deps_for_request_raises_config_error_without_env_vars(self) -> None:
        """
        测试 get_deps_for_request() 在缺少环境变量时抛出 ConfigError

        get_deps_for_request() 内部调用 get_container()，应触发相同的错误。
        """
        script = """
        import os

        # 确保环境变量已清除
        assert os.environ.get("PROJECT_KEY") is None
        assert os.environ.get("POSTGRES_DSN") is None

        # 导入依赖模块
        from engram.gateway.config import ConfigError
        from engram.gateway.dependencies import get_deps_for_request

        # 显式调用 get_deps_for_request() 应抛出 ConfigError
        try:
            get_deps_for_request()
            raise AssertionError("Should have raised ConfigError")
        except ConfigError as e:
            error_message = str(e)
            assert "PROJECT_KEY" in error_message
            assert "POSTGRES_DSN" in error_message

        print("OK")
        """
        result = _run_import_script(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout
