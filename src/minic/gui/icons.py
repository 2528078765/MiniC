"""图标加载：读取 MiniC/icon/*.png 并返回 QIcon/QPixmap。"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap


def _resolve_icon_dir() -> Path:
    """图标目录：打包后位于 PyInstaller 解包目录，开发环境位于项目根。"""
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir is not None:
        return Path(bundle_dir) / "icon"
    # 项目根：src/minic/gui/icons.py -> parents[3] = <项目根>
    return Path(__file__).resolve().parents[3] / "icon"


_ICON_DIR = _resolve_icon_dir()


def icon_path(name: str) -> Path:
    """返回图标文件路径（不存在时返回路径本身，调用方自行处理）。"""
    return _ICON_DIR / f"{name}.png"


def load_icon(name: str) -> QIcon:
    """按名称加载 QIcon；文件缺失返回空 QIcon。"""
    return QIcon(str(icon_path(name)))


def load_pixmap(name: str, size: int = 16) -> QPixmap:
    """按名称加载并缩放到指定尺寸的 QPixmap；文件缺失返回空 QPixmap。"""
    pix = QPixmap(str(icon_path(name)))
    if not pix.isNull():
        pix = pix.scaled(
            size, size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return pix
