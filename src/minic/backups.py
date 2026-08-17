"""备份清单扫描与恢复（G9 接口补齐）。

目录约定：
- ``<project>/.minic/backups/sessions/``：会话 JSON 备份（``ShortMemoryStore.backup`` 写入）。
- ``<project>/.minic/backups/files/``：文件 .bak 备份（``ToolExecutor._backup`` 写入），
  文件名 ``file-{name}-{timestamp}.bak`` 不含原路径，通过 ``manifest.jsonl`` 记录原路径。
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from minic.chat.memory import ShortMemoryStore

_TIMESTAMP_RE = re.compile(r"-(\d{14,20})$")


def write_file_backup(backup_dir: Path, source_path: Path, backup_id: str | None = None) -> str:
    """复制文件到 backups/files 并写入 manifest 条目，返回 backup_id。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_id = backup_id or (
        f"file-{source_path.name}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    )
    shutil.copy2(source_path, backup_dir / f"{backup_id}.bak")
    entry = {
        "backup_id": backup_id,
        "original_path": str(source_path.expanduser().resolve()),
        "created_at": datetime.now().astimezone().isoformat(),
    }
    manifest_path = backup_dir / "manifest.jsonl"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return backup_id


def load_manifest(files_dir: Path) -> dict[str, str]:
    """读取 manifest，返回 ``backup_id -> original_path`` 映射。"""
    manifest_path = files_dir / "manifest.jsonl"
    mapping: dict[str, str] = {}
    if not manifest_path.exists():
        return mapping
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("backup_id") and entry.get("original_path"):
            mapping[entry["backup_id"]] = entry["original_path"]
    return mapping


def _original_name_from_backup(stem: str) -> str:
    """从 ``file-{name}-{timestamp}`` 中还原原文件名。"""
    name = stem[len("file-") :] if stem.startswith("file-") else stem
    match = _TIMESTAMP_RE.search(name)
    if match:
        name = name[: match.start()]
    return name


class BackupManager:
    """扫描 backups 目录生成备份清单，并支持恢复。"""

    def __init__(
        self,
        backups_dir: Path,
        short_memory: ShortMemoryStore,
        workspace: Path,
    ) -> None:
        self.backups_dir = backups_dir
        self.sessions_dir = backups_dir / "sessions"
        self.files_dir = backups_dir / "files"
        self.short_memory = short_memory
        self.workspace = workspace.resolve()

    def _created_at(self, path: Path) -> str:
        """从文件名时间戳或 mtime 解析创建时间。"""
        match = _TIMESTAMP_RE.search(path.stem)
        if match:
            try:
                parsed = datetime.strptime(match.group(1)[:14], "%Y%m%d%H%M%S")
                return parsed.astimezone().isoformat()
            except ValueError:
                pass
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat()

    def _entry(self, backup_id: str, backup_type: str, path: Path) -> dict[str, Any]:
        """构造备份清单条目。"""
        return {
            "id": backup_id,
            "scope": "project",
            "type": backup_type,
            "created_at": self._created_at(path),
            "path": str(path.relative_to(self.backups_dir)).replace("\\", "/"),
        }

    def list_backups(self) -> list[dict[str, Any]]:
        """返回可恢复备份清单，按创建时间倒序。"""
        entries: list[dict[str, Any]] = []
        if self.sessions_dir.is_dir():
            for path in self.sessions_dir.glob("*.json"):
                entries.append(self._entry(path.stem, "session", path))
        if self.files_dir.is_dir():
            for path in self.files_dir.glob("*.bak"):
                entries.append(self._entry(path.stem, "file", path))
        entries.sort(key=lambda entry: entry["created_at"], reverse=True)
        return entries

    def restore(self, backup_id: str) -> bool:
        """恢复备份，成功返回 True；id 不存在或路径非法返回 False。"""
        session_path = self.sessions_dir / f"{backup_id}.json"
        if session_path.exists():
            try:
                data = json.loads(session_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return False
            return self.short_memory.restore(data)
        file_path = self.files_dir / f"{backup_id}.bak"
        if file_path.exists():
            return self._restore_file(file_path)
        return False

    def _restore_file(self, file_path: Path) -> bool:
        """把 .bak 内容恢复到原路径；无 manifest 时按文件名匹配工作区内同名文件。"""
        original = load_manifest(self.files_dir).get(file_path.stem)
        if original:
            target = Path(original).expanduser().resolve()
            if not target.is_relative_to(self.workspace):
                return False
        else:
            name = _original_name_from_backup(file_path.stem)
            if not name:
                return False
            matches = sorted(
                path
                for path in self.workspace.rglob(name)
                if path.is_file() and ".minic" not in path.parts
            )
            if not matches:
                return False
            target = matches[0]
        # 先备份当前文件再覆盖，保持可回滚
        if target.exists():
            write_file_backup(self.files_dir, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, target)
        return True
