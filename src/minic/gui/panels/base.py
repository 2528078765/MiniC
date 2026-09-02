"""设置面板共享基础：SettingCard 卡片控件与通用构建辅助。"""

from __future__ import annotations

from typing import Callable

from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from minic.gui.theme import (
    COLOR_ACCENT,
    COLOR_BG_MAIN,
    COLOR_BORDER,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_SECONDARY,
)


class SettingCard(QFrame):
    """设置卡片：左文（标题 + 描述）+ 右侧控件区，可选整行内容区。"""

    def __init__(
        self,
        title: str = "",
        desc: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SettingCard")
        self.setStyleSheet(
            f"#SettingCard {{"
            f"  background-color: {COLOR_BG_MAIN};"
            f"  border: 1px solid {COLOR_BORDER};"
            f"  border-radius: 10px;"
            f"}}"
        )
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(16, 14, 16, 14)
        self._root.setSpacing(10)

        self._head_row = QHBoxLayout()
        self._head_row.setSpacing(16)

        self._text_box = QWidget(self)
        self._text_layout = QVBoxLayout(self._text_box)
        self._text_layout.setContentsMargins(0, 0, 0, 0)
        self._text_layout.setSpacing(3)
        self._title_label = QLabel(title, self._text_box)
        self._title_label.setStyleSheet(
            f"color: #ffffff; font-size: 14px; font-weight: 500; background: transparent; border: none;"
        )
        self._desc_label = QLabel(desc, self._text_box)
        self._desc_label.setWordWrap(True)
        self._desc_label.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 12px; background: transparent; border: none;"
        )
        self._text_layout.addWidget(self._title_label)
        self._text_layout.addWidget(self._desc_label)
        self._head_row.addWidget(self._text_box, 1)

        self._control_widget: QWidget | None = None
        self._root.addLayout(self._head_row)

    def set_title(self, title: str) -> None:
        """设置标题。"""
        self._title_label.setText(title)

    def set_desc(self, desc: str) -> None:
        """设置描述。"""
        self._desc_label.setText(desc)
        self._desc_label.setVisible(bool(desc))

    def add_control(self, widget: QWidget) -> None:
        """右侧控件区（单控件）。"""
        if self._control_widget is not None:
            return
        self._control_widget = widget
        self._head_row.addWidget(widget, 0)

    def add_body(self, widget: QWidget) -> None:
        """整行内容区（标题/描述下方）。"""
        self._root.addWidget(widget)

    def add_body_layout(self, layout) -> None:
        """整行布局（标题/描述下方）。"""
        self._root.addLayout(layout)


class PanelBase(QWidget):
    """设置面板基类：持有 CoreClient 与 toast 回调。

    子类实现 :meth:`reload`（有核心时拉取真实数据）；构造时 ``reload_on_show``
    控制进入面板时是否自动刷新。

    编辑模式：可编辑控件经 :meth:`register_editable` 注册（初始禁用），
    点面板「编辑」按钮后进入编辑态（``set_edit_mode(True)``）才可修改；
    进入编辑态即标记未保存（``_dirty``），:meth:`save` 保存后回到只读态。
    主窗口在面板未保存时返回会弹「是否保存」确认。
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
        self.client = client  # CoreClient 或 None（核心未运行时界面仍可用）
        self.toast = toast or (lambda message: None)
        self.workspace = workspace  # 当前工作区目录（本地配置降级读取用）
        self.notify = notify or (lambda text, kind="success": None)  # 顶部通知条（成功/失败）
        self.progress = progress or (lambda visible: None)  # 顶栏入库进度条
        self._reload_on_show = True
        self._has_loaded = False
        self._editable_widgets: list[QWidget] = []
        self._edit_mode = False
        self._dirty = False

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 接口命名
        """首次显示时触发 reload。"""
        super().showEvent(event)
        if self._reload_on_show and not self._has_loaded:
            self._has_loaded = True
            self.reload()

    def reload(self) -> None:
        """子类覆写：有核心时拉取真实数据。"""

    # ---- 编辑模式 ----

    def register_editable(self, *widgets: QWidget) -> None:
        """注册可编辑控件/按钮：初始禁用，仅编辑模式下启用。"""
        for widget in widgets:
            self._editable_widgets.append(widget)
            widget.setEnabled(False)

    def set_edit_mode(self, enabled: bool) -> None:
        """切换编辑态：启用/禁用注册控件，维护未保存标记。"""
        self._edit_mode = enabled
        self._dirty = enabled
        for widget in self._editable_widgets:
            widget.setEnabled(enabled)
        self.on_edit_mode_changed(enabled)

    def on_edit_mode_changed(self, enabled: bool) -> None:
        """子类覆写：编辑态切换时同步按钮文案等（需调 super 保持编辑按钮文案）。"""
        if hasattr(self, "_edit_btn"):
            self._edit_btn.setText("保存" if enabled else "编辑")

    def has_unsaved_changes(self) -> bool:
        """是否有未保存修改（编辑态即视为有）。"""
        return self._dirty

    def save(self) -> bool:
        """子类覆写：保存面板数据，返回是否已保存（False=校验失败未保存）。"""
        return False

    def discard(self) -> None:
        """放弃未保存修改：回滚界面值并回到只读态（清除未保存标记）。"""
        self.set_edit_mode(False)
        self.on_discard()

    def on_discard(self) -> None:
        """子类覆写：把界面控件回滚到已保存的值。"""

    def _make_edit_button(self, layout) -> QPushButton:
        """构造「编辑/保存」切换按钮：点击进入编辑态/触发保存。"""
        button = QPushButton("编辑")
        button.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {COLOR_ACCENT};"
            f"border: 1px solid {COLOR_ACCENT}; border-radius: 6px; padding: 4px 14px;"
            f"font-size: 12px; }}"
            f"QPushButton:hover {{ background-color: rgba(79, 195, 247, 38); }}"
            f"QPushButton:pressed {{ background-color: rgba(79, 195, 247, 77); }}"
        )
        button.clicked.connect(self._on_edit_button_clicked)
        self._edit_btn = button
        layout.addWidget(button)
        return button

    def _on_edit_button_clicked(self) -> None:
        """编辑按钮点击：未编辑 → 进入编辑态；编辑中 → 保存。"""
        if not self._edit_mode:
            self.set_edit_mode(True)
        else:
            self.save()


def group_title(text: str, parent: QWidget | None = None) -> QLabel:
    """分组标题标签。"""
    label = QLabel(text, parent)
    label.setStyleSheet(
        f"color: {COLOR_TEXT_SECONDARY}; font-size: 13px; font-weight: 500;"
        f"background: transparent; border: none; padding: 8px 2px 6px;"
    )
    return label


def sub_note(text: str, parent: QWidget | None = None) -> QLabel:
    """说明文字。"""
    label = QLabel(text, parent)
    label.setWordWrap(True)
    label.setStyleSheet(
        f"color: {COLOR_TEXT_MUTED}; font-size: 12px; background: transparent; border: none;"
    )
    return label
