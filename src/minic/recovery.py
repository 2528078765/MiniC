"""崩溃恢复：工具执行 intent/result 日志与 interrupted 标记。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any


class ToolExecutionLog:
    """按行追加工具执行日志，供崩溃恢复使用。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, record: dict[str, Any]) -> None:
        """原子追加一条日志并 flush。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
            file.flush()

    def read(self) -> list[dict[str, Any]]:
        """读取全部日志。"""
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

    def has_result(self, run_id: str, tool_call_id: str) -> bool:
        """判断 run 内工具调用是否已有结果。"""
        return any(
            record.get("event") == "result"
            and record.get("run_id") == run_id
            and record.get("tool_call_id") == tool_call_id
            for record in self.read()
        )

    def mark_interrupted(self) -> None:
        """把没有 result 的 intent 标记为 interrupted。"""
        records = self.read()
        intents = [
            record
            for record in records
            if record.get("event") == "intent"
        ]
        completed = {
            (record.get("run_id"), record.get("tool_call_id"))
            for record in records
            if record.get("event") == "result"
        }
        for intent in intents:
            key = (intent.get("run_id"), intent.get("tool_call_id"))
            if key in completed:
                continue
            self.append(
                {
                    "event": "result",
                    "thread_id": intent.get("thread_id"),
                    "run_id": intent.get("run_id"),
                    "tool_call_id": intent.get("tool_call_id"),
                    "idempotency_key": intent.get("idempotency_key"),
                    "status": "interrupted",
                    "output": None,
                    "timestamp": datetime.now().astimezone().isoformat(),
                }
            )

    @staticmethod
    def idempotency_key(thread_id: str, tool: str, args: dict[str, Any]) -> str:
        """生成工具调用幂等键。"""
        normalized = json.dumps(args, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(f"{thread_id}:{tool}:{normalized}".encode("utf-8")).hexdigest()
