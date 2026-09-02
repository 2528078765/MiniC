"""G11 断线续传/取消 + checkpointer 测试。

覆盖：SseEventLog 追加/回放/脱敏/24h 清理、RunManager 注册/取消/订阅/发布、
/chat/stream 事件写日志、GET events 回放与续传、POST cancel、
核心重启后 interrupted、MemorySaver 编译。全部使用 tmp_path 隔离。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from langchain_core.callbacks.manager import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

from minic.chat.engine import ChatEngine
from minic.chat.memory import ShortMemoryStore
from minic.chat.models import MockChatModel
from minic.core.config import AppSettings
from minic.core.server import create_app
from minic.graph import build_super_graph
from minic.memory import LongTermMemoryStore
from minic.rag.embeddings import MockEmbeddingProvider
from minic.rag.store import RagStore
from minic.run_manager import RunManager, SseEventLog

HEADERS = {"Authorization": "Bearer test-token"}


class SlowModel(MockChatModel):
    """慢速流式模型：每个 token 间隔 0.15s，便于中途断开/取消。"""

    model_name: str = "slow"
    temperature: float = 0.0

    async def _astream(self, messages, stop=None, run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs):
        del stop, kwargs
        for part in ["这是", "基于资料", "生成的", "模拟回答。"]:
            await asyncio.sleep(0.15)
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=part))
            if run_manager is not None:
                await run_manager.on_llm_new_token(part, chunk=chunk.message)
            yield chunk


class RedactModel(MockChatModel):
    """流式 token 含 sk-xxx 的模型，用于验证日志脱敏。"""

    model_name: str = "redact"
    temperature: float = 0.0

    async def _astream(self, messages, stop=None, run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs):
        del stop, kwargs
        for part in ["密钥是 sk-abcdefghijklmnop", " 正常文本。"]:
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=part))
            if run_manager is not None:
                await run_manager.on_llm_new_token(part, chunk=chunk.message)
            yield chunk


def _settings() -> AppSettings:
    """测试配置（mock 模型/embedding）。"""
    return AppSettings(
        model={"provider": "mock"},
        embedding={"provider": "mock", "dimension": 64},
    )


def _make_app(tmp_path: Path) -> tuple:
    """构造隔离 app（项目根与全部数据目录都在 tmp_path 内）。"""
    settings = _settings()
    app = create_app(
        settings=settings,
        token="test-token",
        project_root=tmp_path,
        rag_data_dir=tmp_path / "rag-data",
        short_memory_dir=tmp_path / "memory" / "short_memory",
        global_memory_dir=tmp_path / "global-memory",
        global_permissions_path=tmp_path / "global-permissions.json",
        skills_global_dir=tmp_path / "skills-global",
        skills_project_dir=tmp_path / ".minic" / "skills",
        global_settings_path=tmp_path / "home" / "minic.json",
    )
    return app, settings


def _slow_app(tmp_path: Path) -> tuple:
    """构造用 SlowModel 的 app（验证中途断开/取消）。"""
    app, settings = _make_app(tmp_path)
    graph = build_super_graph(
        rag_store=app.state.rag_store,
        chat_model=SlowModel(),
        memory_store=app.state.long_term_memory,
        settings=settings,
        tool_runtime=app.state.tool_runtime,
        skill_manager=app.state.skill_manager,
    )
    app.state.chat_engine.graph = graph
    return app, settings


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """把 SSE 文本解析成 (事件名, data) 列表。"""
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


# ---------------------------------------------------------------- SseEventLog / RunManager 单元


def test_sse_log_append_read_after_last_seq(tmp_path: Path) -> None:
    """事件按 id=run_id:seq 追加，read_after/last_seq/has_run 正确。"""
    log = SseEventLog(tmp_path / "sse_events.jsonl")
    log.append("run-1", 1, "message_start", {"run_id": "run-1"})
    log.append("run-1", 2, "token", {"delta": "a"})
    log.append("run-2", 1, "message_start", {"run_id": "run-2"})
    log.append("run-1", 3, "done", {"status": "completed"})

    assert log.has_run("run-1") and log.has_run("run-2")
    assert log.last_seq("run-1") == 3
    assert log.last_seq("run-2") == 1
    assert log.last_seq("run-x") == 0
    after = log.read_after("run-1", 1)
    assert [entry["event"] for entry in after] == ["token", "done"]
    assert [entry["seq"] for entry in after] == [2, 3]
    assert not log.has_done("run-2")
    assert log.has_done("run-1")


def test_sse_log_redacts_pii(tmp_path: Path) -> None:
    """data 中的字符串值（token/tool args）在写入日志前脱敏。"""
    log = SseEventLog(tmp_path / "sse_events.jsonl")
    log.append("run-1", 1, "token", {"delta": "密钥是 sk-abcdefghijklmnop"})
    log.append("run-1", 2, "tool_call", {"tool": "Read", "args": {"path": "sk-abcdefghijklmnop"}})
    text = log.path.read_text(encoding="utf-8")
    assert "[REDACTED_API_KEY]" in text
    assert "sk-abcdefghijklmnop" not in text
    record = log.read_after("run-1", 0)[0]
    assert record["data"]["delta"] == "密钥是 [REDACTED_API_KEY]"
    assert record["event"] == "token"


def test_sse_log_cleanup_expired(tmp_path: Path) -> None:
    """超过 24h 的事件被清理，新事件保留。"""
    log = SseEventLog(tmp_path / "sse_events.jsonl")
    old = (datetime.now().astimezone() - timedelta(hours=25)).isoformat()
    log.path.parent.mkdir(parents=True, exist_ok=True)
    with log.path.open("a", encoding="utf-8") as file:
        file.write(
            json.dumps(
                {"id": "run-old:1", "event": "token", "data": {"delta": "old"}, "timestamp": old}
            )
            + "\n"
        )
    log.append("run-new", 1, "token", {"delta": "new"})
    assert log.cleanup_expired(ttl_hours=24) == 1
    assert log.has_run("run-old") is False
    assert log.has_run("run-new") is True
    # 无过期事件时不再重写
    assert log.cleanup_expired(ttl_hours=24) == 0


def test_mark_interrupted_appends_done(tmp_path: Path) -> None:
    """没有 done 的 run 被补 done(interrupted)，已有 done 的不动且幂等。"""
    log = SseEventLog(tmp_path / "sse_events.jsonl")
    log.append("run-a", 1, "message_start", {"run_id": "run-a"})
    log.append("run-a", 2, "token", {"delta": "半截"})
    log.append("run-b", 1, "message_start", {"run_id": "run-b"})
    log.append("run-b", 2, "done", {"status": "completed"})
    assert log.mark_interrupted() == 1
    assert log.has_done("run-a")
    events = log.read_after("run-a", 2)
    assert events[-1]["event"] == "done"
    assert events[-1]["data"] == {"status": "interrupted"}
    assert log.last_seq("run-b") == 2
    assert log.mark_interrupted() == 0


def test_run_manager_register_cancel_finish_subscribe(tmp_path: Path) -> None:
    """RunManager 注册/查询/取消/结束/订阅/发布。"""
    rm = RunManager(tmp_path / "sse_events.jsonl")
    record = rm.register("r1", "t1")
    assert rm.get("r1") is record
    assert record["status"] == "running" and record["cancelled"] is False

    queue = rm.subscribe("r1")
    assert queue is not None
    rm.publish("r1", 1, "message_start", {"run_id": "r1"})
    assert queue.qsize() == 1
    assert rm.event_log.has_run("r1")

    rm.mark_cancelled("r1")
    assert record["cancelled"] is True
    rm.finish("r1", "cancelled")
    assert record["status"] == "cancelled"
    assert record["queue"] is None
    assert rm.subscribe("r1") is None  # 已结束不可再订阅
    rm.publish("r1", 2, "done", {"status": "cancelled"})
    assert queue.qsize() == 1  # 结束后发布不再入队


# ---------------------------------------------------------------- HTTP：日志写入与脱敏


def test_chat_stream_logs_events_with_run_seq(tmp_path: Path) -> None:
    """/chat/stream 的事件按 id=run_id:seq 写入 sse_events.jsonl。"""
    app, _ = _make_app(tmp_path)
    client = TestClient(app)
    response = client.post("/chat/stream", headers=HEADERS, json={"message": "你好"})
    assert response.status_code == 200
    events = _parse_sse(response.text)
    names = [name for name, _ in events]
    assert names[0] == "message_start"
    assert names[-2:] == ["message_end", "done"]
    run_id = next(data["run_id"] for name, data in events if name == "message_start")

    log_path = tmp_path / ".minic" / "logs" / "sse_events.jsonl"
    records = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    run_records = [record for record in records if str(record["id"]).startswith(run_id + ":")]
    assert len(run_records) == len(events)
    for index, (name, data) in enumerate(events, start=1):
        record = run_records[index - 1]
        assert record["id"] == f"{run_id}:{index}"
        assert record["event"] == name
        assert record["data"] == data
    assert run_records[-1]["event"] == "done"
    assert run_records[-1]["data"]["status"] == "completed"
    assert app.state.run_manager.get(run_id)["status"] == "completed"


def test_chat_stream_log_redacts_pii_but_client_sees_raw(tmp_path: Path) -> None:
    """客户端流看到原始 token，日志中脱敏。"""
    app, settings = _make_app(tmp_path)
    graph = build_super_graph(
        rag_store=app.state.rag_store,
        chat_model=RedactModel(),
        memory_store=app.state.long_term_memory,
        settings=settings,
        tool_runtime=app.state.tool_runtime,
        skill_manager=app.state.skill_manager,
    )
    app.state.chat_engine.graph = graph
    client = TestClient(app)
    response = client.post("/chat/stream", headers=HEADERS, json={"message": "hi"})
    events = _parse_sse(response.text)
    token_text = "".join(data.get("delta", "") for name, data in events if name == "token")
    assert "sk-abcdefghijklmnop" in token_text  # 客户端看到原始值

    text = (tmp_path / ".minic" / "logs" / "sse_events.jsonl").read_text(encoding="utf-8")
    assert "[REDACTED_API_KEY]" in text
    assert "sk-abcdefghijklmnop" not in text


# ---------------------------------------------------------------- HTTP：GET events 回放


def test_events_replay_finished_run(tmp_path: Path) -> None:
    """run 已结束：无 last_event_id 全量回放；有则只回放之后事件并关闭。"""
    app, _ = _make_app(tmp_path)
    client = TestClient(app)
    response = client.post("/chat/stream", headers=HEADERS, json={"message": "你好"})
    events = _parse_sse(response.text)
    run_id = next(data["run_id"] for name, data in events if name == "message_start")

    # 全量回放
    replay = client.get(f"/chat/stream/{run_id}/events", headers=HEADERS)
    assert replay.status_code == 200
    assert replay.headers["content-type"].startswith("text/event-stream")
    replayed = _parse_sse(replay.text)
    assert replayed == events
    assert replayed[-1][1]["status"] == "completed"

    # 只回放之后事件（last_event_id = message_end 的序号）
    message_end_seq = len(events) - 1
    replay2 = client.get(
        f"/chat/stream/{run_id}/events",
        headers=HEADERS,
        params={"last_event_id": f"{run_id}:{message_end_seq}"},
    )
    assert _parse_sse(replay2.text) == events[message_end_seq:]

    # last_event_id 已是 done 序号：无回放内容，补发 done 关闭
    replay3 = client.get(
        f"/chat/stream/{run_id}/events",
        headers=HEADERS,
        params={"last_event_id": f"{run_id}:{len(events)}"},
    )
    tail = _parse_sse(replay3.text)
    assert tail[-1][1]["status"] == "completed"


def test_events_not_found_and_bad_last_event_id(tmp_path: Path) -> None:
    """run 不存在 404；last_event_id 格式错误 400。"""
    app, _ = _make_app(tmp_path)
    client = TestClient(app)
    response = client.post("/chat/stream", headers=HEADERS, json={"message": "hi"})
    run_id = next(data["run_id"] for name, data in _parse_sse(response.text) if name == "message_start")

    assert client.get("/chat/stream/no-such-run/events", headers=HEADERS).status_code == 404
    assert client.get(f"/chat/stream/{run_id}/events", headers=HEADERS, params={"last_event_id": "bad"}).status_code == 400
    assert client.get(f"/chat/stream/{run_id}/events", headers=HEADERS, params={"last_event_id": "other:5"}).status_code == 400
    assert client.get(f"/chat/stream/{run_id}/events", headers=HEADERS, params={"last_event_id": f"{run_id}:abc"}).status_code == 400


# ---------------------------------------------------------------- HTTP：POST cancel


def test_cancel_returns_404_and_409(tmp_path: Path) -> None:
    """不存在的 run cancel 404；已结束的 run cancel 409。"""
    app, _ = _make_app(tmp_path)
    client = TestClient(app)
    assert client.post("/chat/stream/no-run/cancel", headers=HEADERS).status_code == 404

    response = client.post("/chat/stream", headers=HEADERS, json={"message": "hi"})
    run_id = next(data["run_id"] for name, data in _parse_sse(response.text) if name == "message_start")
    cancel = client.post(f"/chat/stream/{run_id}/cancel", headers=HEADERS)
    assert cancel.status_code == 409  # 流已结束
    detail = cancel.json()["error"]
    assert detail["code"] == "CONFLICT"
    assert detail["detail"]["status"] == "completed"


def test_cancel_stops_streaming_with_cancelled_events(tmp_path: Path) -> None:
    """取消后流不再发新 token，收到 message_end(cancelled)+done(cancelled) 关闭。"""
    app, _ = _slow_app(tmp_path)
    client = TestClient(app)
    events: list[tuple[str, dict]] = []

    def stream_thread() -> None:
        # TestClient 会缓冲完整流式响应；结束后解析全部事件
        response = client.post("/chat/stream", headers=HEADERS, json={"message": "hi"})
        events.extend(_parse_sse(response.text))

    thread = threading.Thread(target=stream_thread)
    thread.start()
    # 轮询注册表，等 run 出现且仍 running（慢速模型保证窗口足够）
    run_id = None
    deadline = time.time() + 5
    while time.time() < deadline:
        running = [
            rid
            for rid in app.state.run_manager.runs
            if app.state.run_manager.get(rid)["status"] == "running"
        ]
        if running:
            run_id = running[0]
            break
        time.sleep(0.005)
    assert run_id, "run 未出现在注册表"

    cancel = client.post(f"/chat/stream/{run_id}/cancel", headers=HEADERS)
    assert cancel.status_code == 204
    thread.join(timeout=10)

    names = [name for name, _ in events]
    assert names.count("done") == 1
    assert names[-2:] == ["message_end", "done"]
    assert events[-2][1]["status"] == "cancelled"
    assert events[-1][1]["status"] == "cancelled"
    end_index = names.index("message_end")
    assert "token" not in names[end_index + 1 :]  # 取消后不再发新 token
    assert app.state.run_manager.get(run_id)["status"] == "cancelled"


# ---------------------------------------------------------------- 核心重启：interrupted


def test_events_interrupted_after_restart(tmp_path: Path) -> None:
    """核心重启后未完成 run 标记 interrupted，GET events 回放 interrupted 结束。"""
    app, _ = _make_app(tmp_path)
    TestClient(app)  # 第一次启动，完成日志文件初始化
    log_path = tmp_path / ".minic" / "logs" / "sse_events.jsonl"
    run_manager = RunManager(log_path)
    run_manager.register("run-int", "t1")
    run_manager.publish("run-int", 1, "message_start", {"thread_id": "t1", "run_id": "run-int"})
    run_manager.publish("run-int", 2, "token", {"delta": "半截回答"})

    # 模拟核心重启：同一项目根重新创建 app，create_app 里 mark_interrupted 补 done(interrupted)
    app2, _ = _make_app(tmp_path)
    client2 = TestClient(app2)
    response = client2.get("/chat/stream/run-int/events", headers=HEADERS)
    assert response.status_code == 200
    events = _parse_sse(response.text)
    names = [name for name, _ in events]
    assert names[:2] == ["message_start", "token"]
    assert names[-1] == "done"
    assert events[-1][1]["status"] == "interrupted"


# ---------------------------------------------------------------- 实时续传（ChatEngine 直接驱动）


def test_events_live_continuation_after_partial_disconnect(tmp_path: Path) -> None:
    """慢速模型：第一次连接取部分事件断开，续传拿到剩余事件直到 done，无重复无缺口。"""
    settings = _settings()
    memory_store = LongTermMemoryStore(tmp_path / "global-memory", tmp_path)
    rag_store = RagStore(tmp_path / "rag-data", settings, MockEmbeddingProvider(64))
    graph = build_super_graph(rag_store, SlowModel(), memory_store, settings)
    short_memory = ShortMemoryStore(tmp_path / "memory" / "short_memory")
    thread_id = "t-live"
    short_memory.create(thread_id, str(tmp_path))
    engine = ChatEngine(rag_store=rag_store, short_memory=short_memory, graph=graph)
    run_id = "run-live"
    run_manager = RunManager(tmp_path / ".minic" / "logs" / "sse_events.jsonl")
    run_manager.register(run_id, thread_id)

    async def driver() -> None:
        """模拟服务端 event_stream：逐事件发布到 RunManager。"""
        sequence = 0
        status = "completed"
        try:
            async for event_name, data in engine.stream_chat(
                thread_id=thread_id,
                workspace=str(tmp_path),
                project_root=str(tmp_path),
                message="hi",
                run_id=run_id,
                message_id="m1",
            ):
                sequence += 1
                run_manager.publish(run_id, sequence, event_name, data)
                if event_name == "done":
                    status = data.get("status", "completed")
        finally:
            run_manager.finish(run_id, status)

    async def main() -> None:
        driver_task = asyncio.create_task(driver())
        # 第一次连接：订阅并取前两个事件后“断开”
        queue1 = run_manager.subscribe(run_id)
        first_events: list[tuple[str, dict, int]] = []
        while len(first_events) < 2:
            name, data, seq = await asyncio.wait_for(queue1.get(), timeout=5)
            first_events.append((name, data, seq))
        last_seq = first_events[-1][2]

        # 续传：回放日志中 seq > last_seq 的事件，再订阅实时队列直到 done
        replayed = run_manager.event_log.read_after(run_id, last_seq)
        queue2 = run_manager.subscribe(run_id)
        assert queue2 is not None
        got = list(replayed)
        while not any(entry["event"] == "done" for entry in got):
            name, data, seq = await asyncio.wait_for(queue2.get(), timeout=5)
            got.append({"event": name, "data": data, "seq": seq})
        await driver_task

        all_seq = [entry[2] for entry in first_events] + [entry["seq"] for entry in got]
        assert all_seq == list(range(1, all_seq[-1] + 1)), "续传不应有缺口或重复"
        assert len(set(all_seq)) == len(all_seq)
        assert got[-1]["event"] == "done"
        assert got[-1]["data"]["status"] == "completed"

    asyncio.run(main())


# ---------------------------------------------------------------- checkpointer


def test_build_super_graph_uses_memory_saver(tmp_path: Path) -> None:
    """总图 compile 挂 MemorySaver，编译结果类型不变。"""
    settings = _settings()
    memory_store = LongTermMemoryStore(tmp_path / "global-memory", tmp_path)
    rag_store = RagStore(tmp_path / "rag-data", settings, MockEmbeddingProvider(64))
    graph = build_super_graph(rag_store, MockChatModel(), memory_store, settings)
    assert getattr(graph, "checkpointer", None) is not None
    assert type(graph).__name__ == "CompiledStateGraph"

    async def run() -> None:
        final = {}
        config = {"configurable": {"thread_id": "ms-test"}}
        async for mode, payload in graph.astream(
            {
                "thread_id": "t1",
                "workspace": str(tmp_path),
                "project_root": str(tmp_path),
                "user_message": "hi",
                "history": [],
            },
            config=config,
            stream_mode=["values"],
        ):
            if mode == "values":
                final = payload
        assert final["answer"]

    asyncio.run(run())
