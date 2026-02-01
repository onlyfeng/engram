# Gateway Public API 表面设计

> 创建日期: 2026-02-02
>
> 状态: Active
>
> 相关文件: `src/engram/gateway/public_api.py`

## 概述

`engram.gateway.public_api` 模块是 Gateway 对外暴露的统一公共接口。
此模块采用 **Tier A/B 分层延迟导入策略**，确保：

1. **快速导入**: `import engram.gateway.public_api` 不触发重型依赖加载
2. **渐进式降级**: 缺少可选依赖时，Tier A 符号仍可正常使用
3. **类型安全**: 通过 `TYPE_CHECKING` 块保持完整的静态类型支持

---

## 分层设计

### Tier A: 核心稳定层（直接导入）

Tier A 符号在模块加载时直接导入，无外部依赖，启动速度快。

| 符号 | 源模块 | 说明 |
|------|--------|------|
| `RequestContext` | `.di` | 请求上下文 dataclass |
| `GatewayDeps` | `.di` | 依赖容器实现类 |
| `GatewayDepsProtocol` | `.di` | 依赖容器 Protocol |
| `create_request_context` | `.di` | 便捷工厂函数 |
| `create_gateway_deps` | `.di` | 便捷工厂函数 |
| `WriteAuditPort` | `.services.ports` | 审计写入端口 |
| `UserDirectoryPort` | `.services.ports` | 用户目录端口 |
| `ActorPolicyConfigPort` | `.services.ports` | Actor 策略配置端口 |
| `ToolExecutorPort` | `.services.ports` | 工具执行器端口 |
| `ToolRouterPort` | `.services.ports` | 工具路由器端口 |
| `ToolDefinition` | `.services.ports` | 工具定义 |
| `ToolCallContext` | `.services.ports` | 工具调用上下文 |
| `ToolCallResult` | `.services.ports` | 工具调用结果 |
| `McpErrorCode` | `.error_codes` | JSON-RPC 错误码 |
| `McpErrorCategory` | `.error_codes` | 错误分类常量 |
| `McpErrorReason` | `.error_codes` | 错误原因码 |
| `ToolResultErrorCode` | `.result_error_codes` | 工具结果错误码 |

### Tier B: 可选依赖层（延迟导入）

Tier B 符号通过 `__getattr__` 实现延迟导入，仅在首次访问时加载。

| 符号 | 源模块 | 外部依赖 | 说明 |
|------|--------|----------|------|
| `LogbookAdapter` | `.logbook_adapter` | `engram_logbook` | Logbook 数据库适配器 |
| `get_adapter` | `.logbook_adapter` | `engram_logbook` | 获取全局适配器单例 |
| `get_reliability_report` | `.logbook_adapter` | `engram_logbook` | 可靠性统计报告 |
| `execute_tool` | `.entrypoints.tool_executor` | Gateway 完整依赖 | MCP 工具执行入口 |
| `dispatch_jsonrpc_request` | `.mcp_rpc` | Gateway 完整依赖 | JSON-RPC 请求分发 |
| `JsonRpcDispatchResult` | `.mcp_rpc` | Gateway 完整依赖 | JSON-RPC 结果封装 |

---

## 延迟导入机制

### `__getattr__` 实现

```python
_TIER_B_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "LogbookAdapter": (".logbook_adapter", "LogbookAdapter"),
    "get_adapter": (".logbook_adapter", "get_adapter"),
    # ...
}

def __getattr__(name: str) -> Any:
    if name in _TIER_B_LAZY_IMPORTS:
        module_path, attr_name = _TIER_B_LAZY_IMPORTS[name]
        try:
            module = importlib.import_module(module_path, __package__)
            obj = getattr(module, attr_name)
            globals()[name] = obj  # 缓存避免重复导入
            return obj
        except ImportError as e:
            raise ImportError(f"无法导入 '{name}': {e}\n\n{install_hint}") from e
    raise AttributeError(f"模块没有属性 {name!r}")
```

### 缓存策略

首次延迟导入成功后，符号被缓存到模块 `globals()` 中，后续访问直接返回缓存对象，无额外开销。

### 错误处理

导入失败时抛出 `ImportError`，包含：

1. 失败的符号名
2. 原始错误信息
3. 安装指引（如 `pip install -e ".[full]"`）

---

## 类型检查支持

通过 `TYPE_CHECKING` 块，IDE 和 mypy 可以获得完整的类型提示：

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .logbook_adapter import (
        LogbookAdapter as LogbookAdapter,
        get_adapter as get_adapter,
        get_reliability_report as get_reliability_report,
    )
    # ...
```

**注意**: `as X` 语法是为了满足 mypy 的 re-export 要求（否则 mypy 可能报告 `implicit reexport` 警告）。

---

## 使用场景

### 场景 1: 仅使用 Tier A 符号

```python
# 快速导入，无外部依赖
from engram.gateway.public_api import (
    RequestContext,
    GatewayDepsProtocol,
    McpErrorCode,
)
```

### 场景 2: 使用 Tier B 符号

```python
# 首次访问 LogbookAdapter 时触发延迟导入
from engram.gateway.public_api import LogbookAdapter

# 如果 engram_logbook 未安装，会抛出 ImportError:
# ImportError: 无法导入 'LogbookAdapter'（来自 .logbook_adapter）: ...
#
# 此功能需要 engram_logbook 模块。
# 请安装：pip install -e ".[full]" 或 pip install engram-logbook
```

### 场景 3: 测试环境

```python
# 测试可以 mock 延迟导入的符号
import sys
from unittest.mock import MagicMock

# 在测试 setup 中预先注入 mock
mock_adapter = MagicMock()
sys.modules['engram.gateway.logbook_adapter'] = MagicMock()
sys.modules['engram.gateway.logbook_adapter'].LogbookAdapter = mock_adapter
```

---

## 兼容性保证

### 向后兼容

所有 `__all__` 中列出的符号在相同主版本号内保持：

- 函数签名不变
- 返回类型不变
- Protocol 接口方法不变

### 语义兼容

```python
# 这两种导入方式语义等价
from engram.gateway.public_api import LogbookAdapter  # 推荐
from engram.gateway.logbook_adapter import LogbookAdapter  # 也可用
```

---

## 测试契约

### 必须通过的测试

```bash
# 1. 验证 Tier A 符号可直接导入（无需完整依赖）
pytest tests/gateway/test_public_api_tier_a.py -v

# 2. 验证 Tier B 延迟导入行为
pytest tests/gateway/test_public_api_lazy_import.py -v

# 3. 验证 ImportError 包含安装指引
pytest tests/gateway/test_public_api_importerror_hint.py -v
```

### 验收标准

| # | 检查项 | 验证方式 |
|---|--------|---------|
| 1 | `import engram.gateway.public_api` 不触发 `engram_logbook` 导入 | 单元测试 |
| 2 | Tier A 符号在缺少可选依赖时可正常使用 | 单元测试 |
| 3 | Tier B 符号延迟导入失败时抛出包含安装指引的 ImportError | 单元测试 |
| 4 | `__all__` 与实际可导出符号一致 | CI 门禁 |
| 5 | TYPE_CHECKING 块提供完整类型提示 | mypy 检查 |

---

## 已知限制

### Tier A 模块的间接依赖

当前 `di.py`（Tier A 模块）在顶层导入了 `mcp_rpc.py`：

```python
# di.py L63
from .mcp_rpc import generate_correlation_id
```

而 `mcp_rpc.py` 又尝试导入 `logbook_adapter`（try-except 包裹）：

```python
# mcp_rpc.py L77-84
try:
    from engram.gateway.logbook_adapter import LogbookDBCheckError
except ImportError:
    pass
```

这导致 `import engram.gateway.public_api` 仍会触发 `engram.logbook.*` 模块的加载（当 `engram_logbook` 已安装时）。

**影响范围**：仅影响启动时间，不影响功能正确性。

**后续优化方向**：
1. 将 `di.py` 中的 `generate_correlation_id` 导入改为延迟导入
2. 或在 `mcp_rpc.py` 中将 `logbook_adapter` 的 ImportError 检查移到使用时

---

## 相关文档

- [Gateway ImportError 与可选依赖处理规范](./gateway_importerror_and_optional_deps.md)
- [Gateway 模块边界与 Import 规则](./gateway_module_boundaries.md)
- [ADR: Gateway DI 与入口边界统一](./adr_gateway_di_and_entry_boundary.md)

---

## 变更日志

| 日期 | 版本 | 变更内容 |
|------|------|----------|
| 2026-02-02 | v1.0 | 初始版本，定义 Tier A/B 分层延迟导入策略 |

---

> 更新时间：2026-02-02
