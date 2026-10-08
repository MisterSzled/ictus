"""The ``ictus`` command line."""

from __future__ import annotations

import http.client
import importlib.util
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import TYPE_CHECKING, Annotated, Literal

import typer

from ictus.answer import resolve, submit
from ictus.config import CONFIG_FILE, MINIMAL, PipelineConfig, read_config
from ictus.errors import IctusError
from ictus.gate import add_start_gate, attach_start_herald
from ictus.graph.pipeline import Pipeline
from ictus.graph.signals import RunSignal
from ictus.integrate import apply_integrations
from ictus.interfaces.conductor import conductor
from ictus.interfaces.conductor.events import history, step_outputs
from ictus.interfaces.conductor.events import watch as watch_run
from ictus.interfaces.conductor.manifest import SUFFIX as MANIFEST_SUFFIX
from ictus.interfaces.conductor.runs import LiveRun, live_runs
from ictus.interfaces.conductor.trace import LOG_DIR, find_logs, read_trace
from ictus.lint import lint_pipeline
from ictus.notify import Delivered, deliver
from ictus.notify.slack.listen import (
    Click,
    Note,
    SlackError,
    open_form,
    presses,
    retire,
    say,
    verdict,
)
from ictus.notify.slack.send import reply
from ictus.notify.slack.trigger import Asked, start, triggers_in
from ictus.runspec import PipelineFolder, read_input_file
from ictus.scaffold import STARTER_INPUT, STARTER_PIPELINE

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ictus.graph.requirements import Integration
    from ictus.interfaces import PreflightIssue, SignalEvent

app = typer.Typer(
    help="Typed composition for Conductor workflows.",
    no_args_is_help=True,
    add_completion=False,
    # Typer prints every local of every frame on an uncaught exception, which
    # in `listen` and `watch` means the Slack tokens, whole.
    pretty_exceptions_show_locals=False,
)


def _fail(message: str) -> None:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _folders(path: Path) -> list[PipelineFolder]:
    """The pipeline folders at or under ``path``."""
    try:
        return PipelineFolder.find(path)
    except IctusError as exc:
        _fail(str(exc))
        return []


def _load_module(module_path: Path) -> list[Pipeline]:
    """Import one module and collect the top-level pipelines it defines.

    A pipeline that is the body of a stage is dropped: it is emitted as part of
    its parent, and emitting it again at top level would produce a second file
    claiming the same name.
    """
    spec = importlib.util.spec_from_file_location(module_path.stem, module_path)
    if spec is None or spec.loader is None:
        _fail(f"could not load {module_path}")
        return []
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except IctusError as exc:
        _fail(f"{module_path}: {exc}")
    except Exception as exc:
        _fail(f"{module_path}: {type(exc).__name__}: {exc}")

    found: list[Pipeline] = []
    for name in sorted(dir(module)):
        value = getattr(module, name)
        if isinstance(value, Pipeline) and not any(value is p for p in found):
            found.append(value)
    nested = {id(child) for p in found for child in _descendants(p)}
    return [p for p in found if id(p) not in nested]


def _load(
    folder: PipelineFolder, *, require_config: bool = True, attach: bool = True
) -> list[Pipeline]:
    """The pipelines a folder defines, with its run policy applied.

    Policy and the start gate are applied here rather than in the composition so
    every command sees the same thing: a lint reading a different provider from
    the emit would be checking a workflow nobody runs.

    ``attach=False`` leaves integrations for the caller to apply, for one that
    needs to know what attaching them inserted.

    ``require_config`` is what ``lint`` relaxes. A folder with no ``config.yaml``
    used to fail before a single composition rule ran, so the one command whose
    whole job is to find problems in a graph reported exactly one problem and it
    was about a file. The graph is still worth checking; what the run would do
    with it is not yet decided. A malformed config still fails either way — that
    is an error to fix, not a decision left open.
    """
    pipelines = _load_module(folder.module)
    if not require_config and not folder.config_file.is_file():
        typer.secho(
            f"warning: {folder.config_file} does not exist, so the run policy is unknown. "
            "Checking the graph only — the provider and the start gate are not applied, "
            "and `ictus emit` will still refuse this folder.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        try:
            for pipeline in pipelines:
                if attach:
                    apply_integrations(pipeline)
        except IctusError as exc:
            _fail(str(exc))
        return pipelines
    try:
        settings = read_config(folder.config_file)
        _check_provider(settings.provider, where=str(folder.config_file))
        for pipeline in pipelines:
            settings.apply(pipeline, where=str(folder.config_file))
            if settings.start_gate:
                add_start_gate(pipeline)
            else:
                attach_start_herald(pipeline)
            # After the start policy: the start gate is the first gate every run
            # stops at, so it is announced like the others, and its prompt counts
            # the work being approved rather than the reporting about it.
            if attach:
                apply_integrations(pipeline)
    except IctusError as exc:
        _fail(str(exc))
    return pipelines


def _check_provider(name: str, *, where: str) -> None:
    """Refuse a provider the backend cannot use, where it was written."""
    known = BACKEND.capabilities().providers
    if known and name not in known:
        _fail(
            f"{where}: {name!r} is not a provider {BACKEND.capabilities().name} can use; "
            f"choose one of {sorted(known)}"
        )


def _policy(folder: PipelineFolder) -> PipelineConfig:
    """A folder's run policy, on its own."""
    try:
        return read_config(folder.config_file)
    except IctusError as exc:
        _fail(str(exc))
        raise


def _only(folder: PipelineFolder, *, attach: bool = True) -> Pipeline:
    """The single pipeline a folder defines, which a run needs."""
    pipelines = _load(folder, attach=attach)
    if not pipelines:
        _fail(f"{folder.module} defines no pipeline")
    if len(pipelines) > 1:
        names = ", ".join(sorted(p.pipeline_id for p in pipelines))
        _fail(
            f"{folder.module} defines more than one top-level pipeline ({names}); "
            "a pipeline folder runs exactly one"
        )
    return pipelines[0]


def _descendants(pipeline: Pipeline) -> list[Pipeline]:
    out: list[Pipeline] = []
    for child in pipeline.children.values():
        out.append(child)
        out.extend(_descendants(child))
    return out


# One line to change when a second backend exists; nothing else in the CLI
# names an engine.
BACKEND = conductor


@dataclass(frozen=True, slots=True)
class _Written:
    """One emitted file, and what writing it did to the bytes already there.

    ``build/`` is committed, so "did this write move the artifact" is the thing
    a caller needs to report. A compile that produces identical bytes is not a
    change, and saying "emitted" for it hides the writes that are.
    """

    path: Path
    status: Literal["wrote", "updated", "same"]

    @property
    def changed(self) -> bool:
        return self.status != "same"


def _write(pipeline: Pipeline, out: Path) -> list[_Written]:
    """Compile and write every document a pipeline produces.

    Each file goes to a temporary sibling and is renamed, so a failure part way
    through cannot leave a truncated document that still parses.
    """
    out.mkdir(parents=True, exist_ok=True)
    written: list[_Written] = []
    for document in BACKEND.compile(pipeline):
        target = out / document.filename
        before = target.read_text(encoding="utf-8") if target.is_file() else None
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(document.content, encoding="utf-8")
        tmp.replace(target)
        status: Literal["wrote", "updated", "same"] = (
            "wrote" if before is None else "same" if before == document.content else "updated"
        )
        written.append(_Written(target, status))
    return written


def _prune(destination: Path, keep: set[Path]) -> list[Path]:
    """Delete compiled output in ``destination`` that no pipeline claims.

    Manifests as well as workflows: deleting a ``listen_on`` line has to stop
    the listener starting that pipeline, and a stale manifest left behind would
    go on matching messages against a prefix nothing declares any more.
    """
    if not destination.is_dir():
        return []
    compiled = set(destination.glob("*.yaml")) | set(destination.glob(f"*{MANIFEST_SUFFIX}"))
    stale = sorted(compiled - keep)
    for path in stale:
        path.unlink()
    return stale


def _report_written(written: Sequence[_Written], pruned: Sequence[Path]) -> None:
    """Say which committed files moved, so the write is auditable without a diff."""
    for path in pruned:
        typer.secho(f"pruned  {path}", fg=typer.colors.YELLOW)
    for item in sorted(written, key=lambda w: w.path):
        colour = typer.colors.YELLOW if item.changed else None
        typer.secho(f"{item.status:<7} {item.path}", fg=colour)


def _refuse_problems(problems: list[str], *, consequence: str) -> None:
    """Report composition problems and stop, naming what did not happen."""
    if not problems:
        return
    for problem in problems:
        typer.secho(f"  - {problem}", fg=typer.colors.RED, err=True)
    _fail(f"{len(problems)} composition problem(s); {consequence}")


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
    # Loaded once: emitting objects re-loaded after the lint would write a graph
    # nothing checked, because a module is free to build a different one.
    targets = [(out if out is not None else f.build, _load(f)) for f in folders]
    pipelines = [p for _, group in targets for p in group]
    if not pipelines:
        _fail(f"no pipelines found in {where} (nothing was emitted)")

    # Keyed by where a file would land, not by its name. Two pipelines in their
    # own folders may both place `read.yaml` in their own `build/`, and that is
    # the ordinary consequence of reusing a stage — refusing it would make a
    # stdlib stage usable in one pipeline per repository. Only `--out`, which
    # gathers everything into one directory, can make two names actually
    # collide, and there the clash is real.
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
    # Manifests are not workflows, and counting them as such reads as a stage
    # nobody wrote.
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


def _report_preflight(issues: list[PreflightIssue], *, probed: bool) -> None:
    """Print what the environment is missing, and what to do about it."""
    for issue in issues:
        marker = "BLOCK" if issue.blocking else "warn "
        colour = typer.colors.RED if issue.blocking else typer.colors.YELLOW
        typer.secho(f"{marker} {issue.requirement}: {issue.problem}", fg=colour, err=True)
        typer.secho(f"      fix: {issue.remedy}", fg=typer.colors.CYAN, err=True)
    if not issues:
        depth = "checked and probed" if probed else "checked (offline only)"
        typer.secho(f"preflight clean — requirements {depth}", fg=typer.colors.GREEN)


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

    Separate from `validate`: a workflow can be perfectly well-formed and still
    be unrunnable here because a server is not installed or a token is unset.
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
    # Pipeline folders first. A folder holds `config.yaml`, which is not a
    # workflow — globbing *.yaml here handed it to Conductor's loader, which
    # rejected it for the entirely correct reason that it has no `agents:`.
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
    project and go. A `repo:` key in the input file, or `--repo`, overrides that
    — a run that touches a checkout can then say which one in something you can
    commit.

    Detached by default. A foreground run holds the terminal: Conductor puts it
    in cbreak mode for its interrupt listener and answers gates there, and
    anything that blocks on it blocks the same event loop that serves the
    dashboard — so the browser freezes on whatever it last saw and the run looks
    hung when it is waiting for a keystroke nobody is watching. Detached, the
    dashboard is the only place anything is answered, which is the one place you
    are looking.
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
    # Before the inputs, because a broken graph is a source defect and saying so
    # first is more use than asking for values that will not be spent. Before
    # `_write` and the launch, because `--reemit` is on by default: without this
    # the committed artifact is overwritten with output nothing checked, and then
    # run.
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
    # A dry run substitutes nothing, so demanding values it will never spend
    # would put the plan behind the very inputs you are reading it to decide.
    # A *misspelled* one is still refused above: that is a defect either way.
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
        # Only the moves: a reemit that says nothing has confirmed `build/` was
        # already what the source compiles to, which is the common case and the
        # one worth being quiet about.
        _report_written(
            [item for item in written if item.changed],
            _prune(target.build, {item.path for item in written}),
        )
    path = target.build / f"{pipeline.pipeline_id}.yaml"
    if not path.is_file():
        _fail(f"{path} does not exist; run `ictus emit {folder}` first")

    if dry_run:
        # Ahead of preflight, which opens real connections: a plan that spends
        # nothing should not need a live environment to print.
        typer.secho(f"{pipeline.pipeline_id}: plan only, nothing runs", fg=typer.colors.CYAN)
        raise typer.Exit(code=BACKEND.plan(path, working_dir=working))

    if not skip_preflight:
        # Preflight reads the pipeline module, not the emitted file: the
        # requirements are a property of what was authored, and deliberately do
        # not carry secrets into the compiled artifact.
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

    # Re-read rather than thread it through `_load`: the policy is a property of
    # the folder, and a flag on the command line beats the file.
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

    A run's exit code says whether it finished, not whether it thought. The
    engine already records every tool call; not reading them back is how a
    council shipped a report whose findings nobody had checked. A background run
    is still going, so it gets the command instead of the answer — naming the
    folder, because that is what `ictus trace` takes and the workflow's own
    name is the thing somebody would otherwise type.
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


@app.command()
def init(
    folder: Annotated[Path, typer.Argument(help="The pipeline folder to scaffold")],
) -> None:
    """Create the files a pipeline folder needs.

    Nothing that already exists is touched: running this on a folder someone has
    started is how you add the file you forgot, not a way to lose work.
    """
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
    # Deliberately stops short of `ictus run`. The old closing line named it, and
    # following the tool's own instruction on an untouched folder starts a real,
    # billable Conductor process against a placeholder.
    typer.echo(
        f"\nnext:\n"
        f"  1. {folder / 'pipeline.py'} — replace CHANGE-ME with a name, "
        f"then say what the step does\n"
        f"  2. {folder / 'input.md'} — the values to run it on\n"
        f"  3. ictus lint {folder}\n"
    )


def _declared_ceilings(pipeline: Pipeline, _into: dict[str, int] | None = None) -> dict[str, int]:
    """Each step's own ``max_turns``, including those inside nested stages.

    A stage compiles to its own workflow file but its steps appear in the parent
    run's event log under their own names, so a trace of the whole run needs
    every level's limits or it measures a stage's steps against the default.
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

    A step's output says what it concluded. It does not say whether it looked at
    anything first, and those are different runs that read identically. The
    engine records every tool call already; this reads them back.
    """
    ceilings: dict[str, int] = {}
    if log is not None:
        path = log
    else:
        try:
            located = PipelineFolder.at(folder or Path())
        except IctusError as exc:
            # `ictus trace asked` is the natural thing to type and it takes a
            # folder, so the wrong guess deserves a line rather than a traceback.
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


@dataclass(frozen=True, slots=True)
class _Finished:
    """A watched run stopped, for whatever reason."""

    run_id: str
    error: str = ""


def _follow(run: LiveRun, out: Queue[SignalEvent | _Finished]) -> None:
    """Relay one run's signals onto the queue, then say it is done.

    Every failure is reported rather than raised: this runs on its own thread,
    and one unreachable run must not take the watcher down with it.
    """
    try:
        for event in watch_run(run):
            out.put(event)
    except Exception as exc:
        # The thread boundary. Anything not reported here is lost with the
        # thread, and the watcher would wait forever on a run it has stopped
        # following: a truncated read of the run's history did exactly that.
        out.put(_Finished(run.run_id, f"{type(exc).__name__}: {exc}"))
        return
    out.put(_Finished(run.run_id))


def _notifiers_of(folder: Path | None) -> tuple[str, tuple[Integration, ...], dict[str, str]]:
    """A pipeline folder's id, the integrations it declares, and their openers.

    Read from the source rather than from the emitted workflow: the steps an
    integration inserts are in the YAML, but which service they report to, and
    what it is subscribed to, exist only in `pipeline.py`. The openers are the
    steps whose output is each service's thread for a run, so a report from
    outside can land in it.
    """
    if folder is None:
        return "", (), {}
    pipeline = _only(PipelineFolder.at(folder), attach=False)
    openers = {a.integration: a.opener for a in apply_integrations(pipeline) if a.opener}
    return pipeline.pipeline_id, pipeline.all_integrations(), openers


def _threads(run: LiveRun, openers: dict[str, str]) -> dict[str, str]:
    """Each integration's thread for ``run``, read from what its opener printed.

    Best effort: a report that cannot find its thread still goes, to the channel.
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

    Attaches to every run currently serving a dashboard and prints each moment
    worth reporting. Without `--follow` it exits once the runs it found have
    ended; with it, it keeps looking for new ones until interrupted.

    Given a pipeline folder, it also reports to what that pipeline integrates,
    for runs of that workflow, into each run's own thread — but only what no step
    inside the run already said: a step failing, a budget crossed, a run paused,
    the iteration limit reached, the engine dying. Gates and endings are
    announced from inside the graph, and what happened before it attached is not
    news. A destination whose variable is unset is reported once per signal
    rather than silently skipped — one nobody can tell is not firing is the
    failure the declaration exists to prevent.

    A dropped connection is dialled again while the run's process lives; a run
    whose process vanished without saying so is reported as failed.

    It detaches the moment a run ends, and that is not tidiness: a detached run
    shuts itself down only once every client has disconnected, so a watcher that
    held on would leave one resident process per run, each keeping that run's
    whole event history in memory. While attached it is a client, so an agent
    paused from the dashboard stays paused until somebody resumes it.
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
            # Learnt on the first event, while the run can still be asked: the
            # report most worth threading is the engine dying, and by then its
            # dashboard is gone and so is the history the thread is read from.
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

    A failure here is never fatal — the run is unaffected by whether anyone was
    told about it — but it is always printed, because a destination that quietly
    stops working is indistinguishable from a quiet week.
    """
    for result in results:
        if result.sent:
            typer.secho(f"      -> {result.integration}", fg=typer.colors.BRIGHT_BLACK)
        else:
            typer.secho(f"      -> {result.integration}: {result.detail}", fg=typer.colors.RED)


APP_TOKEN_ENV = "SLACK_APP_TOKEN"
#: Needed to reply under the question, to ask for a choice's text, and to take
#: the buttons off a question once it is answered.
BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"

#: Presses handled at once. The socket thread only acknowledges and hands on,
#: so one slow run cannot hold up the acknowledgement of the next press.
LISTEN_WORKERS = 4


@app.command()
def listen(
    where: Annotated[
        Path | None,
        typer.Argument(help="A built pipeline folder, or a directory of them, to start runs from"),
    ] = None,
    allow: Annotated[
        list[str] | None,
        typer.Option("--allow", help="Slack user id that may answer; repeatable"),
    ] = None,
) -> None:
    """Answer gates from Slack, by listening for button presses.

    Opens a websocket outward to Slack, so nothing here has to be publicly
    reachable. Reads the app-level token from $SLACK_APP_TOKEN and the bot token
    from $SLACK_BOT_TOKEN.

    A press is answered on the run that posted the button, and only if that
    message is the newest time the question was asked — a button from an
    earlier round of a loop is refused rather than applied to the current one.
    A choice that asks for text opens a form for it. What happened is posted
    back where the button was, including when nothing happened, because a button
    that silently does nothing is worse than no button; an answered question
    loses its buttons.

    The connection is redialled whenever it drops. Only Slack refusing the
    token stops it.

    Without `--allow`, anyone who can see the button may answer. That is right
    for a channel people were invited to and wrong for a deploy; there is no
    middle setting, because who may approve something is a decision to make
    rather than inherit.
    """
    token = os.environ.get(APP_TOKEN_ENV)
    bot = os.environ.get(BOT_TOKEN_ENV, "")
    if not token:
        _fail(
            f"${APP_TOKEN_ENV} is not set. Enable Socket Mode on the Slack app, generate "
            "an app-level token with connections:write, and export it."
        )
        return
    if not bot:
        _fail(
            f"${BOT_TOKEN_ENV} is not set. Without it a choice that needs text cannot ask "
            "for it, nothing can be said in the thread, and an answered question keeps "
            "its buttons. Export the same bot token the pipeline posts with."
        )
        return
    permitted = frozenset(allow or ())
    watching = triggers_in(where) if where is not None else []
    if where is not None and not watching:
        _fail(
            f"no manifests under {where}. A pipeline is startable from a channel once it "
            "declares `listen_on(...)` and has been emitted; without that this would "
            "listen for a prefix nothing claims."
        )
    typer.secho(
        "listening for button presses"
        + (f"; only {', '.join(sorted(permitted))} may answer" if permitted else ""),
        fg=typer.colors.CYAN,
    )
    for trigger in watching:
        typer.secho(
            f'starting {trigger.pipeline or trigger.workflow.name} on "{trigger.prefix} ..."',
            fg=typer.colors.CYAN,
        )
        # At startup, not at the first message: a listener that looks healthy
        # for a week and then says in public that it cannot start the thing
        # somebody just asked for is the failure this is here to prevent.
        for gap in trigger.missing():
            typer.secho(f"  warn  {gap}", fg=typer.colors.YELLOW)
    seen: set[str] = set()
    with ThreadPoolExecutor(max_workers=LISTEN_WORKERS) as pool:
        try:
            for event in presses(token, triggers=watching):
                if isinstance(event, Asked):
                    # Slack redelivers what it thinks was not acknowledged, and a
                    # redelivery reads exactly like somebody asking twice.
                    if event.thread in seen or event.trigger is None:
                        continue
                    seen.add(event.thread)
                    pool.submit(_handle_ask, event, bot)
                    continue
                pool.submit(_handle_press, event, permitted, bot)
        except KeyboardInterrupt:
            typer.secho("\nstopped listening; the runs are untouched", fg=typer.colors.BRIGHT_BLACK)
        except SlackError as exc:
            _fail(str(exc))


def _handle_ask(request: Asked, bot: str) -> None:
    """Start a run for one request, and say in its thread what became of it.

    The acknowledgement is the point. Starting a run takes long enough that
    silence reads as a bot that is not listening, and a refusal — preflight, a
    missing credential — is something the person who asked can act on.
    """
    typer.echo(f"  ask from {request.who}: {request.question[:60]}")
    if request.trigger is None:  # pragma: no cover - the caller already checked
        return
    started = start(request, request.trigger)
    line = (
        f"Working on it — <@{request.who}> asked about *{request.question[:120]}*"
        if started.ok
        else f"Could not start: {started.why}"
    )
    if started.dashboard:
        # The only moment anybody can learn it: the port is assigned when the
        # run binds, and the run outlives the command that printed it.
        line += f"\n{started.dashboard}"
    typer.secho(
        f"    -> {line.splitlines()[0]}",
        fg=typer.colors.BRIGHT_BLACK if started.ok else typer.colors.RED,
    )
    if started.dashboard:
        typer.secho(f"    -> {started.dashboard}", fg=typer.colors.CYAN)
    said = reply(token=bot, channel=request.channel, thread_ts=request.thread, text=line)
    if said:
        typer.secho(f"    -> could not say so in the thread: {said}", fg=typer.colors.RED)


def _handle_press(event: Click | Note, permitted: frozenset[str], bot: str) -> None:
    """Answer one press or one submitted form, and say what became of it.

    Every failure is printed rather than raised: this runs on a worker thread,
    where an exception would vanish with nobody told — the press already
    acknowledged, and the thread under the question silent.
    """
    click = event.click if isinstance(event, Note) else event
    try:
        if isinstance(event, Note):
            outcome = submit(event, allowed=permitted)
        else:
            outcome = resolve(event, allowed=permitted)
            if outcome.needs_note:
                why = open_form(bot, event)
                if why:
                    say(
                        event,
                        verdict(
                            event, answered=False, reason=f"could not ask for {event.ask}: {why}"
                        ),
                        token=bot,
                    )
                return
        line = verdict(
            click, answered=outcome.answered, reason=outcome.reason, run_id=outcome.run_id
        )
        typer.echo(f"  {outcome.run_id or '-'} {click.gate}={click.choice} -> {line}")
        say(click, line, token=bot)
        if outcome.answered:
            why = retire(bot, click, line)
            if why:
                typer.secho(f"  could not take the buttons off: {why}", fg=typer.colors.YELLOW)
    except Exception as exc:
        typer.secho(
            f"  {click.gate}={click.choice}: {type(exc).__name__} while answering: {exc}",
            fg=typer.colors.RED,
            err=True,
        )


# Last in the file, and it must stay last. Under `python -m ictus.cli` this
# guard is true and the module stops executing here, so a command decorated
# below it is never registered — while the console script, which imports the
# module rather than running it, registers everything. `trace` sat below this
# for a release: `ictus trace` worked, `python -m ictus.cli trace` said no such
# command, and the docs looked wrong. `test_every_command_is_reachable` is what
# keeps it honest.
if __name__ == "__main__":
    app()
