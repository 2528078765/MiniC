"""MiniC 配置加载与合并。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator


class ModelConfig(BaseModel):
    """单个模型/供应商配置。"""

    name: str = "DeepSeek"  # 显示名（唯一）
    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"
    temperature: float = 0.7
    api_key: str | None = None
    context_length: int | None = None
    enabled: bool = True  # 是否启用（桌面端可配置多个模型并开关）


class ModelSettings(BaseModel):
    """模型配置：多个模型/供应商列表（每项带 enabled 开关）。

    兼容旧版单对象格式：读取到单对象 dict 时自动包成单元素列表。
    """

    models: list[ModelConfig] = Field(default_factory=lambda: [ModelConfig()])

    @model_validator(mode="before")
    @classmethod
    def _compat_single_model(cls, values: Any) -> Any:
        """兼容旧版单对象格式与局部更新。

        - 无 models 且有 ModelConfig 字段（旧格式）→ 包成单元素列表（默认启用）。
        - 有 models 且顶层还有 ModelConfig 字段（旧式局部更新如
          ``{"model": {"temperature": 0.2}}``）→ 覆盖进第一个模型。
        """
        if isinstance(values, dict):
            extra = {key: value for key, value in values.items() if key in ModelConfig.model_fields}
            models = values.get("models")
            if models is None:
                if extra:
                    extra.setdefault("name", "DeepSeek")
                    extra.setdefault("enabled", True)
                    values = {"models": [extra]}
                else:
                    values = {"models": [ModelConfig()]}
            elif extra and isinstance(models, list):
                if models and isinstance(models[0], dict):
                    first = dict(models[0])
                    first.update(extra)
                    values = {"models": [first, *models[1:]]}
        return values

    @property
    def enabled_models(self) -> list[ModelConfig]:
        """启用的模型列表。"""
        return [item for item in self.models if item.enabled]

    def primary(self) -> ModelConfig:
        """默认模型：第一个启用的；全禁用时回退第一个（不崩）。"""
        enabled = self.enabled_models
        if enabled:
            return enabled[0]
        return self.models[0] if self.models else ModelConfig()


class EmbeddingSettings(BaseModel):
    """Embedding 配置。

    provider 为 langchain init_embeddings 的 provider 名（openai / ollama /
    azure_openai 等）；默认 openai 走百炼 OpenAI 兼容接口。
    """

    provider: str = "openai"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model: str = "text-embedding-v3"
    dimension: int = 1024
    api_key: str | None = None


class RAGSettings(BaseModel):
    """RAG 检索参数（单库：全局一份数据目录 + 知识库源路径）。"""

    chunk_size: int = 500
    chunk_overlap: int = 100
    top_k: int = 5
    bm25_weight: float = 0.45
    vector_weight: float = 0.55
    auto_ingest_paths: list[str] = Field(default_factory=list)  # 兼容旧字段：读取时若 knowledge_base_paths 为空则沿用其值
    default_directory: str | None = None  # 默认知识库目录：IngestDirectory 工具缺省 path 时使用，并默认并入沙箱写白名单
    data_dir: str | None = None  # RAG 数据目录（None=用默认全局 ~/.minic/rag-data；解析在装配处）
    knowledge_base_paths: list[str] = Field(default_factory=list)  # 知识库源路径（可文件/目录/多路径）：启动自动入库与手动入库依据

    @model_validator(mode="before")
    @classmethod
    def _compat_auto_ingest_paths(cls, values: Any) -> Any:
        """旧 auto_ingest_paths 兼容：knowledge_base_paths 为空时用 auto_ingest_paths 的值。"""
        if isinstance(values, dict):
            knowledge_base_paths = values.get("knowledge_base_paths")
            auto_ingest_paths = values.get("auto_ingest_paths")
            if not knowledge_base_paths and auto_ingest_paths:
                values["knowledge_base_paths"] = list(auto_ingest_paths)
        return values


class ApprovalSettings(BaseModel):
    """审批策略配置。"""

    workspace_read_auto_approve: bool = True
    workspace_write_auto_approve: bool = False
    expiry_seconds: int = 300


class SandboxSettings(BaseModel):
    """沙箱策略配置。"""

    command_timeout: int = 60
    model_api_whitelist: list[str] = [
        "http://127.0.0.1:11434",
        "http://localhost:11434",
        "127.0.0.1",
        "localhost",
        "api.deepseek.com",
        "dashscope.aliyuncs.com",
        "api.moonshot.cn",
        "open.bigmodel.cn",
        "api.minimax.chat",
        "api.siliconflow.cn",
    ]
    default_level: str = "workspace-write"
    summarize_threshold_chars: int = 12000
    high_risk_patterns: list[str] = [
        "rm -rf /",
        "rm -rf ~",
        "format c:",
        "del /f /s /q c:",
        "shutdown /",
        "taskkill /f /im",
    ]
    allowed_write_dirs: list[str] = Field(default_factory=list)  # 沙箱写白名单：Write/Edit 等写工具可写这些工作区外目录（仍走审批）；装配处默认并入 rag.default_directory


class MemorySettings(BaseModel):
    """长期记忆注入配置。"""

    long_term_inject_threshold_tokens: int = 4000


class RateLimitSettings(BaseModel):
    """限流配置。"""

    max_requests: int = 120
    window_seconds: int = 60


class SubAgentSettings(BaseModel):
    """子 Agent 配置。"""

    max_concurrent: int = 3
    timeout_seconds: int = 120


class AppSettings(BaseModel):
    """应用整体配置。"""

    model: ModelSettings = ModelSettings()
    embedding: EmbeddingSettings = EmbeddingSettings()
    rag: RAGSettings = RAGSettings()
    approval: ApprovalSettings = ApprovalSettings()
    sandbox: SandboxSettings = SandboxSettings()
    memory: MemorySettings = MemorySettings()
    rate_limit: RateLimitSettings = RateLimitSettings()
    subagent: SubAgentSettings = SubAgentSettings()


def merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并两份字典，override 优先。"""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_json_file(path: Path) -> dict[str, Any]:
    """读取 JSON 配置文件，文件不存在时返回空字典。"""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _dir_is_writable(path: Path) -> bool:
    """通过写探测文件判断目录是否可写。"""
    try:
        probe = path / f".minic_write_probe_{os.getpid()}"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def resolve_project_root(workspace: str | Path, fallback: str | Path) -> Path:
    """选择项目数据落盘根目录：工作区可写时用工作区，否则用 fallback。"""
    workspace_path = Path(workspace).resolve()
    fallback_path = Path(fallback).resolve()
    config_dir = workspace_path / ".minic"
    if (config_dir / "minic.json").exists() and _dir_is_writable(config_dir):
        return workspace_path
    return fallback_path


def load_settings(project_root: Path | None = None) -> AppSettings:
    """合并默认配置、全局配置与项目配置后返回设置对象。

    - model/rag 等段项目优先合并；
    - embedding 只读全局（向量空间必须一致），项目 minic.json 的 embedding 段忽略。
    """
    global_dir = Path.home() / ".minic"
    project_dir = project_root or Path.cwd()
    global_config = _read_json_file(global_dir / "minic.json")
    project_config = _read_json_file(project_dir / ".minic" / "minic.json")
    merged = merge_dict(global_config, project_config)
    merged["embedding"] = global_config.get("embedding", {})  # embedding 只读全局：不合并项目 embedding 段
    return AppSettings.model_validate(merged)
