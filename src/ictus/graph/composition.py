"""The records a graph is built from, and the vocabulary its settings use.

Everything here is frozen and inert: an ``Edge`` knows what it joins and
nothing about where it lives. ``Pipeline`` owns them all and is the only thing
that may make one — which is why these moved out of ``pipeline.py`` and the
class did not. Ten of the eleven names other packages take from that module are
here; the eleventh is ``Pipeline``.

**Two comparison rules, and every ``is`` in the library depends on which.**
``ParallelGroup``, ``WorkflowInput``, ``Edge`` and ``ExposedOutput`` are
``eq=False``: they compare and hash by identity, so two structurally identical
nodes route independently and ``edge.source is node`` means what it says.
``Listener`` and ``DataDep`` are ``slots=True`` and compare by value, because
nothing holds one by identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

# Runtime, not TYPE_CHECKING, and ruff is told so below: `RouteEnd` and
# `EdgeTarget` are PEP 695 aliases, which resolve their right-hand side lazily
# against this module's globals. Under TYPE_CHECKING the names are absent at
# run time and `RouteEnd.__value__` — which is what `get_type_hints` reaches
# for — raises NameError instead of returning a type.
from ictus.graph.mapping import MapGroup
from ictus.graph.node import Node
from ictus.graph.ref import Origin, Ref, Template

if TYPE_CHECKING:
    from ictus.graph.ports import OutputPort, PortConnection, PortType
    from ictus.graph.requirements import Integration
    from ictus.graph.values import YamlScalar

__all__ = [
    "ABORT_CASE",
    "END",
    "BudgetMode",
    "ContextMode",
    "DataDep",
    "Edge",
    "EdgeTarget",
    "ExposedOutput",
    "FailureMode",
    "Listener",
    "NativeTools",
    "ParallelGroup",
    "RouteEnd",
    "TrimStrategy",
    "WorkflowInput",
]

#: The ``case`` on the edge a person takes by abandoning a set of questions.
#: Never a choice value, so the leading underscores keep it out of the space a
#: gate's options occupy. One definition: `lint/rules.py` had a second copy.
ABORT_CASE = "__abort__"

ContextMode = Literal["accumulate", "last_only", "explicit"]
#: Built-in tools for a step that names none of its own: nothing, everything
#: the CLI can do, or exactly the tool ids listed.
NativeTools = Literal["none", "claude_code"] | tuple[str, ...]
BudgetMode = Literal["audit", "enforce"]


class FailureMode(StrEnum):
    """What a parallel group does when one of its members fails."""

    FAIL_FAST = "fail_fast"
    CONTINUE_ON_ERROR = "continue_on_error"
    ALL_OR_NOTHING = "all_or_nothing"


class TrimStrategy(StrEnum):
    """How an engine makes room when accumulated context hits its ceiling."""

    TRUNCATE = "truncate"
    """Shorten fields in place. The only one that leaves references resolvable."""

    DROP_OLDEST = "drop_oldest"
    """Delete whole step outputs, oldest first."""

    SUMMARIZE = "summarize"
    """Replace outputs with one summary. Falls back to ``DROP_OLDEST`` with no model."""


@dataclass(frozen=True, eq=False)
class ParallelGroup:
    """Members that run at once, routed as one node.

    Only model calls and computations may be members.
    Members carry no edges of their own.
    """

    group_id: str
    members: tuple[Node, ...]
    description: str = ""
    failure_mode: FailureMode = FailureMode.FAIL_FAST

    @property
    def node_id(self) -> str:
        return self.group_id


class _End:
    """Sentinel for Conductor's ``$end`` route target."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "END"


END = _End()

type RouteEnd = Node | ParallelGroup | MapGroup
type EdgeTarget = RouteEnd | _End


@dataclass(frozen=True, eq=False)
class WorkflowInput:
    """A typed parameter of the whole pipeline. Emitted as ``workflow.input``."""

    name: str
    port_type: PortType
    required: bool = True
    default: YamlScalar = None
    description: str = ""
    prose: bool = False
    """Whether the input file's body text feeds this parameter. At most one per pipeline."""

    def ref(self) -> Ref:
        return Ref(
            source_id=self.name,
            port=self.name,
            port_type=self.port_type,
            origin=Origin.WORKFLOW_INPUT,
            source=self,
        )


@dataclass(frozen=True, slots=True)
class Listener:
    """What wakes a pipeline. Compiled into a manifest beside the workflow."""

    service: Integration | None
    """Where it reports back, when it reports anywhere.

    ``None`` is the ordinary case for a pipeline that only needs starting: the
    credential for a channel belongs to whoever reads it, and a run that says
    nothing there has no use for one."""

    prefix: str
    """What marks a message as a request rather than conversation."""

    into: WorkflowInput
    """The input the text after the prefix arrives in."""

    thread: WorkflowInput | None = None
    """Where the conversation is addressed. ``None`` when the pipeline reports nowhere."""


@dataclass(frozen=True, eq=False)
class Edge:
    """A control edge — who runs next. Emitted as ``routes`` or ``options[].route``.

    Carries no data; what a node reads is a ``DataDep``.
    """

    source: RouteEnd
    target: EdgeTarget
    case: str | None = None
    when: str | Template | None = None

    def condition_refs(self) -> tuple[Ref, ...]:
        return tuple(self.when.refs()) if isinstance(self.when, Template) else ()

    @property
    def is_end(self) -> bool:
        return isinstance(self.target, _End)

    @property
    def target_node(self) -> RouteEnd | None:
        """The successor, or ``None`` when this edge ends the run."""
        return None if isinstance(self.target, _End) else self.target

    @property
    def describe_target(self) -> str:
        """For error messages only. Never emitted."""
        target = self.target_node
        return "END" if target is None else target.node_id


@dataclass(frozen=True, eq=False)
class ExposedOutput:
    """One entry of a pipeline's final ``output:`` map, before rendering.

    Held as source and port, not a rendered template; the backend addresses it.
    """

    source: Node | MapGroup
    port: OutputPort
    default: str | None = None


@dataclass(frozen=True, slots=True)
class DataDep:
    """A data edge — what a node reads. Emitted into the target's ``input`` list.

    The source may be a map group, which publishes one aggregate under its name.
    """

    source: Node | MapGroup
    target: Node
    connection: PortConnection
    previous_pass: bool = False
    """Whether this edge reads what the source produced on the previous pass.

    Only valid between two members of one parallel group, inside a loop.
    """
