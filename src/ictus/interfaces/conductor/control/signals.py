"""Which Conductor events stand for which ``RunSignal``.

Here rather than beside the enum, because the right-hand side is Conductor's
spelling.

One signal covers several event names. Most of Conductor's ~45 event types map
to none: ``agent_started``, ``checkpoint_saved``, ``route_taken`` and the
per-tool events are a trace, not news.

A gate is not the only place a run waits for a person — the iteration-limit
prompt and an agent's dialog both do, and no step can stand in front of either.

``test_signals.py`` checks every name here against the installed engine.
"""

from __future__ import annotations

from ictus.graph.signals import RunSignal

__all__ = ["REPORTABLE", "SIGNAL_EVENTS", "signal_for"]

#: Conductor event names, by the signal they stand for.
SIGNAL_EVENTS: dict[RunSignal, frozenset[str]] = {
    RunSignal.RUN_STARTED: frozenset({"workflow_started"}),
    RunSignal.RUN_FINISHED: frozenset({"workflow_completed"}),
    RunSignal.RUN_FAILED: frozenset({"workflow_failed"}),
    RunSignal.RUN_PAUSED: frozenset({"agent_paused"}),
    RunSignal.DECISION_NEEDED: frozenset(
        {"gate_presented", "questions_presented", "iteration_limit_reached", "dialog_started"}
    ),
    RunSignal.DECISION_MADE: frozenset(
        {"gate_resolved", "questions_completed", "iteration_limit_resolved", "dialog_completed"}
    ),
    RunSignal.STEP_FAILED: frozenset(
        {
            "agent_failed",
            "agent_validation_failed",
            "script_failed",
            "set_failed",
            "wait_failed",
            "mcp_failed",
            "subworkflow_failed",
            "parallel_agent_failed",
            "for_each_item_failed",
        }
    ),
    RunSignal.BUDGET_EXCEEDED: frozenset({"budget_exceeded"}),
}

#: Every signal Conductor can report. Declared in ``Capabilities.signals``.
REPORTABLE: frozenset[RunSignal] = frozenset(SIGNAL_EVENTS)

_BY_EVENT: dict[str, RunSignal] = {
    event: signal for signal, events in SIGNAL_EVENTS.items() for event in events
}


def signal_for(event_type: str) -> RunSignal | None:
    """The signal ``event_type`` stands for, or ``None`` if it is not news.

    Returning ``None`` rather than raising is deliberate: an engine upgrade that
    adds an event type must not break a watcher that is mid-run. An unmapped
    event is dropped, and the test suite is what notices a rename.
    """
    return _BY_EVENT.get(event_type)
