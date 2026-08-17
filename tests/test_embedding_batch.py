"""DashScope embedding 批量限制测试：单次请求最多 10 条文本。"""

from __future__ import annotations

from unittest import mock

from minic.rag.embeddings import DashScopeEmbeddingProvider


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
