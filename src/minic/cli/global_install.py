"""MiniC 全局命令安装与卸载。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


_MARKER_NAME = "global-install.json"


def get_user_path() -> list[str]:
    """读取用户环境变量 PATH。"""
    if os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
                return value.split(";") if value else []
        except OSError:
            return []
    return os.environ.get("PATH", "").split(os.pathsep)


def set_user_path(entries: list[str]) -> None:
    """写入用户环境变量 PATH。"""
    value = ";".join(entries)
    if os.name == "nt":
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, value)
    else:
        os.environ["PATH"] = value


def _normalize_entry(entry: str) -> str:
    """归一化 PATH 条目用于去重比较。"""
    return os.path.normcase(os.path.normpath(entry)).rstrip("\\/")


def _same_path(candidate: str, entry: str) -> bool:
    """判断两个 PATH 条目是否指向同一目录。"""
    return _normalize_entry(candidate) == _normalize_entry(entry)


def _notify_environment_change() -> None:
    """广播环境变量变更，让新终端进程立即使用新 PATH。"""
    if os.name != "nt":
        return
    import ctypes

    HWND_BROADCAST = 0xFFFF
    WM_SETTINGCHANGE = 0x001A
    SMTO_ABORTIFHUNG = 0x0002
    result = ctypes.c_size_t()
    ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST,
        WM_SETTINGCHANGE,
        0,
        ctypes.c_wchar_p("Environment"),
        SMTO_ABORTIFHUNG,
        5000,
        ctypes.byref(result),
    )


def _verify_path_entries(entries: list[str], expect_present: bool = True) -> None:
    """读回用户 PATH，确认本次写入的条目状态符合预期。"""
    current = get_user_path()
    normalized_current = {_normalize_entry(entry) for entry in current}
    if expect_present:
        missing = [entry for entry in entries if _normalize_entry(entry) not in normalized_current]
        if missing:
            raise RuntimeError(f"PATH 写入校验失败，缺少条目: {', '.join(missing)}")
        return
    remaining = [entry for entry in entries if _normalize_entry(entry) in normalized_current]
    if remaining:
        raise RuntimeError(f"PATH 写入校验失败，仍存在条目: {', '.join(remaining)}")


def _marker_path(home: Path | None = None) -> Path:
    """返回安装记录文件路径。"""
    base = (home or Path.home()).resolve() / ".minic"
    return base / _MARKER_NAME


def _read_marker(home: Path | None = None) -> dict[str, Any]:
    """读取安装记录。"""
    path = _marker_path(home)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_marker(home: Path | None, data: dict[str, Any]) -> None:
    """原子写入安装记录。"""
    path = _marker_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _delete_marker(home: Path | None) -> None:
    """删除安装记录。"""
    _marker_path(home).unlink(missing_ok=True)


def _render_shim(python_exe: Path) -> str:
    """生成转发到 MiniC 模块的命令行 shim。"""
    if os.name == "nt":
        return (
            "@echo off\r\n"
            f'"{python_exe}" -m minic.cli.main %*\r\n'
        )
    return (
        "#!/bin/sh\n"
        f'exec "{python_exe}" -m minic.cli.main "$@"\n'
    )


def _default_venv_scripts() -> Path:
    """从包路径推断项目虚拟环境 Scripts 目录。"""
    project_root = Path(__file__).resolve().parents[3]
    return project_root / ".venv" / "Scripts"


def install_global(venv_scripts: str | Path, home: Path | None = None) -> dict[str, Any]:
    """幂等安装全局命令，返回本次安装记录。"""
    scripts = Path(venv_scripts).resolve()
    exe_name = "minic.exe" if os.name == "nt" else "minic"
    if not (scripts / exe_name).exists():
        raise FileNotFoundError(f"未找到命令入口: {scripts / exe_name}")

    bin_dir = (home or Path.home()).resolve() / ".minic" / "bin"
    cmd_name = "minic.cmd" if os.name == "nt" else "minic"
    cmd_path = bin_dir / cmd_name
    bin_dir.mkdir(parents=True, exist_ok=True)
    python_name = "python.exe" if os.name == "nt" else "python"
    cmd_path.write_text(_render_shim(scripts / python_name), encoding="utf-8")

    current = get_user_path()
    entries = [entry for entry in current if entry.strip()]
    added: list[str] = []
    for candidate in (str(scripts), str(bin_dir)):
        if not any(_same_path(candidate, entry) for entry in entries):
            entries.append(candidate)
            added.append(candidate)
    if added:
        set_user_path(entries)
        _verify_path_entries(added)
        _notify_environment_change()

    marker = _read_marker(home)
    previous = marker.get("entries", [])
    combined = list(previous)
    for entry in added:
        if not any(_same_path(entry, existing) for existing in combined):
            combined.append(entry)
    data = {
        "venv_scripts": str(scripts),
        "bin_dir": str(bin_dir),
        "cmd_path": str(cmd_path),
        "entries": combined,
    }
    _write_marker(home, data)
    return {**data, "added": added}


def uninstall_global(home: Path | None = None) -> dict[str, Any]:
    """按安装记录卸载全局命令。"""
    marker = _read_marker(home)
    current = get_user_path()
    entries = [entry for entry in current if entry.strip()]
    recorded = marker.get("entries", [])
    removed: list[str] = []
    if recorded:
        normalized = {_normalize_entry(entry) for entry in recorded}
        kept: list[str] = []
        for entry in entries:
            if _normalize_entry(entry) in normalized:
                removed.append(entry)
            else:
                kept.append(entry)
        if removed:
            set_user_path(kept)
            _verify_path_entries(removed, expect_present=False)
            _notify_environment_change()

    cmd_path = Path(marker["cmd_path"]) if marker.get("cmd_path") else None
    if cmd_path is not None and cmd_path.exists():
        cmd_path.unlink()
    bin_dir = Path(marker["bin_dir"]) if marker.get("bin_dir") else None
    if bin_dir is not None and bin_dir.exists():
        try:
            bin_dir.rmdir()
        except OSError:
            pass
    _delete_marker(home)
    return {"removed": removed, "cmd_path": str(cmd_path) if cmd_path else None}


def main(argv: list[str] | None = None) -> None:
    """全局安装脚本的命令行入口。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="install-global", description="MiniC 全局命令安装/卸载")
    parser.add_argument("--venv-scripts", default=None, help="虚拟环境 Scripts 目录")
    parser.add_argument("--home", default=None, help="用户主目录（测试用）")
    parser.add_argument("--uninstall", action="store_true", help="卸载全局命令")
    args = parser.parse_args(argv)
    home = Path(args.home).resolve() if args.home else None
    try:
        if args.uninstall:
            uninstall_global(home=home)
            print("已卸载 MiniC 全局命令")
        else:
            scripts = Path(args.venv_scripts).resolve() if args.venv_scripts else _default_venv_scripts()
            result = install_global(scripts, home=home)
            print(f"已安装 MiniC 全局命令：{result['cmd_path']}")
            print("请打开新终端窗口后输入 minic")
    except Exception as exc:  # noqa: BLE001 - 安装失败时给出可读错误
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
