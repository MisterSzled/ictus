"""Answering a gate from a choice somebody made outside the run.

Between the two boundaries and owned by neither: the choice comes from a
service, the gate from a run on an engine. ``Press`` is the whole contract —
who, which gate, which answer, which step posted it, which message it is on,
and whether the answer needs text.

A press names no run. It names the step that posted its button and the message
it is on, and the run is found by asking each live run's history which posted
that message. That settles two things:

* **Which run**, with no engine variable reaching the button.
* **Which round.** A gate in a loop is asked again under the same name, and
  the engine matches an answer by name alone, so a press is accepted only on
  the message the step posted last.

The answer must also be one the gate offers: the engine takes any other value
with a 200, then fails the run on it.
"""

from __future__ import annotations

import http.client
from dataclasses import dataclass

from ictus.interfaces.conductor.control.events import history, offered, step_outputs
from ictus.interfaces.conductor.control.live import LiveRun, live_runs
from ictus.interfaces.conductor.control.respond import answer_gate

__all__ = ["Outcome", "Press", "resolve", "submit"]


@dataclass(frozen=True, slots=True)
class Press:
    """Somebody outside the run chose one of a gate's answers. Carries no transport."""

    who: str
    """Whoever the service says made the choice. The only authorisation signal
    there is, and meaningful only to the service that issued it."""

    gate: str
    choice: str
    step: str
    """The step that posted it. Which run it belongs to is found from this."""

    message: str
    """The message the choice was offered on: which time the question was asked."""

    needs_text: bool = False
    """The choice asks for text as well. ``resolve`` says so rather than
    answering, and ``submit`` takes what comes back."""


@dataclass(frozen=True, slots=True)
class Outcome:
    """What became of one press."""

    answered: bool
    reason: str = ""
    """Why not, when not. Phrased for whoever pressed, not for a log."""

    run_id: str = ""
    needs_note: bool = False
    """The choice asks for text: ask for it, then ``submit`` what comes back."""


def resolve(press: Press, *, allowed: frozenset[str] = frozenset()) -> Outcome:
    """Answer the gate a press names, or say why not.

    ``allowed`` is the user ids that may answer. Empty means anyone who can
    see the button.
    """
    if allowed and press.who not in allowed:
        return Outcome(False, f"they are not allowed to answer {press.gate}")
    found = _current(press)
    if isinstance(found, Outcome):
        return found
    if press.needs_text:
        return Outcome(False, run_id=found.run_id, needs_note=True)
    return _answer(found, press, None)


def submit(press: Press, text: str, *, allowed: frozenset[str] = frozenset()) -> Outcome:
    """Answer with the text a form collected.

    Checked again from the start: the question may have moved on.
    """
    if allowed and press.who not in allowed:
        return Outcome(False, f"they are not allowed to answer {press.gate}")
    found = _current(press)
    if isinstance(found, Outcome):
        return found
    return _answer(found, press, text)


def _current(press: Press) -> LiveRun | Outcome:
    """The run that posted this message, if it is still the question being asked."""
    for run in live_runs():
        try:
            events = history(run)
        except (OSError, ValueError, http.client.HTTPException):
            continue  # a run that cannot be read cannot be the one that posted it
        posted = [str(output.get("thread", "")) for output in step_outputs(events, press.step)]
        if press.message not in posted:
            continue
        if posted[-1] != press.message:
            return Outcome(
                False,
                f"{press.gate} has been asked again since that message; answer the newest one, "
                "or answer in the dashboard",
                run.run_id,
            )
        options = offered(events, press.gate)
        if options and press.choice not in options:
            return Outcome(
                False, f"{press.choice!r} is not an answer {press.gate} offers", run.run_id
            )
        return run
    return Outcome(
        False,
        "no run on this machine asked that question; it has finished, or it is running elsewhere",
    )


def _answer(run: LiveRun, press: Press, note: str | None) -> Outcome:
    result = answer_gate(run, gate=press.gate, choice=press.choice, note=note)
    if result.accepted:
        return Outcome(True, run_id=run.run_id)
    return Outcome(False, f"the run would not take it: {result.detail}", run.run_id)
