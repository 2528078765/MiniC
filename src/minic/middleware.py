"""S7/G10 中间件：PII、日志、限流、记忆注入、重试、摘要、沙箱策略。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar
from urllib.parse import urlsplit

import httpx
from langchain_core.messages import HumanMessage, SystemMessage


_API_KEY_RE = re.compile(r"sk-[A-Za-z0-9_-]{12,}")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID_CARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")


def redact_pii(text: str) -> str:
    """把 API Key、手机号、身份证号替换为脱敏标记。"""
    text = _API_KEY_RE.sub("[REDACTED_API_KEY]", text)
    text = _PHONE_RE.sub("[REDACTED_PHONE]", text)
    text = _ID_CARD_RE.sub("[REDACTED_ID_CARD]", text)
    return text


class RateLimiter:
    """基于窗口计数的简单限流器。"""

    def __init__(self, max_requests: int = 120, window_seconds: int = 60) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.history: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        """判断 key 是否允许本次请求。"""
        now = time.monotonic()
        records = self.history.setdefault(key, deque())
        while records and now - records[0] > self.window_seconds:
            records.popleft()
        if len(records) >= self.max_requests:
            return False
        records.append(now)
        return True


class RequestLogger:
    """把请求日志写入 JSONL，日志内容先做 PII 脱敏。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def log(self, method: str, path: str, status: int, duration_ms: float) -> None:
        """追加一条脱敏请求日志。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "method": method,
            "path": redact_pii(path),
            "status": status,
            "duration_ms": round(duration_ms, 3),
            "timestamp": datetime.now().astimezone().isoformat(),
        }
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
            file.flush()


T = TypeVar("T")


def is_retryable(exc: BaseException) -> bool:
    """判断异常是否属于可重试的网络或限流错误。"""
    message = str(exc).lower()
    if isinstance(exc, httpx.HTTPError):
        return True
    return any(keyword in message for keyword in ("timeout", "connection", "rate limit", "429", "502", "503"))


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    max_retries: int = 3,
) -> T:
    """只对网络或限流错误重试，其他异常直接抛出。"""
    for attempt in range(max_retries):
        try:
            return await operation()
        except Exception as exc:  # noqa: BLE001 - 需要判断异常类型
            if attempt == max_retries - 1 or not is_retryable(exc):
                raise
            await asyncio.sleep(0.5 * (attempt + 1))
    raise RuntimeError("重试失败")


def memory_injection(content: str, threshold_tokens: int = 4000) -> str:
    """按 token 估算注入长期记忆，超阈值只注入摘要。"""
    content = content.strip()
    if not content:
        return ""
    estimated_tokens = max(len(content) // 2, 1)
    if estimated_tokens <= threshold_tokens:
        return content
    return f"{content[:2000]}...（长期记忆过长，已截断为摘要）"


_SUMMARY_SYSTEM_PROMPT = "总结以下对话为简洁摘要，保留关键事实与未完成事项。只输出摘要文本，不要解释。"
_TRUNCATE_TAIL_CHARS = 4000


def estimate_chars(messages: list[dict[str, Any]]) -> int:
    """估算会话历史文本总字符数。"""
    return sum(len(str(message.get("content", ""))) for message in messages)


def _history_to_text(messages: list[dict[str, Any]]) -> str:
    """把会话历史消息列表转成纯文本。"""
    return "\n".join(
        f"{message.get('role', 'human')}: {message.get('content', '')}"
        for message in messages
    )


async def summarize_history(
    messages: list[dict[str, Any]],
    chat_model: Any,
    threshold_chars: int = 12000,
) -> list[dict[str, Any]]:
    """历史超阈值时用 chat_model 生成摘要，返回注入回答节点的历史列表。

    未超阈值直接返回原 history；超阈值时组装摘要提示并一次 ainvoke，
    网络错误走 retry_async，任何异常降级为截断（保留末尾文本 + 提示），绝不抛出。
    返回的历史列表把摘要置为首条，并保留原 history 的最后一条（本次用户消息），
    与图内 build_messages 的 ``history[:-1]`` + ``user_message`` 组装约定一致。
    """
    if estimate_chars(messages) <= threshold_chars:
        return messages
    summary = ""
    try:
        response = await retry_async(
            lambda: chat_model.ainvoke(
                [
                    SystemMessage(content=_SUMMARY_SYSTEM_PROMPT),
                    HumanMessage(content=_history_to_text(messages)),
                ]
            ),
            max_retries=3,
        )
        summary = str(getattr(response, "content", "") or "").strip()
    except Exception:  # noqa: BLE001 - 摘要失败降级为截断，绝不能破坏 SSE 流
        summary = ""
    history: list[dict[str, Any]] = []
    if summary:
        history.append({"role": "human", "content": f"历史摘要：{summary}"})
    else:
        text = _history_to_text(messages)
        tail = text[-_TRUNCATE_TAIL_CHARS:] if len(text) > _TRUNCATE_TAIL_CHARS else text
        history.append({"role": "human", "content": f"{tail}（历史过长已截断）"})
    if messages:
        history.append(messages[-1])  # 保留本次用户消息，供 build_messages 跳过
    return history


DEFAULT_HIGH_RISK_PATTERNS: list[str] = [
    "rm -rf /",
    "rm -rf ~",
    "format c:",
    "del /f /s /q c:",
    "shutdown /",
    "taskkill /f /im",
]

DEFAULT_MODEL_API_WHITELIST: list[str] = [
    "http://127.0.0.1:11434",
    "api.deepseek.com",
    "dashscope.aliyuncs.com",
]


def _url_host(value: str) -> str:
    """提取 URL 或裸域名/IP 的 host（忽略端口与路径）。"""
    text = str(value).strip()
    if not text:
        return ""
    if "://" in text:
        host = urlsplit(text).hostname
        return (host or "").lower()
    host = text.split("/", 1)[0]
    if ":" in host:  # 处理裸 host:port，取端口前部分
        host = host.rsplit(":", 1)[0]
    return host.lower()


class SandboxPolicy:
    """沙箱强制层：路径工具工作区内、Bash 危险命令黑名单、模型 API 域名白名单。"""

    PATH_TOOLS = {
        "Read", "Write", "Edit", "Format", "TextSearch", "Lint",
        "GitStatus", "GitDiff", "GitLog", "GitCommit", "GitBranch",
    }
    WRITE_PATH_TOOLS = {"Write", "Edit", "Format"}  # 带 path 参数的写工具：可放行 allowed_write_dirs 白名单（仍走审批）
    NO_CHECK_TOOLS = {"DelegateToSubagent"}

    def __init__(
        self,
        high_risk_patterns: list[str] | None = None,
        model_api_whitelist: list[str] | None = None,
        allowed_write_dirs: list[str] | None = None,
    ) -> None:
        self.high_risk_patterns = list(high_risk_patterns) if high_risk_patterns else DEFAULT_HIGH_RISK_PATTERNS
        self.model_api_whitelist = list(model_api_whitelist) if model_api_whitelist else DEFAULT_MODEL_API_WHITELIST
        self.allowed_write_dirs = [Path(item).expanduser().resolve() for item in (allowed_write_dirs or [])]

    def check_tool(self, tool: str, args: dict[str, Any], workspace: str | Path) -> str | None:
        """返回拒绝原因或 None；MCP 工具（名含 .）与 DelegateToSubagent 不检查。"""
        if "." in tool or tool in self.NO_CHECK_TOOLS:
            return None
        if tool == "Bash":
            return self._check_bash(str(args.get("command", "")))
        if tool in self.PATH_TOOLS:
            return self._check_path(tool, args, workspace)
        return None

    def _check_bash(self, command: str) -> str | None:
        """Bash 命令子串命中高危模式即拒绝。"""
        lowered = command.lower()
        for pattern in self.high_risk_patterns:
            if pattern.lower() in lowered:
                return "危险命令被沙箱策略拒绝"
        return None

    def _in_allowed_write_dirs(self, resolved: Path) -> bool:
        """路径是否位于 allowed_write_dirs 白名单任一目录内（G13 沙箱写放行）。"""
        return any(resolved.is_relative_to(entry) for entry in self.allowed_write_dirs)

    def _check_path(self, tool: str, args: dict[str, Any], workspace: str | Path) -> str | None:
        """路径类工具带 path 参数时必须位于工作区内；G13 写工具在白名单目录内放行（仍走审批）。"""
        raw_path = args.get("path")
        if raw_path is None:
            return None
        try:
            resolved = Path(str(raw_path)).expanduser().resolve()
        except (OSError, ValueError):
            return f"路径不在工作区内: {raw_path}"
        workspace_resolved = Path(str(workspace)).expanduser().resolve()
        if resolved.is_relative_to(workspace_resolved):
            return None
        if tool in self.WRITE_PATH_TOOLS and self._in_allowed_write_dirs(resolved):
            return None
        return f"路径不在工作区内: {raw_path}"

    def check_network(self, url: str) -> bool:
        """url 的 host 是否在模型 API 域名白名单内。"""
        host = _url_host(url)
        if not host:
            return False
        return any(_url_host(item) == host for item in self.model_api_whitelist)
