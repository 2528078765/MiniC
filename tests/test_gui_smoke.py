"""MiniC 桌面端（PySide6）冒烟测试：窗口/面板可创建、核心控件行为。

隔离说明：全部用例使用 offscreen 平台（模块导入时设置），HOME 用 tmp_path
monkeypatch 隔离，不触碰真实 ~/.minic；gui 模块导入不创建 QApplication。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    """会话级 QApplication（offscreen）。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 Path.home() 指向临时目录，隔离真实 ~/.minic。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


# ---------------------------------------------------------------- 主题

def test_theme_qss_nonempty():
    """全局 QSS 非空且包含天蓝强调色。"""
    from minic.gui.theme import QSS, COLOR_ACCENT

    assert isinstance(QSS, str) and QSS.strip()
    assert COLOR_ACCENT == "#4fc3f7"
    assert COLOR_ACCENT in QSS


# ---------------------------------------------------------------- CoreClient

def test_core_client_construct():
    """CoreClient 可构造，关键信号存在。"""
    from minic.gui.core_client import CoreClient

    client = CoreClient()
    assert client.base_url is None
    assert client.token is None
    for signal_name in (
        "connected",
        "connection_failed",
        "unauthorized",
        "chat_event",
        "chat_error",
        "request_error",
    ):
        assert hasattr(client, signal_name), f"缺少信号 {signal_name}"


def test_core_client_connect_without_runtime(isolated_home: Path, qapp, monkeypatch):
    """无 runtime.json 时 connect_core 返回 False 且不抛异常。"""
    from minic.gui.core_client import CoreClient

    failures: list[str] = []
    client = CoreClient()
    client.connection_failed.connect(lambda message: failures.append(message))
    ok = client.connect_core()
    assert ok is False
    assert failures and "runtime.json" in failures[0]


# ---------------------------------------------------------------- 主窗口

def test_main_window_creates(qapp, isolated_home: Path):
    """主窗口可创建并显示，包含侧栏/顶栏/欢迎页。"""
    from PySide6.QtTest import QTest

    from minic.gui.main_window import MainWindow

    window = MainWindow(workspace=str(isolated_home))
    window.show()
    assert window.isVisible()
    assert window.title_bar is not None
    assert window._chat_stack.count() == 3          # 欢迎页 + 对话流 + 设置页
    assert window._chat_stack.currentIndex() == 0   # 初始为欢迎页
    assert window._input_mode == "welcome"          # 唯一输入框挂在欢迎页组内
    assert window.client is not None
    QTest.qWait(200)  # 等待核心连接 worker 结束
    window.close()


def test_main_window_new_task_and_add_project(qapp, isolated_home: Path):
    """侧栏新建任务/添加项目（工作区）/新建会话（第一句话创建）不崩溃。"""
    from PySide6.QtTest import QTest

    from minic.gui.main_window import MainWindow

    window = MainWindow(workspace=str(isolated_home))
    window.show()
    window._new_task()
    assert window._chat_stack.currentIndex() == 0
    # 模拟新建项目（避免 QFileDialog）：加入树顶层并选中
    window._projects["测试项目"] = []
    window._project_empty.hide()
    window._add_workspace_item("测试项目")
    window._select_project("测试项目")
    assert window._current_project == "测试项目"
    assert not window._project_empty.isVisible()
    assert window._project_tree.topLevelItemCount() == 1
    # 新建会话：回到欢迎页等待第一句话，不立即创建选项卡
    window._new_session_for("测试项目")
    assert window._projects["测试项目"] == []
    assert window._chat_stack.currentIndex() == 0
    assert window._pending_session_ws == "测试项目"
    top = window._project_tree.topLevelItem(0)
    assert top.childCount() == 0
    # 发送第一句话 → 创建会话选项卡（名 = 第一句话截断）
    window.client.base_url = "http://127.0.0.1:1"
    window.client.stream_chat = lambda *args, **kwargs: None
    window._input.setPlainText("帮我看看桌面有没有 test.txt 文件")
    window._send_message()
    assert window._projects["测试项目"] == ["帮我看看桌面有没有 te…"]
    assert top.childCount() == 1
    assert window._pending_session_ws is None
    QTest.qWait(200)
    window.close()


def test_main_window_first_message_goes_to_task(qapp, isolated_home: Path):
    """刚进桌面端直接发消息：自动创建任务会话并挂到任务区。"""
    from PySide6.QtTest import QTest

    from minic.gui.main_window import MainWindow

    window = MainWindow(workspace=str(isolated_home))
    window.show()
    assert window._current_session is None  # 初始无会话归属
    assert window._task_sessions == []
    window.client.base_url = "http://127.0.0.1:1"
    window.client.stream_chat = lambda *args, **kwargs: None
    window._input.setPlainText("帮我整理一下笔记")
    window._send_message()
    assert window._task_sessions == ["帮我整理一下笔记"]
    key = window._task_key("帮我整理一下笔记")
    assert window._current_session == key  # 流已归属任务会话
    assert ("user", "帮我整理一下笔记") in window._session_messages.get(key, [])
    assert window.title_bar._title_label.text() == "任务"
    QTest.qWait(200)
    window.close()


def test_main_window_model_combo(qapp, isolated_home: Path):
    """输入框右下角模型下拉框：列出配置模型，选中后切换生效并持久化。"""
    import json

    from PySide6.QtTest import QTest

    from minic.gui.main_window import MainWindow

    (isolated_home / ".minic").mkdir(exist_ok=True)
    (isolated_home / ".minic" / "minic.json").write_text(
        json.dumps(
            {
                "model": {
                    "models": [
                        {"name": "DeepSeek", "provider": "deepseek", "base_url": "https://api.deepseek.com", "model": "deepseek-v4-flash", "enabled": True},
                        {"name": "QWen", "provider": "dashscope", "base_url": "https://dashscope.aliyuncs.com", "model": "qwen-plus", "enabled": True},
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    window = MainWindow(workspace=str(isolated_home))
    window.show()
    assert window._model_combo.count() == 2
    assert window._current_model() == "DeepSeek"  # 默认第一项
    window._model_combo.setCurrentIndex(1)
    assert window._current_model() == "QWen"
    assert window._saved_model == "QWen"
    # 持久化后可恢复选中
    state_path = isolated_home / ".minic" / "gui_state.json"
    assert json.loads(state_path.read_text(encoding="utf-8"))["model"] == "QWen"
    # 设置里新增模型后，展开下拉前刷新列表（popup_opened 信号）
    (isolated_home / ".minic" / "minic.json").write_text(
        json.dumps(
            {
                "model": {
                    "models": [
                        {"name": "DeepSeek", "provider": "deepseek", "base_url": "https://api.deepseek.com", "model": "deepseek-v4-flash", "enabled": True},
                        {"name": "QWen", "provider": "dashscope", "base_url": "https://dashscope.aliyuncs.com", "model": "qwen-plus", "enabled": True},
                        {"name": "Kimi", "provider": "moonshot", "base_url": "https://api.moonshot.cn", "model": "moonshot-v1", "enabled": True},
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    window._model_combo.popup_opened.emit()
    assert window._model_combo.count() == 3
    assert window._current_model() == "QWen"  # 刷新后保持原选中
    QTest.qWait(200)
    window.close()


# ---------------------------------------------------------------- 设置窗口

def test_settings_window_creates(qapp):
    """设置窗口可创建，含 7 个面板与导航。"""
    from minic.gui.settings_window import SettingsWindow

    window = SettingsWindow()
    window.show()
    assert window._stack.count() == 7
    assert window._panel_name_to_index["skills"] == 3
    window.open_panel("skills")
    assert window._stack.currentIndex() == 3
    assert window._panel_name_to_item["memory"] is not None
    window.close()


def test_settings_page_creates(qapp):
    """内嵌设置页可创建：返回信号 + 7 面板 + 面板切换。"""
    from minic.gui.settings_window import SettingsPage

    page = SettingsPage()
    page.show()
    assert page._stack.count() == 7
    assert page._panel_name_to_index["memory"] == 2
    page.open_panel("skills")
    assert page._stack.currentIndex() == 3
    page.close()


def test_main_window_settings_inline(qapp, isolated_home: Path):
    """设置内嵌：不弹新窗口，切到设置页并返回。"""
    from minic.gui.main_window import MainWindow

    window = MainWindow(workspace=str(isolated_home))
    window.show()
    window._open_settings("skills")
    assert window._chat_stack.currentIndex() == 2          # 设置页
    assert window._settings_page._stack.currentIndex() == 3  # 技能面板
    assert not window._input_bar.isVisible()               # 设置页隐藏输入栏
    window._close_settings()
    assert window._chat_stack.currentIndex() == 0          # 回到欢迎页
    assert window._input_bar.isVisible()
    window.close()


def test_all_panels_creatable(qapp):
    """7 个设置面板均可独立创建。"""
    from minic.gui.panels import (
        KnowledgePanel,
        McpPanel,
        MemoryPanel,
        ModelPanel,
        SkillsPanel,
        SubagentsPanel,
        UsagePanel,
    )

    classes = (
        ModelPanel,
        KnowledgePanel,
        MemoryPanel,
        SkillsPanel,
        SubagentsPanel,
        McpPanel,
        UsagePanel,
    )
    for panel_class in classes:
        panel = panel_class(client=None, toast=lambda message: None)
        assert panel is not None
        panel.show()
        panel.close()


def test_model_panel_emb_key_echo(qapp, isolated_home: Path):
    """Embedding 配置（含 api_key）从本地 minic.json 回显到表单。"""
    import json

    from minic.gui.panels.model_panel import ModelPanel

    (isolated_home / ".minic").mkdir(exist_ok=True)
    minic_json = isolated_home / ".minic" / "minic.json"
    minic_json.write_text(
        json.dumps(
            {
                "model": {
                    "models": [
                        {
                            "name": "DeepSeek",
                            "provider": "deepseek",
                            "base_url": "https://api.deepseek.com",
                            "model": "deepseek-v4-flash",
                            "api_key": "sk-model-1",
                            "enabled": True,
                        }
                    ]
                },
                "embedding": {
                    "provider": "openai",
                    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "model": "text-embedding-v3",
                    "api_key": "sk-emb-1",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    panel = ModelPanel(client=None, toast=lambda message: None, workspace=str(isolated_home))
    panel.reload()
    assert panel._emb_provider.text() == "openai"
    assert panel._emb_model.text() == "text-embedding-v3"
    assert panel._emb_url.text() == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert panel._emb_key.text() == "sk-emb-1"
    panel.close()


def test_model_panel_name_provider_separate_and_delete(qapp, isolated_home: Path, monkeypatch):
    """模型面板：name/provider 独立字段回显；删除按钮移除并立即持久化剩余项。"""
    import json

    from PySide6.QtTest import QTest

    from minic.gui.panels.model_panel import ModelPanel

    (isolated_home / ".minic").mkdir(exist_ok=True)
    (isolated_home / ".minic" / "minic.json").write_text(
        json.dumps(
            {
                "model": {
                    "models": [
                        {"name": "我的DeepSeek", "provider": "deepseek", "base_url": "https://api.deepseek.com", "model": "deepseek-v4-flash", "enabled": True},
                        {"name": "百炼", "provider": "openai", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen3.7-flash", "enabled": True},
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    panel = ModelPanel(client=None, toast=lambda message: None, workspace=str(isolated_home))
    panel.reload()
    assert set(panel._providers) == {"我的DeepSeek", "百炼"}
    panel._select_provider("百炼")
    assert panel._name_input.text() == "百炼"
    assert panel._provider_input.text() == "openai"
    assert panel._url_input.text() == "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # 删除按钮：ConfirmDialog 直接确认 → 移除 + 提交剩余列表
    payloads: list[dict] = []

    class _FakeDialog:
        confirmed = True

        def exec(self) -> None:
            return None

    class _FakeClient:
        base_url = "http://127.0.0.1:1"

        def put_settings(self, payload: dict) -> bool:
            payloads.append(payload)
            return True

    monkeypatch.setattr("minic.gui.widgets.dialogs.ConfirmDialog", lambda **kwargs: _FakeDialog())
    panel.client = _FakeClient()
    panel._delete_provider("百炼")
    QTest.qWait(300)  # 等待提交 Worker 完成
    assert set(panel._providers) == {"我的DeepSeek"}
    assert len(payloads) == 1
    submitted = payloads[0]["model"]["models"]
    assert len(submitted) == 1
    assert submitted[0]["name"] == "我的DeepSeek"
    assert submitted[0]["provider"] == "deepseek"
    panel.close()


# ---------------------------------------------------------------- 复用控件

def test_toggle_switch_toggles(qapp):
    """ToggleSwitch 点击切换状态。"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from minic.gui.widgets.toggle import ToggleSwitch

    toggle = ToggleSwitch()
    assert not toggle.isChecked()
    QTest.mouseClick(toggle, Qt.MouseButton.LeftButton)
    assert toggle.isChecked()
    QTest.mouseClick(toggle, Qt.MouseButton.LeftButton)
    assert not toggle.isChecked()


def test_tool_card_status_update(qapp):
    """ToolCard 结果状态更新。"""
    from minic.gui.widgets.tool_card import ToolCard

    card = ToolCard("Read", {"path": "/tmp/a.md"}, status="pending")
    card.set_result("success", "file content")
    assert "完成" in card._status_label.text()


def test_message_bubble_append(qapp):
    """MessageBubble 流式追加。"""
    from minic.gui.widgets.message_bubble import MessageBubble

    bubble = MessageBubble("ai", "")
    bubble.append_text("这是")
    bubble.append_text("增量")
    assert bubble.text() == "这是增量"


def test_toast_show(qapp):
    """Toast 提示已禁用：show_message 不弹出浮层（右下角提示按用户要求去掉）。"""
    from PySide6.QtWidgets import QWidget

    from minic.gui.widgets.toast import Toast

    host = QWidget()
    host.show()
    toast = Toast(host)
    toast.show_message("测试提示")
    assert toast.isHidden()
    host.close()


def test_slash_menu_commands(qapp, isolated_home: Path):
    """输入 / 弹出命令菜单：过滤、上下选择、回车执行、Esc/普通文本关闭。"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from minic.gui.main_window import MainWindow

    window = MainWindow(workspace=str(isolated_home))
    window.show()
    menu = window._slash_menu
    assert not menu.isVisible()

    # 输入 / → 菜单显示全部命令
    window._input.setPlainText("/")
    QTest.qWait(50)
    assert menu.isVisible()
    assert menu._list.count() == 7

    # 前缀过滤 /新 → 新建与清空（说明含"新建"）
    window._input.setPlainText("/新")
    QTest.qWait(50)
    assert menu._list.count() == 2
    assert menu._list.item(0).text().startswith("/新建")

    # 上下键移动选择（键盘路径）
    QTest.keyClick(window._input, Qt.Key.Key_Down)
    assert menu._list.currentRow() == 1
    QTest.keyClick(window._input, Qt.Key.Key_Up)
    assert menu._list.currentRow() == 0

    # 回车执行选中命令 → 发 activated 信号并关闭菜单
    executed: list[str] = []
    menu.activated.connect(lambda cmd: executed.append(cmd))
    QTest.keyClick(window._input, Qt.Key.Key_Return)
    assert executed == ["/新建"]
    assert not menu.isVisible()

    # Esc 关闭
    window._input.setPlainText("/设置")
    QTest.qWait(50)
    assert menu.isVisible()
    QTest.keyClick(window._input, Qt.Key.Key_Escape)
    assert not menu.isVisible()

    # 普通文本隐藏菜单
    window._input.setPlainText("/设置")
    QTest.qWait(50)
    assert menu.isVisible()
    window._input.setPlainText("你好")
    QTest.qWait(50)
    assert not menu.isVisible()

    window.close()
