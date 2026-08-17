"""模型设置面板：供应商管理（左列表 + 右表单）+ Embedding 表单。"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from minic.gui.local_data import local_settings_raw
from minic.gui.panels.base import PanelBase, SettingCard, sub_note
from minic.gui.theme import (
    COLOR_ACCENT,
    COLOR_BG_ACTIVE,
    COLOR_BG_CARD,
    COLOR_BG_HOVER,
    COLOR_BG_MAIN,
    COLOR_BORDER,
    COLOR_GREEN,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_SECONDARY,
)
from minic.gui.icons import load_pixmap
from minic.gui.widgets.worker import Worker

# provider 显示名 → 核心 provider 取值
_PROVIDER_LABELS = {
    "deepseek": "DeepSeek",
    "ollama": "Ollama（本地）",
    "dashscope": "DashScope",
    "zhipu": "智谱 GLM",
}


class ProviderItem(QFrame):
    """供应商列表项：图标 + 名称，点击选中。"""

    clicked = Signal(str)

    def __init__(
        self,
        name: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.name = name
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("ProviderItem")
        self.setStyleSheet(
            f"#ProviderItem {{ background: transparent; border: none; border-radius: 6px; }}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(8)

        icon_label = QLabel(self)
        icon_label.setPixmap(load_pixmap("模型供应商", 16))
        icon_label.setFixedSize(18, 18)
        icon_label.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(icon_label)

        name_label = QLabel(name, self)
        name_label.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; background: transparent; border: none; font-size: 13px;"
        )
        layout.addWidget(name_label, 1)

        self.setFixedHeight(34)

    def set_active(self, active: bool) -> None:
        """切换选中态背景。"""
        if active:
            self.setStyleSheet(
                f"#ProviderItem {{ background-color: {COLOR_BG_ACTIVE}; border: none; border-radius: 6px; }}"
            )
        else:
            self.setStyleSheet(
                f"#ProviderItem {{ background: transparent; border: none; border-radius: 6px; }}"
                f"#ProviderItem:hover {{ background-color: {COLOR_BG_HOVER}; }}"
            )

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt 接口命名
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.name)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ModelPanel(PanelBase):
    """模型设置面板：管理模型供应商（左侧列表 + 右侧表单）与 Embedding 配置。"""

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
        # 本地供应商注册表：name -> {name, base_url, model, api_key}
        self._providers: dict[str, dict[str, Any]] = {}
        self._selected: str | None = None
        self._provider_items: list[ProviderItem] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(14)

        # ---- 面板级「编辑/保存」按钮（右上角）----
        edit_row = QHBoxLayout()
        edit_row.addStretch(1)
        self._make_edit_button(edit_row)
        root.addLayout(edit_row)

        # ---- 供应商管理 ----
        manage = QFrame(self)
        manage.setStyleSheet(
            f"#Mgr {{ background-color: {COLOR_BG_CARD}; border: 1px solid {COLOR_BORDER};"
            f"border-radius: 10px; }}"
        )
        manage.setObjectName("Mgr")
        mgr_layout = QHBoxLayout(manage)
        mgr_layout.setContentsMargins(0, 0, 0, 0)
        mgr_layout.setSpacing(0)

        # 左栏：供应商列表
        left = QWidget(manage)
        left.setFixedWidth(230)
        left.setStyleSheet(f"background-color: {COLOR_BG_CARD}; border: none;")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 10, 8, 10)
        left_layout.setSpacing(2)

        group = QLabel("自定义供应商", left)
        group.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 11px; background: transparent;"
            f"border: none; padding: 6px 10px 4px;"
        )
        left_layout.addWidget(group)

        provider_scroll = QScrollArea(left)
        provider_scroll.setWidgetResizable(True)
        provider_scroll.setStyleSheet(
            f"QScrollArea {{ background: transparent; border: none; }}"
            f"QScrollArea > QWidget > QWidget {{ background: transparent; }}"
        )
        provider_container = QWidget()
        self._provider_layout = QVBoxLayout(provider_container)
        self._provider_layout.setContentsMargins(0, 0, 0, 0)
        self._provider_layout.setSpacing(2)
        self._provider_layout.addStretch(1)
        provider_scroll.setWidget(provider_container)
        left_layout.addWidget(provider_scroll, 1)

        add_btn = QPushButton("＋ 添加供应商", left)
        add_btn.setStyleSheet(
            f"background: transparent; color: {COLOR_TEXT_SECONDARY};"
            f"border: 1px dashed {COLOR_BORDER}; border-radius: 6px; padding: 7px 10px;"
            f"font-size: 13px; text-align: center;"
        )
        add_btn.clicked.connect(self._add_provider)
        left_layout.addWidget(add_btn)

        mgr_layout.addWidget(left)

        # 右栏：配置表单
        form = QWidget(manage)
        form.setStyleSheet(f"background-color: {COLOR_BG_CARD}; border: none;")
        form_layout = QVBoxLayout(form)
        form_layout.setContentsMargins(20, 18, 20, 18)
        form_layout.setSpacing(12)

        form_title = QLabel("添加模型供应商", form)
        form_title.setStyleSheet(
            f"color: #ffffff; font-size: 15px; font-weight: 600; background: transparent; border: none;"
        )
        form_layout.addWidget(form_title)

        form_desc = QLabel("配置一个完全自定义的 API 端点和初始模型。", form)
        form_desc.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 12px; background: transparent; border: none;"
        )
        form_layout.addWidget(form_desc)

        self._form_title = form_title
        self._name_input = self._make_form_row(form_layout, "Provider", "如：DeepSeek（供应商名称）", form)
        self._name_input.textChanged.connect(self._on_name_changed)
        self._url_input = self._make_form_row(
            form_layout, "Base URL", "https://api.example.com/v1", form, hint="使用兼容 OpenAI 的 baseurl"
        )
        self._model_input = self._make_form_row(form_layout, "模型", "如：deepseek-v4-flash", form)
        self._key_input = self._make_form_row(
            form_layout, "API Key", "sk-…（仅本地存储，不入库）", form, password=True
        )

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self._save_btn = QPushButton("保存", form)
        self._save_btn.clicked.connect(self.save)
        test_btn = QPushButton("测试连接", form)
        test_btn.clicked.connect(self._test_model_connection)
        actions.addWidget(self._save_btn)
        actions.addWidget(test_btn)
        actions.addStretch(1)
        form_layout.addLayout(actions)
        form_layout.addStretch(1)

        mgr_layout.addWidget(form, 1)
        root.addWidget(manage)

        # ---- Embedding 表单 ----
        embedding_card = SettingCard(
            "Embedding",
            "仅全局配置（单一向量空间）。",
            self,
        )
        embody = QWidget(embedding_card)
        embody_layout = QVBoxLayout(embody)
        embody_layout.setContentsMargins(0, 0, 0, 0)
        embody_layout.setSpacing(12)

        provider_row = QHBoxLayout()
        provider_label = QLabel("Provider", embody)
        provider_label.setFixedWidth(90)
        provider_label.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; background: transparent; border: none;"
        )
        provider_row.addWidget(provider_label)
        # 自由文本输入（与大模型设置一致，不限制为固定供应商）
        provider_column = QVBoxLayout()
        provider_column.setSpacing(3)
        self._emb_provider = QLineEdit(embody)
        self._emb_provider.setPlaceholderText("如：dashscope（阿里云百炼）/ openai / ollama")
        self._emb_provider.setStyleSheet(
            f"background-color: {COLOR_BG_MAIN}; color: #ffffff;"
            f"border: 1px solid {COLOR_BORDER}; border-radius: 6px; padding: 6px 10px;"
        )
        provider_column.addWidget(self._emb_provider)
        provider_hint = QLabel("兼容 OpenAI 的供应商名，可自行填写", embody)
        provider_hint.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 11px; background: transparent; border: none;"
        )
        provider_column.addWidget(provider_hint)
        provider_row.addLayout(provider_column, 1)
        embody_layout.addLayout(provider_row)

        self._emb_model = self._make_form_row(
            embody_layout, "模型", "如：text-embedding-v3", embody
        )
        self._emb_url = self._make_form_row(
            embody_layout, "Base URL", "https://dashscope.aliyuncs.com", embody,
            hint="原生接口填 https://dashscope.aliyuncs.com；OpenAI 兼容填 https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        self._emb_key = self._make_form_row(
            embody_layout, "API Key", "sk-…（仅本地存储，不入库）", embody, password=True
        )

        emb_actions = QHBoxLayout()
        emb_test = QPushButton("测试连接", embody)
        emb_test.clicked.connect(self._test_embedding_connection)
        emb_actions.addWidget(emb_test)
        emb_actions.addStretch(1)
        # Embedding 自己的「编辑/保存」按钮（独立于模型卡）
        self._emb_edit_btn = QPushButton("编辑", embody)
        self._emb_edit_btn.setStyleSheet(
            f"background: transparent; color: {COLOR_ACCENT};"
            f"border: 1px solid {COLOR_ACCENT}; border-radius: 6px; padding: 4px 14px;"
            f"font-size: 12px;"
        )
        self._emb_edit_btn.clicked.connect(self._on_emb_edit_clicked)
        emb_actions.addWidget(self._emb_edit_btn)
        embody_layout.addLayout(emb_actions)

        embedding_card.add_body(embody)
        root.addWidget(embedding_card)

        root.addWidget(sub_note("保存写入核心全局配置（scope=global）；API Key 仅本地保留，不入库。", self))
        root.addStretch(1)

        # 编辑分组：模型卡与 Embedding 卡各自编辑/保存；测试连接始终可点
        self.register_editable(
            add_btn,
            self._name_input, self._url_input, self._model_input, self._key_input,
            self._save_btn,
        )
        self._emb_edit_widgets = [self._emb_provider, self._emb_model, self._emb_url, self._emb_key]
        for widget in self._emb_edit_widgets:
            widget.setEnabled(False)  # Embedding 初始只读，由自己的编辑按钮控制
        self._emb_edit_on = False
        self._emb_dirty = False

        # Embedding 已保存值快照（放弃修改时回滚用）
        self._emb_snapshot: dict[str, str] = {
            "provider": self._emb_provider.text(),
            "model": self._emb_model.text(),
            "base_url": self._emb_url.text(),
            "api_key": self._emb_key.text(),
        }

    # ---- 表单构建辅助 ----

    def _make_form_row(
        self,
        layout: QVBoxLayout,
        label_text: str,
        placeholder: str,
        parent: QWidget,
        hint: str | None = None,
        password: bool = False,
    ) -> QLineEdit:
        """构造表单行：标签 + 输入框（可选提示）。"""
        row = QHBoxLayout()
        label = QLabel(label_text, parent)
        label.setFixedWidth(90)
        label.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; background: transparent; border: none;")
        row.addWidget(label)

        column = QVBoxLayout()
        column.setSpacing(3)
        edit = QLineEdit(parent)
        edit.setPlaceholderText(placeholder)
        if password:
            edit.setEchoMode(QLineEdit.EchoMode.Password)
        edit.setStyleSheet(
            f"background-color: {COLOR_BG_MAIN}; color: #ffffff;"
            f"border: 1px solid {COLOR_BORDER}; border-radius: 6px; padding: 6px 10px;"
        )
        column.addWidget(edit)
        if hint:
            hint_label = QLabel(hint, parent)
            hint_label.setStyleSheet(
                f"color: {COLOR_TEXT_MUTED}; font-size: 11px; background: transparent; border: none;"
            )
            column.addWidget(hint_label)
        row.addLayout(column, 1)
        layout.addLayout(row)
        return edit

    # ---- 数据加载 ----

    def reload(self) -> None:
        """加载配置：直接本地读 ~/.minic/minic.json（保留 api_key 回显，仅本机）。"""
        self._on_settings_loaded(local_settings_raw(self.workspace))

    def _on_settings_loaded(self, data: Any) -> None:
        """用 /settings 数据预填（model.models 多模型列表，兼容旧单对象格式）。"""
        if not isinstance(data, dict):
            return
        model_cfg = data.get("model") or {}
        models = model_cfg.get("models")
        if not isinstance(models, list):  # 旧格式单对象兼容
            models = [model_cfg] if model_cfg.get("provider") or model_cfg.get("model") else []
        for entry in models:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if not name:  # 无名字时按 provider 显示名兜底
                name = _PROVIDER_LABELS.get(str(entry.get("provider", "")), str(entry.get("provider", "")))
            if name not in self._providers:
                self._providers[name] = {
                    "name": name,
                    "provider": str(entry.get("provider", "deepseek")),
                    "base_url": str(entry.get("base_url", "") or ""),
                    "model": str(entry.get("model", "") or ""),
                    "api_key": str(entry.get("api_key", "") or ""),
                    "enabled": bool(entry.get("enabled", True)),
                }
        self._render_providers()
        if self._providers:
            self._select_provider(next(iter(self._providers)))

        embedding_cfg = data.get("embedding") or {}
        self._emb_provider.setText(str(embedding_cfg.get("provider", "dashscope")))
        self._emb_model.setText(str(embedding_cfg.get("model", "") or ""))
        self._emb_url.setText(str(embedding_cfg.get("base_url", "") or ""))
        self._emb_key.setText(str(embedding_cfg.get("api_key", "") or ""))  # 本地明文回显（仅本机）
        # 更新已保存值快照（放弃修改时回滚基准）
        self._emb_snapshot = {
            "provider": self._emb_provider.text(),
            "model": self._emb_model.text(),
            "base_url": self._emb_url.text(),
            "api_key": self._emb_key.text(),
        }

    # ---- 供应商列表 ----

    def _render_providers(self) -> None:
        """重建供应商列表（所有已添加的模型均为可用，选谁用谁由输入框下拉决定）。"""
        # 清空现有项（保留末尾 stretch）
        for item in self._provider_items:
            self._provider_layout.removeWidget(item)
            item.deleteLater()
        self._provider_items = []
        for name in self._providers:
            item = ProviderItem(name, parent=self)
            item.clicked.connect(self._select_provider)
            item.set_active(name == self._selected)
            self._provider_layout.insertWidget(self._provider_layout.count() - 1, item)
            self._provider_items.append(item)

    def _select_provider(self, name: str) -> None:
        """选中供应商并预填表单。"""
        self._selected = name
        for item in self._provider_items:
            item.set_active(item.name == name)
        provider = self._providers.get(name)
        if provider is None:
            return
        self._form_title.setText(name)
        self._name_input.setText(provider.get("name", ""))
        self._url_input.setText(provider.get("base_url", ""))
        self._model_input.setText(provider.get("model", ""))
        self._key_input.setText(provider.get("api_key", ""))

    def _add_provider(self) -> None:
        """新增供应商条目并清空表单（默认启用）。"""
        index = len(self._providers) + 1
        name = f"新供应商 {index}"
        self._providers[name] = {
            "name": "", "provider": "deepseek", "base_url": "",
            "model": "", "api_key": "", "enabled": True,
        }
        self._render_providers()
        self._select_provider(name)
        self._name_input.setFocus()
        self.toast(f"已添加供应商：{name}")

    def _on_name_changed(self, text: str) -> None:
        """编辑态改名称时即时同步注册表与列表项（改名不依赖保存）。"""
        if not self._edit_mode or not self._selected:
            return
        new_name = text.strip()
        if not new_name or new_name == self._selected:
            return
        provider = self._providers.pop(self._selected, {})
        provider["name"] = new_name
        self._providers[new_name] = provider
        self._selected = new_name
        self._form_title.setText(new_name)
        self._render_providers()  # 重建列表显示新名（不重填表单，保留正在输入的字段）

    # ---- 保存 / 测试 ----

    def _collect_form(self) -> dict[str, Any]:
        """收集表单内容并写回注册表（保留 enabled 开关状态）。"""
        name = self._name_input.text().strip() or (self._selected or "")
        provider = {
            "name": name,
            "provider": (self._providers.get(self._selected) or {}).get("provider", "deepseek"),
            "base_url": self._url_input.text().strip(),
            "model": self._model_input.text().strip(),
            "api_key": self._key_input.text(),
            "enabled": bool((self._providers.get(self._selected) or {}).get("enabled", True)),
        }
        if self._selected and name != self._selected:
            self._providers.pop(self._selected, None)
        self._providers[name] = provider
        self._selected = name
        self._render_providers()
        self._select_provider(name)
        return provider

    def save(self) -> bool:
        """保存模型卡（多模型列表），成功后退出模型卡编辑态回到只读。

        返回是否已保存（False=必填字段缺失，保持编辑态）。
        """
        provider = self._collect_form()
        if not provider["name"] or not provider["base_url"] or not provider["model"]:
            self.toast("请填写 Provider、Base URL 与模型")
            return False  # 校验失败保持编辑态
        self._submit_model(provider)
        self.set_edit_mode(False)
        return True

    def _on_emb_edit_clicked(self) -> None:
        """Embedding 编辑/保存按钮：独立于模型卡的编辑分组。"""
        if not self._emb_edit_on:
            self._emb_edit_on = True
            self._emb_dirty = True
            for widget in self._emb_edit_widgets:
                widget.setEnabled(True)
            self._emb_edit_btn.setText("保存")
            return
        self._submit_embedding()
        self._emb_edit_on = False
        self._emb_dirty = False
        for widget in self._emb_edit_widgets:
            widget.setEnabled(False)
        self._emb_edit_btn.setText("编辑")
        self._emb_snapshot = {
            "provider": self._emb_provider.text(),
            "model": self._emb_model.text(),
            "base_url": self._emb_url.text(),
            "api_key": self._emb_key.text(),
        }

    def has_unsaved_changes(self) -> bool:
        """未保存 = 模型卡编辑态或 Embedding 编辑态。"""
        return self._dirty or self._emb_dirty

    def discard(self) -> None:
        """放弃未保存修改：回滚模型卡与 Embedding 卡。"""
        super().discard()
        self._emb_edit_on = False
        self._emb_dirty = False
        for widget in self._emb_edit_widgets:
            widget.setEnabled(False)
        self._emb_edit_btn.setText("编辑")

    def on_discard(self) -> None:
        """放弃修改：回滚供应商表单与 Embedding 控件到已保存值。"""
        if self._selected and self._selected in self._providers:
            self._select_provider(self._selected)
        else:
            # 无已保存供应商：清空表单回到初始状态
            self._selected = None
            self._form_title.setText("添加模型供应商")
            self._name_input.setText("")
            self._url_input.setText("")
            self._model_input.setText("")
            self._key_input.setText("")
        snapshot = self._emb_snapshot or {}
        self._emb_provider.setText(snapshot.get("provider", ""))
        self._emb_model.setText(snapshot.get("model", ""))
        self._emb_url.setText(snapshot.get("base_url", ""))
        self._emb_key.setText(snapshot.get("api_key", ""))  # 回滚到已保存的 api_key（本地明文）

    def _put_settings_waiting(self, payload: dict[str, Any]) -> bool:
        """核心未就绪时轮询等待（最多 30 秒），就绪后再提交设置。

        双击启动后核心在后台拉起（约 14 秒），若用户立刻保存会连接失败；
        等待就绪后提交，仍失败则抛 RuntimeError 让通知条显示原因。
        """
        client = self.client
        deadline = time.monotonic() + 30.0
        while client.base_url is None and time.monotonic() < deadline:
            time.sleep(1.0)
        if client.base_url is None:
            raise RuntimeError("核心未就绪，请稍后重试")
        return client.put_settings(payload)

    def _submit_model(self, provider: dict[str, Any]) -> None:
        """提交模型配置到核心（全局 scope，models 列表含 enabled 与 api_key 明文）。"""
        if self.client is None:
            self.toast("核心未运行，保存仅保留在本地")
            return
        models = []
        for item in self._providers.values():  # 全部供应商/模型配置一起提交
            models.append(
                {
                    "name": item.get("name") or item.get("display", ""),
                    "provider": item.get("provider") or "deepseek",
                    "base_url": item.get("base_url", ""),
                    "model": item.get("model", ""),
                    "api_key": item.get("api_key", "") or None,
                    "enabled": True,  # 启用开关已移除，所有已添加的模型均可用
                }
            )
        payload = {"scope": "global", "model": {"models": models}}
        worker = Worker(lambda: self._put_settings_waiting(payload), self)
        worker.completed.connect(
            lambda ok: self.notify("已保存模型配置（下拉框可选）", "success") if ok else self.notify("保存失败", "failed")
        )
        worker.failed.connect(lambda message: self.notify(f"保存失败：{message}", "failed"))
        worker.start()

    def _submit_embedding(self) -> None:
        """提交 Embedding 配置到核心（全局 scope，api_key 本地明文存储）。"""
        if self.client is None:
            return
        payload = {
            "scope": "global",
            "embedding": {
                "provider": self._emb_provider.text().strip(),
                "base_url": self._emb_url.text().strip(),
                "model": self._emb_model.text().strip(),
                "api_key": self._emb_key.text() or None,
            },
        }
        worker = Worker(lambda: self._put_settings_waiting(payload), self)
        worker.completed.connect(
            lambda ok: self.notify("已保存 Embedding 配置", "success") if ok else self.notify("保存失败", "failed")
        )
        worker.failed.connect(lambda message: self.notify(f"保存失败：{message}", "failed"))
        worker.start()

    def _test_model_connection(self) -> None:
        """测试模型供应商连接（顶部通知条反馈：成功 xx ms / 失败原因）。"""
        provider = self._collect_form()
        base_url = provider["base_url"].rstrip("/")
        if not base_url:
            self.notify("测试连接失败：请先填写 Base URL", "failed")
            return
        api_key = provider["api_key"]
        started = time.monotonic()

        def _test() -> bool:
            import httpx

            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            with httpx.Client(timeout=10.0) as client:
                response = client.get(f"{base_url}/models", headers=headers)
                response.raise_for_status()
                return True

        worker = Worker(_test, self)
        worker.completed.connect(
            lambda ok: self.notify(
                f"测试连接成功：{int((time.monotonic() - started) * 1000)} ms", "success"
            )
        )
        worker.failed.connect(
            lambda message: self.notify(f"测试连接失败：{message}", "failed")
        )
        worker.start()

    def _test_embedding_connection(self) -> None:
        """测试 Embedding 连接：按 provider 真实发一次最小 embedding 请求。

        openai 兼容走 POST {base_url}/embeddings；dashscope 原生走 POST
        {base_url}/api/v1/services/embeddings/text-embedding/text-embedding；
        ollama 走 POST {base_url}/api/embed。旧实现探测 GET /embeddings 对
        上述接口均不存在（只接受 POST），必然失败，已废弃。
        失败时尝试 GET {base_url}/models 列出可用模型名辅助排查。
        """
        provider = self._emb_provider.text().strip().lower()
        base_url = self._emb_url.text().strip().rstrip("/")
        model = self._emb_model.text().strip()
        api_key = self._emb_key.text()
        if not base_url:
            self.notify("测试连接失败：请先填写 Base URL", "failed")
            return
        if not model:
            self.notify("测试连接失败：请先填写模型名", "failed")
            return
        started = time.monotonic()

        def _available_models() -> list[str]:
            """GET {base_url}/models 列出可用模型（仅 openai 兼容接口有此端点）。"""
            import httpx

            try:
                headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
                with httpx.Client(timeout=10.0) as client:
                    response = client.get(f"{base_url}/models", headers=headers)
                    if response.status_code != 200:
                        return []
                    data = response.json()
                    models = [str(item.get("id", "")) for item in data.get("data", [])]
                    return [name for name in models if name]
            except httpx.HTTPError:
                return []

        def _test() -> bool:
            import httpx

            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            if provider == "dashscope":  # 原生接口（路径自带的 /api/v1 归一化掉）
                base = base_url
                if base.endswith("/api/v1"):
                    base = base[: -len("/api/v1")]
                url = f"{base}/api/v1/services/embeddings/text-embedding/text-embedding"
                payload = {"model": model, "input": {"texts": ["测试连接"]}, "parameters": {"text_type": "query"}}
            elif provider == "ollama":  # Ollama 原生接口
                url = f"{base_url}/api/embed"
                payload = {"model": model, "input": ["测试连接"]}
                headers = {}
            else:  # openai 兼容接口
                url = f"{base_url}/embeddings"
                payload = {"model": model, "input": ["测试连接"]}
            try:
                with httpx.Client(timeout=15.0) as client:
                    response = client.post(url, headers=headers, json=payload)
                    response.raise_for_status()
                    return True
            except httpx.HTTPStatusError as exc:
                detail = ""
                try:
                    body = exc.response.json()
                    detail = str(
                        body.get("error", {}).get("message")
                        or body.get("message")
                        or ""
                    )
                except ValueError:
                    detail = exc.response.text[:200]
                hint = ""
                available = _available_models()
                if available:
                    hint = f"；可用模型：{', '.join(available[:6])}"
                raise RuntimeError(f"HTTP {exc.response.status_code} {detail}{hint}") from None

        worker = Worker(_test, self)
        worker.completed.connect(
            lambda ok: self.notify(
                f"测试连接成功：{int((time.monotonic() - started) * 1000)} ms", "success"
            )
        )
        worker.failed.connect(
            lambda message: self.notify(f"测试连接失败：{message}", "failed")
        )
        worker.start()
