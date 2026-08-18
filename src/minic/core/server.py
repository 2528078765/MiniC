"""MiniC 核心 HTTP 服务。"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from minic import __version__
from minic.backups import BackupManager
from minic.chat.engine import ChatEngine, new_run_ids
from minic.graph import build_super_graph, clear_mcp_tools, register_mcp_tools
from minic.chat.memory import ShortMemoryStore
from minic.chat.models import create_chat_model, validate_model_configs
from minic.core.config import (
    AppSettings,
    load_settings,
    merge_dict,
    resolve_project_root,
)
from minic.core.runtime import (
    acquire_single_instance_lock,
    find_free_port,
    generate_token,
    release_single_instance_lock,
    write_runtime,
)
from minic.mcp.client import McpManager
from minic.memory import LongTermMemoryStore, MemoryConflictError
from minic.middleware import RateLimiter, RequestLogger, SandboxPolicy, summarize_history
from minic.rag.embeddings import ConfigEmbeddingProvider, create_embedding_provider
from minic.rag.store import RagStore
from minic.recovery import ToolExecutionLog
from minic.run_manager import RunManager
from minic.skills import SkillManager
from minic.subagent import SubAgentManager
from minic.tools.runtime import ToolRuntime
from minic.tools.service import ApprovalManager, PermissionStore, ToolExecutor


def _error(status_code: int, code: str, message: str, detail: dict[str, Any] | None = None) -> HTTPException:
    """构造统一错误响应。"""
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "detail": detail or {}}},
    )


def _require_auth(request: Request) -> None:
    """校验 Bearer 令牌。"""
    authorization = request.headers.get("Authorization", "")
    token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    if not token or token != request.app.state.runtime_token:
        raise _error(401, "UNAUTHORIZED", "访问令牌无效")


def _strip_api_keys(value: Any) -> Any:
    """递归剔除字典中的 api_key 字段（GET /settings 响应用；模型列表每项都有 key）。"""
    if isinstance(value, dict):
        return {key: _strip_api_keys(item) for key, item in value.items() if key != "api_key"}
    if isinstance(value, list):
        return [_strip_api_keys(item) for item in value]
    return value


def _persist_settings(config_path: Path, payload: dict[str, Any]) -> None:
    """把局部更新合并进 minic.json 并原子写入。"""
    existing: dict[str, Any] = {}
    if config_path.exists():
        existing = json.loads(config_path.read_text(encoding="utf-8-sig"))
    merged = merge_dict(existing, payload)
    try:
        AppSettings.model_validate(merged)
    except ValidationError as exc:
        raise _error(400, "VALIDATION_ERROR", "设置格式无效", {"errors": exc.errors()}) from exc
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(config_path)


class IngestRequest(BaseModel):
    """入库请求体。"""

    path: str
    extensions: list[str] | None = None
    source: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    """聊天请求体。"""

    thread_id: str | None = None
    workspace: str | None = None
    message: str
    model: str | None = None


class MemoryWriteRequest(BaseModel):
    """长期记忆写入请求体。"""

    scope: str = "project"
    workspace: str | None = None
    content: str
    mode: str = "replace"
    base_version: str | None = None


class ToolRunRequest(BaseModel):
    """工具执行请求体。"""

    thread_id: str | None = None
    workspace: str | None = None
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = None
    tool_call_id: str | None = None


class ApproveRequest(BaseModel):
    """审批提交请求体。"""

    approval_id: str
    decision: str


class SkillEnableRequest(BaseModel):
    """SKILL 启用请求体（可选 confirm 用于冲突确认）。"""

    confirm: bool = False


class AgentRunRequest(BaseModel):
    """子 Agent 运行请求体。"""

    task: str
    allowed_tools: list[str] | None = None
    budget: int | None = None


def _thread_public(thread: dict[str, Any]) -> dict[str, Any]:
    """把完整会话记录转换为接口返回的 Thread 结构。"""
    return {
        key: thread.get(key)
        for key in (
            "thread_id",
            "name",
            "description",
            "workspace",
            "created_at",
            "updated_at",
            "archived",
        )
    }


def create_app(
    settings: AppSettings,
    token: str,
    project_root: Path,
    rag_data_dir: Path | None = None,
    knowledge_base_paths: list[str] | None = None,
    short_memory_dir: Path | None = None,
    global_memory_dir: Path | None = None,
    global_permissions_path: Path | None = None,
    mcp_settings_path: Path | None = None,
    skills_global_dir: Path | None = None,
    skills_project_dir: Path | None = None,
    global_settings_path: Path | None = None,
) -> FastAPI:
    """创建 MiniC 应用实例。

    单库 RAG：``rag_data_dir`` 为全局数据目录（缺省用配置 ``rag.data_dir`` 或
    ``~/.minic/rag-data``）；``knowledge_base_paths`` 缺省从 minic.json 的
    ``rag.knowledge_base_paths`` 读取（含旧 auto_ingest_paths 兼容）。
    """
    app = FastAPI(title="MiniC Core", version=__version__)
    app.state.runtime_token = token
    app.state.settings = settings
    app.state.project_root = project_root.resolve()
    app.state.started_at = datetime.now().astimezone().isoformat()
    app.state.active_threads: set[str] = set()
    app.state.backups_dir = app.state.project_root / ".minic" / "backups" / "sessions"
    app.state.global_settings_path = global_settings_path or (Path.home() / ".minic" / "minic.json")

    # 懒加载 embedding：无 Key 时核心照常启动，配置热更新即时生效
    embedding_provider = ConfigEmbeddingProvider(lambda: app.state.settings)
    if knowledge_base_paths is None:
        knowledge_base_paths = list(settings.rag.knowledge_base_paths)
    if rag_data_dir is None:
        raw_data_dir = settings.rag.data_dir
        rag_data_dir = (
            Path(raw_data_dir).expanduser().resolve()
            if raw_data_dir
            else Path.home() / ".minic" / "rag-data"
        )
    rag_store = RagStore(  # 单库：RAG 索引（向量 + BM25），数据目录缺省 ~/.minic/rag-data
        rag_data_dir=rag_data_dir,
        settings=settings,
        embedding_provider=embedding_provider,
        scope_name="global",
    )
    if short_memory_dir is None:  # 缺省用项目默认短期记忆目录
        short_memory_dir = project_root / ".minic" / "memory" / "short_memory"
    short_memory = ShortMemoryStore(root_dir=short_memory_dir)
    long_term_memory = LongTermMemoryStore(
        global_dir=global_memory_dir or (Path.home() / ".minic" / "memory"),
        project_root=project_root,
    )
    permission_store = PermissionStore(
        global_path=global_permissions_path or (Path.home() / ".minic" / "permissions.json"),
        project_path=project_root / ".minic" / "permissions.json",
    )
    sandbox_write_dirs = list(settings.sandbox.allowed_write_dirs)  # 沙箱写白名单（G13）
    if settings.rag.default_directory:  # 默认含 rag.default_directory：装配处并入，跨字段默认在装配处而非 pydantic
        default_dir = str(Path(settings.rag.default_directory).expanduser().resolve())
        if default_dir not in sandbox_write_dirs:
            sandbox_write_dirs.append(default_dir)
    tool_executor = ToolExecutor(
        project_root=project_root,
        backup_dir=project_root / ".minic" / "backups" / "files",
        memory_store=long_term_memory,
        command_timeout=settings.sandbox.command_timeout,
        allowed_write_dirs=sandbox_write_dirs,
    )
    mcp_manager = McpManager(settings_path=mcp_settings_path)
    mcp_manager.add_tools_changed_callback(lambda: register_mcp_tools(mcp_manager))
    approval_manager = ApprovalManager(
        permission_store=permission_store,
        workspace=project_root,
        settings=settings,
        mcp_manager=mcp_manager,
    )
    tool_log = ToolExecutionLog(project_root / ".minic" / "logs" / "tool_execution.jsonl")
    tool_log.mark_interrupted()
    sandbox_policy = SandboxPolicy(  # 沙箱强制层：路径工作区内 + Bash 危险命令 + 模型域名白名单
        high_risk_patterns=settings.sandbox.high_risk_patterns,  # 危险命令黑名单
        model_api_whitelist=settings.sandbox.model_api_whitelist,  # 模型 API 域名白名单
        allowed_write_dirs=sandbox_write_dirs,  # 写工具可写的知识库目录（仍走审批）
    )
    skill_manager = SkillManager(
        global_dir=skills_global_dir or (Path.home() / ".minic" / "skills"),
        project_dir=skills_project_dir or (project_root / ".minic" / "skills"),
        state_path=project_root / ".minic" / "skills_state.json",
    )
    tool_runtime = ToolRuntime(
        approval_manager=approval_manager,
        tool_executor=tool_executor,
        tool_log=tool_log,
        mcp_manager=mcp_manager,
        skill_manager=skill_manager,
        sandbox_policy=sandbox_policy,
        rag_store=rag_store,  # IngestDirectory 入库目标（单库）
    )
    chat_model = create_chat_model(settings)
    subagent_manager = SubAgentManager(
        chat_model=chat_model,
        memory_store=long_term_memory,
        tool_runtime=tool_runtime,
        settings=settings,
        workspace=project_root,
    )
    tool_runtime.subagent_manager = subagent_manager  # 互相注入，供 DelegateToSubagent 转发
    rate_limiter = RateLimiter(
        max_requests=settings.rate_limit.max_requests,
        window_seconds=settings.rate_limit.window_seconds,
    )
    request_logger = RequestLogger(project_root / ".minic" / "logs" / "requests.jsonl")
    run_manager = RunManager(project_root / ".minic" / "logs" / "sse_events.jsonl")
    run_manager.mark_interrupted()  # 核心重启后未完成 run 标记 interrupted，不自动恢复
    run_manager.cleanup_expired()  # 启动时清理超过 24h 的事件日志（规格书 4.6）
    chat_graph = build_super_graph(  # 总图：路由 + 知识/动作子图
        rag_store=rag_store,
        chat_model=chat_model,
        memory_store=long_term_memory,
        settings=settings,
        tool_runtime=tool_runtime,
        skill_manager=skill_manager,
    )
    chat_engine = ChatEngine(
        rag_store=rag_store,
        short_memory=short_memory,
        graph=chat_graph,
        tool_runtime=tool_runtime,
        summarize_history=lambda messages: summarize_history(  # 历史超阈值时生成摘要（只影响注入图的 history）
            messages,  # 会话历史
            chat_model,  # 模型
            settings.sandbox.summarize_threshold_chars,  # 字符阈值
        ),
    )

    app.state.rag_store = rag_store  # 单库
    app.state.knowledge_base_paths = knowledge_base_paths
    app.state.short_memory = short_memory
    app.state.chat_engine = chat_engine
    app.state.long_term_memory = long_term_memory
    app.state.tool_executor = tool_executor
    app.state.approval_manager = approval_manager
    app.state.permission_store = permission_store
    app.state.tool_log = tool_log
    app.state.tool_runtime = tool_runtime
    app.state.mcp_manager = mcp_manager
    app.state.skill_manager = skill_manager
    app.state.subagent_manager = subagent_manager
    app.state.rate_limiter = rate_limiter
    app.state.request_logger = request_logger
    app.state.run_manager = run_manager
    app.state.last_auto_ingest_at: str | None = None  # 最近一次启动自动入库完成时间（首次启动未完成时为 None）
    app.state.auto_ingest_task: asyncio.Task | None = None  # 启动自动入库后台任务句柄（可等待/取消）
    backup_manager = BackupManager(
        backups_dir=project_root / ".minic" / "backups",
        short_memory=short_memory,
        workspace=project_root,
    )
    app.state.backup_manager = backup_manager

    @app.on_event("startup")
    async def start_mcp_manager() -> None:
        """异步连接已配置的 MCP 服务（后台任务，不阻塞启动）。"""
        await app.state.mcp_manager.start()

    @app.on_event("startup")
    async def start_auto_ingest() -> None:
        """配置了 knowledge_base_paths 时在后台执行启动自动入库（不阻塞启动，/health 秒回）。"""
        if not app.state.knowledge_base_paths:
            return
        app.state.auto_ingest_task = asyncio.create_task(auto_ingest_on_startup())

    async def auto_ingest_on_startup() -> None:
        """后台逐路径增量入库 knowledge_base_paths（单库）；单路径失败记录日志不影响其他路径。

        - 用 asyncio.to_thread 在线程池执行同步 ingest_directory，避免阻塞事件循环。
        - 结果逐路径追加到 <project>/.minic/logs/auto_ingest.jsonl（每行一个路径）。
        - 全部完成后更新 app.state.last_auto_ingest_at。
        """
        log_path = app.state.project_root / ".minic" / "logs" / "auto_ingest.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        async def ingest_into(store: RagStore, path: str) -> None:
            entry: dict[str, Any] = {
                "path": str(path),
                "ingested": 0,
                "skipped": 0,
                "failed": [],
                "error": None,
            }
            try:
                result = await asyncio.to_thread(  # 同步 ingest_directory 放入线程池，避免阻塞事件循环
                    store.ingest_directory,
                    path=str(path),
                    extensions=None,
                    source=None,
                )
                entry["ingested"] = result.ingested
                entry["skipped"] = result.skipped
                entry["failed"] = result.failed
            except Exception as exc:  # noqa: BLE001 - 单路径失败（如路径不存在）不影响其他路径与核心启动
                entry["error"] = f"{type(exc).__name__}: {exc}"
            entry["at"] = datetime.now().astimezone().isoformat()
            with log_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(entry, ensure_ascii=False) + "\n")
                file.flush()

        for path in app.state.knowledge_base_paths:  # 知识库源路径 → 单库
            await ingest_into(rag_store, str(path))
        app.state.last_auto_ingest_at = datetime.now().astimezone().isoformat()

    @app.on_event("shutdown")
    async def shutdown_mcp_manager() -> None:
        """关闭 MCP 连接并清理工具注册。"""
        await app.state.mcp_manager.shutdown()
        clear_mcp_tools()

    @app.middleware("http")
    async def minic_middleware(request: Request, call_next):
        """统一限流与请求日志中间件。"""
        client_ip = request.client.host if request.client else "unknown"
        if not app.state.rate_limiter.allow(client_ip):
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "RATE_LIMITED",
                        "message": "请求过于频繁",
                        "detail": {},
                    }
                },
            )
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = (time.monotonic() - start) * 1000
        app.state.request_logger.log(
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
        )
        return response

    @app.exception_handler(HTTPException)
    async def minic_exception_handler(request: Request, exc: HTTPException):
        """保持接口规格中的统一错误格式。"""
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(status_code=exc.status_code, content=exc.detail, headers=exc.headers)
        return await http_exception_handler(request, exc)

    @app.get("/health")
    def health() -> dict[str, Any]:
        """健康检查，不需要鉴权。"""
        return {
            "status": "ok",
            "pid": os.getpid(),
            "started_at": app.state.started_at,
            "version": __version__,
        }

    @app.get("/settings", dependencies=[Depends(_require_auth)])
    def get_settings() -> dict[str, Any]:
        """读取合并后的设置（热更新后的内存配置），不返回 API Key。"""
        data = _strip_api_keys(app.state.settings.model_dump())
        return {
            "scope": "merged",
            "workspace": str(app.state.project_root),
            **data,
        }

    @app.put("/settings", dependencies=[Depends(_require_auth)])
    def put_settings(payload: dict[str, Any]) -> Response:
        """局部更新设置并持久化到 minic.json（模型列表的 api_key 允许写入，本地明文）。

        模型段先做构建校验（域名白名单等），错误配置直接 400 拒绝，不落盘。
        """
        scope = str(payload.pop("scope", "project"))
        workspace = payload.pop("workspace", None)
        if scope not in {"global", "project"}:
            raise _error(400, "VALIDATION_ERROR", "scope 只支持 global 或 project")
        if scope == "project":
            root = (Path(workspace) if workspace else app.state.project_root).expanduser().resolve()
            config_path = root / ".minic" / "minic.json"
        else:
            config_path = app.state.global_settings_path
        # 候选配置构建 + 模型构建校验（失败 400，不落盘）
        candidate_settings = app.state.settings
        if "model" in payload:
            existing: dict[str, Any] = {}
            if config_path.exists():
                existing = json.loads(config_path.read_text(encoding="utf-8-sig"))
            merged_candidate = merge_dict(existing, payload)
            try:
                candidate_settings = AppSettings.model_validate(merged_candidate)
                validate_model_configs(candidate_settings)  # 域名白名单等校验（不构建实例）
            except ValidationError as exc:
                raise _error(400, "VALIDATION_ERROR", "设置格式无效", {"errors": exc.errors()}) from exc
            except ValueError as exc:
                raise _error(400, "VALIDATION_ERROR", str(exc)) from exc
        _persist_settings(config_path, payload)
        if "model" in payload or "rag" in payload or "embedding" in payload:
            # 热更新：重载配置并重建模型注册表（禁用/新增模型立即生效）
            app.state.settings = load_settings(app.state.project_root)
            chat_model.update(app.state.settings)
        return Response(status_code=204)

    @app.get("/usage", dependencies=[Depends(_require_auth)])
    def usage_stats() -> dict[str, Any]:
        """汇总 ~/.minic/usage.jsonl 的 token 消耗总量。"""
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

    @app.get("/rag/status", dependencies=[Depends(_require_auth)])
    def rag_status() -> dict[str, Any]:
        """返回单库 RAG 索引状态。"""
        documents = rag_store._load_documents()
        total_chunks = sum(int(document.get("chunk_count", 0)) for document in documents)
        embedded_at_list = [
            document.get("embedded_at") for document in documents if document.get("embedded_at")
        ]
        embedding_model = documents[0].get("embedding_model") if documents else (
            f"{app.state.settings.embedding.provider}/{app.state.settings.embedding.model}"
        )
        return {
            "total_documents": len(documents),
            "total_chunks": total_chunks,
            "embedding_model": embedding_model,
            "last_ingest_at": max(embedded_at_list) if embedded_at_list else None,
            "last_auto_ingest_at": app.state.last_auto_ingest_at,
        }

    @app.get("/mcp", dependencies=[Depends(_require_auth)])
    def mcp_status() -> dict[str, Any]:
        """返回 MCP 服务状态列表（查询前重载配置：启动后新增服务无需重启核心）。"""
        try:
            app.state.mcp_manager.reload_config()
        except ValueError as exc:
            raise _error(400, "VALIDATION_ERROR", str(exc)) from exc  # 配置解析错误带路径原因
        return {"servers": app.state.mcp_manager.status()}

    @app.post("/mcp/{name}/connect", dependencies=[Depends(_require_auth)])
    async def mcp_connect(name: str) -> dict[str, Any]:
        """手动重连指定 MCP 服务。"""
        try:
            status = await app.state.mcp_manager.connect(name)
        except KeyError as exc:
            raise _error(404, "NOT_FOUND", str(exc)) from exc
        except ValueError as exc:
            raise _error(400, "VALIDATION_ERROR", str(exc)) from exc
        except ConnectionError as exc:
            raise _error(502, "MCP_CONNECT_FAILED", str(exc)) from exc
        return status

    @app.get("/skills", dependencies=[Depends(_require_auth)])
    def skills_list() -> dict[str, Any]:
        """返回全部 SKILL 列表（含 enabled/scope/conflict 标记）。

        查询前实时重扫描技能目录（启动后新增/删除 SKILL 无需重启核心）。
        """
        app.state.skill_manager.rescan()
        return {"skills": app.state.skill_manager.list()}

    @app.post("/skills/{name}/enable", dependencies=[Depends(_require_auth)])
    def skills_enable(name: str, payload: SkillEnableRequest | None = None) -> dict[str, Any]:
        """启用 SKILL；同名冲突且未确认时返回 409，请求体带 confirm=true 直接确认启用。"""
        confirm = bool(payload.confirm) if payload is not None else False
        try:
            result, _ = app.state.skill_manager.enable(name, confirm=confirm)
        except KeyError as exc:
            raise _error(404, "NOT_FOUND", str(exc)) from exc
        if result == "needs_confirmation":
            raise _error(
                409,
                "CONFLICT",
                "SKILL 同名冲突，需要确认",
                {"name": name, "conflict": True},
            )
        return {"name": name, "enabled": True}

    @app.post("/skills/{name}/disable", dependencies=[Depends(_require_auth)])
    def skills_disable(name: str) -> Response:
        """禁用 SKILL。"""
        try:
            app.state.skill_manager.disable(name)
        except KeyError as exc:
            raise _error(404, "NOT_FOUND", str(exc)) from exc
        return Response(status_code=204)

    @app.post("/agents", dependencies=[Depends(_require_auth)])
    async def run_agent(payload: AgentRunRequest) -> dict[str, Any]:
        """同步执行子 Agent 任务，返回 subagent_id/status/output。"""
        task = (payload.task or "").strip()
        if not task:
            raise _error(400, "VALIDATION_ERROR", "task 不能为空")
        result = await app.state.subagent_manager.run(
            task=task,
            allowed_tools=payload.allowed_tools,
            budget=payload.budget,
        )
        return {
            "subagent_id": result["subagent_id"],
            "status": result["status"],
            "output": result["output"],
        }

    @app.get("/agents", dependencies=[Depends(_require_auth)])
    def list_agents() -> dict[str, Any]:
        """返回最近子任务列表。"""
        return {"agents": app.state.subagent_manager.status()}

    @app.get("/agents/{subagent_id}", dependencies=[Depends(_require_auth)])
    def get_agent(subagent_id: str) -> dict[str, Any]:
        """返回单个子任务状态。"""
        for entry in app.state.subagent_manager.status():
            if entry["subagent_id"] == subagent_id:
                return entry
        raise _error(404, "NOT_FOUND", "子任务不存在", {"subagent_id": subagent_id})

    @app.post("/rag/ingest", dependencies=[Depends(_require_auth)])
    def ingest(payload: IngestRequest) -> dict[str, Any]:
        """入库文件或文件夹（单库）。"""
        try:
            result = rag_store.ingest_directory(
                path=payload.path,
                extensions=payload.extensions,
                source=payload.source,
            )
            return {
                "ingested": result.ingested,
                "skipped": result.skipped,
                "failed": result.failed,
            }
        except ValueError as exc:
            raise _error(400, "VALIDATION_ERROR", str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 统一转成内部错误
            raise _error(500, "INTERNAL_ERROR", "入库失败", {"reason": str(exc)}) from exc

    @app.get("/rag/query", dependencies=[Depends(_require_auth)])
    def query(
        q: str = Query(..., description="用户问题"),
        top_k: int = Query(5, ge=1, le=50, description="返回条数"),
        source: str | None = Query(None, description="按知识库来源过滤"),
    ) -> dict[str, Any]:
        """执行混合检索（单库）。"""
        if not q.strip():
            raise _error(400, "VALIDATION_ERROR", "q 不能为空")
        results = rag_store.query(query=q, top_k=top_k, source=source)
        return {"query": q, "results": [result.to_dict() for result in results]}

    @app.get("/rag/documents", dependencies=[Depends(_require_auth)])
    def rag_documents(
        source: str | None = None,
        cursor: str | None = None,
        page_size: int = Query(50, ge=1, le=100),
    ) -> dict[str, Any]:
        """列出已入库文档，支持 source 过滤与游标分页。"""
        documents, next_cursor = rag_store.list_documents(
            source=source, cursor=cursor, page_size=page_size
        )
        return {"documents": documents, "next_cursor": next_cursor}

    @app.delete("/rag/documents", dependencies=[Depends(_require_auth)])
    def rag_documents_delete_by_path(
        path: str = Query(..., description="知识库路径（目录或文件）"),
    ) -> dict[str, int]:
        """按路径删除该知识库的全部已入库文档。"""
        deleted = rag_store.delete_documents_by_path(path)
        return {"deleted": deleted}

    @app.delete("/rag/documents/{doc_id}", dependencies=[Depends(_require_auth)])
    def rag_document_delete(doc_id: str) -> Response:
        """删除已入库文档，同步清理 Chroma/BM25/metadata（单库）。"""
        deleted = rag_store.delete_document(doc_id)
        if not deleted:
            raise _error(404, "NOT_FOUND", "文档不存在", {"doc_id": doc_id})
        return Response(status_code=204)

    @app.post("/chat/stream", dependencies=[Depends(_require_auth)])
    def chat_stream(payload: ChatRequest) -> StreamingResponse:
        """流式问答，返回 SSE 事件流；事件同时写入 sse_events.jsonl 供断线续传。"""
        if not payload.message.strip():
            raise _error(400, "VALIDATION_ERROR", "message 不能为空")
        workspace = str((Path(payload.workspace) if payload.workspace else app.state.project_root).resolve())
        project_root = str(resolve_project_root(workspace, app.state.project_root))  # 不可写工作区回退到核心项目根
        if payload.thread_id:
            thread_id = payload.thread_id
            if app.state.short_memory.load(thread_id, workspace=workspace) is None:
                raise _error(404, "NOT_FOUND", "会话不存在", {"thread_id": thread_id})
        else:
            thread_id = str(uuid.uuid4())  # 未指定会话时新建会话
            app.state.short_memory.create(thread_id, workspace)
        if thread_id in app.state.active_threads:
            raise _error(409, "CONFLICT", "该会话已有流式请求正在执行", {"thread_id": thread_id})

        run_id, message_id = new_run_ids()
        app.state.active_threads.add(thread_id)  # 标记会话运行中，避免并发流式请求
        app.state.run_manager.register(run_id, thread_id)  # 注册 run，供 events 续传与 cancel

        # 请求级模型选择：payload.model 指定启用的模型名（未指定用默认）
        model_token: contextvars.Token | None = None
        if payload.model:
            try:
                model_token = chat_model.use(payload.model)
            except KeyError as exc:
                app.state.active_threads.discard(thread_id)
                raise _error(400, "VALIDATION_ERROR", str(exc)) from exc

        async def event_stream() -> AsyncIterator[str]:
            """把引擎事件转成 SSE 文本并写入事件日志；取消时发送 cancelled 事件后终止。"""
            sequence = 0
            status: str | None = None
            message_end_sent = False
            try:
                async for event_name, data in app.state.chat_engine.stream_chat(
                    thread_id=thread_id,
                    workspace=workspace,
                    project_root=project_root,
                    message=payload.message,
                    run_id=run_id,
                    message_id=message_id,
                ):
                    run_record = app.state.run_manager.get(run_id)
                    if run_record is not None and run_record["cancelled"]:
                        # 已收到取消请求：不再转发引擎后续事件，直接以 cancelled 收尾
                        # 无论 message_start 是否已发出，都补发 message_end(cancelled)（规格书第 9 节）
                        status = "cancelled"
                        if not message_end_sent:
                            message_end_sent = True
                            sequence += 1
                            end_data = {"message_id": message_id, "status": "cancelled"}
                            app.state.run_manager.publish(run_id, sequence, "message_end", end_data)
                            yield (
                                f"id: {run_id}:{sequence}\n"
                                f"event: message_end\n"
                                f"data: {json.dumps(end_data, ensure_ascii=False)}\n\n"
                            )
                        sequence += 1
                        done_data = {"thread_id": thread_id, "status": "cancelled", "sources": []}
                        app.state.run_manager.publish(run_id, sequence, "done", done_data)
                        yield (
                            f"id: {run_id}:{sequence}\n"
                            f"event: done\n"
                            f"data: {json.dumps(done_data, ensure_ascii=False)}\n\n"
                        )
                        break
                    sequence += 1
                    app.state.run_manager.publish(run_id, sequence, event_name, data)
                    yield (
                        f"id: {run_id}:{sequence}\n"
                        f"event: {event_name}\n"
                        f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                    )
                    if event_name == "message_end":
                        message_end_sent = True
                    elif event_name == "done":
                        status = data.get("status")
            finally:
                # 正常结束 status 为 done 的 status；取消为 cancelled；中途断开未收尾按 interrupted
                app.state.run_manager.finish(run_id, status or "interrupted")
                app.state.active_threads.discard(thread_id)
                if model_token is not None:
                    chat_model.reset(model_token)  # 恢复默认模型选择

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/chat/stream/{run_id}/events", dependencies=[Depends(_require_auth)])
    def chat_stream_events(run_id: str, last_event_id: str | None = None) -> StreamingResponse:
        """回放 run 的 SSE 事件；run 仍运行时回放后订阅实时续传直到 done。

        - run 不存在（注册表与日志均无）-> 404。
        - 核心重启后 run 不在注册表但日志里有事件（无 done 已由启动时 mark_interrupted
          补 done(interrupted)）-> 回放 interrupted 后关闭。
        - run 已结束 -> 回放剩余事件后按 run 状态发 done 关闭。
        """
        run_manager = app.state.run_manager
        if run_manager.get(run_id) is None and not run_manager.event_log.has_run(run_id):
            raise _error(404, "NOT_FOUND", "run 不存在", {"run_id": run_id})
        last_seq = 0
        if last_event_id:
            if not last_event_id.startswith(f"{run_id}:") or not last_event_id[len(run_id) + 1 :].isdigit():
                raise _error(
                    400,
                    "VALIDATION_ERROR",
                    "last_event_id 格式错误，应为 {run_id}:{seq}",
                    {"run_id": run_id, "last_event_id": last_event_id},
                )
            last_seq = int(last_event_id[len(run_id) + 1 :])

        def sse(event_name: str, data: dict[str, Any], seq: int) -> str:
            """渲染一条 SSE 事件文本。"""
            return (
                f"id: {run_id}:{seq}\n"
                f"event: {event_name}\n"
                f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            )

        async def events_stream() -> AsyncIterator[str]:
            """回放 + 实时续传。"""
            done_sent = False
            current_seq = last_seq
            for entry in run_manager.event_log.read_after(run_id, last_seq):
                seq = int(entry["seq"])
                current_seq = seq
                event_name = entry["event"]
                data = entry["data"]
                yield sse(event_name, data, seq)
                if event_name == "done":
                    done_sent = True

            def close_done(status: str, thread_id: str | None) -> str | None:
                """未回放到 done 时补发 done 关闭，返回 SSE 文本；已回放 done 返回 None。"""
                nonlocal done_sent, current_seq
                if done_sent:
                    return None
                current_seq += 1
                done_sent = True
                return sse("done", {"thread_id": thread_id, "status": status, "sources": []}, current_seq)

            current = run_manager.get(run_id)
            if current is None:
                # 核心重启后：日志已由 mark_interrupted 补 done(interrupted)，回放即可
                line = close_done("interrupted", None)
                if line:
                    yield line
                return
            status = current["status"]
            if status != "running":
                line = close_done(status, current["thread_id"])
                if line:
                    yield line
                return

            queue = run_manager.subscribe(run_id)
            if queue is None:  # 竞态：回放后、订阅前刚结束
                current = run_manager.get(run_id)
                line = close_done(
                    current["status"] if current is not None else "interrupted",
                    current["thread_id"] if current is not None else None,
                )
                if line:
                    yield line
                return
            try:
                while True:
                    try:
                        event_name, data, seq = await asyncio.wait_for(queue.get(), timeout=2.0)
                    except asyncio.TimeoutError:
                        current = run_manager.get(run_id)
                        if current is None:
                            line = close_done("interrupted", None)
                            if line:
                                yield line
                            break
                        if current["cancelled"]:
                            # 已取消但主生成器未及时发 done（如卡在审批/子进程），此处收尾
                            line = close_done("cancelled", current["thread_id"])
                            if line:
                                yield line
                            break
                        if current["status"] != "running":
                            line = close_done(current["status"], current["thread_id"])
                            if line:
                                yield line
                            break
                        continue
                    current_seq = seq
                    yield sse(event_name, data, seq)
                    if event_name == "done":
                        done_sent = True
                        break
            finally:
                current = run_manager.get(run_id)
                if current is not None and current.get("queue") is queue:
                    current["queue"] = None

        return StreamingResponse(
            events_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/chat/stream/{run_id}/cancel", dependencies=[Depends(_require_auth)])
    def cancel_chat_stream(run_id: str) -> Response:
        """取消运行中的 run：置取消标志，流式循环检测后发 message_end(cancelled)+done(cancelled) 关闭。

        run 不存在 -> 404；run 已结束或已取消 -> 409 CONFLICT（规格书 6.18）。
        """
        run_manager = app.state.run_manager
        run = run_manager.get(run_id)
        if run is None:
            raise _error(404, "NOT_FOUND", "run 不存在", {"run_id": run_id})
        if run["status"] != "running":
            raise _error(
                409,
                "CONFLICT",
                "run 已结束或已取消",
                {"run_id": run_id, "status": run["status"]},
            )
        run_manager.mark_cancelled(run_id)
        return Response(status_code=204)

    @app.get("/threads", dependencies=[Depends(_require_auth)])
    def list_threads(
        archived: bool | None = None,
        workspace: str | None = None,
        cursor: str | None = None,
        page_size: int = Query(50, ge=1, le=100),
    ) -> dict[str, Any]:
        """列出会话，支持归档过滤和分页。"""
        effective_workspace = workspace or str(app.state.project_root)
        threads = app.state.short_memory.list(workspace=effective_workspace, archived=archived)
        start = int(cursor) if cursor and cursor.isdigit() else 0
        page = threads[start : start + page_size]
        next_cursor = str(start + len(page)) if start + len(page) < len(threads) else None
        return {
            "next_cursor": next_cursor,
            "threads": [_thread_public(thread) for thread in page],
        }

    @app.post("/threads/{thread_id}/resume", dependencies=[Depends(_require_auth)])
    def resume_thread(thread_id: str) -> dict[str, Any]:
        """恢复会话并返回消息记录。"""
        thread = app.state.short_memory.load(thread_id)
        if thread is None:
            raise _error(404, "NOT_FOUND", "会话不存在", {"thread_id": thread_id})
        return {"thread_id": thread_id, "messages": thread.get("messages", [])}

    @app.post("/threads/{thread_id}/compress", dependencies=[Depends(_require_auth)])
    def compress_thread(thread_id: str) -> dict[str, Any]:
        """压缩会话：先备份，再用摘要替换消息列表。"""
        if thread_id in app.state.active_threads:
            raise _error(409, "CONFLICT", "会话正在运行，不能压缩", {"thread_id": thread_id})
        thread = app.state.short_memory.load(thread_id)
        if thread is None:
            raise _error(404, "NOT_FOUND", "会话不存在", {"thread_id": thread_id})
        backup_id, compressed = app.state.short_memory.compress(
            thread_id,
            app.state.backups_dir,
            chat_model=getattr(app.state, "chat_model", None),  # LLM 摘要 + 保留最近 5 条原文
        )
        return {
            "thread_id": thread_id,
            "summary": compressed.get("summary", ""),
            "backup_id": backup_id,
        }

    @app.post("/threads/{thread_id}/archive", dependencies=[Depends(_require_auth)])
    def archive_thread(thread_id: str) -> Response:
        """归档会话，归档前先备份。"""
        if thread_id in app.state.active_threads:
            raise _error(409, "CONFLICT", "会话正在运行，不能归档", {"thread_id": thread_id})
        thread = app.state.short_memory.load(thread_id)
        if thread is None:
            raise _error(404, "NOT_FOUND", "会话不存在", {"thread_id": thread_id})
        if not thread.get("archived"):
            app.state.short_memory.backup(thread_id, app.state.backups_dir)
        app.state.short_memory.set_archived(thread_id, True)
        return Response(status_code=204)

    @app.post("/threads/{thread_id}/unarchive", dependencies=[Depends(_require_auth)])
    def unarchive_thread(thread_id: str) -> Response:
        """恢复归档会话。"""
        thread = app.state.short_memory.load(thread_id)
        if thread is None:
            raise _error(404, "NOT_FOUND", "会话不存在", {"thread_id": thread_id})
        if not thread.get("archived"):
            raise _error(409, "CONFLICT", "会话未归档", {"thread_id": thread_id})
        app.state.short_memory.set_archived(thread_id, False)
        return Response(status_code=204)

    @app.delete("/threads/{thread_id}", dependencies=[Depends(_require_auth)])
    def delete_thread(thread_id: str) -> dict[str, Any]:
        """删除会话，删除前先备份。"""
        if thread_id in app.state.active_threads:
            raise _error(409, "CONFLICT", "会话正在运行，不能删除", {"thread_id": thread_id})
        thread = app.state.short_memory.load(thread_id)
        if thread is None:
            raise _error(404, "NOT_FOUND", "会话不存在", {"thread_id": thread_id})
        backup_id = app.state.short_memory.backup(thread_id, app.state.backups_dir)
        app.state.short_memory.delete(thread_id)
        return {"backup_id": backup_id}

    @app.get("/memory", dependencies=[Depends(_require_auth)])
    def get_memory(
        scope: str = "merged",
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """读取长期记忆。"""
        try:
            return app.state.long_term_memory.read(scope=scope, workspace=workspace)
        except ValueError as exc:
            raise _error(400, "VALIDATION_ERROR", str(exc)) from exc

    @app.post("/memory", dependencies=[Depends(_require_auth)])
    def write_memory(payload: MemoryWriteRequest) -> Response:
        """写入长期记忆，支持 replace 与 merge。"""
        try:
            app.state.long_term_memory.write(
                scope=payload.scope,
                workspace=payload.workspace,
                content=payload.content,
                mode=payload.mode,
                base_version=payload.base_version,
            )
        except MemoryConflictError as exc:
            raise _error(
                409,
                "CONFLICT",
                "长期记忆版本冲突",
                {"current_content": exc.content, "current_version": exc.version},
            ) from exc
        except ValueError as exc:
            raise _error(400, "VALIDATION_ERROR", str(exc)) from exc
        return Response(status_code=204)

    @app.post("/threads/{thread_id}/approve", dependencies=[Depends(_require_auth)])
    def approve_request(thread_id: str, payload: ApproveRequest) -> Response:
        """提交审批结果。"""
        try:
            app.state.approval_manager.submit(
                thread_id=thread_id,
                approval_id=payload.approval_id,
                decision=payload.decision,
            )
        except KeyError as exc:
            raise _error(404, "NOT_FOUND", "审批不存在", {"approval_id": payload.approval_id}) from exc
        except ValueError as exc:
            message = str(exc)
            if "已处理" in message:
                raise _error(409, "CONFLICT", message) from exc
            raise _error(400, "VALIDATION_ERROR", message) from exc
        return Response(status_code=204)

    @app.post("/tools/run", dependencies=[Depends(_require_auth)])
    def run_tool(payload: ToolRunRequest) -> StreamingResponse:
        """执行工具，需要审批时先流式发出审批事件。"""
        if payload.thread_id:
            thread_id = payload.thread_id
            if app.state.short_memory.load(thread_id) is None:
                raise _error(404, "NOT_FOUND", "会话不存在", {"thread_id": thread_id})
        else:
            thread_id = str(uuid.uuid4())
            workspace = str(
                (Path(payload.workspace) if payload.workspace else app.state.project_root).resolve()
            )
            app.state.short_memory.create(thread_id, workspace)
        run_id = payload.run_id or str(uuid.uuid4())
        tool_call_id = payload.tool_call_id or str(uuid.uuid4())

        async def event_stream() -> AsyncIterator[str]:
            """生成工具执行 SSE 事件。"""
            sequence = 0
            queue: asyncio.Queue = asyncio.Queue()

            def emit(event_name: str, data: dict[str, Any]) -> None:
                """把事件放入队列，由主循环实时转发。"""
                queue.put_nowait(("event", event_name, data))

            async def run_tool() -> None:
                """通过共享 ToolRuntime 执行工具并结束。"""
                try:
                    result = await app.state.tool_runtime.execute(
                        thread_id=thread_id,
                        run_id=run_id,
                        tool=payload.tool,
                        args=payload.args,
                        tool_call_id=tool_call_id,
                        emit=emit,
                    )
                    status = result["status"]
                except Exception as exc:  # noqa: BLE001 - 异常统一按失败结束
                    status = "failed"
                queue.put_nowait(("done", status))

            task = asyncio.create_task(run_tool())
            try:
                while True:
                    item = await queue.get()
                    if item[0] == "done":
                        status = item[1]
                        sequence += 1
                        yield (
                            f"id: {run_id}:{sequence}\n"
                            f"event: done\n"
                            f"data: {json.dumps({'thread_id': thread_id, 'status': status}, ensure_ascii=False)}\n\n"
                        )
                        break
                    event_name, data = item[1], item[2]
                    sequence += 1
                    yield (
                        f"id: {run_id}:{sequence}\n"
                        f"event: {event_name}\n"
                        f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                    )
            finally:
                task.cancel()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/permissions", dependencies=[Depends(_require_auth)])
    def list_permissions(
        scope: str = "project",
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """列出持久化权限。"""
        if scope not in {"global", "project"}:
            raise _error(400, "VALIDATION_ERROR", "scope 只支持 global 或 project")
        root = Path(workspace) if workspace else app.state.project_root
        permissions = app.state.permission_store._entries(scope, root)
        return {"permissions": permissions}

    @app.delete("/permissions/{permission_id}", dependencies=[Depends(_require_auth)])
    def delete_permission(
        permission_id: str,
        scope: str = "project",
        workspace: str | None = None,
    ) -> Response:
        """撤销持久化权限。"""
        root = Path(workspace) if workspace else app.state.project_root
        removed = app.state.permission_store.revoke(scope, root, permission_id)
        if not removed:
            raise _error(404, "NOT_FOUND", "权限不存在", {"permission_id": permission_id})
        return Response(status_code=204)

    @app.get("/backups", dependencies=[Depends(_require_auth)])
    def backups_list(
        scope: str = "project",
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """列出可恢复备份（当前仅项目作用域）。"""
        del workspace
        if scope not in {"global", "project"}:
            raise _error(400, "VALIDATION_ERROR", "scope 只支持 global 或 project")
        backups = app.state.backup_manager.list_backups() if scope == "project" else []
        return {"backups": backups}

    @app.post("/backups/{backup_id}/restore", dependencies=[Depends(_require_auth)])
    def backups_restore(backup_id: str) -> Response:
        """恢复备份，无效 id 返回 404。"""
        restored = app.state.backup_manager.restore(backup_id)
        if not restored:
            raise _error(404, "NOT_FOUND", "备份不存在", {"backup_id": backup_id})
        return Response(status_code=204)

    return app


def run_core(project_root: Path | None = None, port: int | None = None) -> None:
    """启动核心服务并写入运行时信息。"""
    root = (project_root or Path.cwd()).resolve()
    settings = load_settings(root)
    lock_file = acquire_single_instance_lock()
    try:
        actual_port = find_free_port(port)
        token = generate_token()
        write_runtime(actual_port, token)
        app = create_app(
            settings=settings,
            token=token,
            project_root=root,
            short_memory_dir=root / ".minic" / "memory" / "short_memory",
        )
        uvicorn.run(app, host="127.0.0.1", port=actual_port, log_level="info")
    finally:
        release_single_instance_lock(lock_file)
