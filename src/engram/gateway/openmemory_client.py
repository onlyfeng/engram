"""
OpenMemory HTTP API 客户端
负责：调用 OpenMemory 后端进行 store/query/reinforce 操作

字段映射：
- content = payload_md
- user_id = actor_user_id（或空）
- metadata 包含 target_space/kind/module/evidence_refs/payload_sha

重试策略：仅网络错误和 5xx 错误时重试
"""

from __future__ import annotations

import os
import logging
import time
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


# ---------- 重试配置 ----------

@dataclass
class RetryConfig:
    """HTTP 重试配置"""
    max_retries: int = 3               # 最大重试次数
    base_delay: float = 0.5            # 基础延迟秒数
    max_delay: float = 10.0            # 最大延迟秒数
    jitter: float = 0.25               # 抖动因子 (0.0 ~ 1.0)
    retry_on_5xx: bool = True          # 5xx 错误时重试
    retry_on_network_error: bool = True  # 网络错误时重试
    
    def calculate_delay(self, attempt: int) -> float:
        """计算指数退避延迟（含抖动）"""
        delay = self.base_delay * (2 ** attempt)
        delay = min(delay, self.max_delay)
        jitter_range = delay * self.jitter
        delay += random.uniform(-jitter_range, jitter_range)
        return max(0.1, delay)


DEFAULT_RETRY_CONFIG = RetryConfig()


# ---------- 异常类 ----------

class OpenMemoryError(Exception):
    """OpenMemory API 基础异常"""

    def __init__(self, message: str, status_code: Optional[int] = None, response: Optional[Dict] = None):
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
    results: list[dict[str, Any]] = None
    error: Optional[str] = None
    
    def __post_init__(self):
        if self.results is None:
            self.results = []


# ---------- HTTP 客户端 ----------

class OpenMemoryClient:
    """OpenMemory HTTP API 客户端"""
    
    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 30.0,
        retry_config: Optional[RetryConfig] = None
    ):
        """
        初始化客户端
        
        Args:
            base_url: OpenMemory 服务地址，默认从环境变量获取
            api_key: API Key，默认从环境变量获取
            timeout: HTTP 请求超时秒数
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
        self,
        url: str,
        payload: dict,
        retry_config: Optional[RetryConfig] = None
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
        last_exception: Optional[Exception] = None
        
        for attempt in range(config.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.post(
                        url,
                        json=payload,
                        headers=self._get_headers()
                    )
                    response.raise_for_status()
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
                response=None
            )
        elif isinstance(last_exception, (httpx.ConnectError, httpx.RemoteProtocolError)):
            raise OpenMemoryConnectionError(
                message=f"无法连接到 OpenMemory 服务（已重试 {config.max_retries} 次）: {last_exception}",
                status_code=None,
                response=None
            )
        elif isinstance(last_exception, httpx.HTTPStatusError):
            try:
                error_body = last_exception.response.json()
            except Exception:
                error_body = {"detail": last_exception.response.text}
            raise OpenMemoryAPIError(
                message=f"OpenMemory API 错误（已重试 {config.max_retries} 次）: {last_exception.response.status_code}",
                status_code=last_exception.response.status_code,
                response=error_body
            )
        else:
            raise OpenMemoryError(
                message=f"OpenMemory 请求失败（已重试 {config.max_retries} 次）: {last_exception}",
                status_code=None,
                response=None
            )
    
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
            "metadata": metadata
        }
        
        try:
            response = self._post_with_retry(url, payload)
            data = response.json()
            
            return StoreResult(
                success=data.get("success", True),
                memory_id=data.get("data", {}).get("id"),
                data=data.get("data")
            )
            
        except httpx.HTTPStatusError as e:
            # 4xx 错误不重试，直接处理
            logger.error(f"OpenMemory add_memory HTTP error: {e.response.status_code} - {e.response.text}")
            try:
                error_body = e.response.json()
            except Exception:
                error_body = {"detail": e.response.text}
            raise OpenMemoryAPIError(
                message=f"OpenMemory API 错误: {e.response.status_code}",
                status_code=e.response.status_code,
                response=error_body
            )
            
        except OpenMemoryError:
            raise
            
        except Exception as e:
            logger.error(f"OpenMemory add_memory error: {e}")
            raise OpenMemoryError(
                message=f"OpenMemory 请求失败: {e}",
                status_code=None,
                response=None
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
            "metadata": final_metadata
        }
        
        try:
            response = self._post_with_retry(url, payload)
            data = response.json()
            
            return StoreResult(
                success=data.get("success", True),
                memory_id=data.get("data", {}).get("id"),
                data=data.get("data")
            )
                
        except httpx.HTTPStatusError as e:
            # 4xx 错误不重试，直接处理
            logger.error(f"OpenMemory store HTTP error: {e.response.status_code} - {e.response.text}")
            try:
                error_body = e.response.json()
            except Exception:
                error_body = {"detail": e.response.text}
            raise OpenMemoryAPIError(
                message=f"OpenMemory API 错误: {e.response.status_code}",
                status_code=e.response.status_code,
                response=error_body
            )
            
        except OpenMemoryError:
            raise
            
        except Exception as e:
            logger.error(f"OpenMemory store error: {e}")
            raise OpenMemoryError(
                message=f"OpenMemory 请求失败: {e}",
                status_code=None,
                response=None
            )
    
    def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        limit: int = 10,
        filters: Optional[dict[str, Any]] = None
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
        url = f"{self.base_url}/memory/search"
        payload = {
            "query": query,
            "user_id": user_id,
            "limit": limit,
            "filters": filters or {}
        }
        
        try:
            response = self._post_with_retry(url, payload)
            data = response.json()
            
            return SearchResult(
                success=True,
                results=data.get("results", [])
            )
                
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
        
        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(url, headers=self._get_headers())
                response.raise_for_status()
                data = response.json()
                return data.get("status") == "ok"
                
        except Exception as e:
            logger.warning(f"OpenMemory health check failed: {e}")
            return False


# ---------- 便捷函数 ----------

_default_client: Optional[OpenMemoryClient] = None


def get_client() -> OpenMemoryClient:
    """获取默认客户端实例（单例）"""
    global _default_client
    if _default_client is None:
        _default_client = OpenMemoryClient()
    return _default_client


def store_memory(
    content: str,
    user_id: Optional[str] = None,
    tags: Optional[list[str]] = None,
    metadata: Optional[dict[str, Any]] = None
) -> StoreResult:
    """便捷函数：存储记忆"""
    return get_client().store(content, user_id, tags, metadata)


def search_memory(
    query: str,
    user_id: Optional[str] = None,
    limit: int = 10,
    filters: Optional[dict[str, Any]] = None
) -> SearchResult:
    """便捷函数：搜索记忆"""
    return get_client().search(query, user_id, limit, filters)
