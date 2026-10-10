"""CoreClient：桌面端访问本地核心服务的 API 封装（httpx + 信号）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import httpx
from PySide6.QtCore import QObject, Signal

from minic.core.runtime import read_runtime


def _error_detail(response: httpx.Response) -> str:
    """从核心错误响应体提取可读信息（error.message/detail），提取不到返回空串。"""
    try:
        body = response.json()
    except ValueError:
        return ""
    error = body.get("error") or {}
    if isinstance(error, dict):
        detail = error.get("message") or error.get("detail") or ""
        return str(detail)
    return ""


class CoreClient(QObject):
    """封装核心 API：连接、鉴权、聊天 SSE、设置/记忆/RAG 等。

    所有网络调用在调用方线程执行；耗时操作应由调用方放入 QThread。
    通过 Qt 信号把结果抛回 UI 线程。
    """

    # 信号
    connected = Signal(dict)                       # health 信息
    connection_failed = Signal(str)                # 连接失败原因
    unauthorized = Signal()                        # 令牌失效，需重新读取 runtime
    chat_event = Signal(str, dict)                 # SSE 事件 (event_name, data)，data 带 _run
    chat_error = Signal(str, str)                  # 聊天/SSE 错误 (message, run_key)
    chat_stream_closed = Signal(str)               # HTTP 响应已关闭，才能开始后续恢复流
    request_error = Signal(str, dict)              # (操作名, 错误详情)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.base_url: str | None = None
        self.token: str | None = None
        self.health: dict[str, Any] | None = None

    # ---- 连接 ----

    def connect_core(self) -> bool:
        """读取 runtime.json 并调用 /health；成功返回 True。"""
        try:
            runtime = read_runtime()
        except FileNotFoundError as exc:
            self.connection_failed.emit(str(exc))
            return False
        self.base_url = f"http://127.0.0.1:{runtime['port']}"
        self.token = runtime["token"]
        try:
            with httpx.Client(trust_env=False, timeout=5.0) as client:
                response = client.get(f"{self.base_url}/health")
                response.raise_for_status()
                self.health = response.json()
        except httpx.HTTPError as exc:
            self.connection_failed.emit(f"无法连接核心服务: {exc}")
            return False
        self.connected.emit(self.health)
        return True

    def _headers(self) -> dict[str, str]:
        """构造带令牌的请求头。"""
        return {"Authorization": f"Bearer {self.token}"}

    def _refresh_runtime(self) -> bool:
        """重新读取 runtime.json 更新地址与令牌（核心重启后令牌轮换）。

        返回是否刷新成功；成功后调用方应使用新 token 重试一次。
        """
        try:
            runtime = read_runtime()
        except (FileNotFoundError, OSError, ValueError):
            return False
        self.base_url = f"http://127.0.0.1:{runtime['port']}"
        self.token = runtime["token"]
        return True

    def _handle_unauthorized(self, response: httpx.Response) -> bool:
        """401 时发出 unauthorized 信号，返回 True。"""
        if response.status_code == 401:
            self.unauthorized.emit()
            return True
        return False

    # ---- 请求封装 ----

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """GET 请求并返回 JSON；失败发 request_error 返回 None。

        401 时自动重读 runtime.json 刷新令牌并重试一次（核心重启自愈）。
        """
        try:
            with httpx.Client(trust_env=False, timeout=30.0) as client:
                response = client.get(f"{self.base_url}{path}", headers=self._headers(), params=params)
                if response.status_code == 401:
                    self._handle_unauthorized(response)
                    if self._refresh_runtime():  # 令牌轮换：用新 token 重试一次
                        response = client.get(f"{self.base_url}{path}", headers=self._headers(), params=params)
                if self._handle_unauthorized(response):
                    return None
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            self.request_error.emit(path, {"message": str(exc)})
            return None

    def post_json(self, path: str, payload: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any] | None:
        """POST 请求并返回 JSON；204 返回空 dict；失败发 request_error 返回 None。

        401 时自动重读 runtime.json 刷新令牌并重试一次（核心重启自愈）。
        """
        try:
            with httpx.Client(trust_env=False, timeout=timeout) as client:
                response = client.post(f"{self.base_url}{path}", headers=self._headers(), json=payload or {})
                if response.status_code == 401:
                    self._handle_unauthorized(response)
                    if self._refresh_runtime():
                        response = client.post(f"{self.base_url}{path}", headers=self._headers(), json=payload or {})
                if self._handle_unauthorized(response):
                    return None
                response.raise_for_status()
                if response.status_code == 204:
                    return {}
                return response.json()
        except httpx.HTTPError as exc:
            self.request_error.emit(path, {"message": str(exc)})
            return None

    def put_json(self, path: str, payload: dict[str, Any] | None = None) -> bool:
        """PUT 请求；成功返回 True；错误响应提取核心错误信息抛 RuntimeError。

        401 时自动重读 runtime.json 刷新令牌并重试一次（核心重启自愈）。
        """
        try:
            with httpx.Client(trust_env=False, timeout=30.0) as client:
                response = client.put(f"{self.base_url}{path}", headers=self._headers(), json=payload or {})
                if response.status_code == 401:
                    self._handle_unauthorized(response)
                    if self._refresh_runtime():
                        response = client.put(f"{self.base_url}{path}", headers=self._headers(), json=payload or {})
                if self._handle_unauthorized(response):
                    return False
                response.raise_for_status()
                return True
        except httpx.HTTPStatusError as exc:
            detail = _error_detail(exc.response)  # 核心 400/500 的可读原因
            if detail:
                raise RuntimeError(detail) from None  # 冒泡给 Worker.failed 显示失败原因
            self.request_error.emit(path, {"message": str(exc)})
            return False
        except httpx.HTTPError as exc:
            self.request_error.emit(path, {"message": str(exc)})
            return False

    def delete_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """DELETE 请求并返回 JSON；失败发 request_error 返回 None。

        401 时自动重读 runtime.json 刷新令牌并重试一次（核心重启自愈）。
        """
        try:
            with httpx.Client(trust_env=False, timeout=30.0) as client:
                response = client.delete(f"{self.base_url}{path}", headers=self._headers(), params=params)
                if response.status_code == 401:
                    self._handle_unauthorized(response)
                    if self._refresh_runtime():
                        response = client.delete(f"{self.base_url}{path}", headers=self._headers(), params=params)
                if self._handle_unauthorized(response):
                    return None
                response.raise_for_status()
                if response.status_code == 204:
                    return {}
                return response.json()
        except httpx.HTTPError as exc:
            self.request_error.emit(path, {"message": str(exc)})
            return None

    # ---- 聊天（SSE 流式）----

    def stream_chat(
        self,
        thread_id: str | None,
        workspace: str,
        message: str,
        model: str | None = None,
        run_key: str = "",
        resume: bool = False,
    ) -> None:
        """POST /chat/stream 并消费 SSE，逐事件发 chat_event 信号。

        在后台线程调用；每个事件触发 chat_event(event_name, data)，
        data 附加 ``_run=run_key`` 标识所属流（支持多会话并行）。
        出错发 chat_error(message, run_key)。
        """
        payload: dict[str, Any] = {
            "thread_id": thread_id,
            "workspace": workspace,
            "message": message,
            "model": model,
            "resume": resume,
        }
        try:
            # 必须用 with 打开流式响应（client.stream() 只有进入 with 才会真正
            # 发送请求；直接赋值不发送——曾致请求从未到核心、界面一直转圈）
            for attempt in range(2):
                with httpx.Client(
                    trust_env=False,
                    timeout=httpx.Timeout(connect=5.0, read=120.0, write=15.0, pool=5.0),
                ) as client:
                    with client.stream(
                        "POST", f"{self.base_url}/chat/stream", headers=self._headers(), json=payload
                    ) as response:
                        if response.status_code == 401 and attempt == 0:
                            # 核心重启令牌轮换：刷新后重试一次
                            self._handle_unauthorized(response)
                            if self._refresh_runtime():
                                continue
                            self.chat_error.emit("登录状态失效", run_key)
                            return
                        if self._handle_unauthorized(response):
                            self.chat_error.emit("登录状态失效", run_key)
                            return
                        if response.is_error:
                            response.read()  # 流式错误响应需要先读取响应体。
                            detail = _error_detail(response)
                            if detail:
                                self.chat_error.emit(f"HTTP {response.status_code}: {detail}", run_key)
                                return
                        response.raise_for_status()
                        event_name: str | None = None
                        data_lines: list[str] = []
                        for line in response.iter_lines():
                            if line == "":
                                if event_name and data_lines:
                                    try:
                                        data = json.loads("\n".join(data_lines))
                                    except json.JSONDecodeError:
                                        data = {"raw": "\n".join(data_lines)}
                                    data["_run"] = run_key
                                    self.chat_event.emit(event_name, data)
                                event_name = None
                                data_lines = []
                                continue
                            if line.startswith("event:"):
                                event_name = line[len("event:") :].strip()
                            elif line.startswith("data:"):
                                data_lines.append(line[len("data:") :].strip())
                        return  # 成功消费完，退出（不重试）
        except httpx.HTTPError as exc:
            self.chat_error.emit(str(exc), run_key)  # 连接失败/超时必报错，转圈不会无限
        finally:
            self.chat_stream_closed.emit(run_key)
    # ---- 常用 API 快捷方法 ----

    def get_settings(self) -> dict[str, Any] | None:
        """GET /settings（merged）。"""
        return self.get_json("/settings")

    def put_settings(self, payload: dict[str, Any]) -> bool:
        """PUT /settings（scope/workspace 由 payload 携带）。"""
        return self.put_json("/settings", payload)

    def get_memory(self, scope: str = "merged", workspace: str | None = None) -> dict[str, Any] | None:
        """GET /memory。"""
        params: dict[str, Any] = {"scope": scope}
        if workspace:
            params["workspace"] = workspace
        return self.get_json("/memory", params)

    def post_memory(self, payload: dict[str, Any]) -> bool:
        """POST /memory（scope/workspace/content/mode/base_version）。"""
        return self.post_json("/memory", payload) is not None

    def get_usage(self) -> dict[str, Any] | None:
        """GET /usage（token 消耗总量）。"""
        return self.get_json("/usage")

    def get_rag_status(self) -> dict[str, Any] | None:
        """GET /rag/status。"""
        return self.get_json("/rag/status")

    def get_skills(self) -> list[dict[str, Any]] | None:
        """GET /skills。"""
        data = self.get_json("/skills")
        return data.get("skills") if data else None

    def get_mcp(self) -> dict[str, Any] | None:
        """GET /mcp。"""
        return self.get_json("/mcp")

    def get_agents(self) -> dict[str, Any] | None:
        """GET /agents。"""
        return self.get_json("/agents")

    def approve(self, thread_id: str, approval_id: str, decision: str) -> bool:
        """POST /threads/{id}/approve。"""
        return self.post_json(f"/threads/{thread_id}/approve", {"approval_id": approval_id, "decision": decision}) is not None
