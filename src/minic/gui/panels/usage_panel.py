"""使用统计面板：真实 token 消耗总量（核心 /usage，来自 ~/.minic/usage.jsonl）。"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout

from minic.gui.panels.base import PanelBase, SettingCard
from minic.gui.theme import COLOR_ACCENT, COLOR_TEXT_SECONDARY
from minic.gui.widgets.worker import Worker


class UsagePanel(PanelBase):
    """使用统计：总 token 数量（用户输入 + AI 输出的 token 消耗总量）。"""

    def __init__(
        self,
        client: object | None = None,
        toast: Callable[[str], None] | None = None,
        parent=None,
        workspace: str | None = None,
        notify: Callable[[str, str], None] | None = None,
        progress: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__(client, toast, parent, workspace, notify, progress)
        self._reload_on_show = True

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(14)

        header = QHBoxLayout()
        title = QLabel("使用统计", self)
        title.setStyleSheet(
            f"color: #ffffff; font-size: 20px; font-weight: 600; background: transparent; border: none;"
        )
        header.addWidget(title)
        header.addStretch(1)
        root.addLayout(header)

        total_card = SettingCard("总 Token 消耗", "用户输入 + AI 输出的 token 总量（按对话轮次累计）。", self)
        self._total_label = QLabel("—", self)
        self._total_label.setStyleSheet(
            f"color: {COLOR_ACCENT}; font-size: 28px; font-weight: 700;"
            f"background: transparent; border: none;"
        )
        total_card.add_control(self._total_label)
        root.addWidget(total_card)

        detail_card = SettingCard("明细", "", self)
        self._detail_label = QLabel("", self)
        self._detail_label.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font-size: 13px; background: transparent; border: none;"
        )
        detail_card.add_control(self._detail_label)
        root.addWidget(detail_card)

        root.addStretch(1)

    def reload(self) -> None:
        """拉取 /usage 总量；核心未运行时本地读 usage.jsonl。"""
        if self.client is None or not getattr(self.client, "base_url", None):
            self._on_usage_loaded(self._load_local_usage())
            return
        worker = Worker(lambda: self.client.get_usage(), self)
        worker.completed.connect(self._on_usage_loaded)
        worker.failed.connect(lambda message: self._on_usage_loaded(self._load_local_usage()))
        worker.start()

    @staticmethod
    def _load_local_usage() -> dict[str, Any]:
        """本地汇总 ~/.minic/usage.jsonl。"""
        import json
        from pathlib import Path

        path = Path.home() / ".minic" / "usage.jsonl"
        total_prompt = total_completion = 0
        rounds = 0
        try:
            with path.open("r", encoding="utf-8") as file:
                for line in file:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    total_prompt += int(record.get("prompt_tokens", 0))
                    total_completion += int(record.get("completion_tokens", 0))
                    rounds += 1
        except OSError:
            pass
        return {
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "total_tokens": total_prompt + total_completion,
            "rounds": rounds,
        }

    def _on_usage_loaded(self, data: Any) -> None:
        """渲染总量。"""
        if not isinstance(data, dict):
            return
        total = int(data.get("total_tokens", 0))
        prompt = int(data.get("prompt_tokens", 0))
        completion = int(data.get("completion_tokens", 0))
        rounds = int(data.get("rounds", 0))
        self._total_label.setText(f"{total:,}")
        self._detail_label.setText(
            f"用户输入 {prompt:,} · AI 输出 {completion:,} · 共 {rounds} 轮对话"
        )
