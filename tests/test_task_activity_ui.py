"""执行过程、最终答复与历史记录使用同一真实事件状态。"""

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def window(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from minic.gui.main_window import MainWindow
    from minic.gui.widgets.message_bubble import MessageBubble
    from minic.gui.widgets.tool_group import ToolGroup

    app = QApplication.instance() or QApplication([])
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(MainWindow, "_connect_core", lambda self: None)
    value = MainWindow(workspace=str(tmp_path))
    value._current_session = "task::demo"
    value._chat_stack.setCurrentIndex(1)
    group = ToolGroup(parent=value._flow_container)
    bubble = MessageBubble("ai", "")
    value._flow_layout.addWidget(group)
    value._flow_layout.addWidget(bubble)
    group.begin()
    value._streams["task::demo"] = {
        "bubble": bubble, "tool_group": group, "session": "task::demo", "saved": False,
        "pending_retried": False, "pending_text": "整理文件", "tool_records": [], "tool_cards": {},
    }
    yield value, group, bubble, home
    for stream in list(value._streams):
        value._finish_chat(stream)
    value.close()
    app.processEvents()


def event(window, name, **data):
    window._on_chat_event(name, {"_run": "task::demo", **data})


def test_steps_expand_then_finish_with_separate_answer(window):
    value, group, bubble, home = window
    event(value, "token", delta="我先查看文件。", channel="activity")
    event(value, "tool_call", id="read-1", tool="Read", args={"path": "guide.md"})
    event(value, "tool_result", id="read-1", status="success", output="内容")
    event(value, "tool_call", id="write-2", tool="Write", args={"path": "summary.md"})
    event(value, "tool_result", id="write-2", status="denied", output="用户拒绝")
    assert not group._collapsed
    assert group._steps[0].status == "success"
    assert group._steps[1].status == "denied"
    assert not bubble.text()
    event(value, "answer", text="已查看文件。写入未获批准，你可以确认后继续。")
    event(value, "message_end", status="completed")
    event(value, "done", status="completed", sources=[])
    assert group._collapsed
    assert not group._timer.isActive()
    assert "1 未完成" in group._summary.text()
    assert bubble.text().startswith("已查看文件")
    assert "我先查看" not in bubble.text()
    records = value._session_messages["task::demo"]
    assert len(records) == 1
    assert records[0]["tools"][1]["status"] == "denied"
    assert records[0]["activity"]["items"][0]["text"] == "我先查看文件。"
    persisted = json.loads((home / ".minic/gui_state.json").read_text(encoding="utf-8"))
    assert persisted["session_messages"]["task::demo"][0]["activity"]["status"] == "completed"
    group._toggle()
    assert not group._collapsed


def test_background_tool_results_update_and_replay(window):
    value, group, _, _ = window
    value._current_session = "other-session"
    event(value, "tool_call", id="background", tool="Read", args={"path": "notes.md"})
    value._tool_cards.clear()  # 切换会话会清空旧的全局卡片索引。
    event(value, "tool_result", id="background", status="success", output="已读取")
    assert group._steps[0].status == "success"
    assert group.isHidden()
    event(value, "answer", text="读取完成")
    event(value, "message_end", status="completed")
    event(value, "done", status="completed", sources=[])
    saved = value._session_messages["task::demo"][0]
    value._append_replay_tool_group(saved["tools"], saved["activity"])
    from minic.gui.widgets.tool_group import ToolGroup
    replay = value._flow_container.findChildren(ToolGroup)[-1]
    assert replay._collapsed
    assert replay._steps[0].status == "success"
    assert replay._elapsed == saved["activity"]["elapsed"]


def test_failure_and_cancel_never_label_pending_steps_as_success(window):
    value, group, bubble, _ = window
    event(value, "tool_call", id="pending", tool="Bash", args={"command": "dir"})
    event(value, "error", message="连接已断开")
    assert group._status == "failed"
    assert group._steps[0].status == "interrupted"
    assert group._collapsed
    assert "连接已断开" in bubble.text()
    assert value._session_messages["task::demo"][0]["activity"]["status"] == "failed"


def test_replay_success_with_empty_output_is_not_running(window):
    value, _, _, _ = window
    value._append_replay_tool_group([{"id": "1", "tool": "Write", "args": {}, "status": "success", "output": ""}])
    from minic.gui.widgets.tool_group import ToolGroup
    replay = value._flow_container.findChildren(ToolGroup)[-1]
    assert replay._steps[0].status == "success"
    assert "1 完成" in replay._summary.text()


def test_final_text_is_not_duplicated_in_activity(window):
    value, group, bubble, _ = window
    event(value, "token", delta="已完成整理。", channel="activity")
    event(value, "answer", text="已完成整理。")
    assert not group.snapshot()["items"]
    assert bubble.text() == "已完成整理。"


def test_markdown_render_preserves_original_text(window):
    from PySide6.QtCore import Qt
    _, _, bubble, _ = window
    text = "**已完成**\n\n- 查看文件\n- 整理结果"
    bubble.set_streaming(True)
    bubble.set_text(text)
    assert bubble._label.textFormat() == Qt.TextFormat.PlainText
    bubble.set_streaming(False)
    assert bubble._label.textFormat() == Qt.TextFormat.RichText
    assert bubble.text() == text


def test_finish_duration_does_not_keep_increasing(window):
    _, group, _, _ = window
    group.finish("completed", elapsed=12.3)
    group.finish("completed")
    assert group._elapsed == 12.3


def test_cancelled_run_shows_a_result_and_closes_pending_step(window):
    value, group, bubble, _ = window
    event(value, "tool_call", id="cancelled", tool="Bash", args={"command": "dir"})
    event(value, "message_end", status="cancelled")
    event(value, "done", status="cancelled", sources=[])
    assert group._steps[0].status == "cancelled"
    assert group._status == "cancelled"
    assert "已取消" in bubble.text()
    assert value._session_messages["task::demo"][0]["text"] == bubble.text()


def test_unexpected_stream_end_preserves_interrupted_activity(window):
    value, group, bubble, _ = window
    event(value, "tool_call", id="lost", tool="Read", args={"path": "notes.md"})
    event(value, "__done__")
    assert group._status == "interrupted"
    assert group._steps[0].status == "interrupted"
    assert "未完成" in bubble.text()
    saved = value._session_messages["task::demo"][0]
    assert saved["activity"]["status"] == "interrupted"
    assert saved["tools"][0]["status"] == "interrupted"
