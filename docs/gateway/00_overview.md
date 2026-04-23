# 概览

> **相关文档**：
> - [根 README 快速开始](../../README.md) — 部署指南、健康检查
> - [Gateway ↔ Logbook 边界契约](../contracts/gateway_logbook_boundary.md) — 组件职责划分、接口规范
> - [Cursor MCP 集成指南](02_mcp_integration_cursor.md) — IDE 端配置、端到端集成步骤

## Gateway（OpenMemory MCP Gateway）解决什么问题
- 把“经验/知识”从零散文件与人脑中抽离，形成可检索、可强化、可共享的记忆资产
- 支持“角色切换”（数字分身）：按 owner/module/scope 召回，辅助开发/审查/解释
- 与 Logbook 配合：记忆条目必须能回跳到证据链（commit/rev/mr/event_id/patch_sha）

## 为什么需要 Gateway
- 团队可写开关必须服务端强制执行，不能依赖 IDE 侧自律
- Gateway 统一：写入裁剪、策略校验、审计落库、失败降级（写 outbox）

## OpenMemory 依赖面

### 引用方式（docker-compose.unified.yml）

| 服务 | 构建方式 | 说明 |
|------|----------|------|
| `openmemory` | 上游镜像 `OPENMEMORY_IMAGE`（`docker/openmemory.Dockerfile` 透传） | 默认 |
| `openmemory_migrate` | 同上（占位迁移容器） | 默认 |

> **注意**: 当前默认走上游镜像，不再依赖 vendoring。  
> Dashboard/看板为可选 profile（`dashboard`），默认不启动。

### 关键环境变量

**PostgreSQL 连接**

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `OM_METADATA_BACKEND` | `postgres` | 元数据后端 |
| `OM_PG_HOST` | `postgres` | 数据库主机 |
| `OM_PG_PORT` | `5432` | 数据库端口 |
| `OM_PG_DB` | `${POSTGRES_DB:-engram}` | 数据库名（与 Logbook 同库） |
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
| `OM_VEC_DIM` | 建议显式设为 `1536` | 向量维度（避免随上游默认值漂移） |

### API 路径与字段映射（openmemory_client.py）

**API 端点**

| 方法 | 路径 | 用途 |
|------|------|------|
| `POST` | `/memory/add` | 添加记忆 |
| `POST` | `/memory/query` | 搜索记忆（当前上游主路径） |
| `GET` | `/health` | 健康检查 |
| `GET` | `/dashboard/health` | Dashboard 健康检查（public endpoint） |
| `GET` | `/dashboard/stats` | Dashboard 指标（JSON） |
| `GET` | `/dashboard/activity` | 最近活动（JSON） |
| `GET` | `/memory/all` | 记忆列表（JSON） |
| `GET` | `/memory/:id` | 记忆详情（JSON，可选 `user_id` query param） |
| `POST` | `/memory/reinforce` | 强化记忆 |
| `DELETE` | `/users/:user_id/memories` | 按用户清空记忆（当前上游主路径） |

> 说明：
> - `/dashboard/*` 为指标 JSON 端点（非 HTML UI），浏览器可直接打开查看 JSON。
> - 当设置了 `OM_API_KEY`（或 `OPENMEMORY_API_KEY`）时，除 public endpoint 外都需要携带 `Authorization: Bearer <key>` 或 `x-api-key: <key>`。
> - `GET /memory/:id` 在 private 空间排查场景下可附带 `user_id`，让 OpenMemory 按 owner 约束读取，避免跨用户 dedup 命中错误对象。
> - 浏览器注入 Header（ModHeader）与端点自检清单，参见 [安装指南](../installation.md) 的「验证 OpenMemory 连接」小节。
> - 由于 OpenMemory 上游处于重写期，Gateway client 当前会自动兼容 `/memory/query` / `/memory/search`、`/memory/*` / `/api/memory/*`、以及缺失 wipe 端点时的逐条删除回退。
> - Gateway 在直写 `memory_store` 与 outbox flush 成功路径都会执行一次 `GET /memory/:id` 写后读校验，确认 `space` / `content` / `payload_sha`；仅瞬时或未分类 GET 错误不会降级已确认的写入。

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

> **详细说明**: [根 README §统一栈验证入口](../../README.md#统一栈验证入口)

| 入口 | 命令 | 说明 |
|------|------|------|
| **主入口** | `make verify-unified` | 推荐使用，自动配置环境变量 |

**Makefile 目标**

```bash
make verify-unified                    # 基础验证（推荐）
VERIFY_FULL=1 make verify-unified      # 完整验证（含降级测试）
make test-gateway-integration          # Gateway 集成测试
make openmemory-upgrade-check          # OpenMemory 升级验证
```

**环境变量**

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GATEWAY_URL` | `http://localhost:8787` | Gateway 地址 |
| `OPENMEMORY_URL` | `http://localhost:8080` | OpenMemory 地址 |
| `POSTGRES_DSN` | — | 降级测试必需 |
| `COMPOSE_PROJECT_NAME` | — | 多项目隔离 |

## OpenMemory 上游镜像说明

统一栈默认固定到上游镜像 `ghcr.io/caviraoss/openmemory:v1.3.3`（通过 `OPENMEMORY_IMAGE` 配置），无需 vendoring。  
如需升级、回滚或内部定制，请维护你自己的镜像仓库或 tag/digest，并在 `.env` 中替换 `OPENMEMORY_IMAGE`。

详细的冲突分级（L0-L3）、Freeze 机制、回滚流程请参阅 [docs/openmemory/00_vendoring_and_patches.md](../openmemory/00_vendoring_and_patches.md)。

### 附录参考

- **Appendix A**: Category B 补丁的最小可上游 PR 划分（按文件/功能）及与 Category A 解耦点
- **Appendix B**: fork+subtree 迁移 Checklist（含 `OpenMemory.upstream.lock.json` 审计语义保留、fetch/sync 流程替换、CI 变更点）

详见 [docs/openmemory/00_vendoring_and_patches.md#appendix-a-category-b-补丁的最小可上游-pr-划分](../openmemory/00_vendoring_and_patches.md#appendix-a-category-b-补丁的最小可上游-pr-划分)。
