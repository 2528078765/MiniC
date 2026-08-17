"""S5 工具执行与审批管理。"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import re
import contextvars
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from minic.backups import write_file_backup
from minic.core.config import AppSettings
from minic.memory import LongTermMemoryStore, parse_topics
from minic.middleware import redact_pii


WRITE_TOOLS = {"Write", "Edit", "Format", "GitCommit", "GitBranch"}
READ_TOOLS = {"Read", "ReadMemory", "TextSearch", "Lint", "GitStatus", "GitDiff", "GitLog"}


@dataclass
class ToolResult:
    """工具执行结果。"""

    status: str
    output: str
    backup_id: str | None = None


class PermissionStore:
    """持久化 allow_always 与 deny 权限。"""

    def __init__(self, global_path: Path, project_path: Path) -> None:
        self.global_path = global_path
        self.project_path = project_path

    def _load(self, path: Path) -> list[dict[str, Any]]:
        """读取权限文件。"""
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8")).get("permissions", [])

    def _save(self, path: Path, permissions: list[dict[str, Any]]) -> None:
        """写入权限文件。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps({"permissions": permissions}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(path)

    def _entries(self, scope: str, workspace: Path) -> list[dict[str, Any]]:
        """返回指定作用域的权限条目。"""
        if scope == "global":
            return self._load(self.global_path)
        return self._load(workspace / ".minic" / "permissions.json")

    def _pattern_for(self, tool: str, args: dict[str, Any]) -> str:
        """根据工具参数生成权限匹配模式。"""
        if tool == "ReadMemory":
            return "memory"
        path = args.get("path")
        if path:
            return str(Path(path).expanduser().resolve())
        return "*"

    def check(self, tool: str, args: dict[str, Any], workspace: Path) -> str | None:
        """检查权限，返回 allow_always/deny 或 None。"""
        pattern = self._pattern_for(tool, args)
        global_entries = self._entries("global", workspace)
        project_entries = self._entries("project", workspace)
        global_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in global_entries:
            key = (entry["name"], entry.get("pattern", ""))
            if entry.get("mode") == "deny" or global_by_key.get(key, {}).get("mode") != "deny":
                global_by_key[key] = entry
        project_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in project_entries:
            key = (entry["name"], entry.get("pattern", ""))
            if entry.get("mode") == "deny" or project_by_key.get(key, {}).get("mode") != "deny":
                project_by_key[key] = entry
        by_key = global_by_key
        by_key.update(project_by_key)
        for entry in by_key.values():
            if entry.get("name") != tool or entry.get("mode") == "allow_once":
                continue
            if fnmatch.fnmatch(pattern, entry.get("pattern", "")) or entry.get("pattern") == "*":
                if entry.get("mode") == "deny":
                    return "deny"
                if entry.get("mode") == "allow_always":
                    return "allow_always"
        return None

    def grant(
        self,
        scope: str,
        workspace: Path,
        name: str,
        pattern: str,
        mode: str,
    ) -> dict[str, Any]:
        """新增持久化权限。"""
        if scope == "global":
            path = self.global_path
        else:
            path = workspace / ".minic" / "permissions.json"
        permissions = self._load(path)
        entry = {
            "id": str(uuid.uuid4()),
            "level": scope,
            "target": "tool",
            "name": name,
            "pattern": pattern,
            "mode": mode,
            "created_at": datetime.now().astimezone().isoformat(),
        }
        permissions.append(entry)
        self._save(path, permissions)
        return entry

    def revoke(self, scope: str, workspace: Path, permission_id: str) -> bool:
        """撤销权限。"""
        if scope == "global":
            path = self.global_path
        else:
            path = workspace / ".minic" / "permissions.json"
        permissions = self._load(path)
        remaining = [entry for entry in permissions if entry.get("id") != permission_id]
        if len(remaining) == len(permissions):
            return False
        self._save(path, remaining)
        return True


class ToolExecutor:
    """执行 S5 内置文件工具。"""

    def __init__(
        self,
        project_root: Path,
        backup_dir: Path,
        memory_store: LongTermMemoryStore | None = None,
        command_timeout: int = 60,
        allowed_write_dirs: list[str] | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.backup_dir = backup_dir
        self.memory_store = memory_store
        self.command_timeout = command_timeout
        self.allowed_write_dirs = [Path(item).expanduser().resolve() for item in (allowed_write_dirs or [])]
        self._project_root_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
            "tool_project_root", default=None
        )

    @property
    def _effective_root(self) -> Path:
        """当前请求的项目根：请求级覆盖 > 装配默认（contextvar 隔离并发请求）。"""
        override = self._project_root_ctx.get()
        if override:
            return Path(override).resolve()
        return self.project_root

    def _resolve_path(self, path: str) -> Path:
        """解析并返回绝对路径；相对路径基于当前请求的项目根（而非进程 cwd）。"""
        raw = Path(path).expanduser()
        if not raw.is_absolute():
            raw = self._effective_root / raw
        return raw.resolve()

    def _ensure_workspace(self, path: Path) -> None:
        """确保路径位于工作区内，或位于 allowed_write_dirs 白名单目录内（G13 沙箱写放行，仍走审批）。"""
        if path.is_relative_to(self._effective_root):
            return
        if any(path.is_relative_to(entry) for entry in self.allowed_write_dirs):
            return
        raise PermissionError(f"路径不在工作区内: {path}")

    def _backup(self, path: Path) -> str | None:
        """写操作前备份文件，并把原路径写入 manifest 供恢复使用。"""
        if not path.exists():
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        return write_file_backup(self.backup_dir, path)

    def _read(self, args: dict[str, Any]) -> ToolResult:
        """读取文件，支持行范围。"""
        path = self._resolve_path(str(args["path"]))
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()
        start = int(args.get("start_line", 1))
        end = int(args.get("end_line", len(lines)))
        selected = lines[max(start - 1, 0) : end]
        return ToolResult(status="success", output="\n".join(selected))

    def _read_memory(self, args: dict[str, Any]) -> ToolResult:
        """按主题读取长期记忆。"""
        if self.memory_store is None:
            return ToolResult(status="failed", output="长期记忆不可用")
        data = self.memory_store.read("merged", str(self._effective_root))
        topics = parse_topics(data["content"])
        topic = args.get("topic")
        if topic:
            return ToolResult(status="success", output=topics.get(topic, ""))
        return ToolResult(status="success", output=data["content"])

    def _write(self, args: dict[str, Any]) -> ToolResult:
        """写入文件。"""
        path = self._resolve_path(str(args["path"]))
        self._ensure_workspace(path)
        backup_id = self._backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(args.get("content", "")), encoding="utf-8")
        return ToolResult(status="success", output=f"已写入 {path}", backup_id=backup_id)

    def _edit(self, args: dict[str, Any]) -> ToolResult:
        """增量替换文件内容。"""
        path = self._resolve_path(str(args["path"]))
        self._ensure_workspace(path)
        content = path.read_text(encoding="utf-8")
        old_text = str(args.get("old_text", ""))
        new_text = str(args.get("new_text", ""))
        if old_text not in content:
            raise ValueError("old_text 未在文件中找到")
        backup_id = self._backup(path)
        path.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return ToolResult(status="success", output=f"已编辑 {path}", backup_id=backup_id)

    def _text_search(self, args: dict[str, Any]) -> ToolResult:
        """在文本文件中搜索正则表达式。"""
        root = self._resolve_path(str(args.get("path", self.project_root)))
        pattern = re.compile(str(args.get("pattern", "")))
        matches: list[str] = []
        for file_path in root.rglob("*"):
            if not file_path.is_file() or ".minic" in file_path.parts:
                continue
            try:
                text = file_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    matches.append(f"{file_path}:{line_number}:{line.strip()}")
                if len(matches) >= 50:
                    break
            if len(matches) >= 50:
                break
        return ToolResult(status="success", output="\n".join(matches))

    def _lint(self, args: dict[str, Any]) -> ToolResult:
        """对 Python 文件做语法检查。"""
        path = self._resolve_path(str(args["path"]))
        if path.suffix == ".py":
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
            return ToolResult(status="success", output="Lint 通过")
        return ToolResult(status="success", output="Lint 通过（非 Python 文件）")

    def _format(self, args: dict[str, Any]) -> ToolResult:
        """简单格式化文件。"""
        path = self._resolve_path(str(args["path"]))
        self._ensure_workspace(path)
        content = path.read_text(encoding="utf-8").replace("\t", "    ").rstrip() + "\n"
        backup_id = self._backup(path)
        path.write_text(content, encoding="utf-8")
        return ToolResult(status="success", output=f"已格式化 {path}", backup_id=backup_id)

    def _bash(self, args: dict[str, Any]) -> ToolResult:
        """执行终端命令，stdout 与 stderr 合并返回，输出做 PII 脱敏。"""
        command = str(args.get("command", "")).strip()
        if not command:
            return ToolResult(status="failed", output="命令不能为空")
        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
                cwd=self._effective_root,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(status="failed", output="命令超时")
        except OSError as exc:
            return ToolResult(status="failed", output=f"命令执行失败: {exc}")
        output = redact_pii((proc.stdout or "") + (proc.stderr or ""))
        if proc.returncode != 0:
            return ToolResult(status="failed", output=output or "命令执行失败")
        return ToolResult(status="success", output=output)

    def _git(self, *args: str) -> ToolResult:
        """运行只读 git 子命令。"""
        try:
            proc = subprocess.run(
                ["git", *args],
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
                cwd=self._effective_root,
            )
        except FileNotFoundError:
            return ToolResult(status="failed", output="git 未安装")
        except subprocess.TimeoutExpired:
            return ToolResult(status="failed", output="命令超时")
        except OSError as exc:
            return ToolResult(status="failed", output=f"git 执行失败: {exc}")
        output = redact_pii((proc.stdout or "") + (proc.stderr or ""))
        if proc.returncode != 0:
            return ToolResult(status="failed", output=output or "git 命令失败")
        return ToolResult(status="success", output=output)

    def _git_status(self, args: dict[str, Any]) -> ToolResult:
        """查看工作区状态。"""
        del args
        return self._git("status", "--short", "--branch")

    def _git_diff(self, args: dict[str, Any]) -> ToolResult:
        """查看工作区改动，可指定 path。"""
        command = ["diff"]
        path = args.get("path")
        if path:
            command.append(str(path))
        return self._git(*command)

    def _git_log(self, args: dict[str, Any]) -> ToolResult:
        """查看最近提交记录。"""
        try:
            count = int(args.get("n", 20))
        except (TypeError, ValueError):
            count = 20
        return self._git("log", "--oneline", "-n", str(count))

    def _git_commit(self, args: dict[str, Any]) -> ToolResult:
        """暂存全部改动并提交。"""
        message = str(args.get("message", "")).strip()
        if not message:
            raise ValueError("GitCommit 需要 message 参数")
        add_result = self._git("add", "-A")
        if add_result.status != "success":
            return add_result
        return self._git("commit", "-m", message)

    def _git_branch(self, args: dict[str, Any]) -> ToolResult:
        """新建分支。"""
        name = str(args.get("name", "")).strip()
        if not name:
            raise ValueError("GitBranch 需要 name 参数")
        return self._git("checkout", "-b", name)

    def execute(self, tool: str, args: dict[str, Any], project_root: str | None = None) -> ToolResult:
        """执行工具；``project_root`` 为请求级项目根（覆盖装配默认，并发隔离）。"""
        if project_root:  # 有请求级项目根时用 contextvar 隔离本次执行
            token = self._project_root_ctx.set(project_root)
            try:
                return self._dispatch(tool, args)
            finally:
                self._project_root_ctx.reset(token)
        return self._dispatch(tool, args)

    def _dispatch(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """按工具名分发。"""
        if tool == "Read":
            return self._read(args)
        if tool == "ReadMemory":
            return self._read_memory(args)
        if tool == "Write":
            return self._write(args)
        if tool == "Edit":
            return self._edit(args)
        if tool == "TextSearch":
            return self._text_search(args)
        if tool == "Lint":
            return self._lint(args)
        if tool == "Format":
            return self._format(args)
        if tool == "Bash":
            return self._bash(args)
        if tool == "GitStatus":
            return self._git_status(args)
        if tool == "GitDiff":
            return self._git_diff(args)
        if tool == "GitLog":
            return self._git_log(args)
        if tool == "GitCommit":
            return self._git_commit(args)
        if tool == "GitBranch":
            return self._git_branch(args)
        raise ValueError(f"不支持的工具: {tool}")


@dataclass
class ApprovalRequest:
    """待审批请求。"""

    id: str
    tool_call_id: str
    thread_id: str
    tool: str
    args: dict[str, Any]
    options: list[str]
    status: str = "pending"
    decision: str | None = None
    subagent_id: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())


class ApprovalManager:
    """管理审批请求与会话级放行。"""

    def __init__(
        self,
        permission_store: PermissionStore,
        workspace: Path,
        settings: AppSettings,
        mcp_manager: Any | None = None,
    ) -> None:
        self.permission_store = permission_store
        self.workspace = workspace.resolve()
        self.settings = settings
        self.mcp_manager = mcp_manager  # MCP 管理器（用于 autoApprove 判定）
        self.pending: dict[str, ApprovalRequest] = {}
        self.events: dict[str, asyncio.Event] = {}
        self.session_allows: dict[str, set[str]] = {}

    def _pattern(self, tool: str, args: dict[str, Any]) -> str:
        """返回工具对应的权限模式。"""
        return self.permission_store._pattern_for(tool, args)

    def _mcp_auto_approved(self, tool: str) -> bool:
        """判断 MCP 工具（server.tool 格式）是否在 autoApprove 列表内。"""
        if self.mcp_manager is None or "." not in tool:
            return False
        server_name, tool_name = tool.split(".", 1)
        return self.mcp_manager.is_auto_approved(server_name, tool_name)

    def _pattern(self, tool: str, args: dict[str, Any]) -> str:
        """返回工具对应的权限模式。"""
        return self.permission_store._pattern_for(tool, args)

    def _needs_approval(self, tool: str, args: dict[str, Any]) -> bool:
        """判断工具是否需要审批。"""
        if tool == "Bash":
            return True  # full 权限工具必须人工确认
        if tool == "IngestDirectory":
            return True  # 入库产生 embedding 费用，必须人工确认
        if tool in WRITE_TOOLS:
            return not self.settings.approval.workspace_write_auto_approve
        if tool in READ_TOOLS:
            if tool == "ReadMemory":
                return False
            path = self._resolve_path(args.get("path", self.workspace))
            inside = path.is_relative_to(self.workspace)
            return not (inside and self.settings.approval.workspace_read_auto_approve)
        return True

    def _resolve_path(self, value: Any) -> Path:
        """解析路径。"""
        return Path(str(value)).expanduser().resolve()

    def plan(self, tool: str, args: dict[str, Any], thread_id: str) -> str:
        """规划工具执行方式，返回 auto/approval/allow_always/allow_session/deny。"""
        if tool == "Bash":
            # full 权限：跳过 allow_always/allow_session，恒返回 approval，除非已有 deny 记录
            if self.permission_store.check(tool, args, self.workspace) == "deny":
                return "deny"
            return "approval"
        permission = self.permission_store.check(tool, args, self.workspace)
        if permission == "deny":
            return "deny"
        if permission == "allow_always":
            return "allow_always"
        pattern = self._pattern(tool, args)
        if thread_id in self.session_allows and any(
            fnmatch.fnmatch(pattern, allowed) for allowed in self.session_allows[thread_id]
        ):
            return "allow_session"
        if self._mcp_auto_approved(tool):
            return "auto"
        if self._needs_approval(tool, args):
            return "approval"
        return "auto"

    def request(
        self,
        thread_id: str,
        tool: str,
        args: dict[str, Any],
        subagent_id: str | None = None,
    ) -> ApprovalRequest:
        """创建待审批请求。"""
        if tool == "Bash":
            options = ["allow_once", "deny"]  # full 权限：只有一次/拒绝
        elif tool == "IngestDirectory":
            options = ["allow_once", "allow_always", "deny"]  # 入库有 embedding 成本：不带 allow_session
        else:
            options = ["allow_once", "allow_session", "allow_always", "deny"]
        approval = ApprovalRequest(
            id=str(uuid.uuid4()),
            tool_call_id=str(uuid.uuid4()),
            thread_id=thread_id,
            tool=tool,
            args=args,
            options=options,
            subagent_id=subagent_id,
        )
        self.pending[approval.id] = approval
        self.events[approval.id] = asyncio.Event()
        return approval

    async def wait(self, approval_id: str, timeout: int | None = None) -> None:
        """等待审批结果。"""
        event = self.events.get(approval_id)
        if event is None:
            raise KeyError(f"审批不存在: {approval_id}")
        await asyncio.wait_for(event.wait(), timeout=timeout or self.settings.approval.expiry_seconds)

    def submit(self, thread_id: str, approval_id: str, decision: str) -> ApprovalRequest:
        """提交审批结果。"""
        approval = self.pending.get(approval_id)
        if approval is None or approval.thread_id != thread_id:
            raise KeyError(f"审批不存在: {approval_id}")
        if approval.status != "pending":
            raise ValueError(f"审批已处理: {approval_id}")
        if decision not in {"allow_once", "allow_session", "allow_always", "deny"}:
            raise ValueError(f"不支持的审批决定: {decision}")
        if approval.tool == "Bash" and decision in {"allow_session", "allow_always"}:
            raise ValueError("Bash 只支持 allow_once 或 deny")
        if approval.tool == "IngestDirectory" and decision == "allow_session":
            raise ValueError("IngestDirectory 只支持 allow_once、allow_always 或 deny")
        approval.status = "approved" if decision != "deny" else "denied"
        approval.decision = decision
        if decision == "allow_always":
            self.permission_store.grant(
                "project",
                self.workspace,
                approval.tool,
                self._pattern(approval.tool, approval.args),
                "allow_always",
            )
        if decision == "allow_session":
            self.session_allows.setdefault(thread_id, set()).add(
                self._pattern(approval.tool, approval.args)
            )
        self.events[approval_id].set()
        return approval
