"""G7b SKILL 扫描与开关管理（规格书 15 节）。

SKILL 存放于 ``~/.minic/skills``（全局）与 ``<project>/.minic/skills``（项目级），
结构为 ``skills/<skill-name>/SKILL.md``。SKILL.md 以 YAML frontmatter 声明
``name``、``description``、``when_to_use``、``allowed-tools``。
启用状态持久化在 ``<project>/.minic/skills_state.json``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class SkillSpec:
    """单个 SKILL 定义（来自 SKILL.md frontmatter）。"""

    name: str
    description: str = ""
    when_to_use: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    scope: str = "global"  # "global" | "project"
    path: Path | None = None
    enabled: bool = False
    conflict: bool = False


def parse_skill_md(path: Path) -> SkillSpec | None:
    """读取 SKILL.md 并解析 frontmatter；无 frontmatter 或 name 缺失时返回 None。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not text.lstrip().startswith("---"):
        return None
    lines = text.splitlines()
    end_index = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end_index = index
            break
    if end_index is None:
        return None
    frontmatter = "\n".join(lines[1:end_index])
    try:
        data = yaml.safe_load(frontmatter)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    raw_name = data.get("name")
    name = str(raw_name).strip() if raw_name is not None else ""
    if not name:
        return None
    raw_allowed = data.get("allowed-tools") or []
    if isinstance(raw_allowed, str):
        raw_allowed = [raw_allowed]
    allowed_tools = [str(item).strip() for item in raw_allowed if str(item).strip()]
    return SkillSpec(
        name=name,
        description=str(data.get("description", "")).strip(),
        when_to_use=str(data.get("when_to_use", "")).strip(),
        allowed_tools=allowed_tools,
        path=Path(path).resolve(),
    )


def _scan_dir(skills_root: Path, scope: str) -> list[SkillSpec]:
    """扫描 SKILL.md，返回该层级的 SKILL 列表。

    兼容两种传参：直接传入 skills 目录（如 ``~/.minic/skills``，含
    ``<name>/SKILL.md``），或传入其父目录（如 ``~/.minic``，含
    ``skills/<name>/SKILL.md``）。同层同名只保留第一条。
    """
    specs: list[SkillSpec] = []
    root = Path(skills_root)
    names: set[str] = set()
    for candidate in (root, root / "skills"):
        if not candidate.is_dir():
            continue
        for skill_dir in sorted(candidate.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue
            spec = parse_skill_md(skill_md)
            if spec is None or spec.name in names:
                continue
            names.add(spec.name)
            spec.scope = scope
            specs.append(spec)
    return specs


def scan_skills(global_dir: str | Path, project_dir: str | Path) -> list[SkillSpec]:
    """扫描全局与项目 SKILL；同名冲突时项目级优先并标记 conflict=True。

    冲突时项目级条目保留（生效条目），同时保留全局条目供 ``GET /skills`` 展示冲突。
    """
    project_specs = _scan_dir(project_dir, "project")
    global_specs = _scan_dir(global_dir, "global")
    global_names = {spec.name for spec in global_specs}
    result: list[SkillSpec] = []
    for spec in project_specs:
        if spec.name in global_names:
            spec.conflict = True
        result.append(spec)
    result.extend(global_specs)
    return result


def load_skill_state(state_path: str | Path) -> dict[str, Any]:
    """读取 skills_state.json；缺失或损坏时返回空状态。"""
    path = Path(state_path)
    if not path.exists():
        return {"enabled": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"enabled": {}}
    if not isinstance(data, dict):
        return {"enabled": {}}
    data.setdefault("enabled", {})
    return data


def save_skill_state(state_path: str | Path, state: dict[str, Any]) -> None:
    """原子写入 skills_state.json。"""
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def skill_inject_text(skill_manager: Any) -> str:
    """返回 SKILL 注入文本；无管理器时返回空串。"""
    if skill_manager is None:
        return ""
    return skill_manager.inject_text()


class SkillManager:
    """SKILL 扫描、开关状态与注入文本管理。"""

    def __init__(
        self,
        global_dir: str | Path,
        project_dir: str | Path,
        state_path: str | Path,
    ) -> None:
        self.global_dir = Path(global_dir)
        self.project_dir = Path(project_dir)
        self.state_path = Path(state_path)
        self._entries: list[SkillSpec] = []
        self._specs: dict[str, SkillSpec] = {}
        self._conflicts: set[str] = set()
        self._state = load_skill_state(state_path)
        self.rescan()

    def rescan(self) -> None:
        """重新扫描 SKILL.md 并应用持久化开关状态。"""
        self._entries = scan_skills(self.global_dir, self.project_dir)
        self._specs = {}
        self._conflicts = set()
        for spec in self._entries:
            if spec.scope == "project":
                self._specs[spec.name] = spec  # 同名冲突时项目级生效
                if spec.conflict:
                    self._conflicts.add(spec.name)
            else:
                self._specs.setdefault(spec.name, spec)  # 全局条目兜底
        enabled = self._state.get("enabled", {})
        for name, entry in enabled.items():
            scope = (entry or {}).get("scope", "project")
            for spec in self._entries:
                if spec.name == name and spec.scope == scope:
                    spec.enabled = True

    def _set_enabled(self, name: str, scope: str, confirmed: bool) -> None:
        """记录启用状态到 skills_state.json 并同步内存。"""
        self._state.setdefault("enabled", {})[name] = {
            "scope": scope,
            "confirmed": bool(confirmed),
        }
        save_skill_state(self.state_path, self._state)
        for spec in self._entries:
            if spec.name == name:
                spec.enabled = spec.scope == scope

    def enable(self, name: str, confirm: bool = False) -> tuple[str, None]:
        """启用 SKILL；同名冲突且未确认时返回 needs_confirmation。"""
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(f"SKILL 不存在: {name}")
        entry = self._state.get("enabled", {}).get(name) or {}
        if name in self._conflicts and not confirm and not entry.get("confirmed"):
            return ("needs_confirmation", None)
        self._set_enabled(name, spec.scope, True)
        return ("ok", None)

    def confirm(self, name: str) -> None:
        """显式确认同名冲突后启用。"""
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(f"SKILL 不存在: {name}")
        self._set_enabled(name, spec.scope, True)

    def disable(self, name: str) -> None:
        """禁用 SKILL 并持久化。"""
        if name not in self._specs:
            raise KeyError(f"SKILL 不存在: {name}")
        self._state.setdefault("enabled", {}).pop(name, None)
        save_skill_state(self.state_path, self._state)
        for spec in self._entries:
            if spec.name == name:
                spec.enabled = False

    def active(self) -> list[SkillSpec]:
        """返回当前启用的 SKILL（同名冲突时只返回生效条目）。"""
        return [spec for spec in self._entries if spec.enabled]

    def allowed_tools(self, name: str) -> list[str]:
        """返回指定 SKILL 的 allowed-tools。"""
        spec = self._specs.get(name)
        return list(spec.allowed_tools) if spec is not None else []

    def allowed_union(self) -> set[str]:
        """返回启用 SKILL 的 allowed-tools 并集（供工具调用层白名单约束）。"""
        union: set[str] = set()
        for spec in self.active():
            if spec.allowed_tools:
                union.update(spec.allowed_tools)
        return union

    def inject_text(self, max_chars: int = 2000) -> str:
        """拼接 SKILL 注入文本：启用技能的描述 + 未启用技能清单。

        未启用技能也列出（标注未启用），保证"你有什么技能"类问题能
        如实回答完整技能清单，而不是让模型自由发挥。
        """
        parts: list[str] = []
        for spec in self.active():
            text = f"- {spec.name}：{spec.description}"
            if spec.when_to_use:
                text += f"（适用场景：{spec.when_to_use}）"
            parts.append(text)
        disabled = [spec.name for spec in self._entries if not spec.enabled]
        if disabled:
            parts.append(f"未启用技能（可在设置中开启）：{'、'.join(disabled)}")
        joined = "\n".join(parts)
        if len(joined) > max_chars:
            return joined[:max_chars] + "…"
        return joined

    def list(self) -> list[dict[str, Any]]:
        """返回全部 SKILL 视图（含 enabled/scope/conflict 标记）。"""
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "when_to_use": spec.when_to_use,
                "allowed_tools": list(spec.allowed_tools),
                "enabled": spec.enabled,
                "scope": spec.scope,
                "conflict": spec.conflict,
            }
            for spec in self._entries
        ]
