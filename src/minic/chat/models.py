"""聊天模型工厂，真实 provider 使用 init_chat_model。"""  # 模块说明

from __future__ import annotations  # 延迟注解求值

import contextvars  # 请求级模型选择
import json  # JSON 序列化
from typing import Any, AsyncIterator, Iterator  # 类型提示

from langchain_core.callbacks.manager import (  # 回调管理器
    AsyncCallbackManagerForLLMRun,  # 异步回调
    CallbackManagerForLLMRun,  # 同步回调
)  # 导入结束
from langchain_core.language_models.chat_models import BaseChatModel  # 聊天模型基类
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage  # 消息类型
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult  # 输出类型

from minic.core.config import AppSettings  # 应用配置
from minic.middleware import SandboxPolicy  # 沙箱网络白名单


class MockChatModel(BaseChatModel):  # 测试用模型
    """测试用确定性模型，不调用任何外部服务。"""  # 类说明

    model_name: str = "mock"  # 模型名称
    temperature: float = 0.0  # 固定温度

    @property  # 属性
    def _llm_type(self) -> str:  # 返回类型标识
        """返回模型类型标识。"""  # 方法说明
        return "mock"  # 固定类型

    def bind_tools(self, tools: list[dict[str, Any]], **kwargs: Any) -> "MockChatModel":  # 工具绑定
        """忽略工具绑定（mock 无原生工具调用）。"""  # 方法说明
        del tools, kwargs  # 未使用
        return self  # 返回自身

    def _mock_content(self, messages: list[BaseMessage]) -> str:  # 生成固定回答
        """根据消息内容返回固定输出。"""  # 方法说明
        first_content = messages[0].content if messages else ""  # 读取首条消息
        if isinstance(first_content, str):  # 只处理字符串内容
            if "总图路由" in first_content or "意图路由" in first_content:  # 路由提示
                return json.dumps({"intent": "knowledge"})  # 固定返回知识意图
            if "记忆提取" in first_content:  # 提取提示
                return "[]"  # 固定返回空结果
            if "用户刚刚提供了个人信息" in first_content:  # 确认提示
                return "好的，我记住了。"  # 固定确认文本
            if "改写" in first_content:  # 改写提示
                return "LangGraph 的作用"  # 固定改写结果
        return "这是基于资料生成的模拟回答。"  # 默认回答

    def _generate(  # 同步生成
        self,  # 实例
        messages: list[BaseMessage],  # 消息列表
        stop: list[str] | None = None,  # 停止词
        run_manager: CallbackManagerForLLMRun | None = None,  # 同步回调
        **kwargs: Any,  # 其他参数
    ) -> ChatResult:  # 返回生成结果
        """同步生成完整回答。"""  # 方法说明
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self._mock_content(messages)))])  # 返回固定结果

    async def _agenerate(  # 异步生成
        self,  # 实例
        messages: list[BaseMessage],  # 消息列表
        stop: list[str] | None = None,  # 停止词
        run_manager: AsyncCallbackManagerForLLMRun | None = None,  # 异步回调
        **kwargs: Any,  # 其他参数
    ) -> ChatResult:  # 返回生成结果
        """异步生成完整回答。"""  # 方法说明
        return self._generate(messages, stop=stop, **kwargs)  # 复用同步实现

    def _stream(  # 同步流式
        self,  # 实例
        messages: list[BaseMessage],  # 消息列表
        stop: list[str] | None = None,  # 停止词
        run_manager: CallbackManagerForLLMRun | None = None,  # 同步回调
        **kwargs: Any,  # 其他参数
    ) -> Iterator[ChatGenerationChunk]:  # 返回分片迭代器
        """同步按分片流式返回回答。"""  # 方法说明
        for part in ["这是", "基于资料", "生成的", "模拟回答。"]:  # 固定分片
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=part))  # 构造分片
            if run_manager is not None:  # 有回调
                run_manager.on_llm_new_token(part, chunk=chunk.message)  # 通知新 token
            yield chunk  # 产出分片

    async def _astream(  # 异步流式
        self,  # 实例
        messages: list[BaseMessage],  # 消息列表
        stop: list[str] | None = None,  # 停止词
        run_manager: AsyncCallbackManagerForLLMRun | None = None,  # 异步回调
        **kwargs: Any,  # 其他参数
    ) -> AsyncIterator[ChatGenerationChunk]:  # 返回异步分片迭代器
        """异步按分片流式返回回答。"""  # 方法说明
        for part in ["这是", "基于资料", "生成的", "模拟回答。"]:  # 固定分片
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=part))  # 构造分片
            if run_manager is not None:  # 有回调
                await run_manager.on_llm_new_token(part, chunk=chunk.message)  # 通知新 token
            yield chunk  # 产出分片


class DisabledChatModel(BaseChatModel):
    """未就绪的模型：核心可正常启动，实际调用时报明确错误（如缺 API Key）。

    首次安装没有 API Key 时用于占位：用户在设置中配置后热更新即替换为真实模型。
    """

    name: str = ""  # 配置名（与注册表 key 一致，仅用于错误信息）
    reason: str = "请先在设置中配置 API Key"  # 未就绪原因

    @property
    def _llm_type(self) -> str:
        """返回模型类型标识。"""
        return "disabled"

    def bind_tools(self, tools: list[dict[str, Any]], **kwargs: Any) -> "DisabledChatModel":
        """忽略工具绑定（禁用态模型不发起真实调用）。"""
        del tools, kwargs
        return self

    def _error(self) -> RuntimeError:
        """构造带明确原因的运行时错误。"""
        return RuntimeError(f"模型「{self.name or '未命名'}」未就绪：{self.reason}")

    def _generate(self, messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
        """禁用态模型不支持生成。"""
        raise self._error()

    async def _agenerate(self, messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
        """禁用态模型不支持生成。"""
        raise self._error()

    def _stream(self, messages: list[BaseMessage], **kwargs: Any) -> Iterator[ChatGenerationChunk]:
        """禁用态模型不支持流式生成。"""
        raise self._error()
        yield  # pragma: no cover - 使函数成为生成器

    async def _astream(self, messages: list[BaseMessage], **kwargs: Any) -> AsyncIterator[ChatGenerationChunk]:
        """禁用态模型不支持流式生成。"""
        raise self._error()
        yield  # pragma: no cover - 使函数成为异步生成器


def _get_init_chat_model() -> Any:  # 获取 init_chat_model
    """兼容不同版本的 init_chat_model 导入路径。"""  # 函数说明
    try:  # 优先新版路径
        from langchain.chat_models import init_chat_model  # 新版导入
    except ImportError:  # 新版不存在
        from langchain_core.language_models.chat_models import init_chat_model  # 旧版导入
    return init_chat_model  # 返回工厂函数


def create_chat_model(settings: AppSettings) -> Any:  # 创建模型注册表
    """根据配置创建多模型注册表（每项启用配置一个实例）。

    返回 :class:`ModelRegistry`：graph 节点调用 ``chat_model.ainvoke/astream``
    时按请求级 contextvar 选择当前模型（默认第一个启用模型）。
    mock 仅用于测试；真实模型校验 base_url 域名白名单。
    """
    models, default_name = _build_models(settings)
    return ModelRegistry(models=models, default_name=default_name)  # 返回注册表


def validate_model_configs(settings: AppSettings) -> None:
    """仅做配置级校验（域名白名单），不创建模型实例（PUT /settings 时用）。"""
    sandbox_policy = SandboxPolicy(
        model_api_whitelist=settings.sandbox.model_api_whitelist,
    )
    for cfg in settings.model.enabled_models:
        if cfg.provider == "mock":
            continue
        if not sandbox_policy.check_network(cfg.base_url):
            raise ValueError(
                f"模型 {cfg.name} 的 base_url 域名不在沙箱白名单内: {cfg.base_url}，"
                "请在 sandbox.model_api_whitelist 中添加该域名"
            )


def _build_models(settings: AppSettings) -> tuple[dict[str, Any], str]:
    """按配置构建 模型名 -> 实例 字典与默认模型名（仅启用模型）。"""
    sandbox_policy = SandboxPolicy(  # 沙箱策略实例
        model_api_whitelist=settings.sandbox.model_api_whitelist,  # 域名白名单
    )  # 策略结束
    init_chat_model = _get_init_chat_model()  # 获取模型工厂
    models: dict[str, Any] = {}  # name -> 模型实例
    for cfg in settings.model.enabled_models:  # 遍历启用模型
        if cfg.provider == "mock":  # 测试配置
            models[cfg.name] = MockChatModel()  # 测试环境使用确定性 mock 模型
            continue
        if not sandbox_policy.check_network(cfg.base_url):  # 域名不在白名单内
            raise ValueError(  # 启动即失败，错误信息明确
                f"模型 {cfg.name} 的 base_url 域名不在沙箱白名单内: {cfg.base_url}，"  # 提示配置项
                "请在 sandbox.model_api_whitelist 中添加该域名"  # 修改指引
            )  # 异常结束
        kwargs: dict[str, Any] = {  # 模型参数
            "model_provider": cfg.provider,  # 提供商
            "base_url": cfg.base_url,  # API 地址
            "temperature": cfg.temperature,  # 温度
        }  # 参数结束
        if cfg.api_key:  # 配置了密钥
            kwargs["api_key"] = cfg.api_key  # 配置了 API Key 才传给真实模型
        try:  # 缺 API Key 等环境问题不阻止核心启动（首次安装双击即用）
            models[cfg.name] = init_chat_model(cfg.model, **kwargs)  # 按配置创建真实聊天模型
        except Exception as exc:  # noqa: BLE001 - 未就绪模型降级为禁用态，调用时报明确错误
            models[cfg.name] = DisabledChatModel(name=cfg.name, reason=str(exc))
    if not models:  # 全禁用/空配置兜底
        models["mock"] = MockChatModel()  # 保证可用
    default_name = settings.model.primary().name  # 默认模型名
    return models, default_name


class ModelRegistry:
    """多模型注册表：按请求（contextvar）选择当前模型，代理到模型实例。

    graph 节点保持 ``chat_model.ainvoke(messages)`` 等调用不变；
    请求级切换用 :meth:`use` / :meth:`reset`；配置变更用 :meth:`update` 热更新。
    """

    def __init__(self, models: dict[str, Any], default_name: str) -> None:
        self._models = models  # name -> 模型实例（仅启用模型）
        self._default_name = default_name  # 缺省模型名
        self._ctx: contextvars.ContextVar[str] = contextvars.ContextVar(  # 请求级模型选择
            "current_model", default=default_name
        )

    def update(self, settings: AppSettings) -> None:
        """配置变更后热更新：重建启用模型实例集合（禁用模型立即不可用）。

        构建失败（如无网络/凭据）时保留旧模型：配置已持久化，下次启动生效。
        """
        try:
            models, default_name = _build_models(settings)
        except Exception:  # noqa: BLE001 - 构建失败保留旧实例
            return
        self._models = models
        self._default_name = default_name
        if self._ctx.get() not in self._models:  # 当前请求模型已禁用时回退默认
            self._ctx.set(default_name)

    def use(self, name: str) -> contextvars.Token:
        """选择当前请求使用的模型，返回恢复 token。"""
        if name not in self._models:  # 未知/未启用模型名
            raise KeyError(f"模型未启用或不存在: {name}")
        return self._ctx.set(name)

    def reset(self, token: contextvars.Token) -> None:
        """恢复上一个请求级模型选择。"""
        self._ctx.reset(token)

    def current(self) -> Any:
        """当前模型实例（缺省第一个启用模型）。"""
        name = self._ctx.get()
        return self._models.get(name) or self._models[self._default_name]

    def names(self) -> list[str]:
        """可用模型名列表（仅启用）。"""
        return list(self._models.keys())

    def __getattr__(self, item: str) -> Any:
        """代理模型接口（ainvoke/astream 等）到当前模型实例。"""
        return getattr(self.current(), item)
