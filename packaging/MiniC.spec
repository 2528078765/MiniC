# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：MiniC 桌面端单文件 exe（内嵌核心，双击即用）。

用法（在项目根目录执行）：
    ``pyinstaller packaging/MiniC.spec --noconfirm --clean``
产物：``dist/MiniC.exe``

注意：spec 中的相对路径以 spec 所在目录为基准，统一用 SPECPATH 换算项目根。
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = __import__("pathlib").Path(SPECPATH).parent  # noqa: F821 - PyInstaller 注入 SPECPATH

datas: list = [(str(ROOT / "icon"), "icon")]
binaries: list = []
hiddenimports: list = []

# 动态导入较多的第三方包整体收集（数据/二进制/隐藏导入）
for package in (
    "langchain",
    "langchain_core",
    "langchain_openai",
    "langchain_deepseek",
    "langgraph",
    "langgraph.checkpoint.sqlite",
    "aiosqlite",
    "sqlite_vec",
    "langgraph_checkpoint",
    "langgraph_prebuilt",
    "langgraph_sdk",
    "mcp",
    "mcp_types",
    "httpx2",
    "chromadb",
    "pymilvus",
    "milvus_lite",
    "faiss",
    "onnxruntime",
    "rank_bm25",
    "tiktoken",
    "fastapi",
    "uvicorn",
    "starlette",
    "pydantic",
    "pydantic_core",
    "httpx",
    "httpcore",
    "yaml",
    "jsonschema",
    "jsonschema_specifications",
    "multipart",
):
    try:
        collected = collect_all(package)
        datas += collected[0]
        binaries += collected[1]
        hiddenimports += collected[2]
    except Exception:
        pass

hiddenimports += collect_submodules("minic")

# Windows 的 DLL 查找会使用 PATH。其他软件的同名 ICU/OpenSSL DLL
# 可能覆盖 Qt 所需的系统库，因此分析时只保留 Python 和 Windows 目录。
# 第三方包自身的 DLL 目录仍由对应的 PyInstaller hook 负责发现。
original_path = os.environ.get("PATH", "")
if sys.platform == "win32":
    system_root = Path(os.environ["SystemRoot"])
    os.environ["PATH"] = os.pathsep.join(dict.fromkeys([
        str(Path(sys.executable).parent),
        str(Path(sys.base_prefix)),
        str(system_root / "System32"),
        str(system_root),
    ]))
try:
    a = Analysis(
        [str(ROOT / "src" / "minic" / "gui" / "app.py")],
        pathex=[str(ROOT / "src")],
        binaries=binaries,
        datas=datas,
        hiddenimports=hiddenimports,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=["tkinter"],
        noarchive=False,
    )
finally:
    os.environ["PATH"] = original_path
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MiniC",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ROOT / "icon" / "Log.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
)
