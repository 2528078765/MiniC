"""知识库面板：知识库路径 / 数据目录 / 切分参数 / 检索参数 / 启动自动入库。"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from minic.gui.panels.base import PanelBase, SettingCard
from minic.gui.theme import COLOR_TEXT_MUTED
from minic.gui.widgets.dialogs import ConfigDialog
from minic.gui.widgets.toggle import ToggleSwitch
from minic.gui.widgets.worker import Worker


class KnowledgePanel(PanelBase):
    """知识库（RAG）设置面板（单库，全部配置在全局 ~/.minic/minic.json）。

    - 知识库路径：多目录配置弹窗 → 全局 rag.knowledge_base_paths。
    - 数据目录：统一全局 ~/.minic/rag-data（暂不支持自定义，配置已移除）。
    - 切分/检索参数：输入框 + 保存 → 全局 rag 段。
    - 启动自动入库开关。
    """

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
        self._saved_kb_paths: list[str] = []  # 已保存的知识库路径（弹窗预填用）
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(14)

        # ---- 知识库路径 ----
        kb_card = SettingCard(
            "知识库路径",
            "全局配置，支持具体文件或目录；启动时自动增量入库。",
            self,
        )
        self._kb_btn = QPushButton("配置", self)
        self._kb_btn.clicked.connect(lambda: self._open_config("知识库路径", "multi_dir"))
        kb_card.add_control(self._kb_btn)
        root.addWidget(kb_card)

        # ---- 切分参数 ----
        split_card = SettingCard(
            "切分参数",
            "按标题感知切分；chunk 500 字、overlap 100 字为默认。",
            self,
        )
        split_row = QHBoxLayout()
        split_row.setSpacing(8)
        self._chunk_size = QSpinBox(self)
        self._chunk_size.setRange(64, 4096)
        self._chunk_size.setValue(500)
        self._chunk_size.setSuffix("  chunk_size")
        split_row.addWidget(self._chunk_size)
        self._chunk_overlap = QSpinBox(self)
        self._chunk_overlap.setRange(0, 1024)
        self._chunk_overlap.setValue(100)
        self._chunk_overlap.setSuffix("  chunk_overlap")
        split_row.addWidget(self._chunk_overlap)
        split_row.addStretch(1)
        split_save = QPushButton("保存", self)
        split_save.clicked.connect(self._save_split)
        split_row.addWidget(split_save)
        split_card.add_body_layout(split_row)
        root.addWidget(split_card)

        # ---- 检索参数 ----
        search_card = SettingCard(
            "检索参数",
            "top_k 返回条数；混合检索加权 0.55 向量 + 0.45 BM25。",
            self,
        )
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self._top_k = QSpinBox(self)
        self._top_k.setRange(1, 50)
        self._top_k.setValue(5)
        self._top_k.setSuffix("  top_k")
        search_row.addWidget(self._top_k)
        self._vector_weight = QDoubleSpinBox(self)
        self._vector_weight.setRange(0.0, 1.0)
        self._vector_weight.setSingleStep(0.05)
        self._vector_weight.setValue(0.55)
        self._vector_weight.setDecimals(2)
        self._vector_weight.setSuffix("  向量")
        search_row.addWidget(self._vector_weight)
        self._bm25_weight = QDoubleSpinBox(self)
        self._bm25_weight.setRange(0.0, 1.0)
        self._bm25_weight.setSingleStep(0.05)
        self._bm25_weight.setValue(0.45)
        self._bm25_weight.setDecimals(2)
        self._bm25_weight.setSuffix("  BM25")
        search_row.addWidget(self._bm25_weight)
        search_row.addStretch(1)
        search_save = QPushButton("保存", self)
        search_save.clicked.connect(self._save_search)
        search_row.addWidget(search_save)
        search_card.add_body_layout(search_row)
        root.addWidget(search_card)

        # ---- 启动自动入库 ----
        ingest_card = SettingCard(
            "启动自动入库",
            "核心启动时自动检查知识库路径，新增/修改文档自动入库。",
            self,
        )
        self._auto_ingest = ToggleSwitch(self)
        self._auto_ingest.toggled.connect(
            lambda checked: self.toast(f"启动自动入库：{'开' if checked else '关'}（保存知识库路径后生效）")
        )
        ingest_card.add_control(self._auto_ingest)
        root.addWidget(ingest_card)

        root.addStretch(1)

    # ---- 配置弹窗 ----

    def _open_config(self, title: str, mode: str) -> None:
        """打开配置弹窗（预填已保存的知识库路径）。"""
        dialog = ConfigDialog(
            title=title, mode=mode, initial_paths=self._saved_kb_paths, parent=self
        )
        dialog.path_deleted.connect(self._delete_kb_docs)
        if dialog.exec() == ConfigDialog.DialogCode.Accepted:
            payload = dialog.result_payload
            if mode == "multi_dir":
                paths = payload["paths"]
                self._saved_kb_paths = list(paths)  # 立即更新本地记录
                self._save_kb_paths(paths)

    def _delete_kb_docs(self, path: str) -> None:
        """删除该知识库路径在核心 RAG 库中已入库的文档。"""
        if self.client is None or not getattr(self.client, "base_url", None):
            self.notify(f"核心未运行，仅从配置列表移除：{path}", "failed")
            return
        worker = Worker(
            lambda: self.client.delete_json("/rag/documents", params={"path": path}),
            self,
        )
        worker.completed.connect(
            lambda result: self.notify(
                f"已删除知识库文档 {result.get('deleted', 0) if result else 0} 篇",
                "success",
            )
        )
        worker.failed.connect(lambda message: self.notify(f"删除知识库失败：{message}", "failed"))
        worker.start()

    def _save_kb_paths(self, paths: list[str]) -> None:
        """保存知识库路径（全局 scope）并立即触发入库（顶栏显示进度条）。"""
        self._saved_kb_paths = list(paths)
        if self.client is None or not getattr(self.client, "base_url", None):
            self.toast(f"核心未运行，已记录知识库路径 {len(paths)} 条")
            return

        def _on_saved(ok: bool) -> None:
            """保存成功：空列表只提示清空，有路径则触发入库。"""
            if not ok:
                self.notify("保存知识库路径失败", "failed")
            elif not paths:
                self.notify("已保存知识库路径（0 条），已清空", "success")
            else:
                self._start_ingest(paths)

        worker = Worker(
            lambda: self.client.put_settings({"scope": "global", "rag": {"knowledge_base_paths": paths}}),
            self,
        )
        worker.completed.connect(_on_saved)
        worker.failed.connect(lambda message: self.notify(f"保存知识库路径失败：{message}", "failed"))
        worker.start()

    def _start_ingest(self, paths: list[str]) -> None:
        """逐个路径增量入库：期间顶栏进度条可见，完成通知条反馈。"""
        self.progress(True)
        worker = Worker(lambda: self._ingest_all(paths), self)
        worker.completed.connect(self._on_ingest_done)
        worker.failed.connect(self._on_ingest_failed)
        worker.start()

    def _ingest_all(self, paths: list[str]) -> dict[str, int]:
        """同步入库全部路径并统计；任一请求失败抛异常走失败通知。"""
        ingested = skipped = failed = 0
        for path in paths:
            result = self.client.post_json("/rag/ingest", {"path": path})
            if result is None:
                raise RuntimeError(f"入库请求失败：{path}")
            ingested += int(result.get("ingested", 0))
            skipped += int(result.get("skipped", 0))
            failed += len(result.get("failed") or [])
        return {"ingested": ingested, "skipped": skipped, "failed": failed}

    def _on_ingest_done(self, result: dict[str, int]) -> None:
        """入库完成：隐藏进度条 + 通知（有失败文件用失败样式）。"""
        self.progress(False)
        failed = result.get("failed", 0)
        if failed:
            self.notify(
                f"入库完成：新增 {result.get('ingested', 0)} 篇，跳过 {result.get('skipped', 0)} 篇，失败 {failed} 篇",
                "failed",
            )
        else:
            self.notify(
                f"入库完成：新增 {result.get('ingested', 0)} 篇，跳过 {result.get('skipped', 0)} 篇",
                "success",
            )

    def _on_ingest_failed(self, message: str) -> None:
        """入库失败：隐藏进度条 + 失败通知。"""
        self.progress(False)
        self.notify(f"入库失败：{message}", "failed")

    # ---- 参数保存 ----

    def _save_split(self) -> None:
        """保存切分参数。"""
        payload = {
            "scope": "global",
            "rag": {
                "chunk_size": self._chunk_size.value(),
                "chunk_overlap": self._chunk_overlap.value(),
            },
        }
        self._put_rag(payload, "已保存切分参数")

    def _save_search(self) -> None:
        """保存检索参数。"""
        payload = {
            "scope": "global",
            "rag": {
                "top_k": self._top_k.value(),
                "vector_weight": self._vector_weight.value(),
                "bm25_weight": self._bm25_weight.value(),
            },
        }
        self._put_rag(payload, "已保存检索参数")

    def _put_rag(self, payload: dict[str, Any], message: str) -> None:
        """PUT /settings 并提示（顶部通知条：成功绿色/失败红色带原因）。"""
        if self.client is None:
            self.notify(f"核心未运行，{message}（仅本地）", "failed")
            return
        worker = Worker(lambda: self.client.put_settings(payload), self)
        worker.completed.connect(
            lambda ok: self.notify(f"{message}", "success") if ok else self.notify(f"{message}失败", "failed")
        )
        worker.failed.connect(lambda err: self.notify(f"{message}失败：{err}", "failed"))
        worker.start()

    # ---- 数据加载 ----

    def reload(self) -> None:
        """加载配置：本地读 ~/.minic/minic.json 的 rag 段（缺失字段自动补默认写回）。"""
        from minic.gui.local_data import local_settings

        self._on_settings_loaded(local_settings(self.workspace))

    def _on_settings_loaded(self, data: Any) -> None:
        """用配置数据预填（含已保存的知识库路径，弹窗预填用）。"""
        if not isinstance(data, dict):
            return
        rag = data.get("rag") or {}
        self._chunk_size.setValue(int(rag.get("chunk_size", 500)))
        self._chunk_overlap.setValue(int(rag.get("chunk_overlap", 100)))
        self._top_k.setValue(int(rag.get("top_k", 5)))
        self._vector_weight.setValue(float(rag.get("vector_weight", 0.55)))
        self._bm25_weight.setValue(float(rag.get("bm25_weight", 0.45)))
        kb_paths = rag.get("knowledge_base_paths") or []
        self._saved_kb_paths = [str(item) for item in kb_paths]
        self._auto_ingest.setChecked(bool(kb_paths))
