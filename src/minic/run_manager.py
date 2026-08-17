"""G11 断线续传/取消：RunManager 注册表与 SSE 事件日志。

- SseEventLog：把 /chat/stream 的 SSE 事件按 id=run_id:seq 原子追加到
  ``<project>/.minic/logs/sse_events.jsonl``，data 中的字符串值先做 PII 脱敏
  （redact_pii），run 结束后事件保留 24h，启动时清理过期事件。
- RunManager：run_id -> {thread_id, status, cancelled, queue} 内存注册表，
  提供取消标志、实时续传队列与事件发布；核心重启后把日志中没有 done 的 run
  标记为 interrupted（补一条 done(interrupted)），保证 events 回放能结束。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from minic.middleware import redact_pii


def _redact_value(value: Any) -> Any:
    """递归脱敏事件 data 中的字符串值（API Key/手机号/身份证号）。"""
    if isinstance(value, str):
        return redact_pii(value)
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


class SseEventLog:
    """按行追加的 SSE 事件日志，供断线重放与续传。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, run_id: str, seq: int, event_name: str, data: dict[str, Any]) -> None:
        """原子追加一条事件日志（data 脱敏后写盘并 flush）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "id": f"{run_id}:{seq}",
            "event": event_name,
            "data": _redact_value(data),
            "timestamp": datetime.now().astimezone().isoformat(),
        }
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
            file.flush()

    def _records(self) -> list[dict[str, Any]]:
        """读取全部日志记录（损坏行跳过）。"""
        if not self.path.exists():
            return []
        records = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    @staticmethod
    def _parse_seq(run_id: str, event_id: str) -> int | None:
        """从 id=run_id:seq 解析 seq；不属于该 run 返回 None。"""
        prefix = run_id + ":"
        if not event_id.startswith(prefix):
            return None
        try:
            return int(event_id[len(prefix):])
        except ValueError:
            return None

    def read_after(self, run_id: str, last_seq: int) -> list[dict[str, Any]]:
        """返回该 run 中 seq > last_seq 的事件列表（升序）。"""
        events = []
        for record in self._records():
            seq = self._parse_seq(run_id, str(record.get("id", "")))
            if seq is None or seq <= last_seq:
                continue
            events.append(
                {
                    "seq": seq,
                    "event": record.get("event"),
                    "data": record.get("data"),
                }
            )
        return events

    def last_seq(self, run_id: str) -> int:
        """该 run 当前最大 seq。"""
        max_seq = 0
        for record in self._records():
            seq = self._parse_seq(run_id, str(record.get("id", "")))
            if seq is not None and seq > max_seq:
                max_seq = seq
        return max_seq

    def has_done(self, run_id: str) -> bool:
        """该 run 是否已有 done 事件。"""
        for record in self._records():
            seq = self._parse_seq(run_id, str(record.get("id", "")))
            if seq is not None and record.get("event") == "done":
                return True
        return False

    def has_run(self, run_id: str) -> bool:
        """该 run 是否已有任意事件。"""
        for record in self._records():
            if self._parse_seq(run_id, str(record.get("id", ""))) is not None:
                return True
        return False

    def mark_interrupted(self) -> int:
        """把日志中所有没有 done 事件的 run 补一条 done(interrupted)，返回标记条数。

        核心重启后未完成 run 即视为 interrupted，不自动恢复；补上的 done 事件
        保证 GET events 回放时能以 done(interrupted) 正常结束。
        """
        run_ids = set()
        for record in self._records():
            event_id = str(record.get("id", ""))
            if ":" in event_id:
                run_ids.add(event_id.rsplit(":", 1)[0])
        marked = 0
        for run_id in run_ids:
            if self.has_done(run_id):
                continue
            seq = self.last_seq(run_id) + 1
            self.append(run_id, seq, "done", {"status": "interrupted"})
            marked += 1
        return marked

    def cleanup_expired(self, ttl_hours: int = 24) -> int:
        """清理超过 ttl_hours 的事件日志行（按 timestamp 判断），返回清理条数。

        时间戳缺失或无法解析的行保守保留，避免误删。
        """
        cutoff = datetime.now().astimezone() - timedelta(hours=ttl_hours)
        records = self._records()
        kept: list[dict[str, Any]] = []
        removed = 0
        for record in records:
            timestamp = record.get("timestamp")
            try:
                if timestamp and datetime.fromisoformat(str(timestamp)).astimezone() >= cutoff:
                    kept.append(record)
                else:
                    removed += 1
            except ValueError:
                kept.append(record)
        if removed:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(".tmp")
            with tmp_path.open("w", encoding="utf-8") as file:
                for record in kept:
                    file.write(json.dumps(record, ensure_ascii=False) + "\n")
            tmp_path.replace(self.path)
        return removed


class RunManager:
    """run 注册表：状态/取消标志 + SSE 事件日志 + 实时续传队列。"""

    def __init__(self, log_path: Path) -> None:
        self.event_log = SseEventLog(log_path)
        self.runs: dict[str, dict[str, Any]] = {}

    def register(self, run_id: str, thread_id: str) -> dict[str, Any]:
        """注册一个 run（status=running，cancelled=False）。"""
        record: dict[str, Any] = {
            "thread_id": thread_id,
            "status": "running",
            "cancelled": False,
            "queue": None,
        }
        self.runs[run_id] = record
        return record

    def get(self, run_id: str) -> dict[str, Any] | None:
        """查询 run 记录。"""
        return self.runs.get(run_id)

    def mark_cancelled(self, run_id: str) -> None:
        """置取消标志；由运行中的生成器循环检测并终止。"""
        record = self.runs.get(run_id)
        if record is not None:
            record["cancelled"] = True

    def finish(self, run_id: str, status: str) -> None:
        """更新 run 状态并清空实时队列。"""
        record = self.runs.get(run_id)
        if record is not None:
            record["status"] = status
            record["queue"] = None

    def mark_interrupted(self) -> int:
        """启动时把日志中没有 done 的 run 标记为 interrupted。"""
        return self.event_log.mark_interrupted()

    def cleanup_expired(self, ttl_hours: int = 24) -> int:
        """启动时清理超过 24h 的事件日志行。"""
        return self.event_log.cleanup_expired(ttl_hours=ttl_hours)

    def subscribe(self, run_id: str) -> asyncio.Queue | None:
        """给仍 running 的 run 挂实时续传队列；不存在或已结束返回 None。"""
        record = self.runs.get(run_id)
        if record is None or record["status"] != "running":
            return None
        queue: asyncio.Queue = asyncio.Queue()
        record["queue"] = queue
        return queue

    def publish(self, run_id: str, seq: int, event_name: str, data: dict[str, Any]) -> None:
        """写入事件日志；该 run 有订阅队列时实时转发。"""
        self.event_log.append(run_id, seq, event_name, data)
        record = self.runs.get(run_id)
        queue = record.get("queue") if record is not None else None
        if queue is not None:
            queue.put_nowait((event_name, data, seq))
