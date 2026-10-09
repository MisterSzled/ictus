"""Sending one report, and deciding which reports go where.

The machinery behind the boundary. ``ictus/notify/__init__.py`` used to hold all
of this, which made the file whose job is to *be* a boundary the file with the
subprocess calls and the credential masking in it — and left it the only package
init in the tree carrying an implementation.

One way to send, used twice. An ``Integration`` carries a program that sends one
report; a step runs it from inside the graph, and the watcher runs it from
outside for the moments no step can see. Neither knows which service is on the
other end.

A report never breaks a run. Delivery happens outside the engine and after the
fact, and a channel being unreachable says nothing about whether the work
succeeded, so every failure is collected and returned rather than raised.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ictus.graph.signals import RunSignal

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from ictus.graph.requirements import Integration
    from ictus.interfaces import SignalEvent

__all__ = ["HEADLINE", "Delivered", "deliver", "send", "summarise"]

TIMEOUT_SECONDS = 20.0

#: What each signal says, in the voice of somebody telling you about it.
HEADLINE: dict[RunSignal, str] = {
    RunSignal.RUN_STARTED: "started",
    RunSignal.RUN_FINISHED: "finished",
    RunSignal.RUN_FAILED: "failed",
    RunSignal.RUN_PAUSED: "is paused, waiting to be resumed",
    RunSignal.DECISION_NEEDED: "needs a decision",
    RunSignal.DECISION_MADE: "got its answer",
    RunSignal.STEP_FAILED: "had a step fail",
    RunSignal.BUDGET_EXCEEDED: "went over budget",
}


@dataclass(frozen=True, slots=True)
class Delivered:
    """What happened to one report."""

    integration: str
    signal: str
    sent: bool
    detail: str = ""
    """Why not, when not. Never contains a credential."""


def summarise(event: SignalEvent, *, dashboard: str = "") -> str:
    """One report, as text, for whatever service is going to carry it.

    Plain: each integration's program wraps it the way its own service wants.
    The first line says what happened and which run; the rest is in the
    dashboard, linked when its address is known.
    """
    lines = [f"*{event.workflow}* {HEADLINE.get(event.signal, event.signal.value)}"]
    if event.step:
        lines.append(f"> step: `{event.step}`")
    if event.signal is RunSignal.DECISION_NEEDED:
        if event.options:
            lines.append("> waiting on: " + ", ".join(f"`{o}`" for o in event.options))
        lines.extend(_opening(event.prompt))
    if event.signal is RunSignal.DECISION_MADE:
        if event.choice:
            lines.append(f"> answered: `{event.choice}`")
        lines.extend(f"> {name}: {text}" for name, text in event.notes)
    if event.signal in (RunSignal.RUN_FAILED, RunSignal.STEP_FAILED):
        lines.extend(_opening(event.reason))
    footer = f"run `{event.run_id}`"
    lines.append(f"{footer} · {dashboard}" if dashboard else footer)
    return "\n".join(lines)


def _opening(value: str, *, limit: int = 160) -> list[str]:
    """The first line of some prose, flattened. A channel is not a document."""
    if not value.strip():
        return []
    opening = value.strip().splitlines()[0]
    return ["> " + (opening if len(opening) <= limit else opening[: limit - 1] + "…")]


def send(
    service: Integration,
    text: str,
    *,
    thread: str = "",
    env: Mapping[str, str] | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> str:
    """Run ``service``'s program with ``text`` on stdin. "" on success, else why not.

    The same program a step runs. Credentials are read by the program from the
    environment and never pass through here.

    ``thread`` is what to reply under, when the service threads; ``timeout`` is
    the program's own deadline, and the process is given five seconds more.
    """
    merged = {**os.environ, **(env or {})}
    try:
        done = subprocess.run(
            [service.command, "-c", service.program, thread, "", f"{timeout:g}"],
            input=text,
            capture_output=True,
            text=True,
            timeout=timeout + 5,
            check=False,
            env=merged,
        )
    except FileNotFoundError:
        return f"{service.command!r} is not on PATH, so nothing could be sent"
    except subprocess.TimeoutExpired:
        return f"{service.name!r} did not answer in {timeout:g}s"
    said = _said(done.stdout)
    if done.returncode == 0 and said.get("posted") == "true":
        return ""
    # The program's own stderr, masked again here rather than trusting every
    # program to keep the no-credentials promise itself.
    why = (done.stderr or done.stdout or "the report failed with no explanation").strip()
    return _masked(why, service, merged)


def _said(stdout: str) -> dict[str, object]:
    """What the program printed about itself, or nothing if it printed nonsense."""
    try:
        loaded = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _masked(text: str, service: Integration, env: Mapping[str, str]) -> str:
    """``text`` with the value of every variable ``service`` declares replaced."""
    for var in service.required_env:
        value = env.get(var.name, "")
        for form in {value, value.strip()}:
            if form:
                text = text.replace(form, "***")
    return text


def deliver(
    event: SignalEvent,
    services: Iterable[Integration],
    *,
    dashboard: str = "",
    threads: Mapping[str, str] | None = None,
    env: Mapping[str, str] | None = None,
) -> list[Delivered]:
    """Report ``event`` to every service that asked for its signal.

    One result per attempt, successes included. Raises nothing.

    Two kinds of event are dropped: one a step already announced from inside
    the run, and one replayed from the run's history on attaching.

    ``threads`` is each service's conversation for this run, by name.
    """
    if event.at_a_step or event.replayed:
        return []
    text = summarise(event, dashboard=dashboard)
    results: list[Delivered] = []
    for service in services:
        if not service.wants(event.signal):
            continue
        why = send(service, text, thread=(threads or {}).get(service.name, ""), env=env)
        results.append(Delivered(service.name, event.signal.value, sent=not why, detail=why))
    return results
