# Optional Layers 接入规范

本文档定义 Engram 可选层（Optional Layer）的接入约束、目录结构、开关命名、验证语义以及 Compose Override 最佳实践。

> **参考实现**：SeekDB 是当前唯一的可选层实现，本文档以 SeekDB 为样例说明。

---

## 1. 设计原则与约束

### 1.1 核心约束

| 约束 | 说明 | 验证方式 |
|------|------|----------|
| **不阻塞 SoT** | 可选层故障不影响 Logbook 事件写入、Gateway 记忆卡片存储 | `<LAYER>_ENABLE=0` 时核心验收通过 |
| **可重建** | 可选层数据可从 SoT 层完全重建，丢失不影响数据完整性 | Nightly 重建验证 |
| **只做指针** | 证据原文以 Logbook/制品为准，可选层只存索引与指针 | 角色权限审计 |
| **显式启用** | 通过 `<LAYER>_ENABLE` 环境变量控制，默认可启用（`1`） | Makefile 检查 |

### 1.2 数据流约束

```
┌─────────────────────────────────────────────────────────────────────┐
│                        数据流向约束                                   │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌──────────────┐        读取          ┌──────────────────┐         │
│  │  SoT 层      │ ◀──────────────────  │  Optional Layer  │         │
│  │  (Logbook)   │                      │  (SeekDB 等)     │         │
│  └──────────────┘                      └──────────────────┘         │
│         │                                      │                    │
│         │                                      │ 禁止写入           │
│         ▼                                      ▼                    │
│  ┌──────────────┐                      ┌──────────────────┐         │
│  │ 事件/制品    │                      │ 索引/指针        │         │
│  │ (真实数据)   │                      │ (可重建数据)     │         │
│  └──────────────┘                      └──────────────────┘         │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

**规则**：
1. 可选层**只读**访问 SoT 层（Logbook schema）
2. 可选层**禁止写入** SoT 层（例外：有限的 `logbook.kv` namespace）
3. 可选层数据**可丢失**，从 SoT 层重建

---

## 2. 目录结构规范

### 2.1 推荐目录结构

```
apps/<layer_name>_<suffix>/
├── sql/
│   ├── 01_<layer>_roles_and_grants.sql    # 角色与权限
│   └── 02_<layer>_<feature>.sql           # 功能表结构
├── scripts/
│   └── <layer>_cli.py                     # CLI 工具（可选）
├── tests/
│   ├── test_<layer>_unit.py               # 单元测试
│   └── test_<layer>_integration.py        # 集成测试
└── README.md                              # 组件说明
```

### 2.2 Compose Override 文件

```
docker-compose.unified.<layer>.yml         # 可选层 override compose
```

**命名规则**：
- 主 Compose：`docker-compose.unified.yml`
- 可选层 Override：`docker-compose.unified.<layer>.yml`
- 目标项目重命名：`docker-compose.engram.<layer>.yml`

---

## 3. 开关变量命名规范

### 3.1 环境变量命名

| 类型 | 命名模式 | 示例 | 说明 |
|------|----------|------|------|
| **启用开关** | `<LAYER>_ENABLE` | `SEEKDB_ENABLE` | `0`=禁用，`1`=启用（默认） |
| **迁移密码** | `<LAYER>_MIGRATOR_PASSWORD` | `SEEKDB_MIGRATOR_PASSWORD` | DDL 迁移账号密码 |
| **服务密码** | `<LAYER>_SVC_PASSWORD` | `SEEKDB_SVC_PASSWORD` | 运行时 DML 账号密码 |
| **Schema 名** | `<LAYER>_PG_SCHEMA` | `SEEKDB_PG_SCHEMA` | PostgreSQL schema（默认与层名一致） |
| **DSN 连接** | `<LAYER>_PGVECTOR_DSN` | `SEEKDB_PGVECTOR_DSN` | 显式 DSN 连接字符串 |

### 3.2 GUC 变量命名

PostgreSQL GUC（Grand Unified Configuration）变量命名：

| 类型 | 命名模式 | 示例 | 说明 |
|------|----------|------|------|
| **启用开关** | `<layer>.enabled` | `seek.enabled` | 在 SQL 脚本中控制验证 |
| **目标 Schema** | `<layer>.target_schema` | `seek.target_schema` | 指定目标 schema |

> **注意**：GUC 变量作为稳定接口，命名可与环境变量前缀不同（如 `seek.*` vs `SEEKDB_*`）。

### 3.3 Deprecated 别名规范

当重命名变量时，需提供兼容窗口：

```makefile
# Makefile 中的 deprecation 处理示例
<LAYER>_ENABLE_EFFECTIVE := $(or $(<LAYER>_ENABLE),$(<OLD_LAYER>_ENABLE),1)

# 输出 deprecation 提示
@if [ -n "$${<OLD_LAYER>_ENABLE}" ] && [ -z "$${<LAYER>_ENABLE}" ]; then \
    echo "[DEPRECATION WARNING] <OLD_LAYER>_ENABLE 已废弃，请迁移到 <LAYER>_ENABLE"; \
fi
```

---

## 4. verify/acceptance 的 SKIP/FAIL/WARN 语义

### 4.1 语义定义

| 语义 | 输出标记 | 含义 | 退出码 |
|------|----------|------|--------|
| **PASS** | `[OK]` / `[PASS]` | 检查通过 | `0` |
| **SKIP** | `[SKIP]` | 检查被跳过（功能禁用） | `0` |
| **WARN** | `[WARN]` | 检查通过但有警告（降级可用） | `0` |
| **FAIL** | `[FAIL]` / `[ERROR]` | 检查失败 | `1` |

### 4.2 SKIP 触发条件

当 `<LAYER>_ENABLE=0` 时，以下操作应输出 `[SKIP]`：

| 操作类型 | SKIP 消息示例 | 触发条件 |
|----------|---------------|----------|
| **迁移** | `[SKIP] <Layer> 迁移已跳过 (<LAYER>_ENABLE != 1)` | `<LAYER>_ENABLE=0` |
| **权限验证** | `[SKIP] <Layer> 权限验证已跳过` | `<LAYER>_ENABLE=0` |
| **单元测试** | `SKIPPED (<Layer> disabled)` | `<LAYER>_ENABLE=0` |
| **集成测试** | `[SKIP] <Layer> 集成测试已跳过` | `<LAYER>_ENABLE=0` |

### 4.3 verify-permissions 注入机制

`99_verify_permissions.sql` 通过 GUC 变量控制可选层验证：

```sql
-- 设置可选层启用状态（通过 psql -c 注入）
SET <layer>.enabled = 'true';   -- 或 'false'

-- 在验证脚本中检查
DO $$
BEGIN
  IF current_setting('<layer>.enabled', true) = 'true' THEN
    -- 执行可选层验证
    RAISE NOTICE 'PASS: <Layer> 权限验证通过';
  ELSE
    RAISE NOTICE 'SKIP: <Layer> 权限验证已跳过 (<layer>.enabled = false)';
  END IF;
END
$$;
```

### 4.4 Makefile 中的 SKIP 实现

```makefile
verify-permissions: ## 验证数据库权限配置
    @$(DOCKER_COMPOSE) exec -T postgres \
        psql -U postgres -d $${POSTGRES_DB:-engram} \
        -c "SET <layer>.enabled = '$(if $(filter 1,$(<LAYER>_ENABLE_EFFECTIVE)),true,false)'" \
        -f /docker-entrypoint-initdb.d/99_verify_permissions.sql
```

### 4.5 pytest 中的 SKIP 实现

```python
import os
import pytest

LAYER_ENABLED = os.environ.get("<LAYER>_ENABLE", "1") == "1"

@pytest.mark.skipif(not LAYER_ENABLED, reason="<Layer> disabled (<LAYER>_ENABLE=0)")
def test_layer_feature():
    ...
```

---

## 5. Compose Override 最佳实践

### 5.1 Override Compose 文件结构

可选层的 override compose 文件**仅包含该层特有的配置**：

```yaml
# docker-compose.unified.<layer>.yml
services:
  postgres:
    volumes:
      # 可选层 SQL 脚本挂载
      - ./<layer_path>/sql/01_<layer>_roles_and_grants.sql:/sql/06_<layer>_roles.sql:ro
      - ./<layer_path>/sql/02_<layer>_feature.sql:/sql/09_<layer>_feature.sql:ro

  bootstrap_roles:
    volumes:
      # 角色初始化脚本挂载
      - ./<layer_path>/sql/01_<layer>_roles_and_grants.sql:/sql/06_<layer>_roles.sql:ro
```

### 5.2 SQL 脚本编号规范

| 编号范围 | 用途 | 示例 |
|----------|------|------|
| `01-05` | Logbook 核心 schema | `01_bootstrap.sql` |
| `06-08` | 可选层角色/权限 | `06_seekdb_roles_and_grants.sql` |
| `09-10` | 可选层功能表 | `09_seekdb_index.sql` |
| `99` | 统一权限验证 | `99_verify_permissions.sql` |

### 5.3 启用/禁用命令

```bash
# 启用可选层（叠加 override compose）
docker compose -f docker-compose.unified.yml -f docker-compose.unified.<layer>.yml up -d
# 或通过 Makefile:
make deploy  # 默认启用

# 禁用可选层（仅使用主 compose 文件）
docker compose -f docker-compose.unified.yml up -d
# 或通过 Makefile:
<LAYER>_ENABLE=0 make deploy
```

### 5.4 主 Compose 文件约束

**关键规则**：主 `docker-compose.unified.yml` **不应引用可选层路径**。

| 约束 | 说明 |
|------|------|
| 无硬依赖 | 主 compose 不包含可选层的 volume bind |
| 无路径引用 | 主 compose 不引用 `apps/<layer>/` 目录 |
| 独立可用 | 禁用可选层时无需复制任何可选层文件 |

---

## 6. SeekDB 参考实现

### 6.1 文件结构

```
apps/seekdb_rag_hybrid/
├── sql/
│   ├── 01_seekdb_roles_and_grants.sql     # SeekDB 角色与权限
│   └── 02_seekdb_index.sql                # chunks 表与向量索引
├── scripts/
│   └── seek_*.py                          # SeekDB CLI 工具
└── tests/
    └── test_seek_*.py                     # SeekDB 测试

docker-compose.unified.seekdb.yml          # SeekDB override compose
```

### 6.2 docker-compose.unified.seekdb.yml 解析

```yaml
# SeekDB override compose - 仅包含 SeekDB 特有配置
services:
  postgres:
    volumes:
      # SeekDB SQL 脚本（供 migrate-seekdb 使用）
      - ./apps/seekdb_rag_hybrid/sql/01_seekdb_roles_and_grants.sql:/sql/06_seekdb_roles_and_grants.sql:ro
      - ./apps/seekdb_rag_hybrid/sql/02_seekdb_index.sql:/sql/09_seekdb_index.sql:ro

  bootstrap_roles:
    volumes:
      # SeekDB 权限脚本
      - ./apps/seekdb_rag_hybrid/sql/01_seekdb_roles_and_grants.sql:/sql/06_seekdb_roles_and_grants.sql:ro
```

**设计要点**：
- 仅定义 `volumes` 覆盖，不重复定义服务
- SQL 脚本编号 `06_`/`09_` 确保在 Logbook 之后、verify 之前执行
- `bootstrap_roles` 服务同样挂载权限脚本

### 6.3 SEEKDB_ENABLE 控制流

```
SEEKDB_ENABLE
     │
     ▼
┌────────────────┐
│ Makefile       │
│ (COMPOSE_FILE) │
└────────────────┘
     │
     ├─ SEEKDB_ENABLE=1 ──▶ COMPOSE_FILE = unified.yml + unified.seekdb.yml
     │
     └─ SEEKDB_ENABLE=0 ──▶ COMPOSE_FILE = unified.yml
     │
     ▼
┌────────────────┐
│ migrate        │
└────────────────┘
     │
     ├─ SEEKDB_ENABLE=1 ──▶ 执行 migrate-seek
     │
     └─ SEEKDB_ENABLE=0 ──▶ [SKIP] SeekDB 迁移已跳过
     │
     ▼
┌────────────────┐
│ verify-permissions │
└────────────────┘
     │
     ├─ SEEKDB_ENABLE=1 ──▶ SET seek.enabled = 'true' → 执行 SeekDB 验证
     │
     └─ SEEKDB_ENABLE=0 ──▶ SET seek.enabled = 'false' → [SKIP] SeekDB 验证
```

### 6.4 verify-unified 中的 SeekDB 处理

Makefile 中的 `verify-unified` 目标：

```makefile
verify-unified: ## 统一栈验证
    @echo "SeekDB 启用: $(SEEKDB_ENABLE_EFFECTIVE)"
    @SEEKDB_ENABLE=$(SEEKDB_ENABLE_EFFECTIVE) \
     ./apps/openmemory_gateway/scripts/verify_unified_stack.sh --mode default
```

验证脚本根据 `SEEKDB_ENABLE` 决定：
- `SEEKDB_ENABLE=1`：检查 SeekDB 服务健康、索引状态
- `SEEKDB_ENABLE=0`：跳过 SeekDB 相关检查，输出 `[SKIP]`

---

## 7. 新增可选层检查清单

当新增可选层时，请按以下清单逐项完成：

### 7.1 代码与配置

- [ ] **目录结构**
  - [ ] 创建 `apps/<layer_name>_<suffix>/` 目录
  - [ ] 添加 `sql/01_<layer>_roles_and_grants.sql`
  - [ ] 添加 `sql/02_<layer>_<feature>.sql`
  - [ ] 添加 `README.md`

- [ ] **Compose Override**
  - [ ] 创建 `docker-compose.unified.<layer>.yml`
  - [ ] 仅包含可选层特有的 volume bind
  - [ ] 确保主 compose 无可选层路径引用

- [ ] **环境变量**
  - [ ] 定义 `<LAYER>_ENABLE`（启用开关）
  - [ ] 定义 `<LAYER>_MIGRATOR_PASSWORD`（可选）
  - [ ] 定义 `<LAYER>_SVC_PASSWORD`（可选）
  - [ ] 定义 `<LAYER>_PG_SCHEMA`（可选）

### 7.2 Makefile 目标

- [ ] **Compose 文件叠加**
  - [ ] 在 Makefile 顶部定义 `<LAYER>_ENABLE_EFFECTIVE`
  - [ ] 更新 `COMPOSE_FILE` 逻辑以支持新层

- [ ] **迁移目标**
  - [ ] 添加 `migrate-<layer>` 目标
  - [ ] 添加 `_migrate-<layer>-conditional` 目标（条件执行）
  - [ ] 更新 `migrate` 目标依赖链

- [ ] **验证目标**
  - [ ] 更新 `verify-permissions` 注入 `<layer>.enabled` GUC
  - [ ] 更新 `verify-unified` 传递 `<LAYER>_ENABLE`

- [ ] **测试目标**
  - [ ] 添加 `test-<layer>-unit` 目标
  - [ ] 添加 `test-<layer>-integration` 目标（可选）

### 7.3 CI 触发条件

- [ ] **ci.yml**
  - [ ] 添加 `<LAYER>_ENABLE` 环境变量
  - [ ] 添加可选层测试步骤（`continue-on-error: true`）
  - [ ] 添加路径触发条件：`apps/<layer>/**`

- [ ] **nightly.yml**
  - [ ] 添加可选层严格测试（fail-closed）
  - [ ] 添加重建验证步骤（如适用）

### 7.4 Manifest 条目

- [ ] **unified_stack_import_v1.json**
  - [ ] 在 `files.optional` 添加 compose override 条目
  - [ ] 在 `files.optional` 添加 SQL 目录条目
  - [ ] 在 `constraints` 添加 override 使用说明
  - [ ] 在 `env_vars.optional` 添加启用开关

示例条目：

```json
{
  "id": "<layer>_compose_override",
  "description": "<Layer> override compose 文件",
  "source_path": "docker-compose.unified.<layer>.yml",
  "target_path": "docker-compose.engram.<layer>.yml",
  "type": "file",
  "use_case": "启用 <Layer> 时叠加使用"
},
{
  "id": "<layer>_sql",
  "description": "<Layer> SQL 脚本",
  "source_path": "apps/<layer_path>/sql",
  "target_path": "apps/<layer_path>/sql",
  "type": "directory",
  "use_case": "使用 compose override 时必需",
  "note": "禁用 <Layer> 时无需复制"
}
```

### 7.5 文档

- [ ] **组件文档**
  - [ ] 创建 `docs/<layer>/README.md`
  - [ ] 创建 `docs/<layer>/00_overview.md`
  - [ ] 说明可选层定位与 SoT 约束

- [ ] **契约文档**
  - [ ] 创建 `docs/contracts/logbook_<layer>_boundary.md`
  - [ ] 定义数据依赖、禁用开关行为、契约项

- [ ] **架构文档**
  - [ ] 更新 `docs/architecture/naming.md` 添加组件命名
  - [ ] 更新本文档（optional_layers_integration.md）添加新层参考

- [ ] **README.md**
  - [ ] 在 "Optional <Layer>" 章节说明启用/禁用方法
  - [ ] 添加相关文档链接

---

## 8. 相关文档

| 文档 | 说明 |
|------|------|
| [命名规范](naming.md) | 组件命名、环境变量、模块路径规范 |
| [Logbook ↔ SeekDB 边界契约](../contracts/logbook_seekdb_boundary.md) | SeekDB 数据依赖与禁用开关规范 |
| [可选层 Gate 一致性](../seekdb/05_optional_layer_gate_consistency.md) | SeekDB 状态机、Fail-Open/Closed 规则 |
| [SeekDB 概览](../seekdb/00_overview.md) | SeekDB 架构与可选层定位 |
| [项目集成 Manifest](../guides/manifests/unified_stack_import_v1.json) | 统一栈导入清单 |

---

更新时间：2026-01-30
