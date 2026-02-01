"""
Gateway DI 边界策略常量模块

此模块包含 Gateway DI 边界检查的所有策略配置常量，
供 CI 脚本 (check_gateway_di_boundaries.py) 和 pytest 测试
(test_di_boundaries.py) 共享使用。

设计原则：
- 纯常量/纯函数模块，无副作用
- 单一来源（SSOT），避免重复定义导致不一致
- 可独立导入，不触发其他模块的加载

SSOT 文档: docs/architecture/gateway_module_boundaries.md
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Set

# ============================================================================
# SSOT 文档路径
# ============================================================================

# SSOT 文档路径（相对于项目根目录）
# 本脚本的禁止模式与该文档第 3 节保持同步
SSOT_DOC_PATH = "docs/architecture/gateway_module_boundaries.md"

# ============================================================================
# 入口层模块配置
# ============================================================================

# 入口层模块列表（这些模块允许使用全局获取函数）
# handlers/services 目录之外的这些模块是 DI 边界的"入口点"
# 它们负责创建/获取依赖并传递给 handlers/services
ENTRYPOINT_MODULES: List[str] = [
    "main.py",
    "app.py",
    "routes.py",
    "middleware.py",
    "lifecycle.py",
    "dependencies.py",
]

# ============================================================================
# 禁止的调用模式
# ============================================================================

# 禁止的调用模式（正则表达式）
# handlers/services 模块禁止直接调用这些全局获取函数
# 格式: (正则模式, 模式名称, 可选的自定义消息)
# 若第三个元素为 None，使用默认消息
FORBIDDEN_PATTERNS: List[tuple[str, str, str | None]] = [
    # 容器/配置获取
    (r"\bget_container\s*\(", "get_container(", None),
    (r"\bget_config\s*\(", "get_config(", None),
    (r"\bget_client\s*\(", "get_client(", None),
    (r"\bget_gateway_deps\s*\(", "get_gateway_deps(", None),
    # 适配器全局获取
    (r"\blogbook_adapter\.get_adapter\s*\(", "logbook_adapter.get_adapter(", None),
    # 依赖容器直接创建
    (r"\bGatewayDeps\.create\s*\(", "GatewayDeps.create(", None),
    # deps 可选性检查（deps 应由调用方提供，不应为 None）
    (r"\bdeps\s+is\s+None\b", "deps is None", None),
    # correlation_id 生成（应由入口层生成后传入）
    (r"\bgenerate_correlation_id\s*\(", "generate_correlation_id(", None),
    # 禁止直接访问 deps.db（应使用 deps.logbook_adapter）
    (
        r"\bdeps\.db\b",
        "deps.db",
        "禁止直接访问 deps.db，应使用 deps.logbook_adapter 进行数据库操作",
    ),
]

# 允许例外的文件（相对于 handlers 目录）
# 当前无文件级例外
ALLOWED_EXCEPTIONS: dict[str, Set[str]] = {}

# ============================================================================
# DI 边界允许标记
# ============================================================================

# DI 边界允许标记（用于标识 legacy fallback 兼容分支）
# 格式: # DI-BOUNDARY-ALLOW: <reason>
DI_BOUNDARY_ALLOW_MARKER = "# DI-BOUNDARY-ALLOW:"

# DEPS-DB-ALLOW 标记（针对 deps.db 模式的豁免）
# 格式: # DEPS-DB-ALLOW: <reason>; expires=YYYY-MM-DD; owner=<team>
DEPS_DB_ALLOW_MARKER = "# DEPS-DB-ALLOW:"

# DEPS-DB-ALLOW inline marker 正则模式
# 匹配: <reason>; expires=YYYY-MM-DD; owner=<team>
DEPS_DB_INLINE_MARKER_PATTERN = re.compile(
    r"^(?P<reason>[^;]+);\s*expires=(?P<expires>\d{4}-\d{2}-\d{2});\s*owner=(?P<owner>\S+)$"
)

# DEPS-DB-ALLOW id 引用模式
# 匹配: 仅包含 id 字符的简单引用（不含分号）
DEPS_DB_ID_REF_PATTERN = re.compile(r"^(?P<id>[a-z0-9_-]+)$")

# DEPS-DB-ALLOW 最大有效期限（天）
# 超过此期限的标记建议需要 Tech Lead 审批
DEPS_DB_MAX_EXPIRY_DAYS = 180  # 6 个月

# DEPS-DB-ALLOW allowlist 文件路径（相对于项目根）
DEPS_DB_ALLOWLIST_PATH = Path("scripts/ci/gateway_deps_db_allowlist.json")

# ============================================================================
# 扫描目录配置
# ============================================================================

# 扫描目标目录（相对于项目根）
SCAN_DIRECTORIES = [
    Path("src/engram/gateway/handlers"),
    Path("src/engram/gateway/services"),
]

# 向后兼容别名
HANDLERS_DIR = Path("src/engram/gateway/handlers")

# ============================================================================
# 迁移阶段常量
# ============================================================================

# 迁移阶段
PHASE_COMPAT = "compat"  # 兼容期：deps.db 违规仅警告
PHASE_REMOVAL = "removal"  # 移除期：deps.db 违规阻断

# ============================================================================
# 废弃导入扫描配置
# ============================================================================

# 废弃的模块导入（全仓扫描）
# 格式: (import 模式正则, 模式名称, 消息)
DEPRECATED_IMPORT_PATTERNS: List[tuple[str, str, str]] = [
    (
        r"(?:from\s+engram\.gateway\.logbook_db\s+import|import\s+engram\.gateway\.logbook_db)",
        "engram.gateway.logbook_db import",
        "禁止导入已废弃的 engram.gateway.logbook_db 模块，应使用 engram.gateway.logbook_adapter",
    ),
    (
        r"(?:from\s+engram\.gateway\s+import\s+.*\blogbook_db\b|from\s+engram\.gateway\s+import\s+logbook_db)",
        "from engram.gateway import logbook_db",
        "禁止导入已废弃的 logbook_db，应使用 logbook_adapter",
    ),
]

# 废弃导入扫描的全仓目录（相对于项目根）
DEPRECATED_IMPORT_SCAN_DIRECTORIES = [
    Path("src"),
    Path("tests"),
    Path("scripts"),
]

# 允许存在废弃导入的兼容目录（相对于项目根）
# 这些目录在迁移期间允许使用废弃导入
#
# 清空说明（2026-02-01）：
# - tests/logbook/test_logbook_db.py 测试的是 engram.logbook.db（核心模块），
#   与废弃的 engram.gateway.logbook_db 无关，无需兼容豁免
# - tests/gateway/test_correlation_id_proxy.py 已完成迁移，
#   不再包含废弃导入，无需兼容豁免
#
# 当前无需保留任何兼容目录
DEPRECATED_IMPORT_COMPAT_DIRECTORIES: List[str] = []
