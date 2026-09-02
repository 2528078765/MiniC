"""minic 直连模式：自动启动核心、等待健康检查、按归属关闭核心。"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from minic.core.config import resolve_project_root
from minic.core.runtime import read_runtime


def _api_base(runtime: dict[str, Any]) -> str:
    """根据 runtime 生成 API 地址。"""
    return f"http://127.0.0.1:{runtime['port']}"


def _is_healthy(runtime: dict[str, Any]) -> bool:
    """检查核心服务是否可连接。"""
    try:
        with httpx.Client(timeout=1.0) as client:
            response = client.get(f"{_api_base(runtime)}/health")
            return response.status_code == 200
    except httpx.HTTPError:
        return False


def select_core_project_root(workspace: Path) -> Path:
    """选择自动启动核心时使用的项目根。"""
    return resolve_project_root(workspace, Path(__file__).resolve().parents[3])


@dataclass
class CoreHandle:
    """核心服务连接句柄。"""

    runtime: dict[str, Any]
    process: subprocess.Popen | None = None
    started_by_self: bool = False

    def stop(self) -> None:
        """仅关闭由本次 CLI 启动的核心。"""
        if not self.started_by_self or self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()


def ensure_core(
    project_root: Path,
    workspace: Path,
    preferred_port: int | None = None,
) -> CoreHandle:
    """连接已运行核心，未运行则后台启动并等待健康检查。"""
    root = Path(project_root).resolve()
    workspace_path = Path(workspace).resolve()
    try:
        runtime = read_runtime()
        if _is_healthy(runtime):
            return CoreHandle(runtime=runtime, started_by_self=False)  # 已运行核心直接复用
    except FileNotFoundError:
        pass

    command = [sys.executable, "-m", "minic.cli", "serve", "--project", str(root)]
    if preferred_port is not None:
        command.extend(["--port", str(preferred_port)])
    process = subprocess.Popen(  # 后台启动核心，等待 /health 就绪
        command,
        cwd=str(root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20  # 最多等待 20 秒健康检查
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            runtime = read_runtime()
            if _is_healthy(runtime):
                return CoreHandle(runtime=runtime, process=process, started_by_self=True)
        except FileNotFoundError:
            pass
        time.sleep(0.2)

    try:
        runtime = read_runtime()
        if _is_healthy(runtime):
            return CoreHandle(runtime=runtime, process=process, started_by_self=True)
    except FileNotFoundError:
        pass
    process.terminate()
    raise RuntimeError("自动启动核心服务失败")
