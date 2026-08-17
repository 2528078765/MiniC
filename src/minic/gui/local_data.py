"""桌面端本地配置读取：核心未运行时从 ~/.minic 与项目 .minic 读取真实配置。

复用核心加载器（``minic.skills`` / ``minic.mcp.settings`` / ``minic.core.config``
/ ``minic.memory``），与核心运行时的解析行为保持一致。
有核心运行时面板仍走核心 API（状态更实时），此处仅作未运行时的降级数据源。

**不修改任何已有内容**；目录/文件不存在时自动创建（技能目录、MCP 配置、
minic.json、记忆文件），配置内容按核心默认值写入——首次运行即可看到完整结构。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from minic.core.config import AppSettings, load_settings
from minic.mcp.settings import load_mcp_settings
from minic.memory import LongTermMemoryStore
from minic.skills import SkillManager

_EMPTY_MCP_CONFIG = '{\n  "mcpServers": {}\n}\n'


def _ensure_dir(path: Path) -> None:
    """目录不存在时创建（失败静默，读取逻辑仍可继续）。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _ensure_default_minic_json(path: Path) -> None:
    """minic.json 不存在时创建；存在时补全缺失配置段/字段（不覆盖已有值）。

    首次打开桌面端即把 rag/approval/sandbox/memory 等默认段写全，
    用户在设置里的修改持久化到该文件。
    """
    try:
        defaults = AppSettings()
        try:
            default_data = defaults.model_dump()
        except AttributeError:  # pydantic v1 兼容
            default_data = defaults.dict()
    except Exception:  # noqa: BLE001 - 默认值构造失败不影响读取
        return
    _ensure_dir(path.parent)
    try:
        if not path.exists():
            path.write_text(
                json.dumps(default_data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return
        existing = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(existing, dict):
            return
        # 缺失段/字段补默认值（已有值不动）
        from minic.core.config import merge_dict

        merged = merge_dict(default_data, existing)
        if merged != existing:
            path.write_text(
                json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    except OSError:
        pass
    except json.JSONDecodeError:
        pass  # 配置损坏时不覆盖，交由核心报错处理


def local_skills(workspace: str | Path | None = None) -> list[dict[str, Any]]:
    """扫描全局/项目技能目录，返回与 ``GET /skills`` 同构的列表。

    目录：全局 ``~/.minic/skills``、项目 ``<项目>/.minic/skills``（不存在自动创建）；
    启用状态来自 ``<项目>/.minic/skills_state.json``。
    """
    project_root = Path(workspace) if workspace else Path.cwd()
    global_dir = Path.home() / ".minic" / "skills"
    project_dir = project_root / ".minic" / "skills"
    _ensure_dir(global_dir)
    _ensure_dir(project_dir)
    manager = SkillManager(
        global_dir=global_dir,
        project_dir=project_dir,
        state_path=project_root / ".minic" / "skills_state.json",
    )
    return manager.list()


def local_mcp() -> list[dict[str, Any]]:
    """读取 ``~/.minic/mcp/minic_mcp_settings.json``，返回与 ``GET /mcp`` 同构的列表。

    文件不存在时自动创建（默认 ``{"mcpServers": {}}``）。
    status：配置 ``disabled=true`` → ``disabled``，否则 → ``configured``
    （本地静态配置无连接状态，实际连接状态以核心为准）。
    """
    # 运行时求值 HOME（避免 import 时求值的常量在 HOME 隔离/变更时写错位置）
    path = Path.home() / ".minic" / "mcp" / "minic_mcp_settings.json"
    if not path.exists():
        _ensure_dir(path.parent)
        try:
            path.write_text(_EMPTY_MCP_CONFIG, encoding="utf-8")
        except OSError:
            pass
    try:
        settings = load_mcp_settings(path)
    except ValueError:
        return []
    servers: list[dict[str, Any]] = []
    for name, cfg in (settings.get("mcpServers") or {}).items():
        entry = dict(cfg) if isinstance(cfg, dict) else {}
        entry["name"] = name
        entry["status"] = "disabled" if entry.get("disabled") else "configured"
        servers.append(entry)
    return servers


def local_settings(workspace: str | Path | None = None) -> dict[str, Any]:
    """读取全局/项目 ``minic.json``，返回与 ``GET /settings`` 同构的配置段。

    全局 minic.json 不存在时自动创建（核心默认配置）。
    embedding 只读全局（与核心合并规则一致）；api_key 剔除（与 GET /settings 一致）。
    """
    _ensure_default_minic_json(Path.home() / ".minic" / "minic.json")
    return local_settings_raw(workspace)


def local_settings_raw(workspace: str | Path | None = None) -> dict[str, Any]:
    """读取配置但**保留 api_key**（桌面端模型面板回显用，仅本机读取）。"""
    settings = load_settings(Path(workspace) if workspace else None)
    try:
        data = settings.model_dump()
    except AttributeError:  # pydantic v1 兼容
        data = settings.dict()
    return data


def local_memory(scope: str = "global", workspace: str | Path | None = None) -> dict[str, Any]:
    """读取长期记忆（默认全局 ``~/.minic/memory/minic.md``），返回与 ``GET /memory`` 同构。

    目录与 minic.md 不存在时自动创建（空内容）。
    """
    memory_dir = Path.home() / ".minic" / "memory"
    _ensure_dir(memory_dir)
    memory_file = memory_dir / "minic.md"
    if not memory_file.exists():
        try:
            memory_file.write_text("", encoding="utf-8")
        except OSError:
            pass
    project_root = Path(workspace) if workspace else Path.cwd()
    store = LongTermMemoryStore(
        global_dir=memory_dir,
        project_root=project_root,
    )
    return store.read(scope=scope)
