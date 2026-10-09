"""Answering a gate from a choice made outside the run.

Not one Slack object anywhere in this file, which is the assertion: ``Press``
is six fields and no transport.
"""

from __future__ import annotations

import json

import pytest

from ictus.interfaces.conductor.control.live import LiveRun
from ictus.interfaces.conductor.control.respond import Answered
from ictus.runs.answer import Outcome, Press, resolve, submit

RUN = LiveRun(run_id="abc12345", workflow="w", port=59999, pid=1, started_at="2026")
OTHER = LiveRun(run_id="ffff0000", workflow="w", port=59998, pid=1, started_at="2026")


def _press(**over: object) -> Press:
    fields: dict[str, object] = {
        "gate": "ship_it",
        "choice": "approved",
        "step": "report_ship_it",
        "who": "U123",
        "message": "200.2",
    }
    fields.update(over)
    return Press(**fields)  # type: ignore[arg-type]


def _history(*threads: str, options: tuple[str, ...] = ("approved", "rejected")) -> list[object]:
    """A run whose report step posted these messages, in order."""
    posted: list[object] = [
        {
            "type": "script_completed",
            "data": {
                "agent_name": "report_ship_it",
                "stdout": json.dumps({"thread": ts, "posted": "true"}) + "\n",
            },
        }
        for ts in threads
    ]
    presented = {
        "type": "gate_presented",
        "data": {"agent_name": "ship_it", "options": list(options)},
    }
    return [*posted, presented]


@pytest.fixture
def answered(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    """Every answer sent, by run id and note, with two runs live and nothing sent for real."""
    sent: list[tuple[str, str | None]] = []
    histories = {RUN.run_id: _history("200.1", "200.2"), OTHER.run_id: _history("900.1")}

    def answer(run: LiveRun, *, note: str | None = None, **_: object) -> Answered:
        sent.append((run.run_id, note))
        return Answered(True)

    monkeypatch.setattr("ictus.runs.answer.live_runs", lambda: [OTHER, RUN])
    monkeypatch.setattr("ictus.runs.answer.history", lambda run: histories[run.run_id])
    monkeypatch.setattr("ictus.runs.answer.answer_gate", answer)
    return sent


def test_a_press_is_answered_on_the_run_that_posted_it(
    answered: list[tuple[str, str | None]],
) -> None:
    """No run id travels with the button; the run's own history says it asked."""
    assert resolve(_press(message="200.2")) == Outcome(True, run_id=RUN.run_id)
    assert answered == [(RUN.run_id, None)]


def test_a_press_on_an_earlier_round_is_refused(answered: list[tuple[str, str | None]]) -> None:
    """The engine matches a gate by name, so this used to approve a round nobody read."""
    outcome = resolve(_press(message="200.1"))
    assert not outcome.answered
    assert "asked again since" in outcome.reason
    assert answered == []


def test_a_question_no_live_run_asked_says_so(answered: list[tuple[str, str | None]]) -> None:
    outcome = resolve(_press(message="555.5"))
    assert not outcome.answered
    assert "no run on this machine" in outcome.reason
    assert answered == []


def test_an_answer_the_gate_does_not_offer_is_refused(
    answered: list[tuple[str, str | None]],
) -> None:
    """The engine takes it with a 200, then fails the run on it."""
    outcome = resolve(_press(choice="maybe"))
    assert not outcome.answered
    assert "is not an answer" in outcome.reason
    assert answered == []


def test_a_press_from_somebody_not_allowed_does_nothing(
    answered: list[tuple[str, str | None]],
) -> None:
    outcome = resolve(_press(who="U999"), allowed=frozenset({"U123"}))
    assert not outcome.answered
    assert "not allowed" in outcome.reason
    assert answered == []


def test_a_choice_that_needs_text_waits_for_it(answered: list[tuple[str, str | None]]) -> None:
    """Answering without the notes ran the revision on nothing."""
    outcome = resolve(_press(choice="rejected", needs_text=True))
    assert outcome.needs_note
    assert answered == []


def test_a_note_is_answered_with_its_text(answered: list[tuple[str, str | None]]) -> None:
    press = _press(choice="rejected", needs_text=True)
    assert submit(press, "the tests are missing").answered
    assert answered == [(RUN.run_id, "the tests are missing")]


def test_a_note_for_a_question_that_moved_on_is_refused(
    answered: list[tuple[str, str | None]],
) -> None:
    """The form was open while somebody answered in the dashboard and the loop came round."""
    press = _press(choice="rejected", needs_text=True, message="200.1")
    assert not submit(press, "x").answered
    assert answered == []


def test_a_run_that_cannot_be_read_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    def history(run: LiveRun) -> list[object]:
        if run is OTHER:
            raise ConnectionResetError
        return _history("200.2")

    monkeypatch.setattr("ictus.runs.answer.live_runs", lambda: [OTHER, RUN])
    monkeypatch.setattr("ictus.runs.answer.history", history)
    monkeypatch.setattr("ictus.runs.answer.answer_gate", lambda *a, **k: Answered(True))  # noqa: ARG005
    assert resolve(_press()).answered
