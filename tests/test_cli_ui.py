"""CLI Welcome 面板与状态栏测试。"""

from __future__ import annotations

import re
from pathlib import Path

from minic.cli.main import _print_commands, _print_shortcuts
from minic.cli.ui import (
    WELCOME_FRAME_COLOR,
    WELCOME_LINE_COLOR,
    render_status_bar,
    render_welcome,
)
from minic.core.config import AppSettings


def _visible_width(line: str) -> int:
    """去掉 ANSI 转义后返回视觉宽度。"""
    return len(re.sub(r"\x1b\[[0-9;]*m", "", line))


def _settings() -> AppSettings:
    """返回测试配置。"""
    return AppSettings(
        model={"provider": "deepseek", "model": "deepseek-v4-flash"},
        embedding={"provider": "dashscope", "model": "text-embedding-v3"},
    )


def test_render_welcome_contains_sections() -> None:
    """Welcome 面板包含左右分栏和动态模型信息。"""
    settings = _settings()
    text = render_welcome(settings, Path("C:/workspace"), width=100)
    assert "Welcome back!" in text
    assert "Tips for getting started" in text
    assert "What's new" in text
    assert "deepseek-v4-flash" in text
    assert "workspace" in text
    assert "deepseek-v4-pro[1M]" not in text


def test_render_welcome_border_aligned() -> None:
    """固定宽度下，Welcome 面板每一行视觉宽度一致。"""
    text = render_welcome(_settings(), Path("C:/workspace"), width=100)
    widths = {_visible_width(line) for line in text.splitlines()}
    assert len(widths) == 1


def test_welcome_uses_sky_blue_constants() -> None:
    """Welcome 外框与 M Logo 用天蓝色，中间分隔线用更浅天蓝色。"""
    assert WELCOME_FRAME_COLOR != WELCOME_LINE_COLOR
    text = render_welcome(_settings(), Path("C:/workspace"), width=100)
    lines = text.splitlines()
    assert WELCOME_FRAME_COLOR in lines[0]
    assert WELCOME_FRAME_COLOR in lines[-1]
    assert WELCOME_LINE_COLOR in text
    logo_lines = [line for line in lines if "█" in line]
    assert logo_lines
    assert all(WELCOME_FRAME_COLOR in line for line in logo_lines)
    assert "\033[31m" not in text


def test_status_bar_dynamic_model_and_context() -> None:
    """状态栏模型和上下文长度动态读取，不写死。"""
    settings = _settings()
    bar = render_status_bar(settings, Path("C:/workspace"))
    assert "deepseek/deepseek-v4-flash[model]" in bar
    assert "manual mode on" in bar
    assert "deepseek-v4-pro[1M]" not in bar

    settings.model.models[0].context_length = 131072
    bar = render_status_bar(settings, Path("C:/workspace"))
    assert "deepseek/deepseek-v4-flash[131072]" in bar


def test_print_commands_wraps_description(capsys) -> None:
    """命令列表包含全部命令，长描述可换行缩进。"""
    _print_commands()
    output = capsys.readouterr().out
    assert "/memory" in output
    assert "/skills" in output
    assert "/mcp" in output
    assert "/agent" in output


def test_print_shortcuts(capsys) -> None:
    """快捷键帮助面板包含常用快捷键。"""
    _print_shortcuts()
    output = capsys.readouterr().out
    assert "快捷键帮助" in output
    assert "Ctrl+C" in output
    assert "Ctrl+L" in output
    assert "Enter" in output
