"""G5 缺陷守护测试：build_messages 工具消息配对。"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from minic.graph.common import build_messages


def _assert_tool_pairs(messages: list) -> list[int]:
    """校验每条 ToolMessage 前均有配对 AIMessage(tool_calls)，返回 ToolMessage 下标。"""
    indexes: list[int] = []
    for idx, message in enumerate(messages):
        if isinstance(message, ToolMessage):
            assert idx >= 1, "ToolMessage 不能是首条消息"
            previous = messages[idx - 1]
            assert isinstance(previous, AIMessage), "ToolMessage 前必须是 AIMessage"
            assert previous.tool_calls, "前置 AIMessage 必须带 tool_calls"
            assert (
                previous.tool_calls[0]["id"] == message.tool_call_id
            ), "AIMessage.tool_calls id 必须与 ToolMessage.tool_call_id 一致"
            indexes.append(idx)
    return indexes


def test_build_messages_without_tools_keeps_plain_sequence() -> None:
    """无工具结果时保持 system -> history -> user 顺序（末条历史=本次消息，跳过）。"""
    history = [
        {"role": "human", "content": "你好"},
        {"role": "ai", "content": "你好，有什么可以帮你"},
        {"role": "human", "content": "继续"},
    ]
    messages = build_messages("sys", history, "继续", None)
    assert isinstance(messages[0], SystemMessage)
    assert isinstance(messages[1], HumanMessage)
    assert isinstance(messages[2], AIMessage)
    assert isinstance(messages[3], HumanMessage)
    assert messages[3].content == "继续"
    assert not any(isinstance(m, ToolMessage) for m in messages)


def test_build_messages_pairs_single_tool_result() -> None:
    """单条工具结果前有配对 AIMessage(tool_calls)，id/name/args 一致。"""
    history = [{"role": "human", "content": "读一下"}]
    tool_context = [
        {
            "tool_call_id": "call-1",
            "tool": "Read",
            "args": {"path": "a.txt"},
            "status": "completed",
            "output": "文件内容",
        }
    ]
    messages = build_messages("sys", history, "帮我读文件", tool_context)
    assert isinstance(messages[0], SystemMessage)
    assert messages[-1] == messages[-1] and isinstance(messages[-1], HumanMessage)
    assert messages[-1].content == "帮我读文件"
    tool_indexes = _assert_tool_pairs(messages)
    assert len(tool_indexes) == 1
    idx = tool_indexes[0]
    assistant = messages[idx - 1]
    assert assistant.content == ""
    assert len(assistant.tool_calls) == 1
    call = assistant.tool_calls[0]
    assert call["id"] == "call-1"
    assert call["name"] == "Read"
    assert call["args"] == {"path": "a.txt"}
    assert messages[idx].content == "文件内容"


def test_build_messages_pairs_multi_round_tool_results() -> None:
    """多轮工具结果逐条配对，id 不串号。"""
    tool_context = [
        {
            "tool_call_id": "call-1",
            "tool": "Read",
            "args": {"path": "a.txt"},
            "status": "completed",
            "output": "aaa",
        },
        {
            "tool_call_id": "call-2",
            "tool": "TextSearch",
            "args": {"query": "关键词"},
            "status": "completed",
            "output": "bbb",
        },
        {
            "tool_call_id": "call-3",
            "tool": "Write",
            "args": {"path": "b.txt", "content": "x"},
            "status": "denied",
            "output": None,
        },
    ]
    messages = build_messages("sys", [], "多轮结果", tool_context)
    tool_indexes = _assert_tool_pairs(messages)
    assert tool_indexes == [2, 4, 6]
    assert [messages[idx].tool_call_id for idx in tool_indexes] == ["call-1", "call-2", "call-3"]
    assert [messages[idx - 1].tool_calls[0]["name"] for idx in tool_indexes] == [
        "Read",
        "TextSearch",
        "Write",
    ]
    assert messages[tool_indexes[2]].content == "denied"  # 无 output 时回退 status
    assert isinstance(messages[-1], HumanMessage)
    assert messages[-1].content == "多轮结果"


def test_build_messages_missing_tool_call_id_uses_stable_placeholder() -> None:
    """tool_call_id 缺失时用稳定占位，且 AIMessage 与 ToolMessage 使用同一占位。"""
    tool_context = [
        {"tool": "Read", "args": {"path": "a.txt"}, "status": "completed", "output": "x"},
        {"tool_call_id": "real-id", "tool": "Lint", "args": {}, "status": "completed", "output": "y"},
    ]
    messages = build_messages("sys", [], "占位测试", tool_context)
    tool_indexes = _assert_tool_pairs(messages)
    assert len(tool_indexes) == 2
    assert messages[tool_indexes[0]].tool_call_id == "tool-0"
    assert messages[tool_indexes[0] - 1].tool_calls[0]["id"] == "tool-0"
    assert messages[tool_indexes[1]].tool_call_id == "real-id"
    assert messages[tool_indexes[1] - 1].tool_calls[0]["id"] == "real-id"


def test_build_messages_history_before_tool_pairs() -> None:
    """整体顺序：system -> history -> (AIMessage+ToolMessage)* -> HumanMessage(user)。"""
    history = [
        {"role": "human", "content": "上一问"},
        {"role": "ai", "content": "上一答"},
        {"role": "human", "content": "本次问题"},
    ]
    tool_context = [
        {"tool_call_id": "c1", "tool": "Read", "args": {"path": "a.txt"}, "status": "completed", "output": "ok"}
    ]
    messages = build_messages("sys", history, "本次问题", tool_context)
    kinds = [type(m) for m in messages]
    assert kinds == [
        SystemMessage,
        HumanMessage,
        AIMessage,
        AIMessage,
        ToolMessage,
        HumanMessage,
    ]
    assert messages[-1].content == "本次问题"


def test_split_incomplete_dsml_streamed_chunks() -> None:
    """流式逐块喂入时 DSML 工具调用标记不泄漏（开标签前奏 "<" 必须缓冲）。"""
    from minic.graph.common import split_incomplete_dsml

    sample = (
        "好的，我来规划路线。\n\n"
        '<|DSML| tool_calls>\n'
        '<|DSML| invoke name="amap-maps-streamableHTTP.maps_geo">\n'
        '<|DSML| parameter name="address" string="true">葫芦岛市连山区步行街</|DSML| parameter>\n'
        "</|DSML| invoke>\n</|DSML| tool_calls>"
    )
    buffer = ""
    parts: list[str] = []
    for i in range(0, len(sample), 3):
        buffer, clean = split_incomplete_dsml(buffer + sample[i : i + 3])
        if clean:
            parts.append(clean)
    final = "".join(parts) + buffer
    assert "DSML" not in final
    assert "maps_geo" not in final
    assert "步行街" not in final
    assert "好的，我来规划路线。" in final


def test_split_incomplete_dsml_plain_text() -> None:
    """普通文本原样放行。"""
    from minic.graph.common import split_incomplete_dsml

    assert split_incomplete_dsml("你好世界") == ("", "你好世界")
    assert split_incomplete_dsml("没有标签的文本。") == ("", "没有标签的文本。")
