from collections.abc import Callable
from copy import deepcopy

import litellm

from minisweagent import Model
from minisweagent.models.litellm_model import LitellmModel
from minisweagent.models.utils.actions_toolcall import BASH_TOOL

TokenCounter = Callable[[list[dict]], int]


def make_litellm_token_counter(
    *,
    model_name: str,
    tools: list[dict] | None = None,
    prepare_messages: Callable[[list[dict]], list[dict]] | None = None,
    tool_choice: str | dict | None = None,
) -> TokenCounter:
    """创建绑定模型配置的本地计数函数；跨厂商 fallback 结果仍可能有误差。"""

    def count_tokens(messages: list[dict]) -> int:
        # 某些模型预处理会原地修改消息，深拷贝可保护 Agent 的完整历史。
        prepared = deepcopy(messages)
        if prepare_messages is not None:
            # 复用真实请求的预处理，确保“估算的消息”和“发送的消息”格式一致。
            prepared = prepare_messages(prepared)
        # extra 保存费用、原始响应等本地元数据，它们不会发送给模型，也不应计数。
        prepared = [{key: value for key, value in message.items() if key != "extra"} for message in prepared]
        for message in prepared:
            content = message.get("content")
            if isinstance(content, list) and any(
                not isinstance(block, dict) or block.get("type") != "text" for block in content
            ):
                raise ValueError("CE-02 token estimation currently supports text chat messages only")
        if litellm.disable_token_counter:
            raise ValueError("LiteLLM token counting is disabled; cannot enforce context budget")
        # tools 和 tool_choice 也会占上下文，不能只计算 messages。
        return litellm.token_counter(
            model=model_name,
            messages=prepared,
            tools=deepcopy(tools),
            tool_choice=tool_choice,
            use_default_image_token_count=True,
        )

    return count_tokens


def make_model_token_counter(model: Model, *, max_output_tokens: int = 0) -> TokenCounter:
    """从正在运行的 LiteLLM 模型提取名称、消息预处理和 Bash 工具定义。"""

    # 其他模型适配器的请求结构可能不同，必须显式注入与其匹配的计数器。
    # isinstance 用于静态类型收窄；精确类型检查继续排除请求格式未知的子类。
    if not isinstance(model, LitellmModel):
        raise ValueError("Automatic CE-02 setup supports LitellmModel only; inject a ContextManager for other adapters")
    if type(model) is not LitellmModel:
        raise ValueError("Automatic CE-02 setup supports LitellmModel only; inject a ContextManager for other adapters")
    # 避免两个输出上限同时存在，导致“预算值”和“实际请求值”不一致。
    if max_output_tokens and "max_completion_tokens" in model.config.model_kwargs:
        raise ValueError("Use context.max_output_tokens instead of model_kwargs.max_completion_tokens")
    return make_litellm_token_counter(
        model_name=model.config.model_name,
        tools=[BASH_TOOL],
        prepare_messages=model._prepare_messages_for_api,
        tool_choice=model.config.model_kwargs.get("tool_choice"),
    )
