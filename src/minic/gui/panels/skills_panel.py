"""技能面板：GET /skills 列表 + 搜索/过滤 + 开关/删除。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from PySide6 import QtCore
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from minic.gui.icons import load_icon, load_pixmap
from minic.gui.local_data import local_skills
from minic.graph.tools import _TOOL_REGISTRY
from minic.gui.panels.base import PanelBase, group_title, sub_note
from minic.gui.theme import (
    COLOR_ACCENT,
    COLOR_BG_CARD,
    COLOR_BORDER,
    COLOR_RED,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_SECONDARY,
)
from minic.gui.widgets.dialogs import ConfirmDialog
from minic.gui.widgets.toggle import ToggleSwitch
from minic.gui.widgets.worker import Worker
import shutil


class SkillItemRow(QFrame):
    """技能/内置工具条目：图标 + 名称 + 描述 + 标签 + 开关 + 删除。"""

    def __init__(
        self,
        name: str,
        description: str,
        enabled: bool,
        tag: str,
        on_toggled: Callable[[str, bool], None],
        on_delete: Callable[[str], None],
        parent: QWidget | None = None,
        builtin: bool = False,
    ) -> None:
        super().__init__(parent)
        self.name = name
        self.setObjectName("SkillItem")
        self.setStyleSheet(
            f"#SkillItem {{ background-color: {COLOR_BG_CARD};"
            f"border: 1px solid {COLOR_BORDER}; border-radius: 8px; }}"
            f"#SkillItem:hover {{ border-color: #4a4a4a; }}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        icon = QLabel(self)
        icon.setPixmap(load_pixmap("技能", 16))
        icon.setFixedSize(20, 20)
        icon.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(icon)

        info = QVBoxLayout()
        info.setSpacing(2)
        name_label = QLabel(name, self)
        name_label.setStyleSheet(
            f"color: #ffffff; font-size: 13px; font-weight: 500; background: transparent; border: none;"
        )
        info.addWidget(name_label)
        desc_label = QLabel(description or "", self)
        desc_label.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 12px; background: transparent; border: none;"
        )
        desc_label.setWordWrap(True)
        info.addWidget(desc_label)
        info.addStretch(1)
        layout.addLayout(info, 1)

        tag_label = QLabel(tag, self)
        tag_label.setStyleSheet(
            f"color: {COLOR_ACCENT}; border: 1px solid #2d4a5e; border-radius: 10px;"
            f"padding: 2px 8px; font-size: 11px; background: transparent;"
        )
        layout.addWidget(tag_label)

        toggle = ToggleSwitch(self)
        toggle.setChecked(enabled)
        toggle.setEnabled(not builtin)  # 内置工具不可开关
        toggle.toggled.connect(lambda checked, n=name: on_toggled(n, checked))
        layout.addWidget(toggle)

        delete_btn = QPushButton(self)
        delete_btn.setIcon(load_icon("删 除"))
        delete_btn.setIconSize(QtCore.QSize(14, 14))
        delete_btn.setFixedSize(26, 26)
        delete_btn.setToolTip("删除")
        delete_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; border-radius: 4px; }}"
            f"QPushButton:hover {{ background-color: rgba(232, 17, 35, 51); }}"
        )
        delete_btn.setEnabled(not builtin)  # 内置工具不可删除
        delete_btn.clicked.connect(lambda: on_delete(name))
        layout.addWidget(delete_btn)


class SkillsPanel(PanelBase):
    """技能面板：管理项目级与用户级技能（GET /skills）。"""

    def __init__(
        self,
        client: object | None = None,
        toast: Callable[[str], None] | None = None,
        parent=None,
        workspace: str | None = None,
        notify: Callable[[str, str], None] | None = None,
        progress: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__(client, toast, parent, workspace, notify, progress)
        self._skills: list[dict[str, Any]] = []
        # 内置工具（代码注册表，不可开关/删除，不参与搜索）
        self._builtin_tools = [
            {"name": spec.name, "description": spec.description}
            for spec in _TOOL_REGISTRY.values()
        ]

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(10)

        # 标题 + 右上角按钮
        header = QHBoxLayout()
        title = QLabel("技能", self)
        title.setStyleSheet(
            f"color: #ffffff; font-size: 20px; font-weight: 600; background: transparent; border: none;"
        )
        header.addWidget(title)
        header.addStretch(1)
        refresh_btn = QPushButton(self)
        refresh_btn.setIcon(load_icon("刷新"))
        refresh_btn.setIconSize(QtCore.QSize(18, 18))
        refresh_btn.setFixedSize(32, 32)
        refresh_btn.setToolTip("刷新")
        refresh_btn.setStyleSheet(
            f"background: transparent; color: {COLOR_TEXT_MUTED}; border: none; font-size: 14px;"
            f"border-radius: 6px;"
        )
        refresh_btn.clicked.connect(self._on_refresh)
        header.addWidget(refresh_btn)
        root.addLayout(header)

        # 搜索 + 过滤
        search_row = QHBoxLayout()
        self._search = QLineEdit(self)
        self._search.setPlaceholderText("搜索技能...")
        self._search.setStyleSheet(
            f"background-color: {COLOR_BG_CARD}; color: #ffffff;"
            f"border: 1px solid {COLOR_BORDER}; border-radius: 6px; padding: 6px 10px;"
        )
        self._search.textChanged.connect(lambda _text: self._render())
        search_row.addWidget(self._search, 1)
        root.addLayout(search_row)

        # 分组容器
        self._group_container = QWidget(self)
        self._group_layout = QVBoxLayout(self._group_container)
        self._group_layout.setContentsMargins(0, 0, 0, 0)
        self._group_layout.setSpacing(6)
        root.addWidget(self._group_container, 1)
        root.addStretch(1)

    # ---- 数据 ----

    def reload(self) -> None:
        """加载技能：核心可用走 /skills，否则本地扫描 ~/.minic/skills 与项目 skills。"""
        if self.client is None or not getattr(self.client, "base_url", None):
            self._skills = local_skills(self.workspace)
            self._render()
            return
        worker = Worker(lambda: self.client.get_skills(), self)
        worker.completed.connect(self._on_skills_loaded)
        worker.failed.connect(lambda err: self.notify(f"读取技能失败：{err}", "failed"))
        worker.start()

    def _on_skills_loaded(self, skills: Any) -> None:
        """渲染技能列表。"""
        self._skills = list(skills) if isinstance(skills, list) else []
        self._render()

    def _render(self) -> None:
        """按过滤条件渲染技能分组。"""
        # 清空分组
        while self._group_layout.count():
            item = self._group_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        query = self._search.text().strip().lower()

        filtered: list[dict[str, Any]] = []
        for skill in self._skills:
            name = str(skill.get("name", ""))
            if query and query not in name.lower():
                continue
            filtered.append(skill)

        # 已安装技能分组（搜索时只显示匹配的技能）
        group_title_widget = group_title(
            f"已安装技能 ({len(filtered)} 项)", self._group_container
        )
        self._group_layout.addWidget(group_title_widget)
        if not filtered:
            empty = QLabel("还没有技能", self._group_container)
            empty.setStyleSheet(
                f"color: {COLOR_TEXT_MUTED}; font-size: 12px; background: transparent; border: none;"
                f"padding: 8px 2px;"
            )
            self._group_layout.addWidget(empty)
        for skill in filtered:
            self._group_layout.addWidget(
                SkillItemRow(
                    name=str(skill.get("name", "")),
                    description=str(skill.get("description", "") or ""),
                    enabled=bool(skill.get("enabled", False)),
                    tag="个人" if skill.get("scope") == "global" else "工作区",
                    on_toggled=self._on_toggle,
                    on_delete=self._on_delete,
                    parent=self._group_container,
                )
            )

        # 内置工具分组（代码注册表，不参与搜索：搜索时隐藏）
        if not query:
            tools_title = group_title(
                f"内置工具 ({len(self._builtin_tools)} 项)", self._group_container
            )
            self._group_layout.addWidget(tools_title)
            for tool in self._builtin_tools:
                self._group_layout.addWidget(
                    SkillItemRow(
                        name=str(tool.get("name", "")),
                        description=str(tool.get("description", "") or ""),
                        enabled=True,
                        tag="内置",
                        on_toggled=self._on_toggle,
                        on_delete=self._on_delete,
                        parent=self._group_container,
                        builtin=True,
                    )
                )

    # ---- 交互 ----

    def _on_toggle(self, name: str, checked: bool) -> None:
        """启用/禁用技能（顶部通知条反馈）。"""
        if self.client is None:
            self.notify("核心未运行，开关未生效", "failed")
            return
        if checked:
            worker = Worker(lambda: self.client.post_json(f"/skills/{name}/enable", {"confirm": True}), self)
        else:
            worker = Worker(lambda: self.client.post_json(f"/skills/{name}/disable"), self)
        worker.completed.connect(
            lambda ok: (
                self.notify(f"已{'启用' if checked else '禁用'}技能：{name}", "success")
                if ok
                else self.notify("操作失败", "failed")
            )
        )
        worker.failed.connect(lambda err: self.notify(f"操作失败：{err}", "failed"))
        worker.start()

    def _on_delete(self, name: str) -> None:
        """删除技能：确认后删除 ~/.minic/skills/<name> 目录并刷新。"""
        dialog = ConfirmDialog(f"是否要删除 {name} 技能", "删除", self)
        if not (dialog.exec() and dialog.confirmed):
            return
        target = Path.home() / ".minic" / "skills" / name
        try:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            self.notify(f"已删除技能：{name}", "success")
        except OSError as exc:
            self.notify(f"删除技能失败：{exc}", "failed")
        self.reload()

    def _on_refresh(self) -> None:
        """刷新列表。"""
        self.reload()
