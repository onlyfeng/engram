# Iteration 3 回归 Runbook

本文档记录本地与 CI 对齐的回归测试命令，以及实际执行结果记录。

---

## 1. 本地与 CI 对齐的回归命令

以下命令与 `.github/workflows/ci.yml` 中的 CI 流程保持一致。

### 1.1 Lint 检查

```bash
# 本地执行
make lint

# CI 对应步骤 (lint job)
ruff check src/ tests/
```

### 1.2 格式检查

```bash
# 本地执行
make format-check

# CI 对应步骤 (lint job)
ruff format --check src/ tests/
```

### 1.3 类型检查

```bash
# 本地执行
make typecheck

# CI 对应步骤 (lint job)
mypy src/engram/
```

### 1.4 Schema 校验

```bash
# 本地执行
make check-schemas

# CI 对应步骤 (schema-validate job)
python scripts/validate_schemas.py --validate-fixtures --verbose
```

### 1.5 环境变量一致性检查

```bash
# 本地执行
make check-env-consistency

# CI 对应步骤 (env-var-consistency job)
python scripts/ci/check_env_var_consistency.py --verbose
```

### 1.6 Logbook 配置一致性检查

```bash
# 本地执行
make check-logbook-consistency

# CI 对应步骤 (logbook-consistency job)
python scripts/verify_logbook_consistency.py --verbose
```

### 1.7 迁移文件检查

```bash
# 本地执行
make check-migration-sanity

# CI 对应步骤 (migration-sanity job)
# 检查必需的 SQL 迁移文件存在性
```

### 1.8 数据库迁移与权限验证

```bash
# 本地执行（需要 PostgreSQL 运行）
make migrate-ddl
make apply-roles
make apply-openmemory-grants
make verify-permissions

# CI 对应步骤 (test job)
python -m engram.logbook.cli.db_migrate --dsn "$POSTGRES_DSN" --apply-roles --apply-openmemory-grants
python -m engram.logbook.cli.db_migrate --dsn "$POSTGRES_DSN" --verify --verify-strict
```

### 1.9 完整测试

```bash
# 本地执行（需要 PostgreSQL 运行）
make test

# CI 对应步骤 (test job)
pytest tests/gateway/ -v
pytest tests/acceptance/ -v
```

### 1.10 一键 CI 检查（无需数据库）

```bash
# 运行所有静态检查（lint + format + typecheck + schema + consistency）
make ci
```

---

## 2. 命令快速参考表

| 检查项 | 本地命令 | CI Job | 需要数据库 |
|--------|----------|--------|------------|
| Lint | `make lint` | `lint` | 否 |
| 格式检查 | `make format-check` | `lint` | 否 |
| 类型检查 | `make typecheck` | `lint` | 否 |
| Schema 校验 | `make check-schemas` | `schema-validate` | 否 |
| 环境变量一致性 | `make check-env-consistency` | `env-var-consistency` | 否 |
| Logbook 一致性 | `make check-logbook-consistency` | `logbook-consistency` | 否 |
| 迁移文件检查 | `make check-migration-sanity` | `migration-sanity` | 否 |
| 数据库迁移 | `make migrate-ddl` | `test` | **是** |
| 权限验证 | `make verify-permissions` | `test` | **是** |
| 完整测试 | `make test` | `test` | **是** |
| 一键静态检查 | `make ci` | - | 否 |

---

## 3. 执行结果记录

### 3.1 执行记录 - 2026-01-31

| 字段 | 内容 |
|------|------|
| **日期** | 2026-01-31 |
| **时间** | 14:05:05 UTC |
| **Commit** | `38ff81c` |
| **环境** | Darwin 24.6.0 (x86_64) / Python 3.13.2 |

#### 执行结果摘要

| 检查项 | 状态 | 退出码 | 说明 |
|--------|------|--------|------|
| `make lint` | ⚠️ WARN | 0 | 有 I001/F401/W293 警告，不阻塞 |
| `make format-check` | ❌ FAIL | 141 | 100+ 文件需要格式化 |
| `make typecheck` | ❌ FAIL | 141 | 90+ mypy 类型错误 |
| `make check-schemas` | ✅ PASS | 0 | 7 schema, 19 fixtures 全部通过 |
| `make check-env-consistency` | ⚠️ WARN | 141 | 文档与代码环境变量不完全对齐 |
| `make check-logbook-consistency` | ❌ FAIL | 2 | 4 个配置不一致问题 |
| `make check-migration-sanity` | ✅ PASS | 0 | 所有必需 SQL 文件存在 |
| `make migrate-ddl` | ⏭️ SKIP | - | 当前环境无 PostgreSQL |
| `make verify-permissions` | ⏭️ SKIP | - | 当前环境无 PostgreSQL |
| `make test` | ⏭️ SKIP | - | 当前环境无 PostgreSQL |

#### 关键输出摘要

**Lint 警告（不阻塞）**:
- `I001`: Import block is un-sorted or un-formatted (2 处)
- `F401`: Unused import (3 处)
- `W293`: Blank line contains whitespace (2 处)

**格式检查失败**:
- 100+ 文件需要运行 `make format` 修复

**类型检查错误**:
- `no-any-return`: 多个函数返回 `Any` 类型
- `assignment`: 类型不兼容的赋值
- `import-untyped`: 缺少 `requests`/`boto3` 的类型桩

**Schema 校验通过**:
```
Schema 校验结果汇总
  总计:     7
  通过:     7
  失败:     0

Fixtures 校验结果:
  总计:     19
  通过:     19
  失败:     0
```

**Logbook 一致性检查失败**:
- [B] 未找到 `acceptance-logbook-only` 目标
- [C] docs `up-logbook` 描述与 Makefile 实现不一致
- [D] README.md 中未找到 Logbook-only 分步验收章节
- [F] 04_acceptance_criteria.md 缺失 `migrate-logbook-stepwise`, `verify-permissions-logbook`

**迁移文件检查通过**:
```
[OK] 存在: sql/01_logbook_schema.sql
[OK] 存在: sql/02_scm_migration.sql
[OK] 存在: sql/04_roles_and_grants.sql
[OK] 存在: sql/05_openmemory_roles_and_grants.sql
```

---

## 4. 已知失败与修复计划

### 4.1 格式化问题

| 问题 | 修复方式 | 优先级 |
|------|----------|--------|
| 100+ 文件需要格式化 | `make format` | 高 |

### 4.2 类型检查问题

| 问题类型 | 文件数 | 修复方式 | 优先级 |
|----------|--------|----------|--------|
| `no-any-return` | 20+ | 添加显式类型注解 | 中 |
| `assignment` 类型不兼容 | 15+ | 修正类型定义 | 中 |
| `import-untyped` | 3 | 安装 `types-requests`, `boto3-stubs` | 低 |

### 4.3 Logbook 配置一致性问题

| 问题 | 修复方式 | 相关文件 |
|------|----------|----------|
| [B] 缺少 `acceptance-logbook-only` 目标 | 在 Makefile 中添加目标 | `Makefile` |
| [C] `up-logbook` 描述与实现不一致 | 更新文档或 Makefile | `docs/`, `Makefile` |
| [D] README.md 缺少 Logbook-only 章节 | 添加分步验收章节 | `README.md` |
| [F] 验收标准文档缺少命令 | 添加缺失命令 | `docs/logbook/04_acceptance_criteria.md` |

---

## 5. CI 与本地执行差异说明

### 5.1 环境差异

| 项目 | 本地 | CI |
|------|------|-----|
| Python 版本 | 3.13.2 | 3.10/3.11/3.12 (matrix) |
| 操作系统 | macOS Darwin 24.6.0 | Ubuntu latest |
| PostgreSQL | 按需启动 | pgvector/pgvector:pg16 服务 |
| 超时设置 | 无限制 | 10-15 分钟 |

### 5.2 测试策略差异

| 场景 | 本地 | CI |
|------|------|-----|
| 数据库测试 | 可选（SKIP） | 必须通过 |
| 静态检查 | `make ci` 一键执行 | 分 job 并行执行 |
| Artifact 保留 | 无 | 14 天 |

---

## 6. 回归测试检查清单

在提交 PR 前，请确保以下检查通过：

- [ ] `make lint` 无错误（警告可接受）
- [ ] `make format-check` 通过
- [ ] `make typecheck` 通过
- [ ] `make check-schemas` 通过
- [ ] `make check-migration-sanity` 通过
- [ ] `make check-env-consistency` 通过
- [ ] `make check-logbook-consistency` 通过

如有数据库环境：

- [ ] `make migrate-ddl` 通过
- [ ] `make verify-permissions` 通过
- [ ] `make test` 通过

---

## 7. 相关文档

- [验收测试矩阵](./00_acceptance_matrix.md)
- [CI 工作流配置](../../.github/workflows/ci.yml)
- [安装指南](../installation.md)
- [环境变量参考](../reference/environment_variables.md)
