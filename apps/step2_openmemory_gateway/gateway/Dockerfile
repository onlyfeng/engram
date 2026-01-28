# syntax=docker/dockerfile:1.4
# Memory Gateway Dockerfile
# 桥接 Step1 Logbook 与 OpenMemory
#
# 构建说明：
#   需要将构建上下文设为项目根目录，以便访问 Step1 脚本包
#   docker build -f step2_openmemory_gateway/gateway/Dockerfile -t memory-gateway .
#
# 或者通过 compose 配置：
#   build:
#     context: ../..  # 项目根目录
#     dockerfile: step2_openmemory_gateway/gateway/Dockerfile
#
# 运行说明：
#   默认启动 Gateway API: docker run -p 8787:8787 memory-gateway
#   启动 Outbox Worker: docker run memory-gateway python -m gateway.outbox_worker --loop
#
# BuildKit 优化：
#   DOCKER_BUILDKIT=1 docker build ...
#
# ---------- 构建策略 ----------
# 
# Step1 包安装策略: pip wheel + pip install (方案A)
#   - 生产镜像: 从最小源码子集构建 wheel，然后安装 (非 editable)
#   - 本地开发/CI: 使用 editable 安装 (pip install -e)，不进入生产镜像
#
# Gateway 依赖的 engram_step1 模块边界:
#   - engram_step1.config    (Config, get_config)
#   - engram_step1.db        (get_connection, query_knowledge_candidates)
#   - engram_step1.errors    (ErrorCode, DatabaseError)
#   - engram_step1.governance
#   - engram_step1.outbox
#   - engram_step1.migrate   (run_all_checks, run_migrate)
#
# 注意: 顶层 db.py 模块仅供 scm_sync_* 脚本使用，Gateway 不需要

FROM python:3.12-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    && rm -rf /var/lib/apt/lists/*

# ========== Layer 1: 第三方依赖（变化最少，缓存最稳定）==========
# 复制运行时依赖清单（纯第三方 PyPI 包）
COPY apps/step2_openmemory_gateway/gateway/requirements.runtime.txt /app/requirements.runtime.txt

# 安装第三方依赖（启用 BuildKit cache mount 加速冷启动）
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /app/requirements.runtime.txt

# ========== Layer 2: Step1 本地包安装 ==========
# 复制 Step1 scripts 目录（包含 engram_step1 包和相关脚本）
COPY apps/step1_logbook_postgres/scripts /step1_scripts

# 构建并安装 Step1 wheel（非 editable 模式，适合生产环境）
RUN --mount=type=cache,target=/root/.cache/pip \
    pip wheel --no-deps --wheel-dir=/tmp/wheels /step1_scripts \
    && pip install /tmp/wheels/engram_step1-*.whl \
    && rm -rf /tmp/wheels

# ========== Layer 3: Gateway 应用代码（变化最频繁）==========
# 复制 Gateway 包和配置文件，通过 pip install 安装
COPY apps/step2_openmemory_gateway/gateway/pyproject.toml /app/
COPY apps/step2_openmemory_gateway/gateway/README.md /app/
COPY apps/step2_openmemory_gateway/gateway/gateway/ /app/gateway/
COPY apps/step2_openmemory_gateway/gateway/tests/ /app/tests/

# 安装 Gateway 包（使用 pyproject.toml 声明的依赖）
# 依赖已在 Layer 1 安装，此处仅安装包本身（--no-deps 避免重复安装依赖）
RUN pip install --no-deps /app

# ========== 环境配置 ==========
# Gateway 已通过 pip install 安装到 site-packages
# Step1 包已通过 wheel 安装到 site-packages
# 无需额外 PYTHONPATH 配置

# 暴露端口
EXPOSE 8787

# 健康检查（仅对 Gateway API 有效）
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:8787/health || exit 1

# ========== 入口配置 ==========
# 默认启动 Gateway API 服务
# 可通过 compose 的 command 覆盖：
#   - API 服务: python -m uvicorn gateway.main:app --host 0.0.0.0 --port 8787
#   - Outbox Worker: python -m gateway.outbox_worker --loop
#   - Worker 单次执行: python -m gateway.outbox_worker --once
CMD ["python", "-m", "uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8787"]
