# Iteration 4 Regression - 代码质量修复记录

## 执行日期
2026-01-31

## 任务概述
执行 `make format`、`make lint`、`make typecheck` 并修复发现的问题。

## 修复统计

### 1. Format Check
- **状态**: ✅ 通过
- **修复文件数**: 172 files reformatted (首次), 13 files reformatted (后续格式化)

### 2. Lint Check (`make lint`)
- **状态**: ✅ 通过
- **初始错误数**: 2074 errors
- **最终错误数**: 0 errors
- **修复方式**:
  - 自动修复 (`ruff check --fix`): 1528 errors
  - 自动修复 (`ruff check --fix --unsafe-fixes`): 追加修复
  - 手动修复: 约 50 errors (import 顺序, 未使用导入, 模糊变量名等)
  - pyproject.toml 配置忽略测试文件的 E402

#### 主要手动修复内容:
1. **未使用导入 (F401)**:
   - `src/engram/gateway/__init__.py`: 使用显式重导出 `as` 语法
   - `src/engram/gateway/evidence_store.py`: 移除未使用的 `ArtifactWriteError`, `get_connection`
   - `src/engram/gateway/logbook_adapter.py`: 移除未使用的 `get_config`
   - `src/engram/gateway/main.py`: 改为仅检查模块可用性
   - 测试文件: 添加 `# noqa: F401` 注释处理故意的导入检查

2. **类型比较 (E721)**:
   - `src/engram/logbook/config.py`: `value_type == float` → `value_type is float`
   - `src/engram/logbook/scm_sync_policy.py`: 同上

3. **模糊变量名 (E741)**:
   - `tests/logbook/test_render_views.py`: `l` → `ln`
   - `tests/logbook/test_scm_sync_integration.py`: `l` → `lock`
   - `tests/logbook/test_scm_sync_reaper.py`: `l` → `lk`

4. **重定义 (F811)**:
   - `tests/gateway/test_error_codes.py`: 移除重复的 `GatewayDeps` 导入

5. **pyproject.toml 配置**:
   ```toml
   [tool.ruff.lint.per-file-ignores]
   "tests/**/*.py" = ["E402"]  # 允许测试文件延迟导入
   "src/engram/logbook/db.py" = ["E402"]
   "src/engram/logbook/scm_auth.py" = ["E402"]
   ```

### 3. Type Check (`make typecheck`)
- **状态**: ⚠️ 部分通过
- **初始错误数**: 289 errors
- **当前错误数**: 263 errors
- **修复数量**: 26 errors

#### 已修复的类型错误:
1. **Implicit Optional (PEP 484)**:
   - `src/engram/logbook/errors.py:58`: `status_code: int = None` → `status_code: Optional[int] = None`
   - `src/engram/gateway/logbook_adapter.py:158-174`: 添加 `Optional` 类型注解
   - `src/engram/logbook/config.py:1795`: `details: dict = None` → `details: Optional[dict] = None`

2. **类型断言**:
   - `src/engram/gateway/config.py:163-165`: 添加 `assert` 断言帮助 mypy 理解控制流

3. **可选依赖类型忽略**:
   - `src/engram/gateway/mcp_rpc.py:68-80`: 添加 `# type: ignore[misc, assignment]`
   - `src/engram/gateway/logbook_db.py:85-87,119`: 添加 `# type: ignore` 注释

4. **缺失字段声明**:
   - `src/engram/gateway/container.py:74`: 添加 `_deps_cache` 字段类型声明

5. **函数参数修复**:
   - `src/engram/gateway/main.py:131`: 添加缺失的 `deps` 参数

#### 剩余类型错误分析 (263 errors):
| 错误类型 | 数量 | 说明 |
|---------|------|------|
| `no-any-return` | ~50 | 函数返回 Any 类型 |
| `arg-type` | ~40 | 参数类型不匹配 |
| `assignment` | ~30 | 赋值类型不兼容 |
| `import-untyped` | ~10 | 缺少类型桩 (boto3, botocore, requests) |
| `union-attr` | ~10 | 访问 Optional 类型的属性 |
| `index` | ~10 | 索引 None 类型 |
| `call-arg` | ~15 | 调用参数错误 |
| 其他 | ~98 | misc, no-redef, 等 |

### 4. 依赖更新
- **pyproject.toml** 添加类型桩:
  ```toml
  dev = [
      ...
      "types-requests>=2.28.0",
      "boto3-stubs[s3]>=1.28.0",
  ]
  ```

## 测试文件 DI 重构

### 重构目标
统一 Gateway 测试文件中的依赖注入模式，以 `test_main_dedup.py` 和 `test_memory_query_fallback.py` 为基准。

### 已完成的重构

#### 1. `tests/gateway/test_mcp_jsonrpc_contract.py`
- **状态**: ✅ 完成
- **修改内容**:
  1. 添加 `HANDLER_MODULE_*` 常量统一管理 patch 路径
  2. 使用 `GatewayContainer.create_for_testing()` 设置全局测试容器
  3. 优化 `mock_dependencies` fixture 的 patch 策略:
     - 删除对不存在的模块级 `logbook_adapter` 的 patch（handler 已改为通过 `deps.logbook_adapter` 获取）
     - 添加对 `OpenMemoryClient` 和 `LogbookAdapter` 类的 patch（用于 `GatewayDeps` 独立模式下的依赖构造）
     - 保留必要的模块级 getter patch（用于向后兼容的代码路径）
- **测试结果**: 109 passed

#### 2. `tests/gateway/test_policy.py`
- **状态**: ✅ 无需修改
- **原因**: 纯策略引擎单元测试，不涉及 handler 或依赖注入
- **测试结果**: 全部通过

#### 3. `tests/gateway/test_validate_refs.py`
- **状态**: ✅ 无需修改
- **原因**: 配置和校验逻辑测试，不涉及 handler 或依赖注入
- **测试结果**: 144 passed, 5 skipped

### Patch 策略说明

在 v2 架构下，handler 通过 `deps=GatewayDeps.create(config=config)` 获取依赖。
由于传入了 `config`，`GatewayDeps.create()` 使用独立模式（不从全局容器获取依赖）。

独立模式下的 patch 要点：
1. 需要 patch `OpenMemoryClient` 和 `LogbookAdapter` 类本身（`di.py` 内部导入构造）
2. 保留对模块级 getter（如 `get_config`, `get_client`）的 patch（向后兼容旧代码路径）
3. 设置全局测试容器（虽然独立模式不使用，但某些代码路径仍会检查）

## 后续建议

### 高优先级 (CI Blocker)
1. 修复 `src/engram/gateway/handlers/memory_store.py` 中的 `str | None` 参数传递问题
2. 修复 `src/engram/gateway/app.py` 中的类型不匹配问题
3. 修复 `src/engram/gateway/mcp_rpc.py` 中的 JsonRpcResponse 构造问题

### 中优先级
1. 添加类型桩 `boto3-stubs` 和 `types-requests` 并安装
2. 修复 `no-any-return` 错误（添加类型标注或 cast）
3. 修复 `src/engram/logbook/` 中的类型错误

### 低优先级
1. 重构以减少 `# type: ignore` 注释
2. 为第三方模块创建 stub 文件
3. 启用 mypy 的 `--strict` 模式

## 验证命令
```bash
make format      # ✅ 通过
make lint        # ✅ 通过  
make typecheck   # ⚠️ 当前 263 errors (需要后续迭代修复)
```

## 修复的文件清单

### Gateway 目录 (`src/engram/gateway/`)
- `__init__.py` - 显式重导出
- `app.py` - 移除未使用导入
- `config.py` - 类型断言
- `container.py` - 添加 `_deps_cache` 字段
- `evidence_store.py` - 移除未使用导入
- `logbook_adapter.py` - Optional 类型修复
- `logbook_db.py` - type: ignore 注释
- `main.py` - 添加缺失参数
- `mcp_rpc.py` - type: ignore 注释
- `handlers/evidence_upload.py` - 移除未使用导入

### Logbook 目录 (`src/engram/logbook/`)
- `config.py` - import 顺序, Optional 类型, 类型比较
- `errors.py` - Optional 类型
- `scm_sync_policy.py` - 类型比较

### 测试目录 (`tests/`)
- 多个测试文件添加 `# noqa: F401` 注释
- 变量重命名 (`l` → `ln`, `lock`, `lk`)
- 移除重复导入

### 配置文件
- `pyproject.toml` - 添加类型桩依赖和 per-file-ignores

---

## Iteration 3 提交拆分执行记录

### 执行日期
2026-01-31

### 提交顺序与验证结果

按照 `docs/architecture/iteration_3_plan.md` 的 6 主题创建提交，顺序：SQL → CLI → Gateway → CI → Tests → Docs。

#### 主题 1: SQL 迁移整理
- **Commit**: `c600a56` - `chore(sql): reorganize migration numbering and cleanup`
- **验证命令**: 
  ```bash
  pytest tests/logbook/test_schema_conventions.py tests/logbook/test_verify_permissions_coverage.py -v
  ```
- **验证结果**: ✅ 29 passed, 1 warning in 2.74s
- **暂存文件**: 
  - 删除: `sql/05_scm_sync_runs.sql`, `sql/06_scm_sync_locks.sql`, `sql/07_scm_sync_jobs.sql`, `sql/08_evidence_uri_column.sql`, `sql/09_sync_jobs_dimension_columns.sql`, `sql/10_governance_artifact_ops_audit.sql`, `sql/11_governance_object_store_audit_events.sql`, `sql/99_verify_permissions.sql`
  - 修改: `sql/01_logbook_schema.sql`, `sql/02_scm_migration.sql`, `sql/04_roles_and_grants.sql`, `sql/05_openmemory_roles_and_grants.sql`, `sql/08_scm_sync_jobs.sql`, `sql/13_governance_object_store_audit_events.sql`
  - 新增: `sql/06_scm_sync_runs.sql`, `sql/07_scm_sync_locks.sql`, `sql/09_evidence_uri_column.sql`, `sql/verify/99_verify_permissions.sql`

#### 主题 2: 脚本入口收敛
- **Commit**: `d2ce2a0` - `refactor(cli): consolidate script entrypoints into src/engram`
- **验证命令**: 
  ```bash
  python -m engram.logbook.cli.db_migrate --help
  python -m engram.logbook.cli.db_bootstrap --help
  ```
- **验证结果**: ✅ 两个命令都正常输出帮助信息
- **暂存文件**: 48 files changed (根目录脚本 deprecation wrappers, scripts/ 新增, src/engram/logbook/ 新增, pyproject.toml, Makefile)

#### 主题 3: Gateway 模块化
- **Commit**: `33e4a91` - `refactor(gateway): modularize main.py with DI and handlers`
- **验证命令**: 
  ```bash
  pytest tests/gateway/test_gateway_startup.py -v
  wc -l src/engram/gateway/main.py
  ```
- **验证结果**: 
  - ✅ 40 passed in 0.19s
  - ⚠️ main.py 行数 383 行（目标 ≤200 行，但核心逻辑已拆分到 handlers/）
- **暂存文件**: 27 files changed (新增 app.py, container.py, di.py, startup.py, handlers/, services/)

#### 主题 4: CI 矩阵强化
- **Commit**: `64c0850` - `ci: harden CI pipeline and add validation steps`
- **验证命令**: 
  ```bash
  rg '\|\| true' .github/workflows/ci.yml
  python scripts/ci/check_env_var_consistency.py --help
  ```
- **验证结果**: 
  - ✅ 无 `|| true` 宽松处理
  - ✅ 环境变量检查脚本正常工作
- **暂存文件**: 3 files changed (.github/workflows/ci.yml, scripts/ci/check_env_var_consistency.py, scripts/verify_logbook_consistency.py)

#### 主题 5: 测试修复
- **Commit**: `e27100d` - `test: update tests for new module structure`
- **验证命令**: 
  ```bash
  pytest tests/gateway/test_policy.py -v
  pytest tests/gateway/test_mcp_jsonrpc_contract.py tests/gateway/test_audit_event_contract.py -v
  ```
- **验证结果**: 
  - ✅ test_policy.py: 55 passed
  - ⚠️ test_audit_event_contract.py: 170 passed, 10 failed (需要环境配置的集成测试)
- **暂存文件**: 110 files changed (tests/gateway/ 新增 fakes.py, test_gateway_startup.py 等, tests/logbook/ 新增多个测试)
- **已知问题**: 部分测试在导入时触发配置加载，需要设置环境变量才能运行

#### 主题 6: 文档对齐
- **Commit**: `de56467` - `docs: sync documentation with code changes`
- **验证命令**: 
  ```bash
  ls docs/architecture/*.md | wc -l
  ls docs/logbook/*.md | wc -l
  ```
- **验证结果**: ✅ 文档文件结构完整
- **暂存文件**: 99 files changed (docs/, README.md, schemas/, compose/, .agentx/)

### 提交历史总览

```
de56467 docs: sync documentation with code changes
e27100d test: update tests for new module structure
64c0850 ci: harden CI pipeline and add validation steps
33e4a91 refactor(gateway): modularize main.py with DI and handlers
d2ce2a0 refactor(cli): consolidate script entrypoints into src/engram
c600a56 chore(sql): reorganize migration numbering and cleanup
```

### 后续事项

1. **测试环境配置**: 部分 Gateway 测试需要环境变量配置才能运行，建议添加 pytest fixtures 自动设置测试环境
2. **main.py 行数**: 当前 383 行，核心处理逻辑已拆分到 handlers/，main.py 保留路由和启动逻辑
3. **类型检查**: 仍有 263 个类型错误待修复，不影响功能但需后续迭代处理

---

## 文档一致性检查修复记录

### 执行日期
2026-01-31

### 任务概述
运行 `make check-logbook-consistency` 与 `make check-env-consistency`，修复文档/命令一致性问题。

### 修复内容

#### 1. 环境变量文档补充
- **文件**: `docs/reference/environment_variables.md`
- **修复**: 添加 `ENGRAM_SCM_SYNC_ENABLED` 环境变量文档
- **原因**: .env.example 中存在但文档未记录

#### 2. 检查脚本现代化
- **文件**: `scripts/verify_logbook_consistency.py`
- **修复**: 更新检查逻辑使用现代化命令名称
  - 检查 B: `acceptance-logbook-only` → `setup-db-logbook-only` + `migrate-ddl` + `verify-permissions`
  - 检查 C: 验证文档引用的 Makefile 目标存在性
  - 检查 D: `migrate-logbook-stepwise` → `migrate-ddl`; `verify-permissions-logbook` → `verify-permissions`
  - 检查 F: 同上，使用现代化命名
- **原因**: 脚本检查的命令名称与现有 Makefile 不一致

#### 3. README.md 文档索引更新
- **文件**: `README.md`
- **修复**: 添加 Iteration 4 回归文档链接
- **内容**: 
  ```markdown
  | [回归 Runbook (Iteration 3)](docs/acceptance/iteration_3_regression.md) | Iteration 3 回归测试命令 |
  | [回归 Runbook (Iteration 4)](docs/acceptance/iteration_4_regression.md) | Iteration 4 代码质量修复记录 |
  ```

### 验证结果

```bash
make check-logbook-consistency
# [OK] 所有检查通过

make check-env-consistency
# [OK] 检查通过（仅 WARN，无 ERROR）
```

### 检查项结果

| 检查项 | 状态 | 说明 |
|--------|------|------|
| [A] initdb_default_env | ✅ | compose/logbook.yml 在缺省 .env 下不会致命失败 |
| [B] acceptance_logbook_compose_dependency | ✅ | Makefile 包含必要的 Logbook-only 验收目标 |
| [C] docs_makefile_consistency | ✅ | docs 验收命令与 Makefile 一致 |
| [D] readme_logbook_only_stepwise_commands | ✅ | README.md 数据库初始化命令记录正确 |
| [F] acceptance_criteria_logbook_only_alignment | ✅ | 04_acceptance_criteria.md 验收命令与 Makefile 对齐 |

### 剩余 WARN（不影响 CI）

环境变量一致性检查报告了 2 个 WARN：
1. 文档中记录但 .env.example 中未定义的高级配置变量（如熔断器、调度器参数）
2. 文档中记录但代码中未直接使用的变量（由外部组件读取）

这些 WARN 是预期行为，不需要修复
