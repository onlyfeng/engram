"""
测试 public_api 模块的导入契约

验证在 engram.gateway.logbook_adapter 模块导入失败时：
1. Tier A 符号（RequestContext, GatewayDepsProtocol, McpErrorCode, ToolResultErrorCode）可正常导入
2. Tier B 符号（LogbookAdapter）触发 ImportError，错误文本包含缺失模块名与安装指引

设计原则：
- 使用 subprocess 进行真正的进程隔离测试，避免 sys.modules 污染
- 使用 sys.meta_path blocking finder 模拟 logbook_adapter 模块导入失败
- 确保 public_api 的 Tier A/B 分层策略正确实现

注意：
- engram/__init__.py 在模块级别导入了 engram.logbook，所以不能阻断 engram.logbook
- 正确的测试策略是阻断 engram.gateway.logbook_adapter 模块

详见 ADR: adr_gateway_di_and_entry_boundary.md
"""

from __future__ import annotations

import textwrap

from tests.gateway.helpers.public_api_import_contract_helpers import (
    BLOCKING_LOGBOOK_ADAPTER_CODE,
    run_subprocess,
)


class TestPublicApiTierAImportWithBlockedLogbookAdapter:
    """
    测试 Tier A 符号在 engram.gateway.logbook_adapter 模块不可用时仍可正常导入

    使用 sys.meta_path blocking finder 模拟 logbook_adapter 导入失败。
    验证 public_api 的 Tier A 符号不依赖 logbook_adapter。
    """

    def test_tier_a_symbols_import_with_blocked_logbook_adapter(self) -> None:
        """
        契约测试：Tier A 符号在 logbook_adapter 被阻断时仍可导入

        验证 Tier A 核心符号可正常导入使用。
        """
        script = BLOCKING_LOGBOOK_ADAPTER_CODE + textwrap.dedent("""
        # ============ 验证 Tier A 符号可正常导入 ============
        from engram.gateway.public_api import (
            RequestContext,
            GatewayDepsProtocol,
            McpErrorCode,
            ToolResultErrorCode,
        )

        # 验证符号可用
        assert RequestContext is not None
        assert GatewayDepsProtocol is not None
        assert McpErrorCode is not None
        assert ToolResultErrorCode is not None

        # 验证 RequestContext 可创建测试实例
        ctx = RequestContext.for_testing()
        assert ctx.actor_user_id == "test-user"

        # 验证错误码常量可访问
        assert McpErrorCode.PARSE_ERROR == -32700
        assert ToolResultErrorCode.DEPENDENCY_MISSING == "DEPENDENCY_MISSING"

        print("OK")
        """)
        result = run_subprocess(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_tier_a_additional_symbols_with_blocked_logbook_adapter(self) -> None:
        """
        契约测试：更多 Tier A 符号在 logbook_adapter 被阻断时仍可导入
        """
        script = BLOCKING_LOGBOOK_ADAPTER_CODE + textwrap.dedent("""
        from engram.gateway.public_api import (
            GatewayDeps,
            generate_correlation_id,
            create_request_context,
            WriteAuditPort,
            UserDirectoryPort,
            ActorPolicyConfigPort,
            ToolExecutorPort,
            ToolCallContext,
            ToolCallResult,
            McpErrorCategory,
            McpErrorReason,
        )

        # 验证 generate_correlation_id 可用
        corr_id = generate_correlation_id()
        assert corr_id.startswith("corr-")
        assert len(corr_id) == 21  # corr- (5) + 16 hex chars

        # 验证 ToolCallContext 可实例化
        context = ToolCallContext(
            correlation_id="corr-0000000000000000",
            get_deps=lambda: None,
        )
        assert context.correlation_id == "corr-0000000000000000"

        # 验证 ToolCallResult 可实例化
        result = ToolCallResult(ok=True, result={"data": "test"})
        assert result.ok is True

        # 验证错误码常量
        assert McpErrorCategory.PROTOCOL == "protocol"
        assert McpErrorReason.UNKNOWN_TOOL == "UNKNOWN_TOOL"

        print("OK")
        """)
        result = run_subprocess(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout


class TestPublicApiTierBImportFailureWithBlockedLogbookAdapter:
    """
    测试 Tier B 符号在 engram.gateway.logbook_adapter 模块不可用时触发 ImportError

    验证 public_api 的延迟导入策略：
    - Tier B 符号在首次访问时才触发导入
    - 导入失败时抛出 ImportError，包含缺失模块名和安装指引
    """

    def test_logbook_adapter_import_raises_with_blocked_module(self) -> None:
        """
        契约测试：LogbookAdapter 在 logbook_adapter 被阻断时触发 ImportError
        """
        script = BLOCKING_LOGBOOK_ADAPTER_CODE + textwrap.dedent("""
        import engram.gateway.public_api

        # 验证 Tier A 符号可访问
        assert hasattr(engram.gateway.public_api, "RequestContext")

        # ============ 验证 LogbookAdapter 触发 ImportError ============
        try:
            from engram.gateway.public_api import LogbookAdapter
            raise AssertionError("LogbookAdapter 导入应触发 ImportError")
        except ImportError as e:
            error_msg = str(e)
            assert "LogbookAdapter" in error_msg, f"错误应提及 LogbookAdapter: {error_msg}"
            assert "logbook_adapter" in error_msg, f"错误应提及 logbook_adapter 模块: {error_msg}"

        print("OK")
        """)
        result = run_subprocess(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_get_adapter_import_raises_with_blocked_module(self) -> None:
        """
        契约测试：get_adapter 在 logbook_adapter 被阻断时触发 ImportError
        """
        script = BLOCKING_LOGBOOK_ADAPTER_CODE + textwrap.dedent("""
        import engram.gateway.public_api

        try:
            from engram.gateway.public_api import get_adapter
            raise AssertionError("get_adapter 导入应触发 ImportError")
        except ImportError as e:
            error_msg = str(e)
            assert "get_adapter" in error_msg, f"错误应提及 get_adapter: {error_msg}"
            assert "logbook_adapter" in error_msg, f"错误应提及 logbook_adapter: {error_msg}"

        print("OK")
        """)
        result = run_subprocess(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_tier_b_symbols_lazy_load_isolation(self) -> None:
        """
        契约测试：Tier B 符号延迟加载不影响 Tier A 符号使用
        """
        script = BLOCKING_LOGBOOK_ADAPTER_CODE + textwrap.dedent("""
        from engram.gateway.public_api import (
            RequestContext,
            GatewayDepsProtocol,
            McpErrorCode,
        )

        # 尝试导入 Tier B（失败）
        import_error_count = 0
        try:
            from engram.gateway.public_api import LogbookAdapter
        except ImportError:
            import_error_count += 1

        try:
            from engram.gateway.public_api import get_adapter
        except ImportError:
            import_error_count += 1

        assert import_error_count == 2, f"两个 Tier B 符号都应导入失败，实际: {import_error_count}"

        # 验证 Tier A 符号仍可使用
        ctx = RequestContext.for_testing()
        assert ctx.actor_user_id == "test-user"
        assert McpErrorCode.INTERNAL_ERROR == -32603

        print("OK")
        """)
        result = run_subprocess(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout


class TestPublicApiImportErrorMessageQuality:
    """
    测试 ImportError 错误消息的质量
    """

    def test_import_error_message_contains_module_name(self) -> None:
        """
        契约测试：ImportError 错误消息包含模块名
        """
        script = BLOCKING_LOGBOOK_ADAPTER_CODE + textwrap.dedent("""
        try:
            from engram.gateway.public_api import LogbookAdapter
            raise AssertionError("应抛出 ImportError")
        except ImportError as e:
            error_msg = str(e)

            # 验证错误消息包含关键信息
            assert "LogbookAdapter" in error_msg, f"应提及 LogbookAdapter: {error_msg}"
            assert "logbook_adapter" in error_msg, f"应提及 logbook_adapter: {error_msg}"

        print("OK")
        """)
        result = run_subprocess(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_import_error_preserves_original_cause(self) -> None:
        """
        契约测试：ImportError 保留原始错误原因
        """
        script = BLOCKING_LOGBOOK_ADAPTER_CODE + textwrap.dedent("""
        try:
            from engram.gateway.public_api import LogbookAdapter
            raise AssertionError("应抛出 ImportError")
        except ImportError as e:
            assert e.__cause__ is not None, "ImportError 应有 __cause__"
            cause_msg = str(e.__cause__)
            # 原始错误应来自 BlockingFinder
            assert "BlockingFinder" in cause_msg or "logbook_adapter" in cause_msg, \\
                f"原始错误应被保留: {cause_msg}"

        print("OK")
        """)
        result = run_subprocess(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_import_error_message_contains_install_hint(self) -> None:
        """
        契约测试：ImportError 错误消息包含安装指引
        """
        script = BLOCKING_LOGBOOK_ADAPTER_CODE + textwrap.dedent("""
        try:
            from engram.gateway.public_api import LogbookAdapter
            raise AssertionError("应抛出 ImportError")
        except ImportError as e:
            error_msg = str(e)

            # 验证错误消息包含安装指引
            assert "pip install" in error_msg, f"应包含 pip install 指引: {error_msg}"
            # 验证包含 full 安装选项或 engram-logbook
            assert "[full]" in error_msg or "engram-logbook" in error_msg, \\
                f"应包含 [full] 或 engram-logbook 安装选项: {error_msg}"

        print("OK")
        """)
        result = run_subprocess(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_import_error_message_contains_original_error(self) -> None:
        """
        契约测试：ImportError 错误消息包含原始错误信息
        """
        script = BLOCKING_LOGBOOK_ADAPTER_CODE + textwrap.dedent("""
        try:
            from engram.gateway.public_api import LogbookAdapter
            raise AssertionError("应抛出 ImportError")
        except ImportError as e:
            error_msg = str(e)

            # 验证错误消息包含"原因"字段
            assert "原因:" in error_msg or "原因：" in error_msg, \\
                f"应包含'原因'字段: {error_msg}"
            # 验证包含 BlockingFinder 的错误信息（来自原始 ImportError）
            assert "BlockingFinder" in error_msg, \\
                f"应包含原始错误信息 (BlockingFinder): {error_msg}"

        print("OK")
        """)
        result = run_subprocess(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_import_error_template_all_required_fields(self) -> None:
        """
        契约测试：ImportError 错误消息模板包含所有必需字段

        验证错误消息包含：
        - symbol_name: 导入失败的符号名
        - module_path: 来源模块路径
        - original_error: 原始错误信息
        - install_hint: 安装指引
        """
        script = BLOCKING_LOGBOOK_ADAPTER_CODE + textwrap.dedent("""
        try:
            from engram.gateway.public_api import get_adapter
            raise AssertionError("应抛出 ImportError")
        except ImportError as e:
            error_msg = str(e)

            # 必需字段 1: symbol_name（符号名）
            assert "get_adapter" in error_msg, f"应包含符号名 get_adapter: {error_msg}"

            # 必需字段 2: module_path（模块路径）
            assert ".logbook_adapter" in error_msg, f"应包含模块路径: {error_msg}"

            # 必需字段 3: original_error（原始错误）
            # BlockingFinder 抛出的错误会被包含
            assert "BlockingFinder" in error_msg or "engram_logbook" in error_msg, \\
                f"应包含原始错误信息: {error_msg}"

            # 必需字段 4: install_hint（安装指引）
            assert "pip install" in error_msg, f"应包含安装指引: {error_msg}"

        print("OK")
        """)
        result = run_subprocess(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout


class TestPublicApiMcpRpcTierBImport:
    """
    测试 mcp_rpc 模块的 Tier B 符号导入行为

    验证 dispatch_jsonrpc_request 和 JsonRpcDispatchResult 可通过 public_api 正常导入。
    这些符号不依赖 engram_logbook，应在正常环境下可导入。
    """

    def test_dispatch_jsonrpc_request_importable(self) -> None:
        """
        契约测试：dispatch_jsonrpc_request 可正常导入
        """
        script = textwrap.dedent("""
        from engram.gateway.public_api import dispatch_jsonrpc_request

        # 验证是异步函数
        import asyncio
        assert asyncio.iscoroutinefunction(dispatch_jsonrpc_request), \\
            "dispatch_jsonrpc_request 应是异步函数"

        print("OK")
        """)
        result = run_subprocess(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_jsonrpc_dispatch_result_importable(self) -> None:
        """
        契约测试：JsonRpcDispatchResult 可正常导入
        """
        script = textwrap.dedent("""
        from engram.gateway.public_api import JsonRpcDispatchResult

        # 验证是类
        assert isinstance(JsonRpcDispatchResult, type), \\
            "JsonRpcDispatchResult 应是类"

        # 验证有 response 和 correlation_id 字段
        # 使用 model_fields 检查 Pydantic 字段
        assert hasattr(JsonRpcDispatchResult, "model_fields"), \\
            "JsonRpcDispatchResult 应是 Pydantic 模型"
        assert "response" in JsonRpcDispatchResult.model_fields, \\
            "JsonRpcDispatchResult 应有 response 字段"
        assert "correlation_id" in JsonRpcDispatchResult.model_fields, \\
            "JsonRpcDispatchResult 应有 correlation_id 字段"

        print("OK")
        """)
        result = run_subprocess(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_mcp_rpc_symbols_do_not_affect_tier_a(self) -> None:
        """
        契约测试：mcp_rpc 符号的导入不影响 Tier A 符号可用性
        """
        script = textwrap.dedent("""
        # 先导入 Tier A 符号
        from engram.gateway.public_api import (
            RequestContext,
            GatewayDepsProtocol,
            McpErrorCode,
        )

        # 再导入 mcp_rpc Tier B 符号
        from engram.gateway.public_api import (
            dispatch_jsonrpc_request,
            JsonRpcDispatchResult,
        )

        # 验证 Tier A 符号仍可使用
        ctx = RequestContext.for_testing()
        assert ctx.actor_user_id == "test-user"
        assert McpErrorCode.PARSE_ERROR == -32700

        print("OK")
        """)
        result = run_subprocess(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_jsonrpc_dispatch_result_has_to_dict_and_http_status(self) -> None:
        """
        契约测试：JsonRpcDispatchResult 有 to_dict 方法和 http_status 属性
        """
        script = textwrap.dedent("""
        from engram.gateway.public_api import JsonRpcDispatchResult

        # 验证有 to_dict 方法（callable）
        assert callable(getattr(JsonRpcDispatchResult, 'to_dict', None)), \\
            "JsonRpcDispatchResult 应有 to_dict 方法"

        # 验证有 http_status 属性（property）
        # Pydantic 模型的 property 可以通过 __dict__ 或 dir() 检查
        assert 'http_status' in dir(JsonRpcDispatchResult), \\
            "JsonRpcDispatchResult 应有 http_status 属性"

        print("OK")
        """)
        result = run_subprocess(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_dispatch_jsonrpc_request_returns_result_with_http_methods(self) -> None:
        """
        契约测试：dispatch_jsonrpc_request 返回的结果有 to_dict 和 http_status
        """
        script = textwrap.dedent("""
        import asyncio
        from engram.gateway.public_api import dispatch_jsonrpc_request

        async def test():
            result = await dispatch_jsonrpc_request({
                "jsonrpc": "2.0",
                "method": "tools/list",
                "id": 1,
            })

            # 验证 http_status
            assert isinstance(result.http_status, int), \\
                f"http_status 应是 int，实际是 {type(result.http_status)}"
            assert result.http_status == 200, \\
                f"成功响应的 http_status 应是 200，实际是 {result.http_status}"

            # 验证 to_dict
            d = result.to_dict()
            assert isinstance(d, dict), f"to_dict 应返回 dict，实际是 {type(d)}"
            assert "jsonrpc" in d, "to_dict 结果应包含 jsonrpc"
            assert d["jsonrpc"] == "2.0"

            print("OK")

        asyncio.run(test())
        """)
        result = run_subprocess(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout


class TestPublicApiModuleLevelImportSafe:
    """
    测试 public_api 模块级别的 import-safe 特性
    """

    def test_module_import_succeeds_without_logbook_adapter(self) -> None:
        """
        契约测试：import engram.gateway.public_api 在 logbook_adapter 被阻断时成功
        """
        script = BLOCKING_LOGBOOK_ADAPTER_CODE + textwrap.dedent("""
        import engram.gateway.public_api

        assert hasattr(engram.gateway.public_api, "__all__")
        assert "RequestContext" in engram.gateway.public_api.__all__
        assert "LogbookAdapter" in engram.gateway.public_api.__all__

        # 验证 logbook_adapter 模块未被加载
        assert "engram.gateway.logbook_adapter" not in sys.modules, \\
            "logbook_adapter 不应在 import public_api 时被加载"

        print("OK")
        """)
        result = run_subprocess(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_public_api_tier_a_accessible_tier_b_blocked(self) -> None:
        """
        契约测试：Tier A 符号可访问，logbook 相关 Tier B 符号被阻断
        """
        script = BLOCKING_LOGBOOK_ADAPTER_CODE + textwrap.dedent("""
        TIER_A_SYMBOLS = [
            "RequestContext",
            "GatewayDeps",
            "GatewayDepsProtocol",
            "create_request_context",
            "create_gateway_deps",
            "generate_correlation_id",
            "WriteAuditPort",
            "UserDirectoryPort",
            "ActorPolicyConfigPort",
            "ToolExecutorPort",
            "ToolRouterPort",
            "ToolDefinition",
            "ToolCallContext",
            "ToolCallResult",
            "McpErrorCode",
            "McpErrorCategory",
            "McpErrorReason",
            "ToolResultErrorCode",
        ]

        # logbook_adapter 相关的 Tier B 符号
        TIER_B_LOGBOOK_SYMBOLS = [
            "LogbookAdapter",
            "get_adapter",
            "get_reliability_report",
        ]

        # mcp_rpc 相关的 Tier B 符号（不依赖 engram_logbook，应可访问）
        TIER_B_MCP_RPC_SYMBOLS = [
            "dispatch_jsonrpc_request",
            "JsonRpcDispatchResult",
        ]

        import engram.gateway.public_api as public_api

        # 验证 Tier A 符号全部可访问
        for name in TIER_A_SYMBOLS:
            obj = getattr(public_api, name, None)
            assert obj is not None, f"Tier A 符号 {name} 应可访问"

        # 验证 mcp_rpc 相关 Tier B 符号可访问（不依赖 engram_logbook）
        for name in TIER_B_MCP_RPC_SYMBOLS:
            obj = getattr(public_api, name, None)
            assert obj is not None, f"Tier B mcp_rpc 符号 {name} 应可访问"

        # 验证 logbook_adapter 相关 Tier B 符号触发 ImportError
        for name in TIER_B_LOGBOOK_SYMBOLS:
            try:
                getattr(public_api, name)
                print(f"FAIL: Tier B 符号 {name} 应触发 ImportError")
                import sys
                sys.exit(1)
            except ImportError:
                pass  # 预期行为

        print("OK")
        """)
        result = run_subprocess(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout


class TestPublicApiImportContractWithSysModulesPatch:
    """
    测试使用 sys.modules patch 阻断 logbook_adapter 模块的场景
    """

    def test_tier_a_with_patched_sys_modules(self) -> None:
        """
        契约测试：使用 sys.modules patch 模拟 logbook_adapter 导入失败
        """
        script = textwrap.dedent("""
        import sys

        # 创建一个在导入时抛出 ImportError 的模块占位
        class FailingModule:
            def __getattr__(self, name):
                raise ImportError("logbook_adapter 模块不可用（测试模拟）")

        # 在 sys.modules 中预置失败模块
        sys.modules['engram.gateway.logbook_adapter'] = FailingModule()

        # 验证 Tier A 符号可导入
        from engram.gateway.public_api import (
            RequestContext,
            GatewayDepsProtocol,
            McpErrorCode,
            ToolResultErrorCode,
        )

        # 验证可使用
        ctx = RequestContext.for_testing()
        assert ctx.correlation_id.startswith("corr-")
        assert McpErrorCode.PARSE_ERROR == -32700

        print("OK")
        """)
        result = run_subprocess(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout
