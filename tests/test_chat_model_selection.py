"""请求模型必须传入流式任务，并且并发会话互相隔离。"""

import contextvars
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from minic.chat.models import ModelRegistry
from minic.core.config import AppSettings
from minic.core.server import ChatRequest, create_app

HEADERS = {"Authorization": "Bearer test-token"}


@pytest.fixture
def model_client(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    registry = ModelRegistry({"first": "FIRST", "second": "SECOND"}, "first")
    monkeypatch.setattr("minic.core.server.create_chat_model", lambda settings: registry)
    monkeypatch.setattr("minic.core.server.RagStore", lambda **kwargs: object())
    settings = AppSettings(model={"provider": "mock"}, embedding={"provider": "mock", "dimension": 64})
    app = create_app(settings, "test-token", tmp_path, rag_data_dir=tmp_path / "rag")

    class RecordingEngine:
        async def cancel(self, run_id):
            pass
        async def stream_chat(self, **kwargs):
            before = registry.current()
            yield "token", {"delta": before + ":" + registry.current()}
            yield "message_end", {"status": "completed"}
            yield "done", {"status": "completed", "thread_id": kwargs["thread_id"]}

    app.state.chat_engine = RecordingEngine()
    endpoint = next(route.endpoint for route in app.routes if route.path == "/chat/stream")
    return app, endpoint, registry


def next_event(iterator, context):
    """在流所属上下文推进生成器；不依赖 Windows 事件循环的 socketpair。"""
    try:
        context.run(iterator.__anext__().send, None)
    except StopIteration as result:
        return result.value
    except StopAsyncIteration:
        return None
    raise AssertionError("测试流不应等待外部 IO")


def consume(response):
    context = contextvars.Context()
    parts = []
    while (event := next_event(response.body_iterator, context)) is not None:
        parts.append(event)
    return "".join(json.loads(line[6:]).get("delta", "")
                   for line in "".join(parts).splitlines() if line.startswith("data: "))


def request(endpoint, model=None):
    # 同步请求线程和异步响应任务使用不同 Context，复现真实的线程边界。
    coroutine = contextvars.Context().run(endpoint, ChatRequest(message="hello", model=model))
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError("此隔离端点不应访问外部 IO")


def test_stream_uses_selected_model_and_restores_default(model_client):
    _, endpoint, registry = model_client
    response = request(endpoint, "second")
    assert response.status_code == 200
    assert consume(response) == "SECOND:SECOND"
    assert consume(request(endpoint)) == "FIRST:FIRST"
    assert registry.current() == "FIRST"


def test_concurrent_streams_keep_their_selected_models(model_client):
    _, endpoint, registry = model_client
    first, second = request(endpoint, "first"), request(endpoint, "second")
    first_context, second_context = contextvars.Context(), contextvars.Context()
    assert "FIRST:FIRST" in next_event(first.body_iterator, first_context)
    assert "SECOND:SECOND" in next_event(second.body_iterator, second_context)
    # 交替推进两条进行中的流，确保请求上下文相互隔离。
    while next_event(first.body_iterator, first_context) is not None:
        pass
    while next_event(second.body_iterator, second_context) is not None:
        pass
    assert registry.current() == "FIRST"


def test_invalid_model_does_not_register_a_run(model_client):
    app, endpoint, _ = model_client
    with pytest.raises(HTTPException) as error:
        request(endpoint, "removed")
    assert error.value.status_code == 400
    assert not app.state.run_manager.runs
    assert not app.state.active_threads


def test_registry_update_changes_default_in_a_fresh_context(monkeypatch):
    registry = ModelRegistry({"first": "FIRST", "second": "SECOND"}, "first")
    monkeypatch.setattr("minic.chat.models._build_models", lambda settings: ({"first": "FIRST", "second": "SECOND"}, "second"))
    registry.update(AppSettings())
    import contextvars
    assert contextvars.Context().run(registry.current) == "SECOND"
