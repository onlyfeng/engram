"""
Memory Gateway 模块

提供 MCP Server 入口，负责：
- 治理开关和策略校验 (team_write_enabled + policy)
- 写入审计 (governance.write_audit)
- 失败降级 (logbook.outbox_memory)
- OpenMemory API 调用

子模块：
- step1_adapter: Step1 engram_step1 包适配器（推荐使用）
- step1_db: Postgres 数据库操作（已弃用，转发到 step1_adapter）
- openmemory_client: OpenMemory HTTP API 客户端
- outbox_worker: Outbox 队列处理 Worker
"""

__version__ = "0.1.0"

from . import step1_adapter
from . import openmemory_client
from . import outbox_worker

# 保留 step1_db 向后兼容导入（会触发弃用警告）
# 新代码请使用 step1_adapter 模块
