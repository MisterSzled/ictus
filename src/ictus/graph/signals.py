"""Moments in a run that something outside it may want to hear about.

Named for what happens rather than for what any engine calls it. A backend
declares in ``Capabilities`` which it can report.

Not a routing mechanism — a gate's answer and a scope's outcome are edges.
This is for telling somebody, including about moments no node can observe.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["ANNOUNCED_BY_STEPS", "RunSignal"]


class RunSignal(StrEnum):
    """A moment in a run worth reporting."""

    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    """Includes a run declined at the start gate: nothing was attempted."""

    RUN_FAILED = "run_failed"
    RUN_PAUSED = "run_paused"
    """A person paused it and it is waiting to be resumed."""

    DECISION_NEEDED = "decision_needed"
    """The run is parked, spending nothing, until somebody arrives."""

    DECISION_MADE = "decision_made"
    STEP_FAILED = "step_failed"
    """Not ``RUN_FAILED``: a graph can route around a failed step, so this fires
    on runs that go on to succeed."""

    BUDGET_EXCEEDED = "budget_exceeded"
    """Whether it stops the run depends on ``budget_mode``, so this says the
    ceiling was crossed and nothing about what happened next."""


#: What a step can stand in front of, so an attached integration reports it
#: from inside the run. ``RUN_FAILED`` only partly: an explicit failed exit
#: has a step in front of it, an engine failure does not. The rest need
#: ``ictus watch``.
ANNOUNCED_BY_STEPS = frozenset(
    {
        RunSignal.RUN_STARTED,
        RunSignal.DECISION_NEEDED,
        RunSignal.RUN_FINISHED,
        RunSignal.RUN_FAILED,
    }
)
