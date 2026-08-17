"""ToggleSwitch：胶囊开关（开=天蓝），用 QCheckBox + 自绘实现。"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import QCheckBox

from minic.gui.theme import COLOR_ACCENT


class ToggleSwitch(QCheckBox):
    """胶囊开关。

    与 QCheckBox 用法一致（``isChecked``/``setChecked``/``toggled`` 信号）。
    关闭时深灰胶囊白点靠左；开启时天蓝胶囊白点靠右。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(38, 20)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # 隐藏系统自绘指示器，完全走 paintEvent
        self.setStyleSheet("QCheckBox { background: transparent; }")

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt 接口命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0, 0, 38, 20)
        background = QColor(COLOR_ACCENT) if self.isChecked() else QColor("#3a3a3a")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(background)
        painter.drawRoundedRect(rect, 10, 10)
        knob_x = 20 if self.isChecked() else 2
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(QRectF(knob_x, 2, 16, 16))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt 接口命名
        """点击切换开关状态。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.setChecked(not self.isChecked())
            event.accept()
            return
        super().mouseReleaseEvent(event)
