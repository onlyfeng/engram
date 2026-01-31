"""
Memory Gateway - MCP Server 入口

提供 /mcp 端点，暴露以下 MCP 工具：
- memory_store: 存储记忆（含策略校验、审计、失败降级）
- memory_query: 查询记忆
- memory_promote: 提升记忆空间（可选）
- memory_reinforce: 强化记忆（可选）

启动命令:
    uvicorn engram.gateway.main:app --host 0.0.0.0 --port 8787
    或
    python -m engram.gateway.main

模块结构（v2 重构）:
- app.py: 应用工厂 (create_app)，统一依赖获取与路由注册
- container.py: 依赖组装容器 (GatewayContainer)，仅负责组装
- di.py: 依赖注入模块 (RequestContext, GatewayDeps)
- handlers/: 核心业务逻辑（memory_store, memory_query, governance_update, evidence_upload）
- services/: 共享纯函数（hash_utils, actor_validation, audit_service）
- 本文件仅负责调用 create_app() 并暴露 app 与 main()

依赖注入设计（ADR：入口层统一 + deps 参数透传）:
============================================================

1. 依赖组装与获取分离:
   - GatewayContainer: 仅负责组装依赖实例，不直接暴露给业务逻辑
   - GatewayDeps: 通过 container.deps 获取，作为统一依赖接口
   - create_app() 中从 container 获取 deps，显式传入所有 handler

2. 依赖获取路径（推荐）:
   - 入口层: create_app() 中 deps = container.deps
   - handlers: 通过 deps 参数接收依赖（deps=deps）
   - 测试: GatewayDeps.for_testing() / GatewayContainer.create_for_testing()

3. correlation_id 统一规则:
   - 只在入口层生成一次（mcp_endpoint / REST endpoints）
   - 通过参数透传到所有 handlers
   - 审计记录、错误响应都使用同一个 correlation_id

4. 线程安全性:
   - container/deps/config/adapter/client 都是单例，初始化后不变
   - RequestContext 每次请求创建新实例，无跨请求共享
   - handlers 接收 deps 参数，不依赖全局状态

5. 可测试替换:
   - GatewayDeps.for_testing(config=mock, logbook_adapter=mock, ...)
   - container.set_container(mock_container)
   - 测试完成后调用对应的 reset_*() 函数清理

6. 预热策略（lifespan）:
   - lifespan 中预热 deps.logbook_adapter 和 deps.openmemory_client
   - 确保首次请求时无初始化延迟
"""

import logging
import sys
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI

from .app import create_app
from .config import ConfigError, get_config, validate_config
from .container import GatewayContainer, get_container, reset_container, set_container

# NOTE: logbook_db 模块已弃用，此处仅用于启动时数据库初始化
# 应用代码（handlers/services）应通过 deps.db 或 deps.logbook_adapter 获取数据库操作
from .logbook_db import get_db, set_default_dsn

# 检查 engram.logbook 模块是否可用
try:
    import engram.logbook.errors  # noqa: F401
except ImportError:
    print(
        "\n"
        "=" * 60 + "\n"
        "[ERROR] 缺少依赖: engram.logbook\n"
        "=" * 60 + "\n"
        "\n"
        "Gateway 依赖 engram 核心包（engram.logbook 模块提供统一错误码等），\n"
        "请确保已正确安装 engram 包：\n"
        "\n"
        "  # 在 monorepo 根目录执行（推荐）\n"
        '  pip install -e ".[gateway]"    # 仅 Gateway 依赖\n'
        '  pip install -e ".[full]"       # 完整安装\n'
        "\n"
        "  # 或 Docker 环境（已自动安装）\n"
        "  docker compose -f docker-compose.unified.yml up gateway\n"
        "\n"
        "注：engram_logbook 是 engram.logbook 的兼容别名，无需单独安装。\n"
        "=" * 60 + "\n"
    )
    sys.exit(1)

# 向后兼容导出：handlers 中的响应模型
from .handlers import (
    MemoryStoreResponse,
)

# 向后兼容导出（供测试和旧代码使用）
from .services.actor_validation import validate_actor_user as _validate_actor_user_v2
from .startup import check_logbook_db_on_startup

# 向后兼容导出：app.py 中的请求模型

# 向后兼容导出：mcp_rpc 模块

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("gateway")


def _validate_actor_user(
    actor_user_id: str,
    config,
    target_space: str,
    payload_sha: str,
    evidence_refs,
    correlation_id: str,
    deps=None,
):
    """
    向后兼容包装器：调用新的 validate_actor_user 并转换返回值

    旧签名返回 Optional[MemoryStoreResponse]，新签名返回 ActorValidationResult
    """
    # 如果没有提供 deps，从全局 container 获取
    if deps is None:
        deps = get_container().deps

    result = _validate_actor_user_v2(
        actor_user_id=actor_user_id,
        config=config,
        target_space=target_space,
        payload_sha=payload_sha,
        evidence_refs=evidence_refs,
        correlation_id=correlation_id,
        deps=deps,
    )

    if not result.should_continue and result.response_data:
        return MemoryStoreResponse(**result.response_data)

    if result.degraded_space:
        # 降级场景：返回 redirect 响应
        return MemoryStoreResponse(
            ok=True,
            action="redirect",
            space_written=result.degraded_space,
            memory_id=None,
            outbox_id=None,
            correlation_id=correlation_id,
            evidence_refs=evidence_refs,
            message=f"用户不存在，降级到 {result.degraded_space}",
        )

    return None


# ===================== Lifespan 管理 =====================


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    应用生命周期管理器

    提供增强初始化功能，在应用启动时进行配置验证、资源初始化和依赖预热。

    依赖组装与预热（ADR：入口层统一 + deps 参数透传）:
    ============================================================

    1. GatewayConfig 创建:
       - 来源: get_config() -> load_config() -> 环境变量
       - 必填: PROJECT_KEY, POSTGRES_DSN
       - 可选: OPENMEMORY_BASE_URL, OPENMEMORY_API_KEY, GATEWAY_PORT 等

    2. GatewayContainer 组装:
       - 来源: create_app() 中创建，仅负责组装依赖
       - 不直接暴露给业务逻辑

    3. GatewayDeps 预热:
       - 来源: container.deps（绑定到 container 的统一依赖接口）
       - 预热: 触发 deps.logbook_adapter 和 deps.openmemory_client 的延迟初始化
       - 目的: 确保首次请求时无初始化延迟

    设计原则:
    - container 仅负责组装依赖，create_app() 中获取 deps 传入 handler
    - lifespan 提供: DB 健康检查、配置验证、依赖预热
    - 如果配置缺失或检查失败，lifespan 会优雅降级（警告但不阻止启动）
    - 这确保了测试环境（可能没有完整配置）也能正常工作

    Lifecycle:
        startup:
            1. 验证配置（如果可用）
            2. 初始化 DB 连接（如果 DSN 可用）
            3. 检查 Logbook DB 结构（非阻塞）
            4. 预热 deps.logbook_adapter 和 deps.openmemory_client
        shutdown:
            1. 清理 container 资源（调用 reset_container）
    """
    # ===== Startup =====
    logger.info("Gateway lifespan: 启动...")

    # 检查 container 是否已经由 create_app() 初始化
    try:
        existing_container = get_container()
        container_initialized = existing_container is not None
    except Exception:
        container_initialized = False

    if container_initialized:
        logger.info("Container 已由 create_app() 组装，lifespan 进行预热和增强初始化")

    # 尝试进行增强初始化（配置验证、DB 检查、依赖预热等）
    # 如果失败，仅警告不阻止启动（支持测试环境）
    try:
        # 1. 尝试加载并验证配置
        config = get_config()
        validate_config()
        logger.info(f"配置验证成功: project={config.project_key}")

        # 2. 尝试初始化 DB 连接
        try:
            set_default_dsn(config.postgres_dsn)
            get_db(dsn=config.postgres_dsn)
            logger.info("数据库连接初始化完成")

            # 3. 检查 Logbook DB 结构（非阻塞，仅警告）
            try:
                if not check_logbook_db_on_startup(config):
                    logger.warning("Logbook DB 检查发现问题，服务将继续启动但可能功能受限")
            except Exception as e:
                logger.warning(f"Logbook DB 检查异常: {e}，服务将继续启动")
        except Exception as e:
            logger.warning(f"数据库初始化跳过: {e}（测试环境可忽略）")

        # 4. 如果 container 未初始化，现在创建
        if not container_initialized:
            container = GatewayContainer.create(config)
            set_container(container)
            logger.info("GatewayContainer 组装完成")

        # 5. 预热 deps（触发延迟初始化，确保首次请求无延迟）
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


# ===================== 应用实例 =====================

# 延迟创建 app 实例，在模块加载时不立即初始化
# 这样可以在 main() 中先进行配置验证和 DB 检查
_app = None


def get_app():
    """获取 FastAPI 应用实例（延迟初始化）"""
    global _app
    if _app is None:
        _app = create_app()
    return _app


# 为 uvicorn 暴露 app 变量
# 注意：直接使用 uvicorn engram.gateway.main:app 时会触发此处
# 此时 lifespan 会在 app 启动时执行，完成依赖预热
#
# 依赖注入设计：
# - create_app() 组装 container，并从 container.deps 获取统一依赖
# - lifespan 预热 deps.logbook_adapter 和 deps.openmemory_client
# - 所有 handler 调用都显式传入 deps=deps，确保依赖来源单一可控
app = create_app(lifespan=lifespan)


# ===================== 启动入口 =====================


def main():
    """
    CLI 启动入口

    职责：
    1. 解析命令行参数
    2. 启动前预检查（配置验证、DB 连接测试）
    3. 启动 uvicorn 服务器

    注意：
    - container 组装由 create_app() 完成
    - lifespan 负责预热 deps.logbook_adapter 和 deps.openmemory_client
    - 所有 handler 调用都显式传入 deps=deps
    """
    import argparse

    import uvicorn

    if any(flag in sys.argv for flag in ("-h", "--help")):
        parser = argparse.ArgumentParser(
            prog="engram-gateway",
            description="Engram Gateway 服务入口",
        )
        parser.add_argument(
            "--host",
            default="0.0.0.0",
            help="监听地址（默认 0.0.0.0）",
        )
        parser.add_argument(
            "--port",
            type=int,
            default=8787,
            help="监听端口（默认 8787）",
        )
        parser.print_help()
        return

    # 启动前预检查（配置验证）
    # 注意：container 的实际初始化由 lifespan 负责
    try:
        config = get_config()
        validate_config()
        logger.info(f"配置预检查通过: project={config.project_key}, port={config.gateway_port}")

        # DB 连接预检查（仅测试连接，不初始化 container）
        set_default_dsn(config.postgres_dsn)
        get_db(dsn=config.postgres_dsn)
        logger.info("数据库连接预检查通过")

        # Logbook DB 结构预检查（严格模式：失败则退出）
        if not check_logbook_db_on_startup(config):
            logger.error("Logbook DB 预检查失败，服务无法启动")
            sys.exit(1)

        logger.info("所有预检查通过，启动 uvicorn 服务器...")
        logger.info("注意：GatewayContainer 将由 lifespan 初始化")

    except ConfigError as e:
        logger.error(f"配置错误: {e}")
        sys.exit(1)

    # 启动 uvicorn，lifespan 将在应用启动时初始化 container
    uvicorn.run(
        "engram.gateway.main:app",
        host="0.0.0.0",
        port=config.gateway_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
