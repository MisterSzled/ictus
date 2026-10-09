"""The Typer object, and what every command group needs to reach a pipeline.

A folder's ``config.yaml`` decides the provider, whether a start gate goes in
front, and which integrations get attached — all at load, so what is emitted
is what runs.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import typer

from ictus.assemble.announcements import apply_integrations
from ictus.assemble.start_gate import add_start_gate, attach_start_herald
from ictus.errors import IctusError
from ictus.graph.pipeline import Pipeline
from ictus.interfaces.conductor import conductor
from ictus.interfaces.conductor.emit.manifest import SUFFIX as MANIFEST_SUFFIX
from ictus.runspec.config import PipelineConfig, read_config
from ictus.runspec.inputs import PipelineFolder

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from ictus.interfaces import PreflightIssue

app = typer.Typer(
    help="Typed composition for Conductor workflows.",
    no_args_is_help=True,
    add_completion=False,
    # Typer prints every local of every frame on an uncaught exception, which
    # in `watch` means an integration's credential, whole.
    pretty_exceptions_show_locals=False,
)


def _fail(message: str) -> None:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _folders(path: Path) -> list[PipelineFolder]:
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

    Policy and the start gate are applied here, not in the composition, so
    every command sees the same graph.

    ``attach=False`` leaves integrations for the caller to apply.

    ``require_config=False`` is what ``lint`` relaxes: the graph is checked
    without a ``config.yaml``. A malformed one still fails either way.
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
            # After the start policy, so the start gate is announced like any
            # other and its prompt counts the work rather than the reporting.
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


# The only place in the CLI that names an engine.
BACKEND = conductor


@dataclass(frozen=True, slots=True)
class _Written:
    """One emitted file, and what writing it did to the bytes already there.

    ``build/`` is committed, so whether a write moved the artifact is reported.
    """

    path: Path
    status: Literal["wrote", "updated", "same"]

    @property
    def changed(self) -> bool:
        return self.status != "same"


def _write(pipeline: Pipeline, out: Path) -> list[_Written]:
    """Compile and write every document a pipeline produces.

    Each file goes to a temporary sibling and is renamed, so a part-way failure
    cannot leave a truncated document that still parses.
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

    Manifests as well as workflows: a stale one would go on matching messages
    against a prefix nothing declares.
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
