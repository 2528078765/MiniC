"""G9 PUT /settings 局部更新、持久化与 api_key 拒绝测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from minic.core.config import AppSettings, load_settings
from minic.core.server import create_app


HEADERS = {"Authorization": "Bearer test-token"}


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    """把 Path.home() 指向临时目录，隔离真实 ~/.minic。"""
    home = tmp_path / "fake-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def settings_client(settings: AppSettings, tmp_path: Path, isolated_home: Path) -> TestClient:
    """使用临时项目目录与隔离全局配置路径的设置客户端（双库隔离）。"""
    app = create_app(
        settings=settings,
        token="test-token",
        project_root=tmp_path,
        rag_data_dir=tmp_path / "rag-data",
        short_memory_dir=tmp_path / "memory" / "short_memory",
        global_settings_path=isolated_home / ".minic" / "minic.json",
    )
    return TestClient(app)


def test_put_settings_nested_dict_merge(
    settings_client: TestClient, tmp_path: Path, isolated_home: Path
) -> None:
    """只改 model.temperature 时嵌套合并，其余字段保持不变（model 仅全局，用 global scope）。"""
    response = settings_client.put(
        "/settings", headers=HEADERS, json={"scope": "global", "model": {"temperature": 0.1}}
    )
    assert response.status_code == 204

    config_path = isolated_home / ".minic" / "minic.json"
    assert config_path.exists()
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["model"]["temperature"] == 0.1

    loaded = load_settings(tmp_path)
    assert loaded.model.primary().temperature == 0.1
    assert loaded.model.primary().provider == "deepseek"  # 未出现字段使用默认值
    assert loaded.model.primary().model == "deepseek-v4-flash"


def test_put_settings_absent_fields_unchanged(
    settings_client: TestClient, tmp_path: Path, isolated_home: Path
) -> None:
    """未出现在请求体中的字段保持不变。"""
    response = settings_client.put(
        "/settings", headers=HEADERS, json={"scope": "global", "model": {"temperature": 0.2}}
    )
    assert response.status_code == 204
    loaded = load_settings(tmp_path)
    assert loaded.model.primary().temperature == 0.2
    assert loaded.sandbox.command_timeout == 60
    assert loaded.rag.top_k == 5
    assert loaded.approval.workspace_write_auto_approve is False


def test_put_settings_array_replaced_wholesale(
    settings_client: TestClient, tmp_path: Path, isolated_home: Path
) -> None:
    """数组整体替换而不是追加。"""
    response = settings_client.put(
        "/settings",
        headers=HEADERS,
        json={"sandbox": {"model_api_whitelist": ["http://example.com"]}},
    )
    assert response.status_code == 204
    loaded = load_settings(tmp_path)
    assert loaded.sandbox.model_api_whitelist == ["http://example.com"]


def test_put_settings_allows_model_api_key_and_get_strips(settings_client: TestClient, tmp_path: Path) -> None:
    """模型列表允许写入 api_key（本地明文存储，global scope）；GET /settings 响应剔除。"""
    response = settings_client.put(
        "/settings",
        headers=HEADERS,
        json={
            "scope": "global",
            "model": {
                "models": [
                    {
                        "name": "自定义",
                        "provider": "openai",
                        "base_url": "http://127.0.0.1:11434",
                        "model": "llama3",
                        "api_key": "sk-test",
                        "enabled": True,
                    }
                ]
            },
        },
    )
    assert response.status_code == 204
    loaded = load_settings(tmp_path)
    assert loaded.model.models[0].api_key == "sk-test"
    # GET 剔除所有 api_key（含列表内）
    data = settings_client.get("/settings", headers=HEADERS).json()
    assert "sk-test" not in json.dumps(data, ensure_ascii=False)
    assert all("api_key" not in item for item in data.get("model", {}).get("models", []))


def test_put_settings_global_scope(
    settings_client: TestClient, tmp_path: Path, isolated_home: Path
) -> None:
    """scope=global 写入全局配置路径，项目 minic.json 不创建。"""
    response = settings_client.put(
        "/settings",
        headers=HEADERS,
        json={"scope": "global", "memory": {"long_term_inject_threshold_tokens": 8000}},
    )
    assert response.status_code == 204

    global_config = isolated_home / ".minic" / "minic.json"
    assert global_config.exists()
    data = json.loads(global_config.read_text(encoding="utf-8"))
    assert data["memory"]["long_term_inject_threshold_tokens"] == 8000
    assert not (tmp_path / ".minic" / "minic.json").exists()

    loaded = load_settings(tmp_path)
    assert loaded.memory.long_term_inject_threshold_tokens == 8000


def test_put_settings_invalid_scope(settings_client: TestClient) -> None:
    """scope 只支持 global/project。"""
    response = settings_client.put("/settings", headers=HEADERS, json={"scope": "invalid"})
    assert response.status_code == 400


def test_put_settings_invalid_value_400(
    settings_client: TestClient, tmp_path: Path
) -> None:
    """合并结果无法通过 AppSettings 校验时返回 400 且不写文件。"""
    response = settings_client.put("/settings", headers=HEADERS, json={"model": "deepseek"})
    assert response.status_code == 400
    assert not (tmp_path / ".minic" / "minic.json").exists()


def test_put_settings_requires_auth(settings_client: TestClient) -> None:
    """未鉴权时 PUT /settings 返回 401。"""
    assert settings_client.put("/settings", json={"model": {}}).status_code == 401
