# v2.0 升级指南：移除 Handler DI 兼容层

> 状态: **计划中**  
> 创建日期: 2026-02-03  
> 目标版本: v2.0  
> 前置版本: v1.x

----

## 1. 概述

本文档描述从 v1.x 升级到 v2.0 时的 **Handler 依赖注入（DI）兼容层移除**变更。

### 1.1 变更摘要

| 维度 | v1.x（当前） | v2.0（目标） |
|------|-------------|-------------|
| `deps` 参数 | `Optional[GatewayDeps]`（可选） | `GatewayDeps`（必需） |
| Handler 模块导入 | 允许直接使用 `deps.db` | 禁止直接使用（必须走 DI） |
| Handler 入口签名 | 允许 `deps=None` | 统一要求 `deps` |
| 兼容层 | `deps = resolve_deps()` | 彻底移除 |

## 2. 影响范围

### 2.1 代码模块

| 模块 | 位置 | 影响说明 |
|------|------|----------|
| `handlers/memory_store.py` | `handlers/memory_store.py` | 签名变更 + Legacy 参数移除 |
| `handlers/memory_query.py` | `handlers/memory_query.py` | 签名变更 + Legacy 参数移除 |
| `handlers/governance_update.py` | `handlers/governance_update.py` | 签名变更 + Legacy 参数移除 |
| `handlers/evidence_upload.py` | `handlers/evidence_upload.py` | 签名变更 + Legacy 参数移除 |
| `handlers/__init__.py` | `handlers/__init__.py` | 对外导出 API 调整 |
| `gateway/dependencies.py` | `dependencies.py` | 移除 resolve_deps fallback |

### 2.2 配置与依赖

- `GatewayDeps` 的构建必须走 DI 容器。
- 禁止在 Handler 层直接初始化 DB 连接或访问 `deps.db`。

## 3. 行为变化

### 3.1 Handler 签名变更

**旧版（v1.x）**：

```python
async def handle_memory_store(
    payload: MemoryStorePayload,
    deps: Optional[GatewayDeps] = None,
) -> MemoryStoreResult:
    deps = deps or resolve_deps()
    ...
```

**新版（v2.0）**：

```python
async def handle_memory_store(
    payload: MemoryStorePayload,
    deps: GatewayDeps,
) -> MemoryStoreResult:
    ...
```

### 3.2 依赖注入来源

- 统一由 `container.py` / `dependencies.py` 构建。
- Handler 不再自建 deps，也不再自建 db。

## 4. 升级步骤

1. **移除 resolve_deps fallback**  
   - 删除 `deps = deps or resolve_deps()` 类逻辑。
2. **统一 Handler 签名**  
   - `deps` 必须作为显式参数传入。
3. **更新调用方**  
   - 所有调用 Handler 的入口必须传入 `deps`。
4. **强化 CI 检查**  
   - 启用 `make check-gateway-di-boundaries`。
5. **运行回归**  
   - `make test-gateway-unit` / `make test-gateway-integration`

## 5. 兼容性说明

- **不再兼容** `deps=None`。
- **不再兼容** Handler 内部直接构建 DB 依赖。
- 任何旧调用路径必须更新。

## 6. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| 调用方未更新 | 运行期异常 | CI 门禁 + 集成测试覆盖 |
| Handler 内部直接用 deps.db | DI 边界破坏 | `make check-gateway-di-boundaries` |
| Handler 入口导出变化 | Public API 破坏 | `make check-gateway-public-api-surface` |

## 7. 测试建议

- `make check-gateway-di-boundaries`
- `make check-gateway-public-api-surface`
- `make check-gateway-public-api-docs-sync`
- `make test-gateway-unit`
- `make test-gateway-integration`

## 8. 相关文档

- [Gateway DI 边界 ADR](../architecture/adr_gateway_di_and_entry_boundary.md)
- [Gateway 模块边界与导入规范](../architecture/gateway_module_boundaries.md)
- [Gateway Import Timing 依赖图](../architecture/gateway_import_timing_dependency_graph.md)
