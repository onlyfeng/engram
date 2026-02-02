# Engram Makefile - 快速部署与开发工具
# 
# 快速开始:
#   make ci           - 运行所有 CI 检查（lint、typecheck、schema 等）
#   make setup-db     - 一键初始化数据库
#   make gateway      - 启动 Gateway 服务
#   make help         - 查看所有命令
#
# 详细文档: docs/installation.md

.PHONY: install install-dev test test-logbook test-gateway test-acceptance test-e2e test-quick test-cov test-iteration-tools lint format typecheck typecheck-gate typecheck-strict-island mypy-baseline-update mypy-metrics check-mypy-metrics-thresholds migrate migrate-ddl migrate-plan migrate-plan-full migrate-precheck apply-roles apply-openmemory-grants verify verify-permissions verify-permissions-strict verify-unified bootstrap-roles bootstrap-roles-required gateway clean help setup-db setup-db-logbook-only precheck ci regression check-env-consistency check-logbook-consistency check-schemas check-migration-sanity check-scm-sync-consistency check-gateway-error-reason-usage check-gateway-public-api-surface check-gateway-public-api-docs-sync check-gateway-di-boundaries check-gateway-import-surface check-gateway-correlation-id-single-source check-iteration-docs check-iteration-docs-headings check-iteration-docs-headings-warn check-iteration-docs-superseded-only check-iteration-evidence iteration-init iteration-init-next iteration-promote iteration-export iteration-snapshot iteration-audit validate-workflows validate-workflows-strict validate-workflows-json check-workflow-contract-docs-sync check-workflow-contract-docs-sync-json check-workflow-contract-version-policy check-workflow-contract-version-policy-json check-workflow-contract-doc-anchors check-workflow-contract-doc-anchors-json check-workflow-contract-internal-consistency check-workflow-contract-internal-consistency-json check-workflow-contract-coupling-map-sync check-workflow-contract-coupling-map-sync-json check-workflow-make-targets-consistency check-workflow-make-targets-consistency-json workflow-contract-drift-report workflow-contract-drift-report-json workflow-contract-drift-report-markdown workflow-contract-drift-report-all render-workflow-contract-docs update-workflow-contract-docs check-workflow-contract-docs-generated check-cli-entrypoints check-noqa-policy check-no-root-wrappers check-mcp-error-contract check-mcp-error-docs-sync check-mcp-error-docs-sync-json check-ci-test-isolation check-ci-test-isolation-json

# 默认目标
.DEFAULT_GOAL := help

# 变量
PYTHON := python3
PIP := pip
PYTEST := pytest
UVICORN := uvicorn

# PostgreSQL 配置（可通过环境变量覆盖）
POSTGRES_DSN ?= postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@$(POSTGRES_HOST):$(POSTGRES_PORT)/$(POSTGRES_DB)
POSTGRES_HOST ?= localhost
POSTGRES_PORT ?= 5432
POSTGRES_USER ?= postgres
POSTGRES_PASSWORD ?= postgres
POSTGRES_DB ?= engram

# 项目配置
PROJECT_KEY ?= default

# Gateway 配置
GATEWAY_PORT ?= 8787
OPENMEMORY_BASE_URL ?= http://localhost:8080

# 服务账号密码（unified-stack 模式必须设置，logbook-only 模式可不设置）
LOGBOOK_MIGRATOR_PASSWORD ?=
LOGBOOK_SVC_PASSWORD ?=
OPENMEMORY_MIGRATOR_PASSWORD ?=
OPENMEMORY_SVC_PASSWORD ?=

## ==================== 快速部署 ====================

setup-db: precheck db-create bootstrap-roles migrate-ddl apply-roles apply-openmemory-grants verify-permissions  ## 一键初始化数据库（创建库 + 角色 + DDL + 权限 + 验证）
	@echo "========== 初始化完成 =========="
	@echo ""
	@if [ -z "$(LOGBOOK_SVC_PASSWORD)" ]; then \
		echo "部署模式: logbook-only"; \
		echo "下一步："; \
		echo "  1. 设置环境变量："; \
		echo "     export POSTGRES_DSN=\"postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@$(POSTGRES_HOST):$(POSTGRES_PORT)/$(POSTGRES_DB)\""; \
		echo "     export OPENMEMORY_BASE_URL=\"http://localhost:8080\""; \
		echo "  2. 启动 Gateway："; \
		echo "     make gateway"; \
	else \
		echo "部署模式: unified-stack"; \
		echo "下一步："; \
		echo "  1. 设置环境变量："; \
		echo "     export POSTGRES_DSN=\"postgresql://logbook_svc:\$$LOGBOOK_SVC_PASSWORD@$(POSTGRES_HOST):$(POSTGRES_PORT)/$(POSTGRES_DB)\""; \
		echo "     export OPENMEMORY_BASE_URL=\"http://localhost:8080\""; \
		echo "  2. 启动 Gateway："; \
		echo "     make gateway"; \
	fi

setup-db-logbook-only: db-create migrate-ddl verify-permissions  ## 一键初始化数据库（logbook-only 模式，跳过服务账号创建）
	@echo "========== Logbook-only 初始化完成 =========="
	@echo ""
	@echo "部署模式: logbook-only (使用 postgres 超级用户)"
	@echo "下一步："
	@echo "  1. 设置环境变量："
	@echo "     export POSTGRES_DSN=\"postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@$(POSTGRES_HOST):$(POSTGRES_PORT)/$(POSTGRES_DB)\""
	@echo "  2. 启动 Gateway："
	@echo "     make gateway"

precheck:  ## 检查部署模式和环境变量
	@echo "检查部署模式..."
	@PWD_COUNT=0; \
	if [ -n "$(LOGBOOK_MIGRATOR_PASSWORD)" ]; then PWD_COUNT=$$((PWD_COUNT+1)); fi; \
	if [ -n "$(LOGBOOK_SVC_PASSWORD)" ]; then PWD_COUNT=$$((PWD_COUNT+1)); fi; \
	if [ -n "$(OPENMEMORY_MIGRATOR_PASSWORD)" ]; then PWD_COUNT=$$((PWD_COUNT+1)); fi; \
	if [ -n "$(OPENMEMORY_SVC_PASSWORD)" ]; then PWD_COUNT=$$((PWD_COUNT+1)); fi; \
	if [ "$$PWD_COUNT" = "0" ]; then \
		echo "[INFO] 部署模式: logbook-only"; \
		echo "       未设置任何服务账号密码，将跳过 login role 创建"; \
		echo "       将使用 postgres 超级用户进行后续操作"; \
	elif [ "$$PWD_COUNT" = "4" ]; then \
		echo "[INFO] 部署模式: unified-stack"; \
		echo "       已设置全部 4 个服务账号密码"; \
	else \
		echo "[ERROR] 配置错误：已设置 $$PWD_COUNT/4 个密码"; \
		echo "        unified-stack 模式要求全部设置，logbook-only 模式要求全部不设置"; \
		echo ""; \
		echo "修复方法："; \
		echo "  方案 A (logbook-only): 取消设置所有密码环境变量"; \
		echo "  方案 B (unified-stack): 设置以下全部环境变量："; \
		echo "    export LOGBOOK_MIGRATOR_PASSWORD=<密码>"; \
		echo "    export LOGBOOK_SVC_PASSWORD=<密码>"; \
		echo "    export OPENMEMORY_MIGRATOR_PASSWORD=<密码>"; \
		echo "    export OPENMEMORY_SVC_PASSWORD=<密码>"; \
		exit 1; \
	fi

## ==================== 安装 ====================

install:  ## 安装核心依赖
	$(PIP) install -e .

install-full:  ## 安装完整依赖（包含 Gateway 和 SCM）
	$(PIP) install -e ".[full]"

install-dev:  ## 安装开发依赖
	$(PIP) install -e ".[full,dev]"

## ==================== 测试 ====================

test:  ## 运行所有测试
	$(PYTEST) tests/ -v

test-logbook:  ## 运行 Logbook 测试
	$(PYTEST) tests/logbook/ -v

test-gateway:  ## 运行 Gateway 测试
	$(PYTEST) tests/gateway/ -v

test-acceptance:  ## 运行验收测试
	$(PYTEST) tests/acceptance/ -v

test-e2e:  ## 运行端到端测试
	$(PYTEST) tests/acceptance/test_e2e_workflow.py tests/acceptance/test_gateway_startup.py -v

test-quick:  ## 快速冒烟测试（仅安装和导入验证）
	$(PYTEST) tests/acceptance/test_installation.py -v

test-iteration-tools:  ## 运行迭代工具脚本测试（无需数据库）
	$(PYTEST) tests/iteration/ -q

test-cov:  ## 运行测试并生成覆盖率报告
	$(PYTEST) tests/ --cov=src/engram --cov-report=html --cov-report=term

## ==================== 代码质量 ====================

lint:  ## 代码检查（ruff check）
	ruff check src/ tests/

format:  ## 代码格式化
	ruff format src/ tests/

format-check:  ## 代码格式检查（不修改）
	ruff format --check src/ tests/

typecheck:  ## 类型检查（mypy）
	mypy src/engram/

typecheck-gate:  ## mypy 类型检查（baseline 模式，CI 门禁使用）
	@echo "运行 mypy 类型检查（baseline 模式）..."
	$(PYTHON) -m scripts.ci.check_mypy_gate --gate baseline --verbose
	@echo "mypy 类型检查通过"

typecheck-strict-island:  ## mypy 类型检查（strict-island 模式，仅检查 strict island 模块）
	@echo "运行 mypy 类型检查（strict-island 模式）..."
	$(PYTHON) -m scripts.ci.check_mypy_gate --gate strict-island --verbose
	@echo "mypy strict-island 类型检查通过"

mypy-baseline-update:  ## 更新 mypy baseline 文件
	@echo "更新 mypy baseline..."
	$(PYTHON) -m scripts.ci.check_mypy_gate --write-baseline
	@echo "mypy baseline 已更新"

mypy-metrics:  ## 生成 mypy 指标报告（聚合 baseline 错误统计）
	@echo "生成 mypy 指标报告..."
	@mkdir -p artifacts
	$(PYTHON) -m scripts.ci.mypy_metrics --output artifacts/mypy_metrics.json --verbose
	@echo "mypy 指标报告已生成: artifacts/mypy_metrics.json"

check-mypy-metrics-thresholds:  ## 检查 mypy 指标阈值（仅告警，不阻断 CI）
	@echo "检查 mypy 指标阈值..."
	$(PYTHON) -m scripts.ci.check_mypy_metrics_thresholds --verbose
	@echo "mypy 指标阈值检查完成"

## ==================== CI 检查目标（与 GitHub Actions 对齐） ====================

check-env-consistency:  ## 检查环境变量一致性（.env.example, docs, code）
	@echo "检查环境变量一致性..."
	$(PYTHON) -m scripts.ci.check_env_var_consistency --verbose
	@echo "环境变量一致性检查通过"

check-logbook-consistency:  ## 检查 Logbook 配置一致性
	@echo "检查 Logbook 配置一致性..."
	$(PYTHON) scripts/verify_logbook_consistency.py --verbose
	@echo "Logbook 配置一致性检查通过"

check-schemas:  ## 校验 JSON Schema 和 fixtures
	@echo "校验 JSON Schema 和 fixtures..."
	$(PYTHON) scripts/validate_schemas.py --validate-fixtures --verbose
	@echo "Schema 校验通过"

check-migration-sanity:  ## 检查 SQL 迁移文件存在性
	@echo "检查 SQL 迁移文件..."
	@required_files="sql/01_logbook_schema.sql sql/02_scm_migration.sql sql/04_roles_and_grants.sql sql/05_openmemory_roles_and_grants.sql sql/06_scm_sync_runs.sql sql/07_scm_sync_locks.sql sql/08_scm_sync_jobs.sql sql/11_sync_jobs_dimension_columns.sql"; \
	missing=0; \
	for f in $$required_files; do \
		if [ ! -f "$$f" ]; then \
			echo "[ERROR] 缺失: $$f"; \
			missing=$$((missing + 1)); \
		else \
			echo "[OK] 存在: $$f"; \
		fi; \
	done; \
	if [ "$$missing" -gt 0 ]; then \
		echo "缺失 $$missing 个必需的迁移文件"; \
		exit 1; \
	fi
	@echo "SQL 迁移文件检查通过"

check-scm-sync-consistency:  ## 检查 SCM Sync 一致性（文档/代码/配置对齐）
	@echo "检查 SCM Sync 一致性..."
	$(PYTHON) scripts/verify_scm_sync_consistency.py --verbose
	@echo "SCM Sync 一致性检查通过"

check-gateway-error-reason-usage:  ## 检查 Gateway ErrorReason 使用规范（禁止硬编码 reason 字符串）
	@echo "检查 Gateway ErrorReason 使用规范..."
	$(PYTHON) -m scripts.ci.check_gateway_error_reason_usage --verbose
	@echo "Gateway ErrorReason 使用规范检查通过"

check-gateway-public-api-surface:  ## 检查 Gateway Public API 导入表面（确保 Tier B 模块懒加载）
	@echo "检查 Gateway Public API 导入表面..."
	$(PYTHON) -m scripts.ci.check_gateway_public_api_import_surface --verbose
	@echo "Gateway Public API 导入表面检查通过"

check-gateway-public-api-docs-sync:  ## 检查 Gateway Public API 代码与文档同步
	@echo "检查 Gateway Public API 文档同步..."
	$(PYTHON) -m scripts.ci.check_gateway_public_api_docs_sync --verbose
	@echo "Gateway Public API 文档同步检查通过"

check-gateway-di-boundaries:  ## 检查 Gateway DI 边界（禁止 deps.db 直接使用）
	@echo "检查 Gateway DI 边界..."
	$(PYTHON) -m scripts.ci.check_gateway_di_boundaries --verbose
	@echo "Gateway DI 边界检查通过"

check-gateway-import-surface:  ## 检查 Gateway __init__.py 懒加载策略（禁止 eager-import）
	@echo "检查 Gateway Import Surface..."
	$(PYTHON) -m scripts.ci.check_gateway_import_surface --verbose
	@echo "Gateway Import Surface 检查通过"

check-gateway-correlation-id-single-source:  ## 检查 Gateway correlation_id 单一来源（禁止重复定义）
	@echo "检查 Gateway correlation_id 单一来源..."
	$(PYTHON) -m scripts.ci.check_gateway_correlation_id_single_source --verbose
	@echo "Gateway correlation_id 单一来源检查通过"

check-iteration-docs:  ## 检查迭代文档规范（.iteration/ 链接禁止 + .artifacts/ 链接禁止 + SUPERSEDED 一致性 + 模板占位符 + 证据合约）
	@echo "检查迭代文档规范..."
	$(PYTHON) -m scripts.ci.check_no_iteration_links_in_docs --verbose
	$(PYTHON) -m scripts.ci.check_no_local_artifact_links_in_docs --verbose
	$(PYTHON) -m scripts.ci.check_iteration_docs_placeholders --verbose --warn-only
	$(PYTHON) -m scripts.ci.check_iteration_evidence_contract --verbose
	@echo "迭代文档规范检查通过"

check-iteration-docs-headings:  ## 检查 regression 文件标准标题（阻断模式）
	@echo "检查 regression 文件标准标题..."
	$(PYTHON) -m scripts.ci.check_iteration_docs_placeholders --verbose
	@echo "regression 文件标准标题检查通过"

check-iteration-docs-headings-warn:  ## 检查 regression 文件标准标题（仅警告，不阻断）
	@echo "检查 regression 文件标准标题（仅警告）..."
	$(PYTHON) -m scripts.ci.check_iteration_docs_placeholders --verbose --warn-only
	@echo "regression 文件标准标题检查完成（仅警告模式）"

check-iteration-docs-superseded-only:  ## 仅检查 SUPERSEDED 一致性（跳过 .iteration/ 链接检查）
	@echo "检查 SUPERSEDED 一致性..."
	$(PYTHON) -m scripts.ci.check_no_iteration_links_in_docs --superseded-only --verbose
	@echo "SUPERSEDED 一致性检查通过"

check-iteration-evidence:  ## 检查迭代证据文件合约（命名规范 + JSON Schema）
	@echo "检查迭代证据文件合约..."
	$(PYTHON) -m scripts.ci.check_iteration_evidence_contract --verbose
	@echo "迭代证据文件合约检查通过"

validate-workflows:  ## Workflow 合约校验（默认模式）
	@echo "校验 Workflow 合约..."
	$(PYTHON) -m scripts.ci.validate_workflows
	@echo "Workflow 合约校验通过"

validate-workflows-strict:  ## Workflow 合约校验（严格模式，CI 使用）
	@echo "校验 Workflow 合约（严格模式）..."
	$(PYTHON) -m scripts.ci.validate_workflows --strict
	@echo "Workflow 合约校验通过（严格模式）"

validate-workflows-json:  ## Workflow 合约校验（JSON 输出）
	$(PYTHON) -m scripts.ci.validate_workflows --json

check-workflow-contract-docs-sync:  ## Workflow 合约与文档同步检查
	@echo "检查 Workflow 合约与文档同步..."
	$(PYTHON) -m scripts.ci.check_workflow_contract_docs_sync
	@echo "Workflow 合约与文档同步检查通过"

check-workflow-contract-docs-sync-json:  ## Workflow 合约与文档同步检查（JSON 输出）
	$(PYTHON) -m scripts.ci.check_workflow_contract_docs_sync --json

check-workflow-contract-version-policy:  ## Workflow 合约版本策略检查（关键文件变更时强制版本更新）
	@echo "检查 Workflow 合约版本策略..."
	$(PYTHON) -m scripts.ci.check_workflow_contract_version_policy --pr-mode
	@echo "Workflow 合约版本策略检查通过"

check-workflow-contract-version-policy-json:  ## Workflow 合约版本策略检查（JSON 输出）
	$(PYTHON) -m scripts.ci.check_workflow_contract_version_policy --pr-mode --json

check-workflow-contract-doc-anchors:  ## Workflow 合约文档锚点检查（验证错误消息中的锚点引用）
	@echo "检查 Workflow 合约文档锚点..."
	$(PYTHON) -m scripts.ci.check_workflow_contract_doc_anchors --verbose
	@echo "Workflow 合约文档锚点检查通过"

check-workflow-contract-doc-anchors-json:  ## Workflow 合约文档锚点检查（JSON 输出）
	$(PYTHON) -m scripts.ci.check_workflow_contract_doc_anchors --json

check-workflow-contract-internal-consistency:  ## Workflow 合约内部一致性检查（job_ids/job_names 长度、无重复等）
	@echo "检查 Workflow 合约内部一致性..."
	$(PYTHON) -m scripts.ci.check_workflow_contract_internal_consistency --verbose
	@echo "Workflow 合约内部一致性检查通过"

check-workflow-contract-internal-consistency-json:  ## Workflow 合约内部一致性检查（JSON 输出）
	$(PYTHON) -m scripts.ci.check_workflow_contract_internal_consistency --json

check-workflow-contract-coupling-map-sync:  ## Workflow 合约与 Coupling Map 同步检查
	@echo "检查 Workflow 合约与 Coupling Map 同步..."
	$(PYTHON) -m scripts.ci.check_workflow_contract_coupling_map_sync --verbose
	@echo "Workflow 合约与 Coupling Map 同步检查通过"

check-workflow-contract-coupling-map-sync-json:  ## Workflow 合约与 Coupling Map 同步检查（JSON 输出）
	$(PYTHON) -m scripts.ci.check_workflow_contract_coupling_map_sync --json

check-workflow-make-targets-consistency:  ## Workflow make targets 与 Makefile/Contract 一致性检查
	@echo "检查 Workflow make targets 一致性..."
	$(PYTHON) -m scripts.ci.check_workflow_make_targets_consistency --verbose
	@echo "Workflow make targets 一致性检查通过"

check-workflow-make-targets-consistency-json:  ## Workflow make targets 一致性检查（JSON 输出）
	$(PYTHON) -m scripts.ci.check_workflow_make_targets_consistency --json

workflow-contract-drift-report:  ## Workflow 合约 drift 报告（JSON 输出）
	$(PYTHON) -m scripts.ci.workflow_contract_drift_report

workflow-contract-drift-report-json:  ## Workflow 合约 drift 报告（JSON 输出到文件）
	@mkdir -p artifacts
	$(PYTHON) -m scripts.ci.workflow_contract_drift_report --output artifacts/workflow_contract_drift.json

workflow-contract-drift-report-markdown:  ## Workflow 合约 drift 报告（Markdown 输出）
	$(PYTHON) -m scripts.ci.workflow_contract_drift_report --markdown

workflow-contract-drift-report-all:  ## Workflow 合约 drift 报告（JSON + Markdown 输出到 artifacts/）
	@mkdir -p artifacts
	$(PYTHON) -m scripts.ci.workflow_contract_drift_report --output artifacts/workflow_contract_drift.json || true
	$(PYTHON) -m scripts.ci.workflow_contract_drift_report --markdown --output artifacts/workflow_contract_drift.md || true
	@echo "Drift reports 已生成到 artifacts/ 目录"

render-workflow-contract-docs:  ## 渲染 Workflow 合约文档受控块（仅预览输出，不写入）
	@echo "渲染 Workflow 合约文档受控块..."
	$(PYTHON) -m scripts.ci.render_workflow_contract_docs --target all
	@echo "渲染完成（仅预览，未写入文件）"

update-workflow-contract-docs:  ## 更新 Workflow 合约文档受控块（就地写入）
	@echo "更新 Workflow 合约文档受控块..."
	$(PYTHON) -m scripts.ci.render_workflow_contract_docs --write --target all
	@echo "Workflow 合约文档受控块已更新"

check-workflow-contract-docs-generated:  ## 检查 Workflow 合约文档生成状态（docs-sync + coupling-map-sync）
	@echo "检查 Workflow 合约文档生成状态..."
	$(PYTHON) -m scripts.ci.check_workflow_contract_docs_sync --verbose
	$(PYTHON) -m scripts.ci.check_workflow_contract_coupling_map_sync --verbose
	@echo "Workflow 合约文档生成状态检查通过"

check-cli-entrypoints:  ## CLI 入口点一致性检查（pyproject.toml 与文档同步）
	@echo "检查 CLI 入口点一致性..."
	$(PYTHON) scripts/verify_cli_entrypoints_consistency.py --verbose
	@echo "CLI 入口点一致性检查通过"

check-noqa-policy:  ## noqa 注释策略检查
	@echo "检查 noqa 注释策略..."
	$(PYTHON) -m scripts.ci.check_noqa_policy --verbose
	@echo "noqa 注释策略检查通过"

check-no-root-wrappers:  ## 根目录 wrapper 禁止导入检查
	@echo "检查根目录 wrapper 导入..."
	$(PYTHON) -m scripts.ci.check_no_root_wrappers_usage --verbose
	$(PYTHON) -m scripts.ci.check_no_root_wrappers_allowlist --verbose
	@echo "根目录 wrapper 导入检查通过"

check-mcp-error-contract:  ## MCP JSON-RPC 错误码合约检查
	@echo "检查 MCP JSON-RPC 错误码合约..."
	$(PYTHON) -m scripts.ci.check_mcp_jsonrpc_error_contract --verbose
	@echo "MCP JSON-RPC 错误码合约检查通过"

check-mcp-error-docs-sync:  ## MCP JSON-RPC 错误码文档与 Schema 同步检查
	@echo "检查 MCP JSON-RPC 错误码文档同步..."
	$(PYTHON) -m scripts.ci.check_mcp_jsonrpc_error_docs_sync --verbose
	@echo "MCP JSON-RPC 错误码文档同步检查通过"

check-mcp-error-docs-sync-json:  ## MCP JSON-RPC 错误码文档同步检查（JSON 输出）
	$(PYTHON) -m scripts.ci.check_mcp_jsonrpc_error_docs_sync --json

check-ci-test-isolation:  ## CI 测试隔离检查（禁止模块级 sys.path 污染和顶层 CI 模块导入）
	@echo "检查 CI 测试隔离..."
	$(PYTHON) -m scripts.ci.check_ci_test_isolation --verbose
	@echo "CI 测试隔离检查通过"

check-ci-test-isolation-json:  ## CI 测试隔离检查（JSON 输出）
	$(PYTHON) -m scripts.ci.check_ci_test_isolation --json

ci: lint format-check typecheck-gate typecheck-strict-island mypy-metrics check-mypy-metrics-thresholds check-schemas check-env-consistency check-logbook-consistency check-migration-sanity check-scm-sync-consistency check-gateway-error-reason-usage check-gateway-public-api-surface check-gateway-public-api-docs-sync check-gateway-di-boundaries check-gateway-import-surface check-gateway-correlation-id-single-source check-iteration-docs validate-workflows-strict check-workflow-contract-docs-sync check-workflow-contract-version-policy check-workflow-contract-internal-consistency check-workflow-make-targets-consistency check-mcp-error-contract check-mcp-error-docs-sync check-ci-test-isolation  ## 运行所有 CI 检查（与 GitHub Actions 对齐）
	@echo ""
	@echo "=========================================="
	@echo "[OK] 所有 CI 检查通过"
	@echo "=========================================="
	@echo ""
	@echo "提示: 运行 'make test' 执行完整测试（需要数据库）"

regression: ci  ## 运行回归测试（CI 检查 + 回归 Runbook 提示）
	@echo ""
	@echo "=========================================="
	@echo "回归测试完成"
	@echo "=========================================="
	@echo ""
	@echo "回归 Runbook: docs/acceptance/iteration_3_regression.md"
	@echo ""
	@echo "如有数据库环境，请继续运行:"
	@echo "  make migrate-ddl"
	@echo "  make verify-permissions"
	@echo "  make test"

## ==================== 数据库 ====================

migrate-plan:  ## 查看迁移计划（不连接数据库）
	@echo "查看迁移计划..."
	$(PYTHON) -m engram.logbook.cli.db_migrate --plan --pretty

migrate-plan-full:  ## 查看完整迁移计划（DDL + 权限 + 验证）
	@echo "查看完整迁移计划..."
	$(PYTHON) -m engram.logbook.cli.db_migrate --plan --apply-roles --apply-openmemory-grants --verify --pretty

migrate-precheck:  ## 仅执行预检（验证配置和数据库连接，不执行迁移）
	@echo "执行预检..."
	$(PYTHON) -m engram.logbook.cli.db_migrate \
		--dsn "postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@$(POSTGRES_HOST):$(POSTGRES_PORT)/$(POSTGRES_DB)" \
		--precheck-only
	@echo "预检完成"

migrate-ddl:  ## 仅执行 DDL 迁移（Schema/表/索引）
	@echo "执行 DDL 迁移..."
	$(PYTHON) -m engram.logbook.cli.db_migrate \
		--dsn "postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@$(POSTGRES_HOST):$(POSTGRES_PORT)/$(POSTGRES_DB)"
	@echo "DDL 迁移完成"

apply-roles:  ## 应用 Logbook 角色和权限（04_roles_and_grants.sql）
	@echo "应用 Logbook 角色和权限..."
	$(PYTHON) -m engram.logbook.cli.db_migrate \
		--dsn "postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@$(POSTGRES_HOST):$(POSTGRES_PORT)/$(POSTGRES_DB)" \
		--apply-roles
	@echo "Logbook 角色已应用"

apply-openmemory-grants:  ## 应用 OpenMemory 权限（05_openmemory_roles_and_grants.sql）
	@echo "应用 OpenMemory 权限..."
	$(PYTHON) -m engram.logbook.cli.db_migrate \
		--dsn "postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@$(POSTGRES_HOST):$(POSTGRES_PORT)/$(POSTGRES_DB)" \
		--apply-openmemory-grants
	@echo "OpenMemory 权限已应用"

verify-permissions:  ## 验证数据库权限配置（99_verify_permissions.sql）
	@echo "验证数据库权限..."
	$(PYTHON) -m engram.logbook.cli.db_migrate \
		--dsn "postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@$(POSTGRES_HOST):$(POSTGRES_PORT)/$(POSTGRES_DB)" \
		--verify
	@echo "权限验证完成"

verify-permissions-strict:  ## 验证数据库权限配置（严格模式，失败时报错退出）
	@echo "验证数据库权限（严格模式）..."
	$(PYTHON) -m engram.logbook.cli.db_migrate \
		--dsn "postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@$(POSTGRES_HOST):$(POSTGRES_PORT)/$(POSTGRES_DB)" \
		--verify --verify-strict
	@echo "权限验证完成（严格模式）"

migrate:  ## [废弃] 请使用 migrate-ddl，此目标仅执行 DDL 迁移
	@echo "警告：migrate 目标已废弃，请使用 migrate-ddl"
	@$(MAKE) migrate-ddl

verify:  ## [废弃] 请使用 verify-permissions
	@echo "警告：verify 目标已废弃，请使用 verify-permissions"
	@$(MAKE) verify-permissions

## ==================== 统一栈验证 ====================

# 可配置变量
GATEWAY_URL ?= http://localhost:$(GATEWAY_PORT)
OPENMEMORY_URL ?= http://localhost:8080
VERIFY_TIMEOUT ?= 5

verify-unified:  ## 统一栈验证（健康检查 + DB 权限 + smoke 测试）
	@echo "========== 统一栈验证 =========="
	@echo ""
	@# Step 1: 服务健康检查
	@echo "[1/3] 检查服务健康状态..."
	@GATEWAY_OK=0; OM_OK=0; \
	if curl -sf --max-time $(VERIFY_TIMEOUT) $(GATEWAY_URL)/health > /dev/null 2>&1; then \
		echo "  ✓ Gateway ($(GATEWAY_URL)/health) 正常"; \
		GATEWAY_OK=1; \
	else \
		echo "  ✗ Gateway ($(GATEWAY_URL)/health) 不可用"; \
	fi; \
	if curl -sf --max-time $(VERIFY_TIMEOUT) $(OPENMEMORY_URL)/health > /dev/null 2>&1; then \
		echo "  ✓ OpenMemory ($(OPENMEMORY_URL)/health) 正常"; \
		OM_OK=1; \
	else \
		echo "  ✗ OpenMemory ($(OPENMEMORY_URL)/health) 不可用"; \
	fi; \
	if [ "$$GATEWAY_OK" = "0" ] || [ "$$OM_OK" = "0" ]; then \
		echo ""; \
		echo "[ERROR] 服务健康检查失败，请确保 Docker Compose 已启动："; \
		echo "  docker compose -f docker-compose.unified.yml up -d"; \
		exit 1; \
	fi
	@echo ""
	@# Step 2: 数据库权限验证
	@echo "[2/3] 验证数据库权限..."
	@if docker compose -f docker-compose.unified.yml ps --format json 2>/dev/null | grep -q postgres; then \
		docker compose -f docker-compose.unified.yml exec -T postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -c "SELECT 1 FROM logbook.facts LIMIT 0" > /dev/null 2>&1 && \
		echo "  ✓ Logbook schema 可访问" || \
		echo "  ✗ Logbook schema 不可访问（可能尚未迁移）"; \
	else \
		$(PYTHON) -m engram.logbook.cli.db_migrate \
			--dsn "postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@$(POSTGRES_HOST):$(POSTGRES_PORT)/$(POSTGRES_DB)" \
			--verify && echo "  ✓ 权限验证通过" || echo "  ✗ 权限验证失败"; \
	fi
	@echo ""
	@# Step 3: Smoke 测试
	@echo "[3/3] 执行 smoke 测试..."
	@SMOKE_OK=1; \
	HEALTH_RESP=$$(curl -sf --max-time $(VERIFY_TIMEOUT) $(GATEWAY_URL)/health 2>/dev/null); \
	if echo "$$HEALTH_RESP" | grep -qi "ok\|healthy\|status" > /dev/null 2>&1; then \
		echo "  ✓ Gateway /health 返回有效响应"; \
	else \
		echo "  ✓ Gateway /health 端点可达"; \
	fi; \
	if [ -n "$(VERIFY_FULL)" ]; then \
		echo "  [VERIFY_FULL=1] 执行扩展验证..."; \
		if curl -sf --max-time $(VERIFY_TIMEOUT) "$(GATEWAY_URL)/mcp" -X POST \
			-H "Content-Type: application/json" \
			-d '{"jsonrpc":"2.0","method":"tools/list","id":1}' 2>/dev/null | grep -q "tools\|result"; then \
			echo "  ✓ MCP RPC 端点响应正常"; \
		else \
			echo "  ⚠ MCP RPC 端点响应异常（可能需要配置）"; \
		fi; \
	fi
	@echo ""
	@echo "========== 验证完成 =========="
	@echo ""
	@echo "统一栈状态: 正常"
	@echo ""
	@echo "可选：设置 VERIFY_FULL=1 执行完整验证"
	@echo "  VERIFY_FULL=1 make verify-unified"

bootstrap-roles: precheck  ## 初始化服务账号（支持 logbook-only 跳过或 unified-stack 创建）
	@echo "初始化服务账号..."
	$(PYTHON) scripts/db_bootstrap.py \
		--dsn "postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@$(POSTGRES_HOST):$(POSTGRES_PORT)/postgres"

bootstrap-roles-required:  ## 强制创建服务账号（unified-stack 模式，密码必须设置）
	@echo "初始化服务账号（强制模式）..."
	$(PYTHON) scripts/db_bootstrap.py \
		--dsn "postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@$(POSTGRES_HOST):$(POSTGRES_PORT)/postgres" \
		--require-roles
	@echo "服务账号已就绪"

db-create:  ## 创建数据库并启用 pgvector 扩展
	@echo "创建数据库 $(POSTGRES_DB)..."
	@createdb -h $(POSTGRES_HOST) -p $(POSTGRES_PORT) -U $(POSTGRES_USER) $(POSTGRES_DB) 2>/dev/null || echo "数据库已存在，跳过创建"
	@echo "启用 pgvector 扩展..."
	@psql -h $(POSTGRES_HOST) -p $(POSTGRES_PORT) -U $(POSTGRES_USER) -d $(POSTGRES_DB) -c "CREATE EXTENSION IF NOT EXISTS vector;" 2>/dev/null || echo "pgvector 已启用"

db-drop:  ## 删除数据库（危险操作）
	@echo "警告：即将删除数据库 $(POSTGRES_DB)"
	@read -p "确认删除？(y/N) " confirm && [ "$$confirm" = "y" ] && \
		dropdb -h $(POSTGRES_HOST) -p $(POSTGRES_PORT) -U $(POSTGRES_USER) $(POSTGRES_DB) || echo "已取消"

## ==================== 迭代文档工作流 ====================

# 迭代编号（通过 N= 参数传入）
N ?=

iteration-init:  ## 初始化本地迭代草稿（用法: make iteration-init N=13 或 make iteration-init N=next）
	@if [ -z "$(N)" ]; then \
		echo "❌ 错误: 请指定迭代编号，例如: make iteration-init N=13 或 make iteration-init N=next"; \
		exit 1; \
	fi
	@if [ "$(N)" = "next" ]; then \
		$(PYTHON) scripts/iteration/init_local_iteration.py --next; \
	else \
		$(PYTHON) scripts/iteration/init_local_iteration.py $(N); \
	fi

iteration-init-next:  ## 初始化下一可用编号的本地迭代草稿（自动选择编号）
	$(PYTHON) scripts/iteration/init_local_iteration.py --next

iteration-promote:  ## 将本地迭代晋升到 SSOT（用法: make iteration-promote N=13）
	@if [ -z "$(N)" ]; then \
		echo "❌ 错误: 请指定迭代编号，例如: make iteration-promote N=13"; \
		exit 1; \
	fi
	$(PYTHON) scripts/iteration/promote_iteration.py $(N)

iteration-export:  ## 导出本地迭代草稿为 zip 以便分享（用法: make iteration-export N=13）
	@if [ -z "$(N)" ]; then \
		echo "❌ 错误: 请指定迭代编号，例如: make iteration-export N=13"; \
		exit 1; \
	fi
	$(PYTHON) scripts/iteration/export_local_iteration.py $(N) --output-zip .artifacts/iteration-draft-export/iteration_$(N)_draft.zip

# 快照可选参数
OUT ?=
FORCE ?=

iteration-snapshot:  ## 快照 SSOT 迭代到本地只读副本（用法: make iteration-snapshot N=10 [OUT=path] [FORCE=1]）
	@if [ -z "$(N)" ]; then \
		echo "❌ 错误: 请指定迭代编号，例如: make iteration-snapshot N=10"; \
		echo ""; \
		echo "💡 列出可用编号: python scripts/iteration/snapshot_ssot_iteration.py --list"; \
		exit 1; \
	fi
	@ARGS="$(N)"; \
	if [ -n "$(OUT)" ]; then ARGS="$$ARGS --output-dir $(OUT)"; fi; \
	if [ "$(FORCE)" = "1" ]; then ARGS="$$ARGS --force"; fi; \
	$(PYTHON) scripts/iteration/snapshot_ssot_iteration.py $$ARGS
	@echo ""
	@echo "⚠️  重要: 快照仅供本地阅读和实验，不可用于 promote 覆盖旧编号"

iteration-audit:  ## 生成迭代文档审计报告（输出到 .artifacts/iteration-audit/）
	$(PYTHON) scripts/iteration/audit_iteration_docs.py --output-dir .artifacts/iteration-audit

## ==================== 服务 ====================

gateway:  ## 启动 Gateway 服务（带热重载）
	@echo "启动 Gateway 服务..."
	@echo "  端口: $(GATEWAY_PORT)"
	@echo "  项目: $(PROJECT_KEY)"
	@echo "  OpenMemory: $(OPENMEMORY_BASE_URL)"
	@echo ""
	POSTGRES_DSN=$(POSTGRES_DSN) \
	PROJECT_KEY=$(PROJECT_KEY) \
	OPENMEMORY_BASE_URL=$(OPENMEMORY_BASE_URL) \
	$(UVICORN) engram.gateway.main:app --host 0.0.0.0 --port $(GATEWAY_PORT) --reload

gateway-prod:  ## 启动 Gateway 服务（生产模式，无热重载）
	@echo "启动 Gateway 服务（生产模式）..."
	POSTGRES_DSN=$(POSTGRES_DSN) \
	PROJECT_KEY=$(PROJECT_KEY) \
	OPENMEMORY_BASE_URL=$(OPENMEMORY_BASE_URL) \
	$(UVICORN) engram.gateway.main:app --host 0.0.0.0 --port $(GATEWAY_PORT) --workers 4

## ==================== 清理 ====================

clean:  ## 清理临时文件
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	rm -rf build/ dist/ htmlcov/ .coverage

## ==================== 帮助 ====================

help:  ## 显示帮助信息
	@echo ""
	@echo "\033[1mEngram - AI 友好的事实账本与记忆管理模块\033[0m"
	@echo ""
	@echo "\033[1m快速开始:\033[0m"
	@echo "  1. 设置密码环境变量（见下方）"
	@echo "  2. make setup-db    # 一键初始化数据库"
	@echo "  3. make gateway     # 启动服务"
	@echo ""
	@echo "\033[1m可用命令:\033[0m"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "\033[1m服务账号密码（unified-stack 模式必须设置，logbook-only 模式可不设置）:\033[0m"
	@echo "  LOGBOOK_MIGRATOR_PASSWORD      Logbook 迁移账号密码"
	@echo "  LOGBOOK_SVC_PASSWORD           Logbook 服务账号密码"
	@echo "  OPENMEMORY_MIGRATOR_PASSWORD   OpenMemory 迁移账号密码"
	@echo "  OPENMEMORY_SVC_PASSWORD        OpenMemory 服务账号密码"
	@echo ""
	@echo "\033[1m部署模式说明:\033[0m"
	@echo "  logbook-only:   不设置任何密码 → 跳过服务账号创建，使用 postgres 超级用户"
	@echo "  unified-stack:  设置全部 4 个密码 → 创建独立服务账号"
	@echo ""
	@echo "\033[1m可选环境变量:\033[0m"
	@echo "  POSTGRES_HOST         PostgreSQL 主机 (默认: localhost)"
	@echo "  POSTGRES_PORT         PostgreSQL 端口 (默认: 5432)"
	@echo "  POSTGRES_DB           数据库名 (默认: engram)"
	@echo "  PROJECT_KEY           项目标识 (默认: default)"
	@echo "  GATEWAY_PORT          Gateway 端口 (默认: 8787)"
	@echo "  OPENMEMORY_BASE_URL   OpenMemory 地址 (默认: http://localhost:8080)"
	@echo ""
	@echo "\033[1m多项目部署示例:\033[0m"
	@echo "  PROJECT_KEY=proj_a POSTGRES_DB=proj_a make setup-db"
	@echo "  PROJECT_KEY=proj_a POSTGRES_DB=proj_a make gateway"
	@echo ""
	@echo "\033[1m详细文档:\033[0m docs/installation.md"
	@echo ""
