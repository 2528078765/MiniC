"""斜杠命令菜单：输入框输入 / 时浮出命令列表（中文），上下键选择、回车执行。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from minic.gui.theme import (
    COLOR_BG_CARD,
    COLOR_BG_ACTIVE,
    COLOR_BORDER,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_PRIMARY,
    COLOR_TEXT_SECONDARY,
)

# 命令名 → 说明（中文；按规格文档，桌面端支持的命令）
SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/压缩", "压缩当前会话，压缩前备份"),
    ("/新建", "归档当前会话并新建，先备份"),
    ("/清空", "归档当前会话并新建，先备份"),
    ("/记忆", "查看/编辑长期记忆"),
    ("/知识库", "RAG 状态、入库、检索、删除"),
    ("/设置", "设置面板"),
    ("/帮助", "帮助"),
]


class SlashMenu(QFrame):
    """斜杠命令浮层：深色圆角卡片 + 命令列表（命令名 + 说明）。

    信号 ``activated(str)`` 在用户按回车时发出选中命令名；
    键盘上下/Esc 由输入框转发调用 :meth:`move_up`/:meth:`move_down`/:meth:`hide_menu`。
    """

    activated = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SlashMenu")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setStyleSheet(
            f"#SlashMenu {{ background-color: {COLOR_BG_CARD};"
            f"border: 1px solid {COLOR_BORDER}; border-radius: 10px; }}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 6, 4, 6)
        root.setSpacing(2)

        title = QLabel("命令", self)
        title.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; background: transparent; border: none;"
            f"font-size: 11px; padding: 2px 8px;"
        )
        root.addWidget(title)

        self._list = QListWidget(self)
        self._list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._list.setStyleSheet(
            f"QListWidget {{ background: transparent; border: none; outline: none; }}"
            f"QListWidget::item {{ color: {COLOR_TEXT_PRIMARY}; background: transparent;"
            f"border-radius: 6px; padding: 5px 8px; }}"
            f"QListWidget::item:selected {{ background-color: {COLOR_BG_ACTIVE}; }}"
        )
        self._list.itemClicked.connect(lambda _item: self._activate_current())
        self._list.itemActivated.connect(lambda _item: self._activate_current())
        root.addWidget(self._list)

        hint = QLabel("ⓘ 输入内容以搜索命令，↑↓ 选择，回车执行，Esc 关闭", self)
        hint.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; background: transparent; border: none;"
            f"font-size: 11px; padding: 4px 8px 2px;"
        )
        root.addWidget(hint)

        self._commands: list[tuple[str, str]] = list(SLASH_COMMANDS)
        self.setFixedWidth(340)
        self.hide()

    # ---- 过滤与显示 ----

    def show_for(self, query: str) -> None:
        """按查询词（/ 后的文本）过滤命令并显示；无匹配保持隐藏。"""
        keyword = (query or "").strip().lstrip("/").strip()
        self._list.clear()
        for command, description in self._commands:
            if keyword and keyword not in command and keyword not in description:
                continue
            item = QListWidgetItem(f"{command}    {description}")
            item.setData(Qt.ItemDataRole.UserRole, command)
            self._list.addItem(item)
        if self._list.count() == 0:
            self.hide()
            return
        self._list.setCurrentRow(0)
        # 高度随内容自适应（上限约 8 行）
        item_height = self._list.sizeHintForRow(0) or 28
        self._list.setFixedHeight(min(self._list.count(), 8) * item_height + 4)
        self.adjustSize()
        self.show()
        self.raise_()

    def move_up(self) -> None:
        """上移选择。"""
        row = self._list.currentRow()
        if row > 0:
            self._list.setCurrentRow(row - 1)

    def move_down(self) -> None:
        """下移选择。"""
        row = self._list.currentRow()
        if row < self._list.count() - 1:
            self._list.setCurrentRow(row + 1)

    def activate_selected(self) -> bool:
        """回车执行当前选中项；返回是否执行了命令。"""
        item = self._list.currentItem()
        if item is None:
            return False
        command = item.data(Qt.ItemDataRole.UserRole) or ""
        self.hide()
        if command:
            self.activated.emit(str(command))
            return True
        return False

    def hide_menu(self) -> None:
        """关闭菜单。"""
        self.hide()

    def _activate_current(self) -> None:
        """点击列表项执行。"""
        self.activate_selected()
