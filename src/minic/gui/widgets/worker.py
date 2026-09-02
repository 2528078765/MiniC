"""通用后台线程：在 QThread 中执行同步网络调用，用信号把结果抛回 UI 线程。"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QThread, Signal


class Worker(QThread):
    """在独立线程执行 ``fn``，完成后发 ``finished(result)``，异常发 ``failed(message)``。

    用法：:

        worker = Worker(lambda: client.get_settings())
        worker.completed.connect(on_result)
        worker.failed.connect(on_error)
        worker.start()

    运行期间保活：调用方创建后可能立即失去 Python 引用（局部变量），
    若不保活，Python 侧对象被回收会导致信号连接丢失（finished 回调不触发）。
    类级活跃集合在运行期间持有引用，完成后自动清理。
    """

    _ACTIVE: set["Worker"] = set()  # 运行中实例保活

    completed = Signal(object)       # 成功结果（dict/list/str/None）；不用 finished 名（与 QThread 自带信号冲突，槽不触发）
    failed = Signal(str)            # 异常信息

    def __init__(self, fn: Callable[[], Any], parent: Any | None = None) -> None:
        super().__init__(parent)
        self._fn = fn
        self.completed.connect(self._release)
        self.failed.connect(self._release)
        Worker._ACTIVE.add(self)

    def run(self) -> None:
        """线程体：执行任务并发射信号。"""
        try:
            result = self._fn()
        except Exception as exc:  # noqa: BLE001 - 网络/解析等任何异常都要回 UI 提示
            self.failed.emit(str(exc))
            return
        self.completed.emit(result)

    def _release(self, *_args: Any) -> None:
        """信号发出后从保活集合移除自身。"""
        Worker._ACTIVE.discard(self)
