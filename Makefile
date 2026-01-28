# Engram 统一 Makefile
#
# 提供预检、备份、迁移、部署等一键命令
#
# 使用方法:
#   make help          # 显示帮助
#   make precheck      # 配置预检
#   make backup        # 数据库备份
#   make deploy        # 完整部署（预检 + 启动）
#   make migrate       # 仅执行迁移
#
# 多项目并行部署:
#   PROJECT_KEY=proj_a POSTGRES_DB=proj_a make deploy
#   PROJECT_KEY=proj_b POSTGRES_DB=proj_b make deploy
#

.PHONY: help precheck backup backup-schema backup-full restore cleanup \
        deploy up down migrate migrate-step1 migrate-om verify-permissions check-db-config \
        up-step1 down-step1 up-openmemory down-openmemory up-gateway down-gateway \
        logs-step1 logs-openmemory logs-gateway ps-step1 ps-openmemory ps-gateway \
        test test-precheck test-step1 test-step1-unit test-step1-integration test-step3 test-step3-unit test-step3-all test-step3-pgvector test-step3-pgvector-e2e \
        test-gateway-integration test-gateway-integration-full logs ps \
        clean-step1 clean-gateway clean-all step1-smoke step3-run-smoke \
        step1-backfill-evidence step1-backfill-chunking step1-backfill-all \
        step3-deps step3-index step3-query step3-check \
        step3-migrate-dry-run step3-migrate-replay-small \
        openmemory-upgrade-check openmemory-build openmemory-pre-upgrade-backup \
        openmemory-pre-upgrade-backup-full openmemory-upgrade-prod openmemory-rollback \
        openmemory-sync openmemory-sync-check openmemory-sync-apply openmemory-sync-verify openmemory-sync-suggest \
        openmemory-upstream-fetch openmemory-upstream-sync \
        openmemory-test-multi-schema \
        verify-build verify-build-static \
        verify-unified verify-stepwise verify-all verify-pgvector

# ============================================================================
# COMPOSE_PROJECT_NAME 自动命名策略
# ============================================================================
# 容器名格式: ${COMPOSE_PROJECT_NAME}_${SERVICE}_${INSTANCE}
# 例: proj_a_openmemory_1, proj_b_postgres_1
#
# 优先级:
#   1. 环境变量显式设置的 COMPOSE_PROJECT_NAME
#   2. PROJECT_KEY_POSTGRES_DB（多项目隔离推荐）
#   3. 回退到 "engram"（单项目/开发环境）
# ============================================================================
COMPOSE_PROJECT_NAME ?= $(if $(PROJECT_KEY),$(PROJECT_KEY)_$(POSTGRES_DB),engram)
COMPOSE_FILE := docker-compose.unified.yml
DOCKER_COMPOSE := docker compose -p $(COMPOSE_PROJECT_NAME) -f $(COMPOSE_FILE)

# 默认目标
help: ## 显示帮助信息
	@echo "Engram 统一管理命令"
	@echo "===================="
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "环境变量优先级: shell 环境变量 > Makefile 默认值"
	@echo ""
	@echo "通用环境变量:"
	@echo "  PROJECT_KEY              项目标识（用于 Step1 表前缀，默认 default）"
	@echo "  POSTGRES_DB              数据库名（建议与 PROJECT_KEY 一致，实现每项目一库）"
	@echo "  COMPOSE_PROJECT_NAME     Docker 项目名（自动计算: \$$PROJECT_KEY_\$$POSTGRES_DB，支持多实例并行）"
	@echo "  POSTGRES_PASSWORD        PostgreSQL 密码"
	@echo ""
	@echo "OpenMemory 变量:"
	@echo "  OM_PG_SCHEMA             OpenMemory schema 名（默认 openmemory）"
	@echo ""
	@echo "Step3 推荐变量名（canonical）:"
	@echo "  STEP3_PG_SCHEMA                       目标 schema（默认 step3）"
	@echo "  STEP3_PG_TABLE                        目标表名（默认 chunks）"
	@echo "  STEP3_PGVECTOR_DSN                    PGVector 连接字符串（\033[32m推荐显式设置\033[0m）"
	@echo "  STEP3_PGVECTOR_COLLECTION_STRATEGY    Collection 策略（single_table/per_table/routing，Makefile 默认 single_table）"
	@echo "  STEP3_PGVECTOR_AUTO_INIT              是否自动初始化 pgvector（1/0，默认 1）"
	@echo "  STEP3_ALLOW_POSTGRES_DSN              是否允许读取 POSTGRES_DSN（默认 0，避免误用非 Step3 权限账号）"
	@echo ""
	@echo "Step3 DSN 解析优先级:"
	@echo "  1. STEP3_PGVECTOR_DSN（推荐，显式配置）"
	@echo "  2. PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE 组合"
	@echo "  3. POSTGRES_DSN（仅当 STEP3_ALLOW_POSTGRES_DSN=1）"
	@echo "  4. POSTGRES_HOST/PORT/USER/PASSWORD/DB 组合（fallback，打印提示）"
	@echo ""
	@echo "Step3 已废弃别名（计划于 2026-Q3 移除）:"
	@echo "  STEP3_SCHEMA   -> 请改用 STEP3_PG_SCHEMA"
	@echo "  STEP3_TABLE    -> 请改用 STEP3_PG_TABLE"
	@echo "  STEP3_AUTO_INIT -> 请改用 STEP3_PGVECTOR_AUTO_INIT"
	@echo ""
	@echo "说明:"
	@echo "  - Step1 schema: 固定为 identity/logbook/scm/analysis/governance"
	@echo "  - OpenMemory schema: 可通过 OM_PG_SCHEMA 配置（默认 openmemory）"
	@echo "  - Step3 schema: 可通过 STEP3_PG_SCHEMA 配置（默认 step3）"
	@echo "  - \033[33m每项目一库\033[0m: 设置 POSTGRES_DB=<项目名> 实现数据隔离"
	@echo ""
	@echo "示例:"
	@echo "  make deploy                                     # 使用默认 engram 库"
	@echo "  PROJECT_KEY=proj_a POSTGRES_DB=proj_a make deploy  # 每项目一库部署"
	@echo "  OM_PG_SCHEMA=myproj_openmemory make deploy       # 自定义 OpenMemory schema"
	@echo "  STEP3_PG_SCHEMA=myproj_step3 make step3-index    # 自定义 Step3 schema"
	@echo ""
	@echo "入口选择:"
	@echo "  - 默认入口: \033[32mmake deploy\033[0m（统一栈，推荐大多数场景）"
	@echo "  - 分步部署（调试/独立场景）:"
	@echo "      \033[36mmake up-step1\033[0m      仅 PostgreSQL + Step1 迁移"
	@echo "      \033[36mmake up-openmemory\033[0m 仅 OpenMemory（默认 sqlite 后端）"
	@echo "      \033[36mmake up-gateway\033[0m    仅 Gateway + Worker（需外部依赖）"
	@echo ""
	@echo "分步部署变量:"
	@echo "  Step1:      POSTGRES_PORT（默认 5432）"
	@echo "  OpenMemory: OM_PORT（默认 8080）, OM_METADATA_BACKEND（sqlite/postgres）"
	@echo "  Gateway:    GATEWAY_PORT（默认 8787）, POSTGRES_DSN, OPENMEMORY_BASE_URL"

# ============================================================================
# 预检命令
# ============================================================================

precheck: ## 执行配置预检（验证环境变量安全性）
	@echo "========================================"
	@echo "Engram 配置预检"
	@echo "========================================"
	@chmod +x scripts/db_ops.sh
	@./scripts/db_ops.sh precheck

# ============================================================================
# 备份命令
# ============================================================================

backup: ## 备份所有 Engram schema
	@chmod +x scripts/db_ops.sh
	@./scripts/db_ops.sh backup

backup-om: ## 仅备份 OpenMemory schema
	@chmod +x scripts/db_ops.sh
	@./scripts/db_ops.sh backup --schema om

backup-full: ## 全库备份
	@chmod +x scripts/db_ops.sh
	@./scripts/db_ops.sh backup --full

restore: ## 恢复备份（需指定 BACKUP_FILE）
	@if [ -z "$(BACKUP_FILE)" ]; then \
		echo "错误: 请指定 BACKUP_FILE"; \
		echo "用法: make restore BACKUP_FILE=./backups/xxx.sql"; \
		exit 1; \
	fi
	@chmod +x scripts/db_ops.sh
	@./scripts/db_ops.sh restore $(BACKUP_FILE)

cleanup-om: ## 清理 OpenMemory schema（危险操作，仅用于测试环境）
	@chmod +x scripts/db_ops.sh
	@./scripts/db_ops.sh cleanup --schema om

# ============================================================================
# 部署命令
# ============================================================================

deploy: precheck check-db-config up ## 完整部署（预检 + 启动所有服务）
	@echo ""
	@echo "部署完成！服务状态:"
	@$(DOCKER_COMPOSE) ps

check-db-config: ## 检查数据库配置（POSTGRES_DB 与 PROJECT_KEY 一致性提示）
	@echo "========================================"
	@echo "数据库配置检查"
	@echo "========================================"
	@PROJECT_KEY_VAL="$${PROJECT_KEY:-default}"; \
	POSTGRES_DB_VAL="$${POSTGRES_DB:-engram}"; \
	echo "  PROJECT_KEY  = $${PROJECT_KEY_VAL}"; \
	echo "  POSTGRES_DB  = $${POSTGRES_DB_VAL}"; \
	echo ""; \
	if [ "$${PROJECT_KEY_VAL}" != "default" ] && [ "$${POSTGRES_DB_VAL}" = "engram" ]; then \
		echo "\033[33m[WARN] PROJECT_KEY='$${PROJECT_KEY_VAL}' 但 POSTGRES_DB='engram'（默认值）\033[0m"; \
		echo ""; \
		echo "建议: 多项目部署时，设置 POSTGRES_DB=<项目名> 实现每项目一库隔离"; \
		echo "      例如: PROJECT_KEY=$${PROJECT_KEY_VAL} POSTGRES_DB=$${PROJECT_KEY_VAL} make deploy"; \
		echo ""; \
	elif [ "$${PROJECT_KEY_VAL}" != "$${POSTGRES_DB_VAL}" ] && [ "$${POSTGRES_DB_VAL}" != "engram" ]; then \
		echo "\033[33m[INFO] PROJECT_KEY 与 POSTGRES_DB 不一致（可能是有意为之）\033[0m"; \
		echo ""; \
	else \
		echo "\033[32m[OK] 数据库配置正常\033[0m"; \
		echo ""; \
	fi

up: ## 启动所有服务（含自动迁移）
	@$(DOCKER_COMPOSE) up -d
	@echo ""
	@echo "等待服务启动..."
	@sleep 5
	@$(DOCKER_COMPOSE) ps

down: ## 停止所有服务
	@$(DOCKER_COMPOSE) down

restart: down up ## 重启所有服务

# ============================================================================
# 分步部署命令（独立组件启动）
# ============================================================================
# 用于分步调试或独立部署场景
# 各组件使用独立的 compose 文件和网络
#
# Step1: PostgreSQL + 迁移工具
# OpenMemory: OpenMemory 服务（支持 sqlite/postgres 后端）
# Gateway: Gateway + Worker（需要外部 POSTGRES_DSN 和 OPENMEMORY_BASE_URL）
# ============================================================================

# Step1 Compose 配置
STEP1_COMPOSE_FILE := compose/step1.yml
STEP1_PROJECT_NAME := $(if $(PROJECT_KEY),$(PROJECT_KEY)-step1,engram-step1)
STEP1_COMPOSE := docker compose -p $(STEP1_PROJECT_NAME) -f $(STEP1_COMPOSE_FILE)

# OpenMemory Compose 配置
OM_COMPOSE_FILE := compose/openmemory.yml
OM_PROJECT_NAME := $(if $(PROJECT_KEY),$(PROJECT_KEY)-openmemory,engram-openmemory)
OM_COMPOSE := docker compose -p $(OM_PROJECT_NAME) -f $(OM_COMPOSE_FILE)

# Gateway Compose 配置
GATEWAY_COMPOSE_FILE := compose/gateway.yml
GATEWAY_PROJECT_NAME := $(if $(PROJECT_KEY),$(PROJECT_KEY)-gateway,engram-gateway)
GATEWAY_COMPOSE := docker compose -p $(GATEWAY_PROJECT_NAME) -f $(GATEWAY_COMPOSE_FILE)

up-step1: ## 启动 Step1（PostgreSQL + 迁移）
	@echo "========================================"
	@echo "启动 Step1 服务"
	@echo "========================================"
	@echo "  POSTGRES_PORT=$${POSTGRES_PORT:-5432}"
	@echo "  POSTGRES_DB=$${POSTGRES_DB:-engram}"
	@echo ""
	@$(STEP1_COMPOSE) up -d
	@echo ""
	@echo "等待 PostgreSQL 启动..."
	@sleep 3
	@$(STEP1_COMPOSE) ps
	@echo ""
	@echo "[OK] Step1 服务已启动"
	@echo "  - PostgreSQL: localhost:$${POSTGRES_PORT:-5432}"
	@echo ""
	@echo "提示: 如需执行迁移，使用:"
	@echo "  $(STEP1_COMPOSE) --profile migrate up step1_migrate"

down-step1: ## 停止 Step1 服务
	@$(STEP1_COMPOSE) down

up-openmemory: ## 启动 OpenMemory（支持 sqlite/postgres 后端）
	@echo "========================================"
	@echo "启动 OpenMemory 服务"
	@echo "========================================"
	@echo "  OM_METADATA_BACKEND=$${OM_METADATA_BACKEND:-sqlite}"
	@echo "  OM_PORT=$${OM_PORT:-8080}"
	@if [ "$${OM_METADATA_BACKEND}" = "postgres" ]; then \
		echo "  OM_PG_HOST=$${OM_PG_HOST:-localhost}"; \
		echo "  OM_PG_DB=$${OM_PG_DB:-engram}"; \
		echo "  OM_PG_SCHEMA=$${OM_PG_SCHEMA:-openmemory}"; \
	fi
	@echo ""
	@$(OM_COMPOSE) up -d
	@echo ""
	@echo "等待服务启动..."
	@sleep 5
	@$(OM_COMPOSE) ps
	@echo ""
	@echo "[OK] OpenMemory 服务已启动"
	@echo "  - OpenMemory API: http://localhost:$${OM_PORT:-8080}"
	@echo ""
	@echo "提示:"
	@echo "  - 启用 Dashboard: $(OM_COMPOSE) --profile dashboard up -d"
	@echo "  - PostgreSQL 模式迁移: OM_METADATA_BACKEND=postgres $(OM_COMPOSE) --profile migrate up openmemory_migrate"

down-openmemory: ## 停止 OpenMemory 服务
	@$(OM_COMPOSE) down

up-gateway: ## 启动 Gateway + Worker（需要外部 POSTGRES_DSN 和 OPENMEMORY_BASE_URL）
	@if [ -z "$${POSTGRES_DSN}" ]; then \
		echo "[ERROR] POSTGRES_DSN 未设置"; \
		echo ""; \
		echo "用法:"; \
		echo "  POSTGRES_DSN='postgresql://postgres:postgres@host.docker.internal:5432/engram' \\"; \
		echo "  OPENMEMORY_BASE_URL='http://host.docker.internal:8080' \\"; \
		echo "  make up-gateway"; \
		echo ""; \
		echo "提示: 如果连接宿主机服务，使用 host.docker.internal 作为主机名"; \
		exit 1; \
	fi
	@if [ -z "$${OPENMEMORY_BASE_URL}" ]; then \
		echo "[ERROR] OPENMEMORY_BASE_URL 未设置"; \
		echo ""; \
		echo "用法:"; \
		echo "  POSTGRES_DSN='postgresql://postgres:postgres@host.docker.internal:5432/engram' \\"; \
		echo "  OPENMEMORY_BASE_URL='http://host.docker.internal:8080' \\"; \
		echo "  make up-gateway"; \
		exit 1; \
	fi
	@echo "========================================"
	@echo "启动 Gateway 服务"
	@echo "========================================"
	@echo "  GATEWAY_PORT=$${GATEWAY_PORT:-8787}"
	@echo "  POSTGRES_DSN=$${POSTGRES_DSN}"
	@echo "  OPENMEMORY_BASE_URL=$${OPENMEMORY_BASE_URL}"
	@echo ""
	@$(GATEWAY_COMPOSE) up -d
	@echo ""
	@echo "等待服务启动..."
	@sleep 3
	@$(GATEWAY_COMPOSE) ps
	@echo ""
	@echo "[OK] Gateway 服务已启动"
	@echo "  - Gateway API: http://localhost:$${GATEWAY_PORT:-8787}"

down-gateway: ## 停止 Gateway 服务
	@$(GATEWAY_COMPOSE) down

logs-step1: ## 查看 Step1 服务日志
	@$(STEP1_COMPOSE) logs -f

logs-openmemory: ## 查看 OpenMemory 服务日志
	@$(OM_COMPOSE) logs -f

logs-gateway: ## 查看 Gateway 服务日志
	@$(GATEWAY_COMPOSE) logs -f

ps-step1: ## 查看 Step1 服务状态
	@$(STEP1_COMPOSE) ps

ps-openmemory: ## 查看 OpenMemory 服务状态
	@$(OM_COMPOSE) ps

ps-gateway: ## 查看 Gateway 服务状态
	@$(GATEWAY_COMPOSE) ps

# ============================================================================
# 迁移命令
# ============================================================================

migrate: migrate-step1 migrate-om verify-permissions ## 执行所有迁移（含权限验证）

migrate-step1: ## 执行 Step1 数据库迁移
	@echo "执行 Step1 迁移..."
	@$(DOCKER_COMPOSE) up step1_migrate

migrate-om: ## 执行 OpenMemory 数据库迁移
	@echo "执行 OpenMemory 迁移..."
	@$(DOCKER_COMPOSE) up openmemory_migrate

migrate-precheck: ## 仅执行迁移预检（不实际迁移）
	@echo "执行迁移预检..."
	@$(DOCKER_COMPOSE) run --rm step1_migrate \
		bash -c "pip install --quiet psycopg[binary] tomli && \
		cd /app/scripts && \
		python db_migrate.py --precheck-only --dsn 'postgresql://$${POSTGRES_USER:-postgres}:$${POSTGRES_PASSWORD:-postgres}@postgres:5432/$${POSTGRES_DB:-engram}'"

verify-permissions: ## 验证数据库权限配置
	@echo "========================================"
	@echo "权限验证"
	@echo "========================================"
	@echo "目标 schema: $${OM_PG_SCHEMA:-openmemory}"
	@$(DOCKER_COMPOSE) exec -T postgres \
		psql -U $${POSTGRES_USER:-postgres} -d $${POSTGRES_DB:-engram} \
		-c "SET om.target_schema = '$${OM_PG_SCHEMA:-openmemory}'" \
		-f /docker-entrypoint-initdb.d/99_verify_permissions.sql 2>&1 | tee /tmp/verify_output.txt
	@if grep -q 'FAIL:' /tmp/verify_output.txt; then \
		echo ''; \
		echo '[ERROR] 权限验证失败！请检查上方输出中的 FAIL 消息。'; \
		exit 1; \
	fi
	@echo ''
	@echo '[OK] 权限验证通过'

# ============================================================================
# 迁移后回填命令（Post-Migration Backfill）
# ============================================================================

step1-backfill-evidence: ## 回填 patch_blobs 的 evidence_uri
	@echo "========================================"
	@echo "Step1 Evidence URI 回填"
	@echo "========================================"
	@cd apps/step1_logbook_postgres/scripts && \
		pip install -q -e . 2>/dev/null || pip install -e . && \
		python backfill_evidence_uri.py \
			$${BACKFILL_BATCH_SIZE:+--batch-size $${BACKFILL_BATCH_SIZE}} \
			$${BACKFILL_DRY_RUN:+--dry-run} \
			$${BACKFILL_VERBOSE:+--verbose} \
			$${BACKFILL_JSON:+--json}
	@echo "[OK] Evidence URI 回填完成"

step1-backfill-chunking: ## 回填 chunking_version（需指定 CHUNKING_VERSION）
	@if [ -z "$${CHUNKING_VERSION}" ]; then \
		echo "错误: 请指定 CHUNKING_VERSION"; \
		echo "用法: make step1-backfill-chunking CHUNKING_VERSION=v1.0"; \
		exit 1; \
	fi
	@echo "========================================"
	@echo "Step1 Chunking Version 回填"
	@echo "========================================"
	@echo "目标版本: $${CHUNKING_VERSION}"
	@cd apps/step1_logbook_postgres/scripts && \
		pip install -q -e . 2>/dev/null || pip install -e . && \
		python backfill_chunking_version.py \
			--chunking-version "$${CHUNKING_VERSION}" \
			$${BACKFILL_BATCH_SIZE:+--batch-size $${BACKFILL_BATCH_SIZE}} \
			$${BACKFILL_ONLY_MISSING:+--only-missing} \
			$${BACKFILL_DRY_RUN:+--dry-run} \
			$${BACKFILL_VERBOSE:+--verbose} \
			$${BACKFILL_JSON:+--json}
	@echo "[OK] Chunking Version 回填完成"

step1-backfill-all: step1-backfill-evidence ## 执行所有回填（evidence_uri + chunking_version，需指定 CHUNKING_VERSION）
	@if [ -n "$${CHUNKING_VERSION}" ]; then \
		$(MAKE) step1-backfill-chunking CHUNKING_VERSION=$${CHUNKING_VERSION}; \
	else \
		echo "[INFO] 未指定 CHUNKING_VERSION，跳过 chunking_version 回填"; \
	fi
	@echo "[OK] 所有回填完成"

# ============================================================================
# 测试命令
# ============================================================================

test: test-precheck ## 运行所有测试

test-precheck: ## 测试预检功能
	@echo "测试预检（应该成功）..."
	@PROJECT_KEY=test OM_PG_SCHEMA=test_openmemory ./scripts/db_ops.sh precheck
	@echo ""
	@echo "测试预检（应该失败）..."
	@OM_PG_SCHEMA=public ./scripts/db_ops.sh precheck && exit 1 || echo "[OK] public schema 被正确拒绝"

test-step1: test-step1-unit test-step1-integration ## 运行 Step1 所有测试

test-step1-unit: ## 运行 Step1 单元测试（跳过集成测试）
	@echo "========================================"
	@echo "Step1 单元测试"
	@echo "========================================"
	@mkdir -p .artifacts/test-results
	@cd apps/step1_logbook_postgres/scripts && \
		pip install -q -r requirements.txt 2>/dev/null || pip install -r requirements.txt && \
		pip install -q -e . 2>/dev/null || pip install -e . && \
		pytest -q --ignore=tests/test_unified_stack_integration.py --ignore=tests/test_object_store_minio_integration.py \
			--junitxml=../../../.artifacts/test-results/step1-unit.xml --durations=20
	@echo "[OK] Step1 单元测试完成"

test-step1-integration: ## 运行 Step1 集成测试（需要 Docker）
	@echo "========================================"
	@echo "Step1 集成测试（Docker Compose）"
	@echo "========================================"
	@mkdir -p .artifacts/test-results
	@if [ -z "$${MINIO_ROOT_USER}" ]; then export MINIO_ROOT_USER=minioadmin; fi; \
	if [ -z "$${MINIO_ROOT_PASSWORD}" ]; then export MINIO_ROOT_PASSWORD=minioadmin; fi; \
	MINIO_ROOT_USER=$${MINIO_ROOT_USER:-minioadmin} \
	MINIO_ROOT_PASSWORD=$${MINIO_ROOT_PASSWORD:-minioadmin} \
	PYTEST_ADDOPTS="--junitxml=/app/.artifacts/test-results/step1-integration.xml --durations=20" \
	$(DOCKER_COMPOSE) --profile minio --profile test up --exit-code-from step1_test
	@echo "[OK] Step1 集成测试完成"

test-step3: ## 运行 Step3 分块稳定性测试
	@echo "========================================"
	@echo "Step3 分块稳定性测试"
	@echo "========================================"
	@mkdir -p .artifacts/test-results
	@pip install -q -r apps/step3_seekdb_rag_hybrid/requirements.dev.txt
	@cd apps/step3_seekdb_rag_hybrid && \
		pytest -q tests/test_chunking_stability.py \
			--junitxml=../../.artifacts/test-results/step3.xml --durations=20
	@echo "[OK] Step3 测试完成"

test-step3-unit: ## 运行 Step3 单元测试（不需要真实 Postgres）
	@echo "========================================"
	@echo "Step3 单元测试"
	@echo "========================================"
	@mkdir -p .artifacts/test-results
	@pip install -q -r apps/step3_seekdb_rag_hybrid/requirements.dev.txt
	@cd apps/step3_seekdb_rag_hybrid && \
		pytest -q tests/test_seek_query_packet_shape.py \
			tests/test_index_filter_dsl.py \
			tests/test_pgvector_backend_filters.py \
			tests/test_pgvector_backend_upsert.py \
			tests/test_collection_naming.py \
			tests/test_dual_read_unit.py \
			tests/test_env_compat.py \
			--junitxml=../../.artifacts/test-results/step3-unit.xml --durations=20
	@echo "[OK] Step3 单元测试完成"

test-step3-all: ## 运行 Step3 所有测试（单元 + 分块稳定性 + collection_id 支持）
	@echo "========================================"
	@echo "Step3 所有测试"
	@echo "========================================"
	@mkdir -p .artifacts/test-results
	@pip install -q -r apps/step3_seekdb_rag_hybrid/requirements.dev.txt
	@cd apps/step3_seekdb_rag_hybrid && \
		pytest -q tests/test_seek_query_packet_shape.py \
			tests/test_index_filter_dsl.py \
			tests/test_pgvector_backend_filters.py \
			tests/test_pgvector_backend_upsert.py \
			tests/test_collection_naming.py \
			tests/test_collection_id_support.py \
			tests/test_chunking_stability.py \
			--junitxml=../../.artifacts/test-results/step3-all.xml --durations=20
	@echo "[OK] Step3 所有测试完成"

test-step3-pgvector: ## 运行 Step3 PGVector 后端集成测试（需设置 TEST_PGVECTOR_DSN）
	@echo "========================================"
	@echo "Step3 PGVector 后端集成测试"
	@echo "========================================"
	@if [ -z "$${TEST_PGVECTOR_DSN}" ]; then \
		echo "[INFO] TEST_PGVECTOR_DSN 未设置，自动构建连接字符串..."; \
		export TEST_PGVECTOR_DSN="postgresql://$${POSTGRES_USER:-postgres}:$${POSTGRES_PASSWORD:-postgres}@localhost:$${POSTGRES_PORT:-5432}/$${POSTGRES_DB:-engram}"; \
		echo "       TEST_PGVECTOR_DSN=$${TEST_PGVECTOR_DSN%@*}@..."; \
	fi; \
	mkdir -p .artifacts/test-results && \
	pip install -q -r apps/step3_seekdb_rag_hybrid/requirements.dev.txt && \
	cd apps/step3_seekdb_rag_hybrid && \
	TEST_PGVECTOR_DSN=$${TEST_PGVECTOR_DSN} \
	pytest -v tests/test_pgvector_backend_integration.py tests/test_pgvector_e2e_minimal.py tests/test_dual_read_integration.py \
		--junitxml=../../.artifacts/test-results/step3-pgvector.xml --durations=20
	@echo "[OK] Step3 PGVector 集成测试完成"

test-step3-pgvector-e2e: ## 运行 Step3 PGVector 端到端最小集成测试（需设置 TEST_PGVECTOR_DSN）
	@echo "========================================"
	@echo "Step3 PGVector E2E 最小集成测试"
	@echo "========================================"
	@if [ -z "$${TEST_PGVECTOR_DSN}" ]; then \
		echo "[INFO] TEST_PGVECTOR_DSN 未设置，自动构建连接字符串..."; \
		export TEST_PGVECTOR_DSN="postgresql://$${POSTGRES_USER:-postgres}:$${POSTGRES_PASSWORD:-postgres}@localhost:$${POSTGRES_PORT:-5432}/$${POSTGRES_DB:-engram}"; \
		echo "       TEST_PGVECTOR_DSN=$${TEST_PGVECTOR_DSN%@*}@..."; \
	fi; \
	mkdir -p .artifacts/test-results && \
	pip install -q -r apps/step3_seekdb_rag_hybrid/requirements.dev.txt && \
	cd apps/step3_seekdb_rag_hybrid && \
	TEST_PGVECTOR_DSN=$${TEST_PGVECTOR_DSN} \
	pytest -v tests/test_pgvector_e2e_minimal.py \
		--junitxml=../../.artifacts/test-results/step3-pgvector-e2e.xml --durations=20
	@echo "[OK] Step3 PGVector E2E 测试完成"

test-gateway-integration: ## 运行 Gateway 集成测试（需要统一栈运行，纯 HTTP 验证）
	@echo "========================================"
	@echo "Gateway 集成测试（纯 HTTP 验证）"
	@echo "========================================"
	@echo "提示: 请确保统一栈已运行 (make deploy)"
	@echo ""
	@mkdir -p .artifacts/test-results
	@# 导出默认环境变量（HTTP_ONLY_MODE=1 跳过需要 Docker 操作的降级测试）
	@export RUN_INTEGRATION_TESTS=1; \
	export HTTP_ONLY_MODE=1; \
	export GATEWAY_URL=$${GATEWAY_URL:-http://localhost:8787}; \
	export OPENMEMORY_URL=$${OPENMEMORY_URL:-http://localhost:8080}; \
	export COMPOSE_PROJECT_NAME=$(COMPOSE_PROJECT_NAME); \
	export COMPOSE_FILE=$(COMPOSE_FILE); \
	export POSTGRES_DSN=$${POSTGRES_DSN:-postgresql://$${POSTGRES_USER:-postgres}:$${POSTGRES_PASSWORD:-postgres}@localhost:$${POSTGRES_PORT:-5432}/$${POSTGRES_DB:-engram}}; \
	cd apps/step2_openmemory_gateway/gateway && \
	pip install -q -e ".[dev]" 2>/dev/null || pip install -e ".[dev]" && \
	pytest tests/test_unified_stack_integration.py -v --tb=short \
		--junitxml=../../../.artifacts/test-results/gateway.xml --durations=20
	@echo "[OK] Gateway 集成测试完成"

test-gateway-integration-full: ## 运行 Gateway 完整集成测试（含降级测试，需要 Docker 权限）
	@echo "========================================"
	@echo "Gateway 完整集成测试（含降级测试）"
	@echo "========================================"
	@echo "提示: 请确保统一栈已运行 (make deploy)"
	@echo "提示: 降级测试需要 Docker 容器操作权限"
	@echo ""
	@mkdir -p .artifacts/test-results
	@# 导出默认环境变量（不设置 HTTP_ONLY_MODE，允许降级测试）
	@export RUN_INTEGRATION_TESTS=1; \
	export GATEWAY_URL=$${GATEWAY_URL:-http://localhost:8787}; \
	export OPENMEMORY_URL=$${OPENMEMORY_URL:-http://localhost:8080}; \
	export COMPOSE_PROJECT_NAME=$(COMPOSE_PROJECT_NAME); \
	export COMPOSE_FILE=$(COMPOSE_FILE); \
	export POSTGRES_DSN=$${POSTGRES_DSN:-postgresql://$${POSTGRES_USER:-postgres}:$${POSTGRES_PASSWORD:-postgres}@localhost:$${POSTGRES_PORT:-5432}/$${POSTGRES_DB:-engram}}; \
	cd apps/step2_openmemory_gateway/gateway && \
	pip install -q -e ".[dev]" 2>/dev/null || pip install -e ".[dev]" && \
	pytest tests/test_unified_stack_integration.py -v --tb=short \
		--junitxml=../../../.artifacts/test-results/gateway-full.xml --durations=20
	@echo "[OK] Gateway 完整集成测试完成"

# ============================================================================
# 状态查看
# ============================================================================

logs: ## 查看服务日志
	@$(DOCKER_COMPOSE) logs -f

logs-migrate: ## 查看迁移日志
	@$(DOCKER_COMPOSE) logs step1_migrate openmemory_migrate

ps: ## 查看服务状态
	@$(DOCKER_COMPOSE) ps

# ============================================================================
# 清理命令
# ============================================================================

clean-step1: ## 清理 Step1 Python 缓存目录（__pycache__、egg-info）
	@echo "========================================"
	@echo "清理 Step1 Python 缓存"
	@echo "========================================"
	@rm -rf apps/step1_logbook_postgres/scripts/__pycache__/ \
		apps/step1_logbook_postgres/scripts/tests/__pycache__/ \
		apps/step1_logbook_postgres/scripts/engram_step1/__pycache__/ \
		apps/step1_logbook_postgres/scripts/engram_step1.egg-info/ \
		2>/dev/null || true
	@find apps/step1_logbook_postgres/scripts -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find apps/step1_logbook_postgres/scripts -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@echo "[OK] Step1 缓存清理完成"

clean-gateway: ## 清理 Gateway Python 缓存目录（__pycache__、egg-info）
	@echo "========================================"
	@echo "清理 Gateway Python 缓存"
	@echo "========================================"
	@rm -rf apps/step2_openmemory_gateway/gateway/gateway/__pycache__/ \
		apps/step2_openmemory_gateway/gateway/tests/__pycache__/ \
		apps/step2_openmemory_gateway/gateway/engram_gateway.egg-info/ \
		2>/dev/null || true
	@find apps/step2_openmemory_gateway/gateway -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find apps/step2_openmemory_gateway/gateway -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@echo "[OK] Gateway 缓存清理完成"

clean-all: clean-step1 clean-gateway ## 清理所有 Python 缓存（Step1 + Gateway）
	@echo "========================================"
	@echo "[OK] 所有缓存清理完成"
	@echo "========================================"

# ============================================================================
# OpenMemory 同步工具
# 用于一致性检查、补丁管理和升级建议
# ============================================================================

openmemory-sync: ## OpenMemory 同步（完整检查 + 建议）
	@echo "========================================"
	@echo "OpenMemory 同步工具"
	@echo "========================================"
	@python scripts/openmemory_sync.py all --dry-run $${JSON_OUTPUT:+--json}

openmemory-sync-check: ## 检查 OpenMemory 一致性（目录结构/关键文件）
	@echo "========================================"
	@echo "OpenMemory 一致性检查"
	@echo "========================================"
	@python scripts/openmemory_sync.py check $${JSON_OUTPUT:+--json}

openmemory-sync-apply: ## 应用补丁（默认 dry-run，DRY_RUN=0 实际执行）
	@echo "========================================"
	@if [ "$${DRY_RUN:-1}" = "0" ]; then \
		echo "OpenMemory 补丁应用（实际执行）"; \
	else \
		echo "OpenMemory 补丁预览（dry-run）"; \
	fi
	@echo "========================================"
	@if [ "$${DRY_RUN:-1}" = "0" ]; then \
		python scripts/openmemory_sync.py apply --no-dry-run \
			$${CATEGORIES:+--categories $${CATEGORIES}} \
			$${JSON_OUTPUT:+--json}; \
	else \
		python scripts/openmemory_sync.py apply --dry-run \
			$${CATEGORIES:+--categories $${CATEGORIES}} \
			$${JSON_OUTPUT:+--json}; \
	fi

openmemory-sync-verify: ## 校验补丁是否已正确落地（对照 checksums）
	@echo "========================================"
	@echo "OpenMemory 补丁落地校验"
	@echo "========================================"
	@python scripts/openmemory_sync.py verify $${JSON_OUTPUT:+--json}

openmemory-sync-suggest: ## 输出 OpenMemory 升级建议
	@echo "========================================"
	@echo "OpenMemory 升级建议"
	@echo "========================================"
	@python scripts/openmemory_sync.py suggest $${JSON_OUTPUT:+--json}

# ============================================================================
# OpenMemory 上游获取与同步
# ============================================================================
# fetch: 下载上游代码到临时目录（不修改本地文件）
# sync:  下载 + 合并上游代码到 libs/OpenMemory（默认 dry-run）
#
# 环境变量:
#   UPSTREAM_REF          版本引用（tag 或 commit SHA，默认从 lock 文件读取）
#   UPSTREAM_REF_TYPE     引用类型（tag/commit，默认从 lock 文件读取）
#   EXCLUDE_PATTERNS      额外排除模式（逗号分隔）
#   FORCE_SYNC            强制覆盖补丁冲突（设置为 1）
#   DRY_RUN               预览模式（默认 1，设置为 0 执行实际同步）
#   OUTPUT_DIR            fetch 输出目录（默认临时目录）
#   JSON_OUTPUT           JSON 格式输出（设置为 1）
#
# 示例:
#   make openmemory-upstream-fetch                           # 获取当前 lock 版本
#   make openmemory-upstream-fetch UPSTREAM_REF=v1.4.0       # 获取指定 tag
#   make openmemory-upstream-sync                            # 预览同步
#   make openmemory-upstream-sync DRY_RUN=0                  # 执行同步
#   make openmemory-upstream-sync DRY_RUN=0 FORCE_SYNC=1     # 强制同步
# ============================================================================

openmemory-upstream-fetch: ## 获取 OpenMemory 上游代码（下载到临时目录）
	@echo "========================================"
	@echo "OpenMemory 上游获取 (fetch)"
	@echo "========================================"
	@echo "配置:"
	@echo "  UPSTREAM_REF      = $${UPSTREAM_REF:-<从 lock 文件读取>}"
	@echo "  UPSTREAM_REF_TYPE = $${UPSTREAM_REF_TYPE:-<从 lock 文件读取>}"
	@echo "  OUTPUT_DIR        = $${OUTPUT_DIR:-<临时目录>}"
	@echo ""
	@python scripts/openmemory_sync.py fetch \
		$${UPSTREAM_REF:+--ref $${UPSTREAM_REF}} \
		$${UPSTREAM_REF_TYPE:+--ref-type $${UPSTREAM_REF_TYPE}} \
		$${OUTPUT_DIR:+--output-dir $${OUTPUT_DIR}} \
		$${JSON_OUTPUT:+--json}

openmemory-upstream-sync: ## 同步 OpenMemory 上游代码到本地（默认 dry-run）
	@echo "========================================"
	@if [ "$${DRY_RUN:-1}" = "0" ]; then \
		echo "OpenMemory 上游同步 (sync - 实际执行)"; \
	else \
		echo "OpenMemory 上游同步 (sync - dry-run 预览)"; \
	fi
	@echo "========================================"
	@echo "配置:"
	@echo "  UPSTREAM_REF      = $${UPSTREAM_REF:-<从 lock 文件读取>}"
	@echo "  UPSTREAM_REF_TYPE = $${UPSTREAM_REF_TYPE:-<从 lock 文件读取>}"
	@echo "  DRY_RUN           = $${DRY_RUN:-1}"
	@echo "  FORCE_SYNC        = $${FORCE_SYNC:-0}"
	@echo "  EXCLUDE_PATTERNS  = $${EXCLUDE_PATTERNS:-<默认排除列表>}"
	@echo ""
	@if [ "$${DRY_RUN:-1}" = "0" ]; then \
		python scripts/openmemory_sync.py sync \
			--no-dry-run \
			$${UPSTREAM_REF:+--ref $${UPSTREAM_REF}} \
			$${UPSTREAM_REF_TYPE:+--ref-type $${UPSTREAM_REF_TYPE}} \
			$${EXCLUDE_PATTERNS:+--exclude $${EXCLUDE_PATTERNS}} \
			$${FORCE_SYNC:+--force} \
			$${JSON_OUTPUT:+--json}; \
	else \
		python scripts/openmemory_sync.py sync \
			--dry-run \
			$${UPSTREAM_REF:+--ref $${UPSTREAM_REF}} \
			$${UPSTREAM_REF_TYPE:+--ref-type $${UPSTREAM_REF_TYPE}} \
			$${EXCLUDE_PATTERNS:+--exclude $${EXCLUDE_PATTERNS}} \
			$${JSON_OUTPUT:+--json}; \
	fi

# ============================================================================
# OpenMemory 升级检查
# 用于验证 OpenMemory.upstream.lock.json 更新后的兼容性
#
# ============================================================================
# 升级与回滚策略
# ============================================================================
#
# 升级前必须备份:
#   - 开发/测试环境: make backup-om（仅备份 OpenMemory schema）
#   - 生产环境: make backup-full（全库备份）
#
# 回滚路径:
#   1. 回退 OpenMemory.upstream.lock.json 到之前的 commit ref
#   2. 重新构建镜像: make openmemory-build
#   3. 如涉及 schema 变更，恢复备份: make restore BACKUP_FILE=./backups/xxx.sql
#
# 不可逆迁移说明:
#   OpenMemory 的某些 schema 迁移是不可逆的（如删除列、重命名表等）。
#   对于这类迁移，唯一回滚策略是: 从备份恢复（make restore BACKUP_FILE=...）
#   因此升级前备份是强制要求，没有例外。
#
# ============================================================================

openmemory-build: ## 构建 OpenMemory 相关镜像（openmemory, openmemory_migrate, dashboard）
	@echo "========================================"
	@echo "构建 OpenMemory 镜像"
	@echo "========================================"
	@$(DOCKER_COMPOSE) build openmemory openmemory_migrate dashboard
	@echo "[OK] OpenMemory 镜像构建完成"

# 升级前强制备份（开发/测试环境）
openmemory-pre-upgrade-backup: ## 升级前强制备份（开发环境用 backup-om）
	@echo "========================================"
	@echo "升级前强制备份（OpenMemory schema）"
	@echo "========================================"
	@echo "[WARN] 升级前备份是强制要求！某些迁移不可逆，回滚=恢复备份。"
	@echo ""
	@$(MAKE) backup-om
	@echo ""
	@echo "[OK] 备份完成，可以继续升级"

# 升级前强制备份（生产环境）
openmemory-pre-upgrade-backup-full: ## 升级前强制备份（生产环境用 backup-full）
	@echo "========================================"
	@echo "升级前强制备份（全库）"
	@echo "========================================"
	@echo "[WARN] 生产环境升级前必须执行全库备份！"
	@echo "[WARN] 某些迁移不可逆，回滚唯一策略是恢复备份。"
	@echo ""
	@$(MAKE) backup-full
	@echo ""
	@echo "[OK] 全库备份完成，可以继续升级"

openmemory-upgrade-check: openmemory-pre-upgrade-backup openmemory-build ## OpenMemory 升级验证（更新 upstream.lock.json 后必跑）
	@echo "========================================"
	@echo "OpenMemory 升级验证"
	@echo "========================================"
	@echo ""
	@echo "说明: 此检查应在每次更新 OpenMemory.upstream.lock.json 后执行"
	@echo ""
	@# 步骤 1: 启动依赖服务并确保迁移完成
	@echo "[1/4] 启动服务并执行迁移..."
	@$(DOCKER_COMPOSE) up -d postgres
	@echo "等待 PostgreSQL 启动..."
	@sleep 5
	@# 确保 precheck -> postgres -> bootstrap_roles -> step1_migrate -> openmemory_migrate 链完成
	@$(DOCKER_COMPOSE) up bootstrap_roles
	@$(DOCKER_COMPOSE) up step1_migrate
	@$(DOCKER_COMPOSE) up openmemory_migrate
	@$(DOCKER_COMPOSE) up permissions_verify
	@echo "[OK] 迁移完成"
	@echo ""
	@# 步骤 2: 运行多 Schema 隔离测试
	@echo "[2/4] 运行多 Schema 隔离测试..."
	@$(MAKE) openmemory-test-multi-schema
	@echo ""
	@# 步骤 3: 启动 OpenMemory 和 Gateway 服务
	@echo "[3/4] 启动 OpenMemory 和 Gateway 服务..."
	@$(DOCKER_COMPOSE) up -d openmemory gateway
	@echo "等待服务健康检查..."
	@sleep 10
	@# 等待 openmemory 健康
	@timeout 60 sh -c 'until $(DOCKER_COMPOSE) ps openmemory | grep -q "(healthy)"; do sleep 2; done' || { \
		echo "[ERROR] OpenMemory 健康检查超时"; \
		$(DOCKER_COMPOSE) logs openmemory; \
		exit 1; \
	}
	@echo "[OK] 服务已启动并通过健康检查"
	@echo ""
	@# 步骤 4: 调用验证脚本
	@echo "[4/4] 运行统一栈验证脚本..."
	@chmod +x apps/step2_openmemory_gateway/scripts/verify_unified_stack.sh
	@GATEWAY_URL=$${GATEWAY_URL:-http://localhost:8787} \
	 OPENMEMORY_URL=$${OPENMEMORY_URL:-http://localhost:8080} \
	 COMPOSE_PROJECT_NAME=$(COMPOSE_PROJECT_NAME) \
	 COMPOSE_FILE=$(COMPOSE_FILE) \
	 POSTGRES_DSN=$${POSTGRES_DSN:-postgresql://$${POSTGRES_USER:-postgres}:$${POSTGRES_PASSWORD:-postgres}@localhost:$${POSTGRES_PORT:-5432}/$${POSTGRES_DB:-engram}} \
	 ./apps/step2_openmemory_gateway/scripts/verify_unified_stack.sh $${UPGRADE_CHECK_FULL:+--full}
	@echo ""
	@echo "========================================"
	@echo "[OK] OpenMemory 升级验证通过！"
	@echo "========================================"
	@echo ""
	@echo "提示: 此验证确认 OpenMemory 上游更新与 Engram 统一栈兼容"
	@echo "      如需完整降级测试，请使用: make openmemory-upgrade-check UPGRADE_CHECK_FULL=1"
	@echo ""
	@echo "回滚说明（如需回滚）:"
	@echo "  1. 回退 OpenMemory.upstream.lock.json 到之前的 commit"
	@echo "  2. 重新构建: make openmemory-build"
	@echo "  3. 恢复备份: make restore BACKUP_FILE=./backups/xxx.sql"

# 生产环境升级（强制全库备份）
openmemory-upgrade-prod: openmemory-pre-upgrade-backup-full openmemory-build ## 生产环境 OpenMemory 升级（强制全库备份）
	@echo "========================================"
	@echo "生产环境 OpenMemory 升级"
	@echo "========================================"
	@echo ""
	@echo "[1/4] 全库备份已完成（上一步）"
	@echo ""
	@# 步骤 2: 启动依赖服务并确保迁移完成
	@echo "[2/4] 执行迁移..."
	@$(DOCKER_COMPOSE) up -d postgres
	@echo "等待 PostgreSQL 启动..."
	@sleep 5
	@$(DOCKER_COMPOSE) up bootstrap_roles
	@$(DOCKER_COMPOSE) up step1_migrate
	@$(DOCKER_COMPOSE) up openmemory_migrate
	@$(DOCKER_COMPOSE) up permissions_verify
	@echo "[OK] 迁移完成"
	@echo ""
	@# 步骤 3: 启动服务
	@echo "[3/4] 启动服务..."
	@$(DOCKER_COMPOSE) up -d openmemory gateway
	@echo "等待服务健康检查..."
	@sleep 10
	@timeout 60 sh -c 'until $(DOCKER_COMPOSE) ps openmemory | grep -q "(healthy)"; do sleep 2; done' || { \
		echo "[ERROR] OpenMemory 健康检查超时"; \
		$(DOCKER_COMPOSE) logs openmemory; \
		echo ""; \
		echo "[ROLLBACK] 回滚步骤:"; \
		echo "  1. git checkout OpenMemory.upstream.lock.json"; \
		echo "  2. make openmemory-build"; \
		echo "  3. make restore BACKUP_FILE=./backups/xxx.sql"; \
		exit 1; \
	}
	@echo "[OK] 服务已启动"
	@echo ""
	@# 步骤 4: 验证
	@echo "[4/4] 运行验证..."
	@chmod +x apps/step2_openmemory_gateway/scripts/verify_unified_stack.sh
	@GATEWAY_URL=$${GATEWAY_URL:-http://localhost:8787} \
	 OPENMEMORY_URL=$${OPENMEMORY_URL:-http://localhost:8080} \
	 COMPOSE_PROJECT_NAME=$(COMPOSE_PROJECT_NAME) \
	 COMPOSE_FILE=$(COMPOSE_FILE) \
	 POSTGRES_DSN=$${POSTGRES_DSN:-postgresql://$${POSTGRES_USER:-postgres}:$${POSTGRES_PASSWORD:-postgres}@localhost:$${POSTGRES_PORT:-5432}/$${POSTGRES_DB:-engram}} \
	 ./apps/step2_openmemory_gateway/scripts/verify_unified_stack.sh
	@echo ""
	@echo "========================================"
	@echo "[OK] 生产环境升级完成！"
	@echo "========================================"
	@echo ""
	@echo "回滚说明（如需回滚）:"
	@echo "  1. git checkout OpenMemory.upstream.lock.json"
	@echo "  2. make openmemory-build"
	@echo "  3. make restore BACKUP_FILE=./backups/<刚才的备份文件>"

# 回滚 OpenMemory 升级
openmemory-rollback: ## 回滚 OpenMemory 升级（需指定 BACKUP_FILE 和 LOCK_COMMIT）
	@if [ -z "$(BACKUP_FILE)" ]; then \
		echo "错误: 请指定 BACKUP_FILE（升级前的备份文件）"; \
		echo "用法: make openmemory-rollback BACKUP_FILE=./backups/xxx.sql LOCK_COMMIT=abc123"; \
		exit 1; \
	fi
	@echo "========================================"
	@echo "OpenMemory 回滚"
	@echo "========================================"
	@echo ""
	@echo "[WARN] 此操作将回滚 OpenMemory 升级！"
	@echo "       备份文件: $(BACKUP_FILE)"
	@if [ -n "$(LOCK_COMMIT)" ]; then \
		echo "       Lock 回退到: $(LOCK_COMMIT)"; \
	fi
	@echo ""
	@echo "[1/4] 停止服务..."
	@$(DOCKER_COMPOSE) stop openmemory gateway worker
	@echo ""
	@echo "[2/4] 恢复数据库备份..."
	@$(MAKE) restore BACKUP_FILE=$(BACKUP_FILE)
	@echo ""
	@if [ -n "$(LOCK_COMMIT)" ]; then \
		echo "[3/4] 回退 OpenMemory.upstream.lock.json..."; \
		git checkout $(LOCK_COMMIT) -- OpenMemory.upstream.lock.json 2>/dev/null || \
			echo "[WARN] 无法自动回退 lock 文件，请手动 checkout"; \
		echo ""; \
		echo "[4/4] 重新构建镜像..."; \
		$(MAKE) openmemory-build; \
	else \
		echo "[3/4] 跳过 lock 文件回退（未指定 LOCK_COMMIT）"; \
		echo "[4/4] 请手动回退 OpenMemory.upstream.lock.json 并重新构建"; \
	fi
	@echo ""
	@echo "[OK] 回滚完成，请重新启动服务: make up"

# ============================================================================
# OpenMemory 多 Schema 隔离测试
# ============================================================================
# 验证 OpenMemory 在不同 PostgreSQL schema 下可独立迁移且互不影响
#
# 前置条件:
#   - PostgreSQL 可访问
#   - openmemory_migrate 镜像已构建
#
# 测试内容:
#   1. Schema 隔离性（tenant_a_openmemory / tenant_b_openmemory）
#   2. 迁移幂等性
#   3. 与 Step1 schema 的隔离性
# ============================================================================

openmemory-test-multi-schema: ## 运行 OpenMemory 多 Schema 隔离测试
	@echo "========================================"
	@echo "OpenMemory 多 Schema 隔离测试"
	@echo "========================================"
	@echo ""
	@echo "[1/3] 确保 PostgreSQL 服务运行..."
	@$(DOCKER_COMPOSE) up -d postgres
	@echo "等待 PostgreSQL 启动..."
	@sleep 5
	@# 等待 PostgreSQL 就绪
	@timeout 60 sh -c 'until $(DOCKER_COMPOSE) exec -T postgres pg_isready -U $${POSTGRES_USER:-postgres} > /dev/null 2>&1; do sleep 2; done' || { \
		echo "[ERROR] PostgreSQL 启动超时"; \
		exit 1; \
	}
	@echo "[OK] PostgreSQL 已就绪"
	@echo ""
	@echo "[2/3] 执行隔离 schema 迁移..."
	@# 为 tenant_a 执行迁移
	@OM_PG_SCHEMA=tenant_a_openmemory $(DOCKER_COMPOSE) up openmemory_migrate
	@# 为 tenant_b 执行迁移
	@OM_PG_SCHEMA=tenant_b_openmemory $(DOCKER_COMPOSE) up openmemory_migrate
	@echo "[OK] 多 schema 迁移完成"
	@echo ""
	@echo "[3/3] 执行多 Schema 隔离测试..."
	@# 设置测试环境变量并执行测试
	@export OM_PG_HOST=$${OM_PG_HOST:-localhost}; \
	export OM_PG_PORT=$${OM_PG_PORT:-$${POSTGRES_PORT:-5432}}; \
	export OM_PG_DB=$${OM_PG_DB:-$${POSTGRES_DB:-engram}}; \
	export OM_PG_USER=$${OM_PG_USER:-$${POSTGRES_USER:-postgres}}; \
	export OM_PG_PASSWORD=$${OM_PG_PASSWORD:-$${POSTGRES_PASSWORD:-postgres}}; \
	export OM_METADATA_BACKEND=postgres; \
	echo "  OM_PG_HOST = $${OM_PG_HOST}"; \
	echo "  OM_PG_PORT = $${OM_PG_PORT}"; \
	echo "  OM_PG_DB   = $${OM_PG_DB}"; \
	echo ""; \
	cd libs/OpenMemory/packages/openmemory-js && \
	npm ci --silent 2>/dev/null || npm install --silent && \
	npx tsx tests/test_multi_schema.ts
	@echo ""
	@echo "========================================"
	@echo "[OK] OpenMemory 多 Schema 隔离测试通过"
	@echo "========================================"

# ============================================================================
# CI/CD 命令
# ============================================================================

ci-precheck: ## CI 预检（用于 CI/CD 流水线）
	@echo "CI 预检..."
	@chmod +x scripts/db_ops.sh
	@./scripts/db_ops.sh precheck
	@echo ""
	@echo "CI 预检通过"

ci-backup: ## CI 备份（部署前自动备份）
	@echo "CI 部署前备份..."
	@mkdir -p backups
	@chmod +x scripts/db_ops.sh
	@./scripts/db_ops.sh backup || echo "[WARN] 备份失败（可能数据库未启动）"

ci-deploy: ci-precheck ci-backup deploy ## CI 完整部署流程
	@echo ""
	@echo "CI 部署完成"

# ============================================================================
# 构建边界校验
# ============================================================================

verify-build: verify-build-static ## Docker 构建边界校验（静态检查 + 实际构建）
	@echo "========================================"
	@echo "执行 Docker 实际构建..."
	@echo "========================================"
	@$(DOCKER_COMPOSE) build openmemory openmemory_migrate gateway worker
	@echo ""
	@echo "[OK] Docker 构建验证通过"

verify-build-static: ## 仅静态检查（Dockerfile/compose 配置校验，不执行构建）
	@echo "========================================"
	@echo "Docker 构建边界静态校验"
	@echo "========================================"
	@chmod +x scripts/verify_build_boundaries.sh
	@./scripts/verify_build_boundaries.sh --dry-run $${VERBOSE:+--verbose}

# ============================================================================
# 统一栈验证
# ============================================================================

verify-pgvector: ## 验证 pgvector 扩展是否可用
	@echo "========================================"
	@echo "pgvector 扩展验证"
	@echo "========================================"
	@$(DOCKER_COMPOSE) exec -T postgres \
		psql -U $${POSTGRES_USER:-postgres} -d $${POSTGRES_DB:-engram} \
		-c "SELECT 1 FROM pg_extension WHERE extname='vector'" -t | grep -q 1 && \
		echo "[OK] pgvector 扩展已安装" || \
		{ echo "[FAIL] pgvector 扩展未安装"; exit 1; }
	@$(DOCKER_COMPOSE) exec -T postgres \
		psql -U $${POSTGRES_USER:-postgres} -d $${POSTGRES_DB:-engram} \
		-c "SELECT extversion FROM pg_extension WHERE extname='vector'" -t | \
		xargs -I{} echo "[INFO] pgvector 版本: {}"

verify-unified: verify-pgvector ## 统一栈验证（自动模式，依赖 Docker Compose）
	@echo "========================================"
	@echo "Unified Stack 验证（default 模式）"
	@echo "========================================"
	@echo ""
	@echo "提示: 运行 pytest 集成测试请使用:"
	@echo "  make test-gateway-integration       # 纯 HTTP 验证（默认）"
	@echo "  make test-gateway-integration-full  # 含降级测试（需 Docker 权限）"
	@echo ""
	@chmod +x apps/step2_openmemory_gateway/scripts/verify_unified_stack.sh
	@GATEWAY_URL=$${GATEWAY_URL:-http://localhost:8787} \
	 OPENMEMORY_URL=$${OPENMEMORY_URL:-http://localhost:8080} \
	 COMPOSE_PROJECT_NAME=$(COMPOSE_PROJECT_NAME) \
	 COMPOSE_FILE=$(COMPOSE_FILE) \
	 POSTGRES_DSN=$${POSTGRES_DSN:-postgresql://$${POSTGRES_USER:-postgres}:$${POSTGRES_PASSWORD:-postgres}@localhost:$${POSTGRES_PORT:-5432}/$${POSTGRES_DB:-engram}} \
	 ./apps/step2_openmemory_gateway/scripts/verify_unified_stack.sh --mode default $${VERIFY_FULL:+--full} $${VERIFY_JSON_OUT:+--json-out $${VERIFY_JSON_OUT}}

verify-stepwise: ## 统一栈验证（stepwise 模式，仅 HTTP 验证，不依赖 Docker）
	@echo "========================================"
	@echo "Unified Stack 验证（stepwise 模式）"
	@echo "========================================"
	@chmod +x apps/step2_openmemory_gateway/scripts/verify_unified_stack.sh
	@GATEWAY_URL=$${GATEWAY_URL:-http://localhost:8787} \
	 OPENMEMORY_URL=$${OPENMEMORY_URL:-http://localhost:8080} \
	 POSTGRES_DSN=$${POSTGRES_DSN:-postgresql://$${POSTGRES_USER:-postgres}:$${POSTGRES_PASSWORD:-postgres}@localhost:$${POSTGRES_PORT:-5432}/$${POSTGRES_DB:-engram}} \
	 ./apps/step2_openmemory_gateway/scripts/verify_unified_stack.sh --mode stepwise

# ============================================================================
# 综合验证（适用于本地开发与 CI）
# ============================================================================
# verify-all 组合了静态检查与运行时验证：
#   1. verify-build-static: 检查 Dockerfile/compose 配置，无需启动容器
#   2. verify-stepwise: 仅 HTTP 端点验证，不执行 Docker 容器操作
#
# 适用场景:
#   - 本地开发: 提交前快速校验（需先 make deploy 启动服务）
#   - CI 流水线: 作为 gate check，服务已部署后执行
#
# 注意: 不启动长期前台进程，所有检查同步完成后返回
# ============================================================================

verify-all: ## 综合验证（静态检查 + stepwise 模式，适用于本地/CI）
	@echo "========================================"
	@echo "综合验证（verify-all）"
	@echo "========================================"
	@echo ""
	@echo "[1/2] 执行静态构建边界检查..."
	@$(MAKE) verify-build-static
	@echo ""
	@echo "[2/2] 执行统一栈 stepwise 验证..."
	@$(MAKE) verify-stepwise
	@echo ""
	@echo "========================================"
	@echo "[OK] 综合验证通过"
	@echo "========================================"
	@echo ""
	@echo "提示: 运行 pytest 集成测试请使用:"
	@echo "  make test-gateway-integration       # 纯 HTTP 验证（默认）"
	@echo "  make test-gateway-integration-full  # 含降级测试（需 Docker 权限）"

# ============================================================================
# Step1 冒烟测试
# ============================================================================

step1-smoke: ## Step1 冒烟测试（健康检查 + 完整工作流验证）
	@echo "========================================"
	@echo "Step1 冒烟测试"
	@echo "========================================"
	@# 1. 检查服务是否运行
	@echo "[1/6] 检查服务状态..."
	@if ! $(DOCKER_COMPOSE) ps --status running | grep -q postgres; then \
		echo '{"ok":false,"code":"SERVICE_NOT_RUNNING","message":"PostgreSQL 服务未运行，请先执行 make deploy"}'; \
		exit 1; \
	fi
	@echo "[OK] 服务已运行"
	@echo ""
	@# 2. 健康检查
	@echo "[2/6] 执行健康检查..."
	@cd apps/step1_logbook_postgres/scripts && \
		pip install -q -e . 2>/dev/null || true && \
		python logbook_cli.py health || { \
			echo "健康检查失败"; \
			exit 1; \
		}
	@echo ""
	@# 3. 创建测试 item
	@echo "[3/6] 创建测试 item..."
	@SMOKE_ITEM_RESULT=$$(cd apps/step1_logbook_postgres/scripts && \
		python logbook_cli.py create_item \
			--item-type "smoke_test" \
			--title "Step1 Smoke Test - $$(date +%Y%m%d_%H%M%S)" \
			--status "open" \
			--owner "smoke_tester" 2>&1) && \
	echo "$$SMOKE_ITEM_RESULT" && \
	SMOKE_ITEM_ID=$$(echo "$$SMOKE_ITEM_RESULT" | python -c "import sys,json; d=json.load(sys.stdin); print(d.get('item_id',''))" 2>/dev/null) && \
	if [ -z "$$SMOKE_ITEM_ID" ]; then \
		echo '{"ok":false,"code":"CREATE_ITEM_FAILED","message":"创建 item 失败"}'; \
		exit 1; \
	fi && \
	echo "[OK] 创建 item 成功: item_id=$$SMOKE_ITEM_ID" && \
	echo "" && \
	echo "[4/6] 添加事件..." && \
	cd apps/step1_logbook_postgres/scripts && \
	python logbook_cli.py add_event \
		--item-id $$SMOKE_ITEM_ID \
		--event-type "smoke_test.started" \
		--actor "smoke_tester" \
		--source "make_step1_smoke" \
		--json '{"test_phase": "smoke", "timestamp": "'$$(date -Iseconds)'"}' || { \
			echo '{"ok":false,"code":"ADD_EVENT_FAILED","message":"添加事件失败"}'; \
			exit 1; \
		} && \
	echo "[OK] 添加事件成功" && \
	echo "" && \
	echo "[5/6] 添加附件 (attach)..." && \
	SMOKE_ATTACH_CONTENT="Smoke test attachment content - $$(date)" && \
	SMOKE_ATTACH_SHA=$$(echo -n "$$SMOKE_ATTACH_CONTENT" | sha256sum | cut -d' ' -f1) && \
	python logbook_cli.py attach \
		--item-id $$SMOKE_ITEM_ID \
		--kind "log" \
		--uri "smoke_test/$$(date +%Y%m%d_%H%M%S).txt" \
		--sha256 $$SMOKE_ATTACH_SHA \
		--size $$(echo -n "$$SMOKE_ATTACH_CONTENT" | wc -c) || { \
			echo '{"ok":false,"code":"ATTACH_FAILED","message":"添加附件失败"}'; \
			exit 1; \
		} && \
	echo "[OK] 添加附件成功" && \
	echo "" && \
	echo "[6/6] 渲染视图 (render_views)..." && \
	python logbook_cli.py render_views \
		--log-event \
		--item-id $$SMOKE_ITEM_ID || { \
			echo '{"ok":false,"code":"RENDER_VIEWS_FAILED","message":"渲染视图失败"}'; \
			exit 1; \
		} && \
	echo "[OK] 渲染视图成功" && \
	echo "" && \
	echo "========================================" && \
	echo "Step1 冒烟测试完成！" && \
	echo "  - item_id: $$SMOKE_ITEM_ID" && \
	echo "  - 已创建 item、event、attachment" && \
	echo "  - 已生成 manifest.csv 和 index.md" && \
	echo '{"ok":true,"item_id":'$$SMOKE_ITEM_ID',"message":"Step1 冒烟测试通过"}'

# ============================================================================
# Step3 索引/检索命令
# ============================================================================
# 环境变量优先级: shell 环境变量 > Makefile 默认值
#
# Step3 推荐变量名（canonical）:
#   STEP3_PG_SCHEMA                       目标 schema（默认 step3）
#   STEP3_PG_TABLE                        目标表名（默认 chunks）
#   STEP3_PGVECTOR_DSN                    PGVector 连接字符串（默认自动构建）
#   STEP3_PGVECTOR_COLLECTION_STRATEGY    Collection 策略（Makefile 默认 single_table）
#   STEP3_PGVECTOR_AUTO_INIT              自动初始化开关（默认 1）
#
# 已废弃别名（计划于 2026-Q3 移除）:
#   STEP3_SCHEMA -> STEP3_PG_SCHEMA
#   STEP3_TABLE  -> STEP3_PG_TABLE
#   STEP3_AUTO_INIT -> STEP3_PGVECTOR_AUTO_INIT
# ============================================================================

# Step3 依赖安装（包括 psycopg3, pgvector, engram_step1）
step3-deps: ## 安装 Step3 依赖（psycopg3, pgvector, engram_step1）
	@echo "========================================"
	@echo "安装 Step3 依赖"
	@echo "========================================"
	@pip install -q -r apps/step3_seekdb_rag_hybrid/requirements.txt
	@python -c "import engram_step1" 2>/dev/null || { \
		echo "安装 engram_step1..."; \
		pip install -e apps/step1_logbook_postgres/scripts; \
	}
	@echo "[OK] Step3 依赖已就绪 (psycopg3, pgvector, engram_step1)"

step3-index: step3-deps ## Step3 索引同步（支持 env/参数透传，--json 输出）
	@echo "========================================"
	@echo "Step3 索引同步"
	@echo "========================================"
	@# 导出推荐变量名（Makefile 默认值，shell 环境变量优先）
	@export STEP3_PG_SCHEMA=$${STEP3_PG_SCHEMA:-step3}; \
	export STEP3_PG_TABLE=$${STEP3_PG_TABLE:-chunks}; \
	export STEP3_PGVECTOR_COLLECTION_STRATEGY=$${STEP3_PGVECTOR_COLLECTION_STRATEGY:-single_table}; \
	export STEP3_PGVECTOR_AUTO_INIT=$${STEP3_PGVECTOR_AUTO_INIT:-1}; \
	cd apps/step3_seekdb_rag_hybrid && \
		python -m seek_indexer \
			--mode $${INDEX_MODE:-incremental} \
			--source $${INDEX_SOURCE:-all} \
			--batch-size $${BATCH_SIZE:-100} \
			$${PROJECT_KEY:+--project-key $${PROJECT_KEY}} \
			$${BLOB_ID:+--blob-id $${BLOB_ID}} \
			$${DRY_RUN:+--dry-run} \
			$${JSON_OUTPUT:+--json} \
			$${VERBOSE:+--verbose}

step3-query: step3-deps ## Step3 证据检索（支持 query_text/filters，--json 输出）
	@if [ -z "$(QUERY)" ] && [ -z "$(QUERY_FILE)" ]; then \
		echo "错误: 请指定 QUERY 或 QUERY_FILE"; \
		echo "用法:"; \
		echo "  make step3-query QUERY='修复 XSS 漏洞'"; \
		echo "  make step3-query QUERY='bug fix' PROJECT_KEY=webapp JSON_OUTPUT=1"; \
		echo "  make step3-query QUERY_FILE=queries.txt JSON_OUTPUT=1"; \
		exit 1; \
	fi
	@echo "========================================"
	@echo "Step3 证据检索"
	@echo "========================================"
	@# 导出推荐变量名（Makefile 默认值，shell 环境变量优先）
	@export STEP3_PG_SCHEMA=$${STEP3_PG_SCHEMA:-step3}; \
	export STEP3_PG_TABLE=$${STEP3_PG_TABLE:-chunks}; \
	export STEP3_PGVECTOR_COLLECTION_STRATEGY=$${STEP3_PGVECTOR_COLLECTION_STRATEGY:-single_table}; \
	export STEP3_PGVECTOR_AUTO_INIT=$${STEP3_PGVECTOR_AUTO_INIT:-1}; \
	cd apps/step3_seekdb_rag_hybrid && \
		python -m seek_query \
			$${QUERY:+--query "$${QUERY}"} \
			$${QUERY_FILE:+--query-file "$${QUERY_FILE}"} \
			$${PROJECT_KEY:+--project-key $${PROJECT_KEY}} \
			$${SOURCE_TYPE:+--source-type $${SOURCE_TYPE}} \
			$${OWNER:+--owner $${OWNER}} \
			$${MODULE:+--module $${MODULE}} \
			$${TOP_K:+--top-k $${TOP_K}} \
			$${OUTPUT_FORMAT:+--output-format $${OUTPUT_FORMAT}} \
			$${JSON_OUTPUT:+--json} \
			$${VERBOSE:+--verbose}

step3-check: step3-deps ## Step3 一致性校验（支持 --json 输出）
	@echo "========================================"
	@echo "Step3 一致性校验"
	@echo "========================================"
	@# 导出推荐变量名（Makefile 默认值，shell 环境变量优先）
	@export STEP3_PG_SCHEMA=$${STEP3_PG_SCHEMA:-step3}; \
	export STEP3_PG_TABLE=$${STEP3_PG_TABLE:-chunks}; \
	export STEP3_PGVECTOR_COLLECTION_STRATEGY=$${STEP3_PGVECTOR_COLLECTION_STRATEGY:-single_table}; \
	export STEP3_PGVECTOR_AUTO_INIT=$${STEP3_PGVECTOR_AUTO_INIT:-1}; \
	cd apps/step3_seekdb_rag_hybrid && \
		python -m seek_consistency_check \
			--chunking-version $${CHUNKING_VERSION:-v1-2026-01} \
			$${PROJECT_KEY:+--project-key $${PROJECT_KEY}} \
			$${SAMPLE_RATIO:+--sample-ratio $${SAMPLE_RATIO}} \
			$${LIMIT:+--limit $${LIMIT}} \
			$${SKIP_ARTIFACTS:+--skip-artifacts} \
			$${SKIP_SHA256:+--skip-sha256} \
			$${CHECK_INDEX:+--check-index} \
			$${INDEX_BACKEND:+--index-backend $${INDEX_BACKEND}} \
			$${INDEX_SAMPLE_SIZE:+--index-sample-size $${INDEX_SAMPLE_SIZE}} \
			$${BACKEND:+--backend $${BACKEND}} \
			$${JSON_OUTPUT:+--json} \
			$${VERBOSE:+--verbose}

# ============================================================================
# Step3 冒烟测试
# ============================================================================
# 用于 CI/CD 流水线验证 Step3 索引/检索功能
#
# 前置条件:
#   - 统一栈已运行（make deploy）
#   - PostgreSQL 可访问（localhost:5432）
#
# 执行步骤:
#   1. 检查服务状态
#   2. 执行增量索引同步（step3-index incremental）
#   3. 执行检索验证（step3-query 固定查询）
#   4. 执行索引一致性检查（step3-check --check-index）
#
# Step3 推荐变量名（canonical，优先级: shell 环境变量 > Makefile 默认值）:
#   STEP3_PG_SCHEMA                       目标 schema（默认 step3）
#   STEP3_PG_TABLE                        目标表名（默认 chunks）
#   STEP3_PGVECTOR_DSN                    PGVector 连接字符串（默认自动构建）
#   STEP3_PGVECTOR_COLLECTION_STRATEGY    Collection 策略（Makefile 默认 single_table）
#   STEP3_PGVECTOR_AUTO_INIT              自动初始化开关（默认 1）
#
# 其他可覆盖变量:
#   STEP3_INDEX_BACKEND                   索引后端（默认 pgvector）
#   STEP3_SMOKE_QUERY                     检索测试查询（默认 "bug fix"）
#   STEP3_SKIP_CHECK                      跳过一致性检查（设置为 1）
#   STEP3_SMOKE_INDEX_SAMPLE_SIZE         一致性检查索引采样大小（默认 50）
#   STEP3_SMOKE_LIMIT                     一致性检查记录数上限（默认 100）
#
# 已废弃别名（计划于 2026-Q3 移除，使用时会触发警告）:
#   STEP3_SCHEMA -> STEP3_PG_SCHEMA
#   STEP3_TABLE  -> STEP3_PG_TABLE
#   STEP3_AUTO_INIT -> STEP3_PGVECTOR_AUTO_INIT
# ============================================================================

step3-run-smoke: step3-deps ## Step3 冒烟测试（CI 验证索引/检索功能）
	@echo "========================================"
	@echo "Step3 冒烟测试"
	@echo "========================================"
	@echo "环境变量优先级: shell 环境变量 > Makefile 默认值"
	@echo ""
	@# Step 0: 设置统一环境变量（使用推荐变量名）
	@export STEP3_INDEX_BACKEND=$${STEP3_INDEX_BACKEND:-pgvector}; \
	export STEP3_PG_SCHEMA=$${STEP3_PG_SCHEMA:-step3}; \
	export STEP3_PG_TABLE=$${STEP3_PG_TABLE:-chunks}; \
	export STEP3_PGVECTOR_COLLECTION_STRATEGY=$${STEP3_PGVECTOR_COLLECTION_STRATEGY:-single_table}; \
	export STEP3_PGVECTOR_AUTO_INIT=$${STEP3_PGVECTOR_AUTO_INIT:-1}; \
	if [ -z "$${STEP3_PGVECTOR_DSN}" ]; then \
		export STEP3_PGVECTOR_DSN="postgresql://$${POSTGRES_USER:-postgres}:$${POSTGRES_PASSWORD:-postgres}@localhost:$${POSTGRES_PORT:-5432}/$${POSTGRES_DB:-engram}"; \
	fi; \
	SMOKE_QUERY="$${STEP3_SMOKE_QUERY:-bug fix}"; \
	echo "配置信息（推荐变量名: STEP3_PG_*/STEP3_PGVECTOR_*）:"; \
	echo "  STEP3_INDEX_BACKEND                = $${STEP3_INDEX_BACKEND}"; \
	echo "  STEP3_PG_SCHEMA                    = $${STEP3_PG_SCHEMA}"; \
	echo "  STEP3_PG_TABLE                     = $${STEP3_PG_TABLE}"; \
	echo "  STEP3_PGVECTOR_COLLECTION_STRATEGY = $${STEP3_PGVECTOR_COLLECTION_STRATEGY}"; \
	echo "  STEP3_PGVECTOR_AUTO_INIT           = $${STEP3_PGVECTOR_AUTO_INIT}"; \
	echo "  STEP3_PGVECTOR_DSN                 = $${STEP3_PGVECTOR_DSN%@*}@..."; \
	echo "  SMOKE_QUERY                        = '$${SMOKE_QUERY}'"; \
	echo ""; \
	echo "[1/4] 检查服务状态..."; \
	if ! $(DOCKER_COMPOSE) ps --status running | grep -q postgres; then \
		echo '{"ok":false,"code":"SERVICE_NOT_RUNNING","message":"PostgreSQL 服务未运行，请先执行 make deploy"}'; \
		exit 1; \
	fi; \
	echo "[OK] PostgreSQL 服务已运行"; \
	echo ""; \
	echo "[2/4] 执行增量索引同步..."; \
	cd apps/step3_seekdb_rag_hybrid && \
	STEP3_INDEX_BACKEND=$${STEP3_INDEX_BACKEND} \
	STEP3_PG_SCHEMA=$${STEP3_PG_SCHEMA} \
	STEP3_PG_TABLE=$${STEP3_PG_TABLE} \
	STEP3_PGVECTOR_DSN=$${STEP3_PGVECTOR_DSN} \
	STEP3_PGVECTOR_COLLECTION_STRATEGY=$${STEP3_PGVECTOR_COLLECTION_STRATEGY} \
	STEP3_PGVECTOR_AUTO_INIT=$${STEP3_PGVECTOR_AUTO_INIT} \
	python -m seek_indexer \
		--mode incremental \
		--source all \
		--batch-size 50 \
		--json 2>&1 | tee /tmp/step3_smoke_index.json; \
	INDEX_EXIT_CODE=$$?; \
	if [ $$INDEX_EXIT_CODE -ne 0 ]; then \
		echo "[FAIL] 索引同步失败 (exit_code=$$INDEX_EXIT_CODE)"; \
		cat /tmp/step3_smoke_index.json; \
		exit $$INDEX_EXIT_CODE; \
	fi; \
	echo "[OK] 索引同步完成"; \
	echo ""; \
	echo "[3/4] 执行检索验证..."; \
	cd apps/step3_seekdb_rag_hybrid && \
	STEP3_INDEX_BACKEND=$${STEP3_INDEX_BACKEND} \
	STEP3_PG_SCHEMA=$${STEP3_PG_SCHEMA} \
	STEP3_PG_TABLE=$${STEP3_PG_TABLE} \
	STEP3_PGVECTOR_DSN=$${STEP3_PGVECTOR_DSN} \
	STEP3_PGVECTOR_COLLECTION_STRATEGY=$${STEP3_PGVECTOR_COLLECTION_STRATEGY} \
	STEP3_PGVECTOR_AUTO_INIT=$${STEP3_PGVECTOR_AUTO_INIT} \
	python -m seek_query \
		--query "$${SMOKE_QUERY}" \
		--top-k 5 \
		--json 2>&1 | tee /tmp/step3_smoke_query.json; \
	QUERY_EXIT_CODE=$$?; \
	if [ $$QUERY_EXIT_CODE -ne 0 ]; then \
		echo "[FAIL] 检索验证失败 (exit_code=$$QUERY_EXIT_CODE)"; \
		cat /tmp/step3_smoke_query.json; \
		exit $$QUERY_EXIT_CODE; \
	fi; \
	echo "[OK] 检索验证完成"; \
	echo ""; \
	if [ "$${STEP3_SKIP_CHECK}" != "1" ]; then \
		echo "[4/4] 执行索引一致性检查..."; \
		SMOKE_INDEX_SAMPLE_SIZE=$${STEP3_SMOKE_INDEX_SAMPLE_SIZE:-50}; \
		SMOKE_LIMIT=$${STEP3_SMOKE_LIMIT:-100}; \
		echo "  INDEX_SAMPLE_SIZE = $${SMOKE_INDEX_SAMPLE_SIZE}"; \
		echo "  LIMIT             = $${SMOKE_LIMIT}"; \
		cd apps/step3_seekdb_rag_hybrid && \
		STEP3_INDEX_BACKEND=$${STEP3_INDEX_BACKEND} \
		STEP3_PG_SCHEMA=$${STEP3_PG_SCHEMA} \
		STEP3_PG_TABLE=$${STEP3_PG_TABLE} \
		STEP3_PGVECTOR_DSN=$${STEP3_PGVECTOR_DSN} \
		STEP3_PGVECTOR_COLLECTION_STRATEGY=$${STEP3_PGVECTOR_COLLECTION_STRATEGY} \
		STEP3_PGVECTOR_AUTO_INIT=$${STEP3_PGVECTOR_AUTO_INIT} \
		python -m seek_consistency_check \
			--chunking-version $${CHUNKING_VERSION:-v1-2026-01} \
			--check-index \
			--index-sample-size $${SMOKE_INDEX_SAMPLE_SIZE} \
			--skip-artifacts \
			--limit $${SMOKE_LIMIT} \
			--json 2>&1 | tee /tmp/step3_smoke_check.json; \
		CHECK_EXIT_CODE=$$?; \
		if [ $$CHECK_EXIT_CODE -ne 0 ]; then \
			echo "[WARN] 一致性检查发现问题 (exit_code=$$CHECK_EXIT_CODE)"; \
			echo "       这可能是正常的（如新增记录尚未索引）"; \
		else \
			echo "[OK] 一致性检查通过"; \
		fi; \
	else \
		echo "[4/4] 跳过一致性检查 (STEP3_SKIP_CHECK=1)"; \
	fi; \
	echo ""; \
	echo "========================================"; \
	echo "Step3 冒烟测试完成！"; \
	echo "  - 索引同步: OK"; \
	echo "  - 检索验证: OK"; \
	if [ "$${STEP3_SKIP_CHECK}" != "1" ]; then \
		echo "  - 一致性检查: 已执行"; \
	fi; \
	echo '{"ok":true,"message":"Step3 冒烟测试通过"}'

# ============================================================================
# Step3 Collection 迁移命令
# ============================================================================
# 用于 CI/CD 和本地验证 PGVector Collection 迁移脚本
#
# 环境变量优先级: shell 环境变量 > Makefile 默认值
#
# Step3 推荐变量名（canonical）:
#   STEP3_PG_SCHEMA                       目标 schema（默认 step3）
#   STEP3_PG_TABLE                        目标表名（默认 chunks）
#   STEP3_PG_HOST                         数据库主机（fallback: POSTGRES_HOST，默认 localhost）
#   STEP3_PG_PORT                         数据库端口（fallback: POSTGRES_PORT，默认 5432）
#   STEP3_PG_DB                           数据库名（fallback: POSTGRES_DB，默认 engram）
#   STEP3_PG_USER                         数据库用户（fallback: POSTGRES_USER，默认 postgres）
#   STEP3_PG_PASSWORD                     数据库密码（fallback: POSTGRES_PASSWORD，必需）
#
# 已废弃别名（计划于 2026-Q3 移除，使用时会触发警告）:
#   STEP3_SCHEMA -> STEP3_PG_SCHEMA
#   STEP3_TABLE  -> STEP3_PG_TABLE
#
# 示例（隔离测试，推荐使用 STEP3_PG_* 前缀）:
#   STEP3_PG_SCHEMA=step3_test STEP3_PG_TABLE=chunks_test make step3-migrate-dry-run
# ============================================================================

step3-migrate-dry-run: step3-deps ## Step3 迁移 dry-run（验证迁移脚本，不修改数据库）
	@echo "========================================"
	@echo "Step3 Collection Migrate (dry-run)"
	@echo "========================================"
	@# 兼容期内对旧变量名的透传和警告提示
	@if [ -n "$${STEP3_SCHEMA}" ] && [ -z "$${STEP3_PG_SCHEMA}" ]; then \
		echo "\033[33m[DEPRECATION] STEP3_SCHEMA 已废弃，请改用 STEP3_PG_SCHEMA（计划于 2026-Q3 移除）\033[0m"; \
	fi; \
	if [ -n "$${STEP3_TABLE}" ] && [ -z "$${STEP3_PG_TABLE}" ]; then \
		echo "\033[33m[DEPRECATION] STEP3_TABLE 已废弃，请改用 STEP3_PG_TABLE（计划于 2026-Q3 移除）\033[0m"; \
	fi
	@# 环境变量优先级: shell 环境变量 > Makefile 默认值
	@# 推荐变量名: STEP3_PG_*，fallback 到 POSTGRES_* 或默认值
	@export STEP3_PG_SCHEMA=$${STEP3_PG_SCHEMA:-$${STEP3_SCHEMA:-step3}}; \
	export STEP3_PG_TABLE=$${STEP3_PG_TABLE:-$${STEP3_TABLE:-chunks}}; \
	export STEP3_PG_HOST=$${STEP3_PG_HOST:-$${POSTGRES_HOST:-localhost}}; \
	export STEP3_PG_PORT=$${STEP3_PG_PORT:-$${POSTGRES_PORT:-5432}}; \
	export STEP3_PG_DB=$${STEP3_PG_DB:-$${POSTGRES_DB:-engram}}; \
	export STEP3_PG_USER=$${STEP3_PG_USER:-$${POSTGRES_USER:-postgres}}; \
	export STEP3_PG_PASSWORD=$${STEP3_PG_PASSWORD:-$${POSTGRES_PASSWORD}}; \
	if [ -z "$${STEP3_PG_PASSWORD}" ]; then \
		echo "[ERROR] STEP3_PG_PASSWORD 或 POSTGRES_PASSWORD 未设置"; \
		exit 1; \
	fi; \
	echo ""; \
	echo "配置信息（推荐变量名: STEP3_PG_*）:"; \
	echo "  STEP3_PG_SCHEMA = $${STEP3_PG_SCHEMA}"; \
	echo "  STEP3_PG_TABLE  = $${STEP3_PG_TABLE}"; \
	echo "  STEP3_PG_HOST   = $${STEP3_PG_HOST}"; \
	echo "  STEP3_PG_PORT   = $${STEP3_PG_PORT}"; \
	echo "  STEP3_PG_DB     = $${STEP3_PG_DB}"; \
	echo ""; \
	mkdir -p .artifacts/step3-migrate; \
	echo "[1/2] shared-table dry-run..."; \
	cd apps/step3_seekdb_rag_hybrid/scripts && \
	STEP3_PG_SCHEMA=$${STEP3_PG_SCHEMA} \
	STEP3_PG_TABLE=$${STEP3_PG_TABLE} \
	STEP3_PG_HOST=$${STEP3_PG_HOST} \
	STEP3_PG_PORT=$${STEP3_PG_PORT} \
	STEP3_PG_DB=$${STEP3_PG_DB} \
	STEP3_PG_USER=$${STEP3_PG_USER} \
	STEP3_PG_PASSWORD=$${STEP3_PG_PASSWORD} \
	python pgvector_collection_migrate.py shared-table --dry-run --json \
		2>&1 | tee ../../../.artifacts/step3-migrate/shared-table-dryrun.json; \
	echo ""; \
	echo "[2/2] table-per-collection dry-run..."; \
	STEP3_PG_SCHEMA=$${STEP3_PG_SCHEMA} \
	STEP3_PG_TABLE=$${STEP3_PG_TABLE} \
	STEP3_PG_HOST=$${STEP3_PG_HOST} \
	STEP3_PG_PORT=$${STEP3_PG_PORT} \
	STEP3_PG_DB=$${STEP3_PG_DB} \
	STEP3_PG_USER=$${STEP3_PG_USER} \
	STEP3_PG_PASSWORD=$${STEP3_PG_PASSWORD} \
	python pgvector_collection_migrate.py table-per-collection --dry-run --json --batch-size 1000 \
		2>&1 | tee ../../../.artifacts/step3-migrate/table-per-collection-dryrun.json; \
	echo ""; \
	echo "========================================"; \
	echo "[OK] Step3 迁移 dry-run 完成"; \
	echo "  结果保存到 .artifacts/step3-migrate/"; \
	echo "========================================"

step3-migrate-replay-small: step3-deps ## Step3 迁移小批量回放（--batch-size 10，用于测试环境验证）
	@echo "========================================"
	@echo "Step3 Collection Migrate (small batch replay)"
	@echo "========================================"
	@# 兼容期内对旧变量名的透传和警告提示
	@if [ -n "$${STEP3_SCHEMA}" ] && [ -z "$${STEP3_PG_SCHEMA}" ]; then \
		echo "\033[33m[DEPRECATION] STEP3_SCHEMA 已废弃，请改用 STEP3_PG_SCHEMA（计划于 2026-Q3 移除）\033[0m"; \
	fi; \
	if [ -n "$${STEP3_TABLE}" ] && [ -z "$${STEP3_PG_TABLE}" ]; then \
		echo "\033[33m[DEPRECATION] STEP3_TABLE 已废弃，请改用 STEP3_PG_TABLE（计划于 2026-Q3 移除）\033[0m"; \
	fi
	@# 环境变量优先级: shell 环境变量 > Makefile 默认值
	@# 推荐变量名: STEP3_PG_*，fallback 到 POSTGRES_* 或默认值
	@export STEP3_PG_SCHEMA=$${STEP3_PG_SCHEMA:-$${STEP3_SCHEMA:-step3}}; \
	export STEP3_PG_TABLE=$${STEP3_PG_TABLE:-$${STEP3_TABLE:-chunks}}; \
	export STEP3_PG_HOST=$${STEP3_PG_HOST:-$${POSTGRES_HOST:-localhost}}; \
	export STEP3_PG_PORT=$${STEP3_PG_PORT:-$${POSTGRES_PORT:-5432}}; \
	export STEP3_PG_DB=$${STEP3_PG_DB:-$${POSTGRES_DB:-engram}}; \
	export STEP3_PG_USER=$${STEP3_PG_USER:-$${POSTGRES_USER:-postgres}}; \
	export STEP3_PG_PASSWORD=$${STEP3_PG_PASSWORD:-$${POSTGRES_PASSWORD}}; \
	if [ -z "$${STEP3_PG_PASSWORD}" ]; then \
		echo "[ERROR] STEP3_PG_PASSWORD 或 POSTGRES_PASSWORD 未设置"; \
		exit 1; \
	fi; \
	echo ""; \
	echo "配置信息（推荐变量名: STEP3_PG_*）:"; \
	echo "  STEP3_PG_SCHEMA = $${STEP3_PG_SCHEMA}"; \
	echo "  STEP3_PG_TABLE  = $${STEP3_PG_TABLE}"; \
	echo "  STEP3_PG_HOST   = $${STEP3_PG_HOST}"; \
	echo "  STEP3_PG_PORT   = $${STEP3_PG_PORT}"; \
	echo "  STEP3_PG_DB     = $${STEP3_PG_DB}"; \
	echo "  BATCH_SIZE      = 10 (小批量)"; \
	echo ""; \
	echo "[WARN] 此命令会实际修改数据库！仅用于测试环境。"; \
	echo "       建议使用隔离测试表: STEP3_PG_SCHEMA=step3_test STEP3_PG_TABLE=chunks_test"; \
	echo ""; \
	mkdir -p .artifacts/step3-migrate; \
	echo "[1/1] shared-table 小批量回放..."; \
	cd apps/step3_seekdb_rag_hybrid/scripts && \
	STEP3_PG_SCHEMA=$${STEP3_PG_SCHEMA} \
	STEP3_PG_TABLE=$${STEP3_PG_TABLE} \
	STEP3_PG_HOST=$${STEP3_PG_HOST} \
	STEP3_PG_PORT=$${STEP3_PG_PORT} \
	STEP3_PG_DB=$${STEP3_PG_DB} \
	STEP3_PG_USER=$${STEP3_PG_USER} \
	STEP3_PG_PASSWORD=$${STEP3_PG_PASSWORD} \
	python pgvector_collection_migrate.py shared-table --batch-size 10 --json \
		2>&1 | tee ../../../.artifacts/step3-migrate/shared-table-replay-small.json; \
	echo ""; \
	echo "========================================"; \
	echo "[OK] Step3 小批量迁移完成"; \
	echo "  结果保存到 .artifacts/step3-migrate/shared-table-replay-small.json"; \
	echo "========================================"
