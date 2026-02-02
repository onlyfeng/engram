FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN python -m pip install --upgrade pip

# 安装依赖与源码（保持 sql/ 在项目根目录，便于迁移脚本定位）
COPY pyproject.toml requirements.txt README.md ./
COPY src ./src
COPY sql ./sql
COPY logbook_postgres ./logbook_postgres
COPY engram_logbook ./engram_logbook
COPY scripts ./scripts

RUN pip install -e ".[full]"
