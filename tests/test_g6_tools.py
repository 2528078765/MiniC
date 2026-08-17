"""G6 Bash + Git 工具测试。"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from minic.core.config import AppSettings
from minic.graph import get_tool, list_tools
from minic.memory import LongTermMemoryStore
from minic.recovery import ToolExecutionLog
from minic.tools.runtime import ToolRuntime
from minic.tools.service import ApprovalManager, PermissionStore, ToolExecutor


@pytest.fixture
def g6_env(tmp_path: Path) -> dict:
    """构造隔离的工具与审批环境。"""
    settings = AppSettings(model={"provider": "mock"}, embedding={"provider": "mock", "dimension": 64})
    memory_store = LongTermMemoryStore(tmp_path / "global-memory", tmp_path)
    permission_store = PermissionStore(
        tmp_path / "global-permissions.json",
        tmp_path / ".minic" / "permissions.json",
    )
    backup_dir = tmp_path / ".minic" / "backups" / "files"
    executor = ToolExecutor(tmp_path, backup_dir, memory_store)
    approval_manager = ApprovalManager(permission_store, tmp_path, settings)
    tool_log = ToolExecutionLog(tmp_path / ".minic" / "logs" / "tool_execution.jsonl")
    return {
        "executor": executor,
        "approval_manager": approval_manager,
        "permission_store": permission_store,
        "root": tmp_path,
        "tool_log": tool_log,
    }


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """临时 git 仓库（含一次初始提交），仓库本地配置 user 身份避免依赖全局配置。"""
    init = subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, capture_output=True, text=True)
    assert init.returncode == 0, init.stderr
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, capture_output=True, text=True, check=True)
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, text=True, check=True)
    commit = subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert commit.returncode == 0, commit.stderr
    return tmp_path


async def _run_bash_with_decision(
    approval_manager: ApprovalManager,
    executor: ToolExecutor,
    tool_log: ToolExecutionLog,
    command: str,
    decision: str,
    thread_id: str = "bash-t1",
):
    """并发消费 Bash 审批事件并按给定决策提交，返回执行结果与事件序列。"""
    runtime = ToolRuntime(approval_manager, executor, tool_log)
    queue: asyncio.Queue = asyncio.Queue()
    runtime.attach(thread_id, lambda name, data: queue.put_nowait((name, data)))
    events: list[tuple[str, dict]] = []

    async def execute() -> dict:
        """在后台执行一次 Bash 工具调用。"""
        return await runtime.execute(
            thread_id=thread_id,
            run_id="run-1",
            tool="Bash",
            args={"command": command},
        )

    task = asyncio.create_task(execute())
    while not task.done():
        try:
            name, data = await asyncio.wait_for(queue.get(), timeout=5)
        except asyncio.TimeoutError:
            break
        events.append((name, data))
        if name == "approval_requested":
            approval_manager.submit(thread_id, data["id"], decision)
    await task
    runtime.detach(thread_id)
    return task.result(), events


def test_bash_executes_successfully(g6_env: dict) -> None:
    """Bash 执行成功并返回输出。"""
    result = g6_env["executor"].execute("Bash", {"command": "echo hi"})
    assert result.status == "success"
    assert "hi" in result.output


def test_bash_approval_options_only_once_and_deny(g6_env: dict) -> None:
    """Bash 恒需审批，options 只含 allow_once/deny。"""
    approval_manager = g6_env["approval_manager"]
    approval = approval_manager.request("t", "Bash", {"command": "echo hi"})
    assert approval.options == ["allow_once", "deny"]
    assert approval_manager.plan("Bash", {"command": "echo hi"}, "t") == "approval"


def test_bash_submit_allow_always_and_session_raises(g6_env: dict) -> None:
    """对 Bash 提交 allow_session/allow_always 抛 ValueError。"""
    approval_manager = g6_env["approval_manager"]
    approval = approval_manager.request("t", "Bash", {"command": "echo hi"})
    with pytest.raises(ValueError):
        approval_manager.submit("t", approval.id, "allow_always")
    approval = approval_manager.request("t", "Bash", {"command": "echo hi"})
    with pytest.raises(ValueError):
        approval_manager.submit("t", approval.id, "allow_session")


def test_bash_plan_skips_allow_always_history(g6_env: dict) -> None:
    """Bash 即使有历史 allow_always 记录也仍返回 approval；deny 记录仍生效。"""
    permission_store = g6_env["permission_store"]
    approval_manager = g6_env["approval_manager"]
    root = g6_env["root"]
    permission_store.grant("project", root, "Bash", "*", "allow_always")
    assert approval_manager.plan("Bash", {"command": "echo hi"}, "t") == "approval"
    permission_store.grant("project", root, "Bash", "*", "deny")
    assert approval_manager.plan("Bash", {"command": "echo hi"}, "t") == "deny"


def test_bash_allow_once_executes(g6_env: dict) -> None:
    """Bash 审批 allow_once 后执行，approval_requested 事件带正确 options。"""
    marker = g6_env["root"] / "bash-marker.txt"
    command = f'echo marker > "{marker}"'
    result, events = asyncio.run(
        _run_bash_with_decision(
            g6_env["approval_manager"],
            g6_env["executor"],
            g6_env["tool_log"],
            command,
            "allow_once",
        )
    )
    assert result["status"] == "completed"
    assert marker.exists()
    approval_events = [data for name, data in events if name == "approval_requested"]
    assert approval_events
    assert approval_events[0]["options"] == ["allow_once", "deny"]


def test_bash_deny_not_executed(g6_env: dict) -> None:
    """Bash 审批 deny 后不执行命令。"""
    marker = g6_env["root"] / "bash-denied-marker.txt"
    command = f'echo marker > "{marker}"'
    result, _ = asyncio.run(
        _run_bash_with_decision(
            g6_env["approval_manager"],
            g6_env["executor"],
            g6_env["tool_log"],
            command,
            "deny",
        )
    )
    assert result["status"] == "denied"
    assert not marker.exists()


def test_bash_timeout_returns_failed(tmp_path: Path) -> None:
    """Bash 超过 command_timeout 返回 failed 且输出提示超时。"""
    executor = ToolExecutor(tmp_path, tmp_path / ".minic" / "backups" / "files", command_timeout=1)
    command = f'"{sys.executable}" -c "import time; time.sleep(5)"'
    result = executor.execute("Bash", {"command": command})
    assert result.status == "failed"
    assert "命令超时" in result.output


def test_bash_output_redacts_pii(g6_env: dict) -> None:
    """Bash 输出中的 API Key 与手机号被脱敏。"""
    result = g6_env["executor"].execute("Bash", {"command": "echo sk-abcdefghijklmnop 13812345678"})
    assert result.status == "success"
    assert "sk-abcdefghijklmnop" not in result.output
    assert "13812345678" not in result.output
    assert "[REDACTED_API_KEY]" in result.output
    assert "[REDACTED_PHONE]" in result.output


def test_git_read_tools_auto_approved(git_repo: Path, g6_env: dict) -> None:
    """GitStatus/GitDiff/GitLog 工作区内只读自动放行并可执行。"""
    approval_manager = g6_env["approval_manager"]
    executor = g6_env["executor"]
    assert approval_manager.plan("GitStatus", {}, "t") == "auto"
    assert approval_manager.plan("GitDiff", {}, "t") == "auto"
    assert approval_manager.plan("GitLog", {}, "t") == "auto"
    status = executor.execute("GitStatus", {})
    assert status.status == "success"
    log = executor.execute("GitLog", {"n": 5})
    assert log.status == "success"
    assert "init" in log.output


def test_git_read_tools_fail_outside_repo(g6_env: dict) -> None:
    """工作区不是 git 仓库时只读工具返回 failed 提示。"""
    result = g6_env["executor"].execute("GitStatus", {})
    assert result.status == "failed"
    assert "not a git repository" in result.output.lower()


def test_git_commit_requires_approval_and_commits(git_repo: Path, g6_env: dict) -> None:
    """GitCommit 需审批，allow_once 后提交成功且 git log 可见。"""
    (git_repo / "a.txt").write_text("changed", encoding="utf-8")
    args = {"message": "feat: change"}
    approval_manager = g6_env["approval_manager"]
    executor = g6_env["executor"]
    assert approval_manager.plan("GitCommit", args, "t") == "approval"
    approval = approval_manager.request("t", "GitCommit", args)
    approval_manager.submit("t", approval.id, "allow_once")
    asyncio.run(approval_manager.wait(approval.id))
    result = executor.execute("GitCommit", args)
    assert result.status == "success"
    log = executor.execute("GitLog", {"n": 5})
    assert log.status == "success"
    assert "feat: change" in log.output


def test_git_commit_missing_message_raises(git_repo: Path, g6_env: dict) -> None:
    """GitCommit 缺少 message 参数时报错。"""
    with pytest.raises(ValueError):
        g6_env["executor"].execute("GitCommit", {})


def test_git_branch_requires_approval_and_creates(git_repo: Path, g6_env: dict) -> None:
    """GitBranch 需审批，allow_once 后创建分支且当前分支切换。"""
    args = {"name": "feature"}
    approval_manager = g6_env["approval_manager"]
    executor = g6_env["executor"]
    assert approval_manager.plan("GitBranch", args, "t") == "approval"
    approval = approval_manager.request("t", "GitBranch", args)
    approval_manager.submit("t", approval.id, "allow_once")
    asyncio.run(approval_manager.wait(approval.id))
    result = executor.execute("GitBranch", args)
    assert result.status == "success"
    status = executor.execute("GitStatus", {})
    assert status.status == "success"
    assert "feature" in status.output


def test_registry_contains_new_tools() -> None:
    """工具注册表能取到 Bash 与五个 Git 工具。"""
    names = {tool.name for tool in list_tools()}
    for name in ("Bash", "GitStatus", "GitDiff", "GitLog", "GitCommit", "GitBranch"):
        assert name in names
        assert get_tool(name) is not None
    assert get_tool("Bash").category == "exec"
    assert get_tool("Bash").description == "执行终端命令，需要人工审批"
    assert get_tool("GitStatus").category == "read"
    assert get_tool("GitCommit").category == "write"


def test_bash_allow_always_rejected_by_api(client) -> None:
    """HTTP 层对 Bash 提交 allow_always 返回 400 VALIDATION_ERROR。"""
    headers = {"Authorization": "Bearer test-token"}
    thread_id = "bash-http-1"
    client.app.state.short_memory.create(thread_id, str(Path.cwd()))
    approval = client.app.state.approval_manager.request(thread_id, "Bash", {"command": "echo hi"})
    response = client.post(
        f"/threads/{thread_id}/approve",
        headers=headers,
        json={"approval_id": approval.id, "decision": "allow_always"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
