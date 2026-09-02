"""把 RAG 向量数据从 Chroma 迁移到 Milvus Lite（或反向）。

- 仅迁移向量数据（``chroma/`` → ``milvus.db``）；
- ``metadata.json``（文档清单）与 ``bm25_index.json``（BM25 索引）与后端无关，原地保留；
- 幂等：目标后端为空库时直接写入，重复执行会按 chunk_id 覆盖，不会产生重复数据。

用法（先退出 MiniC / 停掉核心，避免文件锁冲突）::

    python scripts/migrate_rag_backend.py --rag-data "C:\\Users\\<你>\\AppData\\Roaming\\miniC\\rag-data" --to milvus

默认 rag-data 为 ``~/.minic/rag-data``。完成后在设置 → 知识库 → 向量后端选择 Milvus Lite。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from minic.rag.vector_stores import (  # noqa: E402 - 先改 sys.path 再导入
    ChromaVectorStore,
    MilvusVectorStore,
)


def _resolve_rag_data(raw: str | None) -> Path:
    """解析 rag-data 目录：命令行优先，缺省全局默认。"""
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home() / ".minic" / "rag-data"


def _open(rag_data: Path, backend: str):
    """打开指定后端（chroma 用 chroma/ 子目录，milvus 用 rag-data 根目录）。"""
    if backend == "milvus":
        return MilvusVectorStore(rag_data)
    return ChromaVectorStore(rag_data / "chroma")


def migrate(rag_data: Path, source: str, target: str) -> int:
    """从 source 后端导出全部向量并写入 target 后端，返回迁移条数。"""
    src_store = _open(rag_data, source)
    try:
        rows = src_store.export_all()
    finally:
        src_store.close()
    if not rows:
        print(f"源后端 {source} 无数据，无需迁移（或已迁移过）")
        return 0
    dst_store = _open(rag_data, target)
    try:
        dst_store.add(
            ids=[row["id"] for row in rows],
            texts=[row["text"] for row in rows],
            embeddings=[row["vector"] for row in rows],
            metadatas=[
                {key: value for key, value in row.items() if key not in ("id", "text", "vector")}
                for row in rows
            ],
        )
    finally:
        dst_store.close()
    print(f"已从 {source} 迁移 {len(rows)} 条向量到 {target}（metadata.json / bm25_index.json 原地保留）")
    return len(rows)


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="迁移 MiniC RAG 向量后端（Chroma ⇄ Milvus Lite）")
    parser.add_argument("--rag-data", default=None, help="rag-data 目录（缺省 ~/.minic/rag-data）")
    parser.add_argument(
        "--to",
        choices=["milvus", "chroma"],
        required=True,
        help="目标后端：milvus（Chroma→Milvus Lite）或 chroma（Milvus→Chroma）",
    )
    args = parser.parse_args()

    rag_data = _resolve_rag_data(args.rag_data)
    if not rag_data.exists():
        print(f"rag-data 目录不存在：{rag_data}")
        return 1
    source = "chroma" if args.to == "milvus" else "milvus"
    migrate(rag_data, source=source, target=args.to)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
