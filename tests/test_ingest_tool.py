"""G13 IngestDirectory 入库工具测试：审批中断（options/message）、deny 文案、allow_always 持久化、沙箱写放行、默认目录、幂等。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from minic.core.config import AppSettings
from minic.core.server import create_app
from minic.graph import get_tool
from minic.memory import LongTermMemoryStore
from minic.middleware import SandboxPolicy
from minic.rag.embeddings import MockEmbeddingProvider
from minic.rag.store import RagStore
from minic.recovery import ToolExecutionLog
from minic.tools.runtime import ToolRuntime
from minic.tools.service import ApprovalManager, PermissionStore, ToolExecutor

HEADERS = {"Authorization": "Bearer test-token"}


def make_settings(
    tmp_path: Path,
    *,
    default_directory: str | None = None,
    allowed_write_dirs: list[str] | None = None,
) -> AppSettings:
    """带 mock 模型/mock embedding、可选 default_directory 与 allowed_write_dirs 的测试配置。"""
    return AppSettings(
        model={"provider": "mock"},
        embedding={"provider": "mock", "dimension": 64},
        rag={
            "chunk_size": 500,
            "chunk_overlap": 100,
            "top_k": 5,
            "bm25_weight": 0.45,
            "vector_weight": 0.55,
            "default_directory": default_directory,
        },
        sandbox={
            "allowed_write_dirs": allowed_write_dirs or [],
        },
    )


def make_client(tmp_path: Path, settings: AppSettings) -> TestClient:
    """用临时项目根/临时 RAG 数据/临时权限与记忆目录创建客户端（双库隔离真实用户数据目录）。"""
    project_root = tmp_path / "project"
    app = create_app(
        settings=settings,
        token="test-token",
        project_root=project_root,
        rag_data_dir=tmp_path / "rag-data",
        short_memory_dir=tmp_path / "memory" / "short_memory",
        global_memory_dir=tmp_path / "home" / ".minic" / "memory",
        global_permissions_path=tmp_path / "home" / ".minic" / "permissions.json",
        skills_global_dir=tmp_path / "home" / ".minic" / "skills",
        skills_project_dir=project_root / ".minic" / "skills",
    )
    return TestClient(app)


def make_kb(tmp_path: Path, name: str = "kb") -> Path:
    """构造含 1 个 Markdown 文件的知识库目录。"""
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    (folder / "doc.md").write_text(
        "# 入库文档\n\n这是入库工具测试内容，包含唯一关键词：菠萝蜜火山岩。",
        encoding="utf-8",
    )
    return folder


def _ingest_payload(kb_dir: Path) -> dict:
    """构造 IngestDirectory 工具调用载荷。"""
    return {"tool": "IngestDirectory", "args": {"path": str(kb_dir)}}


def _run_tool_with_decision(
    client: TestClient,
    payload: dict,
    decision: str | None = None,
) -> list[tuple[str, dict]]:
    """消费 /tools/run SSE 流；审批事件由后台线程提交 decision。

    TestClient 的 client.stream 会缓冲整个响应（ASGI 应用跑完才返回），主线程无法在
    流中间感知审批事件，因此后台线程轮询 app.state 的 pending 审批并提交决策；
    decision 为 None 时不提交（用于免审批路径）。
    """
    events: list[tuple[str, dict]] = []

    def submit_loop() -> None:
        """后台线程：轮询 pending 审批，出现与本次工具匹配的待审批请求时提交决策。"""
        if decision is None:
            return
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            manager = client.app.state.approval_manager
            for approval_id, approval in list(manager.pending.items()):
                if approval.status == "pending" and approval.tool == payload["tool"]:
                    response = client.post(
                        f"/threads/{approval.thread_id}/approve",
                        headers=HEADERS,
                        json={"approval_id": approval_id, "decision": decision},
                    )
                    response.raise_for_status()
                    return
            time.sleep(0.02)

    worker = threading.Thread(target=submit_loop, daemon=True)
    worker.start()
    try:
        with client.stream("POST", "/tools/run", headers=HEADERS, json=payload) as response:
            response.raise_for_status()  # 响应已被 TestClient 完整缓冲
            event_name = None
            data_lines: list[str] = []
            for line in response.iter_lines():
                if line == "":
                    if event_name and data_lines:
                        events.append((event_name, json.loads("\n".join(data_lines))))
                    event_name = None
                    data_lines = []
                    continue
                if line.startswith("event:"):
                    event_name = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:") :].strip())
    finally:
        worker.join(timeout=5.0)
    return events


# ---------------------------------------------------------------- 注册表

def test_ingest_tool_registered() -> None:
    """注册表包含 IngestDirectory，参数为可选 path，描述含人工审批与写总结工作流引导。"""
    spec = get_tool("IngestDirectory")
    assert spec is not None
    assert spec.category == "exec"
    assert "增量入库" in spec.description
    assert "人工审批" in spec.description
    assert "embedding" in spec.description
    assert "Write" in spec.description  # 「Write 总结 → IngestDirectory 入库」工作流引导


# ---------------------------------------------------------------- 审批：单元

def test_ingest_needs_approval_and_options(tmp_path: Path) -> None:
    """IngestDirectory 默认需审批；options 只含 allow_once/allow_always/deny；allow_session 提交报错。"""
    settings = make_settings(tmp_path)
    permission_store = PermissionStore(tmp_path / "g.json", tmp_path / ".minic" / "permissions.json")
    manager = ApprovalManager(permission_store, tmp_path, settings)
    args = {"path": str(tmp_path / "kb")}
    assert manager._needs_approval("IngestDirectory", args) is True
    assert manager.plan("IngestDirectory", args, "t") == "approval"
    approval = manager.request("t", "IngestDirectory", args)
    assert approval.options == ["allow_once", "allow_always", "deny"]

    with pytest.raises(ValueError, match="IngestDirectory"):
        manager.submit("t", approval.id, "allow_session")

    approval2 = manager.request("t", "IngestDirectory", args)
    manager.submit("t", approval2.id, "allow_always")
    assert approval2.status == "approved"
    assert approval2.decision == "allow_always"


def test_ingest_deny_permission_has_priority(tmp_path: Path) -> None:
    """IngestDirectory 已有 deny 记录时直接拒绝。"""
    settings = make_settings(tmp_path)
    permission_store = PermissionStore(tmp_path / "g.json", tmp_path / ".minic" / "permissions.json")
    manager = ApprovalManager(permission_store, tmp_path, settings)
    path = str(tmp_path / "kb")
    permission_store.grant("project", tmp_path, "IngestDirectory", path, "deny")
    assert manager.plan("IngestDirectory", {"path": path}, "t") == "deny"


# ---------------------------------------------------------------- 沙箱放行：单元

def test_sandbox_allowed_write_dirs() -> None:
    """写工具路径在 allowed_write_dirs 白名单内（工作区外）放行；白名单外工作区外仍拒绝；读工具不加白名单。"""
    workspace = Path("/ws/project")
    kb = Path("/ws/kb")
    policy = SandboxPolicy(allowed_write_dirs=[str(kb)])
    assert policy.check_tool("Write", {"path": str(kb / "a.md")}, workspace) is None
    assert policy.check_tool("Edit", {"path": str(kb / "a.md")}, workspace) is None
    assert policy.check_tool("Format", {"path": str(kb / "a.py")}, workspace) is None
    assert policy.check_tool("Write", {"path": str(Path("/ws/other/a.md"))}, workspace) is not None
    # 读工具白名单外工作区外仍拒绝，不加白名单（避免范围扩大）
    assert policy.check_tool("Read", {"path": str(kb / "a.md")}, workspace) is not None
    # 工作区内写工具不受影响
    assert policy.check_tool("Write", {"path": str(workspace / "a.md")}, workspace) is None
    # 未配置白名单时工作区外写工具仍拒绝
    assert SandboxPolicy().check_tool("Write", {"path": str(kb / "a.md")}, workspace) is not None


# ---------------------------------------------------------------- ToolRuntime：执行与审批事件

def _runtime_env(
    tmp_path: Path,
    *,
    default_directory: str | None = None,
    allowed_write_dirs: list[str] | None = None,
) -> dict:
    """构造注入 rag_store 与沙箱策略的隔离工具环境。"""
    settings = make_settings(tmp_path, default_directory=default_directory, allowed_write_dirs=allowed_write_dirs)
    rag_store = RagStore(tmp_path / "rag-data", settings, MockEmbeddingProvider(64))
    memory_store = LongTermMemoryStore(tmp_path / "global-memory", tmp_path)
    permission_store = PermissionStore(tmp_path / "g.json", tmp_path / ".minic" / "permissions.json")
    executor = ToolExecutor(
        tmp_path,
        tmp_path / ".minic" / "backups" / "files",
        memory_store,
        allowed_write_dirs=allowed_write_dirs,
    )
    approval_manager = ApprovalManager(permission_store, tmp_path, settings)
    tool_log = ToolExecutionLog(tmp_path / ".minic" / "logs" / "tool_execution.jsonl")
    runtime = ToolRuntime(
        approval_manager,
        executor,
        tool_log,
        sandbox_policy=SandboxPolicy(allowed_write_dirs=allowed_write_dirs),
        rag_store=rag_store,
    )
    return {"runtime": runtime, "approval": approval_manager, "root": tmp_path, "rag": rag_store}


async def _run_with_decision(
    runtime: ToolRuntime,
    thread_id: str,
    tool: str,
    args: dict,
    decision: str | None = None,
):
    """并发消费工具事件，审批时按 decision 提交。"""
    queue: asyncio.Queue = asyncio.Queue()
    runtime.attach(thread_id, lambda name, data: queue.put_nowait((name, data)))
    events: list[tuple[str, dict]] = []

    async def execute() -> dict:
        return await runtime.execute(
            thread_id=thread_id,
            run_id="run-1",
            tool=tool,
            args=args,
        )

    task = asyncio.create_task(execute())
    while not task.done():
        try:
            name, data = await asyncio.wait_for(queue.get(), timeout=10)
        except asyncio.TimeoutError:
            break
        events.append((name, data))
        if name == "approval_requested" and decision is not None:
            runtime.approval_manager.submit(thread_id, data["id"], decision)
    await task
    while not queue.empty():
        events.append(queue.get_nowait())
    runtime.detach(thread_id)
    return task.result(), events


def test_runtime_ingest_allow_once_and_message(tmp_path: Path) -> None:
    """allow_once 后入库成功返回统计；approval_requested 含 options 与 message。"""
    kb = make_kb(tmp_path)
    env = _runtime_env(tmp_path)
    result, events = asyncio.run(
        _run_with_decision(env["runtime"], "t1", "IngestDirectory", {"path": str(kb)}, "allow_once")
    )
    assert result["status"] == "completed"
    assert "已入库 1 篇" in result["output"]
    assert "跳过 0 篇" in result["output"]
    approvals = [data for name, data in events if name == "approval_requested"]
    assert approvals
    assert approvals[0]["options"] == ["allow_once", "allow_always", "deny"]
    assert approvals[0]["message"] == f"将把 {kb} 增量入库到 RAG 知识库"
    assert len(env["rag"].list_documents()[0]) == 1


def test_runtime_ingest_deny_output(tmp_path: Path) -> None:
    """deny 后 status=denied、output=「用户拒绝入库」，知识库未新增文档。"""
    kb = make_kb(tmp_path)
    env = _runtime_env(tmp_path)
    result, events = asyncio.run(
        _run_with_decision(env["runtime"], "t2", "IngestDirectory", {"path": str(kb)}, "deny")
    )
    assert result["status"] == "denied"
    assert result["output"] == "用户拒绝入库"
    assert any(name == "approval_result" for name, _ in events)
    assert env["rag"].list_documents()[0] == []


def test_runtime_ingest_idempotent(tmp_path: Path) -> None:
    """重复入库同目录第二次 skipped，不重复入库。"""
    kb = make_kb(tmp_path)
    env = _runtime_env(tmp_path)
    first, _ = asyncio.run(
        _run_with_decision(env["runtime"], "t3", "IngestDirectory", {"path": str(kb)}, "allow_once")
    )
    assert first["status"] == "completed"
    assert "已入库 1 篇" in first["output"]
    second, _ = asyncio.run(
        _run_with_decision(env["runtime"], "t4", "IngestDirectory", {"path": str(kb)}, "allow_once")
    )
    assert second["status"] == "completed"
    assert "已入库 0 篇" in second["output"]
    assert "跳过 1 篇" in second["output"]
    assert len(env["rag"].list_documents()[0]) == 1  # 不重复入库


# ---------------------------------------------------------------- HTTP：审批中断

def test_http_ingest_approval_allow_once_then_query(tmp_path: Path) -> None:
    """/tools/run IngestDirectory：approval_requested（options/message）→ allow_once → 入库可检索。"""
    kb = make_kb(tmp_path)
    settings = make_settings(tmp_path, default_directory=str(kb), allowed_write_dirs=[str(kb)])
    with make_client(tmp_path, settings) as client:
        events = _run_tool_with_decision(client, _ingest_payload(kb), decision="allow_once")
        approvals = [data for name, data in events if name == "approval_requested"]
        assert approvals
        assert approvals[0]["options"] == ["allow_once", "allow_always", "deny"]
        assert "将把" in approvals[0]["message"]
        assert str(kb) in approvals[0]["message"]
        results = [data for name, data in events if name == "tool_result"]
        assert results and results[0]["status"] == "success"
        assert "已入库 1 篇" in results[0]["output"]
        # 入库后 /rag/query 可检索到
        response = client.get("/rag/query", headers=HEADERS, params={"q": "菠萝蜜火山岩", "top_k": 3})
        assert response.status_code == 200
        assert response.json()["results"]


def test_http_ingest_deny_output_and_no_documents(tmp_path: Path) -> None:
    """deny 后 tool_result status=denied、output=「用户拒绝入库」，知识库未新增文档。"""
    kb = make_kb(tmp_path)
    settings = make_settings(tmp_path, default_directory=str(kb), allowed_write_dirs=[str(kb)])
    with make_client(tmp_path, settings) as client:
        events = _run_tool_with_decision(client, _ingest_payload(kb), decision="deny")
        results = [data for name, data in events if name == "tool_result"]
        assert results and results[0]["status"] == "denied"
        assert results[0]["output"] == "用户拒绝入库"
        documents = client.get("/rag/documents", headers=HEADERS).json()["documents"]
        assert documents == []


def test_http_ingest_allow_always_persisted_and_revoked(tmp_path: Path) -> None:
    """allow_always 持久化 permissions.json；同路径免审批；撤销后恢复审批。"""
    kb = make_kb(tmp_path)
    settings = make_settings(tmp_path, default_directory=str(kb), allowed_write_dirs=[str(kb)])
    with make_client(tmp_path, settings) as client:
        # 首次调用 allow_always
        events = _run_tool_with_decision(client, _ingest_payload(kb), decision="allow_always")
        assert any(name == "approval_requested" for name, _ in events)
        results = [data for name, data in events if name == "tool_result"]
        assert results and results[0]["status"] == "success"
        # permissions.json 落盘
        permissions_path = tmp_path / "project" / ".minic" / "permissions.json"
        permissions = json.loads(permissions_path.read_text(encoding="utf-8"))["permissions"]
        entries = [entry for entry in permissions if entry["name"] == "IngestDirectory"]
        assert len(entries) == 1
        assert entries[0]["mode"] == "allow_always"
        permission_id = entries[0]["id"]
        # 同路径再次调用免审批（直接执行）
        events2 = _run_tool_with_decision(client, _ingest_payload(kb))
        assert "approval_requested" not in [name for name, _ in events2]
        results2 = [data for name, data in events2 if name == "tool_result"]
        assert results2 and results2[0]["status"] == "success"
        assert "跳过 1 篇" in results2[0]["output"]
        # 撤销权限后恢复审批
        response = client.delete(f"/permissions/{permission_id}", headers=HEADERS)
        assert response.status_code == 204
        events3 = _run_tool_with_decision(client, _ingest_payload(kb), decision="allow_once")
        assert any(name == "approval_requested" for name, _ in events3)


# ---------------------------------------------------------------- HTTP：默认目录

def test_http_ingest_default_directory_when_no_path(tmp_path: Path) -> None:
    """不传 path 时使用 rag.default_directory，message 与实际入库目标一致。"""
    kb = make_kb(tmp_path)
    settings = make_settings(tmp_path, default_directory=str(kb), allowed_write_dirs=[str(kb)])
    with make_client(tmp_path, settings) as client:
        events = _run_tool_with_decision(client, {"tool": "IngestDirectory", "args": {}}, decision="allow_once")
        approvals = [data for name, data in events if name == "approval_requested"]
        assert approvals
        assert "将把" in approvals[0]["message"]
        assert str(kb) in approvals[0]["message"]
        results = [data for name, data in events if name == "tool_result"]
        assert results and results[0]["status"] == "success"
        assert "已入库 1 篇" in results[0]["output"]
        documents = client.get("/rag/documents", headers=HEADERS).json()["documents"]
        assert len(documents) == 1


def test_http_ingest_no_path_and_no_default_directory_fails(tmp_path: Path) -> None:
    """未传 path 且未配置 rag.default_directory 时返回 failed 提示。"""
    settings = make_settings(tmp_path)
    with make_client(tmp_path, settings) as client:
        events = _run_tool_with_decision(client, {"tool": "IngestDirectory", "args": {}}, decision="allow_once")
        results = [data for name, data in events if name == "tool_result"]
        assert results and results[0]["status"] == "failed"
        assert "default_directory" in results[0]["output"]


# ---------------------------------------------------------------- HTTP：沙箱写放行

def test_http_sandbox_allowed_write_dirs_write_passes(tmp_path: Path) -> None:
    """allowed_write_dirs 内（工作区外）Write 不被沙箱拦截，仍走审批，审批后实际写入成功。"""
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    settings = make_settings(tmp_path, default_directory=str(kb_dir), allowed_write_dirs=[str(kb_dir)])
    with make_client(tmp_path, settings) as client:
        target = kb_dir / "topic.md"
        events = _run_tool_with_decision(
            client,
            {"tool": "Write", "args": {"path": str(target), "content": "# 总结\n\n知识库总结内容。"}},
            decision="allow_once",
        )
        names = [name for name, _ in events]
        assert "approval_requested" in names  # 沙箱放行但审批是另一道闸
        results = [data for name, data in events if name == "tool_result"]
        assert results and results[0]["status"] == "success"
        assert target.exists()  # 白名单内工作区外实际写入成功


def test_http_sandbox_write_outside_whitelist_denied(tmp_path: Path) -> None:
    """白名单外工作区外 Write 被沙箱 denied「沙箱策略限制」，不进入审批。"""
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    settings = make_settings(tmp_path, allowed_write_dirs=[str(kb_dir)])
    with make_client(tmp_path, settings) as client:
        outside = tmp_path.parent / "g13-outside.txt"
        events = _run_tool_with_decision(
            client,
            {"tool": "Write", "args": {"path": str(outside), "content": "x"}},
        )
        names = [name for name, _ in events]
        assert "approval_requested" not in names
        results = [data for name, data in events if name == "tool_result"]
        assert results and results[0]["status"] == "denied"
        assert "沙箱策略限制" in results[0]["output"]
        assert not outside.exists()


# ---------------------------------------------------------------- HTTP：幂等

def test_http_ingest_idempotent_second_skipped(tmp_path: Path) -> None:
    """重复调用 IngestDirectory 同目录：第二次 skipped，不重复入库。"""
    kb = make_kb(tmp_path)
    settings = make_settings(tmp_path, default_directory=str(kb), allowed_write_dirs=[str(kb)])
    with make_client(tmp_path, settings) as client:
        first = _run_tool_with_decision(client, _ingest_payload(kb), decision="allow_once")
        first_results = [data for name, data in first if name == "tool_result"]
        assert first_results and first_results[0]["status"] == "success"
        assert "已入库 1 篇" in first_results[0]["output"]
        second = _run_tool_with_decision(client, _ingest_payload(kb), decision="allow_once")
        second_results = [data for name, data in second if name == "tool_result"]
        assert second_results and second_results[0]["status"] == "success"
        assert "已入库 0 篇" in second_results[0]["output"]
        assert "跳过 1 篇" in second_results[0]["output"]
        documents = client.get("/rag/documents", headers=HEADERS).json()["documents"]
        assert len(documents) == 1
