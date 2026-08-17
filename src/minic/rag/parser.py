"""Markdown 解析，提取章节结构。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class MarkdownSection:
    """一个 Markdown 章节。"""

    title: str
    section: str
    content: str


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def parse_markdown(text: str, file_path: Path) -> list[MarkdownSection]:
    """按标题切分 Markdown，并返回章节列表。"""
    title = file_path.stem
    heading_stack: list[tuple[int, str]] = []
    sections: list[MarkdownSection] = []
    current_lines: list[str] = []

    def flush() -> None:
        """把当前标题下的文本保存为章节。"""
        content = "\n".join(current_lines).strip()
        current_lines.clear()
        if not content:
            return
        section_name = " > ".join(heading for _, heading in heading_stack) or "文档开头"
        sections.append(MarkdownSection(title=title, section=section_name, content=content))

    for line in text.splitlines():
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
