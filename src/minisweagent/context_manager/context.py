from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass
from math import isfinite

from minisweagent.context_manager.token_counter import TokenCounter


@dataclass(frozen=True)
class ContextConfig:
    """上下文预算配置；默认 max_context_tokens=0，保持上游行为不变。"""

    # 这是 Agent 自己采用的总预算，不一定等于模型厂商公布的最大窗口。
    max_context_tokens: int = 0
    # 只限制单次生成长度，不参与上下文占用率与压缩判断。
    max_output_tokens: int = 0
    # 输入占总窗口的比例达到此值时，先压缩，再重新检查。
    compaction_threshold: float = 0.8

    def __post_init__(self):
        for value in (self.max_context_tokens, self.max_output_tokens):
            if type(value) is not int or value < 0:
                raise ValueError("Token limits must be non-negative integers")
        if not isfinite(self.compaction_threshold) or not 0 < self.compaction_threshold <= 1:
            raise ValueError("compaction_threshold must be in (0, 1]")


@dataclass(frozen=True)
class ContextUsage:
    """一次模型调用前计算出的预算快照。"""

    estimated_input_tokens: int
    max_context_tokens: int
    remaining_context_tokens: int
    utilization: float
    needs_compaction: bool


class ContextBudgetExceeded(RuntimeError):
    def __init__(self, usage: ContextUsage, reason: str):
        self.usage = usage
        super().__init__(
            f"{reason}: {usage.estimated_input_tokens} input tokens, "
            f"window utilization {usage.utilization:.2%}, context window {usage.max_context_tokens}"
        )


class ContextManager:
    def __init__(
        self,
        *,
        max_context_tokens: int = 0,
        max_output_tokens: int = 0,
        compaction_threshold: float = 0.8,
        token_counter: TokenCounter | None = None,
        compactor: Callable[[list[dict]], list[dict]] | None = None,
    ):
        self.config = ContextConfig(max_context_tokens, max_output_tokens, compaction_threshold)
        # Token 规则依赖具体模型，因此通过函数注入，而不是在管理器里写死厂商。
        if self.config.max_context_tokens and token_counter is None:
            raise ValueError("An enabled context budget requires a token_counter")
        self.token_counter = token_counter
        self.compactor = compactor
        self.last_usage: ContextUsage | None = None
        self.last_observation: dict | None = None
        self.last_compaction: dict | None = None

    def prepare_messages(self, messages: list[dict]) -> list[dict]:
        """构造本轮模型输入，并在真正请求模型前完成预算检查。"""

        prepared = deepcopy(messages)
        # 每次调用都对应一份独立报告，不能误用上一轮的真实 usage。
        self.last_usage = None
        self.last_observation = None
        self.last_compaction = None
        if self.config.max_context_tokens == 0:
            return prepared
        self.last_usage = self._measure(prepared)
        if self.last_usage.needs_compaction:
            self.last_compaction = {
                "before": asdict(self.last_usage),
                "after": None,
                "attempts": 0,
                "status": "unavailable",
            }
            if self.compactor is None:
                raise ContextBudgetExceeded(self.last_usage, "Compaction required but no compactor configured")
            # 每次预检最多压缩一次；不使用 while，避免无效压缩导致死循环。
            self.last_compaction.update(attempts=1, status="running")
            prepared = self.compactor(prepared)
            if not isinstance(prepared, list) or not prepared or not all(isinstance(m, dict) for m in prepared):
                self.last_compaction["status"] = "invalid_result"
                raise ContextBudgetExceeded(self.last_usage, "Compactor must return a non-empty message list")
            self.last_usage = self._measure(prepared)
            self.last_compaction["after"] = asdict(self.last_usage)
            if self.last_usage.needs_compaction:
                self.last_compaction["status"] = "insufficient"
                raise ContextBudgetExceeded(self.last_usage, "Compaction did not meet the context budget")
            self.last_compaction["status"] = "succeeded"
        return prepared

    def _measure(self, messages: list[dict]) -> ContextUsage:
        assert self.token_counter is not None
        estimated = self.token_counter(messages)
        if type(estimated) is not int or estimated < 0 or (messages and estimated == 0):
            raise ValueError("token_counter must return a positive integer for non-empty messages")
        return ContextUsage(
            estimated_input_tokens=estimated,
            max_context_tokens=self.config.max_context_tokens,
            remaining_context_tokens=self.config.max_context_tokens - estimated,
            utilization=estimated / self.config.max_context_tokens,
            needs_compaction=estimated >= self.config.max_context_tokens * self.config.compaction_threshold,
        )

    def query_kwargs(self) -> dict:
        """可选地限制单次生成长度；它与上下文压缩阈值无关。"""

        if self.config.max_output_tokens:
            return {"max_tokens": self.config.max_output_tokens}
        return {}

    def record_usage(self, message: dict) -> None:
        """模型返回后，用厂商 usage 核对调用前估算值并保存误差。"""

        if self.last_usage is None:
            return
        response = message.get("extra", {}).get("response")
        usage = response.get("usage") if isinstance(response, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        # 兼容 Chat Completions 和 Responses 风格的字段名。
        actual_input = usage.get("prompt_tokens", usage.get("input_tokens"))
        actual_output = usage.get("completion_tokens", usage.get("output_tokens"))
        actual_input = actual_input if type(actual_input) is int and actual_input >= 0 else None
        actual_output = actual_output if type(actual_output) is int and actual_output >= 0 else None
        estimated = self.last_usage.estimated_input_tokens
        # cached_tokens/reasoning_tokens 都是明细，已包含在总数中，不能再次相加。
        self.last_observation = {
            **asdict(self.last_usage),
            "actual_input_tokens": actual_input,
            "actual_output_tokens": actual_output,
            "input_error_tokens": actual_input - estimated if actual_input is not None else None,
            "actual_to_estimated_ratio": actual_input / estimated if actual_input is not None and estimated else None,
        }
        message.setdefault("extra", {})["context_usage"] = self.last_observation.copy()

    def serialize(self) -> dict:
        """提供可写入 trajectory 的配置、预检结果和实际 usage 核对结果。"""

        if self.config.max_context_tokens == 0:
            return {}
        return {
            "config": asdict(self.config),
            "last_usage": asdict(self.last_usage) if self.last_usage is not None else None,
            "last_observation": self.last_observation,
            "last_compaction": self.last_compaction,
        }
