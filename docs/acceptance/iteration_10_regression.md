> **⚠️ Superseded by Iteration 11**
>
> 本文档已被 [Iteration 11 回归记录](iteration_11_regression.md) 取代。

# Iteration 10 Regression - CI 流水线验证记录

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
| 1 | make ci | `make ci` | ❌ FAIL | mypy baseline gate 失败 (86 新增错误) |
| 2 | Gateway 单元测试 | `pytest tests/gateway/ -q` | ⚠️ PARTIAL | 15 失败, 807 通过, 156 跳过 |
| 3 | Acceptance 测试 | `pytest tests/acceptance/ -q` | ✅ PASS | 158 通过, 50 跳过, 0 失败 |

---

## 详细执行记录

### 1. make ci

**命令**: `make ci`

**状态**: ❌ FAIL

**执行流程**:

| 步骤 | 检查项 | 状态 | 说明 |
|------|--------|------|------|
| 1 | `ruff check src/ tests/` | ✅ PASS | All checks passed |
| 2 | `ruff format --check` | ✅ PASS | 215 files already formatted |
| 3 | `typecheck-gate` (mypy baseline) | ❌ FAIL | 存在 86 个新增错误 |

#### 1.1 Mypy Baseline Gate 失败详情

**错误统计**:
- 门禁级别: baseline
- 当前错误数: 135
- 基线错误数: 328
- 已修复: 279 个
- **新增错误: 86 个**

**新增错误涉及的主要模块**:

| 模块路径 | 主要错误类型 |
|----------|--------------|
| `src/engram/gateway/app.py` | Missing named argument "error_code" |
| `src/engram/gateway/evidence_store.py` | Incompatible types |
| `src/engram/gateway/logbook_db.py` | Returning Any, truthy-function |
| `src/engram/logbook/artifact_delete.py` | ParsedUri has no attribute |
| `src/engram/logbook/artifact_gc.py` | Unexpected keyword argument, Incompatible types |
| `src/engram/logbook/artifact_store.py` | Returning Any, Incompatible types, union-attr |
| `src/engram/logbook/cli/db_bootstrap.py` | arg-type, call-overload |
| `src/engram/logbook/cli/scm.py` | no-any-return, attr-defined |
| `src/engram/logbook/gitlab_client.py` | no-any-return, assignment, name-defined |
| `src/engram/logbook/scm_db.py` | no-any-return |
| `src/engram/logbook/scm_integrity_check.py` | arg-type |

---

### 2. Gateway 单元测试

**命令**: `pytest tests/gateway/ -q`

**状态**: ⚠️ PARTIAL (15 失败)

**统计**:
- 通过: 807
- 失败: 15
- 跳过: 156
- 执行时间: 15.75s

**失败用例分类**:

#### 2.1 test_audit_event.py (2 失败)

| 测试用例 | 失败原因 |
|----------|----------|
| `test_uses_provided_correlation_id` | correlation_id 被覆盖而非使用提供的值 |
| `test_no_pollution_between_fields` | 同上，gateway_event 的 correlation_id 被自动生成覆盖 |

**根本原因**: `build_gateway_audit_event` 或 `build_audit_event` 函数可能总是生成新的 correlation_id，而非使用传入的值。

#### 2.2 test_logbook_db.py (2 失败)

| 测试用例 | 失败原因 |
|----------|----------|
| `test_ensure_db_ready_no_auto_migrate_raises_error` | 错误消息已改为 `engram-migrate` 而非 `db_migrate.py` |
| `test_error_message_contains_repair_hint` | 同上 |

**根本原因**: 代码已更新使用新的 CLI 命令名称，但测试断言仍检查旧名称。

#### 2.3 test_mcp_jsonrpc_contract.py (11 失败)

| 测试用例 | 失败原因 |
|----------|----------|
| `test_tools_call_reliability_report` | `engram.gateway.app` 没有 `get_reliability_report` 属性 |
| `test_legacy_format_with_empty_arguments` | 同上 |
| `test_legacy_protocol_success_response_has_correlation_id` | 同上 |
| `test_legacy_protocol_success_correlation_id_pattern` | 同上 |
| `test_legacy_reliability_report_tool` | 同上 |
| `test_legacy_response_has_correlation_id` | 同上 |
| `test_legacy_extra_fields_ignored` | 同上 |
| `test_legacy_request_gets_legacy_response` | 同上 |
| `test_tools_call_reliability_report_has_correlation_id` | 同上 |
| `test_legacy_reliability_report_has_correlation_id` | 同上 |
| `test_all_tools_have_consistent_correlation_id_behavior` | 同上 |

**根本原因**: 测试尝试 patch `engram.gateway.app.get_reliability_report`，但该函数在 `app.py` 中不存在或未导入。可能是 `get_reliability_report` 的位置已移动或重构。

---

### 3. Acceptance 测试

**命令**: `pytest tests/acceptance/ -q`

**状态**: ✅ PASS

**统计**:
- 通过: 158
- 失败: 0
- 跳过: 50
- 执行时间: 33.89s

**跳过原因**: 主要是需要数据库连接的测试 (`@pytest.mark.requires_db`)

---

## 失败修复建议

### P0 - CI 阻断（必须修复）

1. **Mypy 新增错误** (86个)
   - 选项 A: 逐一修复类型标注问题
   - 选项 B: 更新 mypy baseline 文件（需 reviewer 批准）
   
   更新基线命令:
   ```bash
   python scripts/ci/check_mypy_gate.py --write-baseline
   ```

### P1 - 测试修复

2. **test_logbook_db.py 断言更新** (2个失败)
   - 更新断言: `"db_migrate.py"` → `"engram-migrate"` 或检查新的错误消息格式
   - 文件: `tests/gateway/test_logbook_db.py`

3. **test_audit_event.py correlation_id 问题** (2个失败)
   - 检查 `build_audit_event` / `build_gateway_audit_event` 函数的 correlation_id 参数处理
   - 确保传入的 correlation_id 被正确使用而非覆盖

4. **test_mcp_jsonrpc_contract.py mock 路径问题** (11个失败)
   - 检查 `get_reliability_report` 函数的实际位置
   - 更新 patch 路径或在 `app.py` 中导入该函数

---

## 失败修复追踪

| 失败项 | 类型 | 涉及文件 | 修复 PR/Commit | 状态 |
|--------|------|----------|----------------|------|
| mypy baseline 86 新增错误 | 类型检查 | 多个 logbook/gateway 文件 | TBD | ⏳ 待修复 |
| test_uses_provided_correlation_id | 测试失败 | `tests/gateway/test_audit_event.py` | TBD | ⏳ 待修复 |
| test_no_pollution_between_fields | 测试失败 | `tests/gateway/test_audit_event.py` | TBD | ⏳ 待修复 |
| test_ensure_db_ready_no_auto_migrate_raises_error | 测试失败 | `tests/gateway/test_logbook_db.py` | TBD | ⏳ 待修复 |
| test_error_message_contains_repair_hint | 测试失败 | `tests/gateway/test_logbook_db.py` | TBD | ⏳ 待修复 |
| test_mcp_jsonrpc_contract.py (11个) | 测试失败 | `tests/gateway/test_mcp_jsonrpc_contract.py` | TBD | ⏳ 待修复 |

---

## 与 Iteration 9 对比

| 指标 | Iteration 9 | Iteration 10 | 变化 |
|------|-------------|--------------|------|
| ruff lint | ✅ PASS | ✅ PASS | 持平 |
| ruff format | ✅ PASS | ✅ PASS | 持平 |
| mypy 新增错误 | 77 | 86 | +9 |
| Gateway 测试失败 | 4 | 15 | +11 |
| Acceptance 测试 | ✅ 143 通过 | ✅ 158 通过 | +15 |

**分析**:
- Acceptance 测试有改善 (+15 通过)
- Gateway 测试新增 11 个失败，主要集中在 `test_mcp_jsonrpc_contract.py`
- mypy 新增错误略有增加，但基线已修复 279 个

---

## 下一步行动

### 立即行动（Iteration 11）

1. **修复 test_mcp_jsonrpc_contract.py mock 路径问题** (11 失败)
   - 确认 `get_reliability_report` 函数位置
   - 更新测试中的 patch 路径

2. **修复 test_audit_event.py correlation_id 问题** (2 失败)
   - 检查 correlation_id 参数传递逻辑

3. **修复 test_logbook_db.py 断言** (2 失败)
   - 更新错误消息断言以匹配新的 CLI 命令名称

4. **处理 mypy baseline**
   - 选项 A: 更新基线文件
   - 选项 B: 逐步修复类型错误

### 验证命令

```bash
# 完整 CI 验证
make ci && pytest tests/gateway/ -q && pytest tests/acceptance/ -q
```

---

## 相关文档

- [Iteration 9 回归记录](iteration_9_regression.md)
- [验收测试矩阵](00_acceptance_matrix.md)
- [Mypy 基线策略](../architecture/adr_mypy_baseline_and_gating.md)
- [CLI 入口文档](../architecture/cli_entrypoints.md)
