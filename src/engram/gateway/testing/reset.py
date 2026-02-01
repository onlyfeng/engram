"""
Gateway 测试状态重置模块

提供 reset_gateway_runtime_state() 函数，用于在测试之间重置所有 Gateway 运行时状态。

================================================================================
                        设计原则: 职责分离
================================================================================

本模块与 container.reset_all_singletons() 的职责划分：

1. container.reset_all_singletons():
   - 重置纯单例（config, logbook_adapter, openmemory_client, container, tool_executor）
   - 由测试 fixture 调用

2. reset_gateway_runtime_state()（本模块）:
   - 重置所有运行时状态，包括：
     - 纯单例（通过调用 reset_all_singletons）
     - 懒加载缓存（engram.gateway 包命名空间）
     - ContextVar 状态（correlation_id 等）
   - 测试 fixture 应优先使用此函数

================================================================================
                        状态清单（与文档保持同步）
================================================================================

以下状态由本模块统一管理重置，详细说明参见:
docs/architecture/gateway_test_isolation_state_model.md

1. 纯单例（模块级全局变量）：
   - container._container           (container.py:252)
   - config._config                 (config.py:252)
   - logbook_adapter._adapter_instance   (logbook_adapter.py:1535)
   - openmemory_client._default_client   (openmemory_client.py:509)

2. 实例缓存（依附于单例）：
   - GatewayContainer._deps_cache   (container.py:95) - 随宿主单例重置

3. ContextVar 状态：
   - mcp_rpc._current_correlation_id     (mcp_rpc.py:357-359)
   - middleware._request_correlation_id  (middleware.py:39-41)

4. 全局注册器：
   - mcp_rpc._tool_executor   (mcp_rpc.py:1346)

5. globals 缓存（懒加载）：
   - engram/gateway/__init__.py:64 - 懒加载子模块缓存
   - mcp_rpc.py:1884-1895 - api_models 动态导出（一般无需重置）

================================================================================
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger("gateway.testing.reset")


def _reset_gateway_lazy_import_cache_for_testing() -> None:
    """
    重置 engram.gateway 包的懒加载缓存

    engram.gateway.__init__.py 使用 __getattr__ 实现懒加载，首次访问子模块时
    会通过 globals()[name] = module 缓存到模块命名空间。此函数删除这些缓存属性，
    确保下次访问时重新触发导入逻辑。

    清理的属性：
    - logbook_adapter
    - openmemory_client
    - outbox_worker

    注意：一般测试场景不需要重置此缓存，除非需要测试模块导入行为本身。
    """
    gateway_module_name = "engram.gateway"
    gateway_module = sys.modules.get(gateway_module_name)

    if gateway_module is None:
        logger.debug("engram.gateway 模块未加载，跳过懒加载缓存重置")
        return

    lazy_attrs = ("logbook_adapter", "openmemory_client", "outbox_worker")
    for attr in lazy_attrs:
        if hasattr(gateway_module, attr) and attr in gateway_module.__dict__:
            delattr(gateway_module, attr)
            logger.debug("已清除懒加载缓存: engram.gateway.%s", attr)


def reset_gateway_runtime_state() -> None:
    """
    重置所有 Gateway 运行时状态（统一入口）

    此函数用于测试 teardown，确保所有运行时状态被正确清理，避免测试间状态污染。
    这是测试代码应使用的首选 reset 函数。

    重置内容（按调用顺序）：

    1. 纯单例（通过 container.reset_all_singletons）：
       - _container → None（同时清除 _deps_cache）
       - _config → None
       - _adapter_instance → None
       - _default_client → None
       - _tool_executor → None

    2. 懒加载缓存（engram.gateway 包命名空间）：
       - logbook_adapter
       - openmemory_client
       - outbox_worker

    3. ContextVar 状态（直接调用各模块的 reset 函数）：
       - mcp_rpc._current_correlation_id → None
       - middleware._request_correlation_id → None

    调用层级图::

        reset_gateway_runtime_state()  [本函数]
        ├── reset_all_singletons()     [container.py]
        │   ├── _container.reset()     → 清除 _deps_cache
        │   ├── _container = None
        │   ├── reset_config()
        │   ├── reset_adapter()
        │   ├── reset_client()
        │   └── reset_tool_executor_for_testing()
        ├── _reset_gateway_lazy_import_cache_for_testing()
        ├── reset_current_correlation_id_for_testing()   [mcp_rpc.py]
        └── reset_request_correlation_id_for_testing()   [middleware.py]

    使用场景：
    - pytest fixture 的 setup/teardown（auto_reset_gateway_state）
    - 集成测试的 setup/teardown
    - 需要完全隔离的测试场景

    Usage::

        @pytest.fixture(autouse=True)
        def reset_state():
            reset_gateway_runtime_state()  # setup
            yield
            reset_gateway_runtime_state()  # teardown

    线程安全: 否（建议在单线程环境下调用，如测试 teardown）

    注意事项：
    - 若需测试模块导入失败，使用 SysModulesPatcher（见 conftest.py）
    - mcp_rpc 的 api_models 动态导出缓存一般无需重置
    """
    # 1. 重置纯单例（config, container, adapter, client, tool_executor）
    # reset_all_singletons 会递归调用各模块的 reset 函数
    try:
        from engram.gateway.container import reset_all_singletons

        reset_all_singletons()
        logger.debug("reset_all_singletons 完成")
    except ImportError:
        logger.debug("reset_all_singletons 不可用，尝试单独重置")
        # 回退方案：单独重置各单例
        _reset_singletons_fallback()

    # 2. 重置 engram.gateway 包的懒加载缓存
    _reset_gateway_lazy_import_cache_for_testing()

    # 3. 重置 mcp_rpc 模块的 ContextVar 状态
    # 注意：_tool_executor 已在 reset_all_singletons 中重置
    try:
        from engram.gateway.mcp_rpc import reset_current_correlation_id_for_testing

        reset_current_correlation_id_for_testing()
        logger.debug("mcp_rpc._current_correlation_id 已重置")
    except ImportError:
        logger.debug("mcp_rpc reset_current_correlation_id_for_testing 不可用")

    # 4. 重置 middleware 模块的 ContextVar 状态
    try:
        from engram.gateway.middleware import reset_request_correlation_id_for_testing

        reset_request_correlation_id_for_testing()
        logger.debug("middleware._request_correlation_id 已重置")
    except ImportError:
        logger.debug("middleware reset_request_correlation_id_for_testing 不可用")


def _reset_singletons_fallback() -> None:
    """
    单例重置的回退方案

    当 container.reset_all_singletons 不可用时，单独重置各单例。
    """
    # config
    try:
        from engram.gateway.config import reset_config

        reset_config()
    except ImportError:
        pass

    # logbook_adapter
    try:
        from engram.gateway.logbook_adapter import reset_adapter

        reset_adapter()
    except ImportError:
        pass

    # openmemory_client
    try:
        from engram.gateway.openmemory_client import reset_client

        reset_client()
    except ImportError:
        pass

    # tool_executor
    try:
        from engram.gateway.mcp_rpc import reset_tool_executor_for_testing

        reset_tool_executor_for_testing()
    except ImportError:
        pass
