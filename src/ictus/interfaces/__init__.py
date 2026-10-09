"""The boundary between an ictus graph and whatever executes it.

A ``Backend`` is the only thing permitted to know an engine's spelling: its
field names, its template dialect, its iteration accounting, its CLI. An
engine's field name above this line is a defect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ictus.graph.signals import RunSignal

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from ictus.graph.node import NodeKind
    from ictus.graph.pipeline import Pipeline

__all__ = [
    "ENDED",
    "Backend",
    "Capabilities",
    "Document",
    "PreflightIssue",
    "SignalEvent",
    "ValidationResult",
]


#: After one of these the run is over, and whatever is attached must let go.
ENDED = frozenset({RunSignal.RUN_FINISHED, RunSignal.RUN_FAILED})


@dataclass(frozen=True, slots=True)
class SignalEvent:
    """One reportable moment, with the run it happened in.

    The engine-neutral facts a report is made of, filled in by a backend from
    its own payload.
    """

    signal: RunSignal
    run_id: str
    workflow: str
    at: float
    event_type: str
    """The engine's own name for it, kept so a report can say what it saw."""

    step: str = ""
    """The step it happened at, when there is one."""

    options: tuple[str, ...] = ()
    """What a decision offers."""

    prompt: str = ""
    """What a decision asks."""

    choice: str = ""
    """What a decision was answered with."""

    notes: tuple[tuple[str, str], ...] = ()
    """Any text left with that answer, by name."""

    reason: str = ""
    """Why a run or a step ended the way it did, when it says."""

    at_a_step: bool = False
    """A step in the graph stands in front of this moment, and has already
    announced it, so a reporter outside the run should not say it twice."""

    replayed: bool = False
    """Read from the run's history on attaching, rather than seen as it
    happened. Not news, so not reported onward."""

    @property
    def ends_the_run(self) -> bool:
        """Whether nothing further will arrive for this run."""
        return self.signal in ENDED


@dataclass(frozen=True, slots=True)
class Document:
    """One rendered file, ready to be written. Text, since the format is the
    engine's business."""

    filename: str
    content: str


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What an engine can actually express.

    Declared, so a graph using a feature the target lacks is refused at
    composition.
    """

    name: str
    kinds: frozenset[NodeKind]
    providers: frozenset[str] = frozenset()
    """Who can answer a model call. Empty means the engine does not constrain it."""

    remembering_providers: frozenset[str] = frozenset()
    """Providers whose steps can resume a session rather than starting cold.

    One that cannot rejects the request rather than ignoring it.
    """

    tool_allowlists: bool = False
    """Whether a step can name which tools it may use.

    Three states: omitted is the engine's default set, empty is none, and a
    non-empty list is exactly these. Only an engine that can translate the
    third declares this true.
    """

    conditional_routes: bool = True
    cycles: bool = True
    sub_graphs: bool = True

    signals: frozenset[RunSignal] = frozenset()
    """Which moments of a run this engine can actually report.

    A subscription to one that is absent is refused at composition. Empty
    means the engine reports nothing.
    """

    notes: str = ""


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """The engine's own opinion of a compiled document."""

    path: Path
    ok: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PreflightIssue:
    """Something the environment must provide before a pipeline can run.

    ``remedy`` says what to type or where to click, not what is wrong.
    """

    requirement: str
    problem: str
    remedy: str
    blocking: bool = True


@runtime_checkable
class Backend(Protocol):
    """An execution target for an ictus pipeline."""

    def capabilities(self) -> Capabilities:
        """What this backend can express."""
        ...

    def compile(self, pipeline: Pipeline) -> list[Document]:
        """Render ``pipeline`` and every stage it contains."""
        ...

    def lint(self, pipeline: Pipeline) -> list[str]:
        """Engine-specific problems the generic composition rules cannot know."""
        ...

    def validate(self, paths: Sequence[Path]) -> list[ValidationResult]:
        """Ask the engine itself whether the compiled documents load."""
        ...

    def preflight(self, pipeline: Pipeline, *, probe: bool) -> list[PreflightIssue]:
        """Check the environment can satisfy what the pipeline declares.

        ``probe`` additionally opens each declared connection, which catches a
        wrong credential that an offline check cannot.
        """
        ...

    def run(
        self,
        path: Path,
        *,
        inputs: Mapping[str, str],
        dashboard: bool,
        workspace_instructions: bool = True,
        working_dir: Path | None = None,
    ) -> int:
        """Execute a compiled document in ``working_dir``. Returns the exit code.

        ``workspace_instructions`` asks the engine to read the target project's
        own instruction files; which ones it finds is its business.
        """
        ...
