"""Embedding 测试：langchain init_embeddings 通用包装（透传/分批/降级/工厂）。"""

from __future__ import annotations

from unittest import mock

from minic.core.config import AppSettings
from minic.rag.embeddings import (
    DisabledEmbeddingProvider,
    LangChainEmbeddingProvider,
    create_embedding_provider,
)


class _FakeEmbeddings:
    """langchain init_embeddings 返回的假嵌入模型。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[0.1, 0.2] for _ in texts]


def test_langchain_provider_delegates_embed_documents() -> None:
    """包装：embed_texts 委托 init_embeddings 产物，参数全部透传。"""
    fake = _FakeEmbeddings()
    with mock.patch("langchain.embeddings.init_embeddings", return_value=fake) as init:
        provider = LangChainEmbeddingProvider(
            model="text-embedding-v3",
            provider="openai",
            base_url="https://example.com/v1",
            api_key="sk",
        )
        vectors = provider.embed_texts(["a", "b"])

    assert init.call_args.args[0] == "text-embedding-v3"
    assert init.call_args.kwargs["provider"] == "openai"
    assert init.call_args.kwargs["base_url"] == "https://example.com/v1"
    assert init.call_args.kwargs["api_key"] == "sk"
    assert fake.calls == [["a", "b"]]
    assert vectors == [[0.1, 0.2], [0.1, 0.2]]


def test_langchain_provider_batches_generic_limit() -> None:
    """超过 25 条按 25 一批拆分（各家接口单批上限不同，统一通用分批）。"""
    fake = _FakeEmbeddings()
    with mock.patch("langchain.embeddings.init_embeddings", return_value=fake):
        provider = LangChainEmbeddingProvider(
            model="m", provider="openai", base_url="https://example.com/v1", api_key="sk"
        )
        texts = [f"t{i}" for i in range(60)]
        vectors = provider.embed_texts(texts)

    assert [len(batch) for batch in fake.calls] == [25, 25, 10]
    assert len(vectors) == 60


def test_langchain_provider_omits_empty_base_url_and_key() -> None:
    """base_url/api_key 为空时不透传（用 provider 默认值或环境变量）。"""
    fake = _FakeEmbeddings()
    with mock.patch("langchain.embeddings.init_embeddings", return_value=fake) as init:
        LangChainEmbeddingProvider(model="m", provider="ollama", base_url="", api_key=None)

    assert "base_url" not in init.call_args.kwargs
    assert "api_key" not in init.call_args.kwargs


def test_factory_supports_openai_provider() -> None:
    """工厂：openai provider 返回 langchain 包装。"""
    settings = AppSettings(
        embedding={"provider": "openai", "base_url": "https://example.com/v1", "model": "m", "api_key": "sk"}
    )
    provider = create_embedding_provider(settings)
    assert isinstance(provider, LangChainEmbeddingProvider)


def test_factory_missing_api_key_returns_disabled() -> None:
    """缺 api_key：openai provider 构造抛 OpenAIError → 降级禁用态不崩核心。"""
    settings = AppSettings(
        embedding={"provider": "openai", "base_url": "https://example.com/v1", "model": "m", "api_key": None}
    )
    provider = create_embedding_provider(settings)
    assert isinstance(provider, DisabledEmbeddingProvider)


def test_factory_unknown_provider_returns_disabled() -> None:
    """langchain 不认识的 provider：降级为禁用态而不是崩溃。"""
    settings = AppSettings(
        embedding={"provider": "no-such-provider", "base_url": "https://example.com", "model": "m", "api_key": "sk"}
    )
    provider = create_embedding_provider(settings)
    assert isinstance(provider, DisabledEmbeddingProvider)


def test_factory_empty_provider_returns_disabled() -> None:
    """0.0.2 默认全空：provider 为空时降级为禁用态并给出配置指引。"""
    settings = AppSettings(embedding={"provider": "", "base_url": "", "model": "", "api_key": None})
    provider = create_embedding_provider(settings)
    assert isinstance(provider, DisabledEmbeddingProvider)
    assert "设置 → 模型设置 → Embedding" in provider.reason
