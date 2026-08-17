"""G7b SKILL 扫描与开关测试。

覆盖：frontmatter 解析、两级目录扫描、同名冲突、enable/disable 状态持久化、
HTTP /skills 接口（含 409 冲突路径）、allowed-tools 强制执行、系统提示注入、CLI 面板渲染。
所有测试使用 tmp_path，不触碰真实 ~/.minic 或项目 .minic。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from minic.cli.ui import render_skills_panel
from minic.core.config import AppSettings
from minic.core.server import create_app
from minic.graph import build_super_graph
from minic.graph.action.action_node import action_answer_node
from minic.graph.knowledge.knowledge_node import knowledge_answer_node
from minic.graph.super_node import route_node
from minic.recovery import ToolExecutionLog
from minic.skills import (
    SkillManager,
    load_skill_state,
    parse_skill_md,
    save_skill_state,
    scan_skills,
)
from minic.tools.runtime import ToolRuntime
from minic.tools.service import ApprovalManager, PermissionStore, ToolExecutor


AUTH_HEADERS = {"Authorization": "Bearer test-token"}  # 测试令牌


def _write_skill(
    skills_dir: Path,
    name: str,
    *,
    description: str = "",
    when_to_use: str = "",
    allowed_tools: list[str] | None = None,
    no_frontmatter: bool = False,
    no_name: bool = False,
) -> Path:
    """在 skills 目录下构造一个 SKILL.md fixture，返回 SKILL.md 路径。"""
    skill_dir = Path(skills_dir) / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    if no_frontmatter:
        content = f"# {name}\n\n没有 frontmatter 的内容。"
    else:
        parts = ["---", f"name: {name if not no_name else ''}"]
        if description:
            parts.append(f"description: {description}")
        if when_to_use:
            parts.append(f"when_to_use: {when_to_use}")
        if allowed_tools is not None:
            parts.append("allowed-tools:")
            for tool in allowed_tools:
                parts.append(f"  - {tool}")
        parts.append("---")
        parts.append("")
        parts.append(f"# {name}")
        content = "\n".join(parts)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(content, encoding="utf-8")
    return skill_md


def _env(tmp_path: Path) -> dict[str, Path]:
    """返回隔离的全局/项目 skills 目录与状态文件路径。"""
    return {
        "global": tmp_path / "home" / ".minic" / "skills",
        "project": tmp_path / "proj" / ".minic" / "skills",
        "state": tmp_path / "proj" / ".minic" / "skills_state.json",
    }


def _manager(env: dict[str, Path]) -> SkillManager:
    """构造 SkillManager。"""
    return SkillManager(env["global"], env["project"], env["state"])


# ---------------------------------------------------------------- 扫描解析


def test_parse_skill_md_full_fields(tmp_path: Path) -> None:
    """frontmatter 字段完整解析：name/description/when_to_use/allowed-tools。"""
    skill_md = _write_skill(
        tmp_path,
        "codegen",
        description="生成代码的能力",
        when_to_use="用户要求写代码时",
        allowed_tools=["Read", "Write"],
    )
    spec = parse_skill_md(skill_md)
    assert spec is not None
    assert spec.name == "codegen"
    assert spec.description == "生成代码的能力"
    assert spec.when_to_use == "用户要求写代码时"
    assert spec.allowed_tools == ["Read", "Write"]
    assert spec.path == skill_md.resolve()
    assert spec.scope == "global"


def test_parse_skill_md_skips_no_frontmatter_and_no_name(tmp_path: Path) -> None:
    """无 frontmatter 或 name 缺失的文件跳过。"""
    assert parse_skill_md(_write_skill(tmp_path, "plain", no_frontmatter=True)) is None
    assert parse_skill_md(_write_skill(tmp_path, "noname", no_name=True)) is None
    # 非 SKILL.md 文件不存在时返回 None
    assert parse_skill_md(tmp_path / "missing" / "SKILL.md") is None


def test_scan_skills_merges_two_levels(tmp_path: Path) -> None:
    """全局与项目两级目录合并返回全部 skill。"""
    env = _env(tmp_path)
    _write_skill(env["global"], "pilot", description="全局驾驶助手")
    _write_skill(env["project"], "docs", description="项目文档技能")
    specs = scan_skills(env["global"], env["project"])
    by_name = {spec.name: spec for spec in specs}
    assert set(by_name) == {"pilot", "docs"}
    assert by_name["pilot"].scope == "global"
    assert by_name["docs"].scope == "project"


def test_scan_skills_conflict_marks_project_entry(tmp_path: Path) -> None:
    """同名冲突时项目级条目 conflict=True，全局条目保留。"""
    env = _env(tmp_path)
    _write_skill(env["global"], "pilot", description="全局驾驶助手")
    _write_skill(env["project"], "pilot", description="项目驾驶助手")
    specs = scan_skills(env["global"], env["project"])
    assert len(specs) == 2
    project_entry = next(spec for spec in specs if spec.scope == "project")
    global_entry = next(spec for spec in specs if spec.scope == "global")
    assert project_entry.conflict is True
    assert global_entry.conflict is False
    assert global_entry.name == project_entry.name == "pilot"


# ---------------------------------------------------------------- 开关与持久化


def test_enable_disable_persists_state(tmp_path: Path) -> None:
    """enable/disable 写 skills_state.json，新实例加载后状态恢复。"""
    env = _env(tmp_path)
    _write_skill(env["project"], "docs", description="文档技能")
    mgr = _manager(env)

    result, _ = mgr.enable("docs")
    assert result == "ok"
    state = json.loads(env["state"].read_text(encoding="utf-8"))
    assert state["enabled"]["docs"]["scope"] == "project"

    mgr2 = _manager(env)  # 新实例应恢复启用状态
    assert mgr2.list()[0]["enabled"] is True

    mgr2.disable("docs")
    assert not mgr2.active()
    state = json.loads(env["state"].read_text(encoding="utf-8"))
    assert state["enabled"] == {}


def test_conflict_skill_requires_confirmation(tmp_path: Path) -> None:
    """同名冲突首次 enable 返回 needs_confirmation，confirm 后启用并持久化。"""
    env = _env(tmp_path)
    _write_skill(env["global"], "pilot", description="全局")
    _write_skill(env["project"], "pilot", description="项目")
    mgr = _manager(env)

    result, _ = mgr.enable("pilot")
    assert result == "needs_confirmation"
    assert not mgr.active()

    result, _ = mgr.enable("pilot", confirm=True)
    assert result == "ok"
    assert [spec.name for spec in mgr.active()] == ["pilot"]
    assert mgr.active()[0].scope == "project"  # 项目级优先生效
    state = json.loads(env["state"].read_text(encoding="utf-8"))
    assert state["enabled"]["pilot"]["confirmed"] is True

    # 已确认后再次 enable 直接 ok
    result, _ = mgr.enable("pilot")
    assert result == "ok"


def test_confirm_method_and_unknown_skill(tmp_path: Path) -> None:
    """confirm() 显式确认冲突后启用；未知 skill 抛 KeyError。"""
    env = _env(tmp_path)
    _write_skill(env["global"], "pilot")
    _write_skill(env["project"], "pilot")
    mgr = _manager(env)

    mgr.confirm("pilot")
    assert [spec.name for spec in mgr.active()] == ["pilot"]

    with pytest.raises(KeyError):
        mgr.enable("missing")
    with pytest.raises(KeyError):
        mgr.disable("missing")
    with pytest.raises(KeyError):
        mgr.confirm("missing")


def test_allowed_tools_and_inject_text(tmp_path: Path) -> None:
    """allowed_tools(name)、allowed_union 与 inject_text 拼接。"""
    env = _env(tmp_path)
    _write_skill(
        env["project"],
        "codegen",
        description="生成代码",
        when_to_use="写代码时",
        allowed_tools=["Read", "Write"],
    )
    _write_skill(
        env["project"],
        "pilot",
        description="驾驶",
        when_to_use="驾驶任务",
        allowed_tools=["Bash"],
    )
    mgr = _manager(env)
    mgr.enable("codegen")
    mgr.enable("pilot")

    assert mgr.allowed_tools("codegen") == ["Read", "Write"]
    assert mgr.allowed_tools("missing") == []
    assert mgr.allowed_union() == {"Read", "Write", "Bash"}
    text = mgr.inject_text()
    assert "codegen：生成代码（适用场景：写代码时）" in text
    assert "pilot：驾驶（适用场景：驾驶任务）" in text
    assert "未启用技能" not in text  # 全部启用时不带未启用清单


def test_inject_text_lists_disabled_skills(tmp_path: Path) -> None:
    """存在未启用技能时，注入文本带未启用清单。"""
    env = _env(tmp_path)
    _write_skill(env["project"], "codegen", description="生成代码")
    _write_skill(env["project"], "pilot", description="驾驶")
    mgr = _manager(env)
    mgr.enable("codegen")

    text = mgr.inject_text()
    assert "codegen" in text
    assert "未启用技能" in text
    assert "pilot" in text


def test_inject_text_truncates_over_limit(tmp_path: Path) -> None:
    """inject_text 超长时截断。"""
    env = _env(tmp_path)
    _write_skill(env["project"], "long", description="长" * 5000)
    mgr = _manager(env)
    mgr.enable("long")
    text = mgr.inject_text(max_chars=100)
    assert len(text) <= 101
    assert text.endswith("…")


def test_save_load_skill_state_roundtrip(tmp_path: Path) -> None:
    """save_skill_state/load_skill_state 幂等往返。"""
    state = {"enabled": {"a": {"scope": "project", "confirmed": True}}}
    save_skill_state(tmp_path / "skills_state.json", state)
    loaded = load_skill_state(tmp_path / "skills_state.json")
    assert loaded == state


# ---------------------------------------------------------------- HTTP 接口


def _http_app(tmp_path: Path, settings: AppSettings, env: dict[str, Path] | None = None):
    """构造带隔离 skills 目录的应用（env 内的 skills 需先写入再调用）。"""
    env = env or _env(tmp_path)
    app = create_app(
        settings=settings,
        token="test-token",
        project_root=tmp_path / "proj",
        rag_data_dir=tmp_path / "rag-data",
        short_memory_dir=tmp_path / "memory" / "short_memory",
        skills_global_dir=env["global"],
        skills_project_dir=env["project"],
        mcp_settings_path=tmp_path / "mcp_settings.json",  # 空 MCP 配置，避免连接真实服务
    )
    return app, env


def test_get_skills_endpoint_lists_all(tmp_path: Path, settings: AppSettings) -> None:
    """GET /skills 返回完整列表（含 enabled/scope/conflict）。"""
    env = _env(tmp_path)
    _write_skill(env["global"], "pilot", description="全局驾驶")
    _write_skill(env["project"], "pilot", description="项目驾驶", allowed_tools=["Read"])
    _write_skill(env["project"], "docs")
    app, env = _http_app(tmp_path, settings, env)
    with TestClient(app) as client:
        data = client.get("/skills", headers=AUTH_HEADERS)
        data.raise_for_status()
        skills = data.json()["skills"]
        assert len(skills) == 3
        pilot_project = next(s for s in skills if s["name"] == "pilot" and s["scope"] == "project")
        pilot_global = next(s for s in skills if s["name"] == "pilot" and s["scope"] == "global")
        assert pilot_project["conflict"] is True
        assert pilot_project["allowed_tools"] == ["Read"]
        assert pilot_global["conflict"] is False
        assert all(s["enabled"] is False for s in skills)


def test_enable_conflict_returns_409_then_confirm(tmp_path: Path, settings: AppSettings) -> None:
    """enable 冲突未确认返回 409；带 confirm=true 直接启用；disable 返回 204。"""
    env = _env(tmp_path)
    _write_skill(env["global"], "pilot", description="全局")
    _write_skill(env["project"], "pilot", description="项目")
    app, env = _http_app(tmp_path, settings, env)
    with TestClient(app) as client:
        # 未确认 -> 409
        response = client.post("/skills/pilot/enable", headers=AUTH_HEADERS, json={})
        assert response.status_code == 409
        body = response.json()
        assert body["error"]["code"] == "CONFLICT"
        assert body["error"]["message"] == "SKILL 同名冲突，需要确认"
        assert body["error"]["detail"]["name"] == "pilot"
        assert body["error"]["detail"]["conflict"] is True

        # 确认启用 -> 200 且状态落盘
        response = client.post("/skills/pilot/enable", headers=AUTH_HEADERS, json={"confirm": True})
        assert response.status_code == 200
        state = json.loads(env["state"].read_text(encoding="utf-8"))
        assert state["enabled"]["pilot"]["confirmed"] is True
        skills = client.get("/skills", headers=AUTH_HEADERS).json()["skills"]
        pilot_project = next(s for s in skills if s["name"] == "pilot" and s["scope"] == "project")
        assert pilot_project["enabled"] is True

        # 禁用 -> 204
        response = client.post("/skills/pilot/disable", headers=AUTH_HEADERS)
        assert response.status_code == 204
        skills = client.get("/skills", headers=AUTH_HEADERS).json()["skills"]
        assert all(s["enabled"] is False for s in skills if s["name"] == "pilot")

        # 未知 skill -> 404
        assert client.post("/skills/nope/enable", headers=AUTH_HEADERS).status_code == 404
        assert client.post("/skills/nope/disable", headers=AUTH_HEADERS).status_code == 404


def test_enable_non_conflict_directly(tmp_path: Path, settings: AppSettings) -> None:
    """非冲突 skill 直接启用，无 409。"""
    env = _env(tmp_path)
    _write_skill(env["project"], "docs")
    app, env = _http_app(tmp_path, settings, env)
    with TestClient(app) as client:
        response = client.post("/skills/docs/enable", headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert response.json()["enabled"] is True


# ---------------------------------------------------------------- allowed-tools 强制


def _tool_env(tmp_path: Path, skill_manager: SkillManager | None):
    """构造 ToolRuntime 环境。"""
    permission_store = PermissionStore(
        tmp_path / "global-permissions.json",
        tmp_path / ".minic" / "permissions.json",
    )
    tool_executor = ToolExecutor(tmp_path, tmp_path / ".minic" / "backups" / "files")
    approval_manager = ApprovalManager(permission_store, tmp_path, AppSettings())
    tool_log = ToolExecutionLog(tmp_path / ".minic" / "logs" / "tool_execution.jsonl")
    tool_runtime = ToolRuntime(
        approval_manager,
        tool_executor,
        tool_log,
        skill_manager=skill_manager,
    )
    return tool_runtime


async def _execute(runtime: ToolRuntime, thread: str, tool: str, args: dict[str, Any]):
    """并发消费工具事件执行工具；遇到审批自动 allow_once，避免审批超时挂起。"""
    queue: asyncio.Queue = asyncio.Queue()
    runtime.attach(thread, lambda name, data: queue.put_nowait((name, data)))
    events: list[tuple[str, dict[str, Any]]] = []
    task = asyncio.create_task(
        runtime.execute(
            thread_id=thread,
            run_id=f"run-{thread}",
            tool=tool,
            args=args,
        )
    )
    try:
        while not task.done():
            try:
                name, data = await asyncio.wait_for(queue.get(), timeout=10)
            except asyncio.TimeoutError:
                break
            events.append((name, data))
            if name == "approval_requested":
                runtime.approval_manager.submit(thread, data["id"], "allow_once")
        result = await task
        while not queue.empty():
            events.append(queue.get_nowait())
        return result, events
    finally:
        runtime.detach(thread)


def test_allowed_tools_whitelist_enforced(tmp_path: Path) -> None:
    """启用带 allowed-tools 的 skill 后，白名单内工具可执行、之外被拒绝。"""
    env = _env(tmp_path)
    _write_skill(env["project"], "guard", allowed_tools=["Read"])
    mgr = _manager(env)
    mgr.enable("guard", confirm=True)

    target = tmp_path / "a.txt"
    target.write_text("hello", encoding="utf-8")
    runtime = _tool_env(tmp_path, mgr)

    # Read 在 allowed-tools 内 -> 自动执行
    result, events = asyncio.run(_execute(runtime, "t1", "Read", {"path": str(target)}))
    assert result["status"] == "completed"
    assert result["output"] == "hello"
    assert not any(name == "approval_requested" for name, _ in events)

    # TextSearch 不在 allowed-tools 内 -> denied + SKILL 限制
    result, events = asyncio.run(
        _execute(runtime, "t2", "TextSearch", {"path": str(tmp_path), "pattern": "x"})
    )
    assert result["status"] == "denied"
    assert "SKILL 限制" in result["output"]
    names = [name for name, _ in events]
    assert "tool_call" in names and "tool_result" in names
    assert "approval_requested" not in names


def test_no_skill_unrestricted(tmp_path: Path) -> None:
    """无启用 skill 或未注入管理器时工具不受限。"""
    target = tmp_path / "a.txt"
    target.write_text("hello", encoding="utf-8")

    runtime = _tool_env(tmp_path, None)
    result, _ = asyncio.run(_execute(runtime, "t1", "TextSearch", {"path": str(tmp_path), "pattern": "hello"}))
    assert result["status"] == "completed"

    # 启用但不带 allowed-tools 的 skill 不产生白名单
    env = _env(tmp_path)
    _write_skill(env["project"], "loose")
    mgr = _manager(env)
    mgr.enable("loose")
    runtime2 = _tool_env(tmp_path, mgr)
    result, _ = asyncio.run(
        _execute(runtime2, "t2", "TextSearch", {"path": str(tmp_path), "pattern": "hello"})
    )
    assert result["status"] == "completed"


def test_approval_still_applies_within_allowed_tools(tmp_path: Path) -> None:
    """allowed-tools 内的写工具仍需审批（审批是另一道闸）。"""
    env = _env(tmp_path)
    _write_skill(env["project"], "writer", allowed_tools=["Write"])
    mgr = _manager(env)
    mgr.enable("writer", confirm=True)

    target = tmp_path / "a.txt"
    target.write_text("old", encoding="utf-8")
    runtime = _tool_env(tmp_path, mgr)
    result, events = asyncio.run(
        _execute(runtime, "t1", "Write", {"path": str(target), "content": "new"})
    )
    names = [name for name, _ in events]
    assert "approval_requested" in names  # 白名单放行但审批仍需
    assert result["status"] == "completed"  # allow_once 后执行
    assert target.read_text(encoding="utf-8") == "new"


def test_allowed_union_across_multiple_skills(tmp_path: Path) -> None:
    """多个启用 skill 的 allowed-tools 取并集。"""
    env = _env(tmp_path)
    _write_skill(env["project"], "s1", allowed_tools=["Read"])
    _write_skill(env["project"], "s2", allowed_tools=["TextSearch"])
    mgr = _manager(env)
    mgr.enable("s1")
    mgr.enable("s2")
    runtime = _tool_env(tmp_path, mgr)
    result, _ = asyncio.run(
        _execute(runtime, "t1", "TextSearch", {"path": str(tmp_path), "pattern": "hello"})
    )
    assert result["status"] == "completed"
    result, _ = asyncio.run(_execute(runtime, "t2", "Lint", {"path": str(tmp_path)}))
    assert result["status"] == "denied"


def test_http_tools_run_denied_by_skill(tmp_path: Path, settings: AppSettings) -> None:
    """/tools/run 端到端：启用 skill 后白名单外工具返回 denied。"""
    env = _env(tmp_path)
    _write_skill(env["project"], "guard", allowed_tools=["Read"])
    app, env = _http_app(tmp_path, settings, env)
    with TestClient(app) as client:
        assert client.post("/skills/guard/enable", headers=AUTH_HEADERS).status_code == 200
        payload = {
            "thread_id": None,
            "workspace": str(tmp_path / "proj"),
            "tool": "TextSearch",
            "args": {"path": str(tmp_path / "proj"), "pattern": "x"},
        }
        with client.stream("POST", "/tools/run", headers=AUTH_HEADERS, json=payload) as response:
            response.raise_for_status()
            events: list[str] = []
            denied_outputs: list[str] = []
            for line in response.iter_lines():
                if line.startswith("event:"):
                    events.append(line[6:].strip())
                elif line.startswith("data:") and events and events[-1] == "tool_result":
                    denied_outputs.append(json.loads(line[5:].strip()).get("output", ""))
        assert "tool_result" in events
        assert "approval_requested" not in events
        assert any("SKILL 限制" in output for output in denied_outputs)


# ---------------------------------------------------------------- 系统提示注入


class CaptureModel:
    """记录收到的系统提示的模型替身。"""

    def __init__(self) -> None:
        self.invoke_prompts: list[str] = []
        self.stream_prompts: list[str] = []

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        self.invoke_prompts.append(messages[0].content)
        return AIMessage(content=json.dumps({"intent": "chat_action"}))

    async def astream(self, messages: list[Any]):
        self.stream_prompts.append(messages[0].content)
        for part in ["这是", "回答。"]:
            yield AIMessageChunk(content=part)


def _enabled_skill_manager(tmp_path: Path) -> SkillManager:
    """构造一个启用 codegen 技能的 SkillManager。"""
    env = _env(tmp_path)
    _write_skill(
        env["project"],
        "codegen",
        description="生成代码的能力",
        when_to_use="用户要求写代码时",
        allowed_tools=["Read", "Write"],
    )
    mgr = _manager(env)
    mgr.enable("codegen")
    return mgr


def _state(tmp_path: Path, **extra: Any) -> dict[str, Any]:
    """构造图状态。"""
    state = {
        "thread_id": "t1",
        "workspace": str(tmp_path),
        "project_root": str(tmp_path),
        "user_message": "帮我写个函数",
        "history": [],
        "intent": "chat_action",
    }
    state.update(extra)
    return state


def test_route_node_injects_skill_text(tmp_path: Path) -> None:
    """总图路由系统提示包含启用 SKILL 描述。"""
    mgr = _enabled_skill_manager(tmp_path)
    model = CaptureModel()
    asyncio.run(route_node(_state(tmp_path), model, None, None, None, mgr))
    assert model.invoke_prompts
    assert "codegen" in model.invoke_prompts[0]
    assert "生成代码的能力" in model.invoke_prompts[0]


def test_action_answer_node_injects_skill_text(tmp_path: Path) -> None:
    """动作回答系统提示包含启用 SKILL 描述。"""
    mgr = _enabled_skill_manager(tmp_path)
    model = CaptureModel()
    result = asyncio.run(action_answer_node(_state(tmp_path), model, None, None, mgr))
    assert model.stream_prompts
    assert "codegen" in model.stream_prompts[0]
    assert "生成代码的能力" in model.stream_prompts[0]
    assert result["answer"] == "这是回答。"


def test_knowledge_answer_node_injects_skill_text(tmp_path: Path) -> None:
    """知识回答系统提示包含启用 SKILL 描述。"""
    mgr = _enabled_skill_manager(tmp_path)
    model = CaptureModel()
    state = _state(
        tmp_path,
        contexts=[{"file_path": "a.md", "section": "sec", "text": "资料内容"}],
    )
    result = asyncio.run(knowledge_answer_node(state, model, None, None, mgr))
    assert model.stream_prompts
    assert "codegen" in model.stream_prompts[0]
    assert "生成代码的能力" in model.stream_prompts[0]
    assert result["answer"] == "这是回答。"


class SkillGraphModel(BaseChatModel):


    def bind_tools(self, tools, **kwargs):
        """忽略工具绑定（mock）。"""
        del tools, kwargs
        return self
    """图级注入测试模型：记录系统提示并按阶段返回。"""

    model_name: str = "skill-graph"
    temperature: float = 0.0
    system_prompts: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "skill-graph"

    def _content(self, messages: list[BaseMessage]) -> str:
        system = messages[0].content
        self.system_prompts.append(system)
        if "意图路由" in system:
            return json.dumps({"intent": "chat_action"})
        if "记忆提取" in system:
            return "[]"
        return "普通回答。"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=self._content(messages)))]
        )

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return self._generate(messages, stop=stop, **kwargs)

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        content = self._content(messages)
        if content != "普通回答。":
            parts = [content]
        else:
            parts = ["普通", "回答。"]
        for part in parts:
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=part))
            if run_manager is not None:
                run_manager.on_llm_new_token(part, chunk=chunk.message)
            yield chunk

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        for chunk in self._stream(messages, stop=stop, run_manager=run_manager):
            yield chunk


def test_graph_answer_prompt_includes_skill_description(tmp_path: Path) -> None:
    """总图完整链路：回答节点系统提示包含 SKILL 描述（skill_manager 透传）。"""
    mgr = _enabled_skill_manager(tmp_path)
    model = SkillGraphModel()
    graph = build_super_graph(
        rag_store=None,
        chat_model=model,
        memory_store=None,
        settings=AppSettings(model={"provider": "mock"}),
        skill_manager=mgr,
    )

    async def run() -> None:
        _graph_state = _state(tmp_path)
        config = {"configurable": {"thread_id": str(_graph_state.get("run_id") or _graph_state.get("thread_id") or "test")}}
        async for _mode, _payload in graph.astream(_graph_state, config=config, stream_mode=["values"]):
            pass

    asyncio.run(run())
    answer_prompts = [s for s in model.system_prompts if "当前启用的 SKILL 能力" in s]
    assert answer_prompts, "回答阶段系统提示应包含 SKILL 注入块"
    assert "codegen" in answer_prompts[-1]
    assert "生成代码的能力" in answer_prompts[-1]
    assert "用户要求写代码时" in answer_prompts[-1]


# ---------------------------------------------------------------- CLI 面板


def test_render_skills_panel_real_data() -> None:
    """CLI /skills 面板真实渲染启用/禁用/冲突状态。"""
    skills = [
        {
            "name": "codegen",
            "description": "生成代码",
            "when_to_use": "写代码时",
            "allowed_tools": ["Read", "Write"],
            "enabled": True,
            "scope": "project",
            "conflict": True,
        },
        {
            "name": "pilot",
            "description": "",
            "when_to_use": "",
            "allowed_tools": [],
            "enabled": False,
            "scope": "global",
            "conflict": False,
        },
    ]
    panel = render_skills_panel(skills)
    assert "── SKILL 技能 ──" in panel
    assert "codegen [已启用] [项目]（冲突，项目级优先）" in panel
    assert "生成代码" in panel
    assert "写代码时" in panel
    assert "Read, Write" in panel
    assert "pilot [已禁用] [全局]" in panel
    assert "（未配置 SKILL）" in render_skills_panel([])
