"""聊天流式执行引擎。"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from minic.chat.memory import ShortMemoryStore
from minic.graph.common import split_incomplete_dsml, strip_dsml
from minic.rag.store import RagStore


def _estimate_tokens(chars: int) -> int:
    """字符数估算 token（中文约 1 字/token、英文约 4 字符/token，取折中 0.6）。"""
    return max(int(chars * 0.6), 1)


def _record_usage(message: str, history: list[dict[str, Any]], answer: str, thread_id: str) -> None:
    """记录一轮对话的 token 消耗到 ~/.minic/usage.jsonl（总量统计用）。"""
    prompt_chars = len(message) + sum(len(str(item.get("content", ""))) for item in history)
    completion_chars = len(answer)
    record = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "thread_id": thread_id,
        "prompt_tokens": _estimate_tokens(prompt_chars),
        "completion_tokens": _estimate_tokens(completion_chars),
    }
    path = Path.home() / ".minic" / "usage.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


class ChatEngine:
    """把短期记忆和 LangGraph 工作流串成 SSE 事件流。"""

    def __init__(
        self,
        rag_store: RagStore,
        short_memory: ShortMemoryStore,
        graph: Any,
        tool_runtime: Any = None,
        summarize_history: Callable[[list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]] | None = None,
    ) -> None:
        self.rag_store = rag_store
        self.short_memory = short_memory
        self.graph = graph
        self.tool_runtime = tool_runtime
        self.summarize_history = summarize_history  # 历史摘要回调，仅影响注入图的 history

    async def _run_graph(
        self,
        queue: asyncio.Queue,
        graph_input: dict[str, Any],
    ) -> None:
        """后台遍历图，把事件放入统一队列。"""
        try:
            # 每次 run 用独立的 checkpoint 命名空间（run_id），避免跨 run 状态串扰
            config = {
                "configurable": {
                    "thread_id": str(graph_input.get("run_id") or graph_input.get("thread_id") or "default")
                }
            }
            async for item in self.graph.astream(
                graph_input,
                config=config,
                stream_mode=["messages", "values"],
                subgraphs=True,
            ):
                if not isinstance(item, tuple) or len(item) != 3:
                    continue
                namespace, mode, payload = item
                queue.put_nowait(("graph", mode, payload))
            queue.put_nowait(("graph_end",))
        except Exception as exc:  # noqa: BLE001 - 统一转队列错误
            queue.put_nowait(("graph_error", exc))

    async def stream_chat(
        self,
        thread_id: str,
        workspace: str,
        project_root: str,
        message: str,
        run_id: str,
        message_id: str,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """生成聊天事件，事件类型为 message_start/token/message_end/done。"""
        self.short_memory.append_message(thread_id, "human", message, workspace=workspace)
        thread = self.short_memory.load(thread_id, workspace=workspace)
        history = thread.get("messages", [])
        yield (
            "message_start",
            {
                "thread_id": thread_id,
                "message_id": message_id,
                "run_id": run_id,
            },
        )
        queue: asyncio.Queue = asyncio.Queue()
        if self.tool_runtime is not None:
            self.tool_runtime.attach(
                thread_id,
                lambda event_name, data: queue.put_nowait(("tool", event_name, data)),
            )
        if self.summarize_history is not None:  # 历史超阈值时生成摘要，只影响注入图的 history
            try:
                history = await self.summarize_history(history)
            except Exception:  # noqa: BLE001 - 摘要失败不破坏流，使用原始历史
                pass
        graph_task = asyncio.create_task(
            self._run_graph(
                queue,
                {
                    "thread_id": thread_id,
                    "workspace": workspace,
                    "project_root": project_root,
                    "user_message": message,
                    "history": history,
                    "run_id": run_id,
                    "message_id": message_id,
                },
            )
        )
        try:
            answer_parts: list[str] = []
            sources: list[dict[str, Any]] = []
            dsml_buffer = ""  # 跨 chunk 的 DSML 块残留（闭合前不转发）
            while True:
                item = await queue.get()
                kind = item[0]
                if kind == "graph_end":
                    break
                if kind == "graph_error":
                    raise item[1]
                if kind == "tool":
                    event_name, data = item[1], item[2]
                    self._record_tool_message(thread_id, workspace, event_name, data)  # 工具消息写入短期记忆
                    yield (event_name, data)
                    continue
                mode, payload = item[1], item[2]
                if mode == "messages":
                    message_chunk, metadata = payload
                    if metadata.get("langgraph_node") not in ("answer", "agent"):
                        continue
                    # 只转发模型流式分片；节点返回的完整消息（System/Human/AIMessage
                    # 增量重放）与工具调用轮不转发，避免提示词文本泄漏为 token
                    from langchain_core.messages import AIMessageChunk

                    if not isinstance(message_chunk, AIMessageChunk):
                        continue
                    if getattr(message_chunk, "tool_calls", None):
                        continue
                    delta = getattr(message_chunk, "content", "")
                    if delta:
                        # 流式过滤 DSML：块闭合前的内容不转发，避免工具标记泄漏到界面
                        dsml_buffer, clean = split_incomplete_dsml(dsml_buffer + delta)
                        if clean:
                            answer_parts.append(clean)
                            yield ("token", {"message_id": message_id, "delta": clean})
                elif mode == "values" and payload.get("answer"):
                    answer_parts = [strip_dsml(payload["answer"])]  # 完整答案同样剔除 DSML（存档不泄漏）
                    sources = payload.get("sources", [])

            await graph_task
            answer = "".join(answer_parts)
            self.short_memory.append_message(thread_id, "ai", answer, workspace=workspace)
            _record_usage(message, history, answer, thread_id)
            yield ("message_end", {"message_id": message_id, "status": "completed"})
            yield (
                "done",
                {
                    "thread_id": thread_id,
                    "status": "completed",
                    "sources": sources,
                },
            )
        except Exception as exc:  # noqa: BLE001 - 需要把异常转成 SSE error 事件
            yield (
                "error",
                {
                    "code": "INTERNAL_ERROR",
                    "message": str(exc),
                    "detail": {},
                },
            )
            yield ("message_end", {"message_id": message_id, "status": "failed"})
            yield (
                "done",
                {
                    "thread_id": thread_id,
                    "status": "failed",
                    "sources": [],
                },
            )
        finally:
            if self.tool_runtime is not None:
                self.tool_runtime.detach(thread_id)

    def _record_tool_message(
        self,
        thread_id: str,
        workspace: str,
        event_name: str,
        data: dict[str, Any],
    ) -> None:
        """把工具调用/结果写入短期记忆（ReAct 会话结构）。

        ``tool_call`` 事件写 role=ai + tool_calls；``tool_result`` 事件写
        role=tool + tool_call_id，与 AIMessage.tool_calls 配对。
        """
        if event_name == "tool_call":  # 模型发起的工具调用
            self.short_memory.append_message(
                thread_id,
                "ai",
                "",
                workspace=workspace,
                tool_calls=[
                    {
                        "name": data.get("tool"),
                        "args": data.get("args") or {},
                        "id": data.get("id") or str(uuid.uuid4()),
                    }
                ],
            )
        elif event_name == "tool_result":  # 工具执行结果
            self.short_memory.append_message(
                thread_id,
                "tool",
                str(data.get("output") or data.get("status") or ""),
                workspace=workspace,
                tool_call_id=data.get("tool_call_id") or data.get("id"),
            )


def new_run_ids() -> tuple[str, str]:
    """生成 run_id 和 message_id。"""
    return str(uuid.uuid4()), str(uuid.uuid4())
