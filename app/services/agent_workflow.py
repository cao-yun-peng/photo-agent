"""搜索、选择、确认和生成的显式工作流守卫。"""

from __future__ import annotations

from typing import Any

WORKFLOW_STATES = frozenset(
    {
        "idle",
        "searching",
        "results_ready",
        "awaiting_selection",
        "selection_confirmed",
        "awaiting_generation_confirmation",
        "generation_queued",
        "failed",
    }
)

_TRANSITIONS = {
    "idle": {
        "searching",
        "selection_confirmed",
        "awaiting_generation_confirmation",
        "failed",
    },
    "searching": {"results_ready", "awaiting_selection", "failed"},
    "results_ready": {
        "searching",
        "selection_confirmed",
        "awaiting_generation_confirmation",
        "failed",
    },
    "awaiting_selection": {"selection_confirmed", "searching", "failed"},
    "selection_confirmed": {
        "searching",
        "awaiting_generation_confirmation",
        "failed",
    },
    "awaiting_generation_confirmation": {
        "generation_queued",
        "searching",
        "failed",
    },
    "generation_queued": {"searching", "failed"},
    "failed": {"searching", "idle"},
}


def transition_workflow(state: Any, target: str) -> None:
    current = str(getattr(state, "workflow_state", "idle") or "idle")
    if target not in WORKFLOW_STATES:
        raise ValueError(f"unknown workflow state: {target}")
    if target == current:
        return
    if target not in _TRANSITIONS.get(current, set()):
        raise ValueError(f"invalid workflow transition: {current} -> {target}")
    state.workflow_state = target
