"""MCP 客户端管理器：连接管理、工具注册与调用。

基于 ``mcp`` SDK（2.x）的 ``Client`` + ``Transport`` 模型实现：
- streamable-http 通过自定义 ``httpx2.AsyncClient`` 传入配置的 headers/timeout。
- stdio 通过 ``stdio_client`` + ``StdioServerParameters``。
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
from pathlib import Path
from typing import Any, Callable

import httpx2  # mcp 2.x 的 HTTP 传输基于 httpx2

from mcp import types  # 协议数据类型
from mcp.client import Client  # 高层客户端
from mcp.client.stdio import StdioServerParameters, stdio_client  # stdio 传输
from mcp.client.streamable_http import streamable_http_client  # streamable-http 传输
from mcp.shared._stream_protocols import ReadStream, WriteStream  # 传输流类型

from minic.mcp.settings import load_mcp_settings  # 配置加载

TransportStreams = tuple[ReadStream[Any], WriteStream[Any]]  # 传输流元组别名

_STREAMABLE_HTTP = "streamable-http"  # HTTP 传输标识
_STDIO = "stdio"  # stdio 传输标识

# 连接失败指数退避参数：初次失败后重试 3 次，延迟依次为 0.5s / 1s / 2s
DEFAULT_MAX_ATTEMPTS = 4  # 初次尝试 + 3 次重试
DEFAULT_BACKOFF_DELAYS = (0.5, 1.0, 2.0)  # 重试延迟序列（秒）


class _HttpTransport:
    """streamable-http 传输：进入时创建 httpx2 客户端并启动传输上下文。"""

    def __init__(self, url: str, headers: dict[str, str] | None, timeout: int | None) -> None:
        self._url = url
        self._headers = headers or {}
        self._timeout = timeout
        self._stack: contextlib.AsyncExitStack | None = None

    async def __aenter__(self) -> TransportStreams:
        stack = contextlib.AsyncExitStack()
        # trust_env=False：本地 MCP 服务不走系统代理，避免代理干扰本地连接
        client = httpx2.AsyncClient(
            headers=self._headers,
            timeout=self._timeout or 60,
            trust_env=False,
        )
        await stack.enter_async_context(client)
        streams = await stack.enter_async_context(
            streamable_http_client(self._url, http_client=client)
        )
        self._stack = stack
        return streams

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._stack is not None:
            await self._stack.__aexit__(exc_type, exc_val, exc_tb)
            self._stack = None


class _StdioTransport:
    """stdio 传输：进入时通过 stdio_client 拉起子进程并建立消息流。"""

    def __init__(self, params: StdioServerParameters) -> None:
        self._params = params
        self._stack: contextlib.AsyncExitStack | None = None

    async def __aenter__(self) -> TransportStreams:
        stack = contextlib.AsyncExitStack()
        streams = await stack.enter_async_context(stdio_client(self._params))
        self._stack = stack
        return streams

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._stack is not None:
            await self._stack.__aexit__(exc_type, exc_val, exc_tb)
            self._stack = None


def _connection_error_text(exc: BaseException) -> str:
    """从异常组/异常链中提取有意义的连接错误信息。"""
    messages: list[str] = []
    seen: set[int] = set()

    def walk(e: BaseException) -> None:
        if id(e) in seen:
            return
        seen.add(id(e))
        text = str(e).strip()
        if text and text not in messages:
            messages.append(text)
        for sub in getattr(e, "exceptions", None) or []:
            walk(sub)
        if e.__cause__ is not None:
            walk(e.__cause__)
        if e.__context__ is not None:
            walk(e.__context__)

    walk(exc)
    for msg in messages:
        if any(keyword in msg for keyword in ("WinError", "ConnectError", "ConnectionRefused", "ConnectionReset", "连接")):
            return msg
    return messages[-1] if messages else type(exc).__name__


def _serialize_call_result(result: Any) -> str:
    """把 MCP call_tool 返回结果序列化为文本。"""
    parts: list[str] = []
    content = getattr(result, "content", None) or []
    for block in content:
        block_type = getattr(block, "type", "")
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(str(text))
        elif block_type == "image":
            parts.append(f"[image mime_type={getattr(block, 'mime_type', '')}]")
        else:
            try:
                parts.append(json.dumps(block.model_dump(), ensure_ascii=False, default=str))
            except Exception:  # noqa: BLE001 - 无法序列化时退化为字符串
                parts.append(str(block))
    if not parts:
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            try:
                parts.append(json.dumps(structured, ensure_ascii=False, default=str))
            except Exception:  # noqa: BLE001 - 无法序列化时退化为字符串
                parts.append(str(structured))
    if not parts:
        parts.append(str(result))
    return "\n".join(parts)


class McpManager:
    """MCP 服务连接与工具管理。"""

    def __init__(
        self,
        settings_path: str | Path | None = None,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_delays: tuple[float, ...] = DEFAULT_BACKOFF_DELAYS,
    ) -> None:
        """初始化管理器并读取配置。"""
        self.settings_path = settings_path  # 配置文件路径
        self._config = load_mcp_settings(settings_path)["mcpServers"]  # 服务配置
        self._max_attempts = max_attempts  # 最大连接尝试次数
        self._backoff_delays = backoff_delays  # 重试延迟序列
        self._clients: dict[str, Client] = {}  # 已连接服务 -> 活跃客户端
        self._tool_lists: dict[str, list[Any]] = {}  # 已连接服务 -> 工具清单
        self._status: dict[str, dict[str, Any]] = {}  # 服务名 -> 状态字典
        self._tasks: dict[str, asyncio.Task] = {}  # 服务名 -> 会话循环任务
        self._generation: dict[str, int] = {}  # 服务名 -> 当前连接代次（用于取代旧连接）
        self._signal: dict[str, asyncio.Event] = {}  # 服务名 -> 重连信号
        self._stop: dict[str, asyncio.Event] = {}  # 服务名 -> 停止信号
        self._pending: dict[str, list[asyncio.Future]] = {}  # 服务名 -> 等待连接结果的外部调用
        self._connect_timeout = 30.0  # 单次连接/重连等待超时（秒）
        self._callbacks: list[Callable[[], None]] = []  # 工具清单变化回调
        for name in self._config:
            self._status[name] = self._initial_status(name)
            self._signal[name] = asyncio.Event()
            self._stop[name] = asyncio.Event()

    def _initial_status(self, name: str) -> dict[str, Any]:
        """构造初始状态。"""
        cfg = self._config[name]
        if cfg.get("disabled"):
            return {
                "name": name,
                "transport": cfg.get("transport", ""),
                "url": cfg.get("url"),
                "command": cfg.get("command"),
                "status": "disabled",
                "tools_count": 0,
                "attempts": 0,
                "error_message": None,
            }
        return {
            "name": name,
            "transport": cfg.get("transport", ""),
            "url": cfg.get("url"),
            "command": cfg.get("command"),
            "status": "connecting",
            "tools_count": 0,
            "attempts": 0,
            "error_message": None,
        }

    def _set_status(self, name: str, **fields: Any) -> None:
        """更新状态字典的字段。"""
        self._status[name].update(fields)

    def _build_transport(self, name: str) -> Any:
        """按配置构建传输对象。"""
        cfg = self._config[name]
        transport = cfg.get("transport", _STREAMABLE_HTTP)
        timeout = int(cfg.get("timeout") or 60)
        if transport == _STDIO:
            params = StdioServerParameters(
                command=cfg.get("command", ""),
                args=list(cfg.get("args") or []),
                env=dict(cfg.get("env") or {}),
            )
            return _StdioTransport(params)
        return _HttpTransport(
            url=cfg.get("url", ""),
            headers=cfg.get("headers") or {},
            timeout=timeout,
        )

    async def _connect_once(self, name: str, generation: int) -> tuple[Client, list[Any]]:
        """连接单个服务并拉取工具清单；失败抛异常。

        注意：返回的 Client 已处于进入状态（transport 上下文已建立），
        必须由同一任务调用其 __aexit__ 关闭，因此只在 _session_loop 内调用。
        """
        del generation  # 代次由调用方在上层判断
        transport = self._build_transport(name)
        timeout = int(self._config[name].get("timeout") or 60)
        client = Client(transport, read_timeout_seconds=timeout)
        await client.__aenter__()
        try:
            tool_result = await client.list_tools()
        except Exception:
            with contextlib.suppress(Exception):
                await client.__aexit__(None, None, None)
            raise
        return client, list(tool_result.tools)

    async def _wait_trigger(
        self,
        name: str,
        stop: asyncio.Event,
        signal: asyncio.Event,
        generation: int,
    ) -> str:
        """等待停止 / 重连信号 / 代次变化，返回对应原因。"""
        while not stop.is_set():
            if signal.is_set():
                return "signal"
            if self._generation.get(name) != generation:
                return "superseded"
            await asyncio.sleep(0.05)
        return "stop"

    async def _session_loop(self, name: str) -> None:
        """单个 server 的会话循环：唯一负责 Client 的进入与退出。

        mcp 2.x 的 streamable-http/stdio 传输基于 anyio TaskGroup，
        Client.__aenter__ 与 __aexit__ 必须在同一任务内执行，跨任务转移会触发
        “Attempted to exit cancel scope in a different task” 运行时错误。
        因此所有连接/重连/关闭都在本循环内完成，外部通过事件与代次协调。
        """
        stop = self._stop[name]
        signal = self._signal[name]
        while not stop.is_set():
            if signal.is_set():
                signal.clear()
            generation = self._generation.get(name, 0)
            self._generation[name] = generation
            self._set_status(name, status="connecting", error_message=None)
            client: Client | None = None
            tools: list[Any] = []
            last_error: Exception | None = None
            attempts = 0
            connected = False
            superseded = False
            while not stop.is_set():
                if self._generation.get(name) != generation:
                    superseded = True
                    break
                attempts += 1
                self._set_status(name, attempts=attempts)
                try:
                    client, tools = await self._connect_once(name, generation)
                except Exception as exc:  # noqa: BLE001 - 连接失败统一走退避
                    last_error = exc
                    if stop.is_set() or self._generation.get(name) != generation:
                        superseded = True
                        break
                    if attempts >= self._max_attempts:
                        break
                    delay = self._backoff_delays[min(attempts - 1, len(self._backoff_delays) - 1)]
                    await asyncio.sleep(delay)
                    continue
                if self._generation.get(name) != generation:
                    # 连接成功但已被更新的请求取代
                    with contextlib.suppress(Exception):
                        await client.__aexit__(None, None, None)
                    superseded = True
                    client = None
                    break
                connected = True
                break
            if connected:
                self._clients[name] = client
                self._tool_lists[name] = tools
                self._set_status(name, status="connected", tools_count=len(tools), error_message=None)
                self._notify_tools_changed()
            elif not superseded:
                self._set_status(
                    name,
                    status="error",
                    tools_count=0,
                    error_message=_connection_error_text(last_error) if last_error else "连接失败",
                )
            if not superseded:
                self._resolve_pending(name, generation)
            if connected and client is not None:
                # 持有会话，等待停止 / 重连信号 / 被取代
                while True:
                    reason = await self._wait_trigger(name, stop, signal, generation)
                    if reason == "stop":
                        break
                    if reason in {"signal", "superseded"}:
                        break
                with contextlib.suppress(Exception):
                    await client.__aexit__(None, None, None)
                self._clients.pop(name, None)
                self._tool_lists.pop(name, None)
                if signal.is_set():
                    signal.clear()
                continue
            if superseded:
                continue  # 直接进入下一轮（使用新代次）
            # 失败状态：等待重连触发
            while True:
                reason = await self._wait_trigger(name, stop, signal, generation)
                if reason == "stop":
                    break
                if reason in {"signal", "superseded"}:
                    break
            if signal.is_set():
                signal.clear()

    def _resolve_pending(self, name: str, generation: int) -> None:
        """结算等待该服务连接结果的外部调用方（仅当仍是当前代次时）。"""
        if self._generation.get(name) != generation:
            return
        pending = self._pending.pop(name, [])
        for future in pending:
            if not future.done():
                future.set_result(True)

    async def _ensure_loop(self, name: str) -> None:
        """确保会话循环任务在运行。"""
        task = self._tasks.get(name)
        if task is None or task.done():
            self._tasks[name] = asyncio.create_task(self._session_loop(name))

    async def _wait_settle(self, name: str, *, timeout: float) -> None:
        """等待该服务完成一轮连接尝试（connected 或 error）。"""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending.setdefault(name, []).append(future)
        try:
            await asyncio.wait_for(future, timeout=timeout)
        finally:
            pending = self._pending.get(name, [])
            if future in pending:
                pending.remove(future)

    async def start(self) -> None:
        """为所有未禁用服务异步发起连接（后台任务，不阻塞启动）。"""
        for name, cfg in self._config.items():
            if cfg.get("disabled"):
                continue
            self._signal[name].set()
            await self._ensure_loop(name)

    async def connect_all(self) -> None:
        """同步等待连接所有未禁用服务（测试与手动场景使用）。"""
        await self.start()
        for name, cfg in self._config.items():
            if cfg.get("disabled"):
                continue
            await self._wait_settle(name, timeout=self._connect_timeout)

    def reload_config(self) -> None:
        """重新读取配置文件：新增服务初始化状态/信号，已存在服务更新配置。"""
        self._config = load_mcp_settings(self.settings_path)["mcpServers"]
        for name in self._config:
            if name not in self._status:
                self._status[name] = self._initial_status(name)
                self._signal[name] = asyncio.Event()
                self._stop[name] = asyncio.Event()

    async def connect(self, name: str) -> dict[str, Any]:
        """手动重连单个服务。

        通过递增代次 + 触发信号让会话循环重新连接（不取消 mcp 传输任务），
        由会话循环在同一任务内关闭旧连接。

        Args:
            name: 服务名。

        Returns:
            连接成功后的新状态。

        Raises:
            KeyError: 服务不存在。
            ValueError: 服务被禁用。
            ConnectionError: 连接失败时抛出（含错误信息）。
        """
        if name not in self._config:
            raise KeyError(f"MCP 服务不存在: {name}")
        if self._config[name].get("disabled"):
            raise ValueError(f"MCP 服务已禁用: {name}")
        self._generation[name] = self._generation.get(name, 0) + 1
        self._signal[name].set()
        await self._ensure_loop(name)
        await self._wait_settle(name, timeout=self._connect_timeout)
        status = self._status[name]
        if status["status"] == "error":
            raise ConnectionError(status["error_message"] or "连接失败")
        return status

    def status(self) -> list[dict[str, Any]]:
        """返回全部服务的状态列表。"""
        return [dict(self._status[name]) for name in self._config]

    def tools(self) -> list[tuple[str, dict[str, Any]]]:
        """返回所有已连接服务的 (server_name, tool_schema) 列表。

        tool_schema 结构为 ``{"name", "description", "args_schema"}``，
        供工具注册表转换为 ToolSpec。
        """
        result: list[tuple[str, dict[str, Any]]] = []
        for name in self._config:
            tools = self._tool_lists.get(name, [])
            for tool in tools:
                schema = {
                    "name": tool.name,
                    "description": tool.description or "",
                    "args_schema": tool.input_schema or {},
                }
                result.append((name, schema))
        return result

    async def call_tool(self, server_name: str, tool_name: str, args: dict[str, Any]) -> str:
        """通过对应会话调用 MCP 工具并返回文本结果。

        Raises:
            ConnectionError: 服务未连接。
            RuntimeError: 工具返回错误（is_error=True），携带错误文本。
        """
        client = self._clients.get(server_name)
        if client is None:
            raise ConnectionError(f"MCP 服务未连接: {server_name}")
        result = await client.call_tool(tool_name, args or {})
        if getattr(result, "is_error", False):
            raise RuntimeError(_serialize_call_result(result))
        return _serialize_call_result(result)

    def is_auto_approved(self, server_name: str, tool_name: str) -> bool:
        """判断工具是否在配置的 autoApprove 列表内（支持通配符）。"""
        cfg = self._config.get(server_name)
        if not cfg:
            return False
        auto_approve = list(cfg.get("autoApprove") or [])
        full_name = f"{server_name}.{tool_name}"
        for entry in auto_approve:
            if entry in (full_name, tool_name, "*"):
                return True
            if fnmatch.fnmatch(full_name, entry) or fnmatch.fnmatch(tool_name, entry):
                return True
        return False

    def add_tools_changed_callback(self, callback: Callable[[], None]) -> None:
        """注册工具清单变化回调（连接成功或重连后触发）。"""
        self._callbacks.append(callback)

    def _notify_tools_changed(self) -> None:
        """通知工具清单变化。"""
        for callback in self._callbacks:
            callback()

    async def shutdown(self) -> None:
        """关闭全部连接并停止会话循环。"""
        for name in self._config:
            self._stop[name].set()
            self._signal[name].set()
        tasks = list(self._tasks.values())
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10)
            except asyncio.TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        # 防御性兜底：若仍有未关闭的会话，尽量关闭
        for name, client in self._clients.items():
            with contextlib.suppress(Exception):
                await client.__aexit__(None, None, None)
        self._clients.clear()
        self._tool_lists.clear()
        for name in self._config:
            status = self._status[name].get("status")
            if status not in {"disabled"}:
                self._set_status(name, status="error", error_message="已关闭")
