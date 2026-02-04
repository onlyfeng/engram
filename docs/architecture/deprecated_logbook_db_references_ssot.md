# Deprecated LogbookDB 引用 SSOT（单一事实来源）

> **文档目的**: 系统性记录 `deps.db`、`container.db`、`get_logbook_db`、`engram.gateway.logbook_db`、`LogbookDatabase` 的所有引用位置，按类别分类并提供替代路径。
>
> **生成日期**: 2026-02-01  
> **状态**: v0.9 弃用，v1.0 移除

---

## 1. 弃用 API 概览

| 弃用 API | 所属模块 | v0.9 状态 | v1.0 状态 | 替代方案 |
|----------|----------|-----------|-----------|----------|
| `deps.db` | `GatewayDeps` | 弃用（产生警告） | **移除** | `deps.logbook_adapter` |
| `container.db` | `GatewayContainer` | 弃用（产生警告） | **移除** | `container.logbook_adapter` |
| `get_db()` | `logbook_db.py` | 弃用 | **模块移除** | `get_gateway_deps().logbook_adapter` |
| `get_logbook_db()` | `logbook_db.py` | 弃用 | **模块移除** | `get_gateway_deps().logbook_adapter` |
| `engram.gateway.logbook_db` | 模块 | 弃用（导入产生警告） | **模块移除** | `engram.gateway.logbook_adapter` |
| `LogbookDatabase` | 类 | 弃用 | **移除** | `LogbookAdapter` |

---

## 2. 引用分类清单

### 2.1 外部导入点（Import Points）

**当前状态**: `src/engram/gateway/logbook_db.py` 已删除（git status: `D src/engram/gateway/logbook_db.py`）

| 引用模式 | 文件位置 | 当前状态 | 替代路径 |
|----------|----------|----------|----------|
| `from engram.gateway.logbook_db import get_db` | N/A（模块已删除） | 已移除 | `from engram.gateway.container import get_gateway_deps` |
| `from engram.gateway.logbook_db import get_logbook_db` | N/A（模块已删除） | 已移除 | `from engram.gateway.container import get_gateway_deps` |
| `from engram.gateway.logbook_db import LogbookDatabase` | N/A（模块已删除） | 已移除 | `from engram.gateway.logbook_adapter import LogbookAdapter` |
| `import engram.gateway.logbook_db` | N/A（模块已删除） | 已移除 | `import engram.gateway.logbook_adapter` |

**替代导入语句**:

```python
# 旧代码（已移除）
from engram.gateway.logbook_db import get_db, get_logbook_db, LogbookDatabase

# 新代码（推荐）
from engram.gateway.container import get_gateway_deps
from engram.gateway.logbook_adapter import LogbookAdapter

# 获取适配器实例
adapter = get_gateway_deps().logbook_adapter
```

### 2.2 运行期路径（Runtime Paths）

| 引用模式 | 文件位置 | 行号 | 当前状态 | 替代路径 |
|----------|----------|------|----------|----------|
| `container.db` | `src/engram/gateway/di.py` | 260 | 文档注释中提及 | `container.logbook_adapter` |
| `container.db` | `src/engram/gateway/container.py` | 371 | docstring 示例代码 | `container.logbook_adapter` |
| `deps.db` | `src/engram/gateway/handlers/memory_store.py` | 203 | 注释（已迁移） | `deps.logbook_adapter`（已使用） |
| `deps.db` | `src/engram/gateway/handlers/governance_update.py` | 13, 83 | 注释（已迁移） | `deps.logbook_adapter`（已使用） |
| `deps.db` | `src/engram/gateway/services/actor_validation.py` | 88 | 注释（已迁移） | `deps.logbook_adapter`（已使用） |

**Handler 替代方案**:

```python
# 旧代码（已弃用）
db = deps.db
result = db.execute_query("SELECT * FROM governance.settings")

# 新代码（推荐）
adapter = deps.logbook_adapter
settings = adapter.get_governance_settings()  # 使用 adapter 的具名方法
```

### 2.3 测试文件引用（Test Files）

| 引用模式 | 文件位置 | 行号 | 当前状态 | 替代路径 |
|----------|----------|------|----------|----------|
| `FakeLogbookDatabase` | `tests/gateway/fakes.py` | 354-366, 1096-1099 | 存在（弃用） | `FakeLogbookAdapter` |
| `deps.db` | `tests/gateway/fakes.py` | 365 | 迁移指南注释 | `deps.logbook_adapter` |
| `deps.db` | `tests/gateway/test_audit_two_phase_default_db_path.py` | 3, 15 | docstring 说明 | 已迁移至 adapter-first |
| `deps.db` | `tests/gateway/test_two_phase_audit_adapter_first.py` | 643 | 注释（已移除） | N/A（已完全移除） |
| `FakeLogbookDatabase` | `tests/gateway/conftest.py` | 463-472 | fixture 定义 | `FakeLogbookAdapter` |
| `LogbookDatabase` | `tests/gateway/test_reconcile_outbox.py` | 2761 | 迁移说明注释 | `LogbookAdapter` |
| `LogbookDatabase` | `tests/gateway/test_validate_refs.py` | 785 | 迁移说明注释 | `LogbookAdapter` |
| `DB_MODULE` | `tests/gateway/test_mcp_jsonrpc_contract.py` | 30 | 常量定义 | `ADAPTER_MODULE` |

**测试替代方案**:

```python
# 旧代码（已弃用）
from tests.gateway.fakes import FakeLogbookDatabase
db = FakeLogbookDatabase()
deps = GatewayDeps.for_testing(db=db)

# 新代码（推荐）
from tests.gateway.fakes import FakeLogbookAdapter
adapter = FakeLogbookAdapter()
deps = GatewayDeps.for_testing(logbook_adapter=adapter)
```

### 2.4 文档引用（Documentation）

| 引用模式 | 文件位置 | 行号 | 内容类型 | 替代路径 |
|----------|----------|------|----------|----------|
| `deps.db` | `docs/gateway/upgrade_v2_0_remove_handler_di_compat.md` | 24, 104-107, 565-572, 647-651, 682-724 | 升级指南 | `deps.logbook_adapter` |
| `container.db` | `docs/gateway/upgrade_v2_0_remove_handler_di_compat.md` | 25, 107, 583-592, 650, 685, 697, 715, 723 | 升级指南 | `container.logbook_adapter` |
| `get_logbook_db()` | `docs/gateway/upgrade_v2_0_remove_handler_di_compat.md` | 26, 96, 612-617, 651, 687 | 升级指南 | `get_gateway_deps().logbook_adapter` |
| `engram.gateway.logbook_db` | `docs/gateway/upgrade_v2_0_remove_handler_di_compat.md` | 27, 113, 612, 652, 688, 698 | 模块弃用说明 | `engram.gateway.logbook_adapter` |
| `deps.db` | `docs/architecture/adr_gateway_di_and_entry_boundary.md` | 960-986, 1120 | ADR 弃用说明 | `deps.logbook_adapter` |
| `container.db` | `docs/architecture/adr_gateway_di_and_entry_boundary.md` | 312, 984 | ADR 依赖图 | `container.logbook_adapter` |
| `LogbookDatabase` | `docs/architecture/adr_gateway_di_and_entry_boundary.md` | 312 | ADR 依赖图 | `LogbookAdapter` |
| `deps.db` | `docs/gateway/06_gateway_design.md` | 116 | 弃用表格 | `deps.logbook_adapter` |
| `container.db` | `docs/gateway/06_gateway_design.md` | 117 | 弃用表格 | `container.logbook_adapter` |
| `get_logbook_db()` | `docs/gateway/06_gateway_design.md` | 99, 118 | 弃用说明 | `get_gateway_deps().logbook_adapter` |
| `engram.gateway.logbook_db` | `docs/gateway/06_gateway_design.md` | 98-99, 119 | 模块弃用说明 | `engram.gateway.logbook_adapter` |
| `deps.db` | `docs/architecture/gateway_module_boundaries.md` | 100, 330-352 | 禁止模式 | `deps.logbook_adapter` |

### 2.5 CI 脚本引用（CI Scripts）

| 引用模式 | 文件位置 | 行号 | 内容类型 | 替代路径 |
|----------|----------|------|----------|----------|
| `deps.db` | `scripts/ci/check_gateway_di_boundaries.py` | 20, 96-101, 566 | 检查规则 | `deps.logbook_adapter` |

---

## 3. 替代路径完整参考

### 3.1 依赖获取路径

| 旧路径 | 新路径 | 说明 |
|--------|--------|------|
| `deps.db` | `deps.logbook_adapter` | GatewayDeps 属性 |
| `container.db` | `container.logbook_adapter` | GatewayContainer 属性 |
| `get_db()` | `get_gateway_deps().logbook_adapter` | 全局获取函数 |
| `get_logbook_db()` | `get_gateway_deps().logbook_adapter` | 全局获取函数 |

### 3.2 模块/类替代

| 旧模块/类 | 新模块/类 | 路径 |
|-----------|-----------|------|
| `engram.gateway.logbook_db` | `engram.gateway.logbook_adapter` | 模块导入 |
| `LogbookDatabase` | `LogbookAdapter` | `from engram.gateway.logbook_adapter import LogbookAdapter` |
| `FakeLogbookDatabase` | `FakeLogbookAdapter` | `from tests.gateway.fakes import FakeLogbookAdapter` |

### 3.3 FastAPI 依赖注入

| 旧函数 | 新函数 | 模块 |
|--------|--------|------|
| `get_db()` | `get_logbook_adapter_dep()` | `engram.gateway.container` |
| `get_logbook_db()` | `get_logbook_adapter_dep()` | `engram.gateway.container` |
| N/A | `get_gateway_deps()` | `engram.gateway.container`（推荐） |

### 3.4 服务端口接口

新代码应使用 `services/ports.py` 中定义的 Protocol 接口：

```python
from engram.gateway.services.ports import LogbookPort

# LogbookPort 定义了 logbook 操作的抽象接口
# LogbookAdapter 实现了 LogbookPort
```

---

## 4. 迁移检查清单

### 4.1 代码迁移

- [x] `src/engram/gateway/handlers/memory_store.py` - 已使用 `deps.logbook_adapter`
- [x] `src/engram/gateway/handlers/governance_update.py` - 已使用 `deps.logbook_adapter`
- [x] `src/engram/gateway/services/actor_validation.py` - 已使用 `deps.logbook_adapter`
- [x] `src/engram/gateway/logbook_db.py` - 已删除

### 4.2 文档更新

- [x] `docs/gateway/upgrade_v2_0_remove_handler_di_compat.md` - 包含完整迁移指南
- [x] `docs/architecture/adr_gateway_di_and_entry_boundary.md` - 包含弃用说明
- [x] `docs/gateway/06_gateway_design.md` - 包含弃用表格
- [x] `docs/architecture/gateway_module_boundaries.md` - 包含禁止模式说明

### 4.3 测试迁移

- [x] `tests/gateway/fakes.py` - `FakeLogbookDatabase` 仍存在（已标记弃用），`create_test_dependencies()` 已移除 db 返回
- [x] `tests/gateway/conftest.py` - `fake_logbook_db` fixture 已标记 LEGACY，新增 DeprecationWarning
- [x] `tests/gateway/test_di_boundaries.py` - 已移除所有 `fake_logbook_db` 参数依赖
- [x] `tests/gateway/test_logbook_db.py` - 已删除

**移除里程碑**:
- v1.0 (Iteration 5): 完成 - 所有测试不再依赖 `fake_logbook_db` 的实际功能
- v1.1 (Iteration 6): 计划 - 移除 `fake_logbook_db` fixture
- v2.0: 计划 - 完全移除 `FakeLogbookDatabase` 类

**兼容目录清理（2026-02-01）**:
- `DEPRECATED_IMPORT_COMPAT_DIRECTORIES` 已清空，无剩余兼容目录
- `tests/logbook/test_logbook_db.py` - 测试的是 `engram.logbook.db`（核心模块），与废弃的 `engram.gateway.logbook_db` 无关
- `tests/gateway/test_correlation_id_proxy.py` - 已完成迁移至 `engram.gateway.logbook_adapter`，不再包含废弃导入
- 全仓扫描验证：未发现任何废弃的 `engram.gateway.logbook_db` 导入

### 4.4 CI 检查

- [x] `scripts/ci/check_gateway_di_boundaries.py` - 已包含 `deps.db` 禁止规则

---

## 5. 相关文档

| 文档 | 路径 | 说明 |
|------|------|------|
| v2.0 升级指南 | `docs/gateway/upgrade_v2_0_remove_handler_di_compat.md` | 完整迁移步骤 |
| Gateway DI ADR | `docs/architecture/adr_gateway_di_and_entry_boundary.md` | 架构决策记录 |
| Gateway 设计 | `docs/gateway/06_gateway_design.md` | Gateway 整体设计 |
| 模块边界 | `docs/architecture/gateway_module_boundaries.md` | DI 边界检查规则 |

---

## 6. 更新日志

| 日期 | 版本 | 变更说明 |
|------|------|----------|
| 2026-02-01 | v1.2 | 清空兼容目录：确认所有测试已完成迁移，`DEPRECATED_IMPORT_COMPAT_DIRECTORIES` 清空 |
| 2026-02-01 | v1.1 | 完成测试迁移：移除 `fake_logbook_db` 依赖，标记 fixture 为 LEGACY |
| 2026-02-01 | v1.0 | 初始创建：系统性收集所有弃用引用 |
