"""Starting a run, and saying where to watch it.

A subprocess, not an import: a run outlives whatever started it, and
``--web-bg`` already detaches one.

Never raises: every failure is something to tell whoever asked.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ictus.interfaces.conductor.control.launch import binary, launch_command, launch_env
from ictus.interfaces.conductor.control.live import live_runs

if TYPE_CHECKING:
    from ictus.runs.triggers import Trigger

logger = logging.getLogger(__name__)

__all__ = ["Asked", "Started", "start"]

#: How long to wait for Conductor to detach before giving up on it.
LAUNCH_TIMEOUT_SECONDS = 180.0


@dataclass(frozen=True, slots=True)
class Asked:
    """Somebody asked for a run, and what they asked about."""

    question: str
    thread: str
    """Whatever the asking service calls this conversation. Opaque here, and
    handed back to the pipeline as an input."""

    channel: str
    who: str
    trigger: Trigger | None = None
    """Which pipeline was asked for. One listener serves many."""


@dataclass(frozen=True, slots=True)
class Started:
    """What became of a request to start a run."""

    why: str = ""
    """Empty when it started. Otherwise what to tell the person who asked."""

    dashboard: str = ""
    """Where to watch it, from the run's own record."""

    run_id: str = ""

    @property
    def ok(self) -> bool:
        return not self.why


def start(request: Asked, trigger: Trigger) -> Started:
    """Launch a run for ``request``, and say where to watch it. Never raises."""
    # Before the subprocess: the check's whole worth is being free.
    gaps = trigger.missing()
    if gaps:
        return Started("; ".join(gaps))
    inputs = {trigger.question_input: request.question}
    if trigger.thread_input:
        inputs[trigger.thread_input] = request.thread
    try:
        command = launch_command(
            binary(),
            # Resolved: the run works in another directory, where a path
            # relative to this one would name nothing.
            trigger.workflow.resolve(),
            inputs=inputs,
            dashboard=True,
            background=True,
            workspace_instructions=trigger.workspace_instructions,
            # Both of these: `listen_on` refuses an `into` or a thread that is
            # not a string, so neither was ever meant to be read as a number.
            # A conversation's id is the one that suffers — Slack's are
            # timestamps, and a coerced one matches no message.
            verbatim=tuple(inputs),
        )
    except FileNotFoundError as exc:
        return Started(str(exc))
    # What the pipeline announced, and nothing else. The run's process is the
    # only place an environment can be cut, so this listener's own app-level
    # token — declared by no pipeline — never reaches a run.
    passing = launch_env(need.name for need in trigger.env)
    held_back = len(os.environ) - len(passing)
    if held_back > 0:
        logger.debug(
            "%s: passing %d variables, holding back %d", trigger.pipeline, len(passing), held_back
        )
    # Conductor prints the dashboard only to a terminal, so the new run's port
    # is read from its own record: whichever id was not there a moment ago.
    before = {run.run_id for run in live_runs()}
    try:
        done = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=LAUNCH_TIMEOUT_SECONDS,
            env=passing,
            # Never this listener's own directory: a deployed pipeline's
            # relative paths belong to it, not to wherever it was listened for.
            cwd=trigger.folder,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Started("the run did not finish starting in time")
    if done.returncode != 0:
        said = (done.stderr or done.stdout or "").strip().splitlines()
        return Started(said[-1] if said else "it refused to start")
    return _launched(before)


def _launched(before: set[str]) -> Started:
    """The run that was not there before, and where to watch it.

    A bare success when it cannot be told apart: two launches at once, or a
    record not yet written.
    """
    fresh = [run for run in live_runs() if run.run_id not in before]
    if len(fresh) != 1:
        return Started()
    return Started(dashboard=fresh[0].dashboard, run_id=fresh[0].run_id)
