import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from minisweagent.agents.default import DefaultAgent
from minisweagent.context_manager.context import ContextBudgetExceeded, ContextManager
from minisweagent.context_manager.token_counter import make_litellm_token_counter, make_model_token_counter
from minisweagent.environments.local import LocalEnvironment
from minisweagent.exceptions import FormatError
from minisweagent.models.litellm_model import LitellmModel
from minisweagent.models.test_models import DeterministicModel, make_output
from minisweagent.models.utils.actions_toolcall import BASH_TOOL


class LastMessageContextManager(ContextManager):
    def __init__(self):
        super().__init__()
        self.received_messages: list[dict] = []

    def prepare_messages(self, messages: list[dict]) -> list[dict]:
        self.received_messages = list(messages)
        return [messages[-1]]


class RecordingModel(DeterministicModel):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.received_messages: list[dict] = []

    def query(self, messages: list[dict[str, str]], **kwargs) -> dict:
        self.received_messages = list(messages)
        self.received_kwargs = kwargs
        return super().query(messages, **kwargs)


def test_context_manager_builds_model_view_without_mutating_history():
    context_manager = LastMessageContextManager()
    model = RecordingModel(
        outputs=[
            make_output(
                "Inspect the repository",
                [{"command": "pwd"}],
            )
        ]
    )
    agent = DefaultAgent(
        model=model,
        env=LocalEnvironment(),
        context_manager=context_manager,
        system_template="You are a coding agent.",
        instance_template="{{task}}",
    )

    original_history = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "Fix the bug."},
    ]
    agent.add_messages(*original_history)

    agent.query()

    assert context_manager.received_messages == original_history
    assert model.received_messages == [original_history[-1]]
    assert agent.messages[:2] == original_history
    assert len(agent.messages) == 3
    assert agent.messages[-1]["role"] == "assistant"


def count_characters(messages: list[dict]) -> int:
    return sum(len(message.get("content") or "") for message in messages)


def make_agent(manager: ContextManager, model=None, **kwargs) -> DefaultAgent:
    return DefaultAgent(
        model=model if model is not None else RecordingModel(outputs=[]),
        env=LocalEnvironment(),
        context_manager=manager,
        system_template="system",
        instance_template="{{task}}",
        **kwargs,
    )


@pytest.mark.parametrize(("length", "remaining"), [(63, 37), (64, 36), (79, 21)])
def test_budget_uses_total_window_below_threshold(length, remaining):
    manager = ContextManager(
        max_context_tokens=100,
        max_output_tokens=1000,
        token_counter=count_characters,
    )
    messages = [{"role": "user", "content": "x" * length}]
    prepared = manager.prepare_messages(messages)
    assert prepared == messages and prepared is not messages
    assert manager.last_usage.estimated_input_tokens == length
    assert manager.last_usage.max_context_tokens == 100
    assert manager.last_usage.remaining_context_tokens == remaining
    assert manager.last_usage.utilization == pytest.approx(length / 100)
    assert manager.last_usage.needs_compaction is False
    assert manager.last_compaction is None


@pytest.mark.parametrize(
    ("before", "after", "success"), [(80, 79, True), (110, 79, True), (80, 80, False), (90, 85, False), (90, 90, False)]
)
def test_compaction_runs_once_and_recounts_before_model_call(before, after, success, tmp_path):
    calls = []

    def compact(messages):
        calls.append(deepcopy(messages))
        messages[0]["content"] = "x" * after
        return messages

    manager = ContextManager(
        max_context_tokens=100,
        token_counter=count_characters,
        compactor=compact,
    )
    agent = make_agent(manager, RecordingModel(outputs=[make_output("done", [])]))
    messages = [{"role": "user", "content": "x" * before}]
    agent.add_messages(*deepcopy(messages))
    if success:
        response = agent.query()
        assert agent.model.received_messages[0]["content"] == "x" * after
        assert response["extra"]["context_usage"]["estimated_input_tokens"] == after
        assert agent.n_calls == 1
    else:
        with pytest.raises(ContextBudgetExceeded, match="Compaction did not meet"):
            agent.query()
        assert agent.n_calls == 0 and agent.cost == 0
    assert len(calls) == 1
    assert calls[0] == messages and agent.messages[:1] == messages
    report = agent.save(tmp_path / "compaction.json")["info"]["context"]["last_compaction"]
    assert report["before"]["estimated_input_tokens"] == before
    assert report["after"]["estimated_input_tokens"] == after
    assert report["attempts"] == 1
    assert report["status"] == ("succeeded" if success else "insufficient")


def test_failed_compaction_exits_run_once_and_saves_evidence(tmp_path):
    attempts = []

    def compact(messages):
        attempts.append(1)
        return messages

    agent = make_agent(
        ContextManager(max_context_tokens=10, token_counter=count_characters, compactor=compact),
        output_path=tmp_path / "failed.json",
    )
    assert agent.run("task")["exit_status"] == "ContextBudgetExceeded"
    saved = json.loads((tmp_path / "failed.json").read_text())
    assert attempts == [1] and agent.n_calls == 0
    assert saved["info"]["context"]["last_compaction"]["status"] == "insufficient"


def test_output_cap_does_not_affect_compaction_threshold():
    manager = ContextManager(max_context_tokens=100, max_output_tokens=1000, token_counter=count_characters)
    messages = [{"role": "user", "content": "x" * 79}]
    assert manager.prepare_messages(messages) == messages
    assert manager.last_usage.needs_compaction is False
    assert manager.last_compaction is None
    assert manager.query_kwargs() == {"max_tokens": 1000}


def test_output_cap_can_be_used_without_enabling_context_budget():
    model = RecordingModel(outputs=[make_output("done", [])])
    agent = DefaultAgent(
        model,
        LocalEnvironment(),
        system_template="system",
        instance_template="task",
        context={"max_output_tokens": 50},
    )
    agent.add_messages({"role": "user", "content": "x" * 1000})
    agent.query()
    assert model.received_kwargs == {"max_tokens": 50}
    assert agent.context_manager.last_usage is None


def test_empty_compaction_result_cannot_bypass_budget():
    manager = ContextManager(max_context_tokens=10, token_counter=count_characters, compactor=lambda messages: [])
    agent = make_agent(manager)
    agent.add_messages({"role": "user", "content": "12345678"})
    with pytest.raises(ContextBudgetExceeded, match="non-empty message list"):
        agent.query()
    assert agent.n_calls == 0
    assert manager.last_compaction["status"] == "invalid_result"


def test_overflow_prevents_call_and_preserves_history():
    manager = ContextManager(max_context_tokens=10, token_counter=count_characters)
    agent = make_agent(manager)
    messages = [{"role": "user", "content": "123456789"}]
    agent.add_messages(*messages)
    with pytest.raises(ContextBudgetExceeded) as exc:
        agent.query()
    assert exc.value.usage.remaining_context_tokens == 1
    assert agent.n_calls == 0 and agent.cost == 0
    assert agent.model.current_index == -1
    assert agent.messages == messages
    assert manager.last_compaction["status"] == "unavailable"
    assert manager.last_compaction["attempts"] == 0


def test_overflow_run_saves_clean_exit(tmp_path):
    agent = make_agent(
        ContextManager(max_context_tokens=3, token_counter=count_characters),
        output_path=tmp_path / "run.json",
    )
    assert agent.run("task")["exit_status"] == "ContextBudgetExceeded"
    saved = json.loads((tmp_path / "run.json").read_text())
    assert saved["info"]["model_stats"]["api_calls"] == 0
    assert saved["info"]["context"]["last_usage"]["estimated_input_tokens"] == 10
    assert saved["messages"][-1]["extra"]["context_usage"]["max_context_tokens"] == 3


def test_disabled_budget_does_not_count_or_change_output_limit():
    def forbidden_counter(messages):
        raise AssertionError("Disabled budget must not invoke counter")

    manager = ContextManager(token_counter=forbidden_counter)
    messages = [{"role": "user", "content": "hello"}]
    assert manager.prepare_messages(messages) == messages
    assert manager.last_usage is None
    assert manager.query_kwargs() == {} and manager.serialize() == {}


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        ({"max_context_tokens": -1}, "non-negative integers"),
        ({"max_output_tokens": -1}, "non-negative integers"),
        ({"compaction_threshold": 0}, "compaction_threshold"),
        ({"compaction_threshold": 1.1}, "compaction_threshold"),
        ({"compaction_threshold": float("nan")}, "compaction_threshold"),
        ({"max_context_tokens": 1.5}, "non-negative integers"),
    ],
)
def test_invalid_budget_configuration(config, reason):
    with pytest.raises(ValueError, match=reason):
        ContextManager(**config, token_counter=count_characters)


def test_enabled_budget_requires_counter():
    with pytest.raises(ValueError, match="token_counter"):
        ContextManager(max_context_tokens=10)


@pytest.mark.parametrize(
    ("count", "reason"),
    [(-1, "positive integer"), (0, "positive integer"), (1.5, "positive integer"), (True, "positive integer")],
)
def test_invalid_counts_cannot_bypass_budget(count, reason):
    agent = make_agent(ContextManager(max_context_tokens=10, token_counter=lambda messages: count))
    agent.add_messages({"role": "user", "content": "hello"})
    with pytest.raises(ValueError, match=reason):
        agent.query()
    assert agent.n_calls == 0


def test_real_trajectory_usage_and_missing_usage_are_recorded_per_request():
    trajectory = json.loads((Path(__file__).parents[1] / "test_weather_mcp" / "lo.json").read_text())
    first = deepcopy(trajectory["messages"][2])
    manager = ContextManager(max_context_tokens=2000, max_output_tokens=100, token_counter=count_characters)
    model = RecordingModel(outputs=[first, make_output("no usage", [])])
    agent = make_agent(manager, model)
    agent.add_messages({"role": "user", "content": "x" * 1000})
    response = agent.query()
    report = response["extra"]["context_usage"]
    assert report["estimated_input_tokens"] == 1000
    assert report["actual_input_tokens"] == 1134
    assert report["actual_output_tokens"] == 85
    assert report["input_error_tokens"] == 134
    assert report["actual_to_estimated_ratio"] == pytest.approx(1.134)
    assert model.received_kwargs == {"max_tokens": 100}
    assert agent.serialize()["info"]["context"]["last_observation"] == report
    assert agent.query()["extra"]["context_usage"]["actual_input_tokens"] is None
    assert manager.last_observation["input_error_tokens"] is None
    assert report["actual_input_tokens"] == 1134


def test_billed_format_error_retains_usage_and_budget(tmp_path):
    error_message = {
        "role": "user",
        "content": "missing tool call",
        "extra": {"cost": 0.2, "response": {"usage": {"prompt_tokens": 15, "completion_tokens": 3}}},
    }

    class FormatErrorModel(RecordingModel):
        def query(self, messages, **kwargs):
            raise FormatError(deepcopy(error_message))

    agent = make_agent(
        ContextManager(max_context_tokens=100, token_counter=count_characters),
        FormatErrorModel(outputs=[]),
        max_consecutive_format_errors=1,
        output_path=tmp_path / "format-error.json",
    )
    assert agent.run("task")["exit_status"] == "RepeatedFormatError"
    assert agent.n_calls == 1 and agent.cost == pytest.approx(0.2)
    saved = json.loads((tmp_path / "format-error.json").read_text())
    assert saved["messages"][2]["extra"]["context_usage"]["actual_input_tokens"] == 15


def test_litellm_counts_tools_and_ignores_private_metadata_without_mutation():
    messages = [
        {"role": "user", "content": "Fix the bug."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": '{"command":"pwd"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "/workspace"},
    ]
    model = LitellmModel(model_name="gpt-4o-mini")
    counter = make_litellm_token_counter(
        model_name=model.config.model_name, tools=[BASH_TOOL], prepare_messages=model._prepare_messages_for_api
    )
    expected = counter(messages)
    assert expected > make_litellm_token_counter(model_name="gpt-4o-mini")(messages) > 0
    messages[-1]["extra"] = {"raw_output": "large output" * 1000, "response": {"cost": 12}}
    original = deepcopy(messages)
    assert counter(messages) == expected
    assert messages == original


def test_text_only_scope_is_explicit():
    counter = make_litellm_token_counter(model_name="gpt-4o-mini")
    with pytest.raises(ValueError, match="text chat"):
        counter([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/x"}}]}])


def test_automatic_setup_checks_adapter():
    with pytest.raises(ValueError, match="LitellmModel only"):
        DefaultAgent(
            RecordingModel(outputs=[]),
            LocalEnvironment(),
            system_template="s",
            instance_template="t",
            context={"max_context_tokens": 100},
        )


def test_model_counter_factory_narrows_model_without_accepting_custom_subclasses():
    model = LitellmModel(model_name="gpt-4o-mini")
    assert make_model_token_counter(model)([{"role": "user", "content": "hello"}]) > 0

    class CustomLitellmModel(LitellmModel):
        pass

    with pytest.raises(ValueError, match="LitellmModel only"):
        make_model_token_counter(CustomLitellmModel(model_name="gpt-4o-mini"))


def test_cli_context_config_blocks_before_any_generation(tmp_path):
    output = tmp_path / "cli.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "minisweagent.run.mini",
            "-m",
            "gpt-4o-mini",
            "-t",
            "Offline budget test",
            "-c",
            "mini.yaml",
            "-c",
            "context_budget.yaml",
            "-c",
            "agent.context.max_context_tokens=2",
            "-c",
            "agent.context.max_output_tokens=1",
            "-o",
            str(output),
            "--exit-immediately",
        ],
        env=os.environ | {"MSWEA_CONFIGURED": "true", "MSWEA_SILENT_STARTUP": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    saved = json.loads(output.read_text())
    assert saved["info"]["exit_status"] == "ContextBudgetExceeded"
    assert saved["info"]["model_stats"]["api_calls"] == 0
    assert saved["info"]["context"]["config"]["max_output_tokens"] == 1
