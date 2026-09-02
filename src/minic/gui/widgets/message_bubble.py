"""MessageBubble：用户/AI 两种消息气泡（长文本自动换行展高，不裁剪）。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QVBoxLayout, QWidget

from minic.gui.theme import COLOR_BG_ACTIVE, COLOR_TEXT_PRIMARY, COLOR_TEXT_SECONDARY
from minic.gui.widgets.spinner import SpinnerLabel


class _WrapLabel(QLabel):
    """wordWrap 标签：宽度/文本变化时按实际换行高度自扩展。

    Qt 的布局在 AlignTop 下不查询 heightForWidth，长文本高度会被钳在
    sizeHint 导致底部被裁；这里用 QTextDocument（与 QLabel wordWrap 渲染
    同机制）精确计算所需高度并设为 minimumHeight。
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setWordWrap(True)

    def _update_height(self) -> None:
        """按当前宽度与文本重算所需高度（QTextDocument 布局）。"""
        doc = QTextDocument()
        doc.setDefaultFont(self.font())
        doc.setTextWidth(max(self.width(), 1))
        doc.setPlainText(self.text())
        self.setMinimumHeight(int(doc.size().height()) + 1)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt 接口命名
        """wordWrap 时 Qt 的 sizeHint 宽度偏窄（短文本也强制换行），
        改为按单行全文宽度返回（不超过 maximumWidth），气泡不再过窄。"""
        hint = super().sizeHint()  # 原始提示尺寸
        width = self.fontMetrics().horizontalAdvance(self.text()) + 4  # 单行文本宽
        max_w = self.maximumWidth()  # 宽度上限
        if max_w < 16777215:  # 设了上限时取小
            width = min(width, max_w)  # 不超过上限
        return QSize(width, hint.height())  # 高度保持原提示

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 接口命名
        """宽度变化时重算高度。"""
        super().resizeEvent(event)
        self._update_height()

    def setText(self, text: str) -> None:  # noqa: N802 - Qt 接口命名
        """文本变化时重算高度。"""
        super().setText(text)
        self._update_height()


class MessageBubble(QWidget):
    """消息气泡。

    - 用户消息：右对齐，背景 ``#37373d`` 圆角块（QFrame 背景 + 布局 padding）。
    - AI 消息：左对齐，透明背景。

    支持 :meth:`append_text` 追加流式 token（增量渲染）；长文本按换行
    自动增高，显示完整内容。
    """

    def __init__(
        self,
        role: str,
        text: str = "",
        parent: QWidget | None = None,
        max_width: int = 820,
    ) -> None:
        super().__init__(parent)
        self._role = role
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 4)
        outer.setSpacing(0)

        align = Qt.AlignmentFlag.AlignRight if role == "user" else Qt.AlignmentFlag.AlignLeft
        outer.setAlignment(align)

        if role == "user":
            # 背景/圆角放 QFrame，padding 用布局边距（避免 QSS padding
            # 干扰高度计算），内层 QLabel 透明无 padding
            self._frame = QFrame(self)
            self._frame.setStyleSheet(
                f"background-color: {COLOR_BG_ACTIVE}; border-radius: 10px;"
            )
            frame_layout = QVBoxLayout(self._frame)
            frame_layout.setContentsMargins(14, 10, 14, 10)
            frame_layout.setSpacing(0)
            self._label = _WrapLabel(text, self._frame)
            self._label.setTextFormat(Qt.TextFormat.PlainText)
            self._label.setStyleSheet(
                f"color: {COLOR_TEXT_PRIMARY}; background: transparent; border: none;"
                f"font-size: 14px;"
            )
            frame_layout.addWidget(self._label)
            self._frame.setMaximumWidth(max_width)
            self._label.setMaximumWidth(max_width)
            outer.addWidget(self._frame)
        else:
            self._frame = None
            self._label = _WrapLabel(text, self)
            self._label.setTextFormat(Qt.TextFormat.PlainText)
            self._label.setStyleSheet(
                f"background: transparent; color: {COLOR_TEXT_SECONDARY};"
                f"font-size: 14px;"
            )
            self._label.setMaximumWidth(max_width)
            outer.addWidget(self._label)
            # AI 输出中的转圈指示（回答生成完自动停止）
            self._spinner = SpinnerLabel(self)
            outer.addWidget(self._spinner, 0, Qt.AlignmentFlag.AlignLeft)

        self._label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_streaming(self, streaming: bool) -> None:
        """AI 气泡：流式输出中显示转圈，输出结束停止隐藏。"""
        spinner = getattr(self, "_spinner", None)
        if spinner is None:
            return
        if streaming:
            spinner.start()
        else:
            spinner.stop()

    def append_text(self, delta: str) -> None:
        """追加流式 token 到 AI 气泡。"""
        self._label.setText(self._label.text() + delta)

    def set_text(self, text: str) -> None:
        """整体替换文本。"""
        self._label.setText(text)

    def text(self) -> str:
        """当前文本。"""
        return self._label.text()
