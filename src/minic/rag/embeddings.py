"""Embedding 提供商与工厂。"""

from __future__ import annotations

import hashlib
import math
from typing import Callable, Protocol

import httpx

from minic.core.config import AppSettings


class EmbeddingProvider(Protocol):
    """Embedding 提供商接口。"""

    def embed_texts(self, texts: list[str], text_type: str = "document") -> list[list[float]]:
        """把文本列表转换为向量列表。"""


class DisabledEmbeddingProvider:
    """未配置的 embedding：核心可正常启动，实际使用时报明确错误。"""

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


class OllamaEmbeddingProvider:
    """通过 Ollama 的 /api/embed 生成向量。"""

    def __init__(self, base_url: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model

    def embed_texts(self, texts: list[str], text_type: str = "document") -> list[list[float]]:
        """调用 Ollama 接口并返回向量。"""
        response = httpx.post(
            f"{self.base_url}/api/embed",
            json={"model": self.model, "input": texts},
            timeout=120.0,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data.get("embeddings"), list):
            return data["embeddings"]
        if isinstance(data.get("embedding"), list):
            return [data["embedding"]]
        raise ValueError("Ollama 返回的 embedding 格式不正确")


class LangChainEmbeddingProvider:
    """langchain ``init_embeddings`` 统一入口包装。

    与 chat 模型侧 ``init_chat_model`` 保持一致：openai 兼容等 langchain
    内建 provider（openai/azure/bedrock/cohere 等）都用它创建，支持
    ``provider:model`` 前缀写法（如 ``openai:Pro/BAAI/bge-m3``）。
    dashscope 原生与 ollama 因批量上限/依赖原因保留手写实现。
    """

    def __init__(self, model: str, provider: str, base_url: str, api_key: str | None) -> None:
        from langchain.embeddings import init_embeddings

        model_name = model
        if ":" in model_name and model_name.split(":", 1)[0] == provider:
            model_name = model_name.split(":", 1)[1]  # 兼容 "provider:model" 前缀写法
        kwargs: dict[str, Any] = {}
        if base_url:
            kwargs["base_url"] = base_url
        if api_key:
            kwargs["api_key"] = api_key
        self._embeddings = init_embeddings(model_name, provider=provider, **kwargs)

    def embed_texts(self, texts: list[str], text_type: str = "document") -> list[list[float]]:
        """批量向量化（委托 langchain ``embed_documents``）。"""
        del text_type
        return self._embeddings.embed_documents(texts)


class DashScopeEmbeddingProvider:
    """通过阿里云百炼的 text-embedding-v3 生成向量。"""

    def __init__(self, api_key: str, model: str, base_url: str) -> None:
        self.api_key = api_key
        self.model = model
        base = base_url.rstrip("/")
        if base.endswith("/api/v1"):  # 用户误填接口路径时归一化到服务根地址
            base = base[: -len("/api/v1")]
        self.endpoint = (
            f"{base}/api/v1/services/embeddings/text-embedding/text-embedding"
        )

    def embed_texts(self, texts: list[str], text_type: str = "document") -> list[list[float]]:
        """调用百炼接口并返回向量。

        百炼批量接口单次最多 10 条文本，超限返回 400（batch size is invalid,
        it should not be larger than 10），因此按 10 条一批循环调用。
        """
        embeddings: list[list[float]] = []
        batch_size = 10
        with httpx.Client(trust_env=False, timeout=120.0) as client:
            for start in range(0, len(texts), batch_size):
                batch = texts[start : start + batch_size]
                payload = {
                    "model": self.model,
                    "input": {"texts": batch},
                    "parameters": {"text_type": text_type},
                }
                response = client.post(
                    self.endpoint,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                embeddings.extend(
                    item["embedding"] for item in data["output"]["embeddings"]
                )
        return embeddings


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
    """根据配置创建 embedding 提供商。"""
    provider = settings.embedding.provider
    if provider == "mock":
        return MockEmbeddingProvider(dimension=settings.embedding.dimension)
    if provider == "ollama":
        return OllamaEmbeddingProvider(
            base_url=settings.embedding.base_url,
            model=settings.embedding.model,
        )
    if provider == "dashscope":
        if not settings.embedding.api_key:
            return DisabledEmbeddingProvider(
                "dashscope embedding 需要配置 api_key（设置 → 模型设置 → Embedding）"
            )
        return DashScopeEmbeddingProvider(
            api_key=settings.embedding.api_key,
            model=settings.embedding.model,
            base_url=settings.embedding.base_url,
        )
    # 其余 provider（openai、azure_openai、bedrock 等）走 langchain init_embeddings
    # 统一入口（与 chat 模型侧 init_chat_model 对齐）；初始化失败降级为禁用态，
    # 不阻断核心启动，调用时报明确原因。
    if not settings.embedding.api_key:
        return DisabledEmbeddingProvider(
            f"{provider} embedding 需要配置 api_key（设置 → 模型设置 → Embedding）"
        )
    try:
        return LangChainEmbeddingProvider(
            model=settings.embedding.model,
            provider=provider,
            base_url=settings.embedding.base_url,
            api_key=settings.embedding.api_key,
        )
    except Exception as exc:  # noqa: BLE001 - 未知 provider/缺依赖时降级禁用态
        return DisabledEmbeddingProvider(f"初始化 {provider} embedding 失败：{exc}")
