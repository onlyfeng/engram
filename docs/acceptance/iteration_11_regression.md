> **⚠️ Superseded by Iteration 12**
>
> 本文档已被 [Iteration 12 回归记录](iteration_12_regression.md) 取代。

# Iteration 11 Regression - CI 流水线验证记录

> **背景说明**：此前在本地/讨论中使用 `iteration_id=7` 追踪，本次晋升为 Iteration 11 以避免与历史 Iteration 7 冲突。
> 历史 Iteration 7 的记录保留于 [iteration_7_regression.md](iteration_7_regression.md)（状态：SUPERSEDED）。

## 执行信息

| 项目 | 值 |
|------|-----|
| **执行日期** | 2026-02-01 |
| **执行环境** | darwin 24.6.0 |
| **Python 版本** | 3.13.2 |
| **pytest 版本** | 9.0.2 |
| **执行者** | Cursor Agent |
| **Commit SHA** | `4289e5b` |

---

## 执行结果总览

| 序号 | 测试阶段 | 命令 | 状态 | 详情 |
|------|----------|------|------|------|
| 1 | make ci | `make ci` | ✅ PASS | 全部检查通过（修复格式问题后） |
| 2 | Gateway 单元测试 | `pytest tests/gateway/ -q` | ⚠️ PARTIAL | 1188 通过, 8 失败, 204 跳过 (26.47s) |
| 3 | Acceptance 测试 | `pytest tests/acceptance/ -q` | ✅ PASS | 132 通过, 0 失败, 48 跳过 (32.22s) |

**状态图例**：
- ✅ PASS - 全部通过
- ⚠️ PARTIAL - 部分通过（存在失败但非阻断）
- ❌ FAIL - 存在阻断性失败
- ⏳ 待执行 - 尚未运行

---

## 继承自 Iteration 10 的待修复项

本迭代继承 [Iteration 10](iteration_10_regression.md) 的以下待修复项：

### P0 - CI 阻断（必须修复）

1. **Mypy 新增错误** (86 个) ✅ **已解决**
   - 状态: ✅ 已修复，当前 baseline 错误数为 0
   - 修复方式: 类型注解完善 + TypedDict 封装 + 边界层 cast()
   - 验证: `make typecheck-gate` 通过（0 错误）
   
   验证命令:
   ```bash
   make typecheck-gate  # 输出: 当前错误数: 0, 基线错误数: 0
   ```

### P1 - 测试修复

2. **test_mcp_jsonrpc_contract.py ErrorReason 问题** (2 个失败) ⏳ **待修复**
   - 现象: `ErrorReason` 公开常量与白名单不一致
   - 文件: `tests/gateway/test_mcp_jsonrpc_contract.py`
   - 涉及用例: `test_error_reason_public_constants_match_whitelist`, `test_no_undocumented_public_error_reasons`

3. **test_audit_event.py correlation_id 问题** ✅ **已解决**
   - 状态: ✅ 测试通过，不再出现在失败列表中

4. **test_logbook_db.py 断言更新** ✅ **已解决**
   - 状态: ✅ 文件已删除（`logbook_db.py` 模块已废弃）
   - 迁移说明: 相关测试行为已由以下测试覆盖：
     - `tests/gateway/test_migrate_import.py::TestGatewayDbCheckPath` - Gateway 启动期 DB check 路径
     - `tests/gateway/test_gateway_startup.py::TestFormatDBRepairCommands::test_basic_format_without_params` - 验证修复提示包含 `engram-migrate`
   - 参见: `docs/architecture/deprecated_logbook_db_references_ssot.md`

---

## 详细执行记录

### 1. make ci

**命令**: `make ci`

**状态**: ✅ PASS（task-aacb4fd1 验证于 2026-02-01）

**执行流程**:

| 步骤 | 检查项 | 状态 | 说明 |
|------|--------|------|------|
| 1 | `ruff check src/ tests/` | ✅ | All checks passed! |
| 2 | `ruff format --check` | ✅ | 248 files already formatted |
| 3 | `typecheck-gate` (mypy baseline) | ✅ | 0 错误 |
| 4 | `typecheck-gate` (strict-island) | ✅ | 0 错误 |
| 5 | `check-schemas` | ✅ | 7 schema 通过，19 fixtures 通过 |
| 6 | `check-env-consistency` | ✅ | 0 错误，0 警告 |
| 7 | `check-logbook-consistency` | ✅ | 所有检查通过 |
| 8 | `check-migration-sanity` | ✅ | 14 SQL 文件验证通过 |
| 9 | `check-scm-sync-consistency` | ✅ | 所有检查通过 |
| 10 | `check-cli-entrypoints` | ✅ | 所有入口点一致 |
| 11 | `check-noqa-policy` | ✅ | 所有检查通过 |
| 12 | `check-gateway-di-boundaries` | ✅ | 0 违规 |
| 13 | `check-sql-inventory-consistency` | ✅ | 所有检查通过 |
| 14 | `validate-workflows` | ✅ | 合约验证通过 |

---

### 2. Gateway 单元测试

**命令**: `pytest tests/gateway/ -q`

**状态**: ⚠️ PARTIAL（8 个失败）

**统计**:
- 通过: 1188
- 失败: 8
- 跳过: 204
- 执行时间: 26.47s

**失败用例列表**（共 8 个）:

| 序号 | 测试文件 | 失败用例 | 失败原因 |
|------|----------|----------|----------|
| 1 | test_correlation_id_proxy.py | test_infer_value_error_reason | ImportError: `_infer_value_error_reason` 不存在于 mcp_rpc 模块 |
| 2 | test_correlation_id_proxy.py | test_infer_runtime_error_reason | ImportError: `_infer_runtime_error_reason` 不存在于 mcp_rpc 模块 |
| 3 | test_error_codes.py | test_dependency_reasons_exist | McpErrorReason 没有 DEPENDENCY_MISSING 属性 |
| 4 | test_importerror_optional_deps_contract.py | test_make_dependency_missing_error_field_semantics | ErrorReason.DEPENDENCY_MISSING 常量不存在 |
| 5 | test_importerror_optional_deps_contract.py | test_error_reason_constant_exported | ErrorReason 没有 DEPENDENCY_MISSING 属性 |
| 6 | test_importerror_optional_deps_contract.py | test_evidence_upload_missing_content_returns_error | 期望 `MISSING_REQUIRED_PARAMETER` 但实际返回 `MISSING_REQUIRED_PARAM` |
| 7 | test_two_phase_audit_adapter_first.py | test_pending_to_redirected_adapter_first_path | action 应为 'deferred' 但实际是 'error' |
| 8 | test_two_phase_audit_adapter_first.py | test_redirected_branch_evidence_refs_correlation_id_consistency | action 应为 'deferred' 但实际是 'error' |

---

### 3. Acceptance 测试

**命令**: `pytest tests/acceptance/ -q`

**状态**: ✅ PASS

**统计**:
- 通过: 132
- 失败: 0
- 跳过: 48
- 执行时间: 28.56s

**备注**: 全部通过，无失败用例。跳过的 48 个用例主要是需要数据库连接的集成测试。

---

## 失败修复追踪

| 失败项 | 类型 | 涉及文件 | 修复 PR/Commit | 状态 |
|--------|------|----------|----------------|------|
| mypy baseline 86 新增错误 | 类型检查 | 多个 logbook/gateway 文件 | task-aacb4fd1 | ✅ 已修复 |
| ruff format (4 文件) | 格式检查 | 4 个测试文件 | task-b48e9f08 | ✅ 已修复 |
| test_error_reason_dependency_missing_is_private | 契约测试 | `tests/gateway/test_evidence_upload.py` | - | ✅ 已通过（原 21 失败已收敛） |
| test_main_dedup.py (9 个) | Mock 路径 | `tests/gateway/test_main_dedup.py` | - | ✅ 已通过（原 21 失败已收敛） |
| test_mcp_jsonrpc_contract.py (2 个) | ErrorReason 白名单 | `tests/gateway/test_mcp_jsonrpc_contract.py` | - | ✅ 已通过（原 21 失败已收敛） |
| test_outbox_worker.py (4 个) | get_openmemory_client patch | `tests/gateway/test_outbox_worker.py` | - | ✅ 已通过（原 21 失败已收敛） |
| test_unified_stack_integration.py (5 个) | 环境变量缺失 | `tests/gateway/test_unified_stack_integration.py` | - | ✅ 已通过（原 21 失败已收敛） |
| test_correlation_id_proxy.py (2 个) | 私有函数导入 | `tests/gateway/test_correlation_id_proxy.py` | TBD | ⏳ 待修复 |
| test_error_codes.py (1 个) | DEPENDENCY_MISSING 常量缺失 | `tests/gateway/test_error_codes.py` | TBD | ⏳ 待修复 |
| test_importerror_optional_deps_contract.py (3 个) | 常量命名/错误码变更 | `tests/gateway/test_importerror_optional_deps_contract.py` | TBD | ⏳ 待修复 |
| test_two_phase_audit_adapter_first.py (2 个) | 两阶段审计行为变更 | `tests/gateway/test_two_phase_audit_adapter_first.py` | TBD | ⏳ 待修复 |

---

## Mypy 类型检查修复追踪（task-aacb4fd1）

### 执行信息

| 项目 | 值 |
|------|-----|
| **执行日期** | 2026-02-01 |
| **mypy 基线错误数** | 0 |
| **当前错误数** | 0 |
| **检查结果** | ✅ PASS |

### 高频文件类型注解策略

以下为涉及的高频文件及其类型安全策略：

| 文件 | 错误类型 | 修复策略 | 当前状态 |
|------|----------|----------|----------|
| `src/engram/logbook/gitlab_client.py` | GitLab REST API 动态 JSON | 1. 使用 `Dict[str, Any]` 包装 GitLab API 响应<br>2. `GitLabAPIResult.data` 设为 `Optional[Any]`<br>3. 类型转换使用 `cast()` 明确边界 | ✅ 类型安全 |
| `src/engram/logbook/artifact_store.py` | boto3 S3 客户端类型 | 1. `TYPE_CHECKING` 块导入 `mypy_boto3_s3.client.S3Client`<br>2. 内部使用 `S3Client | None` 惰性初始化<br>3. multipart upload parts 使用 `list[dict[str, Any]]` | ✅ 类型安全 |
| `src/engram/logbook/artifact_gc.py` | 数据库行类型 | 1. 使用 dataclass 定义 `GCCandidate`, `GCResult`<br>2. `ReferencedUris` 封装数据库引用集合<br>3. 函数返回类型明确 `List[Tuple[...]]` | ✅ 类型安全 |
| `src/engram/logbook/scm_db.py` | psycopg 游标返回类型 | 1. `_dict_cursor()` 返回 `psycopg.Cursor[DictRow]`<br>2. 数据库行解析使用 `cast(Dict[str, Any], ...)`<br>3. JSON 字段处理时检查 `isinstance(value, str)` | ✅ 类型安全 |
| `src/engram/logbook/scm_integrity_check.py` | 数据库行规范化 | 1. 使用 `TypedDict` 定义 `PatchBlobRowDict`<br>2. `_normalize_row()` 函数处理 dict/tuple 两种格式<br>3. NOT NULL 字段使用 `assert` 验证 | ✅ 类型安全 |

### 类型安全边界处理策略

对于外部 SDK 和动态 JSON，采用以下边界封装策略：

1. **GitLab REST API**（`gitlab_client.py`）
   - `GitLabAPIResult.data: Optional[Any]` - API 响应可能是 dict/list/str
   - 调用方负责类型断言或解析
   - 返回类型使用 `List[Dict[str, Any]]` 避免深层泛型

2. **S3/MinIO 对象存储**（`artifact_store.py`）
   - 使用 `TYPE_CHECKING` 条件导入 boto3 类型存根
   - `_client: S3Client | None` 惰性初始化模式
   - multipart 响应使用 `# type: ignore[typeddict-item]` 处理 SDK 类型不完整问题

3. **PostgreSQL psycopg**（`scm_db.py`）
   - `dict_row` 工厂返回 `DictRow` 类型
   - 显式 `cast()` 转换数据库行为 `Dict[str, Any]`
   - JSON 字段反序列化前检查字符串类型

4. **数据库行规范化**（`scm_integrity_check.py`）
   - `TypedDict` 定义预期的数据库行结构
   - `_normalize_row()` 统一处理 dict 和 tuple 两种游标返回格式
   - NOT NULL 字段使用 `assert` 验证，nullable 字段使用 `Optional`

---

## 与 Iteration 10 对比

| 指标 | Iteration 10 | Iteration 11 (之前) | Iteration 11 (当前) | 变化 |
|------|--------------|---------------------|---------------------|------|
| ruff lint | ✅ PASS | ✅ PASS | ✅ PASS | 无变化 |
| ruff format | ✅ PASS | ⚠️ 4 文件需格式化 | ✅ PASS | ✅ 已修复 |
| mypy 新增错误 | 86 | 0 | 0 | ✅ -86（全部修复） |
| mypy baseline 错误 | 86 | 0 | 0 | ✅ -86（全部清零） |
| Gateway 测试通过 | - | 1119 | 1188 | ✅ +69 |
| Gateway 测试失败 | 15 | 21 | 8 | ✅ -13（显著改善） |
| Gateway 测试跳过 | - | 209 | 204 | ⬇️ -5 |
| Acceptance 测试通过 | 158 | 132 | 132 | 无变化 |
| Acceptance 测试跳过 | - | 48 | 48 | 无变化 |

---

## Skip 治理记录（task-bcbe80fd）

> **执行时间**: 2026-02-01
> **任务**: 统计 skip 来源，分类治理，确保 skip reason 与 acceptance matrix 一致

### Skip 来源分类统计

| 分类 | 数量 | 原因 | 合理性 |
|------|------|------|--------|
| **capability (db)** | 110 | 无法创建测试数据库 | ✅ 合理（需数据库连接） |
| **profile (integration)** | 47 | 集成测试需要设置 RUN_INTEGRATION_TESTS=1 | ✅ 合理（profile 控制） |
| **capability (schema)** | 41 | Schema file not found | ⚠️ 需检查 schema 路径 |
| **capability (env)** | 6 | 需要 TEST_PG_DSN 环境变量 | ✅ 合理（环境变量控制） |
| **legacy (DI)** | 5 | [LEGACY:DI_MIGRATION] 需要迁移到 gateway_test_container | ⏳ 待迁移 |
| **总计** | **209** | - | - |

### Skip Reason 格式标准化

#### Legacy 类 (test_evidence_upload.py)

已更新 5 个 legacy skip 的 reason 格式，使用统一标识符 `[LEGACY:DI_MIGRATION]`：

```
[LEGACY:DI_MIGRATION] 需要迁移到 gateway_test_container fixture，
当前 patch 方式无法正确 mock deps.logbook_adapter
```

**迁移计划**：
- 目标：使用 `gateway_test_container` fixture 替代手动 patch
- 优先级：P2（非阻断，功能有其他测试覆盖）
- 参见：`tests/gateway/conftest.py` 中 `gateway_test_container` 使用指南

#### Profile 类 (test_outbox_worker_integration.py, test_unified_stack_integration.py)

已验证 skip reason 与 `docs/acceptance/00_acceptance_matrix.md` 一致：

| 文件 | Skip Reason | 与文档一致性 |
|------|-------------|--------------|
| `test_outbox_worker_integration.py` | `HTTP_ONLY_MODE: Outbox Worker 集成测试需要 Docker 和数据库` | ✅ 一致 |
| `test_unified_stack_integration.py` | `HTTP_ONLY_MODE: 统一栈测试需要 Docker 和数据库` | ✅ 一致 |
| `test_unified_stack_integration.py` | `集成测试需要设置 RUN_INTEGRATION_TESTS=1` | ✅ 一致 |

#### Capability 类

| 原因 | 处理 |
|------|------|
| 无法创建测试数据库 | ✅ 合理，需 PostgreSQL 服务运行 |
| Schema file not found | ⚠️ 需检查 schema 文件路径配置 |
| 需要 TEST_PG_DSN | ✅ 合理，环境变量控制 |

### Skip 数量变化

| 时间点 | Skip 数量 | 说明 |
|--------|-----------|------|
| 本次执行前 | 209 | - |
| 本次执行后 | 209 | 无变化（仅更新 reason 格式） |

### 剩余 Skip 的合理性分析

1. **capability 类 (157 个)**：环境/能力依赖，在 CI 环境中会自动满足，合理
2. **profile 类 (47 个)**：通过 `RUN_INTEGRATION_TESTS=1` 环境变量控制，合理
3. **legacy 类 (5 个)**：待迁移到 DI 注入方式，已标记明确迁移路径

---

## 下一步行动

### 已完成

- ✅ `make ci` 全部通过（修复 ruff format 问题后）
- ✅ `pytest tests/acceptance/` 全部通过（132 通过, 48 跳过）
- ✅ mypy baseline 错误清零
- ✅ Skip 来源分类统计（task-bcbe80fd）
- ✅ Legacy skip reason 格式标准化
- ✅ Profile skip reason 与 acceptance matrix 一致性验证
- ✅ Gateway 测试失败从 21 个收敛到 8 个（task-b48e9f08）

### 待修复（8 个 Gateway 测试失败）

#### 1. 私有函数导入问题 (2 失败)

**涉及文件**: `tests/gateway/test_correlation_id_proxy.py`

| 用例 | 问题 |
|------|------|
| `test_infer_value_error_reason` | `_infer_value_error_reason` 函数从 mcp_rpc 模块移除 |
| `test_infer_runtime_error_reason` | `_infer_runtime_error_reason` 函数从 mcp_rpc 模块移除 |

**修复方向**: 确认函数是否已重构到其他模块，或测试用例需要删除/更新

#### 2. DEPENDENCY_MISSING 常量缺失 (4 失败)

**涉及文件**: 
- `tests/gateway/test_error_codes.py` (1 失败)
- `tests/gateway/test_importerror_optional_deps_contract.py` (3 失败)

| 用例 | 问题 |
|------|------|
| `test_dependency_reasons_exist` | McpErrorReason.DEPENDENCY_MISSING 属性不存在 |
| `test_make_dependency_missing_error_field_semantics` | ErrorReason.DEPENDENCY_MISSING 常量不存在 |
| `test_error_reason_constant_exported` | ErrorReason 没有 DEPENDENCY_MISSING 属性 |
| `test_evidence_upload_missing_content_returns_error` | 错误码从 `MISSING_REQUIRED_PARAMETER` 缩短为 `MISSING_REQUIRED_PARAM` |

**修复方向**: 
- 确认 `DEPENDENCY_MISSING` 常量是否被移除或重命名
- 更新测试用例中的错误码断言（`MISSING_REQUIRED_PARAM`）

#### 3. 两阶段审计行为变更 (2 失败)

**涉及文件**: `tests/gateway/test_two_phase_audit_adapter_first.py`

| 用例 | 问题 |
|------|------|
| `test_pending_to_redirected_adapter_first_path` | API error 时 action='error' 而非 'deferred' |
| `test_redirected_branch_evidence_refs_correlation_id_consistency` | 同上 |

**修复方向**: 
- 检查 `FakeOpenMemoryClient.configure_store_api_error` 的行为是否变更
- 确认 503 错误是否应路由到 outbox（deferred）还是直接返回 error

### 下一 Iteration 计划条目

以下失败用例计划在 **Iteration 12** 中修复：

| 任务 | 优先级 | 涉及文件 | 说明 |
|------|--------|----------|------|
| 移除/更新 `_infer_*_reason` 测试 | P1 | test_correlation_id_proxy.py | 私有函数已移除 |
| 同步 DEPENDENCY_MISSING 常量 | P1 | test_error_codes.py, test_importerror_optional_deps_contract.py | 确认常量状态 |
| 更新错误码断言 | P2 | test_importerror_optional_deps_contract.py | `MISSING_REQUIRED_PARAM` vs `MISSING_REQUIRED_PARAMETER` |
| 修复两阶段审计行为测试 | P1 | test_two_phase_audit_adapter_first.py | 确认 API error 路由策略 |

### 验证命令

```bash
# 完整 CI 验证
make ci && pytest tests/gateway/ -q && pytest tests/acceptance/ -q
```

---

## 相关文档

- [Iteration 11 计划](iteration_11_plan.md)
- [Iteration 10 回归记录](iteration_10_regression.md)
- [Iteration 7 回归记录](iteration_7_regression.md)（历史，已被取代）
- [验收测试矩阵](00_acceptance_matrix.md)
- [Mypy 基线策略](../architecture/adr_mypy_baseline_and_gating.md)
- [CLI 入口文档](../architecture/cli_entrypoints.md)
