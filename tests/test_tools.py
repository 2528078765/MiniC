"""S5 工具与审批测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from minic.core.config import AppSettings
from minic.memory import LongTermMemoryStore
from minic.tools.service import ApprovalManager, PermissionStore, ToolExecutor


@pytest.fixture
def tools_env(settings: AppSettings, tmp_path: Path):
    """构造隔离的工具与审批环境。"""
    memory_store = LongTermMemoryStore(tmp_path / "global-memory", tmp_path)
    permission_store = PermissionStore(
        tmp_path / "global-permissions.json",
        tmp_path / ".minic" / "permissions.json",
    )
    backup_dir = tmp_path / ".minic" / "backups" / "files"
    executor = ToolExecutor(tmp_path, backup_dir, memory_store)
    approval_manager = ApprovalManager(permission_store, tmp_path, settings)
    return {
        "executor": executor,
        "approval_manager": approval_manager,
        "permission_store": permission_store,
        "root": tmp_path,
        "backup_dir": backup_dir,
    }


def test_write_outside_workspace_rejected(tools_env: dict) -> None:
    """Write 不能写工作区外路径。"""
    executor = tools_env["executor"]
    outside = tools_env["root"].parent / "outside.txt"
    with pytest.raises(PermissionError):
        executor.execute("Write", {"path": str(outside), "content": "x"})


def test_write_requires_approval_and_backup(tools_env: dict) -> None:
    """Write 需要审批，allow_always 后执行并生成备份。"""
    root = tools_env["root"]
    approval_manager = tools_env["approval_manager"]
    executor = tools_env["executor"]
    target = root / "a.txt"
    target.write_text("old", encoding="utf-8")
    args = {"path": str(target), "content": "new"}

    assert approval_manager.plan("Write", args, "thread-1") == "approval"
    approval = approval_manager.request("thread-1", "Write", args)
    approval_manager.submit("thread-1", approval.id, "allow_always")
    asyncio.run(approval_manager.wait(approval.id))
    assert approval_manager.plan("Write", args, "thread-1") == "allow_always"

    result = executor.execute("Write", args)
    assert result.status == "success"
    assert target.read_text(encoding="utf-8") == "new"
    assert result.backup_id
    assert (tools_env["backup_dir"] / f"{result.backup_id}.bak").exists()


def test_deny_decision_prevents_execution(tools_env: dict) -> None:
    """deny 后工具不执行。"""
    approval_manager = tools_env["approval_manager"]
    root = tools_env["root"]
    args = {"path": str(root / "a.txt"), "content": "x"}
    assert approval_manager.plan("Write", args, "thread-2") == "approval"
    approval = approval_manager.request("thread-2", "Write", args)
    approval_manager.submit("thread-2", approval.id, "deny")
    asyncio.run(approval_manager.wait(approval.id))
    assert approval.status == "denied"
    assert approval.decision == "deny"


def test_format_requires_write_approval(tools_env: dict) -> None:
    """Format 归入写权限，需要审批。"""
    approval_manager = tools_env["approval_manager"]
    root = tools_env["root"]
    target = root / "a.py"
    target.write_text("x=1\n", encoding="utf-8")
    assert approval_manager.plan("Format", {"path": str(target)}, "thread-3") == "approval"


def test_deny_permission_has_priority(tools_env: dict) -> None:
    """deny 权限优先于 allow_always。"""
    approval_manager = tools_env["approval_manager"]
    permission_store = tools_env["permission_store"]
    root = tools_env["root"]
    path = str(root / "secret.txt")
    permission_store.grant("project", root, "Write", path, "deny")
    permission_store.grant("project", root, "Write", path, "allow_always")
    assert approval_manager.plan("Write", {"path": path, "content": "x"}, "thread-4") == "deny"
