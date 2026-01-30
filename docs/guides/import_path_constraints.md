# 导入路径约束参考

本文档详细列出 Engram 项目导入时的所有路径约束、失败症状、约束来源及修复方式。

> **原则**：优先使用 override compose 覆盖，而非修改深层代码（Dockerfile/脚本）。

---

## 目录

- [约束总览](#约束总览)
- [Gateway build.context 约束](#gateway-buildcontext-约束)
- [Volume Bind Mount 约束](#volume-bind-mount-约束)
- [OpenMemory Build Context 约束](#openmemory-build-context-约束)
- [SQL 初始化脚本路径约束](#sql-初始化脚本路径约束)
- [SeekDB Override Compose 约束](#seekdb-override-compose-约束)
- [MinIO Profile 路径约束](#minio-profile-路径约束)
- [修复方式总结](#修复方式总结)
- [预检脚本](#预检脚本)

---

## 约束总览

| 约束类型 | 失败症状 | 来源文件 | 修复难度 |
|----------|----------|----------|----------|
| Gateway build.context | `COPY failed: file not found` | Dockerfile | 高 |
| Volume bind mount | `source path does not exist` | Compose | 低 |
| OpenMemory build context | `failed to solve: dockerfile parse error` | Compose | 低 |
| SQL 初始化脚本 | 数据库启动后缺少表/角色 | Compose | 低 |
| SeekDB SQL 文件 | `bootstrap_roles` 服务找不到 `/sql/06_*.sql` | Override Compose | 低 |
| MinIO ops/templates | `minio_init` 初始化失败 | Compose | 低 |

---

## Gateway build.context 约束

### 约束说明

Gateway 的 `build.context` **必须指向目标项目根目录**。这是由 Dockerfile 中的 COPY 指令决定的硬性约束。

### 来源文件

**主文件**：`apps/openmemory_gateway/gateway/Dockerfile`

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

**Compose 引用**：

| 文件 | 服务 | context 值 |
|------|------|------------|
| `docker-compose.unified.yml` | gateway, worker | `.`（项目根） |
| `compose/gateway.yml` | gateway, worker | `..`（相对于 compose/） |

### 失败症状

```
------
 > [gateway 4/9] COPY apps/logbook_postgres/scripts /logbook_scripts:
------
ERROR: failed to solve: failed to compute cache key: failed to calculate checksum of ref: 
  "/apps/logbook_postgres/scripts": not found
```

或：

```
COPY failed: file not found in build context or excluded by .dockerignore: 
  stat apps/openmemory_gateway/gateway/requirements.runtime.txt: file does not exist
```

### 根因分析

| 原因 | 说明 |
|------|------|
| context 设置错误 | `build.context` 不是项目根目录 |
| 缺少跨目录依赖 | `apps/logbook_postgres/scripts` 未复制 |
| .dockerignore 排除 | 路径被 `.dockerignore` 意外排除 |

### 修复方式

**方式 1：确保 context 正确（推荐）**

```yaml
# docker-compose.engram.yml（在项目根目录）
services:
  gateway:
    build:
      context: .  # 必须是项目根目录
      dockerfile: apps/openmemory_gateway/gateway/Dockerfile
```

**方式 2：使用 override compose 覆盖 context**

如果项目结构特殊，可创建 `docker-compose.override.yml`：

```yaml
# docker-compose.override.yml
services:
  gateway:
    build:
      context: /absolute/path/to/project/root
```

**方式 3：修改 Dockerfile（不推荐）**

如果必须修改目录结构，需要同步修改：
1. `apps/openmemory_gateway/gateway/Dockerfile` - 所有 COPY 路径
2. `docker-compose.unified.yml` - gateway/worker 服务的 build 配置
3. `compose/gateway.yml` - gateway/worker 服务的 build 配置

### 验证方法

```bash
# 验证构建
docker compose -f docker-compose.engram.yml build gateway

# 或手动构建
docker build -f apps/openmemory_gateway/gateway/Dockerfile -t gateway-test .
```

---

## Volume Bind Mount 约束

### 约束说明

多个服务通过 volume bind mount 挂载本地目录，源路径必须存在。

### 来源文件

**主文件**：`docker-compose.unified.yml`

### 完整路径清单

| 服务 | 源路径 | 容器目标 | 必需性 |
|------|--------|----------|--------|
| postgres | `./apps/logbook_postgres/sql` | `/docker-entrypoint-initdb.d:ro` | **必需** |
| bootstrap_roles | `./apps/logbook_postgres/sql/03_pgvector_extension.sql` | `/sql/03_pgvector_extension.sql:ro` | **必需** |
| bootstrap_roles | `./apps/logbook_postgres/sql/04_roles_and_grants.sql` | `/sql/04_roles_and_grants.sql:ro` | **必需** |
| bootstrap_roles | `./apps/logbook_postgres/sql/05_openmemory_roles_and_grants.sql` | `/sql/05_openmemory_roles_and_grants.sql:ro` | **必需** |
| bootstrap_roles | `./apps/logbook_postgres/sql/99_verify_permissions.sql` | `/sql/99_verify_permissions.sql:ro` | **必需** |
| logbook_migrate | `./apps/logbook_postgres` | `/app:ro` | **必需** |
| permissions_verify | `./apps/logbook_postgres/sql/99_verify_permissions.sql` | `/verify.sql:ro` | **必需** |
| logbook_tools | `./apps/logbook_postgres/scripts` | `/app/scripts:ro` | tools profile |
| scm_scheduler | `./apps/logbook_postgres/scripts` | `/app/scripts:ro` | scm_sync profile |
| scm_worker | `./apps/logbook_postgres/scripts` | `/app/scripts:ro` | scm_sync profile |
| scm_reaper | `./apps/logbook_postgres/scripts` | `/app/scripts:ro` | scm_sync profile |
| logbook_test | `./apps/logbook_postgres` | `/app:ro` | test profile |
| minio_init | `./apps/logbook_postgres/scripts/ops` | `/ops:ro` | minio profile |
| minio_init | `./apps/logbook_postgres/templates` | `/templates:ro` | minio profile |

### 失败症状

```
Error response from daemon: invalid mount config for type "bind": 
  bind source path does not exist: /path/to/project/apps/logbook_postgres/sql
```

或：

```
service "postgres" depends on service "bootstrap_roles" but "bootstrap_roles" 
  has an error: source path does not exist
```

### 修复方式

**方式 1：复制缺失目录（推荐）**

```bash
# 复制 Logbook SQL
mkdir -p apps/logbook_postgres
cp -r "$ENGRAM_SRC/apps/logbook_postgres/sql" apps/logbook_postgres/

# 复制 Logbook scripts（如需 migrate/tools/scm_sync）
cp -r "$ENGRAM_SRC/apps/logbook_postgres/scripts" apps/logbook_postgres/
```

**方式 2：使用 override compose 覆盖路径**

```yaml
# docker-compose.override.yml
services:
  postgres:
    volumes:
      - /custom/path/to/sql:/docker-entrypoint-initdb.d:ro
```

**方式 3：禁用不需要的 profile**

如果某些 profile 不需要，无需复制其依赖文件：

```bash
# 仅核心服务，无需 tools/scm_sync/minio profile
docker compose -f docker-compose.engram.yml up -d
```

---

## OpenMemory Build Context 约束

### 约束说明

OpenMemory 服务的 build context 必须指向包含 Dockerfile 的目录。

### 来源文件

**主文件**：`docker-compose.unified.yml`

```yaml
services:
  openmemory_migrate:
    build:
      context: ./libs/OpenMemory/packages/openmemory-js
      dockerfile: Dockerfile
      target: builder
      
  openmemory:
    build:
      context: ./libs/OpenMemory/packages/openmemory-js
      dockerfile: Dockerfile
```

**分步部署**：`compose/openmemory.yml`

```yaml
services:
  openmemory:
    build:
      context: ../libs/OpenMemory/packages/openmemory-js
      dockerfile: Dockerfile
```

### 失败症状

```
failed to solve: dockerfile parse error: file does not exist: 
  /path/to/project/libs/OpenMemory/packages/openmemory-js/Dockerfile
```

或：

```
ERROR: failed to solve: failed to compute cache key: 
  "/libs/OpenMemory/packages/openmemory-js" not found
```

### 修复方式

**方式 1：复制 OpenMemory 源码（推荐）**

```bash
mkdir -p libs
cp -r "$ENGRAM_SRC/libs/OpenMemory" libs/
```

**方式 2：使用 override compose 指向自定义路径**

```yaml
# docker-compose.override.yml
services:
  openmemory:
    build:
      context: /custom/path/to/OpenMemory/packages/openmemory-js
```

**方式 3：使用预构建镜像（跳过构建）**

```yaml
# docker-compose.override.yml
services:
  openmemory:
    image: your-registry/openmemory:latest
    build: !reset null
```

---

## SQL 初始化脚本路径约束

### 约束说明

PostgreSQL 容器在首次启动时执行 `/docker-entrypoint-initdb.d/` 中的脚本。

### 来源文件

**主文件**：`docker-compose.unified.yml`

```yaml
services:
  postgres:
    volumes:
      - ./apps/logbook_postgres/sql:/docker-entrypoint-initdb.d:ro
```

### 必需脚本清单

| 脚本文件 | 用途 |
|----------|------|
| `00_init_service_accounts.sh` | 创建服务账号 |
| `01_logbook_schema.sql` | Logbook 核心表 |
| `02_logbook_indexes.sql` | Logbook 索引 |
| `03_pgvector_extension.sql` | pgvector 扩展 |
| `04_roles_and_grants.sql` | Engram 角色权限 |
| `05_openmemory_roles_and_grants.sql` | OpenMemory 角色权限 |
| `99_verify_permissions.sql` | 权限验证脚本 |

### 失败症状

- 数据库启动成功，但缺少表
- `relation "xxx" does not exist` 错误
- 服务账号不存在：`role "logbook_migrator" does not exist`

### 修复方式

**方式 1：复制完整 SQL 目录（推荐）**

```bash
mkdir -p apps/logbook_postgres
cp -r "$ENGRAM_SRC/apps/logbook_postgres/sql" apps/logbook_postgres/
```

**方式 2：手动执行迁移（已有数据库）**

```bash
# 如果数据库已存在但缺少表，使用 migrate 服务
docker compose -f docker-compose.engram.yml up logbook_migrate
```

---

## SeekDB Override Compose 约束

### 约束说明

SeekDB 通过 override compose 文件启用，需要额外的 SQL 文件和 compose 配置。

### 来源文件

**Override Compose**：`docker-compose.unified.seekdb.yml`

```yaml
services:
  postgres:
    volumes:
      - ./apps/seekdb_rag_hybrid/sql/01_seekdb_roles_and_grants.sql:/sql/06_seekdb_roles_and_grants.sql:ro
      - ./apps/seekdb_rag_hybrid/sql/02_seekdb_index.sql:/sql/09_seekdb_index.sql:ro
  
  bootstrap_roles:
    volumes:
      - ./apps/seekdb_rag_hybrid/sql/01_seekdb_roles_and_grants.sql:/sql/06_seekdb_roles_and_grants.sql:ro
```

### 失败症状

启用 SeekDB 时：

```
ERROR: source path does not exist: 
  /path/to/project/apps/seekdb_rag_hybrid/sql/01_seekdb_roles_and_grants.sql
```

或：

```
[WARN] SEEKDB_ENABLE=1 but /sql/06_seekdb_roles_and_grants.sql does not exist
```

### 修复方式

**方式 1：禁用 SeekDB（推荐，如不需要）**

```bash
# 不使用 seekdb override compose，无需复制任何 SeekDB 文件
docker compose -f docker-compose.engram.yml up -d
```

**方式 2：复制 SeekDB 文件并启用**

```bash
# 复制 SQL 文件
mkdir -p apps/seekdb_rag_hybrid/sql
cp "$ENGRAM_SRC/apps/seekdb_rag_hybrid/sql/01_seekdb_roles_and_grants.sql" apps/seekdb_rag_hybrid/sql/
cp "$ENGRAM_SRC/apps/seekdb_rag_hybrid/sql/02_seekdb_index.sql" apps/seekdb_rag_hybrid/sql/

# 复制 override compose
cp "$ENGRAM_SRC/docker-compose.unified.seekdb.yml" docker-compose.engram.seekdb.yml

# 启动时叠加 override
docker compose -f docker-compose.engram.yml -f docker-compose.engram.seekdb.yml up -d
```

---

## MinIO Profile 路径约束

### 约束说明

启用 minio profile 时，`minio_init` 服务需要 ops 脚本和模板文件。

### 来源文件

**主文件**：`docker-compose.unified.yml`

```yaml
services:
  minio_init:
    profiles:
      - minio
    volumes:
      - ./apps/logbook_postgres/scripts/ops:/ops:ro
      - ./apps/logbook_postgres/templates:/templates:ro
```

### 失败症状

```
ERROR: source path does not exist: 
  /path/to/project/apps/logbook_postgres/scripts/ops
```

或 minio_init 初始化时：

```
/bin/sh: /ops/generate_policy.sh: No such file or directory
```

### 修复方式

**方式 1：不启用 minio profile（推荐，如不需要）**

```bash
# 不使用 --profile minio
docker compose -f docker-compose.engram.yml up -d
```

**方式 2：复制必需文件**

```bash
# scripts 目录（含 ops/）
cp -r "$ENGRAM_SRC/apps/logbook_postgres/scripts" apps/logbook_postgres/

# templates 目录
cp -r "$ENGRAM_SRC/apps/logbook_postgres/templates" apps/logbook_postgres/

# 启动时启用 minio profile
docker compose -f docker-compose.engram.yml --profile minio up -d
```

---

## 修复方式总结

| 优先级 | 方式 | 适用场景 | 优点 | 缺点 |
|--------|------|----------|------|------|
| 1 | 复制缺失文件 | 路径存在但文件缺失 | 简单直接 | 需要维护同步 |
| 2 | Override Compose | 需要自定义路径 | 不修改原文件 | 需要额外配置文件 |
| 3 | 禁用不需要的 Profile | 功能不需要 | 减少依赖 | 功能受限 |
| 4 | 修改 Dockerfile | 目录结构大改 | 灵活性高 | 维护成本高，需同步多文件 |

### Override Compose 示例

```yaml
# docker-compose.override.yml
# 自动被 docker compose 加载

services:
  # 覆盖 Gateway 构建上下文
  gateway:
    build:
      context: /custom/path/to/root

  # 覆盖 PostgreSQL 初始化脚本路径
  postgres:
    volumes:
      - /custom/path/to/sql:/docker-entrypoint-initdb.d:ro

  # 使用预构建镜像替代本地构建
  openmemory:
    image: your-registry/openmemory:v1.0
    build: !reset null
```

---

## 预检脚本

使用预检脚本在部署前验证所有路径约束。

### 运行预检

```bash
# 基本检查
python scripts/import_preflight.py .

# 详细模式
python scripts/import_preflight.py . --verbose

# JSON 输出（CI 集成）
python scripts/import_preflight.py . --json

# 检查特定 manifest
python scripts/import_preflight.py . --manifest docs/guides/manifests/unified_stack_import_v1.json
```

### 预检内容

| 检查项 | 验证内容 |
|--------|----------|
| Compose 文件 | 所有 `docker-compose*.yml` 文件存在 |
| Build Context | 所有 `build.context` 目录存在 |
| Volume 源路径 | 所有 volume bind mount 源路径存在 |
| Dockerfile 模式 | 检查 `COPY ..` 等不当模式 |
| .dockerignore | 检查是否排除了必需路径 |

### 预检输出示例

**通过**：

```
==================================================
Engram 项目导入预检
==================================================

项目路径: /path/to/your-project
Compose 文件: 1

[OK] 找到 1 个 Compose 文件
[OK] 所有 4 个 build context 路径有效
[OK] 所有 12 个 volume 源路径有效
[OK] 检查了 1 个 Dockerfile，未发现问题
[OK] .dockerignore 配置正确

==================================================
[OK] 预检通过
==================================================
```

**失败**：

```
==================================================
Engram 项目导入预检
==================================================

[FAIL] build context 不存在: libs/OpenMemory/packages/openmemory-js
  来源: docker-compose.engram.yml (openmemory 服务)
  修复: cp -r "$ENGRAM_SRC/libs/OpenMemory" libs/

[FAIL] volume 源路径不存在: apps/logbook_postgres/sql
  来源: docker-compose.engram.yml (postgres 服务)
  修复: cp -r "$ENGRAM_SRC/apps/logbook_postgres/sql" apps/logbook_postgres/

==================================================
[FAIL] 预检失败：2 个错误
==================================================
```

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [集成指南](./integrate_existing_project.md) | 完整的项目导入流程 |
| [统一栈 Manifest](./manifests/unified_stack_import_v1.json) | 机器可读的文件清单 |
| [Gateway Dockerfile](../../apps/openmemory_gateway/gateway/Dockerfile) | Gateway 构建配置源码 |
| [docker-compose.unified.yml](../../docker-compose.unified.yml) | 统一栈 Compose 配置 |
