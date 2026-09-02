"""把 icon/Log.png 转成多尺寸 icon/Log.ico（PyInstaller 打包 exe 图标用）。

ICO 容器直接内嵌 PNG 数据（Vista+ 规范），不依赖 Qt 的 ico 写入插件。
用法：``python scripts/make_icon.py``
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPixmap

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "icon" / "Log.png"
TARGET = ROOT / "icon" / "Log.ico"
SIZES = [16, 24, 32, 48, 64, 128, 256]


def png_bytes(pixmap: QPixmap, size: int) -> bytes:
    """把 pixmap 缩放到 size 并编码为 PNG 字节。"""
    scaled = pixmap.scaled(
        size,
        size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    image = scaled.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def build_ico(payloads: list[bytes]) -> bytes:
    """按 Vista+ 规范组装多尺寸 ICO（内嵌 PNG 数据）。"""
    header = struct.pack("<HHH", 0, 1, len(payloads))
    entries = b""
    offset = 6 + 16 * len(payloads)
    for size, payload in zip(SIZES, payloads):
        dimension = 0 if size >= 256 else size
        entries += struct.pack(
            "<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(payload), offset
        )
        offset += len(payload)
    return header + entries + b"".join(payloads)


def main() -> int:
    """生成 Log.ico，返回退出码。"""
    if not SOURCE.exists():
        print(f"未找到 {SOURCE}", file=sys.stderr)
        return 1
    app = QGuiApplication(sys.argv)
    pixmap = QPixmap(str(SOURCE))
    if pixmap.isNull():
        print(f"无法加载 {SOURCE}", file=sys.stderr)
        return 1
    payloads = [png_bytes(pixmap, size) for size in SIZES]
    TARGET.write_bytes(build_ico(payloads))
    print(f"已生成 {TARGET}（尺寸 {SIZES}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
