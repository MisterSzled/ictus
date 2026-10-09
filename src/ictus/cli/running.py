"""``ictus run`` — compile a folder, check it, and launch what was compiled.

Re-emits by default, so what runs is what the source says rather than whatever
was committed, and preflights by default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ictus.cli.app import (
    BACKEND,
    _fail,
    _only,
    _policy,
    _prune,
    _refuse_problems,
    _report_preflight,
    _report_written,
    _write,
    app,
)
from ictus.errors import IctusError
from ictus.graph.ports import PortType
from ictus.interfaces.conductor.control.trace import find_logs, read_trace
from ictus.lint import lint_pipeline
from ictus.runspec.inputs import PipelineFolder, read_input_file


@app.command()
def run(
    folder: Annotated[Path, typer.Argument(help="The pipeline folder to run")] = Path(),
    input_file: Annotated[
        Path | None,
        typer.Option("--input-file", "-f", help="Use this instead of the folder's input.md"),
    ] = None,
    repo: Annotated[
        Path | None,
        typer.Option(help="Work in this directory instead of the current one"),
    ] = None,
    web: Annotated[
        bool, typer.Option(help="Serve the dashboard so gates can be answered remotely")
    ] = True,
    probe: Annotated[bool, typer.Option(help="Open each declared connection at preflight")] = True,
    workspace_instructions: Annotated[
        bool | None,
        typer.Option(
            "--workspace-instructions/--no-workspace-instructions",
            help="Read the target project's CLAUDE.md/AGENTS.md (default: config.yaml, else on)",
        ),
    ] = None,
    skip_preflight: Annotated[
        bool, typer.Option(help="Launch without checking the environment first")
    ] = False,
    reemit: Annotated[
        bool, typer.Option(help="Compile before running, so the YAML matches the source")
    ] = True,
    background: Annotated[
        bool,
        typer.Option(
            "--background/--foreground",
            "-b/-F",
            help="Detach and let the dashboard drive, rather than tying the run to this terminal",
        ),
    ] = True,
    inputs: Annotated[
        list[str] | None,
        typer.Option("--input", "-i", help="Override one input as name=value; repeatable"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the execution plan and stop, spending nothing"),
    ] = False,
    log_file: Annotated[
        str | None,
        typer.Option(
            "--log-file",
            "-l",
            help="Write full debug output here, or 'auto' for a generated temp file",
        ),
    ] = None,
) -> None:
    """Run a pipeline folder against a project.

    The work happens in the directory you invoked this from, so `cd` to a
    project and go. A `repo:` key in the input file, or `--repo`, overrides it.

    Detached by default. A foreground run holds the terminal — Conductor puts
    it in cbreak mode and answers gates there — and blocks the event loop that
    serves the dashboard.
    """
    if background and not web:
        _fail(
            "--background and --no-web cannot both hold: a detached run's gates are "
            "only answerable through the dashboard, so there would be no way to "
            "reach it. Drop one."
        )
    started_in = Path.cwd()
    try:
        target = PipelineFolder.at(folder)
    except IctusError as exc:
        _fail(str(exc))
        return
    pipeline = _only(target)
    # Before the inputs, and before `_write`: with `--reemit` on by default,
    # the committed artifact would otherwise be overwritten unchecked.
    _refuse_problems(
        lint_pipeline(pipeline, backend=BACKEND),
        consequence="nothing was compiled or launched",
    )

    spec = None
    source = input_file if input_file is not None else target.input_file
    if source.is_file():
        try:
            spec = read_input_file(source, pipeline, cwd=started_in)
        except IctusError as exc:
            _fail(str(exc))
    elif input_file is not None:
        _fail(f"{input_file} does not exist")

    supplied: dict[str, str] = dict(spec.inputs) if spec else {}
    for pair in inputs or []:
        name, sep, value = pair.partition("=")
        if not sep or not name:
            _fail(f"--input expects name=value, got {pair!r}")
        supplied[name] = value

    declared = {param.name: param for param in pipeline.workflow_inputs}
    unknown = sorted(set(supplied) - set(declared))
    if unknown:
        known = ", ".join(sorted(declared)) or "(none)"
        _fail(f"{unknown} are not inputs of {pipeline.pipeline_id!r}; declared: {known}")
    missing = sorted(n for n, d in declared.items() if d.required and n not in supplied)
    # A dry run substitutes nothing, so it does not need the values. A
    # misspelled one is still refused above.
    if missing and not dry_run:
        hint = f" Add them to {source}," if source.is_file() else f" Create {target.input_file},"
        _fail(f"{pipeline.pipeline_id!r} requires {missing}.{hint} or pass -i name=value.")

    working = (
        repo.expanduser().resolve()
        if repo is not None
        else (spec.working_dir if spec else started_in)
    )
    if not working.is_dir():
        _fail(f"{working} is not a directory")

    if reemit:
        try:
            written = _write(pipeline, target.build)
        except IctusError as exc:
            _fail(f"{pipeline.pipeline_id}: {exc}")
            raise
        # Only the moves: silence means `build/` already matched the source.
        _report_written(
            [item for item in written if item.changed],
            _prune(target.build, {item.path for item in written}),
        )
    path = target.build / f"{pipeline.pipeline_id}.yaml"
    if not path.is_file():
        _fail(f"{path} does not exist; run `ictus emit {folder}` first")

    if dry_run:
        # Ahead of preflight, which opens real connections.
        typer.secho(f"{pipeline.pipeline_id}: plan only, nothing runs", fg=typer.colors.CYAN)
        raise typer.Exit(code=BACKEND.plan(path, working_dir=working))

    if not skip_preflight:
        # The pipeline module, not the emitted file: requirements are
        # authored, and never carried into the artifact.
        issues = BACKEND.preflight(pipeline, probe=probe)
        blocking = [i for i in issues if i.blocking]
        if issues:
            _report_preflight(issues, probed=probe)
        if blocking:
            _fail(
                f"{len(blocking)} requirement(s) unmet; nothing was launched. "
                "Fix them, or pass --skip-preflight to launch anyway."
            )

    typer.secho(f"{pipeline.pipeline_id} in {working}", fg=typer.colors.CYAN)
    for name in sorted(supplied):
        preview = supplied[name].replace("\n", " ")
        typer.echo(f"  {name} = {preview[:70]}{'…' if len(preview) > 70 else ''}")

    # Re-read rather than threaded through `_load`; a flag beats the file.
    reads_project = (
        workspace_instructions
        if workspace_instructions is not None
        else _policy(target).workspace_instructions
    )
    if reads_project:
        typer.secho(
            "  reading the project's own instruction files (CLAUDE.md, AGENTS.md)",
            fg=typer.colors.BRIGHT_BLACK,
        )

    try:
        code = BACKEND.run(
            path,
            inputs=supplied,
            # What the pipeline declared a string stays the text that was
            # typed; the engine still coerces the rest, which is how an int
            # input gets an int.
            verbatim=tuple(
                name for name, port in declared.items() if port.port_type is PortType.STRING
            ),
            dashboard=web,
            background=background,
            workspace_instructions=reads_project,
            working_dir=working,
            log_file=log_file,
        )
    except FileNotFoundError as exc:
        _fail(str(exc))
        return
    _report_activity(pipeline.pipeline_id, folder, background=background)
    raise typer.Exit(code=code)


def _report_activity(workflow: str, folder: Path, *, background: bool) -> None:
    """Say which steps answered without looking at anything.

    A background run is still going, so it gets the `ictus trace` command
    instead of the answer, named by folder since that is what trace takes.
    """
    if background:
        typer.secho(f"\nwhen it finishes: ictus trace {folder}", fg=typer.colors.BRIGHT_BLACK)
        return
    found = find_logs(workflow)
    if not found:
        return
    try:
        seen = read_trace(found[0])
    except OSError:
        return
    idle = seen.incurious
    if idle:
        typer.secho(
            f"\n{len(idle)} step(s) answered without consulting anything: "
            f"{', '.join(s.name for s in idle)}",
            fg=typer.colors.YELLOW,
        )
        typer.echo(
            "Right for a step whose whole input is in its prompt, wrong for one asked "
            f"to assess something it was only shown a summary of. `ictus trace {workflow}` "
            "shows what each one opened."
        )
    stopped = seen.capped
    if stopped:
        typer.secho(
            f"\n{len(stopped)} step(s) hit the turn ceiling: "
            f"{', '.join(s.name for s in stopped)}. Give them max_turns.",
            fg=typer.colors.RED,
        )
