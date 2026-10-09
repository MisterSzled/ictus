"""``ictus watch`` — what a live run is doing, relayed to its audience.

Reads; cannot change a run. Covers what no step inside the graph can report: a
budget tripping, a step failing, the engine being killed. ``trace``, which
reads a run that has already finished, is in ``tracing.py``.
"""

from __future__ import annotations

import http.client
import threading
from dataclasses import dataclass

# Runtime, not TYPE_CHECKING: Typer resolves a command's annotations with
# `get_type_hints` to build the CLI, so a name only present for the checker
# is a NameError at import. Ruff cannot see that and asks for TC003.
from pathlib import Path  # noqa: TC003
from queue import Empty, Queue
from typing import TYPE_CHECKING, Annotated

import typer

from ictus.assemble.announcements import apply_integrations
from ictus.cli.app import _fail, _only, app
from ictus.errors import IctusError
from ictus.graph.signals import RunSignal
from ictus.interfaces.conductor.control.events import history, step_outputs
from ictus.interfaces.conductor.control.events import watch as watch_run
from ictus.interfaces.conductor.control.live import LiveRun, live_runs
from ictus.notify import Delivered, deliver
from ictus.runspec.inputs import PipelineFolder

if TYPE_CHECKING:
    from ictus.graph.requirements import Integration
    from ictus.interfaces import SignalEvent


@dataclass(frozen=True, slots=True)
class _Finished:
    """A watched run stopped, for whatever reason."""

    run_id: str
    error: str = ""


def _follow(run: LiveRun, out: Queue[SignalEvent | _Finished]) -> None:
    """Relay one run's signals onto the queue, then say it is done.

    Runs on its own thread, so every failure is reported rather than raised.
    """
    try:
        for event in watch_run(run):
            out.put(event)
    except Exception as exc:
        # The thread boundary: anything not reported here is lost, and the
        # watcher waits forever on a run it has stopped following.
        out.put(_Finished(run.run_id, f"{type(exc).__name__}: {exc}"))
        return
    out.put(_Finished(run.run_id))


def _notifiers_of(folder: Path | None) -> tuple[str, tuple[Integration, ...], dict[str, str]]:
    """A pipeline folder's id, the integrations it declares, and their openers.

    Read from the source: which service a step reports to, and what it is
    subscribed to, exist only in ``pipeline.py``. An opener is the step whose
    output is a service's thread for a run.
    """
    if folder is None:
        return "", (), {}
    pipeline = _only(PipelineFolder.at(folder), attach=False)
    openers = {a.integration: a.opener for a in apply_integrations(pipeline) if a.opener}
    return pipeline.pipeline_id, pipeline.all_integrations(), openers


def _threads(run: LiveRun, openers: dict[str, str]) -> dict[str, str]:
    """Each integration's thread for ``run``, read from what its opener printed.

    Best effort: a report that cannot find its thread still goes to the channel.
    """
    if not openers:
        return {}
    try:
        events = history(run)
    except (OSError, ValueError, http.client.HTTPException):
        return {}
    found: dict[str, str] = {}
    for name, opener in openers.items():
        printed = step_outputs(events, opener)
        thread = printed[-1].get("thread") if printed else None
        if isinstance(thread, str) and thread:
            found[name] = thread
    return found


@app.command()
def watch(
    folder: Annotated[
        Path | None,
        typer.Argument(help="A pipeline folder, to also report to what it integrates"),
    ] = None,
    follow: Annotated[
        bool, typer.Option("--follow", "-f", help="Keep attaching to runs as they start")
    ] = False,
    poll_seconds: Annotated[
        float, typer.Option("--poll", help="How often to look for new runs, with --follow")
    ] = 2.0,
) -> None:
    """Report what live runs are doing, as signals.

    Attaches to every run serving a dashboard. Without ``--follow`` it exits
    once those runs end; with it, it keeps looking for new ones.

    Given a pipeline folder, it also reports to what that pipeline integrates,
    into each run's own thread — but only what no step inside the run already
    said: a step failing, a budget crossed, a run paused, the iteration limit
    reached, the engine dying. A destination whose variable is unset is
    reported once per signal rather than skipped.

    A dropped connection is dialled again while the run's process lives; a run
    whose process vanished is reported as failed.

    Detaches the moment a run ends: a detached run shuts down only once every
    client disconnects. While attached it is a client, so an agent paused from
    the dashboard stays paused.
    """
    try:
        workflow, targets, openers = _notifiers_of(folder)
    except IctusError as exc:
        _fail(str(exc))
        return
    if targets:
        typer.secho(
            f"reporting {workflow} to: {', '.join(t.name for t in targets)}",
            fg=typer.colors.CYAN,
        )

    events: Queue[SignalEvent | _Finished] = Queue()
    attached: set[str] = set()
    threads: list[threading.Thread] = []
    followed: dict[str, LiveRun] = {}
    conversations: dict[str, dict[str, str]] = {}

    def attach() -> int:
        started = 0
        for run in live_runs():
            if run.run_id in attached:
                continue
            attached.add(run.run_id)
            followed[run.run_id] = run
            thread = threading.Thread(target=_follow, args=(run, events), daemon=True)
            thread.start()
            threads.append(thread)
            started += 1
            typer.secho(
                f"watching {run.workflow} ({run.run_id}) on {run.dashboard}",
                fg=typer.colors.CYAN,
            )
        return started

    if attach() == 0 and not follow:
        typer.secho("no run is serving a dashboard", fg=typer.colors.YELLOW)
        typer.echo("Start one with `ictus run <folder>`, or pass --follow to wait for one.")
        return

    live = len(attached)
    try:
        while live or follow:
            try:
                item = events.get(timeout=poll_seconds)
            except Empty:
                live += attach()
                continue
            if isinstance(item, _Finished):
                live -= 1
                if item.error:
                    typer.secho(f"  {item.run_id}: {item.error}", fg=typer.colors.RED)
                else:
                    typer.secho(f"  {item.run_id}: detached", fg=typer.colors.BRIGHT_BLACK)
                continue
            _report_signal(item)
            if not (targets and item.workflow == workflow):
                continue
            run = followed[item.run_id]
            # Learnt on the first event, while the run can still be asked: by
            # the time it dies its dashboard and history are gone.
            if item.run_id not in conversations:
                found = _threads(run, openers)
                if len(found) == len(openers):
                    conversations[item.run_id] = found
            if not (item.at_a_step or item.replayed):
                threads_now = conversations.get(item.run_id) or _threads(run, openers)
                _report_delivery(
                    deliver(item, targets, dashboard=run.dashboard, threads=threads_now)
                )
    except KeyboardInterrupt:
        typer.secho("\nstopped watching; the runs are untouched", fg=typer.colors.BRIGHT_BLACK)


def _report_signal(event: SignalEvent) -> None:
    """One line per signal, with the detail that decides what to do about it."""
    colour = {
        RunSignal.DECISION_NEEDED: typer.colors.YELLOW,
        RunSignal.RUN_FAILED: typer.colors.RED,
        RunSignal.STEP_FAILED: typer.colors.RED,
        RunSignal.BUDGET_EXCEEDED: typer.colors.RED,
    }.get(event.signal, typer.colors.GREEN)
    when = "  (before attaching)" if event.replayed else ""
    detail = event.step or event.reason
    typer.secho(f"  {event.workflow}  {event.signal.value}  {detail}{when}", fg=colour)
    if event.signal is RunSignal.DECISION_NEEDED and event.options:
        typer.echo(f"      waiting on: {', '.join(event.options)}")


def _report_delivery(results: list[Delivered]) -> None:
    """Say what was reported, and say when it was not.

    Never fatal, always printed.
    """
    for result in results:
        if result.sent:
            typer.secho(f"      -> {result.integration}", fg=typer.colors.BRIGHT_BLACK)
        else:
            typer.secho(f"      -> {result.integration}: {result.detail}", fg=typer.colors.RED)
