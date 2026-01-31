# Logbook Definition of Done (DoD)

> **适用人群**：开发者、代码审查者、CI 维护者
> **文档状态**：权威规范

本文档定义 Logbook 相关变更的 Definition of Done (DoD)，确保每次变更都完整更新所有相关文件。

---

## 变更类型与必须同步更新的文件

### 变更类型矩阵

| 变更类型 | SQL 迁移 | CLI 命令 | 契约文档 | 单元测试 | 集成测试 | Makefile | 用户文档 | 验收矩阵 |
|----------|:--------:|:--------:|:--------:|:--------:|:--------:|:--------:|:--------:|:--------:|
| **Schema 新增表/字段** | ✓ | - | ✓ | ✓ | ✓ | - | ✓ | ✓ |
| **Schema 修改字段** | ✓ | - | ✓ | ✓ | ✓ | - | ✓ | ✓ |
| **新增 CLI 命令** | - | ✓ | ✓ | ✓ | - | ✓ | ✓ | - |
| **修改 CLI 行为** | - | ✓ | ✓ | ✓ | ✓ | - | ✓ | - |
| **URI 格式变更** | - | - | ✓ | ✓ | ✓ | - | ✓ | ✓ |
| **契约协议变更** | - | - | ✓ | ✓ | ✓ | - | ✓ | ✓ |
| **新增 Makefile target** | - | - | - | - | - | ✓ | ✓ | ✓ |
| **错误码新增/修改** | - | - | ✓ | ✓ | - | - | ✓ | ✓ |
| **配置/环境变量变更** | - | - | - | ✓ | - | - | ✓ | - |

### 文件路径参考

| 类型 | 文件路径 |
|------|----------|
| **SQL 迁移** | `src/engram/logbook/migrate.py` |
| **CLI 实现** | `logbook_postgres/scripts/logbook_cli.py` |
| **契约文档** | `docs/contracts/*.md` |
| **单元测试** | `tests/logbook/test_*.py` |
| **集成测试** | `tests/gateway/test_*_integration.py` |
| **Makefile** | `Makefile` |
| **用户文档** | `docs/logbook/*.md`, `docs/gateway/*.md` |
| **验收矩阵** | `docs/logbook/04_acceptance_criteria.md`, `docs/acceptance/00_acceptance_matrix.md` |

---

## 破坏性变更要求

### 何为破坏性变更

以下变更被视为**破坏性变更**，需要额外处理：

| 变更类型 | 示例 | 影响 |
|----------|------|------|
| 删除/重命名表或字段 | 移除 `legacy_field` | 现有查询失败 |
| 收紧约束 | `NOT NULL` 新增、`maxItems` 减小 | 现有数据校验失败 |
| 修改字段类型 | `VARCHAR` → `INTEGER` | 序列化/反序列化失败 |
| 修改语义 | URI scheme 变更 | 消费方逻辑错误 |
| 修改枚举值（删除/重命名） | `status` 枚举值删除 | 现有数据无效 |

### 破坏性变更必须执行的步骤

#### 1. 新增迁移脚本

```
sql/
└── NN_<migration_name>.sql
```

迁移脚本必须：
- 包含 `-- UP:` 和 `-- DOWN:` 标记
- 提供回滚能力
- 在 CI 中通过 `db_migrate.py --verify` 验证

#### 2. 提供 Backfill/Repair 命令

对于影响现有数据的变更，必须提供修复工具：

| 工具类型 | 路径 | 用途 |
|----------|------|------|
| Backfill 脚本 | `backfill_<feature>.py`（放在 `logbook_postgres/scripts/`） | 填充新字段默认值 |
| Repair 脚本 | `repair_<issue>.py`（放在 `logbook_postgres/scripts/`） | 修复损坏数据 |
| CLI 子命令 | `logbook repair <subcommand>` | 用户可执行的修复操作 |

**Backfill 脚本要求**：
- 支持 `--dry-run` 模式
- 支持 `--batch-size` 参数
- 输出处理统计信息
- 支持断点续传（通过 cursor）

#### 3. 更新版本判定

在以下位置更新版本信息：

| 文件 | 更新内容 |
|------|----------|
| `schemas/*_v{N}.schema.json` | 文件名版本号递增 |
| Schema 内 `schema_version` | 内部版本号更新 |
| `docs/contracts/versioning.md` | 变更记录 |
| `CHANGELOG.md` | 版本更新说明 |

**版本号规则**（参考 [versioning.md](../contracts/versioning.md)）：
- Minor 变更（向后兼容）：内部 `schema_version` 递增（如 `1.0` → `1.1`）
- Major 变更（破坏性）：文件名版本递增（如 `v1` → `v2`）

---

## Logbook-only 边界变更

### 边界变更类型矩阵

当变更涉及 Logbook-only 部署模式的边界时，需要同步更新多个文件。

| 边界变更类型 | 说明 | 影响范围 |
|-------------|------|----------|
| **initdb 行为** | SQL 初始化脚本变更、schema 定义修改 | DB 结构、验收测试 |
| **服务账号策略** | 角色定义、权限分配、账号创建逻辑 | 安全配置、权限验证 |
| **验收命令链** | Makefile targets、验收脚本入口点 | CI 流程、验收产物 |
| **verify-permissions 门禁** | 权限验证脚本、seek.enabled 控制 | 部署验证、CI 门禁 |
| **最小复制清单** | logbook_only_import_v1.json 文件列表 | 集成指南、复制工具 |

### 边界变更 → 必须更新的文件映射

| 边界变更类型 | 必须更新的文件 |
|-------------|---------------|
| **initdb 行为** | `compose/logbook.yml`<br>`sql/01_logbook_schema.sql`<br>`sql/04_roles_and_grants.sql`<br>`docs/logbook/03_deploy_verify_troubleshoot.md` |
| **服务账号策略** | `logbook_postgres/scripts/db_bootstrap.py`<br>`sql/04_roles_and_grants.sql`<br>`sql/verify/99_verify_permissions.sql`<br>`docs/logbook/04_acceptance_criteria.md` |
| **验收命令链** | `Makefile`<br>`logbook_postgres/scripts/logbook_cli.py`<br>`docs/logbook/04_acceptance_criteria.md`<br>`.github/workflows/ci.yml` |
| **verify-permissions 门禁** | `sql/verify/99_verify_permissions.sql`<br>`Makefile` (`verify-permissions` target)<br>`docs/logbook/04_acceptance_criteria.md` |
| **最小复制清单** | `docs/guides/manifests/logbook_only_import_v1.json`<br>`docs/guides/integrate_existing_project.md`<br>`docs/logbook/04_acceptance_criteria.md` |

### 文件路径详细清单

| 类型 | 文件路径 | 说明 |
|------|----------|------|
| **Compose 配置** | `compose/logbook.yml` | Logbook-only 部署配置 |
| **SQL 初始化** | `logbook_postgres/scripts/db_bootstrap.py` | 服务账号初始化 |
| **SQL 初始化** | `sql/01_logbook_schema.sql` | 核心 schema 定义 |
| **SQL 初始化** | `sql/04_roles_and_grants.sql` | 角色与权限定义 |
| **SQL 初始化** | `sql/verify/99_verify_permissions.sql` | 权限验证脚本 |
| **Makefile** | `Makefile` | 构建与验收 targets |
| **文档** | `docs/logbook/03_deploy_verify_troubleshoot.md` | 部署验证与排错 |
| **文档** | `docs/logbook/04_acceptance_criteria.md` | 验收标准与矩阵 |
| **集成指南** | `docs/guides/integrate_existing_project.md` | 项目集成指南 |
| **Manifest** | `docs/guides/manifests/logbook_only_import_v1.json` | 最小复制清单定义 |
| **CI 脚本** | `.github/workflows/ci.yml` | CI 检查流程 |
| **CI 脚本** | `Makefile` | 测试运行入口 |

### PR 自检命令集合

在提交 Logbook-only 边界变更的 PR 前，必须执行以下自检命令：

#### 基础验收（必需）

```bash
# 1. Logbook-only 完整验收
make acceptance-logbook-only

# 2. 权限验证门禁
make verify-permissions

# 3. 单元测试
make test-logbook-unit
```

#### 边界变更专项检查（按变更类型选择）

```bash
# initdb 行为变更 - 验证 schema 迁移
make up-logbook && make migrate-ddl
docker compose -f compose/logbook.yml exec postgres psql -U postgres -d $POSTGRES_DB -c "\\dt logbook.*"

# 服务账号策略变更 - 验证账号创建与权限
docker compose -f compose/logbook.yml exec postgres psql -U postgres -d $POSTGRES_DB -c "\\du engram_*"
docker compose -f compose/logbook.yml exec postgres psql -U postgres -d $POSTGRES_DB -c "SET seek.enabled = 'false';" -f /docker-entrypoint-initdb.d/99_verify_permissions.sql

# 验收命令链变更 - 验证 Makefile targets
make -n acceptance-logbook-only  # dry-run 检查
make logbook-smoke

# verify-permissions 门禁变更 - 验证 Seek 禁用场景
psql -d $POSTGRES_DB -c "SET seek.enabled = 'false';" -f sql/verify/99_verify_permissions.sql

# 最小复制清单变更 - 验证 manifest 完整性
python -c "import json; m = json.load(open('docs/guides/manifests/logbook_only_import_v1.json')); print('OK:', len(m['files']['required']), 'required,', len(m['files']['optional']), 'optional')"
```

#### 完整自检脚本

```bash
#!/bin/bash
# Logbook-only PR 自检脚本

set -e

echo "=== Logbook-only PR Self-Check ==="

# 步骤 1: 基础验收
echo "[1/5] Running acceptance-logbook-only..."
make acceptance-logbook-only

# 步骤 2: 权限验证
echo "[2/5] Running verify-permissions..."
make verify-permissions

# 步骤 3: 单元测试
echo "[3/5] Running test-logbook-unit..."
make test-logbook-unit

# 步骤 4: 检查无旧阶段别名
echo "[4/5] Checking legacy stage aliases..."
python scripts/check_no_legacy_stage_aliases.py --fail

# 步骤 5: 验证 manifest JSON 格式
echo "[5/5] Validating manifest JSON..."
python -c "import json; json.load(open('docs/guides/manifests/logbook_only_import_v1.json'))"

echo "=== All checks passed ==="
```

### Logbook-only 变更检查清单

- [ ] `compose/logbook.yml` 配置与 SQL 目录路径一致
- [ ] SQL 文件编号顺序正确（00-99）
- [ ] `99_verify_permissions.sql` 支持 `seek.enabled='false'` 跳过
- [ ] Makefile 中 `acceptance-logbook-only` target 正常工作
- [ ] `logbook_only_import_v1.json` 文件列表完整且路径正确
- [ ] `docs/logbook/04_acceptance_criteria.md` 验收矩阵已更新
- [ ] `docs/guides/integrate_existing_project.md` 集成说明已同步
- [ ] CI 脚本支持 Logbook-only 模式（无 Gateway/OpenMemory 依赖）

---

## DoD 检查清单

### 通用检查项

- [ ] 代码遵循项目命名规范
- [ ] 无阶段编号式旧别名引入
- [ ] 本地测试通过（`make test-logbook-unit`）
- [ ] CI 检查通过

### Schema/DB 变更检查项

- [ ] SQL 迁移脚本已添加并包含 UP/DOWN
- [ ] `db_migrate.py --verify` 通过
- [ ] 相关契约文档已更新
- [ ] 单元测试覆盖新 schema
- [ ] 集成测试验证数据流
- [ ] 验收矩阵已更新

### CLI 变更检查项

- [ ] CLI 帮助文档正确
- [ ] `--json-out` 输出符合产物规范
- [ ] Makefile target 已添加（如适用）
- [ ] 用户文档已更新
- [ ] 单元测试覆盖新命令

### 契约变更检查项

- [ ] 契约文档已更新（`docs/contracts/*.md`）
- [ ] 版本号已正确递增
- [ ] 示例数据已更新
- [ ] 契约测试已更新
- [ ] 相关组件边界文档已同步

### 破坏性变更检查项

- [ ] 迁移脚本包含回滚能力
- [ ] Backfill/Repair 命令已提供
- [ ] 版本号已升级（文件名 v1 → v2）
- [ ] 旧版本已标记 deprecated
- [ ] 迁移指南已编写
- [ ] CHANGELOG.md 已更新

---

## 验收流程

### PR 提交前

```bash
# 1. 检查旧阶段别名/历史命名
python scripts/check_no_legacy_stage_aliases.py --fail

# 2. 运行单元测试
make test-logbook-unit

# 3. 验证 DB 迁移（如有 schema 变更）
python logbook_postgres/scripts/db_migrate.py --dsn "$POSTGRES_DSN" --verify

# 4. 运行冒烟测试
make logbook-smoke
```

### PR 合并条件

1. 所有 CI 检查通过
2. DoD 检查清单全部完成
3. 至少一位 Reviewer 批准
4. 无未解决的 Review 评论

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [03_deploy_verify_troubleshoot.md](03_deploy_verify_troubleshoot.md) | 部署验证与排错（含部署级别定义） |
| [04_acceptance_criteria.md](04_acceptance_criteria.md) | 验收标准与验收矩阵 |
| [versioning.md](../contracts/versioning.md) | Schema 版本控制契约 |
| [gateway_logbook_boundary.md](../contracts/gateway_logbook_boundary.md) | Gateway ↔ Logbook 边界契约 |
| [00_acceptance_matrix.md](../acceptance/00_acceptance_matrix.md) | 完整验收测试矩阵 |
| [integrate_existing_project.md](../guides/integrate_existing_project.md) | 项目集成指南（含部署级别选择） |

---

更新时间：2026-01-30
