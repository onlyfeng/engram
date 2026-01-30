"""
engram.gateway - MCP Gateway 模块

提供 MCP Server 入口，连接 Cursor IDE 与 OpenMemory，负责：
- 治理开关和策略校验 (team_write_enabled + policy)
- 写入审计 (governance.write_audit)
- 失败降级 (logbook.outbox_memory)
- OpenMemory API 调用

子模块：
- logbook_adapter: Logbook 适配器
- openmemory_client: OpenMemory HTTP API 客户端
- outbox_worker: Outbox 队列处理 Worker
- mcp_rpc: MCP JSON-RPC 2.0 协议实现
- policy: 策略决策引擎
"""

__version__ = "0.1.0"

from . import logbook_adapter
from . import openmemory_client
from . import outbox_worker

# 保留 logbook_db 向后兼容导入（会触发弃用警告）
# 新代码请使用 logbook_adapter 模块
