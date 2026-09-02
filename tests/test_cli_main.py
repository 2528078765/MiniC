"""minic chat 自动启动核心测试。"""

from __future__ import annotations

from types import SimpleNamespace

from minic.cli import main as cli_main


class FakeHandle:
    """测试用核心连接句柄。"""

    runtime = {"port": 8765, "token": "test"}

    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class FakeResponse:
    """模拟健康检查响应。"""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"pid": 1, "started_at": "2026-01-01", "version": "0.1.0"}


class FakeClient:
    """模拟 httpx 客户端。"""

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def get(self, *args, **kwargs) -> FakeResponse:
        return FakeResponse()


def test_cmd_chat_uses_ensure_core(monkeypatch, tmp_path) -> None:
    """minic chat 通过 ensure_core 自动启动/复用核心并进入会话。"""
    handle = FakeHandle()
    calls: dict = {}
    monkeypatch.chdir(tmp_path)

    def fake_ensure_core(project_root, workspace, preferred_port=None):
        calls["project_root"] = project_root
        calls["workspace"] = workspace
        return handle

    monkeypatch.setattr(cli_main, "ensure_core", fake_ensure_core)
    monkeypatch.setattr(cli_main.httpx, "Client", lambda *args, **kwargs: FakeClient())
    monkeypatch.setattr(cli_main, "_print_health", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli_main, "_run_session_loop", lambda **kwargs: calls.update(kwargs))

    cli_main.cmd_chat(SimpleNamespace(path=None, thread=None))

    assert calls["project_root"] == cli_main.select_core_project_root(tmp_path)
    assert calls["workspace"] == tmp_path.resolve()
    assert calls["path"] is None
    assert calls["thread_id"] is None
    assert handle.stopped is True
