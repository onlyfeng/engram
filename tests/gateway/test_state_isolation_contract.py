# -*- coding: utf-8 -*-
"""
Gateway 状态隔离契约测试

验证 conftest.py 中 auto_reset_gateway_state fixture 正确清理所有运行时状态。

================================================================================
                            测试设计说明
================================================================================

这些测试用例按顺序执行（pytest 默认按定义顺序），每组由两个测试组成：
1. 第一个测试设置某个状态（但不主动清理）
2. 第二个测试验证该状态已被 autouse fixture 清理

通过这种设计，我们可以验证 auto_reset_gateway_state fixture 的 teardown 阶段
正确调用了 reset_gateway_runtime_state()。

状态隔离覆盖范围：
1. mcp_rpc._current_correlation_id (ContextVar)
2. mcp_rpc._tool_executor (全局变量)
3. middleware._request_correlation_id (ContextVar)
4. engram.gateway 懒加载缓存 (模块命名空间属性)

注意：
- 这些测试在同一进程/同一 event loop 下执行
- 测试顺序由命名控制 (test_01_*, test_02_* 等)
- 不应单独运行某个测试，需要整个模块一起执行以验证隔离效果

================================================================================
"""

# ===================== Group 1: mcp_rpc correlation_id 隔离测试 =====================


def test_01_mcp_rpc_correlation_id_set_without_reset():
    """
    设置 mcp_rpc correlation_id，不主动 reset token

    此测试设置 correlation_id 但不调用 reset，依赖 autouse fixture
    在 teardown 阶段清理状态。下一个测试将验证清理效果。
    """
    from engram.gateway.mcp_rpc import (
        get_current_correlation_id,
        set_current_correlation_id,
    )

    # 设置一个有效的 correlation_id
    test_corr_id = "corr-test01abcdef1234"
    token = set_current_correlation_id(test_corr_id)

    # 验证设置成功
    assert get_current_correlation_id() == test_corr_id

    # 故意不调用 _current_correlation_id.reset(token)
    # 依赖 auto_reset_gateway_state fixture 的 teardown 清理
    _ = token  # 保留 token 引用但不使用


def test_02_mcp_rpc_correlation_id_should_be_none_after_previous_test():
    """
    验证 mcp_rpc correlation_id 已被 autouse fixture 清理

    此测试验证 auto_reset_gateway_state fixture 的 teardown 阶段
    正确调用了 reset_current_correlation_id_for_testing()。
    """
    from engram.gateway.mcp_rpc import get_current_correlation_id

    # 上一个测试设置了 correlation_id，但 autouse fixture 应该已清理
    current_corr_id = get_current_correlation_id()
    assert current_corr_id is None, (
        f"状态隔离失败: mcp_rpc correlation_id 应为 None，但实际为 {current_corr_id!r}\n"
        "这表明 auto_reset_gateway_state fixture 的 teardown 未正确清理 ContextVar 状态"
    )


# ===================== Group 2: mcp_rpc tool_executor 隔离测试 =====================


def test_03_mcp_rpc_tool_executor_register():
    """
    注册 mcp_rpc tool_executor，不主动清理

    此测试注册一个工具执行器但不调用 reset，依赖 autouse fixture
    在 teardown 阶段清理状态。下一个测试将验证清理效果。
    """
    from engram.gateway.mcp_rpc import get_tool_executor, register_tool_executor

    # 定义一个简单的工具执行器
    async def fake_executor(tool_name: str, tool_args: dict, *, correlation_id: str):
        return {"ok": True, "tool": tool_name}

    # 注册工具执行器
    register_tool_executor(fake_executor)

    # 验证注册成功
    executor = get_tool_executor()
    assert executor is not None
    assert executor is fake_executor

    # 故意不调用 reset_tool_executor_for_testing()
    # 依赖 auto_reset_gateway_state fixture 的 teardown 清理


def test_04_mcp_rpc_tool_executor_should_be_none_after_previous_test():
    """
    验证 mcp_rpc tool_executor 已被 autouse fixture 清理

    此测试验证 auto_reset_gateway_state fixture 的 teardown 阶段
    正确调用了 reset_tool_executor_for_testing()。
    """
    from engram.gateway.mcp_rpc import get_tool_executor

    # 上一个测试注册了 tool_executor，但 autouse fixture 应该已清理
    executor = get_tool_executor()
    assert executor is None, (
        f"状态隔离失败: mcp_rpc tool_executor 应为 None，但实际为 {executor!r}\n"
        "这表明 auto_reset_gateway_state fixture 的 teardown 未正确清理全局执行器"
    )


# ===================== Group 3: middleware correlation_id 隔离测试 =====================


def test_05_middleware_correlation_id_set_without_reset():
    """
    设置 middleware correlation_id，不主动 reset token

    此测试设置 middleware 的 correlation_id 但不调用 reset，依赖 autouse fixture
    在 teardown 阶段清理状态。下一个测试将验证清理效果。
    """
    from engram.gateway.middleware import (
        get_request_correlation_id,
        set_request_correlation_id,
    )

    # 设置一个有效的 correlation_id
    test_corr_id = "corr-test05abcdef5678"
    token = set_request_correlation_id(test_corr_id)

    # 验证设置成功
    assert get_request_correlation_id() == test_corr_id

    # 故意不调用 _request_correlation_id.reset(token)
    # 依赖 auto_reset_gateway_state fixture 的 teardown 清理
    _ = token  # 保留 token 引用但不使用


def test_06_middleware_correlation_id_should_be_none_after_previous_test():
    """
    验证 middleware correlation_id 已被 autouse fixture 清理

    此测试验证 auto_reset_gateway_state fixture 的 teardown 阶段
    正确调用了 reset_request_correlation_id_for_testing()。
    """
    from engram.gateway.middleware import get_request_correlation_id

    # 上一个测试设置了 correlation_id，但 autouse fixture 应该已清理
    current_corr_id = get_request_correlation_id()
    assert current_corr_id is None, (
        f"状态隔离失败: middleware correlation_id 应为 None，但实际为 {current_corr_id!r}\n"
        "这表明 auto_reset_gateway_state fixture 的 teardown 未正确清理 ContextVar 状态"
    )


# ===================== Group 4: 综合隔离测试（同时设置多个状态）=====================


def test_07_set_all_states_simultaneously():
    """
    同时设置所有状态，验证综合隔离能力

    此测试同时设置 mcp_rpc 和 middleware 的多个状态，验证 autouse fixture
    能够正确处理多个状态的同时清理。
    """
    from engram.gateway.mcp_rpc import (
        get_current_correlation_id,
        get_tool_executor,
        register_tool_executor,
        set_current_correlation_id,
    )
    from engram.gateway.middleware import (
        get_request_correlation_id,
        set_request_correlation_id,
    )

    # 设置 mcp_rpc correlation_id
    mcp_corr_id = "corr-test07mcp12345678"
    mcp_token = set_current_correlation_id(mcp_corr_id)
    assert get_current_correlation_id() == mcp_corr_id

    # 设置 middleware correlation_id
    middleware_corr_id = "corr-test07mid12345678"
    middleware_token = set_request_correlation_id(middleware_corr_id)
    assert get_request_correlation_id() == middleware_corr_id

    # 注册 tool_executor
    async def combined_executor(tool_name: str, tool_args: dict, *, correlation_id: str):
        return {"combined": True}

    register_tool_executor(combined_executor)
    assert get_tool_executor() is combined_executor

    # 保留 token 引用但不使用
    _ = mcp_token
    _ = middleware_token


def test_08_all_states_should_be_none_after_combined_test():
    """
    验证所有状态都已被 autouse fixture 清理

    此测试验证 auto_reset_gateway_state fixture 能够正确处理
    多个状态的同时清理。
    """
    from engram.gateway.mcp_rpc import get_current_correlation_id, get_tool_executor
    from engram.gateway.middleware import get_request_correlation_id

    # 验证 mcp_rpc correlation_id 已清理
    mcp_corr_id = get_current_correlation_id()
    assert mcp_corr_id is None, (
        f"状态隔离失败: mcp_rpc correlation_id 应为 None，但实际为 {mcp_corr_id!r}"
    )

    # 验证 middleware correlation_id 已清理
    middleware_corr_id = get_request_correlation_id()
    assert middleware_corr_id is None, (
        f"状态隔离失败: middleware correlation_id 应为 None，但实际为 {middleware_corr_id!r}"
    )

    # 验证 tool_executor 已清理
    executor = get_tool_executor()
    assert executor is None, f"状态隔离失败: tool_executor 应为 None，但实际为 {executor!r}"


# ===================== Group 5: 边界情况测试 =====================


def test_09_repeated_set_same_contextvar():
    """
    测试重复设置同一 ContextVar 的情况

    验证在同一测试中多次设置 ContextVar 后，autouse fixture
    仍能正确清理到初始状态。
    """
    from engram.gateway.mcp_rpc import (
        get_current_correlation_id,
        set_current_correlation_id,
    )

    # 第一次设置
    first_corr_id = "corr-test09first12345"
    token1 = set_current_correlation_id(first_corr_id)
    assert get_current_correlation_id() == first_corr_id

    # 第二次设置（不 reset 第一次的 token）
    second_corr_id = "corr-test09second1234"
    token2 = set_current_correlation_id(second_corr_id)
    assert get_current_correlation_id() == second_corr_id

    # 第三次设置
    third_corr_id = "corr-test09third12345"
    token3 = set_current_correlation_id(third_corr_id)
    assert get_current_correlation_id() == third_corr_id

    # 保留所有 token 引用但不使用
    _ = token1
    _ = token2
    _ = token3


def test_10_contextvar_should_be_none_after_repeated_sets():
    """
    验证重复设置后状态仍被正确清理

    即使 ContextVar 被多次覆盖设置，autouse fixture 的
    reset_current_correlation_id_for_testing() 仍应将其重置为 None。
    """
    from engram.gateway.mcp_rpc import get_current_correlation_id

    current_corr_id = get_current_correlation_id()
    assert current_corr_id is None, (
        f"状态隔离失败: 多次设置后 correlation_id 应为 None，但实际为 {current_corr_id!r}\n"
        "这表明 reset_current_correlation_id_for_testing() 无法正确重置多次设置的 ContextVar"
    )


# ===================== Group 6: 独立测试（不依赖顺序）=====================


class TestStateIsolationIndependent:
    """
    独立的状态隔离测试类

    这些测试不依赖其他测试的执行顺序，每个测试在 setup 时状态应已是干净的。
    用于验证 autouse fixture 的 setup 阶段也正确执行了重置。
    """

    def test_initial_mcp_rpc_correlation_id_is_none(self):
        """验证测试开始时 mcp_rpc correlation_id 为 None"""
        from engram.gateway.mcp_rpc import get_current_correlation_id

        assert get_current_correlation_id() is None

    def test_initial_middleware_correlation_id_is_none(self):
        """验证测试开始时 middleware correlation_id 为 None"""
        from engram.gateway.middleware import get_request_correlation_id

        assert get_request_correlation_id() is None

    def test_initial_tool_executor_is_none(self):
        """验证测试开始时 tool_executor 为 None"""
        from engram.gateway.mcp_rpc import get_tool_executor

        assert get_tool_executor() is None


# ===================== Group 7: 懒加载缓存隔离测试 =====================


def test_11_lazy_import_cache_set_without_reset():
    """
    触发 engram.gateway 懒加载缓存，不主动清理

    此测试访问 engram.gateway.logbook_adapter 触发懒加载缓存，依赖 autouse fixture
    在 teardown 阶段清理状态。下一个测试将验证清理效果。
    """
    import sys

    import engram.gateway

    # 触发懒加载，缓存模块到 engram.gateway 命名空间
    _ = engram.gateway.logbook_adapter

    # 验证缓存已设置（在模块的 __dict__ 中）
    gateway_module = sys.modules["engram.gateway"]
    assert "logbook_adapter" in gateway_module.__dict__, (
        "懒加载缓存未生效: logbook_adapter 应在 engram.gateway.__dict__ 中"
    )

    # 记录当前缓存的模块对象 id，供下一个测试参考
    # （虽然我们主要验证缓存是否被清除，而非 id 变化）


def test_12_lazy_import_cache_should_be_cleared_after_previous_test():
    """
    验证 engram.gateway 懒加载缓存已被 autouse fixture 清理

    此测试验证 auto_reset_gateway_state fixture 的 teardown 阶段
    正确调用了 _reset_gateway_lazy_import_cache_for_testing()。
    """
    import sys

    gateway_module = sys.modules.get("engram.gateway")
    assert gateway_module is not None, "engram.gateway 模块应该仍在 sys.modules 中"

    # 验证懒加载缓存已被清除
    assert "logbook_adapter" not in gateway_module.__dict__, (
        "状态隔离失败: logbook_adapter 不应在 engram.gateway.__dict__ 中\n"
        "这表明 auto_reset_gateway_state fixture 的 teardown 未正确清理懒加载缓存"
    )


# ===================== Group 8: config 单例隔离测试 =====================


def test_13_config_singleton_set_without_reset():
    """
    通过 override_config() 设置 config 单例，不主动 reset

    此测试设置 config 单例但不调用 reset_config()，依赖 autouse fixture
    在 teardown 阶段清理状态。下一个测试将验证清理效果。
    """
    from engram.gateway.config import (
        GatewayConfig,
        get_config_or_none,
        override_config,
    )

    # 创建测试配置
    test_config = GatewayConfig(
        project_key="test-isolation-project",
        postgres_dsn="postgresql://test@localhost/test_isolation",
        openmemory_base_url="http://test-isolation:8080",
    )

    # 使用 override_config 设置单例
    override_config(test_config)

    # 验证设置成功
    assert get_config_or_none() is not None
    assert get_config_or_none() is test_config
    assert get_config_or_none().project_key == "test-isolation-project"

    # 故意不调用 reset_config()
    # 依赖 auto_reset_gateway_state fixture 的 teardown 清理


def test_14_config_singleton_should_be_none_after_previous_test():
    """
    验证 config 单例已被 autouse fixture 清理

    此测试验证 auto_reset_gateway_state fixture 的 teardown 阶段
    正确调用了 reset_config()。
    """
    from engram.gateway.config import get_config_or_none

    # 上一个测试设置了 config 单例，但 autouse fixture 应该已清理
    current_config = get_config_or_none()
    assert current_config is None, (
        f"状态隔离失败: config 单例应为 None，但实际为 {current_config!r}\n"
        "这表明 auto_reset_gateway_state fixture 的 teardown 未正确清理 _config 全局变量"
    )


# ===================== Group 9: container 单例隔离测试 =====================


def test_15_container_singleton_set_without_reset():
    """
    通过 set_container() 设置 container 单例，不主动 reset

    此测试设置 container 单例但不调用 reset_container()，依赖 autouse fixture
    在 teardown 阶段清理状态。下一个测试将验证清理效果。
    """
    from engram.gateway.config import GatewayConfig
    from engram.gateway.container import (
        GatewayContainer,
        get_container_or_none,
        set_container,
    )

    # 创建测试配置（避免触发 load_config 读取环境变量）
    test_config = GatewayConfig(
        project_key="test-container-isolation",
        postgres_dsn="postgresql://test@localhost/test_container",
        openmemory_base_url="http://test-container:8080",
    )

    # 使用 create_for_testing 创建容器
    test_container = GatewayContainer.create_for_testing(config=test_config)

    # 使用 set_container 设置全局单例
    set_container(test_container)

    # 验证设置成功
    assert get_container_or_none() is not None
    assert get_container_or_none() is test_container
    assert get_container_or_none().config.project_key == "test-container-isolation"

    # 故意不调用 reset_container()
    # 依赖 auto_reset_gateway_state fixture 的 teardown 清理


def test_16_container_singleton_should_be_none_after_previous_test():
    """
    验证 container 单例已被 autouse fixture 清理

    此测试验证 auto_reset_gateway_state fixture 的 teardown 阶段
    正确调用了 reset_container()。
    """
    from engram.gateway.container import get_container_or_none

    # 上一个测试设置了 container 单例，但 autouse fixture 应该已清理
    current_container = get_container_or_none()
    assert current_container is None, (
        f"状态隔离失败: container 单例应为 None，但实际为 {current_container!r}\n"
        "这表明 auto_reset_gateway_state fixture 的 teardown 未正确清理 _container 全局变量"
    )


# ===================== Group 10: adapter 单例隔离测试 =====================


def test_17_adapter_singleton_set_without_reset():
    """
    通过 override_adapter() 设置 adapter 单例，不主动 reset

    此测试设置 adapter 单例但不调用 reset_adapter()，依赖 autouse fixture
    在 teardown 阶段清理状态。下一个测试将验证清理效果。
    """
    from unittest.mock import Mock

    from engram.gateway.logbook_adapter import (
        LogbookAdapter,
        get_adapter_or_none,
        override_adapter,
    )

    # 创建 mock adapter
    mock_adapter = Mock(spec=LogbookAdapter)
    mock_adapter._test_marker = "test_17_adapter_isolation"

    # 使用 override_adapter 设置单例
    override_adapter(mock_adapter)

    # 验证设置成功
    assert get_adapter_or_none() is not None
    assert get_adapter_or_none() is mock_adapter
    assert get_adapter_or_none()._test_marker == "test_17_adapter_isolation"

    # 故意不调用 reset_adapter()
    # 依赖 auto_reset_gateway_state fixture 的 teardown 清理


def test_18_adapter_singleton_should_be_none_after_previous_test():
    """
    验证 adapter 单例已被 autouse fixture 清理

    此测试验证 auto_reset_gateway_state fixture 的 teardown 阶段
    正确调用了 reset_adapter()。
    """
    from engram.gateway.logbook_adapter import get_adapter_or_none

    # 上一个测试设置了 adapter 单例，但 autouse fixture 应该已清理
    current_adapter = get_adapter_or_none()
    assert current_adapter is None, (
        f"状态隔离失败: adapter 单例应为 None，但实际为 {current_adapter!r}\n"
        "这表明 auto_reset_gateway_state fixture 的 teardown 未正确清理 _adapter_instance 全局变量"
    )


# ===================== Group 11: client 单例隔离测试 =====================


def test_19_client_singleton_set_without_reset():
    """
    通过 override_client() 设置 client 单例，不主动 reset

    此测试设置 client 单例但不调用 reset_client()，依赖 autouse fixture
    在 teardown 阶段清理状态。下一个测试将验证清理效果。
    """
    from unittest.mock import Mock

    from engram.gateway.openmemory_client import (
        OpenMemoryClient,
        get_client_or_none,
        override_client,
    )

    # 创建 mock client
    mock_client = Mock(spec=OpenMemoryClient)
    mock_client._test_marker = "test_19_client_isolation"

    # 使用 override_client 设置单例
    override_client(mock_client)

    # 验证设置成功
    assert get_client_or_none() is not None
    assert get_client_or_none() is mock_client
    assert get_client_or_none()._test_marker == "test_19_client_isolation"

    # 故意不调用 reset_client()
    # 依赖 auto_reset_gateway_state fixture 的 teardown 清理


def test_20_client_singleton_should_be_none_after_previous_test():
    """
    验证 client 单例已被 autouse fixture 清理

    此测试验证 auto_reset_gateway_state fixture 的 teardown 阶段
    正确调用了 reset_client()。
    """
    from engram.gateway.openmemory_client import get_client_or_none

    # 上一个测试设置了 client 单例，但 autouse fixture 应该已清理
    current_client = get_client_or_none()
    assert current_client is None, (
        f"状态隔离失败: client 单例应为 None，但实际为 {current_client!r}\n"
        "这表明 auto_reset_gateway_state fixture 的 teardown 未正确清理 _default_client 全局变量"
    )


# ===================== Group 12: engram.gateway 懒加载 globals 缓存隔离测试 =====================


def test_21_gateway_openmemory_client_lazy_import_cache():
    """
    触发 engram.gateway.openmemory_client 懒加载缓存，不主动清理

    此测试访问 engram.gateway.openmemory_client 触发懒加载缓存，依赖 autouse fixture
    在 teardown 阶段清理状态。下一个测试将验证清理效果。
    """
    import sys

    import engram.gateway

    # 触发懒加载，缓存模块到 engram.gateway 命名空间
    _ = engram.gateway.openmemory_client

    # 验证缓存已设置（在模块的 __dict__ 中）
    gateway_module = sys.modules["engram.gateway"]
    assert "openmemory_client" in gateway_module.__dict__, (
        "懒加载缓存未生效: openmemory_client 应在 engram.gateway.__dict__ 中"
    )


def test_22_gateway_openmemory_client_cache_should_be_cleared():
    """
    验证 engram.gateway.openmemory_client 懒加载缓存已被 autouse fixture 清理

    此测试验证 auto_reset_gateway_state fixture 的 teardown 阶段
    正确调用了 _reset_gateway_lazy_import_cache_for_testing()。
    """
    import sys

    gateway_module = sys.modules.get("engram.gateway")
    assert gateway_module is not None, "engram.gateway 模块应该仍在 sys.modules 中"

    # 验证懒加载缓存已被清除
    assert "openmemory_client" not in gateway_module.__dict__, (
        "状态隔离失败: openmemory_client 不应在 engram.gateway.__dict__ 中\n"
        "这表明 auto_reset_gateway_state fixture 的 teardown 未正确清理懒加载缓存"
    )


# ===================== Group 13: 综合单例隔离测试 =====================


def test_23_set_all_singletons_simultaneously():
    """
    同时设置所有单例状态，验证综合隔离能力

    此测试同时设置 config、container、adapter、client 单例，
    验证 autouse fixture 能够正确处理多个单例的同时清理。
    """
    import sys
    from unittest.mock import Mock

    import engram.gateway
    from engram.gateway.config import (
        GatewayConfig,
        get_config_or_none,
        override_config,
    )
    from engram.gateway.container import (
        GatewayContainer,
        get_container_or_none,
        set_container,
    )
    from engram.gateway.logbook_adapter import (
        LogbookAdapter,
        get_adapter_or_none,
        override_adapter,
    )
    from engram.gateway.openmemory_client import (
        OpenMemoryClient,
        get_client_or_none,
        override_client,
    )

    # 1. 设置 config 单例
    test_config = GatewayConfig(
        project_key="test-comprehensive-isolation",
        postgres_dsn="postgresql://test@localhost/test_comprehensive",
        openmemory_base_url="http://test-comprehensive:8080",
    )
    override_config(test_config)
    assert get_config_or_none() is test_config

    # 2. 设置 container 单例
    test_container = GatewayContainer.create_for_testing(config=test_config)
    set_container(test_container)
    assert get_container_or_none() is test_container

    # 3. 设置 adapter 单例
    mock_adapter = Mock(spec=LogbookAdapter)
    override_adapter(mock_adapter)
    assert get_adapter_or_none() is mock_adapter

    # 4. 设置 client 单例
    mock_client = Mock(spec=OpenMemoryClient)
    override_client(mock_client)
    assert get_client_or_none() is mock_client

    # 5. 触发 engram.gateway 懒加载缓存
    _ = engram.gateway.openmemory_client
    gateway_module = sys.modules["engram.gateway"]
    assert "openmemory_client" in gateway_module.__dict__

    # 故意不调用任何 reset 函数
    # 依赖 auto_reset_gateway_state fixture 的 teardown 清理


def test_24_all_singletons_should_be_none_after_comprehensive_test():
    """
    验证所有单例都已被 autouse fixture 清理

    此测试验证 auto_reset_gateway_state fixture 能够正确处理
    多个单例的同时清理。
    """
    import sys

    from engram.gateway.config import get_config_or_none
    from engram.gateway.container import get_container_or_none
    from engram.gateway.logbook_adapter import get_adapter_or_none
    from engram.gateway.openmemory_client import get_client_or_none

    # 验证 config 单例已清理
    current_config = get_config_or_none()
    assert current_config is None, (
        f"状态隔离失败: config 单例应为 None，但实际为 {current_config!r}"
    )

    # 验证 container 单例已清理
    current_container = get_container_or_none()
    assert current_container is None, (
        f"状态隔离失败: container 单例应为 None，但实际为 {current_container!r}"
    )

    # 验证 adapter 单例已清理
    current_adapter = get_adapter_or_none()
    assert current_adapter is None, (
        f"状态隔离失败: adapter 单例应为 None，但实际为 {current_adapter!r}"
    )

    # 验证 client 单例已清理
    current_client = get_client_or_none()
    assert current_client is None, (
        f"状态隔离失败: client 单例应为 None，但实际为 {current_client!r}"
    )

    # 验证 engram.gateway 懒加载缓存已清理
    gateway_module = sys.modules.get("engram.gateway")
    assert gateway_module is not None, "engram.gateway 模块应该仍在 sys.modules 中"
    assert "openmemory_client" not in gateway_module.__dict__, (
        "状态隔离失败: openmemory_client 不应在 engram.gateway.__dict__ 中"
    )
