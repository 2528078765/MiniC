"""共享工具执行核心：/tools/run 与工具调用子图复用。"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any, Callable

from minic.recovery import ToolExecutionLog
from minic.tools.service import ApprovalManager, ToolExecutor, ToolResult


Emit = Callable[[str, dict[str, Any]], None]


class ToolRuntime:
    """统一封装工具权限、审批、执行与崩溃日志。"""

    def __init__(
        self,
        approval_manager: ApprovalManager,
        tool_executor: ToolExecutor,
        tool_log: ToolExecutionLog,
        mcp_manager: Any | None = None,
        skill_manager: Any | None = None,
        subagent_manager: Any | None = None,
        sandbox_policy: Any | None = None,
        rag_store: Any | None = None,
    ) -> None:
        self.approval_manager = approval_manager
        self.tool_executor = tool_executor
        self.tool_log = tool_log
        self.mcp_manager = mcp_manager  # MCP 管理器（server.tool 格式工具转发）
        self.skill_manager = skill_manager  # SKILL 管理器（allowed-tools 白名单约束）
        self.subagent_manager = subagent_manager  # 子 Agent 管理器（DelegateToSubagent 转发）
        self.sandbox_policy = sandbox_policy  # 沙箱强制层（路径/Bash/网络策略）
        self.rag_store = rag_store  # RAG 存储（单库；IngestDirectory 入库目标）
        self._sinks: dict[str, Emit] = {}

    def _skill_restriction(self, tool: str) -> str | None:
        """按启用 SKILL 的 allowed-tools 并集做白名单检查；不受限时返回 None。"""
        if self.skill_manager is None:
            return None
        allowed = self.skill_manager.allowed_union()
        if not allowed:
            return None
        if tool in allowed:
            return None
        return "SKILL 限制：该工具不在当前启用 SKILL 的 allowed-tools 内"

    async def _execute_tool(
        self,
        tool: str,
        args: dict[str, Any],
        thread_id: str = "",
        emit: Emit | None = None,
        project_root: str | None = None,
    ) -> ToolResult:
        """按工具名分发执行：IngestDirectory/MCP 工具（含 .）与 DelegateToSubagent 转发，其余走内置执行器。"""
        if tool == "DelegateToSubagent":
            return await self._delegate_subagent(args, thread_id, emit)
        if tool == "IngestDirectory":
            return await self._ingest_directory(args, thread_id, emit)
        if self.mcp_manager is not None and "." in tool:
            server_name, tool_name = tool.split(".", 1)
            output = await self.mcp_manager.call_tool(server_name, tool_name, args)
            return ToolResult(status="success", output=output)
        return self.tool_executor.execute(tool, args, project_root=project_root)

    def _ingest_path(self, args: dict[str, Any]) -> str | None:
        """返回 IngestDirectory 的目标目录：args.path 优先，缺省用 rag.default_directory。"""
        raw = args.get("path")
        if raw:
            return str(raw)
        if self.rag_store is not None:
            default_dir = getattr(getattr(self.rag_store.settings, "rag", None), "default_directory", None)
            if default_dir:
                return str(default_dir)
        return None

    async def _ingest_directory(
        self,
        args: dict[str, Any],
        thread_id: str,
        emit: Emit | None,
    ) -> ToolResult:
        """执行 IngestDirectory：增量入库到单库，返回入库统计文本。

        同步入库放入 asyncio.to_thread 线程池避免阻塞事件循环（复用 G12 per-source 锁）。
        """
        del thread_id, emit  # 本工具不需要额外事件，tool_call/tool_result 由 execute 统一发出
        if self.rag_store is None:
            return ToolResult(status="failed", output="RAG 存储不可用")
        path = self._ingest_path(args)
        if not path:
            return ToolResult(status="failed", output="未指定 path 且未配置 rag.default_directory")
        try:
            result = await asyncio.to_thread(
                self.rag_store.ingest_directory,
                path=str(path),
                extensions=None,
                source=None,
            )
        except Exception as exc:  # noqa: BLE001 - 入库失败统一转工具结果
            return ToolResult(status="failed", output=str(exc))
        parts = [f"已入库 {result.ingested} 篇", f"跳过 {result.skipped} 篇"]
        if result.failed:
            parts.append(f"失败 {len(result.failed)} 篇")
        parts.append(f"目录 {path}")
        return ToolResult(status="success", output="；".join(parts))

    async def _delegate_subagent(
        self,
        args: dict[str, Any],
        thread_id: str,
        emit: Emit | None,
    ) -> ToolResult:
        """执行 DelegateToSubagent：把子任务转发给 SubAgentManager，结果转成 ToolResult。"""
        if self.subagent_manager is None:
            return ToolResult(status="failed", output="子 Agent 管理器不可用")
        task = str(args.get("task", "")).strip()
        if not task:
            return ToolResult(status="failed", output="task 不能为空")
        raw_allowed = args.get("allowed_tools")
        allowed_tools = (
            [str(item) for item in raw_allowed] if isinstance(raw_allowed, list) else None
        )
        try:
            budget = int(args.get("budget", 4)) if args.get("budget") is not None else 4
        except (TypeError, ValueError):
            budget = 4
        result = await self.subagent_manager.run(
            task=task,
            allowed_tools=allowed_tools,
            budget=budget,
            thread_id=thread_id,
            emit=emit,
        )
        if result.get("status") == "completed":
            return ToolResult(status="success", output=str(result.get("output") or ""))
        return ToolResult(status="failed", output=str(result.get("output") or "子任务失败"))

    def attach(self, thread_id: str, emit: Emit) -> None:
        """为聊天线程绑定实时事件出口。"""
        self._sinks[thread_id] = emit

    def detach(self, thread_id: str) -> None:
        """移除聊天线程的事件出口。"""
        self._sinks.pop(thread_id, None)

    def _sink(self, thread_id: str, emit: Emit | None) -> Emit:
        """返回本次调用的事件出口。"""
        if emit is not None:
            return emit
        sink = self._sinks.get(thread_id)
        if sink is None:
            raise RuntimeError("ToolRuntime 未绑定事件出口")
        return sink

    def _existing_result(self, run_id: str, tool_call_id: str) -> dict[str, Any] | None:
        """按 run_id + tool_call_id 查找已完成结果。"""
        if not run_id or not tool_call_id:
            return None
        for record in self.tool_log.read():
            if (
                record.get("event") == "result"
                and record.get("run_id") == run_id
                and record.get("tool_call_id") == tool_call_id
            ):
                return record
        return None

    def _denied_output(self, tool: str) -> Any:
        """deny 结果的 output：IngestDirectory 固定「用户拒绝入库」，其他工具保持 None（兼容现有行为）。"""
        return "用户拒绝入库" if tool == "IngestDirectory" else None

    def _finish(
        self,
        sink: Emit,
        *,
        thread_id: str,
        run_id: str,
        tool_call_id: str,
        idempotency_key: str,
        status: str,
        output: Any,
        backup_id: str | None,
    ) -> None:
        """写 result 日志并发出 tool_result 事件。"""
        self.tool_log.append(
            {
                "event": "result",
                "thread_id": thread_id,
                "run_id": run_id,
                "tool_call_id": tool_call_id,
                "idempotency_key": idempotency_key,
                "status": status,
                "output": output,
                "timestamp": datetime.now().astimezone().isoformat(),
            }
        )
        data: dict[str, Any] = {
            "id": tool_call_id,
            "status": status,
            "output": output,
        }
        if backup_id is not None:
            data["backup_id"] = backup_id
        sink("tool_result", data)

    async def execute(
        self,
        *,
        thread_id: str,
        run_id: str,
        tool: str,
        args: dict[str, Any],
        tool_call_id: str | None = None,
        emit: Emit | None = None,
        subagent_id: str | None = None,
        project_root: str | None = None,
    ) -> dict[str, Any]:
        """执行一次工具调用并返回结果摘要。

        ``project_root`` 为请求级项目根（每会话工作区目录），
        相对路径解析与 Bash 执行目录基于它；缺省用装配默认。
        """
        sink = self._sink(thread_id, emit)
        call_id = tool_call_id or str(uuid.uuid4())
        idempotency_key = ToolExecutionLog.idempotency_key(thread_id, tool, args)
        existing = self._existing_result(run_id, call_id)

        sink(
            "tool_call",
            {"id": call_id, "tool": tool, "args": args, "status": "pending"},
        )
        if existing is not None:
            sink(
                "tool_result",
                {
                    "id": call_id,
                    "status": existing.get("status"),
                    "output": existing.get("output"),
                },
            )
            return {
                "status": "completed",
                "output": existing.get("output"),
                "backup_id": None,
                "tool_call_id": call_id,
            }

        if self.sandbox_policy is not None:  # 沙箱强制层：在审批前拦截，不进入审批流程
            sandbox_reason = self.sandbox_policy.check_tool(tool, args, self.approval_manager.workspace)
            if sandbox_reason is not None:
                output = f"沙箱策略限制: {sandbox_reason}"
                self._finish(
                    sink,
                    thread_id=thread_id,
                    run_id=run_id,
                    tool_call_id=call_id,
                    idempotency_key=idempotency_key,
                    status="denied",
                    output=output,
                    backup_id=None,
                )
                return {
                    "status": "denied",
                    "output": output,
                    "backup_id": None,
                    "tool_call_id": call_id,
                }

        restriction = self._skill_restriction(tool)  # SKILL allowed-tools 白名单约束
        if restriction is not None:
            self._finish(
                sink,
                thread_id=thread_id,
                run_id=run_id,
                tool_call_id=call_id,
                idempotency_key=idempotency_key,
                status="denied",
                output=restriction,
                backup_id=None,
            )
            return {
                "status": "denied",
                "output": restriction,
                "backup_id": None,
                "tool_call_id": call_id,
            }

        self.tool_log.append(
            {
                "event": "intent",
                "thread_id": thread_id,
                "run_id": run_id,
                "tool_call_id": call_id,
                "idempotency_key": idempotency_key,
                "tool": tool,
                "args": args,
                "timestamp": datetime.now().astimezone().isoformat(),
            }
        )
        plan = self.approval_manager.plan(tool, args, thread_id)
        if plan == "deny":
            denied_output = self._denied_output(tool)
            self._finish(
                sink,
                thread_id=thread_id,
                run_id=run_id,
                tool_call_id=call_id,
                idempotency_key=idempotency_key,
                status="denied",
                output=denied_output,
                backup_id=None,
            )
            return {
                "status": "denied",
                "output": denied_output,
                "backup_id": None,
                "tool_call_id": call_id,
            }

        if plan == "approval":
            approval = self.approval_manager.request(thread_id, tool, args, subagent_id=subagent_id)
            approval_data: dict[str, Any] = {
                "id": approval.id,
                "thread_id": thread_id,
                "tool_call_id": approval.tool_call_id,
                "subagent_id": approval.subagent_id,
                "parent_run_id": None,
                "tool": approval.tool,
                "args": approval.args,
                "options": approval.options,
            }
            if tool == "IngestDirectory":  # 供 CLI/UI 展示入库目标，不新增事件名（同 G7c subagent_id 做法）
                ingest_path = self._ingest_path(args)
                if ingest_path:
                    approval_data["message"] = f"将把 {ingest_path} 增量入库到 RAG 知识库"
            sink(
                "approval_requested",
                approval_data,
            )
            try:
                await self.approval_manager.wait(approval.id)
            except asyncio.TimeoutError:
                sink(
                    "approval_result",
                    {
                        "id": approval.id,
                        "tool_call_id": approval.tool_call_id,
                        "status": "expired",
                    },
                )
                self._finish(
                    sink,
                    thread_id=thread_id,
                    run_id=run_id,
                    tool_call_id=call_id,
                    idempotency_key=idempotency_key,
                    status="failed",
                    output="审批超时",
                    backup_id=None,
                )
                return {
                    "status": "failed",
                    "output": "审批超时",
                    "backup_id": None,
                    "tool_call_id": call_id,
                }
            sink(
                "approval_result",
                {
                    "id": approval.id,
                    "tool_call_id": approval.tool_call_id,
                    "status": "approved" if approval.decision != "deny" else "denied",
                    "decision": approval.decision,
                },
            )
            if approval.decision == "deny":
                denied_output = self._denied_output(tool)
                self._finish(
                    sink,
                    thread_id=thread_id,
                    run_id=run_id,
                    tool_call_id=call_id,
                    idempotency_key=idempotency_key,
                    status="denied",
                    output=denied_output,
                    backup_id=None,
                )
                return {
                    "status": "denied",
                    "output": denied_output,
                    "backup_id": None,
                    "tool_call_id": call_id,
                }

        try:
            result = await self._execute_tool(tool, args, thread_id, sink, project_root)
            self._finish(
                sink,
                thread_id=thread_id,
                run_id=run_id,
                tool_call_id=call_id,
                idempotency_key=idempotency_key,
                status=result.status,
                output=result.output,
                backup_id=result.backup_id,
            )
            return {
                "status": "completed",
                "output": result.output,
                "backup_id": result.backup_id,
                "tool_call_id": call_id,
            }
        except Exception as exc:  # noqa: BLE001 - 工具失败统一转结果
            self._finish(
                sink,
                thread_id=thread_id,
                run_id=run_id,
                tool_call_id=call_id,
                idempotency_key=idempotency_key,
                status="failed",
                output=str(exc),
                backup_id=None,
            )
            return {
                "status": "failed",
                "output": str(exc),
                "backup_id": None,
                "tool_call_id": call_id,
            }
