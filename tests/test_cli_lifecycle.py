"""minic 自动启动核心生命周期测试。"""

from __future__ import annotations

from pathlib import Path

from minic.cli import lifecycle
from minic.cli.lifecycle import CoreHandle, ensure_core
from minic.core import config as core_config


class FakeProcess:
    """模拟子进程。"""

    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.poll_result = None

    def poll(self) -> None:
        return self.poll_result

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> None:
        return None


def test_core_handle_only_stops_self_started() -> None:
    """只关闭由本次 CLI 启动的核心。"""
    process = FakeProcess()
    handle = CoreHandle(runtime={"port": 8765}, process=process, started_by_self=True)
    handle.stop()
    assert process.terminated

    process = FakeProcess()
    handle = CoreHandle(runtime={"port": 8765}, process=process, started_by_self=False)
    handle.stop()
    assert not process.terminated


def test_ensure_core_reuses_running_core(monkeypatch, tmp_path: Path) -> None:
    """核心已运行时直接连接，不启动新进程。"""
    runtime = {"port": 8765, "token": "test"}
    monkeypatch.setattr(lifecycle, "read_runtime", lambda: runtime)
    monkeypatch.setattr(lifecycle, "_is_healthy", lambda current: True)
    handle = ensure_core(tmp_path, tmp_path)
    assert handle.started_by_self is False
    assert handle.process is None


def test_ensure_core_starts_when_not_running(monkeypatch, tmp_path: Path) -> None:
    """核心未运行时后台启动并等待健康检查。"""
    runtime = {"port": 8765, "token": "test"}
    calls = {"read": 0, "healthy": 0}
    popen_kwargs: dict = {}
    popen_args: list = []

    def fake_read() -> dict:
        calls["read"] += 1
        return runtime

    def fake_healthy(current: dict) -> bool:
        calls["healthy"] += 1
        return calls["healthy"] > 1

    fake_process = FakeProcess()
    monkeypatch.setattr(lifecycle, "read_runtime", fake_read)
    monkeypatch.setattr(lifecycle, "_is_healthy", fake_healthy)

    def fake_popen(*args, **kwargs):
        popen_args.extend(args)
        popen_kwargs.update(kwargs)
        return fake_process

    monkeypatch.setattr(lifecycle.subprocess, "Popen", fake_popen)
    handle = ensure_core(tmp_path, tmp_path)
    assert handle.started_by_self is True
    assert handle.process is fake_process
    assert calls["read"] >= 2
    assert popen_kwargs["cwd"] == str(tmp_path)
    command = popen_args[0] if popen_args else None
    assert "--project" in command
    assert str(tmp_path) in command


def test_select_core_project_root_uses_writable_workspace(tmp_path: Path, monkeypatch) -> None:
    """工作区有 .minic/minic.json 且可写时使用工作区作为核心项目根。"""
    config_dir = tmp_path / ".minic"
    config_dir.mkdir()
    (config_dir / "minic.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(core_config, "_dir_is_writable", lambda path: True)
    assert lifecycle.select_core_project_root(tmp_path) == tmp_path.resolve()


def test_select_core_project_root_falls_back_without_config(tmp_path: Path) -> None:
    """工作区没有配置时回退到 MiniC 项目根。"""
    expected = Path(lifecycle.__file__).resolve().parents[3]
    assert lifecycle.select_core_project_root(tmp_path) == expected


def test_select_core_project_root_falls_back_when_not_writable(tmp_path: Path, monkeypatch) -> None:
    """工作区配置目录不可写时回退到 MiniC 项目根。"""
    config_dir = tmp_path / ".minic"
    config_dir.mkdir()
    (config_dir / "minic.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(core_config, "_dir_is_writable", lambda path: False)
    expected = Path(lifecycle.__file__).resolve().parents[3]
    assert lifecycle.select_core_project_root(tmp_path) == expected


def test_resolve_project_root_uses_writable_workspace(tmp_path: Path, monkeypatch) -> None:
    """resolve_project_root 在工作区可写时使用工作区。"""
    config_dir = tmp_path / ".minic"
    config_dir.mkdir()
    (config_dir / "minic.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(core_config, "_dir_is_writable", lambda path: True)
    fallback = tmp_path / "fallback"
    assert core_config.resolve_project_root(tmp_path, fallback) == tmp_path.resolve()


def test_resolve_project_root_falls_back_when_not_writable(tmp_path: Path, monkeypatch) -> None:
    """resolve_project_root 在工作区不可写时回退到 fallback。"""
    config_dir = tmp_path / ".minic"
    config_dir.mkdir()
    (config_dir / "minic.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(core_config, "_dir_is_writable", lambda path: False)
    fallback = tmp_path / "fallback"
    assert core_config.resolve_project_root(tmp_path, fallback) == fallback.resolve()


def test_resolve_project_root_falls_back_without_config(tmp_path: Path) -> None:
    """resolve_project_root 在工作区无配置时回退到 fallback。"""
    fallback = tmp_path / "fallback"
    assert core_config.resolve_project_root(tmp_path, fallback) == fallback.resolve()
