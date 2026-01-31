"""
memory_query handler - memory_query 工具核心实现

提供 memory_query_impl 函数，处理：
1. 查询 OpenMemory
2. OpenMemory 失败时降级到 Logbook 回退查询

================================================================================
                       依赖注入与迁移指引
================================================================================

推荐的依赖获取方式（优先级从高到低）:

1. 通过 deps 参数传入 GatewayDeps (推荐):
   ```python
   from engram.gateway.di import GatewayDeps

   deps = GatewayDeps.create()  # 或 GatewayDeps.for_testing(...)
   result = await memory_query_impl(
       query="...",
       correlation_id="...",
       deps=deps,
   )
   ```

2. 通过 _config, _openmemory_client 参数 (向后兼容):
   ```python
   result = await memory_query_impl(
       query="...",
       correlation_id="...",
       _config=my_config,
       _openmemory_client=my_client,
   )
   ```

3. 使用模块级全局函数 (已弃用):
   不传入任何依赖参数时，使用 get_config() / get_client() 等全局函数

================================================================================
                       correlation_id 单一来源原则
================================================================================

correlation_id 是必需参数，必须由调用方（HTTP 入口层）生成后传入。
handler 不再自行生成 correlation_id，确保同一请求使用同一 ID。

响应中的 correlation_id 必须与请求保持一致。
"""

import logging
import warnings
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from pydantic import BaseModel

from ..config import GatewayConfig, get_config
from ..di import GatewayDeps, GatewayDepsProtocol
from ..openmemory_client import (
    OpenMemoryError,
    get_client,
)

if TYPE_CHECKING:
    from ..openmemory_client import OpenMemoryClient

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
    correlation_id: Optional[str] = None,
    # 依赖注入参数（推荐方式）
    deps: Optional[GatewayDepsProtocol] = None,
    # [DEPRECATED] 以下参数将在后续版本移除，请使用 deps 参数
    # 迁移计划：这些参数仅为向后兼容保留，新代码应使用 deps=GatewayDeps.create() 或 deps=GatewayDeps.for_testing(...)
    _config: Optional[GatewayConfig] = None,
    _openmemory_client: Optional["OpenMemoryClient"] = None,
) -> MemoryQueryResponse:
    """
    memory_query 核心实现

    当 OpenMemory 查询失败时，会降级到 Logbook 的 knowledge_candidates 表进行回退查询。

    Args:
        correlation_id: 追踪 ID（必需）。必须由 HTTP 入口层生成后传入，
                        确保同一请求使用同一 ID。handler 不再自行生成。
        deps: 可选的 GatewayDeps 依赖容器，优先使用其中的依赖
        _config: 可选的 GatewayConfig 对象，不传则使用 get_config()
        _openmemory_client: 可选的 OpenMemoryClient 对象，不传则使用 get_client()

    Raises:
        ValueError: 如果 correlation_id 未提供
    """
    # correlation_id 必须由调用方提供（单一来源原则）
    if correlation_id is None:
        raise ValueError(
            "correlation_id 是必需参数：必须由 HTTP 入口层生成后传入，"
            "handler 不再自行生成 correlation_id"
        )

    # [DEPRECATED] 弃用警告：_config/_openmemory_client 参数将在后续版本移除
    if _config is not None or _openmemory_client is not None:
        warnings.warn(
            "memory_query_impl 的 _config/_openmemory_client 参数已弃用，"
            "请使用 deps=GatewayDeps.create() 或 deps=GatewayDeps.for_testing(...) 替代。"
            "这些参数将在后续版本移除。",
            DeprecationWarning,
            stacklevel=2,
        )

    # 获取配置（支持依赖注入）：deps 优先 > _config 参数 > 全局 getter
    if deps is not None:
        config = deps.config
    elif _config is not None:
        config = _config
    else:
        config = get_config()

    # 确保有可用的 deps 对象（用于 fallback 查询时获取 logbook_adapter）
    if deps is None:
        deps = GatewayDeps.create(config=config)
        # [LEGACY] 兼容分支：如果有显式传入的 _openmemory_client，注入到 deps 中
        if _openmemory_client is not None:
            deps._openmemory_client = _openmemory_client

    # 默认搜索空间
    if not spaces:
        spaces = [config.default_team_space]

    try:
        # 获取 OpenMemory client（支持依赖注入）：deps 优先 > _openmemory_client 参数 > 全局 getter
        # 注意：不使用不带 config 的 get_client()，确保 base_url/api_key 来自 config
        if deps is not None:
            client = deps.openmemory_client
        elif _openmemory_client is not None:
            client = _openmemory_client
        else:
            client = get_client(config)
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

        return MemoryQueryResponse(
            ok=True,
            results=result.results,
            total=len(result.results),
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
