# Gateway 模块边界与 Import 规则

> 状态: **稳定**  
> 创建日期: 2026-02-01  
> SSOT 文档：本文档是 Gateway 模块边界的单一真相源（SSOT）  
> 相关脚本: `scripts/ci/check_gateway_di_boundaries.py`

---

## 1. 概述

本文档定义 `src/engram/gateway/` 目录下各模块的层级划分、依赖注入边界以及允许/禁止的 import 规则。CI 门禁脚本 `check_gateway_di_boundaries.py` 依据本文档执行静态检查。

**核心原则**：

- 入口层负责创建/获取依赖，并透传给业务层
- handlers/services 禁止直接调用全局获取函数
- 依赖通过 `deps: GatewayDeps` 参数显式传递

---

## 2. 模块层级划分

```
┌─────────────────────────────────────────────────────────────────────┐
│                       ENTRYPOINTS（入口层）                          │
│   职责：HTTP 路由、协议解析、依赖获取、correlation_id 生成            │
│   文件：main.py, app.py, routes.py, middleware.py,                  │
│        lifecycle.py, dependencies.py, entrypoints/                 │
│   允许：get_container(), get_gateway_deps(), generate_correlation_id() │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ 依赖传递（deps, correlation_id）
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       HANDLERS（业务逻辑层）                         │
│   职责：处理业务请求、调用 services、构建响应                         │
│   目录：handlers/                                                   │
│   要求：接收 deps 和 correlation_id 参数，禁止调用全局获取函数         │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ 依赖传递（deps, correlation_id）
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       SERVICES（服务层）                             │
│   职责：封装通用业务逻辑、工具函数、领域服务                           │
│   目录：services/                                                   │
│   要求：接收 deps 参数或仅作为纯函数，禁止调用全局获取函数              │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ 使用
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       INFRA（基础设施层）                            │
│   职责：数据库适配器、外部客户端、配置管理                             │
│   文件：logbook_adapter.py, openmemory_client.py,                   │
│        config.py, container.py, di.py                              │
│   说明：被 deps 封装，不直接被 handlers/services 导入                 │
│   注意：logbook_db.py 已在 v1.0 移除，使用 logbook_adapter 代替       │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.1 入口层文件清单

入口层模块是 DI 边界的"起点"，负责创建/获取依赖并传递给 handlers/services。

| 文件 | 职责 | 允许使用的全局函数 |
|------|------|-------------------|
| `main.py` | 启动预检查、调用 create_app | `get_config()`, `validate_config()` |
| `app.py` | FastAPI 应用创建 | `get_container()`, `get_gateway_deps()` |
| `routes.py` | 路由注册、工具执行 | `get_container()`, `generate_correlation_id()` |
| `middleware.py` | correlation_id 生成 | `generate_correlation_id()` |
| `lifecycle.py` | 依赖预热、容器初始化 | `get_config()`, `get_container()`, `set_container()` |
| `dependencies.py` | FastAPI 依赖函数 | `get_container()`, `get_gateway_deps()` |
| `entrypoints/*.py` | 工具执行入口 | `get_container()`, `get_gateway_deps()` |

### 2.2 扫描范围

CI 脚本仅扫描以下目录：

| 目录 | 说明 |
|------|------|
| `src/engram/gateway/handlers/` | 业务逻辑层 |
| `src/engram/gateway/services/` | 服务层 |

入口层文件不在扫描范围内。

---

## 3. 禁止的 Import 模式

handlers/services 目录下禁止调用以下全局获取函数：

| 禁止调用 | 检测正则 | 原因 | 替代方案 |
|----------|----------|------|----------|
| `get_container()` | `\bget_container\s*\(` | 绕过 DI 层 | `deps` 参数 |
| `get_config()` | `\bget_config\s*\(` | 隐式依赖 | `deps.config` |
| `get_client()` | `\bget_client\s*\(` | 隐式依赖 | `deps.openmemory_client` |
| `get_gateway_deps()` | `\bget_gateway_deps\s*\(` | 应由入口层调用 | `deps` 参数 |
| `logbook_adapter.get_adapter()` | `\blogbook_adapter\.get_adapter\s*\(` | 隐式依赖 | `deps.logbook_adapter` |
| `GatewayDeps.create()` | `\bGatewayDeps\.create\s*\(` | 不应直接创建 | `deps` 参数 |
| `deps is None` | `\bdeps\s+is\s+None\b` | deps 必须由调用方提供 | 移除兼容分支 |
| `generate_correlation_id()` | `\bgenerate_correlation_id\s*\(` | 应由入口层生成 | `correlation_id` 参数 |
| `deps.db` | `\bdeps\.db\b` | 直接访问 db 绕过适配器封装 | `deps.logbook_adapter` |

### 3.1 禁止模式示例

```python
# ❌ 禁止：在 handlers 中直接获取容器
from ..container import get_container
container = get_container()

# ❌ 禁止：在 handlers 中直接获取配置
from ..config import get_config
config = get_config()

# ❌ 禁止：在 handlers 中检查 deps 是否为 None
if deps is None:
    deps = get_gateway_deps()

# ❌ 禁止：在 handlers 中生成 correlation_id
from ..mcp_rpc import generate_correlation_id
correlation_id = generate_correlation_id()
```

---

## 4. 允许的 Import 模式

handlers/services 目录下允许导入以下模块：

| 模块 | 允许导入 | 用途 |
|------|----------|------|
| `di.py` | `GatewayDeps`, `GatewayDepsProtocol`, `RequestContext` | 类型注解和参数 |
| `mcp_rpc.py` | `ErrorData`, `ErrorCategory`, `ErrorReason` | 错误处理 |
| `audit_event.py` | `build_audit_event`, `build_evidence_refs_json` | 审计构建 |
| `policy.py` | `check_policy`, `PolicyResult` | 策略检查 |
| `services/hash_utils.py` | `compute_sha256` | 工具函数 |
| `services/ports.py` | 端口定义 | 类型注解 |
| `api_models.py` | API 模型 | 请求/响应类型 |
| `memory_card.py` | MemoryCard 模型 | 数据结构 |

### 4.1 允许模式示例

```python
# ✅ 允许：导入 DI 类型
from ..di import GatewayDeps, GatewayDepsProtocol

# ✅ 允许：通过 deps 参数获取依赖
async def memory_store_impl(
    payload_md: str,
    correlation_id: str,
    deps: GatewayDeps,
) -> MemoryStoreResponse:
    config = deps.config
    adapter = deps.logbook_adapter
    client = deps.openmemory_client

# ✅ 允许：导入错误处理工具
from ..mcp_rpc import ErrorData, ErrorCategory

# ✅ 允许：导入审计构建工具
from ..audit_event import build_audit_event

# ✅ 允许：导入策略检查工具
from ..policy import check_policy, PolicyResult
```

---

## 5. 例外标记机制

当前处于 v0.9 兼容期，部分 legacy fallback 代码可使用 `# DI-BOUNDARY-ALLOW:` 标记临时豁免。

### 5.1 标记格式

```python
# 行尾注释标记
if deps is None:  # DI-BOUNDARY-ALLOW: v0.9 兼容期 legacy fallback
    deps = get_gateway_deps()

# 上一行注释标记
# DI-BOUNDARY-ALLOW: v0.9 兼容期 legacy fallback，v1.0 移除
if deps is None:
    deps = get_gateway_deps()
```

### 5.2 标记规则

| 规则 | 说明 |
|------|------|
| 必须包含原因 | 标记后需说明原因，如 `v0.9 兼容期 legacy fallback` |
| 临时性质 | 所有标记将在 v1.0 版本移除 |
| 需要 code review | 新增标记需要 reviewer 批准 |

### 5.3 DEPS-DB-ALLOW 豁免标记（草案）

> **状态**：草案（Draft）  
> **目的**：针对 `deps.db` 禁止模式提供细粒度的豁免机制

当确实需要直接访问 `deps.db`（例如 adapter 层内部实现、特殊迁移场景）时，可使用 `# DEPS-DB-ALLOW:` 标记进行豁免。

**标记格式**：

```python
# 行尾注释标记（必须包含 reason、expires、owner）
conn = deps.db  # DEPS-DB-ALLOW: adapter 内部实现需直接访问连接; expires=2026-06-30; owner=@platform-team

# 上一行注释标记
# DEPS-DB-ALLOW: 迁移脚本需要原始连接; expires=2026-03-31; owner=@data-team
conn = deps.db
```

**字段说明**：

| 字段 | 必填 | 格式 | 说明 |
|------|------|------|------|
| `reason` | 是 | 自由文本 | 说明为何需要直接访问 db |
| `expires` | 是 | `YYYY-MM-DD` | 豁免过期日期 |
| `owner` | 是 | `@team` 或 `@user` | 负责人/团队 |

**过期语义**：

- 过期判定：`today > expires` 时视为过期（即 expires 当天仍有效）
- 过期的 DEPS-DB-ALLOW 标记会导致 CI 门禁失败
- 建议最大期限：6 个月（超过需 Tech Lead 审批）

**替代方案建议**：

| 场景 | 推荐方案 |
|------|----------|
| 查询操作 | `deps.logbook_adapter.query(...)` |
| 写入操作 | `deps.logbook_adapter.execute(...)` |
| 事务操作 | `deps.logbook_adapter.transaction(...)` |
| 特殊 port | 定义新的 port 接口（如 `deps.raw_connection_port`）|

### 5.4 v1.0 移除计划

当迁移完成后，可启用 `--disallow-allow-markers` 选项强制移除所有标记：

```bash
# 迁移完成后启用此选项
python scripts/ci/check_gateway_di_boundaries.py --disallow-allow-markers

# 检查 DEPS-DB-ALLOW 标记过期状态
python scripts/ci/check_gateway_di_boundaries.py --check-deps-db-expiry
```

---

## 6. CI 门禁检查

### 6.1 本地验证命令

```bash
# Make 目标（推荐）
make check-gateway-di-boundaries

# 直接调用脚本
python scripts/ci/check_gateway_di_boundaries.py --verbose

# JSON 输出（适合脚本处理）
python scripts/ci/check_gateway_di_boundaries.py --json

# 禁止 allow-markers（迁移完成后）
python scripts/ci/check_gateway_di_boundaries.py --disallow-allow-markers
```

### 6.2 检查输出示例

```
======================================================================
Gateway DI 边界检查
======================================================================

扫描目录:
  - src/engram/gateway/handlers
  - src/engram/gateway/services
扫描文件数: 8

[ERROR] 发现 2 处违规调用:

  src/engram/gateway/handlers/memory_store.py:42
    模式: get_config(
    代码: config = get_config()

  src/engram/gateway/services/audit_service.py:15
    模式: deps is None
    代码: if deps is None:

----------------------------------------------------------------------
违规总数: 2
涉及文件: 2

[FAIL] DI 边界检查失败
```

### 6.3 退出码

| 退出码 | 含义 |
|--------|------|
| 0 | 检查通过，无违规 |
| 1 | 发现违规调用（或存在 DI-BOUNDARY-ALLOW 标记，当 `--disallow-allow-markers` 启用时）|

---

## 7. 常见问题修复

### 7.1 问题：handlers 中调用 get_config()

**错误代码**：

```python
# handlers/memory_store.py
from ..config import get_config

async def memory_store_impl(...):
    config = get_config()  # ❌ 违规
```

**修复方法**：

```python
# handlers/memory_store.py
from ..di import GatewayDeps

async def memory_store_impl(
    ...,
    deps: GatewayDeps,  # ✅ 通过参数获取
):
    config = deps.config  # ✅ 从 deps 获取
```

### 7.2 问题：handlers 中检查 deps is None

**错误代码**：

```python
async def memory_store_impl(..., deps: Optional[GatewayDeps] = None):
    if deps is None:  # ❌ 违规
        deps = get_gateway_deps()
```

**修复方法**：

```python
# 方式 1：移除兼容分支（推荐，v1.0 目标）
async def memory_store_impl(..., deps: GatewayDeps):
    config = deps.config  # deps 为必传参数

# 方式 2：临时使用 allow-marker（v0.9 兼容期）
async def memory_store_impl(..., deps: Optional[GatewayDeps] = None):
    # DI-BOUNDARY-ALLOW: v0.9 兼容期 legacy fallback，v1.0 移除
    if deps is None:
        deps = get_gateway_deps()
```

### 7.3 问题：handlers 中生成 correlation_id

**错误代码**：

```python
from ..mcp_rpc import generate_correlation_id

async def memory_store_impl(...):
    correlation_id = generate_correlation_id()  # ❌ 违规
```

**修复方法**：

```python
async def memory_store_impl(
    ...,
    correlation_id: str,  # ✅ 由入口层生成并传入
):
    # 使用传入的 correlation_id
    audit_event = build_audit_event(..., correlation_id=correlation_id)
```

### 7.4 问题：handlers 中直接访问 deps.db

**错误代码**：

```python
# handlers/memory_store.py
async def memory_store_impl(..., deps: GatewayDeps):
    conn = deps.db  # ❌ 违规：直接访问 db 绕过适配器封装
    cursor = conn.cursor()
```

**修复方法**：

```python
# handlers/memory_store.py
async def memory_store_impl(..., deps: GatewayDeps):
    adapter = deps.logbook_adapter  # ✅ 通过适配器访问数据库
    result = adapter.query(...)
```

**原因**：直接访问 `deps.db` 绕过了 `logbook_adapter` 的封装层，破坏了数据访问的抽象边界。所有数据库操作应通过 `deps.logbook_adapter` 进行，以确保一致的事务管理、审计日志和错误处理。

---

## 8. 相关文档

| 文档 | 说明 |
|------|------|
| [ADR: Gateway 依赖注入与入口边界统一](adr_gateway_di_and_entry_boundary.md) | 设计决策与迁移计划 |
| [v2.0 升级指南](../gateway/upgrade_v2_0_remove_handler_di_compat.md) | Legacy 参数移除与迁移清单 |
| [CI 门禁 Runbook](../dev/ci_gate_runbook.md) | 门禁运行说明与常见修复步骤 |

---

## 9. 变更日志

| 日期 | 版本 | 变更内容 |
|------|------|----------|
| 2026-02-01 | v1.0 | 初始版本，从 ADR 文档中提取为独立 SSOT 文档 |

---

> 更新时间：2026-02-01
