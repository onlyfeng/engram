# 在已有项目中引入 Engram

本文档说明如何将 Engram 集成到已有项目中，提供两种部署模式的完整复制清单。

> 更新说明：统一栈已切换为 `docker/engram.Dockerfile` + `OPENMEMORY_IMAGE` 方案，本文档以当前结构为准。

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
mkdir -p sql
cp -r "$ENGRAM_SRC/sql" ./

# ============================================================
# 可选文件（按需选择）
# ============================================================

# [可选] Logbook CLI & 迁移脚本
# 用途: db_migrate.py、logbook_cli_main.py 等 CLI 工具
# 如需运行 Logbook CLI 或数据库迁移脚本，请复制：
# mkdir -p logbook_postgres
# cp -r "$ENGRAM_SRC/logbook_postgres/scripts" logbook_postgres/
```

</details>

### 2. 目录结构

```
your-project/
├── .env                           # 环境变量配置
├── compose/
│   └── logbook.yml                # Logbook Compose 配置
├── sql/                           # [必需] Logbook SQL 初始化脚本
│   ├── 01_logbook_schema.sql
│   ├── 02_scm_migration.sql
│   └── ...
└── logbook_postgres/
    └── scripts/                   # [可选] Logbook CLI & 迁移脚本
        ├── logbook_cli_main.py
        ├── db_migrate.py
        └── ...
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
cd logbook_postgres/scripts && pip install -e .
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

---

## Unified stack（统一栈）

适用于需要完整记忆系统（事实账本 + 语义搜索 + MCP 集成）的场景。

### Compose 文件角色

| 源文件 | 目标文件 | 角色 | 说明 |
|--------|----------|------|------|
| `docker-compose.unified.yml` | `docker-compose.engram.yml` | **主入口** | 完整统一栈部署 |
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

# 包含可选文件（Gateway 模板等）
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

# 1.2 Dockerfile（Gateway/Worker/OpenMemory）
cp -r "$ENGRAM_SRC/docker" docker/

# 1.3 核心源码与迁移脚本
cp -r "$ENGRAM_SRC/src" src/
cp -r "$ENGRAM_SRC/sql" sql/
cp -r "$ENGRAM_SRC/logbook_postgres" logbook_postgres/
cp -r "$ENGRAM_SRC/engram_logbook" engram_logbook/
cp "$ENGRAM_SRC/db_bootstrap.py" ./
cp "$ENGRAM_SRC/pyproject.toml" ./
cp "$ENGRAM_SRC/requirements.txt" ./
cp "$ENGRAM_SRC/README.md" ./

# ============================================================
# 可选文件（按需选择）
# ============================================================

# [可选] .env 示例与 MCP 配置模板
cp "$ENGRAM_SRC/.env.example" ./
mkdir -p configs/mcp
cp "$ENGRAM_SRC/configs/mcp/.mcp.json.example" configs/mcp/
```

</details>

### 2. 目录结构

```
your-project/
├── .env                              # 环境变量配置
├── .env.example                      # [可选] 环境变量模板
├── docker-compose.engram.yml         # 统一栈主配置
├── docker/                           # Dockerfile（Gateway/Worker/OpenMemory 透传）
├── src/                              # Engram 核心源码
├── sql/                              # Logbook SQL 迁移脚本
├── logbook_postgres/                 # 迁移与工具脚本
├── engram_logbook/                   # 兼容包
├── db_bootstrap.py                   # 服务账号初始化
├── pyproject.toml
├── requirements.txt
├── README.md
└── configs/
    └── mcp/
        └── .mcp.json.example         # [可选] MCP 配置模板
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

# HTTP 验证（统一栈验证）
# 验证服务是否正常响应（需要先启动服务）
make verify-unified
```

**一键验证脚本**（推荐）：

```bash
# 复制验证脚本到本地（一次性）
mkdir -p scripts
cp "$ENGRAM_SRC/scripts/import_preflight.py" scripts/
cp "$ENGRAM_SRC/scripts/verify_build_boundaries.sh" scripts/

# 串联执行三阶段验证（预检→构建→服务）
python scripts/import_preflight.py . --verbose && \
bash scripts/verify_build_boundaries.sh --dry-run && \
echo "[INFO] 启动服务后运行: make verify-unified"
```

**各步骤说明**：

| 步骤 | 脚本 | 检查内容 | 依赖 |
|------|------|----------|------|
| 预检 | `import_preflight.py` | Compose 文件、build context、volume 路径 | Python |
| 构建边界检查 | `verify_build_boundaries.sh --dry-run` | Dockerfile 模式、.dockerignore、context 配置 | Bash |
| HTTP 验证 | `make verify-unified` | HTTP 健康检查、MCP 工具调用 | `jq`, `curl` |

**验证入口参数约定**：

| Makefile 调用 | 环境变量 | 说明 |
|---------------|----------|------|
| `make verify-unified` | `VERIFY_MODE` | 默认模式（依赖 Docker） |
| `VERIFY_FULL=1 make verify-unified` | `VERIFY_FULL` | 完整验证（含降级测试） |
| `VERIFY_JSON_OUT=path make verify-unified` | `JSON_OUT_PATH` | JSON 输出路径 |

```bash
make verify-unified
VERIFY_FULL=1 VERIFY_JSON_OUT=.artifacts/verify.json make verify-unified
```

**预检内容**：
- 检查 docker-compose.*.yml 文件（包括 docker-compose.engram.yml）
- 验证所有 `build.context` 目录存在（`docker/`、`src/`、`sql/` 等）
- 验证所有 volume bind mount 路径存在
- 检查 Dockerfile 模式和 .dockerignore 配置

**构建边界检查**：
- 验证 Compose context 配置正确性
- 检查 Dockerfile 中的 COPY 模式（如 `COPY ..` 危险模式）
- 确认 .dockerignore 排除了大文件/目录

**常见预检错误**：

| 错误 | 原因 | 修复 |
|------|------|------|
| `build context 不存在: src` | 核心源码未复制 | `cp -r "$ENGRAM_SRC/src" src/` |
| `volume 源路径不存在: sql` | SQL 脚本未复制 | `cp -r "$ENGRAM_SRC/sql" sql/` |
| `jq: command not found` | 缺少 jq 工具 | `brew install jq` / `apt install jq` |

### 5. 启动

```bash
cd "$TARGET_PROJECT"

# ============================================================
# 启动统一栈
# ============================================================
docker compose -f docker-compose.engram.yml up -d

# ============================================================
# 使用 project name 隔离（多项目部署）
# ============================================================
COMPOSE_PROJECT_NAME=${PROJECT_KEY:-myproject} \
  docker compose -f docker-compose.engram.yml up -d

# ============================================================
# 可选 Profile 启动
# ============================================================

# 启用 Dashboard（metabase/pgadmin）
docker compose -f docker-compose.engram.yml --profile dashboard up -d

# 启用 MinIO（对象存储，开发/CI 使用）
docker compose -f docker-compose.engram.yml --profile minio up -d

# 启用 SCM 同步
docker compose -f docker-compose.engram.yml --profile scm_sync up -d

# 组合多个 profile
docker compose -f docker-compose.engram.yml \
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
pip install -e .
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
| **Logbook CLI** | 数据库迁移、健康检查、调试 | `pip install -e .` | `logbook_postgres/` |
| **Dashboard** | metabase/pgadmin | `--profile dashboard` | 无额外文件 |
| **MinIO** | 对象存储（开发/CI） | `--profile minio` | 无额外文件 |
| **SCM Sync** | SCM 增量同步 | `--profile scm_sync` | `logbook_postgres/scripts/`（已包含在核心依赖） |

---

## 复制档位

统一栈不再 vendoring OpenMemory，默认通过 `OPENMEMORY_IMAGE` 直接使用上游镜像。

### 版本锁定建议

在 `.env` 中指定具体 tag 或 digest 以锁定版本：

```bash
OPENMEMORY_IMAGE=ghcr.io/caviraoss/openmemory:v1.2.3
# 或
OPENMEMORY_IMAGE=ghcr.io/caviraoss/openmemory@sha256:...
```

---

## 路径修正指南

复制文件后，需要修正 Compose 文件中的相对路径。以下是需要检查的路径。

> **完整约束清单**：如需了解每个约束的失败症状、根因分析和修复方式，请参阅 [导入路径约束参考](./import_path_constraints.md)。

### docker-compose.engram.yml（统一栈）

```yaml
# 确认以下路径相对于 docker-compose.engram.yml 正确
volumes:
  - ./sql:/docker-entrypoint-initdb.d:ro

build:
  context: .  # Gateway/Worker 构建需要项目根目录
  dockerfile: docker/engram.Dockerfile
```

### Gateway build.context 约束（重要）

**Gateway 的 `build.context` 必须指向目标项目根目录**。这是由 Dockerfile 中的 COPY 指令决定的硬性约束。

**Dockerfile COPY 依赖**（参见 `docker/engram.Dockerfile`）：

```dockerfile
COPY pyproject.toml requirements.txt README.md ./
COPY src ./src
COPY sql ./sql
COPY logbook_postgres ./logbook_postgres
COPY engram_logbook ./engram_logbook
COPY db_bootstrap.py ./
```

**关键约束**：
- 所有 COPY 路径都是相对于 `build.context` 的
- `logbook_postgres/scripts` 是 Gateway 构建的**跨目录依赖**
- 如果 `build.context` 不是项目根目录，Dockerfile 将无法找到这些路径

**Compose 配置要求**：

| Compose 文件 | build.context | 说明 |
|--------------|---------------|------|
| `docker-compose.engram.yml` | `.` | 项目根目录 |

**允许的重构方式**：

如需改变目录结构（例如重命名 `apps/` 或移动 `logbook_postgres/`），**必须同步修改以下文件**：

1. `docker/engram.Dockerfile` - 更新所有 COPY 路径
2. `docker-compose.unified.yml` - 更新 `gateway` 与 `worker` 服务的 build 配置

**风险警告**：

| 风险 | 后果 | 预防措施 |
|------|------|----------|
| 修改目录结构未更新 Dockerfile | 构建失败：`COPY failed: file not found` | 修改前运行 `docker build` 验证 |
| build.context 设置错误 | 构建失败或镜像缺少依赖 | 使用预检脚本验证路径 |
| Dockerfile 路径与实际结构不一致 | CI/CD 构建失败 | 在 PR 中包含 Dockerfile 修改的完整测试 |

### compose/logbook.yml（Logbook-only）

```yaml
# 确认以下路径相对于 compose/logbook.yml 正确
volumes:
  - ../sql:/docker-entrypoint-initdb.d:ro
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
ls -la sql/
ls -la src/
ls -la docker/engram.Dockerfile
```

### Q: 如何只复制最小必需文件？

**Logbook-only**:
- `compose/logbook.yml`
- `sql/`

**Unified stack**:
- `docker-compose.unified.yml`
- `docker/`
- `src/`
- `sql/`
- `logbook_postgres/`
- `engram_logbook/`
- `db_bootstrap.py`
- `pyproject.toml`
- `requirements.txt`

### Q: 服务账号密码是什么？

统一栈使用最小权限角色分离：
- `logbook_migrator`: Logbook DDL 迁移账号
- `logbook_svc`: Logbook 运行时 DML 账号
- `openmemory_migrator_login`: OpenMemory DDL 迁移账号
- `openmemory_svc`: OpenMemory 运行时 DML 账号

这些角色可通过 `logbook_postgres/scripts/db_bootstrap.py` 自动创建。

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

---

## 依赖路径完整映射

本节提供 Docker Compose 和 Dockerfile 中所有路径依赖的完整对照表。

### docker-compose.unified.yml 依赖分析

#### Volume Bind Mounts

**docker-compose.unified.yml（核心）**:

| 服务 | 源路径 | 容器目标 | 必需性 |
|------|--------|----------|--------|
| postgres | `./sql` | `/docker-entrypoint-initdb.d:ro` | **必需** |
| 其余服务 | （无 volume bind） | — | — |

#### Build Contexts

| 服务 | Context | Dockerfile | 必需性 |
|------|---------|------------|--------|
| openmemory_migrate | `.` (项目根) | `docker/openmemory.Dockerfile` | **必需** |
| openmemory | `.` (项目根) | `docker/openmemory.Dockerfile` | **必需** |
| gateway | `.` (项目根) | `docker/engram.Dockerfile` | **必需** |
| worker | `.` (项目根) | `docker/engram.Dockerfile` | **必需** |
| dashboard | （image 方式） | — | dashboard profile 启用时可选 |

### Gateway Dockerfile COPY 依赖

Gateway 和 Worker 服务的 Dockerfile 从项目根目录 COPY 以下路径：

| 源路径 | 容器目标 | 用途 |
|--------|----------|------|
| `pyproject.toml` | `/app/` | 包配置 |
| `requirements.txt` | `/app/` | 依赖清单 |
| `README.md` | `/app/` | 包元数据 |
| `src/` | `/app/src/` | Gateway/Logbook 核心代码 |
| `sql/` | `/app/sql/` | 迁移脚本 |
| `logbook_postgres/` | `/app/logbook_postgres/` | 迁移与工具脚本 |
| `engram_logbook/` | `/app/engram_logbook/` | 兼容包 |
| `db_bootstrap.py` | `/app/db_bootstrap.py` | 服务账号初始化 |

### 分步部署 Compose 文件依赖

分步 compose 仅提供 Logbook-only（`compose/logbook.yml`），其余组件统一使用 `docker-compose.engram.yml`。

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

# Unified Stack（含可选文件，如 Gateway 模板）
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
mkdir -p sql
cp -r "$ENGRAM_SRC/sql" ./

# ============================================================
# 可选文件
# ============================================================

# [可选] Logbook CLI & 迁移脚本
# mkdir -p logbook_postgres
# cp -r "$ENGRAM_SRC/logbook_postgres/scripts" logbook_postgres/
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

# 2. Dockerfile（Gateway/Worker/OpenMemory）
cp -r "$ENGRAM_SRC/docker" docker/

# 3. 核心源码与迁移脚本
cp -r "$ENGRAM_SRC/src" src/
cp -r "$ENGRAM_SRC/sql" sql/
cp -r "$ENGRAM_SRC/logbook_postgres" logbook_postgres/
cp -r "$ENGRAM_SRC/engram_logbook" engram_logbook/
cp "$ENGRAM_SRC/db_bootstrap.py" ./
cp "$ENGRAM_SRC/pyproject.toml" ./
cp "$ENGRAM_SRC/requirements.txt" ./
cp "$ENGRAM_SRC/README.md" ./

# ============================================================
# 可选文件（按 Profile 需求）
# ============================================================

# [可选] .env 示例与 MCP 配置模板
cp "$ENGRAM_SRC/.env.example" ./
mkdir -p configs/mcp
cp "$ENGRAM_SRC/configs/mcp/.mcp.json.example" configs/mcp/
```

</details>

---

## 风险与缺口说明

| 风险类型 | 描述 | 缓解措施 |
|----------|------|----------|
| **Gateway 构建失败** | Dockerfile COPY 需要项目根目录作为 context | 确保 `docker/`、`src/`、`sql/`、`logbook_postgres/` 已完整复制 |
| **MinIO 初始化失败** | minio profile 依赖环境变量配置 | 检查 `.env` 中 `MINIO_*` 变量 |
| **权限验证失败** | 缺少 `99_verify_permissions.sql` | 确保完整复制 `sql/` 目录 |
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
