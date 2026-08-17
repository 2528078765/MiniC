"""0.0.4 修复回归：提示词无旧协议残留、tool-null 标记过滤、跨 Context reset。"""

from __future__ import annotations

import contextvars

from langchain_core.messages import AIMessage

from minic.graph.action.action_node import _extract_final_answer
from minic.graph.common import tool_system_prompt


def test_tool_prompt_uses_function_calling_not_json_protocol() -> None:
    """工具提示使用原生函数调用，不再残留旧纯文本 JSON 协议。"""
    prompt = tool_system_prompt()
    assert "函数调用" in prompt
    assert '"tool": null' not in prompt
    assert "不需要调用时输出" not in prompt


def test_extract_final_answer_skips_tool_null_marker() -> None:
    """模型偶发输出 {"tool": null} 标记时视为空回答（触发兜底重新生成）。"""
    messages = [
        AIMessage(content="", tool_calls=[{"name": "Read", "args": {}, "id": "t1"}]),
        AIMessage(content='{"tool": null}'),
    ]
    assert _extract_final_answer({"messages": messages}) == ""

    messages_ok = [
        AIMessage(content="", tool_calls=[{"name": "Read", "args": {}, "id": "t1"}]),
        AIMessage(content="文件内容如下：…"),
    ]
    assert _extract_final_answer({"messages": messages_ok}) == "文件内容如下：…"


def test_model_registry_reset_tolerates_cross_context_token(monkeypatch) -> None:
    """跨 asyncio Context 的 reset 抛 ValueError 时吞掉（选择随上下文销毁）。"""
    from minic.chat.models import ModelRegistry

    registry = ModelRegistry(models={"mock": object()}, default_name="mock")

    class _BrokenContextVar:
        def reset(self, token) -> None:
            raise ValueError("was created in a different Context")

    monkeypatch.setattr(registry, "_ctx", _BrokenContextVar())
    real_token = contextvars.ContextVar("probe").set(1)  # 真实 Token
    registry.reset(real_token)  # 不应抛异常
