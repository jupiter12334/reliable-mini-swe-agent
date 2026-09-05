# CE-02：调用前 Token 预算与调用后 usage 核对

CE-01 已把完整历史和模型输入之间的 Context Manager 接入点建立起来。
CE-02 在这个位置加入预算预检，并将同一次请求的估算值、实际用量和误差保存到 trajectory。

## 修改文件与阅读顺序

| 文件（仓库根目录下） | 新增职责 |
|---|---|
| `src/minisweagent/context_manager/token_counter.py` | 构建可注入计数函数，复用实际 LiteLLM 模型的消息预处理与 Bash 工具定义 |
| `src/minisweagent/context_manager/context.py` | 校验配置、计算总窗口占用率、触发单次压缩并复检、记录 usage |
| `src/minisweagent/agents/default.py` | 从配置组装 Context Manager；在调用前预检、调用后记账、退出时保存预算证据 |
| `src/minisweagent/config/context_budget.yaml` | 可直接与 `mini.yaml` 合并的配置示例 |
| `tests/agents/test_context.py` | 预算边界、历史保留、真实 usage、格式错误、CLI 拦截和本地计数测试 |

## 调用流程

```text
完整历史 self.messages
  → ContextManager.prepare_messages()
      → token_counter：复制消息，复用模型预处理，移除 extra
      → 估算消息 + 工具定义 + tool_choice
      → 计算 ContextUsage
      → 输入占总窗口 ≥ 80%：调用已注入的 compactor 一次
      → 对压缩结果重新计数；仍 ≥ 80%：安全退出
      → 未配置 compactor：明确报告缺失并安全退出
      → 通过预检：继续调用模型
  → n_calls += 1
  → model.query(model_messages, max_tokens=可选的生成长度上限)
  → 累计本次费用
  → ContextManager.record_usage(message)
      → 读取 response.usage
      → 保存实际输入/输出与本次估算误差
  → 追加 assistant 消息到完整历史
  → 按原流程执行工具和保存 trajectory
```

`prepare_messages()` 深拷贝完整历史，再将副本交给计数器与压缩器。压缩结果只用作
本轮模型输入，不覆盖完整 trajectory。实际 LiteLLM 计数器也会在副本上预处理。

## 预算计算

```text
remaining_context_tokens = max_context_tokens - estimated_input_tokens
utilization = estimated_input_tokens / max_context_tokens
needs_compaction = utilization >= compaction_threshold
```

默认 `compaction_threshold=0.8`，分母只使用模型总窗口。
例如总窗口 100：输入 79 不压缩；输入 80 必须压缩。
压缩后必须严格低于 80；降到恰好 80 仍退出。即使初始输入超过硬预算，也先给压缩器
一次机会，不会先因超限退出。一次预检最多压缩一次，不反复重试。

`max_output_tokens` 只是可选的单次生成长度上限，会作为 `max_tokens` 传给模型。
它不参与占用率、压缩或安全退出判断。CE-02 也不再设置 tokenizer 安全余量。

当前提供的是压缩调度接口，尚未实现生产历史摘要算法。可向 ContextManager 注入
`compactor: Callable[[list[dict]], list[dict]]`；它接收消息副本，返回完整的压缩后模型输入。
没有配置压缩器时，达到阈值会明确提示 `Compaction required but no compactor configured`，
保存 `ContextBudgetExceeded` 退出状态，不能把它误认为“压缩已执行但失败”。CLI 示例配置
尚不自动创建压缩器，生产摘要与压缩视图的跨轮复用仍待后续 CE 阶段实现。

`info.context.last_compaction` 保存压缩前后预算、尝试次数和状态；`unavailable` 表示
没有压缩器，`succeeded` 表示复检通过，`insufficient` 表示复检仍超限，`invalid_result`
表示返回了空消息或不合法的消息列表。压缩器必须负责保留任务信息与工具调用/结果配对。

`max_context_tokens=0` 默认关闭预算。启用时必须提供计数器，Token 限额不能为负，
压缩阈值必须在 `(0, 1]`。

如果设置 `max_output_tokens`，它会覆盖 LiteLLM 配置中原来的 `model_kwargs.max_tokens`。
避免同时配置 `max_completion_tokens`：自动组装会拒绝这个冲突。供应商仍可能拒绝不支持
的输出上限，使用者应选择其支持的数值。

## 如何计数与理解误差

自动组装从正在运行的模型读取 `model_name`，没有写死 DeepSeek。
计数时使用 `LitellmModel._prepare_messages_for_api()` 和真实的 `BASH_TOOL`，
保留工具调用参数与工具结果，移除不发送到 API 的 `extra.raw_output`、费用等元数据。

LiteLLM 本地计数是估算；它可能使用通用 tokenizer 回退，不能承诺跨厂商精确计数。
厂商上下文溢出仍由原版 `ContextWindowExceededError` 处理，不能因为预检通过就无限重试。

自动配置当前只支持标准 `LitellmModel` 的文本/Bash tool-call 链路，其他适配器要先
提供匹配其请求格式的 Context Manager。多模态内容在本轮明确拒绝；计数失败直接报错，
不会把异常当成 0 Token 放行。首次使用 tokenizer 可能需要下载资源，缓存就绪后可本地
计数；测试不调用生成 API，也不产生模型费用。

ContextManager 仍接收 `Callable[[list[dict]], int]`。测试可以注入字符计数函数，精确
验证预算公式；生产使用 LiteLLM 计数器，未来也可以替换成厂商计数接口。

## usage 如何被利用

运行中直接读取模型返回的 `extra.response.usage`，无需重新打开 trajectory 文件。
保存到每条已计数模型消息的 `extra.context_usage`：

```json
{
  "estimated_input_tokens": 1000,
  "max_context_tokens": 2000,
  "remaining_context_tokens": 1000,
  "utilization": 0.5,
  "needs_compaction": false,
  "actual_input_tokens": 1134,
  "actual_output_tokens": 85,
  "input_error_tokens": 134,
  "actual_to_estimated_ratio": 1.134
}
```

上面是测试示例：计数函数固定估出 1000，实际 usage 来自已保存的 `lo.json` 第一轮。
不是把字符数作为生产 Token 计数方式。

- `input_error_tokens = actual_input_tokens - estimated_input_tokens`，正数表示低估。
- 用量缺失时记录 `null`，不能当作 0，也不能沿用上一轮数据。
- 不额外加上 cached_tokens 或 reasoning_tokens；原始 usage 保留在原处。
- 不把所有轮次的 prompt_tokens 相加作为当前上下文长度。
- 保存的是校准依据，本轮不会根据一次误差自动调整预算。
- 已付费但格式错误的响应也记录 usage，避免遗漏。

`info.context` 保存生效配置、最新预算预检与最近一次 usage 核对。历史各轮仍在
`messages[].extra.context_usage` 中。超限请求没有返回 usage，其退出消息只记录预检报告。

## 测试与运行

先执行离线测试：

```bash
uv run pytest -q tests/agents/test_context.py tests/agents/test_default.py
uv run ruff check src/minisweagent/context_manager src/minisweagent/agents/default.py tests/agents/test_context.py
uv run ruff format --check src/minisweagent/context_manager src/minisweagent/agents/default.py tests/agents/test_context.py
```

可以用极小预算演示 CLI 拦截；这个命令在生成 API 调用前结束：

```bash
MSWEA_CONFIGURED=true uv run mini -m gpt-4o-mini \
  -c mini.yaml \
  -c agent.context.max_context_tokens=2 \
  -c agent.context.max_output_tokens=1 \
  -t "测试预算拦截" --exit-immediately \
  -o trajectories/ce02-blocked.json
```

预期 `info.exit_status=ContextBudgetExceeded`，`info.model_stats.api_calls=0`。
CLI 沿用上游行为，业务终止状态保存在 trajectory 中，Shell 退出码仍可能为 0。

已有模型凭证后，可在实际任务目录用已安装的 `mini` 启用示例配置：

```bash
mini -c mini.yaml -c context_budget.yaml \
  -t "你的代码修改任务" -o ce02-run.json
```

这个命令会调用配置的真实模型。示例 32768 是应用自设预算，使用前确认它不超过模型的
上下文能力；也要核对厂商单独的输入与输出限制。不要把注册表中的 `max_tokens` 一律
当作总上下文窗口。默认 `mini.yaml` 仍使用人工确认命令的模式。

检查每轮报告：

```bash
jq '.messages[] | select(.extra.context_usage != null) | .extra.context_usage' ce02-run.json
```

CE-02 已接入单次压缩调度与复检，尚不提供生产历史摘要器、输出外置和断点恢复。
