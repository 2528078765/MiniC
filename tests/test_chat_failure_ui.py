"""配置更新与聊天失败应在桌面端正确反映。"""

import json
import os
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def window(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from minic.gui.main_window import MainWindow

    application = QApplication.instance() or QApplication([])
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(MainWindow, "_connect_core", lambda self: None)
    value = MainWindow(workspace=str(tmp_path))
    yield value, home
    value.close()
    application.processEvents()


def test_models_refresh_after_rename_and_hide_disabled_models(window):
    value, home = window
    path = home / ".minic/minic.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"model": {"models": [
        {"name": "disabled", "model": "old", "enabled": False},
        {"name": "renamed", "model": "new", "enabled": True},
    ]}}), encoding="utf-8")
    value._saved_model = "removed"
    value._reload_models()
    assert value._model_combo.count() == 1
    assert value._current_model() == "renamed"
    assert value._saved_model == "renamed"


def test_sse_error_stays_in_answer_and_saved_session(window):
    from minic.gui.widgets.message_bubble import MessageBubble

    value, _ = window
    bubble = MessageBubble("ai", "partial")
    value._flow_layout.addWidget(bubble)
    value._streams["session"] = {"bubble": bubble, "session": "session", "saved": False,
                                 "pending_retried": False, "tool_records": []}
    value._on_chat_event("error", {"_run": "session", "message": "provider rejected request"})
    assert "partial" in bubble.text()
    assert "provider rejected request" in bubble.text()
    assert "session" not in value._streams
    assert value._session_messages["session"][-1]["text"] == bubble.text()


def test_stream_http_failure_reports_response_detail(monkeypatch):
    from minic.gui.core_client import CoreClient

    original = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(400, json={
        "error": {"message": "model was removed"}}))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=transport, **kwargs))
    value = CoreClient()
    value.base_url = "http://127.0.0.1:8781"
    value.token = "test-token"
    errors = []
    value.chat_error.connect(lambda message, key: errors.append((message, key)))
    value.stream_chat(None, "workspace", "hello", "removed", run_key="session")
    assert errors == [("HTTP 400: model was removed", "session")]
