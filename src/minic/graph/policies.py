"""只对可安全重试的读取/决策节点启用原生重试，写工具不自动重试。"""
from langgraph.types import RetryPolicy
import httpx

def transient_error(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, httpx.TransportError)):
        return True
    status = getattr(exc, "status_code", None)
    return status in (408, 429, 500, 502, 503, 504)

READ_RETRY = RetryPolicy(max_attempts=3, initial_interval=0.5, backoff_factor=2,
                         max_interval=3, retry_on=transient_error)
