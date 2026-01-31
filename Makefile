# Engram Makefile - 快速部署与开发工具
# 
# 快速开始:
#   make setup-db     - 一键初始化数据库
#   make gateway      - 启动 Gateway 服务
#   make help         - 查看所有命令
#
# 详细文档: docs/installation.md

.PHONY: install install-dev test test-logbook test-gateway test-acceptance test-e2e test-quick test-cov lint format migrate gateway clean help setup-db precheck

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

# 服务账号密码（部署时必须设置）
LOGBOOK_MIGRATOR_PASSWORD ?=
LOGBOOK_SVC_PASSWORD ?=
OPENMEMORY_MIGRATOR_PASSWORD ?=
OPENMEMORY_SVC_PASSWORD ?=

## ==================== 快速部署 ====================

setup-db: precheck  ## 一键初始化数据库（创建库 + 角色 + 迁移）
	@echo "========== 一键初始化数据库 =========="
	@echo "1. 创建数据库 $(POSTGRES_DB)..."
	@createdb -h $(POSTGRES_HOST) -p $(POSTGRES_PORT) -U $(POSTGRES_USER) $(POSTGRES_DB) 2>/dev/null || echo "   数据库已存在，跳过创建"
	@echo "2. 启用 pgvector 扩展..."
	@psql -h $(POSTGRES_HOST) -p $(POSTGRES_PORT) -U $(POSTGRES_USER) -d $(POSTGRES_DB) -c "CREATE EXTENSION IF NOT EXISTS vector;" 2>/dev/null || echo "   pgvector 已启用"
	@echo "3. 初始化服务账号..."
	@$(PYTHON) logbook_postgres/scripts/db_bootstrap.py \
		--dsn "postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@$(POSTGRES_HOST):$(POSTGRES_PORT)/postgres" \
		|| (echo "   错误：初始化服务账号失败"; exit 1)
	@echo "4. 执行迁移脚本..."
	@$(PYTHON) logbook_postgres/scripts/db_migrate.py \
		--dsn "postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@$(POSTGRES_HOST):$(POSTGRES_PORT)/$(POSTGRES_DB)" \
		--apply-roles --apply-openmemory-grants \
		|| (echo "   错误：迁移失败"; exit 1)
	@echo "========== 初始化完成 =========="
	@echo ""
	@echo "下一步："
	@echo "  1. 设置环境变量："
	@echo "     export POSTGRES_DSN=\"postgresql://logbook_svc:\$$LOGBOOK_SVC_PASSWORD@$(POSTGRES_HOST):$(POSTGRES_PORT)/$(POSTGRES_DB)\""
	@echo "     export OPENMEMORY_BASE_URL=\"http://localhost:8080\""
	@echo "  2. 启动 Gateway："
	@echo "     make gateway"

precheck:  ## 检查环境变量是否已设置
	@echo "检查必需的环境变量..."
	@if [ -z "$(LOGBOOK_MIGRATOR_PASSWORD)" ]; then \
		echo "错误：LOGBOOK_MIGRATOR_PASSWORD 未设置"; \
		echo "请运行：export LOGBOOK_MIGRATOR_PASSWORD=<密码>"; \
		exit 1; \
	fi
	@if [ -z "$(LOGBOOK_SVC_PASSWORD)" ]; then \
		echo "错误：LOGBOOK_SVC_PASSWORD 未设置"; \
		echo "请运行：export LOGBOOK_SVC_PASSWORD=<密码>"; \
		exit 1; \
	fi
	@if [ -z "$(OPENMEMORY_MIGRATOR_PASSWORD)" ]; then \
		echo "错误：OPENMEMORY_MIGRATOR_PASSWORD 未设置"; \
		echo "请运行：export OPENMEMORY_MIGRATOR_PASSWORD=<密码>"; \
		exit 1; \
	fi
	@if [ -z "$(OPENMEMORY_SVC_PASSWORD)" ]; then \
		echo "错误：OPENMEMORY_SVC_PASSWORD 未设置"; \
		echo "请运行：export OPENMEMORY_SVC_PASSWORD=<密码>"; \
		exit 1; \
	fi
	@echo "环境变量检查通过"

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

lint:  ## 代码检查
	ruff check src/ tests/

format:  ## 代码格式化
	ruff format src/ tests/

typecheck:  ## 类型检查
	mypy src/engram/

## ==================== 数据库 ====================

migrate:  ## 执行数据库迁移（仅 SQL 脚本）
	@echo "执行 SQL 迁移脚本..."
	@for f in sql/*.sql; do \
		echo "执行: $$f"; \
		psql "$(POSTGRES_DSN)" -f "$$f" || exit 1; \
	done
	@echo "迁移完成"

db-create:  ## 创建数据库
	createdb -h $(POSTGRES_HOST) -p $(POSTGRES_PORT) -U $(POSTGRES_USER) $(POSTGRES_DB) 2>/dev/null || echo "数据库已存在"

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
	@echo "\033[1m必需环境变量（部署前设置）:\033[0m"
	@echo "  LOGBOOK_MIGRATOR_PASSWORD      Logbook 迁移账号密码"
	@echo "  LOGBOOK_SVC_PASSWORD           Logbook 服务账号密码"
	@echo "  OPENMEMORY_MIGRATOR_PASSWORD   OpenMemory 迁移账号密码"
	@echo "  OPENMEMORY_SVC_PASSWORD        OpenMemory 服务账号密码"
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
