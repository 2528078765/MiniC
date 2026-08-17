"""Toast 右下角浮层提示：天蓝左边框，3 秒自动消失。"""

from __future__ import annotations

from PySide6.QtCore import QPropertyAnimation, QRect, Qt, QTimer
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from minic.gui.theme import COLOR_ACCENT, COLOR_BG_CARD, COLOR_BORDER


class Toast(QWidget):
    """右下角浮层提示。

    附着在宿主窗口上（不单独建窗口），调用 :meth:`show_message` 弹出，
    3 秒后自动淡出消失。同一宿主复用同一 Toast 实例。
    """

    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("MiniToast")
        self.setStyleSheet(
            f"#MiniToast {{"
            f"  background-color: {COLOR_BG_CARD};"
            f"  color: #ffffff;"
            f"  border: 1px solid {COLOR_BORDER};"
            f"  border-left: 3px solid {COLOR_ACCENT};"
            f"  border-radius: 8px;"
            f"}}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 18, 10)
        self._label = QLabel(self)
        self._label.setStyleSheet("background: transparent; border: none;")
        self._label.setWordWrap(True)
        self._label.setMaximumWidth(360)
        layout.addWidget(self._label)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._hide)

        self._animation: QPropertyAnimation | None = None
        self.hide()

    def show_message(self, message: str) -> None:
        """提示入口（已按用户要求禁用：不再显示右下角浮层）。

        保留调用点与接口，未来如需恢复删除下方 ``return`` 即可。
        """
        return
        self._label.setText(message)
        self.adjustSize()
        self._reposition()
        self.show()
        self.raise_()
        self._timer.start(3000)

    def _reposition(self) -> None:
        """定位到宿主窗口右下角。"""
        host = self.parentWidget()
        if host is None:
            return
        x = max(8, host.width() - self.width() - 20)
        y = max(8, host.height() - self.height() - 20)
        self.move(x, y)

    def _hide(self) -> None:
        """淡出并隐藏。"""
        if self.isHidden():
            return
        self._animation = QPropertyAnimation(self, b"windowOpacity", self)
        self._animation.setDuration(200)
        self._animation.setStartValue(self.windowOpacity())
        self._animation.setEndValue(0.0)
        self._animation.finished.connect(self.hide)
        self._animation.start()
