"""``ictus trace`` — what a finished run did, read back off its log.

Reads; cannot change a run. The other half of what used to be one module is
``watching.py``, which is about a run still going: the two share no helper, no
constant and no type, and have had their own test files all along.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from ictus.cli.app import _fail, _only, app
from ictus.errors import IctusError
from ictus.interfaces.conductor.control.trace import LOG_DIR, find_logs, read_trace
from ictus.runspec.inputs import PipelineFolder

if TYPE_CHECKING:
    from ictus.graph.pipeline import Pipeline


def _declared_ceilings(pipeline: Pipeline, _into: dict[str, int] | None = None) -> dict[str, int]:
    """Each step's own ``max_turns``, including those inside nested stages.

    A stage's steps appear in the parent run's log under their own names.
    """
    found = {} if _into is None else _into
    for node in pipeline.nodes:
        limit = getattr(node, "max_turns", None)
        if limit is not None:
            found[node.node_id] = limit
    for child in pipeline.children.values():
        _declared_ceilings(child, found)
    return found


@app.command()
def trace(
    folder: Annotated[
        Path | None,
        typer.Argument(help="A pipeline folder, to trace its most recent run"),
    ] = None,
    log: Annotated[Path | None, typer.Option("--log", help="Read this event log instead")] = None,
    files: Annotated[bool, typer.Option(help="List what each step opened")] = False,
) -> None:
    """Show what each step of the last run actually did.

    Reads back the tool calls the engine recorded, which a step's own output
    does not show.
    """
    ceilings: dict[str, int] = {}
    if log is not None:
        path = log
    else:
        try:
            located = PipelineFolder.at(folder or Path())
        except IctusError as exc:
            # This takes a folder, and a workflow name is the natural guess.
            _fail(f"{exc}. This takes a pipeline folder, not a workflow name.")
            return
        pipeline = _only(located)
        ceilings = _declared_ceilings(pipeline)
        found = find_logs(pipeline.pipeline_id)
        if not found:
            _fail(
                f"no run of {pipeline.pipeline_id!r} found under {LOG_DIR}. "
                "Runs write an event log as they go; this one may not have started."
            )
        path = found[0]
    if not path.is_file():
        _fail(f"{path} does not exist")

    seen = read_trace(path, ceilings=ceilings)
    typer.secho(f"{seen.workflow}  {path.name}", fg=typer.colors.CYAN)
    if not seen.steps:
        typer.echo("no steps recorded yet")
        return

    header = f"  {'step':<20} {'turns':>5} {'looked':>7} {'tokens':>8} {'cost':>8}  tools"
    typer.secho(header, fg=typer.colors.BRIGHT_BLACK)
    for step in seen.steps.values():
        used = ", ".join(f"{t}x{n}" for t, n in step.tools.most_common()) or "—"
        colour = typer.colors.YELLOW if not step.looked and step.turns else None
        typer.secho(
            f"  {step.name:<20} {step.turns:>5} {step.investigated:>7} "
            f"{step.tokens:>8} {step.cost_usd:>8.4f}  {used}",
            fg=colour,
        )
        if files and step.reads:
            for target in dict.fromkeys(step.reads):
                typer.secho(f"      {target}", fg=typer.colors.BRIGHT_BLACK)

    stopped = seen.capped
    if stopped:
        typer.secho(
            f"\n{len(stopped)} step(s) hit the turn ceiling: {', '.join(s.name for s in stopped)}",
            fg=typer.colors.RED,
        )
        typer.echo(
            "A step that runs out of turns does not return what it had — the provider "
            "raises, and that error is not one a scope can turn into an outcome, so it "
            "fails the whole run. Give it max_turns."
        )

    idle = seen.incurious
    if idle:
        typer.secho(
            f"\n{len(idle)} step(s) answered without consulting anything: "
            f"{', '.join(s.name for s in idle)}",
            fg=typer.colors.YELLOW,
        )
        typer.echo(
            "That is right for a step whose whole input is in its prompt, and wrong "
            "for one asked to assess something it was only shown a summary of."
        )
