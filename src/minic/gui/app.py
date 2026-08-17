"""MiniC 桌面端入口：QApplication → 应用 QSS 主题 → MainWindow → 显示。"""

from __future__ import annotations

import sys


def _redirect_streams() -> None:
    """打包后无控制台（windowed）：把 stdout/stderr 重定向到日志文件，避免 None 流崩溃。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    from pathlib import Path

    log_dir = Path.home() / ".minic" / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        stream = open(log_dir / "app.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream
    except OSError:
        pass


def main() -> int:
    """启动桌面端主窗口。

    用法：``python -m minic.gui.app``（可指定 ``--workspace <路径>``）。
    核心由桌面端自动在进程内拉起（后台线程），双击即用；
    已有核心在运行时直接复用，不重复启动。
    """
    _redirect_streams()

    from pathlib import Path

    from PySide6.QtWidgets import QApplication

    # Qt6 默认启用高 DPI，无需额外设置

    workspace: str | None = None
    args = sys.argv[1:]
    if "--workspace" in args:
        index = args.index("--workspace")
        if index + 1 < len(args):
            workspace = args[index + 1]
    if workspace is None and getattr(sys, "frozen", False):
        # 安装版/绿色版：cwd 可能是只读的安装目录或下载目录，
        # 无工作区时统一落 ~/.minic/work（与 CLI 无工作区行为一致）
        workspace = str(Path.home() / ".minic" / "work")

    # 首次启动自动生成 ~/.minic/minic.json（完整默认配置模板，用户无需手写）
    from minic.gui.local_data import ensure_default_config

    ensure_default_config()

    # 内嵌核心：双击即可用，无需命令行单独启动服务
    import threading

    from minic.gui.core_launcher import start_embedded_core, stop_embedded_core

    project_root = Path(workspace).resolve() if workspace else Path.cwd()

    app = QApplication(sys.argv)
    app.setApplicationName("MiniC")
    app.setOrganizationName("MiniC")
    app.aboutToQuit.connect(stop_embedded_core)

    from minic.gui.icons import load_icon

    app.setWindowIcon(load_icon("Log"))  # 窗口图标（Windows 任务栏显示此图标）

    from minic.gui.theme import QSS

    app.setStyleSheet(QSS)

    from minic.gui.main_window import MainWindow

    window = MainWindow(workspace=workspace)
    window.show()

    # 核心在窗口构建完成后才启动后台线程：主线程的 GUI 导入链已全部完成，
    # 核心线程再导入核心链路（graph/langchain/chromadb/mcp）不会与主线程
    # 并发首次导入同一模块——否则会触发 Python 3.13+ 模块锁 _DeadlockError
    # （打包后两线程并发 import minic.graph.* 必崩）。连接失败由主窗口自动重试兜底。
    threading.Thread(
        target=start_embedded_core,
        args=(project_root,),
        daemon=True,
        name="minic-core-launcher",
    ).start()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
