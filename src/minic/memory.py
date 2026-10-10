"""长期记忆存储：全局与项目 Markdown 记忆。"""

from __future__ import annotations

import hashlib
import json
import re
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


def _markdown_headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """识别真实标题，代码围栏内的注释不是记忆主题。"""
    headings = []
    fence = None
    for index, line in enumerate(lines):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        match = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*$", line)
        if match and fence is None:
            headings.append((index, len(match.group(1)), match.group(2)))
    return headings


def parse_topics(content: str) -> dict[str, str]:
    """把 Markdown 记忆解析成主题字典，保留代码内注释。"""
    lines = content.splitlines(keepends=True)
    headings = _markdown_headings(lines)
    if not headings:
        return {"未分类": content.strip()} if content.strip() else {}
    topics = {}
    for position, (start, _, topic) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        topics[topic] = "".join(lines[start + 1:end]).strip()
    return topics


def render_topics(topics: dict[str, str]) -> str:
    """把主题字典渲染成 Markdown 记忆。"""
    blocks = []
    for topic, content in sorted(topics.items()):
        block = f"# {topic}"
        if content:
            block = f"{block}\n{content}"
        blocks.append(block)
    return "\n\n".join(blocks).strip()

def update_topic_markdown(existing: str, topic: str, content: str | None) -> str:
    """只改一个主题，保留人工编辑的前言、其他标题和代码块。"""
    lines = existing.splitlines(keepends=True)
    headings = _markdown_headings(lines)
    target = next((item for item in headings if item[2] == topic), None)
    if target:
        start, level, _ = target
        end = next((index for index, _, _ in headings if index > start), len(lines))
        replacement = f"{'#' * level} {topic}\n{content}\n\n" if content is not None else ""
        return "".join(lines[:start]) + replacement + "".join(lines[end:])
    if content is None:
        return existing
    return f"{existing.rstrip()}\n\n# {topic}\n{content}".lstrip()


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
        if directory == self.global_dir:
            return directory / "minic.md"
        return directory.parent.parent / "Agent.md"

    def ensure_project(self, workspace: str | None = None) -> Path:
        """仅为当前使用的项目创建可编辑记忆；已有文件不覆盖。"""
        directory = self._dir_for("project", workspace)
        path = self._memory_path(directory)
        if not path.exists():
            legacy = directory / "minic.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            content = legacy.read_text(encoding="utf-8") if legacy.exists() else "# 项目长期记忆\n\n在此记录项目约定、偏好和可复用信息。MiniC 每轮对话重新读取本文件。\n"
            try:
                with path.open("x", encoding="utf-8") as file:
                    file.write(content)
            except FileExistsError:
                pass
        return path

    def _meta_path(self, directory: Path) -> Path:
        """返回记忆元数据文件路径。"""
        return directory / "minic.meta.json"

    def _read_raw(self, directory: Path) -> str:
        """读取记忆文件原始内容。"""
        path = self._memory_path(directory)
        if not path.exists():
            path = directory / "minic.md"  # 老项目只读兼容，正式使用时迁移。
            if not path.exists():
                return ""
        return path.read_text(encoding="utf-8-sig").strip()

    def _write_raw(self, directory: Path, content: str) -> None:
        """原子写入记忆文件。"""
        directory.mkdir(parents=True, exist_ok=True)
        path = self._memory_path(directory)
        if directory != self.global_dir and path.exists() and path.read_text(encoding="utf-8") != content:
            from minic.backups import write_file_backup
            write_file_backup(directory.parent / "backups" / "files", path)
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
        path = self._memory_path(self._dir_for(scope, workspace)) if scope != "merged" else self._memory_path(self._dir_for("project", workspace))
        return {"scope": scope, "content": content, "version": _sha256(content), "path": str(path)}

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
            new_content = current
            previous = parse_topics(current)
            for topic, value in merged.items():
                if previous.get(topic) != value:
                    new_content = update_topic_markdown(new_content, topic, value)
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
        existing_markdown = self._read_raw(directory)
        current_topics = parse_topics(existing_markdown)
        existing_content = current_topics.get(topic)
        existing_source = entry.get("source")
        if existing_content == content and existing_source == source:
            return False  # 相同内容与来源不重复写入
        if existing_source == "user" and source != "user":
            return False  # 用户明确内容优先，推断内容不覆盖
        current_topics[topic] = content
        meta.setdefault("topics", {})[topic] = {"source": source, "deleted": False}
        self._save_meta(directory, meta)
        self._write_raw(directory, update_topic_markdown(existing_markdown, topic, content))
        return True

    def mark_deleted(self, scope: str, workspace: str | None, topic: str) -> None:
        """把主题标记为删除，后续 add_topic 不再重写。"""
        directory = self._dir_for(scope, workspace)
        meta = self._load_meta(directory)
        meta.setdefault("topics", {})[topic] = {"source": "user", "deleted": True}
        existing = self._read_raw(directory)
        self._save_meta(directory, meta)
        self._write_raw(directory, update_topic_markdown(existing, topic, None))

    def remove_topic(self, scope: str, workspace: str | None, topic: str) -> None:
        """从记忆内容与元数据中彻底移除主题。"""
        directory = self._dir_for(scope, workspace)
        meta = self._load_meta(directory)
        meta.get("topics", {}).pop(topic, None)
        existing = self._read_raw(directory)
        self._save_meta(directory, meta)
        self._write_raw(directory, update_topic_markdown(existing, topic, None))

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
