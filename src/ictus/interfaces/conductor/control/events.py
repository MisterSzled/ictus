"""Reading a live run as signals.

``signals_from`` is a pure function over event dicts; the transport is separate
and needs a running engine.

Five rules:

* **Connect before seeding.** The socket replays nothing, so history is fetched
  after it opens. The overlap is deduped on ``(type, timestamp)``.
* **Reading needs no token.** ``GET /api/state`` is guarded by Origin/Host
  alone; only the socket handshake and writes need one.
* **Let go on the run's own terminal event.** A held socket stops a detached
  run from exiting. A stage emits its own ``workflow_completed`` stamped with
  ``subworkflow_path``, which is filtered out.
* **A dropped socket is not an ended run.** The watch dials again while the
  process lives, and reports the run failing once it is gone.
* **Holding the socket keeps a paused agent paused.** The engine resumes one
  only once every client has disconnected, and a watcher is a client.
"""

from __future__ import annotations

import http.client
import json
import time
from dataclasses import replace
from typing import TYPE_CHECKING

from ictus.graph.signals import RunSignal
from ictus.interfaces import SignalEvent
from ictus.interfaces.conductor.control.live import LOOPBACK, alive, token_for
from ictus.interfaces.conductor.control.signals import signal_for
from ictus.net.websocket import WebSocket

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from ictus.interfaces.conductor.control.live import LiveRun

__all__ = ["STATE_TIMEOUT_SECONDS", "history", "offered", "signals_from", "step_outputs", "watch"]

STATE_TIMEOUT_SECONDS = 15.0

#: Consecutive failures to reach a live dashboard before giving up.
RECONNECT_ATTEMPTS = 5

#: The run's own lifecycle. From a stage, these are the stage's, not the run's.
_LIFECYCLE = frozenset({"workflow_started", "workflow_completed", "workflow_failed"})

#: Moments a step stands in front of when an integration is attached.
_ANNOUNCED = frozenset(
    {"workflow_started", "workflow_completed", "gate_presented", "questions_presented"}
)


def signals_from(
    events: Iterable[dict[str, object]],
    run: LiveRun,
    *,
    seen: set[tuple[str, float]] | None = None,
    live_from: float | None = None,
) -> Iterator[SignalEvent]:
    """Every reportable moment in ``events``, in order, stopping when the run ends.

    An event standing for no signal is dropped rather than raising. ``seen``
    carries the dedupe across reconnects; anything older than ``live_from`` is
    marked replayed.
    """
    seen = set() if seen is None else seen
    for event in events:
        kind = event.get("type")
        if not isinstance(kind, str):
            continue
        at = event.get("timestamp")
        moment = float(at) if isinstance(at, (int, float)) else 0.0
        if (kind, moment) in seen:
            continue
        seen.add((kind, moment))
        signal = signal_for(kind)
        if signal is None:
            continue
        payload = event.get("data")
        data = payload if isinstance(payload, dict) else {}
        if kind in _LIFECYCLE and data.get("subworkflow_path"):
            continue  # a stage starting or finishing, not the run
        reported = _neutral(signal, kind, moment, data, run)
        if live_from is not None and moment < live_from:
            reported = replace(reported, replayed=True)
        yield reported
        if reported.ends_the_run:
            return


def _neutral(
    signal: RunSignal, kind: str, moment: float, data: dict[object, object], run: LiveRun
) -> SignalEvent:
    """The engine-neutral facts in one Conductor payload."""
    notes = data.get("additional_input")
    options = data.get("options")
    step = data.get("agent_name") or data.get("group_name") or ""
    prompt = data.get("prompt") or data.get("opening_question") or ""
    reason = data.get("termination_reason") or data.get("message") or data.get("error") or ""
    explicit = kind == "workflow_failed" and data.get("is_explicit") is True
    return SignalEvent(
        signal=signal,
        run_id=run.run_id,
        workflow=run.workflow,
        at=moment,
        event_type=kind,
        step=str(step),
        options=tuple(str(o) for o in options) if isinstance(options, list) else (),
        prompt=str(prompt),
        choice=str(data.get("selected_option") or ""),
        notes=tuple((str(k), str(v)) for k, v in notes.items() if v)
        if isinstance(notes, dict)
        else (),
        reason=str(reason),
        # A failed terminate is an explicit exit a step announced; a failure
        # the engine raised is not.
        at_a_step=kind in _ANNOUNCED or explicit,
    )


def history(run: LiveRun, *, timeout: float = STATE_TIMEOUT_SECONDS) -> list[dict[str, object]]:
    """Everything the run emitted before now. Needs no token."""
    with LOOPBACK.open(f"{run.dashboard}/api/state", timeout=timeout) as response:
        loaded = json.loads(response.read())
    return (
        [event for event in loaded if isinstance(event, dict)] if isinstance(loaded, list) else []
    )


def step_outputs(events: Iterable[dict[str, object]], step: str) -> list[dict[str, object]]:
    """What ``step`` printed each time it completed, oldest first.

    A script's output reaches the stream only as the stdout of
    ``script_completed``, so it is parsed back here. A run that printed nothing
    readable counts as an empty object rather than being skipped.
    """
    found: list[dict[str, object]] = []
    for event in events:
        data = event.get("data")
        if event.get("type") != "script_completed" or not isinstance(data, dict):
            continue
        if data.get("agent_name") != step:
            continue
        stdout = data.get("stdout")
        lines = stdout.strip().splitlines() if isinstance(stdout, str) else []
        try:
            parsed = json.loads(lines[-1]) if lines else {}
        except ValueError:
            parsed = {}
        found.append(parsed if isinstance(parsed, dict) else {})
    return found


def offered(events: Iterable[dict[str, object]], gate: str) -> tuple[str, ...]:
    """The answers ``gate`` offered the last time it was asked, if it has been.

    The engine accepts an unoffered value with a 200, then fails the run on it.
    """
    options: tuple[str, ...] = ()
    for event in events:
        data = event.get("data")
        if event.get("type") != "gate_presented" or not isinstance(data, dict):
            continue
        if data.get("agent_name") == gate and isinstance(data.get("options"), list):
            options = tuple(str(option) for option in data["options"])
    return options


def watch(
    run: LiveRun,
    *,
    token: str | None = None,
    pause: Callable[[float], None] = time.sleep,
) -> Iterator[SignalEvent]:
    """Attach to ``run`` and yield its signals until it ends.

    Dials again while the run's process lives, and reports the run failing if
    the process disappears. Closes the socket however it leaves: a run cannot
    reap while anything is connected to it.
    """
    resolved = token if token is not None else token_for(run.port)
    headers = {"Authorization": f"Bearer {resolved}"} if resolved else {}
    seen: set[tuple[str, float]] = set()
    live_from = time.time()
    failures = 0
    while True:
        try:
            with WebSocket("127.0.0.1", run.port, "/ws", headers=headers) as socket:
                failures = 0
                for event in signals_from(
                    _seeded(socket, run), run, seen=seen, live_from=live_from
                ):
                    yield event
                    if event.ends_the_run:
                        return
        except (OSError, ValueError, http.client.HTTPException):
            failures += 1
        if not alive(run.pid):
            yield SignalEvent(
                signal=RunSignal.RUN_FAILED,
                run_id=run.run_id,
                workflow=run.workflow,
                at=time.time(),
                event_type="engine_gone",
                reason="the engine stopped without reporting why: killed, crashed, or asleep",
            )
            return
        if failures >= RECONNECT_ATTEMPTS:
            raise ConnectionError(
                f"run {run.run_id} is alive but its dashboard on port {run.port} stopped "
                f"answering after {failures} attempts"
            )
        pause(min(2.0**failures, 30.0))


def _seeded(socket: WebSocket, run: LiveRun) -> Iterator[dict[str, object]]:
    """The run's history, then everything after it.

    Fetched after the socket is open, so an overlapping event arrives twice
    rather than not at all.
    """
    yield from history(run)
    for raw in socket.messages():
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            yield loaded
