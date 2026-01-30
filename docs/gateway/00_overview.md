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

> **详细说明**: [根 README §统一栈验证入口](../../README.md#统一栈验证入口)

| 入口 | 命令 | 说明 |
|------|------|------|
| **主入口** | `make verify-unified` | 推荐使用，自动配置环境变量 |
| **脚本入口** | `apps/openmemory_gateway/scripts/verify_unified_stack.sh` | 备用入口，用于调试或自定义参数 |

**Makefile 目标**

```bash
make verify-unified                    # 基础验证（推荐）
VERIFY_FULL=1 make verify-unified      # 完整验证（含降级测试）
make test-gateway-integration          # Gateway 集成测试
make openmemory-upgrade-check          # OpenMemory 升级验证
```

**脚本直接调用（备用）**

```bash
./apps/openmemory_gateway/scripts/verify_unified_stack.sh           # 基础验证
./apps/openmemory_gateway/scripts/verify_unified_stack.sh --full    # 完整验证
./apps/openmemory_gateway/scripts/verify_unified_stack.sh --help    # 查看所有选项
```

**环境变量**

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GATEWAY_URL` | `http://localhost:8787` | Gateway 地址 |
| `OPENMEMORY_URL` | `http://localhost:8080` | OpenMemory 地址 |
| `POSTGRES_DSN` | — | 降级测试必需 |
| `COMPOSE_PROJECT_NAME` | — | 多项目隔离 |

## OpenMemory Vendoring 与补丁管理（精简摘要）

> **完整文档**: [docs/openmemory/00_vendoring_and_patches.md](../openmemory/00_vendoring_and_patches.md)

### 集成方式

Engram 采用 **Vendored 模式** 集成 OpenMemory（`libs/OpenMemory/`），通过 `OpenMemory.upstream.lock.json` 锁定上游版本，`openmemory_patches.json` 管理补丁清单。

### 补丁分类

| 类别 | 数量 | 处理策略 |
|------|------|----------|
| A (必保留) | 14 | 始终在本地维护，不上游 |
| B (可上游) | 5 | 短期本地维护，长期提交 PR |
| C (可移除) | 1 | 重构后删除 |

### 关键 Makefile Targets

```bash
# 日常检查
make openmemory-vendor-check               # 结构检查（CI required）
make openmemory-sync-verify                # 补丁落地校验

# Schema 校验
make openmemory-schema-validate SCHEMA_STRICT=1   # 严格模式（CI 门禁）

# 升级流程
make openmemory-upgrade-check              # 完整升级验证（upstream_ref 变更后必跑）
```

### upstream_ref 变更门槛

当 `upstream_ref` 发生变更时，CI 必须执行 5 项门禁检查：

1. `make openmemory-sync-check` — 一致性检查
2. `make openmemory-sync-verify` — 补丁落地校验
3. `make openmemory-test-multi-schema` — 多 Schema 隔离测试
4. `make openmemory-schema-validate SCHEMA_STRICT=1` — JSON Schema 严格校验
5. `make openmemory-vendor-check` — Vendor 结构检查

详细的冲突分级（L0-L3）、Freeze 机制、回滚流程请参阅 [docs/openmemory/00_vendoring_and_patches.md](../openmemory/00_vendoring_and_patches.md)。

### 附录参考

- **Appendix A**: Category B 补丁的最小可上游 PR 划分（按文件/功能）及与 Category A 解耦点
- **Appendix B**: fork+subtree 迁移 Checklist（含 `OpenMemory.upstream.lock.json` 审计语义保留、fetch/sync 流程替换、CI 变更点）

详见 [docs/openmemory/00_vendoring_and_patches.md#appendix-a-category-b-补丁的最小可上游-pr-划分](../openmemory/00_vendoring_and_patches.md#appendix-a-category-b-补丁的最小可上游-pr-划分)。
