"""S6 崩溃恢复测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from minic.core.config import AppSettings
from minic.core.server import create_app
from minic.recovery import ToolExecutionLog


HEADERS = {"Authorization": "Bearer test-token"}


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """解析 SSE 事件。"""
    events = []
    event_name = None
    data_lines = []
    for line in text.splitlines():
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
    return events


def test_intent_result_log(tmp_path: Path) -> None:
    """intent/result 写入 JSONL 并可读取。"""
    log = ToolExecutionLog(tmp_path / "logs" / "tool_execution.jsonl")
    log.append({"event": "intent", "run_id": "run-1", "tool_call_id": "call-1", "tool": "Read"})
    log.append({"event": "result", "run_id": "run-1", "tool_call_id": "call-1", "status": "success"})
    records = log.read()
    assert len(records) == 2
    assert log.has_result("run-1", "call-1")


def test_mark_interrupted(tmp_path: Path) -> None:
    """没有 result 的 intent 会被标记为 interrupted。"""
    log = ToolExecutionLog(tmp_path / "logs" / "tool_execution.jsonl")
    log.append({"event": "intent", "run_id": "run-1", "tool_call_id": "call-1", "tool": "Write"})
    log.mark_interrupted()
    records = log.read()
    assert len(records) == 2
    assert records[1]["status"] == "interrupted"
    log.mark_interrupted()
    assert len(log.read()) == 2


def test_idempotency_key_stable() -> None:
    """相同 thread/tool/args 的幂等键稳定。"""
    args = {"path": "a.txt", "content": "x"}
    assert ToolExecutionLog.idempotency_key("t", "Write", args) == ToolExecutionLog.idempotency_key(
        "t",
        "Write",
        args,
    )
    assert ToolExecutionLog.idempotency_key("t", "Write", args) != ToolExecutionLog.idempotency_key(
        "t",
        "Write",
        {"path": "a.txt", "content": "y"},
    )


@pytest.fixture
def recovery_client(settings: AppSettings, tmp_path: Path) -> TestClient:
    """使用临时目录的恢复测试客户端。"""
    app = create_app(
        settings=settings,
        token="test-token",
        project_root=tmp_path,
        rag_data_dir=tmp_path / "rag-data",
        short_memory_dir=tmp_path / "memory" / "short_memory",
        global_memory_dir=tmp_path / "global-memory",
        global_permissions_path=tmp_path / "global-permissions.json",
    )
    return TestClient(app)


def test_tool_run_does_not_replay_completed_call(
    recovery_client: TestClient,
    tmp_path: Path,
) -> None:
    """同 run_id + tool_call_id 已有 result 时直接回放，不再产生新 intent。"""
    client = recovery_client
    client.app.state.short_memory.create("thread-1", str(tmp_path))
    target = tmp_path / "a.txt"
    client.app.state.permission_store.grant(
        "project",
        tmp_path,
        "Write",
        str(target),
        "allow_always",
    )
    payload = {
        "thread_id": "thread-1",
        "tool": "Write",
        "args": {"path": str(target), "content": "new"},
        "run_id": "run-1",
        "tool_call_id": "call-1",
    }
    first = client.post("/tools/run", headers=HEADERS, json=payload)
    assert first.status_code == 200
    second = client.post("/tools/run", headers=HEADERS, json=payload)
    assert second.status_code == 200
    records = client.app.state.tool_log.read()
    intents = [record for record in records if record.get("event") == "intent"]
    results = [record for record in records if record.get("event") == "result"]
    assert len(intents) == 1
    assert len(results) == 1
    assert any(name == "tool_result" for name, _ in _parse_sse(second.text))
