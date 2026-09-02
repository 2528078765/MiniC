"""CoreClient 401 自愈测试：核心重启令牌轮换后自动重读 runtime.json 重试。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from minic.gui.core_client import CoreClient


class _FakeResponse:
    """带状态码与 JSON 的假响应。"""

    def __init__(self, status_code: int, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """假 httpx.Client：第一次请求 401，第二次 200（记录请求头）。"""

    def __init__(self, *args, **kwargs) -> None:
        self.calls: list[dict] = []
        self.count = 0
        self.statuses: list[int] = []

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *args) -> None:
        return None

    def get(self, url: str, headers=None, params=None) -> _FakeResponse:
        self.calls.append({"url": url, "headers": headers, "params": params})
        self.count += 1
        if self.count == 1:
            self.statuses.append(401)
            return _FakeResponse(401)
        self.statuses.append(200)
        return _FakeResponse(200, {"ok": True, "token_seen": headers["Authorization"]})

    def put(self, url: str, headers=None, json=None) -> _FakeResponse:
        self.calls.append({"url": url, "headers": headers, "json": json})
        self.count += 1
        if self.count == 1:
            self.statuses.append(401)
            return _FakeResponse(401)
        self.statuses.append(200)
        return _FakeResponse(200, {})


def test_get_json_refreshes_token_and_retries_on_401(tmp_path: Path, monkeypatch) -> None:
    """401 时重读 runtime.json 得到新 token 并重试成功。"""
    client = CoreClient()
    client.base_url = "http://127.0.0.1:1"  # 旧地址
    client.token = "old-token"

    fake_http = _FakeClient()
    monkeypatch.setattr("minic.gui.core_client.httpx.Client", lambda **kwargs: fake_http)
    new_runtime = tmp_path / "runtime.json"
    new_runtime.write_text(
        json.dumps({"port": 8765, "token": "new-token"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "minic.gui.core_client.read_runtime",
        lambda: {"port": 8765, "token": "new-token"},
    )

    result = client.get_json("/settings")

    assert result == {"ok": True, "token_seen": "Bearer new-token"}
    assert fake_http.statuses == [401, 200]  # 首次 401，刷新后重试 200
    assert client.token == "new-token"
    assert client.base_url == "http://127.0.0.1:8765"
    # 第二次请求带新 token
    assert fake_http.calls[1]["headers"]["Authorization"] == "Bearer new-token"


def test_put_json_retries_on_401(tmp_path: Path, monkeypatch) -> None:
    """put_json 401 → 刷新 token 重试后成功（保存设置不再误报失败）。"""
    from minic.gui.core_client import CoreClient as _C

    client = _C()
    client.base_url = "http://127.0.0.1:1"
    client.token = "old"

    fake_http = _FakeClient()
    monkeypatch.setattr("minic.gui.core_client.httpx.Client", lambda **kwargs: fake_http)
    monkeypatch.setattr(
        "minic.gui.core_client.read_runtime",
        lambda: {"port": 8765, "token": "fresh"},
    )

    assert client.put_json("/settings", {"scope": "global"}) is True
    assert fake_http.statuses == [401, 200]


def test_stream_chat_uses_bounded_timeout(monkeypatch) -> None:
    """SSE 流使用有限超时（connect 5s / read 120s），不再无限等导致永久转圈。"""
    import httpx as _httpx

    captured: dict = {}

    class _Client:
        def __init__(self, timeout=None, **kwargs):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def stream(self, *args, **kwargs):
            raise _httpx.ConnectError("boom")

    monkeypatch.setattr("minic.gui.core_client.httpx.Client", _Client)
    client = CoreClient()
    client.base_url = "http://127.0.0.1:8765"
    client.token = "t"
    errors: list[str] = []
    client.chat_error.connect(lambda msg, run: errors.append(msg))

    client.stream_chat(None, "C:/w", "你好", run_key="k")
    assert errors  # 连接失败必须报错（不再静默）
    timeout = captured["timeout"]
    assert timeout.connect == 5.0
    assert timeout.read == 120.0


class _StreamFakeResponse:
    """模拟流式响应：带状态码与 SSE 行。"""

    def __init__(self, status_code: int, lines: list[str] | None = None) -> None:
        self.status_code = status_code
        self._lines = lines or []

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_lines(self):
        for line in self._lines:
            yield line


class _StreamFakeClient:
    """模拟 httpx：记录是否以 with 进入 stream（真正发送的判据）。"""

    def __init__(self, statuses: list[int], *args, **kwargs) -> None:
        self.statuses = statuses
        self.stream_entered = 0  # with 进入次数 = 真实发送次数
        self.attempt = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def stream(self, method: str, url: str, **kwargs):
        fake = _StreamFakeClient._StreamCtx(self)
        return fake

    class _StreamCtx:
        def __init__(self, owner: "_StreamFakeClient"):
            self.owner = owner
            self.response = None

        def __enter__(self) -> _StreamFakeResponse:
            self.owner.stream_entered += 1
            status = self.owner.statuses[min(self.owner.attempt, len(self.owner.statuses) - 1)]
            self.owner.attempt += 1
            lines = ["event: done", "data: {}", ""] if status == 200 else []
            self.response = _StreamFakeResponse(status, lines)
            return self.response

        def __exit__(self, *args):
            return None


def test_stream_chat_actually_sends_request(monkeypatch) -> None:
    """回归：流式请求必须通过 with 真正发送（此前丢 with 导致请求从未到核心）。"""
    fake_http = _StreamFakeClient(statuses=[200])
    monkeypatch.setattr("minic.gui.core_client.httpx.Client", lambda **kwargs: fake_http)
    client = CoreClient()
    client.base_url = "http://127.0.0.1:8765"
    client.token = "t"
    events: list[tuple[str, dict]] = []
    client.chat_event.connect(lambda name, data: events.append((name, data)))

    client.stream_chat(None, "C:/w", "你好", run_key="k")

    assert fake_http.stream_entered == 1  # 请求真正发送了 1 次
    assert events == [("done", {"_run": "k"})]


def test_stream_chat_retries_once_on_401(monkeypatch) -> None:
    """401 自愈：with 发送 → 401 → 刷新 token → 重试 → 200，共发送 2 次且第二次带新 token。"""
    monkeypatch.setattr(
        "minic.gui.core_client.read_runtime",
        lambda: {"port": 8765, "token": "fresh"},
    )
    fake_http = _StreamFakeClient(statuses=[401, 200])
    monkeypatch.setattr("minic.gui.core_client.httpx.Client", lambda **kwargs: fake_http)
    client = CoreClient()
    client.base_url = "http://127.0.0.1:1"
    client.token = "old"
    events: list[tuple[str, dict]] = []
    client.chat_event.connect(lambda name, data: events.append((name, data)))

    client.stream_chat(None, "C:/w", "你好", run_key="k")

    assert fake_http.stream_entered == 2  # 401 后重试一次，共 2 次真实发送
    assert client.token == "fresh"
