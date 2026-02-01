# -*- coding: utf-8 -*-
"""
Gateway 测试 patch 路径常量集中管理

本模块集中声明所有 handler/module 的 patch 路径常量，
供 tests/gateway/ 下的测试文件统一引用，避免路径字符串散落在各文件中。

使用示例:
    from tests.gateway.patch_paths import (
        HANDLER_MODULE_MEMORY_STORE,
        PATCH_WRITE_PENDING_AUDIT,
    )

    with patch(PATCH_WRITE_PENDING_AUDIT) as mock_pending_audit:
        ...
"""

# ==============================================================================
# Handler 模块路径
# ==============================================================================

HANDLER_MODULE_MEMORY_STORE = "engram.gateway.handlers.memory_store"
HANDLER_MODULE_MEMORY_QUERY = "engram.gateway.handlers.memory_query"
HANDLER_MODULE_GOVERNANCE = "engram.gateway.handlers.governance_update"
HANDLER_MODULE_EVIDENCE = "engram.gateway.handlers.evidence_upload"

# ==============================================================================
# 服务/客户端模块路径
# ==============================================================================

CONFIG_MODULE = "engram.gateway.config"
CLIENT_MODULE = "engram.gateway.openmemory_client"
ADAPTER_MODULE = "engram.gateway.logbook_adapter"
OUTBOX_WORKER_MODULE = "engram.gateway.outbox_worker"

# ==============================================================================
# memory_store handler patch 路径
# 审计服务相关：handlers 从 services 稳定导出点导入，patch 路径指向使用点
# ==============================================================================

PATCH_WRITE_PENDING_AUDIT = f"{HANDLER_MODULE_MEMORY_STORE}.write_pending_audit_or_raise"
PATCH_FINALIZE_AUDIT = f"{HANDLER_MODULE_MEMORY_STORE}.finalize_audit"
PATCH_WRITE_AUDIT = f"{HANDLER_MODULE_MEMORY_STORE}.write_audit_or_raise"
PATCH_CREATE_ENGINE = f"{HANDLER_MODULE_MEMORY_STORE}.create_engine_from_settings"

# ==============================================================================
# logbook_adapter patch 路径
# ==============================================================================

RELIABILITY_REPORT_PATCH = f"{ADAPTER_MODULE}.get_reliability_report"

# ==============================================================================
# outbox_worker patch 路径
# ==============================================================================

PATCH_OUTBOX_WORKER_LOGBOOK_ADAPTER = f"{OUTBOX_WORKER_MODULE}.logbook_adapter"
PATCH_OUTBOX_WORKER_OPENMEMORY_CLIENT = f"{OUTBOX_WORKER_MODULE}.openmemory_client"
