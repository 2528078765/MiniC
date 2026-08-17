"""MiniC 命令行入口。"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import textwrap
from pathlib import Path
from typing import Any, Iterator

import httpx

from minic.cli.lifecycle import ensure_core, select_core_project_root
from minic.cli.ui import (
    normalize_approval_input,
    render_agent_panel,
    render_answer_block,
    render_approval_menu,
    render_mcp_panel,
    render_memory_panel,
    render_rag_panel,
    render_skills_panel,
    render_sources,
    render_status_bar,
    render_thread_panel,
    render_tool_card,
    render_welcome,
)
from minic.core.config import load_settings
from minic.core.runtime import read_runtime
from minic.core.server import run_core


COMMANDS = [
    ("/help", "显示帮助"),
    ("/new", "归档当前会话并新建，先备份"),
    ("/clear", "归档当前会话并新建，先备份"),
    ("/resume", "恢复历史会话"),
    ("/compress", "压缩当前会话，压缩前备份，可回滚"),
    ("/memory", "查看/编辑长期记忆，支持合并与冲突处理，用户内容优先，删除标记阻止重写"),
    ("/rag", "RAG 状态、入库、检索，显示来源"),
    ("/settings", "打开设置"),
    ("/quit", "退出 MiniC"),
    ("/skills", "SKILL 状态面板（界面占位）"),
    ("/mcp", "MCP 服务状态面板"),
    ("/agent", "子 Agent 状态面板（界面占位）"),
]


def _print_commands() -> None:
    """输出全部命令，描述自动换行并缩进对齐。"""
    width = shutil.get_terminal_size((100, 30)).columns
    column = max(len(command) for command, _ in COMMANDS) + 4
    for command, description in COMMANDS:
        lines = textwrap.wrap(description, width=max(width - column - 2, 20)) or [""]
        print(f"{command.ljust(column)}{lines[0]}")
        for line in lines[1:]:
            print(" " * column + line)


def _print_shortcuts() -> None:
    """输出快捷键帮助面板。"""
    print("快捷键帮助")
    print("  /             显示全部命令")
    print("  ?             快捷键帮助")
    print("  Ctrl+C        停止当前输出")
    print("  Ctrl+L        清屏")
    print("  ↑ / ↓         历史命令")
    print("  Tab           命令补全")
    print("  ← / →         Agent 面板（界面占位）")
    print("  Enter         发送消息 / 确认审批")


def _prompt_approval(options: list[str] | None = None) -> str:
    """行内等待审批输入，返回归一化决策；options 限定可用决策（如 Bash 只接受 allow_once/deny）。"""
    choices = options or ["allow_once", "allow_session", "allow_always", "deny"]
    print(render_approval_menu(options=choices), end="")
    while True:
        raw = input().strip()
        if not raw:
            return "allow_once"
        decision = normalize_approval_input(raw)
        if decision is None or decision not in choices:
            allowed_text = "/".join(
                {
                    "allow_once": "1",
                    "allow_session": "2",
                    "allow_always": "3",
                    "deny": "4",
                }.get(choice, choice)
                for choice in choices
            )
            print(f"  无效输入，请输入 {allowed_text}")
            print("  > ", end="")
            continue
        return decision


def _api_base(runtime: dict[str, Any]) -> str:
    """根据 runtime 生成 API 地址。"""
    return f"http://127.0.0.1:{runtime['port']}"


def _auth_headers(runtime: dict[str, Any]) -> dict[str, str]:
    """生成带令牌的请求头。"""
    return {"Authorization": f"Bearer {runtime['token']}"}


def _print_health(runtime: dict[str, Any], health: dict[str, Any]) -> None:
    """输出核心服务连接信息。"""
    print("已连接 MiniC 核心")
    print(f"  端口: {runtime['port']}")
    print(f"  PID: {health['pid']}")
    print(f"  启动时间: {health['started_at']}")
    print(f"  版本: {health['version']}")


def cmd_serve(args: argparse.Namespace) -> None:
    """启动核心服务（阻塞运行）。"""
    run_core(project_root=args.project, port=args.port)


def cmd_status(args: argparse.Namespace) -> None:
    """读取 runtime.json 并调用 /health。"""
    del args
    try:
        runtime = read_runtime()
    except FileNotFoundError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(f"{_api_base(runtime)}/health")
            response.raise_for_status()
            _print_health(runtime, response.json())
    except httpx.HTTPError as exc:
        print(f"无法连接核心服务: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def cmd_ingest(args: argparse.Namespace) -> None:
    """调用 POST /rag/ingest 入库 Markdown。"""
    runtime = read_runtime()
    payload = {
        "path": str(Path(args.path).resolve()),
        "extensions": args.extensions,
        "source": args.source,
    }
    with httpx.Client(timeout=600.0) as client:
        response = client.post(
            f"{_api_base(runtime)}/rag/ingest",
            headers=_auth_headers(runtime),
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
    print(f"入库完成: 成功 {data['ingested']}，跳过 {data['skipped']}")
    for failed in data.get("failed", []):
        print(f"  失败: {failed['path']} - {failed['message']}")


def cmd_query(args: argparse.Namespace) -> None:
    """调用 GET /rag/query 执行单次检索。"""
    runtime = read_runtime()
    params = {"q": args.query, "top_k": args.top_k}
    if args.source:
        params["source"] = args.source
    with httpx.Client(timeout=60.0) as client:
        response = client.get(
            f"{_api_base(runtime)}/rag/query",
            headers=_auth_headers(runtime),
            params=params,
        )
        response.raise_for_status()
        data = response.json()
    print(f"问题: {data['query']}")
    if not data.get("results"):
        print("未检索到")
    for index, result in enumerate(data.get("results", []), start=1):
        print(f"{index}. {result['file_path']} [{result['section']}] 分数 {result['score']}")
        print(f"   {result['text'][:120]}...")


def _iter_sse_events(response: httpx.Response) -> Iterator[tuple[str, dict[str, Any]]]:
    """解析 SSE 响应为 (事件名, 数据) 序列。"""
    event_name: str | None = None
    data_lines: list[str] = []
    for line in response.iter_lines():
        if line == "":
            if event_name and data_lines:
                yield event_name, json.loads("\n".join(data_lines))
            event_name = None
            data_lines = []
            continue
        if line.startswith("event:"):
            event_name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())


def _ingest_folder(client: httpx.Client, runtime: dict[str, Any], path: str) -> None:
    """在当前会话中先完成一次入库。"""
    payload = {"path": str(Path(path).resolve()), "extensions": [".md"]}
    response = client.post(
        f"{_api_base(runtime)}/rag/ingest",
        headers=_auth_headers(runtime),
        json=payload,
    )
    response.raise_for_status()
    data = response.json()
    print(f"入库完成: 成功 {data['ingested']}，跳过 {data['skipped']}")


def _run_session_loop(
    client: httpx.Client,
    runtime: dict[str, Any],
    workspace: Path,
    path: str | None = None,
    thread_id: str | None = None,
    status_bar: str | None = None,
) -> None:
    """进入交互式会话循环。"""
    if path:
        _ingest_folder(client, runtime, path)
    print("进入问答模式，输入 /help 或 / 查看命令，/quit 退出")
    while True:
        if status_bar:
            print(f"\n{status_bar}")
        message = input("\n你: ").strip()
        if not message:
            continue
        if message.lower() in {"exit", "quit", "退出", "/quit", "/退出"}:
            break
        if message in {"?", "？"}:
            _print_shortcuts()
            continue
        if message == "/":
            _print_commands()
            continue
        if message in {"/help", "/帮助"}:
            _print_commands()
            continue
        if message in {"/new", "/新建", "/clear", "/清空"}:
            if thread_id:
                archive_response = client.post(
                    f"{_api_base(runtime)}/threads/{thread_id}/archive",
                    headers=_auth_headers(runtime),
                )
                archive_response.raise_for_status()
            thread_id = None
            print("已新建会话")
            continue
        if message in {"/memory", "/记忆"}:
            memory_data = {}
            for scope in ("global", "project", "merged"):
                memory_response = client.get(
                    f"{_api_base(runtime)}/memory",
                    headers=_auth_headers(runtime),
                    params={"scope": scope, "workspace": str(workspace)},
                )
                memory_response.raise_for_status()
                memory_data[scope] = memory_response.json().get("content", "")
            print(render_memory_panel(
                memory_data["global"],
                memory_data["project"],
                memory_data["merged"],
            ))
            continue
        if message in {"/resume", "/恢复"}:
            threads_response = client.get(
                f"{_api_base(runtime)}/threads",
                headers=_auth_headers(runtime),
                params={"workspace": str(workspace)},
            )
            threads_response.raise_for_status()
            threads = threads_response.json().get("threads", [])
            print(render_thread_panel(threads))
            choice = input("输入编号或 thread_id（回车取消）: ").strip()
            if not choice:
                continue
            selected = None
            if choice.isdigit() and 1 <= int(choice) <= len(threads):
                selected = threads[int(choice) - 1]["thread_id"]
            elif any(thread["thread_id"] == choice for thread in threads):
                selected = choice
            if selected is None:
                print("无效选择")
                continue
            thread_id = selected
            print("已恢复会话")
            continue
        if message in {"/settings", "/设置"}:
            settings_response = client.get(
                f"{_api_base(runtime)}/settings",
                headers=_auth_headers(runtime),
            )
            settings_response.raise_for_status()
            settings_data = settings_response.json()
            model_cfg = settings_data.get("model") or {}
            model_models = model_cfg.get("models") or []
            primary_model = next((m for m in model_models if m.get("enabled", True)), None) or (
                model_models[0] if model_models else {}
            )
            print(
                f"模型: {primary_model.get('provider', '-')}/{primary_model.get('model', '-')}"
                f"（共 {len(model_models)} 个配置）"
            )
            print(
                f"Embedding: {settings_data['embedding']['provider']}/{settings_data['embedding']['model']}"
            )
            continue
        if message in {"/rag", "/知识库"}:
            rag_response = client.get(
                f"{_api_base(runtime)}/rag/status",
                headers=_auth_headers(runtime),
            )
            rag_response.raise_for_status()
            rag_data = rag_response.json()
            print(render_rag_panel(rag_data))
            continue
        if message in {"/agent", "/子agent", "/子Agent"}:
            agents_response = client.get(
                f"{_api_base(runtime)}/agents",
                headers=_auth_headers(runtime),
            )
            agents_response.raise_for_status()
            agents = agents_response.json().get("agents", [])
            settings_response = client.get(
                f"{_api_base(runtime)}/settings",
                headers=_auth_headers(runtime),
            )
            settings_response.raise_for_status()
            max_concurrent = settings_response.json().get("subagent", {}).get("max_concurrent", 3)
            print(render_agent_panel(agents, max_concurrent=max_concurrent))
            continue
        if message in {"/skills", "/mcp"}:
            if message == "/mcp":
                mcp_response = client.get(
                    f"{_api_base(runtime)}/mcp",
                    headers=_auth_headers(runtime),
                )
                mcp_response.raise_for_status()
                servers = mcp_response.json().get("servers", [])
                print(render_mcp_panel(servers))
            else:
                skills_response = client.get(
                    f"{_api_base(runtime)}/skills",
                    headers=_auth_headers(runtime),
                )
                skills_response.raise_for_status()
                skills = skills_response.json().get("skills", [])
                print(render_skills_panel(skills))
            continue
        payload = {
            "thread_id": thread_id,
            "workspace": str(workspace),
            "message": message,
        }
        sources: list[dict[str, Any]] = []
        answer_header_printed = False
        pending_tool: dict[str, Any] | None = None
        with client.stream(
            "POST",
            f"{_api_base(runtime)}/chat/stream",
            headers=_auth_headers(runtime),
            json=payload,
        ) as response:
            response.raise_for_status()
            for event_name, data in _iter_sse_events(response):
                if event_name == "token":
                    if not answer_header_printed:
                        print("  ── 回答 ──\n  ", end="", flush=True)
                        answer_header_printed = True
                    print(data.get("delta", ""), end="", flush=True)
                elif event_name == "tool_call":
                    pending_tool = {
                        "tool": data.get("tool"),
                        "args": data.get("args", {}),
                    }
                elif event_name == "approval_requested":
                    tool = data.get("tool", "")
                    args = data.get("args", {})
                    options = data.get("options", [])
                    pending_tool = {"tool": tool, "args": args}
                    print(render_tool_card(tool, args, "需要审批"))
                    if data.get("message"):
                        print(f"    {data['message']}")
                    decision = _prompt_approval(options=options)
                    approve_response = client.post(
                        f"{_api_base(runtime)}/threads/{data.get('thread_id')}/approve",
                        headers=_auth_headers(runtime),
                        json={"approval_id": data.get("id"), "decision": decision},
                    )
                    approve_response.raise_for_status()
                    print(render_tool_card(tool, args, "已审批" if decision != "deny" else "已拒绝"))
                elif event_name == "tool_result":
                    status = {
                        "success": "完成",
                        "denied": "已拒绝",
                        "failed": "失败",
                    }.get(data.get("status"), data.get("status", "失败"))
                    if pending_tool is not None:
                        print(
                            render_tool_card(
                                pending_tool["tool"],
                                pending_tool["args"],
                                status,
                                output=data.get("output"),
                                backup_id=data.get("backup_id"),
                            )
                        )
                elif event_name == "done":
                    sources = data.get("sources", [])
                    if thread_id is None:
                        thread_id = data.get("thread_id")
                elif event_name == "error":
                    print(f"\n错误: {data.get('message', '未知错误')}", file=sys.stderr)
        print()
        if sources:
            print(render_sources(sources))


def cmd_chat(args: argparse.Namespace) -> None:
    """进入交互式流式问答。"""
    workspace = Path.cwd().resolve()
    core_root = select_core_project_root(workspace)
    core = ensure_core(project_root=core_root, workspace=workspace)
    try:
        with httpx.Client(timeout=600.0) as client:
            health = client.get(f"{_api_base(core.runtime)}/health")
            health.raise_for_status()
            _print_health(core.runtime, health.json())
            _run_session_loop(
                client=client,
                runtime=core.runtime,
                workspace=workspace,
                path=args.path,
                thread_id=args.thread,
            )
    finally:
        core.stop()


def cmd_threads(args: argparse.Namespace) -> None:
    """列出会话。"""
    runtime = read_runtime()
    params: dict[str, Any] = {
        "workspace": str(Path(args.workspace).resolve()),
        "page_size": args.page_size,
    }
    if args.archived is not None:
        params["archived"] = args.archived
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            f"{_api_base(runtime)}/threads",
            headers=_auth_headers(runtime),
            params=params,
        )
        response.raise_for_status()
        data = response.json()
    for thread in data.get("threads", []):
        state = "已归档" if thread.get("archived") else "活跃"
        print(f"{thread['thread_id']} [{state}] {thread['name']} - {thread.get('description', '')}")


def cmd_resume(args: argparse.Namespace) -> None:
    """恢复会话并查看消息。"""
    runtime = read_runtime()
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{_api_base(runtime)}/threads/{args.thread_id}/resume",
            headers=_auth_headers(runtime),
        )
        response.raise_for_status()
        data = response.json()
    for message in data.get("messages", []):
        print(f"{message['role']}: {message['content']}")


def cmd_compress(args: argparse.Namespace) -> None:
    """压缩会话并返回备份 ID。"""
    runtime = read_runtime()
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            f"{_api_base(runtime)}/threads/{args.thread_id}/compress",
            headers=_auth_headers(runtime),
        )
        response.raise_for_status()
        data = response.json()
    print(f"摘要: {data['summary']}")
    print(f"备份: {data['backup_id']}")


def cmd_archive(args: argparse.Namespace) -> None:
    """归档会话。"""
    runtime = read_runtime()
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{_api_base(runtime)}/threads/{args.thread_id}/archive",
            headers=_auth_headers(runtime),
        )
        response.raise_for_status()
    print("已归档")


def cmd_unarchive(args: argparse.Namespace) -> None:
    """恢复归档会话。"""
    runtime = read_runtime()
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{_api_base(runtime)}/threads/{args.thread_id}/unarchive",
            headers=_auth_headers(runtime),
        )
        response.raise_for_status()
    print("已恢复归档")


def cmd_delete(args: argparse.Namespace) -> None:
    """删除会话并返回备份 ID。"""
    runtime = read_runtime()
    with httpx.Client(timeout=30.0) as client:
        response = client.request(
            "DELETE",
            f"{_api_base(runtime)}/threads/{args.thread_id}",
            headers=_auth_headers(runtime),
        )
        response.raise_for_status()
        data = response.json()
    print(f"已删除，备份: {data['backup_id']}")


def cmd_memory_get(args: argparse.Namespace) -> None:
    """读取长期记忆。"""
    runtime = read_runtime()
    params: dict[str, Any] = {"scope": args.scope}
    if args.workspace:
        params["workspace"] = str(Path(args.workspace).resolve())
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            f"{_api_base(runtime)}/memory",
            headers=_auth_headers(runtime),
            params=params,
        )
        response.raise_for_status()
        data = response.json()
    print(f"版本: {data['version']}")
    print(data.get("content") or "（暂无长期记忆）")


def cmd_memory_set(args: argparse.Namespace) -> None:
    """写入长期记忆。"""
    runtime = read_runtime()
    payload: dict[str, Any] = {
        "scope": args.scope,
        "content": args.content,
        "mode": args.mode,
    }
    if args.workspace:
        payload["workspace"] = str(Path(args.workspace).resolve())
    if args.base_version:
        payload["base_version"] = args.base_version
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{_api_base(runtime)}/memory",
            headers=_auth_headers(runtime),
            json=payload,
        )
        response.raise_for_status()
    print("已写入长期记忆")


def cmd_tool(args: argparse.Namespace) -> None:
    """执行工具，需要审批时行内交互。"""
    runtime = read_runtime()
    thread_id = args.thread
    try:
        tool_args = json.loads(args.args)
    except json.JSONDecodeError as exc:
        print(f"错误: 参数不是合法 JSON: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    payload = {
        "thread_id": thread_id,
        "workspace": str(Path.cwd().resolve()),
        "tool": args.tool,
        "args": tool_args,
    }
    with httpx.Client(timeout=600.0) as client:
        with client.stream(
            "POST",
            f"{_api_base(runtime)}/tools/run",
            headers=_auth_headers(runtime),
            json=payload,
        ) as response:
            response.raise_for_status()
            for event_name, data in _iter_sse_events(response):
                if event_name == "approval_requested":
                    thread_id = data["thread_id"]
                    options = data.get("options", [])
                    print(render_tool_card(data["tool"], data["args"], "需要审批"))
                    if data.get("message"):
                        print(f"    {data['message']}")
                    decision = _prompt_approval(options=options)
                    approve_response = client.post(
                        f"{_api_base(runtime)}/threads/{thread_id}/approve",
                        headers=_auth_headers(runtime),
                        json={"approval_id": data["id"], "decision": decision},
                    )
                    approve_response.raise_for_status()
                    print(render_tool_card(data["tool"], data["args"], "已审批" if decision != "deny" else "已拒绝"))
                elif event_name == "tool_result":
                    status = {
                        "success": "完成",
                        "denied": "已拒绝",
                        "failed": "失败",
                    }.get(data.get("status"), data.get("status", "失败"))
                    print(
                        render_tool_card(
                            args.tool,
                            tool_args,
                            status,
                            output=data.get("output"),
                            backup_id=data.get("backup_id"),
                        )
                    )
                elif event_name == "error":
                    print(f"错误: {data.get('message', '未知错误')}", file=sys.stderr)


def cmd_approve(args: argparse.Namespace) -> None:
    """手动提交审批结果。"""
    runtime = read_runtime()
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{_api_base(runtime)}/threads/{args.thread_id}/approve",
            headers=_auth_headers(runtime),
            json={"approval_id": args.approval_id, "decision": args.decision},
        )
        response.raise_for_status()
    print("已提交审批")


def cmd_agent(args: argparse.Namespace) -> None:
    """子 Agent：run 执行子任务，list 查看最近任务。"""
    runtime = read_runtime()
    if args.agent_command == "run":
        payload: dict[str, Any] = {"task": args.task}
        if args.allowed_tools:
            payload["allowed_tools"] = list(args.allowed_tools)
        if args.budget:
            payload["budget"] = args.budget
        with httpx.Client(timeout=600.0) as client:
            response = client.post(
                f"{_api_base(runtime)}/agents",
                headers=_auth_headers(runtime),
                json=payload,
            )
            if response.status_code == 400:
                print(f"错误: {response.json()['error']['message']}", file=sys.stderr)
                raise SystemExit(1)
            response.raise_for_status()
            data = response.json()
        print(f"子 Agent {data['subagent_id']} [{data['status']}]")
        print(data.get("output") or "（无输出）")
        return
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            f"{_api_base(runtime)}/agents",
            headers=_auth_headers(runtime),
        )
        response.raise_for_status()
        agents = response.json().get("agents", [])
    print(render_agent_panel(agents))


def cmd_mcp(args: argparse.Namespace) -> None:
    """列出 MCP 服务状态。"""
    del args
    runtime = read_runtime()
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            f"{_api_base(runtime)}/mcp",
            headers=_auth_headers(runtime),
        )
        response.raise_for_status()
        data = response.json()
    print(render_mcp_panel(data.get("servers", [])))


def cmd_skills(args: argparse.Namespace) -> None:
    """SKILL 列表与开关。"""
    runtime = read_runtime()
    with httpx.Client(timeout=30.0) as client:
        if args.action in {"enable", "disable"}:
            if not args.name:
                print("错误: enable/disable 需要指定 SKILL 名称", file=sys.stderr)
                raise SystemExit(1)
            url = f"{_api_base(runtime)}/skills/{args.name}/{'enable' if args.action == 'enable' else 'disable'}"
            payload = {"confirm": True} if args.confirm else {}
            response = client.post(url, headers=_auth_headers(runtime), json=payload)
            if response.status_code == 409:
                print("SKILL 同名冲突，需要确认。再次 enable 时加 --confirm 确认启用。", file=sys.stderr)
                raise SystemExit(1)
            response.raise_for_status()
            print(f"已{'启用' if args.action == 'enable' else '禁用'} {args.name}")
        else:
            response = client.get(
                f"{_api_base(runtime)}/skills",
                headers=_auth_headers(runtime),
            )
            response.raise_for_status()
            skills = response.json().get("skills", [])
            print(render_skills_panel(skills))


def cmd_ui(args: argparse.Namespace) -> None:
    """无子命令入口：自动连接/启动核心并进入会话。"""
    workspace = Path(args.workspace or Path.cwd()).resolve()
    core_root = select_core_project_root(workspace)
    core = ensure_core(project_root=core_root, workspace=workspace, preferred_port=args.port)
    try:
        settings = load_settings(core_root)
        welcome = render_welcome(settings, workspace)
        status_bar = render_status_bar(settings, workspace)
        print(welcome)
        print()
        with httpx.Client(timeout=600.0) as client:
            _run_session_loop(
                client=client,
                runtime=core.runtime,
                workspace=workspace,
                status_bar=status_bar,
            )
    finally:
        core.stop()


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(prog="minic", description="MiniC 本地知识问答助手")
    parser.add_argument("--workspace", type=Path, default=None, help="工作区路径")
    parser.add_argument("--port", type=int, default=None, help="自动启动核心时的优先端口")
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser("serve", help="启动核心服务")
    serve_parser.add_argument("--port", type=int, default=None, help="优先使用的端口")
    serve_parser.add_argument("--project", type=Path, default=Path.cwd(), help="项目根目录")
    serve_parser.set_defaults(func=cmd_serve)

    status_parser = subparsers.add_parser("status", help="检查核心服务连接")
    status_parser.set_defaults(func=cmd_status)

    ingest_parser = subparsers.add_parser("ingest", help="入库 Markdown 文件夹")
    ingest_parser.add_argument("path", help="文件夹或文件路径")
    ingest_parser.add_argument("--extensions", nargs="*", default=[".md"], help="文件扩展名")
    ingest_parser.add_argument("--source", default=None, help="知识库来源标识")
    ingest_parser.set_defaults(func=cmd_ingest)

    query_parser = subparsers.add_parser("query", help="执行单次检索")
    query_parser.add_argument("query", help="检索问题")
    query_parser.add_argument("--top-k", type=int, default=5, help="返回条数")
    query_parser.add_argument("--source", default=None, help="知识库来源过滤")
    query_parser.set_defaults(func=cmd_query)

    chat_parser = subparsers.add_parser("chat", help="进入交互式问答")
    chat_parser.add_argument("--path", default=None, help="先入库的 Markdown 文件夹")
    chat_parser.add_argument("--thread", default=None, help="复用已有会话 ID")
    chat_parser.set_defaults(func=cmd_chat)

    threads_parser = subparsers.add_parser("threads", help="列出会话")
    threads_parser.add_argument("--workspace", default=Path.cwd(), help="工作区路径")
    threads_parser.add_argument("--archived", action="store_true", default=None, help="只显示已归档会话")
    threads_parser.add_argument("--page-size", type=int, default=50, help="分页大小")
    threads_parser.set_defaults(func=cmd_threads)

    resume_parser = subparsers.add_parser("resume", help="恢复会话")
    resume_parser.add_argument("thread_id", help="会话 ID")
    resume_parser.set_defaults(func=cmd_resume)

    compress_parser = subparsers.add_parser("compress", help="压缩会话")
    compress_parser.add_argument("thread_id", help="会话 ID")
    compress_parser.set_defaults(func=cmd_compress)

    archive_parser = subparsers.add_parser("archive", help="归档会话")
    archive_parser.add_argument("thread_id", help="会话 ID")
    archive_parser.set_defaults(func=cmd_archive)

    unarchive_parser = subparsers.add_parser("unarchive", help="恢复归档会话")
    unarchive_parser.add_argument("thread_id", help="会话 ID")
    unarchive_parser.set_defaults(func=cmd_unarchive)

    delete_parser = subparsers.add_parser("delete", help="删除会话")
    delete_parser.add_argument("thread_id", help="会话 ID")
    delete_parser.set_defaults(func=cmd_delete)

    memory_parser = subparsers.add_parser("memory", help="长期记忆")
    memory_subparsers = memory_parser.add_subparsers(dest="memory_command", required=True)
    memory_get_parser = memory_subparsers.add_parser("get", help="读取长期记忆")
    memory_get_parser.add_argument("--scope", choices=["global", "project", "merged"], default="merged")
    memory_get_parser.add_argument("--workspace", default=None, help="项目工作区路径")
    memory_get_parser.set_defaults(func=cmd_memory_get)
    memory_set_parser = memory_subparsers.add_parser("set", help="写入长期记忆")
    memory_set_parser.add_argument("content", help="记忆内容")
    memory_set_parser.add_argument("--scope", choices=["global", "project"], default="project")
    memory_set_parser.add_argument("--workspace", default=None, help="项目工作区路径")
    memory_set_parser.add_argument("--mode", choices=["replace", "merge"], default="replace")
    memory_set_parser.add_argument("--base-version", default=None, help="期望的当前版本")
    memory_set_parser.set_defaults(func=cmd_memory_set)

    tool_parser = subparsers.add_parser("tool", help="执行工具")
    tool_parser.add_argument(
        "tool",
        choices=[
            "Read",
            "ReadMemory",
            "Write",
            "Edit",
            "TextSearch",
            "Lint",
            "Format",
            "Bash",
            "GitStatus",
            "GitDiff",
            "GitLog",
            "GitCommit",
            "GitBranch",
            "DelegateToSubagent",
            "IngestDirectory",
        ],
    )
    tool_parser.add_argument("args", help="JSON 参数，例如 {\"path\":\"a.txt\"}")
    tool_parser.add_argument("--thread", default=None, help="会话 ID")
    tool_parser.set_defaults(func=cmd_tool)

    approve_parser = subparsers.add_parser("approve", help="提交审批")
    approve_parser.add_argument("thread_id", help="会话 ID")
    approve_parser.add_argument("approval_id", help="审批 ID")
    approve_parser.add_argument(
        "decision",
        choices=["allow_once", "allow_session", "allow_always", "deny"],
    )
    approve_parser.set_defaults(func=cmd_approve)

    mcp_parser = subparsers.add_parser("mcp", help="查看 MCP 服务状态")
    mcp_parser.set_defaults(func=cmd_mcp)

    skills_parser = subparsers.add_parser("skills", help="查看 SKILL 列表与开关")
    skills_parser.add_argument(
        "action",
        nargs="?",
        choices=["list", "enable", "disable"],
        default="list",
        help="操作：list 列表（默认）/ enable 启用 / disable 禁用",
    )
    skills_parser.add_argument("name", nargs="?", default=None, help="SKILL 名称")
    skills_parser.add_argument("--confirm", action="store_true", help="同名冲突时确认启用（仅 enable）")
    skills_parser.set_defaults(func=cmd_skills)

    agent_parser = subparsers.add_parser("agent", help="子 Agent 任务与状态")
    agent_subparsers = agent_parser.add_subparsers(dest="agent_command", required=True)
    agent_run_parser = agent_subparsers.add_parser("run", help="执行子任务")
    agent_run_parser.add_argument("task", help="子任务指令")
    agent_run_parser.add_argument("--allowed-tools", nargs="*", default=None, help="允许使用的工具白名单")
    agent_run_parser.add_argument("--budget", type=int, default=None, help="工具循环最大轮数")
    agent_run_parser.set_defaults(func=cmd_agent)
    agent_list_parser = agent_subparsers.add_parser("list", help="查看最近子任务")
    agent_list_parser.set_defaults(func=cmd_agent)

    return parser


def main() -> None:
    """CLI 入口。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args()
    if args.command is None:
        cmd_ui(args)
    else:
        args.func(args)


if __name__ == "__main__":
    main()
