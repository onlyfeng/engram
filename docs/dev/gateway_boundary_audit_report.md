# Gateway 边界检查审计报告

> 生成时间: 2026-02-01  
> 执行环境: macOS Darwin 24.6.0, Python 3.13.2

---

## 执行摘要

| 检查项 | 状态 | 耗时 |
|--------|------|------|
| Gateway DI 边界检查 | ✅ 通过 | 684ms |
| 废弃 logbook_db 导入检查 | ✅ 通过 | 15.5s |
| Gateway Import Surface 检查 | ✅ 通过 | 419ms |
| deps.db Allowlist 校验 | ✅ 通过 | 449ms |
| ImportError 可选依赖契约测试 | ✅ 16/16 通过 | 6.11s |

**总体结论**: 所有检查通过，Gateway 模块边界符合规范。

---

## 1. Gateway DI 边界检查

**命令**: `make check-gateway-di-boundaries`

```
======================================================================
Gateway DI 边界检查
======================================================================

SSOT 文档: docs/architecture/gateway_module_boundaries.md
迁移阶段: removal

扫描目录:
  - src/engram/gateway/handlers
  - src/engram/gateway/services
扫描文件数: 10

[MODE] --disallow-allow-markers 已启用
       DI-BOUNDARY-ALLOW 标记本身将作为违规报告

[OK] 未发现 DI 边界违规
----------------------------------------------------------------------
违规总数: 0
警告总数: 0
过期 allowlist 条目: 0
统计: 违规 0 | 过期 0 | 即将过期 0 | 超期限 0
无效 id 引用: 0 | 无效 DEPS-DB-ALLOW: 0
被放行 DEPS-DB-ALLOW: 0
涉及文件: 0

[OK] DI 边界检查通过
```

---

## 2. 废弃 logbook_db 导入检查

**命令**: `make check-deprecated-logbook-db`

```
======================================================================
废弃导入扫描: engram.gateway.logbook_db
======================================================================

SSOT 文档: docs/architecture/deprecated_logbook_db_references_ssot.md

扫描目录:
  - src
  - tests
  - scripts

兼容目录（允许废弃导入）:
  - tests/logbook/test_logbook_db.py
  - tests/gateway/test_correlation_id_proxy.py

扫描文件数: 322

[OK] 未发现废弃的 logbook_db 导入
----------------------------------------------------------------------
违规总数: 0
涉及文件: 0

[OK] 废弃导入检查通过
```

---

## 3. Gateway Import Surface 检查

**命令**: `python scripts/ci/check_gateway_import_surface.py --verbose`

```
======================================================================
Gateway Import Surface 检查
======================================================================

检查文件: src/engram/gateway/__init__.py
懒加载子模块: logbook_adapter, openmemory_client, outbox_worker

懒加载策略检测:
  [OK] 检测到 TYPE_CHECKING 块（静态类型提示）
  [OK] 检测到 __getattr__ 懒加载函数

[OK] 未发现 eager-import 违规
----------------------------------------------------------------------
违规总数: 0

[OK] Gateway import surface 检查通过
```

---

## 4. deps.db Allowlist 校验

**命令**: `make check-gateway-deps-db-allowlist`

```
======================================================================
Gateway deps.db Allowlist 校验
======================================================================

项目根目录: /Users/a4399/Documents/ai/onlyfeng/engram
Allowlist 文件: scripts/ci/gateway_deps_db_allowlist.json
Schema 文件: schemas/gateway_deps_db_allowlist_v2.schema.json
条目数: 0

----------------------------------------------------------------------
统计: 条目 0 | 即将过期 0 | 超期限 0 | 已过期 0

[OK] Allowlist 校验通过
```

---

## 5. ImportError 可选依赖契约测试

**命令**: `pytest tests/gateway/test_importerror_optional_deps_contract.py -v`

```
============================= test session starts ==============================
platform darwin -- Python 3.13.2, pytest-9.0.2

tests/gateway/test_importerror_optional_deps_contract.py:
  TestImportSafetySubprocess::test_app_import_succeeds_in_minimal_env PASSED
  TestImportSafetySubprocess::test_health_endpoint_accessible_with_test_container PASSED
  TestEvidenceUploadDependencyMissing::test_evidence_upload_returns_structured_error_on_import_failure PASSED
  TestGatewayGracefulDegradation::test_gateway_app_factory_with_mocked_imports PASSED
  TestErrorResponseContract::test_dependency_missing_error_structure PASSED
  TestErrorResponseContract::test_missing_parameter_error_structure PASSED
  TestLifecycleModuleImportSafety::test_lifecycle_module_imports_successfully PASSED
  TestLifecycleModuleImportSafety::test_lifecycle_with_blocked_config_module PASSED
  TestCreateAppWithBlockedDependencies::test_create_app_without_container_params_defers_init PASSED
  TestCreateAppWithBlockedDependencies::test_create_app_with_blocked_logbook_adapter PASSED
  TestModuleImportBoundaries::test_mcp_rpc_import_without_openmemory_client PASSED
  TestModuleImportBoundaries::test_services_actor_validation_import_without_logbook_errors PASSED
  TestSysModulesPatchForImportBoundaries::test_evidence_upload_handler_with_sys_modules_patch PASSED
  TestIntegrationWithFakes::test_health_endpoint_with_fakes PASSED
  TestIntegrationWithFakes::test_mcp_options_preflight PASSED
  TestIntegrationWithFakes::test_evidence_upload_missing_content_returns_error PASSED

======================== 16 passed, 1 warning in 6.11s =========================
```

**警告**: `DeprecationWarning: There is no current event loop` (非阻断)

---

## 可选加严开关

以下开关可用于进一步加严检查，适合在迁移完成后或 CI 严格模式下启用：

### check_gateway_di_boundaries.py

| 开关 | 说明 | 当前状态 |
|------|------|----------|
| `--disallow-allow-markers` | 禁止 `DI-BOUNDARY-ALLOW` 标记，任何包含此标记的行都作为违规报告 | ✅ 已启用（Makefile 默认） |
| `--phase removal` | 将 `deps.db` 违规从警告升级为阻断 | ✅ 已启用（Makefile 默认） |
| `--fail-on-max-expiry` | 超过最大期限（180 天）时失败，而非仅警告 | ❌ 未启用 |
| `--scan-deprecated-imports` | 全仓扫描废弃的 `engram.gateway.logbook_db` 导入 | ✅ 独立目标启用 |

### check_gateway_deps_db_allowlist.py

| 开关 | 说明 | 当前状态 |
|------|------|----------|
| `--fail-on-max-expiry` | 超过最大期限（180 天）时失败 | ❌ 未启用 |
| `--expiring-soon-days N` | 调整即将过期预警天数（默认 14） | 默认值 |
| `--max-expiry-days N` | 调整最大过期期限天数（默认 180） | 默认值 |

### check_gateway_import_surface.py

| 开关 | 说明 | 当前状态 |
|------|------|----------|
| `--json` | JSON 格式输出（便于 CI 集成） | ❌ 未启用 |

---

## 建议的加严路线图

1. **短期**（迁移稳定后）:
   - 在 CI 中启用 `--fail-on-max-expiry` 防止过期条目积压

2. **中期**（完全移除 legacy 路径后）:
   - 移除 `gateway_deps_db_allowlist.json` 中的所有条目
   - 考虑移除 `--disallow-allow-markers` 检查（已无需要）

3. **长期**:
   - 简化检查脚本，移除过渡期兼容逻辑

---

## 相关文档

- [Gateway 模块边界](../architecture/gateway_module_boundaries.md)
- [废弃 logbook_db 引用 SSOT](../architecture/deprecated_logbook_db_references_ssot.md)
- [CI 门禁 Runbook](ci_gate_runbook.md)

---

*报告由自动化审计任务生成*
