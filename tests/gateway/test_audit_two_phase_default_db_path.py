# -*- coding: utf-8 -*-
"""
测试两阶段审计默认 DB 路径 - 弃用薄转发

.. deprecated::
    本模块已弃用，测试已迁移至 test_two_phase_audit_adapter_first.py。
    本模块仅保留薄转发以兼容外部引用（如 pytest -k 命名查找）。

重要说明（v1.0 变更）：
=======================
- logbook_db.py 模块已删除，deps.db 访问路径不再可用
- handlers/services 必须通过 deps.logbook_adapter 获取依赖
- 本模块的存在不代表 handlers/services 可使用 deps.db
- 这只是一个测试命名兼容性转发，实际测试逻辑在 adapter-first 模块中

迁移说明：
- 所有两阶段审计测试场景 → test_two_phase_audit_adapter_first.py
- Adapter-first 模式（仅注入 logbook_adapter + openmemory_client + config）

参见：
- 契约文档：docs/contracts/gateway_audit_evidence_correlation_contract.md
- DI 边界 ADR：docs/architecture/adr_gateway_di_and_entry_boundary.md
"""

import warnings

# 发出弃用警告（模块级别）
warnings.warn(
    "test_audit_two_phase_default_db_path 模块已弃用，"
    "请使用 test_two_phase_audit_adapter_first 模块。"
    "本模块仅保留薄转发以兼容外部引用。",
    DeprecationWarning,
    stacklevel=1,
)

# 薄转发：从新模块导入所有测试类
# 这样外部引用（如 pytest -k）仍可找到这些测试
#
# v1.0 变更:
# - 移除 Legacy 测试类（TestLegacyDepsDbDeprecationWarning, TestLegacyTwoPhaseAuditViaDepsDb）
# - logbook_db.py 已删除，不再需要 legacy 兼容测试
from tests.gateway.test_two_phase_audit_adapter_first import (
    TestAdapterFirstCorrelationIdConsistency as TestTwoPhaseAuditCorrelationIdConsistency,
)
from tests.gateway.test_two_phase_audit_adapter_first import (
    TestAdapterFirstSQLQueryContract as TestTwoPhaseAuditSQLQueryContract,
)
from tests.gateway.test_two_phase_audit_adapter_first import (
    TestAdapterFirstTwoPhaseAuditRedirectedBranch as TestTwoPhaseAuditRedirectedBranch,
)
from tests.gateway.test_two_phase_audit_adapter_first import (
    TestAdapterFirstTwoPhaseAuditSuccessBranch as TestTwoPhaseAuditSuccessBranch,
)

# 导出所有类（供外部引用）
# v1.0: 移除 Legacy 类（logbook_db 已删除）
__all__ = [
    "TestTwoPhaseAuditSuccessBranch",
    "TestTwoPhaseAuditRedirectedBranch",
    "TestTwoPhaseAuditCorrelationIdConsistency",
    "TestTwoPhaseAuditSQLQueryContract",
]
