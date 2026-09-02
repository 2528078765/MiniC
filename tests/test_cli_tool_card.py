"""C2 工具卡片与来源块测试。"""

from __future__ import annotations

from minic.cli.ui import render_answer_block, render_sources, render_tool_card


def test_render_tool_card_completed() -> None:
    """工具卡片包含工具名、参数和完成状态。"""
    card = render_tool_card(
        "Read",
        {"path": "README.md"},
        "完成",
        output="内容",
    )
    assert "✦ Read" in card
    assert "path: README.md" in card
    assert "── 完成" in card
    assert "内容" in card


def test_render_tool_card_approval() -> None:
    """写工具需要审批时显示需要审批卡片。"""
    card = render_tool_card("Write", {"path": "a.txt"}, "需要审批")
    assert "✦ Write" in card
    assert "── 需要审批" in card


def test_render_sources_block() -> None:
    """来源以独立块展示，不混入回答。"""
    sources = [
        {"file_path": "docs/a.md", "section": "章节一"},
        {"file_path": "docs/b.md", "section": "章节二"},
    ]
    block = render_sources(sources)
    assert "── 来源 ──" in block
    assert "docs/a.md [章节一]" in block
    assert "docs/b.md [章节二]" in block


def test_render_answer_block_plain() -> None:
    """回答块不包含工具卡片符号。"""
    block = render_answer_block("未检索到。")
    assert "── 回答 ──" in block
    assert "未检索到。" in block
    assert "✦" not in block
