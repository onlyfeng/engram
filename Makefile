# Engram Makefile - 快速部署与开发工具
# 
# 快速开始:
#   make ci           - 运行所有 CI 检查（lint、typecheck、schema 等）
#   make setup-db     - 一键初始化数据库
#   make gateway      - 启动 Gateway 服务
#   make help         - 查看所有命令
#
# 详细文档: docs/installation.md

.PHONY: install install-dev test test-logbook test-gateway test-acceptance test-e2e test-quick test-cov lint format typecheck migrate migrate-ddl apply-roles apply-openmemory-grants verify verify-permissions verify-permissions-strict verify-unified bootstrap-roles bootstrap-roles-required gateway clean help setup-db setup-db-logbook-only precheck ci regression check-env-consistency check-logbook-consistency check-schemas check-migration-sanity

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

## ==================== CI 检查目标（与 GitHub Actions 对齐） ====================

check-env-consistency:  ## 检查环境变量一致性（.env.example, docs, code）
	@echo "检查环境变量一致性..."
	$(PYTHON) scripts/ci/check_env_var_consistency.py --verbose
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
	@required_files="sql/01_logbook_schema.sql sql/02_scm_migration.sql sql/04_roles_and_grants.sql sql/05_openmemory_roles_and_grants.sql"; \
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

ci: lint format-check typecheck check-schemas check-env-consistency check-logbook-consistency check-migration-sanity  ## 运行所有 CI 检查（与 GitHub Actions 对齐）
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
