"""记忆面板：全局长期记忆大输入框 + 编辑模式 + 保存。"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from minic.gui.panels.base import PanelBase
from minic.gui.theme import (
    COLOR_ACCENT,
    COLOR_BG_CARD,
    COLOR_BORDER,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_SECONDARY,
)
from minic.gui.widgets.worker import Worker


class MemoryPanel(PanelBase):
    """记忆面板：本地/核心读取全局长期记忆预填，编辑模式保存（mode=replace）。

    点「编辑」才可修改；未保存切走弹「是否保存」；点保存后回只读并通知条反馈。
    """

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
        self._saved_content = ""  # 已保存内容快照（放弃修改时回滚用）

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("记忆", self)
        title.setStyleSheet(
            f"color: #ffffff; font-size: 20px; font-weight: 600; background: transparent; border: none;"
        )
        header.addWidget(title)

        badge = QLabel("全局", self)
        badge.setStyleSheet(
            f"background-color: #37373d; color: {COLOR_TEXT_SECONDARY}; border-radius: 10px;"
            f"padding: 2px 10px; font-size: 12px;"
        )
        header.addWidget(badge)
        header.addStretch(1)
        # 编辑/保存按钮（右上角，同其他面板风格）
        self._make_edit_button(header)
        root.addLayout(header)

        desc = QLabel("管理全局长期记忆（~/.minic/memory/minic.md）。点右上角「编辑」后修改，保存才写入记忆文件。", self)
        desc.setWordWrap(True)
        desc.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 12px; background: transparent; border: none;"
        )
        root.addWidget(desc)

        self._editor = QPlainTextEdit(self)
        self._editor.setPlaceholderText("# 姓名\n张三\n\n# 偏好\n中文交流，Windows")
        self._editor.setStyleSheet(
            f"QPlainTextEdit {{ background-color: {COLOR_BG_CARD}; color: #ffffff;"
            f"border: 1px solid {COLOR_BORDER}; border-radius: 10px; padding: 14px 16px;"
            f"font-family: Consolas, 'Cascadia Code', 'Courier New'; font-size: 13px;"
            f"line-height: 1.7; }}"
            f"QPlainTextEdit:focus {{ border-color: {COLOR_ACCENT}; }}"
        )
        self._editor.setMinimumHeight(360)
        root.addWidget(self._editor, 1)

        save_row = QHBoxLayout()
        save_row.addStretch(1)
        self._save_btn = QPushButton("保存", self)
        self._save_btn.setStyleSheet(
            f"QPushButton {{ background-color: {COLOR_ACCENT}; color: #0b1220; border: none;"
            f"border-radius: 6px; padding: 6px 18px; font-weight: 600; }}"
            f"QPushButton:hover {{ background-color: #6fd3ff; }}"
            f"QPushButton:pressed {{ background-color: #3d9bc4; }}"
        )
        self._save_btn.clicked.connect(self.save)
        save_row.addWidget(self._save_btn)
        root.addLayout(save_row)

        # 编辑模式：初始只读，点「编辑」才可修改/保存
        self.register_editable(self._editor, self._save_btn)

    def reload(self) -> None:
        """加载全局长期记忆：核心可用走 /memory，否则本地读 ~/.minic/memory/minic.md。"""
        if self.client is None or not getattr(self.client, "base_url", None):
            from minic.gui.local_data import local_memory

            self._on_memory_loaded(local_memory("global", self.workspace))
            return
        worker = Worker(lambda: self.client.get_memory(scope="global"), self)
        worker.completed.connect(self._on_memory_loaded)
        worker.failed.connect(lambda err: self.notify(f"读取记忆失败：{err}", "failed"))
        worker.start()

    def _on_memory_loaded(self, data: Any) -> None:
        """用 GET /memory 结果预填输入框。"""
        if isinstance(data, dict):
            content = str(data.get("content", "") or "")
            self._editor.setPlainText(content)
            self._saved_content = content

    def save(self) -> bool:
        """保存全局长期记忆（mode=replace），成功后回只读并通知反馈。"""
        content = self._editor.toPlainText().strip()
        if not content:
            self.notify("保存失败：长期记忆为空", "failed")
            return False
        if self.client is None or not getattr(self.client, "base_url", None):
            self.notify("保存失败：核心未运行", "failed")
            return False
        payload = {"scope": "global", "content": content, "mode": "replace"}
        worker = Worker(lambda: self.client.post_memory(payload), self)
        worker.completed.connect(
            lambda ok: (self.notify("已保存全局长期记忆", "success"), self._on_saved(content))
            if ok
            else self.notify("保存失败", "failed")
        )
        worker.failed.connect(lambda message: self.notify(f"保存失败：{message}", "failed"))
        worker.start()
        return True

    def _on_saved(self, content: str) -> None:
        """保存成功：更新快照并回只读。"""
        self._saved_content = content
        self.set_edit_mode(False)

    def on_discard(self) -> None:
        """放弃修改：回滚编辑器到已保存内容。"""
        self._editor.setPlainText(self._saved_content)
