from types import SimpleNamespace

from app.services.agent_workflow import transition_workflow


def test_confirmed_selection_can_queue_an_explicit_generation() -> None:
    state = SimpleNamespace(workflow_state="selection_confirmed")

    transition_workflow(state, "generation_queued")

    assert state.workflow_state == "generation_queued"

