"""长期记忆存储：全局与项目 Markdown 记忆。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class MemoryConflictError(Exception):
    """记忆版本冲突异常。"""

    def __init__(self, content: str, version: str) -> None:
        self.content = content
        self.version = version
        super().__init__("长期记忆版本冲突")


def _sha256(content: str) -> str:
    """返回内容的 sha256。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def parse_topics(content: str) -> dict[str, str]:
    """把 Markdown 记忆解析成主题字典。"""
    topics: dict[str, list[str]] = {}
    current_topic: str | None = None
    for line in content.splitlines():
        if line.startswith("#"):
            current_topic = line.lstrip("#").strip()
            topics.setdefault(current_topic, [])
        elif current_topic is not None:
            topics[current_topic].append(line)
    if not topics and content.strip():
        topics["未分类"] = [content.strip()]
    return {topic: "\n".join(lines).strip() for topic, lines in topics.items()}


def render_topics(topics: dict[str, str]) -> str:
    """把主题字典渲染成 Markdown 记忆。"""
    blocks = []
    for topic, content in sorted(topics.items()):
        block = f"# {topic}"
        if content:
            block = f"{block}\n{content}"
        blocks.append(block)
    return "\n\n".join(blocks).strip()


class LongTermMemoryStore:
    """负责长期记忆的读取、写入、合并、去重和删除标记。"""

    def __init__(self, global_dir: Path, project_root: Path) -> None:
        self.global_dir = global_dir
        self.project_root = project_root.resolve()

    def _dir_for(self, scope: str, workspace: str | None = None) -> Path:
        """按作用域返回记忆目录。"""
        if scope == "global":
            return self.global_dir
        if scope == "project":
            return (Path(workspace) if workspace else self.project_root).resolve() / ".minic" / "memory"
        raise ValueError(f"不支持的作用域: {scope}")

    def _memory_path(self, directory: Path) -> Path:
        """返回记忆 Markdown 文件路径。"""
        return directory / "minic.md"

    def _meta_path(self, directory: Path) -> Path:
        """返回记忆元数据文件路径。"""
        return directory / "minic.meta.json"

    def _read_raw(self, directory: Path) -> str:
        """读取记忆文件原始内容。"""
        path = self._memory_path(directory)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8").strip()

    def _write_raw(self, directory: Path, content: str) -> None:
        """原子写入记忆文件。"""
        directory.mkdir(parents=True, exist_ok=True)
        path = self._memory_path(directory)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(path)

    def _load_meta(self, directory: Path) -> dict[str, Any]:
        """读取记忆元数据。"""
        path = self._meta_path(directory)
        if not path.exists():
            return {"topics": {}}
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_meta(self, directory: Path, meta: dict[str, Any]) -> None:
        """写入记忆元数据。"""
        directory.mkdir(parents=True, exist_ok=True)
        path = self._meta_path(directory)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def _update_meta_sources(self, directory: Path, topics: dict[str, str], source: str) -> None:
        """把主题来源写入元数据，保留用户来源和删除标记。"""
        meta = self._load_meta(directory)
        known = meta.setdefault("topics", {})
        for topic in topics:
            entry = known.setdefault(topic, {})
            if entry.get("deleted"):
                continue
            if entry.get("source") != "user":
                entry["source"] = source
        self._save_meta(directory, meta)

    def _merge_markdown(self, existing: str, new: str, directory: Path) -> dict[str, str]:
        """按主题合并记忆，用户内容优先，删除标记生效。"""
        existing_topics = parse_topics(existing)
        new_topics = parse_topics(new)
        meta = self._load_meta(directory)
        known = meta.get("topics", {})
        merged = dict(existing_topics)
        for topic, content in new_topics.items():
            entry = known.get(topic, {})
            if entry.get("deleted"):
                continue
            if known.get(topic, {}).get("source") == "user":
                continue
            merged[topic] = content
        return merged

    def read(self, scope: str = "merged", workspace: str | None = None) -> dict[str, str]:
        """读取长期记忆，scope 支持 global/project/merged。"""
        if scope == "global":
            content = self._read_raw(self.global_dir)
        elif scope == "project":
            content = self._read_raw(self._dir_for("project", workspace))
        elif scope == "merged":
            global_content = self._read_raw(self.global_dir)
            project_content = self._read_raw(self._dir_for("project", workspace))
            content = "\n\n".join(part for part in (global_content, project_content) if part).strip()
        else:
            raise ValueError(f"不支持的作用域: {scope}")
        return {"scope": scope, "content": content, "version": _sha256(content)}

    def write(
        self,
        scope: str,
        workspace: str | None,
        content: str,
        mode: str = "replace",
        base_version: str | None = None,
    ) -> None:
        """写入长期记忆，支持 replace 与 merge。"""
        directory = self._dir_for(scope, workspace)
        current = self._read_raw(directory)
        current_version = _sha256(current)
        if base_version is not None and base_version != current_version:
            raise MemoryConflictError(content=current, version=current_version)
        if mode == "replace":
            new_content = content.strip()
            self._update_meta_sources(directory, parse_topics(new_content), "user")
        elif mode == "merge":
            merged = self._merge_markdown(current, content, directory)
            new_content = render_topics(merged)
            self._update_meta_sources(directory, parse_topics(new_content), "user")
        else:
            raise ValueError(f"不支持的写入模式: {mode}")
        self._write_raw(directory, new_content)

    def add_topic(
        self,
        scope: str,
        workspace: str | None,
        topic: str,
        content: str,
        source: str = "inferred",
    ) -> bool:
        """新增或更新主题，自动去重并遵循用户内容优先。"""
        directory = self._dir_for(scope, workspace)
        meta = self._load_meta(directory)
        entry = meta.get("topics", {}).get(topic, {})
        if entry.get("deleted"):
            return False  # 已删除主题不再重写
        current_topics = parse_topics(self._read_raw(directory))
        existing_content = current_topics.get(topic)
        existing_source = entry.get("source")
        if existing_content == content and existing_source == source:
            return False  # 相同内容与来源不重复写入
        if existing_source == "user" and source != "user":
            return False  # 用户明确内容优先，推断内容不覆盖
        current_topics[topic] = content
        meta.setdefault("topics", {})[topic] = {"source": source, "deleted": False}
        self._save_meta(directory, meta)
        self._write_raw(directory, render_topics(current_topics))  # 渲染 Markdown 后落盘
        return True

    def mark_deleted(self, scope: str, workspace: str | None, topic: str) -> None:
        """把主题标记为删除，后续 add_topic 不再重写。"""
        directory = self._dir_for(scope, workspace)
        meta = self._load_meta(directory)
        meta.setdefault("topics", {})[topic] = {"source": "user", "deleted": True}
        topics = parse_topics(self._read_raw(directory))
        topics.pop(topic, None)
        self._save_meta(directory, meta)
        self._write_raw(directory, render_topics(topics))

    def remove_topic(self, scope: str, workspace: str | None, topic: str) -> None:
        """从记忆内容与元数据中彻底移除主题。"""
        directory = self._dir_for(scope, workspace)
        meta = self._load_meta(directory)
        meta.get("topics", {}).pop(topic, None)
        topics = parse_topics(self._read_raw(directory))
        topics.pop(topic, None)
        self._save_meta(directory, meta)
        self._write_raw(directory, render_topics(topics))

    def migrate_project_to_global(
        self,
        classify,
        workspace: str | None = None,
    ) -> list[dict[str, str]]:
        """按通用分类把项目记忆中属于 global 的主题迁移到全局记忆。"""
        project_workspace = str(workspace or self.project_root)
        directory = self._dir_for("project", project_workspace)
        meta = self._load_meta(directory)
        topics = parse_topics(self._read_raw(directory))
        migrated: list[dict[str, str]] = []
        for topic, content in topics.items():
            if classify(topic, content) != "global":
                continue
            source = meta.get("topics", {}).get(topic, {}).get("source", "inferred")
            self.add_topic("global", None, topic, content, source=source)
            self.remove_topic("project", project_workspace, topic)
            migrated.append({"topic": topic, "content": content})
        return migrated
