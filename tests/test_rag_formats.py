"""多格式文档入库解析测试（TXT/JSON/HTML/PDF 解析 + 入库/检索接口）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from minic.rag.parser import parse_document, parse_html, parse_json, parse_pdf, parse_txt


HEADERS = {"Authorization": "Bearer test-token"}


def _build_text_pdf(text: str) -> bytes:
    """构造一个含可见文本层的单页 PDF 字节串（Type1 Helvetica，仅支持 ASCII）。"""
    escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    stream = f"BT /F1 24 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    buf = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(buf))
        buf += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_offset = len(buf)
    buf += f"xref\n0 {len(objects) + 1}\n".encode()
    buf += b"0000000000 65535 f \n"
    for offset in offsets:
        buf += f"{offset:010d} 00000 n \n".encode()
    buf += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode()
    return bytes(buf)


# ---------------------------------------------------------------- 纯文本解析

def test_parse_txt_single_section() -> None:
    """TXT 整篇映射为单 section，section 为「文档开头」，content 非空。"""
    sections = parse_txt("第一行\n第二行", Path("note.txt"))
    assert len(sections) == 1
    assert sections[0].section == "文档开头"
    assert sections[0].title == "note"
    assert sections[0].content == "第一行\n第二行"
    assert sections[0].page is None


# ---------------------------------------------------------------- JSON 解析

def test_parse_json_splits_by_top_level_keys() -> None:
    """JSON 按顶层 key 切分，section 顺序等于 key 顺序。"""
    sections = parse_json('{"z": 1, "a": {"n": 2}, "b": [1, 2, 3]}', Path("data.json"))
    assert [section.section for section in sections] == ["z", "a", "b"]
    assert sections[0].content == "1"
    assert sections[1].content == '{"n": 2}'
    assert sections[2].content == "[1, 2, 3]"


def test_parse_json_invalid_raises_value_error() -> None:
    """非法 JSON 抛 ValueError。"""
    with pytest.raises(ValueError):
        parse_json("{not valid", Path("bad.json"))


def test_parse_json_non_object_falls_back_to_txt() -> None:
    """顶层非 object 时整篇按纯文本处理。"""
    sections = parse_json("[1, 2, 3]", Path("list.json"))
    assert len(sections) == 1
    assert sections[0].section == "文档开头"
    assert sections[0].content == "[1, 2, 3]"


# ---------------------------------------------------------------- HTML 解析

def test_parse_html_headings_and_body() -> None:
    """h1 成为 section，p 文本进入 content；script/style 内容被丢弃。"""
    html = (
        "<style>body { color: red; }</style>"
        "<script>var secret = 12345;</script>"
        "<h1>第一章</h1><p>第一章正文内容</p>"
        "<h1>第二章</h1><p>第二章正文内容</p>"
    )
    sections = parse_html(html, Path("doc.html"))
    assert len(sections) == 2
    assert sections[0].section == "第一章"
    assert "第一章正文内容" in sections[0].content
    assert sections[1].section == "第二章"
    assert "第二章正文内容" in sections[1].content
    joined = "\n".join(section.section + section.content for section in sections)
    assert "secret" not in joined
    assert "12345" not in joined
    assert "color" not in joined


# ---------------------------------------------------------------- PDF 解析

def test_parse_pdf_extracts_text_layer(tmp_path: Path) -> None:
    """含文本层的 PDF 返回非空 section 且 page 为 1 基整数。"""
    text = (
        "MiniC multi-format document parsing test. This PDF contains a real text "
        "layer so that parse_pdf can extract it and verify page numbers."
    )
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(_build_text_pdf(text))

    sections = parse_pdf(pdf_path)
    assert sections
    assert sections[0].content.strip()
    assert all(isinstance(section.page, int) and section.page >= 1 for section in sections)


def test_parse_pdf_scanned_raises(tmp_path: Path) -> None:
    """无文本层的 PDF 抛「扫描件 PDF 无文本层」ValueError。"""
    pdf_path = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with pdf_path.open("wb") as file_handle:
        writer.write(file_handle)

    with pytest.raises(ValueError, match="扫描件"):
        parse_pdf(pdf_path)


# ---------------------------------------------------------------- 文档分发

def test_parse_document_dispatches_txt(tmp_path: Path) -> None:
    """parse_document 按 .txt 后缀分发到 parse_txt。"""
    path = tmp_path / "note.txt"
    path.write_text("正文内容", encoding="utf-8")
    sections = parse_document(path)
    assert len(sections) == 1
    assert sections[0].section == "文档开头"


def test_parse_document_unknown_suffix_raises(tmp_path: Path) -> None:
    """未知后缀抛 ValueError。"""
    path = tmp_path / "file.xyz"
    path.write_text("content", encoding="utf-8")
    with pytest.raises(ValueError, match="不支持的文件类型"):
        parse_document(path)


# ---------------------------------------------------------------- 入库接口

def test_ingest_txt_and_list_documents(client: TestClient, tmp_path: Path) -> None:
    """.txt 目录入库 ingested==1，文档清单 file_type 为 txt。"""
    folder = tmp_path / "txtdocs"
    folder.mkdir()
    (folder / "note.txt").write_text("龙井茶种植与品鉴说明。", encoding="utf-8")

    response = client.post(
        "/rag/ingest",
        headers=HEADERS,
        json={"path": str(folder), "extensions": [".txt"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["ingested"] == 1
    assert data["failed"] == []

    documents = client.get("/rag/documents", headers=HEADERS).json()["documents"]
    assert len(documents) == 1
    assert documents[0]["file_type"] == "txt"
    assert documents[0]["file_path"].endswith("note.txt")


def test_json_not_ingested_by_default(client: TestClient, tmp_path: Path) -> None:
    """.json 目录在不传 extensions 时不应被入库（默认扩展名不含 .json）。"""
    folder = tmp_path / "jsondocs"
    folder.mkdir()
    (folder / "data.json").write_text('{"a": 1}', encoding="utf-8")

    response = client.post("/rag/ingest", headers=HEADERS, json={"path": str(folder)})
    assert response.status_code == 200
    assert response.json()["ingested"] == 0

    documents = client.get("/rag/documents", headers=HEADERS).json()["documents"]
    assert documents == []


# ---------------------------------------------------------------- 检索来源

def test_query_hits_txt_source(client: TestClient, tmp_path: Path) -> None:
    """入库 .txt 后检索可命中，file_path 以 .txt 结尾且 section 非空。"""
    folder = tmp_path / "txtdocs"
    folder.mkdir()
    (folder / "note.txt").write_text("这是一份关于龙井茶种植与品鉴的说明文档。", encoding="utf-8")
    client.post(
        "/rag/ingest",
        headers=HEADERS,
        json={"path": str(folder), "extensions": [".txt"]},
    )

    response = client.get(
        "/rag/query",
        headers=HEADERS,
        params={"q": "龙井茶怎么品鉴", "top_k": 5},
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert results
    result = results[0]
    assert result["file_path"].endswith(".txt")
    assert result["section"]


# ---------------------------------------------------------------- 增量跳过

def test_reingest_txt_skips(client: TestClient, tmp_path: Path) -> None:
    """同一 .txt 目录重复 ingest，第二次 skipped>=1。"""
    folder = tmp_path / "txtdocs"
    folder.mkdir()
    (folder / "note.txt").write_text("龙井茶说明文档。", encoding="utf-8")
    payload = {"path": str(folder), "extensions": [".txt"]}

    first = client.post("/rag/ingest", headers=HEADERS, json=payload)
    assert first.json()["ingested"] == 1

    second = client.post("/rag/ingest", headers=HEADERS, json=payload)
    assert second.json()["ingested"] == 0
    assert second.json()["skipped"] >= 1
