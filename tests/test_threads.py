"""S3 会话管理测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from minic.core.config import AppSettings
from minic.core.server import create_app


HEADERS = {"Authorization": "Bearer test-token"}


@pytest.fixture
def thread_client(settings: AppSettings, tmp_path: Path) -> TestClient:
    """使用临时项目目录的会话管理客户端。"""
    app = create_app(
        settings=settings,
        token="test-token",
        project_root=tmp_path,
        rag_data_dir=tmp_path / "rag-data",
        short_memory_dir=tmp_path / "memory" / "short_memory",
    )
    return TestClient(app)


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """把 SSE 文本解析成事件列表。"""
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


def _create_thread(client: TestClient) -> str:
    """通过聊天接口创建会话并返回 thread_id。"""
    response = client.post("/chat/stream", headers=HEADERS, json={"message": "你好"})
    assert response.status_code == 200
    done = next(data for name, data in _parse_sse(response.text) if name == "done")
    return done["thread_id"]


def test_list_and_resume_thread(thread_client: TestClient, tmp_path: Path) -> None:
    """新建会话后可列出并可恢复。"""
    thread_id = _create_thread(thread_client)
    threads = thread_client.get(
        "/threads",
        headers=HEADERS,
        params={"workspace": str(tmp_path)},
    )
    assert threads.status_code == 200
    assert any(thread["thread_id"] == thread_id for thread in threads.json()["threads"])

    resume = thread_client.post(f"/threads/{thread_id}/resume", headers=HEADERS)
    assert resume.status_code == 200
    roles = [message["role"] for message in resume.json()["messages"]]
    assert "human" in roles
    assert "ai" in roles


def test_compress_backs_up(thread_client: TestClient, tmp_path: Path) -> None:
    """压缩会话会先备份，并用摘要替换消息。"""
    thread_id = _create_thread(thread_client)
    response = thread_client.post(f"/threads/{thread_id}/compress", headers=HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert data["summary"]
    assert data["backup_id"]
    backup_path = tmp_path / ".minic" / "backups" / "sessions" / f"{data['backup_id']}.json"
    assert backup_path.exists()

    resume = thread_client.post(f"/threads/{thread_id}/resume", headers=HEADERS)
    assert resume.status_code == 200
    roles = [message["role"] for message in resume.json()["messages"]]
    assert "system" in roles


def test_archive_and_unarchive(thread_client: TestClient, tmp_path: Path) -> None:
    """归档后可从归档列表看到，取消归档后可恢复。"""
    thread_id = _create_thread(thread_client)
    assert thread_client.post(f"/threads/{thread_id}/archive", headers=HEADERS).status_code == 204
    archived = thread_client.get(
        "/threads",
        headers=HEADERS,
        params={"workspace": str(tmp_path), "archived": "true"},
    )
    assert any(thread["thread_id"] == thread_id for thread in archived.json()["threads"])

    assert thread_client.post(f"/threads/{thread_id}/unarchive", headers=HEADERS).status_code == 204
    assert thread_client.post(f"/threads/{thread_id}/unarchive", headers=HEADERS).status_code == 409


def test_delete_backs_up(thread_client: TestClient, tmp_path: Path) -> None:
    """删除会话前会备份，删除后恢复返回 404。"""
    thread_id = _create_thread(thread_client)
    response = thread_client.request("DELETE", f"/threads/{thread_id}", headers=HEADERS)
    assert response.status_code == 200
    backup_id = response.json()["backup_id"]
    assert (tmp_path / ".minic" / "backups" / "sessions" / f"{backup_id}.json").exists()
    assert thread_client.post(f"/threads/{thread_id}/resume", headers=HEADERS).status_code == 404
