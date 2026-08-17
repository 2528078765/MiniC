"""全局命令安装与卸载测试。"""

from __future__ import annotations

import os
from pathlib import Path

from minic.cli import global_install


def _fake_venv(tmp_path: Path) -> Path:
    """构造带 minic 入口的假虚拟环境。"""
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    exe_name = "minic.exe" if os.name == "nt" else "minic"
    python_name = "python.exe" if os.name == "nt" else "python"
    (scripts / exe_name).write_bytes(b"")
    (scripts / python_name).write_bytes(b"")
    return scripts


def _patch_path(monkeypatch, entries: list[str]) -> None:
    """把用户 PATH 读写替换成内存列表。"""
    monkeypatch.setattr(global_install, "get_user_path", lambda: list(entries))

    def set_entries(new_entries: list[str]) -> None:
        entries.clear()
        entries.extend(new_entries)

    monkeypatch.setattr(global_install, "set_user_path", set_entries)


def test_install_global_idempotent_and_uninstall(monkeypatch, tmp_path: Path) -> None:
    """全局安装幂等，卸载后移除本次添加的 PATH 条目与 shim。"""
    scripts = _fake_venv(tmp_path)
    home = tmp_path / "home"
    path_entries: list[str] = []
    notify_calls: list[int] = []
    _patch_path(monkeypatch, path_entries)
    monkeypatch.setattr(global_install, "_notify_environment_change", lambda: notify_calls.append(1))

    first = global_install.install_global(scripts, home=home)
    bin_dir = home / ".minic" / "bin"
    marker = home / ".minic" / "global-install.json"
    assert len(path_entries) == 2
    assert str(scripts) in path_entries
    assert str(bin_dir) in path_entries
    cmd_name = "minic.cmd" if os.name == "nt" else "minic"
    assert (bin_dir / cmd_name).exists()
    assert marker.exists()
    assert len(notify_calls) == 1

    second = global_install.install_global(scripts, home=home)
    assert len(path_entries) == 2
    assert second["added"] == []
    assert len(notify_calls) == 1

    result = global_install.uninstall_global(home=home)
    assert len(result["removed"]) == 2
    assert str(scripts) not in path_entries
    assert str(bin_dir) not in path_entries
    cmd_name = "minic.cmd" if os.name == "nt" else "minic"
    assert not (bin_dir / cmd_name).exists()
    assert not marker.exists()
    assert len(notify_calls) == 2


def test_uninstall_global_preserves_unrelated_entries(monkeypatch, tmp_path: Path) -> None:
    """卸载只移除本次安装添加的条目，保留用户原有 PATH。"""
    scripts = _fake_venv(tmp_path)
    home = tmp_path / "home"
    path_entries: list[str] = []
    _patch_path(monkeypatch, path_entries)
    monkeypatch.setattr(global_install, "_notify_environment_change", lambda: None)
    global_install.install_global(scripts, home=home)
    path_entries.append("C:\\Other")

    result = global_install.uninstall_global(home=home)
    assert len(result["removed"]) == 2
    assert "C:\\Other" in path_entries
    assert str(scripts) not in path_entries


def test_uninstall_global_without_marker_is_noop(monkeypatch, tmp_path: Path) -> None:
    """没有安装记录时卸载不修改 PATH。"""
    path_entries = ["C:\\Other"]
    _patch_path(monkeypatch, path_entries)
    monkeypatch.setattr(global_install, "_notify_environment_change", lambda: None)
    result = global_install.uninstall_global(home=tmp_path / "home")
    assert result["removed"] == []
    assert path_entries == ["C:\\Other"]


def test_install_global_requires_command_entry(tmp_path: Path) -> None:
    """虚拟环境缺少 minic 入口时报错。"""
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    try:
        global_install.install_global(scripts, home=tmp_path / "home")
        raise AssertionError("应当抛出 FileNotFoundError")
    except FileNotFoundError:
        pass


def test_install_global_verifies_written_path(monkeypatch, tmp_path: Path) -> None:
    """PATH 写入后读回校验，未生效时报错。"""
    scripts = _fake_venv(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setattr(global_install, "get_user_path", lambda: [])
    monkeypatch.setattr(global_install, "set_user_path", lambda entries: None)
    monkeypatch.setattr(global_install, "_notify_environment_change", lambda: None)
    try:
        global_install.install_global(scripts, home=home)
        raise AssertionError("应当抛出 RuntimeError")
    except RuntimeError:
        pass
