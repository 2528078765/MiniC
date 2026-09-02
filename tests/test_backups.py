"""G9 /backups 备份清单与恢复测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from minic.core.config import AppSettings
from minic.core.server import create_app
from minic.tools.service import ToolExecutor


HEADERS = {"Authorization": "Bearer test-token"}


@pytest.fixture
def backup_client(settings: AppSettings, tmp_path: Path) -> TestClient:
    """使用临时项目目录的备份客户端。"""
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


def _file_executor(tmp_path: Path) -> ToolExecutor:
    """使用与核心一致的备份目录构造 ToolExecutor。"""
    return ToolExecutor(tmp_path, tmp_path / ".minic" / "backups" / "files")


def test_backups_list_contains_session_and_file(backup_client: TestClient, tmp_path: Path) -> None:
    """写操作产生 file 备份与 manifest 记录，GET /backups 同时列出 session 与 file。"""
    thread_id = _create_thread(backup_client)
    compress = backup_client.post(f"/threads/{thread_id}/compress", headers=HEADERS)
    assert compress.status_code == 200
    session_backup_id = compress.json()["backup_id"]

    target = tmp_path / "report.md"
    target.write_text("v1", encoding="utf-8")
    result = _file_executor(tmp_path).execute("Write", {"path": str(target), "content": "v2"})
    assert result.status == "success"
    file_backup_id = result.backup_id
    assert file_backup_id

    response = backup_client.get("/backups", headers=HEADERS)
    assert response.status_code == 200
    backups = response.json()["backups"]
    by_id = {entry["id"]: entry for entry in backups}
    assert session_backup_id in by_id
    assert file_backup_id in by_id
    assert by_id[session_backup_id]["type"] == "session"
    assert by_id[file_backup_id]["type"] == "file"
    assert by_id[file_backup_id]["scope"] == "project"
    assert by_id[file_backup_id]["created_at"]
    assert by_id[file_backup_id]["path"].startswith("files/")

    manifest_path = tmp_path / ".minic" / "backups" / "files" / "manifest.jsonl"
    assert manifest_path.exists()
    manifest = manifest_path.read_text(encoding="utf-8")
    assert file_backup_id in manifest
    entries = [json.loads(line) for line in manifest.splitlines() if line.strip()]
    entry = next(item for item in entries if item["backup_id"] == file_backup_id)
    assert Path(entry["original_path"]).resolve() == target.resolve()


def test_file_restore_to_original_path(backup_client: TestClient, tmp_path: Path) -> None:
    """file 备份恢复写回 manifest 记录的原路径，内容正确，覆盖前先备份当前内容。"""
    target = tmp_path / "notes" / "report.md"
    target.parent.mkdir(parents=True)
    target.write_text("old-content", encoding="utf-8")
    result = _file_executor(tmp_path).execute("Write", {"path": str(target), "content": "new-content"})
    assert result.status == "success"
    assert target.read_text(encoding="utf-8") == "new-content"

    response = backup_client.post(f"/backups/{result.backup_id}/restore", headers=HEADERS)
    assert response.status_code == 204
    assert target.read_text(encoding="utf-8") == "old-content"

    files_dir = tmp_path / ".minic" / "backups" / "files"
    assert len(list(files_dir.glob("*.bak"))) == 2  # 覆盖前备份了当前内容，保持可回滚


def test_session_restore_rolls_back_compress(backup_client: TestClient, tmp_path: Path) -> None:
    """session 备份恢复后压缩的会话回滚到原始消息。"""
    thread_id = _create_thread(backup_client)
    compress = backup_client.post(f"/threads/{thread_id}/compress", headers=HEADERS)
    assert compress.status_code == 200
    backup_id = compress.json()["backup_id"]

    resumed = backup_client.post(f"/threads/{thread_id}/resume", headers=HEADERS).json()
    assert "system" in [message["role"] for message in resumed["messages"]]

    response = backup_client.post(f"/backups/{backup_id}/restore", headers=HEADERS)
    assert response.status_code == 204

    resumed = backup_client.post(f"/threads/{thread_id}/resume", headers=HEADERS).json()
    roles = [message["role"] for message in resumed["messages"]]
    assert "system" not in roles
    assert "human" in roles


def test_restore_invalid_id_404(backup_client: TestClient) -> None:
    """无效备份 id 恢复返回 404。"""
    response = backup_client.post("/backups/not-exist/restore", headers=HEADERS)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_backups_requires_auth(backup_client: TestClient) -> None:
    """未鉴权时列表与恢复返回 401。"""
    assert backup_client.get("/backups").status_code == 401
    assert backup_client.post("/backups/x/restore").status_code == 401


def test_backups_global_scope_empty(backup_client: TestClient) -> None:
    """当前仅项目作用域备份，scope=global 返回空列表。"""
    data = backup_client.get("/backups", headers=HEADERS, params={"scope": "global"}).json()
    assert data["backups"] == []


def test_file_restore_rejects_outside_workspace_path(
    backup_client: TestClient, tmp_path: Path
) -> None:
    """manifest 原路径指向工作区外时恢复失败返回 404，目标文件不变。"""
    target = tmp_path / "secret.txt"
    target.write_text("old", encoding="utf-8")
    result = _file_executor(tmp_path).execute("Write", {"path": str(target), "content": "new"})
    assert result.status == "success"
    backup_id = result.backup_id

    manifest_path = tmp_path / ".minic" / "backups" / "files" / "manifest.jsonl"
    outside = str((tmp_path.parent / "evil.txt").resolve())
    lines = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        if entry["backup_id"] == backup_id:
            entry["original_path"] = outside
        lines.append(json.dumps(entry, ensure_ascii=False))
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    response = backup_client.post(f"/backups/{backup_id}/restore", headers=HEADERS)
    assert response.status_code == 404
    assert target.read_text(encoding="utf-8") == "new"
