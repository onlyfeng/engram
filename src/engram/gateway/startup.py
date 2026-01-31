# -*- coding: utf-8 -*-
"""
Gateway 启动时检查模块

封装启动时的 Logbook DB 检查逻辑与修复提示格式化。
main.py 通过调用此模块完成启动前置检查。
"""

import logging
from typing import Any, Dict, Optional

from .logbook_adapter import (
    LogbookDBCheckError,
    ensure_db_ready,
    is_db_migrate_available,
)

logger = logging.getLogger("gateway.startup")


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
        "python logbook_postgres/scripts/db_bootstrap.py",
        "python logbook_postgres/scripts/db_migrate.py --apply-roles --apply-openmemory-grants",
        "",
        "# 方案 2: 仅执行迁移（角色已存在）",
        "python logbook_postgres/scripts/db_migrate.py",
        "",
        "# 方案 3: Docker 环境",
        "docker compose -f docker-compose.unified.yml up bootstrap_roles logbook_migrate openmemory_migrate",
        "",
        "# 验证修复结果",
        "python logbook_postgres/scripts/db_migrate.py --verify",
        "",
    ]

    if error_code:
        lines.insert(1, f"错误代码: {error_code}")

    if missing_items:
        lines.append("缺失项详情:")
        if isinstance(missing_items, dict):
            items_list = []
            for key, value in missing_items.items():
                if isinstance(value, list):
                    for v in value:
                        items_list.append(f"{key}: {v}")
                else:
                    items_list.append(f"{key}: {value}")
            missing_items = items_list

        for item in list(missing_items)[:10]:
            lines.append(f"  - {item}")
        if len(missing_items) > 10:
            lines.append(f"  ... 还有 {len(missing_items) - 10} 项")
        lines.append("")

    return "\n".join(lines)


def check_logbook_db_on_startup(config) -> bool:
    """
    启动时检查 Logbook DB 结构（统一入口）

    Args:
        config: GatewayConfig 配置对象，需包含以下属性：
            - logbook_check_on_startup: 是否执行启动检查
            - auto_migrate_on_startup: 是否自动迁移
            - postgres_dsn: 数据库连接字符串

    Returns:
        True 表示检查通过（或跳过检查），False 表示检查失败
    """
    if not config.logbook_check_on_startup:
        logger.info("跳过 Logbook DB 检查 (LOGBOOK_CHECK_ON_STARTUP=false)")
        return True

    if not is_db_migrate_available():
        logger.warning("db_migrate 模块不可用，跳过 Logbook DB 检查")
        return True

    logger.info("========================================")
    logger.info("DB 层预检: 检查 Logbook 数据库结构...")
    logger.info("========================================")

    try:
        result = ensure_db_ready(
            dsn=config.postgres_dsn,
            auto_migrate=config.auto_migrate_on_startup,
        )

        if result.ok:
            if result.message:
                logger.info(f"[OK] Logbook DB 检查通过: {result.message}")
            else:
                logger.info("[OK] Logbook DB 检查通过: 所有 schema/表/索引/物化视图已就绪")
            return True
        else:
            logger.error("========================================")
            logger.error("[FAIL] Logbook DB 检查失败")
            logger.error("========================================")
            logger.error(f"原因: {result.message}")

            repair_hint = format_db_repair_commands(
                error_code=getattr(result, "code", None),
                missing_items=getattr(result, "missing_items", None),
            )
            logger.error(repair_hint)
            return False

    except LogbookDBCheckError as e:
        logger.error("========================================")
        logger.error("[FAIL] Logbook DB 检查失败")
        logger.error("========================================")
        logger.error(f"错误码: {e.code}")
        logger.error(f"原因: {e.message}")

        repair_hint = format_db_repair_commands(
            error_code=e.code,
            missing_items=e.missing_items,
        )
        logger.error(repair_hint)
        return False

    except Exception as e:
        logger.error("========================================")
        logger.error("[FAIL] Logbook DB 检查时发生未预期错误")
        logger.error("========================================")
        logger.exception(f"错误详情: {e}")

        repair_hint = format_db_repair_commands()
        logger.error(repair_hint)
        return False
