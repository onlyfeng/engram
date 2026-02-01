# -*- coding: utf-8 -*-
"""
Gateway 生命周期管理模块

封装 FastAPI 应用的 lifespan 上下文管理器，负责：
- 配置验证: get_config() + validate_config()
- 容器初始化: GatewayContainer.create() + set_container()
- DB 结构检查: check_logbook_db_on_startup() (由 startup.py 提供)
- 依赖预热: deps.logbook_adapter, deps.openmemory_client
- 关闭时的资源清理

数据库连接初始化策略：
- DB 连接通过 deps.logbook_adapter 预热时自动初始化
- LogbookAdapter 构造时会设置 POSTGRES_DSN 环境变量
- 无需显式调用 logbook_db 模块（已弃用）

设计原则:
- 配置缺失或检查失败时，lifespan 会优雅降级（警告但不阻止启动）
- 这确保了测试环境（可能没有完整配置）也能正常工作
- 生产环境通过 main() 的预检查保证配置完整性

Usage:
    from .lifecycle import lifespan
    app = create_app(lifespan=lifespan)
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncGenerator

if TYPE_CHECKING:
    from fastapi import FastAPI

from .config import ConfigError, get_config, validate_config
from .container import (
    GatewayContainer,
    get_container,
    is_container_set,
    reset_container,
    set_container,
)
from .startup import check_logbook_db_on_startup

logger = logging.getLogger("gateway.lifecycle")


@asynccontextmanager
async def lifespan(app: "FastAPI") -> AsyncGenerator[None, None]:
    """
    应用生命周期管理器

    负责完整的依赖初始化（方案 A：延迟初始化策略的核心）。

    依赖初始化时机说明:
    ============================================================

    1. import-time（create_app 调用时）:
       - 仅创建 FastAPI 应用和注册路由
       - 不触发 get_config()/get_container()
       - 支持无环境变量的模块导入

    2. lifespan startup（本函数）:
       - 加载并验证配置: get_config() + validate_config()
       - 初始化 GatewayContainer: GatewayContainer.create() + set_container()
       - DB 结构检查: check_logbook_db_on_startup() (由 startup.py 提供)
       - 依赖预热: deps.logbook_adapter, deps.openmemory_client
         (LogbookAdapter 预热时自动初始化 DB 连接)
       - 确保首次请求时无初始化延迟

    3. 请求时:
       - handler 通过 get_container().deps 获取依赖
       - lifespan 已预热，此处仅获取引用

    设计原则:
    - 配置缺失或检查失败时，lifespan 会优雅降级（警告但不阻止启动）
    - 这确保了测试环境（可能没有完整配置）也能正常工作
    - 生产环境通过 main() 的预检查保证配置完整性

    Lifecycle:
        startup:
            1. 加载并验证配置（如果可用）
            2. 创建并设置 GatewayContainer
            3. 检查 Logbook DB 结构（非阻塞，由 startup.py 提供）
            4. 预热 deps.logbook_adapter（自动初始化 DB 连接）
            5. 预热 deps.openmemory_client
        shutdown:
            1. 清理 container 资源（调用 reset_container）
    """
    # ===== Startup =====
    logger.info("Gateway lifespan: 启动...")

    # 检查 container 是否已初始化（可能由测试代码通过 set_container 注入）
    # 使用 is_container_set() - 无副作用检查，不触发 load_config() 或创建容器
    # 这确保在未设置 PROJECT_KEY/POSTGRES_DSN 时也能安全检查
    container_initialized = is_container_set()

    if container_initialized:
        logger.info("Container 已预先初始化（测试场景），lifespan 进行预热")

    # 尝试进行增强初始化（配置验证、DB 检查、依赖预热等）
    # 如果失败，仅警告不阻止启动（支持测试环境）
    try:
        # 1. 尝试加载并验证配置
        config = get_config()
        validate_config()
        logger.info(f"配置验证成功: project={config.project_key}")

        # 2. 检查 Logbook DB 结构（非阻塞，仅警告）
        # DB 连接通过后续的 deps.logbook_adapter 预热自动初始化
        try:
            if not check_logbook_db_on_startup(config):
                logger.warning("Logbook DB 检查发现问题，服务将继续启动但可能功能受限")
        except Exception as e:
            logger.warning(f"Logbook DB 检查异常: {e}，服务将继续启动")

        # 3. 如果 container 未初始化，现在创建
        if not container_initialized:
            container = GatewayContainer.create(config)
            set_container(container)
            logger.info("GatewayContainer 组装完成")

        # 4. 预热 deps（触发延迟初始化，确保首次请求无延迟）
        # 注意: logbook_adapter 预热时会自动初始化 DB 连接（设置 POSTGRES_DSN 环境变量）
        try:
            container = get_container()
            deps = container.deps
            # 预热 logbook_adapter（触发延迟初始化）
            _ = deps.logbook_adapter
            logger.info("依赖预热: logbook_adapter 已初始化")
            # 预热 openmemory_client（触发延迟初始化）
            _ = deps.openmemory_client
            logger.info("依赖预热: openmemory_client 已初始化")
        except Exception as e:
            logger.warning(f"依赖预热异常: {e}（首次请求时将延迟初始化）")

    except ConfigError as e:
        # 配置错误：在测试环境中可以继续，生产环境应该在 main() 预检查时就失败
        logger.warning(f"配置加载失败: {e}（测试环境可忽略）")
    except Exception as e:
        logger.warning(f"增强初始化异常: {e}（服务将继续启动）")

    logger.info("Gateway lifespan: 启动完成")

    # ===== Yield to application =====
    yield

    # ===== Shutdown =====
    logger.info("Gateway lifespan: 开始关闭...")

    # 清理 container 资源（当前 container 是轻量的，无需特殊清理）
    # 如果将来 container 持有连接池等资源，可在此处关闭
    try:
        reset_container()
        logger.info("GatewayContainer 已重置")
    except Exception as e:
        logger.warning(f"Container 重置异常: {e}")

    logger.info("Gateway lifespan: 关闭完成")


__all__ = ["lifespan"]
