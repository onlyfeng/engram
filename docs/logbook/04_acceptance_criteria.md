# Logbook 验收标准

> **适用人群**：开发者、测试人员、运维
> **文档状态**：权威规范

本文档定义 Logbook 组件的 MVP 能力清单、不变量约束与验收矩阵，作为功能验收的唯一权威标准。

---

## MVP 能力清单

### 1. 核心数据模型

| 能力 | 模块 | 说明 | 验收入口 |
|------|------|------|----------|
| **Items** | `logbook.items` | 任务/对象实体存储 | `logbook item create` |
| **Events** | `logbook.events` | 状态变化追加日志 | `logbook event add` |
| **Attachments** | `logbook.attachments` | 证据链指针（URI + sha256） | `logbook attachment add` |
| **KV** | `logbook.kv` | 键值存储（cursor、配置等） | `logbook kv get/set` |

**数据模型关系**：

```
items (1) ──────> (N) events
  │
  └─────────────> (N) attachments
```

### 2. 视图渲染 (render_views)

| 功能 | 命令 | 产物 | 说明 |
|------|------|------|------|
| 渲染 Manifest | `logbook render manifest` | `.artifacts/manifest.json` | 项目级索引视图 |
| 渲染 Index | `logbook render index` | `.artifacts/index.html` | 可浏览的 HTML 索引 |
| 批量渲染 | `logbook render all` | `.artifacts/*` | 全部视图产物 |

### 3. URI 三轨规范

Logbook 是 URI Grammar 的**唯一规范 owner**，定义三类 URI 格式：

| 类型 | 格式 | 用途 | 示例 |
|------|------|------|------|
| **Artifact Key** | 无 scheme 相对路径 | DB 存储（**强制默认**） | `scm/proj_a/1/svn/r100/abc123.diff` |
| **Physical URI** | `file://`、`s3://`、`https://` | 特例输入（需谨慎） | `s3://bucket/engram/proj_a/scm/1/r100.diff` |
| **Evidence URI** | `memory://patch_blobs/...`<br>`memory://attachments/...` | 证据引用（analysis/governance） | `memory://patch_blobs/svn/1:100/abc123` |

**规范实现**：`src/engram/logbook/uri.py`

**关键函数**：

| 函数 | 用途 |
|------|------|
| `parse_uri(uri)` | 解析 URI 结构 |
| `build_evidence_uri(source_type, source_id, sha256)` | 构建 evidence URI |
| `parse_evidence_uri(evidence_uri)` | 解析 patch_blobs evidence |
| `parse_attachment_evidence_uri(evidence_uri)` | 解析 attachment evidence |

### 4. Outbox Lease 协议

Outbox 模块提供记忆降级缓冲队列，支持 Lease 租约协议：

| 函数 | 用途 | Worker 调用场景 |
|------|------|-----------------|
| `enqueue_memory(payload, target_space, item_id)` | 入队 | OpenMemory 写入失败时 |
| `check_dedup(target_space, payload_sha)` | 幂等检查 | 入队前去重 |
| `claim_outbox(worker_id, limit, lease_seconds)` | 获取任务（Lease） | 批量获取待处理 |
| `ack_sent(outbox_id, worker_id, memory_id)` | 确认成功 | 写入成功 |
| `fail_retry(outbox_id, worker_id, error, next_attempt_at)` | 标记重试 | 可重试失败 |
| `mark_dead_by_worker(outbox_id, worker_id, error)` | 标记死信 | 不可恢复或重试耗尽 |
| `renew_lease(outbox_id, worker_id)` | 续期租约 | 长时间处理 |

**状态机**：

```
pending ──────────────────────────> sent   (写入成功)
    │                                 
    └──────────────────────────────> dead   (重试耗尽)
```

**详细契约**：[`docs/contracts/outbox_lease_v2.md`](../contracts/outbox_lease_v2.md)

### 5. Governance Settings + Write Audit

| 功能 | 函数 | 说明 |
|------|------|------|
| 获取/创建治理设置 | `get_or_create_settings(project_key)` | 返回 `team_write_enabled`、`policy_json` |
| 更新治理设置 | `upsert_settings(project_key, ...)` | 管理员配置变更 |
| 写入审计 | `insert_write_audit(actor, target_space, action, ...)` | 每次写入后记录 |
| 查询审计 | `query_write_audit(since, limit, ...)` | 用于 reliability_report |

**write_audit 必需字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `actor_user_id` | `str` | 操作者 |
| `target_space` | `str` | 目标空间（如 `team:project-x`） |
| `action` | `str` | `allow` / `redirect` / `deny` |
| `evidence_refs_json` | `dict` | 证据引用（含 `artifact_uri` 列表） |

---

## 不变量清单

以下不变量必须在系统运行过程中始终成立，违反时应触发告警。

### INV-1: URI 归属唯一性

> **URI Grammar 的唯一规范由 Logbook 层定义和维护**

| 约束 | 说明 |
|------|------|
| 规范文档 | `src/engram/logbook/uri.py` 模块文档 |
| 解析实现 | `parse_uri()`、`parse_evidence_uri()` 为唯一权威解析器 |
| Gateway 职责 | 仅调用 Logbook URI 模块，不自行定义 URI 格式 |

**验证命令**：

```bash
# 检查 URI 解析一致性
python -c "from engram_logbook.uri import parse_uri; print(parse_uri('scm/1/git/abc.diff'))"
```

### INV-2: Redirect ↔ Outbox 计数闭环

> **每个 redirect 审计必须有对应的 outbox 记录**

```sql
-- 预期相等
SELECT COUNT(*) FROM governance.write_audit WHERE action = 'redirect';
SELECT COUNT(*) FROM logbook.outbox_memory WHERE status IN ('pending', 'sent', 'dead');
```

**验证方式**：`reconcile_outbox` 定期检查

**违反场景**：
- audit 写入成功但 outbox 入队失败（应回滚 audit）
- outbox 记录被意外删除

### INV-3: Evidence URI 可解析性

> **`evidence_refs_json` 中的 `artifact_uri` 必须可被 Logbook 解析**

```python
# 验证示例
from engram_logbook.uri import parse_evidence_uri, validate_evidence_ref

for ref in evidence_refs_json.get("patches", []):
    ok, err = validate_evidence_ref(ref)
    assert ok, f"无效 evidence ref: {err}"
```

**URI 格式**：
- `memory://patch_blobs/<source_type>/<source_id>/<sha256>`
- `memory://attachments/<attachment_id>/<sha256>`

### INV-4: Outbox 幂等性保证

> **相同 `(target_space, payload_sha)` 且 `status='sent'` 视为重复**

```sql
-- 重复入队时应返回已存在记录而非创建新记录
SELECT * FROM logbook.outbox_memory 
WHERE target_space = :space AND payload_sha = :sha AND status = 'sent';
```

**验证方式**：`check_dedup()` 前置检查

### INV-5: 审计写入阻断规则

> **Gateway 必须先写审计，失败即阻断主操作**

| 规则 | 说明 |
|------|------|
| 审计优先 | 任何写入操作必须先调用 `insert_write_audit()`，成功后才能继续 |
| 失败阻断 | 审计写入失败时，必须返回错误，不允许继续执行 |
| 不可跳过 | 即使 OpenMemory 写入成功，若审计写入失败，也应视为整体失败 |

---

## 验收矩阵

### 部署级别与能力

Logbook-only 部署支持两种验收级别，详见 [03_deploy_verify_troubleshoot.md#部署级别与验收能力](03_deploy_verify_troubleshoot.md#部署级别与验收能力)：

| 级别 | 名称 | 验收能力 | 典型用途 |
|------|------|----------|----------|
| **A** | DB Baseline-only | `docker compose up` + `pg_isready` | CI 基础设施检查 |
| **B** | Acceptance-ready | 完整 CLI 验收套件 | 功能验证、发布前检查 |

> **SeekDB 非阻塞约束**：Logbook-only 模式下，SeekDB 必须不阻塞。详见 [部署级别与验收能力#SeekDB 非阻塞约束](03_deploy_verify_troubleshoot.md#seekdb-非阻塞约束)。

### 部署模式对比

| 模式 | 组件 | 适用场景 | 验收命令 | 环境语义 |
|------|------|----------|----------|----------|
| **Logbook-only** | PostgreSQL + Logbook | 事实账本、独立部署 | `make acceptance-logbook-only` | - |
| **Unified Standard** | Logbook + Gateway + OpenMemory | 最小统一栈 | `make acceptance-unified-min` | HTTP_ONLY_MODE=1 |
| **Unified Full** | 上述 + 完整测试 | Nightly/发布前 | `make acceptance-unified-full` | **VERIFY_FULL=1**, 含降级测试 |
| **With SeekDB** | Unified + SeekDB | 启用语义检索 | `make acceptance-with-seekdb` | - |
| **SeekDB Disabled** | Unified (SeekDB=disabled) | 禁用语义检索 | `make acceptance-seekdb-disabled` | - |

### Logbook-only 验收

仅验证 Logbook 核心能力，不依赖 Gateway/OpenMemory/SeekDB。

> **注意**：`up-logbook` 启动服务**并执行迁移**（通过 `--profile migrate`）。如需复用已有 PostgreSQL 或分步调试，可使用 `SKIP_DEPLOY=1` 跳过启动步骤。

| 验收项 | 命令 | 预期输出 | 产物路径 | 失败排错 |
|--------|------|----------|----------|----------|
| **服务启动** | `make up-logbook` | `[OK] Logbook 服务已启动` | - | [§排错-端口占用](#1-端口被占用) |
| **DDL 迁移** | `make migrate-ddl` | `DDL 迁移完成` | - | [§排错-schema](#3-schema-不存在) |
| **角色权限** | `make apply-roles` | `Logbook 角色已应用` | - | 检查角色权限配置 |
| **健康检查** | `logbook health` | `{"ok": true, "database": "connected"}` | - | [§排错-连接失败](#2-数据库连接失败) |
| **创建 Item** | `logbook item create --type task` | `{"ok": true, "item_id": N}` | - | [§排错-schema](#3-schema-不存在) |
| **添加 Event** | `logbook event add --item-id N --payload '{}'` | `{"ok": true}` | - | - |
| **添加 Attachment** | `logbook attachment add --item-id N --uri 'test.txt'` | `{"ok": true}` | `.artifacts/test.txt` | - |
| **KV 操作** | `logbook kv set ns key value && logbook kv get ns key` | `value` | - | - |
| **渲染视图** | `logbook render manifest` | `Manifest rendered` | `.artifacts/manifest.json` | - |
| **权限验证** | `make verify-permissions` | `权限验证完成` | `.artifacts/verify-permissions.txt` | 检查角色权限配置 |
| **冒烟测试** | `make logbook-smoke` | `Logbook 冒烟测试通过` | `.artifacts/diag/smoke.log` | [03_deploy_verify_troubleshoot.md](03_deploy_verify_troubleshoot.md) |

### Unified (Standard) 验收

验证 Logbook + Gateway + OpenMemory 集成。

| 验收项 | 命令 | 预期输出 | 产物路径 | 失败排错 |
|--------|------|----------|----------|----------|
| **统一栈启动** | `make up-unified` | `[OK] 统一栈已启动` | - | [README#快速开始](../../README.md#快速开始) |
| **健康检查** | `make verify-unified` | 所有服务 healthy | `.artifacts/diag/health.json` | 检查各服务日志 |
| **Gateway 就绪** | `curl localhost:8787/health` | `{"ok": true}` | - | `make logs-gateway` |
| **Logbook 写入** | `logbook item create` | `{"ok": true}` | - | `make logs-logbook` |
| **OpenMemory 写入** | Gateway memory_store | `{"success": true}` | - | `make logs-openmemory` |
| **Audit 记录** | `logbook audit list --limit 1` | 返回最近审计 | - | - |
| **Outbox 状态** | `logbook outbox status` | 队列状态统计 | - | - |

### Unified (Full) 验收

完整验收，包括单元测试和集成测试。

| 验收项 | 命令 | 预期输出 | 产物路径 | 失败排错 |
|--------|------|----------|----------|----------|
| **Logbook 单元测试** | `make test-logbook-unit` | 全部通过 | `.artifacts/test/logbook-unit.xml` | 查看测试报告 |
| **Gateway 单元测试** | `make test-gateway-unit` | 全部通过 | `.artifacts/test/gateway-unit.xml` | 查看测试报告 |
| **集成测试** | `make test-gateway-integration` | 全部通过 | `.artifacts/test/integration.xml` | 查看测试报告 |
| **Outbox Lease 测试** | pytest `test_outbox_lease.py` | 全部通过 | `.artifacts/test/outbox-lease.xml` | [outbox_lease_v2.md](../contracts/outbox_lease_v2.md) |
| **URI 解析测试** | pytest `test_uri.py` | 全部通过 | `.artifacts/test/uri.xml` | 检查 `uri.py` |

### With SeekDB 验收

验证 SeekDB 语义检索功能。

| 验收项 | 命令 | 预期输出 | 产物路径 | 失败排错 |
|--------|------|----------|----------|----------|
| **SeekDB 启动** | `make up-seekdb` | SeekDB 服务 healthy | - | `make logs-seekdb` |
| **索引构建** | `seekdb index build` | 索引构建成功 | `.artifacts/seekdb/index/` | [docs/seekdb/](../seekdb/) |
| **语义搜索** | `seekdb search "query"` | 返回相关结果 | - | 检查索引状态 |
| **证据回溯** | `seekdb resolve <evidence_uri>` | 返回原始内容 | - | 检查 evidence_resolver |

### SeekDB Disabled 验收

验证禁用 SeekDB 时系统的正常运行。

| 验收项 | 命令 | 预期输出 | 产物路径 | 失败排错 |
|--------|------|----------|----------|----------|
| **禁用启动** | `SEEKDB_ENABLE=0 make up-unified` | 统一栈启动（无 SeekDB） | - | - |
| **Gateway 降级** | `curl localhost:8787/health` | `{"ok": true, "seekdb": "disabled"}` | - | - |
| **Logbook 正常** | `logbook health` | `{"ok": true}` | - | - |
| **OpenMemory 正常** | Gateway memory_store | `{"success": true}` | - | - |
| **搜索降级** | Gateway memory_search | 返回降级提示或基础搜索 | - | 检查降级策略 |

---

## FULL Profile 验收要求

### DB 不变量检查 (db_invariants)

FULL profile 下 `db_invariants` 步骤为**必需步骤**，缺少前置条件时必须失败（不允许静默跳过）。

#### 前置条件

| 条件 | 环境变量 | 检测方式 | 缺失时错误码 |
|------|----------|----------|--------------|
| PostgreSQL DSN | `POSTGRES_DSN` | 环境变量检查 | `CAP_POSTGRES_DSN_MISSING` |
| DB 访问能力 | psql 或 psycopg | 命令/模块检测 | `CAP_NO_DB_ACCESS` |

#### 必须失败条件

以下条件在 FULL profile 下**必须导致验证失败**：

| 条件 | 错误码 | 说明 |
|------|--------|------|
| `POSTGRES_DSN` 未设置 | `CAP_POSTGRES_DSN_MISSING` | 无法连接数据库 |
| 无 DB 访问工具 | `CAP_NO_DB_ACCESS` | 既无 psql 也无 psycopg |
| Schema 缺失 | `LOGBOOK_DB_SCHEMA_MISSING` | 必需 schema 不存在 |
| 表缺失 | `LOGBOOK_DB_TABLE_MISSING` | 必需表不存在 |
| 索引缺失 | `LOGBOOK_DB_INDEX_MISSING` | 必需索引不存在 |
| 物化视图缺失 | `LOGBOOK_DB_MATVIEW_MISSING` | 必需物化视图不存在 |
| DB 结构不完整 | `LOGBOOK_DB_STRUCTURE_INCOMPLETE` | 综合结构检查失败 |

#### 错误码与修复命令映射

| 错误码 | 修复命令 | 说明 |
|--------|----------|------|
| `CAP_POSTGRES_DSN_MISSING` | `export POSTGRES_DSN="postgresql://user:pass@host:port/dbname"` | 设置数据库连接字符串 |
| `CAP_NO_DB_ACCESS` | `pip install psycopg2-binary` 或 `brew install postgresql` | 安装数据库访问工具 |
| `LOGBOOK_DB_SCHEMA_MISSING` | `python logbook_postgres/scripts/db_migrate.py --dsn "$POSTGRES_DSN"` | 执行数据库迁移 |
| `LOGBOOK_DB_TABLE_MISSING` | `python logbook_postgres/scripts/db_migrate.py --dsn "$POSTGRES_DSN"` | 执行数据库迁移 |
| `LOGBOOK_DB_INDEX_MISSING` | `python logbook_postgres/scripts/db_migrate.py --dsn "$POSTGRES_DSN"` | 执行数据库迁移 |
| `LOGBOOK_DB_MATVIEW_MISSING` | `python logbook_postgres/scripts/db_migrate.py --dsn "$POSTGRES_DSN"` | 执行数据库迁移 |
| `LOGBOOK_DB_STRUCTURE_INCOMPLETE` | `python logbook_postgres/scripts/db_migrate.py --dsn "$POSTGRES_DSN"` | 执行数据库迁移 |
| `LOGBOOK_DB_MIGRATE_FAILED` | 检查数据库权限和连接，重试迁移 | 迁移脚本执行失败 |
| `LOGBOOK_DB_CONNECTION_FAILED` | 检查 DSN 格式和网络连接 | 数据库连接失败 |

#### 验证命令

```bash
# 单独验证 DB 不变量（使用 db_migrate.py --verify）
python logbook_postgres/scripts/db_migrate.py --dsn "$POSTGRES_DSN" --verify --json

# 通过 Gateway logbook_adapter 验证
python -c "
import os
os.environ['POSTGRES_DSN'] = '$POSTGRES_DSN'
from gateway.logbook_adapter import ensure_db_ready
result = ensure_db_ready(auto_migrate=False)
print('OK' if result.ok else f'FAIL: {result.message}')
"

# FULL profile 完整验证（包含 db_invariants）
VERIFY_FULL=1 make verify-unified
```

#### LogbookDBErrorCode 常量参考

Gateway 的 `logbook_adapter.py` 定义了以下错误码常量：

```python
class LogbookDBErrorCode:
    # DB 检查相关
    SCHEMA_MISSING = "LOGBOOK_DB_SCHEMA_MISSING"
    TABLE_MISSING = "LOGBOOK_DB_TABLE_MISSING"
    INDEX_MISSING = "LOGBOOK_DB_INDEX_MISSING"
    MATVIEW_MISSING = "LOGBOOK_DB_MATVIEW_MISSING"
    STRUCTURE_INCOMPLETE = "LOGBOOK_DB_STRUCTURE_INCOMPLETE"
    
    # 迁移相关
    MIGRATE_NOT_AVAILABLE = "LOGBOOK_DB_MIGRATE_NOT_AVAILABLE"
    MIGRATE_FAILED = "LOGBOOK_DB_MIGRATE_FAILED"
    MIGRATE_PARTIAL = "LOGBOOK_DB_MIGRATE_PARTIAL"
    
    # 连接相关
    CONNECTION_FAILED = "LOGBOOK_DB_CONNECTION_FAILED"
    CHECK_FAILED = "LOGBOOK_DB_CHECK_FAILED"
```

### Degradation 测试

FULL profile 下 `degradation` 步骤为**必需步骤**，但可通过以下方式明确跳过：

| 跳过方式 | 环境变量 | 说明 |
|----------|----------|------|
| 显式跳过 | `SKIP_DEGRADATION_TEST=1` | 明确声明跳过 |
| HTTP Only 模式 | `HTTP_ONLY_MODE=1` | 仅 HTTP 验证模式 |

#### 必须失败条件（非明确跳过时）

| 条件 | 错误码 | 说明 |
|------|--------|------|
| Docker 不可用 | `CAP_DOCKER_NOT_FOUND` | 无法执行容器操作 |
| Docker daemon 未运行 | `CAP_DOCKER_DAEMON_DOWN` | daemon 不响应 |
| 无法停止容器 | `CAP_CANNOT_STOP_OPENMEMORY` | 权限或容器问题 |

---

## Logbook-only 必需 SQL 文件

### SQL 文件适用模式一览

| 文件 | Logbook-only | Unified Stack | With SeekDB | 说明 |
|------|--------------|---------------|-------------|------|
| `db_bootstrap.py` | 可选 | 必需 | 必需 | 自动检测模式，SKIP 或 CREATE（`python logbook_postgres/scripts/db_bootstrap.py`） |
| `01_logbook_schema.sql` | **必需** | 必需 | 必需 | 核心 schema 定义（identity, logbook, scm, analysis, governance） |
| `04_roles_and_grants.sql` | **必需** | 必需 | 必需 | 核心角色定义（engram_*） |
| `05_openmemory_roles_and_grants.sql` | 不运行 | 必需 | 必需 | OpenMemory 专用 schema 权限 |
| `08_database_hardening.sql` | 推荐 | 必需 | 必需 | 数据库级安全加固 |
| `99_verify_permissions.sql` | 部分执行 | 完整执行 | 完整执行 | 权限验证脚本 |

### Logbook-only 最小 SQL 集

在 Logbook-only 部署模式下，以下 SQL 文件为**必需**：

```
sql/
├── 01_logbook_schema.sql     # 必需：核心 schema 和表定义
└── 04_roles_and_grants.sql   # 必需：核心角色和权限
```

**说明**：
- `db_bootstrap.py`：如果未设置任何 `*_PASSWORD` 环境变量，脚本自动进入 SKIP 模式（执行命令：`python logbook_postgres/scripts/db_bootstrap.py`）
- `05_openmemory_roles_and_grants.sql`：仅 Unified Stack 需要，Logbook-only 不执行
- `08_database_hardening.sql`：推荐执行以提升安全性，但在简化部署时可跳过

### Seek 相关脚本处理策略

在 Logbook-only 模式下，所有 Seek/SeekDB 相关脚本应**不运行/不校验**：

| 脚本/配置 | Logbook-only 处理 | 说明 |
|-----------|-------------------|------|
| `14_seek_schema_rename.sql` | 不运行 | Seek schema 迁移脚本 |
| `seek.enabled` 配置变量 | 设置为 `'false'` | 禁用 Seek 相关验证 |
| `99_verify_permissions.sql` 中的 Seek 验证 | 自动跳过 | 通过 `seek.enabled='false'` 控制 |
| `SEEKDB_*_PASSWORD` 环境变量 | 不设置 | SeekDB 服务账号不创建 |

**验证脚本使用（Logbook-only）**：

```bash
# 方式 1：通过 psql 执行时设置
psql -d $POSTGRES_DB -c "SET seek.enabled = 'false';" -f sql/verify/99_verify_permissions.sql

# 方式 2：在 SQL 中预设（需在 sql/verify/ 目录下执行）
SET seek.enabled = 'false';
\i 99_verify_permissions.sql
```

**预期输出**：
```
[INFO] Seek 未启用 (seek.enabled=false)，跳过 Seek 角色验证
[INFO] Seek 未启用 (seek.enabled=false)，跳过 Seek LOGIN 角色验证
SKIP: Seek 未启用 (seek.enabled=false)，跳过 Seek schema 验证
...
```

---

## 验收命令汇总

### 快速验收（CI PR 推荐）

```bash
# Logbook-only 快速验收
make acceptance-logbook-only

# 统一栈最小验收
make acceptance-unified-min
```

### 完整验收（Nightly/发布前推荐）

```bash
# 统一栈完整验收
make acceptance-unified-full

# 含 SeekDB 验收
make acceptance-with-seekdb

# SeekDB 禁用场景验收
SEEKDB_ENABLE=0 make acceptance-unified-full
```

### 分步验收

```bash
# 1. 部署
make deploy

# 2. 健康检查（标准模式）
make verify-unified
# 或完整模式（含降级测试）
VERIFY_FULL=1 make verify-unified

# 3. Logbook 冒烟测试
make logbook-smoke

# 4. 单元测试
make test-logbook-unit
make test-gateway-unit

# 5. 集成测试（标准模式，HTTP_ONLY）
make test-gateway-integration
# 或完整模式（含降级测试，需要 Docker 权限）
make test-gateway-integration-full
```

---

## 产物路径约定

| 产物类型 | 路径 | 说明 |
|----------|------|------|
| 测试报告 | `.artifacts/test/*.xml` | JUnit XML 格式 |
| 日志 | `.artifacts/diag/*.log` | 诊断日志 |
| 健康检查 | `.artifacts/diag/health.json` | 服务健康状态 |
| 视图产物 | `.artifacts/manifest.json`<br>`.artifacts/index.html` | 渲染输出 |
| SeekDB 索引 | `.artifacts/seekdb/index/` | 语义索引 |
| 覆盖率报告 | `.artifacts/coverage/` | 测试覆盖率 |

---

## 失败排错入口

### 1. 端口被占用

**症状**：`bind: address already in use`

```bash
lsof -i :5432
sudo kill -9 <PID>
# 或使用其他端口
POSTGRES_PORT=5433 make up-logbook
```

### 2. 数据库连接失败

**症状**：`{"ok": false, "code": "CONNECTION_ERROR"}`

```bash
make ps-logbook                    # 检查容器状态
docker logs postgres               # 查看日志
docker exec postgres pg_isready    # 测试连接
```

### 3. Schema 不存在

**症状**：`schema "logbook" does not exist`

```bash
docker logs logbook_migrate        # 检查迁移日志
make migrate-ddl                   # 手动执行 DDL 迁移
make apply-roles                   # 应用角色权限（如需要）
```

### 4. Outbox Lease 失败

**症状**：Worker 无法获取任务或 ack 失败

```bash
# 检查 outbox 状态
logbook outbox status

# 检查 stale lock
SELECT * FROM logbook.outbox_memory 
WHERE status = 'pending' AND locked_until < NOW();
```

### 5. Evidence URI 解析失败

**症状**：`parse_evidence_uri` 返回 None

```bash
# 验证 URI 格式
python -c "from engram_logbook.uri import parse_evidence_uri; print(parse_evidence_uri('memory://...'))"

# 检查 evidence_refs_json 结构
logbook audit show --id <audit_id> | jq '.evidence_refs_json'
```

---

## 验收产物规范

### 通用产物字段

所有验收产物 JSON 文件必须包含以下标准字段：

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `ok` | `boolean` | ✓ | 验收是否通过 |
| `code` | `string` | ✓ | 结果代码（如 `SMOKE_TEST_PASSED`、`HEALTH_CHECK_FAILED`） |
| `message` | `string` | ✓ | 人读描述信息 |
| `ids` | `object` | - | 创建的资源 ID 集合 |
| `sha256` | `object` | - | 相关文件的 SHA256 哈希值 |
| `files` | `array` | - | 生成的文件路径列表 |

### 成功结果示例

```json
{
  "ok": true,
  "code": "SMOKE_TEST_PASSED",
  "message": "Logbook 冒烟测试通过",
  "ids": {
    "item_id": 42,
    "event_id": 101,
    "attachment_id": 55
  },
  "sha256": {
    "attachment": "abc123...",
    "manifest": "def456..."
  },
  "files": [
    "smoke_test/20260130_120000.txt",
    "manifest.csv,index.md"
  ]
}
```

### 失败结果示例

```json
{
  "ok": false,
  "code": "CONNECTION_ERROR",
  "message": "数据库连接失败",
  "detail": {
    "hint": "请检查 POSTGRES_DSN 配置"
  }
}
```

### 产物文件路径约定

| 验收目标 | 产物文件 | 环境变量 | 说明 |
|----------|----------|----------|------|
| `logbook-smoke` | `.artifacts/logbook-smoke.json` | `LOGBOOK_SMOKE_JSON_OUT` | 冒烟测试结果 |
| `health` | 通过 `--json-out` 指定 | - | 健康检查结果 |
| `render_views` | 通过 `--json-out` 指定 | - | 视图渲染结果 |
| `verify-unified` | `.artifacts/verify-results.json` | `VERIFY_JSON_OUT` | 统一栈验证结果 |

### CLI `--json-out` 参数

所有 `engram-logbook` 子命令支持 `--json-out` 参数，可将 JSON 输出同时写入文件：

```bash
# 健康检查并输出到文件
engram-logbook health --json-out .artifacts/health.json

# 渲染视图并输出到文件
engram-logbook render_views --json-out .artifacts/render_views.json

# 创建 item 并输出到文件
engram-logbook create_item --item-type task --title "Test" --json-out result.json
```

### 错误码清单

#### 通用错误码

| 错误码 | 场景 | 修复建议 |
|--------|------|----------|
| `INSTALL_FAILED` | engram 安装失败 | `pip install -e .` |
| `SERVICE_NOT_RUNNING` | PostgreSQL 服务未运行 | `make deploy` |
| `HEALTH_CHECK_FAILED` | 数据库健康检查失败 | 检查 DSN 配置和数据库连接 |
| `CONNECTION_ERROR` | 数据库连接失败 | 检查 `POSTGRES_DSN` 环境变量 |
| `CREATE_ITEM_FAILED` | 创建 item 失败 | 检查数据库 schema |
| `ADD_EVENT_FAILED` | 添加事件失败 | 检查 item_id 是否存在 |
| `ATTACH_FAILED` | 添加附件失败 | 检查参数完整性 |
| `RENDER_VIEWS_FAILED` | 渲染视图失败 | 检查数据库连接和权限 |
| `SMOKE_TEST_PASSED` | 冒烟测试通过 | 成功状态 |

#### Capability 检测错误码（CAP_*）

| 错误码 | 场景 | 修复建议 |
|--------|------|----------|
| `CAP_DOCKER_NOT_FOUND` | Docker CLI 不可用 | 安装 Docker |
| `CAP_DOCKER_DAEMON_DOWN` | Docker daemon 未运行 | `sudo systemctl start docker` |
| `CAP_COMPOSE_NOT_CONFIGURED` | docker-compose.yml 不存在 | 检查项目配置 |
| `CAP_CANNOT_STOP_OPENMEMORY` | 无法停止 OpenMemory 容器 | 检查 Docker 权限 |
| `CAP_PSQL_NOT_FOUND` | psql CLI 不可用 | `brew install postgresql` 或 `apt install postgresql-client` |
| `CAP_PSYCOPG_NOT_FOUND` | psycopg 库不可用 | `pip install psycopg2-binary` |
| `CAP_NO_DB_ACCESS` | 无 DB 访问能力 | 安装 psql 或 psycopg |
| `CAP_POSTGRES_DSN_MISSING` | POSTGRES_DSN 未设置 | `export POSTGRES_DSN="postgresql://..."` |
| `CAP_OPENMEMORY_ENDPOINT_MISSING` | OPENMEMORY_ENDPOINT 未设置 | `export OPENMEMORY_ENDPOINT="http://..."` |

#### Profile 校验错误码（PROF_*）

| 错误码 | 场景 | 修复建议 |
|--------|------|----------|
| `PROF_INVALID` | 无效的 profile 名称 | 使用 http_only/standard/full |
| `PROF_MISSING_CAPABILITY` | 缺少必需的 capability | 参考 CAP_* 错误码修复 |
| `PROF_DEGRADATION_BLOCKED` | 降级测试被阻塞 | 设置 Docker 环境或 SKIP_DEGRADATION_TEST=1 |
| `PROF_DB_INVARIANTS_BLOCKED` | DB 不变量检查被阻塞 | 设置 POSTGRES_DSN 并安装 DB 工具 |

#### Logbook DB 错误码（LOGBOOK_DB_*）

| 错误码 | 场景 | 修复建议 |
|--------|------|----------|
| `LOGBOOK_DB_SCHEMA_MISSING` | 必需 schema 不存在 | `engram-migrate --dsn "$POSTGRES_DSN"` |
| `LOGBOOK_DB_TABLE_MISSING` | 必需表不存在 | `engram-migrate --dsn "$POSTGRES_DSN"` |
| `LOGBOOK_DB_INDEX_MISSING` | 必需索引不存在 | `engram-migrate --dsn "$POSTGRES_DSN"` |
| `LOGBOOK_DB_MATVIEW_MISSING` | 必需物化视图不存在 | `engram-migrate --dsn "$POSTGRES_DSN"` |
| `LOGBOOK_DB_STRUCTURE_INCOMPLETE` | DB 结构不完整 | `engram-migrate --dsn "$POSTGRES_DSN"` |
| `LOGBOOK_DB_MIGRATE_NOT_AVAILABLE` | 迁移模块不可用 | `pip install -e .` |
| `LOGBOOK_DB_MIGRATE_FAILED` | 迁移执行失败 | 检查数据库权限和连接 |
| `LOGBOOK_DB_MIGRATE_PARTIAL` | 迁移部分完成 | 检查迁移日志，手动修复 |
| `LOGBOOK_DB_CONNECTION_FAILED` | 数据库连接失败 | 检查 DSN 格式和网络 |
| `LOGBOOK_DB_CHECK_FAILED` | DB 检查执行失败 | 检查数据库状态和权限 |

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [00_overview.md](00_overview.md) | Logbook 概览 |
| [01_architecture.md](01_architecture.md) | 架构与 URI 规范 |
| [02_tools_contract.md](02_tools_contract.md) | 工具契约 |
| [03_deploy_verify_troubleshoot.md](03_deploy_verify_troubleshoot.md) | 部署排错（含部署级别与验收能力定义） |
| [05_definition_of_done.md](05_definition_of_done.md) | Definition of Done |
| [gateway_logbook_boundary.md](../contracts/gateway_logbook_boundary.md) | Gateway ↔ Logbook 边界契约 |
| [outbox_lease_v2.md](../contracts/outbox_lease_v2.md) | Outbox 租约协议 |
| [evidence_packet.md](../contracts/evidence_packet.md) | 证据包契约 |
| [00_acceptance_matrix.md](../acceptance/00_acceptance_matrix.md) | 验收测试矩阵 |
| [integrate_existing_project.md](../guides/integrate_existing_project.md) | 项目集成指南（含部署级别选择） |

---

更新时间：2026-01-30

> **变更记录**：
> - 2026-01-30: 添加"Logbook-only 必需 SQL 文件"章节，包含 SQL 文件适用模式一览、最小 SQL 集定义、Seek 相关脚本处理策略
> - 2026-01-30: 添加 FULL Profile 验收要求章节，包含 db_invariants 必须失败条件和 LogbookDBErrorCode 错误码映射
