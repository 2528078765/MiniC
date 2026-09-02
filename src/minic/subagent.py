"""G7c 子 Agent（基础版）管理（规格书 16 节）。

SubAgentManager 负责：
- 并发控制（``subagent.max_concurrent`` 信号量，默认 3）。
- 独立子任务上下文：独立 subagent_id 与消息列表，与主会话完全隔离
  （不读取主会话 short_memory，不写入任何 short_memory）。
- 长期记忆只读注入：系统提示注入合并记忆，子任务不触发记忆提取写入。
- 超时控制（``subagent.timeout_seconds``，默认 120s）。
- 审批按 subagent_id 透传：子任务内工具调用走现有 ToolRuntime 审批，
  approval_requested 事件 data 携带 subagent_id。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from typing import Any, Callable

from langchain_core.messages import HumanMessage, SystemMessage

from minic.graph.common import parse_model_json
from minic.graph.tools import get_tool
from minic.middleware import memory_injection, retry_async


class SubAgentManager:
    """管理子任务执行：并发、隔离上下文、只读记忆、超时与状态查询。"""

    def __init__(
        self,
        chat_model: Any,
        memory_store: Any,
        tool_runtime: Any,
        settings: Any,
        workspace: Any,
    ) -> None:
        self.chat_model = chat_model
        self.memory_store = memory_store
        self.tool_runtime = tool_runtime
        self.settings = settings
        self.workspace = str(workspace)
        self.semaphore = asyncio.Semaphore(settings.subagent.max_concurrent)
        self._recent: list[dict[str, Any]] = []
        self._max_history = 20

    # ------------------------------------------------------------ 状态查询

    def status(self) -> list[dict[str, Any]]:
        """返回最近子任务记录（subagent_id/status/output_summary/created_at）。"""
        return list(self._recent)

    def _record(self, subagent_id: str, status: str, output: str) -> None:
        """记录最近一次子任务结果。"""
        entry = {
            "subagent_id": subagent_id,
            "status": status,
            "output_summary": (output or "")[:120],
            "output": output or "",
            "created_at": datetime.now().astimezone().isoformat(),
        }
        self._recent.append(entry)
        if len(self._recent) > self._max_history:
            del self._recent[: len(self._recent) - self._max_history]

    # ------------------------------------------------------------ 记忆注入

    def _merged_memory_text(self) -> str:
        """读取合并后的长期记忆（只读注入，超过阈值截断为摘要）。"""
        if self.memory_store is None:
            return ""
        memory_data = self.memory_store.read("merged", self.workspace)
        threshold = getattr(
            getattr(self.settings, "memory", None),
            "long_term_inject_threshold_tokens",
            4000,
        )
        return memory_injection(memory_data.get("content", ""), threshold)

    # ------------------------------------------------------------ 主入口

    async def run(
        self,
        task: str,
        allowed_tools: list[str] | None = None,
        budget: int = 4,
        thread_id: str | None = None,
        emit: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """执行子任务，返回 ``{"subagent_id", "status", "output"}``。

        status 取值：``completed``（子 Agent 给出最终回答）、``failed``（超时或
        预算耗尽）、``denied``（保留字段，子任务本身不直接产生该状态）。
        """
        subagent_id = str(uuid.uuid4())
        try:
            try:
                effective_budget = max(int(budget) if budget is not None else 4, 1)
            except (TypeError, ValueError):
                effective_budget = 4
            async with self.semaphore:
                result = await asyncio.wait_for(
                    self._run_inner(subagent_id, task, allowed_tools, effective_budget, thread_id, emit),
                    timeout=self.settings.subagent.timeout_seconds,
                )
        except asyncio.TimeoutError:
            result = {"subagent_id": subagent_id, "status": "failed", "output": "子任务超时"}
        self._record(subagent_id, result["status"], result["output"])
        return result

    # ------------------------------------------------------------ 内部循环

    def _build_system_prompt(self, task: str, allowed_tools: list[str] | None) -> str:
        """构造子 Agent 系统提示：角色约束 + 任务 + 只读记忆 + 工具白名单。"""
        lines = [
            "你是 MiniC 子 Agent，只负责完成以下子任务，不要扩展到其他内容。",
            f"子任务：{task}",
        ]
        memory_text = self._merged_memory_text()
        if memory_text:
            lines.append(f"只读长期记忆（仅供参考，不要修改）：\n{memory_text}")
        if allowed_tools:
            lines.append(f"可使用的工具（只允许以下工具，其他工具一律不得使用）：{', '.join(allowed_tools)}")
        else:
            lines.append("你可以直接给出答案，也可以使用可用工具帮助完成任务。")
        lines.append(
            "执行环境是 Windows，Bash 命令在 cmd.exe 中运行："
            "请使用 Windows 命令与路径（dir、type、echo、C:\\Users\\<用户名>），"
            "用户主目录可用命令 `echo %USERPROFILE%` 获取，"
            "不要使用 ls、~、/home、/dev 等 Linux 语法。"
        )
        lines.append(
            "每轮只输出一个 JSON：需要工具时 {\"tool\": \"工具名\", \"args\": {参数}}；"
            "完成任务时 {\"answer\": \"最终回答\"}。不要输出其他内容。"
        )
        return "\n".join(lines)

    async def _run_inner(
        self,
        subagent_id: str,
        task: str,
        allowed_tools: list[str] | None,
        budget: int,
        thread_id: str | None,
        emit: Callable[[str, dict[str, Any]], None] | None,
    ) -> dict[str, Any]:
        """子任务独立工具循环，最多 budget 轮。"""
        del thread_id  # 子任务不绑定主会话，thread_id 仅用于审批路由
        system_text = self._build_system_prompt(task, allowed_tools)
        base_task = f"请完成子任务：{task}"
        messages: list[Any] = [
            SystemMessage(content=system_text),
            HumanMessage(content=base_task),
        ]
        results: list[str] = []

        def sub_emit(event_name: str, data: dict[str, Any]) -> None:
            """只把审批事件转发给主会话，避免子任务内部工具卡片污染主会话事件配对。"""
            if emit is not None and event_name in ("approval_requested", "approval_result"):
                emit(event_name, data)

        for _ in range(max(int(budget), 1)):
            response = await retry_async(lambda: self.chat_model.ainvoke(messages), max_retries=2)
            try:
                data = parse_model_json(response.content)
            except (ValueError, TypeError, AttributeError):
                break  # 非法输出按无法继续处理
            if not isinstance(data, dict):
                break
            if data.get("answer") is not None:
                answer = str(data.get("answer", "")).strip()
                return {"subagent_id": subagent_id, "status": "completed", "output": answer}
            tool = data.get("tool")
            if not tool:
                break  # 无工具决策，按未完成任务处理
            args = data.get("args") if isinstance(data.get("args"), dict) else {}
            tool_name = str(tool)
            if allowed_tools and tool_name not in allowed_tools:
                results.append(
                    f"调用工具 {tool_name} 被拒绝：不在允许列表（{', '.join(allowed_tools)}）中，"
                    "只能使用允许的工具或直接给出答案。"
                )
            elif get_tool(tool_name) is None:
                results.append(f"调用工具 {tool_name} 被拒绝：该工具不存在。")
            else:
                result = await self.tool_runtime.execute(
                    thread_id=subagent_id,
                    run_id=str(uuid.uuid4()),
                    tool=tool_name,
                    args=args,
                    subagent_id=subagent_id,
                    emit=sub_emit,
                )
                output = str(result.get("output") or result.get("status") or "")
                results.append(
                    f"调用工具 {tool_name}（参数 {json.dumps(args, ensure_ascii=False)}）结果：{output}"
                )
            # 每轮重置为 system + human（结果拼进文本），不传 assistant/tool 消息：
            # DeepSeek thinking 模式要求含 tool_calls 的 assistant 消息原样回传
            # reasoning_content，重构消息无法满足，会直接 400。
            feedback = "\n".join(f"- {item}" for item in results)
            messages = [
                SystemMessage(content=system_text),
                HumanMessage(
                    content=(
                        f"{base_task}\n\n已执行的步骤与结果：\n{feedback}\n\n"
                        "请继续：调用下一个工具或输出最终答案。"
                    )
                ),
            ]
        return {"subagent_id": subagent_id, "status": "failed", "output": "子 Agent 未在预算轮数内完成"}
