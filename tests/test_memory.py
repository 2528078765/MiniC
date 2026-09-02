"""S4 长期记忆测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from minic.core.config import AppSettings
from minic.core.server import create_app
from minic.memory import LongTermMemoryStore


HEADERS = {"Authorization": "Bearer test-token"}


@pytest.fixture
def memory_client(settings: AppSettings, tmp_path: Path) -> tuple[TestClient, Path]:
    """使用临时全局记忆目录的长期记忆客户端。"""
    app = create_app(
        settings=settings,
        token="test-token",
        project_root=tmp_path,
        rag_data_dir=tmp_path / "rag-data",
        short_memory_dir=tmp_path / "memory" / "short_memory",
        global_memory_dir=tmp_path / "global-memory",
    )
    return TestClient(app), tmp_path


def test_global_and_project_scope(memory_client: tuple[TestClient, Path]) -> None:
    """全局与项目记忆分开存储，merged 可合并查看。"""
    client, root = memory_client
    response = client.post(
        "/memory",
        headers=HEADERS,
        json={"scope": "global", "content": "# 全局\n全局内容"},
    )
    assert response.status_code == 204

    global_data = client.get("/memory", headers=HEADERS, params={"scope": "global"}).json()
    assert "全局内容" in global_data["content"]

    project_data = client.get(
        "/memory",
        headers=HEADERS,
        params={"scope": "project", "workspace": str(root)},
    ).json()
    assert project_data["content"] == ""

    merged = client.get(
        "/memory",
        headers=HEADERS,
        params={"scope": "merged", "workspace": str(root)},
    ).json()
    assert "全局内容" in merged["content"]


def test_merge_keeps_user_content(memory_client: tuple[TestClient, Path]) -> None:
    """merge 时用户已有内容优先，不被推断内容覆盖。"""
    client, root = memory_client
    client.post(
        "/memory",
        headers=HEADERS,
        json={
            "scope": "project",
            "workspace": str(root),
            "content": "# 主题\n用户内容",
        },
    )
    version = client.get(
        "/memory",
        headers=HEADERS,
        params={"scope": "project", "workspace": str(root)},
    ).json()["version"]
    client.post(
        "/memory",
        headers=HEADERS,
        json={
            "scope": "project",
            "workspace": str(root),
            "content": "# 主题\n推断内容",
            "mode": "merge",
            "base_version": version,
        },
    )
    content = client.get(
        "/memory",
        headers=HEADERS,
        params={"scope": "project", "workspace": str(root)},
    ).json()["content"]
    assert "用户内容" in content
    assert "推断内容" not in content


def test_memory_version_conflict(memory_client: tuple[TestClient, Path]) -> None:
    """base_version 不匹配时返回 409 和当前内容。"""
    client, root = memory_client
    first = client.post(
        "/memory",
        headers=HEADERS,
        json={"scope": "project", "workspace": str(root), "content": "# A\n1"},
    )
    assert first.status_code == 204
    old_version = client.get(
        "/memory",
        headers=HEADERS,
        params={"scope": "project", "workspace": str(root)},
    ).json()["version"]
    client.post(
        "/memory",
        headers=HEADERS,
        json={"scope": "project", "workspace": str(root), "content": "# A\n2"},
    )
    conflict = client.post(
        "/memory",
        headers=HEADERS,
        json={
            "scope": "project",
            "workspace": str(root),
            "content": "# A\n3",
            "base_version": old_version,
        },
    )
    assert conflict.status_code == 409
    assert "2" in conflict.json()["error"]["detail"]["current_content"]


def test_delete_marker_prevents_rewrite(memory_client: tuple[TestClient, Path]) -> None:
    """删除标记生效后，add_topic 不会重写该主题。"""
    client, root = memory_client
    store = LongTermMemoryStore(root / "global-memory", root)
    store.add_topic("project", str(root), "旧主题", "旧内容", source="inferred")
    store.mark_deleted("project", str(root), "旧主题")
    assert store.add_topic("project", str(root), "旧主题", "新内容", source="user") is False
    data = client.get(
        "/memory",
        headers=HEADERS,
        params={"scope": "project", "workspace": str(root)},
    ).json()
    assert "旧主题" not in data["content"]


def test_add_topic_dedupe_and_user_priority(memory_client: tuple[TestClient, Path]) -> None:
    """自动写入去重，用户内容优先于推断内容。"""
    client, root = memory_client
    store = LongTermMemoryStore(root / "global-memory", root)
    assert store.add_topic("project", str(root), "偏好", "推断A", source="inferred") is True
    assert store.add_topic("project", str(root), "偏好", "推断A", source="inferred") is False
    assert store.add_topic("project", str(root), "偏好", "用户B", source="user") is True
    data = client.get(
        "/memory",
        headers=HEADERS,
        params={"scope": "project", "workspace": str(root)},
    ).json()
    assert "用户B" in data["content"]
    assert "推断A" not in data["content"]
