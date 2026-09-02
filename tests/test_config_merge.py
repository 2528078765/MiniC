"""配置合并规则：model/embedding/rag 仅全局可改（项目 minic.json 不可改这三段）。"""

from __future__ import annotations

import json
from pathlib import Path

from minic.core.config import load_settings


def test_project_config_cannot_override_model_embedding_rag(tmp_path: Path, monkeypatch) -> None:
    """项目 minic.json 里的 model/embedding/rag 段被忽略，以全局为准。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    (home / ".minic").mkdir()
    (home / ".minic" / "minic.json").write_text(
        json.dumps(
            {
                "model": {
                    "models": [
                        {
                            "name": "全局模型",
                            "provider": "deepseek",
                            "base_url": "https://api.deepseek.com",
                            "model": "deepseek-v4-flash",
                        }
                    ]
                },
                "embedding": {"provider": "openai", "base_url": "https://x.com/v1", "model": "e3"},
                "rag": {"chunk_size": 500, "knowledge_base_paths": ["D:/全局知识库"]},
            }
        ),
        encoding="utf-8",
    )
    project = tmp_path / "proj"
    (project / ".minic").mkdir(parents=True)
    (project / ".minic" / "minic.json").write_text(
        json.dumps(
            {
                "model": {"models": []},  # 手写空模型：不能遮蔽全局
                "embedding": {"provider": "", "base_url": "", "model": ""},
                "rag": {"chunk_size": 1, "knowledge_base_paths": []},
                "memory": {"long_term_inject_threshold_tokens": 9999},
            }
        ),
        encoding="utf-8",
    )
    settings = load_settings(project)
    assert [item.name for item in settings.model.models] == ["全局模型"]
    assert settings.embedding.provider == "openai"
    assert settings.rag.chunk_size == 500
    assert settings.rag.knowledge_base_paths == ["D:/全局知识库"]
    assert settings.memory.long_term_inject_threshold_tokens == 9999  # memory 项目可改


def test_ensure_project_minic_json_omits_global_only_sections(tmp_path: Path) -> None:
    """项目标记文件不含 model/embedding/rag 段（其余段保留）。"""
    from minic.gui.local_data import ensure_project_minic_json

    path = tmp_path / "proj" / ".minic" / "minic.json"
    ensure_project_minic_json(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "model" not in data
    assert "embedding" not in data
    assert "rag" not in data
    assert "sandbox" in data
    assert "memory" in data
