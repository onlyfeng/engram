# 在已有项目中引入 Engram

本文档说明如何将 Engram 集成到已有项目中，提供两种部署模式的完整复制清单。

---

## Manifest 与版本策略

本文档的复制清单同步维护在机器可读的 manifest 文件中：

| Manifest 文件 | 适用模式 | 版本 |
|--------------|----------|------|
| [`logbook_only_import_v1.json`](./manifests/logbook_only_import_v1.json) | Logbook-only | 1.0 |
| [`unified_stack_import_v1.json`](./manifests/unified_stack_import_v1.json) | Unified stack | 1.0 |

**版本策略**（遵循 [`docs/contracts/versioning.md`](../contracts/versioning.md)）：

- **文件命名**：`*_v{major}.json`，major 版本号仅在破坏性变更时递增
- **内部版本**：`manifest_version` 字段支持 `1.x` 演进（如 `1.0` → `1.1`）
- **向后兼容变更**（minor）：新增可选文件、放宽约束、扩展 profiles
- **破坏性变更**（major）：删除必需文件、修改路径结构、收紧约束 → 升级至 v2

---

## 概述

| 模式 | 说明 | 复杂度 |
|------|------|--------|
| **Logbook-only** | 仅事实账本（PostgreSQL + Logbook Schema） | 低 |
| **Unified stack** | 完整栈（Logbook + OpenMemory + Gateway） | 中 |

> **路径约束参考**：如果遇到构建或启动失败，请参阅 [导入路径约束参考](./import_path_constraints.md) 获取详细的失败症状、根因分析和修复方式。

---

## 准备工作

### 最小依赖

验证脚本需要以下工具：

| 工具 | 用途 | 安装方式 |
|------|------|----------|
| `jq` | JSON 解析（验证脚本必需） | `brew install jq` / `apt install jq` |
| `curl` | HTTP 请求（健康检查） | 通常已预装 |
| `python3` | 预检脚本（可选） | 通常已预装 |

```bash
# 检查依赖是否已安装
command -v jq >/dev/null && echo "jq: OK" || echo "jq: 缺失"
command -v curl >/dev/null && echo "curl: OK" || echo "curl: 缺失"
```

### 环境变量设置

```bash
# 假设 Engram 源码位于 ENGRAM_SRC（请替换为实际路径）
export ENGRAM_SRC=/path/to/engram

# 假设目标项目位于 TARGET_PROJECT
export TARGET_PROJECT=/path/to/your-project

# 确认 Engram 源码存在
ls "$ENGRAM_SRC/compose/logbook.yml" || echo "错误: ENGRAM_SRC 路径无效"
```

---

## Logbook-only（事实账本）

适用于仅需 PostgreSQL 事实账本、不需要 OpenMemory 语义记忆的场景。

### 1. 复制清单

#### 方式 A：使用自动化脚本（推荐）

```bash
# 基本用法：复制必需文件
python "$ENGRAM_SRC/scripts/import_copy.py" \
  --src "$ENGRAM_SRC" \
  --dst "$TARGET_PROJECT" \
  --mode logbook-only

# 包含可选文件（Logbook CLI & 迁移脚本）
python "$ENGRAM_SRC/scripts/import_copy.py" \
  --src "$ENGRAM_SRC" \
  --dst "$TARGET_PROJECT" \
  --mode logbook-only \
  --include-optional

# 干运行：仅显示将要复制的文件
python "$ENGRAM_SRC/scripts/import_copy.py" \
  --src "$ENGRAM_SRC" \
  --dst "$TARGET_PROJECT" \
  --mode logbook-only \
  --dry-run
```

#### 方式 B：手动复制（备用）

<details>
<summary>展开手动复制命令</summary>

```bash
cd "$TARGET_PROJECT"

# ============================================================
# 必需文件
# ============================================================

# 1.1 Compose 配置
mkdir -p compose
cp "$ENGRAM_SRC/compose/logbook.yml" compose/

# 1.2 Logbook SQL 初始化脚本
mkdir -p apps/logbook_postgres
cp -r "$ENGRAM_SRC/apps/logbook_postgres/sql" apps/logbook_postgres/

# ============================================================
# 可选文件（按需选择）
# ============================================================

# [可选] Logbook CLI & 迁移脚本
# 用途: db_migrate.py、logbook_cli_main.py 等 CLI 工具
# 如需运行 Logbook CLI 或数据库迁移脚本，请复制：
# cp -r "$ENGRAM_SRC/apps/logbook_postgres/scripts" apps/logbook_postgres/
```

</details>

### 2. 目录结构

```
your-project/
├── .env                           # 环境变量配置
├── compose/
│   └── logbook.yml                # Logbook Compose 配置
└── apps/logbook_postgres/
    ├── sql/                       # [必需] Logbook SQL 初始化脚本
    │   ├── 00_init_service_accounts.sh
    │   ├── 01_logbook_schema.sql
    │   ├── 02_logbook_indexes.sql
    │   └── ...
    └── scripts/                   # [可选] Logbook CLI & 迁移脚本
        ├── logbook_cli_main.py
        ├── db_migrate.py
        └── engram_logbook/
```

### 3. 最小 .env

```bash
cat > "$TARGET_PROJECT/.env" << 'EOF'
# ============================================================
# Logbook-only 最小配置
# ============================================================
PROJECT_KEY=myproject
POSTGRES_DB=myproject
POSTGRES_PASSWORD=changeme

# [可选] 端口配置（默认值如下）
# POSTGRES_PORT=5432
EOF
```

### 3.1 运行模式选择

Logbook-only 支持两种运行模式，根据安全需求选择：

#### 模式 A: Postgres Superuser（简单快速）

**适用场景**：开发环境、PoC、单用户部署

使用 postgres 超级用户直接连接，无需创建额外服务账号：

```bash
# .env 配置
PROJECT_KEY=myproject
POSTGRES_DB=myproject
POSTGRES_PASSWORD=changeme

# 健康检查使用 postgres 用户
POSTGRES_DSN="postgresql://postgres:${POSTGRES_PASSWORD}@localhost:5432/${POSTGRES_DB}"
logbook health
```

**特点**：
- 配置简单，适合快速验证
- 不需要设置 `LOGBOOK_*_PASSWORD`
- 所有操作使用同一超级用户

#### 模式 B: Service Accounts（最小权限）

**适用场景**：生产环境、多租户、合规要求

启用角色分离，使用独立的迁移账号和运行时账号：

```bash
# .env 配置
PROJECT_KEY=myproject
POSTGRES_DB=myproject
POSTGRES_PASSWORD=changeme

# [可选] 服务账号密码（启用角色分离）
LOGBOOK_MIGRATOR_PASSWORD=secure_migrator_pwd
LOGBOOK_SVC_PASSWORD=secure_svc_pwd
```

**特点**：
- `logbook_migrator`: 仅用于 DDL 迁移（CREATE/ALTER/DROP）
- `logbook_svc`: 仅用于运行时 DML 操作（SELECT/INSERT/UPDATE/DELETE）
- 满足最小权限原则和审计要求

**服务账号创建**：

服务账号在 PostgreSQL 首次启动时由 `00_init_service_accounts.sh` 自动创建。如手动创建：

```sql
-- 创建迁移账号
CREATE ROLE logbook_migrator LOGIN PASSWORD 'secure_migrator_pwd';
GRANT ALL ON SCHEMA logbook TO logbook_migrator;

-- 创建运行时账号
CREATE ROLE logbook_svc LOGIN PASSWORD 'secure_svc_pwd';
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA logbook TO logbook_svc;
```

> **注意**：两种模式均可通过 Acceptance Level A/B 验收。服务账号模式是可选的安全增强，不是验收必需。

### 4. 预检验证

复制完成后，运行验证脚本确保配置正确：

```bash
cd "$TARGET_PROJECT"

# ============================================================
# 验证流程（两步）
# ============================================================

# 预检: 文件完整性检查
python "$ENGRAM_SRC/scripts/import_preflight.py" . --verbose

# 构建边界检查（可选，Logbook-only 通常较简单）
bash "$ENGRAM_SRC/scripts/verify_build_boundaries.sh" --dry-run

# 或复制脚本到本地
mkdir -p scripts
cp "$ENGRAM_SRC/scripts/import_preflight.py" scripts/
python scripts/import_preflight.py .
```

> **依赖说明**：验证脚本需要 `jq` 和 `curl`。安装方式：`brew install jq` 或 `apt install jq`。

**预检内容**：
- 检查 docker-compose.*.yml 文件存在性
- 验证 `build.context` 目录是否存在
- 验证 volume bind mount 源路径是否存在
- 检查 Dockerfile 中的不当模式（如 `COPY ..`）
- 检查 .dockerignore 配置

**预期输出**（通过）：

```
==================================================
Engram 项目导入预检
==================================================

项目路径: /path/to/your-project
Compose 文件: 1

[OK] 找到 1 个 Compose 文件
[OK] 所有 0 个 build context 路径有效
[OK] 所有 2 个 volume 源路径有效
[OK] 检查了 0 个 Dockerfile，未发现问题
[OK] .dockerignore 配置正确
[OK] 未指定 manifest 文件，跳过检查

==================================================
[OK] 预检通过
==================================================
```

### 5. 启动

```bash
cd "$TARGET_PROJECT"

# 启动服务
docker compose -f compose/logbook.yml up -d

# 或使用 project name 隔离（多项目部署）
docker compose -p ${PROJECT_KEY:-myproject}-logbook -f compose/logbook.yml up -d
```

### 6. 健康检查

```bash
# 检查服务状态
docker compose -f compose/logbook.yml ps

# PostgreSQL 连接测试
docker compose -f compose/logbook.yml exec postgres \
  pg_isready -U postgres -d ${POSTGRES_DB:-myproject}

# [可选] 如已安装 Logbook CLI
cd apps/logbook_postgres/scripts && pip install -e .
POSTGRES_DSN="postgresql://postgres:${POSTGRES_PASSWORD:-changeme}@localhost:5432/${POSTGRES_DB:-myproject}" \
  logbook health
```

### 7. 成功标准

Logbook-only 支持两种验收级别，按需选择：

| 级别 | 名称 | 检查方式 | 依赖 |
|------|------|----------|------|
| **A** | DB Baseline-only | `pg_isready` | 仅 Docker |
| **B** | Acceptance-ready | `logbook health` / `make logbook-smoke` | Docker + Python scripts |

> **详细说明**：参见 [部署级别与验收能力](../logbook/03_deploy_verify_troubleshoot.md#部署级别与验收能力)

#### A) DB Baseline-only（最小验证）

| 检查项 | 命令 | 成功标志 |
|--------|------|----------|
| 服务状态 | `docker compose -f compose/logbook.yml ps` | `postgres` 显示 `running (healthy)` |
| 连接测试 | `docker exec postgres pg_isready -U postgres` | `accepting connections` |

#### B) Acceptance-ready（完整验证）

| 检查项 | 命令 | 成功标志 |
|--------|------|----------|
| CLI 健康检查 | `logbook health` | JSON 输出包含 `"ok": true` |
| 冒烟测试 | `make logbook-smoke` | `SMOKE_TEST_PASSED` |

**预期输出**（CLI 健康检查）：

```json
{"ok": true, "checks": {"connection": {"status": "ok"}, "schemas": {"status": "ok"}, ...}}
```

> **SeekDB 非阻塞**：Logbook-only 模式下 SeekDB 必须不阻塞。详见 [SeekDB 非阻塞约束](../logbook/03_deploy_verify_troubleshoot.md#seekdb-非阻塞约束)。

---

## Unified stack（统一栈）

适用于需要完整记忆系统（事实账本 + 语义搜索 + MCP 集成）的场景。

### Compose 文件角色

| 源文件 | 目标文件 | 角色 | 说明 |
|--------|----------|------|------|
| `docker-compose.unified.yml` | `docker-compose.engram.yml` | **主入口** | 完整统一栈部署 |
| `compose/openmemory.yml` | `compose/openmemory.yml` | 分步调试 | OpenMemory 独立调试（SQLite/PostgreSQL） |
| `compose/gateway.yml` | `compose/gateway.yml` | 分步调试 | Gateway 独立调试（需外部 PG/OM） |
| `compose/logbook.yml` | `compose/logbook.yml` | Logbook-only | Logbook 独立部署 |

> **验证入口**：`make verify-unified` 调用 `verify_unified_stack.sh`，参数约定见 [统一栈验证入口](#7-成功标准)。

### 1. 复制清单

#### 方式 A：使用自动化脚本（推荐）

使用 `import_copy.py` 脚本自动读取 manifest 并复制所需文件：

```bash
# 基本用法：复制必需文件（默认 unified 模式）
python "$ENGRAM_SRC/scripts/import_copy.py" \
  --src "$ENGRAM_SRC" \
  --dst "$TARGET_PROJECT"

# Logbook-only 模式：轻量级事实账本
python "$ENGRAM_SRC/scripts/import_copy.py" \
  --src "$ENGRAM_SRC" \
  --dst "$TARGET_PROJECT" \
  --mode logbook-only

# Unified 模式：完整栈（显式指定）
python "$ENGRAM_SRC/scripts/import_copy.py" \
  --src "$ENGRAM_SRC" \
  --dst "$TARGET_PROJECT" \
  --mode unified

# 包含可选文件（SeekDB、Gateway 模板等）
python "$ENGRAM_SRC/scripts/import_copy.py" \
  --src "$ENGRAM_SRC" \
  --dst "$TARGET_PROJECT" \
  --include-optional

# 干运行：仅显示将要复制的文件，不实际复制
python "$ENGRAM_SRC/scripts/import_copy.py" \
  --src "$ENGRAM_SRC" \
  --dst "$TARGET_PROJECT" \
  --dry-run

# 使用自定义 manifest（覆盖 --mode 选择）
python "$ENGRAM_SRC/scripts/import_copy.py" \
  --src "$ENGRAM_SRC" \
  --dst "$TARGET_PROJECT" \
  --manifest custom_manifest.json

# 跳过预检（仅复制，不运行验证）
python "$ENGRAM_SRC/scripts/import_copy.py" \
  --src "$ENGRAM_SRC" \
  --dst "$TARGET_PROJECT" \
  --skip-preflight
```

**脚本功能**：
- 支持 `--mode logbook-only|unified` 选择部署模式
  - `logbook-only`: 加载 `logbook_only_import_v1.json`
  - `unified`: 加载 `unified_stack_import_v1.json`（默认）
- 使用 `--manifest` 可覆盖 `--mode` 的默认选择
- 按 manifest 定义复制必需文件
- 使用 `--include-optional` 包含可选文件
- 复制完成后自动运行 `import_preflight.py` 验证
- 输出中明确显示 required/optional 文件的选择结果
- 支持 `--dry-run` 预览复制内容

#### 方式 B：手动复制（备用）

如需手动控制或自定义复制过程，可使用以下命令：

<details>
<summary>展开手动复制命令</summary>

```bash
cd "$TARGET_PROJECT"

# ============================================================
# 必需文件
# ============================================================

# 1.1 统一栈 Compose 配置
cp "$ENGRAM_SRC/docker-compose.unified.yml" docker-compose.engram.yml

# 1.2 分步部署 Compose 配置
mkdir -p compose
cp "$ENGRAM_SRC/compose/logbook.yml" compose/
cp "$ENGRAM_SRC/compose/openmemory.yml" compose/
cp "$ENGRAM_SRC/compose/gateway.yml" compose/

# 1.3 Logbook SQL 初始化脚本
mkdir -p apps/logbook_postgres
cp -r "$ENGRAM_SRC/apps/logbook_postgres/sql" apps/logbook_postgres/

# 1.4 Logbook 迁移脚本（统一栈必需）
cp -r "$ENGRAM_SRC/apps/logbook_postgres/scripts" apps/logbook_postgres/

# 1.5 Gateway 服务代码
mkdir -p apps/openmemory_gateway
cp -r "$ENGRAM_SRC/apps/openmemory_gateway/gateway" apps/openmemory_gateway/

# 1.6 OpenMemory 源码
mkdir -p libs
cp -r "$ENGRAM_SRC/libs/OpenMemory" libs/

# 1.7 SeekDB SQL 初始化脚本（可选）
# 仅当需要启用 SeekDB 功能时才需要复制
# SeekDB 现已通过 override compose 文件（docker-compose.unified.seekdb.yml）管理
# 如需启用 SeekDB:
mkdir -p apps/seekdb_rag_hybrid/sql
cp "$ENGRAM_SRC/apps/seekdb_rag_hybrid/sql/01_seekdb_roles_and_grants.sql" apps/seekdb_rag_hybrid/sql/
cp "$ENGRAM_SRC/apps/seekdb_rag_hybrid/sql/02_seekdb_index.sql" apps/seekdb_rag_hybrid/sql/
cp "$ENGRAM_SRC/docker-compose.unified.seekdb.yml" ./

# ============================================================
# 可选文件（按需选择）
# ============================================================

# [可选] Gateway 模板文件（.mcp.json 示例、环境变量示例）
cp -r "$ENGRAM_SRC/apps/openmemory_gateway/templates" apps/openmemory_gateway/

# [可选] Dashboard（OpenMemory Web UI）
# 需先从上游同步源码到 libs/OpenMemory/dashboard/
# 启用方式: docker compose --profile dashboard up -d
```

</details>

### 2. 目录结构

```
your-project/
├── .env                           # 环境变量配置
├── docker-compose.engram.yml      # 统一栈主配置
├── docker-compose.engram.seekdb.yml  # [可选] SeekDB override compose
├── compose/                       # 分步部署配置
│   ├── logbook.yml
│   ├── openmemory.yml
│   └── gateway.yml
├── apps/
│   ├── logbook_postgres/
│   │   ├── sql/                   # [必需] Logbook SQL 初始化脚本
│   │   └── scripts/               # [必需] Logbook 迁移脚本
│   │       └── engram_logbook/
│   ├── openmemory_gateway/
│   │   ├── gateway/               # [必需] Gateway 服务代码
│   │   └── templates/             # [可选] 模板文件
│   └── seekdb_rag_hybrid/         # [可选] 仅启用 SeekDB 时需要
│       └── sql/                   #   禁用 SeekDB 时无需复制
└── libs/
    └── OpenMemory/                # [必需] OpenMemory 源码
```

### 3. 最小 .env

```bash
cat > "$TARGET_PROJECT/.env" << 'EOF'
# ============================================================
# Unified stack 最小配置
# ============================================================
PROJECT_KEY=myproject
POSTGRES_DB=myproject

# 服务账号密码（统一栈强制要求）
LOGBOOK_MIGRATOR_PASSWORD=changeme1
LOGBOOK_SVC_PASSWORD=changeme2
OPENMEMORY_MIGRATOR_PASSWORD=changeme3
OPENMEMORY_SVC_PASSWORD=changeme4

# ============================================================
# 可选配置
# ============================================================

# PostgreSQL 超级用户密码（默认 postgres）
# POSTGRES_PASSWORD=postgres

# 端口配置（默认值如下）
# POSTGRES_PORT=5432
# OM_PORT=8080
# GATEWAY_PORT=8787

# ============================================================
# SeekDB 配置
# ============================================================
# 推荐变量名: SEEKDB_ENABLE（规范前缀）
# ⚠️ 废弃变量: SEEK_ENABLE 将于 2026-Q3 移除
# 详见: docs/reference/environment_variables.md#seekdb-组件可选层
#
# SeekDB 通过 override compose 文件启用:
# 启用: docker compose -f docker-compose.engram.yml -f docker-compose.engram.seekdb.yml up -d
# 禁用: docker compose -f docker-compose.engram.yml up -d
#
# SEEKDB_ENABLE=1

# ============================================================
# API 密钥配置
# ============================================================
# 推荐变量名: OM_API_KEY（规范前缀）
# ⚠️ 废弃变量: OPENMEMORY_API_KEY 仍支持但优先级低于 OM_API_KEY
# 详见: docs/reference/environment_variables.md#openmemory-组件
#
# OM_API_KEY=your-api-key

# Dashboard 端口（启用 dashboard profile 时）
# DASHBOARD_PORT=3000
EOF
```

### 4. 预检验证（文件完整性 / 构建边界 / 服务验证）

复制完成后，**务必运行验证脚本**确保部署正确。统一栈包含多个组件，推荐按以下三阶段流程执行验证：

```bash
cd "$TARGET_PROJECT"

# ============================================================
# 推荐验证流程（文件完整性→构建边界→服务验证）
# ============================================================

# 预检: 文件完整性检查（import_preflight.py）
# 验证所有依赖路径是否存在
python "$ENGRAM_SRC/scripts/import_preflight.py" . --verbose

# 构建边界检查（verify_build_boundaries.sh）
# 验证 Dockerfile 和 Compose 配置是否正确
bash "$ENGRAM_SRC/scripts/verify_build_boundaries.sh" --dry-run

# HTTP 验证（verify_unified_stack.sh）
# 验证服务是否正常响应（需要先启动服务）
bash "$ENGRAM_SRC/apps/openmemory_gateway/scripts/verify_unified_stack.sh" --mode stepwise
```

**一键验证脚本**（推荐）：

```bash
# 复制验证脚本到本地（一次性）
mkdir -p scripts
cp "$ENGRAM_SRC/scripts/import_preflight.py" scripts/
cp "$ENGRAM_SRC/scripts/verify_build_boundaries.sh" scripts/
cp "$ENGRAM_SRC/apps/openmemory_gateway/scripts/verify_unified_stack.sh" scripts/

# 串联执行三阶段验证（预检→构建→服务）
python scripts/import_preflight.py . --verbose && \
bash scripts/verify_build_boundaries.sh --dry-run && \
echo "[INFO] 启动服务后运行: bash scripts/verify_unified_stack.sh --mode stepwise"
```

**各步骤说明**：

| 步骤 | 脚本 | 检查内容 | 依赖 |
|------|------|----------|------|
| 预检 | `import_preflight.py` | Compose 文件、build context、volume 路径 | Python |
| 构建边界检查 | `verify_build_boundaries.sh --dry-run` | Dockerfile 模式、.dockerignore、context 配置 | Bash |
| HTTP 验证 | `verify_unified_stack.sh --mode stepwise` | HTTP 健康检查、MCP 工具调用 | `jq`, `curl` |

**验证入口参数约定**：

Makefile 的 `verify-unified` 与 `verify_unified_stack.sh` 参数保持一致：

| Makefile 调用 | 脚本参数 | 环境变量 | 说明 |
|---------------|----------|----------|------|
| `make verify-unified` | `--mode default` | `VERIFY_MODE` | 默认模式（依赖 Docker） |
| `VERIFY_FULL=1 make verify-unified` | `--full` | `VERIFY_FULL` | 完整验证（含降级测试） |
| `VERIFY_JSON_OUT=path make verify-unified` | `--json-out path` | `JSON_OUT_PATH` | JSON 输出路径 |

```bash
# 直接调用脚本（自定义参数）
./apps/openmemory_gateway/scripts/verify_unified_stack.sh --mode stepwise --json-out .artifacts/verify.json

# 通过 Makefile 调用（推荐）
make verify-unified
VERIFY_FULL=1 VERIFY_JSON_OUT=.artifacts/verify.json make verify-unified
```

**预检内容**：
- 检查 docker-compose.*.yml 文件（包括 docker-compose.engram.yml）
- 验证所有 `build.context` 目录存在（libs/OpenMemory/packages/openmemory-js 等）
- 验证所有 volume bind mount 路径存在（apps/logbook_postgres/sql 等）
- 检查 Dockerfile 模式和 .dockerignore 配置

**构建边界检查**：
- 验证 Compose context 配置正确性
- 检查 Dockerfile 中的 COPY 模式（如 `COPY ..` 危险模式）
- 确认 .dockerignore 排除了大文件/目录

**常见预检错误**：

| 错误 | 原因 | 修复 |
|------|------|------|
| `build context 不存在: libs/OpenMemory/...` | OpenMemory 未复制 | `cp -r "$ENGRAM_SRC/libs/OpenMemory" libs/` |
| `volume 源路径不存在: apps/logbook_postgres/sql` | SQL 脚本未复制 | `cp -r "$ENGRAM_SRC/apps/logbook_postgres/sql" apps/logbook_postgres/` |
| `volume 源路径不存在: apps/seekdb_rag_hybrid/sql/...` | SeekDB SQL 未复制 | 见下方 SeekDB 说明或禁用 SeekDB |
| `jq: command not found` | 缺少 jq 工具 | `brew install jq` / `apt install jq` |

**SeekDB 路径说明**：启用 SeekDB 时需要额外复制 SQL 文件。详见 [路径修正指南](#seekdb-sql-文件路径要求)。

### 5. 启动

```bash
cd "$TARGET_PROJECT"

# ============================================================
# 方式 A: 不使用 SeekDB（默认，无需额外文件）
# ============================================================
# 仅 Logbook + OpenMemory，不需要复制 seekdb/sql 目录
docker compose -f docker-compose.engram.yml up -d

# ============================================================
# 方式 B: 启用 SeekDB（需要先复制 SeekDB 相关文件）
# ============================================================
# 需要先复制:
#   - apps/seekdb_rag_hybrid/sql/01_seekdb_roles_and_grants.sql
#   - apps/seekdb_rag_hybrid/sql/02_seekdb_index.sql
#   - docker-compose.unified.seekdb.yml（重命名为 docker-compose.engram.seekdb.yml）
docker compose -f docker-compose.engram.yml -f docker-compose.engram.seekdb.yml up -d

# ============================================================
# 使用 project name 隔离（多项目部署）
# ============================================================
COMPOSE_PROJECT_NAME=${PROJECT_KEY:-myproject} \
  docker compose -f docker-compose.engram.yml up -d

# ============================================================
# 可选 Profile 启动
# ============================================================

# 启用 Dashboard（需先同步上游 dashboard 源码）
docker compose -f docker-compose.engram.yml --profile dashboard up -d

# 启用 MinIO（对象存储，开发/CI 使用）
docker compose -f docker-compose.engram.yml --profile minio up -d

# 启用 SCM 同步
docker compose -f docker-compose.engram.yml --profile scm_sync up -d

# 组合多个 profile（含 SeekDB）
docker compose -f docker-compose.engram.yml -f docker-compose.engram.seekdb.yml \
  --profile minio --profile dashboard up -d
```

### 6. 健康检查

```bash
cd "$TARGET_PROJECT"

# 服务状态
docker compose -f docker-compose.engram.yml ps

# PostgreSQL 健康检查
docker compose -f docker-compose.engram.yml exec postgres \
  pg_isready -U postgres -d ${POSTGRES_DB:-myproject}

# OpenMemory 健康检查
curl -sf http://localhost:${OM_PORT:-8080}/health && echo "OpenMemory: OK"

# Gateway 健康检查
curl -sf http://localhost:${GATEWAY_PORT:-8787}/health && echo "Gateway: OK"

# Logbook CLI 健康检查（可选）
cd apps/logbook_postgres/scripts && pip install -e .
POSTGRES_DSN="postgresql://logbook_svc:${LOGBOOK_SVC_PASSWORD}@localhost:5432/${POSTGRES_DB:-myproject}" \
  logbook health
```

### 7. 成功标准

| 检查项 | 成功标志 |
|--------|----------|
| PostgreSQL | 状态显示 `running (healthy)` |
| OpenMemory | `curl /health` 返回 `{"status":"ok"}` |
| Gateway | `curl /health` 返回 `{"status":"ok"}` |
| Logbook CLI | JSON 输出包含 `"ok": true` |

**快速验证命令**：

```bash
# 统一栈一键健康检查
docker compose -f docker-compose.engram.yml ps | grep -q "healthy" && \
curl -sf http://localhost:8080/health && \
curl -sf http://localhost:8787/health && \
echo "✓ Unified stack 部署成功"
```

---

## 可选组件说明

| 组件 | 用途 | 启用方式 | 必需文件 |
|------|------|----------|----------|
| **Logbook CLI** | 数据库迁移、健康检查、调试 | `pip install -e apps/logbook_postgres/scripts` | `apps/logbook_postgres/scripts/` |
| **SeekDB** | RAG 分块索引、混合检索 | `-f docker-compose.engram.seekdb.yml` 叠加 | `apps/seekdb_rag_hybrid/sql/`、`docker-compose.unified.seekdb.yml` |
| **Dashboard** | OpenMemory Web UI | `--profile dashboard` | 需从上游同步 `libs/OpenMemory/dashboard/` |
| **MinIO** | 对象存储（开发/CI） | `--profile minio` | `apps/logbook_postgres/scripts/ops/`、`apps/logbook_postgres/templates/` |
| **SCM Sync** | SCM 增量同步 | `--profile scm_sync` | `apps/logbook_postgres/scripts/`（已包含在核心依赖） |

> **SeekDB 禁用说明**：SeekDB 通过 `docker-compose.unified.seekdb.yml` override compose 文件启用。**禁用 SeekDB 时，无需复制任何 SeekDB 相关文件**（`apps/seekdb_rag_hybrid/` 目录和 `docker-compose.unified.seekdb.yml`）。主 compose 文件 `docker-compose.unified.yml` 不引用任何 SeekDB 路径，因此可直接运行：
>
> ```bash
> # 禁用 SeekDB（默认部署，无需 SeekDB 文件）
> docker compose -f docker-compose.engram.yml up -d
>
> # 启用 SeekDB（需要先复制 SeekDB 相关文件）
> docker compose -f docker-compose.engram.yml -f docker-compose.engram.seekdb.yml up -d
> ```

---

## 复制档位

根据目标项目对 OpenMemory 的维护深度，可选择不同的复制档位。

### 档位定义

| 档位 | 适用场景 | 复制范围 | 维护成本 |
|------|----------|----------|----------|
| **L1 最小部署** | 仅使用 OpenMemory 功能，不关心上游同步 | 核心服务代码 | 低 |
| **L2 版本锁定** | 需要追踪上游版本，但不维护补丁 | L1 + lock 文件 | 低 |
| **L3 完整维护** | 长期维护统一栈，需要上游同步与补丁管理 | L2 + vendoring 工具链 | 中 |

### L1 最小部署（默认）

前述 [Unified Stack 复制清单](#unified-stack-模式) 即为 L1 档位，包含运行所需的全部文件，无需额外复制。

### L2 版本锁定

在 L1 基础上复制 vendoring 元数据文件，用于追踪上游版本：

```bash
# 在 L1 基础上额外复制
cp "$ENGRAM_SRC/OpenMemory.upstream.lock.json" ./
```

**作用**：
- `OpenMemory.upstream.lock.json`：记录当前 vendored 的上游版本（ref、commit SHA、校验和），便于在 CI 或文档中追溯 OpenMemory 版本

> **参考**：详见 [`docs/openmemory/00_vendoring_and_patches.md`](../openmemory/00_vendoring_and_patches.md) 中"关键文件"章节对 lock 文件的说明。

### L3 完整维护（推荐用于长期维护项目）

若目标项目需要长期维护统一栈，建议复制完整的 vendoring 工具链：

```bash
# 在 L2 基础上额外复制
cp "$ENGRAM_SRC/openmemory_patches.json" ./
cp -r "$ENGRAM_SRC/patches/openmemory" patches/
mkdir -p scripts
cp "$ENGRAM_SRC/scripts/openmemory_sync.py" scripts/
cp "$ENGRAM_SRC/scripts/generate_om_patches.sh" scripts/
```

**各文件作用**：

| 文件 | 必需性 | 作用 |
|------|--------|------|
| `OpenMemory.upstream.lock.json` | **必需** | 上游版本锁定，包含 ref、commit SHA、archive SHA256、补丁文件校验和 |
| `openmemory_patches.json` | **必需** | 补丁清单，记录每个修改点的位置、分类（A/B/C）、原因、上游化潜力 |
| `patches/openmemory/` | 可选 | 生成的 `.patch` 文件，用于审计和重建；非严格模式下可为空 |
| `scripts/openmemory_sync.py` | 可选 | 核心同步工具，支持 fetch/sync/verify/schema-validate 等命令 |
| `scripts/generate_om_patches.sh` | 可选 | Bash 补丁生成脚本，支持 generate/apply/verify/backfill 等操作 |

**为何可选**：

- **`patches/openmemory/`**：Engram 默认采用 Non-Strict 模式（见 [vendoring 文档](../openmemory/00_vendoring_and_patches.md#82-non-strict-模式验收标准)），patch 文件仅在 `upstream_ref` 变更或 release 准备时强制要求。日常开发无需 patch 文件存在。
- **`scripts/openmemory_sync.py`** 和 **`scripts/generate_om_patches.sh`**：仅在需要执行上游同步或补丁生成时使用。若仅需追踪版本而不执行同步，可不复制。

**L3 档位的价值**：

1. **上游升级能力**：通过 `openmemory_sync.py` 可执行三阶段升级流程（preview → sync → promote）
2. **补丁审计**：`openmemory_patches.json` 详细记录了 20 个修改点的分类与原因
3. **CI 门禁**：可启用 `make openmemory-sync-check` 等 CI 检查
4. **回滚支持**：lock 文件中的 `rollback_procedure` 和 `checksums` 支持版本回滚

> **参考**：完整的 vendoring 流程、CI 门禁要求、冲突分级策略详见 [`docs/openmemory/00_vendoring_and_patches.md`](../openmemory/00_vendoring_and_patches.md)。

### 档位选择建议

| 场景 | 推荐档位 | 理由 |
|------|----------|------|
| PoC / 原型验证 | L1 | 快速部署，无需关心版本管理 |
| 中小型项目 | L2 | 追踪版本以便排查问题 |
| 企业级长期维护 | L3 | 完整的版本控制与补丁管理能力 |
| 计划贡献上游 | L3 | 需要 patch 文件用于 PR 提交 |

---

## 路径修正指南

复制文件后，需要修正 Compose 文件中的相对路径。以下是需要检查的路径。

> **完整约束清单**：如需了解每个约束的失败症状、根因分析和修复方式，请参阅 [导入路径约束参考](./import_path_constraints.md)。

### docker-compose.engram.yml（统一栈）

```yaml
# 确认以下路径相对于 docker-compose.engram.yml 正确
volumes:
  - ./apps/logbook_postgres/sql:/docker-entrypoint-initdb.d:ro
  - ./apps/logbook_postgres:/app:ro
  - ./libs/OpenMemory/packages/openmemory-js:/app:ro

build:
  context: ./libs/OpenMemory/packages/openmemory-js
  context: .  # Gateway 构建需要项目根目录
```

### Gateway build.context 约束（重要）

**Gateway 的 `build.context` 必须指向目标项目根目录**。这是由 Dockerfile 中的 COPY 指令决定的硬性约束。

**Dockerfile COPY 依赖**（参见 `apps/openmemory_gateway/gateway/Dockerfile`）：

```dockerfile
# Layer 1: 第三方依赖
COPY apps/openmemory_gateway/gateway/requirements.runtime.txt /app/requirements.runtime.txt

# Layer 2: Logbook 本地包（跨目录依赖）
COPY apps/logbook_postgres/scripts /logbook_scripts

# Layer 3: Gateway 应用代码
COPY apps/openmemory_gateway/gateway/pyproject.toml /app/
COPY apps/openmemory_gateway/gateway/README.md /app/
COPY apps/openmemory_gateway/gateway/gateway/ /app/gateway/
COPY apps/openmemory_gateway/gateway/tests/ /app/tests/
```

**关键约束**：
- 所有 COPY 路径都是相对于 `build.context` 的
- `apps/logbook_postgres/scripts` 是 Gateway 构建的**跨目录依赖**
- 如果 `build.context` 不是项目根目录，Dockerfile 将无法找到这些路径

**Compose 配置要求**：

| Compose 文件 | build.context | 说明 |
|--------------|---------------|------|
| `docker-compose.engram.yml` | `.` | 项目根目录 |
| `compose/gateway.yml` | `..` | 相对于 compose/ 的项目根目录 |

**允许的重构方式**：

如需改变目录结构（例如重命名 `apps/` 或移动 `logbook_postgres/`），**必须同步修改以下文件**：

1. `apps/openmemory_gateway/gateway/Dockerfile` - 更新所有 COPY 路径
2. `docker-compose.unified.yml` - 更新 gateway/worker 服务的 build 配置
3. `compose/gateway.yml` - 更新 gateway/worker 服务的 build 配置

**风险警告**：

| 风险 | 后果 | 预防措施 |
|------|------|----------|
| 修改目录结构未更新 Dockerfile | 构建失败：`COPY failed: file not found` | 修改前运行 `docker build` 验证 |
| build.context 设置错误 | 构建失败或镜像缺少依赖 | 使用预检脚本验证路径 |
| Dockerfile 路径与实际结构不一致 | CI/CD 构建失败 | 在 PR 中包含 Dockerfile 修改的完整测试 |

### docker-compose.engram.seekdb.yml（SeekDB override，可选）

```yaml
# 仅在启用 SeekDB 时需要
# 用法: docker compose -f docker-compose.engram.yml -f docker-compose.engram.seekdb.yml up -d
volumes:
  - ./apps/seekdb_rag_hybrid/sql/01_seekdb_roles_and_grants.sql:/sql/06_seekdb_roles_and_grants.sql:ro
  - ./apps/seekdb_rag_hybrid/sql/02_seekdb_index.sql:/sql/09_seekdb_index.sql:ro
```

### SeekDB 启用方式（Override Compose）

SeekDB 相关的 volume bind 已移至独立的 override compose 文件 `docker-compose.unified.seekdb.yml`。

**新的架构**：

| 文件 | 内容 | 场景 |
|------|------|------|
| `docker-compose.unified.yml` | 核心服务（Logbook + OpenMemory + Gateway） | 默认部署 |
| `docker-compose.unified.seekdb.yml` | SeekDB SQL 脚本 volume bind | 启用 SeekDB 时叠加 |

**启用 SeekDB**：

```bash
# 需要先复制 SeekDB 相关文件
mkdir -p apps/seekdb_rag_hybrid/sql
cp "$ENGRAM_SRC/apps/seekdb_rag_hybrid/sql/01_seekdb_roles_and_grants.sql" apps/seekdb_rag_hybrid/sql/
cp "$ENGRAM_SRC/apps/seekdb_rag_hybrid/sql/02_seekdb_index.sql" apps/seekdb_rag_hybrid/sql/
cp "$ENGRAM_SRC/docker-compose.unified.seekdb.yml" docker-compose.engram.seekdb.yml

# 启动时叠加 override compose
docker compose -f docker-compose.engram.yml -f docker-compose.engram.seekdb.yml up -d
```

**禁用 SeekDB（默认）**：

```bash
# 不需要复制 seekdb/sql 目录，也不需要 override compose 文件
docker compose -f docker-compose.engram.yml up -d
```

- **优点**：
  - 禁用 SeekDB 时无需复制任何 SeekDB 相关文件
  - `docker compose up` 不会因缺少文件而报错
  - 后续启用 SeekDB 只需添加 override compose 叠加
- **与旧版本的差异**：
  - 旧版本要求即使 `SEEKDB_ENABLE=0` 也必须复制 SQL 文件
  - 新版本完全解耦，禁用时无需任何 SeekDB 文件

### compose/logbook.yml（Logbook-only）

```yaml
# 确认以下路径相对于 compose/logbook.yml 正确
volumes:
  - ../apps/logbook_postgres/sql:/docker-entrypoint-initdb.d:ro
  - ../apps/logbook_postgres:/app:ro
```

---

## 多项目部署

```bash
# 项目 A
export PROJECT_KEY=proj_a
export POSTGRES_DB=proj_a
COMPOSE_PROJECT_NAME=$PROJECT_KEY docker compose -f docker-compose.engram.yml up -d

# 项目 B
export PROJECT_KEY=proj_b
export POSTGRES_DB=proj_b
COMPOSE_PROJECT_NAME=$PROJECT_KEY docker compose -f docker-compose.engram.yml up -d

# 查看各项目状态
docker compose -p proj_a ps
docker compose -p proj_b ps
```

---

## 常见问题

### Q: 复制后 Docker Compose 报错找不到文件？

检查相对路径是否正确。Compose 文件中的路径是相对于 Compose 文件本身的位置。

```bash
# 验证文件存在
ls -la apps/logbook_postgres/sql/
ls -la libs/OpenMemory/packages/openmemory-js/
```

### Q: 如何只复制最小必需文件？

**Logbook-only**:
- `compose/logbook.yml`
- `apps/logbook_postgres/sql/`

**Unified stack（不含 SeekDB）**:
- `docker-compose.unified.yml`
- `apps/logbook_postgres/sql/`
- `apps/logbook_postgres/scripts/`
- `apps/openmemory_gateway/gateway/`
- `libs/OpenMemory/`

**Unified stack（含 SeekDB）**:
- 上述所有文件，加上：
- `docker-compose.unified.seekdb.yml`
- `apps/seekdb_rag_hybrid/sql/01_seekdb_roles_and_grants.sql`
- `apps/seekdb_rag_hybrid/sql/02_seekdb_index.sql`

**说明**：SeekDB 相关文件现在是可选的。禁用 SeekDB 时无需复制这些文件。详见 [路径修正指南 - SeekDB 启用方式](#seekdb-启用方式override-compose)。

### Q: 服务账号密码是什么？

统一栈使用最小权限角色分离：
- `logbook_migrator`: Logbook DDL 迁移账号
- `logbook_svc`: Logbook 运行时 DML 账号
- `openmemory_migrator_login`: OpenMemory DDL 迁移账号
- `openmemory_svc`: OpenMemory 运行时 DML 账号

这些角色在 PostgreSQL 首次启动时由 `00_init_service_accounts.sh` 自动创建。

### Q: API Key 应该使用哪个变量名？

**推荐变量名**: `OM_API_KEY`

```bash
# 推荐（规范前缀）
OM_API_KEY=your-api-key

# 仍支持但不推荐（优先级高于 OM_API_KEY，但命名不规范）
OPENMEMORY_API_KEY=your-api-key
```

**说明**：Gateway 组件同时支持 `OM_API_KEY` 和 `OPENMEMORY_API_KEY`，后者优先级更高（为兼容旧配置）。新项目建议统一使用 `OM_API_KEY`。

详见 [环境变量参考 - Gateway 组件](../reference/environment_variables.md#gateway-组件) 和 [API Key 优先级](../reference/environment_variables.md#api-key-优先级)。

### Q: 如何禁用 SeekDB？

```bash
# 禁用 SeekDB（默认，不需要复制 seekdb/sql 目录）
docker compose -f docker-compose.engram.yml up -d

# 启用 SeekDB（需要先复制 seekdb/sql 目录和 override compose）
docker compose -f docker-compose.engram.yml -f docker-compose.engram.seekdb.yml up -d
```

**变量命名说明**：

- **推荐变量名**: `SEEKDB_ENABLE`（规范前缀 `SEEKDB_*`）
- **废弃变量**: `SEEK_ENABLE` 将于 **2026-Q3 移除**
- 详细的变量列表和废弃说明请参考 [环境变量参考 - SeekDB 组件](../reference/environment_variables.md#seekdb-组件可选层) 和 [废弃变量](../reference/environment_variables.md#废弃变量)

说明：SeekDB 现在通过 compose 文件叠加控制。禁用 SeekDB 时无需复制任何 SeekDB 相关文件。

---

## 依赖路径完整映射

本节提供 Docker Compose 和 Dockerfile 中所有路径依赖的完整对照表。

### docker-compose.unified.yml 依赖分析

#### Volume Bind Mounts

**docker-compose.unified.yml（核心）**:

| 服务 | 源路径 | 容器目标 | 必需性 |
|------|--------|----------|--------|
| postgres | `./apps/logbook_postgres/sql` | `/docker-entrypoint-initdb.d:ro` | **必需** |
| bootstrap_roles | `./apps/logbook_postgres/sql/03_pgvector_extension.sql` | `/sql/03_pgvector_extension.sql:ro` | **必需** |
| bootstrap_roles | `./apps/logbook_postgres/sql/04_roles_and_grants.sql` | `/sql/04_roles_and_grants.sql:ro` | **必需** |
| bootstrap_roles | `./apps/logbook_postgres/sql/05_openmemory_roles_and_grants.sql` | `/sql/05_openmemory_roles_and_grants.sql:ro` | **必需** |
| bootstrap_roles | `./apps/logbook_postgres/sql/99_verify_permissions.sql` | `/sql/99_verify_permissions.sql:ro` | **必需** |

**docker-compose.unified.seekdb.yml（SeekDB override，可选）**:

| 服务 | 源路径 | 容器目标 | 必需性 |
|------|--------|----------|--------|
| postgres | `./apps/seekdb_rag_hybrid/sql/01_seekdb_roles_and_grants.sql` | `/sql/06_seekdb_roles_and_grants.sql:ro` | SeekDB 启用时必需 |
| postgres | `./apps/seekdb_rag_hybrid/sql/02_seekdb_index.sql` | `/sql/09_seekdb_index.sql:ro` | SeekDB 启用时必需 |
| bootstrap_roles | `./apps/seekdb_rag_hybrid/sql/01_seekdb_roles_and_grants.sql` | `/sql/06_seekdb_roles_and_grants.sql:ro` | SeekDB 启用时必需 |
| logbook_migrate | `./apps/logbook_postgres` | `/app:ro` | **必需** |
| permissions_verify | `./apps/logbook_postgres/sql/99_verify_permissions.sql` | `/verify.sql:ro` | **必需** |
| logbook_tools | `./apps/logbook_postgres/scripts` | `/app/scripts:ro` | tools profile 启用时必需 |
| scm_scheduler | `./apps/logbook_postgres/scripts` | `/app/scripts:ro` | scm_sync profile 启用时必需 |
| scm_worker | `./apps/logbook_postgres/scripts` | `/app/scripts:ro` | scm_sync profile 启用时必需 |
| scm_reaper | `./apps/logbook_postgres/scripts` | `/app/scripts:ro` | scm_sync profile 启用时必需 |
| logbook_test | `./apps/logbook_postgres` | `/app:ro` | test profile 启用时必需 |
| minio_init | `./apps/logbook_postgres/scripts/ops` | `/ops:ro` | minio profile 启用时必需 |
| minio_init | `./apps/logbook_postgres/templates` | `/templates:ro` | minio profile 启用时必需 |

#### Build Contexts

| 服务 | Context | Dockerfile | 必需性 |
|------|---------|------------|--------|
| openmemory_migrate | `./libs/OpenMemory/packages/openmemory-js` | Dockerfile | **必需** |
| openmemory | `./libs/OpenMemory/packages/openmemory-js` | Dockerfile | **必需** |
| gateway | `.` (项目根) | `apps/openmemory_gateway/gateway/Dockerfile` | **必需** |
| worker | `.` (项目根) | `apps/openmemory_gateway/gateway/Dockerfile` | **必需** |
| dashboard | `./libs/OpenMemory/dashboard` | Dockerfile | dashboard profile 启用时必需 |

### Gateway Dockerfile COPY 依赖

Gateway 和 Worker 服务的 Dockerfile 从项目根目录 COPY 以下路径：

| 源路径 | 容器目标 | 用途 |
|--------|----------|------|
| `apps/openmemory_gateway/gateway/requirements.runtime.txt` | `/app/requirements.runtime.txt` | Python 运行时依赖 |
| `apps/logbook_postgres/scripts` | `/logbook_scripts` | engram_logbook 包 |
| `apps/openmemory_gateway/gateway/pyproject.toml` | `/app/` | 包配置 |
| `apps/openmemory_gateway/gateway/README.md` | `/app/` | 包元数据 |
| `apps/openmemory_gateway/gateway/gateway/` | `/app/gateway/` | Gateway 核心代码 |
| `apps/openmemory_gateway/gateway/tests/` | `/app/tests/` | 测试代码（可选） |

### 分步部署 Compose 文件依赖

#### compose/logbook.yml

| 源路径 | 容器目标 |
|--------|----------|
| `../apps/logbook_postgres/sql` | `/docker-entrypoint-initdb.d:ro` |
| `../apps/logbook_postgres` | `/app:ro` |

#### compose/openmemory.yml

| 服务 | Build Context |
|------|---------------|
| openmemory | `../libs/OpenMemory/packages/openmemory-js` |
| openmemory_migrate | `../libs/OpenMemory/packages/openmemory-js` |
| dashboard | `../libs/OpenMemory/dashboard` |

#### compose/gateway.yml

| 服务 | Build Context | Dockerfile |
|------|---------------|------------|
| gateway | `..` (项目根) | `apps/openmemory_gateway/gateway/Dockerfile` |
| worker | `..` (项目根) | `apps/openmemory_gateway/gateway/Dockerfile` |

---

## 完整复制清单（修订版）

基于上述依赖分析，以下为经过验证的完整复制清单。

### 使用自动化脚本（推荐）

推荐使用 `import_copy.py` 自动完成复制，脚本会读取 manifest 并验证结果：

```bash
# Logbook-only（轻量级事实账本）
python "$ENGRAM_SRC/scripts/import_copy.py" --src "$ENGRAM_SRC" --dst "$TARGET_PROJECT" --mode logbook-only

# Logbook-only（含可选文件，如 CLI 脚本）
python "$ENGRAM_SRC/scripts/import_copy.py" --src "$ENGRAM_SRC" --dst "$TARGET_PROJECT" --mode logbook-only --include-optional

# Unified Stack（必需文件，默认模式）
python "$ENGRAM_SRC/scripts/import_copy.py" --src "$ENGRAM_SRC" --dst "$TARGET_PROJECT"

# Unified Stack（显式指定模式）
python "$ENGRAM_SRC/scripts/import_copy.py" --src "$ENGRAM_SRC" --dst "$TARGET_PROJECT" --mode unified

# Unified Stack（含可选文件，如 SeekDB、Gateway 模板）
python "$ENGRAM_SRC/scripts/import_copy.py" --src "$ENGRAM_SRC" --dst "$TARGET_PROJECT" --include-optional

# 干运行预览
python "$ENGRAM_SRC/scripts/import_copy.py" --src "$ENGRAM_SRC" --dst "$TARGET_PROJECT" --dry-run
```

### Logbook-only 模式（手动）

<details>
<summary>展开手动复制命令</summary>

```bash
cd "$TARGET_PROJECT"

# ============================================================
# 必需文件（全部）
# ============================================================

# 1. Compose 配置
mkdir -p compose
cp "$ENGRAM_SRC/compose/logbook.yml" compose/

# 2. Logbook SQL 初始化脚本（整个目录）
mkdir -p apps/logbook_postgres
cp -r "$ENGRAM_SRC/apps/logbook_postgres/sql" apps/logbook_postgres/

# ============================================================
# 可选文件
# ============================================================

# [可选] Logbook CLI & 迁移脚本
# cp -r "$ENGRAM_SRC/apps/logbook_postgres/scripts" apps/logbook_postgres/
```

</details>

### Unified Stack 模式（手动）

<details>
<summary>展开手动复制命令</summary>

```bash
cd "$TARGET_PROJECT"

# ============================================================
# 必需文件（核心服务）
# ============================================================

# 1. 统一栈 Compose 配置
cp "$ENGRAM_SRC/docker-compose.unified.yml" docker-compose.engram.yml

# 2. Logbook SQL 初始化脚本
mkdir -p apps/logbook_postgres
cp -r "$ENGRAM_SRC/apps/logbook_postgres/sql" apps/logbook_postgres/

# 3. Logbook 迁移脚本（含 engram_logbook 包，Gateway 构建依赖）
cp -r "$ENGRAM_SRC/apps/logbook_postgres/scripts" apps/logbook_postgres/

# 4. Gateway 服务代码（整个 gateway 目录）
mkdir -p apps/openmemory_gateway
cp -r "$ENGRAM_SRC/apps/openmemory_gateway/gateway" apps/openmemory_gateway/

# 5. OpenMemory 源码
mkdir -p libs
cp -r "$ENGRAM_SRC/libs/OpenMemory" libs/

# ============================================================
# 可选文件（按 Profile 需求）
# ============================================================

# [可选] 分步部署 Compose 配置
mkdir -p compose
cp "$ENGRAM_SRC/compose/logbook.yml" compose/
cp "$ENGRAM_SRC/compose/openmemory.yml" compose/
cp "$ENGRAM_SRC/compose/gateway.yml" compose/

# [可选] SeekDB RAG 索引（仅启用 SeekDB 时需要）
# 启用 SeekDB: docker compose -f docker-compose.engram.yml -f docker-compose.engram.seekdb.yml up -d
mkdir -p apps/seekdb_rag_hybrid
cp -r "$ENGRAM_SRC/apps/seekdb_rag_hybrid/sql" apps/seekdb_rag_hybrid/
cp "$ENGRAM_SRC/docker-compose.unified.seekdb.yml" docker-compose.engram.seekdb.yml

# [可选] MinIO profile 依赖
# 如需启用 --profile minio，以下文件必需：
cp -r "$ENGRAM_SRC/apps/logbook_postgres/templates" apps/logbook_postgres/
# 注：scripts/ops/ 已包含在 scripts 目录中

# [可选] Gateway 模板文件（.mcp.json 示例）
cp -r "$ENGRAM_SRC/apps/openmemory_gateway/templates" apps/openmemory_gateway/
```

</details>

---

## 风险与缺口说明

| 风险类型 | 描述 | 缓解措施 |
|----------|------|----------|
| **Gateway 构建失败** | Dockerfile COPY 需要项目根目录作为 context | 确保 `apps/logbook_postgres/scripts` 和 `apps/openmemory_gateway/gateway` 完整复制 |
| **SeekDB 迁移失败** | 启用 SeekDB 但缺少 SQL 文件或 override compose | 复制 `apps/seekdb_rag_hybrid/sql/` 和 `docker-compose.unified.seekdb.yml`，或禁用 SeekDB |
| **MinIO 初始化失败** | minio profile 需要 ops 脚本和 templates | 启用 minio profile 前确保复制 `scripts/ops/` 和 `templates/` |
| **权限验证失败** | 缺少 `99_verify_permissions.sql` | 确保完整复制 `apps/logbook_postgres/sql/` 目录 |
| **路径不匹配** | 复制后相对路径可能失效 | 参考"路径修正指南"调整 Compose 文件 |

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [导入路径约束参考](./import_path_constraints.md) | 路径约束、失败症状与修复方式 |
| [`unified_stack_import_v1.json`](./manifests/unified_stack_import_v1.json) | Unified Stack 机器可读 Manifest |
| [`scripts/import_copy.py`](../../scripts/import_copy.py) | 自动化导入复制工具 |
| [`scripts/import_preflight.py`](../../scripts/import_preflight.py) | 导入预检验证工具 |
| [版本控制契约](../contracts/versioning.md) | Schema 与 Manifest 版本策略 |
| [环境变量参考](../reference/environment_variables.md) | 完整的环境变量列表 |
| [Logbook 概述](../logbook/00_overview.md) | Logbook 架构与设计 |
| [Gateway 概述](../gateway/00_overview.md) | Gateway MCP 集成 |
| [SeekDB 概述](../seekdb/00_overview.md) | SeekDB RAG 索引 |
