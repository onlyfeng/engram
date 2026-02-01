"""
测试 public_api 模块 ImportError 错误消息合约

验证在 Tier B 模块导入失败时，ImportError 的错误消息包含所有必需字段：
- symbol_name: 导入失败的符号名
- module_path: 来源模块路径
- original_error: 原始错误信息
- install_hint: 安装指引（pip install）

测试覆盖所有 _TIER_B_LAZY_IMPORTS 中的符号：
- LogbookAdapter, get_adapter, get_reliability_report → .logbook_adapter
- execute_tool → .entrypoints.tool_executor
- dispatch_jsonrpc_request, JsonRpcDispatchResult → .mcp_rpc

设计原则：
- 使用 subprocess 进行真正的进程隔离测试
- 使用 sys.meta_path BlockingFinder 模拟模块导入失败
- 验证 __cause__ 异常链正确保留

详见 ADR: docs/architecture/gateway_public_api_surface.md
"""

from __future__ import annotations

import textwrap

import pytest

from tests.gateway.helpers.public_api_import_contract_helpers import (
    BLOCKING_LOGBOOK_ADAPTER_CODE,
    TIER_B_SYMBOL_SPECS,
    TierBSymbolSpec,
    get_regex_validation_code,
    make_blocking_finder_code,
    run_subprocess,
)


class TestImportErrorMessageContract:
    """
    测试 ImportError 错误消息格式合约

    验证错误消息同时包含以下必需字段：
    - symbol_name (LogbookAdapter)
    - module_path (.logbook_adapter)
    - install_hint (pip install + [full] 或 engram-logbook)
    - original_error (原因字段)
    """

    def test_logbook_adapter_import_error_message_all_required_fields(self) -> None:
        """
        合约测试：LogbookAdapter 导入错误消息包含所有必需字段

        验证 str(e) 同时包含：
        - LogbookAdapter（符号名）
        - .logbook_adapter（模块路径）
        - pip install（安装指引）
        - [full] 或 engram-logbook（安装选项）
        - "原因"字段（原始错误）
        """
        script = BLOCKING_LOGBOOK_ADAPTER_CODE + textwrap.dedent("""
        try:
            from engram.gateway.public_api import LogbookAdapter
            raise AssertionError("LogbookAdapter 导入应触发 ImportError")
        except ImportError as e:
            error_msg = str(e)

            # 1. 验证包含符号名
            assert "LogbookAdapter" in error_msg, \\
                f"错误消息应包含符号名 'LogbookAdapter': {error_msg}"

            # 2. 验证包含模块路径
            assert ".logbook_adapter" in error_msg, \\
                f"错误消息应包含模块路径 '.logbook_adapter': {error_msg}"

            # 3. 验证包含安装指引 pip install
            assert "pip install" in error_msg, \\
                f"错误消息应包含安装指引 'pip install': {error_msg}"

            # 4. 验证包含安装选项 [full] 或 engram-logbook
            assert "[full]" in error_msg or "engram-logbook" in error_msg, \\
                f"错误消息应包含 '[full]' 或 'engram-logbook': {error_msg}"

            # 5. 验证包含原因/原始错误字段
            assert "原因:" in error_msg or "原因：" in error_msg, \\
                f"错误消息应包含'原因'字段: {error_msg}"

            print("OK: 所有必需字段验证通过")
        """)
        result = run_subprocess(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_logbook_adapter_import_error_cause_contains_blocking_finder(self) -> None:
        """
        合约测试：ImportError.__cause__ 非空且包含 BlockingFinder 痕迹

        验证异常链正确保留原始错误
        """
        script = BLOCKING_LOGBOOK_ADAPTER_CODE + textwrap.dedent("""
        try:
            from engram.gateway.public_api import LogbookAdapter
            raise AssertionError("LogbookAdapter 导入应触发 ImportError")
        except ImportError as e:
            # 1. 验证 __cause__ 非空
            assert e.__cause__ is not None, \\
                "ImportError.__cause__ 应非空以保留原始错误"

            # 2. 验证 __cause__ 包含 BlockingFinder 痕迹
            cause_msg = str(e.__cause__)
            assert "BlockingFinder" in cause_msg, \\
                f"__cause__ 应包含 BlockingFinder 痕迹: {cause_msg}"

            print("OK: __cause__ 验证通过")
        """)
        result = run_subprocess(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_import_error_message_includes_original_error_in_body(self) -> None:
        """
        合约测试：错误消息体包含原始 BlockingFinder 错误信息

        验证 str(e) 本身包含 BlockingFinder 的错误信息（不仅仅是 __cause__）
        """
        script = BLOCKING_LOGBOOK_ADAPTER_CODE + textwrap.dedent("""
        try:
            from engram.gateway.public_api import LogbookAdapter
            raise AssertionError("LogbookAdapter 导入应触发 ImportError")
        except ImportError as e:
            error_msg = str(e)

            # 验证错误消息体包含 BlockingFinder 的错误信息
            assert "BlockingFinder" in error_msg, \\
                f"错误消息应包含 BlockingFinder 原始错误信息: {error_msg}"

            print("OK: 原始错误信息嵌入验证通过")
        """)
        result = run_subprocess(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_import_error_combined_contract(self) -> None:
        """
        综合合约测试：验证所有 ImportError 合约要求

        一次性验证：
        - str(e) 包含所有必需字段
        - e.__cause__ 非空且包含 BlockingFinder 痕迹
        """
        script = BLOCKING_LOGBOOK_ADAPTER_CODE + textwrap.dedent("""
        try:
            from engram.gateway.public_api import LogbookAdapter
            raise AssertionError("LogbookAdapter 导入应触发 ImportError")
        except ImportError as e:
            error_msg = str(e)

            # ============ str(e) 字段验证 ============

            # 字段 1: symbol_name
            assert "LogbookAdapter" in error_msg, \\
                f"应包含符号名: {error_msg}"

            # 字段 2: module_path
            assert ".logbook_adapter" in error_msg, \\
                f"应包含模块路径: {error_msg}"

            # 字段 3: pip install
            assert "pip install" in error_msg, \\
                f"应包含 pip install: {error_msg}"

            # 字段 4: [full] 或 engram-logbook
            assert "[full]" in error_msg or "engram-logbook" in error_msg, \\
                f"应包含安装选项: {error_msg}"

            # 字段 5: 原因字段
            assert "原因:" in error_msg or "原因：" in error_msg, \\
                f"应包含原因字段: {error_msg}"

            # 字段 6: 原始错误信息（BlockingFinder）
            assert "BlockingFinder" in error_msg, \\
                f"应包含原始错误信息: {error_msg}"

            # ============ __cause__ 验证 ============

            # __cause__ 非空
            assert e.__cause__ is not None, \\
                "ImportError.__cause__ 应非空"

            # __cause__ 包含 BlockingFinder
            cause_msg = str(e.__cause__)
            assert "BlockingFinder" in cause_msg, \\
                f"__cause__ 应包含 BlockingFinder: {cause_msg}"

            print("OK: 综合合约验证通过")
        """)
        result = run_subprocess(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout


class TestGetAdapterImportErrorMessageContract:
    """
    测试 get_adapter 的 ImportError 错误消息合约

    与 LogbookAdapter 相同的验证要求
    """

    def test_get_adapter_import_error_message_all_required_fields(self) -> None:
        """
        合约测试：get_adapter 导入错误消息包含所有必需字段
        """
        script = BLOCKING_LOGBOOK_ADAPTER_CODE + textwrap.dedent("""
        try:
            from engram.gateway.public_api import get_adapter
            raise AssertionError("get_adapter 导入应触发 ImportError")
        except ImportError as e:
            error_msg = str(e)

            # 验证所有必需字段
            assert "get_adapter" in error_msg, f"应包含符号名: {error_msg}"
            assert ".logbook_adapter" in error_msg, f"应包含模块路径: {error_msg}"
            assert "pip install" in error_msg, f"应包含安装指引: {error_msg}"
            assert "[full]" in error_msg or "engram-logbook" in error_msg, \\
                f"应包含安装选项: {error_msg}"
            assert "原因:" in error_msg or "原因：" in error_msg, \\
                f"应包含原因字段: {error_msg}"

            # 验证 __cause__
            assert e.__cause__ is not None, "__cause__ 应非空"
            assert "BlockingFinder" in str(e.__cause__), \\
                f"__cause__ 应包含 BlockingFinder: {e.__cause__}"

            print("OK")
        """)
        result = run_subprocess(script)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "OK" in result.stdout


class TestAllTierBSymbolsImportErrorMessageContract:
    """
    测试所有 Tier B 符号的 ImportError 错误消息合约

    对 _TIER_B_LAZY_IMPORTS 中的每个符号进行阻断导入测试，
    验证错误消息包含所有必需字段：symbol_name, module_path, 原因, pip install
    """

    @pytest.mark.parametrize(
        "spec",
        TIER_B_SYMBOL_SPECS,
        ids=[s.symbol_name for s in TIER_B_SYMBOL_SPECS],
    )
    def test_tier_b_symbol_import_error_message_all_required_fields(
        self, spec: TierBSymbolSpec
    ) -> None:
        """
        合约测试：Tier B 符号导入错误消息包含所有必需字段

        验证 str(e) 同时包含：
        - symbol_name（符号名）
        - module_path（模块路径）
        - pip install（安装指引）
        - "原因"字段（原始错误）

        同时验证正则能匹配消息结构且四字段均非空。
        """
        blocking_code = make_blocking_finder_code(spec.blocked_module)
        regex_code = get_regex_validation_code()

        script = (
            blocking_code
            + regex_code
            + textwrap.dedent(f"""
        try:
            from engram.gateway.public_api import {spec.symbol_name}
            raise AssertionError("{spec.symbol_name} 导入应触发 ImportError")
        except ImportError as e:
            error_msg = str(e)

            # 1. 验证包含符号名
            assert "{spec.symbol_name}" in error_msg, \\
                f"错误消息应包含符号名 '{spec.symbol_name}': {{error_msg}}"

            # 2. 验证包含模块路径
            assert "{spec.module_path}" in error_msg, \\
                f"错误消息应包含模块路径 '{spec.module_path}': {{error_msg}}"

            # 3. 验证包含安装指引 pip install
            assert "pip install" in error_msg, \\
                f"错误消息应包含安装指引 'pip install': {{error_msg}}"

            # 4. 验证包含可复制安装选项 [full]（所有 Tier B 符号均需要完整依赖）
            assert "[full]" in error_msg, \\
                f"错误消息应包含可复制安装选项 '[full]': {{error_msg}}"

            # 5. 验证包含原因/原始错误字段
            assert "原因:" in error_msg or "原因：" in error_msg, \\
                f"错误消息应包含'原因'字段: {{error_msg}}"

            # 6. 验证 __cause__ 非空（异常链保留）
            assert e.__cause__ is not None, \\
                "ImportError.__cause__ 应非空以保留原始错误"

            # 7. 验证正则能匹配消息结构且四字段均非空
            fields = parse_error(error_msg)
            assert fields is not None, \\
                f"正则无法匹配错误消息结构: {{error_msg}}"
            assert fields["symbol_name"], \\
                f"symbol_name 字段为空: {{error_msg}}"
            assert fields["module_path"], \\
                f"module_path 字段为空: {{error_msg}}"
            assert fields["original_error"], \\
                f"original_error 字段为空: {{error_msg}}"
            assert fields["install_hint"], \\
                f"install_hint 字段为空: {{error_msg}}"

            print("OK: {spec.symbol_name}")
        """)
        )

        result = run_subprocess(script)
        assert result.returncode == 0, (
            f"Script failed for {spec.symbol_name}:\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
        assert "OK" in result.stdout

    @pytest.mark.parametrize(
        "spec",
        TIER_B_SYMBOL_SPECS,
        ids=[s.symbol_name for s in TIER_B_SYMBOL_SPECS],
    )
    def test_tier_b_symbol_import_error_cause_chain_preserved(self, spec: TierBSymbolSpec) -> None:
        """
        合约测试：Tier B 符号 ImportError.__cause__ 包含 BlockingFinder 痕迹

        验证异常链正确保留原始错误
        """
        blocking_code = make_blocking_finder_code(spec.blocked_module)

        script = blocking_code + textwrap.dedent(f"""
        try:
            from engram.gateway.public_api import {spec.symbol_name}
            raise AssertionError("{spec.symbol_name} 导入应触发 ImportError")
        except ImportError as e:
            # 验证 __cause__ 非空
            assert e.__cause__ is not None, \\
                "ImportError.__cause__ 应非空"

            # 验证 __cause__ 包含 BlockingFinder 痕迹
            cause_msg = str(e.__cause__)
            assert "BlockingFinder" in cause_msg, \\
                f"__cause__ 应包含 BlockingFinder 痕迹: {{cause_msg}}"

            print("OK: {spec.symbol_name}")
        """)

        result = run_subprocess(script)
        assert result.returncode == 0, (
            f"Script failed for {spec.symbol_name}:\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
        assert "OK" in result.stdout


class TestTierBSymbolsMatrixCompleteness:
    """
    测试 TIER_B_SYMBOL_SPECS 测试矩阵与 public_api._TIER_B_LAZY_IMPORTS 的一致性

    确保测试覆盖所有 Tier B 符号
    """

    def test_tier_b_symbol_specs_matches_public_api_mapping(self) -> None:
        """
        验证测试矩阵覆盖 public_api 中所有 Tier B 符号
        """
        script = textwrap.dedent("""
        from engram.gateway.public_api import _TIER_B_LAZY_IMPORTS

        # 预期的测试矩阵（与 TIER_B_SYMBOL_SPECS 一致）
        expected_symbols = {
            "LogbookAdapter",
            "get_adapter",
            "get_reliability_report",
            "execute_tool",
            "dispatch_jsonrpc_request",
            "JsonRpcDispatchResult",
        }

        actual_symbols = set(_TIER_B_LAZY_IMPORTS.keys())

        # 检查测试矩阵是否覆盖所有符号
        missing = actual_symbols - expected_symbols
        extra = expected_symbols - actual_symbols

        if missing:
            print(f"FAIL: 测试矩阵缺失符号: {missing}")
            import sys
            sys.exit(1)

        if extra:
            print(f"WARN: 测试矩阵包含多余符号: {extra}")
            # 不失败，只是警告

        print("OK")
        """)

        result = run_subprocess(script)
        assert result.returncode == 0, (
            f"Script failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "OK" in result.stdout
