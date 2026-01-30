# Engram Makefile - 本地开发工具
# 
# 使用方式:
#   make install      - 安装开发环境
#   make test         - 运行测试
#   make migrate      - 执行数据库迁移
#   make gateway      - 启动 Gateway 服务

.PHONY: install install-dev test test-logbook test-gateway test-acceptance test-e2e test-quick test-cov lint format migrate gateway clean help

# 默认目标
.DEFAULT_GOAL := help

# 变量
PYTHON := python3
PIP := pip
PYTEST := pytest
UVICORN := uvicorn

# PostgreSQL 配置（可通过环境变量覆盖）
POSTGRES_DSN ?= postgresql://postgres:postgres@localhost:5432/engram
POSTGRES_USER ?= postgres
POSTGRES_DB ?= engram

# Gateway 配置
GATEWAY_PORT ?= 8787
OPENMEMORY_BASE_URL ?= http://localhost:8080

## 安装

install:  ## 安装核心依赖
	$(PIP) install -e .

install-full:  ## 安装完整依赖（包含 Gateway 和 SCM）
	$(PIP) install -e ".[full]"

install-dev:  ## 安装开发依赖
	$(PIP) install -e ".[full,dev]"

## 测试

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

## 代码质量

lint:  ## 代码检查
	ruff check src/ tests/

format:  ## 代码格式化
	ruff format src/ tests/

typecheck:  ## 类型检查
	mypy src/engram/

## 数据库

migrate:  ## 执行数据库迁移
	@echo "执行 SQL 迁移脚本..."
	@for f in sql/*.sql; do \
		echo "执行: $$f"; \
		psql "$(POSTGRES_DSN)" -f "$$f" || exit 1; \
	done
	@echo "迁移完成"

db-create:  ## 创建数据库
	createdb -U $(POSTGRES_USER) $(POSTGRES_DB) 2>/dev/null || echo "数据库已存在"

db-drop:  ## 删除数据库（危险操作）
	@echo "警告：即将删除数据库 $(POSTGRES_DB)"
	@read -p "确认删除？(y/N) " confirm && [ "$$confirm" = "y" ] && \
		dropdb -U $(POSTGRES_USER) $(POSTGRES_DB) || echo "已取消"

## 服务

gateway:  ## 启动 Gateway 服务
	POSTGRES_DSN=$(POSTGRES_DSN) \
	OPENMEMORY_BASE_URL=$(OPENMEMORY_BASE_URL) \
	$(UVICORN) engram.gateway.main:app --host 0.0.0.0 --port $(GATEWAY_PORT) --reload

## 清理

clean:  ## 清理临时文件
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	rm -rf build/ dist/ htmlcov/ .coverage

## 帮助

help:  ## 显示帮助信息
	@echo "Engram - AI 友好的事实账本与记忆管理模块"
	@echo ""
	@echo "使用方式: make [目标]"
	@echo ""
	@echo "可用目标:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "环境变量:"
	@echo "  POSTGRES_DSN          PostgreSQL 连接字符串 (默认: $(POSTGRES_DSN))"
	@echo "  GATEWAY_PORT          Gateway 端口 (默认: $(GATEWAY_PORT))"
	@echo "  OPENMEMORY_BASE_URL   OpenMemory 服务地址 (默认: $(OPENMEMORY_BASE_URL))"
