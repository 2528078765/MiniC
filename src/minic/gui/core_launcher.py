"""桌面端内嵌核心：在 GUI 进程内用后台线程拉起 FastAPI 核心服务。

目标：双击安装包 exe 即可使用，不需要命令行或其他方式单独启动服务。
已有健康核心在运行（runtime.json 存在且 /health 可达）时直接复用，不重复启动。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import httpx

# 进程内核心的存活引用（保活 + 退出时优雅停止）
_ACTIVE: dict[str, Any] = {}


def _health_ok(port: int, timeout: float = 2.0) -> bool:
    """探测 /health 是否可达。"""
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"http://127.0.0.1:{port}/health")
            return response.status_code == 200
    except httpx.HTTPError:
        return False


def _read_runtime() -> dict[str, Any] | None:
    """读取 runtime.json（不存在/损坏返回 None）。"""
    from minic.core.runtime import get_runtime_path

    path = get_runtime_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _runtime_healthy(runtime: dict[str, Any] | None) -> bool:
    """runtime.json 指向的核心当前是否健康。"""
    if runtime is None:
        return False
    try:
        port = int(runtime.get("port", 0))
    except (TypeError, ValueError):
        return False
    return port > 0 and _health_ok(port)


def _core_log_config() -> dict[str, Any] | None:
    """uvicorn 日志配置：写入 ~/.minic/logs/core.log（打包后无控制台）。"""
    from minic.core.runtime import get_runtime_dir

    log_dir = get_runtime_dir() / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None  # 目录不可写：关闭 uvicorn 日志
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
        },
        "handlers": {
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(log_dir / "core.log"),
                "maxBytes": 5 * 1024 * 1024,
                "backupCount": 2,
                "formatter": "default",
                "encoding": "utf-8",
            },
        },
        "root": {"handlers": ["file"], "level": "INFO"},
        "loggers": {
            "uvicorn": {"handlers": ["file"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"level": "WARNING"},
        },
    }


def start_embedded_core(project_root: Path | None = None, timeout: float = 60.0) -> bool:
    """让核心可用：复用已运行的健康核心，否则在进程内后台线程启动。

    Args:
        project_root: 核心项目根目录（缺省当前工作目录）。
        timeout: 等待核心就绪的最长时间（秒）。

    Returns:
        True=核心可用；False=启动失败（调用方提示用户）。
    """
    runtime = _read_runtime()
    if _runtime_healthy(runtime):
        return True  # 复用正在运行的核心（不重复启动）

    from minic.core.runtime import (
        acquire_single_instance_lock,
        find_free_port,
        generate_token,
        release_single_instance_lock,
        write_runtime,
    )

    try:
        lock_file = acquire_single_instance_lock()
    except RuntimeError:
        # 锁被占用：另一进程的核心可能正在启动，等待其就绪
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _runtime_healthy(_read_runtime()):
                return True
            time.sleep(0.5)
        return False

    root = (project_root or Path.cwd()).resolve()
    try:
        from minic.core.config import load_settings
        from minic.core.server import create_app

        settings = load_settings(root)
        port = find_free_port()
        token = generate_token()
        app = create_app(
            settings=settings,
            token=token,
            project_root=root,
            short_memory_dir=root / ".minic" / "memory" / "short_memory",
        )
    except Exception as exc:  # noqa: BLE001 - 初始化失败释放锁并上报
        logging.getLogger("minic.core_launcher").exception("内嵌核心初始化失败: %s", exc)
        release_single_instance_lock(lock_file)
        return False

    import uvicorn

    try:
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="info",
            log_config=_core_log_config(),
        )
        server = uvicorn.Server(config)
    except Exception:  # noqa: BLE001 - 日志配置不兼容时退化为无日志
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None)
        )

    write_runtime(port, token)  # 就绪前先公布 runtime（GUI 连接失败会自动重试）

    thread = threading.Thread(target=server.run, daemon=True, name="minic-core")
    thread.start()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _health_ok(port):
            _ACTIVE.update({"server": server, "lock_file": lock_file, "thread": thread})
            return True
        if not thread.is_alive():
            break  # 服务线程异常退出
        time.sleep(0.2)

    server.should_exit = True  # 未就绪：停止服务并释放锁
    release_single_instance_lock(lock_file)
    return False


def stop_embedded_core() -> None:
    """优雅停止进程内核心（GUI 退出时调用）。"""
    server = _ACTIVE.get("server")
    if server is not None:
        server.should_exit = True
    thread = _ACTIVE.get("thread")
    if thread is not None:
        thread.join(timeout=3.0)
    from minic.core.runtime import release_single_instance_lock

    release_single_instance_lock(_ACTIVE.get("lock_file"))
    _ACTIVE.clear()
