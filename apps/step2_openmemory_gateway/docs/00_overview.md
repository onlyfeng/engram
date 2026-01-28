# 概览

## Step2 解决什么问题
- 把“经验/知识”从零散文件与人脑中抽离，形成可检索、可强化、可共享的记忆资产
- 支持“角色切换”（数字分身）：按 owner/module/scope 召回，辅助开发/审查/解释
- 与 Step1 配合：记忆条目必须能回跳到证据链（commit/rev/mr/event_id/patch_sha）

## 为什么需要 Gateway
- 团队可写开关必须服务端强制执行，不能依赖 IDE 侧自律
- Gateway 统一：写入裁剪、策略校验、审计落库、失败降级（写 outbox）

## OpenMemory 依赖面

### 引用路径（docker-compose.unified.yml）

| 服务 | Context 路径 | Profile |
|------|-------------|---------|
| `openmemory` | `./libs/OpenMemory/packages/openmemory-js` | 默认 |
| `openmemory_migrate` | `./libs/OpenMemory/packages/openmemory-js` | 默认 |
| `dashboard` | `./libs/OpenMemory/dashboard` | `dashboard`（可选） |

> **注意**: Dashboard 为可选组件（profile: dashboard），默认不启动。
> `libs/OpenMemory/dashboard/` 目前只有 Dockerfile 占位，完整源码需从上游同步后才能构建。
> 启用方式: `docker compose --profile dashboard up -d`

### 关键环境变量

**PostgreSQL 连接**

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `OM_METADATA_BACKEND` | `postgres` | 元数据后端 |
| `OM_PG_HOST` | `postgres` | 数据库主机 |
| `OM_PG_PORT` | `5432` | 数据库端口 |
| `OM_PG_DB` | `${POSTGRES_DB:-engram}` | 数据库名（与 Step1 同库） |
| `OM_PG_SCHEMA` | `openmemory` | Schema 名（禁止设为 public） |
| `OM_PG_TABLE` | `openmemory_memories` | 记忆表名 |
| `OM_PG_AUTO_DDL` | `false` | 运行时禁止自动建表 |

**角色与凭证**

| 场景 | 登录用户 | SET ROLE |
|------|----------|----------|
| 迁移 (DDL) | `openmemory_migrator_login` | `openmemory_migrator` |
| 运行 (DML) | `openmemory_svc` | `openmemory_app` |

**服务配置**

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `OM_PORT` | `8080` | HTTP 端口 |
| `OM_MODE` | `standard` | 运行模式 |
| `OM_VECTOR_BACKEND` | `postgres` | 向量后端 |
| `OM_EMBEDDINGS` | `synthetic` | Embedding 提供者 |
| `OM_VEC_DIM` | `256` | 向量维度 |

### API 路径与字段映射（openmemory_client.py）

**API 端点**

| 方法 | 路径 | 用途 |
|------|------|------|
| `POST` | `/memory/add` | 添加记忆 |
| `POST` | `/memory/search` | 搜索记忆 |
| `GET` | `/health` | 健康检查 |

**字段映射规范**

```
Gateway 字段        →  OpenMemory 字段
─────────────────────────────────────
payload_md         →  content
actor_user_id      →  user_id（可空）
target_space       →  metadata.target_space
kind               →  metadata.kind
module             →  metadata.module
evidence_refs      →  metadata.evidence_refs
payload_sha        →  metadata.payload_sha
```

### 验证入口

**脚本**
- `../scripts/verify_unified_stack.sh`
  - 基础验证: `../scripts/verify_unified_stack.sh`
  - 完整验证: `../scripts/verify_unified_stack.sh --full`（含降级测试）

**Makefile 目标**
- `make test-gateway-integration` — 运行 Gateway 集成测试
- `make openmemory-upgrade-check` — OpenMemory 升级验证（调用 verify_unified_stack.sh）

**环境变量**

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GATEWAY_URL` | `http://localhost:8787` | Gateway 地址 |
| `OPENMEMORY_URL` | `http://localhost:8080` | OpenMemory 地址 |
| `POSTGRES_DSN` | — | 降级测试必需 |
| `COMPOSE_PROJECT_NAME` | — | 多项目隔离 |

## OpenMemory 上游同步策略

### 决策矩阵

| 维度 | vendor+锁 | fork+submodule | fork+subtree | 纯 submodule | patch 队列 |
|------|-----------|----------------|--------------|--------------|------------|
| **可复现性** | ★★★★★ | ★★★★☆ | ★★★★★ | ★★★☆☆ | ★★☆☆☆ |
| **升级成本** | ★★★☆☆ | ★★★★☆ | ★★★☆☆ | ★★★★★ | ★★☆☆☆ |
| **补丁维护成本** | ★★☆☆☆ | ★★★★★ | ★★★★☆ | ★☆☆☆☆ | ★★★★☆ |
| **zip 交付友好度** | ★★★★★ | ★★☆☆☆ | ★★★★★ | ★☆☆☆☆ | ★★★★☆ |
| **CI 复杂度** | ★★★★★ | ★★★☆☆ | ★★★★☆ | ★★★☆☆ | ★★☆☆☆ |
| **回滚难度** | ★★★★★ | ★★★★☆ | ★★★☆☆ | ★★★★☆ | ★★★★★ |

> 评分说明：★★★★★ = 最优，★☆☆☆☆ = 最差

### 维度解释

| 维度 | 说明 |
|------|------|
| 可复现性 | 不同环境、不同时间构建结果是否一致 |
| 升级成本 | 同步上游新版本所需的人力和时间 |
| 补丁维护成本 | 本地修改（20 个补丁）的长期维护开销 |
| zip 交付友好度 | 能否直接打包为 zip 交付（无需 git/npm/network） |
| CI 复杂度 | 持续集成流水线的配置和维护难度 |
| 回滚难度 | 出现问题后恢复到稳定状态的容易程度 |

### 推荐方案

**短期方案（当前 → 6 个月内）：vendor + 锁**

- 保持现有 `libs/OpenMemory/` 目录结构
- 维护 `OpenMemory.upstream.lock.json` 锁定上游版本
- 维护 `openmemory_patches.json` 跟踪本地补丁
- 优点：zip 交付友好、CI 零额外配置、回滚简单
- 升级流程：
  1. 备份当前目录
  2. 拉取上游新版本到临时目录
  3. 合并 Category A 补丁（14 个必保留）
  4. 运行 `test_multi_schema.ts` 验证
  5. 更新锁定文件

**长期方案（6 个月后）：fork + subtree**

- 创建 Engram 组织下的 OpenMemory fork
- 使用 `git subtree` 将 fork 嵌入 `libs/OpenMemory/`
- 在 fork 中维护 Engram 特定分支 (`engram/main`)
- 优点：
  - 保持 zip 交付友好（subtree 是完整代码，非引用）
  - 补丁作为 fork 提交，可用 `git cherry-pick` 升级
  - 可选择性向上游提交 Category B 补丁
- 升级流程：
  1. 在 fork 中 `git fetch upstream && git merge upstream/main`
  2. 解决冲突并测试
  3. `git subtree pull` 更新本项目

### 迁移路径

```
当前状态             短期（维持）           长期（迁移）
─────────────────────────────────────────────────────────
libs/OpenMemory/  →  vendor + 锁         →  fork + subtree
手工复制              版本锁 + 补丁清单       git subtree
无版本控制            可审计升级              原生 git 合并
```

### 补丁分类与上游化

| 类别 | 数量 | 处理策略 |
|------|------|----------|
| A (必保留) | 14 | 始终在本地维护，不上游 |
| B (可上游) | 5 | 短期本地维护，长期提交 PR |
| C (可移除) | 1 | 重构后删除（提取共享模块） |

详见 `openmemory_patches.json` 中各补丁的 `upstream_potential` 字段。

## 治理文件与 Schema 校验

### 治理文件位置

| 文件 | 路径 | 用途 |
|------|------|------|
| Lock 文件 | `OpenMemory.upstream.lock.json` | 锁定上游版本、记录同步状态、存储校验和 |
| Patches 清单 | `openmemory_patches.json` | 详细补丁清单（位置、分类、SHA256） |
| Lock Schema | `schemas/openmemory_upstream_lock.schema.json` | Lock 文件的 JSON Schema 定义 |
| Patches Schema | `schemas/openmemory_patches.schema.json` | Patches 清单的 JSON Schema 定义 |

### Schema 校验命令

```bash
# 警告模式（默认，不阻断 CI）
make openmemory-schema-validate

# 严格模式（CI 门禁使用，校验失败则阻断）
make openmemory-schema-validate SCHEMA_STRICT=1

# Lock 文件格式检查（2空格缩进、键排序、尾换行）
make openmemory-lock-format-check
```

### Patches 目录与 SHA256 用途

**目录结构规划**

```
patches/openmemory/
├── A/                    # Category A: 必须保留的 Engram 约束（14 个）
│   ├── migrate-001-db-name-validation.patch
│   ├── migrate-004-schema-safety-precheck.patch
│   └── ...
├── B/                    # Category B: 可上游化改进（5 个）
│   ├── migrate-002-quote-identifier.patch
│   └── ...
└── C/                    # Category C: 可移除/重构（1 个）
    └── db-001-duplicated-validation.patch
```

**SHA256 校验和用途**

| 字段 | 位置 | 用途 |
|------|------|------|
| `bundle_sha256` | `openmemory_patches.json` → `summary` | 所有 .patch 文件的联合哈希，用于验证补丁完整性 |
| `patch_sha256` | `openmemory_patches.json` → 各补丁条目 | 单个补丁文件哈希，用于追踪变更 |
| `checksums.patched_files[].base` | `OpenMemory.upstream.lock.json` | 上游原始文件 SHA256（基线校验） |
| `checksums.patched_files[].after` | `OpenMemory.upstream.lock.json` | 补丁后文件 SHA256（落地校验） |
| `archive_info.sha256` | `OpenMemory.upstream.lock.json` | GitHub archive 整体 SHA256 |

**校验命令**

```bash
# 验证补丁是否已正确落地（对照 checksums）
make openmemory-sync-verify

# 生成/更新补丁文件的 SHA256
scripts/generate_om_patches.sh
```

## upstream_ref 变更的 CI 门槛

当 `upstream_ref` 发生变更（如从 `v1.3.0` 升级到 `v1.4.0`）时，CI 必须执行以下门禁检查：

### CI 必需检查（Required Checks）

| 序号 | 命令 | 说明 | 阻断级别 |
|------|------|------|----------|
| 1 | `make openmemory-sync-check` | 一致性检查（目录结构/关键文件） | 必须通过 |
| 2 | `make openmemory-sync-verify` | 补丁落地校验（对照 checksums） | 必须通过 |
| 3 | `make openmemory-test-multi-schema` | 多 Schema 隔离测试 | 必须通过 |
| 4 | `make openmemory-schema-validate SCHEMA_STRICT=1` | JSON Schema 严格校验 | 必须通过 |
| 5 | `make openmemory-vendor-check` | Vendor 结构检查 | 必须通过 |

### CI 执行流程

```bash
# 完整升级检查流程（含备份、构建、测试）
make openmemory-upgrade-check

# 或单独执行各项检查
make openmemory-sync-check        # 步骤 1: 一致性检查
make openmemory-sync-verify       # 步骤 2: 补丁落地校验
make openmemory-test-multi-schema # 步骤 3: 多 Schema 隔离测试
make openmemory-schema-validate SCHEMA_STRICT=1  # 步骤 4: Schema 校验
make openmemory-vendor-check      # 步骤 5: Vendor 结构检查
```

### upstream_ref 变更触发条件

- `OpenMemory.upstream.lock.json` 中 `upstream_ref` 字段值变化
- `upstream_commit_sha` 字段值变化
- `archive_info.ref` 字段值变化

## 冲突分级与升级/回滚决策流程

### 冲突分级定义

| 级别 | 触发条件 | 处理策略 | 升级决策 |
|------|----------|----------|----------|
| **L0 无冲突** | 上游变更与补丁文件无交集 | 自动合并 | ✅ 可自动升级 |
| **L1 轻微冲突** | Category B/C 补丁受影响 | 三方合并尝试 | ✅ 可升级（需人工审查） |
| **L2 中度冲突** | Category A 补丁受影响但可调和 | 手动合并 + 验证 | ⚠️ 需技术负责人审批 |
| **L3 严重冲突** | Category A 核心补丁不可调和 | 延迟升级/冻结 | ❌ 触发 freeze |

### 升级决策流程图

```
检测上游新版本
       │
       ▼
┌──────────────────┐
│ make openmemory- │
│ upstream-fetch   │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐     冲突？     ┌──────────────────┐
│ 分析补丁冲突     │───────────────▶│ 判断冲突级别     │
└────────┬─────────┘               └────────┬─────────┘
         │ 无冲突                            │
         ▼                                   ▼
┌──────────────────┐               ┌──────────────────┐
│ 自动同步         │               │ L1/L2: 人工合并  │
│ DRY_RUN=0        │               │ L3: 触发 freeze  │
└────────┬─────────┘               └────────┬─────────┘
         │                                   │
         ▼                                   ▼
┌──────────────────┐               ┌──────────────────┐
│ CI 门禁检查      │               │ 评估 freeze 或   │
│ (5 项必需检查)   │               │ override 路径    │
└────────┬─────────┘               └────────┬─────────┘
         │ 通过                              │
         ▼                                   ▼
┌──────────────────┐               ┌──────────────────┐
│ 更新 lock 文件   │               │ freeze_status    │
│ 提交变更         │               │ 标记冻结         │
└──────────────────┘               └──────────────────┘
```

### Freeze 机制与 Override

**Freeze 触发条件**

| 条件 | freeze_reason 值 |
|------|------------------|
| 统一栈迁移进行中 | `unified_stack_migration` |
| 重大版本发布前 7 天 | `pre_release_freeze` |
| Category A 补丁严重冲突 | `category_a_conflict` |
| 安全漏洞修复等待中 | `security_patch_pending` |

**Lock 文件中的 freeze_status 结构**

```json
{
  "freeze_status": {
    "is_frozen": true,
    "freeze_reason": "category_a_conflict",
    "freeze_started_at": "2026-01-29T00:00:00Z",
    "freeze_expires_at": null,
    "last_override_at": null,
    "last_override_by": null,
    "last_override_reason": null
  }
}
```

**Freeze Override（紧急解冻）**

```bash
# 检查当前冻结状态
cat OpenMemory.upstream.lock.json | jq '.freeze_status'

# Override 需要满足以下条件：
# 1. 技术负责人（tech-lead）批准
# 2. 记录 override 原因
# 3. 安全修复例外：CVE 发布后 48 小时内可紧急升级

# Override 后需更新 lock 文件：
# - last_override_at: 当前时间
# - last_override_by: 批准人
# - last_override_reason: 原因说明
```

### 回滚决策流程

| 触发条件 | 回滚级别 | 操作步骤 |
|----------|----------|----------|
| 升级后测试失败 | 代码回滚 | `git checkout HEAD~1 -- libs/OpenMemory/` + `make openmemory-build` |
| 迁移后数据异常 | 数据库回滚 | `make openmemory-rollback BACKUP_FILE=./backups/xxx.sql` |
| 严重生产问题 | 完整回滚 | 代码回滚 + 数据库回滚 + 服务重启 |

**回滚命令**

```bash
# 开发环境回滚（仅代码）
git checkout <previous_commit> -- libs/OpenMemory/ OpenMemory.upstream.lock.json
make openmemory-build
make up

# 生产环境完整回滚（需指定备份文件和 lock commit）
make openmemory-rollback BACKUP_FILE=./backups/engram_20260129_120000.sql LOCK_COMMIT=abc123

# 回滚步骤说明：
# 1. 停止 openmemory/gateway/worker 服务
# 2. 恢复数据库备份
# 3. 回退 lock 文件到指定 commit
# 4. 重新构建镜像
# 5. 重启服务
```

### 升级前必做检查清单

- [ ] `make openmemory-pre-upgrade-backup`（开发环境）或 `make openmemory-pre-upgrade-backup-full`（生产环境）
- [ ] `make openmemory-pre-upgrade-snapshot-lib`（归档当前 libs/OpenMemory）
- [ ] 检查上游 CHANGELOG 中的 breaking changes
- [ ] 确认 freeze_status.is_frozen == false
- [ ] 运行 `make openmemory-upstream-sync DRY_RUN=1` 预览变更
