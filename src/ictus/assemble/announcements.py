"""Attaching an integration to a pipeline, without the pipeline knowing one.

A declaration says where a run reports; this decides when. Announcements are
inserted when the pipeline is loaded, after the start policy.

What gets inserted:

* **One opener** for a destination that threads, ahead of everything.
* **Before every gate and every question**, at the top and in every nested
  stage, with a gate's own choices as its buttons.
* **Before every way the graph ends**: each explicit exit, and every route to END.

Each announcement reads what the node it stands in front of reads, so it
renders in the channel as it will in the dashboard.

The thread travels into a stage as an optional parameter, since a child
workflow sees its caller only through what it is passed.

Nothing is inserted after a gate: a branch has a target per choice, and the
answer is already reported. Anything no step can stand in front of — a budget
tripping, a step failing, the engine being killed — is ``ictus watch``'s.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ictus.errors import CompositionError
from ictus.graph.composition import WorkflowInput
from ictus.graph.node import GateNode, Node, QuestionsNode, SubGraphNode, TerminateNode
from ictus.graph.ports import InputPort, PortType
from ictus.graph.ref import tpl
from ictus.graph.signals import RunSignal
from ictus.graph.traversal import budget_cost, reaches
from ictus.stdlib.steps.announce import THREAD_PORT, announce

if TYPE_CHECKING:
    from ictus.graph.pipeline import Pipeline
    from ictus.graph.ref import Ref, Template
    from ictus.graph.requirements import Integration

__all__ = ["OPENER_ID", "Attached", "apply_integrations"]

#: The announcement everything else hangs under.
OPENER_ID = "report_opened"

_PREFIX = "report_"


@dataclass(frozen=True, slots=True)
class Attached:
    """One integration, attached."""

    integration: str
    opener: str | None
    """The step whose output is the run's thread, or ``None`` with no threads."""


def apply_integrations(pipeline: Pipeline) -> tuple[Attached, ...]:
    """Insert the announcements every integration in the pipeline asked for.

    Every integration declared anywhere in it, a stage's included. Runs after
    the start policy, so the start gate is announced like any other.
    """
    return tuple(
        _Attachment(pipeline, target).attach()
        for target in pipeline.all_integrations()
        if target.reports
    )


@dataclass(frozen=True, slots=True)
class _Thread:
    """How one pipeline body reads the conversation it reports into.

    The opener's output at the top, a parameter inside a stage.
    """

    source: Node | WorkflowInput

    @property
    def name(self) -> str:
        """The input port a reader declares for it."""
        return self.ref.source_id

    @property
    def ref(self) -> Ref:
        if isinstance(self.source, WorkflowInput):
            return self.source.ref()
        return self.source.ref(THREAD_PORT)

    def wire(self, body: Pipeline, reader: Node) -> None:
        """Give ``reader`` the thread, through whatever ``body`` has it from."""
        if isinstance(self.source, WorkflowInput):
            body.connect_input(self.source, reader, self.name)
        else:
            body.feed(self.source, THREAD_PORT, reader, self.name)


@dataclass
class _Attachment:
    """One integration being attached to one pipeline."""

    root: Pipeline
    target: Integration
    _added: dict[int, list[Node]] = field(default_factory=dict)
    _announced: set[int] = field(default_factory=set)

    @property
    def label(self) -> str:
        return self.root.pipeline_id

    def attach(self) -> Attached:
        bodies = _bodies(self.root)
        priced = {id(body): budget_cost(body) for body in bodies}
        for body in bodies:
            # Pinned first: an inserted node arrives with no inbound edge,
            # which is how an unpinned entry is found.
            body.set_entry(body.entry())

        thread: _Thread | None = None
        opener: Node | None = None
        given = self.root.thread_for(self.target)
        if given is not None and given.name == THREAD_PORT:
            raise CompositionError(
                f"pipeline {self.label!r} reports into an input called {THREAD_PORT!r}, "
                "which is also what an announcement publishes. A node's ports share one "
                "namespace, so the announcement would declare it twice — name the input "
                "something else; only the reporting reads it."
            )
        if given is not None:
            # The conversation was already open when the run started.
            thread = _Thread(given)
        elif self.target.threads:
            opener = self._first(OPENER_ID, f"*{self.label}* — new run")
            thread = _Thread(opener)
        if given is None and not self.target.threads and self.target.wants(RunSignal.RUN_STARTED):
            self._first(f"{_PREFIX}started", f"*{self.label}* started")

        if self.target.wants(RunSignal.DECISION_NEEDED):
            self._decisions(self.root, thread)
        self._exits(thread)
        self._charge(bodies, priced)
        return Attached(self.target.name, opener.node_id if opener is not None else None)

    # -- where announcements go --------------------------------------------

    def _first(self, wanted: str, text: str) -> Node:
        node = self.root.prepend(
            announce(
                node_id=_name(self.root, wanted),
                description=f"Opens this run's report to {self.target.name}",
                text=text,
                to=self.target,
            )
        )
        self._note(self.root, node)
        return node

    def _decisions(self, body: Pipeline, thread: _Thread | None) -> None:
        """Announce every gate and question in ``body``, and in every stage in it.

        A stage placed twice is one file, so its gates are announced once;
        each placement still needs the thread handed to it.
        """
        if id(body) in self._announced:
            asking: list[Node] = []
        else:
            self._announced.add(id(body))
            asking = [n for n in body.nodes if isinstance(n, (GateNode, QuestionsNode))]
        for node in asking:
            if isinstance(node, GateNode):
                self._before(
                    body,
                    node,
                    thread,
                    text=tpl(f"*{self.label}* needs a decision\n\n", node.prompt),
                    answers=node if self.target.threads else None,
                )
            else:
                # A question set shows one question at a time under one name,
                # so a button could only answer whichever was current.
                self._before(
                    body,
                    node,
                    thread,
                    text=(
                        f"*{self.label}* has questions waiting at `{node.node_id}` — "
                        "answer them in the dashboard"
                    ),
                )
        for host, child in _placed(body):
            if _asks(child):
                self._decisions(child, _thread_into(body, host, child, thread))

    def _exits(self, thread: _Thread | None) -> None:
        """Announce each way the top-level graph ends that was asked about.

        Only the top: a stage finishing is not the run finishing.
        """
        for node in [n for n in self.root.nodes if isinstance(n, TerminateNode)]:
            failed = node.status == "failed"
            if self.target.wants(RunSignal.RUN_FAILED if failed else RunSignal.RUN_FINISHED):
                headline = (
                    f"\N{POLICE CARS REVOLVING LIGHT} *{self.label}* failed\n\n"
                    if failed
                    else f"\N{WHITE HEAVY CHECK MARK} *{self.label}* finished\n\n"
                )
                self._before(self.root, node, thread, text=tpl(headline, node.reason))
        if self.target.wants(RunSignal.RUN_FINISHED) and any(e.is_end for e in self.root.edges):
            said = self.root.insert_before_end(
                announce(
                    node_id=_name(self.root, f"{_PREFIX}finished"),
                    description=f"Report the run finishing to {self.target.name}",
                    text=f"\N{WHITE HEAVY CHECK MARK} *{self.label}* finished",
                    to=self.target,
                    thread=thread.ref if thread is not None else None,
                )
            )
            if thread is not None:
                thread.wire(self.root, said)
            self._note(self.root, said)

    def _before(
        self,
        body: Pipeline,
        node: Node,
        thread: _Thread | None,
        *,
        text: str | Template,
        answers: GateNode | None = None,
    ) -> None:
        """Put an announcement in front of ``node``, reading what it reads."""
        if thread is not None and any(port.name == thread.name for port in node.inputs):
            raise CompositionError(
                f"{node.node_id!r} in {body.pipeline_id!r} has an input called "
                f"{thread.name!r}, which {self.target.name!r} needs for its thread; "
                "rename the input"
            )
        said = body.insert_before(
            node,
            announce(
                node_id=_name(body, f"{_PREFIX}{node.node_id}"),
                description=f"Report {node.node_id!r} to {self.target.name}",
                text=text,
                to=self.target,
                thread=thread.ref if thread is not None else None,
                answers=answers,
                inputs=node.inputs,
            ),
        )
        # The announcement carries the node's prompt, so it reads the same
        # things — hand-written references in a plain string included.
        for dep in body.deps_into(node):
            body.feed(
                dep.source,
                dep.connection.source.name,
                said,
                dep.connection.target.name,
                previous_pass=dep.previous_pass,
            )
        for param, reader, port in body.input_bindings:
            if reader is node:
                body.connect_input(param, said, port.name)
        if thread is not None:
            thread.wire(body, said)
        self._note(body, said)

    # -- what it costs -------------------------------------------------------

    def _note(self, body: Pipeline, node: Node) -> None:
        self._added.setdefault(id(body), []).append(node)

    def _charge(self, bodies: list[Pipeline], priced: dict[int, int]) -> None:
        """Raise an explicit ``max_iterations`` by exactly what was added.

        A derived limit needs nothing: it is priced from the graph at emit time.
        """
        for body in bodies:
            added = self._added.get(id(body), [])
            if body.max_iterations is None or not added:
                continue
            looping = [node.node_id for node in added if reaches(body, node, node)]
            if looping and body.loop_passes is None:
                raise CompositionError(
                    f"{body.pipeline_id!r} sets max_iterations explicitly and loops without "
                    f"loop_passes, so the reporting steps {self.target.name!r} adds inside "
                    f"the loop ({', '.join(sorted(looping))}) cannot be priced. Set "
                    "loop_passes, and they are added to the limit for you."
                )
            body.max_iterations += budget_cost(body) - priced[id(body)]


def _bodies(root: Pipeline) -> list[Pipeline]:
    """``root`` and every pipeline nested in it, each once."""
    found: list[Pipeline] = []
    pending = [root]
    while pending:
        body = pending.pop(0)
        if any(body is seen for seen in found):
            continue
        found.append(body)
        pending.extend(body.children.values())
    return found


def _placed(body: Pipeline) -> list[tuple[SubGraphNode, Pipeline]]:
    """Each stage placed in ``body``, with the pipeline it runs."""
    hosts = {node.node_id: node for node in body.nodes if isinstance(node, SubGraphNode)}
    return [(hosts[node_id], child) for node_id, child in body.children.items()]


def _asks(body: Pipeline) -> bool:
    """Whether a run inside ``body`` can stop and wait for somebody."""
    if any(isinstance(node, (GateNode, QuestionsNode)) for node in body.nodes):
        return True
    return any(_asks(child) for child in body.children.values())


def _thread_into(
    body: Pipeline, host: SubGraphNode, child: Pipeline, thread: _Thread | None
) -> _Thread | None:
    """Hand the thread to a stage as a parameter, the only way into one."""
    if thread is None:
        return None
    name = thread.name
    param = next((p for p in child.workflow_inputs if p.name == name), None)
    if param is None:
        param = child.declare_input(
            name,
            PortType.STRING,
            required=False,
            description="The conversation this run reports into, from the caller",
        )
    if not any(port.name == name for port in host.inputs):
        body.widen_subgraph(
            host,
            InputPort(name, PortType.STRING, "The run's reporting thread", optional=True),
        )
        thread.wire(body, host)
    return _Thread(param)


def _name(pipeline: Pipeline, wanted: str) -> str:
    """``wanted``, or the next free spelling of it. The author's name wins."""
    taken = {node.node_id for node in pipeline.nodes}
    if wanted not in taken:
        return wanted
    for suffix in range(2, 100):
        candidate = f"{wanted}_{suffix}"
        if candidate not in taken:
            return candidate
    return f"{wanted}_{len(taken)}"
