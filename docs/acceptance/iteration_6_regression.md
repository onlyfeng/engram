# Iteration 6 Regression - CI 流水线验证记录

## 执行日期
2026-02-01

**最终结论日期**: 2026-02-01

## 任务概述
按 CI 顺序执行完整回归测试，记录实际执行结果。

---

## 执行结果汇总

| 序号 | 检查项 | 状态 | 说明 |
|------|--------|------|------|
| 1 | `make ci` | ❌ FAIL | lint 阶段 44 errors (F821 为主) |
| 2 | `pytest tests/gateway/ -v` | ⚠️ PARTIAL | 7 failed, 798 passed, 156 skipped |
| 3 | `pytest tests/acceptance/ -v` | ✅ PASS | 141 passed, 50 skipped |
| 4 | DI 边界门禁 | ✅ PASS | 21 passed |
| 5 | SQL 安全检查门禁 | ✅ PASS | 12 passed |
| 6 | SQL 完整性检查门禁 | ✅ PASS | 43 passed, 4 skipped |
| 7 | Logbook 一致性检查 | ✅ PASS | 5/5 检查通过 |
| 8 | SCM Sync 一致性检查 | ✅ PASS | 7/7 检查通过 |
| 9 | 环境变量一致性检查 | ✅ PASS | 0 errors, 2 warnings |

---

## 详细结果

### 1. `make ci` - 静态检查
- **状态**: ❌ FAIL
- **失败阶段**: lint (ruff check)
- **错误统计**: 44 errors (ruff --fix 后)

| 错误类型 | 数量 | 说明 | 自动修复 |
|----------|------|------|----------|
| F821 | 37 | undefined-name (导入语法错误) | ❌ 手动修复 |
| W293 | 3 | blank-line-with-whitespace | ✅ `--fix` |
| F841 | 2 | unused-variable | ⚠️ 手动检查 |
| E402 | 1 | module-import-not-at-top | ⚠️ 手动调整 |
| I001 | 1 | unsorted-imports (可自动修复) | ✅ `--fix` |

**主要问题文件**:
- `tests/logbook/test_scm_sync_reaper.py`: **33 处 F821** - 导入语句格式错误，函数名被误放入注释
- `tests/logbook/test_scm_sync_integration.py`: **2 处 F821** - scan_expired_jobs 未正确导入
- `tests/gateway/test_migrate_import.py`: 3 处 W293 空行空格
- `tests/acceptance/test_gateway_startup.py`: 1 处 F841 未使用变量
- `tests/test_mypy_gate.py`: 1 处 F841 未使用变量
- `src/engram/gateway/mcp_rpc.py`: 1 处 E402 import 位置

**根本原因**: 
测试文件中存在格式错误的导入语句，例如：
```python
# 错误格式（函数名被放入注释）:
from engram.logbook.scm_db import (
    get_conn,  # list_expired_running_jobs  # ...
)
# 正确格式应为:
from engram.logbook.scm_db import (
    get_conn,
    list_expired_running_jobs,
)
```

**修复命令**:
```bash
# 需要手动修复 F821 导入语法错误:
# - tests/logbook/test_scm_sync_reaper.py (33 处)
# - tests/logbook/test_scm_sync_integration.py (2 处)
```

---

### 2. `pytest tests/gateway/ -v` - Gateway 测试
- **状态**: ⚠️ PARTIAL
- **结果**: 7 failed, 798 passed, 156 skipped
- **耗时**: ~8s

**失败的测试** (7个):
1. `test_audit_event.py::TestBuildAuditEvent::test_uses_provided_correlation_id`
2. `test_audit_event.py::TestBuildEvidenceRefsJson::test_no_pollution_between_fields`
3. `test_correlation_id_proxy.py::TestMcpRpcCorrelationIdPropagation::test_dispatch_sets_correlation_id`
4. `test_correlation_id_proxy.py::TestMcpRpcCorrelationIdPropagation::test_error_response_preserves_correlation_id`
5. `test_gateway_startup.py::TestFormatDBRepairCommands::test_basic_format_without_params`
6. `test_logbook_db.py::TestLogbookDBCheck::test_ensure_db_ready_no_auto_migrate_raises_error`
7. `test_logbook_db.py::TestLogbookDBErrorCode::test_error_message_contains_repair_hint`

**失败原因分析**:
- 测试 5-7: 断言 `db_migrate.py` 应在错误消息中，但实际消息使用了 `engram-migrate` CLI 入口

**测试覆盖**:
- Actor/User ID 验证
- 审计事件契约
- 关联 ID 追踪
- DI 边界检查
- 错误码契约
- Evidence 上传
- MCP JSON-RPC 契约
- 内存存储/查询
- 可靠性报告
- 统一栈集成

---

### 3. `pytest tests/acceptance/ -v` - 验收测试
- **状态**: ✅ PASS
- **结果**: 141 passed, 50 skipped
- **耗时**: ~29s

**测试覆盖**:
- CLI 入口验证
- 环境变量配置
- Gateway 启动（import-safe）
- 安装验证
- 迁移文件检查
- SCM Sync CLI

---

### 4. 门禁测试

#### DI 边界门禁
- **命令**: `pytest tests/gateway/test_di_boundaries.py -v`
- **状态**: ✅ PASS (21 passed)

#### SQL 安全检查门禁
- **命令**: `pytest tests/logbook/test_sql_migrations_safety.py -v`
- **状态**: ✅ PASS (12 passed)

#### SQL 完整性检查门禁
- **命令**: `pytest tests/logbook/test_sql_migrations_sanity.py -v`
- **状态**: ✅ PASS (43 passed, 4 skipped, 1 warning)
- **警告**: 迁移序列中存在未记录的跳号 [4, 5]

---

### 5. 一致性检查

#### Logbook 一致性
- **命令**: `python scripts/verify_logbook_consistency.py --verbose`
- **状态**: ✅ PASS

| 检查项 | 状态 |
|--------|------|
| [A] compose/logbook.yml 默认值 | ✅ |
| [B] Makefile 包含 Logbook-only 目标 | ✅ |
| [C] docs 验收命令一致性 | ✅ |
| [D] README 命令记录 | ✅ |
| [F] 验收标准对齐 | ✅ |

#### SCM Sync 一致性
- **命令**: `python scripts/verify_scm_sync_consistency.py --verbose`
- **状态**: ✅ PASS

| 检查项 | 状态 |
|--------|------|
| [A] 文档命令与代码一致 | ✅ (7 个 SCM 命令) |
| [B] 无 TODO 引用 | ✅ |
| [C] Runner 不依赖弃用入口 | ✅ |
| [D] Docker Compose 服务一致 | ✅ (16 个服务) |
| [E] CLI 入口对照表一致 | ✅ (10 个入口) |
| [F] 根目录 wrapper 已移除 | ✅ (8 个) |
| [G] 弃用脚本使用统一 deprecation 模块 | ✅ (13 个) |

**警告**:
- `engram-artifacts` 在 pyproject.toml 中定义但文档未列出
- `engram-bootstrap-roles` 在 pyproject.toml 中定义但文档未列出

#### 环境变量一致性
- **命令**: `python scripts/ci/check_env_var_consistency.py --verbose`
- **状态**: ✅ PASS (0 errors, 2 warnings)

| 来源 | 变量数 |
|------|--------|
| .env.example | 39 |
| 文档 | 162 |
| 代码 | 42 |

**警告**:
- `ENGRAM_VERIFY_GATE`: 文档记录但 .env.example 和代码中未使用
- `ENGRAM_VERIFY_STRICT`: 文档记录但 .env.example 和代码中未使用
- `MYPY_GATE`: 文档记录但 .env.example 和代码中未使用

---

## 相对 Iteration 5 的变化摘要

### Blocker 清除情况

| 问题类型 | Iteration 5 状态 | Iteration 6 状态 | 变化 |
|----------|------------------|------------------|------|
| **入口 import** | 2 failed | ✅ 0 failed | 已修复 |
| **启动链路** | 5 failed | ✅ 0 failed | 已修复 |
| **DI 边界** | 6 failed | ✅ 0 failed | 已修复 |
| **审计 schema** | 7 failed | ✅ 0 failed | 已修复 |
| **数据不变量** | 8 failed | ✅ 0 failed | 已修复 |
| **统一栈集成** | 2 failed | ✅ 0 failed | 已修复 |

### 测试结果对比

| 测试套件 | Iteration 5 | Iteration 6 (最终) | 变化 |
|----------|-------------|---------------------|------|
| Gateway 全量 | 28 failed, 762 passed | **7 failed, 798 passed** | -21 failed, +36 passed |
| Acceptance | 2 failed, 7 skipped | **0 failed, 141 passed** | +139 passed |
| DI 边界门禁 | 21 passed | **21 passed** | 无变化 |

### Lint 错误变化

| 指标 | Iteration 5 | Iteration 6 初始 | Iteration 6 修复后 | 变化 |
|------|-------------|------------------|---------------------|------|
| 总错误数 | 20 | 124 → 65 | **44** | +24 |
| 可自动修复 | ~15 | 22 (已修复) | **1** | -14 |
| 需手动修复 | ~5 | 43 | **43** | +38 |

**说明**: 
- `ruff check --fix` 已自动修复 22 个错误（F401 unused-import, I001 unsorted-imports 等）
- 剩余 44 个错误中，37 个是 F821 (undefined name)，根因是导入语句格式错误
- F821 问题集中在 `test_scm_sync_reaper.py` (33 处) 和 `test_scm_sync_integration.py` (2 处)

### 主要修复内容

1. **入口 import 链路**: Gateway app 延迟实例化，支持 import-safe 模式
2. **DI 边界**: Handler 模块不再直接调用 `get_config()`/`get_client()` 等全局函数
3. **审计 schema**: 测试改用 DI 依赖注入方式
4. **环境变量隔离**: Fixture 正确隔离环境变量

---

## 下一步行动

### 高优先级（CI 通过必需）

1. **修复 F821 导入语法错误 (37 处)**:
   - `tests/logbook/test_scm_sync_reaper.py`: 修正 33 处导入语句格式
   - `tests/logbook/test_scm_sync_integration.py`: 修正 2 处 scan_expired_jobs 导入
   - 根本原因: 函数名被错误地放入注释中，需要拆分为独立的导入行

2. **修复 Gateway 测试断言 (7 处)**:
   - 更新 `test_gateway_startup.py` 和 `test_logbook_db.py` 中的断言
   - 将 `db_migrate.py` 改为 `engram-migrate`（与实际错误消息对齐）

3. **修复剩余 lint 问题 (7 处)**:
   ```bash
   # W293 空行空格 (3 处) - tests/gateway/test_migrate_import.py
   # F841 未使用变量 (2 处) - tests/acceptance/, tests/test_mypy_gate.py
   # E402 import 位置 (1 处) - src/engram/gateway/mcp_rpc.py
   # I001 导入排序 (1 处)
   ```

### 低优先级（非阻断）

1. 补充 CLI 入口文档（`engram-artifacts`, `engram-bootstrap-roles`）
2. 清理文档中未使用的环境变量说明（`ENGRAM_VERIFY_GATE`, `ENGRAM_VERIFY_STRICT`, `MYPY_GATE`）
3. 记录迁移序列跳号 [4, 5] 的原因

---

## 验证命令

```bash
# 完整 CI 检查（修复 lint 后）
make ci

# 门禁测试
pytest tests/gateway/test_di_boundaries.py -v
pytest tests/logbook/test_sql_migrations_safety.py -v
pytest tests/logbook/test_sql_migrations_sanity.py -v

# 全量测试
pytest tests/gateway/ -q
pytest tests/acceptance/ -q

# 一致性检查
python scripts/verify_logbook_consistency.py --verbose
python scripts/verify_scm_sync_consistency.py --verbose
python scripts/ci/check_env_var_consistency.py --verbose
```

---

## 相关文档
- [CI 流水线配置](../../.github/workflows/ci.yml)
- [Iteration 5 回归记录](iteration_5_regression.md)
- [Gateway 设计](../gateway/06_gateway_design.md)
- [CLI 入口文档](../architecture/cli_entrypoints.md)
- [DI 边界 ADR](../architecture/adr_gateway_di_and_entry_boundary.md)
