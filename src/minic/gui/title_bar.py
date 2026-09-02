"""自定义标题栏：左侧标题 + 右侧窗口控制（最小化/最大化·恢复/关闭图标），支持鼠标拖动窗口。"""

from __future__ import annotations

from PySide6 import QtCore
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QPushButton, QWidget

from minic.gui.icons import load_icon
from minic.gui.theme import (
    COLOR_BG_SIDEBAR,
    COLOR_BORDER,
    COLOR_RED,
    COLOR_TEXT_PRIMARY,
)


class TitleBar(QWidget):
    """无边框窗口标题栏。

    高度 42px，背景 ``#252526``；左侧标题文本，右侧 最小化/最大化·恢复/关闭。
    按住空白处可拖动窗口。
    """

    def __init__(self, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(42)
        self.setObjectName("MiniTitleBar")
        self.setStyleSheet(
            f"#MiniTitleBar {{ background-color: {COLOR_BG_SIDEBAR};"
            f"border-bottom: 1px solid {COLOR_BORDER}; }}"
        )
        self._drag_offset: QPoint | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 10, 0)
        layout.setSpacing(10)

        self._title_label = QLabel(title, self)
        self._title_label.setStyleSheet(
            f"color: {COLOR_TEXT_PRIMARY}; font-weight: 600; background: transparent; border: none;"
        )
        layout.addWidget(self._title_label)

        # 入库进度条：平时隐藏，知识库加载时显示
        self._progress = QProgressBar(self)
        self._progress.setRange(0, 0)  # 不确定进度（转圈）
        self._progress.setFixedWidth(140)
        self._progress.setTextVisible(False)
        self._progress.setStyleSheet(
            "QProgressBar { background: #2a2d2e; border: none; border-radius: 4px; }"
            "QProgressBar::chunk { background-color: #4fc3f7; border-radius: 4px; }"
        )
        self._progress.hide()
        layout.addWidget(self._progress)

        layout.addStretch(1)

        self._min_btn = self._make_control("最小化", "最小化", self._on_minimize)
        self._max_btn = self._make_control("最大化", "最大化", self._on_maximize)
        self._close_btn = self._make_control("关闭", "关闭", self._on_close)
        self._close_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; border-radius: 4px; }}"
            f"QPushButton:hover {{ background-color: {COLOR_RED}; }}"
        )

        layout.addWidget(self._min_btn)
        layout.addWidget(self._max_btn)
        layout.addWidget(self._close_btn)

    def _make_control(self, icon_name: str, tooltip: str, handler) -> QPushButton:
        """构造窗口控制按钮（图标来自 MiniC/icon/）。"""
        button = QPushButton(self)
        button.setIcon(load_icon(icon_name))
        button.setIconSize(QtCore.QSize(14, 14))
        button.setToolTip(tooltip)
        button.setFixedSize(40, 32)
        button.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; border-radius: 4px; }}"
            f"QPushButton:hover {{ background-color: #3a3a3a; }}"
        )
        button.clicked.connect(handler)
        return button

    def set_title(self, title: str) -> None:
        """更新标题文本。"""
        self._title_label.setText(title)

    def add_left_widget(self, widget: QWidget) -> None:
        """在标题与伸缩之间插入左侧控件（如设置页返回按钮）。"""
        self.layout().insertWidget(1, widget)

    def set_progress_visible(self, visible: bool) -> None:
        """显示/隐藏入库进度条（知识库加载期间可见）。"""
        self._progress.setVisible(visible)

    # ---- 窗口控制 ----

    def _on_minimize(self) -> None:
        window = self.window()
        if window is not None:
            window.showMinimized()

    def _on_maximize(self) -> None:
        window = self.window()
        if window is None:
            return
        if window.isMaximized():
            window.showNormal()
            self._max_btn.setIcon(load_icon("最大化"))
            self._max_btn.setToolTip("最大化")
        else:
            window.showMaximized()
            self._max_btn.setIcon(load_icon("恢复"))
            self._max_btn.setToolTip("恢复")

    def _on_close(self) -> None:
        window = self.window()
        if window is not None:
            window.close()

    # ---- 窗口拖动 ----

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt 接口命名
        """记录拖动起点。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt 接口命名
        """拖动窗口。"""
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            window = self.window()
            window.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt 接口命名
        """结束拖动。"""
        self._drag_offset = None
        super().mouseReleaseEvent(event)
