# Gateway Public API Surface 导出项分析

> 版本: v1.7  
> 创建日期: 2026-02-02  
> 更新日期: 2026-02-02  
> 状态: Active  
> 适用于: `src/engram/gateway/public_api.py`
> 向后兼容策略: [gateway_contract_convergence.md §11](../contracts/gateway_contract_convergence.md#11-public-api-向后兼容策略)

## 概述

本文档是 Gateway 公共 API (`public_api.py`) 导出项的单一事实来源 (SSOT)，详细记录：

1. 每个导出项的来源模块
2. 导入时机（import-time vs request-time）
3. 依赖链与外部包依赖
4. 缺失时的行为与降级策略
5. 推荐的导入路径

## Tier 分层定义

| Tier | 名称 | 导入方式 | 稳定性承诺 | 失败时行为 |
|------|------|----------|-----------|-----------|
| **A** | 核心稳定层 | 直接导入 | 主版本内接口不变 | 不适用（无外部依赖） |
| **B** | 可选依赖层 | 延迟导入 | 主版本内接口不变 | `ImportError` + 安装指引 |
| **C** | 便捷/内部层 | 直接导入 | 可能在次版本调整签名 | 不适用（无外部依赖） |

### 稳定性承诺说明

- **Tier A**: Protocol 接口方法签名、数据类字段、错误码常量在主版本内**不会变更**
- **Tier B**: 函数签名和返回类型在主版本内**不会变更**，但依赖模块可能升级
- **Tier C**: 便捷函数签名可能在**次版本**中调整，插件作者应优先使用 Tier A 替代方案

### Tier C 避免原因与替代方案

> **为什么建议避免 Tier C？**
>
> 1. **稳定性承诺弱**：Tier C 符号可能在**次版本**（如 v1.2 → v1.3）中调整签名或默认值
> 2. **隐式逻辑**：便捷函数可能封装内部实现细节（如 UUID 版本、时间戳精度），升级时可能产生意外行为
> 3. **可替代性强**：每个 Tier C 函数都有对应的 Tier A 替代方案，直接使用 Tier A 更透明

| Tier C 符号 | 避免原因 | Tier A 替代方案 |
|-------------|----------|-----------------|
| `create_request_context(...)` | 隐藏了 correlation_id 生成逻辑 | `RequestContext(correlation_id=..., actor_user_id=...)` |
| `create_gateway_deps(...)` | 隐藏了依赖容器初始化逻辑 | `GatewayDeps(config=..., ...)` 或 `GatewayDeps.for_testing(...)` |
| `generate_correlation_id()` | 通常由中间件自动生成 | 插件无需手动调用；如需测试，直接使用固定字符串 |

**替代写法示例**：

```python
# ❌ 避免：使用 Tier C 便捷函数（签名可能在次版本变更）
from engram.gateway.public_api import create_request_context
ctx = create_request_context(actor_user_id="user-001")

# ✅ 推荐：直接使用 Tier A 数据类构造（接口稳定）
from engram.gateway.public_api import RequestContext
ctx = RequestContext(
    correlation_id="corr-abc123",  # 显式指定，便于追踪和测试
    actor_user_id="user-001",
)
```

## 导出项总览

> **权威来源**：导出符号清单以 `src/engram/gateway/public_api.py:__all__` 为唯一权威来源。
>
> **本文档职责**：提供各导出项的详细分析（来源模块、依赖链、失败语义），不独立维护符号清单。
>
> **Tier 分类定义**：参见上方"Tier 分层定义"章节。
>
> **向后兼容策略**：参见 [gateway_contract_convergence.md §11](../contracts/gateway_contract_convergence.md#11-public-api-向后兼容策略)

---

## Tier A: 直接导入（import-time 立即执行）

Tier A 符号在 `import engram.gateway.public_api` 时立即导入，适合核心类型和 Protocol 定义。

### 1. 依赖注入模块 (`di.py`)

| 导出项 | 类型 | 说明 | import-time 行为 |
|--------|------|------|------------------|
| `RequestContext` | dataclass | 请求上下文，封装单次请求的追踪信息 | ✅ 无外部依赖 |
| `GatewayDeps` | dataclass | 依赖容器实现类 | ✅ 无外部依赖 |
| `GatewayDepsProtocol` | Protocol | 依赖容器协议 | ✅ 无外部依赖 |
| `create_gateway_deps` | function | 创建依赖容器的便捷函数 | ✅ 无外部依赖 |
| `create_request_context` | function | 创建请求上下文的便捷函数 | ✅ 无外部依赖 |
| `generate_correlation_id` | function | 生成 correlation_id | ✅ 无外部依赖 |

**依赖链**:
```
di.py
├── uuid (标准库)
├── dataclasses (标准库)
├── datetime (标准库)
└── typing (标准库)
    └── TYPE_CHECKING 块（仅类型检查时）
        ├── config.GatewayConfig
        ├── container.GatewayContainer
        ├── logbook_adapter.LogbookAdapter
        ├── logbook_db.LogbookDatabase
        └── openmemory_client.OpenMemoryClient
```

**环境变量/配置访问**:
- import-time: 无
- 属性访问时（延迟初始化）: `config.get_config()` → 读取环境变量

**缺失时行为**: 不适用（纯 Python 实现，无外部依赖）

### 2. 错误码模块 (`error_codes.py`)

| 导出项 | 类型 | 说明 | import-time 行为 |
|--------|------|------|------------------|
| `McpErrorCode` | class | JSON-RPC 2.0 标准错误码 | ✅ 无外部依赖 |
| `McpErrorCategory` | class | 错误分类常量 | ✅ 无外部依赖 |
| `McpErrorReason` | class | 错误原因码常量 | ✅ 无外部依赖 |

**依赖链**:
```
error_codes.py
├── typing (标准库)
└── try/except ImportError (可选)
    └── engram.logbook.errors.ErrorCode
        └── 失败时使用 stub 类降级
```

**环境变量/配置访问**: 无

**缺失时行为**: 
- `engram.logbook.errors.ErrorCode` 缺失时使用内置 stub 类
- stub 类提供基本的错误码常量定义

### 3. 工具结果错误码模块 (`result_error_codes.py`)

| 导出项 | 类型 | 说明 | import-time 行为 |
|--------|------|------|------------------|
| `ToolResultErrorCode` | class | 工具执行结果错误码 | ✅ 无外部依赖 |

**依赖链**: 纯 Python 类定义，无外部依赖

**环境变量/配置访问**: 无

**缺失时行为**: 不适用

### 4. 服务端口模块 (`services/ports.py`)

| 导出项 | 类型 | 说明 | import-time 行为 |
|--------|------|------|------------------|
| `WriteAuditPort` | Protocol | 审计写入接口 | ✅ 无外部依赖 |
| `UserDirectoryPort` | Protocol | 用户目录接口 | ✅ 无外部依赖 |
| `ActorPolicyConfigPort` | Protocol | Actor 策略配置接口 | ✅ 无外部依赖 |
| `ToolExecutorPort` | Protocol | 工具执行器端口 | ✅ 无外部依赖 |
| `ToolRouterPort` | Protocol | 工具路由器端口 | ✅ 无外部依赖 |
| `ToolDefinition` | dataclass | 工具定义 | ✅ 无外部依赖 |
| `ToolCallContext` | class | 工具调用上下文 | ✅ 无外部依赖 |
| `ToolCallResult` | class | 工具调用结果 | ✅ 无外部依赖 |

**依赖链**:
```
services/ports.py
├── dataclasses (标准库)
└── typing (标准库)
```

**环境变量/配置访问**: 无

**缺失时行为**: 不适用（纯 Protocol/dataclass 定义）

---

## Tier B: 延迟导入（首次访问时才导入）

Tier B 符号通过 `__getattr__` 机制延迟导入，仅在首次访问时才触发底层模块加载。

### Tier B 失败语义（重要）

当 Tier B 符号依赖的模块不可用时，**在 `from ... import` 语句执行时**即触发懒加载并抛出 `ImportError`：

```python
# 示例：engram_logbook 未安装时

# 方式 1：from ... import 直接触发懒加载
from engram.gateway.public_api import LogbookAdapter  # ← 此行直接抛出 ImportError
# ImportError: 无法导入 'LogbookAdapter'（来自 .logbook_adapter）
#
# 原因: No module named 'engram_logbook'
#
# 此功能需要 engram_logbook 模块。
# 请安装：pip install -e ".[full]" 或 pip install engram-logbook

# 方式 2：通过模块属性访问也会触发
import engram.gateway.public_api as api
adapter_cls = api.LogbookAdapter  # ← 此处触发 __getattr__，抛出 ImportError
```

> **技术说明**：Python 的 `from module import name` 语句会调用模块的 `__getattr__(name)`，
> 因此 Tier B 符号的懒加载在 import 语句执行时即被触发，而非延迟到后续使用时。

**错误消息格式**（必须包含以下字段）：

```
ImportError: 无法导入 '{symbol_name}'（来自 {module_path}）

原因: {original_error}

{install_hint}
```

**错误消息字段要求**：

| 字段 | 说明 | 示例 |
|------|------|------|
| `symbol_name` | 导入失败的符号名 | `LogbookAdapter` |
| `module_path` | 来源模块的相对路径 | `.logbook_adapter` |
| `original_error` | 原始 ImportError 的消息文本 | `No module named 'engram_logbook'` |
| `install_hint` | 包含具体安装命令的指引 | `pip install -e ".[full]"` |

**关键约束**：

| 约束 | 说明 |
|------|------|
| **错误类型** | 必须是 `ImportError`（便于调用方 catch） |
| **触发时机** | `from ... import` 或属性访问时**立即触发** |
| **缺失模块名** | 必须在 `original_error` 字段中体现 |
| **安装指引** | 必须包含具体安装命令（如 `pip install -e ".[full]"`） |

### Tier B 符号安全使用模式（可复制代码片段）

插件作者在使用 Tier B 符号时，**必须**使用 try/except 模式检查依赖可用性：

```python
# ══════════════════════════════════════════════════════════════════
# Tier B 符号安全使用模式（可复制）
# ══════════════════════════════════════════════════════════════════

# 1. 导入时检查依赖可用性
try:
    from engram.gateway.public_api import (
        LogbookAdapter,
        get_adapter,
        get_reliability_report,
    )
    LOGBOOK_AVAILABLE = True
except ImportError:
    LOGBOOK_AVAILABLE = False
    LogbookAdapter = None  # type: ignore[misc, assignment]

# 2. 在入口处验证（推荐在插件初始化时检查）
def my_plugin_init() -> None:
    if not LOGBOOK_AVAILABLE:
        raise RuntimeError(
            "此插件需要 engram_logbook 模块。\n"
            '请安装：pip install -e ".[full]" 或 pip install engram-logbook'
        )

# 3. 使用时通过 Protocol 类型注解（避免运行时类型依赖）
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from engram.gateway.public_api import LogbookAdapter

class LogbookAdapterProtocol(Protocol):
    """用于类型注解，避免运行时 import LogbookAdapter"""
    def get_connection(self): ...

async def my_handler(adapter: "LogbookAdapterProtocol") -> dict:
    # 使用 Protocol 类型注解，运行时不依赖 LogbookAdapter 类
    ...
```

**ImportError 消息解析（用于日志/调试）**：

```python
# 解析 Tier B ImportError 消息的各字段
import re

def parse_tier_b_import_error(error_msg: str) -> dict:
    """解析 Tier B ImportError 消息（仅供调试使用）"""
    pattern = r"无法导入 '([^']+)'（来自 ([^)]+)）\n\n原因: (.+?)\n\n(.+)"
    match = re.match(pattern, error_msg, re.DOTALL)
    if match:
        return {
            "symbol_name": match.group(1),
            "module_path": match.group(2),
            "original_error": match.group(3),
            "install_hint": match.group(4),
        }
    return {}
```

### 延迟导入映射表

> **参考**：实际映射表定义在 `src/engram/gateway/public_api.py` 的 `_TIER_B_LAZY_IMPORTS` 变量中。

当前 Tier B 符号包括：

| 符号 | 来源模块 | 外部依赖 |
|------|----------|----------|
| `LogbookAdapter` | `.logbook_adapter` | engram_logbook |
| `get_adapter` | `.logbook_adapter` | engram_logbook |
| `get_reliability_report` | `.logbook_adapter` | engram_logbook |
| `execute_tool` | `.entrypoints.tool_executor` | Gateway 完整依赖 |
| `dispatch_jsonrpc_request` | `.mcp_rpc` | MCP RPC 支持模块 |
| `JsonRpcDispatchResult` | `.mcp_rpc` | MCP RPC 支持模块 |

### 1. Logbook 适配器模块 (`logbook_adapter.py`)

| 导出项 | 类型 | 说明 | 依赖 |
|--------|------|------|------|
| `LogbookAdapter` | class | Logbook 数据库适配器 | ⚠️ 需要 engram_logbook |
| `get_adapter` | function | 获取 LogbookAdapter 单例 | ⚠️ 需要 engram_logbook |
| `get_reliability_report` | function | 获取可靠性统计报告 | ⚠️ 需要 engram_logbook |

**依赖链**:
```
logbook_adapter.py
├── json (标准库)
├── os (标准库)
├── dataclasses (标准库)
├── datetime (标准库)
├── typing (标准库)
└── engram.logbook (必需，import-time 触发)
    ├── engram.logbook.governance
    ├── engram.logbook.outbox
    ├── engram.logbook.config.Config
    ├── engram.logbook.db (get_connection, KnowledgeCandidateRow, etc.)
    └── engram.logbook.errors.DatabaseError
└── try/except ImportError (可选)
    └── engram.logbook.migrate (run_all_checks, run_migrate)
        └── 失败时 _DB_MIGRATE_AVAILABLE=False
└── engram.gateway.config.UnknownActorPolicy (兼容别名)
```

**环境变量/配置访问**:
- `POSTGRES_DSN`: LogbookAdapter 初始化时访问
- `TEST_PG_DSN`: 备选 DSN

**缺失时行为**:
- `engram_logbook` 缺失: `raise ImportError` + 安装指引
- 安装指引: `pip install -e ".[full]"` 或 `pip install engram-logbook`

### 2. 工具执行器模块 (`entrypoints/tool_executor.py`)

| 导出项 | 类型 | 说明 | 依赖 |
|--------|------|------|------|
| `execute_tool` | function | MCP 工具执行入口 | 函数内延迟导入 |

**依赖链**:
```
entrypoints/tool_executor.py
├── logging (标准库)
├── typing (标准库)
└── 函数内延迟导入（execute_tool 调用时）
    ├── ..handlers.execute_evidence_upload
    ├── ..handlers.governance_update_impl
    ├── ..handlers.memory_query_impl
    ├── ..handlers.memory_store_impl
    └── ..logbook_adapter.get_reliability_report
```

**设计原则**: Import-Safe，模块导入时不触发 `get_config()`/`get_container()`

**环境变量/配置访问**: 无（在 execute_tool 调用时通过 `get_deps` 回调获取）

**缺失时行为**: 
- 底层 handler 依赖缺失时返回结构化错误响应
- 示例: `{"ok": false, "error_code": "DEPENDENCY_MISSING", ...}`

---

## 导入时机与依赖矩阵

<!-- public_api_exports:start -->
| 导出项 | 导入时机 | 外部包依赖 | try/except | 环境变量 |
|--------|----------|-----------|------------|----------|
| **Tier A** | | | | |
| `RequestContext` | import-time | 无 | 无 | 无 |
| `GatewayDeps` | import-time | 无 | 无 | 属性访问时 |
| `GatewayDepsProtocol` | import-time | 无 | 无 | 无 |
| `create_request_context` | import-time | 无 | 无 | 无 |
| `create_gateway_deps` | import-time | 无 | 无 | 无 |
| `generate_correlation_id` | import-time | 无 | 无 | 无 |
| `McpErrorCode` | import-time | 无 | 无 | 无 |
| `McpErrorCategory` | import-time | 无 | 无 | 无 |
| `McpErrorReason` | import-time | 无 | 无 | 无 |
| `ToolResultErrorCode` | import-time | 无 | 无 | 无 |
| `WriteAuditPort` | import-time | 无 | 无 | 无 |
| `UserDirectoryPort` | import-time | 无 | 无 | 无 |
| `ActorPolicyConfigPort` | import-time | 无 | 无 | 无 |
| `ToolExecutorPort` | import-time | 无 | 无 | 无 |
| `ToolRouterPort` | import-time | 无 | 无 | 无 |
| `ToolDefinition` | import-time | 无 | 无 | 无 |
| `ToolCallContext` | import-time | 无 | 无 | 无 |
| `ToolCallResult` | import-time | 无 | 无 | 无 |
| **Tier B** | | | | |
| `LogbookAdapter` | 延迟导入 | engram_logbook | raise ImportError | POSTGRES_DSN |
| `get_adapter` | 延迟导入 | engram_logbook | raise ImportError | POSTGRES_DSN |
| `get_reliability_report` | 延迟导入 | engram_logbook | raise ImportError | POSTGRES_DSN |
| `execute_tool` | 延迟导入 | 函数内延迟 | 结构化错误 | 无 |
| `dispatch_jsonrpc_request` | 延迟导入 | MCP RPC 模块 | raise ImportError | 无 |
| `JsonRpcDispatchResult` | 延迟导入 | MCP RPC 模块 | raise ImportError | 无 |
<!-- public_api_exports:end -->

---

## 推荐导入路径

### 插件作者导入策略（重要）

> **核心原则**：优先依赖 Protocol/错误码/数据类，避免直接依赖实现类。

#### 推荐：Tier A 符号（Protocol/错误码）

> **同步说明**：以下导出项与 `src/engram/gateway/public_api.py` 的 `__all__` 列表保持同步，由 `check_gateway_public_api_docs_sync.py` 门禁保障一致性。

```python
from engram.gateway.public_api import (
    # ✅ 核心类型（依赖注入）
    RequestContext,
    GatewayDeps,
    GatewayDepsProtocol,
    
    # ✅ 服务端口 Protocol（依赖抽象，便于测试 mock）
    WriteAuditPort,
    UserDirectoryPort,
    ActorPolicyConfigPort,
    
    # ✅ 工具执行端口
    ToolExecutorPort,
    ToolRouterPort,
    
    # ✅ 工具调用数据类（稳定的接口契约）
    ToolDefinition,
    ToolCallContext,
    ToolCallResult,
    
    # ✅ 错误码常量（用于错误处理）
    McpErrorCode,
    McpErrorCategory,
    McpErrorReason,
    ToolResultErrorCode,
)

# 示例：定义自定义 handler
async def my_handler(
    ctx: RequestContext,
    deps: GatewayDepsProtocol,  # ← 使用 Protocol 而非实现类
) -> dict:
    ...
```

#### 谨慎：Tier B 符号（实现类/依赖外部模块）

> **当前 Tier B 符号列表**（与 `public_api.__all__` 同步）：
> - `LogbookAdapter`, `get_adapter`, `get_reliability_report`（需要 engram_logbook）
> - `execute_tool`（需要 Gateway 完整依赖）
> - `dispatch_jsonrpc_request`, `JsonRpcDispatchResult`（MCP RPC 支持模块）

**可复制代码片段**：

```python
# ══════════════════════════════════════════════════════════════════
# Tier B 符号安全使用模式
# ══════════════════════════════════════════════════════════════════

# ⚠️ Tier B 符号在 import 语句执行时即触发懒加载
# 如果依赖模块不可用，会立即抛出 ImportError

try:
    from engram.gateway.public_api import (
        LogbookAdapter,
        get_adapter,
        get_reliability_report,
    )
    LOGBOOK_AVAILABLE = True
except ImportError as e:
    # ImportError 消息格式（契约保证）：
    # - symbol_name: 导入失败的符号名
    # - module_path: 来源模块路径
    # - original_error: 原始错误消息
    # - install_hint: 安装指引
    LOGBOOK_AVAILABLE = False
    LogbookAdapter = None  # type: ignore[misc, assignment]

# 在插件入口处检查（推荐）
def my_plugin_init() -> None:
    if not LOGBOOK_AVAILABLE:
        raise RuntimeError(
            "此插件需要 engram_logbook 模块。\n"
            '请安装：pip install -e ".[full]" 或 pip install engram-logbook'
        )
```

#### 避免：Tier C 符号（便捷函数）

> **当前 Tier C 符号列表**（与 `public_api.__all__` 同步）：
> - `create_request_context`
> - `create_gateway_deps`
> - `generate_correlation_id`

**避免原因**：Tier C 便捷函数可能在次版本调整签名或默认值，直接使用 Tier A 数据类更稳定。

```python
# ❌ 避免：Tier C 便捷函数（签名可能在 v1.x 次版本变更）
from engram.gateway.public_api import create_request_context
ctx = create_request_context(actor_user_id="user-001")

# ✅ 推荐：直接使用 Tier A 数据类构造（接口稳定）
from engram.gateway.public_api import RequestContext
ctx = RequestContext(
    correlation_id="corr-abc123",  # 显式指定，便于追踪和测试
    actor_user_id="user-001",
)

# ✅ 测试场景：使用固定 correlation_id 便于断言
ctx_for_test = RequestContext(
    correlation_id="test-corr-fixed",  # 测试用固定值
    actor_user_id="test-user",
)
```

### 内部模块（仅限 engram 内部开发）

可直接从定义模块导入：

```python
# 依赖注入
from engram.gateway.di import RequestContext, GatewayDeps

# 错误码
from engram.gateway.error_codes import McpErrorCode, McpErrorReason
from engram.gateway.result_error_codes import ToolResultErrorCode

# 服务端口
from engram.gateway.services.ports import WriteAuditPort, UserDirectoryPort

# Logbook 适配器（仅在需要时）
from engram.gateway.logbook_adapter import LogbookAdapter, get_adapter
```

### ports vs impl 选择指南

| 场景 | 推荐导入 | Tier | 原因 |
|------|----------|------|------|
| 定义 handler 签名 | `GatewayDepsProtocol`, `*Port` | A | 依赖抽象接口，便于测试 mock |
| 测试中 mock 依赖 | `*Port` Protocol | A | Protocol 支持任意实现 |
| 错误处理/分类 | `McpErrorCode`, `McpErrorReason` | A | 标准化错误码 |
| 类型注解（TYPE_CHECKING） | `*Port`, `*Protocol` | A | 避免 import-time 依赖 |
| 生产代码获取实现 | `LogbookAdapter`, `get_adapter` | B | 获取具体实现（需检查依赖） |
| 快速创建上下文 | `RequestContext(...)` | A | 避免 Tier C 便捷函数 |

---

## 契约文档交叉引用

### 与 `mcp_jsonrpc_error_v2.md` 的关联

| public_api 导出项 | 契约文档章节 | 说明 |
|-------------------|-------------|------|
| `McpErrorCode` | §4. JSON-RPC 错误码映射 | 错误码常量定义 |
| `McpErrorCategory` | §2. 错误数据结构 | 错误分类枚举 |
| `McpErrorReason` | §3. 错误分类与原因码 | 原因码常量定义 |
| `ToolResultErrorCode` | §3.0 错误码命名空间边界 | 业务层 result.error_code |

**边界规则**:
- `error.data.reason` 只能使用 `McpErrorReason.*`
- `result.error_code` 只能使用 `ToolResultErrorCode.*`

参见: [mcp_jsonrpc_error_v2.md](../contracts/mcp_jsonrpc_error_v2.md)

### 与 `gateway_contract_convergence.md` 的关联

| public_api 导出项 | 契约文档章节 | 说明 |
|-------------------|-------------|------|
| `WriteAuditPort` | §2. AuditEvent 域 | 审计写入接口契约 |
| `LogbookAdapter` | §1. Gateway-Logbook 桥接模块 | 当前唯一桥接模块 |
| `execute_tool` | §5. MCP 工具路由实现详解 | 工具执行核心入口 |
| `GatewayDeps` | §5.4 工具执行层 | 依赖注入 |
| `RequestContext` | §6. 跨域契约关联 | correlation_id 传递 |

参见: [gateway_contract_convergence.md](../contracts/gateway_contract_convergence.md)

### 与 `gateway_importerror_and_optional_deps.md` 的关联

| public_api 导出项 | 依赖文档章节 | 说明 |
|-------------------|-------------|------|
| `LogbookAdapter` | §3. logbook_adapter.py | 必需依赖 raise ImportError |
| `get_adapter` | §3. logbook_adapter.py | 必需依赖 raise ImportError |
| `execute_tool` | §6. handlers/evidence_upload.py | request-time 延迟导入 |
| `dispatch_jsonrpc_request` | §2. mcp_rpc.py | 可选依赖设置为 None |

参见: [gateway_importerror_and_optional_deps.md](./gateway_importerror_and_optional_deps.md)

---

## 安装指引映射

当 Tier B 符号依赖缺失时，会返回包含安装指引的 ImportError：

| 模块路径 | 安装指引 |
|----------|----------|
| `.logbook_adapter` | `pip install -e ".[full]"` 或 `pip install engram-logbook` |
| `.entrypoints.tool_executor` | `pip install -e ".[full]"` |
| `.mcp_rpc` | `pip install -e ".[full]"` |

---

## 测试验证

### public_api 导出一致性测试

```bash
pytest tests/gateway/test_public_api_exports.py -v
```

### import-time 依赖测试

```bash
# 验证 Tier A/B 分层导入契约
pytest tests/gateway/test_public_api_import_contract.py -v

# 验证 Tier B 错误消息格式
pytest tests/gateway/test_public_api_import_error_message_contract.py -v

# 验证 DI 边界
python scripts/ci/check_gateway_di_boundaries.py --verbose
```

---

## 验收命令

> 本节定义 Gateway Public API 导入契约的最小验收命令集合。在修改 `public_api.py`、`__init__.py` 或相关模块后，应运行以下命令确保契约完整性。

### 变更类型风险评估

| 变更类型 | 风险等级 | 必跑命令 | CI Job 覆盖 |
|----------|----------|----------|-------------|
| 新增 Tier A 符号 | 🟢 低 | `check_gateway_public_api_import_surface.py` + `test_public_api_exports.py` | `gateway-public-api-surface`, `test` |
| 新增 Tier B 符号 | 🟡 中 | 全部验收命令 | `gateway-public-api-surface`, `gateway-import-surface`, `test` |
| 修改 `__getattr__` 懒加载逻辑 | 🔴 高 | 全部验收命令 + 手动测试 | 全部 Gateway 检查 job |
| 修改 Tier B 符号签名 | 🔴 高 | 全部验收命令 + 相关功能测试 | 全部 |
| 移除任何符号 | ⚫ 极高 | 禁止（破坏性变更，需走废弃流程） | N/A |
| 修改 Tier A 接口签名 | ⚫ 极高 | 禁止（破坏性变更） | N/A |

### 最小验收命令集

> **推荐**：使用 Makefile 目标运行，避免命令散落和参数不一致。

```bash
# 1. public_api.py Tier B 延迟导入策略检查（禁止 eager-import）
make check-gateway-public-api-surface

# 2. gateway __init__.py 懒加载策略检查
make check-gateway-import-surface

# 3. Gateway Public API 代码与文档同步检查
make check-gateway-public-api-docs-sync

# 4. Gateway 测试（包含 public_api 契约测试）
make test-gateway
```

### 单行执行（CI 集成）

```bash
# 通过 Makefile 目标执行（推荐）
make check-gateway-public-api-surface check-gateway-import-surface check-gateway-public-api-docs-sync

# 或运行完整 CI 检查
make ci
```

### 单独运行 public_api 相关测试

如需单独运行 public_api 契约测试（不通过 `make test-gateway`）：

```bash
pytest tests/gateway/test_public_api_import_contract.py \
       tests/gateway/test_public_api_import_error_message_contract.py \
       tests/gateway/test_import_safe_entrypoints.py -q
```

### 命令说明

| 命令 | 检查范围 | 失败原因 |
|------|----------|----------|
| `check_gateway_public_api_import_surface.py` | `public_api.py` 不包含 Tier B 模块的 eager-import | Tier B 符号（LogbookAdapter, execute_tool 等）被直接导入而非通过 `__getattr__` 懒加载 |
| `check_gateway_import_surface.py` | `__init__.py` 不包含重量级子模块的 eager-import | logbook_adapter, openmemory_client 等模块被直接导入 |
| `test_public_api_import_contract.py` | Tier A 符号在 logbook_adapter 缺失时可正常导入；Tier B 符号抛出带安装指引的 ImportError | Tier A/B 分层策略实现错误 |
| `test_import_safe_entrypoints.py` | gateway.main, app, routes, middleware 导入时不触发 get_config()/get_container() | 模块级别代码触发了配置加载 |

### CI Job 对应关系

| 验收命令 | CI Job | 覆盖方式 |
|----------|--------|----------|
| `check_gateway_public_api_import_surface.py` | `gateway-public-api-surface` | 专属 job（`.github/workflows/ci.yml` 第 457-475 行） |
| `check_gateway_import_surface.py` | `gateway-import-surface` | 专属 job（`.github/workflows/ci.yml` 第 434-453 行） |
| `test_public_api_import_contract.py` | `test` | `pytest tests/gateway/` 覆盖（第 102 行） |
| `test_import_safe_entrypoints.py` | `test` | `pytest tests/gateway/` 覆盖（第 102 行） |
| `check_gateway_di_boundaries.py` | `gateway-di-boundaries` | 专属 job（`.github/workflows/ci.yml` 第 364-382 行） |

> **注意**：所有测试文件 `tests/gateway/test_*.py` 均由 `test` job 通过 `pytest tests/gateway/ -v` 自动覆盖。

### 相关 Makefile 目标

```bash
# 可选：通过 Makefile 运行 DI 边界检查（包含更广泛的 gateway 检查）
make check-gateway-di-boundaries
```

---

## 维护者：新增符号模板

> **目标读者**：需要在 `public_api.py` 中新增导出符号的维护者。
> **前置知识**：请先阅读本文档的 [Tier 分层定义](#tier-分层定义) 章节。

本章提供按 Tier 分类的新增符号检查清单，确保代码、文档、测试三者同步更新。

### Tier A：新增核心稳定层符号

**适用场景**：新增 Protocol、dataclass、错误码常量等无外部依赖的符号。

#### 代码改动清单

| 文件 | 改动位置 | 说明 |
|------|----------|------|
| `src/engram/gateway/public_api.py` | `__all__` 列表 | 添加新符号名称 |
| `src/engram/gateway/public_api.py` | 模块顶部 import | 添加 `from .xxx import NewSymbol` |

#### 文档更新清单

| 文档 | 更新位置 | 说明 |
|------|----------|------|
| 本文档 (`gateway_public_api_surface.md`) | [Tier A 符号表](#1-依赖注入模块-dipy) | 添加新符号行 |
| 本文档 (`gateway_public_api_surface.md`) | [导入时机与依赖矩阵](#导入时机与依赖矩阵) | 添加矩阵行 |
| `gateway_contract_convergence.md` | §5.6 公共 API 导出 | 如为 Protocol/Port，更新表格 |

#### 测试更新清单

| 测试文件 | 更新内容 | 说明 |
|----------|----------|------|
| `tests/gateway/test_public_api_exports.py` | `TIER_A_SYMBOLS` 集合 | 添加新符号 |
| `tests/gateway/test_public_api_import_contract.py` | Tier A 导入测试 | 验证无外部依赖导入 |

#### 验收命令

```bash
make check-gateway-public-api-surface && make check-gateway-public-api-docs-sync
```

---

### Tier B：新增可选依赖层符号

**适用场景**：新增需要外部依赖（如 engram_logbook）的实现类或函数。

#### 代码改动清单

| 文件 | 改动位置 | 说明 |
|------|----------|------|
| `src/engram/gateway/public_api.py` | `__all__` 列表 | 添加新符号名称 |
| `src/engram/gateway/public_api.py` | `_TIER_B_LAZY_IMPORTS` 字典 | 添加 `"NewSymbol": ".source_module"` |
| `src/engram/gateway/public_api.py` | `_TIER_B_INSTALL_HINTS` 字典 | 添加 `".source_module": "pip install ..."` |

**⚠️ 禁止**：不要在模块顶部直接 import Tier B 符号，必须通过 `__getattr__` 懒加载。

#### 文档更新清单

| 文档 | 更新位置 | 说明 |
|------|----------|------|
| 本文档 (`gateway_public_api_surface.md`) | [延迟导入映射表](#延迟导入映射表) | 添加新符号行 |
| 本文档 (`gateway_public_api_surface.md`) | [导入时机与依赖矩阵](#导入时机与依赖矩阵) | 添加矩阵行 |
| 本文档 (`gateway_public_api_surface.md`) | [安装指引映射](#安装指引映射) | 如为新模块，添加安装指引 |

#### 测试更新清单

| 测试文件 | 更新内容 | 说明 |
|----------|----------|------|
| `tests/gateway/test_public_api_exports.py` | `TIER_B_SYMBOLS` 集合 | 添加新符号 |
| `tests/gateway/test_public_api_import_contract.py` | Tier B 导入测试 | 验证懒加载和 ImportError 消息 |
| `tests/gateway/test_public_api_import_error_message_contract.py` | 错误消息测试 | 验证安装指引格式 |

#### subprocess 阻断测试模板

新增 Tier B 符号时，建议添加 subprocess 隔离测试，确保在依赖缺失时正确抛出 ImportError：

```python
# tests/gateway/test_public_api_import_contract.py 中添加

def test_new_tier_b_symbol_import_error_subprocess():
    """验证 NewSymbol 在依赖缺失时的 ImportError 消息（subprocess 隔离）"""
    import subprocess
    import sys

    code = '''
import sys
# 模拟依赖缺失
sys.modules["external_dependency"] = None

try:
    from engram.gateway.public_api import NewSymbol
    print("FAIL: should raise ImportError")
    sys.exit(1)
except ImportError as e:
    msg = str(e)
    # 验证消息格式
    assert "NewSymbol" in msg, f"Missing symbol name: {msg}"
    assert ".source_module" in msg, f"Missing module path: {msg}"
    assert "pip install" in msg, f"Missing install hint: {msg}"
    print("PASS")
    sys.exit(0)
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, f"Test failed: {result.stderr}"
```

#### 验收命令

```bash
make check-gateway-public-api-surface && \
make check-gateway-import-surface && \
make check-gateway-public-api-docs-sync
```

---

### Tier C：新增便捷/内部层符号

**适用场景**：新增便捷工厂函数等可能在次版本调整签名的符号。

#### 代码改动清单

| 文件 | 改动位置 | 说明 |
|------|----------|------|
| `src/engram/gateway/public_api.py` | `__all__` 列表 | 添加新符号名称 |
| `src/engram/gateway/public_api.py` | 模块顶部 import | 添加 `from .xxx import new_function` |

#### 文档更新清单

| 文档 | 更新位置 | 说明 |
|------|----------|------|
| 本文档 (`gateway_public_api_surface.md`) | [Tier C 避免原因与替代方案](#tier-c-避免原因与替代方案) | 添加新符号、避免原因、替代方案 |
| 本文档 (`gateway_public_api_surface.md`) | [导入时机与依赖矩阵](#导入时机与依赖矩阵) | 添加矩阵行 |

#### 测试更新清单

| 测试文件 | 更新内容 | 说明 |
|----------|----------|------|
| `tests/gateway/test_public_api_exports.py` | `TIER_C_SYMBOLS` 集合 | 添加新符号 |

#### 验收命令

```bash
make check-gateway-public-api-surface && make check-gateway-public-api-docs-sync
```

---

### 完整验收命令集（所有 Tier）

修改 `public_api.py` 后，运行以下 Makefile 目标确保契约完整性：

```bash
# 推荐：通过 Makefile 目标运行
make check-gateway-public-api-surface   # Tier B 懒加载策略检查
make check-gateway-import-surface       # __init__.py 懒加载检查
make check-gateway-public-api-docs-sync # 代码与文档同步检查

# 测试验证（需要 pytest）
make test-gateway  # 运行所有 Gateway 测试（包含 public_api 契约测试）

# 或单独运行 public_api 相关测试
pytest tests/gateway/test_public_api_*.py tests/gateway/test_import_safe_entrypoints.py -q
```

---

### 拆分模块 Checklist

> **适用场景**：当需要将现有模块拆分为多个子模块，或将符号移动到新模块时。
> **核心原则**：模块路径是契约的一部分，详见 [gateway_contract_convergence.md §11.6.0](../contracts/gateway_contract_convergence.md#1160-模块路径是契约的一部分)

#### 拆分前评估

| 评估项 | 检查内容 | 决策 |
|--------|----------|------|
| **外部引用分析** | 符号是否仅通过 `public_api.py` 导出？ | 是 → 策略 B；否 → 策略 A |
| **Tier 分类** | 符号属于 Tier A/B/C？ | Tier A/B 需保持路径稳定 |
| **依赖方影响** | 是否有已知的外部插件直接引用旧模块？ | 有 → 必须保留 shim |

#### 策略 A Checklist：保留旧模块为 Re-export Shim

**适用**：旧模块路径有外部直接引用（如 `from engram.gateway.di import generate_correlation_id`）

| 步骤 | 文件 | 操作 | 验证命令 |
|------|------|------|----------|
| 1 | 新模块 | 创建新模块，包含实际实现 | - |
| 2 | 旧模块 | 改为 shim：`from .new_module import X as _X` + DeprecationWarning | `make lint` |
| 3 | 旧模块 | 保持 `__all__` 导出（包含废弃符号） | - |
| 4 | `public_api.py` | 更新内部 import 指向新模块 | `make check-gateway-public-api-surface` |
| 5 | 文档 | 更新 `gateway_public_api_surface.md` 符号来源模块 | `make check-gateway-public-api-docs-sync` |
| 6 | 测试 | 添加废弃警告测试（见下方模板） | `make test-gateway` |

#### 策略 B Checklist：仅更新 public_api 内部导入

**适用**：符号仅通过 `public_api.py` 导出，无外部直接引用

| 步骤 | 文件 | 操作 | 验证命令 |
|------|------|------|----------|
| 1 | 新模块 | 创建新模块，包含实际实现 | - |
| 2 | `public_api.py` | Tier A：更新顶部 import 路径 | `make check-gateway-public-api-surface` |
| 3 | `public_api.py` | Tier B：更新 `_TIER_B_LAZY_IMPORTS` 字典 | `make check-gateway-public-api-surface` |
| 4 | 文档 | 更新 `gateway_public_api_surface.md` 符号来源模块 | `make check-gateway-public-api-docs-sync` |
| 5 | 测试 | 验证导入路径稳定性（见下方模板） | `make test-gateway` |
| 6 | 旧模块 | 如无其他引用，可删除旧模块 | `make lint` |

#### 最小测试模板：验证旧路径导入兼容性

以下测试模板用于验证模块拆分后旧导入路径仍然可用，类型和行为保持不变：

```python
# tests/gateway/test_module_split_compat.py（模板，按需添加具体符号）

"""模块拆分向后兼容性测试模板

用于验证：
1. 旧导入路径仍可导入（DeprecationWarning 可接受）
2. 新旧路径导入的符号是同一对象
3. 类型签名/行为保持不变
"""

import warnings
import pytest


class TestModuleSplitCompat:
    """模块拆分兼容性测试基类"""

    def test_old_path_still_importable(self):
        """验证旧路径仍可导入（可能有 DeprecationWarning）"""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            
            # 示例：验证旧路径可导入
            # from engram.gateway.di import generate_correlation_id
            
            # 如果有废弃警告，验证消息格式
            # if w:
            #     assert issubclass(w[-1].category, DeprecationWarning)
            #     assert "已废弃" in str(w[-1].message) or "deprecated" in str(w[-1].message).lower()

    def test_old_and_new_path_same_object(self):
        """验证新旧路径导入的是同一对象"""
        # 示例：
        # from engram.gateway.di import generate_correlation_id as old_func
        # from engram.gateway.correlation_id import generate_correlation_id as new_func
        # assert old_func is new_func or old_func() == new_func()  # 根据符号类型选择

    def test_type_signature_unchanged(self):
        """验证类型签名保持不变"""
        import inspect
        
        # 示例：验证函数签名
        # from engram.gateway.correlation_id import generate_correlation_id
        # sig = inspect.signature(generate_correlation_id)
        # assert sig.return_annotation == str
        # assert list(sig.parameters.keys()) == []  # 无参数

    def test_behavior_unchanged(self):
        """验证行为保持不变"""
        # 示例：验证返回值格式
        # from engram.gateway.correlation_id import generate_correlation_id
        # result = generate_correlation_id()
        # assert result.startswith("corr-")
        # assert len(result) == 21


# subprocess 隔离测试（用于验证 ImportError 场景）
def test_old_path_importable_subprocess():
    """subprocess 隔离验证旧路径可导入"""
    import subprocess
    import sys

    code = '''
import warnings
warnings.simplefilter("always")

try:
    # 替换为实际的旧导入路径
    # from engram.gateway.di import generate_correlation_id
    print("PASS: old path importable")
except ImportError as e:
    print(f"FAIL: ImportError - {e}")
    exit(1)
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert "PASS" in result.stdout, f"Failed: {result.stderr}"
```

#### 废弃期时间线（策略 A）

| 阶段 | 版本跨度 | 旧路径行为 | 新路径行为 |
|------|----------|-----------|-----------|
| Phase 1: 废弃警告 | 至少 2 次版本 | ✅ 可用 + `DeprecationWarning` | ✅ 推荐 |
| Phase 2: 错误警告 | 至少 1 次版本 | ✅ 可用 + `FutureWarning` | ✅ 推荐 |
| Phase 3: 移除 | 主版本升级时 | ❌ `ImportError` | ✅ 唯一路径 |

#### 验收命令（拆分模块后）

```bash
# 1. 基础门禁
make check-gateway-public-api-surface
make check-gateway-public-api-docs-sync

# 2. 导入兼容性测试
pytest tests/gateway/test_public_api_import_contract.py -v

# 3. 如有废弃警告测试
pytest tests/gateway/test_public_api_deprecated_import_warning.py -v

# 4. 完整 Gateway 测试
make test-gateway
```

---

## 向后兼容策略

> **完整策略**：参见 [gateway_contract_convergence.md §11](../contracts/gateway_contract_convergence.md#11-public-api-向后兼容策略)

### 变更规则摘要

| 变更类型 | Tier A | Tier B | Tier C |
|----------|--------|--------|--------|
| 新增符号 | ✅ 允许 | ✅ 允许 | ✅ 允许 |
| 修改签名 | ❌ 禁止 | ❌ 禁止 | ⚠️ 谨慎 |
| 移除符号 | ❌ 禁止 | ❌ 禁止 | ⚠️ 需废弃期 |
| 修改返回类型 | ❌ 禁止 | ❌ 禁止 | ⚠️ 谨慎 |

### 变更流程

1. **提案**：在 `docs/contracts/gateway_contract_convergence.md` 添加变更提案
2. **测试锚点**：更新/新增相关测试锚点
3. **废弃期**：对于移除符号，至少保留 2 个次版本的废弃警告
4. **文档同步**：同步更新本文档和 `public_api.py` 的文档字符串

---

## 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0 | 2026-02-02 | 初始版本，完整记录 public_api.py 导出项分析 |
| v1.1 | 2026-02-02 | 引入 Tier C 分类；明确 Tier B 失败语义；添加插件作者推荐导入策略；添加向后兼容策略章节 |
| v1.2 | 2026-02-02 | 新增"验收命令"段落，定义最小验收命令集合（CI 脚本检查 + pytest 测试） |
| v1.3 | 2026-02-02 | 新增"变更类型风险评估"表格和"CI Job 对应关系"表格，明确 CI workflow 覆盖情况 |
| v1.4 | 2026-02-02 | 更新"相关文档"章节，添加 Gateway Public API JSON-RPC Surface ADR 引用 |
| v1.5 | 2026-02-02 | 明确 Tier C 避免原因与替代方案；添加 Tier B try/except 可复制代码片段和 ImportError 消息字段契约；同步导出项与 `__all__` |
| v1.6 | 2026-02-02 | 明确权威来源层级：符号清单以 `public_api.__all__` 为准，本文档提供详细分析；兼容承诺统一指向 `gateway_contract_convergence.md §11` |
| v1.7 | 2026-02-02 | 新增"维护者：新增符号模板"章节（按 Tier A/B/C 分类的代码/文档/测试检查清单、subprocess 阻断测试模板）；最小验收命令统一引用 Makefile 目标 |
| v1.8 | 2026-02-02 | 新增"拆分模块 Checklist"章节：策略 A/B 检查清单、最小测试模板（验证旧路径导入兼容性）、废弃期时间线 |

---

## 相关文档

| 文档 | 路径 |
|------|------|
| **Gateway Public API / JSON-RPC SSOT 地图** | [docs/contracts/gateway_public_api_jsonrpc_ssot_map.md](../contracts/gateway_public_api_jsonrpc_ssot_map.md) |
| MCP JSON-RPC 错误模型契约 | [docs/contracts/mcp_jsonrpc_error_v2.md](../contracts/mcp_jsonrpc_error_v2.md) |
| Gateway 契约收敛文档 | [docs/contracts/gateway_contract_convergence.md](../contracts/gateway_contract_convergence.md) |
| Gateway Public API JSON-RPC Surface ADR | [docs/architecture/adr_gateway_public_api_jsonrpc_surface.md](./adr_gateway_public_api_jsonrpc_surface.md) |
| Gateway ImportError 规范 | [docs/architecture/gateway_importerror_and_optional_deps.md](./gateway_importerror_and_optional_deps.md) |
| AI Agent 协作指南 | [AGENTS.md](../../AGENTS.md) |
