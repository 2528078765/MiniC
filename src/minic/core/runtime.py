"""核心服务运行时文件与单实例锁。"""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, IO


def get_runtime_dir() -> Path:
    """返回全局运行时目录 ~/.minic。"""
    return Path.home() / ".minic"


def get_runtime_path() -> Path:
    """返回运行时信息文件路径。"""
    return get_runtime_dir() / "runtime.json"


def acquire_single_instance_lock() -> IO[str]:
    """获取核心服务单实例锁，已运行时抛出异常。"""
    lock_path = get_runtime_dir() / "runtime.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "a+", encoding="utf-8")
    lock_file.write("lock")
    lock_file.flush()
    lock_file.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        raise RuntimeError("MiniC 核心服务已在运行") from None
    return lock_file


def release_single_instance_lock(lock_file: IO[str] | None) -> None:
    """释放单实例锁并关闭文件。"""
    if lock_file is None:
        return
    try:
        lock_file.close()
    except OSError:
        pass


def find_free_port(preferred: int | None = None) -> int:
    """优先使用指定端口，否则在默认区间查找可用端口。"""
    candidates: list[int] = []
    if preferred is not None:
        candidates.append(preferred)
    candidates.extend(range(8765, 8900))
    for port in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def generate_token() -> str:
    """生成访问令牌。"""
    return secrets.token_urlsafe(32)


def write_runtime(port: int, token: str) -> None:
    """把端口、令牌和进程信息写入 runtime.json。"""
    from minic import __version__  # 延迟导入避免循环依赖

    runtime_path = get_runtime_path()
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "port": port,
        "token": token,
        "pid": os.getpid(),
        "started_at": datetime.now().astimezone().isoformat(),
        "version": __version__,
    }
    tmp_path = runtime_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, runtime_path)


def read_runtime() -> dict[str, Any]:
    """读取当前核心服务运行时信息。"""
    runtime_path = get_runtime_path()
    if not runtime_path.exists():
        raise FileNotFoundError("未找到 runtime.json，请先启动核心服务")
    return json.loads(runtime_path.read_text(encoding="utf-8"))
