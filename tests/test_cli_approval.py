"""C3 审批行内交互测试。"""

from __future__ import annotations

from minic.cli.ui import normalize_approval_input, render_approval_menu


def test_approval_input_normalization() -> None:
    """审批输入支持英文、中文、数字和空输入。"""
    assert normalize_approval_input("allow_once") == "allow_once"
    assert normalize_approval_input("允许本次") == "allow_once"
    assert normalize_approval_input("1") == "allow_once"
    assert normalize_approval_input("") == "allow_once"
    assert normalize_approval_input("允许会话") == "allow_session"
    assert normalize_approval_input("2") == "allow_session"
    assert normalize_approval_input("始终允许") == "allow_always"
    assert normalize_approval_input("3") == "allow_always"
    assert normalize_approval_input("拒绝") == "deny"
    assert normalize_approval_input("4") == "deny"
    assert normalize_approval_input("unknown") is None


def test_render_approval_menu() -> None:
    """审批菜单包含四个选项和输入行。"""
    menu = render_approval_menu()
    assert "? 允许本次 / 允许会话 / 始终允许 / 拒绝" in menu
    assert "> " in menu
