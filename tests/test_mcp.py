"""G7a MCP 服务接入测试。

使用本地 mock MCP server（mcp SDK 自带 MCPServer，streamable-http）作为 fixture，
覆盖：配置加载、连接管理、工具注册、调用、审批与 HTTP 接口。
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp.server.mcpserver import MCPServer

from minic.core.config import AppSettings
from minic.core.server import create_app
from minic.graph import clear_mcp_tools, get_tool, list_tools, register_mcp_tools
from minic.mcp.client import McpManager
from minic.mcp.settings import load_mcp_settings
from minic.recovery import ToolExecutionLog
from minic.tools.runtime import ToolRuntime
from minic.tools.service import ApprovalManager, PermissionStore, ToolExecutor


AUTH_HEADERS = {"Authorization": "Bearer test-token"}  # 测试令牌


def _free_port() -> int:
    """返回一个当前空闲的端口。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _build_mock_server() -> MCPServer:
    """构造提供 echo 与 add 两个工具的 mock MCP server。"""
    server = MCPServer("mock")

    @server.tool()
    def echo(text: str) -> str:
        """Echo back the text."""
        return f"echo:{text}"

    @server.tool()
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    return server


@pytest.fixture
def mock_mcp_server() -> Iterator[str]:
    """启动本地 mock MCP server（127.0.0.1 随机端口），返回 MCP 端点 URL。"""
    server = _build_mock_server()
    app = server.streamable_http_app()
    port = _free_port()
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not uvicorn_server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not uvicorn_server.started:
        raise RuntimeError("mock MCP server 启动失败")
    yield f"http://127.0.0.1:{port}/mcp"
    uvicorn_server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _clean_mcp_tools() -> Iterator[None]:
    """每个测试结束后清空 MCP 工具注册，避免测试间污染。"""
    yield
    clear_mcp_tools()


def _write_settings(
    tmp_path: Path,
    servers: dict[str, dict[str, Any]],
) -> Path:
    """把 mcpServers 配置写到临时目录并返回配置文件路径。"""
    path = tmp_path / "minic_mcp_settings.json"
    path.write_text(json.dumps({"mcpServers": servers}, ensure_ascii=False), encoding="utf-8")
    return path


def _http_settings(url: str, **extra: Any) -> dict[str, Any]:
    """构造一个 streamable-http 服务配置。"""
    config = {
        "transport": "streamable-http",
        "url": url,
        "timeout": 10,
        "disabled": False,
        "autoApprove": [],
        "headers": {},
    }
    config.update(extra)
    return config


# ---------------------------------------------------------------- 配置加载


def test_load_mcp_settings_missing(tmp_path: Path) -> None:
    """配置文件不存在时返回空 mcpServers。"""
    data = load_mcp_settings(tmp_path / "missing.json")
    assert data == {"mcpServers": {}}


def test_load_mcp_settings_valid(tmp_path: Path) -> None:
    """合法配置按结构返回。"""
    path = _write_settings(tmp_path, {"srv": _http_settings("http://127.0.0.1:1/mcp")})
    data = load_mcp_settings(path)
    assert set(data["mcpServers"]) == {"srv"}
    assert data["mcpServers"]["srv"]["transport"] == "streamable-http"


def test_load_mcp_settings_invalid_json(tmp_path: Path) -> None:
    """JSON 解析失败抛 ValueError。"""
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_mcp_settings(path)


def test_load_mcp_settings_not_object(tmp_path: Path) -> None:
    """JSON 不是对象或缺少 mcpServers 时抛 ValueError。"""
    path = tmp_path / "bad.json"
    path.write_text('["a"]', encoding="utf-8")
    with pytest.raises(ValueError):
        load_mcp_settings(path)


# ---------------------------------------------------------------- 连接管理


def test_connect_success_status_and_tools(tmp_path: Path, mock_mcp_server: str) -> None:
    """连接成功后状态 connected、工具数正确、tools() 返回 mock.echo/mock.add。"""

    async def main() -> None:
        path = _write_settings(tmp_path, {"mock": _http_settings(mock_mcp_server)})
        manager = McpManager(settings_path=str(path))
        try:
            await manager.connect_all()
            status = manager.status()[0]
            assert status["status"] == "connected"
            assert status["transport"] == "streamable-http"
            assert status["tools_count"] == 2
            assert status["error_message"] is None
            names = {schema["name"] for _, schema in manager.tools()}
            assert names == {"echo", "add"}
        finally:
            await manager.shutdown()

    asyncio.run(main())


def test_register_mcp_tools_exposes_get_tool(tmp_path: Path, mock_mcp_server: str) -> None:
    """注册表 get_tool/list_tools 能看到 MCP 工具（server.tool 格式）。"""

    async def main() -> None:
        path = _write_settings(tmp_path, {"mock": _http_settings(mock_mcp_server)})
        manager = McpManager(settings_path=str(path))
        try:
            await manager.connect_all()
            register_mcp_tools(manager)
            tool = get_tool("mock.echo")
            assert tool is not None
            assert tool.category == "mcp"
            assert tool.description  # 使用 MCP 提供的描述
            assert set(tool.args_schema) == {"text"}
            names = {spec.name for spec in list_tools()}
            assert "mock.echo" in names
            assert "mock.add" in names
        finally:
            await manager.shutdown()

    asyncio.run(main())


def test_disabled_server_not_connected(tmp_path: Path) -> None:
    """disabled server 不连接，状态为 disabled。"""

    async def main() -> None:
        path = _write_settings(
            tmp_path,
            {"off": _http_settings("http://127.0.0.1:1/mcp", disabled=True)},
        )
        manager = McpManager(settings_path=str(path))
        try:
            await manager.connect_all()
            status = manager.status()[0]
            assert status["status"] == "disabled"
            assert status["tools_count"] == 0
            assert manager.tools() == []
            assert manager._clients == {}
        finally:
            await manager.shutdown()

    asyncio.run(main())


def test_connection_failure_marks_error_with_retries(tmp_path: Path) -> None:
    """连接失败按指数退避重试，超过上限后标记 error 且不再自动重试。"""

    async def main() -> None:
        path = _write_settings(tmp_path, {"down": _http_settings("http://127.0.0.1:1/mcp")})
        manager = McpManager(
            settings_path=str(path),
            max_attempts=3,
            backoff_delays=(0.01, 0.02),
        )
        try:
            await manager.connect_all()
            status = manager.status()[0]
            assert status["status"] == "error"
            assert status["attempts"] == 3
            assert status["tools_count"] == 0
            assert status["error_message"]
        finally:
            await manager.shutdown()

    asyncio.run(main())


def test_manual_connect_after_server_up(tmp_path: Path, mock_mcp_server: str) -> None:
    """先失败后，POST connect 语义对应的 manager.connect 可在服务恢复后重连成功。"""

    async def main() -> None:
        # 服务已启动，但先指向错误 URL 制造失败
        bad_port = _free_port()
        path = _write_settings(tmp_path, {"mock": _http_settings(f"http://127.0.0.1:{bad_port}/mcp")})
        manager = McpManager(
            settings_path=str(path),
            max_attempts=1,
            backoff_delays=(0.01,),
        )
        try:
            await manager.connect_all()
            assert manager.status()[0]["status"] == "error"
            # 修正 URL 后手动重连
            path.write_text(
                json.dumps(
                    {"mcpServers": {"mock": _http_settings(mock_mcp_server)}},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            manager.reload_config()
            status = await manager.connect("mock")
            assert status["status"] == "connected"
            assert status["tools_count"] == 2
        finally:
            await manager.shutdown()

    asyncio.run(main())


def test_call_tool_returns_text(tmp_path: Path, mock_mcp_server: str) -> None:
    """call_tool 返回 MCP 工具执行结果文本。"""

    async def main() -> None:
        path = _write_settings(tmp_path, {"mock": _http_settings(mock_mcp_server)})
        manager = McpManager(settings_path=str(path))
        try:
            await manager.connect_all()
            assert await manager.call_tool("mock", "echo", {"text": "hi"}) == "echo:hi"
            assert await manager.call_tool("mock", "add", {"a": 2, "b": 3}) == "5"
            with pytest.raises(ConnectionError):
                await manager.call_tool("missing", "echo", {"text": "hi"})
        finally:
            await manager.shutdown()

    asyncio.run(main())


# ---------------------------------------------------------------- 审批与执行


def _approval_env(tmp_path: Path, manager: McpManager, settings: AppSettings):
    """构造与 mcp_manager 关联的审批/执行环境。"""
    permission_store = PermissionStore(
        tmp_path / "global-permissions.json",
        tmp_path / ".minic" / "permissions.json",
    )
    tool_executor = ToolExecutor(tmp_path, tmp_path / ".minic" / "backups" / "files")
    approval_manager = ApprovalManager(
        permission_store,
        tmp_path,
        settings,
        mcp_manager=manager,
    )
    tool_log = ToolExecutionLog(tmp_path / ".minic" / "logs" / "tool_execution.jsonl")
    tool_runtime = ToolRuntime(approval_manager, tool_executor, tool_log, mcp_manager=manager)
    return approval_manager, tool_runtime


def test_mcp_auto_approve_bypasses_approval(tmp_path: Path, mock_mcp_server: str) -> None:
    """autoApprove 内的 MCP 工具免审批；未列入的工具仍走审批。"""

    async def main() -> None:
        path = _write_settings(
            tmp_path,
            {"mock": _http_settings(mock_mcp_server, autoApprove=["mock.echo"])},
        )
        manager = McpManager(settings_path=str(path))
        try:
            await manager.connect_all()
            approval_manager, tool_runtime = _approval_env(tmp_path, manager, AppSettings())
            assert approval_manager.plan("mock.echo", {"text": "x"}, "t1") == "auto"
            assert approval_manager.plan("mock.add", {"a": 1, "b": 1}, "t1") == "approval"

            # autoApprove 工具直接执行，无审批事件
            events: list[tuple[str, dict[str, Any]]] = []
            tool_runtime.attach("t1", lambda name, data: events.append((name, data)))
            result = await tool_runtime.execute(
                thread_id="t1",
                run_id="r1",
                tool="mock.echo",
                args={"text": "hi"},
            )
            assert result["status"] == "completed"
            assert result["output"] == "echo:hi"
            assert not any(name == "approval_requested" for name, _ in events)
            assert any(name == "tool_call" for name, _ in events)

            # 未列入 autoApprove 的 MCP 工具发出审批事件，allow_once 后执行
            queue: asyncio.Queue = asyncio.Queue()
            tool_runtime.attach("t2", lambda name, data: queue.put_nowait((name, data)))
            task = asyncio.create_task(
                tool_runtime.execute(
                    thread_id="t2",
                    run_id="r2",
                    tool="mock.add",
                    args={"a": 2, "b": 3},
                )
            )
            seen: list[str] = []
            while True:
                name, data = await asyncio.wait_for(queue.get(), timeout=10)
                seen.append(name)
                if name == "approval_requested":
                    approval_manager.submit("t2", data["id"], "allow_once")
                    break
            result = await task
            assert result["status"] == "completed"
            assert result["output"] == "5"
            assert seen[0] == "tool_call"
        finally:
            await manager.shutdown()

    asyncio.run(main())


# ---------------------------------------------------------------- HTTP 接口


def _wait_mcp_connected(
    client: TestClient,
    timeout: float = 10.0,
    expect: tuple[str, ...] = ("connected", "error"),
) -> dict[str, Any]:
    """轮询 GET /mcp 直到服务进入期望状态。"""
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get("/mcp", headers=AUTH_HEADERS)
        response.raise_for_status()
        last = response.json()
        servers = last.get("servers", [])
        if servers and servers[0]["status"] in expect:
            return last
        time.sleep(0.1)
    return last


def test_get_mcp_endpoint_returns_status(
    settings: AppSettings,
    tmp_path: Path,
    mock_mcp_server: str,
) -> None:
    """GET /mcp 返回各服务状态与工具数（create_app + TestClient 注入配置路径）。"""
    settings_path = _write_settings(tmp_path, {"mock": _http_settings(mock_mcp_server)})
    app = create_app(
        settings=settings,
        token="test-token",
        project_root=tmp_path,
        rag_data_dir=tmp_path / "rag-data",
        short_memory_dir=tmp_path / "memory" / "short_memory",
        mcp_settings_path=settings_path,
    )
    with TestClient(app) as client:
        data = _wait_mcp_connected(client)
        servers = data["servers"]
        assert len(servers) == 1
        assert servers[0]["name"] == "mock"
        assert servers[0]["status"] == "connected"
        assert servers[0]["tools_count"] == 2
        # 连接后注册表能看到 MCP 工具
        assert get_tool("mock.echo") is not None


def test_post_mcp_connect_reconnect(
    settings: AppSettings,
    tmp_path: Path,
) -> None:
    """服务未启动时 POST connect 返回 502；服务恢复后重连返回 200 connected。"""
    port = _free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    settings_path = _write_settings(
        tmp_path,
        {"mock": _http_settings(url, timeout=3)},
    )
    app = create_app(
        settings=settings,
        token="test-token",
        project_root=tmp_path,
        rag_data_dir=tmp_path / "rag-data",
        short_memory_dir=tmp_path / "memory" / "short_memory",
        mcp_settings_path=settings_path,
    )
    with TestClient(app) as client:
        # 服务未启动：重连失败
        response = client.post("/mcp/mock/connect", headers=AUTH_HEADERS)
        assert response.status_code == 502
        assert "MCP_CONNECT_FAILED" in response.text
        # 启动服务后重连成功
        server = _build_mock_server()
        uvicorn_server = uvicorn.Server(
            uvicorn.Config(server.streamable_http_app(), host="127.0.0.1", port=port, log_level="error")
        )
        thread = threading.Thread(target=uvicorn_server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not uvicorn_server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        try:
            response = client.post("/mcp/mock/connect", headers=AUTH_HEADERS)
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "connected"
            assert data["tools_count"] == 2
        finally:
            uvicorn_server.should_exit = True
            thread.join(timeout=5)


def test_mcp_unknown_server_connect_returns_404(
    settings: AppSettings,
    tmp_path: Path,
) -> None:
    """重连不存在的服务返回 404。"""
    settings_path = _write_settings(tmp_path, {"mock": _http_settings("http://127.0.0.1:1/mcp")})
    app = create_app(
        settings=settings,
        token="test-token",
        project_root=tmp_path,
        rag_data_dir=tmp_path / "rag-data",
        short_memory_dir=tmp_path / "memory" / "short_memory",
        mcp_settings_path=settings_path,
    )
    with TestClient(app) as client:
        response = client.post("/mcp/nope/connect", headers=AUTH_HEADERS)
        assert response.status_code == 404


def test_tools_run_executes_mcp_tool(
    settings: AppSettings,
    tmp_path: Path,
    mock_mcp_server: str,
) -> None:
    """/tools/run 走共享 ToolRuntime 执行 MCP 工具（autoApprove 免审批）。"""
    settings_path = _write_settings(
        tmp_path,
        {"mock": _http_settings(mock_mcp_server, autoApprove=["mock.echo"])},
    )
    app = create_app(
        settings=settings,
        token="test-token",
        project_root=tmp_path,
        rag_data_dir=tmp_path / "rag-data",
        short_memory_dir=tmp_path / "memory" / "short_memory",
        mcp_settings_path=settings_path,
    )
    with TestClient(app) as client:
        _wait_mcp_connected(client)
        payload = {
            "thread_id": None,
            "workspace": str(tmp_path),
            "tool": "mock.echo",
            "args": {"text": "hello-mcp"},
        }
        with client.stream("POST", "/tools/run", headers=AUTH_HEADERS, json=payload) as response:
            response.raise_for_status()
            lines = response.iter_lines()
            events: list[str] = []
            outputs: list[str] = []
            for line in lines:
                if line.startswith("event:"):
                    events.append(line[6:].strip())
                elif line.startswith("data:") and events and events[-1] == "tool_result":
                    outputs.append(json.loads(line[5:].strip()).get("output", ""))
        assert "tool_result" in events
        assert "approval_requested" not in events
        assert any("hello-mcp" in output for output in outputs)


def test_get_mcp_invalid_config_returns_400(settings: AppSettings, tmp_path: Path) -> None:
    """MCP 配置文件非法 JSON（如 URL 混入裸 Tab）：GET /mcp 返回 400 带原因，不 500。"""
    bad = tmp_path / "bad_mcp.json"
    bad.write_text('{"mcpServers": {"x": {"url": "https://a.com?key=\tea"}}}', encoding="utf-8")
    app = create_app(
        settings=settings,
        token="test-token",
        project_root=tmp_path,
        rag_data_dir=tmp_path / "rag-data",
        short_memory_dir=tmp_path / "memory" / "short_memory",
        mcp_settings_path=bad,
    )
    client = TestClient(app)
    response = client.get("/mcp", headers=AUTH_HEADERS)
    assert response.status_code == 400
    assert "不是合法 JSON" in response.json()["error"]["message"]
