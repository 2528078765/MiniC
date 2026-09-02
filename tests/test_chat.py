"""S1 流式问答测试。"""

from pathlib import Path

from fastapi.testclient import TestClient

from minic.graph import unwrap_json_answer


HEADERS = {"Authorization": "Bearer test-token"}


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """把 SSE 文本解析成事件列表。"""
    events = []
    event_name = None
    data_lines = []
    for line in text.splitlines():
        if line == "":
            if event_name and data_lines:
                import json

                events.append((event_name, json.loads("\n".join(data_lines))))
            event_name = None
            data_lines = []
            continue
        if line.startswith("event:"):
            event_name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())
    return events


def _ingest(client: TestClient, markdown_dir: Path) -> None:
    """测试前置入库。"""
    response = client.post(
        "/rag/ingest",
        headers=HEADERS,
        json={"path": str(markdown_dir), "extensions": [".md"]},
    )
    assert response.status_code == 200


def _token_text(events: list[tuple[str, dict]]) -> str:
    """拼接所有 token 事件文本。"""
    return "".join(data["delta"] for name, data in events if name == "token")


def test_chat_stream_returns_answer_and_sources(client: TestClient, markdown_dir: Path) -> None:
    """流式回答包含 token 事件与来源。"""
    _ingest(client, markdown_dir)
    response = client.post(
        "/chat/stream",
        headers=HEADERS,
        json={"message": "LangGraph 的作用是什么"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(response.text)
    names = [name for name, _ in events]
    assert "message_start" in names
    assert "token" in names
    assert "message_end" in names
    assert "done" in names
    done = next(data for name, data in events if name == "done")
    assert done["status"] == "completed"
    assert done["sources"]
    assert done["sources"][0]["file_path"].endswith("guide.md")
    assert done["sources"][0]["section"]
    assert _token_text(events) == "这是基于资料生成的模拟回答。"


def test_follow_up_uses_same_thread(client: TestClient, markdown_dir: Path) -> None:
    """追问可以复用会话并继续获得来源。"""
    _ingest(client, markdown_dir)
    first = client.post("/chat/stream", headers=HEADERS, json={"message": "LangGraph 的作用是什么"})
    first_events = _parse_sse(first.text)
    first_done = next(data for name, data in first_events if name == "done")
    thread_id = first_done["thread_id"]

    second = client.post(
        "/chat/stream",
        headers=HEADERS,
        json={"thread_id": thread_id, "message": "它们的作用是什么"},
    )
    assert second.status_code == 200
    second_events = _parse_sse(second.text)
    second_done = next(data for name, data in second_events if name == "done")
    assert second_done["status"] == "completed"
    assert second_done["sources"]
    assert "LangGraph 的作用" not in _token_text(second_events)


def test_unwrap_json_answer_returns_text() -> None:
    """模型偶发 JSON 包装应被解包为纯文本。"""
    assert unwrap_json_answer('```json\n{"answer": "纯文本回答"}\n```') == "纯文本回答"
    assert unwrap_json_answer('{"content": "正文回答"}') == "正文回答"
