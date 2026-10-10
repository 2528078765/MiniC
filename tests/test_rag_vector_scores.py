"""Verify the backend distance contract and its effect on hybrid ranking."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from minic.core.config import AppSettings
from minic.rag.store import RagStore
from minic.rag.vector_stores import ChromaVectorStore, MilvusVectorStore


def _metadata(chunk_id: str) -> dict:
    return dict(chunk_id=chunk_id, doc_id=chunk_id, source="technical",
                file_path=f"{chunk_id}.md", title="shared", section="shared", text="shared")


class MilvusClientFixture:
    def __init__(self, similarities=(1.0, 0.8, 0.0, -1.0)):
        self.similarities = similarities
        self.requests = []

    def has_collection(self, name):
        return True

    def load_collection(self, name):
        pass

    def get_collection_stats(self, name):
        return {"row_count": len(self.similarities)}

    def search(self, name, **kwargs):
        self.requests.append(kwargs)
        return [[dict(distance=value, entity=dict(id=str(i), vector=[value], **_metadata(str(i))))
                 for i, value in enumerate(self.similarities)]]


def _milvus(similarities=(1.0, 0.8, 0.0, -1.0)):
    adapter = object.__new__(MilvusVectorStore)
    adapter._client = MilvusClientFixture(similarities)
    return adapter


@pytest.mark.parametrize("similarity", [1.0, 0.8, 0.0, -1.0])
def test_milvus_cosine_is_normalized_to_distance(similarity):
    hit = _milvus([similarity]).query([1.0, 0.0], 1)[0]
    assert hit.distance == pytest.approx(1.0 - similarity)
    assert hit.metadata == _metadata("0")


def test_backends_have_the_same_cosine_distance_contract():
    chroma = object.__new__(ChromaVectorStore)
    chroma._collection = SimpleNamespace(query=lambda **kwargs: dict(
        ids=[[str(i) for i in range(4)]], distances=[[0.0, 0.2, 1.0, 2.0]],
        metadatas=[[_metadata(str(i)) for i in range(4)]], documents=[["shared"] * 4]))
    actual = _milvus().query([1.0, 0.0], 4)
    expected = chroma.query([1.0, 0.0], 4)
    assert [h.distance for h in actual] == pytest.approx([h.distance for h in expected])
    assert [h.metadata for h in actual] == [h.metadata for h in expected]


def test_milvus_preserves_filter_and_limits():
    adapter = _milvus()
    adapter.query([1.0, 0.0], 4, source='notes"special')
    request = adapter._client.requests[0]
    assert request["filter"] == 'source == "notes\\"special"'
    assert request["limit"] == 4
    assert request["data"] == [[1.0, 0.0]]


@pytest.mark.parametrize("vector_weight", [0.3, 1.0])
def test_hybrid_ranking_prefers_similarity_with_equal_lexical_evidence(
    tmp_path: Path, monkeypatch, vector_weight
):
    adapter = _milvus()
    monkeypatch.setattr("minic.rag.store.create_vector_store", lambda *args: adapter)
    settings = AppSettings(model={"provider": "mock"},
                           embedding={"provider": "mock", "dimension": 2},
                           rag={"vector_backend": "milvus", "vector_weight": vector_weight,
                                "bm25_weight": 1.0 - vector_weight})
    embeddings = SimpleNamespace(embed_texts=lambda texts, **kwargs: [[1.0, 0.0]] * len(texts))
    store = RagStore(tmp_path, settings, embeddings)
    store._bm25_chunks = [_metadata(str(i)) for i in range(4)]
    monkeypatch.setattr(store, "_bm25_scores", lambda *args, **kwargs: [
        dict(chunk=chunk, score=0.5) for chunk in store._bm25_chunks])
    results = store.query("shared", top_k=4)
    assert [r.doc_id for r in results] == ["0", "1", "2", "3"]
    assert results[0].score == pytest.approx(round(vector_weight + (1-vector_weight)*0.5, 4))
    assert results[1].score < results[0].score
    assert results[2].score == results[3].score  # Negative similarity is not a positive match.
