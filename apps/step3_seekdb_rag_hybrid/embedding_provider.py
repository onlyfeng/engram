#!/usr/bin/env python3
"""
embedding_provider.py - Embedding 服务抽象层

定义 Embedding 服务的统一接口，支持多种实现（OpenAI、本地模型等）。
通过配置或环境变量选择具体实现。

接口定义：
    - embed_texts(texts: List[str]) -> List[List[float]]: 批量文本向量化
    - 元信息：model_id, dim, normalize

使用:
    from step3_seekdb_rag_hybrid.embedding_provider import get_embedding_provider
    
    provider = get_embedding_provider()
    vectors = provider.embed_texts(["hello", "world"])
    print(f"Model: {provider.model_id}, Dim: {provider.dim}")
"""

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ============ 元信息数据结构 ============


@dataclass
class EmbeddingModelInfo:
    """Embedding 模型元信息，用于索引记录和一致性自检"""
    model_id: str       # 模型标识 (如 "text-embedding-3-small")
    dim: int            # 向量维度 (如 1536)
    normalize: bool     # 是否归一化向量
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "model_id": self.model_id,
            "dim": self.dim,
            "normalize": self.normalize,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EmbeddingModelInfo":
        """从字典构建"""
        return cls(
            model_id=data.get("model_id", ""),
            dim=data.get("dim", 0),
            normalize=data.get("normalize", True),
        )
    
    def is_compatible(self, other: "EmbeddingModelInfo") -> bool:
        """检查两个模型是否兼容（用于回滚检测）"""
        return self.model_id == other.model_id and self.dim == other.dim


# ============ Embedding Provider 抽象接口 ============


class EmbeddingProvider(ABC):
    """
    Embedding 服务抽象接口
    
    所有 Embedding 实现必须继承此类，实现核心的 embed_texts 方法。
    """
    
    @property
    @abstractmethod
    def model_id(self) -> str:
        """模型标识"""
        pass
    
    @property
    @abstractmethod
    def dim(self) -> int:
        """向量维度"""
        pass
    
    @property
    @abstractmethod
    def normalize(self) -> bool:
        """是否返回归一化向量"""
        pass
    
    @abstractmethod
    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """
        批量文本向量化
        
        Args:
            texts: 待向量化的文本列表
        
        Returns:
            向量列表，每个向量长度为 dim
        
        Raises:
            EmbeddingError: 向量化失败时抛出
        """
        pass
    
    def embed_text(self, text: str) -> List[float]:
        """
        单文本向量化（便捷方法）
        
        Args:
            text: 待向量化的文本
        
        Returns:
            向量，长度为 dim
        """
        results = self.embed_texts([text])
        return results[0] if results else []
    
    def get_model_info(self) -> EmbeddingModelInfo:
        """获取模型元信息"""
        return EmbeddingModelInfo(
            model_id=self.model_id,
            dim=self.dim,
            normalize=self.normalize,
        )
    
    def health_check(self) -> Dict[str, Any]:
        """
        健康检查
        
        Returns:
            健康状态，包含 status 和 details
        """
        try:
            # 尝试 embed 一个简单文本
            test_vector = self.embed_text("health check")
            if len(test_vector) == self.dim:
                return {
                    "status": "healthy",
                    "model_id": self.model_id,
                    "dim": self.dim,
                }
            else:
                return {
                    "status": "unhealthy",
                    "error": f"维度不匹配: 期望 {self.dim}, 实际 {len(test_vector)}",
                }
        except Exception as e:
            return {
                "status": "unhealthy",
                "error": str(e),
            }


# ============ 异常类 ============


class EmbeddingError(Exception):
    """Embedding 服务错误"""
    
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        self.message = message
        self.details = details or {}
        super().__init__(message)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": "EmbeddingError",
            "message": self.message,
            "details": self.details,
        }


class EmbeddingModelMismatchError(EmbeddingError):
    """模型不匹配错误（用于一致性检测）"""
    
    def __init__(
        self,
        expected: EmbeddingModelInfo,
        actual: EmbeddingModelInfo,
        message: Optional[str] = None,
    ):
        self.expected = expected
        self.actual = actual
        msg = message or (
            f"Embedding 模型不匹配: 期望 {expected.model_id}(dim={expected.dim}), "
            f"实际 {actual.model_id}(dim={actual.dim})"
        )
        super().__init__(msg, {
            "expected": expected.to_dict(),
            "actual": actual.to_dict(),
        })


# ============ 具体实现: Stub Provider ============


class StubEmbeddingProvider(EmbeddingProvider):
    """
    Stub 实现 - 仅用于开发和测试
    
    返回固定维度的零向量，不依赖外部服务。
    """
    
    def __init__(
        self,
        model_id: str = "stub-embedding",
        dim: int = 1536,
        normalize: bool = True,
    ):
        self._model_id = model_id
        self._dim = dim
        self._normalize = normalize
    
    @property
    def model_id(self) -> str:
        return self._model_id
    
    @property
    def dim(self) -> int:
        return self._dim
    
    @property
    def normalize(self) -> bool:
        return self._normalize
    
    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """返回零向量（stub 实现）"""
        logger.debug(f"[STUB] Embedding {len(texts)} texts with dim={self._dim}")
        # 返回零向量
        return [[0.0] * self._dim for _ in texts]


# ============ 具体实现: OpenAI Provider ============


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """
    OpenAI Embedding 实现
    
    使用 OpenAI API 进行文本向量化。
    
    环境变量:
        OPENAI_API_KEY: API 密钥
        OPENAI_BASE_URL: API 基础 URL（可选，用于自定义端点）
    """
    
    # 支持的模型及其维度
    SUPPORTED_MODELS = {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
        "text-embedding-ada-002": 1536,
    }
    
    def __init__(
        self,
        model_id: str = "text-embedding-3-small",
        dim: Optional[int] = None,
        normalize: bool = True,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        batch_size: int = 100,
    ):
        """
        初始化 OpenAI Embedding Provider
        
        Args:
            model_id: 模型标识
            dim: 向量维度（可选，部分模型支持降维）
            normalize: 是否归一化
            api_key: API 密钥（默认从环境变量读取）
            base_url: API 基础 URL
            batch_size: 批量处理大小
        """
        self._model_id = model_id
        self._normalize = normalize
        self._batch_size = batch_size
        
        # 确定维度
        if dim is not None:
            self._dim = dim
        elif model_id in self.SUPPORTED_MODELS:
            self._dim = self.SUPPORTED_MODELS[model_id]
        else:
            self._dim = 1536  # 默认维度
        
        # API 配置
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self._base_url = base_url or os.environ.get("OPENAI_BASE_URL")
        
        # 懒加载 client
        self._client = None
    
    @property
    def model_id(self) -> str:
        return self._model_id
    
    @property
    def dim(self) -> int:
        return self._dim
    
    @property
    def normalize(self) -> bool:
        return self._normalize
    
    def _get_client(self):
        """获取或创建 OpenAI client"""
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError:
                raise EmbeddingError(
                    "openai 包未安装，请运行: pip install openai",
                    {"provider": "openai"},
                )
            
            if not self._api_key:
                raise EmbeddingError(
                    "OPENAI_API_KEY 未设置",
                    {"provider": "openai"},
                )
            
            kwargs = {"api_key": self._api_key}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            
            self._client = OpenAI(**kwargs)
        
        return self._client
    
    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """调用 OpenAI API 进行向量化"""
        if not texts:
            return []
        
        client = self._get_client()
        all_vectors: List[List[float]] = []
        
        # 分批处理
        for i in range(0, len(texts), self._batch_size):
            batch = texts[i:i + self._batch_size]
            
            try:
                # 构建请求参数
                kwargs: Dict[str, Any] = {
                    "input": batch,
                    "model": self._model_id,
                }
                
                # text-embedding-3-* 支持 dimensions 参数
                if self._model_id.startswith("text-embedding-3-"):
                    kwargs["dimensions"] = self._dim
                
                response = client.embeddings.create(**kwargs)
                
                # 提取向量（按 index 排序）
                embeddings = sorted(response.data, key=lambda x: x.index)
                batch_vectors = [e.embedding for e in embeddings]
                all_vectors.extend(batch_vectors)
                
            except Exception as e:
                raise EmbeddingError(
                    f"OpenAI Embedding 调用失败: {e}",
                    {
                        "provider": "openai",
                        "model": self._model_id,
                        "batch_start": i,
                        "batch_size": len(batch),
                    },
                )
        
        return all_vectors


# ============ Provider 工厂函数 ============


# 全局 provider 实例（单例）
_embedding_provider: Optional[EmbeddingProvider] = None


def get_embedding_provider(
    provider_type: Optional[str] = None,
    config: Optional[Any] = None,
    **kwargs,
) -> EmbeddingProvider:
    """
    获取 Embedding Provider 实例
    
    支持通过环境变量或配置参数选择实现：
    - EMBEDDING_PROVIDER: 实现类型 (stub/openai)
    - EMBEDDING_MODEL: 模型标识
    - EMBEDDING_DIM: 向量维度
    
    Args:
        provider_type: 实现类型（可选，默认从环境变量读取）
        config: Step1 配置对象（可选，用于扩展配置）
        **kwargs: 传递给具体 Provider 的参数
    
    Returns:
        EmbeddingProvider 实例
    """
    global _embedding_provider
    
    # 如果已有实例且未指定新参数，返回现有实例
    if _embedding_provider is not None and provider_type is None and not kwargs:
        return _embedding_provider
    
    # 确定 provider 类型
    if provider_type is None:
        provider_type = os.environ.get("EMBEDDING_PROVIDER", "stub")
    
    # 从环境变量读取默认参数
    model_id = kwargs.pop("model_id", None) or os.environ.get("EMBEDDING_MODEL")
    dim = kwargs.pop("dim", None)
    if dim is None:
        dim_str = os.environ.get("EMBEDDING_DIM")
        dim = int(dim_str) if dim_str else None
    
    # 尝试从 config 读取扩展配置
    if config is not None and hasattr(config, "get"):
        embedding_config = config.get("embedding", {})
        if isinstance(embedding_config, dict):
            provider_type = embedding_config.get("provider", provider_type)
            model_id = model_id or embedding_config.get("model_id")
            dim = dim or embedding_config.get("dim")
    
    # 创建 provider
    provider_type = provider_type.lower()
    
    if provider_type == "stub":
        provider_kwargs = {}
        if model_id:
            provider_kwargs["model_id"] = model_id
        if dim:
            provider_kwargs["dim"] = dim
        provider_kwargs.update(kwargs)
        _embedding_provider = StubEmbeddingProvider(**provider_kwargs)
    
    elif provider_type == "openai":
        provider_kwargs = {}
        if model_id:
            provider_kwargs["model_id"] = model_id
        if dim:
            provider_kwargs["dim"] = dim
        provider_kwargs.update(kwargs)
        _embedding_provider = OpenAIEmbeddingProvider(**provider_kwargs)
    
    else:
        raise EmbeddingError(
            f"未知的 Embedding Provider 类型: {provider_type}",
            {"supported": ["stub", "openai"]},
        )
    
    logger.info(
        f"初始化 EmbeddingProvider: {_embedding_provider.model_id} "
        f"(dim={_embedding_provider.dim}, normalize={_embedding_provider.normalize})"
    )
    
    return _embedding_provider


def set_embedding_provider(provider: Optional[EmbeddingProvider]) -> None:
    """设置全局 Embedding Provider 实例"""
    global _embedding_provider
    _embedding_provider = provider


def reset_embedding_provider() -> None:
    """重置全局 Embedding Provider 实例"""
    global _embedding_provider
    _embedding_provider = None


# ============ 一致性检查工具 ============


def check_embedding_consistency(
    index_model_info: EmbeddingModelInfo,
    provider: Optional[EmbeddingProvider] = None,
) -> bool:
    """
    检查当前 provider 与索引记录的模型信息是否一致
    
    用于索引操作前的自检，防止模型不匹配导致的向量空间不一致。
    
    Args:
        index_model_info: 索引中记录的模型信息
        provider: Embedding Provider（可选，默认使用全局实例）
    
    Returns:
        是否一致
    
    Raises:
        EmbeddingModelMismatchError: 模型不匹配时抛出
    """
    if provider is None:
        provider = get_embedding_provider()
    
    current_info = provider.get_model_info()
    
    if not current_info.is_compatible(index_model_info):
        raise EmbeddingModelMismatchError(
            expected=index_model_info,
            actual=current_info,
        )
    
    return True
