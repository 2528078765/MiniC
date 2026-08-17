"""DashScope / OpenAI 兼容 embedding 测试：批量限制、路径归一化与工厂。"""

from __future__ import annotations

from unittest import mock

from minic.core.config import AppSettings
from minic.rag.embeddings import (
    DashScopeEmbeddingProvider,
    DisabledEmbeddingProvider,
    LangChainEmbeddingProvider,
    create_embedding_provider,
)


class _FakeResponse:
    """按请求文本数返回等量向量的假响应。"""

    def __init__(self, count: int) -> None:
        self._count = count

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "output": {
                "embeddings": [{"embedding": [0.1] * 8} for _ in range(self._count)]
            }
        }


class _FakeClient:
    """记录每次请求文本数的假 httpx.Client。"""

    def __init__(self, *args, **kwargs) -> None:
        self.batch_sizes: list[int] = []

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *args) -> None:
        return None

    def post(self, endpoint: str, headers=None, json=None) -> _FakeResponse:
        del endpoint, headers
        texts = json["input"]["texts"]
        self.batch_sizes.append(len(texts))
        return _FakeResponse(len(texts))


def test_dashscope_batches_large_inputs_into_groups_of_ten() -> None:
    """25 条文本应拆成 10/10/5 三次请求，向量按顺序拼回。"""
    provider = DashScopeEmbeddingProvider(
        api_key="sk", model="text-embedding-v3", base_url="https://example.com"
    )
    client = _FakeClient()
    with mock.patch("minic.rag.embeddings.httpx.Client", return_value=client):
        vectors = provider.embed_texts([f"文本{i}" for i in range(25)])

    assert client.batch_sizes == [10, 10, 5]
    assert len(vectors) == 25
    assert all(len(vector) == 8 for vector in vectors)


def test_dashscope_single_batch_shortcuts() -> None:
    """不超过 10 条只发一次请求。"""
    provider = DashScopeEmbeddingProvider(
        api_key="sk", model="text-embedding-v3", base_url="https://example.com"
    )
    client = _FakeClient()
    with mock.patch("minic.rag.embeddings.httpx.Client", return_value=client):
        vectors = provider.embed_texts(["a", "b", "c"])

    assert client.batch_sizes == [3]
    assert len(vectors) == 3


def test_dashscope_base_url_strips_api_v1_suffix() -> None:
    """base_url 误填 /api/v1 时归一化到服务根，不产生重复路径。"""
    provider = DashScopeEmbeddingProvider(
        api_key="sk", model="text-embedding-v3", base_url="https://example.com/api/v1"
    )
    assert provider.endpoint == (
        "https://example.com/api/v1/services/embeddings/text-embedding/text-embedding"
    )


class _FakeEmbeddings:
    """langchain init_embeddings 返回的假嵌入模型。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[0.1, 0.2] for _ in texts]


def test_langchain_provider_delegates_embed_documents() -> None:
    """langchain 包装：embed_texts 委托 init_embeddings 产物。"""
    fake = _FakeEmbeddings()
    with mock.patch("langchain.embeddings.init_embeddings", return_value=fake) as init:
        provider = LangChainEmbeddingProvider(
            model="qwen3-embedding",
            provider="openai",
            base_url="https://example.com/v1",
            api_key="sk",
        )
        vectors = provider.embed_texts(["a", "b"])

    assert init.call_args.args[0] == "qwen3-embedding"
    assert init.call_args.kwargs["provider"] == "openai"
    assert init.call_args.kwargs["base_url"] == "https://example.com/v1"
    assert init.call_args.kwargs["api_key"] == "sk"
    assert fake.calls == [["a", "b"]]
    assert vectors == [[0.1, 0.2], [0.1, 0.2]]


def test_langchain_provider_strips_provider_prefix() -> None:
    """支持文档里的 provider:model 前缀写法（openai:Pro/BAAI/bge-m3）。"""
    fake = _FakeEmbeddings()
    with mock.patch("langchain.embeddings.init_embeddings", return_value=fake) as init:
        LangChainEmbeddingProvider(
            model="openai:Pro/BAAI/bge-m3",
            provider="openai",
            base_url="https://example.com/v1",
            api_key="sk",
        )
    assert init.call_args.args[0] == "Pro/BAAI/bge-m3"  # 前缀已剥离


def test_factory_supports_openai_provider() -> None:
    """工厂支持 openai 兼容 provider；缺 api_key 时降级为禁用态。"""
    settings = AppSettings(
        embedding={"provider": "openai", "base_url": "https://example.com/v1", "model": "m", "api_key": "sk"}
    )
    provider = create_embedding_provider(settings)
    assert isinstance(provider, LangChainEmbeddingProvider)

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
