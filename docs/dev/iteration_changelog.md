# 迭代/变更日志

> 本文档记录 Engram 各迭代周期的变更内容、影响域、关键文件和验收门禁。
>
> **单一来源**：各迭代详细记录位于 `docs/acceptance/iteration_*_regression.md`

---

## 目录

- [变更日志索引](#变更日志索引)
- [按日期窗口详情](#按日期窗口详情)
  - [2026-02-01](#2026-02-01)
  - [2026-01-22](#2026-01-22)
  - [2026-01-16](#2026-01-16)
  - [2026-01-08](#2026-01-08)
  - [2025-12-22](#2025-12-22)
  - [2025-12-18](#2025-12-18)
- [验收门禁速查](#验收门禁速查)

---

## 变更日志索引

| 日期 | 迭代 | 类别 | 影响域 | 摘要 | 状态 | 回归记录 |
|------|------|------|--------|------|------|----------|
| 2026-02-01 | Iteration 12 | feature/fix | Gateway | 私有函数导入、ErrorReason 契约、两阶段审计语义修复 | 🔄 PLANNING | [iteration_12_regression.md](../acceptance/iteration_12_regression.md) |
| 2026-02-01 | Iteration 11 | feature/fix | CI/Gateway | mypy baseline 清零、Gateway 测试收敛 (21→8) | ⚠️ PARTIAL | [iteration_11_regression.md](../acceptance/iteration_11_regression.md) |
| 2026-02-01 | Iteration 10 | fix | CI/Gateway | lint 修复、mypy baseline 更新 | ⚠️ PARTIAL | [iteration_10_regression.md](../acceptance/iteration_10_regression.md) |
| 2026-02-01 | Iteration 9 | fix | CI | lint/mypy 修复 | 🔄 SUPERSEDED | [iteration_9_regression.md](../acceptance/iteration_9_regression.md) |
| 2026-02-01 | Iteration 8 | feature | CI | CI 门禁收敛迭代 | 🔄 PLANNING | [iteration_8_regression.md](../acceptance/iteration_8_regression.md) |
| 2026-02-01 | Iteration 7 | fix | CI | lint/format 修复 | 🔄 SUPERSEDED | [iteration_7_regression.md](../acceptance/iteration_7_regression.md) |
| 2026-02-01 | Iteration 6 | feature | Gateway | Gateway 测试覆盖提升 | ⚠️ PARTIAL | [iteration_6_regression.md](../acceptance/iteration_6_regression.md) |
| 2026-01-29 | Iteration 5 | feature | Gateway/SQL | 基础功能验收 | ✅ PASS | [iteration_5_regression.md](../acceptance/iteration_5_regression.md) |
| 2026-02-01 | Iteration 4 | fix | Gateway | 两阶段审计 E2E 修复 | ⚠️ PARTIAL | [iteration_4_regression.md](../acceptance/iteration_4_regression.md) |
| 2026-01-27 | Iteration 3 | feature | SQL/Docs | SQL 迁移重构 | ✅ PASS | [iteration_3_regression.md](../acceptance/iteration_3_regression.md) |

---

## 按日期窗口详情

### 2026-02-01

#### Iteration 12（规划中）

| 项目 | 内容 |
|------|------|
| **类别** | feature/fix |
| **影响域** | Gateway |
| **目标** | 修复 Iteration 11 遗留的 8 个 Gateway 测试失败 |

**关键文件**：

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `tests/gateway/test_correlation_id_proxy.py` | fix | 移除/更新私有函数导入测试 |
| `tests/gateway/test_error_codes.py` | fix | 同步 ErrorReason 常量 |
| `tests/gateway/test_importerror_optional_deps_contract.py` | fix | 更新错误码断言 |
| `tests/gateway/test_two_phase_audit_adapter_first.py` | fix | 修复两阶段审计语义测试 |

**验收门禁**：

```bash
make ci && pytest tests/gateway/ -q && pytest tests/acceptance/ -q
```

**链接**：[iteration_12_regression.md](../acceptance/iteration_12_regression.md)

---

#### Iteration 11

| 项目 | 内容 |
|------|------|
| **类别** | feature/fix |
| **影响域** | CI/Gateway |
| **成果** | mypy baseline 清零（86→0）；Gateway 测试失败收敛（21→8） |

**关键文件**：

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `src/engram/logbook/gitlab_client.py` | fix | 类型注解完善（GitLab REST API） |
| `src/engram/logbook/artifact_store.py` | fix | boto3 S3 客户端类型安全 |
| `src/engram/logbook/artifact_gc.py` | fix | dataclass 定义 GCCandidate/GCResult |
| `src/engram/logbook/scm_db.py` | fix | psycopg 游标返回类型 |
| `src/engram/logbook/scm_integrity_check.py` | fix | TypedDict 定义 PatchBlobRowDict |
| `scripts/ci/mypy_baseline.txt` | fix | 基线清零 |

**验收门禁**：

```bash
make ci  # 全部 14 项检查通过
pytest tests/gateway/ -q  # 1188 通过, 8 失败, 204 跳过
pytest tests/acceptance/ -q  # 132 通过, 0 失败, 48 跳过
```

**链接**：[iteration_11_regression.md](../acceptance/iteration_11_regression.md)

---

#### Iteration 10

| 项目 | 内容 |
|------|------|
| **类别** | fix |
| **影响域** | CI/Gateway |
| **成果** | lint 修复；发现 86 个 mypy 新增错误 |

**关键文件**：

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `src/engram/gateway/app.py` | 需修复 | Missing named argument "error_code" |
| `src/engram/gateway/evidence_store.py` | 需修复 | Incompatible types |
| `src/engram/logbook/artifact_delete.py` | 需修复 | ParsedUri has no attribute |
| `src/engram/logbook/cli/db_bootstrap.py` | 需修复 | arg-type, call-overload |

**验收门禁**：

```bash
make ci  # mypy baseline gate 失败 (86 新增错误)
pytest tests/gateway/ -q  # 15 失败, 807 通过, 156 跳过
pytest tests/acceptance/ -q  # 158 通过, 50 跳过, 0 失败
```

**链接**：[iteration_10_regression.md](../acceptance/iteration_10_regression.md)

---

### 2026-01-22

> **说明**：此日期窗口的变更记录待补充。以下为占位条目。

| 项目 | 内容 |
|------|------|
| **类别** | TBD |
| **影响域** | TBD |
| **成果** | 待记录 |

**关键文件**：待补充

**验收门禁**：

```bash
make ci
```

---

### 2026-01-16

> **说明**：此日期窗口的变更记录待补充。以下为占位条目。

| 项目 | 内容 |
|------|------|
| **类别** | TBD |
| **影响域** | TBD |
| **成果** | 待记录 |

**关键文件**：待补充

**验收门禁**：

```bash
make ci
```

---

### 2026-01-08

> **说明**：此日期窗口的变更记录待补充。以下为占位条目。

| 项目 | 内容 |
|------|------|
| **类别** | TBD |
| **影响域** | TBD |
| **成果** | 待记录 |

**关键文件**：待补充

**验收门禁**：

```bash
make ci
```

---

### 2025-12-22

> **说明**：此日期窗口的变更记录待补充。以下为占位条目。

| 项目 | 内容 |
|------|------|
| **类别** | TBD |
| **影响域** | TBD |
| **成果** | 待记录 |

**关键文件**：待补充

**验收门禁**：

```bash
make ci
```

---

### 2025-12-18

> **说明**：此日期窗口的变更记录待补充。以下为占位条目。

| 项目 | 内容 |
|------|------|
| **类别** | TBD |
| **影响域** | TBD |
| **成果** | 待记录 |

**关键文件**：待补充

**验收门禁**：

```bash
make ci
```

---

## 验收门禁速查

### 常用命令

| 命令 | 说明 | 适用场景 |
|------|------|----------|
| `make ci` | 完整 CI 检查 | 所有变更 |
| `pytest tests/gateway/ -q` | Gateway 测试 | Gateway 域变更 |
| `pytest tests/acceptance/ -q` | 验收测试 | 所有变更 |
| `make typecheck-gate` | mypy 基线检查 | 类型相关变更 |
| `make lint` | ruff lint 检查 | 代码质量变更 |
| `make check-schemas` | JSON Schema 校验 | Schema 变更 |
| `make check-migration-sanity` | SQL 迁移检查 | SQL 域变更 |

### 按影响域推荐门禁

| 影响域 | 最小门禁 | 完整门禁 |
|--------|----------|----------|
| **CI** | `make lint && make typecheck-gate` | `make ci` |
| **Gateway** | `pytest tests/gateway/ -q` | `make ci && pytest tests/gateway/ -q` |
| **SQL** | `make check-migration-sanity` | `make ci && make test-logbook` |
| **Docs** | `make check-cli-entrypoints` | `make ci` |

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [00_acceptance_matrix.md](../acceptance/00_acceptance_matrix.md) | 验收测试矩阵 SSOT |
| [iteration_local_drafts.md](iteration_local_drafts.md) | 本地迭代草稿管理 |
| [adr_iteration_docs_workflow.md](../architecture/adr_iteration_docs_workflow.md) | 迭代文档工作流 ADR |
| [ci_gate_runbook.md](ci_gate_runbook.md) | CI 门禁 Runbook |

---

## 变更记录

| 日期 | 变更内容 |
|------|----------|
| 2026-02-01 | 初始版本：创建迭代/变更日志文档，填入 2026-02-01 日期窗口记录 |

_更新时间：2026-02-01_
