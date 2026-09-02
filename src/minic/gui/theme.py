"""深色主题 QSS 与颜色常量（与 web-ui/css/style.css 对齐）。"""

from __future__ import annotations

# ---- 颜色常量（对应 web-ui CSS 变量）----
COLOR_BG_MAIN = "#1e1e1e"        # 主内容区背景
COLOR_BG_SIDEBAR = "#252526"     # 侧边栏背景
COLOR_BG_CARD = "#2b2b2b"        # 卡片/引用块背景
COLOR_BG_HOVER = "#2a2d2e"       # 悬停背景
COLOR_BG_ACTIVE = "#37373d"      # 选中项背景
COLOR_BORDER = "#3c3c3c"         # 边框
COLOR_TEXT_PRIMARY = "#ffffff"
COLOR_TEXT_SECONDARY = "#cccccc"
COLOR_TEXT_MUTED = "#858585"
COLOR_ACCENT = "#4fc3f7"         # 天蓝强调
COLOR_GREEN = "#4caf50"          # 更新按钮/成功
COLOR_RED = "#e81123"            # 关闭/错误
COLOR_SHIELD = "#ffa94d"         # 权限橙色

FONT_FAMILY = '"Segoe UI", "Microsoft YaHei"'
FONT_MONO = '"Consolas", "Cascadia Code", "Courier New"'

# ---- 全局 QSS 深色主题 ----
QSS = f"""
* {{
    font-family: {FONT_FAMILY};
    font-size: 13px;
    color: {COLOR_TEXT_PRIMARY};
}}

QMainWindow, QWidget {{
    background-color: {COLOR_BG_MAIN};
}}

/* ---------- 通用控件 ---------- */
QPushButton {{
    background-color: {COLOR_BG_ACTIVE};
    color: {COLOR_TEXT_PRIMARY};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    padding: 6px 14px;
}}
QPushButton:hover {{
    background-color: #44444a;
}}
QPushButton:pressed {{
    background-color: #4a4a52;
}}
QPushButton:disabled {{
    color: {COLOR_TEXT_MUTED};
}}

QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox {{
    background-color: {COLOR_BG_CARD};
    color: {COLOR_TEXT_PRIMARY};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    padding: 6px 10px;
    selection-background-color: {COLOR_ACCENT};
    selection-color: #0b1220;
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{
    border-color: {COLOR_ACCENT};
}}

QComboBox {{
    background-color: {COLOR_BG_CARD};
    color: {COLOR_TEXT_PRIMARY};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    padding: 5px 10px;
}}
QComboBox QAbstractItemView {{
    background-color: {COLOR_BG_CARD};
    color: {COLOR_TEXT_PRIMARY};
    border: 1px solid {COLOR_BORDER};
    selection-background-color: {COLOR_BG_ACTIVE};
}}

QCheckBox::indicator {{
    width: 14px;
    height: 14px;
}}

QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: #424242;
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: #4f4f4f; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

QToolTip {{
    background-color: {COLOR_BG_CARD};
    color: {COLOR_TEXT_PRIMARY};
    border: 1px solid {COLOR_BORDER};
    padding: 4px 8px;
}}

/* ---------- 列表 ---------- */
QListWidget {{
    background-color: {COLOR_BG_SIDEBAR};
    border: none;
    outline: none;
}}
QListWidget::item {{
    color: {COLOR_TEXT_SECONDARY};
    padding: 6px 10px;
    border-radius: 6px;
}}
QListWidget::item:hover {{ background-color: {COLOR_BG_HOVER}; }}
QListWidget::item:selected {{
    background-color: {COLOR_BG_ACTIVE};
    color: {COLOR_TEXT_PRIMARY};
}}

/* ---------- 滚动区内容透明 ---------- */
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
"""


# ---- 浅色主题（深色 QSS 颜色映射生成）----
_LIGHT_MAP = [
    ("#1e1e1e", "#f5f5f5"),  # 主背景
    ("#252526", "#ececec"),  # 侧边栏
    ("#2b2b2b", "#ffffff"),  # 卡片
    ("#2a2d2e", "#e0e0e0"),  # hover
    ("#37373d", "#dcdcdc"),  # active
    ("#3c3c3c", "#c8c8c8"),  # 边框
    ("#44444a", "#c8c8c8"),
    ("#4a4a52", "#b8b8b8"),
    ("#cccccc", "#3a3a3a"),  # 次要文字
    ("#858585", "#6a6a6a"),  # 弱化文字
]

LIGHT_QSS = QSS
for _dark, _light in _LIGHT_MAP:
    LIGHT_QSS = LIGHT_QSS.replace(_dark, _light)

# 浅色下白色文字换深色（最后统一替换，注意放在上面替换之后）
LIGHT_QSS = LIGHT_QSS.replace("color: #ffffff", "color: #1e1e1e")
LIGHT_QSS = LIGHT_QSS.replace("color: #fff", "color: #1e1e1e")


def qss_for(theme_name: str) -> str:
    """按主题名返回全局 QSS（dark / light）。"""
    return QSS if theme_name != "light" else LIGHT_QSS
