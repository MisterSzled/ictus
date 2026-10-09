"""Everything you do to a pipeline before anything runs.

    emit        compile the folders to Conductor YAML
    lint        the composition rules, writing nothing
    preflight   can this machine supply what the pipelines declare
    validate    Conductor's own loader, on what was emitted
    init        write the files a new folder needs

None of them spends money or outlives the command.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from ictus.cli.app import (
    BACKEND,
    _fail,
    _folders,
    _load,
    _prune,
    _refuse_problems,
    _report_preflight,
    _report_written,
    _write,
    _Written,
    app,
)
from ictus.errors import IctusError
from ictus.lint import lint_pipeline
from ictus.runspec.config import CONFIG_FILE, MINIMAL
from ictus.runspec.inputs import PipelineFolder
from ictus.runspec.scaffold import STARTER_INPUT, STARTER_PIPELINE

if TYPE_CHECKING:
    from ictus.interfaces import PreflightIssue


@app.command()
def emit(
    where: Annotated[Path, typer.Argument(help="A pipeline folder, or a directory of them")] = Path(
        "pipelines"
    ),
    out: Annotated[
        Path | None,
        typer.Option(help="Write every workflow here instead of each folder's build/"),
    ] = None,
    prune: Annotated[
        bool, typer.Option(help="Delete YAML in the output that no pipeline claims")
    ] = True,
) -> None:
    """Compile pipeline folders to Conductor YAML.

    Each folder's YAML goes to its own ``build/`` — committed, so a diff shows
    what changed in what actually runs.
    """
    folders = _folders(where)
    # Loaded once: a module is free to build a different graph each time.
    targets = [(out if out is not None else f.build, _load(f)) for f in folders]
    pipelines = [p for _, group in targets for p in group]
    if not pipelines:
        _fail(f"no pipelines found in {where} (nothing was emitted)")

    # Keyed by where a file would land, not by its name: two folders may both
    # place `read.yaml` in their own `build/`. Only `--out` makes names collide.
    seen: dict[tuple[Path, str], str] = {}
    problems: list[str] = []
    for destination, group in targets:
        for pipeline in group:
            for document in BACKEND.compile(pipeline):
                where_it_lands = (destination, document.filename)
                owner = seen.get(where_it_lands)
                if owner is not None:
                    problems.append(
                        f"{destination / document.filename} is claimed by both {owner!r} "
                        f"and {pipeline.pipeline_id!r}"
                    )
                seen[where_it_lands] = pipeline.pipeline_id
            problems.extend(lint_pipeline(pipeline, backend=BACKEND))
    _refuse_problems(problems, consequence="nothing was written")

    written: list[_Written] = []
    for destination, group in targets:
        for pipeline in group:
            try:
                written.extend(_write(pipeline, destination))
            except IctusError as exc:
                _fail(f"{pipeline.pipeline_id}: {exc}")

    pruned: list[Path] = []
    if prune:
        keep = {item.path for item in written}
        for destination in dict.fromkeys(d for d, _ in targets):
            pruned.extend(_prune(destination, keep))

    _report_written(written, pruned)
    # Manifests are not workflows.
    workflows = sum(1 for item in written if item.path.suffix == ".yaml")
    listening = len(written) - workflows
    tail = f", {listening} listening" if listening else ""
    typer.secho(
        f"{workflows} workflow(s) from {len(pipelines)} pipeline(s){tail}", fg=typer.colors.GREEN
    )


@app.command()
def lint(
    where: Annotated[Path, typer.Argument(help="A pipeline folder, or a directory of them")] = Path(
        "pipelines"
    ),
) -> None:
    """Run the composition lints without writing anything."""
    pipelines = [p for folder in _folders(where) for p in _load(folder, require_config=False)]
    if not pipelines:
        _fail(f"no pipelines found in {where}")
    problems = [p for pipeline in pipelines for p in lint_pipeline(pipeline, backend=BACKEND)]
    if problems:
        for problem in problems:
            typer.secho(f"  - {problem}", fg=typer.colors.RED, err=True)
        _fail(f"{len(problems)} composition problem(s)")
    typer.secho(f"{len(pipelines)} pipeline(s) clean", fg=typer.colors.GREEN)


@app.command()
def preflight(
    where: Annotated[Path, typer.Argument(help="A pipeline folder, or a directory of them")] = Path(
        "pipelines"
    ),
    probe: Annotated[
        bool, typer.Option(help="Open each declared connection, not just check it is configured")
    ] = True,
) -> None:
    """Check this environment can supply what the pipelines declare.

    Separate from `validate`, which only asks whether the YAML loads.
    """
    pipelines = [p for folder in _folders(where) for p in _load(folder)]
    if not pipelines:
        _fail(f"no pipelines found in {where}")
    issues: list[PreflightIssue] = []
    for pipeline in pipelines:
        declared = pipeline.all_mcp_servers()
        commands = pipeline.all_executables()
        reporting = pipeline.all_integrations()
        reading = pipeline.all_datasources()
        total = len(declared) + len(commands) + len(reporting) + len(reading)
        typer.echo(f"{pipeline.pipeline_id}: {total} requirement(s) declared")
        for server in declared:
            typer.echo(f"  - mcp:{server.name} — {server.purpose}")
        for tool in commands:
            typer.echo(f"  - exe:{tool.name} — {tool.purpose}")
        for service in reporting:
            typer.echo(f"  - integrate:{service.name} — {service.purpose}")
        for source in reading:
            access = "read-only" if source.read_only else "WRITABLE"
            typer.echo(f"  - read:{source.name} ({access}) — {source.purpose}")
        issues.extend(BACKEND.preflight(pipeline, probe=probe))
    _report_preflight(issues, probed=probe)
    if any(i.blocking for i in issues):
        _fail(f"{sum(1 for i in issues if i.blocking)} blocking requirement(s) unmet")


@app.command()
def validate(
    where: Annotated[
        Path,
        typer.Argument(help="A pipeline folder, a directory of them, or a directory of YAML"),
    ] = Path("pipelines"),
) -> None:
    """Check emitted YAML with Conductor's own validator."""
    # Pipeline folders first: a folder also holds `config.yaml`, which is not
    # a workflow and which Conductor's loader rejects.
    try:
        found = PipelineFolder.find(where)
    except IctusError:
        found = []
    if found:
        files = sorted(f for folder in found for f in folder.build.glob("*.yaml"))
    elif where.is_dir():
        files = sorted(where.glob("*.yaml"))
    else:
        _fail(f"{where} is not a directory")
        return
    if not files:
        _fail(f"no compiled output under {where}; run `ictus emit` first")
    try:
        results = BACKEND.validate(files)
    except FileNotFoundError as exc:
        _fail(str(exc))
        return
    failed = [r for r in results if not r.ok]
    for result in results:
        if result.ok:
            typer.secho(f"ok  {result.path}", fg=typer.colors.GREEN)
        else:
            typer.secho(f"BAD {result.path}", fg=typer.colors.RED, err=True)
            typer.echo(result.detail, err=True)
    if failed:
        name = BACKEND.capabilities().name
        _fail(f"{len(failed)} of {len(results)} workflow(s) rejected by {name}")


@app.command()
def init(
    folder: Annotated[Path, typer.Argument(help="The pipeline folder to scaffold")],
) -> None:
    """Create the files a pipeline folder needs. Nothing existing is touched."""
    folder.mkdir(parents=True, exist_ok=True)
    for name, body in (
        (CONFIG_FILE, MINIMAL),
        ("input.md", STARTER_INPUT),
        ("pipeline.py", STARTER_PIPELINE),
    ):
        target = folder / name
        if target.exists():
            typer.echo(f"kept  {target}")
            continue
        target.write_text(body, encoding="utf-8")
        typer.secho(f"wrote {target}", fg=typer.colors.GREEN)
    # Stops short of naming `ictus run`: on an untouched folder that starts a
    # billable process against a placeholder.
    typer.echo(
        f"\nnext:\n"
        f"  1. {folder / 'pipeline.py'} — replace CHANGE-ME with a name, "
        f"then say what the step does\n"
        f"  2. {folder / 'input.md'} — the values to run it on\n"
        f"  3. ictus lint {folder}\n"
    )
