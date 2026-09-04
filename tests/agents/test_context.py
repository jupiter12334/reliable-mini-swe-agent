from minisweagent.agents.default import DefaultAgent
from minisweagent.context_manager.context import ContextManager
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.test_models import DeterministicModel, make_output


class LastMessageContextManager(ContextManager):
    def __init__(self):
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
