"""会话短期记忆，落盘到各工作区 .minic/memory/short_memory（workspace 分目录）。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage


def summarize_messages(messages: list[dict[str, Any]], max_chars: int = 400) -> str:
    """把会话消息压缩成简短摘要（规则截断，LLM 摘要的降级路径）。"""
    if not messages:
        return "（空会话）"
    text = "\n".join(f"{message.get('role')}: {message.get('content')}" for message in messages).strip()
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}..."


def _llm_summarize(chat_model: Any, messages: list[dict[str, Any]], max_chars: int = 400) -> str:
    """用 LLM 把消息列表压缩成要点摘要；失败时降级为规则截断。"""
    text = "\n".join(f"{message.get('role')}: {message.get('content')}" for message in messages).strip()
    try:
        response = chat_model.invoke(
            [
                SystemMessage(
                    content=(
                        f"你是会话摘要器。把下面的对话压缩成不超过 {max_chars} 字的要点摘要："
                        "保留用户的问题、关键结论与未完成事项，去掉寒暄与重复。只输出摘要文本。"
                    )
                ),
                HumanMessage(content=text),
            ]
        )
        summary = str(getattr(response, "content", "") or "").strip()
        if summary:
            return summary[:max_chars]
    except Exception:  # noqa: BLE001 - LLM 摘要失败时降级
        pass
    return summarize_messages(messages, max_chars)


class ShortMemoryStore:
    """按 thread_id 保存会话消息；会话文件按工作区分目录存放。

    - 有 workspace：``<workspace>/.minic/memory/short_memory/{thread_id}.json``
    - 无 workspace：``root_dir/{thread_id}.json``（旧数据兼容）
    ``root_dir`` 下维护 ``_workspace_dirs.json`` 索引，供无 workspace 的
    load/list 全局查找。
    """

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self.root_dir / "_workspace_dirs.json"
        self._workspace_dirs: set[str] = self._load_index()

    # ---- 索引 ----

    def _load_index(self) -> set[str]:
        """读取已注册工作区目录索引。"""
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {str(item) for item in data}
        except (OSError, json.JSONDecodeError):
            pass
        return set()

    def _save_index(self) -> None:
        """把工作区目录索引原子写盘。"""
        try:
            tmp = self._index_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(sorted(self._workspace_dirs), ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(self._index_path)
        except OSError:
            pass

    def _register(self, workspace: str) -> Path:
        """注册工作区短期记忆目录并返回。"""
        directory = self._dir_for(workspace)
        directory.mkdir(parents=True, exist_ok=True)
        key = str(directory)
        if key not in self._workspace_dirs:
            self._workspace_dirs.add(key)
            self._save_index()
        return directory

    # ---- 路径 ----

    def _dir_for(self, workspace: str | None = None) -> Path:
        """工作区短期记忆目录：有 workspace 用其项目目录，否则用默认根。"""
        if workspace:
            return Path(workspace).resolve() / ".minic" / "memory" / "short_memory"
        return self.root_dir

    def _thread_path(self, thread_id: str, workspace: str | None = None) -> Path:
        """返回会话文件路径。"""
        return self._dir_for(workspace) / f"{thread_id}.json"

    def _locate(self, thread_id: str, workspace: str | None = None) -> Path | None:
        """定位会话文件：优先 workspace 目录，其次默认根，最后索引目录。"""
        candidates: list[Path] = []
        if workspace:
            candidates.append(self._thread_path(thread_id, workspace))
        candidates.append(self._thread_path(thread_id))
        for directory in sorted(self._workspace_dirs):
            path = Path(directory) / f"{thread_id}.json"
            if path not in candidates:
                candidates.append(path)
        for path in candidates:
            if path.exists():
                return path
        return None

    def _write(self, thread: dict[str, Any], path: Path) -> None:
        """把会话记录原子写入磁盘。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(thread, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    # ---- 会话操作 ----

    def create(self, thread_id: str, workspace: str) -> dict[str, Any]:
        """创建新的会话记录（落在 workspace 的短期记忆目录）。"""
        now = datetime.now().astimezone().isoformat()
        thread = {
            "thread_id": thread_id,
            "name": "新会话",
            "description": "",
            "workspace": str(Path(workspace).resolve()),
            "created_at": now,
            "updated_at": now,
            "archived": False,
            "messages": [],
        }
        directory = self._register(workspace)
        self._write(thread, directory / f"{thread_id}.json")
        return thread

    def load(self, thread_id: str, workspace: str | None = None) -> dict[str, Any] | None:
        """读取会话记录，不存在时返回 None。"""
        path = self._locate(thread_id, workspace)
        if path is None:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def list(
        self,
        workspace: str | None = None,
        archived: bool | None = None,
    ) -> list[dict[str, Any]]:
        """列出会话记录，可按工作区和归档状态过滤。"""
        directories: list[Path] = []
        if workspace:
            directories.append(self._dir_for(workspace))
        directories.append(self.root_dir)
        if workspace is None:
            directories.extend(Path(item) for item in sorted(self._workspace_dirs))
        threads: list[dict[str, Any]] = []
        seen: set[str] = set()
        for directory in directories:
            for path in directory.glob("*.json"):
                if path.name == "_workspace_dirs.json":
                    continue
                try:
                    thread = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                thread_id = str(thread.get("thread_id", ""))
                if thread_id in seen:
                    continue
                seen.add(thread_id)
                if workspace and str(Path(thread.get("workspace", "")).resolve()) != str(Path(workspace).resolve()):
                    continue
                if archived is not None and bool(thread.get("archived", False)) != archived:
                    continue
                threads.append(thread)
        threads.sort(key=lambda thread: thread.get("updated_at", ""), reverse=True)
        return threads

    def update_meta(self, thread_id: str, workspace: str | None = None, **fields: Any) -> dict[str, Any]:
        """更新会话元数据并落盘。"""
        thread = self.load(thread_id, workspace)
        if thread is None:
            raise FileNotFoundError(f"会话不存在: {thread_id}")
        thread.update(fields)
        thread["updated_at"] = datetime.now().astimezone().isoformat()
        self._write(thread, self._locate(thread_id, workspace))  # type: ignore[arg-type]
        return thread

    def set_archived(self, thread_id: str, archived: bool, workspace: str | None = None) -> dict[str, Any]:
        """设置会话归档状态。"""
        return self.update_meta(thread_id, workspace, archived=archived)

    def backup(self, thread_id: str, backup_dir: Path, workspace: str | None = None) -> str:
        """备份会话文件并返回备份 ID。"""
        thread = self.load(thread_id, workspace)
        if thread is None:
            raise FileNotFoundError(f"会话不存在: {thread_id}")
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_id = f"session-{thread_id}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        data = dict(thread)
        data["backup_id"] = backup_id
        data["backed_up_at"] = datetime.now().astimezone().isoformat()
        (backup_dir / f"{backup_id}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return backup_id

    def compress(
        self,
        thread_id: str,
        backup_dir: Path,
        workspace: str | None = None,
        chat_model: Any = None,
        keep_recent: int = 5,
    ) -> tuple[str, dict[str, Any]]:
        """备份会话后压缩消息列表。

        有模型且消息数超过 ``keep_recent`` 时：更早的消息压成 LLM 摘要，
        保留最近 ``keep_recent`` 条原文；否则全部压成规则摘要（降级路径）。
        """
        backup_id = self.backup(thread_id, backup_dir, workspace)
        thread = self.load(thread_id, workspace)
        if thread is None:
            raise FileNotFoundError(f"会话不存在: {thread_id}")
        messages = thread.get("messages", [])
        now = datetime.now().astimezone().isoformat()
        if chat_model is not None and len(messages) > keep_recent:
            summary = _llm_summarize(chat_model, messages[:-keep_recent])
            thread["messages"] = [
                {
                    "message_id": str(uuid.uuid4()),
                    "role": "system",
                    "content": f"会话摘要：{summary}",
                    "timestamp": now,
                },
                *messages[-keep_recent:],
            ]
        else:
            summary = summarize_messages(messages)
            thread["messages"] = [
                {
                    "message_id": str(uuid.uuid4()),
                    "role": "system",
                    "content": f"会话摘要：{summary}",
                    "timestamp": now,
                }
            ]
        thread["description"] = summary
        thread["summary"] = summary
        thread["updated_at"] = now
        self._write(thread, self._locate(thread_id, workspace))  # type: ignore[arg-type]
        return backup_id, thread

    def delete(self, thread_id: str, workspace: str | None = None) -> None:
        """删除会话文件。"""
        path = self._locate(thread_id, workspace)
        if path is not None:
            path.unlink(missing_ok=True)

    def restore(self, thread: dict[str, Any]) -> bool:
        """把备份的会话记录恢复写回短期记忆（压缩回滚），返回是否成功。"""
        thread_id = thread.get("thread_id")
        if not thread_id:
            return False
        data = dict(thread)
        data.pop("backup_id", None)
        data.pop("backed_up_at", None)
        workspace = data.get("workspace")
        target = self._dir_for(workspace) if workspace else self.root_dir
        self._register(workspace) if workspace else target.mkdir(parents=True, exist_ok=True)
        self._write(data, target / f"{thread_id}.json")
        return True

    def append_message(
        self,
        thread_id: str,
        role: str,
        content: str,
        workspace: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        """向会话追加一条消息并落盘。

        ReAct 会话消息结构：``role=ai`` 可带 ``tool_calls`` 字段
        （模型工具调用），``role=tool`` 带 ``tool_call_id`` 字段（工具结果）。
        """
        thread = self.load(thread_id, workspace)
        if thread is None:
            raise FileNotFoundError(f"会话不存在: {thread_id}")
        entry: dict[str, Any] = {
            "message_id": str(uuid.uuid4()),
            "role": role,
            "content": content,
            "timestamp": datetime.now().astimezone().isoformat(),
        }
        if tool_calls:
            entry["tool_calls"] = tool_calls
        if tool_call_id:
            entry["tool_call_id"] = tool_call_id
        thread.setdefault("messages", []).append(entry)
        thread["updated_at"] = datetime.now().astimezone().isoformat()
        self._write(thread, self._locate(thread_id, workspace))  # type: ignore[arg-type]
        return thread
