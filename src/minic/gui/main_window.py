"""主窗口：左右结构（左侧通高侧栏 + 右侧对话区：顶栏/内容栈/输入栏）。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6 import QtCore
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from minic.gui.core_client import CoreClient
from minic.gui.icons import load_icon, load_pixmap
from minic.gui.settings_window import SettingsNavList, SettingsPage
from minic.gui.widgets.slash_menu import SlashMenu
from minic.graph.common import strip_dsml
from minic.gui.theme import (
    COLOR_ACCENT,
    COLOR_BG_ACTIVE,
    COLOR_BG_CARD,
    COLOR_BG_SIDEBAR,
    COLOR_BORDER,
    COLOR_GREEN,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_SECONDARY,
)
from minic.gui.title_bar import TitleBar
from minic.gui.widgets.dialogs import ApprovalDialog, ConfirmDialog, SavePromptDialog
from minic.gui.widgets.message_bubble import MessageBubble
from minic.gui.widgets.notification import Notification
from minic.gui.widgets.toast import Toast
from minic.gui.widgets.tool_card import ToolCard
from minic.gui.widgets.tool_group import ToolGroup
from minic.gui.widgets.worker import Worker


class EnterTextEdit(QTextEdit):
    """回车发送（Shift+回车换行）的输入框。

    ``slash_menu`` 属性由主窗口注入：菜单可见时上下键选择、
    回车执行命令、Esc 关闭；菜单不可见时回车发送。
    """

    send_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.slash_menu = None  # 斜杠命令浮层（SlashMenu | None）

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 接口命名
        """回车（无 Shift）触发发送；斜杠菜单可见时优先菜单导航。"""
        menu = self.slash_menu
        if menu is not None and menu.isVisible():
            if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                if event.key() == Qt.Key.Key_Up:
                    menu.move_up()
                else:
                    menu.move_down()
                return
            if event.key() == Qt.Key.Key_Escape:
                menu.hide_menu()
                return
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            ):
                if menu.activate_selected():  # 执行选中命令
                    return
                # 无匹配项时退回普通发送
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.send_requested.emit()
            return
        super().keyPressEvent(event)


class ModelComboBox(QComboBox):
    """模型下拉框：打开前发出通知，实时刷新 minic.json 中的模型配置。"""

    popup_opened = Signal()

    def showPopup(self) -> None:  # noqa: N802 - Qt 接口命名
        """展开下拉前刷新模型列表（设置面板新增的模型立即可选）。"""
        self.popup_opened.emit()
        super().showPopup()


def _greeting_by_time() -> str:
    """按当前时间返回问候语。"""
    hour = datetime.now().hour
    if hour < 6:
        return "夜深了，注意休息"
    if hour < 12:
        return "早上好呀，新的一天开始啦"
    if hour < 18:
        return "下午好，继续加油"
    return "晚上好，今天辛苦啦"


class MainWindow(QMainWindow):
    """MiniC 桌面端主窗口。"""

    def __init__(self, workspace: str | None = None) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
        )
        self.setMinimumSize(960, 640)
        self.resize(1180, 760)
        self.setObjectName("MainWindow")
        self.setStyleSheet(f"#MainWindow {{ background-color: #1e1e1e; }}")

        self.client = CoreClient(self)
        self.chat_thread_id: str | None = None
        self.workspace = str(workspace or Path.cwd())
        self._saved_model: str | None = None  # 持久化选中的模型配置名
        self._model_options: dict[str, str] = {}  # 下拉显示名 -> 配置名(name)
        self._chat_in_progress = False
        # 并行流：run_key（会话复合键 / "__welcome__"）→ 流状态
        # {bubble, session, saved, pending_text, pending_retried}
        self._streams: dict[str, dict[str, Any]] = {}
        self._tool_cards: dict[str, ToolCard] = {}
        self._approval_queue: list[dict[str, Any]] = []
        self._approval_dialog_open = False
        # 侧栏项目/会话数据（项目 = 工作区，管理多个会话）
        self._projects: dict[str, list[str]] = {}
        self._project_dirs: dict[str, str] = {}  # 工作区名 → 目录路径（工具/文件落在该目录）
        self._current_project: str | None = None
        # 会话隔离：会话名 → thread_id（None=核心尚未创建）/ 消息列表 / 当前会话
        self._session_threads: dict[str, str | None] = {}
        self._session_messages: dict[str, list[tuple[str, str]]] = {}
        self._current_session: str | None = None
        self._current_thread_id: str | None = None
        # 待发工作区：点击新建会话后等待第一句话创建会话选项卡
        self._pending_session_ws: str | None = None
        # 任务区：任务会话（直接包含会话，挂全局 ~/.minic/work）+ 待发任务
        self._task_sessions: list[str] = []
        self._pending_task = False
        # 设置页返回目标（0 欢迎页 / 1 对话流）
        self._prev_stack_index = 0
        # 唯一输入框当前挂载位置（welcome / bottom / None）
        self._input_mode: str | None = None

        # QMainWindow 必须用 setCentralWidget 承载布局（不能直接 setLayout）
        # 左右结构：左侧边栏通高 + 右侧对话区（顶栏 + 内容栈 + 输入栏）
        central_root = QWidget(self)
        central_layout = QHBoxLayout(central_root)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)

        self._toast_widget = Toast(self)
        self._toast = self._toast_widget.show_message
        self._notification = Notification(self)
        self._notify = self._notification.show_notice

        # 斜杠命令菜单（输入 / 时浮出，顶级浮窗；须在 _build_chat_area 之前创建）
        self._slash_menu = SlashMenu()
        self._slash_menu.activated.connect(self._run_slash_command)

        self._sidebar = self._build_sidebar(central_root)
        central_layout.addWidget(self._sidebar, 0)
        central_layout.addWidget(self._build_chat_area(central_root), 1)
        self.setCentralWidget(central_root)

        # 聊天 SSE 事件
        self.client.chat_event.connect(self._on_chat_event)
        self.client.chat_error.connect(self._on_chat_error)

        # 加载持久化状态（工作区/会话/消息）
        self._load_state()

        # 模型下拉框：读本地 minic.json 已添加的模型配置
        self._reload_models()

        # 核心连接（后台线程，失败不崩溃）
        self._connect_core()

    # ==================== 构建 UI ====================

    def _build_sidebar(self, parent: QWidget) -> QWidget:
        """构建左侧边栏：M logo（共用）+ 内容栈（主菜单 / 设置导航）。"""
        sidebar = QWidget(parent)
        sidebar.setFixedWidth(248)
        sidebar.setStyleSheet(f"background-color: {COLOR_BG_SIDEBAR};")

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(2)

        # ---- 顶部：Logo（Log 图标，主菜单页与设置导航页共用）----
        top_row = QHBoxLayout()
        top_row.setSpacing(6)

        logo = QLabel(sidebar)
        logo.setPixmap(load_pixmap("Log", 22))
        logo.setFixedSize(26, 26)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setStyleSheet("background: transparent; border: none;")
        top_row.addWidget(logo)
        top_row.addStretch(1)

        layout.addLayout(top_row)

        # ---- 侧栏内容栈：页 0 = 主菜单，页 1 = 设置导航 ----
        self._side_stack = QStackedWidget(sidebar)
        self._side_stack.addWidget(self._build_main_menu(sidebar))
        self._side_stack.addWidget(self._build_settings_nav(sidebar))
        layout.addWidget(self._side_stack, 1)

        return sidebar

    def _build_main_menu(self, parent: QWidget) -> QWidget:
        """侧栏页 0：菜单（新建任务/技能）+ 项目树 + 设置按钮。"""
        container = QWidget(parent)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # ---- 菜单 ----
        layout.addSpacing(4)
        for icon_name, label, kbd, handler in (
            ("新建", "新建任务", "Ctrl N", self._new_task),
            ("技能", "技能", "", self._open_skills),
        ):
            menu_btn = QPushButton(container)
            menu_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {COLOR_TEXT_SECONDARY};"
                f"border: none; border-radius: 6px; padding: 7px 10px; text-align: left;"
                f"font-size: 13px; }}"
                f"QPushButton:hover {{ background-color: #2a2d2e; color: #ffffff; }}"
            )
            menu_layout = QHBoxLayout(menu_btn)
            menu_layout.setContentsMargins(0, 0, 0, 0)
            menu_layout.setSpacing(8)
            # 图标用 pixmap QLabel（与文字并排，不重叠）
            icon_label = QLabel(menu_btn)
            icon_label.setPixmap(load_pixmap(icon_name, 16))
            icon_label.setFixedSize(16, 16)
            icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            icon_label.setStyleSheet("background: transparent; border: none;")
            menu_layout.addWidget(icon_label)
            text_label = QLabel(label, menu_btn)
            text_label.setStyleSheet("background: transparent; border: none;")
            menu_layout.addWidget(text_label)
            menu_layout.addStretch(1)
            if kbd:
                kbd_label = QLabel(kbd, menu_btn)
                kbd_label.setStyleSheet(
                    f"background: transparent; border: 1px solid {COLOR_BORDER};"
                    f"border-radius: 4px; padding: 0 5px; color: {COLOR_TEXT_MUTED};"
                    f"font-size: 11px;"
                )
                menu_layout.addWidget(kbd_label)
            menu_btn.clicked.connect(handler)
            layout.addWidget(menu_btn)

        # ---- 项目区：树（项目 → 工作区 → 会话）----
        layout.addSpacing(8)
        project_section = QWidget(container)
        ps_layout = QVBoxLayout(project_section)
        ps_layout.setContentsMargins(0, 0, 0, 0)
        ps_layout.setSpacing(4)

        # 项目分组标题行：标题 + ＋（新建项目）
        p_head = QHBoxLayout()
        p_title = QLabel("项目", project_section)
        p_title.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 12px; font-weight: 600;"
            f"background: transparent; border: none;"
        )
        p_head.addWidget(p_title)
        p_head.addStretch(1)
        p_add = QPushButton(project_section)
        p_add.setIcon(load_icon("新建项目 任务"))
        p_add.setIconSize(QtCore.QSize(14, 14))
        p_add.setFixedSize(24, 22)
        p_add.setToolTip("新建项目（选择工作区目录）")
        p_add.setStyleSheet(
            "QPushButton { background: transparent; border: none; border-radius: 4px; }"
            "QPushButton:hover { background-color: #2a2d2e; }"
        )
        p_add.clicked.connect(self._new_project)
        p_head.addWidget(p_add)
        ps_layout.addLayout(p_head)

        # 空状态
        self._project_empty = QLabel("还没有项目", project_section)
        self._project_empty.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 12px; background: transparent;"
            f"border: none; padding: 2px 10px;"
        )
        ps_layout.addWidget(self._project_empty)

        # 树：顶层=工作区（📁），子项=会话（📌）（不做拖动）
        # 高度按内容自适应（超出限高后滚动），让「任务」区紧挨「项目」区
        self._project_tree = QTreeWidget(project_section)
        self._project_tree.setHeaderHidden(True)
        self._project_tree.setIndentation(14)
        self._project_tree.setSizeAdjustPolicy(
            QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents
        )
        self._project_tree.setMaximumHeight(280)
        self._project_tree.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._project_tree.setStyleSheet(
            "QTreeWidget { background: transparent; border: none; outline: none; }"
            "QTreeWidget::item { padding: 2px 2px; border-radius: 6px; color: #cccccc; }"
            "QTreeWidget::item:hover { background-color: #2a2d2e; }"
            "QTreeWidget::item:selected { background-color: #37373d; color: #ffffff; }"
        )
        self._project_tree.itemClicked.connect(self._on_tree_item_clicked)
        ps_layout.addWidget(self._project_tree, 0)

        layout.addWidget(project_section, 0)

        # ---- 任务区：与项目平级，直接包含会话（挂全局 ~/.minic/work）----
        layout.addSpacing(8)
        task_section = QWidget(container)
        ts_layout = QVBoxLayout(task_section)
        ts_layout.setContentsMargins(0, 0, 0, 0)
        ts_layout.setSpacing(4)

        t_head = QHBoxLayout()
        t_title = QLabel("任务", task_section)
        t_title.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 12px; font-weight: 600;"
            f"background: transparent; border: none;"
        )
        t_head.addWidget(t_title)
        t_head.addStretch(1)
        t_add = QPushButton(task_section)
        t_add.setIcon(load_icon("新建项目 任务"))
        t_add.setIconSize(QtCore.QSize(14, 14))
        t_add.setFixedSize(24, 22)
        t_add.setToolTip("新建任务（保存到全局 work 目录）")
        t_add.setStyleSheet(
            "QPushButton { background: transparent; border: none; border-radius: 4px; }"
            "QPushButton:hover { background-color: #2a2d2e; }"
        )
        t_add.clicked.connect(self._new_task_pending)
        t_head.addWidget(t_add)
        ts_layout.addLayout(t_head)

        self._task_tree = QTreeWidget(task_section)
        self._task_tree.setHeaderHidden(True)
        self._task_tree.setIndentation(10)
        self._task_tree.setSizeAdjustPolicy(
            QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents
        )
        self._task_tree.setMaximumHeight(150)
        self._task_tree.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._task_tree.setStyleSheet(
            "QTreeWidget { background: transparent; border: none; outline: none; }"
            "QTreeWidget::item { padding: 2px 2px; border-radius: 6px; color: #cccccc; }"
            "QTreeWidget::item:hover { background-color: #2a2d2e; }"
            "QTreeWidget::item:selected { background-color: #37373d; color: #ffffff; }"
        )
        self._task_tree.itemClicked.connect(self._on_task_item_clicked)
        ts_layout.addWidget(self._task_tree)
        layout.addWidget(task_section, 0)

        layout.addStretch(1)  # 剩余空间推到设置按钮之上

        # ---- 底部设置（图标 / 设 / 置 三者间隔拉大，整体居中）----
        settings_btn = QPushButton(container)
        settings_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {COLOR_TEXT_SECONDARY};"
            f"border: none; border-top: 1px solid {COLOR_BORDER}; border-radius: 0;"
            f"padding: 10px; font-size: 13px; }}"
            f"QPushButton:hover {{ background-color: #2a2d2e; }}"
        )
        row = QHBoxLayout(settings_btn)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addStretch(1)
        icon = QLabel(settings_btn)
        icon.setPixmap(load_pixmap("设置", 16))
        icon.setFixedSize(16, 16)
        icon.setStyleSheet("background: transparent; border: none;")
        row.addWidget(icon)
        row.addSpacing(18)  # 图标与"设"间隔
        label1 = QLabel("设", settings_btn)
        label1.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; background: transparent; border: none; font-size: 13px;"
        )
        row.addWidget(label1)
        row.addSpacing(36)  # "设"与"置"间隔
        label2 = QLabel("置", settings_btn)
        label2.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; background: transparent; border: none; font-size: 13px;"
        )
        row.addWidget(label2)
        row.addStretch(1)
        settings_btn.clicked.connect(self._open_settings)
        layout.addWidget(settings_btn)

        return container

    def _build_settings_nav(self, parent: QWidget) -> QWidget:
        """侧栏页 1：← 返回工作区 + 设置导航分组（面板联动 SettingsPage）。"""
        container = QWidget(parent)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(4)

        back_btn = QPushButton("← 返回工作区", container)
        back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        back_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {COLOR_TEXT_SECONDARY};"
            f"border: none; border-radius: 6px; padding: 7px 10px; text-align: left;"
            f"font-size: 13px; }}"
            f"QPushButton:hover {{ background-color: #2a2d2e; color: #ffffff; }}"
        )
        back_btn.clicked.connect(self._close_settings)
        layout.addWidget(back_btn)

        layout.addSpacing(8)

        self._settings_nav = SettingsNavList(container)
        self._settings_nav.panel_selected.connect(self._on_settings_panel_selected)
        layout.addWidget(self._settings_nav, 1)

        return container

    def _build_chat_area(self, parent: QWidget) -> QWidget:
        """构建对话区：顶栏（工作区名+窗口控制）+ 内容栈 + 唯一输入栏（随状态挂载）。"""
        area = QWidget(parent)
        area_layout = QVBoxLayout(area)
        area_layout.setContentsMargins(0, 0, 0, 0)
        area_layout.setSpacing(0)
        self._right_side = area
        self._right_side_layout = area_layout

        # 顶栏：左侧工作区名，右上窗口控制（可拖动窗口，左右结构的右上角）
        self.title_bar = TitleBar("MiniC", area)
        area_layout.addWidget(self.title_bar)

        # 内容栈：欢迎页(0) / 对话流(1) / 设置页(2)
        self._chat_stack = QStackedWidget(area)
        self._chat_stack.addWidget(self._build_welcome_page(area))
        self._chat_stack.addWidget(self._build_chat_flow(area))
        self._settings_page = SettingsPage(
            client=self.client, toast=self._toast, parent=area, workspace=self.workspace,
            notify=self._notify, progress=self.set_ingest_progress,
        )
        self._chat_stack.addWidget(self._settings_page)
        area_layout.addWidget(self._chat_stack, 1)

        # 唯一输入框：初始挂到欢迎页（问候语组内居中）
        self._input_bar = self._build_input_bar(area)
        self._attach_input_welcome()

        return area

    # ---- 欢迎页 ----

    def _build_welcome_page(self, parent: QWidget) -> QWidget:
        """欢迎页：大 M 背景 + 问候语 + 输入栏（整组垂直水平居中，输入栏由唯一输入框承担）。"""
        page = QWidget(parent)
        page.setStyleSheet("background-color: #1e1e1e;")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(48, 32, 48, 32)

        # 大 M 背景：绝对定位右上角（不占布局，避免挤压内容）
        self._welcome_big = QLabel("M", page)
        self._welcome_big.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        self._welcome_big.setStyleSheet(
            "color: rgba(255, 255, 255, 9); background: transparent; border: none;"
            "font-size: 300px; font-weight: 900;"
        )

        def _position_big(_event=None) -> None:
            """页面尺寸就绪后把大 M 定位到右上角。"""
            if page.width() > 0:
                self._welcome_big.setGeometry(page.width() - 380, -60, 380, 380)

        page.resizeEvent = _position_big
        QtCore.QTimer.singleShot(0, _position_big)

        # 对称 stretch → 问候语 + 输入栏整组垂直居中
        layout.addStretch(1)

        greeting = QLabel(_greeting_by_time(), page)
        greeting.setAlignment(Qt.AlignmentFlag.AlignCenter)
        greeting.setStyleSheet(
            f"color: #ffffff; font-size: 26px; font-weight: 600; background: transparent; border: none;"
        )
        layout.addWidget(greeting)

        layout.addSpacing(18)

        # 输入栏挂载位：两侧 stretch 让输入栏（max-width 760）水平居中
        input_wrapper = QHBoxLayout()
        input_wrapper.addStretch(1)
        input_wrapper.addStretch(1)
        self._input_wrapper = input_wrapper
        layout.addLayout(input_wrapper)

        layout.addStretch(1)

        self._welcome_page = page
        self._welcome_layout = layout
        return page

    def _build_input_bar(self, parent: QWidget) -> QWidget:
        """输入栏：输入框卡片 + 模型/质量 + 发送（唯一输入框，随状态挂载）。"""
        bar = QWidget(parent)
        bar.setStyleSheet("background: transparent;")
        bar_layout = QVBoxLayout(bar)
        bar_layout.setContentsMargins(0, 0, 0, 0)
        bar_layout.setSpacing(0)
        self._input_bar_layout = bar_layout

        prompt_box = QFrame(bar)
        prompt_box.setStyleSheet(
            f"QFrame {{ background-color: {COLOR_BG_CARD}; border: 1px solid {COLOR_BORDER};"
            f"border-radius: 12px; }}"
        )
        prompt_layout = QVBoxLayout(prompt_box)
        prompt_layout.setContentsMargins(0, 0, 0, 0)
        prompt_layout.setSpacing(0)

        self._input = EnterTextEdit(prompt_box)
        self._input.setPlaceholderText("向 MiniC 提问，使用 @ 添加上下文，使用 / 选择命令或能力")
        self._input.setStyleSheet(
            f"QTextEdit {{ background: transparent; color: #ffffff; border: none;"
            f"font-size: 14px; padding: 12px 16px; line-height: 1.6; }}"
        )
        self._input.setFixedHeight(72)
        self._input.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._input.send_requested.connect(self._send_message)
        self._input.slash_menu = self._slash_menu  # 菜单可见时上下/回车/Esc 走菜单导航
        self._input.textChanged.connect(self._update_slash_menu)
        prompt_layout.addWidget(self._input)

        tools_row = QHBoxLayout()
        tools_row.setContentsMargins(12, 6, 12, 8)
        tools_row.setSpacing(10)
        tools_row.addStretch(1)

        # 模型下拉框：列出 minic.json 中已添加的模型配置，选谁用谁
        self._model_combo = ModelComboBox(prompt_box)
        self._model_combo.setStyleSheet(
            f"QComboBox {{ background: transparent; color: {COLOR_TEXT_SECONDARY};"
            f"border: none; font-size: 12px; padding: 2px 6px; }}"
            f"QComboBox::drop-down {{ border: none; width: 16px; }}"
            f"QComboBox QAbstractItemView {{ background-color: {COLOR_BG_CARD};"
            f"color: #ffffff; border: 1px solid {COLOR_BORDER};"
            f"selection-background-color: {COLOR_BG_ACTIVE}; outline: none; }}"
        )
        self._model_combo.currentIndexChanged.connect(self._on_model_changed)
        self._model_combo.popup_opened.connect(self._reload_models)  # 展开前刷新列表
        tools_row.addWidget(self._model_combo)

        send_btn = QPushButton(prompt_box)
        send_btn.setIcon(load_icon("上箭头"))
        send_btn.setIconSize(QtCore.QSize(18, 18))
        send_btn.setFixedSize(32, 32)
        send_btn.setStyleSheet(
            f"QPushButton {{ background-color: {COLOR_ACCENT}; color: #0b1220;"
            f"border: none; border-radius: 8px; }}"
            f"QPushButton:hover {{ background-color: #6fd3ff; }}"
        )
        send_btn.clicked.connect(self._send_message)
        tools_row.addWidget(send_btn)

        prompt_layout.addLayout(tools_row)
        bar_layout.addWidget(prompt_box)
        return bar

    # ---- 输入栏挂载：唯一输入框在「欢迎页组内」与「对话区底部」之间移动 ----

    def _attach_input_welcome(self) -> None:
        """输入栏移入欢迎页：问候语下方，整组垂直水平居中（max-width 760）。"""
        if self._input_mode == "welcome":
            self._input_bar.show()
            return
        self._input_mode = "welcome"
        self._input_bar.setParent(self._welcome_page)
        self._input_bar.setMaximumWidth(760)
        self._input_bar_layout.setContentsMargins(0, 0, 0, 0)
        self._input_wrapper.insertWidget(1, self._input_bar, 10)  # 两侧 stretch 之间
        self._input_bar.show()

    def _attach_input_bottom(self) -> None:
        """输入栏移回对话区底部（对话流状态，全宽）。"""
        if self._input_mode == "bottom":
            self._input_bar.show()
            return
        self._input_mode = "bottom"
        self._input_bar.setParent(self._right_side)
        self._input_bar.setMaximumWidth(16777215)
        self._input_bar_layout.setContentsMargins(16, 10, 16, 14)
        self._right_side_layout.addWidget(self._input_bar)
        self._input_bar.show()

    def _detach_input(self) -> None:
        """设置页打开时临时隐藏输入栏（脱离布局，避免挤压）。"""
        self._input_mode = None
        self._input_bar.setParent(self._right_side)
        self._input_bar.hide()

    # ---- 对话流 ----

    def _build_chat_flow(self, parent: QWidget) -> QWidget:
        """对话流：居中容器（最大宽 900px）。"""
        page = QWidget(parent)
        page.setStyleSheet("background-color: #1e1e1e;")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        scroll = QScrollArea(page)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            f"QScrollArea {{ background: transparent; border: none; }}"
            f"QScrollArea > QWidget > QWidget {{ background: transparent; }}"
        )

        self._flow_container = QWidget()
        self._flow_layout = QVBoxLayout(self._flow_container)
        self._flow_layout.setContentsMargins(24, 20, 24, 20)
        self._flow_layout.setSpacing(8)
        self._flow_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        # 容器宽度在 resizeEvent 中动态设为窗口 70%（居中）
        self._flow_container.setMaximumWidth(1200)
        self._flow_container.setMinimumWidth(400)
        self._flow_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        wrapper = QWidget(scroll)
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        wrapper_layout.addWidget(self._flow_container)
        scroll.setWidget(wrapper)
        self._chat_scroll = scroll

        layout.addWidget(scroll)
        return page

    # ==================== 侧栏交互 ====================

    def _new_task(self) -> None:
        """新建任务：回到欢迎页等待第一句话（第一句话创建任务会话，保存到全局 work）。"""
        self._pending_session_ws = None
        self._pending_task = True
        self._reset_conversation()
        self.title_bar.set_title("任务")
        self._toast("输入第一句话，将创建任务会话（保存到全局 work 目录）")

    def _new_task_pending(self) -> None:
        """任务区 ＋ 按钮：同新建任务（待发任务会话）。"""
        self._new_task()

    def _new_project(self) -> None:
        """新建项目（工作区）：弹出目录选择器，加入树顶层。"""
        directory = QFileDialog.getExistingDirectory(self, "选择项目目录（工作区）", self.workspace)
        if not directory:
            self._toast("已取消")
            return
        name = Path(directory).name or directory
        if name in self._projects:
            self._select_project(name)
            self._toast(f"项目已存在：{name}")
            return
        # 初始化工作区 .minic（含项目标记 minic.json，不含 model/embedding 段，
        # 避免空配置遮蔽全局模型）：
        # 核心只把「含 .minic/minic.json 的可写目录」识别为项目根，
        # 否则文件/命令会回退落到核心启动目录而不是本工作区。
        from minic.gui.local_data import ensure_project_minic_json

        ensure_project_minic_json(Path(directory) / ".minic" / "minic.json")
        self._projects[name] = []
        self._project_dirs[name] = directory  # 记住工作区目录（工具/文件落在这里）
        self._project_empty.hide()
        self._add_workspace_item(name)
        self._select_project(name)
        self._save_state()
        self._toast(f"已添加项目：{name}")

    def _add_workspace_item(self, name: str) -> None:
        """在树中加入工作区顶层项：📁 名称 + 删除 ＋（新建会话）。"""
        item = QTreeWidgetItem(self._project_tree, [""])
        item.setData(0, Qt.ItemDataRole.UserRole, {"type": "workspace", "name": name})

        widget = QWidget()
        row_layout = QHBoxLayout(widget)
        row_layout.setContentsMargins(4, 1, 4, 1)
        row_layout.setSpacing(4)
        # 文件夹图标 + 工作区名（对齐 web 的 📁 节点）
        folder_label = QLabel(widget)
        folder_label.setPixmap(load_pixmap("文件夹", 14))
        folder_label.setFixedSize(14, 14)
        folder_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        folder_label.setStyleSheet("background: transparent; border: none;")
        row_layout.addWidget(folder_label)
        label = QLabel(name, widget)
        label.setStyleSheet("background: transparent; border: none; color: #cccccc;")
        row_layout.addWidget(label)
        row_layout.addStretch(1)
        # 删除工作区（弹确认框）
        del_btn = QPushButton(widget)
        del_btn.setIcon(load_icon("删 除"))
        del_btn.setIconSize(QtCore.QSize(12, 12))
        del_btn.setFixedSize(18, 18)
        del_btn.setToolTip("删除项目")
        del_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; border-radius: 4px; }}"
            f"QPushButton:hover {{ background-color: rgba(232, 17, 35, 51); }}"
        )

        def _delete_workspace() -> None:
            dialog = ConfirmDialog(f"是否要删除{name}项目", "删除", self)
            if not (dialog.exec() and dialog.confirmed):
                return
            # 移除树顶层项
            index = self._project_tree.indexOfTopLevelItem(item)
            if index >= 0:
                self._project_tree.takeTopLevelItem(index)
            # 清理数据：项目 + 目录 + 其下全部会话的 thread/messages + 进行中的流
            sessions = self._projects.pop(name, [])
            self._project_dirs.pop(name, None)
            for sname in sessions:
                key = self._session_key(name, sname)
                self._session_threads.pop(key, None)
                self._session_messages.pop(key, None)
                if key in self._streams:
                    self._finish_chat(key)
            self._save_state()
            if self._project_tree.topLevelItemCount() == 0:
                self._project_empty.show()
            # 删除的是当前打开的项目 → 重置对话区与顶栏
            if self._current_project == name:
                self._current_project = None
                self.title_bar.set_title("MiniC")
                self._reset_conversation()
            self._toast(f"已删除项目：{name}")

        del_btn.clicked.connect(_delete_workspace)
        row_layout.addWidget(del_btn)
        add_btn = QPushButton(widget)
        add_btn.setIcon(load_icon("新建"))
        add_btn.setIconSize(QtCore.QSize(14, 14))
        add_btn.setFixedSize(22, 20)
        add_btn.setToolTip("新建会话")
        add_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; border-radius: 4px; }"
            "QPushButton:hover { background-color: #2a2d2e; }"
        )
        add_btn.clicked.connect(lambda: self._new_session_for(name))
        row_layout.addWidget(add_btn)
        self._project_tree.setItemWidget(item, 0, widget)

        self._project_tree.expandItem(item)
        self._render_sessions(item, name)

    def _on_tree_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        """点击树节点：工作区 → 选中；会话 → 先选中父工作区再打开（会话隔离）。"""
        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        if data.get("type") == "session":
            parent = item.parent()
            if parent is not None:
                parent_data = parent.data(0, Qt.ItemDataRole.UserRole) or {}
                if parent_data.get("name"):
                    self._select_project(parent_data["name"])
            self._open_session(str(data.get("key", "")), data.get("name", ""))
            return
        name = data.get("name") or item.text(0).replace("📁 ", "")
        if name:
            self._select_project(name)

    def _select_project(self, name: str) -> None:
        """选中工作区：更新顶栏工作区名。"""
        self._current_project = name
        self.title_bar.set_title(f"📁 {name}")

    def _render_sessions(self, ws_item: QTreeWidgetItem, name: str) -> None:
        """渲染工作区节点下的会话子项。"""
        ws_item.takeChildren()
        for sname in self._projects.get(name, []):
            self._add_session_item(ws_item, sname)

    def _add_session_item(self, ws_item: QTreeWidgetItem, sname: str) -> None:
        """在树中加入会话子项（会话图标 + 名称 + 删除按钮）。"""
        ws_name = (ws_item.data(0, Qt.ItemDataRole.UserRole) or {}).get("name", "")
        key = self._session_key(ws_name, sname)
        item = QTreeWidgetItem(ws_item, [""])
        item.setData(
            0, Qt.ItemDataRole.UserRole,
            {"type": "session", "name": sname, "key": key},
        )

        widget = QWidget()
        row_layout = QHBoxLayout(widget)
        row_layout.setContentsMargins(4, 1, 4, 1)
        row_layout.setSpacing(6)
        icon = QLabel(widget)
        icon.setPixmap(load_pixmap("会话", 12))
        icon.setFixedSize(12, 12)
        icon.setStyleSheet("background: transparent; border: none;")
        row_layout.addWidget(icon)
        label = QLabel(sname, widget)
        label.setStyleSheet("background: transparent; border: none; color: #cccccc;")
        row_layout.addWidget(label)
        row_layout.addStretch(1)
        del_btn = QPushButton(widget)
        del_btn.setIcon(load_icon("删 除"))
        del_btn.setIconSize(QtCore.QSize(12, 12))
        del_btn.setFixedSize(18, 18)
        del_btn.setToolTip("删除")
        del_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; border-radius: 4px; }}"
            f"QPushButton:hover {{ background-color: rgba(232, 17, 35, 51); }}"
        )

        def _delete() -> None:
            dialog = ConfirmDialog(f"是否要删除{sname}会话", "删除", self)
            if not (dialog.exec() and dialog.confirmed):
                return
            sessions = self._projects.get(ws_name, [])
            if sname in sessions:
                sessions.remove(sname)
            self._render_sessions(ws_item, ws_name)
            self._session_threads.pop(key, None)
            self._session_messages.pop(key, None)
            if key in self._streams:
                self._finish_chat(key)  # 终止该会话进行中的流（事件后续被忽略）
            self._save_state()
            # 删除的是当前打开的会话 → 重置对话区（进行中的流保留继续累积）
            if self._current_session == key:
                self._reset_conversation()
            self._toast(f"已删除会话：{sname}")

        del_btn.clicked.connect(_delete)
        row_layout.addWidget(del_btn)
        self._project_tree.setItemWidget(item, 0, widget)

    @staticmethod
    def _session_key(ws_name: str, sname: str) -> str:
        """会话内部键：工作区::会话名（名称跨工作区可重复，键必须唯一）。"""
        return f"{ws_name}::{sname}"

    def _new_session_for(self, ws_name: str) -> None:
        """给指定工作区准备新会话：回到欢迎页等待第一句话，发送后才创建会话选项卡。

        会话名由第一句话生成（截断）；未发送前不产生会话条目。
        """
        self._select_project(ws_name)
        self._pending_task = False  # 进入工作区待发时放弃任务待发
        self._pending_session_ws = ws_name
        self._reset_conversation()  # 回欢迎页（问候语 + 输入框）
        self._toast(f"在工作区 {ws_name} 输入第一句话，将据此创建会话")

    def _create_session_from_first_message(self, text: str) -> None:
        """待发模式：用第一句话创建会话选项卡（名 = 第一句话截断，重名加序号）。"""
        ws = self._pending_session_ws
        self._pending_session_ws = None
        if not ws:
            return
        sessions = self._projects.setdefault(ws, [])
        sname = text[:12].strip() or "新会话"
        if len(text) > 12:
            sname += "…"
        base, counter = sname, 2
        while sname in sessions:
            sname = f"{base} ({counter})"
            counter += 1
        sessions.append(sname)
        key = self._session_key(ws, sname)
        self._session_threads[key] = None
        self._session_messages[key] = []
        for index in range(self._project_tree.topLevelItemCount()):
            top = self._project_tree.topLevelItem(index)
            data = top.data(0, Qt.ItemDataRole.UserRole) or {}
            if data.get("name") == ws:
                self._render_sessions(top, ws)
                break
        self._save_state()
        self._open_session(key, sname)

    # ---- 任务会话（与项目平级，直接包含会话，挂全局 ~/.minic/work）----

    @staticmethod
    def _task_key(sname: str) -> str:
        """任务会话内部键（与项目会话键空间隔离）。"""
        return f"$task$::{sname}"

    def _render_task_tree(self) -> None:
        """渲染任务区会话列表。"""
        self._task_tree.clear()
        for sname in self._task_sessions:
            self._add_task_session_item(sname)

    def _add_task_session_item(self, sname: str) -> None:
        """任务树加入一个任务会话项（会话图标 + 名称 + 删除）。"""
        key = self._task_key(sname)
        item = QTreeWidgetItem(self._task_tree, [""])
        item.setData(
            0, Qt.ItemDataRole.UserRole,
            {"type": "task_session", "name": sname, "key": key},
        )
        widget = QWidget()
        row_layout = QHBoxLayout(widget)
        row_layout.setContentsMargins(4, 1, 4, 1)
        row_layout.setSpacing(6)
        icon = QLabel(widget)
        icon.setPixmap(load_pixmap("会话", 12))
        icon.setFixedSize(12, 12)
        icon.setStyleSheet("background: transparent; border: none;")
        row_layout.addWidget(icon)
        label = QLabel(sname, widget)
        label.setStyleSheet("background: transparent; border: none; color: #cccccc;")
        row_layout.addWidget(label)
        row_layout.addStretch(1)
        del_btn = QPushButton(widget)
        del_btn.setIcon(load_icon("删 除"))
        del_btn.setIconSize(QtCore.QSize(12, 12))
        del_btn.setFixedSize(18, 18)
        del_btn.setToolTip("删除")
        del_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; border-radius: 4px; }}"
            f"QPushButton:hover {{ background-color: rgba(232, 17, 35, 51); }}"
        )

        def _delete() -> None:
            dialog = ConfirmDialog(f"是否要删除{sname}会话", "删除", self)
            if not (dialog.exec() and dialog.confirmed):
                return
            if sname in self._task_sessions:
                self._task_sessions.remove(sname)
            self._session_threads.pop(key, None)
            self._session_messages.pop(key, None)
            if key in self._streams:
                self._finish_chat(key)
            self._render_task_tree()
            self._save_state()
            if self._current_session == key:
                self._reset_conversation()
            self._toast(f"已删除任务会话：{sname}")

        del_btn.clicked.connect(_delete)
        row_layout.addWidget(del_btn)
        self._task_tree.setItemWidget(item, 0, widget)

    def _on_task_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        """点击任务会话：打开（顶栏显示「任务」）。"""
        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        if data.get("type") == "task_session":
            self._open_session(str(data.get("key", "")), data.get("name", ""))
            self.title_bar.set_title("任务")

    def _create_task_session_from_first_message(self, text: str) -> None:
        """待发任务：用第一句话创建任务会话选项卡。"""
        sname = text[:12].strip() or "任务会话"
        if len(text) > 12:
            sname += "…"
        base, counter = sname, 2
        while sname in self._task_sessions:
            sname = f"{base} ({counter})"
            counter += 1
        self._task_sessions.append(sname)
        key = self._task_key(sname)
        self._session_threads[key] = None
        self._session_messages[key] = []
        self._render_task_tree()
        self._save_state()
        self._open_session(key, sname)
        self.title_bar.set_title("任务")

    def _open_session(self, key: str, sname: str) -> None:
        """切换会话：重置对话流并渲染该会话的消息（会话隔离，键 = 工作区::会话名）。

        进行中的 AI 流式回答（可多个会话并行）不中断：气泡摘下隐藏、token 继续
        累积；切回时挂回该会话进行中的气泡并继续实时显示。
        """
        self._detach_stream_bubbles()  # 摘走全部进行中的 AI 气泡（保留引用，token 继续追加）
        self._pending_session_ws = None  # 显式打开会话即放弃待发状态
        self._pending_task = False
        self._current_session = key
        self._current_thread_id = self._session_threads.get(key)
        self.chat_thread_id = self._current_thread_id
        self._tool_cards.clear()
        self._approval_queue.clear()
        while self._flow_layout.count():
            item = self._flow_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        messages = self._session_messages.get(key, [])
        if messages:
            # 有消息 → 对话流渲染该会话消息，输入栏挂底部
            self._chat_stack.setCurrentIndex(1)
            for role, text in messages:
                self._flow_layout.addWidget(MessageBubble(role, text))
            self._scroll_to_bottom()
            self._attach_input_bottom()
        else:
            # 空会话 → 欢迎页（问候语 + 输入栏整组居中）
            self._chat_stack.setCurrentIndex(0)
            self._attach_input_welcome()
        # 切回有流式回答进行中的会话：挂回该流的气泡与工具组继续实时显示
        stream = self._streams.get(key)
        if stream is not None:
            bubble = stream["bubble"]
            self._chat_stack.setCurrentIndex(1)
            self._flow_layout.addWidget(bubble)
            bubble.show()
            group = stream.get("tool_group")
            if group is not None:
                self._flow_layout.addWidget(group)
                group.show()
            self._attach_input_bottom()
            self._scroll_to_bottom()
        self._toast(f"已打开会话：{sname}")

    def _detach_stream_bubbles(self) -> None:
        """把全部进行中的 AI 气泡与工具组从对话流摘下并隐藏（各流继续累积）。"""
        for stream in self._streams.values():
            bubble = stream["bubble"]
            self._flow_layout.removeWidget(bubble)
            bubble.hide()
            group = stream.get("tool_group")
            if group is not None:
                self._flow_layout.removeWidget(group)
                group.hide()

    def _focus_input(self) -> None:
        """聚焦输入框（唯一底部常驻输入框）。"""
        self._input.setFocus()

    # ---- 斜杠命令菜单 ----

    def _update_slash_menu(self) -> None:
        """输入框文本变化：以 / 开头的纯命令输入显示斜杠菜单，否则隐藏。"""
        text = self._input.toPlainText() or ""
        stripped = text.strip()
        if stripped.startswith("/") and " " not in stripped[1:]:
            self._slash_menu.show_for(stripped[1:])
            if self._slash_menu.isVisible():
                self._position_slash_menu()
        else:
            self._slash_menu.hide_menu()

    def _position_slash_menu(self) -> None:
        """把斜杠菜单定位到输入框上方（全局坐标，浮窗不随布局裁剪）。"""
        point = self._input.mapToGlobal(QtCore.QPoint(12, -self._slash_menu.height() - 10))
        self._slash_menu.move(point)

    def _run_slash_command(self, command: str) -> None:
        """执行斜杠命令（命令名见 SlashMenu.SLASH_COMMANDS，按规格文档语义）。"""
        self._input.clear()
        self._input.setFocus()
        if command == "/压缩":
            self._compress_current_session()
        elif command in ("/新建", "/清空"):
            self._archive_and_new()
        elif command == "/记忆":
            self._open_settings("memory")
        elif command == "/知识库":
            self._open_settings("knowledge")
        elif command == "/设置":
            self._open_settings()
        elif command == "/帮助":
            self._show_help()

    def _backup_current_session(self) -> None:
        """备份当前会话消息到 ~/.minic/backups/sessions（新建/清空前调用）。"""
        key = self._current_session
        if key is None:
            return
        messages = self._session_messages.get(key)
        if not messages:
            return
        backup_dir = Path.home() / ".minic" / "backups" / "sessions"
        backup_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^\w\-]", "_", key)
        path = backup_dir / f"{safe_name}.{datetime.now():%Y%m%d%H%M%S}.json"
        path.write_text(
            json.dumps({"session": key, "messages": messages}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _archive_and_new(self) -> None:
        """归档（备份）当前会话并进入新建待发模式（/新建、/清空）。"""
        self._backup_current_session()
        if self._current_project:
            self._new_session_for(self._current_project)
            self._notify(f"已备份并归档，在工作区 {self._current_project} 输入第一句话创建会话", "success")
        else:
            self._new_task()
            self._notify("已备份并归档，输入第一句话创建任务", "success")

    def _show_help(self) -> None:
        """在对话流中显示斜杠命令帮助。"""
        from minic.gui.widgets.slash_menu import SLASH_COMMANDS

        text = "\n".join(f"{command}    {description}" for command, description in SLASH_COMMANDS)
        if self._chat_stack.currentIndex() == 0:
            self._chat_stack.setCurrentIndex(1)
            self._attach_input_bottom()
        self._flow_layout.addWidget(MessageBubble("ai", f"斜杠命令（输入 / 选择，↑↓ 选择回车执行）：\n\n{text}"))
        self._scroll_to_bottom()

    def _compress_current_session(self) -> None:
        """压缩当前会话历史（核心 POST /threads/{id}/compress，压缩前核心自动备份）。"""
        key = self._current_session
        if key is None:
            self._notify("当前没有打开的会话", "failed")
            return
        thread_id = self._session_threads.get(key)
        if not thread_id:
            self._notify("会话尚未开始，先发送消息", "failed")
            return
        worker = Worker(lambda: self.client.post_json(f"/threads/{thread_id}/compress"), self)
        worker.completed.connect(
            lambda result: self._notify("已压缩会话历史（压缩前已备份）", "success") if result else None
        )
        worker.failed.connect(lambda err: self._notify(f"压缩失败：{err}", "failed"))
        worker.start()

    # ---- 模型选择（输入框右下角下拉框）----

    def _reload_models(self) -> None:
        """从本地 minic.json 加载模型列表填充下拉框（显示名 = 名称 / 模型）。"""
        from minic.gui.local_data import local_settings_raw

        try:
            data = local_settings_raw(self.workspace)
        except Exception:  # noqa: BLE001 - 配置读取失败用空列表
            data = {}
        model_cfg = data.get("model") or {}
        models = model_cfg.get("models")
        if not isinstance(models, list):
            models = [model_cfg] if model_cfg.get("provider") or model_cfg.get("model") else []
        self._model_options = {}
        self._model_combo.blockSignals(True)
        self._model_combo.clear()
        for entry in models:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            display = f"{name} / {entry.get('model') or ''}"
            self._model_options[display] = name
            self._model_combo.addItem(display)
        if not self._model_options:  # 无配置时给一个占位
            self._model_combo.addItem("（未配置模型）")
        self._model_combo.blockSignals(False)
        # 恢复已保存的选择，否则用第一项
        saved = self._saved_model
        for index in range(self._model_combo.count()):
            name = self._model_options.get(self._model_combo.itemText(index))
            if name == saved:
                self._model_combo.setCurrentIndex(index)
                break

    def _on_model_changed(self, _index: int) -> None:
        """下拉选中变化：记录选择并持久化。"""
        self._saved_model = self._model_options.get(self._model_combo.currentText())
        self._save_state()

    def _current_model(self) -> str | None:
        """当前下拉选中的模型配置名（传给核心 ChatRequest.model）。"""
        return self._model_options.get(self._model_combo.currentText())

    # ==================== 设置 ====================

    def _open_settings(self, panel: str | None = None) -> None:
        """打开设置：侧栏内容切为设置导航（← 返回工作区 + 分组），右侧切面板页。

        主窗口左右结构与主顶栏保持（返回工作区在侧栏菜单位置）。
        """
        if self._chat_stack.currentIndex() != 2:
            self._prev_stack_index = self._chat_stack.currentIndex()
        self._detach_input()
        if panel:
            self._settings_page.open_panel(panel)
            self._settings_nav.select_panel(panel)
        self._side_stack.setCurrentIndex(1)
        self._chat_stack.setCurrentIndex(2)

    def _on_settings_panel_selected(self, panel_name: str) -> None:
        """侧栏设置导航切换 → 右侧面板栈联动；当前面板未保存先弹「是否保存」。

        是 → 保存成功才切换（校验失败留在原面板）；否 → 放弃修改后切换。
        """
        if self._settings_page.has_unsaved_changes():
            dialog = SavePromptDialog(parent=self)
            dialog.exec()
            if dialog.choice == "save":
                if not self._settings_page.save_current():
                    return  # 校验失败：留在原面板，不切换
            else:
                self._settings_page.discard_current()
        self._settings_page.open_panel(panel_name)

    def _close_settings(self) -> None:
        """返回：侧栏恢复主菜单，回到之前的欢迎页或对话流，按状态重挂输入栏。

        面板存在未保存修改时先弹「是否保存」：是 → 保存成功才返回
        （校验失败留在设置页）；否 → 放弃修改直接返回。
        """
        if self._settings_page.has_unsaved_changes():
            dialog = SavePromptDialog(parent=self)
            dialog.exec()
            if dialog.choice == "save":
                if not self._settings_page.save_current():
                    return  # 校验失败：留在设置页
            else:
                self._settings_page.discard_current()
        self._side_stack.setCurrentIndex(0)
        index = self._prev_stack_index if self._prev_stack_index in (0, 1) else 0
        self._chat_stack.setCurrentIndex(index)
        if index == 0:
            self._attach_input_welcome()
        else:
            self._attach_input_bottom()

    def _open_skills(self) -> None:
        """技能菜单：打开内嵌设置页并切到技能面板。"""
        self._open_settings("skills")

    # ==================== 持久化 ====================

    @staticmethod
    def _state_path() -> Path:
        """桌面端状态文件路径（用户级 ~/.minic/gui_state.json）。"""
        return Path.home() / ".minic" / "gui_state.json"

    def _save_theme(self, name: str) -> None:
        """已废弃：主题设置入口已移除，仅保留深色主题（不再写 gui_state）。"""
        del name  # 保留方法签名兼容旧调用；无实际操作

    def _save_state(self) -> None:
        """保存工作区/会话/消息状态到文件。"""
        path = self._state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "projects": self._projects,
                "project_dirs": self._project_dirs,
                "task_sessions": self._task_sessions,
                "session_threads": self._session_threads,
                "session_messages": {
                    key: [list(item) for item in items]
                    for key, items in self._session_messages.items()
                },
                "model": self._saved_model,
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass  # 保存失败不阻塞 UI

    def _load_state(self) -> None:
        """启动时加载持久化状态并重建树（主题固定深色，不再从状态恢复）。"""
        path = self._state_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self._saved_model = data.get("model")  # 恢复上次选中的模型配置名
        self._projects = data.get("projects", {})
        self._project_dirs = {
            str(key): str(value)
            for key, value in (data.get("project_dirs", {}) or {}).items()
        }
        self._task_sessions = [str(item) for item in (data.get("task_sessions", []) or [])]
        self._render_task_tree()
        threads = data.get("session_threads", {}) or {}
        messages = data.get("session_messages", {}) or {}
        # 旧版键为裸会话名（跨工作区冲突），迁移为 "工作区::会话名" 复合键
        if threads and not any("::" in str(key) for key in threads):
            new_threads: dict[str, str | None] = {}
            new_messages: dict[str, list[tuple[str, str]]] = {}
            for ws, sessions in self._projects.items():
                for sname in sessions:
                    key = self._session_key(ws, sname)
                    new_threads[key] = threads.get(sname)
                    new_messages[key] = [tuple(item) for item in messages.get(sname, [])]
            threads, messages = new_threads, new_messages
        self._session_threads = {str(key): value for key, value in threads.items()}
        self._session_messages = {
            str(key): [tuple(item) for item in items]
            for key, items in messages.items()
        }
        for name in self._projects:
            self._project_empty.hide()
            self._add_workspace_item(name)

    # ==================== 核心连接 ====================

    def _connect_core(self) -> None:
        """后台线程连接核心；失败自动重试（内嵌核心启动中/外部核心未运行）。"""
        self._connected = False
        self._connect_attempts = 0
        self._retry_timer = QtCore.QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.setInterval(2000)
        self._retry_timer.timeout.connect(self._retry_connect_core)
        self.client.connected.connect(self._on_connected)
        self.client.connection_failed.connect(self._on_connection_failed)
        worker = Worker(self.client.connect_core, self)
        worker.start()

    def _retry_connect_core(self) -> None:
        """定时重试连接核心（最多 15 次 × 2s = 30 秒窗口）。"""
        if self._connected:
            return
        self._connect_attempts += 1
        if self._connect_attempts > 15:
            self._toast("核心未运行，请查看 ~/.minic/logs/core.log")
            return
        worker = Worker(self.client.connect_core, self)
        worker.start()

    def _on_connected(self, health: dict[str, Any]) -> None:
        """核心连接成功。"""
        self._connected = True
        self._retry_timer.stop()
        version = health.get("version", "")
        self._toast(f"核心已连接（v{version}）")

    def _on_connection_failed(self, reason: str) -> None:
        """核心连接失败：提示一次并自动重试。"""
        del reason  # 具体原因写日志/由最终失败提示兜底，避免启动期误导
        if self._connect_attempts == 0:
            self._toast("正在启动核心服务，请稍候…")
        self._retry_timer.start()

    # ==================== 聊天 ====================

    def _reset_conversation(self) -> None:
        """重置会话：回到欢迎页（清除当前会话状态；各流继续隐藏累积）。"""
        self._detach_stream_bubbles()
        self.chat_thread_id = None
        self._current_session = None
        self._current_thread_id = None
        self._tool_cards.clear()
        self._approval_queue.clear()
        while self._flow_layout.count():
            item = self._flow_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._input.clear()
        self._chat_stack.setCurrentIndex(0)
        self._attach_input_welcome()

    def _send_message(self, text: str | None = None) -> None:
        """发送消息：进入对话流并流式消费 SSE（跨会话可并行，同会话串行）。

        ``text`` 供 404 自动重试复用；为 None 时读取输入框。
        """
        text = text if isinstance(text, str) else None
        if text is None:
            text = (self._input.toPlainText() or "").strip()
        text = text.strip()
        if not text:
            return
        if self.client.base_url is None:
            self._toast("核心未运行，请先启动核心服务")
            return

        # 待发任务/待发工作区：第一句话创建会话选项卡（会话名取自第一句话）
        if self._pending_task:
            self._pending_task = False
            self._create_task_session_from_first_message(text)
        elif self._current_session is None and self._pending_session_ws:
            self._create_session_from_first_message(text)
        elif self._current_session is None:
            # 无归属欢迎页（刚进桌面端直接发消息）：默认归入任务区，
            # 第一句话创建任务会话并挂到任务树，避免出现无处归属的流。
            self._create_task_session_from_first_message(text)

        # run_key：会话复合键；无归属（欢迎页）用固定 "__welcome__"（同时只跑一条）
        run_key = self._current_session or "__welcome__"

        # 同会话已有进行中流 → 拒绝（核心同 thread 并行返回 409）
        if run_key in self._streams:
            self._toast("该会话正在生成回答，请稍候")
            return

        # 欢迎页发送 → 切换到对话流（输入栏移到底部）
        if self._chat_stack.currentIndex() == 0:
            self._chat_stack.setCurrentIndex(1)
            self._attach_input_bottom()

        # 用户气泡 + 存档到当前会话
        self._flow_layout.addWidget(MessageBubble("user", text))
        self._scroll_to_bottom()
        if self._current_session is not None:
            self._session_messages.setdefault(self._current_session, []).append(("user", text))
            self._save_state()

        # AI 气泡 + 工具调用组 + 流记录（token 追加、message_end 归档、错误处理都按 run_key 定位）
        bubble = MessageBubble("ai", "")
        bubble.set_streaming(True)  # 输出中显示转圈，直到 message_end
        self._flow_layout.addWidget(bubble)
        tool_group = ToolGroup()
        self._flow_layout.addWidget(tool_group)
        self._streams[run_key] = {
            "bubble": bubble,
            "tool_group": tool_group,
            "session": self._current_session,
            "saved": False,
            "pending_text": text,
            "pending_retried": False,
        }
        self._scroll_to_bottom()

        self._input.clear()
        self._fire_chat(text, run_key)

    def _fire_chat(self, text: str, run_key: str) -> None:
        """发起流式请求（thread_id 取流所属会话的；None 时核心新建并回传真实 id）。

        workspace 取当前工作区的目录（工具执行/文件读写落在工作区内）；
        无工作区时落在全局 ~/.minic/work（不落 MiniC 源码目录）。
        """
        self._chat_in_progress = True
        thread_id = self._session_threads.get(run_key) if run_key != "__welcome__" else None
        workspace = (
            self._project_dirs.get(self._current_project or "", None)
            or self._default_work_dir()
        )
        worker = Worker(
            lambda: self.client.stream_chat(thread_id, workspace, text, self._current_model(), run_key=run_key),
            self,
        )
        worker.start()

    def _default_work_dir(self) -> str:
        """无工作区时的全局对话目录 ~/.minic/work（自动创建并初始化 .minic）。

        项目标记 minic.json 不含 model/embedding 段（避免空配置遮蔽全局模型）。
        """
        work = Path.home() / ".minic" / "work"
        work.mkdir(parents=True, exist_ok=True)
        from minic.gui.local_data import ensure_project_minic_json

        ensure_project_minic_json(work / ".minic" / "minic.json")
        return str(work)

    def _scroll_to_bottom(self) -> None:
        """滚动到底部。"""
        if self._chat_scroll is not None:
            bar = self._chat_scroll.verticalScrollBar()
            bar.setValue(bar.maximum())

    # ---- SSE 事件处理（信号连到 CoreClient.chat_event）----

    def _on_chat_event(self, event_name: str, data: dict[str, Any]) -> None:
        """处理 SSE 事件：按 data._run 路由到对应流（支持多会话并行）。"""
        run_key = str(data.pop("_run", "") or "")
        stream = self._streams.get(run_key)
        if stream is None:
            return  # 已结束/未知的流，忽略
        bubble = stream["bubble"]
        if event_name == "message_start":
            thread_id = data.get("thread_id")
            if thread_id:
                if stream["session"] == self._current_session:
                    self.chat_thread_id = thread_id
                    self._current_thread_id = thread_id
                # 回存真实 thread_id 到发起该流的会话（断线续传/上下文延续）
                if stream["session"] is not None:
                    self._session_threads[stream["session"]] = thread_id
                    self._save_state()
        elif event_name == "token":
            bubble.append_text(str(data.get("delta", "") or ""))
            self._scroll_to_bottom()
        elif event_name == "tool_call":
            if stream["session"] == self._current_session:
                self._add_tool_card(data, stream)
        elif event_name == "tool_result":
            if stream["session"] == self._current_session:
                self._update_tool_result(data)
        elif event_name == "approval_requested":
            self._handle_approval_requested(data)
        elif event_name == "approval_result":
            self._update_approval_result(data)
        elif event_name == "message_end":
            # AI 完整文本存档到发起该流的会话（切换后仍正确归属）
            if stream["session"] is not None:
                self._session_messages.setdefault(stream["session"], []).append(
                    ("ai", bubble.text())
                )
                stream["saved"] = True
                self._save_state()
            # 双保险：用过滤后的最终文本替换气泡（防任何工具标记残留显示）
            bubble.set_text(strip_dsml(bubble.text()))
            # 回答完成：停转圈 + 收起该流的工具调用组（可点组头展开）
            bubble.set_streaming(False)
            group = stream.get("tool_group")
            if group is not None:
                group.collapse()
        elif event_name == "done":
            if stream["session"] == self._current_session:
                self._render_sources(data.get("sources") or [])
            bubble.set_streaming(False)
            self._finish_chat(run_key)
        elif event_name == "error":
            self._toast(f"核心错误：{data.get('message', '')}")
            bubble.set_streaming(False)
            self._finish_chat(run_key)
        elif event_name == "__done__":
            bubble.set_streaming(False)
            self._finish_chat(run_key)

    def _add_tool_card(self, data: dict[str, Any], stream: dict[str, Any]) -> None:
        """往该流的工具调用组加入一步。"""
        tool_call_id = str(data.get("id", ""))
        group = stream["tool_group"]
        card = group.add_tool(
            tool=str(data.get("tool", "")),
            args=data.get("args") or {},
            status=str(data.get("status", "pending")),
        )
        self._tool_cards[tool_call_id] = card
        self._scroll_to_bottom()

    def _update_tool_result(self, data: dict[str, Any]) -> None:
        """更新工具卡片结果。"""
        tool_call_id = str(data.get("id", ""))
        card = self._tool_cards.get(tool_call_id)
        if card is not None:
            card.set_result(str(data.get("status", "")), str(data.get("output", "") or ""))
            self._scroll_to_bottom()

    def _handle_approval_requested(self, data: dict[str, Any]) -> None:
        """审批弹窗；对应工具卡片标记「需审批」。"""
        card = self._tool_cards.get(str(data.get("tool_call_id", "")))
        if card is not None:
            card.mark_awaiting()
        self._approval_queue.append(dict(data))
        if not self._approval_dialog_open:
            self._process_approval_queue()

    def _process_approval_queue(self) -> None:
        """依次弹出审批弹窗。"""
        if not self._approval_queue or self._approval_dialog_open:
            return
        self._approval_dialog_open = True
        item = self._approval_queue.pop(0)
        dialog = ApprovalDialog(
            tool=str(item.get("tool", "")),
            args=item.get("args") or {},
            message=item.get("message"),
            options=item.get("options"),
            parent=self,
        )
        thread_id = str(item.get("thread_id", self.chat_thread_id or ""))

        def _on_done(decision: str | None) -> None:
            self._approval_dialog_open = False
            if decision is not None and self.client.base_url is not None:
                self._submit_approval(thread_id, str(item.get("id", "")), decision)
            self._process_approval_queue()

        dialog.finished.connect(
            lambda _result: _on_done(dialog.decision)
        )
        dialog.show()

    def _submit_approval(self, thread_id: str, approval_id: str, decision: str) -> None:
        """提交审批决策。"""
        worker = Worker(
            lambda: self.client.approve(thread_id, approval_id, decision),
            self,
        )
        worker.completed.connect(lambda ok: self._notify(f"已提交审批：{decision}", "success") if ok else self._notify("审批提交失败", "failed"))
        worker.failed.connect(lambda err: self._notify(f"审批提交失败：{err}", "failed"))
        worker.start()

    def _update_approval_result(self, data: dict[str, Any]) -> None:
        """审批结果回执；对应工具卡片恢复「执行中」（超时除外）。"""
        card = self._tool_cards.get(str(data.get("tool_call_id", "")))
        if card is not None and data.get("status") != "expired":
            card.mark_running()
        self._toast(f"审批结果：{data.get('status', '')}")

    def _render_sources(self, sources: list[dict[str, Any]]) -> None:
        """渲染来源块。"""
        if not sources:
            return
        block = QFrame(self._flow_container)
        block.setStyleSheet(
            f"QFrame {{ background-color: {COLOR_BG_CARD};"
            f"border: 1px solid {COLOR_BORDER}; border-radius: 8px; }}"
        )
        layout = QVBoxLayout(block)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)
        title = QLabel("📄 来源", block)
        title.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 12px; background: transparent; border: none;"
            f"font-weight: 600;"
        )
        layout.addWidget(title)
        for source in sources:
            path = source.get("file_path") or source.get("source") or ""
            section = source.get("section")
            scope = source.get("scope")
            text = path
            if section:
                text += f" · {section}"
            if scope:
                text += f" · {scope}"
            line = QLabel(text, block)
            line.setTextFormat(Qt.TextFormat.PlainText)
            line.setWordWrap(True)
            line.setStyleSheet(
                f"color: {COLOR_TEXT_SECONDARY}; font-size: 12px; background: transparent; border: none;"
            )
            layout.addWidget(line)
        self._flow_layout.addWidget(block)
        self._scroll_to_bottom()

    def _finish_chat(self, run_key: str) -> None:
        """结束指定流：气泡/工具组若已不在对话流（切走）则清理，否则保留显示。"""
        stream = self._streams.pop(run_key, None)
        self._chat_in_progress = bool(self._streams)
        if stream is None:
            return
        bubble = stream["bubble"]
        if self._flow_layout.indexOf(bubble) == -1:
            bubble.deleteLater()
        group = stream.get("tool_group")
        if group is not None and self._flow_layout.indexOf(group) == -1:
            group.deleteLater()

    def _on_chat_error(self, message: str, run_key: str = "") -> None:
        """聊天 SSE 错误：按 run_key 定位流。

        - 会话 thread 失效（404 会话不存在）时静默重置并重试一次
          （旧版预生成假 thread_id 的兜底，复用首次气泡与用户消息）。
        - 其余错误：把已生成的部分文本存档，避免半截回答丢失。
        """
        stream = self._streams.get(run_key)
        if stream is None:
            self._notify(f"聊天失败：{message}", "failed")
            return
        if (
            stream["session"] is not None
            and not stream["pending_retried"]
            and "404" in message
        ):
            stream["pending_retried"] = True
            self._session_threads[stream["session"]] = None
            if stream["session"] == self._current_session:
                self._current_thread_id = None
                self.chat_thread_id = None
            self._save_state()
            self._fire_chat(stream["pending_text"], run_key)
            return
        if (
            not stream["saved"]
            and stream["session"] is not None
            and stream["bubble"].text()
        ):
            self._session_messages.setdefault(stream["session"], []).append(
                ("ai", stream["bubble"].text())
            )
            self._save_state()
        self._notify(f"聊天失败：{message}", "failed")
        self._finish_chat(run_key)

    # ---- 对外 ----

    def show_toast(self, message: str) -> None:
        """对外 toast 入口。"""
        self._toast_widget.show_message(message)

    def set_ingest_progress(self, visible: bool) -> None:
        """顶栏入库进度条显示/隐藏（知识库加载期间可见）。"""
        self.title_bar.set_progress_visible(visible)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 接口命名
        """对话流宽度 = 窗口宽度 70%（居中）。"""
        super().resizeEvent(event)
        if hasattr(self, "_flow_container") and self._flow_container is not None:
            self._flow_container.setMaximumWidth(max(int(self.width() * 0.7), 400))
