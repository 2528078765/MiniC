"""Notification：顶部滑入通知条（成功绿/失败红，3 秒自动消失，内容自适应+省略）。"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

_SUCCESS = ("#17351f", "#4caf50")  # (背景, 边框) 成功绿
_FAILED = ("#3b1a1a", "#e81123")  # (背景, 边框) 失败红


class Notification(QWidget):
    """顶部居中滑入的通知条。

    调用 :meth:`show_notice` 显示：从窗口顶部滑入，3 秒后自动滑出；
    背景/边框颜色按成功/失败区分；宽度随内容自适应（160~560px），
    超长文本以省略号截断。
    """

    _MIN_W = 160  # 最小宽度
    _MAX_W = 560  # 最大宽度

    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("MiniNotify")
        self.setFixedHeight(38)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 16, 0)
        self._label = QLabel(self)
        self._label.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(self._label)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._slide_out)

        self._animation: QPropertyAnimation | None = None
        self.hide()

    def show_notice(self, text: str, kind: str = "success", duration_ms: int = 3000) -> None:
        """弹出通知：kind=success/failed；duration_ms 后自动消失。"""
        bg, border = _SUCCESS if kind == "success" else _FAILED
        self.setStyleSheet(
            f"#MiniNotify {{ background-color: {bg}; border: 1px solid {border};"
            f"border-radius: 8px; }}"
            f"#MiniNotify QLabel {{ color: #ffffff; font-size: 13px; }}"
        )
        # 超长文本省略号截断
        metrics = self._label.fontMetrics()
        elided = metrics.elidedText(
            text, Qt.TextElideMode.ElideRight, self._MAX_W - 40
        )
        self._label.setText(elided)
        # 宽度随内容自适应（160~560）
        width = min(max(metrics.horizontalAdvance(elided) + 40, self._MIN_W), self._MAX_W)
        self.setFixedWidth(width)
        self._position_top()
        self.show()
        self.raise_()
        self._slide_in()
        self._timer.start(duration_ms)

    def _position_top(self) -> None:
        """定位到宿主窗口顶部居中。"""
        host = self.parentWidget()
        if host is None:
            return
        x = max(8, (host.width() - self.width()) // 2)
        self.move(x, -self.height() - 4)

    def _slide_in(self) -> None:
        """从顶部滑入。"""
        start = self.pos()
        end = QPoint(self.x(), 10)
        self._animation = QPropertyAnimation(self, b"pos", self)
        self._animation.setDuration(220)
        self._animation.setStartValue(start)
        self._animation.setEndValue(end)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.start()

    def _slide_out(self) -> None:
        """向上滑出后隐藏。"""
        start = self.pos()
        end = QPoint(self.x(), -self.height() - 4)
        self._animation = QPropertyAnimation(self, b"pos", self)
        self._animation.setDuration(180)
        self._animation.setStartValue(start)
        self._animation.setEndValue(end)
        self._animation.setEasingCurve(QEasingCurve.Type.InCubic)
        self._animation.finished.connect(self.hide)
        self._animation.start()
