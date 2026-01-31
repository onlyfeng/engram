# Iteration 5 Regression - CI 流水线验证记录

## 执行日期
2026-02-01 (更新)

## 任务概述
按 CI 顺序执行完整回归测试，记录实际执行结果，并按问题类型分类失败用例。

---

## 失败用例分类

### 本次测试结果摘要 (2026-02-01 更新)

| 测试套件 | 命令 | 结果 |
|----------|------|------|
| Gateway 全量 | `pytest tests/gateway/ -q` | 28 failed, 762 passed, 152 skipped |
| Gateway 启动 | `pytest tests/acceptance/test_gateway_startup.py -q` | 2 failed, 7 skipped |
| DI 边界门禁 | `pytest tests/gateway/test_di_boundaries.py -q` | **21 passed** ✅ |
| 审计契约门禁 | `pytest tests/gateway/test_audit_event_contract.py -q` | 7 failed, 74 passed |

---

### 1. 入口 import (Entry Import) - 2 个

模块级 `app = create_app()` 在导入时触发，缺少环境变量导致失败。

| 测试文件 | 测试用例 | 错误信息 |
|----------|----------|----------|
| `test_gateway_startup.py` | `TestGatewayAppImport::test_app_importable` | `ConfigError: 缺少必填环境变量: PROJECT_KEY, POSTGRES_DSN` |
| `test_gateway_startup.py` | `TestGatewayAppImport::test_app_is_fastapi` | 同上 |

**根因分析**:
```
from engram.gateway.main import app
  → main.py:309: app = create_app(lifespan=lifespan)
    → app.py:198: container = get_container()
      → container.py:258: _container = GatewayContainer.create()
        → container.py:88: config = get_config()
          → config.py:255: _config = load_config()
            → config.py:165: raise ConfigError("缺少必填环境变量...")
```

**修复方向**: 延迟 app 实例化，或在测试中设置环境变量夹具。

---

### 2. 启动链路 (Startup Chain) - 5 个

配置加载/DI 容器初始化链路中的环境变量或配置问题。

| 测试文件 | 测试用例 | 错误信息 |
|----------|----------|----------|
| `test_logbook_db.py` | `TestGatewayConfigIntegration::test_gateway_uses_config_dsn` | Patch 路径失效 |
| `test_logbook_db.py` | `TestCheckLogbookDbOnStartup::test_check_failure_with_auto_migrate_disabled` | Patch 路径失效 |
| `test_logbook_db.py` | `TestCheckLogbookDbOnStartupWithErrorCode::test_check_failure_logs_error_code` | Patch 路径失效 |
| `test_logbook_db.py` | `TestCheckLogbookDbOnStartupWithErrorCode::test_auto_migrate_enabled_passes_correct_flag` | Patch 路径失效 |
| `test_migrate_import.py` | `TestGatewayDbCheckPath::test_main_check_logbook_db_on_startup` | Patch 路径失效 |

**修复方向**: 更新 patch 路径以反映 DI 重构后的模块结构。

---

### 3. DI 边界 (DI Boundary) - 6 个

测试尝试 patch `get_config`/`get_client` 等全局函数，但这些函数已从 handler 模块中移除（符合 DI 边界设计）。

| 测试文件 | 测试用例 | 错误信息 |
|----------|----------|----------|
| `test_correlation_id_proxy.py` | `TestHandlerCorrelationIdRequirement::test_memory_store_requires_correlation_id` | `AttributeError: module has no attribute 'get_config'` |
| `test_correlation_id_proxy.py` | `TestHandlerCorrelationIdRequirement::test_memory_query_requires_correlation_id` | 同上 |
| `test_evidence_upload.py` | `TestEvidenceUploadMCP::test_evidence_upload_without_item_id_auto_creates_item` | 同上 |
| `test_evidence_upload.py` | `TestEvidenceUploadMCP::test_evidence_upload_with_explicit_item_id_does_not_create_item` | 同上 |
| `test_evidence_upload.py` | `TestEvidenceUploadMCP::test_evidence_upload_size_exceeded_via_mcp` | 同上 |
| `test_evidence_upload.py` | `TestEvidenceUploadMCP::test_evidence_upload_invalid_content_type_via_mcp` | 同上 |

**修复方向**: 改用 DI 注入依赖的测试方式（通过 `deps` 参数传递 mock 对象）。

---

### 4. 审计 schema (Audit Schema) - 7 个

`TestMemoryStoreImplAuditPayloadContract` 测试类尝试 patch `engram.gateway.handlers.memory_store.get_config`，但该函数已移除。

| 测试文件 | 测试用例 | 错误信息 |
|----------|----------|----------|
| `test_audit_event_contract.py` | `test_success_branch_audit_payload_schema` | `AttributeError: module has no attribute 'get_config'` |
| `test_audit_event_contract.py` | `test_policy_reject_branch_audit_payload_schema` | 同上 |
| `test_audit_event_contract.py` | `test_openmemory_failure_branch_audit_payload_schema` | 同上 |
| `test_audit_event_contract.py` | `test_dedup_hit_branch_audit_payload_schema` | 同上 |
| `test_audit_event_contract.py` | `test_redirect_branch_audit_payload_schema` | 同上 |
| `test_audit_event_contract.py` | `test_with_evidence_v2_audit_payload_schema` | 同上 |
| `test_audit_event_contract.py` | `test_correlation_id_consistency` | 同上 |

**修复方向**: 
1. 更新测试 patch 路径到正确位置（如 `engram.gateway.config.get_config`）
2. 或重构测试使用 DI 依赖注入方式

---

### 5. 数据不变量 (Data Invariants) - 8 个

测试夹具设置不完整，`UNKNOWN_ACTOR_POLICY` 环境变量残留导致配置加载失败。

| 测试文件 | 测试用例 | 错误信息 |
|----------|----------|----------|
| `test_validate_refs.py` | `TestValidateRefsConfig::test_config_validate_evidence_refs_default_false` | `ConfigError: UNKNOWN_ACTOR_POLICY 值无效: invalid_policy` |
| `test_validate_refs.py` | `TestValidateRefsConfig::test_config_validate_evidence_refs_true` | 同上 |
| `test_validate_refs.py` | `TestResolveValidateRefsMatrix::test_resolve_validate_refs_uses_global_config_when_none[True-strict]` | 同上 |
| `test_validate_refs.py` | `TestResolveValidateRefsMatrix::test_resolve_validate_refs_uses_global_config_when_none[True-compat]` | 同上 |
| `test_validate_refs.py` | `TestResolveValidateRefsMatrix::test_resolve_validate_refs_uses_global_config_when_none[True-None]` | 同上 |
| `test_validate_refs.py` | `TestResolveValidateRefsMatrix::test_resolve_validate_refs_uses_global_config_when_none[False-strict]` | 同上 |
| `test_validate_refs.py` | `TestResolveValidateRefsMatrix::test_resolve_validate_refs_uses_global_config_when_none[False-compat]` | 同上 |
| `test_validate_refs.py` | `TestResolveValidateRefsMatrix::test_resolve_validate_refs_uses_global_config_when_none[False-None]` | 同上 |

**根因**: 测试运行顺序导致环境变量污染（前一个测试设置 `UNKNOWN_ACTOR_POLICY=invalid_policy` 后未清理）。

**修复方向**: 
1. 在 fixture 中使用 `monkeypatch` 正确隔离环境变量
2. 或在测试后清理 `UNKNOWN_ACTOR_POLICY` 环境变量

---

### 6. 统一栈集成 (Unified Stack Integration) - 2 个

| 测试文件 | 测试用例 | 错误信息 |
|----------|----------|----------|
| `test_unified_stack_integration.py` | `TestAuditFirstSemantics::test_audit_failure_blocks_openmemory_integration` | Patch 路径失效 |
| `test_unified_stack_integration.py` | `TestAuditFirstSemantics::test_openmemory_failure_audit_outbox_consistency` | Patch 路径失效 |

**修复方向**: 更新 patch 路径以反映 DI 重构后的模块结构。

---

## 建议修复顺序

### Phase 1: 环境隔离 (最高优先级)
解决测试间环境变量污染问题，确保测试隔离。

```bash
# 回归命令
pytest tests/gateway/test_validate_refs.py -q
```

**修复项目**:
- `test_validate_refs.py` 环境变量夹具隔离

---

### Phase 2: 入口 import 链路
修复模块级 app 实例化导致的导入失败。

```bash
# 回归命令
pytest tests/acceptance/test_gateway_startup.py -q
```

**修复项目**:
- `engram.gateway.main` 延迟实例化
- 或添加测试环境变量夹具

---

### Phase 3: Patch 路径更新
批量更新测试中的 patch 路径以适配 DI 重构。

```bash
# 回归命令
pytest tests/gateway/test_audit_event_contract.py::TestMemoryStoreImplAuditPayloadContract -q
pytest tests/gateway/test_correlation_id_proxy.py -q
pytest tests/gateway/test_evidence_upload.py -q
pytest tests/gateway/test_logbook_db.py -q
pytest tests/gateway/test_unified_stack_integration.py -q
```

**修复原则**:
- `HANDLER_MODULE.get_config` → `engram.gateway.config.get_config`
- `HANDLER_MODULE.get_client` → `engram.gateway.openmemory_client.get_client`
- `HANDLER_MODULE.logbook_adapter` → `engram.logbook.adapter`
- 或改用 DI 依赖注入测试模式

---

### Phase 4: 全量回归

```bash
# 完整 gateway 测试
pytest tests/gateway/ -q

# DI 边界门禁（应始终通过）
pytest tests/gateway/test_di_boundaries.py -q

# 审计契约门禁
pytest tests/gateway/test_audit_event_contract.py -q

# 验收测试
pytest tests/acceptance/test_gateway_startup.py -q
```

---

## CI 命令执行顺序

按照 `.github/workflows/ci.yml` 定义的顺序，CI 流水线包含以下检查：

| 序号 | 命令 | 说明 |
|------|------|------|
| 1 | `make ci` | 运行所有静态检查（lint、format、typecheck、schema、env、logbook、migration、scm-sync） |
| 2 | `python -m engram.logbook.cli.db_migrate --verify --verify-strict` | 数据库迁移 + 严格权限验证 |
| 3 | `pytest tests/gateway` | Gateway 单元测试 |
| 4 | `pytest tests/acceptance` | 验收测试 |
| 5 | `python scripts/verify_logbook_consistency.py` | Logbook 配置一致性检查 |
| 6 | `python scripts/verify_scm_sync_consistency.py` | SCM Sync 一致性检查 |
| 7 | `python scripts/ci/check_env_var_consistency.py` | 环境变量一致性检查 |

### `make ci` 包含的子检查
```makefile
ci: lint format-check typecheck check-schemas check-env-consistency check-logbook-consistency check-migration-sanity check-scm-sync-consistency
```

---

## 历史执行结果

### 1. `make ci` - 静态检查
- **状态**: ❌ FAIL
- **失败阶段**: lint (ruff check)
- **错误统计**: 20 errors

| 错误类型 | 数量 | 说明 | 自动修复 |
|----------|------|------|----------|
| I001 | 6 | unsorted-imports | ✅ `--fix` |
| F401 | 5 | unused-import | ✅ `--fix` |
| W293 | 4 | blank-line-with-whitespace | ✅ `--fix` |
| F841 | 3 | unused-variable | ❌ 手动 |
| E402 | 1 | module-import-not-at-top-of-file | ❌ 手动/忽略 |
| E741 | 1 | ambiguous-variable-name | ❌ 手动 |

**主要问题文件**:
- `src/engram/gateway/handlers/memory_store.py`: 未使用的 TYPE_CHECKING 导入
- `src/engram/logbook/scm_sync_tasks/gitlab_commits.py`: import 排序、未使用导入、空行空格
- `src/engram/logbook/scm_sync_tasks/gitlab_mrs.py`: import 排序、空行空格
- `src/engram/logbook/scm_sync_tasks/svn.py`: import 排序、未使用变量
- `src/engram/logbook/scm_sync_runner.py`: 模块级导入位置

**排查链接**: [Ruff Rules](https://docs.astral.sh/ruff/rules/)

---

### 2. `db_migrate --verify --verify-strict` - 数据库迁移
- **状态**: ⏭️ SKIPPED
- **原因**: 需要数据库连接，本次执行无可用数据库环境
- **命令**:
  ```bash
  python -m engram.logbook.cli.db_migrate \
    --dsn "postgresql://postgres:postgres@localhost:5432/engram" \
    --verify --verify-strict
  ```

---

### 3. `pytest tests/gateway` - Gateway 测试
- **状态**: ❌ FAIL
- **结果**: 28 failed, 762 passed, 152 skipped
- **耗时**: 9.16s

**失败测试分类**:

| 失败数 | 测试文件 | 问题类型 |
|--------|----------|----------|
| 7 | test_audit_event_contract.py | Handler 审计合约 patch 路径 |
| 2 | test_correlation_id_proxy.py | DI 边界 patch 路径 |
| 4 | test_evidence_upload.py | DI 边界 patch 路径 |
| 4 | test_logbook_db.py | 启动链路 patch 路径 |
| 1 | test_migrate_import.py | 启动链路 patch 路径 |
| 2 | test_unified_stack_integration.py | 统一栈 patch 路径 |
| 8 | test_validate_refs.py | 环境变量污染 |

**排查链接**: `docs/gateway/06_gateway_design.md`, `docs/architecture/adr_gateway_di_and_entry_boundary.md`

---

### 4. `pytest tests/acceptance` - 验收测试
- **状态**: ❌ FAIL
- **结果**: 2 failed, 7 skipped
- **耗时**: 0.47s

**失败测试**:
1. `test_gateway_startup.py::TestGatewayAppImport::test_app_importable` - App 导入检查
2. `test_gateway_startup.py::TestGatewayAppImport::test_app_is_fastapi` - FastAPI 类型检查

**排查链接**: `docs/architecture/cli_entrypoints.md`

---

### 5. `python scripts/verify_logbook_consistency.py` - Logbook 一致性
- **状态**: ✅ PASS
- **检查项**: 5/5 通过

| 检查项 | 状态 | 说明 |
|--------|------|------|
| [A] compose/logbook.yml 默认值 | ✅ | 缺省 .env 下不会致命失败 |
| [B] Logbook-only 验收目标 | ✅ | Makefile 包含必要目标 |
| [C] docs 验收命令一致性 | ✅ | 文档与 Makefile 对齐 |
| [D] README 命令记录 | ✅ | 数据库初始化命令正确 |
| [F] 验收标准对齐 | ✅ | 04_acceptance_criteria.md 对齐 |

---

### 6. `python scripts/verify_scm_sync_consistency.py` - SCM Sync 一致性
- **状态**: ✅ PASS
- **检查项**: 6/6 通过

| 检查项 | 状态 | 说明 |
|--------|------|------|
| [A] SQL 文件存在性 | ✅ | 所有必需 SQL 文件存在 |
| [B] Pyproject 入口配置 | ✅ | 10 个 CLI 入口正确配置 |
| [C] 模块导入检查 | ✅ | 核心模块可正常导入 |
| [D] Docker Compose 服务 | ✅ | 15 个服务配置正确 |
| [E] CLI 入口对照表 | ✅ | pyproject.toml 一致 |
| [F] 根目录 wrapper 移除 | ✅ | 8 个旧 wrapper 已清理 |

**警告**:
- `engram-artifacts` 在 pyproject.toml 中定义但文档未列出
- `engram-bootstrap-roles` 在 pyproject.toml 中定义但文档未列出

---

### 7. `python scripts/ci/check_env_var_consistency.py` - 环境变量一致性
- **状态**: ✅ PASS (2 WARN)
- **统计**: 0 errors, 2 warnings

| 来源 | 变量数 |
|------|--------|
| .env.example | 39 |
| 文档 | 161 |
| 代码 | 42 |

**警告详情**:
- `ENGRAM_VERIFY_GATE`: 文档记录但 .env.example 和代码中未使用
- `ENGRAM_VERIFY_STRICT`: 文档记录但 .env.example 和代码中未使用

---

## 执行结果汇总

| 序号 | 检查项 | 状态 | 说明 |
|------|--------|------|------|
| 1 | `make ci` | ❌ FAIL | lint 阶段 20 errors |
| 2 | `db_migrate --verify-strict` | ⏭️ SKIP | 需要数据库 |
| 3 | `pytest tests/gateway` | ❌ FAIL | 28 failed |
| 4 | `pytest tests/acceptance` | ❌ FAIL | 2 failed |
| 5 | `verify_logbook_consistency.py` | ✅ PASS | 5/5 |
| 6 | `verify_scm_sync_consistency.py` | ✅ PASS | 6/6 |
| 7 | `check_env_var_consistency.py` | ✅ PASS | 2 WARN |

---

## 修复优先级总结

### 高优先级 (CI Blocker)
1. **环境变量隔离**: `test_validate_refs.py` fixture 修复
2. **入口 import 链路**: `engram.gateway.main` 延迟实例化
3. **Patch 路径更新**: 批量更新 DI 重构后的 patch 路径

### 中优先级
1. **Lint 错误修复**:
   ```bash
   ruff check --fix src/ tests/
   ```
2. 更新 E402 的 per-file-ignores 配置
3. 补充 CLI 入口文档（engram-artifacts, engram-bootstrap-roles）

### 低优先级
1. 清理 ENGRAM_VERIFY_GATE/ENGRAM_VERIFY_STRICT 文档或实现

---

## 验证命令

```bash
# 完整 CI 检查
make ci

# 数据库迁移（需要数据库环境）
make migrate-ddl
make verify-permissions-strict

# 测试
pytest tests/gateway/ -q
pytest tests/acceptance/ -q

# 门禁测试（应优先通过）
pytest tests/gateway/test_di_boundaries.py -q
pytest tests/gateway/test_audit_event_contract.py -q

# 一致性检查
python scripts/verify_logbook_consistency.py --verbose
python scripts/verify_scm_sync_consistency.py --verbose
python scripts/ci/check_env_var_consistency.py --verbose
```

---

## 相关文档
- [CI 流水线配置](../../.github/workflows/ci.yml)
- [Iteration 4 回归记录](iteration_4_regression.md)
- [Gateway 设计](../gateway/06_gateway_design.md)
- [CLI 入口文档](../architecture/cli_entrypoints.md)
- [DI 边界 ADR](../architecture/adr_gateway_di_and_entry_boundary.md)
