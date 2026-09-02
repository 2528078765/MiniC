"""核心运行时锁测试。"""

from pathlib import Path

import pytest

from minic.core import runtime


def test_single_instance_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """第二个核心实例无法获取锁。"""
    monkeypatch.setattr(runtime, "get_runtime_dir", lambda: tmp_path)
    first = runtime.acquire_single_instance_lock()
    try:
        with pytest.raises(RuntimeError):
            runtime.acquire_single_instance_lock()
    finally:
        runtime.release_single_instance_lock(first)
    second = runtime.acquire_single_instance_lock()
    runtime.release_single_instance_lock(second)
