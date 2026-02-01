# 两阶段审计 E2E 测试数据库隔离指南

> 文档版本: v2.0 | 更新日期: 2026-02-01

本文档描述两阶段审计 E2E 测试的数据库隔离策略，以及如何从 TRUNCATE 隔离迁移到 schema_prefix 隔离。

---

## 目录

- [1. 目标](#1-目标)
- [2. 策略](#2-策略)
- [3. 实施](#3-实施)
- [4. search_path 配置](#4-search_path-配置)
- [5. 并发注意事项](#5-并发注意事项)
- [6. 迁移指南](#6-迁移指南)

---

## 1. 目标

数据库隔离的核心目标：

| 目标 | 说明 |
|-----|------|
| **稳定** | 测试结果不受其他测试残留数据影响，每次运行结果一致 |
| **可并行** | 支持 pytest-xdist 多 worker 并行执行，无锁竞争 |
| **可重复** | 任意顺序执行测试，结果相同；支持 `--lf` 重跑失败测试 |

### 1.1 当前问题（TRUNCATE 方案）

传统 TRUNCATE 方案存在以下问题：

```
问题 1: TRUNCATE 持有 ACCESS EXCLUSIVE 锁
  - xdist worker 并行时可能死锁
  - 长事务期间其他连接无法访问表

问题 2: TRUNCATE 覆盖不足
  - 只清空 outbox_memory 和 write_audit
  - settings/users 表残留数据影响后续测试

问题 3: 顺序依赖
  - 测试 A 写入数据 → 测试 B 读到残留数据
  - 单独运行 测试 B 结果不同
```

---

## 2. 策略

采用 **两层隔离** 策略：

### 2.1 第一层：xdist worker 独立数据库

每个 pytest-xdist worker 使用独立的测试数据库：

```
worker    数据库名
------    --------
gw0       engram_test_<uuid1>
gw1       engram_test_<uuid2>
gw2       engram_test_<uuid3>
master    engram_test_<uuid0>  (单进程模式)
```

**实现机制**：`tests/gateway/conftest.py` 中的 `test_db_info` fixture（session scope）。

### 2.2 第二层：module-scoped schema_prefix

对于需要测试 **新连接/提交语义** 的场景（如两阶段审计），启用 module-scoped schema_prefix：

```
module                      schema_prefix    实际 schema 名
--------------------------  ---------------  --------------
test_two_phase_audit_e2e    tp_a1b2c3d4      tp_a1b2c3d4_logbook
                                             tp_a1b2c3d4_governance
                                             tp_a1b2c3d4_identity
                                             ...
test_other_module           tp_e5f6g7h8      tp_e5f6g7h8_logbook
                                             ...
```

**何时启用 schema_prefix**：

| 场景 | 是否需要 schema_prefix |
|-----|----------------------|
| Handler 单元测试（使用 Fake） | 否 |
| 标准 DB 测试（单连接回滚） | 否 |
| 多连接提交语义测试 | **是** |
| Adapter 内部连接测试 | **是** |
| Outbox worker 集成测试 | **是** |

---

## 3. 实施

### 3.1 核心 Fixture 依赖关系

```
test_db_info (session)
    │
    ├─── migrated_db (session)
    │        └─── db_conn (function, 标准 schema)
    │        └─── db_conn_committed (function, 标准 schema)
    │
    └─── schema_prefix (module)
             │
             └─── prefixed_schema_context (module)
                      │
                      └─── migrated_db_prefixed (module)
                               │
                               └─── db_conn_prefixed_committed (function)
```

### 3.2 Fixture 使用方式

#### 3.2.1 prefixed_schema_context

设置全局 SchemaContext，影响所有后续的 `get_connection()` 调用：

```python
def test_adapter_uses_prefixed_schema(
    prefixed_schema_context,  # 设置全局 SchemaContext
    migrated_db_prefixed,     # 确保 schema 已迁移
):
    # prefixed_schema_context.schema_prefix = "tp_a1b2c3d4"
    # 此后 adapter 内部的 get_connection() 会自动使用带前缀的 search_path
    
    adapter = LogbookAdapter.from_config(config)
    adapter.check_user_exists("user_123")  # 查询 tp_a1b2c3d4_identity.users
```

#### 3.2.2 migrated_db_prefixed

提供已迁移的带前缀 schema 数据库信息：

```python
def test_with_prefixed_db(migrated_db_prefixed):
    # migrated_db_prefixed 包含:
    # {
    #     "dsn": "postgresql://...",
    #     "db_name": "engram_test_xxx",
    #     "worker_id": "gw0",
    #     "schema_prefix": "tp_a1b2c3d4",
    #     "schema_context": SchemaContext(...),
    #     "schemas": {
    #         "identity": "tp_a1b2c3d4_identity",
    #         "logbook": "tp_a1b2c3d4_logbook",
    #         "scm": "tp_a1b2c3d4_scm",
    #         "analysis": "tp_a1b2c3d4_analysis",
    #         "governance": "tp_a1b2c3d4_governance",
    #     }
    # }
    pass
```

#### 3.2.3 db_conn_prefixed_committed

提供可提交的带前缀连接，用于验证跨连接可见性：

```python
def test_two_phase_audit_visibility(
    prefixed_schema_context,
    migrated_db_prefixed,
    db_conn_prefixed_committed,
):
    """验证 adapter 写入后测试连接可见"""
    
    # 1. Adapter 写入（内部连接，独立事务）
    adapter = LogbookAdapter.from_config(config)
    adapter.write_pending_audit(...)  # INSERT + COMMIT
    
    # 2. 测试连接验证（独立连接，可见已提交数据）
    with db_conn_prefixed_committed.cursor() as cur:
        cur.execute("SELECT * FROM governance.write_audit")
        rows = cur.fetchall()
        assert len(rows) == 1  # 可见 adapter 写入的数据
```

### 3.3 完整测试示例

```python
# tests/gateway/test_two_phase_audit_e2e.py

import pytest
from engram.gateway.logbook_adapter import LogbookAdapter


class TestTwoPhaseAuditE2E:
    """两阶段审计端到端测试（使用 schema_prefix 隔离）"""
    
    @pytest.fixture(autouse=True)
    def setup_adapter(self, prefixed_schema_context, migrated_db_prefixed):
        """
        为本测试类设置 adapter
        
        - prefixed_schema_context: 设置全局 SchemaContext
        - migrated_db_prefixed: 确保带前缀的 schema 已创建并迁移
        """
        self.dsn = migrated_db_prefixed["dsn"]
        self.schemas = migrated_db_prefixed["schemas"]
    
    def test_pending_audit_written(self, db_conn_prefixed_committed):
        """验证 pending 审计记录写入"""
        # 创建 adapter（会使用全局 SchemaContext）
        adapter = LogbookAdapter(dsn=self.dsn)
        
        # 写入 pending 审计
        adapter.write_pending_audit(
            correlation_id="corr-123",
            actor_user_id="user_abc",
            target_space="default",
            action="store",
            reason=None,
            payload_sha="sha256:...",
            evidence_refs_json={},
        )
        
        # 用测试连接验证
        with db_conn_prefixed_committed.cursor() as cur:
            cur.execute("""
                SELECT correlation_id, status 
                FROM governance.write_audit 
                WHERE correlation_id = 'corr-123'
            """)
            row = cur.fetchone()
        
        assert row is not None
        assert row[0] == "corr-123"
        assert row[1] == "pending"
```

---

## 4. search_path 配置

### 4.1 get_connection() 优先级

`engram.logbook.db.get_connection()` 确定 search_path 的优先级（从高到低）：

| 优先级 | 来源 | 说明 |
|-------|------|-----|
| 1 | `search_path` 参数 | 显式传入，最高优先级 |
| 2 | `schema_context` 参数 | 传入的 SchemaContext 实例 |
| 3 | `config["postgres.search_path"]` | 配置文件设置 |
| 4 | 全局 `get_schema_context()` | **测试常用**：通过 `set_schema_context()` 设置 |
| 5 | `DEFAULT_SEARCH_PATH` | 兜底默认值 |

### 4.2 默认 search_path 顺序

```python
# src/engram/logbook/db.py
DEFAULT_SEARCH_PATH = ["logbook", "identity", "scm", "analysis", "governance", "public"]
```

**权威顺序定义**：`SchemaContext.search_path` 属性。

### 4.3 推荐配置

#### 生产环境

不设置 schema_prefix，使用默认 schema 名：

```python
# 连接自动设置 search_path 为 DEFAULT_SEARCH_PATH
conn = get_connection(dsn=dsn)
```

#### 测试环境（schema_prefix 隔离）

通过 `prefixed_schema_context` fixture 设置全局上下文：

```python
def test_with_isolation(prefixed_schema_context, migrated_db_prefixed):
    # prefixed_schema_context fixture 已调用:
    #   set_schema_context(SchemaContext(schema_prefix="tp_xxx"))
    
    # 后续所有 get_connection() 调用自动使用带前缀的 search_path
    conn = get_connection(dsn=dsn)  # search_path: tp_xxx_logbook, tp_xxx_identity, ...
```

#### 测试环境（标准 schema）

使用 `db_conn` 或 `db_conn_committed` fixture：

```python
def test_standard(db_conn):
    # db_conn fixture 已设置 search_path 为标准 schema
    # search_path: logbook, scm, identity, analysis, governance, public
    with db_conn.cursor() as cur:
        cur.execute("SELECT * FROM items")  # 解析为 logbook.items
```

---

## 5. 并发注意事项

### 5.1 线程内新连接

当测试代码在 **同一线程内** 创建新连接时：

```python
def test_new_connection_in_thread(prefixed_schema_context, migrated_db_prefixed):
    # 主线程已设置全局 SchemaContext
    
    # 新连接会使用全局 SchemaContext
    conn = get_connection(dsn=migrated_db_prefixed["dsn"])
    # ✅ search_path: tp_xxx_logbook, tp_xxx_identity, ...
```

### 5.2 新线程内的连接

当在 **新线程** 中创建连接时，需要显式传递 schema_context：

```python
import threading

def test_thread_needs_explicit_context(prefixed_schema_context, migrated_db_prefixed):
    ctx = prefixed_schema_context
    dsn = migrated_db_prefixed["dsn"]
    
    def worker():
        # ⚠️ 新线程，全局 SchemaContext 可能不可见（取决于实现）
        # 推荐显式传递 schema_context
        conn = get_connection(dsn=dsn, schema_context=ctx)
        # ✅ 确保使用正确的 search_path
    
    t = threading.Thread(target=worker)
    t.start()
    t.join()
```

### 5.3 全局上下文生命周期

```
prefixed_schema_context fixture (module scope)
    │
    │  ┌── set_schema_context(ctx) ─────────────────────┐
    │  │                                                │
    │  │   test_1() ── get_connection() ✅ 使用 ctx     │
    │  │   test_2() ── get_connection() ✅ 使用 ctx     │
    │  │   test_3() ── get_connection() ✅ 使用 ctx     │
    │  │                                                │
    │  └── reset_schema_context() ──────────────────────┘
    │
    ▼
下一个 module (新的 schema_prefix)
```

### 5.4 Teardown 强清理

`prefixed_schema_context` fixture 在 teardown 时执行强清理：

1. `reset_schema_context()` - 重置全局 SchemaContext
2. `logbook_adapter.reset_adapter()` - 重置 adapter 单例
3. 恢复 `ENGRAM_TESTING` 环境变量

---

## 6. 迁移指南

从 TRUNCATE 方案迁移到 schema_prefix 方案的步骤清单。

### 6.1 迁移步骤清单

#### 步骤 1: 识别需要迁移的测试

检查使用以下模式的测试：

```python
# 特征 1: 使用 TRUNCATE 清理
cur.execute("TRUNCATE table1, table2")

# 特征 2: 自定义 fixture 创建连接并提交
@pytest.fixture
def db_conn_for_xxx(migrated_db):
    conn = psycopg.connect(...)
    conn.commit()  # 需要提交才能被其他连接看到
    yield conn

# 特征 3: 测试 adapter 内部连接行为
adapter.some_method()  # adapter 内部创建新连接
```

#### 步骤 2: 替换 fixture 依赖

```python
# 迁移前
@pytest.fixture
def db_conn_for_two_phase(migrated_db: dict):
    dsn = migrated_db["dsn"]
    conn = psycopg.connect(dsn, autocommit=False)
    with conn.cursor() as cur:
        cur.execute("SET search_path TO logbook, governance, ...")
        cur.execute("TRUNCATE logbook.outbox_memory, governance.write_audit")
    conn.commit()
    yield conn
    conn.rollback()
    conn.close()

# 迁移后
# 直接使用 conftest.py 中的 db_conn_prefixed_committed
def test_xxx(prefixed_schema_context, migrated_db_prefixed, db_conn_prefixed_committed):
    # 不需要 TRUNCATE，每个 module 使用独立的 schema
    pass
```

#### 步骤 3: 更新 adapter 创建方式

```python
# 迁移前
adapter = LogbookAdapter(dsn=migrated_db["dsn"])

# 迁移后
# adapter 会自动使用全局 SchemaContext
adapter = LogbookAdapter(dsn=migrated_db_prefixed["dsn"])
# 或通过 config 创建
adapter = LogbookAdapter.from_config(config)
```

#### 步骤 4: 更新断言中的 schema 引用

```python
# 迁移前（硬编码 schema 名）
cur.execute("SELECT * FROM governance.write_audit")

# 迁移后（使用 fixture 提供的 schema 名，或依赖 search_path）
# 方式 A: 依赖 search_path（推荐）
cur.execute("SELECT * FROM write_audit")  # search_path 会解析到正确的 schema

# 方式 B: 使用 fixture 提供的 schema 名
schemas = migrated_db_prefixed["schemas"]
cur.execute(f"SELECT * FROM {schemas['governance']}.write_audit")
```

#### 步骤 5: 移除 TRUNCATE 相关代码

```python
# 迁移前
with conn.cursor() as cur:
    cur.execute("TRUNCATE logbook.outbox_memory, governance.write_audit")
conn.commit()

# 迁移后
# 删除以上代码，schema_prefix 隔离不需要 TRUNCATE
```

#### 步骤 6: 更新 cleanup 辅助函数调用

如果仍需要在非 schema_prefix 测试中使用 TRUNCATE：

```python
from tests.gateway.conftest import cleanup_two_phase_tables

# 使用辅助函数（支持 schema 参数）
cleanup_two_phase_tables(
    conn,
    logbook_schema=schemas.get("logbook", "logbook"),
    governance_schema=schemas.get("governance", "governance"),
)
```

### 6.2 迁移检查清单

| 检查项 | 完成 |
|-------|------|
| 识别所有使用 TRUNCATE 的测试 | ☐ |
| 替换 `migrated_db` 为 `migrated_db_prefixed` | ☐ |
| 添加 `prefixed_schema_context` 依赖 | ☐ |
| 替换自定义连接 fixture 为 `db_conn_prefixed_committed` | ☐ |
| 移除 TRUNCATE 语句 | ☐ |
| 更新 adapter 创建方式 | ☐ |
| 更新硬编码的 schema 引用 | ☐ |
| 运行测试验证 | ☐ |
| 使用 `pytest -n auto` 验证并行执行 | ☐ |

### 6.3 回滚方案

如果迁移后出现问题，可以临时回滚：

```python
# 在测试文件中添加兼容层
@pytest.fixture
def db_conn_for_two_phase(migrated_db_prefixed, db_conn_prefixed_committed):
    """兼容层：映射新 fixture 到旧名称"""
    return db_conn_prefixed_committed
```

---

## 附录 A: Fixture 代码引用

### A.1 prefixed_schema_context

```python
# tests/gateway/conftest.py

@pytest.fixture(scope="module")
def prefixed_schema_context(
    schema_prefix: str,
) -> Generator["SchemaContext", None, None]:
    """
    设置带前缀的 SchemaContext（module scope）
    
    - 设置 ENGRAM_TESTING=1 环境变量
    - 调用 set_schema_context() 设置全局上下文
    - teardown 时执行强清理
    """
    from engram.logbook.schema_context import (
        SchemaContext,
        reset_schema_context,
        set_schema_context,
    )
    
    old_engram_testing = os.environ.get("ENGRAM_TESTING")
    os.environ["ENGRAM_TESTING"] = "1"
    
    ctx = SchemaContext(schema_prefix=schema_prefix)
    set_schema_context(ctx)
    
    yield ctx
    
    # 强 teardown
    reset_schema_context()
    try:
        from engram.gateway import logbook_adapter
        logbook_adapter.reset_adapter()
    except ImportError:
        pass
    
    if old_engram_testing is None:
        os.environ.pop("ENGRAM_TESTING", None)
    else:
        os.environ["ENGRAM_TESTING"] = old_engram_testing
```

### A.2 migrated_db_prefixed

```python
@pytest.fixture(scope="module")
def migrated_db_prefixed(
    test_db_info: dict,
    prefixed_schema_context: "SchemaContext",
) -> Generator[dict, None, None]:
    """
    在测试数据库中执行迁移，使用带前缀的 schema（module scope）
    
    - 调用 run_migrate(dsn=..., schema_prefix=..., quiet=True)
    - teardown 时 DROP SCHEMA <prefix>_* CASCADE
    """
    from engram.logbook.migrate import run_migrate
    from engram.logbook.schema_context import SCHEMA_SUFFIXES
    
    dsn = test_db_info["dsn"]
    schema_prefix = prefixed_schema_context.schema_prefix
    
    result = run_migrate(dsn=dsn, schema_prefix=schema_prefix, quiet=True)
    
    if not result.get("ok"):
        pytest.fail(f"带前缀的数据库迁移失败: {result.get('message')}")
    
    yield {
        "dsn": dsn,
        "db_name": test_db_info["db_name"],
        "worker_id": test_db_info["worker_id"],
        "schema_prefix": schema_prefix,
        "schema_context": prefixed_schema_context,
        "schemas": prefixed_schema_context.all_schemas,
    }
    
    # teardown: DROP SCHEMA
    try:
        conn = psycopg.connect(dsn, autocommit=True)
        with conn.cursor() as cur:
            for suffix in SCHEMA_SUFFIXES:
                schema_name = f"{schema_prefix}_{suffix}"
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
        conn.close()
    except Exception as e:
        warnings.warn(f"清理带前缀的 schema 失败: {e}")
```

### A.3 db_conn_prefixed_committed

```python
@pytest.fixture(scope="function")
def db_conn_prefixed_committed(
    migrated_db_prefixed: dict,
) -> Generator[psycopg.Connection, None, None]:
    """
    提供一个使用带前缀 schema 的可提交数据库连接（function scope）
    
    - SET search_path TO <prefixed_schema_context.search_path_sql>
    - 返回可 commit 的连接
    """
    dsn = migrated_db_prefixed["dsn"]
    ctx = migrated_db_prefixed["schema_context"]
    schemas = migrated_db_prefixed["schemas"]
    
    conn = psycopg.connect(dsn, autocommit=False)
    
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {ctx.search_path_sql}")
    
    verify_search_path(conn, schemas)
    
    yield conn
    
    conn.close()
```

---

## 附录 B: get_connection() 源码引用

```426:556:src/engram/logbook/db.py
def get_connection(
    dsn: str | None = None,
    config: Config | None = None,
    autocommit: bool = False,
    schema_context: SchemaContext | None = None,
    search_path: Sequence[str] | str | None = None,
    statement_timeout_ms: int | None = None,
) -> psycopg.Connection[Any]:
    """
    获取数据库连接。

    连接后会设置 search_path，优先级:
    1. 显式传入的 search_path 参数
    2. schema_context 提供的 search_path
    3. config 中的 postgres.search_path
    4. 全局 SchemaContext 的 search_path（当有 schema_prefix 时）
    5. 默认 search_path（DEFAULT_SEARCH_PATH）
    """
    # ... (见 src/engram/logbook/db.py)
```

---

*文档更新: 2026-02-01*
