"""S7 中间件与命令测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from minic.chat.memory import summarize_messages
from minic.core.config import AppSettings
from minic.core.server import create_app
from minic.middleware import (
    RateLimiter,
    RequestLogger,
    is_retryable,
    memory_injection,
    redact_pii,
    retry_async,
)


HEADERS = {"Authorization": "Bearer test-token"}


def test_redact_pii() -> None:
    """手机号、身份证和 API Key 被脱敏。"""
    text = "电话 13812345678，身份证 110101199001011234，密钥 sk-abcdefghijklmnop123456"
    redacted = redact_pii(text)
    assert "13812345678" not in redacted
    assert "110101199001011234" not in redacted
    assert "sk-abcdefghijklmnop123456" not in redacted
    assert "[REDACTED_PHONE]" in redacted
    assert "[REDACTED_ID_CARD]" in redacted
    assert "[REDACTED_API_KEY]" in redacted


def test_rate_limiter() -> None:
    """超过窗口上限后拒绝请求。"""
    limiter = RateLimiter(max_requests=2, window_seconds=60)
    assert limiter.allow("user")
    assert limiter.allow("user")
    assert not limiter.allow("user")
    assert limiter.allow("other")


def test_request_logger_redacts(tmp_path: Path) -> None:
    """请求日志写入脱敏后的路径。"""
    logger = RequestLogger(tmp_path / "requests.jsonl")
    logger.log("GET", "/chat?key=sk-abcdefghijklmnop123456", 200, 12.5)
    content = (tmp_path / "requests.jsonl").read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnop123456" not in content
    assert "[REDACTED_API_KEY]" in content


def test_retry_only_retryable_errors() -> None:
    """网络错误会重试，业务错误不重试。"""
    attempts = 0

    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("connection failed")
        return "ok"

    assert asyncio.run(retry_async(flaky, max_retries=3)) == "ok"
    assert attempts == 3
    assert is_retryable(httpx.ConnectError("connection failed"))
    assert not is_retryable(ValueError("bad value"))


def test_memory_injection_and_summary() -> None:
    """短记忆全文注入，长记忆截断，消息摘要可生成。"""
    assert memory_injection("短内容", threshold_tokens=4000) == "短内容"
    assert "截断" in memory_injection("长" * 20000, threshold_tokens=100)
    summary = summarize_messages([{"role": "human", "content": "你好"}])
    assert "你好" in summary


@pytest.fixture
def middleware_client(tmp_path: Path) -> TestClient:
    """使用低限流阈值的中间件测试客户端。"""
    settings = AppSettings(
        model={"provider": "mock"},
        embedding={"provider": "mock", "dimension": 64},
        rate_limit={"max_requests": 2, "window_seconds": 60},
    )
    app = create_app(
        settings=settings,
        token="test-token",
        project_root=tmp_path,
        rag_data_dir=tmp_path / "rag-data",
        short_memory_dir=tmp_path / "memory" / "short_memory",
        global_memory_dir=tmp_path / "global-memory",
        global_permissions_path=tmp_path / "global-permissions.json",
    )
    return TestClient(app)


def test_rate_limit_middleware(middleware_client: TestClient) -> None:
    """连续超过限流阈值后返回 429。"""
    assert middleware_client.get("/health").status_code == 200
    assert middleware_client.get("/health").status_code == 200
    response = middleware_client.get("/health")
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "RATE_LIMITED"
