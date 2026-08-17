"""SpinnerLabel：转圈加载指示（定时轮换旋转字符，天蓝色）。"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QWidget

from minic.gui.theme import COLOR_ACCENT

_FRAMES = ("◐", "◓", "◑", "◒")


class SpinnerLabel(QLabel):
    """转圈指示标签。

    调用 :meth:`start` 开始旋转并显示；:meth:`stop` 停止并隐藏。
    用于工具执行中 / AI 输出中的进行态提示。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"color: {COLOR_ACCENT}; background: transparent; border: none;")
        self._timer = QTimer(self)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._advance)
        self._index = 0
        self.hide()

    def start(self) -> None:
        """开始旋转。"""
        self._index = 0
        self.setText(_FRAMES[0])
        self.show()
        self._timer.start()

    def stop(self) -> None:
        """停止旋转并隐藏。"""
        self._timer.stop()
        self.hide()

    def _advance(self) -> None:
        """轮换到下一帧。"""
        self._index = (self._index + 1) % len(_FRAMES)
        self.setText(_FRAMES[self._index])
