"""弹窗：审批弹窗（ApprovalDialog）、配置弹窗（ConfigDialog）、简单输入弹窗（InputDialog）。"""

from __future__ import annotations

import json
from typing import Any

from PySide6 import QtCore
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from minic.gui.icons import load_icon
from minic.gui.theme import (
    COLOR_ACCENT,
    COLOR_BG_CARD,
    COLOR_BG_MAIN,
    COLOR_BORDER,
    COLOR_GREEN,
    COLOR_RED,
    COLOR_SHIELD,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_SECONDARY,
)

_DECISION_LABELS: dict[str, str] = {
    "allow_once": "允许一次",
    "allow_always": "始终允许",
    "allow_session": "本次会话允许",
    "deny": "拒绝",
}


def _fmt_args(args: dict[str, Any] | None) -> str:
    """把参数格式化为可读文本。"""
    if not args:
        return "（无参数）"
    return json.dumps(args, ensure_ascii=False, indent=1)


class MiniDialog(QDialog):
    """统一风格弹窗基类：无边框（去掉系统白色标题栏）、深色圆角、Esc 关闭。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("MiniDialog")
        self.setStyleSheet(
            f"#MiniDialog {{ background-color: {COLOR_BG_CARD};"
            f"border: 1px solid {COLOR_BORDER}; border-radius: 10px; }}"
        )

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 接口命名
        """Esc 关闭弹窗。"""
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)


class SavePromptDialog(MiniDialog):
    """未保存更改提示：否（不保存）/ 是（保存），两个按钮都会关闭弹窗。

    返回 ``choice``："save"（保存）或 "discard"（不保存）。
    """

    def __init__(self, message: str = "当前面板有未保存的更改，是否保存？", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("未保存的更改")
        self.setModal(True)
        self.setMinimumWidth(380)
        self.choice: str = "discard"

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)

        title = QLabel("未保存的更改", self)
        title.setStyleSheet(
            f"color: #ffffff; font-size: 15px; font-weight: 600;"
            f"background: transparent; border: none;"
        )
        root.addWidget(title)

        msg_label = QLabel(message, self)
        msg_label.setTextFormat(Qt.TextFormat.PlainText)
        msg_label.setWordWrap(True)
        msg_label.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; background: transparent;"
            f"border: none; font-size: 13px;"
        )
        root.addWidget(msg_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)

        no_btn = QPushButton("否", self)
        no_btn.setStyleSheet(
            f"background-color: {COLOR_BG_CARD}; color: {COLOR_TEXT_SECONDARY};"
            f"border: 1px solid {COLOR_BORDER}; border-radius: 6px; padding: 6px 16px;"
        )
        no_btn.clicked.connect(self._choose_discard)
        buttons.addWidget(no_btn)

        yes_btn = QPushButton("是", self)
        yes_btn.setStyleSheet(
            f"QPushButton {{ background-color: {COLOR_GREEN}; color: #0b1220;"
            f"border: none; border-radius: 6px; padding: 6px 16px; font-weight: 600; }}"
            f"QPushButton:hover {{ background-color: #66d16a; }}"
            f"QPushButton:pressed {{ background-color: #2e7d32; }}"
        )
        yes_btn.clicked.connect(self._choose_save)
        buttons.addWidget(yes_btn)

        root.addLayout(buttons)

    def _choose_save(self) -> None:
        """选择保存。"""
        self.choice = "save"
        self.accept()

    def _choose_discard(self) -> None:
        """选择不保存。"""
        self.choice = "discard"
        self.accept()


class ConfirmDialog(MiniDialog):
    """确认弹窗：提示文字 + 取消 / 确认（红色危险操作）。

    返回 ``confirmed``：点确认后为 True。
    """

    def __init__(
        self,
        message: str,
        confirm_text: str = "删除",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("确认")
        self.setModal(True)
        self.setMinimumWidth(360)
        self.confirmed = False

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)

        title = QLabel("确认操作", self)
        title.setStyleSheet(
            f"color: #ffffff; font-size: 15px; font-weight: 600;"
            f"background: transparent; border: none;"
        )
        root.addWidget(title)

        msg_label = QLabel(message, self)
        msg_label.setTextFormat(Qt.TextFormat.PlainText)
        msg_label.setWordWrap(True)
        msg_label.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; background: transparent;"
            f"border: none; font-size: 13px;"
        )
        root.addWidget(msg_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)

        cancel_btn = QPushButton("取消", self)
        cancel_btn.setStyleSheet(
            f"background-color: {COLOR_BG_CARD}; color: {COLOR_TEXT_SECONDARY};"
            f"border: 1px solid {COLOR_BORDER}; border-radius: 6px; padding: 6px 16px;"
        )
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(cancel_btn)

        confirm_btn = QPushButton(confirm_text, self)
        confirm_btn.setStyleSheet(
            f"QPushButton {{ background-color: {COLOR_RED}; color: #ffffff;"
            f"border: none; border-radius: 6px; padding: 6px 16px; font-weight: 600; }}"
            f"QPushButton:hover {{ background-color: #ff3b4a; }}"
            f"QPushButton:pressed {{ background-color: #a30f1a; }}"
        )
        confirm_btn.clicked.connect(self._confirm)
        buttons.addWidget(confirm_btn)

        root.addLayout(buttons)

    def _confirm(self) -> None:
        """记录确认并关闭。"""
        self.confirmed = True
        self.accept()


class ApprovalDialog(MiniDialog):
    """审批弹窗：显示工具名、参数、message；按钮按 options 动态生成。

    返回值：``decision``（allow_once / allow_always / allow_session / deny）；
    Bash 只提供 允许一次 / 拒绝。
    """

    def __init__(
        self,
        tool: str,
        args: dict[str, Any] | None,
        message: str | None = None,
        options: list[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("工具审批")
        self.setModal(True)
        self.setMinimumWidth(420)
        self.decision: str | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(10)

        title = QLabel("⚠️ 需要你的批准", self)
        title.setStyleSheet(
            f"color: {COLOR_SHIELD}; font-size: 15px; font-weight: 600; background: transparent; border: none;"
        )
        root.addWidget(title)

        tool_label = QLabel(f"工具：<b>{tool}</b>", self)
        tool_label.setTextFormat(Qt.TextFormat.RichText)
        tool_label.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; background: transparent; border: none;")
        root.addWidget(tool_label)

        args_box = QLabel(_fmt_args(args), self)
        args_box.setTextFormat(Qt.TextFormat.PlainText)
        args_box.setWordWrap(True)
        args_box.setStyleSheet(
            f"background-color: {COLOR_BG_MAIN}; color: {COLOR_TEXT_SECONDARY};"
            f"border: 1px solid {COLOR_BORDER}; border-radius: 6px; padding: 8px 10px;"
            f"font-family: Consolas, 'Cascadia Code', 'Courier New'; font-size: 12px;"
        )
        root.addWidget(args_box)

        if message:
            msg_label = QLabel(message, self)
            msg_label.setTextFormat(Qt.TextFormat.PlainText)
            msg_label.setWordWrap(True)
            msg_label.setStyleSheet(
                f"color: {COLOR_TEXT_SECONDARY}; background: transparent; border: none; font-size: 12px;"
            )
            root.addWidget(msg_label)

        hint = QLabel("本地核心将执行此操作，请确认是否符合预期。", self)
        hint.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; background: transparent; border: none; font-size: 11px;"
        )
        root.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)

        buttons.addStretch(1)

        # 按钮全部由 options 生成（核心 options 已含 deny，不再额外固定拒绝按钮，
        # 避免出现两个「拒绝」）；deny 排最后、红色，允许类绿色
        options = list(options or ["allow_once", "deny"])
        if "deny" not in options:
            options.append("deny")
        for option in options:
            label = _DECISION_LABELS.get(option, option)
            button = QPushButton(label, self)
            if option == "deny":
                button.setStyleSheet(
                    f"background-color: {COLOR_BG_CARD}; color: {COLOR_RED};"
                    f"border: 1px solid {COLOR_BORDER}; border-radius: 6px; padding: 6px 16px;"
                )
            elif option == "allow_always":
                button.setStyleSheet(
                    f"background-color: {COLOR_BG_CARD}; color: {COLOR_ACCENT};"
                    f"border: 1px solid {COLOR_ACCENT}; border-radius: 6px; padding: 6px 16px;"
                )
            else:
                button.setStyleSheet(
                    f"background-color: {COLOR_GREEN}; color: #0b1220;"
                    f"border: none; border-radius: 6px; padding: 6px 16px; font-weight: 600;"
                )
            button.clicked.connect(lambda _=False, o=option: self._choose(o))
            buttons.addWidget(button)

        root.addLayout(buttons)

    def _choose(self, decision: str) -> None:
        """记录决策并关闭。"""
        self.decision = decision
        self.accept()


class ConfigDialog(MiniDialog):
    """配置弹窗：知识库路径（多目录）/ 数据目录（单目录）。

    目录列表删除按钮在确认后发 ``path_deleted`` 信号，
    由调用方负责删除该路径在 RAG 库中已入库的文档。
    """

    path_deleted = Signal(str)  # 参数：被删除的知识库路径
    """配置弹窗，两种模式：

    - ``multi_dir``（知识库路径）：输入框 + 选择目录 + 选择文件 + 「＋ 增加」列表，保存提交列表。
    - ``single_dir``（数据目录）：仅「全局」输入框 + 选择目录；项目目录固定说明。

    返回结果：multi_dir → ``{"paths": [...], "global_dir": None}``；
    single_dir → ``{"paths": [], "global_dir": str|None}``。
    """

    def __init__(
        self,
        title: str,
        mode: str = "multi_dir",
        initial_paths: list[str] | None = None,
        initial_global: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(440)
        self.result_payload: dict[str, Any] = {"paths": [], "global_dir": None}

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(10)

        title_label = QLabel(title, self)
        title_label.setStyleSheet(
            f"color: #ffffff; font-size: 15px; font-weight: 600; background: transparent; border: none;"
        )
        root.addWidget(title_label)

        self._mode = mode
        self._paths: list[str] = list(initial_paths or [])

        if mode == "multi_dir":
            self._build_multi(root)
        else:
            self._build_single(root, initial_global)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        cancel_btn = QPushButton("取消", self)
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("保存", self)
        save_btn.setStyleSheet(
            f"QPushButton {{ background-color: {COLOR_ACCENT}; color: #0b1220; border: none;"
            f"border-radius: 6px; padding: 6px 16px; font-weight: 600; }}"
            f"QPushButton:hover {{ background-color: #6fd3ff; }}"
            f"QPushButton:pressed {{ background-color: #3d9bc4; }}"
        )
        save_btn.clicked.connect(self._on_save)
        actions.addStretch(1)
        actions.addWidget(cancel_btn)
        actions.addWidget(save_btn)
        root.addLayout(actions)

    # ---- 模式 1：多目录 ----

    def _build_multi(self, root: QVBoxLayout) -> None:
        """多目录模式：输入 + 选择目录/文件 + 增加列表。"""
        row = QHBoxLayout()
        self._input = QLineEdit(self)
        self._input.setPlaceholderText("输入目录或文件路径…")
        self._input.setStyleSheet(
            f"background-color: {COLOR_BG_CARD}; color: #ffffff;"
            f"border: 1px solid {COLOR_BORDER}; border-radius: 6px; padding: 6px 10px;"
        )
        row.addWidget(self._input, 1)

        browse_dir = QPushButton("选择目录", self)
        browse_dir.clicked.connect(self._pick_directory)
        row.addWidget(browse_dir)

        browse_file = QPushButton("选择文件", self)
        browse_file.clicked.connect(self._pick_file)
        row.addWidget(browse_file)
        root.addLayout(row)

        add_btn = QPushButton("＋ 增加", self)
        add_btn.clicked.connect(self._add_path)
        root.addWidget(add_btn)

        self._list_scroll = QScrollArea(self)
        self._list_scroll.setWidgetResizable(True)
        self._list_scroll.setStyleSheet(
            f"QScrollArea {{ background: transparent; border: none; }}"
            f"QScrollArea > QWidget > QWidget {{ background: transparent; }}"
        )
        self._list_container = QWidget()
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(6)
        self._list_layout.addStretch(1)
        self._list_scroll.setWidget(self._list_container)
        self._list_scroll.setFixedHeight(160)
        root.addWidget(self._list_scroll)
        self._render_list()

    def _add_path(self) -> None:
        """把输入框内容加入列表。"""
        value = self._input.text().strip()
        if not value:
            return
        if value not in self._paths:
            self._paths.append(value)
            self._render_list()
        self._input.clear()

    def _pick_directory(self) -> None:
        """选择目录并填入列表。"""
        directory = QFileDialog.getExistingDirectory(self, "选择目录")
        if directory:
            if self._mode == "multi_dir":
                self._input.setText(directory)
                self._add_path()
            else:
                self._global_input.setText(directory)

    def _pick_file(self) -> None:
        """选择文件并填入列表。"""
        file_path, _ = QFileDialog.getOpenFileName(self, "选择文件")
        if file_path:
            if self._mode == "multi_dir":
                self._input.setText(file_path)
                self._add_path()

    def _render_list(self) -> None:
        """重新渲染目录列表。"""
        # 清空已有条目（保留末尾 stretch）：布局项内部的子 widget 也要删除，
        # 否则孤儿 widget 仍挂在容器上显示，看起来像「删不掉」
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            layout = item.layout()
            if layout is not None:
                while layout.count():
                    sub = layout.takeAt(0)
                    widget = sub.widget()
                    if widget is not None:
                        widget.deleteLater()
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for path in self._paths:
            item = QHBoxLayout()
            label = QLabel(path, self._list_container)
            label.setStyleSheet(
                f"background-color: {COLOR_BG_MAIN}; color: {COLOR_TEXT_SECONDARY};"
                f"border: 1px solid {COLOR_BORDER}; border-radius: 6px; padding: 5px 10px;"
                f"font-size: 12px;"
            )
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            item.addWidget(label, 1)
            remove_btn = QPushButton(self._list_container)
            remove_btn.setIcon(load_icon("删 除"))
            remove_btn.setIconSize(QtCore.QSize(12, 12))
            remove_btn.setFixedSize(24, 24)
            remove_btn.setStyleSheet(
                f"background: transparent; border: none; border-radius: 4px;"
            )
            remove_btn.clicked.connect(lambda _=False, p=path: self._confirm_remove(p))
            item.addWidget(remove_btn)
            self._list_layout.insertLayout(self._list_layout.count() - 1, item)

    def _confirm_remove(self, path: str) -> None:
        """确认后从列表移除路径，并通知调用方删除库中已入库文档。"""
        dialog = ConfirmDialog(
            f"是否要删除知识库：{path}？\n删除后该路径下已入库的文档会从知识库中移除。",
            confirm_text="删除",
            parent=self,
        )
        dialog.exec()
        if not dialog.confirmed:
            return
        if path in self._paths:
            self._paths.remove(path)
            self._render_list()
        self.path_deleted.emit(path)

    # ---- 模式 2：单目录 ----

    def _build_single(self, root: QVBoxLayout, initial_global: str | None) -> None:
        """单目录模式：全局输入 + 选择目录；项目固定说明。"""
        row = QHBoxLayout()
        label = QLabel("全局", self)
        label.setFixedWidth(44)
        label.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; background: transparent; border: none;")
        row.addWidget(label)

        self._global_input = QLineEdit(self)
        self._global_input.setPlaceholderText("缺省 ~/.minic/rag-data")
        self._global_input.setText(initial_global or "")
        self._global_input.setStyleSheet(
            f"background-color: {COLOR_BG_CARD}; color: #ffffff;"
            f"border: 1px solid {COLOR_BORDER}; border-radius: 6px; padding: 6px 10px;"
        )
        row.addWidget(self._global_input, 1)

        browse_btn = QPushButton("选择目录", self)
        browse_btn.clicked.connect(self._pick_directory)
        row.addWidget(browse_btn)
        root.addLayout(row)

        note = QLabel("项目数据目录固定于 <项目>/.minic/rag-data，不可修改；全局可配置。", self)
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; background: transparent; border: none; font-size: 11px;")
        root.addWidget(note)

    # ---- 保存 ----

    def _on_save(self) -> None:
        """按模式收集结果。"""
        if self._mode == "multi_dir":
            current = self._input.text().strip()
            if current and current not in self._paths:
                self._paths.append(current)
            self.result_payload = {"paths": list(self._paths), "global_dir": None}
        else:
            value = self._global_input.text().strip()
            self.result_payload = {"paths": [], "global_dir": value or None}
        self.accept()


class InputDialog(MiniDialog):
    """简单输入弹窗：新建项目/会话等。"""

    def __init__(
        self,
        title: str,
        label: str,
        placeholder: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(360)
        self.value: str | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(10)

        label_widget = QLabel(label, self)
        label_widget.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; background: transparent; border: none;"
        )
        root.addWidget(label_widget)

        self._input = QLineEdit(self)
        self._input.setPlaceholderText(placeholder)
        self._input.setStyleSheet(
            f"background-color: {COLOR_BG_CARD}; color: #ffffff;"
            f"border: 1px solid {COLOR_BORDER}; border-radius: 6px; padding: 6px 10px;"
        )
        root.addWidget(self._input)

        actions = QHBoxLayout()
        cancel_btn = QPushButton("取消", self)
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("确定", self)
        ok_btn.setStyleSheet(
            f"background-color: {COLOR_ACCENT}; color: #0b1220; border: none;"
            f"border-radius: 6px; padding: 6px 16px; font-weight: 600;"
        )
        ok_btn.clicked.connect(self._on_ok)
        actions.addStretch(1)
        actions.addWidget(cancel_btn)
        actions.addWidget(ok_btn)
        root.addLayout(actions)

    def _on_ok(self) -> None:
        """收集输入并关闭。"""
        self.value = self._input.text().strip()
        self.accept()
