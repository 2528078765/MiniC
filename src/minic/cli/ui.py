"""CLI Welcome 面板与状态栏渲染。"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from minic.core.config import AppSettings


WELCOME_FRAME_COLOR = "\033[38;2;79;195;247m"
WELCOME_LINE_COLOR = "\033[38;2;179;229;252m"
RESET = "\033[0m"

APPROVAL_CHOICES = [
    ("allow_once", ["allow_once", "允许本次", "1"]),
    ("allow_session", ["allow_session", "允许会话", "2"]),
    ("allow_always", ["allow_always", "始终允许", "3"]),
    ("deny", ["deny", "拒绝", "4"]),
]

_APPROVAL_LABELS = {
    "allow_once": "允许本次",
    "allow_session": "允许会话",
    "allow_always": "始终允许",
    "deny": "拒绝",
}


def model_display(settings: AppSettings, context_length: int | None = None) -> str:
    """返回动态模型显示文本（第一个启用模型）。"""
    primary = settings.model.primary()
    label = f"{primary.provider}/{primary.model}"
    if context_length is not None:
        return f"{label}[{context_length}]"
    return f"{label}[model]"


def _mode_label(settings: AppSettings, mode: str | None = None) -> str:
    """返回审批模式文本。"""
    if mode:
        return mode
    if settings.approval.workspace_write_auto_approve:
        return "auto mode on"
    return "manual mode on"


def _frame(value: str) -> str:
    """把外框与 M Logo 渲染为天蓝色。"""
    return f"{WELCOME_FRAME_COLOR}{value}{RESET}"


def _line(value: str) -> str:
    """把中间分隔线渲染为更浅的天蓝色。"""
    return f"{WELCOME_LINE_COLOR}{value}{RESET}"


def _fit(text: str, width: int) -> str:
    """把文本截断到指定宽度。"""
    if len(text) <= width:
        return text
    return text[: max(width - 1, 1)] + "…"


def render_welcome(
    settings: AppSettings,
    workspace: Path,
    mode: str | None = None,
    context_length: int | None = None,
    width: int | None = None,
) -> str:
    """渲染左右分栏的 Welcome 面板。"""
    terminal_width = width or min(shutil.get_terminal_size((120, 30)).columns, 120)
    line_width = max(40, terminal_width - 4)
    left_width = max(20, int(line_width * 0.3))
    right_width = max(20, line_width - left_width - 1)
    effective_context = context_length if context_length is not None else settings.model.primary().context_length
    model_short = settings.model.primary().model
    model_short = (
        f"{model_short}[{effective_context}]"
        if effective_context is not None
        else f"{model_short}[model]"
    )

    left_lines = [
        "                █▀▀▀▀▀▀▀█",
        "                █   █   █",
        "                █   █   █",
        "                █   █   █",
        "",
        "               Welcome back!",
        "",
        f"  {model_short}",
        "  API Usage Billing",
        f"  {workspace}",
    ]
    right_lines = [
        "  Tips for getting started",
        "  " + "─" * 40,
        "  Run /init to create MiniC.md",
        "  Type /help to see all commands",
        "",
        "  What's new",
        "  " + "─" * 40,
        "  CLI now supports /new /clear /memory /rag",
        "  Write operations require approval",
        "  /release-notes for more",
    ]

    logo_indexes = set(range(4))
    content_lines = []
    for index in range(max(len(left_lines), len(right_lines))):
        left = left_lines[index] if index < len(left_lines) else ""
        right = right_lines[index] if index < len(right_lines) else ""
        left = _fit(left, left_width).ljust(left_width)
        if index in logo_indexes:
            left = _frame(left)
        if right.strip().startswith("─"):
            right_rendered = _line(_fit(right, right_width).ljust(right_width))
        else:
            right_rendered = _fit(right, right_width).ljust(right_width)
        content_lines.append(left + _line("│") + right_rendered)

    border_inner = line_width + 2
    top = _frame("╭" + "─" * border_inner + "╮")
    bottom = _frame("╰" + "─" * border_inner + "╯")
    lines = [top]
    for line in content_lines:
        lines.append(_frame("│ ") + line + _frame(" │"))
    lines.append(bottom)
    return "\n".join(lines)


def render_status_bar(
    settings: AppSettings,
    workspace: Path,
    mode: str | None = None,
    context_length: int | None = None,
) -> str:
    """渲染动态状态栏。"""
    effective_context = context_length if context_length is not None else settings.model.primary().context_length
    model_text = model_display(settings, effective_context)
    mode_text = _mode_label(settings, mode)
    return (
        f"{model_text} · {workspace} · {mode_text} · "
        "? for shortcuts · /help · /new · /clear · ← for agents"
    )


def render_tool_card(
    tool: str,
    args: dict[str, Any],
    status: str,
    output: str | None = None,
    backup_id: str | None = None,
) -> str:
    """渲染工具调用卡片。"""
    status_map = {
        "pending": "── 需要审批",
        "approval_requested": "── 需要审批",
        "需要审批": "── 需要审批",
        "approved": "── 已审批",
        "已审批": "── 已审批",
        "success": "── 完成",
        "completed": "── 完成",
        "完成": "── 完成",
        "denied": "── 已拒绝",
        "已拒绝": "── 已拒绝",
        "failed": "── 失败",
        "失败": "── 失败",
        "expired": "── 已过期",
    }
    lines = [f"  ✦ {tool}"]
    for key, value in args.items():
        lines.append(f"    {key}: {value}")
    lines.append(f"    {status_map.get(status, status)}")
    if output:
        lines.append(f"    {output}")
    if backup_id:
        lines.append(f"    备份: {backup_id}")
    return "\n".join(lines)


def render_sources(sources: list[dict[str, Any]]) -> str:
    """渲染独立来源块。"""
    if not sources:
        return ""
    lines = ["  ── 来源 ──"]
    for source in sources:
        lines.append(f"  - {source['file_path']} [{source['section']}]")
    return "\n".join(lines)


def render_answer_block(text: str) -> str:
    """渲染独立回答块。"""
    return f"  ── 回答 ──\n  {text}"


def normalize_approval_input(value: str) -> str | None:
    """把审批输入归一化为标准决策。"""
    text = value.strip().lower()
    if not text:
        return "allow_once"
    for decision, aliases in APPROVAL_CHOICES:
        if text in aliases or text in [alias.lower() for alias in aliases]:
            return decision
    return None


def render_approval_menu(options: list[str] | None = None) -> str:
    """渲染行内审批菜单，按 options 顺序展示可用决策（默认四项）。

    Bash 等 full 权限传 options=["allow_once", "deny"] 只显示两项；
    IngestDirectory 传 options=["allow_once", "allow_always", "deny"] 显示三项。
    """
    choices = options or ["allow_once", "allow_session", "allow_always", "deny"]
    labels = " / ".join(_APPROVAL_LABELS.get(choice, choice) for choice in choices)
    return f"  ? {labels}\n  > "


def render_thread_panel(threads: list[dict[str, Any]]) -> str:
    """渲染会话列表面板。"""
    if not threads:
        return "  ── 会话列表 ──\n  （暂无会话）"
    lines = ["  ── 会话列表 ──"]
    for index, thread in enumerate(threads, start=1):
        state = "已归档" if thread.get("archived") else "活跃"
        lines.append(f"  {index}. {thread['thread_id']} {thread.get('name', '')} [{state}]")
    return "\n".join(lines)


def _indent(text: str, prefix: str) -> str:
    """给多行文本统一缩进。"""
    return "\n".join(f"{prefix}{line}" for line in text.splitlines())


def render_memory_panel(
    global_content: str,
    project_content: str,
    merged_content: str,
) -> str:
    """渲染长期记忆面板。"""
    lines = ["  ── 长期记忆 ──"]
    lines.append("  全局：")
    lines.append(_indent(global_content or "（暂无记忆）", "    "))
    lines.append("  项目：")
    lines.append(_indent(project_content or "（暂无记忆）", "    "))
    lines.append("  合并：")
    lines.append(_indent(merged_content or "（暂无记忆）", "    "))
    return "\n".join(lines)


def render_rag_panel(status: dict[str, Any]) -> str:
    """渲染 RAG 状态面板。"""
    total_documents = int(status.get("total_documents", 0))
    total_chunks = int(status.get("total_chunks", 0))
    lines = ["  ── RAG 状态 ──"]
    if total_documents == 0 and total_chunks == 0:
        lines.append("  （暂无索引）")
    else:
        lines.append(f"  文档数: {total_documents}")
        lines.append(f"  分块数: {total_chunks}")
        lines.append(f"  Embedding: {status.get('embedding_model', '')}")
        lines.append(f"  最近入库: {status.get('last_ingest_at') or '（未知）'}")
    return "\n".join(lines)


def render_agent_panel(
    agents: list[dict[str, Any]] | None = None,
    max_concurrent: int | None = None,
) -> str:
    """渲染子 Agent 状态面板（真实数据）。"""
    lines = ["  ── 子 Agent ──"]
    agents = agents or []
    concurrency = max_concurrent or 3
    lines.append(f"  并发 {len(agents)}/{concurrency}")
    if not agents:
        lines.append("  （暂无子任务）")
        return "\n".join(lines)
    status_text = {"completed": "完成", "failed": "失败", "denied": "已拒绝"}
    for agent in agents:
        status = status_text.get(agent.get("status", ""), agent.get("status", ""))
        summary = agent.get("output_summary") or agent.get("output") or ""
        lines.append(f"  {agent.get('subagent_id', '')} [{status}] {summary}")
    return "\n".join(lines)


def render_mcp_panel(servers: list[dict[str, Any]] | None = None) -> str:
    """渲染 MCP 状态面板（真实数据）。"""
    lines = ["  ── MCP 服务 ──"]
    servers = servers or []
    if not servers:
        lines.append("  （未配置 MCP 服务）")
        return "\n".join(lines)
    for server in servers:
        name = server.get("name", "")
        transport = server.get("transport", "")
        status = server.get("status", "")
        endpoint = server.get("url") or server.get("command") or ""
        tools_count = int(server.get("tools_count", 0))
        error_message = server.get("error_message")
        status_text = {
            "connected": "已连接",
            "connecting": "连接中",
            "error": "错误",
            "disabled": "已禁用",
        }.get(status, status)
        lines.append(f"  {name} [{status_text}] {transport} {endpoint}")
        if status == "connected":
            lines.append(f"    工具数: {tools_count}")
        elif error_message:
            lines.append(f"    错误: {error_message}")
    return "\n".join(lines)


def render_skills_panel(skills: list[dict[str, Any]] | None = None) -> str:
    """渲染 SKILL 状态面板（真实数据）。"""
    lines = ["  ── SKILL 技能 ──"]
    skills = skills or []
    if not skills:
        lines.append("  （未配置 SKILL）")
        return "\n".join(lines)
    scope_text = {"global": "全局", "project": "项目"}
    for skill in skills:
        name = skill.get("name", "")
        state = "已启用" if skill.get("enabled") else "已禁用"
        scope = scope_text.get(skill.get("scope", ""), skill.get("scope", ""))
        entry = f"  {name} [{state}] [{scope}]"
        if skill.get("conflict"):
            entry += "（冲突，项目级优先）"
        lines.append(entry)
        if skill.get("description"):
            lines.append(f"    {skill.get('description')}")
        if skill.get("when_to_use"):
            lines.append(f"    适用场景：{skill.get('when_to_use')}")
        if skill.get("allowed_tools"):
            lines.append(f"    允许工具：{', '.join(skill.get('allowed_tools'))}")
    return "\n".join(lines)
