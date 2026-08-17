"""C5 子 Agent/MCP/SKILL 占位面板测试。"""

from __future__ import annotations

from pathlib import Path

from minic.cli.ui import (
    render_agent_panel,
    render_mcp_panel,
    render_skills_panel,
    render_status_bar,
)
from minic.core.config import AppSettings


def test_render_agent_panel_empty() -> None:
    """子 Agent 面板无任务时显示空状态。"""
    panel = render_agent_panel([])
    assert "── 子 Agent ──" in panel
    assert "并发 0/3" in panel
    assert "（暂无子任务）" in panel


def test_render_agent_panel_real() -> None:
    """子 Agent 面板渲染真实任务状态。"""
    panel = render_agent_panel(
        [
            {"subagent_id": "a-1", "status": "completed", "output_summary": "子任务完成"},
            {"subagent_id": "a-2", "status": "failed", "output_summary": "子任务超时"},
        ],
        max_concurrent=5,
    )
    assert "── 子 Agent ──" in panel
    assert "并发 2/5" in panel
    assert "a-1 [完成] 子任务完成" in panel
    assert "a-2 [失败] 子任务超时" in panel


def test_render_mcp_panel() -> None:
    """MCP 面板显示空服务状态。"""
    assert "（未配置 MCP 服务）" in render_mcp_panel()


def test_render_skills_panel() -> None:
    """SKILL 面板显示空技能状态。"""
    assert "（未配置 SKILL）" in render_skills_panel()


def test_status_bar_contains_agents_hint() -> None:
    """状态栏包含 ← for agents 提示。"""
    bar = render_status_bar(AppSettings(), Path("C:/workspace"))
    assert "← for agents" in bar
