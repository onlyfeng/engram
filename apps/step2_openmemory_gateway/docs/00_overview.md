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
