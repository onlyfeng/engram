# Iteration 9 Regression - CI 流水线验证记录

## 执行信息

| 项目 | 值 |
|------|-----|
| **执行日期** | 2026-02-01 |
| **执行环境** | 本机 (darwin 24.6.0) |
| **Python 版本** | 3.13.2 |
| **pytest 版本** | 9.0.2 |
| **执行者** | Cursor Agent |
| **CI 运行 ID** | - |

---

## 执行结果总览

| 序号 | 测试阶段 | 命令 | 状态 | 详情 |
|------|----------|------|------|------|
| 1 | make regression | `make regression` | ❌ FAIL | mypy baseline gate 失败 |
| 2 | Gateway 单元测试 | `pytest tests/gateway/ -v` | ⚠️ PARTIAL | 4 失败, 813 通过, 156 跳过 |
| 3 | Acceptance 测试 | `pytest tests/acceptance/ -v` | ✅ PASS | 143 通过, 50 跳过, 0 失败 |

---

## 详细执行记录

### 1. make regression

**命令**: `make regression`

**状态**: ❌ FAIL

**执行流程**:

| 步骤 | 检查项 | 状态 | 说明 |
|------|--------|------|------|
| 1 | `ruff check src/ tests/` | ✅ PASS | 已自动修复 52 个错误 + 手动修复 6 个 |
| 2 | `ruff format --check` | ✅ PASS | 已格式化 6 个文件 |
| 3 | `typecheck-gate` (mypy baseline) | ❌ FAIL | 存在 77 个新增错误 |

#### 1.1 Ruff 修复记录

**修复方式**: 
- 自动修复: `ruff check src/ tests/ --fix` (52 个)
- 手动修复: 6 个无法自动修复的问题

**手动修复的文件**:

| 文件 | 错误类型 | 修复方式 |
|------|----------|----------|
| `src/engram/gateway/mcp_rpc.py` | E402 | 将 `import re` 移至文件顶部 |
| `tests/acceptance/test_gateway_startup.py` | F841 | 移除未使用变量 `original_client` |
| `tests/test_mypy_baseline_policy.py` | W291 | 移除尾随空白 |
| `tests/test_mypy_gate.py` (2处) | F841 | 移除未使用变量 `exit_code` |
| `tests/test_no_root_wrappers_allowlist.py` | F841 | 移除未使用变量 `exit_code` |

**额外修复**:
- `src/engram/gateway/audit_event.py`: 重新导出 `generate_correlation_id` 以保持向后兼容

#### 1.2 Mypy Baseline Gate 失败详情

**命令**: 
```bash
python3 scripts/ci/check_mypy_gate.py --gate baseline --mypy-path src/engram/
```

**错误统计**:
- 门禁级别: baseline
- 当前错误数: 126
- 基线错误数: 328
- 已修复: 279 个
- **新增错误: 77 个**

**新增错误涉及的主要模块**:

| 模块路径 | 错误数量 | 主要错误类型 |
|----------|----------|--------------|
| `src/engram/logbook/gitlab_client.py` | 15+ | no-any-return, assignment, name-defined |
| `src/engram/logbook/artifact_gc.py` | 5 | call-arg, assignment |
| `src/engram/logbook/artifact_store.py` | 10+ | no-any-return, assignment, union-attr |
| `src/engram/logbook/cli/db_bootstrap.py` | 6 | arg-type, call-overload |
| `src/engram/logbook/cli/scm.py` | 4 | no-any-return, attr-defined |
| `src/engram/logbook/scm_db.py` | 6 | no-any-return |
| `src/engram/logbook/scm_integrity_check.py` | 6 | arg-type |
| `src/engram/gateway/logbook_db.py` | 2 | no-any-return, truthy-function |
| `src/engram/gateway/minio_audit_webhook.py` | 1 | no-any-return |

**Artifact 输出**:
- `artifacts/mypy_current.txt` - 126 条当前错误
- `artifacts/mypy_new_errors.txt` - 77 条新增错误

---

### 2. Gateway 单元测试

**命令**: `pytest tests/gateway/ -v`

**状态**: ⚠️ PARTIAL (4 失败)

**统计**:
- 通过: 813
- 失败: 4
- 跳过: 156
- 执行时间: 11.61s

**失败用例**:

| 测试用例 | 文件 | 失败原因 |
|----------|------|----------|
| `test_uses_provided_correlation_id` | `test_audit_event.py:833` | AssertionError |
| `test_no_pollution_between_fields` | `test_audit_event.py` | AssertionError |
| `test_ensure_db_ready_no_auto_migrate_raises_error` | `test_logbook_db.py:407` | 错误消息不包含 "db_migrate.py" |
| `test_error_message_contains_repair_hint` | `test_logbook_db.py:727` | 错误消息不包含 "db_migrate.py" |

**失败分析**:

1. **test_audit_event.py 失败** (2个):
   - 与 correlation_id 相关的测试断言失败
   - 可能需要检查 `generate_correlation_id` 的导出和使用

2. **test_logbook_db.py 失败** (2个):
   - 错误消息格式已更新，不再包含 "db_migrate.py"
   - 新的错误消息使用 `engram-migrate` 命令格式
   - 需要更新测试断言以匹配新的消息格式

---

### 3. Acceptance 测试

**命令**: `pytest tests/acceptance/ -v`

**状态**: ✅ PASS

**统计**:
- 通过: 143
- 失败: 0
- 跳过: 50
- 执行时间: 27.32s

**跳过原因**: 主要是需要数据库连接的测试 (`@pytest.mark.requires_db`)

**测试覆盖模块**:

| 测试文件 | 通过 | 跳过 |
|----------|------|------|
| test_cli.py | 全部 | 0 |
| test_env_config.py | 全部 | 0 |
| test_gateway_startup.py | 27 | 8 |
| test_installation.py | 全部 | 0 |
| test_migration.py | 3 | 10 |
| test_scm_sync_cli.py | 全部 | 0 |

---

## 修复建议

### P0 - CI 阻断（必须修复）

1. **Mypy 新增错误** (77个)
   - 选项 A: 逐一修复类型标注问题
   - 选项 B: 更新 mypy baseline 文件（需要 reviewer 批准）
   
   更新基线命令:
   ```bash
   python scripts/ci/check_mypy_gate.py --write-baseline
   ```

### P1 - 测试修复

2. **test_logbook_db.py 断言更新** (2个失败)
   - 更新断言: `"db_migrate.py"` → `"engram-migrate"` 或移除该断言
   - 文件: `tests/gateway/test_logbook_db.py` 第 407 行和第 727 行

3. **test_audit_event.py 断言检查** (2个失败)
   - 检查 correlation_id 相关的测试逻辑
   - 可能需要调整测试预期或修复代码

---

## 失败修复追踪

| 失败项 | 类型 | 涉及文件 | 修复 PR/Commit | 状态 |
|--------|------|----------|----------------|------|
| mypy baseline 77 新增错误 | 类型检查 | `src/engram/logbook/*.py` | TBD | ⏳ 待修复 |
| test_uses_provided_correlation_id | 测试失败 | `tests/gateway/test_audit_event.py:833` | TBD | ⏳ 待修复 |
| test_no_pollution_between_fields | 测试失败 | `tests/gateway/test_audit_event.py` | TBD | ⏳ 待修复 |
| test_ensure_db_ready_no_auto_migrate_raises_error | 测试失败 | `tests/gateway/test_logbook_db.py:407` | TBD | ⏳ 待修复 |
| test_error_message_contains_repair_hint | 测试失败 | `tests/gateway/test_logbook_db.py:727` | TBD | ⏳ 待修复 |

---

## 下一步

### 立即行动（Iteration 10）

1. **修复 4 个测试失败**
   - 更新 `test_logbook_db.py` 中的断言以匹配新的 `engram-migrate` 命令格式
   - 调查 `test_audit_event.py` 中 correlation_id 相关的断言失败

2. **处理 mypy baseline**
   - 选项 A (推荐): 更新基线文件，允许已有错误
   - 选项 B: 逐步修复 77 个新增类型错误

3. **验证 CI 通过**
   ```bash
   make ci && pytest tests/gateway/ -v && pytest tests/acceptance/ -v
   ```

### 中期目标

- 将 mypy 错误数降至 < 100
- 扩展 strict-island 覆盖率至 50%
- 完成所有 P1 测试修复

---

## 已修改文件列表

本次验证过程中修改的文件:

| 文件 | 修改类型 |
|------|----------|
| `src/engram/gateway/mcp_rpc.py` | 移动 import 到文件顶部 |
| `src/engram/gateway/audit_event.py` | 重新导出 generate_correlation_id |
| `tests/acceptance/test_gateway_startup.py` | 移除未使用变量 |
| `tests/test_mypy_baseline_policy.py` | 移除尾随空白 |
| `tests/test_mypy_gate.py` | 移除未使用变量 (2处) |
| `tests/test_no_root_wrappers_allowlist.py` | 移除未使用变量 |
| 多个文件 | ruff 自动格式化 |

---

## 相关文档

- [Iteration 7 回归记录](iteration_7_regression.md)
- [Mypy 基线文档](../dev/mypy_baseline.md)
- [CLI 入口文档](../architecture/cli_entrypoints.md)
- [No Root Wrappers 迁移映射](../architecture/no_root_wrappers_migration_map.md)
