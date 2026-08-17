"""Embedding：langchain ``init_embeddings`` 统一入口，无供应商特判。

provider 字段直接透传给 langchain 的 ``init_embeddings``（与 chat 模型侧
``init_chat_model`` 对齐），支持 openai / azure_openai / bedrock / cohere 等
全部 langchain 内建 provider；缺 api_key、缺依赖或 provider 未注册时降级为
禁用态（核心照常启动，实际调用时报明确原因），配置热更新即时生效。
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Callable, Protocol

from minic.core.config import AppSettings


class EmbeddingProvider(Protocol):
    """Embedding 提供商接口。"""

    def embed_texts(self, texts: list[str], text_type: str = "document") -> list[list[float]]:
        """把文本列表转换为向量列表。"""


class DisabledEmbeddingProvider:
    """未配置/初始化失败的 embedding：核心可正常启动，实际使用时报明确错误。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def embed_texts(self, texts: list[str], text_type: str = "document") -> list[list[float]]:
        """未配置时不允许生成向量。"""
        del texts, text_type
        raise ValueError(f"Embedding 未配置：{self.reason}")


class ConfigEmbeddingProvider:
    """按当前配置懒创建真实 provider：每次调用前重建，配置热更新立即生效。

    首次启动没有 API Key 时核心照常启动（真实 provider 缺 key 时返回
    :class:`DisabledEmbeddingProvider`），用户在设置里配置后无需重启即可入库。
    """

    def __init__(self, get_settings: Callable[[], AppSettings]) -> None:
        self._get_settings = get_settings

    def embed_texts(self, texts: list[str], text_type: str = "document") -> list[list[float]]:
        """用当前最新配置创建 provider 并生成向量。"""
        return create_embedding_provider(self._get_settings()).embed_texts(texts, text_type=text_type)


class LangChainEmbeddingProvider:
    """langchain ``init_embeddings`` 通用包装（唯一真实 provider）。

    provider 即配置文件的 provider 字段，直接透传 langchain；
    ``base_url`` / ``api_key`` 同样透传，不做任何供应商特判。
    """

    _BATCH_SIZE = 25  # 通用分批：各家接口单批上限不同（如百炼兼容模式 25 条）

    def __init__(self, model: str, provider: str, base_url: str, api_key: str | None) -> None:
        from langchain.embeddings import init_embeddings

        kwargs: dict[str, Any] = {}
        if base_url:
            kwargs["base_url"] = base_url
        if api_key:
            kwargs["api_key"] = api_key
        self._embeddings = init_embeddings(model, provider=provider, **kwargs)

    def embed_texts(self, texts: list[str], text_type: str = "document") -> list[list[float]]:
        """按批向量化（单批条数不超过各家接口上限）。"""
        del text_type
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._BATCH_SIZE):
            vectors.extend(
                self._embeddings.embed_documents(texts[start : start + self._BATCH_SIZE])
            )
        return vectors


class MockEmbeddingProvider:
    """基于内容哈希的确定性 mock embedding，仅用于测试。"""

    def __init__(self, dimension: int = 64) -> None:
        self.dimension = dimension

    def embed_texts(self, texts: list[str], text_type: str = "document") -> list[list[float]]:
        """为每个文本生成归一化的确定性向量。"""
        del text_type
        return [self._embed_text(text) for text in texts]

    def _embed_text(self, text: str) -> list[float]:
        """把文本哈希展开成固定维度向量。"""
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        values = []
        for index in range(self.dimension):
            value = (digest[index % len(digest)] / 255.0) * 2.0 - 1.0
            values.append(value)
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values]


def create_embedding_provider(settings: AppSettings) -> EmbeddingProvider:
    """根据配置创建 embedding 提供商（mock 测试专用，其余走 init_embeddings）。

    初始化失败（缺 api_key、缺依赖包、provider 未注册）降级为
    :class:`DisabledEmbeddingProvider`，不阻断核心启动，原因原样透出。
    """
    provider = settings.embedding.provider
    if provider == "mock":
        return MockEmbeddingProvider(dimension=settings.embedding.dimension)
    if not provider:
        return DisabledEmbeddingProvider(
            "未配置 embedding provider（设置 → 模型设置 → Embedding）"
        )
    try:
        return LangChainEmbeddingProvider(
            model=settings.embedding.model,
            provider=provider,
            base_url=settings.embedding.base_url,
            api_key=settings.embedding.api_key,
        )
    except Exception as exc:  # noqa: BLE001 - 缺 key/缺依赖/未知 provider 降级禁用态
        return DisabledEmbeddingProvider(f"初始化 {provider} embedding 失败：{exc}")
