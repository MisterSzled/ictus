"""Answering a run's gate from outside it.

``events.py`` reads what a run is doing; this decides what it does next, and
is the whole of the authority over a live run.

``POST /api/gate-respond`` rather than the socket, whose gate responses are
fire-and-forget: the REST call answers 200, 409 if the gate moved on, or 403
if the token is wrong.

The engine refuses a response that does not name the gate currently waiting
(``_validate_gate_target``), but matches by name only — so telling the rounds
of a loop apart is the caller's job. See ``ictus.runs.answer``.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ictus.interfaces.conductor.control.live import LOOPBACK, token_for

if TYPE_CHECKING:
    from ictus.interfaces.conductor.control.live import LiveRun

__all__ = ["Answered", "answer_gate"]

TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class Answered:
    """What became of one answer."""

    accepted: bool
    detail: str = ""
    """Why not, when not. Phrased for whoever clicked, not for a log."""


def answer_gate(
    run: LiveRun,
    *,
    gate: str,
    choice: str,
    note: str | None = None,
    token: str | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> Answered:
    """Resolve ``gate`` on ``run`` with ``choice``. Returns rather than raises."""
    resolved = token if token is not None else token_for(run.port)
    if resolved is None:
        return Answered(False, "no dashboard token on this machine, so the run would refuse it")

    body: dict[str, object] = {"agent_name": gate, "selected_value": choice}
    if note is not None:
        body["additional_input"] = note
    request = urllib.request.Request(
        f"{run.dashboard}/api/gate-respond",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {resolved}",
        },
        method="POST",
    )
    try:
        with LOOPBACK.open(request, timeout=timeout) as response:
            if response.status < 300:
                return Answered(True)
            return Answered(False, f"the run answered {response.status}")
    except urllib.error.HTTPError as exc:
        return Answered(False, _why(exc.code, gate))
    except urllib.error.URLError:
        return Answered(False, "the run is no longer listening; it has probably finished")
    except TimeoutError:
        return Answered(False, "the run did not answer in time")
    except (http.client.HTTPException, OSError) as exc:
        # A dashboard shutting down mid-request drops the connection rather
        # than refusing it.
        return Answered(False, f"the run stopped answering partway ({type(exc).__name__})")


def _why(status: int, gate: str) -> str:
    """What a status code means to the person who pressed the button."""
    if status == 409:
        return f"{gate!r} has already been answered, or the run has moved past it"
    if status == 403:
        return "the token was refused"
    if status == 422:
        return "the run could not read that answer"
    return f"the run answered {status}"
