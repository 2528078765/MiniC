"""模型注册表空配置行为：0.0.2 起默认不内置任何模型。"""

from __future__ import annotations

import pytest

from minic.chat.models import ModelRegistry, create_chat_model
from minic.core.config import AppSettings


def test_default_settings_have_no_models() -> None:
    """默认配置模型列表为空（不内置任何供应商）。"""
    settings = AppSettings()
    assert settings.model.models == []


def test_registry_current_raises_when_empty() -> None:
    """未配置任何模型：current() 抛明确错误，不回退 mock 假模型。"""
    registry = create_chat_model(AppSettings())
    with pytest.raises(RuntimeError, match="未配置任何模型"):
        registry.current()
    assert registry.names() == []


def test_registry_current_with_mock_provider() -> None:
    """配置 mock 模型时正常返回实例。"""
    settings = AppSettings(
        model={"models": [{"name": "mock1", "provider": "mock", "base_url": "", "model": "mock", "enabled": True}]}
    )
    registry = create_chat_model(settings)
    assert registry.names() == ["mock1"]
    assert registry.current() is not None
