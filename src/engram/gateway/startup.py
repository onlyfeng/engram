# -*- coding: utf-8 -*-
"""
Gateway 启动时检查模块

封装启动时的 Logbook DB 检查逻辑与修复提示格式化。
main.py 通过调用此模块完成启动前置检查。

启动检查结果通过 StartupCheckResult 返回，包含结构化的状态码：
- StartupStatus.OK: 检查通过
- StartupStatus.SKIPPED: 检查跳过（配置禁用或模块不可用）
- StartupStatus.DB_NOT_READY: 数据库未就绪（表/索引/schema 缺失）
- StartupStatus.DB_CONNECTION_FAILED: 数据库连接失败
- StartupStatus.OPENMEMORY_UNAVAILABLE: OpenMemory 服务不可用
- StartupStatus.CONFIG_MISSING: 关键配置缺失
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .config import GatewayConfig

from .logbook_adapter import (
    LogbookDBCheckError,
    ensure_db_ready,
    is_db_migrate_available,
)

logger = logging.getLogger("gateway.startup")


# ===================== 启动状态码 =====================


class StartupStatus(str, Enum):
    """
    启动检查状态码

    用于结构化表示启动检查结果，便于测试断言和监控。
    """

    OK = "OK"  # 检查通过
    SKIPPED = "SKIPPED"  # 检查跳过（配置禁用或模块不可用）
    DB_NOT_READY = "DB_NOT_READY"  # 数据库未就绪（表/索引/schema 缺失）
    DB_CONNECTION_FAILED = "DB_CONNECTION_FAILED"  # 数据库连接失败
    OPENMEMORY_UNAVAILABLE = "OPENMEMORY_UNAVAILABLE"  # OpenMemory 服务不可用
    CONFIG_MISSING = "CONFIG_MISSING"  # 关键配置缺失
    UNKNOWN_ERROR = "UNKNOWN_ERROR"  # 未知错误


@dataclass
class StartupCheckResult:
    """
    启动检查结果

    Attributes:
        status: 状态码（StartupStatus 枚举）
        ok: 检查是否通过（status 为 OK 或 SKIPPED 时为 True）
        message: 人类可读的消息
        error_code: 原始错误代码（如果有）
        missing_items: 缺失项列表（如果有）
        details: 额外的详情信息
    """

    status: StartupStatus
    ok: bool
    message: str
    error_code: Optional[str] = None
    missing_items: Optional[List[str]] = None
    details: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, message: str = "检查通过") -> "StartupCheckResult":
        """创建成功结果"""
        return cls(status=StartupStatus.OK, ok=True, message=message)

    @classmethod
    def skipped(cls, reason: str) -> "StartupCheckResult":
        """创建跳过结果"""
        return cls(status=StartupStatus.SKIPPED, ok=True, message=f"跳过检查: {reason}")

    @classmethod
    def failure(
        cls,
        status: StartupStatus,
        message: str,
        error_code: Optional[str] = None,
        missing_items: Optional[List[str]] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> "StartupCheckResult":
        """创建失败结果"""
        return cls(
            status=status,
            ok=False,
            message=message,
            error_code=error_code,
            missing_items=missing_items,
            details=details or {},
        )


def format_db_repair_commands(
    error_code: Optional[str] = None,
    missing_items: Optional[Dict[str, Any]] = None,
) -> str:
    """
    格式化数据库修复命令提示。

    Args:
        error_code: 错误代码（可选）
        missing_items: 缺失项详情（可选）

    Returns:
        格式化的修复命令字符串
    """
    lines = [
        "",
        "======================================",
        "修复命令",
        "======================================",
        "",
        "# 方案 1: 完整初始化（首次部署或重建）",
        "# 先初始化角色权限，再执行迁移",
        "engram-bootstrap-roles  # 或 python -m engram.logbook.cli.db_bootstrap",
        "engram-migrate --apply-roles --apply-openmemory-grants",
        "",
        "# 方案 2: 仅执行迁移（角色已存在）",
        "engram-migrate  # 或 python -m engram.logbook.cli.db_migrate",
        "",
        "# 方案 3: Docker 环境",
        "docker compose -f docker-compose.unified.yml up bootstrap_roles logbook_migrate openmemory_migrate",
        "",
        "# 验证修复结果",
        "engram-migrate --verify",
        "",
    ]

    if error_code:
        lines.insert(1, f"错误代码: {error_code}")

    if missing_items:
        lines.append("缺失项详情:")
        items_to_display: List[str]
        if isinstance(missing_items, dict):
            items_to_display = []
            for key, value in missing_items.items():
                if isinstance(value, list):
                    for v in value:
                        items_to_display.append(f"{key}: {v}")
                else:
                    items_to_display.append(f"{key}: {value}")
        else:
            items_to_display = list(missing_items)

        for item in items_to_display[:10]:
            lines.append(f"  - {item}")
        if len(items_to_display) > 10:
            lines.append(f"  ... 还有 {len(items_to_display) - 10} 项")
        lines.append("")

    return "\n".join(lines)


def check_logbook_db_detailed(config: "GatewayConfig") -> StartupCheckResult:
    """
    启动时检查 Logbook DB 结构（返回详细结果）

    Args:
        config: GatewayConfig 配置对象，需包含以下属性：
            - logbook_check_on_startup: 是否执行启动检查
            - auto_migrate_on_startup: 是否自动迁移
            - postgres_dsn: 数据库连接字符串

    Returns:
        StartupCheckResult 包含结构化的检查结果
    """
    if not config.logbook_check_on_startup:
        logger.info("跳过 Logbook DB 检查 (LOGBOOK_CHECK_ON_STARTUP=false)")
        return StartupCheckResult.skipped("LOGBOOK_CHECK_ON_STARTUP=false")

    if not is_db_migrate_available():
        logger.warning("db_migrate 模块不可用，跳过 Logbook DB 检查")
        return StartupCheckResult.skipped("db_migrate 模块不可用")

    logger.info("========================================")
    logger.info("DB 层预检: 检查 Logbook 数据库结构...")
    logger.info("========================================")

    try:
        result = ensure_db_ready(
            dsn=config.postgres_dsn,
            auto_migrate=config.auto_migrate_on_startup,
        )

        if result.ok:
            msg = result.message or "所有 schema/表/索引/物化视图已就绪"
            logger.info(f"[OK] Logbook DB 检查通过: {msg}")
            return StartupCheckResult.success(msg)
        else:
            logger.error("========================================")
            logger.error("[FAIL] Logbook DB 检查失败")
            logger.error("========================================")
            logger.error(f"原因: {result.message}")

            error_code = getattr(result, "code", None)
            raw_missing = getattr(result, "missing_items", None)
            missing_items = _normalize_missing_items(raw_missing)

            repair_hint = format_db_repair_commands(
                error_code=error_code,
                missing_items=raw_missing,
            )
            logger.error(repair_hint)

            return StartupCheckResult.failure(
                status=StartupStatus.DB_NOT_READY,
                message=result.message or "数据库结构不完整",
                error_code=error_code,
                missing_items=missing_items,
            )

    except LogbookDBCheckError as e:
        logger.error("========================================")
        logger.error("[FAIL] Logbook DB 检查失败")
        logger.error("========================================")
        logger.error(f"错误码: {e.code}")
        logger.error(f"原因: {e.message}")

        missing_items = _normalize_missing_items(e.missing_items)

        repair_hint = format_db_repair_commands(
            error_code=e.code,
            missing_items=e.missing_items,
        )
        logger.error(repair_hint)

        # 根据错误码判断是连接失败还是结构问题
        if e.code and "connection" in e.code.lower():
            status = StartupStatus.DB_CONNECTION_FAILED
        else:
            status = StartupStatus.DB_NOT_READY

        return StartupCheckResult.failure(
            status=status,
            message=e.message,
            error_code=e.code,
            missing_items=missing_items,
        )

    except Exception as e:
        logger.error("========================================")
        logger.error("[FAIL] Logbook DB 检查时发生未预期错误")
        logger.error("========================================")
        logger.exception(f"错误详情: {e}")

        repair_hint = format_db_repair_commands()
        logger.error(repair_hint)

        # 判断是否为连接错误
        error_str = str(e).lower()
        if any(kw in error_str for kw in ["connection", "connect", "refused", "timeout"]):
            status = StartupStatus.DB_CONNECTION_FAILED
        else:
            status = StartupStatus.UNKNOWN_ERROR

        return StartupCheckResult.failure(
            status=status,
            message=str(e),
            details={"exception_type": type(e).__name__},
        )


def _normalize_missing_items(raw_missing: Any) -> Optional[List[str]]:
    """将 missing_items 标准化为字符串列表"""
    if raw_missing is None:
        return None
    if isinstance(raw_missing, list):
        return [str(item) for item in raw_missing]
    if isinstance(raw_missing, dict):
        items = []
        for key, value in raw_missing.items():
            if isinstance(value, list):
                for v in value:
                    items.append(f"{key}: {v}")
            else:
                items.append(f"{key}: {value}")
        return items
    return [str(raw_missing)]


def check_logbook_db_on_startup(config: "GatewayConfig") -> bool:
    """
    启动时检查 Logbook DB 结构（统一入口，向后兼容）

    Args:
        config: GatewayConfig 配置对象，需包含以下属性：
            - logbook_check_on_startup: 是否执行启动检查
            - auto_migrate_on_startup: 是否自动迁移
            - postgres_dsn: 数据库连接字符串

    Returns:
        True 表示检查通过（或跳过检查），False 表示检查失败

    Note:
        如需获取详细的检查结果，请使用 check_logbook_db_detailed() 函数。
    """
    result = check_logbook_db_detailed(config)
    return result.ok


def check_openmemory_available(base_url: str, timeout: float = 5.0) -> StartupCheckResult:
    """
    检查 OpenMemory 服务是否可用

    Args:
        base_url: OpenMemory 服务的基础 URL
        timeout: 请求超时时间（秒）

    Returns:
        StartupCheckResult 包含检查结果
    """
    import httpx

    if not base_url:
        logger.warning("OpenMemory base_url 未配置")
        return StartupCheckResult.failure(
            status=StartupStatus.CONFIG_MISSING,
            message="OPENMEMORY_BASE_URL 未配置",
        )

    logger.info(f"检查 OpenMemory 服务: {base_url}")

    try:
        # 尝试访问健康检查端点
        health_url = f"{base_url.rstrip('/')}/health"
        with httpx.Client(timeout=timeout) as client:
            response = client.get(health_url)

        if response.status_code == 200:
            logger.info(f"[OK] OpenMemory 服务可用: {base_url}")
            return StartupCheckResult.success(f"OpenMemory 服务可用: {base_url}")
        else:
            logger.warning(f"OpenMemory 返回非 200 状态码: {response.status_code}")
            return StartupCheckResult.failure(
                status=StartupStatus.OPENMEMORY_UNAVAILABLE,
                message=f"OpenMemory 返回状态码 {response.status_code}",
                details={"status_code": response.status_code, "url": health_url},
            )

    except httpx.ConnectError as e:
        logger.warning(f"无法连接 OpenMemory: {e}")
        return StartupCheckResult.failure(
            status=StartupStatus.OPENMEMORY_UNAVAILABLE,
            message=f"无法连接 OpenMemory: {base_url}",
            details={"exception_type": "ConnectError", "url": base_url},
        )
    except httpx.TimeoutException as e:
        logger.warning(f"连接 OpenMemory 超时: {e}")
        return StartupCheckResult.failure(
            status=StartupStatus.OPENMEMORY_UNAVAILABLE,
            message=f"连接 OpenMemory 超时: {base_url}",
            details={"exception_type": "TimeoutException", "url": base_url},
        )
    except Exception as e:
        logger.warning(f"检查 OpenMemory 时发生错误: {e}")
        return StartupCheckResult.failure(
            status=StartupStatus.OPENMEMORY_UNAVAILABLE,
            message=f"检查 OpenMemory 失败: {e}",
            details={"exception_type": type(e).__name__, "url": base_url},
        )


def check_required_config(config: "GatewayConfig") -> StartupCheckResult:
    """
    检查必需的配置项是否存在

    Args:
        config: GatewayConfig 配置对象

    Returns:
        StartupCheckResult 包含检查结果
    """
    missing = []

    # 检查必需的配置项
    if not getattr(config, "project_key", None):
        missing.append("PROJECT_KEY")

    if not getattr(config, "postgres_dsn", None):
        missing.append("POSTGRES_DSN")

    if not getattr(config, "openmemory_base_url", None):
        missing.append("OPENMEMORY_BASE_URL")

    if missing:
        logger.error(f"缺少必需的配置项: {', '.join(missing)}")
        return StartupCheckResult.failure(
            status=StartupStatus.CONFIG_MISSING,
            message=f"缺少必需的配置项: {', '.join(missing)}",
            missing_items=missing,
        )

    logger.info("[OK] 所有必需配置项已就绪")
    return StartupCheckResult.success("所有必需配置项已就绪")


def run_all_startup_checks(
    config: "GatewayConfig", check_openmemory: bool = True
) -> Dict[str, StartupCheckResult]:
    """
    执行所有启动检查

    Args:
        config: GatewayConfig 配置对象
        check_openmemory: 是否检查 OpenMemory 服务

    Returns:
        字典，key 为检查名称，value 为 StartupCheckResult
    """
    results: Dict[str, StartupCheckResult] = {}

    # 1. 配置检查
    results["config"] = check_required_config(config)

    # 2. 数据库检查
    results["database"] = check_logbook_db_detailed(config)

    # 3. OpenMemory 检查（可选）
    if check_openmemory and getattr(config, "openmemory_base_url", None):
        results["openmemory"] = check_openmemory_available(config.openmemory_base_url)

    return results


def get_overall_status(results: Dict[str, StartupCheckResult]) -> StartupStatus:
    """
    根据所有检查结果获取总体状态

    Args:
        results: 检查结果字典

    Returns:
        总体 StartupStatus
    """
    # 优先级：CONFIG_MISSING > DB_CONNECTION_FAILED > DB_NOT_READY > OPENMEMORY_UNAVAILABLE > OK
    for status in [
        StartupStatus.CONFIG_MISSING,
        StartupStatus.DB_CONNECTION_FAILED,
        StartupStatus.DB_NOT_READY,
        StartupStatus.UNKNOWN_ERROR,
    ]:
        for result in results.values():
            if result.status == status:
                return status

    # OpenMemory 不可用时服务仍可启动（降级模式）
    for result in results.values():
        if result.status == StartupStatus.OPENMEMORY_UNAVAILABLE:
            return StartupStatus.OPENMEMORY_UNAVAILABLE

    return StartupStatus.OK
