#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
engram.logbook.deprecation - 统一的弃用警告工具

本模块提供统一的弃用警告函数，确保所有根目录/scripts目录的 wrapper 脚本
输出一致的弃用提示信息。

使用示例:
    from engram.logbook.deprecation import emit_deprecation_warning

    emit_deprecation_warning(
        old_script="scripts/scm_sync_scheduler.py",
        new_commands=["engram-scm-scheduler", "engram-scm-sync scheduler"],
        package_module="engram.logbook.cli.scm_sync",
    )

弃用警告契约 (v1.0):
    - 所有弃用入口在调用时输出统一格式的警告
    - 警告同时发送到 stderr 和 warnings 系统
    - 包含明确的迁移指引和文档链接
"""

from __future__ import annotations

import sys
import warnings
from typing import Optional

__all__ = [
    "emit_deprecation_warning",
    "emit_import_deprecation_warning",
    "DEPRECATION_DOC_URL",
    "DEPRECATION_VERSION",
]

# 弃用文档链接
DEPRECATION_DOC_URL = (
    "https://github.com/onlyfeng/engram/blob/master/docs/architecture/cli_entrypoints.md"
)

# 弃用版本标识
DEPRECATION_VERSION = "v1.0"


def emit_deprecation_warning(
    old_script: str,
    new_commands: list[str],
    package_module: Optional[str] = None,
    removal_version: str = "v2.0",
    stacklevel: int = 2,
    to_stderr: bool = True,
) -> None:
    """
    发出统一格式的弃用警告。

    此函数同时通过 warnings 模块和 stderr 输出弃用信息，确保用户能够
    收到清晰的迁移指引。

    Args:
        old_script: 已弃用的脚本路径（如 "scripts/scm_sync_scheduler.py"）
        new_commands: 推荐的新命令列表（如 ["engram-scm-scheduler", "engram-scm-sync scheduler"]）
        package_module: 可选的包内模块路径（如 "engram.logbook.cli.scm_sync"）
        removal_version: 计划移除的版本号（默认 "v2.0"）
        stacklevel: warnings.warn 的 stacklevel 参数（默认 2）
        to_stderr: 是否同时输出到 stderr（默认 True）

    Example:
        >>> emit_deprecation_warning(
        ...     old_script="scripts/scm_sync_worker.py",
        ...     new_commands=["engram-scm-worker", "engram-scm-sync worker"],
        ...     package_module="engram.logbook.cli.scm_sync",
        ... )
    """
    # 构建推荐命令列表
    cmd_list = "\n".join(f"    - {cmd}" for cmd in new_commands)

    # 构建警告消息
    message_parts = [
        f"[DEPRECATED] '{old_script}' 已弃用，计划在 {removal_version} 版本移除。",
        "",
        "请使用以下方式替代:",
        cmd_list,
    ]

    if package_module:
        message_parts.extend(
            [
                "",
                "或通过模块调用:",
                f"    python -m {package_module}",
            ]
        )

    message_parts.extend(
        [
            "",
            f"详见: {DEPRECATION_DOC_URL}",
        ]
    )

    full_message = "\n".join(message_parts)

    # 发出 warnings.warn 警告
    # 注意：stacklevel + 1 因为这里多了一层函数调用
    warnings.warn(
        full_message,
        DeprecationWarning,
        stacklevel=stacklevel + 1,
    )

    # 同时输出到 stderr（确保用户能看到）
    if to_stderr:
        # 添加分隔线使警告更醒目
        stderr_message = "\n".join(
            [
                "",
                "=" * 70,
                "⚠️  DEPRECATION WARNING",
                "=" * 70,
                full_message,
                "=" * 70,
                "",
            ]
        )
        print(stderr_message, file=sys.stderr)


def emit_import_deprecation_warning(
    old_module: str,
    new_module: str,
    removal_version: str = "v2.0",
    stacklevel: int = 2,
) -> None:
    """
    发出模块导入弃用警告（用于 re-export wrapper）。

    此函数用于那些仅作为 API 重导出的 wrapper 脚本，在导入时发出警告。

    Args:
        old_module: 已弃用的模块路径（如 "scripts/scm_sync_gitlab_commits.py"）
        new_module: 推荐的新模块路径（如 "engram.logbook.scm_sync_tasks.gitlab_commits"）
        removal_version: 计划移除的版本号（默认 "v2.0"）
        stacklevel: warnings.warn 的 stacklevel 参数（默认 2）

    Example:
        >>> emit_import_deprecation_warning(
        ...     old_module="scripts/scm_sync_svn.py",
        ...     new_module="engram.logbook.scm_sync_tasks.svn",
        ... )
    """
    message = (
        f"[DEPRECATED] '{old_module}' 已弃用，计划在 {removal_version} 版本移除。\n"
        f"请使用 'from {new_module} import ...' 代替。\n"
        f"详见: {DEPRECATION_DOC_URL}"
    )

    warnings.warn(
        message,
        DeprecationWarning,
        stacklevel=stacklevel + 1,
    )
