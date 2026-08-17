"""C4 面板渲染测试。"""

from __future__ import annotations

from minic.cli.ui import render_memory_panel, render_rag_panel, render_thread_panel


def test_render_thread_panel() -> None:
    """会话面板包含编号、thread_id、名称和状态。"""
    panel = render_thread_panel(
        [
            {"thread_id": "t-1", "name": "会话一", "archived": False},
            {"thread_id": "t-2", "name": "会话二", "archived": True},
        ]
    )
    assert "── 会话列表 ──" in panel
    assert "1. t-1 会话一 [活跃]" in panel
    assert "2. t-2 会话二 [已归档]" in panel


def test_render_thread_panel_empty() -> None:
    """无会话时显示空状态。"""
    assert "（暂无会话）" in render_thread_panel([])


def test_render_memory_panel() -> None:
    """记忆面板包含全局、项目、合并三部分。"""
    panel = render_memory_panel("全局记忆", "项目记忆", "合并记忆")
    assert "── 长期记忆 ──" in panel
    assert "全局：" in panel
    assert "项目：" in panel
    assert "合并：" in panel
    assert "全局记忆" in panel
    assert "项目记忆" in panel
    assert "合并记忆" in panel


def test_render_memory_panel_empty() -> None:
    """无记忆时显示空状态。"""
    panel = render_memory_panel("", "", "")
    assert "（暂无记忆）" in panel


def test_render_rag_panel() -> None:
    """RAG 面板包含文档数、分块数、embedding 和最近入库时间。"""
    panel = render_rag_panel(
        {
            "total_documents": 3,
            "total_chunks": 12,
            "embedding_model": "dashscope/text-embedding-v3",
            "last_ingest_at": "2026-08-12T10:00:00+08:00",
        }
    )
    assert "── RAG 状态 ──" in panel
    assert "文档数: 3" in panel
    assert "分块数: 12" in panel
    assert "dashscope/text-embedding-v3" in panel


def test_render_rag_panel_empty() -> None:
    """无索引时显示空状态。"""
    panel = render_rag_panel({})
    assert "（暂无索引）" in panel
