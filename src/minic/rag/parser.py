"""多格式文档解析，提取章节结构（Markdown/TXT/JSON/HTML/PDF）。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable


@dataclass
class MarkdownSection:
    """一个文档章节。

    ``page`` 仅 PDF 使用，表示该章节起始页码（1 基）；其余格式保持 None。
    """

    title: str
    section: str
    content: str
    page: int | None = None


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
# Bump when Markdown section extraction changes; persisted by RagStore.
MARKDOWN_PARSER_VERSION = "markdown-fences-v2"


def read_text_file(path: Path) -> str:
    """读取文本文件，优先 UTF-8（含 BOM），失败时回退 GBK。"""
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return path.read_text(encoding="gbk", errors="replace")


def parse_markdown(text: str, file_path: Path) -> list[MarkdownSection]:
    """按标题切分 Markdown，并返回章节列表。"""
    title = file_path.stem
    heading_stack: list[tuple[int, str]] = []
    sections: list[MarkdownSection] = []
    current_lines: list[str] = []
    fence: tuple[str, int] | None = None

    def flush() -> None:
        """把当前标题下的文本保存为章节。"""
        content = "\n".join(current_lines).strip("\r\n")
        current_lines.clear()
        if not content.strip():
            return
        section_name = " > ".join(heading for _, heading in heading_stack) or "文档开头"
        sections.append(MarkdownSection(title=title, section=section_name, content=content))

    for line in text.splitlines():
        marker = _FENCE_RE.match(line)
        if fence is not None:
            current_lines.append(line)
            if (marker and marker[1][0] == fence[0]
                    and len(marker[1]) >= fence[1] and not marker[2].strip()):
                fence = None
            continue
        if marker and (marker[1][0] != "`" or "`" not in marker[2]):
            fence = (marker[1][0], len(marker[1]))
            current_lines.append(line)
            continue
        heading_match = _HEADING_RE.match(line)
        if heading_match:
            flush()
            level = len(heading_match.group(1))
            heading = heading_match.group(2).strip()
            heading_stack = [item for item in heading_stack if item[0] < level]
            heading_stack.append((level, heading))
            if level == 1:
                title = heading
            continue
        current_lines.append(line)
    flush()
    return sections


def parse_txt(text: str, file_path: Path) -> list[MarkdownSection]:
    """把纯文本整篇作为一个章节（无章节结构）。"""
    content = text.strip()
    if not content:
        return []
    return [MarkdownSection(title=file_path.stem, section="文档开头", content=content)]


def parse_json(text: str, file_path: Path) -> list[MarkdownSection]:
    """按 JSON 顶层 key 切分章节；顶层非 object 时整篇按纯文本处理。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        return parse_txt(text, file_path)
    title = file_path.stem
    sections: list[MarkdownSection] = []
    for key, value in data.items():
        sections.append(
            MarkdownSection(
                title=title,
                section=str(key),
                content=json.dumps(value, ensure_ascii=False),
            )
        )
    return sections


class _HtmlSectionParser(HTMLParser):
    """标准库 HTMLParser 子类：h1-h6 切章节，p/li 作正文，其余标签丢弃。"""

    _HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
    _BODY_TAGS = frozenset({"p", "li"})
    _SKIP_TAGS = frozenset({"script", "style"})

    def __init__(self, title: str) -> None:
        super().__init__(convert_charrefs=True)
        self.title = title
        self.heading_stack: list[tuple[int, str]] = []
        self.sections: list[MarkdownSection] = []
        self._heading_level = 0
        self._heading_parts: list[str] = []
        self._body_depth = 0
        self._skip_depth = 0
        self._current_lines: list[str] = []

    def flush(self) -> None:
        """把当前标题下的正文保存为章节。"""
        content = "\n".join(self._current_lines).strip()
        self._current_lines.clear()
        if not content:
            return
        section_name = " > ".join(heading for _, heading in self.heading_stack) or "文档开头"
        self.sections.append(MarkdownSection(title=self.title, section=section_name, content=content))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._HEADING_TAGS:
            self._heading_level = int(tag[1])
            self._heading_parts = []
        elif tag in self._BODY_TAGS:
            self._body_depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if tag in self._HEADING_TAGS:
            heading = " ".join("".join(self._heading_parts).split())
            if heading:
                self.flush()
                self.heading_stack = [
                    item for item in self.heading_stack if item[0] < self._heading_level
                ]
                self.heading_stack.append((self._heading_level, heading))
                if self._heading_level == 1:
                    self.title = heading
            self._heading_level = 0
            self._heading_parts = []
            return
        if tag in self._BODY_TAGS:
            if self._body_depth > 0:
                self._body_depth -= 1
                self._current_lines.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._heading_level:
            self._heading_parts.append(data)
        elif self._body_depth > 0:
            self._current_lines.append(data)


def parse_html(text: str, file_path: Path) -> list[MarkdownSection]:
    """用 HTMLParser 提取 h1-h6 标题与 p/li 正文，映射为章节列表。"""
    parser = _HtmlSectionParser(file_path.stem)
    parser.feed(text)
    parser.close()
    parser.flush()
    return parser.sections


def _destination_page_number(reader: Any, destination: Any) -> int | None:
    """解析书签目的地对应的 0 基页码（兼容 pypdf 不同版本）。"""
    getter = getattr(reader, "get_destination_page_number", None)
    if getter is not None:
        try:
            return int(getter(destination))
        except Exception:  # noqa: BLE001 - 单个书签解析失败则忽略该字段
            pass
    page = getattr(destination, "page", None)
    if isinstance(page, int):
        return page
    return None


def _flatten_bookmarks(reader: Any) -> list[tuple[int, str]]:
    """把书签树展平为 (页码, 标题) 列表并按页码排序。"""
    try:
        outline = reader.outline
    except Exception:  # noqa: BLE001 - 书签读取失败时退化为按页切分
        outline = []
    bookmarks: list[tuple[int, str]] = []

    def walk(items: list[Any]) -> None:
        for item in items or []:
            if isinstance(item, list):
                walk(item)
                continue
            title = getattr(item, "title", None)
            page = _destination_page_number(reader, item)
            if title and page is not None:
                bookmarks.append((page, title.strip()))

    walk(outline)
    bookmarks.sort(key=lambda item: item[0])
    return bookmarks


def _has_text_layer(page_texts: list[str]) -> bool:
    """判断每页平均字符数是否达到可用阈值（>= 40，隐含非空）。"""
    if not page_texts:
        return False
    total = sum(len(text) for text in page_texts)
    return total / len(page_texts) >= 40


def _sections_from_pages(
    path: Path,
    page_texts: list[str],
    bookmarks: list[tuple[int, str]],
) -> list[MarkdownSection]:
    """按书签或按页把每页文本映射为章节（页码 1 基）。"""
    title = path.stem
    sections: list[MarkdownSection] = []
    if bookmarks:
        for index, (start_page, heading) in enumerate(bookmarks):
            end_page = bookmarks[index + 1][0] if index + 1 < len(bookmarks) else len(page_texts)
            content = "\n".join(page_texts[start_page:end_page]).strip()
            if content:
                sections.append(
                    MarkdownSection(
                        title=title,
                        section=heading,
                        content=content,
                        page=start_page + 1,
                    )
                )
        return sections
    for page_index, text in enumerate(page_texts):
        content = text.strip()
        if content:
            sections.append(
                MarkdownSection(
                    title=title,
                    section=f"第 {page_index + 1} 页",
                    content=content,
                    page=page_index + 1,
                )
            )
    return sections


def _extract_pdf_pypdf(path: Path) -> tuple[list[str], list[tuple[int, str]]]:
    """用 pypdf 提取每页文本与书签。"""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    page_texts = [page.extract_text() or "" for page in reader.pages]
    bookmarks = _flatten_bookmarks(reader)
    return page_texts, bookmarks


def _extract_pdf_pdfplumber(path: Path) -> list[str]:
    """用 pdfplumber 提取每页文本，并把表格渲染成可读文本行。"""
    import pdfplumber

    page_texts: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            parts: list[str] = []
            try:
                text = page.extract_text() or ""
            except Exception:  # noqa: BLE001 - 单页文本提取失败降级为空
                text = ""
            if text.strip():
                parts.append(text.strip())
            try:
                tables = page.extract_tables() or []
            except Exception:  # noqa: BLE001 - 表格提取失败只跳过表格
                tables = []
            for table in tables:
                for row in table or []:
                    cells = ["" if cell is None else str(cell).strip() for cell in (row or [])]
                    parts.append(" | ".join(cells))
            page_texts.append("\n".join(parts))
    return page_texts


def parse_pdf(path: Path) -> list[MarkdownSection]:
    """三级降级提取 PDF：pypdf → pdfplumber → 扫描件报错（本轮不 OCR）。"""
    page_texts, bookmarks = _extract_pdf_pypdf(path)
    if _has_text_layer(page_texts):
        return _sections_from_pages(path, page_texts, bookmarks)

    page_texts = _extract_pdf_pdfplumber(path)
    if _has_text_layer(page_texts):
        return _sections_from_pages(path, page_texts, [])

    raise ValueError("扫描件 PDF 无文本层，OCR 未启用")


_TEXT_PARSERS: dict[str, Callable[[str, Path], list[MarkdownSection]]] = {
    ".md": parse_markdown,
    ".txt": parse_txt,
    ".json": parse_json,
    ".html": parse_html,
}


def parse_document(path: Path) -> list[MarkdownSection]:
    """按文件后缀分发到对应解析器，返回章节列表。"""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return parse_pdf(path)
    parser = _TEXT_PARSERS.get(suffix)
    if parser is None:
        raise ValueError(f"不支持的文件类型: {suffix or path.name}")
    return parser(read_text_file(path), path)
