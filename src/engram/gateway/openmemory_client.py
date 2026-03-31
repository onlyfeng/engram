"""
OpenMemory HTTP API 客户端
负责：调用 OpenMemory 后端进行 store/query/reinforce 操作

字段映射：
- content = payload_md
- user_id = actor_user_id（或空）
- metadata 包含 target_space/kind/module/evidence_refs/payload_sha

重试策略：仅网络错误和 5xx 错误时重试

线程安全与可测试性说明 (ADR: Gateway DI 与入口边界统一):
============================================================

1. 线程安全性 (Thread Safety):
   - OpenMemoryClient 实例: 线程安全（httpx.Client 是线程安全的）
   - _default_client 全局单例: 使用模块级变量，依赖 Python GIL 基本安全
   - 高并发场景下首次初始化可能出现竞态，但结果一致（幂等）

2. 可重入性 (Reentrancy):
   - get_client(): 幂等操作，多次调用返回同一实例
   - reset_client(): 非幂等，会清除全局单例
   - 建议在应用启动时预热，避免运行时竞态

3. 可测试替换 (Test Override):
   - 方式一: 使用 override_client(mock_client) 临时替换全局单例
   - 方式二: 测试完成后调用 reset_client() 恢复默认行为
   - 方式三: 直接构造 OpenMemoryClient 实例传入 GatewayDeps.for_testing()

构造参数来源说明:
   - base_url: OPENMEMORY_BASE_URL 环境变量（默认 http://127.0.0.1:8080）
   - api_key: OPENMEMORY_API_KEY 或 OM_API_KEY 环境变量（可选）
   - timeout: 硬编码 30.0 秒（可通过构造函数覆盖）
   - retry_config: 默认 RetryConfig()（可通过构造函数覆盖）
"""

from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional

import httpx

from engram.gateway.observability import observe_openmemory_call, start_span

logger = logging.getLogger(__name__)


# ---------- 重试配置 ----------


@dataclass
class RetryConfig:
    """HTTP 重试配置"""

    max_retries: int = 3  # 最大重试次数
    base_delay: float = 0.5  # 基础延迟秒数
    max_delay: float = 10.0  # 最大延迟秒数
    jitter: float = 0.25  # 抖动因子 (0.0 ~ 1.0)
    retry_on_5xx: bool = True  # 5xx 错误时重试
    retry_on_network_error: bool = True  # 网络错误时重试

    def calculate_delay(self, attempt: int) -> float:
        """计算指数退避延迟（含抖动）"""
        delay: float = self.base_delay * (2**attempt)
        delay = min(delay, self.max_delay)
        jitter_range = delay * self.jitter
        delay += random.uniform(-jitter_range, jitter_range)
        return max(0.1, delay)


DEFAULT_RETRY_CONFIG = RetryConfig()


# ---------- 异常类 ----------


class OpenMemoryError(Exception):
    """OpenMemory API 基础异常"""

    def __init__(
        self, message: str, status_code: Optional[int] = None, response: Optional[Dict] = None
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response = response


class OpenMemoryConnectionError(OpenMemoryError):
    """OpenMemory 连接异常（超时、网络不可达）"""

    pass


class OpenMemoryAPIError(OpenMemoryError):
    """OpenMemory API 返回错误（HTTP 4xx/5xx）"""

    pass


# ---------- 配置 ----------


def get_base_url() -> str:
    """从环境变量获取 OpenMemory 基础 URL"""
    url = os.getenv("OPENMEMORY_BASE_URL", "http://127.0.0.1:8080")
    return url.rstrip("/")


def get_api_key() -> Optional[str]:
    """
    从环境变量获取 OpenMemory API Key（可选）

    兼容读取：OPENMEMORY_API_KEY 优先，否则回退 OM_API_KEY
    """
    return os.getenv("OPENMEMORY_API_KEY") or os.getenv("OM_API_KEY")


def _as_non_empty_str(value: object) -> Optional[str]:
    if isinstance(value, str) and value:
        return value
    return None


def _extract_memory_id(payload: Any) -> Optional[str]:
    """兼容新旧 OpenMemory 响应格式的 memory_id 提取。"""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        memory_id = _as_non_empty_str(data.get("id"))
        if memory_id is not None:
            return memory_id
    for key in ("id", "memory_id"):
        memory_id = _as_non_empty_str(payload.get(key))
        if memory_id is not None:
            return memory_id
    return None


# ---------- 响应数据结构 ----------


@dataclass
class StoreResult:
    """存储结果"""

    success: bool
    memory_id: Optional[str] = None
    data: Optional[dict[str, Any]] = None
    error: Optional[str] = None


@dataclass
class SearchResult:
    """搜索结果"""

    success: bool
    results: Optional[list[dict[str, Any]]] = None
    error: Optional[str] = None

    def __post_init__(self) -> None:
        if self.results is None:
            self.results = []


@dataclass
class ListResult:
    """记忆列表结果（OpenMemory 1.3.0+）"""

    success: bool
    memories: Optional[list[dict[str, Any]]] = None
    total: int = 0
    error: Optional[str] = None

    def __post_init__(self) -> None:
        if self.memories is None:
            self.memories = []


@dataclass
class GetResult:
    """单条记忆获取结果（OpenMemory 1.3.0+）"""

    success: bool
    memory: Optional[dict[str, Any]] = None
    error: Optional[str] = None


@dataclass
class ReinforceResult:
    """记忆强化结果（OpenMemory 1.3.0+）"""

    success: bool
    memory_id: Optional[str] = None
    new_strength: Optional[float] = None
    error: Optional[str] = None


@dataclass
class WipeResult:
    """数据库清空结果（OpenMemory 1.3.0+，测试隔离用）"""

    success: bool
    deleted_count: int = 0
    error: Optional[str] = None


# ---------- HTTP 客户端 ----------


class OpenMemoryClient:
    """
    OpenMemory HTTP API 客户端

    线程安全: 是（httpx.Client 在 with 语句内使用，每次请求创建新连接）
    可重入: 是（无共享可变状态）

    构造参数来源:
        - base_url: OPENMEMORY_BASE_URL 环境变量（默认 http://127.0.0.1:8080）
        - api_key: OPENMEMORY_API_KEY 或 OM_API_KEY 环境变量（可选）
        - timeout: 默认 30.0 秒
        - retry_config: 默认 RetryConfig(max_retries=3, base_delay=0.5, ...)
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 30.0,
        retry_config: Optional[RetryConfig] = None,
    ):
        """
        初始化客户端

        Args:
            base_url: OpenMemory 服务地址，默认从 OPENMEMORY_BASE_URL 环境变量获取
            api_key: API Key，默认从 OPENMEMORY_API_KEY 或 OM_API_KEY 环境变量获取
            timeout: HTTP 请求超时秒数（默认 30.0）
            retry_config: 重试配置，默认使用 DEFAULT_RETRY_CONFIG
        """
        self.base_url = base_url or get_base_url()
        self.api_key = api_key or get_api_key()
        self.timeout = timeout
        self.retry_config = retry_config or DEFAULT_RETRY_CONFIG

    def _get_headers(self) -> dict[str, str]:
        """获取请求头"""
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _operation_from_url(self, url: str, method: str = "POST") -> str:
        """根据 URL 生成稳定的 OpenMemory 操作名。"""
        path = url
        if url.startswith(self.base_url):
            path = url[len(self.base_url) :]
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{method.upper()} {path}"

    def _is_retryable_error(self, exc: Exception) -> bool:
        """判断异常是否应该重试"""
        # 网络错误：超时、连接失败
        if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)):
            return self.retry_config.retry_on_network_error

        # HTTP 5xx 错误
        if isinstance(exc, httpx.HTTPStatusError):
            if 500 <= exc.response.status_code < 600:
                return self.retry_config.retry_on_5xx

        return False

    def _post_with_retry(
        self, url: str, payload: dict, retry_config: Optional[RetryConfig] = None
    ) -> httpx.Response:
        """
        带可控重试的 POST 请求

        仅在网络错误和 5xx 错误时重试，4xx 错误不重试

        Args:
            url: 请求 URL
            payload: JSON 请求体
            retry_config: 重试配置，默认使用实例配置

        Returns:
            httpx.Response 响应对象

        Raises:
            OpenMemoryConnectionError: 网络错误（超过重试次数）
            OpenMemoryAPIError: API 返回错误
        """
        config = retry_config or self.retry_config
        operation = self._operation_from_url(url, method="POST")
        started = time.perf_counter()
        status = "error"
        last_exception: Optional[Exception] = None

        with start_span(
            "gateway.openmemory.http.post",
            attributes={
                "openmemory.operation": operation,
                "openmemory.retries.max": config.max_retries,
            },
        ):
            try:
                for attempt in range(config.max_retries + 1):
                    try:
                        with httpx.Client(timeout=self.timeout) as client:
                            response = client.post(url, json=payload, headers=self._get_headers())
                            response.raise_for_status()
                            status = "ok"
                            return response

                    except Exception as e:
                        last_exception = e

                        # 判断是否应该重试
                        if not self._is_retryable_error(e):
                            # 不可重试的错误，直接抛出
                            raise

                        # 是否还有重试次数
                        if attempt < config.max_retries:
                            delay = config.calculate_delay(attempt)
                            logger.warning(
                                f"OpenMemory 请求失败 (尝试 {attempt + 1}/{config.max_retries + 1}), "
                                f"{delay:.2f}s 后重试: {e}"
                            )
                            time.sleep(delay)
                        else:
                            logger.error(
                                f"OpenMemory 请求失败，已达最大重试次数 ({config.max_retries + 1}): {e}"
                            )

                # 超过最大重试次数，抛出最后的异常
                if isinstance(last_exception, (httpx.TimeoutException,)):
                    raise OpenMemoryConnectionError(
                        message=f"OpenMemory 请求超时（已重试 {config.max_retries} 次）: {last_exception}",
                        status_code=None,
                        response=None,
                    )
                elif isinstance(last_exception, (httpx.ConnectError, httpx.RemoteProtocolError)):
                    raise OpenMemoryConnectionError(
                        message=f"无法连接到 OpenMemory 服务（已重试 {config.max_retries} 次）: {last_exception}",
                        status_code=None,
                        response=None,
                    )
                elif isinstance(last_exception, httpx.HTTPStatusError):
                    try:
                        error_body = last_exception.response.json()
                    except Exception:
                        error_body = {"detail": last_exception.response.text}
                    raise OpenMemoryAPIError(
                        message=(
                            "OpenMemory API 错误（已重试 "
                            f"{config.max_retries} 次）: {last_exception.response.status_code}"
                        ),
                        status_code=last_exception.response.status_code,
                        response=error_body,
                    )
                else:
                    raise OpenMemoryError(
                        message=f"OpenMemory 请求失败（已重试 {config.max_retries} 次）: {last_exception}",
                        status_code=None,
                        response=None,
                    )
            finally:
                duration = max(time.perf_counter() - started, 0.0)
                observe_openmemory_call(operation, status, duration)

    def add_memory(
        self,
        payload_md: str,
        actor_user_id: Optional[str] = None,
        target_space: Optional[str] = None,
        kind: Optional[str] = None,
        module: Optional[str] = None,
        evidence_refs: Optional[Dict[str, Any]] = None,
        payload_sha: Optional[str] = None,
        tags: Optional[list[str]] = None,
        extra_metadata: Optional[dict[str, Any]] = None,
    ) -> StoreResult:
        """
        添加记忆到 OpenMemory（符合任务字段映射规范）

        字段映射：
        - content = payload_md
        - user_id = actor_user_id（或空）
        - metadata 包含 target_space/kind/module/evidence_refs/payload_sha

        Args:
            payload_md: 记忆内容（markdown 格式）
            actor_user_id: 操作用户 ID（可选）
            target_space: 目标空间 (team:<project> / private:<user> / org:shared)
            kind: 记忆类型
            module: 来源模块
            evidence_refs: 证据引用
            payload_sha: 内容 SHA 哈希
            tags: 标签列表
            extra_metadata: 额外元数据

        Returns:
            StoreResult 结果对象

        Raises:
            OpenMemoryConnectionError: 连接超时或网络错误（超过重试次数）
            OpenMemoryAPIError: API 返回错误
        """
        url = f"{self.base_url}/memory/add"

        # 构建 metadata
        metadata: Dict[str, Any] = {}
        if target_space:
            metadata["target_space"] = target_space
        if kind:
            metadata["kind"] = kind
        if module:
            metadata["module"] = module
        if evidence_refs:
            metadata["evidence_refs"] = evidence_refs
        if payload_sha:
            metadata["payload_sha"] = payload_sha

        # 合并额外 metadata
        if extra_metadata:
            metadata.update(extra_metadata)

        # 构建请求 payload
        payload = {
            "content": payload_md,
            "user_id": actor_user_id,  # 可为 None
            "tags": tags or [],
            "metadata": metadata,
            "infer": False,  # 保留原始 content 全文，不经 LLM 摘要
        }

        try:
            response = self._post_with_retry(url, payload)
            data = response.json()

            return StoreResult(
                success=data.get("success", True),
                memory_id=_extract_memory_id(data),
                data=data.get("data"),
            )

        except httpx.HTTPStatusError as e:
            # 4xx 错误不重试，直接处理
            logger.error(
                f"OpenMemory add_memory HTTP error: {e.response.status_code} - {e.response.text}"
            )
            try:
                error_body = e.response.json()
            except Exception:
                error_body = {"detail": e.response.text}
            raise OpenMemoryAPIError(
                message=f"OpenMemory API 错误: {e.response.status_code}",
                status_code=e.response.status_code,
                response=error_body,
            )

        except OpenMemoryError:
            raise

        except Exception as e:
            logger.error(f"OpenMemory add_memory error: {e}")
            raise OpenMemoryError(
                message=f"OpenMemory 请求失败: {e}", status_code=None, response=None
            )

    def store(
        self,
        content: str,
        space: Optional[str] = None,
        user_id: Optional[str] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        meta: Optional[dict[str, Any]] = None,
    ) -> StoreResult:
        """
        存储记忆到 OpenMemory（兼容旧接口）

        Args:
            content: 记忆内容（markdown）
            space: 目标空间 (team:<project> / private:<user> / org:shared)
            user_id: 用户 ID（用于私有空间，如果 space 未指定）
            tags: 标签列表
            metadata: 额外元数据
            meta: 额外元数据（兼容别名）

        Returns:
            StoreResult 结果对象

        Raises:
            OpenMemoryConnectionError: 连接超时或网络错误
            OpenMemoryAPIError: API 返回错误
        """
        url = f"{self.base_url}/memory/add"

        # 合并 metadata 和 meta
        final_metadata = metadata or meta or {}
        if space:
            final_metadata["space"] = space

        payload = {
            "content": content,
            "user_id": user_id,
            "tags": tags or [],
            "metadata": final_metadata,
            "infer": False,  # 保留原始 content 全文，不经 LLM 摘要
        }

        try:
            response = self._post_with_retry(url, payload)
            data = response.json()

            return StoreResult(
                success=data.get("success", True),
                memory_id=_extract_memory_id(data),
                data=data.get("data"),
            )

        except httpx.HTTPStatusError as e:
            # 4xx 错误不重试，直接处理
            logger.error(
                f"OpenMemory store HTTP error: {e.response.status_code} - {e.response.text}"
            )
            try:
                error_body = e.response.json()
            except Exception:
                error_body = {"detail": e.response.text}
            raise OpenMemoryAPIError(
                message=f"OpenMemory API 错误: {e.response.status_code}",
                status_code=e.response.status_code,
                response=error_body,
            )

        except OpenMemoryError:
            raise

        except Exception as e:
            logger.error(f"OpenMemory store error: {e}")
            raise OpenMemoryError(
                message=f"OpenMemory 请求失败: {e}", status_code=None, response=None
            )

    def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        limit: int = 10,
        filters: Optional[dict[str, Any]] = None,
    ) -> SearchResult:
        """
        搜索 OpenMemory 记忆（带可控重试）

        Args:
            query: 搜索查询
            user_id: 用户 ID（用于私有空间过滤）
            limit: 返回结果数量限制
            filters: 额外过滤条件

        Returns:
            SearchResult 结果对象
        """
        payload = {"query": query, "user_id": user_id, "limit": limit, "filters": filters or {}}
        # 兼容不同版本 OpenMemory 路由：
        # - 旧版本: /memory/search
        # - 新版本 (v1.2.x): /memory/query
        search_urls = [
            f"{self.base_url}/memory/query",
            f"{self.base_url}/memory/search",
        ]

        try:
            last_not_found: Optional[httpx.HTTPStatusError] = None
            data: Optional[Dict[str, Any]] = None

            for url in search_urls:
                try:
                    response = self._post_with_retry(url, payload)
                    data = response.json()
                    break
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 404:
                        last_not_found = e
                        continue
                    raise

            if data is None:
                # 两个候选路由都 404，抛出最后一个 404 便于上层定位
                if last_not_found is not None:
                    raise last_not_found
                return SearchResult(success=False, error="no_search_endpoint_available")

            # 兼容返回结构：
            # - 旧版本: {"results": [...]}
            # - 新版本: {"matches": [...]}
            if isinstance(data.get("results"), list):
                results = data["results"]
            elif isinstance(data.get("matches"), list):
                results = data["matches"]
            else:
                results = []

            return SearchResult(success=True, results=results)

        except OpenMemoryConnectionError as e:
            logger.error(f"OpenMemory search connection error: {e}")
            return SearchResult(success=False, error=f"connection_error: {e.message}")

        except OpenMemoryAPIError as e:
            logger.error(f"OpenMemory search API error: {e.status_code}")
            return SearchResult(success=False, error=f"http_error: {e.status_code}")

        except Exception as e:
            logger.error(f"OpenMemory search error: {e}")
            return SearchResult(success=False, error=str(e))

    def health_check(self) -> bool:
        """
        检查 OpenMemory 服务健康状态

        Returns:
            True 如果服务正常，否则 False
        """
        url = f"{self.base_url}/health"
        operation = self._operation_from_url(url, method="GET")
        started = time.perf_counter()
        status = "error"

        with start_span(
            "gateway.openmemory.health_check",
            attributes={"openmemory.operation": operation},
        ):
            try:
                with httpx.Client(timeout=5.0) as client:
                    response = client.get(url, headers=self._get_headers())
                    response.raise_for_status()
                    data = response.json()
                    health_status: str = data.get("status", "")
                    status = "ok" if health_status == "ok" else "error"
                    return health_status == "ok"

            except Exception as e:
                logger.warning(f"OpenMemory health check failed: {e}")
                return False
            finally:
                observe_openmemory_call(operation, status, max(time.perf_counter() - started, 0.0))

    # ========== OpenMemory 1.3.0+ 新增方法 ==========

    def list_memories(
        self,
        user_id: Optional[str] = None,
        space: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> ListResult:
        """
        列出记忆（OpenMemory 1.3.0+）

        对应端点：GET /memory/all

        Args:
            user_id: 用户 ID（用于过滤私有空间）
            space: 空间过滤（如 team:project, private:user）
            limit: 返回数量限制
            offset: 分页偏移

        Returns:
            ListResult 结果对象
        """
        url = f"{self.base_url}/memory/all"
        operation = self._operation_from_url(url, method="GET")
        started = time.perf_counter()
        status = "error"

        # 构建查询参数
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if user_id:
            params["user_id"] = user_id
        if space:
            params["space"] = space

        with start_span(
            "gateway.openmemory.list_memories",
            attributes={"openmemory.operation": operation},
        ):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.get(url, params=params, headers=self._get_headers())
                    response.raise_for_status()
                    data = response.json()

                    # 兼容返回结构
                    memories = data.get("memories") or data.get("results") or []
                    total = data.get("total") or len(memories)
                    status = "ok"

                    return ListResult(
                        success=True,
                        memories=memories,
                        total=total,
                    )

            except httpx.HTTPStatusError as e:
                logger.error(f"OpenMemory list_memories HTTP error: {e.response.status_code}")
                return ListResult(
                    success=False,
                    error=f"http_error: {e.response.status_code}",
                )

            except Exception as e:
                logger.error(f"OpenMemory list_memories error: {e}")
                return ListResult(success=False, error=str(e))
            finally:
                observe_openmemory_call(operation, status, max(time.perf_counter() - started, 0.0))

    def get_memory(self, memory_id: str) -> GetResult:
        """
        获取单条记忆详情（OpenMemory 1.3.0+）

        对应端点：GET /memory/{id}

        Args:
            memory_id: 记忆 ID

        Returns:
            GetResult 结果对象
        """
        url = f"{self.base_url}/memory/{memory_id}"
        operation = self._operation_from_url(url, method="GET")
        started = time.perf_counter()
        status = "error"

        with start_span(
            "gateway.openmemory.get_memory",
            attributes={"openmemory.operation": operation},
        ):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.get(url, headers=self._get_headers())
                    response.raise_for_status()
                    data = response.json()

                    # 兼容返回结构
                    memory = data.get("memory") or data.get("data") or data
                    status = "ok"

                    return GetResult(success=True, memory=memory)

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return GetResult(success=False, error="memory_not_found")
                logger.error(f"OpenMemory get_memory HTTP error: {e.response.status_code}")
                return GetResult(success=False, error=f"http_error: {e.response.status_code}")

            except Exception as e:
                logger.error(f"OpenMemory get_memory error: {e}")
                return GetResult(success=False, error=str(e))
            finally:
                observe_openmemory_call(operation, status, max(time.perf_counter() - started, 0.0))

    def reinforce(
        self,
        memory_id: str,
        delta: float = 1.0,
        reason: Optional[str] = None,
    ) -> ReinforceResult:
        """
        强化记忆（OpenMemory 1.3.0+）

        对应端点：POST /memory/reinforce

        Args:
            memory_id: 记忆 ID
            delta: 强化增量（默认 1.0）
            reason: 强化原因（可选）

        Returns:
            ReinforceResult 结果对象
        """
        url = f"{self.base_url}/memory/reinforce"

        payload = {
            "memory_id": memory_id,
            "delta": delta,
        }
        if reason:
            payload["reason"] = reason

        try:
            response = self._post_with_retry(url, payload)
            data = response.json()

            return ReinforceResult(
                success=data.get("success", True),
                memory_id=memory_id,
                new_strength=data.get("new_strength") or data.get("strength"),
            )

        except httpx.HTTPStatusError as e:
            logger.error(f"OpenMemory reinforce HTTP error: {e.response.status_code}")
            return ReinforceResult(
                success=False,
                memory_id=memory_id,
                error=f"http_error: {e.response.status_code}",
            )

        except OpenMemoryError as e:
            logger.error(f"OpenMemory reinforce error: {e}")
            return ReinforceResult(success=False, memory_id=memory_id, error=e.message)

        except Exception as e:
            logger.error(f"OpenMemory reinforce error: {e}")
            return ReinforceResult(success=False, memory_id=memory_id, error=str(e))

    def wipe(
        self,
        confirm: bool = False,
        user_id: Optional[str] = None,
    ) -> WipeResult:
        """
        清空数据库（OpenMemory 1.3.0+，测试隔离用）

        ⚠️ 危险操作：会清空所有记忆、向量、路径点等数据
        对应端点：POST /admin/wipe 或 DELETE /memory/all

        Args:
            confirm: 必须设置为 True 才会执行
            user_id: 仅清空该用户的记忆（如支持）

        Returns:
            WipeResult 结果对象
        """
        if not confirm:
            return WipeResult(
                success=False,
                error="confirm must be True to wipe database",
            )

        # 尝试多个可能的端点（OpenMemory 不同版本实现可能不同）
        possible_urls = [
            f"{self.base_url}/admin/wipe",
            f"{self.base_url}/memory/wipe",
            f"{self.base_url}/memory/all",
        ]

        last_error: Optional[str] = None
        started = time.perf_counter()
        status = "error"

        with start_span("gateway.openmemory.wipe", attributes={"openmemory.operation": "wipe"}):
            try:
                for url in possible_urls:
                    try:
                        payload: dict[str, Any] = {"confirm": True}
                        if user_id:
                            payload["user_id"] = user_id

                        with httpx.Client(timeout=30.0) as client:
                            # 尝试 POST
                            try:
                                response = client.post(
                                    url, json=payload, headers=self._get_headers(), timeout=30.0
                                )
                                response.raise_for_status()
                            except httpx.HTTPStatusError as e_post:
                                # 如果 POST 405，尝试 DELETE
                                if e_post.response.status_code == 405 and "all" in url:
                                    response = client.delete(
                                        url,
                                        params=payload,
                                        headers=self._get_headers(),
                                        timeout=30.0,
                                    )
                                    response.raise_for_status()
                                else:
                                    raise

                            data = response.json()
                            status = "ok"

                            return WipeResult(
                                success=data.get("success", True),
                                deleted_count=data.get("deleted_count") or data.get("count", 0),
                            )

                    except httpx.HTTPStatusError as e:
                        if e.response.status_code == 404:
                            last_error = f"endpoint not found: {url}"
                            continue
                        last_error = f"http_error: {e.response.status_code}"
                        if e.response.status_code in (401, 403):
                            return WipeResult(
                                success=False,
                                error=f"unauthorized: {e.response.status_code}",
                            )
                        continue

                    except Exception as e:
                        last_error = str(e)
                        continue

                # 所有端点都失败
                logger.error(f"OpenMemory wipe failed on all endpoints: {last_error}")
                return WipeResult(success=False, error=last_error or "all endpoints failed")
            finally:
                observe_openmemory_call(
                    "POST /admin/wipe",
                    status,
                    max(time.perf_counter() - started, 0.0),
                )


# ---------- 便捷函数 ----------

# 类型标注：避免循环导入
if TYPE_CHECKING:
    from .config import GatewayConfig

_default_client: Optional[OpenMemoryClient] = None


def get_client(config: Optional["GatewayConfig"] = None) -> OpenMemoryClient:
    """
    获取 OpenMemoryClient 实例

    优先级规则：
    1. 如果传入 config，从 config 获取 base_url/api_key（显式配置）
    2. 如果不传入 config，使用全局单例（从环境变量获取配置）

    推荐用法：
    - 在 handlers 中，应通过 Container 获取 client，而非直接调用此函数
    - 此函数保留用于向后兼容和简单场景

    Args:
        config: 可选的 GatewayConfig 对象。如果提供，从中获取 base_url/api_key。

    Returns:
        OpenMemoryClient 实例
    """
    if config is not None:
        # 显式传入 config：每次创建新实例，确保使用 config 的配置
        return OpenMemoryClient(
            base_url=config.openmemory_base_url,
            api_key=config.openmemory_api_key,
        )

    # 无 config：使用全局单例（向后兼容）
    global _default_client
    if _default_client is None:
        _default_client = OpenMemoryClient()
    return _default_client


def reset_client() -> None:
    """
    重置全局单例客户端

    线程安全: 否（建议在单线程环境下调用，如测试 setup/teardown）

    用于测试清理。调用后下次 get_client() 将重新从环境变量初始化。
    """
    global _default_client
    _default_client = None


def override_client(client: OpenMemoryClient) -> None:
    """
    覆盖全局单例客户端（测试专用）

    线程安全: 否（建议在单线程环境下调用，如测试 setup）

    允许测试代码注入 mock 客户端，替换全局单例。
    测试完成后应调用 reset_client() 恢复默认行为。

    Args:
        client: 要设置的 OpenMemoryClient 实例（可以是 mock）

    Usage:
        # 在测试 setup 中
        mock_client = Mock(spec=OpenMemoryClient)
        override_client(mock_client)

        # 测试代码...

        # 在测试 teardown 中
        reset_client()
    """
    global _default_client
    _default_client = client


def get_client_or_none() -> Optional[OpenMemoryClient]:
    """
    获取全局单例客户端（如果已初始化）

    线程安全: 是（只读操作）

    用于检查全局单例状态，不触发延迟初始化。

    Returns:
        已初始化的 OpenMemoryClient 实例，或 None
    """
    return _default_client


def store_memory(
    content: str,
    user_id: Optional[str] = None,
    tags: Optional[list[str]] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> StoreResult:
    """便捷函数：存储记忆"""
    return get_client().store(
        content=content,
        user_id=user_id,
        tags=tags,
        metadata=metadata,
    )


def search_memory(
    query: str,
    user_id: Optional[str] = None,
    limit: int = 10,
    filters: Optional[dict[str, Any]] = None,
) -> SearchResult:
    """便捷函数：搜索记忆"""
    return get_client().search(query, user_id, limit, filters)


# ---------- OpenMemory 1.3.0+ 便捷函数 ----------


def list_memories(
    user_id: Optional[str] = None,
    space: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> ListResult:
    """便捷函数：列出记忆（OpenMemory 1.3.0+）"""
    return get_client().list_memories(user_id, space, limit, offset)


def get_memory(memory_id: str) -> GetResult:
    """便捷函数：获取单条记忆（OpenMemory 1.3.0+）"""
    return get_client().get_memory(memory_id)


def reinforce_memory(
    memory_id: str,
    delta: float = 1.0,
    reason: Optional[str] = None,
) -> ReinforceResult:
    """便捷函数：强化记忆（OpenMemory 1.3.0+）"""
    return get_client().reinforce(memory_id, delta, reason)


def wipe_memory(confirm: bool = False, user_id: Optional[str] = None) -> WipeResult:
    """
    便捷函数：清空数据库（OpenMemory 1.3.0+，测试隔离用）

    ⚠️ 危险操作：会清空所有记忆数据

    Args:
        confirm: 必须设置为 True 才会执行
        user_id: 仅清空该用户的记忆

    Returns:
        WipeResult 结果对象
    """
    return get_client().wipe(confirm, user_id)


# ---------- 导出定义 ----------

__all__ = [
    # 异常类
    "OpenMemoryError",
    "OpenMemoryConnectionError",
    "OpenMemoryAPIError",
    # 配置类
    "RetryConfig",
    "DEFAULT_RETRY_CONFIG",
    # 响应数据类
    "StoreResult",
    "SearchResult",
    "ListResult",  # 1.3.0+
    "GetResult",  # 1.3.0+
    "ReinforceResult",  # 1.3.0+
    "WipeResult",  # 1.3.0+
    # 客户端类
    "OpenMemoryClient",
    # 客户端工厂函数
    "get_client",
    "reset_client",
    "override_client",
    "get_client_or_none",
    # 便捷函数
    "store_memory",
    "search_memory",
    "list_memories",  # 1.3.0+
    "get_memory",  # 1.3.0+
    "reinforce_memory",  # 1.3.0+
    "wipe_memory",  # 1.3.0+
    # 配置函数
    "get_base_url",
    "get_api_key",
]
