# Engram 命名约束文档

> 本文档定义 Engram 项目的官方组件命名、禁止词列表、以及迁移策略。所有代码、文档、日志、注释必须遵循此规范。

---

## 1. 官方组件命名

| 组件 | 官方名称 | 别名（可用） | 模块路径 | 说明 |
|------|----------|--------------|----------|------|
| **事实账本** | Logbook | - | `apps/logbook_postgres/` | 事实账本：事件日志、SCM 同步、证据链 |
| **记忆网关** | Gateway | Memory Gateway | `apps/openmemory_gateway/` | MCP 网关：策略校验、审计落库、降级处理 |
| **检索索引** | SeekDB | Seek Index | `apps/seekdb_rag_hybrid/` | RAG 检索索引：分块索引、混合检索 |

### 1.1 组件职责

| 组件 | 职责 | 核心 Schema |
|------|------|-------------|
| **Logbook** | 事实账本、事件追踪、SCM 同步、制品存储 | `identity`, `logbook`, `scm`, `analysis`, `governance` |
| **Gateway** | OpenMemory 集成、MCP 协议网关、记忆卡片 | `openmemory` |
| **SeekDB** | 向量索引、全文检索、RAG 检索 | `seekdb` |

#### 1.1.1 可选层与 Source of Truth (SoT) 约束

> **重要**：SeekDB 是可选的增强层，不影响 Logbook 作为 Source of Truth (SoT)。

| 层级 | 组件 | SoT 角色 | 可选性 |
|------|------|----------|--------|
| **SoT 层** | Logbook | 事实账本，所有事件的唯一真实来源 | 必需 |
| **SoT 层** | Gateway | 记忆卡片存储 | 必需（统一栈） |
| **可选增强层** | SeekDB | 向量/全文索引，可重建 | 可选（`SEEKDB_ENABLE=0` 可禁用） |

**约束规则**：
1. **可选层不阻塞 SoT**：SeekDB 索引失败不影响 Logbook 事件写入、Gateway 记忆卡片存储
2. **可选层可重建**：SeekDB 索引可从 Logbook/制品完全重建，丢失不影响数据完整性
3. **核心验证兼容**：`SEEKDB_ENABLE=0` 时，`verify-unified`、`acceptance-*` 等核心验证仍能通过

详见 [SeekDB 概览](../seekdb/00_overview.md)。

---

## 1.2 契约文档术语规范

在契约文档（`docs/contracts/`、`docs/gateway/`、`docs/logbook/`）中，组件名称的首次出现应使用完整名称，后续使用简称：

| 组件 | 首次出现写法 | 后续写法 | 历史对应（已废弃） |
|------|-------------|----------|---------------------|
| **记忆网关** | Memory Gateway | Gateway | [查看历史命名](./legacy_naming_governance.md) |
| **检索索引** | SeekDB | SeekDB 或 Seek | [查看历史命名](./legacy_naming_governance.md) |
| **事实账本** | Logbook | Logbook | [查看历史命名](./legacy_naming_governance.md) |

**示例**：
- 首次：`本文档定义 Memory Gateway（MCP 网关）与 Logbook（事实层）之间的...`
- 后续：`Gateway 负责策略校验...`

**边界与数据流段落标题统一**：
- 使用 `## Gateway ↔ Logbook 边界与数据流` 格式
- 避免使用 `Logbook/Gateway` 斜杠格式（易与路径混淆）

---

## 2. 禁止词列表

以下词汇模式**禁止**出现在代码、注释、日志、文档、标题中：

### 2.0.0 禁止模式（正则描述）

**禁止模式**：`(?i)step[1-3](?!\s)` — 匹配任意大小写的 `step` 后紧跟数字 `1`/`2`/`3`，且后面**没有空格**的情况。

| 禁止模式 | 匹配示例 | 替代词 | 说明 |
|----------|----------|--------|------|
| `(?i)step[1-3](?!\s)` | `stepN`、`StepN`、`STEPN`（N=1,2,3） | 官方组件名（`logbook`/`gateway`/`seekdb` 及对应大小写形式） | 旧组件名（任意大小写），已废弃 |

**允许模式**（流程编号）：

| 允许写法 | 示例 | 说明 |
|----------|------|------|
| `Step N`（带空格） | `Step 1: 环境准备`、`Step 2: 安装依赖` | 流程编号（英文） |
| `步骤 N`（带空格） | `步骤 1：安装依赖`、`步骤 2：配置` | 流程编号（中文） |
| `step N`（小写带空格） | `see step 1 above` | 行内引用（英文） |

### 2.0.1 数据库命名规范

> **决策依据**: [ADR: SeekDB Schema 与 Role 命名统一](./adr_seekdb_schema_role_naming.md)

SeekDB 使用 `seekdb` 作为 schema 名称，角色使用 `seekdb_*` 前缀。

| 标准命名 | 说明 |
|----------|------|
| `seekdb` schema | 标准 schema 名称 |
| `seekdb_migrator` | DDL 迁移角色（NOLOGIN） |
| `seekdb_app` | DML 应用角色（NOLOGIN） |
| `seekdb_migrator_login` | 迁移登录账号（LOGIN） |
| `seekdb_svc` | 服务登录账号（LOGIN） |

**迁移说明**: 存量环境如使用旧 `seek` / `seek_*` 命名，请参考 [SeekDB Schema/Role 命名迁移操作手册](../seekdb/04_schema_role_naming_migration.md) 进行迁移。

### 2.1 禁止词检查范围

- **代码文件**: `*.py`, `*.js`, `*.ts`, `*.go`, `*.rs`
- **配置文件**: `*.yml`, `*.yaml`, `*.toml`, `*.json`, `*.env`
- **文档文件**: `*.md`, `*.rst`, `*.txt`
- **Shell 脚本**: `*.sh`, `*.bash`
- **SQL 脚本**: `*.sql`
- **日志输出**: 运行时日志、CLI 输出
- **注释**: 代码注释、TODO 注释

### 2.2 CI 检查

建议在 CI 中添加禁止词检查：

```bash
# 检查禁止词（匹配无空格的 stepN 组件旧名，N=1,2,3）
rg -iP '(?i)step[1-3](?!\s)' --type-add 'code:*.{py,js,ts,go,rs,yml,yaml,toml,json,md,sh,sql}' -t code
```

### 2.3 Step 文案治理

> **决策依据**：[ADR: Step 文案治理 — 组件旧命名与流程编号区分](./adr_step_flow_wording.md)
>
> **详细规范**：[旧组件命名治理规范](./legacy_naming_governance.md)

**关键区分**（使用正则模式）：

| 模式 | 类型 | 是否允许 |
|------|------|----------|
| `(?i)step[1-3](?!\s)` | 组件旧命名（无空格） | **禁止** |
| `Step [1-9]` / `step [1-9]` | 流程编号（英文带空格） | **允许** |
| `步骤 [1-9]` | 流程编号（中文带空格） | **允许** |

> **为什么 `Step N`（带空格）不会与组件旧名混淆？**
>
> - **禁止规则仅针对无空格的 `StepN` 格式**：正则 `(?i)step[1-3](?!\s)` 使用负向前瞻 `(?!\s)` 明确排除后跟空格的情况
> - **语法结构天然区分**：组件旧名是连写标识符（`step1_xxx`），流程编号是 "Step" + 空格 + 数字（`Step 1:`）
> - **CI 检查不会误报**：带空格的流程编号不匹配禁止模式，可安全使用

**示例**：

```markdown
<!-- 禁止：无空格的 StepN（N=1,2,3）形式属于组件旧名 -->
<!-- 示例：Step1、step2、STEP3 等连写形式 -->

<!-- 正确：使用官方组件名称 -->
## Logbook 部署指南

<!-- 正确：流程编号必须带空格（不匹配禁止模式） -->
## Step 1: 环境准备
```

---

### 2.4 流程编号格式规范

本节定义流程编号在文档中的统一写法，适用于所有 Markdown 文档。

#### 2.4.1 英文流程编号

| 场景 | 格式 | 示例 |
|------|------|------|
| **标题（Heading）** | `## N. 标题内容` | `## 1. Environment Setup` |
| **有序列表** | `N)` 或 `N.` | `1) Install dependencies` |
| **行内引用** | `step N` 或 `Step N` | `see step 1 above` |

#### 2.4.2 中文流程编号

| 场景 | 格式 | 示例 |
|------|------|------|
| **标题（Heading）** | `## 步骤 N：标题内容` | `## 步骤 1：环境准备` |
| **有序列表** | `N)` 或 `N.` | `1) 安装依赖` |
| **行内引用** | `步骤 N` | `参见步骤 1` |

#### 2.4.3 可复制模板

**英文标题模板**

```markdown
## 1. Environment Setup

## 2. Install Dependencies

## 3. Run Verification
```

**中文标题模板**

```markdown
## 步骤 1：环境准备

## 步骤 2：安装依赖

## 步骤 3：运行验证
```

**有序列表模板**

```markdown
1) Clone the repository
2) Install dependencies
3) Run the tests

<!-- 或使用点号 -->
1. Clone the repository
2. Install dependencies
3. Run the tests
```

**行内引用模板**

```markdown
<!-- 英文 -->
As described in step 1, you need to...
Please refer to Step 2 for details.

<!-- 中文 -->
如步骤 1 所述，您需要...
详见步骤 2。
```

#### 2.4.4 注意事项

1. **英文标题使用 `N.` 后跟空格**：`## 1. Title`（数字 + 点 + 空格 + 标题）
2. **中文标题使用 `步骤 N：`**：`## 步骤 1：标题`（步骤 + 空格 + 数字 + 中文冒号 + 标题）
3. **列表项 `N)` 与 `N.` 均可**：项目内保持一致即可
4. **行内引用保持小写**：英文行内用 `step N`，句首用 `Step N`

---

## 3. 环境变量规范

### 3.1 Logbook 环境变量

| 变量 | 说明 |
|------|------|
| `ENGRAM_LOGBOOK_CONFIG` | Logbook 配置文件路径 |
| `LOGBOOK_MIGRATOR_PASSWORD` | Logbook 迁移账号密码 |
| `LOGBOOK_SVC_PASSWORD` | Logbook 服务账号密码 |

### 3.2 Gateway 环境变量

| 变量 | 说明 |
|------|------|
| `GATEWAY_PORT` | Gateway 端口（默认 8787） |
| `OPENMEMORY_MIGRATOR_PASSWORD` | OpenMemory 迁移账号密码 |
| `OPENMEMORY_SVC_PASSWORD` | OpenMemory 服务账号密码 |
| `OM_PORT` | OpenMemory 端口（默认 8080） |
| `OM_PG_SCHEMA` | OpenMemory Schema 名 |
| `OM_METADATA_BACKEND` | 元数据后端类型 |

### 3.3 SeekDB 环境变量

#### 3.3.1 Canonical 环境变量（`SEEKDB_*` 前缀）

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `SEEKDB_ENABLE` | SeekDB 启用开关（0=禁用，1=启用） | `1` |
| `SEEKDB_MIGRATOR_PASSWORD` | SeekDB 迁移账号密码（对应 `seekdb_migrator_login` 角色） | - |
| `SEEKDB_SVC_PASSWORD` | SeekDB 服务账号密码（对应 `seekdb_svc` 角色） | - |
| `SEEKDB_PGVECTOR_DSN` | PGVector 连接字符串 | - |
| `SEEKDB_INDEX_BACKEND` | 索引后端类型 | - |
| `SEEKDB_PG_SCHEMA` | PostgreSQL Schema | `seekdb` |

#### 3.3.2 Deprecated 别名（计划于 2026-Q3 移除）

| Deprecated 别名 | Canonical 变量 | 移除时间 |
|-----------------|----------------|----------|
| `SEEK_ENABLE` | `SEEKDB_ENABLE` | 2026-Q3 |
| `SEEK_MIGRATOR_PASSWORD` | `SEEKDB_MIGRATOR_PASSWORD` | 2026-Q3 |
| `SEEK_SVC_PASSWORD` | `SEEKDB_SVC_PASSWORD` | 2026-Q3 |
| `SEEK_PG_SCHEMA` | `SEEKDB_PG_SCHEMA` | 2026-Q3 |
| `SEEK_SCHEMA` | `SEEKDB_PG_SCHEMA` | 2026-Q3 |

#### 3.3.3 优先级与冲突处理

**优先级规则**：Canonical 变量优先于 Deprecated 别名。

```
SEEKDB_ENABLE > SEEK_ENABLE
SEEKDB_PG_SCHEMA > SEEK_PG_SCHEMA > SEEK_SCHEMA
SEEKDB_MIGRATOR_PASSWORD > SEEK_MIGRATOR_PASSWORD
SEEKDB_SVC_PASSWORD > SEEK_SVC_PASSWORD
```

**冲突处理策略**：
1. **双设置检测**：当 canonical 与 deprecated 同时设置且值不同时，使用 canonical 值并输出警告
2. **Deprecation 警告**：仅使用 deprecated 别名时，输出迁移提示
3. **静默降级**：仅使用 canonical 时，无额外日志

**示例**：

```bash
# 推荐：仅使用 canonical
export SEEKDB_ENABLE=1
export SEEKDB_PG_SCHEMA=seekdb

# 兼容期支持：deprecated 别名（会输出 deprecation 警告）
export SEEK_ENABLE=1
# [DEPRECATION WARNING] SEEK_ENABLE 已废弃，请迁移到 SEEKDB_ENABLE（计划于 2026-Q3 移除）

# 冲突场景：canonical 优先
export SEEKDB_ENABLE=1
export SEEK_ENABLE=0
# 实际生效值: SEEKDB_ENABLE=1（canonical 优先）
# [WARNING] SEEKDB_ENABLE 与 SEEK_ENABLE 同时设置且值不同，使用 canonical 值: 1
```

### 3.4 通用环境变量

| 变量 | 说明 |
|------|------|
| `PROJECT_KEY` | 项目标识 |
| `POSTGRES_DB` | 数据库名 |
| `POSTGRES_PASSWORD` | PostgreSQL 密码 |
| `POSTGRES_PORT` | PostgreSQL 端口 |
| `POSTGRES_DSN` | PostgreSQL 连接字符串 |

### 3.4.1 SeekDB 兼容窗口与移除时间线

| 阶段 | 版本 | 时间范围 | 策略 |
|------|------|----------|------|
| **Phase 1: 双写期** | v0.4.x | 2026-Q1 | 新旧 schema/角色/环境变量并存，代码支持两种命名 |
| **Phase 2: 警告期** | v0.5.x | 2026-Q2 | 使用旧命名时输出 deprecation warning |
| **Phase 3: 移除期** | v0.6.0+ | **2026-Q3** | 仅支持 `seekdb` 命名，移除旧兼容代码 |

**关键节点**：
- **2026-Q3 移除点**：所有 `SEEK_*` 环境变量别名、`seek` schema 兼容代码将被移除
- **与 Compose/CI 一致**：`docker-compose.unified.yml`、`.github/workflows/*.yml` 中的注释均标注 `计划于 2026-Q3 移除`

### 3.5 PostgreSQL GUC 配置变量

#### 3.5.1 当前 GUC 状态

| GUC 变量 | 用途 | 当前状态 |
|----------|------|----------|
| `seek.enabled` | SeekDB 功能开关（true/false） | **保留为稳定接口** |
| `seek.target_schema` | SeekDB 目标 schema 名称 | **保留为稳定接口** |

#### 3.5.2 GUC 命名决策

**决策：保留 `seek.*` GUC 名称作为稳定接口，不迁移到 `seekdb.*`。**

**理由**：
1. **向后兼容**：GUC 变量嵌入在 SQL 脚本、存储过程中，变更成本高
2. **稳定接口**：GUC 作为数据库层配置接口，变更频率应低于应用层
3. **语义清晰**：`seek.enabled` 简洁易读，`seekdb.enabled` 冗余
4. **隔离层**：环境变量 (`SEEKDB_*`) 与 GUC (`seek.*`) 分离，职责清晰

**使用示例**：

```sql
-- 启用/禁用 SeekDB 验证
SET seek.enabled = 'true';   -- 启用（默认）
SET seek.enabled = 'false';  -- 禁用

-- 指定目标 schema
SET seek.target_schema = 'seekdb';  -- 默认值
SET seek.target_schema = 'seekdb_test';  -- 测试环境

-- 在 psql 中通过 -c 传入
psql -c "SET seek.enabled = 'true'" -f 99_verify_permissions.sql
```

**映射关系**：

| 环境变量 (Canonical) | GUC 变量 | 说明 |
|---------------------|----------|------|
| `SEEKDB_ENABLE` | `seek.enabled` | 通过 bootstrap 脚本转换 |
| `SEEKDB_PG_SCHEMA` | `seek.target_schema` | 通过 bootstrap 脚本转换 |

---

## 4. CLI 命令规范

| 命令 | 说明 |
|------|------|
| `logbook` | Logbook CLI（console script） |
| `engram-logbook` | Logbook CLI（完整名称） |

---

## 5. 数据库 Schema/Role 规范

### 5.1 Schema

| Schema | 所属组件 | 说明 |
|--------|----------|------|
| `identity` | Logbook | 身份管理 |
| `logbook` | Logbook | 事件日志 |
| `scm` | Logbook | SCM 同步 |
| `analysis` | Logbook | 分析数据 |
| `governance` | Logbook | 治理审计 |
| `openmemory` | Gateway | OpenMemory 数据 |
| `seekdb` | SeekDB | 向量索引 |

### 5.2 Role

> **注意**: SeekDB 使用 `seekdb` schema 和 `seekdb_*` 角色前缀。详见 [ADR: SeekDB Schema 与 Role 命名统一](./adr_seekdb_schema_role_naming.md)。

| 角色 | 类型 | 职责 |
|------|------|------|
| `engram_admin` | NOLOGIN | Engram 超级管理员 |
| `engram_migrator` | NOLOGIN | DDL 迁移角色 |
| `engram_app_readwrite` | NOLOGIN | DML 读写角色 |
| `engram_app_readonly` | NOLOGIN | 只读角色 |
| `logbook_migrator` | LOGIN | Logbook 迁移账号 |
| `logbook_svc` | LOGIN | Logbook 服务账号 |
| `openmemory_migrator` | NOLOGIN | OpenMemory DDL 迁移角色 |
| `openmemory_app` | NOLOGIN | OpenMemory DML 角色 |
| `openmemory_migrator_login` | LOGIN | OpenMemory 迁移账号 |
| `openmemory_svc` | LOGIN | OpenMemory 服务账号 |
| `seekdb_migrator` | NOLOGIN | SeekDB DDL 迁移角色 |
| `seekdb_app` | NOLOGIN | SeekDB DML 角色 |
| `seekdb_migrator_login` | LOGIN | SeekDB 迁移账号 |
| `seekdb_svc` | LOGIN | SeekDB 服务账号 |

### 5.3 权限策略

**Source-of-truth 策略**：
- `seekdb_*` 是标准命名，所有权限定义在此角色上
- Schema owner 设置为 `seekdb_migrator`
- 默认权限 (`ALTER DEFAULT PRIVILEGES`) 配置在 `seekdb_migrator` 上

**环境变量**：

| 环境变量 | 说明 |
|----------|------|
| `SEEKDB_MIGRATOR_PASSWORD` | SeekDB 迁移账号密码 |
| `SEEKDB_SVC_PASSWORD` | SeekDB 服务账号密码 |

---

## 6. 兼容策略

本项目对不同命名域采用差异化治理策略。策略类型定义：

| 策略 | 含义 | 适用场景 |
|------|------|----------|
| **remove** | 已完全移除，无兼容层 | 旧入口、旧模块路径 |
| **deprecate** | 保留兼容窗口，计划移除 | 环境变量别名、旧 schema/role |
| **stub** | 作为稳定接口保留，不迁移 | GUC 变量等嵌入成本高的接口 |

---

### 6.1 域 A：StepX 历史命名

> **治理策略：remove（已移除）**
>
> **适用范围**：目录路径、Python 模块、配置环境变量中的数字阶段命名（`stepN` 形式，N=1,2,3）

以下旧入口已**完全移除**，无兼容层：

| 旧入口 | 替代方案 | 策略 | 状态 |
|--------|----------|------|------|
| 历史目录路径（数字阶段命名） | `apps/logbook_postgres/` 等官方路径 | remove | **已移除** |
| 历史 Python 模块（数字阶段命名） | `engram_logbook` 等官方模块 | remove | **已移除** |
| 历史环境变量（数字阶段命名） | `ENGRAM_LOGBOOK_CONFIG` 等官方变量 | remove | **已移除** |
| 代码/注释/日志中 `(?i)step[1-3](?!\s)` | 官方组件名称 | remove | **禁止** |

#### 历史迁移说明

早期开发阶段项目使用数字编号作为组件临时命名（表示系统构建的三个阶段）。随着项目成熟，这种阶段编号命名存在语义模糊、易与流程步骤混淆、不利于新成员理解等问题，因此采用语义化的组件命名：**Logbook**（事实账本）、**Gateway**（记忆网关）、**SeekDB**（检索索引）。

迁移时间线（2026-01）：
1. 启动命名迁移，定义官方组件名称
2. 完成目录重命名（模块路径统一为 `logbook_postgres`、`openmemory_gateway`、`seekdb_rag_hybrid`）
3. 完成 Python 模块重命名（`engram_logbook`）
4. 发布命名约束文档，定义禁止词列表

---

### 6.2 域 B：SeekDB Schema/Role/环境变量

> **治理策略：deprecate（保留兼容窗口，计划于 2026-Q3 移除）**
>
> **适用范围**：SeekDB 组件的 PostgreSQL schema、角色、环境变量
>
> **决策依据**：[ADR: SeekDB Schema 与 Role 命名统一](./adr_seekdb_schema_role_naming.md)

#### 6.2.1 兼容窗口时间线

| 阶段 | 版本 | 时间范围 | 策略 | 说明 |
|------|------|----------|------|------|
| **Phase 1: 双写期** | v0.4.x | 2026-Q1 | 新旧并存 | 代码支持 `seek` 与 `seekdb` 两种命名 |
| **Phase 2: 警告期** | v0.5.x | 2026-Q2 | deprecation warning | 使用旧命名时输出警告 |
| **Phase 3: 移除期** | v0.6.0+ | **2026-Q3** | 仅支持新命名 | 移除所有旧兼容代码 |

#### 6.2.2 Schema 与 Role 命名

| 对象类型 | 旧名称 | Canonical 名称 | 策略 | 移除时间 |
|----------|--------|----------------|------|----------|
| Schema | `seek` | `seekdb` | deprecate | 2026-Q3 |
| 迁移角色 | `seek_migrator` | `seekdb_migrator` | deprecate | 2026-Q3 |
| 应用角色 | `seek_app` | `seekdb_app` | deprecate | 2026-Q3 |
| 迁移登录角色 | `seek_migrator_login` | `seekdb_migrator_login` | deprecate | 2026-Q3 |
| 服务账号 | `seek_svc` | `seekdb_svc` | deprecate | 2026-Q3 |

#### 6.2.3 环境变量别名

| Deprecated 别名 | Canonical 变量 | 策略 | 移除时间 | 当前行为 |
|-----------------|----------------|------|----------|----------|
| `SEEK_ENABLE` | `SEEKDB_ENABLE` | deprecate | 2026-Q3 | 输出 deprecation warning |
| `SEEK_MIGRATOR_PASSWORD` | `SEEKDB_MIGRATOR_PASSWORD` | deprecate | 2026-Q3 | 输出 deprecation warning |
| `SEEK_SVC_PASSWORD` | `SEEKDB_SVC_PASSWORD` | deprecate | 2026-Q3 | 输出 deprecation warning |
| `SEEK_PG_SCHEMA` | `SEEKDB_PG_SCHEMA` | deprecate | 2026-Q3 | 输出 deprecation warning |
| `SEEK_SCHEMA` | `SEEKDB_PG_SCHEMA` | deprecate | 2026-Q3 | 输出 deprecation warning |

**优先级规则**：Canonical 变量优先于 Deprecated 别名。详见 [§3.3.3 优先级与冲突处理](#333-优先级与冲突处理)。

---

### 6.3 域 C：稳定接口（GUC 变量）

> **治理策略：stub（作为稳定接口保留，不迁移）**
>
> **适用范围**：PostgreSQL GUC 配置变量

| GUC 变量 | 用途 | 策略 | 说明 |
|----------|------|------|------|
| `seek.enabled` | SeekDB 功能开关 | stub | 保留为稳定接口，不迁移到 `seekdb.*` |
| `seek.target_schema` | SeekDB 目标 schema | stub | 保留为稳定接口，不迁移到 `seekdb.*` |

**保留理由**：
1. GUC 变量嵌入在 SQL 脚本、存储过程中，变更成本高
2. 作为数据库层配置接口，稳定性要求高于应用层
3. 环境变量 (`SEEKDB_*`) 与 GUC (`seek.*`) 分离，职责清晰

详见 [§3.5 PostgreSQL GUC 配置变量](#35-postgresql-guc-配置变量)。

---

### 6.4 迁移动作

如果您从旧版本升级，请执行以下迁移动作：

**1. 更新目录路径**

```bash
# 如果您的项目中有旧路径引用，请更新
# 旧: 历史数字阶段命名目录（如 apps/stepN_xxx/）
# 新: 官方组件命名目录（如 apps/logbook_postgres/）
```

**2. 更新 Python 导入**

```python
# 旧（已废弃）:
# from engram_stepN import ...（历史数字阶段命名模块）

# 新:
from engram_logbook import ...
```

**3. 更新环境变量**

```bash
# 旧（已废弃）:
# export ENGRAM_STEPN_CONFIG=/path/to/config.toml（历史数字阶段命名变量）

# 新:
export ENGRAM_LOGBOOK_CONFIG=/path/to/config.toml
```

**4. 更新配置文件引用**

检查以下文件并替换旧命名：

- `.env` 文件
- `docker-compose*.yml` 文件
- CI/CD workflow 文件（`.github/workflows/*.yml`）
- Makefile

**5. 更新代码中的注释和日志**

搜索并替换所有数字阶段命名相关文案：

```bash
# 查找需要更新的位置（匹配无空格的 stepN 组件旧名）
rg -iP '(?i)step[1-3](?!\s)' --type-add 'all:*.{py,js,ts,yml,yaml,toml,json,md,sh,sql}' -t all
```

### 6.5 验证迁移完成

```bash
# 确认无禁止词残留（匹配无空格的 stepN 组件旧名）
rg -iP '(?i)step[1-3](?!\s)' . && echo "ERROR: 存在禁止词" || echo "OK: 无禁止词"

# 验证服务正常
make deploy && make verify-unified
```

---

### 6.6 兼容策略总览

| 域 | 适用范围 | 策略 | 移除时间 | 参考文档 |
|----|----------|------|----------|----------|
| **A: StepX 历史命名** | 目录、模块、数字阶段命名文案 | remove | 已移除 | [§6.1 历史迁移说明](#历史迁移说明) |
| **B: SeekDB schema/role/env** | `seek` → `seekdb` 迁移 | deprecate | 2026-Q3 | [ADR: SeekDB Schema 与 Role 命名统一](./adr_seekdb_schema_role_naming.md) |
| **C: 稳定接口（GUC）** | `seek.*` GUC 变量 | stub | 永久保留 | [§3.5 PostgreSQL GUC 配置变量](#35-postgresql-guc-配置变量) |

---

## 7. 变更日志

| 日期 | 版本 | 变更内容 |
|------|------|----------|
| 2026-01-30 | v2.5 | 删除 `step_glossary.md`，将迁移背景并入 §6.1 历史迁移说明小节 |
| 2026-01-30 | v2.4 | 重写兼容策略章节：按域拆分（A: StepX 历史命名、B: SeekDB schema/role/env、C: 稳定接口）；明确每域策略（remove/deprecate/stub）与时间线；与 ADR 对齐术语（双写期/警告期/移除期） |
| 2026-01-30 | v2.3 | 用正则模式 `(?i)step[1-3](?!\s)` 描述禁止词；更新历史命名引用至 `legacy_naming_governance.md`；统一兼容策略章节，明确 `SEEK_*` 废弃别名的兼容窗口与移除时间线 |
| 2026-01-30 | v2.2 | 添加 SeekDB 兼容窗口（Phase 1/2/3）与 2026-Q3 移除时间点；完善环境变量 canonical/deprecated 列表与优先级处理；添加 GUC 命名决策（保留 `seek.*` 作为稳定接口） |
| 2026-01-30 | v2.1 | 更新 SeekDB schema/role/环境变量章节，与 ADR 对齐（`seek` → `seekdb`） |
| 2026-01-30 | v2.0 | 重构为命名约束文档，添加禁止词列表，采用完全移除策略 |
| 2026-01-30 | v1.0 | 初始版本 |

---

## 8. 参考文档

- [README.md](../../README.md) - 项目快速开始
- [旧组件命名治理规范](./legacy_naming_governance.md) - 旧组件命名与流程编号的区分规则
- [04_roles_and_grants.sql](../../apps/logbook_postgres/sql/04_roles_and_grants.sql) - Logbook 角色定义
- [05_openmemory_roles_and_grants.sql](../../apps/logbook_postgres/sql/05_openmemory_roles_and_grants.sql) - OpenMemory 角色定义
- [01_seekdb_roles_and_grants.sql](../../apps/seekdb_rag_hybrid/sql/01_seekdb_roles_and_grants.sql) - SeekDB 角色定义
- [ADR: SeekDB Schema 与 Role 命名统一](./adr_seekdb_schema_role_naming.md) - SeekDB 命名决策记录
