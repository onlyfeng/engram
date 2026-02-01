# Iteration 2 计划

## 概述

| 字段 | 内容 |
|------|------|
| **迭代编号** | Iteration 2 |
| **开始日期** | 2026-01-31 |
| **状态** | ✅ PASS (已完成) |
| **SSOT** | 本文档 + [iteration_2_regression.md](iteration_2_regression.md) |

---

## 迭代目标

### 主要目标

本迭代聚焦于**代码质量与工程规范化**，通过以下五个里程碑完成：

| # | 里程碑 | 目标描述 |
|---|--------|----------|
| M1 | 脚本入口收敛 | 统一 CLI 入口，清理根目录冗余脚本，所有命令通过 `python -m engram.*` 调用 |
| M2 | SQL 迁移整理 | 重新编号 SQL 迁移脚本，消除文件删除/重命名冲突，确保迁移幂等性 |
| M3 | CI 硬化 | 加强 CI 流水线：lint 检查强制失败、测试覆盖率门槛、迁移验证必跑 |
| M4 | Gateway 模块化 | 拆分 `main.py` 单体，按职责分离路由、中间件、生命周期管理 |
| M5 | 文档对齐 | 文档与代码同步，清理过期文档，补充缺失的组件契约文档 |

### 非目标（范围边界）

| 非目标 | 原因 |
|--------|------|
| 新功能开发 | 本迭代专注于技术债清理，不引入新业务功能 |
| 性能优化 | 当前性能满足需求，优化留待后续迭代 |
| 多租户改造 | 架构调整过大，需独立迭代规划 |
| OpenMemory 集成增强 | 依赖外部服务稳定性，当前集成已满足需求 |
| 前端/UI 开发 | Engram 定位为后端服务，不含前端组件 |

---

## 验收门禁

### M1: 脚本入口收敛

| 验收项 | 验证命令 | CI Job |
|--------|----------|--------|
| 根目录无冗余 `*.py` 脚本 | `ls *.py \| wc -l` 结果 ≤ 3 | `lint` |
| CLI 命令可通过模块调用 | `python -m engram.logbook.cli.db_migrate --help` | `test` |
| 旧命令别名输出 deprecation 警告 | 手动验证 | - |

### M2: SQL 迁移整理

| 验收项 | 验证命令 | CI Job |
|--------|----------|--------|
| SQL 文件连续编号（01-99） | `ls sql/*.sql` 无间隙 | `schema-validate` |
| 迁移脚本幂等执行 | `make migrate && make migrate` 无报错 | `test` |
| 迁移验证通过 | `python -m engram.logbook.cli.db_migrate --verify` | `test` |

### M3: CI 硬化

| 验收项 | 验证命令 | CI Job |
|--------|----------|--------|
| Lint 检查强制失败 | `ruff check src/ tests/` 返回非零则 CI 失败 | `lint` |
| 测试覆盖率 ≥ 70% | `pytest --cov --cov-fail-under=70` | `test` |
| 迁移验证步骤必跑 | CI 日志包含 "Verify database migrations" | `test` |
| Schema 校验必跑 | CI 日志包含 "Schema Validation" | `schema-validate` |

### M4: Gateway 模块化

| 验收项 | 验证命令 | CI Job |
|--------|----------|--------|
| `main.py` 行数 ≤ 200 | `wc -l src/engram/gateway/main.py` | `lint` |
| 新增模块文件存在 | `ls src/engram/gateway/{routes,middleware,lifecycle}.py` | `test` |
| DI 边界检查通过 | `python scripts/ci/check_gateway_di_boundaries.py --verbose` | `lint` |
| Gateway 启动测试通过 | `pytest tests/gateway/test_gateway_startup.py -v` | `test` |
| DI 边界契约测试通过 | `pytest tests/gateway/test_di_boundaries.py -v` | `test` |
| ImportError 契约测试通过 | `pytest tests/gateway/test_importerror_optional_deps_contract.py -v` | `test` |
| 所有 Gateway 测试通过 | `make test-gateway` | `test` |

### M5: 文档对齐

| 验收项 | 验证命令 | CI Job |
|--------|----------|--------|
| README 文档索引与实际文件一致 | `scripts/docs/verify_doc_links.py` | `schema-validate` |
| 无过期文档引用 | 文档内链接均可访问 | `schema-validate` |
| 组件契约文档完整 | `docs/contracts/` 目录包含所有组件边界定义 | - |

---

## 证据要求

### 回归记录

每次验收执行后，需在 [iteration_2_regression.md](iteration_2_regression.md) 记录：

| 字段 | 说明 |
|------|------|
| **执行日期** | YYYY-MM-DD |
| **Commit** | 被验证的 commit SHA |
| **执行命令** | 实际运行的命令 |
| **结果** | PASS / PARTIAL / FAIL |
| **修复文件清单** | 本次修复的文件列表 |

### 产物目录

| 产物 | 路径 | 说明 |
|------|------|------|
| **回归记录** | `docs/acceptance/iteration_2_regression.md` | 版本化的回归记录 |
| **本地迭代笔记** | `.iteration/` | 本地化，不纳入版本控制 |

---

## 风险评估

| 风险 | 等级 | 缓解措施 |
|------|------|----------|
| SQL 迁移重编号导致已部署环境不兼容 | **高** | 提供迁移升级脚本；文档说明升级路径 |
| CLI 入口变更破坏现有脚本 | **中** | 保留旧入口别名一段时间，输出 deprecation 警告 |
| CI 强制 lint 导致大量历史代码报错 | **中** | 分阶段启用：先 warn-only，再逐步强制 |
| Gateway 拆分引入模块导入问题 | **中** | 充分测试，保持向后兼容的公开 API |
| 文档更新滞后于代码变更 | **低** | 在 PR 模板中强制要求文档同步检查 |

---

## 里程碑详细计划

### M1: 脚本入口收敛

**目标**：将根目录下散落的脚本入口迁移到 `src/engram/` 包结构中。

**详细规范**：参见 [cli_entrypoints.md](../architecture/cli_entrypoints.md)

**变更范围**：
- 移动 `db.py` → `src/engram/logbook/cli/db.py`
- 移动 `db_bootstrap.py` → `src/engram/logbook/cli/db_bootstrap.py`
- 移动 `db_migrate.py` → `src/engram/logbook/cli/db_migrate.py`
- 更新 `pyproject.toml` 中的 `[project.scripts]` 入口
- 更新 Makefile 中的命令引用

**调用优先级**（详见 cli_entrypoints.md）：
1. Console Scripts（`engram-*`）- 首选
2. `python -m engram.*` - 开发/CI 场景
3. `scripts/*.py` - 运维工具
4. 根目录脚本 - 兼容期内可用，输出 deprecation 警告

**向后兼容**：
- 根目录保留旧脚本，内容改为 import 转发 + deprecation 警告
- 兼容期与移除条件见 [cli_entrypoints.md](../architecture/cli_entrypoints.md) 第 4 节

---

### M2: SQL 迁移整理

**目标**：修复 SQL 迁移文件编号混乱问题，确保连续编号且幂等。

**当前问题**（参考 git status）：
- 删除：`05_scm_sync_runs.sql`, `06_scm_sync_locks.sql`, `07_scm_sync_jobs.sql`
- 新增/修改：`08_scm_sync_jobs.sql`, `09_evidence_uri_column.sql`

**解决方案**：
1. 统一重新编号 `sql/*.sql`
2. 更新 `src/engram/logbook/migrate.py` 中的迁移版本检测逻辑
3. 编写迁移文档说明升级路径

---

### M3: CI 硬化

**目标**：强化 CI 流水线，消除 `|| true` 宽松处理。

**变更范围**（`.github/workflows/ci.yml`）：

```yaml
# 当前（宽松）
- name: Run ruff check
  run: ruff check src/ tests/ || true

# 目标（严格）
- name: Run ruff check
  run: ruff check src/ tests/
```

**新增检查**：
- 测试覆盖率门槛
- 迁移脚本幂等性验证
- 文档链接检查

---

### M4: Gateway 模块化

**目标**：拆分 `src/engram/gateway/main.py`，提升可维护性。

**实际模块结构**（已完成）：

```
src/engram/gateway/
├── main.py           # 应用入口：启动检查 + 调用 app.create_app()
├── app.py            # FastAPI 应用创建（create_app 函数）
├── routes.py         # API 路由定义（/mcp, /memory/*, /reliability/*, /governance/*）
├── middleware.py     # 中间件（CorrelationIdMiddleware、全局异常处理）
├── lifecycle.py      # 启动/关闭生命周期钩子（lifespan 函数）
├── container.py      # 依赖容器（GatewayContainer 全局单例）
├── di.py             # 依赖注入类型定义（GatewayDeps、GatewayDepsProtocol）
├── config.py         # 配置管理（GatewayConfig、validate_config）
├── handlers/         # 业务逻辑层（接收 deps 参数，禁止直接导入全局依赖）
│   ├── memory_store.py
│   ├── memory_query.py
│   ├── governance_update.py
│   └── evidence_upload.py
├── services/         # 服务层（同 handlers，遵循 DI 边界）
│   ├── audit_service.py
│   ├── actor_validation.py
│   └── hash_utils.py
└── ... (其他现有模块)
```

**职责划分**：

| 模块 | 职责 | import-time 安全 |
|------|------|------------------|
| `main.py` | 启动预检查、调用 `create_app()`、`uvicorn.run()` | ✓（预检查失败则 exit） |
| `app.py` | 创建 FastAPI 应用、组装中间件和路由 | ✓（不触发 get_config） |
| `routes.py` | 路由注册、工具执行器定义 | ✓（延迟导入 handlers） |
| `middleware.py` | correlation_id 生成/传递、全局异常处理 | ✓（延迟导入 mcp_rpc） |
| `lifecycle.py` | lifespan 管理、依赖预热 | ✓（try-except 优雅降级） |
| `container.py` | GatewayContainer 单例管理 | ✓（延迟初始化） |
| `handlers/*` | 业务逻辑实现 | ✓（禁止全局依赖） |

**必须通过的验收命令**：

```bash
# 1. DI 边界检查（必须）
python scripts/ci/check_gateway_di_boundaries.py --verbose

# 2. Gateway 核心契约测试（必须）
pytest tests/gateway/test_gateway_startup.py \
       tests/gateway/test_di_boundaries.py \
       tests/gateway/test_importerror_optional_deps_contract.py -v

# 3. 完整 Gateway 测试（推荐）
pytest tests/gateway/ -v
```

---

### M5: 文档对齐

**目标**：确保文档与代码实现同步。

**检查清单**：
- [x] `README.md` 文档索引与 `docs/` 实际结构一致
- [x] `docs/architecture/README.md` 包含本迭代计划文档引用
- [x] 所有 ADR 状态标注正确
- [x] 环境变量文档与代码中的默认值一致
- [x] CLI 命令帮助信息与文档一致

---

## 依赖关系

```
M1 (脚本入口收敛)
    │
    ├──> M2 (SQL 迁移整理) ──> M3 (CI 硬化)
    │
    └──> M4 (Gateway 模块化) ──> M5 (文档对齐)
```

- M1 是基础，需先完成入口统一
- M2 和 M4 可并行
- M3 依赖 M2（迁移整理后才能强化迁移验证）
- M5 依赖所有其他里程碑完成后进行

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [iteration_2_regression.md](iteration_2_regression.md) | 详细回归记录与修复清单 |
| [00_acceptance_matrix.md](00_acceptance_matrix.md) | 验收测试矩阵总览 |
| [cli_entrypoints.md](../architecture/cli_entrypoints.md) | CLI 入口清单与调用规范（M1 核心参考） |
| [naming.md](../architecture/naming.md) | 命名规范（M1 需遵循） |
| [adr_gateway_audit_atomicity.md](../architecture/adr_gateway_audit_atomicity.md) | Gateway 审计原子性（M4 参考） |
| [gateway_logbook_boundary.md](../contracts/gateway_logbook_boundary.md) | Gateway/Logbook 边界契约（M4 参考） |

---

更新时间：2026-02-01
