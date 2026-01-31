"""
memory_query handler - memory_query 工具核心实现

提供 memory_query_impl 函数，处理：
1. 查询 OpenMemory
2. OpenMemory 失败时降级到 Logbook 回退查询

================================================================================
                       依赖注入 (v1.0)
================================================================================

依赖获取方式:

通过 deps 参数传入 GatewayDeps (必需):
   ```python
   from engram.gateway.di import GatewayDeps

   deps = GatewayDeps.create()  # 或 GatewayDeps.for_testing(...)
   result = await memory_query_impl(
       query="...",
       correlation_id="...",
       deps=deps,
   )
   ```

v1.0 变更：
   - deps 参数为必需参数
   - 已移除 _config/_openmemory_client 参数
   - 已移除 get_config()/get_client() 全局 fallback

================================================================================
                       correlation_id 单一来源原则
================================================================================

correlation_id 是必需参数，必须由调用方（HTTP 入口层）生成后传入。
handler 不再自行生成 correlation_id，确保同一请求使用同一 ID。

响应中的 correlation_id 必须与请求保持一致。
"""

import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from ..di import GatewayDepsProtocol
from ..openmemory_client import OpenMemoryError

logger = logging.getLogger("gateway.handlers.memory_query")


class MemoryQueryResponse(BaseModel):
    """memory_query 响应模型"""

    ok: bool
    results: List[Dict[str, Any]]
    total: int
    spaces_searched: List[str]
    message: Optional[str] = None
    degraded: bool = False  # 降级标记：True 表示结果来自 Logbook 回退查询
    correlation_id: Optional[str] = None  # 追踪 ID


async def memory_query_impl(
    query: str,
    spaces: Optional[List[str]] = None,
    filters: Optional[Dict[str, Any]] = None,
    top_k: int = 10,
    *,
    correlation_id: str,
    deps: GatewayDepsProtocol,
) -> MemoryQueryResponse:
    """
    memory_query 核心实现

    当 OpenMemory 查询失败时，会降级到 Logbook 的 knowledge_candidates 表进行回退查询。

    Args:
        query: 查询字符串
        spaces: 搜索空间列表，不传则使用默认空间
        filters: 过滤条件
        top_k: 返回结果数量限制
        correlation_id: 追踪 ID（必需）。必须由 HTTP 入口层生成后传入，
                        确保同一请求使用同一 ID。handler 不再自行生成。
        deps: GatewayDeps 依赖容器（必需），通过此对象获取所有依赖

    Raises:
        ValueError: 如果 correlation_id 未提供
    """
    # correlation_id 必须由调用方提供（单一来源原则）
    if correlation_id is None:
        raise ValueError(
            "correlation_id 是必需参数：必须由 HTTP 入口层生成后传入，"
            "handler 不再自行生成 correlation_id"
        )

    # 通过 deps 获取配置
    config = deps.config

    # 默认搜索空间
    if not spaces:
        default_space = config.default_team_space
        if default_space is None:
            raise ValueError("config.default_team_space is None, cannot proceed")
        spaces = [default_space]

    try:
        # 通过 deps 获取 OpenMemory client
        client = deps.openmemory_client

        # 使用 openmemory_client 的 search 方法
        combined_filters = filters.copy() if filters else {}
        combined_filters["spaces"] = spaces

        result = client.search(
            query=query,
            limit=top_k,
            filters=combined_filters,
        )

        if not result.success:
            return MemoryQueryResponse(
                ok=False,
                results=[],
                total=0,
                spaces_searched=spaces,
                message=f"查询失败: {result.error}",
                correlation_id=correlation_id,
            )

        # 确保 results 不为 None（SearchResult.__post_init__ 会初始化为空列表）
        results = result.results or []
        return MemoryQueryResponse(
            ok=True,
            results=results,
            total=len(results),
            spaces_searched=spaces,
            message=None,
            correlation_id=correlation_id,
        )

    except OpenMemoryError as e:
        # OpenMemory 查询失败，降级到 Logbook 回退查询
        logger.warning(
            f"OpenMemory 查询失败，降级到 Logbook 回退查询: correlation_id={correlation_id}, error={e.message}"
        )

        try:
            # 从 filters 中提取 evidence_filter 和 space_filter
            evidence_filter = None
            space_filter = None
            if filters:
                evidence_filter = filters.get("evidence")
                if spaces:
                    space_filter = spaces[0]

            # 调用 Logbook 回退查询（通过 deps.logbook_adapter 获取，确保使用统一实例）
            candidates = deps.logbook_adapter.query_knowledge_candidates(
                keyword=query,
                top_k=top_k,
                evidence_filter=evidence_filter,
                space_filter=space_filter,
            )

            # 将 knowledge_candidates 结果转换为统一的结果格式
            results = []
            for candidate in candidates:
                results.append(
                    {
                        "id": f"kc_{candidate['candidate_id']}",
                        "content": candidate["content_md"],
                        "title": candidate["title"],
                        "kind": candidate["kind"],
                        "confidence": candidate["confidence"],
                        "evidence_refs": candidate.get("evidence_refs_json"),
                        "created_at": str(candidate["created_at"])
                        if candidate.get("created_at")
                        else None,
                        "source": "logbook_fallback",
                    }
                )

            return MemoryQueryResponse(
                ok=True,
                results=results,
                total=len(results),
                spaces_searched=spaces or [],
                message=f"降级查询（OpenMemory 不可用）: {e.message}",
                degraded=True,
                correlation_id=correlation_id,
            )

        except Exception as fallback_error:
            logger.exception(
                f"Logbook 回退查询也失败: correlation_id={correlation_id}, error={fallback_error}"
            )
            return MemoryQueryResponse(
                ok=False,
                results=[],
                total=0,
                spaces_searched=spaces or [],
                message=f"查询失败（OpenMemory: {e.message}, 回退: {str(fallback_error)}）",
                degraded=True,
                correlation_id=correlation_id,
            )

    except Exception as e:
        logger.exception(f"memory_query 未预期错误: correlation_id={correlation_id}, error={e}")
        return MemoryQueryResponse(
            ok=False,
            results=[],
            total=0,
            spaces_searched=spaces or [],
            message=f"内部错误: {str(e)}",
            correlation_id=correlation_id,
        )
