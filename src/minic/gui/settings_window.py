"""设置窗口/设置页：左侧导航（QListWidget）+ 右侧面板栈（QStackedWidget）。

- :class:`SettingsWindow`：独立窗口形态（无边框自定义标题栏），测试与外部复用。
- :class:`SettingsPage`：内嵌页面形态（返回按钮），主窗口内容栈直接切换，不新开窗口。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from minic.gui.icons import load_icon
from minic.gui.panels import (
    KnowledgePanel,
    McpPanel,
    MemoryPanel,
    ModelPanel,
    SkillsPanel,
    SubagentsPanel,
    UsagePanel,
)
from minic.gui.theme import (
    COLOR_BG_SIDEBAR,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_SECONDARY,
)
from minic.gui.title_bar import TitleBar
from minic.gui.widgets.toast import Toast

# 导航分组结构：(分组标题, [(显示名, 面板名)])
# 无「常规/主题」：主题固定深色，不再提供设置入口
_NAV_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("基础设置", [("模型设置", "model"), ("知识库", "knowledge")]),
    ("Agent 能力", [("记忆", "memory"), ("技能", "skills"), ("子智能体", "subagents"), ("MCP 服务器", "mcp")]),
    ("数据与统计", [("使用统计", "usage")]),
]

# 面板名 → 导航图标名（icon/ 目录）
_NAV_ICONS: dict[str, str] = {
    "model": "模型",
    "knowledge": "知识库",
    "memory": "记忆",
    "skills": "技能",
    "subagents": "子智能体",
    "mcp": "MCP 服务器",
    "usage": "统计",
}

# 面板名 → 面板类
_PANEL_CLASSES: dict[str, type] = {
    "model": ModelPanel,
    "knowledge": KnowledgePanel,
    "memory": MemoryPanel,
    "skills": SkillsPanel,
    "subagents": SubagentsPanel,
    "mcp": McpPanel,
    "usage": UsagePanel,
}


class _SettingsNavMixin:
    """设置导航 + 面板栈构建与切换（SettingsWindow / SettingsPage 共用）。

    子类需提供 ``self.client``、``self.toast``、``self._panel_name_to_item``、
    ``self._panel_name_to_index`` 属性。
    """

    def _build_nav(self, nav: QListWidget) -> None:
        """构建导航项（含分组头）。"""
        for group_title, items in _NAV_GROUPS:
            header_item = QListWidgetItem(group_title)
            header_item.setFlags(Qt.ItemFlag.NoItemFlags)
            nav.addItem(header_item)
            for display_name, panel_name in items:
                item = QListWidgetItem(display_name)
                icon_name = _NAV_ICONS.get(panel_name)
                if icon_name:
                    item.setIcon(load_icon(icon_name))
                item.setData(Qt.ItemDataRole.UserRole, panel_name)
                nav.addItem(item)
                self._panel_name_to_item[panel_name] = item

    def _build_panels(self, stack: QStackedWidget) -> None:
        """构建全部面板（每个面板包一层滚动区）。"""
        self._panel_widgets: dict[str, object] = {}
        index = 0
        for _group_title, items in _NAV_GROUPS:
            for _display_name, panel_name in items:
                panel_class = _PANEL_CLASSES[panel_name]
                panel = panel_class(
                    client=self.client,
                    toast=self.toast,
                    workspace=getattr(self, "workspace", None),
                    notify=getattr(self, "notify", None),
                    progress=getattr(self, "progress", None),
                )
                self._panel_widgets[panel_name] = panel
                scroll = QScrollArea(stack)
                scroll.setWidgetResizable(True)
                scroll.setStyleSheet(
                    f"QScrollArea {{ background: transparent; border: none; }}"
                    f"QScrollArea > QWidget > QWidget {{ background: transparent; }}"
                )
                inner = QWidget()
                inner_layout = QVBoxLayout(inner)
                inner_layout.setContentsMargins(28, 24, 28, 24)
                inner_layout.addWidget(panel)
                inner_layout.addStretch(1)
                scroll.setWidget(inner)
                stack.addWidget(scroll)
                self._panel_name_to_index[panel_name] = index
                index += 1

    def _on_nav_changed(self, row: int) -> None:
        """导航切换面板。"""
        item = self._nav.item(row)
        if item is None:
            return
        panel_name = item.data(Qt.ItemDataRole.UserRole)
        if panel_name is None:
            return
        self._stack.setCurrentIndex(self._panel_name_to_index[panel_name])

    def _switch_panel(self, name: str) -> None:
        """跳转到指定面板（如技能面板），不改窗口形态。"""
        item = self._panel_name_to_item.get(name)
        if item is None:
            return
        self._nav.setCurrentItem(item)
        self._stack.setCurrentIndex(self._panel_name_to_index[name])

    @staticmethod
    def _nav_style() -> str:
        """左侧导航统一样式。"""
        return (
            f"QListWidget {{ background-color: {COLOR_BG_SIDEBAR}; border: none; outline: none; }}"
            f"QListWidget::item {{ color: {COLOR_TEXT_SECONDARY}; padding: 8px 14px;"
            f"border-radius: 6px; font-size: 13px; }}"
            f"QListWidget::item:hover {{ background-color: #2a2d2e; }}"
            f"QListWidget::item:selected {{ background-color: #37373d; color: #ffffff; }}"
            f"QListWidget::item:disabled {{ color: {COLOR_TEXT_MUTED}; font-size: 11px;"
            f"padding: 10px 14px 4px; }}"
        )


class SettingsWindow(QDialog, _SettingsNavMixin):
    """MiniC 设置窗口（独立窗口形态）。

    无边框自定义标题栏 + 左侧导航 + 右侧面板栈；支持 ``open_panel(name)``
    供外部（如主窗口技能菜单）跳转到指定面板。
    """

    def __init__(
        self,
        client: object | None = None,
        toast: Callable[[str], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
        )
        self.setMinimumSize(900, 620)
        self.resize(1000, 680)
        self.setObjectName("SettingsWindow")
        self.setStyleSheet(
            f"#SettingsWindow {{ background-color: #1e1e1e; }}"
        )

        self.client = client
        self._panel_name_to_item: dict[str, QListWidgetItem] = {}
        self._panel_name_to_index: dict[str, int] = {}

        # Toast 浮层（设置窗口自己的），面板 toast 走同一个
        self._toast_widget = Toast(self)
        self.toast = self._toast_widget.show_message

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        title_bar = TitleBar("设置", self)
        root.addWidget(title_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(1)

        # ---- 左侧导航 ----
        nav = QListWidget(splitter)
        nav.setFixedWidth(220)
        nav.setStyleSheet(self._nav_style())
        self._nav = nav
        self._build_nav(nav)

        # ---- 右侧面板栈 ----
        self._stack = QStackedWidget(splitter)
        self._build_panels(self._stack)

        splitter.addWidget(nav)
        splitter.addWidget(self._stack)
        splitter.setSizes([220, 780])
        root.addWidget(splitter, 1)

        nav.currentRowChanged.connect(self._on_nav_changed)

        # 默认选中「模型设置」
        self._switch_panel("model")

    def open_panel(self, name: str) -> None:
        """跳转到指定面板并置顶窗口。"""
        self._switch_panel(name)
        self.show()
        self.raise_()
        self.activateWindow()

    def show_toast(self, message: str) -> None:
        """对外 toast 入口。"""
        self._toast_widget.show_message(message)


class SettingsPage(QWidget, _SettingsNavMixin):
    """内嵌设置页：仅右侧面板栈（导航在侧栏设置导航区）。

    作为主窗口内容栈的一页使用；``open_panel(name)`` 切换面板。
    """

    def __init__(
        self,
        client: object | None = None,
        toast: Callable[[str], None] | None = None,
        parent: QWidget | None = None,
        workspace: str | None = None,
        notify: Callable[[str, str], None] | None = None,
        progress: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SettingsPage")
        self.setStyleSheet(
            f"#SettingsPage {{ background-color: #1e1e1e; }}"
        )

        self.client = client
        self.toast = toast or (lambda message: None)
        self.workspace = workspace  # 传给面板的本地配置降级读取
        self.notify = notify  # 顶部通知条回调
        self.progress = progress  # 顶栏入库进度条回调
        self._panel_name_to_item: dict[str, QListWidgetItem] = {}
        self._panel_name_to_index: dict[str, int] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 面板栈 ----
        self._stack = QStackedWidget(self)
        self._build_panels(self._stack)
        root.addWidget(self._stack)

    def open_panel(self, name: str) -> None:
        """跳转到指定面板（不改变窗口形态）。"""
        index = self._panel_name_to_index.get(name)
        if index is None:
            return
        self._stack.setCurrentIndex(index)

    def has_unsaved_changes(self) -> bool:
        """任一面板存在未保存修改。"""
        return any(
            getattr(panel, "has_unsaved_changes", lambda: False)()
            for panel in self._panel_widgets.values()
        )

    def _current_panel(self) -> object | None:
        """当前显示的面板实例。"""
        index = self._stack.currentIndex()
        for name, panel_index in self._panel_name_to_index.items():
            if panel_index == index:
                return self._panel_widgets.get(name)
        return None

    def save_current(self) -> bool:
        """保存当前显示面板；返回是否已保存（False=校验失败）。"""
        panel = self._current_panel()
        if panel is not None and hasattr(panel, "save"):
            return bool(panel.save())
        return False

    def discard_current(self) -> None:
        """放弃当前面板未保存修改（回只读态）。"""
        panel = self._current_panel()
        if panel is not None and hasattr(panel, "discard"):
            panel.discard()


class SettingsNavList(QListWidget):
    """设置导航列表（分组 + 项）：选中的面板名通过 panel_selected 发出。

    用于主窗口侧栏设置导航区；与 :class:`SettingsPage` 面板栈联动。
    """

    panel_selected = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._name_to_item: dict[str, QListWidgetItem] = {}
        self.setStyleSheet(_SettingsNavMixin._nav_style())
        for group_title, items in _NAV_GROUPS:
            header_item = QListWidgetItem(group_title)
            header_item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.addItem(header_item)
            for display_name, panel_name in items:
                item = QListWidgetItem(display_name)
                item.setData(Qt.ItemDataRole.UserRole, panel_name)
                self.addItem(item)
                self._name_to_item[panel_name] = item
        self.currentRowChanged.connect(self._on_row_changed)
        if self.count() > 1:  # 跳过首行分组头，默认选中第一个面板项
            self.setCurrentRow(1)

    def _on_row_changed(self, row: int) -> None:
        """行切换：发出面板名信号。"""
        item = self.item(row)
        if item is None:
            return
        panel_name = item.data(Qt.ItemDataRole.UserRole)
        if panel_name is None:
            return
        self.panel_selected.emit(panel_name)

    def select_panel(self, name: str) -> None:
        """高亮指定面板项（不触发切换信号）。"""
        item = self._name_to_item.get(name)
        if item is not None:
            self.setCurrentItem(item)
