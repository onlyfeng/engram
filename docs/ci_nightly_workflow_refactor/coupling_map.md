# CI/Nightly/Release Workflow 耦合映射

> 本文档记录 workflow 与 Makefile targets、环境变量、产物路径的耦合关系。

---

## 0. 摘要（受控）

> 本节内容由 `workflow_contract.v2.json` 自动渲染，请勿手动修改。
> 更新命令：`python -m scripts.ci.render_workflow_contract_docs --target coupling_map --with-markers`

### CI Workflow Jobs 清单

<!-- BEGIN:CI_JOBS_LIST -->
| Job ID | Job Name |
|--------|----------|
| `test` | Test (Python ${{ matrix.python-version }}) |
| `lint` | Lint |
| `no-iteration-tracked` | No .iteration/ Tracked Files |
| `env-var-consistency` | Environment Variable Consistency |
| `schema-validate` | Schema Validation |
| `logbook-consistency` | Logbook Consistency Check |
| `migration-sanity` | Migration Sanity Check |
| `sql-safety` | SQL Migration Safety Check |
| `gateway-di-boundaries` | Gateway DI Boundaries Check |
| `scm-sync-consistency` | SCM Sync Consistency Check |
| `gateway-error-reason-usage` | Gateway ErrorReason Usage Check |
| `gateway-import-surface` | Gateway Import Surface Check |
| `gateway-public-api-surface` | Gateway Public API Import Surface Check |
| `gateway-correlation-id-single-source` | Gateway correlation_id Single Source Check |
| `mcp-error-contract` | MCP Error Contract Check |
| `iteration-docs-check` | Iteration Docs Check |
| `ci-test-isolation` | CI Test Isolation Check |
| `iteration-tools-test` | Iteration Tools Test |
| `workflow-contract` | Workflow Contract Validation |
<!-- END:CI_JOBS_LIST -->

### Nightly Workflow Jobs 清单

<!-- BEGIN:NIGHTLY_JOBS_LIST -->
| Job ID | Job Name |
|--------|----------|
| `unified-stack-full` | Unified Stack Full Verification |
| `iteration-audit` | Iteration Docs Audit |
| `notify-results` | Notify Results |
<!-- END:NIGHTLY_JOBS_LIST -->

### 合约 Make Targets 清单

<!-- BEGIN:MAKE_TARGETS_LIST -->
| Target | 说明 |
|--------|------|
| `apply-openmemory-grants` | CI/workflow 必需 |
| `apply-roles` | CI/workflow 必需 |
| `check-ci-test-isolation` | CI/workflow 必需 |
| `check-env-consistency` | CI/workflow 必需 |
| `check-gateway-correlation-id-single-source` | CI/workflow 必需 |
| `check-gateway-di-boundaries` | CI/workflow 必需 |
| `check-gateway-error-reason-usage` | CI/workflow 必需 |
| `check-gateway-import-surface` | CI/workflow 必需 |
| `check-gateway-public-api-surface` | CI/workflow 必需 |
| `check-iteration-docs` | CI/workflow 必需 |
| `check-iteration-fixtures-freshness` | CI/workflow 必需 |
| `check-logbook-consistency` | CI/workflow 必需 |
| `check-migration-sanity` | CI/workflow 必需 |
| `check-schemas` | CI/workflow 必需 |
| `check-scm-sync-consistency` | CI/workflow 必需 |
| `check-workflow-contract-doc-anchors` | CI/workflow 必需 |
| `check-workflow-contract-docs-sync` | CI/workflow 必需 |
| `check-workflow-contract-error-types-docs-sync` | CI/workflow 必需 |
| `check-workflow-contract-internal-consistency` | CI/workflow 必需 |
| `check-workflow-contract-version-policy` | CI/workflow 必需 |
| `check-workflow-make-targets-consistency` | CI/workflow 必需 |
| `ci` | CI/workflow 必需 |
| `format` | CI/workflow 必需 |
| `format-check` | CI/workflow 必需 |
| `iteration-audit` | CI/workflow 必需 |
| `lint` | CI/workflow 必需 |
| `migrate-ddl` | CI/workflow 必需 |
| `migrate-plan` | CI/workflow 必需 |
| `typecheck` | CI/workflow 必需 |
| `validate-workflows-strict` | CI/workflow 必需 |
| `verify-permissions` | CI/workflow 必需 |
| `verify-permissions-strict` | CI/workflow 必需 |
| `verify-unified` | CI/workflow 必需 |
<!-- END:MAKE_TARGETS_LIST -->

---

## 1. CI Workflow Jobs 与产物映射

### 1.1 test job

| 属性 | 值 |
|------|-----|
| Job ID | `test` |
| Job Name | `Test (Python ${{ matrix.python-version }})` |
| 矩阵 | Python 3.10, 3.11, 3.12 |

**产物上传**：

| Artifact 名称 | 文件路径 | 保留天数 |
|--------------|----------|----------|
| `test-results-{python-version}` | `test-results-*.xml`, `acceptance-results-*.xml` | 14 |
| `migration-logs-{python-version}` | `migration-output-*.log`, `verify-output-*.log` | 14 |

**环境变量**：

| 变量 | 值 | 用途 |
|------|-----|------|
| `POSTGRES_DSN` | PostgreSQL service container | 数据库连接 |
| `TEST_PG_DSN` | 同 POSTGRES_DSN | 测试数据库 |
| `PROJECT_KEY` | `test` | 项目标识 |

### 1.2 lint job

| 属性 | 值 |
|------|-----|
| Job ID | `lint` |
| Job Name | `Lint` |

**执行步骤**：
- `ruff check src/ tests/` - 代码风格检查
- `ruff format --check src/ tests/` - 格式检查
- `mypy src/engram/` - 类型检查

### 1.3 schema-validate job

| 属性 | 值 |
|------|-----|
| Job ID | `schema-validate` |
| Job Name | `Schema Validation` |

**产物上传**：

| Artifact 名称 | 文件路径 | 保留天数 |
|--------------|----------|----------|
| `schema-validation-results` | `schema-validation-results.json` | 14 |

### 1.4 workflow-contract job

| 属性 | 值 |
|------|-----|
| Job ID | `workflow-contract` |
| Job Name | `Workflow Contract Validation` |

**产物上传**：

| Artifact 名称 | 文件路径 | 保留天数 | 分级 |
|--------------|----------|----------|------|
| `workflow-contract-validation` | `artifacts/workflow_contract_validation.json` | 14 | Core |
| `workflow-contract-docs-sync` | `artifacts/workflow_contract_docs_sync.json` | 14 | Core |
| `workflow-contract-drift` | `artifacts/workflow_contract_drift.json`, `artifacts/workflow_contract_drift.md` | 14 | **Optional**（使用 `if-no-files-found: ignore`） |

### 1.5 其他 CI Jobs

| Job ID | Job Name | 说明 |
|--------|----------|------|
| `no-iteration-tracked` | No .iteration/ Tracked Files | 检查 .iteration/ 目录未被 git 跟踪 |
| `env-var-consistency` | Environment Variable Consistency | 环境变量一致性检查 |
| `logbook-consistency` | Logbook Consistency Check | Logbook 配置一致性检查 |
| `migration-sanity` | Migration Sanity Check | SQL 迁移文件存在性和基本语法检查 |
| `sql-safety` | SQL Migration Safety Check | SQL 迁移安全性检查（高危语句检测） |
| `gateway-di-boundaries` | Gateway DI Boundaries Check | Gateway DI 边界检查 |
| `scm-sync-consistency` | SCM Sync Consistency Check | SCM Sync 一致性检查 |
| `gateway-error-reason-usage` | Gateway ErrorReason Usage Check | Gateway ErrorReason 使用规范检查 |
| `gateway-import-surface` | Gateway Import Surface Check | Gateway `__init__.py` 懒加载策略检查（禁止 eager-import） |
| `gateway-public-api-surface` | Gateway Public API Import Surface Check | Gateway Public API 导入表面检查（`__all__` 与实际导出一致性） |
| `gateway-correlation-id-single-source` | Gateway correlation_id Single Source Check | Gateway `correlation_id` 单一来源检查（SSOT 模块） |
| `mcp-error-contract` | MCP Error Contract Check | MCP JSON-RPC 错误码合约与文档同步检查 |
| `iteration-docs-check` | Iteration Docs Check | 迭代文档检查 |
| `iteration-tools-test` | Iteration Tools Test | 迭代工具脚本测试（无需数据库依赖） |

---

## 2. Nightly Workflow Jobs 与产物映射

### 2.1 unified-stack-full job

| 属性 | 值 |
|------|-----|
| Job ID | `unified-stack-full` |
| Job Name | `Unified Stack Full Verification` |
| 超时 | 30 分钟 |

**产物上传**：

| Artifact 名称 | 文件路径 | 保留天数 |
|--------------|----------|----------|
| `nightly-unified-stack-results` | 见下表 | 30 |

产物文件列表：
- `test-unified-stack-results.xml` - 集成测试结果
- `.artifacts/verify-results.json` - 验证结果
- `.artifacts/acceptance-runs/*` - Acceptance 运行记录
- `.artifacts/acceptance-matrix.md` / `.json` - Acceptance 矩阵
- `caps.json` - 环境能力检测结果
- `validate.json` - Gate Contract 校验结果
- `compose-logs.txt` - Docker Compose 日志

**环境变量**：

| 变量 | 值 | 用途 |
|------|-----|------|
| `LOGBOOK_MIGRATOR_PASSWORD` | `ci_migrator_pwd_123` | Logbook 迁移账号密码 |
| `LOGBOOK_SVC_PASSWORD` | `ci_svc_pwd_123` | Logbook 服务账号密码 |
| `OPENMEMORY_MIGRATOR_PASSWORD` | `ci_om_migrator_pwd_123` | OpenMemory 迁移账号密码 |
| `OPENMEMORY_SVC_PASSWORD` | `ci_om_svc_pwd_123` | OpenMemory 服务账号密码 |
| `POSTGRES_USER` | `postgres` | PostgreSQL 用户 |
| `POSTGRES_PASSWORD` | `postgres` | PostgreSQL 密码 |
| `POSTGRES_DB` | `engram` | PostgreSQL 数据库 |
| `GATEWAY_PORT` | `8787` | Gateway 端口 |
| `OM_PORT` | `8080` | OpenMemory 端口 |

**workflow_dispatch 输入**：

| 输入 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `skip_degradation` | boolean | false | 跳过降级测试（调试用） |
| `profile` | choice | full | 验证 Profile（full/standard/http_only） |

### 2.2 iteration-audit job

| 属性 | 值 |
|------|-----|
| Job ID | `iteration-audit` |
| Job Name | `Iteration Docs Audit` |

**执行步骤**：
- `Checkout repository`
- `Set up Python`
- `Run iteration-audit`
- `Upload iteration audit report`

### 2.3 notify-results job

| 属性 | 值 |
|------|-----|
| Job ID | `notify-results` |
| Job Name | `Notify Results` |
| 依赖 | `unified-stack-full`, `iteration-audit` |

---

## 3. Release Workflow Jobs 与产物映射

### 3.1 build job

| 属性 | 值 |
|------|-----|
| Job ID | `build` |
| Job Name | `Build Release` |

**产物上传**：

| Artifact 名称 | 文件路径 | 保留天数 |
|--------------|----------|----------|
| `release-artifacts` | `dist/*.whl`, `dist/*.tar.gz` | 14 |

**执行步骤**：
- `Checkout repository`
- `Set up Python`
- `Install dependencies`
- `Build package`
- `Upload release artifacts`

### 3.2 publish job

| 属性 | 值 |
|------|-----|
| Job ID | `publish` |
| Job Name | `Publish Release Artifacts` |
| 依赖 | `build` |

**产物下载**：`release-artifacts` → `dist/`

**workflow_dispatch 输入**：

| 输入 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `publish` | boolean | false | 是否执行发布步骤 |
| `release_tag` | string | "" | 可选的发布标签 |

### 3.3 notify job

| 属性 | 值 |
|------|-----|
| Job ID | `notify` |
| Job Name | `Notify Release` |
| 依赖 | `build`, `publish` |

**说明**：当前仅输出结果信息，不依赖 Makefile 目标。

---

## 4. Makefile Targets 清单

### 4.1 CI 使用的核心目标

| Target | 说明 | 使用场景 |
|--------|------|----------|
| `ci` | CI 聚合目标 | 本地开发验证 |
| `lint` | 代码风格检查（ruff check） | lint job |
| `format` | 代码格式化 | 本地开发 |
| `format-check` | 代码格式检查（不修改） | lint job |
| `typecheck` | 类型检查（mypy） | lint job |
| `check-env-consistency` | 环境变量一致性检查 | env-var-consistency job |
| `check-logbook-consistency` | Logbook 配置一致性检查 | logbook-consistency job |
| `check-schemas` | JSON Schema 和 fixtures 校验 | schema-validate job |
| `check-migration-sanity` | SQL 迁移文件存在性检查 | migration-sanity job |
| `check-scm-sync-consistency` | SCM Sync 一致性检查 | scm-sync-consistency job |
| `check-gateway-error-reason-usage` | Gateway ErrorReason 使用规范检查 | gateway-error-reason-usage job |
| `check-gateway-import-surface` | Gateway `__init__.py` 懒加载策略检查 | gateway-import-surface job |
| `check-gateway-public-api-surface` | Gateway Public API 导入表面检查 | gateway-public-api-surface job |
| `check-gateway-correlation-id-single-source` | Gateway `correlation_id` 单一来源检查 | gateway-correlation-id-single-source job |
| `check-mcp-error-contract` | MCP JSON-RPC 错误码合约检查 | mcp-error-contract job |

### 4.2 Nightly 使用的核心目标

| Target | 说明 | 使用场景 |
|--------|------|----------|
| `verify-unified` | 统一栈验证 | unified-stack-full job |
| `verify-permissions` | 数据库权限验证 | 可选验证 |
| `migrate-ddl` | 执行 DDL 迁移 | 数据库初始化 |
| `apply-roles` | 应用 Logbook 角色和权限 | 数据库初始化 |
| `apply-openmemory-grants` | 应用 OpenMemory 权限 | 数据库初始化 |
| `iteration-audit` | 迭代文档审计 | iteration-audit job |

### 4.3 测试相关目标

| Target | 说明 |
|--------|------|
| `test` | 运行所有测试 |
| `test-logbook-unit` | Logbook 单元测试 |
| `test-logbook-integration` | Logbook 集成测试 |
| `test-gateway-integration` | Gateway 集成测试 |
| `test-gateway-integration-full` | Gateway 完整集成测试 |

### 4.4 验证相关目标

| Target | 说明 |
|--------|------|
| `verify-build` | Docker 构建边界校验 |
| `verify-build-static` | 静态检查（Dockerfile/compose） |
| `validate-schemas` | JSON Schema 校验 |
| `validate-workflows` | Workflow 文件校验 |
| `validate-workflows-strict` | Workflow 校验（严格模式） |

---

## 5. 合约文件引用

| 文件 | 用途 |
|------|------|
| `scripts/ci/workflow_contract.v2.json` | Workflow 合约定义（jobs、steps、artifacts） |
| `docs/ci_nightly_workflow_refactor/contract.md` | 人类可读的合约文档 |
| `docs/ci_nightly_workflow_refactor/maintenance.md` | 维护指南和 checklist |

---

## 6. 版本控制

| 版本 | 日期 | 变更说明 |
|------|------|----------|
| v2.2 | 2026-02-03 | 新增 release workflow 耦合映射（build/publish/notify） |
| v2.1 | 2026-02-02 | 补全缺失的 CI jobs（gateway-import-surface, gateway-public-api-surface, gateway-correlation-id-single-source, mcp-error-contract, iteration-tools-test）；新增 nightly iteration-audit job；补全相关 make targets |
| v2.0 | 2026-02-02 | 重写为 Phase 1 重构后的新 workflow 结构；移除旧的 detect-changes 耦合映射；更新产物路径和环境变量 |
| v1.1 | 2026-01-30 | 新增 Acceptance 产物归档路径章节 |
| v1.0 | 2026-01-30 | 初始版本，记录耦合关系 |
