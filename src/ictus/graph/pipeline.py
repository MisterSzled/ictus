"""The composition graph.

``Pipeline`` owns every edge. Nodes never point at each other, so a node stays
reusable across pipelines and there is exactly one place that knows the shape of
the graph.

Every rejection happens at the call that introduces the invalid state, not at
emission: a port mismatch fails in ``connect``, a foreign node fails in
``connect``, an unroutable gate choice fails in ``branch``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from ictus.errors import CompositionError, PortTypeError
from ictus.graph.mapping import Item, MapGroup
from ictus.graph.node import (
    OUTCOME_PORT,
    GateNode,
    Node,
    NodeKind,
    QuestionsNode,
    ScopeNode,
    SubGraphNode,
    TerminateNode,
)
from ictus.graph.ports import InputPort, OutputPort, PortConnection, PortType
from ictus.graph.ref import Origin, Ref, Template, equals

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from ictus.graph.requirements import Datasource, Executable, Integration, McpServer
    from ictus.graph.signals import RunSignal
    from ictus.graph.values import YamlScalar

_STRUCTURED = frozenset({PortType.OBJECT, PortType.ARRAY})

# Conductor permits only these inside a parallel group (config/validator.py:748-785).
_GROUPABLE = frozenset({NodeKind.LLM_CALL, NodeKind.COMPUTATION})

ContextMode = Literal["accumulate", "last_only", "explicit"]
#: What built-in tools a step that names none of its own is given. ``none``,
#: everything the CLI can do, or exactly the tool ids listed — which is the one
#: that lets a step read a repository without also being handed a shell.
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
    """Replace outputs with one short summary, deleting the originals — and fall
    back to ``DROP_OLDEST`` where the engine cannot reach a model to write it."""


@dataclass(frozen=True, eq=False)
class ParallelGroup:
    """Members that run at once, addressed as one thing by the graph.

    Routing goes to and from the *group*; members carry no edges of their own.
    That is not an ictus choice — Conductor rejects a member with ``routes``,
    and rejects gates, scripts, waits, sub-workflows and terminals as members
    outright. Only model calls and computations may run in a group.

    ``node_id`` is deliberately the same attribute a node uses, so every routing
    path treats a group and a node identically.
    """

    group_id: str
    members: tuple[Node, ...]
    description: str = ""
    failure_mode: FailureMode = FailureMode.FAIL_FAST

    @property
    def node_id(self) -> str:
        """The identifier routing resolves against."""
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
    """Whether an input file's body text feeds this parameter.

    At most one per pipeline. It is the long input — a brief, a charge, a diff —
    and putting it in the body rather than under a YAML key is the difference
    between editing a document and editing a config file.
    """

    def ref(self) -> Ref:
        """A typed reference to this pipeline parameter."""
        return Ref(
            source_id=self.name,
            port=self.name,
            port_type=self.port_type,
            origin=Origin.WORKFLOW_INPUT,
            source=self,
        )


@dataclass(frozen=True, slots=True)
class Listener:
    """What wakes a pipeline, declared beside what it reports to.

    The other half of ``integrate``. A pipeline that announces into a channel
    but is started by a flag somebody typed is only half written down: a reader
    can see where it will talk and not what makes it run, and the two have to
    agree about which input carries the conversation.

    Compiled into a manifest beside the workflow, so what starts a run ships
    with the run and is reviewed in the same diff.
    """

    service: Integration
    prefix: str
    """What marks a message as a request rather than conversation."""

    into: WorkflowInput
    """The input the text after the prefix arrives in."""

    thread: WorkflowInput | None = None
    """Where the conversation is addressed, taken from the same service's
    ``integrate``. ``None`` when the pipeline reports nowhere."""


@dataclass(frozen=True, eq=False)
class Edge:
    """A control edge — who runs next. Emitted as ``routes`` or ``options[].route``.

    Carries no data. Conductor separates control (``routes``) from data
    (``input``), and collapsing them loses every reference that has to cross a
    gate: a gate's branch decides where to go, not what the target reads.
    """

    source: RouteEnd
    target: EdgeTarget
    case: str | None = None
    when: str | Template | None = None

    def condition_refs(self) -> tuple[Ref, ...]:
        """Typed references inside this edge's condition."""
        return tuple(self.when.refs()) if isinstance(self.when, Template) else ()

    @property
    def is_end(self) -> bool:
        """Whether this edge terminates the run rather than naming a successor."""
        return isinstance(self.target, _End)

    @property
    def target_node(self) -> RouteEnd | None:
        """The successor, or ``None`` when this edge ends the run."""
        return None if isinstance(self.target, _End) else self.target

    @property
    def describe_target(self) -> str:
        """A human-readable target, for error messages only — never emitted."""
        target = self.target_node
        return "END" if target is None else target.node_id


@dataclass(frozen=True, eq=False)
class ExposedOutput:
    """One entry of a pipeline's final ``output:`` map, before rendering.

    Kept as the source and the port rather than a finished template so the
    backend decides how to address it — which is the only place that knows a
    group member is read through its group, and that a value crossing a
    rendered-text boundary has to survive being parsed back.
    """

    source: Node | MapGroup
    port: OutputPort
    default: str | None = None


@dataclass(frozen=True, slots=True)
class DataDep:
    """A data edge — what a node reads. Emitted into the target's ``input`` list.

    The source may be a map group as well as a node: a for-each group publishes
    one aggregate under its own name, and a later step reads it exactly the way
    it reads a step's output.
    """

    source: Node | MapGroup
    target: Node
    connection: PortConnection
    previous_pass: bool = False
    """Whether this edge reads what the source produced on the *last* time round.

    Only meaningful between two members of one parallel group, where it is the
    difference between a reference that resolves to nothing and one that carries
    the previous round. The engine stores a group's result under the group's own
    name and overwrites it when the group next finishes, so while a member is
    running its siblings' entries still hold the pass before. Outside a loop
    there is no such pass, which is what the lint checks.
    """


class Pipeline:
    """A workflow graph plus the run-level settings Conductor needs.

    ``loop_passes`` is mandatory once the graph contains a cycle. Conductor's
    ``limits.max_iterations`` defaults to 10 *total step executions*, so a
    six-node graph with one retry loop is force-stopped on its second pass by a
    number nobody chose. Requiring the bound makes the choice explicit and lets
    the emitter derive a value from the actual graph.
    """

    def __init__(
        self,
        *,
        pipeline_id: str,
        description: str = "",
        version: str = "1",
        provider: str | None = None,
        default_model: str | None = None,
        context_mode: ContextMode = "explicit",
        context_max_tokens: int | None = None,
        context_trim: TrimStrategy | None = None,
        loop_passes: int | None = None,
        budget_usd: float | None = None,
        budget_mode: BudgetMode = "audit",
        max_iterations: int | None = None,
        timeout_seconds: int | None = None,
        metadata: Mapping[str, str] | None = None,
        instructions: Sequence[str] = (),
        system_prompt: str | None = None,
    ) -> None:
        if not pipeline_id:
            raise CompositionError("pipeline_id cannot be empty")
        if loop_passes is not None and loop_passes < 1:
            raise CompositionError(f"loop_passes must be >= 1, got {loop_passes}")
        if timeout_seconds is not None and timeout_seconds < 1:
            raise CompositionError(f"timeout_seconds must be >= 1, got {timeout_seconds}")
        self.pipeline_id = pipeline_id
        self.description = description
        self.version = version
        # None means "whatever the backend defaults to". The graph has no
        # opinion about providers; leaving it unset here is not the same as
        # leaving it unset in the emitted file, which is what silently selected
        # copilot before.
        self.provider = provider
        self.default_model = default_model
        self.native_tools: NativeTools | None = None
        """Whether a step that names no tools gets the engine's built-in set.

        Off unless asked for. A step with no ``tools`` used to be handed the
        filesystem, a shell and the web without the pipeline ever saying so,
        which Conductor closed — and closing it means a step that was reading
        files now quietly answers from memory instead. Set it where the policy
        lives, so the diff shows which pipelines can touch a disk."""
        self.context_mode: ContextMode = context_mode
        self.context_max_tokens = context_max_tokens
        """A soft ceiling on accumulated context, above which the engine trims.

        Per workflow file, which means per *stage*: a stage compiles to its own
        document with its own ``context:`` block, so a long council can be
        bounded without bounding its caller. There is no per-node equivalent —
        the engine has no per-agent context config.
        """

        self.context_trim = context_trim
        """How the engine makes room once the ceiling is reached.

        Named rather than left to the engine, which silently uses
        ``drop_oldest``. That one deletes whole step outputs, oldest first, and
        a deleted output is indistinguishable from inside a prompt from a step
        that has not run yet — so a loop reading it carries on rendering nothing
        and looks like a first pass forever. ``TRUNCATE`` shortens fields in
        place and leaves every reference resolvable, which is the only strategy
        that degrades rather than disappears.
        """
        self.loop_passes = loop_passes
        self.budget_usd = budget_usd
        self.budget_mode: BudgetMode = budget_mode
        self.max_iterations = max_iterations
        # A wall-clock ceiling on the whole run, as against a step's own
        # `timeout_seconds`, which bounds one model call. Nothing else bounds
        # elapsed time: `budget_usd` bounds spend and `max_iterations` bounds
        # step count, and a run can sit for hours without moving either.
        self.timeout_seconds = timeout_seconds
        self.metadata: dict[str, str] = dict(metadata or {})
        # Prepended to every step's prompt. The engine runs its agents with no
        # settings sources at all — no CLAUDE.md, no ambient skills, no hooks —
        # so a step knows nothing about the project it is working on beyond what
        # its prompt says. This is where that context goes back in.
        self.instructions: list[str] = list(instructions)
        # Applied to every model call that does not set its own. Left unset the
        # engine sends an *empty* system prompt, not a default one.
        self.system_prompt = system_prompt

        self._nodes: list[Node] = []
        self._by_id: dict[str, Node] = {}
        self._edges: list[Edge] = []
        self._deps: list[DataDep] = []
        self._inputs: dict[str, WorkflowInput] = {}
        self._input_edges: list[tuple[WorkflowInput, Node, InputPort]] = []
        self._outputs: dict[str, ExposedOutput] = {}
        self._children: dict[str, Pipeline] = {}
        self._groups: dict[str, ParallelGroup] = {}
        self._maps: dict[str, MapGroup] = {}
        self._mcp: dict[str, McpServer] = {}
        self._executables: dict[str, Executable] = {}
        self._integrations: dict[str, Integration] = {}
        self._threads: dict[str, WorkflowInput] = {}
        self._listeners: dict[str, Listener] = {}
        self._datasources: dict[str, Datasource] = {}
        self.workspace_instructions = True
        """Whether a run is given what the working directory says about itself.

        Policy, set from ``config.yaml``, and it travels in the listen manifest
        because the process that starts a run is the one that has to pass the
        flag. On by default: a step otherwise arrives knowing nothing a project
        says about how it wants to be worked in. Off for a pipeline whose work
        is not *about* the directory it runs in — a tracker ticket does not want
        a contributor guide prepended to every prompt."""
        self._before_start: Node | None = None
        self._entry: RouteEnd | None = None

    # -- construction ----------------------------------------------------

    def add[N: Node](self, node: N) -> N:
        """Register a node. Returns it unchanged so callers keep the concrete type."""
        existing = self._by_id.get(node.node_id)
        if existing is not None:
            what = "the same node twice" if existing is node else "two nodes"
            raise CompositionError(
                f"pipeline {self.pipeline_id!r} would register {what} under node_id "
                f"{node.node_id!r}; ids are the routing keyspace and must be unique"
            )
        self._nodes.append(node)
        self._by_id[node.node_id] = node
        return node

    def add_subworkflow(self, node: SubGraphNode, body: Pipeline) -> SubGraphNode:
        """Register a nested workflow and the pipeline it compiles from.

        The child is a full ``Pipeline`` with its own entry point and graph;
        the parent spends exactly one iteration on it.
        """
        self.add(node)
        self._children[node.node_id] = body
        return node

    @property
    def children(self) -> dict[str, Pipeline]:
        """Nested pipelines, keyed by the sub-workflow node that hosts them."""
        return dict(self._children)

    def parallel(
        self,
        group_id: str,
        members: Sequence[Node],
        *,
        description: str = "",
        failure_mode: FailureMode = FailureMode.FAIL_FAST,
    ) -> ParallelGroup:
        """Run ``members`` at once, as one addressable step.

        The members must already be part of this pipeline; the group is a way of
        scheduling them, not a second place to declare them. A group costs its
        member count against the iteration budget all at once.
        """
        if group_id in self._groups or group_id in self._by_id:
            raise CompositionError(
                f"pipeline {self.pipeline_id!r} already has something named {group_id!r}; "
                "groups and nodes share one routing keyspace"
            )
        if len(members) < 2:
            raise CompositionError(
                f"parallel group {group_id!r} needs at least two members; "
                f"got {len(members)}. One member is just a node."
            )
        for member in members:
            self._require_member(member, f"member of parallel group {group_id!r}")
            if member.kind not in _GROUPABLE:
                raise CompositionError(
                    f"{member.node_id!r} is a {member.kind.value} and cannot run inside a "
                    f"parallel group. Conductor permits only model calls and computations as "
                    "members; gates, questions, scripts, waits, sub-workflows and terminals "
                    "are all rejected. Put it before or after the group."
                )
            if self.group_of(member) is not None:
                raise CompositionError(
                    f"{member.node_id!r} is already a member of another parallel group"
                )
        group = ParallelGroup(
            group_id=group_id,
            members=tuple(members),
            description=description,
            failure_mode=failure_mode,
        )
        self._groups[group_id] = group
        return group

    def map_over(
        self,
        group_id: str,
        *,
        source: Ref,
        item: Item,
        body: Node,
        expect_items: int,
        description: str = "",
        max_concurrent: int = 10,
        failure_mode: FailureMode | None = None,
        key_by: str | None = None,
    ) -> MapGroup:
        """Run ``body`` once per element of ``source``, however many there are.

        This is the fan-out whose width the author does not know: it comes out
        of an earlier step at run time. ``body`` is a node of this pipeline and
        stops being an ordinary step — it gets no routes of its own, since an
        item cannot decide where the run goes next, and it is not emitted in
        ``agents:``; it becomes the group's inline template.
        """
        if group_id in self._groups or group_id in self._maps or group_id in self._by_id:
            raise CompositionError(
                f"pipeline {self.pipeline_id!r} already has something named {group_id!r}; "
                "groups and nodes share one routing keyspace"
            )
        self._require_member(body, f"body of map group {group_id!r}")
        if self.group_of(body) is not None:
            raise CompositionError(f"{body.node_id!r} is already a member of a parallel group")
        if self.map_of(body) is not None:
            raise CompositionError(f"{body.node_id!r} is already the body of a map group")
        if any(e.source is body for e in self._edges):
            raise CompositionError(
                f"{body.node_id!r} has routes of its own and cannot be a map body; "
                "a for_each item cannot decide where the run goes next — the group routes "
                "once, after every item has finished"
            )
        group = MapGroup(
            group_id=group_id,
            source=source,
            item=item,
            body=body,
            expect_items=expect_items,
            description=description,
            max_concurrent=max_concurrent,
            failure_mode=failure_mode,
            key_by=key_by,
        )
        self._maps[group_id] = group
        return group

    @property
    def maps(self) -> tuple[MapGroup, ...]:
        """Every map group, in declaration order."""
        return tuple(self._maps.values())

    def map_of(self, node: Node) -> MapGroup | None:
        """The map group ``node`` is the body of, if any."""
        return next((m for m in self._maps.values() if m.body is node), None)

    def map_named(self, group_id: str) -> MapGroup | None:
        """The map group with this id, if the pipeline has one."""
        return self._maps.get(group_id)

    @property
    def groups(self) -> tuple[ParallelGroup, ...]:
        """Every parallel group, in declaration order."""
        return tuple(self._groups.values())

    def group_of(self, node: Node) -> ParallelGroup | None:
        """The group ``node`` runs inside, if any.

        Load-bearing for references: a member's output is read through the
        group, not directly, and emitting the direct form renders empty.
        """
        for group in self._groups.values():
            if any(m is node for m in group.members):
                return group
        return None

    def require_mcp(self, server: McpServer) -> McpServer:
        """Declare an MCP server this pipeline needs access to.

        Declared where the pipeline is written so it can be checked before the
        pipeline launches, rather than discovered when an agent reaches for a
        tool that was never connected.
        """
        existing = self._mcp.get(server.name)
        if existing is not None:
            raise CompositionError(
                f"pipeline {self.pipeline_id!r} already requires an MCP server named "
                f"{server.name!r}; names are how a server is addressed and must be unique"
            )
        self._mcp[server.name] = server
        return server

    @property
    def mcp_servers(self) -> tuple[McpServer, ...]:
        """Every MCP server this pipeline declares, in declaration order."""
        return tuple(self._mcp.values())

    def require_executable(self, tool: Executable) -> Executable:
        """Declare a command that must be reachable before this pipeline runs.

        For a step whose job is to check something against a tool: if the tool
        is not there, the step does not crash, it concludes. Refusing at the
        launch is the difference between a free failure and a paid one.
        """
        existing = self._executables.get(tool.name)
        if existing is not None:
            raise CompositionError(
                f"pipeline {self.pipeline_id!r} already requires an executable named "
                f"{tool.name!r}; a command is addressed by its name and must be unique"
            )
        self._executables[tool.name] = tool
        return tool

    @property
    def executables(self) -> tuple[Executable, ...]:
        """Every command this pipeline declares, in declaration order."""
        return tuple(self._executables.values())

    def all_executables(self) -> tuple[Executable, ...]:
        """This pipeline's commands and those of every stage it contains.

        A stage runs in the same environment as its caller, so its requirement
        is the caller's problem too — the same reasoning as ``all_mcp_servers``.
        """
        seen: dict[str, Executable] = dict(self._executables)
        for child in self._children.values():
            for tool in child.all_executables():
                seen.setdefault(tool.name, tool)
        return tuple(seen.values())

    def require_datasource(self, source: Datasource) -> Datasource:
        """Declare somewhere this pipeline reads data from.

        Beside ``require_executable`` and ``integrate``, and for the same
        reason: a reader should see what a run reaches outside the machine
        before they read what it does, and a connection string that is not set
        should refuse the launch rather than fail a step that has already been
        paid for.

        The commands the source needs are declared with it, so a machine
        without them is refused by the same preflight and nobody has to
        remember that one implies the other.
        """
        existing = self._datasources.get(source.name)
        if existing is not None:
            raise CompositionError(
                f"pipeline {self.pipeline_id!r} already reads from something called "
                f"{source.name!r}; names are how one is addressed and must be unique"
            )
        self._datasources[source.name] = source
        for tool in source.needs:
            self._executables.setdefault(tool.name, tool)
        return source

    @property
    def datasources(self) -> tuple[Datasource, ...]:
        """Every source this pipeline declares, in declaration order."""
        return tuple(self._datasources.values())

    def all_datasources(self) -> tuple[Datasource, ...]:
        """This pipeline's sources and those of every stage it contains."""
        seen: dict[str, Datasource] = dict(self._datasources)
        for child in self._children.values():
            for source in child.all_datasources():
                seen.setdefault(source.name, source)
        return tuple(seen.values())

    def all_mcp_servers(self) -> tuple[McpServer, ...]:
        """This pipeline's servers and those of every stage it contains.

        A stage is compiled as its own workflow with its own runtime block, but
        preflight is about the environment the whole launch needs — so the
        requirement of a nested stage is the caller's problem too.
        """
        seen: dict[str, McpServer] = {s.name: s for s in self._mcp.values()}
        for child in self._children.values():
            for server in child.all_mcp_servers():
                seen.setdefault(server.name, server)
        return tuple(seen.values())

    def before_start_gate[N: Node](self, node: N) -> N:
        """Run ``node`` first, ahead of the confirmation gate.

        The start gate is placed at the entry point when a pipeline is loaded,
        which means nothing can ordinarily precede it — and the one thing most
        worth saying out loud is that a run has parked on it and is waiting for
        somebody. A pipeline whose job is to report had no way to report that.

        Wire nothing: the node is placed by whoever applies the start policy, so
        it is correct whether the gate is on or off. Its outputs are readable
        like any other node's, which is how an announcement that opens a thread
        can have the rest of the run reply underneath it.
        """
        if self._before_start is not None:
            raise CompositionError(
                f"pipeline {self.pipeline_id!r} already runs {self._before_start.node_id!r} "
                "before the start gate; only one thing can go first"
            )
        self.add(node)
        self._before_start = node
        return node

    @property
    def start_herald(self) -> Node | None:
        """What runs before the start gate, if anything was asked to."""
        return self._before_start

    def integrate(
        self, service: Integration, *, thread: WorkflowInput | None = None
    ) -> Integration:
        """Declare a third-party service this pipeline talks to.

        ``thread`` says the conversation is already open and names the input it
        arrives in — a run started *by* a message reports under that message
        rather than beside it. Without one a run opens its own.

        Put these at the top of a pipeline. A reader should see what a run will
        reach outside the machine before they read what it does, and whoever
        approves the run is shown the same list at the start gate.

        Declaring is not attaching. Nothing is inserted until the pipeline is
        loaded, and what gets inserted is decided by the signals the integration
        names — so adding a destination is one line here and removing it is
        deleting that line, rather than unpicking nodes and data edges by hand.
        """
        existing = self._integrations.get(service.name)
        if existing is not None:
            raise CompositionError(
                f"pipeline {self.pipeline_id!r} already integrates something called "
                f"{service.name!r}; names are how one is addressed and must be unique"
            )
        if thread is not None:
            if self._inputs.get(thread.name) is not thread:
                raise CompositionError(
                    f"pipeline {self.pipeline_id!r} reports into {thread.name!r}, which it "
                    "does not declare as an input; declare_input it first"
                )
            if thread.port_type is not PortType.STRING:
                raise CompositionError(
                    f"pipeline {self.pipeline_id!r} reports into {thread.name!r}, which is "
                    f"{thread.port_type.value}; a conversation is addressed by a string"
                )
            self._threads[service.name] = thread
        self._integrations[service.name] = service
        return service

    def listen_on(self, service: Integration, *, prefix: str, into: WorkflowInput) -> Listener:
        """Declare that a message on ``service`` starts this pipeline.

        ``prefix`` is what marks a message as a request; whatever follows it
        becomes ``into``. The conversation comes from the same service's
        ``integrate(thread=...)``, so the run reports under the message that
        started it without the two being named separately and drifting apart.

        Put it beside ``integrate``. A reader should see what wakes a run in
        the same place they see what it talks to, and a listener should not
        need flags that repeat what the pipeline already knows.
        """
        if service.name not in self._integrations:
            raise CompositionError(
                f"pipeline {self.pipeline_id!r} listens on {service.name!r} without "
                "integrating it; integrate() it first so one declaration says both "
                "what starts a run and where it reports"
            )
        if not service.listens:
            raise CompositionError(
                f"integration {service.name!r} cannot be listened on — it can only be "
                "written to. Use a constructor that holds a credential for waiting, "
                "or start this pipeline some other way"
            )
        if not prefix.strip():
            raise CompositionError(
                f"pipeline {self.pipeline_id!r} listens on {service.name!r} with a blank "
                "prefix, which every message matches"
            )
        existing = self._listeners.get(service.name)
        if existing is not None:
            raise CompositionError(
                f"pipeline {self.pipeline_id!r} already listens on {service.name!r}; "
                "one service starts it one way"
            )
        if self._inputs.get(into.name) is not into:
            raise CompositionError(
                f"pipeline {self.pipeline_id!r} listens into {into.name!r}, which it does "
                "not declare as an input; declare_input it first"
            )
        if into.port_type is not PortType.STRING:
            raise CompositionError(
                f"pipeline {self.pipeline_id!r} listens into {into.name!r}, which is "
                f"{into.port_type.value}; what somebody types is a string"
            )
        thread = self._threads.get(service.name)
        if thread is not None and thread.name == into.name:
            raise CompositionError(
                f"pipeline {self.pipeline_id!r} would put the question and the "
                f"conversation both in {into.name!r}; they are two values"
            )
        listener = Listener(service=service, prefix=prefix, into=into, thread=thread)
        self._listeners[service.name] = listener
        return listener

    @property
    def listeners(self) -> tuple[Listener, ...]:
        """Every way this pipeline can be started, in declaration order."""
        return tuple(self._listeners.values())

    def thread_for(self, service: Integration) -> WorkflowInput | None:
        """The input a run's conversation arrives in, if it was given one."""
        return self._threads.get(service.name)

    @property
    def integrations(self) -> tuple[Integration, ...]:
        """Every service this pipeline declares, in declaration order."""
        return tuple(self._integrations.values())

    def all_integrations(self) -> tuple[Integration, ...]:
        """This pipeline's services and those of every stage it contains.

        A stage reports onto its caller's conversation — one run, one thread — so
        a nested declaration is the caller's problem too.
        """
        seen: dict[str, Integration] = dict(self._integrations)
        for child in self._children.values():
            for service in child.all_integrations():
                seen.setdefault(service.name, service)
        return tuple(seen.values())

    def insert_before[N: Node](self, existing: Node, inserted: N) -> N:
        """Put ``inserted`` in front of ``existing``, taking over its inbound edges.

        Every route that pointed at ``existing`` now points at ``inserted``, and
        ``inserted`` routes on to ``existing``. The entry point moves too when
        ``existing`` was it.

        This is what lets an announcement be attached to a graph somebody else
        wrote. Doing it by hand means knowing every edge that arrives — including
        the gate branches, which name their target by choice — and getting one
        wrong is a step that silently never runs.
        """
        self._require_member(existing, "insertion point")
        self.add(inserted)
        self._edges = [
            Edge(source=edge.source, target=inserted, case=edge.case, when=edge.when)
            if edge.target_node is existing
            else edge
            for edge in self._edges
        ]
        self._edges.append(Edge(source=inserted, target=existing))
        if self._entry is existing:
            self._entry = inserted
        return inserted

    def insert_before_end[N: Node](self, inserted: N) -> N:
        """Put ``inserted`` on every way this graph reaches the end of the run.

        The ``END`` counterpart of ``insert_before``: each edge that finished the
        run now runs ``inserted`` instead, and ``inserted`` finishes it. A gate's
        branch keeps its case, so "no" still means no.
        """
        finishing = [edge for edge in self._edges if edge.is_end]
        if not finishing:
            raise CompositionError(
                f"pipeline {self.pipeline_id!r} has no route to END to put "
                f"{inserted.node_id!r} in front of"
            )
        self.add(inserted)
        self._edges = [
            Edge(source=edge.source, target=inserted, case=edge.case, when=edge.when)
            if edge.is_end
            else edge
            for edge in self._edges
        ]
        self._edges.append(Edge(source=inserted, target=END))
        return inserted

    def prepend[N: Node](self, node: N) -> N:
        """Run ``node`` before whatever this graph currently starts with.

        Unlike ``insert_before``, the old entry keeps every edge that arrives at
        it later: a loop that returns to the first step returns there, not to
        ``node``, which runs once.
        """
        first = self.entry()
        self.add(node)
        self.route(node, first)
        self.set_entry(node)
        return node

    def widen_subworkflow(self, host: SubGraphNode, port: InputPort) -> None:
        """Give a stage already placed here one more parameter.

        For what is attached after a stage was placed — an integration threading
        its conversation into the stage's own gates — rather than for authoring;
        a stage's contract is otherwise fixed when it is instantiated.

        The node is changed in place, which a frozen node is not supposed to
        allow. Replacing it was the alternative and is worse: every edge, data
        dependency and typed reference holds the node itself, and a replacement
        would leave all of them pointing at one that is no longer in the graph.
        Only an optional port is accepted, so every other placement of the same
        stage, and every caller that does not supply it, stays valid.
        """
        self._require_member(host, "stage")
        if host.node_id not in self._children:
            raise CompositionError(
                f"{host.node_id!r} is not a stage placed in pipeline {self.pipeline_id!r}"
            )
        if not port.optional:
            raise CompositionError(
                f"stage {host.node_id!r} can only be given an optional parameter after it "
                f"was placed; {port.name!r} is required, and every caller would break"
            )
        if any(existing.name == port.name for existing in host.inputs):
            raise CompositionError(f"stage {host.node_id!r} already has a parameter {port.name!r}")
        object.__setattr__(host, "inputs", (*host.inputs, port))

    def subscribed_signals(self) -> frozenset[RunSignal]:
        """Every signal any integration here or in a nested stage asked for."""
        return frozenset(s for service in self.all_integrations() for s in service.reports)

    def declare_input(
        self,
        name: str,
        port_type: PortType,
        *,
        required: bool = True,
        default: YamlScalar = None,
        description: str = "",
        prose: bool = False,
    ) -> WorkflowInput:
        """Declare a pipeline-level parameter.

        ``prose`` marks the one input an input file's body feeds. At most one
        per pipeline: the body is a single run of text, so two claims on it is
        an ambiguity nothing downstream can resolve.
        """
        if name in self._inputs:
            raise CompositionError(f"workflow input {name!r} is already declared")
        if prose:
            claimed = next((p for p in self._inputs.values() if p.prose), None)
            if claimed is not None:
                raise CompositionError(
                    f"workflow input {claimed.name!r} already takes the input file's body; "
                    f"{name!r} cannot take it as well"
                )
            if port_type is not PortType.STRING:
                raise CompositionError(
                    f"workflow input {name!r} is {port_type.value}, so it cannot take an "
                    "input file's body, which is text"
                )
        param = WorkflowInput(
            name=name,
            port_type=port_type,
            required=required,
            default=default,
            description=description,
            prose=prose,
        )
        self._inputs[name] = param
        return param

    def set_entry(self, start: RouteEnd) -> None:
        """Pin the entry point rather than letting it fall out of insertion order.

        A parallel group is a legal entry point: Conductor resolves
        ``entry_point`` against groups as well as agents.
        """
        self._require_routable(start, "entry point")
        self._entry = start

    # -- edges -----------------------------------------------------------

    def connect(
        self,
        source: Node,
        from_port: str,
        target: Node,
        to_port: str,
        *,
        when: str | Template | None = None,
    ) -> Edge:
        """Wire an output port to an input port.

        Raises before the edge exists if either node is foreign, either port is
        undeclared, or the port types differ.
        """
        self._require_member(source, "edge source")
        self._require_member(target, "edge target")
        self._reject_gate_source(source)
        self._reject_terminal_source(source)
        self._reject_grouped_source(source)

        out_port = source.get_output(from_port)
        in_port = target.get_input(to_port)
        if not out_port.accepts(in_port):
            raise PortTypeError(
                f"cannot connect {source.node_id}.{out_port.name} "
                f"({out_port.port_type.value}) to {target.node_id}.{in_port.name} "
                f"({in_port.port_type.value}): port types differ"
            )
        if when is None:
            self._reject_second_default_route(source, target)
        edge = Edge(source=source, target=target, when=when)
        self._edges.append(edge)
        self._deps.append(
            DataDep(source=source, target=target, connection=PortConnection(out_port, in_port))
        )
        return edge

    def connect_input(self, param: WorkflowInput, target: Node, to_port: str) -> None:
        """Feed a pipeline parameter into a node's input port."""
        if self._inputs.get(param.name) is not param:
            raise CompositionError(
                f"workflow input {param.name!r} was not declared on pipeline {self.pipeline_id!r}"
            )
        self._require_member(target, "input target")
        in_port = target.get_input(to_port)
        if param.port_type is not in_port.port_type:
            raise PortTypeError(
                f"cannot bind workflow input {param.name!r} ({param.port_type.value}) "
                f"to {target.node_id}.{in_port.name} ({in_port.port_type.value}): "
                "types differ"
            )
        self._input_edges.append((param, target, in_port))

    def feed(
        self,
        source: Node | MapGroup,
        from_port: str,
        target: Node,
        to_port: str,
        *,
        previous_pass: bool = False,
    ) -> DataDep:
        """Declare that ``target`` reads a value from ``source``, with no control edge.

        Needed whenever data and control diverge — most often across a gate. The
        gate decides *where* execution goes; the node it routes to still has to
        read the value produced before the gate, and Conductor will not infer
        that under ``context.mode: explicit``.

        ``previous_pass`` is how one member of a parallel group reads another.
        Ordinarily that is refused, because the two run at once and the
        reference resolves to nothing. Inside a loop it resolves to something
        useful instead: the engine keys a group's result by the group's name and
        overwrites it only when the group next *finishes*, so a member running
        its second pass still sees its siblings' first. Saying so explicitly is
        the point — the value is a round behind, and a caller that did not mean
        that has written a subtle bug. The lint refuses the flag on a graph with
        no loop, where there is no previous pass to read.
        """
        self._require_routable(source, "data source")
        self._require_member(target, "data target")
        if isinstance(source, Node):
            shared = self.group_of(source)
            if shared is not None and shared is self.group_of(target) and not previous_pass:
                raise CompositionError(
                    f"{target.node_id!r} cannot read {source.node_id!r}: both run inside "
                    f"parallel group {shared.group_id!r}, at the same time. A member's "
                    "output is only addressable once the whole group has finished, so the "
                    "reference resolves to nothing while the reader is running. Put the "
                    "reader after the group, or pass previous_pass=True if you mean to "
                    "read what it produced last time round the loop."
                )
        if isinstance(source, Node) and self.map_of(source) is not None:
            mapped = self.map_of(source)
            assert mapped is not None
            raise CompositionError(
                f"{source.node_id!r} is the body of map group {mapped.group_id!r}; its "
                f"results are only ever addressable as the group's aggregate. Read "
                f"{mapped.group_id}.outputs instead — a per-item output has no name of "
                "its own, because there are N of them."
            )
        out_port = source.get_output(from_port)
        in_port = target.get_input(to_port)
        if not out_port.accepts(in_port):
            raise PortTypeError(
                f"cannot feed {source.node_id}.{out_port.name} ({out_port.port_type.value}) "
                f"to {target.node_id}.{in_port.name} ({in_port.port_type.value}): "
                "port types differ"
            )
        dep = DataDep(
            source=source,
            target=target,
            connection=PortConnection(out_port, in_port),
            previous_pass=previous_pass,
        )
        self._deps.append(dep)
        return dep

    def route(
        self, source: RouteEnd, target: EdgeTarget, *, when: str | Template | None = None
    ) -> Edge:
        """Add a control-only edge carrying no data.

        Use this when the target needs nothing from the source. It is a distinct
        method rather than an optional-port variant of ``connect`` so that "no
        data flows here" is a decision in the source, not an omission.
        """
        self._require_routable(source, "edge source")
        if not isinstance(target, _End):
            self._require_routable(target, "edge target")
        if isinstance(source, Node):
            self._reject_gate_source(source)
            self._reject_terminal_source(source)
            self._reject_grouped_source(source)
        if when is None:
            self._reject_second_default_route(source, target)
        edge = Edge(source=source, target=target, when=when)
        self._edges.append(edge)
        return edge

    def branch(self, gate: GateNode, routes: Mapping[str, EdgeTarget]) -> None:
        """Route each of a gate's choices to a target.

        Every declared choice must be routed and no unknown value may appear:
        an unrouted choice is a dead button in the dashboard, and an unknown one
        is a route the human can never reach.
        """
        self._require_member(gate, "branch source")
        declared = {c.value for c in gate.choices}
        given = set(routes)
        missing = sorted(declared - given)
        unknown = sorted(given - declared)
        if missing:
            raise CompositionError(
                f"gate {gate.node_id!r} has unrouted choice(s) {missing}; "
                "every option needs a target or the button does nothing"
            )
        if unknown:
            raise CompositionError(
                f"gate {gate.node_id!r} has no choice(s) {unknown}; "
                f"declared choices are {sorted(declared)}"
            )
        if any(e.source is gate for e in self._edges):
            raise CompositionError(f"gate {gate.node_id!r} has already been branched")
        for choice in gate.choices:
            target = routes[choice.value]
            if isinstance(target, Node):
                self._require_member(target, "branch target")
            self._edges.append(Edge(source=gate, target=target, case=choice.value))

    def branch_on_outcome(self, node: ScopeNode, routes: Mapping[str, EdgeTarget]) -> None:
        """Route each of a scope's outcomes to a target.

        The scope's vocabulary is closed and every exit reports a member of it,
        so a complete mapping needs no fallback route — which is the whole point
        of banning outcome names that JSON-parse into non-strings. Leave one out
        and the run reaches the scope, returns, and stops with nowhere to go.
        """
        self._require_member(node, "branch source")
        declared = set(node.outcomes)
        missing = sorted(declared - set(routes))
        unknown = sorted(set(routes) - declared)
        if missing:
            raise CompositionError(
                f"scope {node.node_id!r} has unrouted outcome(s) {missing}; an unrouted "
                "outcome is a dead end, since the scope returns normally on every exit"
            )
        if unknown:
            raise CompositionError(
                f"scope {node.node_id!r} cannot produce outcome(s) {unknown}; "
                f"declared outcomes are {sorted(declared)}"
            )
        if any(e.source is node for e in self._edges):
            raise CompositionError(f"scope {node.node_id!r} has already been branched")
        for outcome in node.outcomes:
            target = routes[outcome]
            if isinstance(target, Node):
                self._require_member(target, "branch target")
            # `case` records which outcome this edge covers. It is not emitted —
            # the `when` is what Conductor evaluates — but it is what lets the
            # "only conditional routes" lint see that the fan-out is complete.
            self._edges.append(
                Edge(
                    source=node,
                    target=target,
                    case=outcome,
                    when=equals(node.ref(OUTCOME_PORT), outcome),
                )
            )

    ABORT_CASE = "__abort__"

    def abort_route(self, node: QuestionsNode, target: EdgeTarget) -> Edge:
        """Where a person goes if they abandon a set of questions.

        A real edge, not a footnote: without it in the graph, whatever handles
        an abandoned run looks unreachable and the lint says so.
        """
        self._require_member(node, "abort source")
        if not isinstance(target, _End):
            self._require_routable(target, "abort target")
        if any(e.source is node and e.case == self.ABORT_CASE for e in self._edges):
            raise CompositionError(f"{node.node_id!r} already has an abort route")
        edge = Edge(source=node, target=target, case=self.ABORT_CASE)
        self._edges.append(edge)
        return edge

    def expose_output(
        self, name: str, node: Node | MapGroup, from_port: str, *, default: str | None = None
    ) -> None:
        """Publish a node output as part of the pipeline's final result.

        The port type is retained even though Conductor's top-level ``output:``
        is ``dict[str, str]`` — a rendered template with JSON coercion, not a
        typed contract. Keeping the type here is what lets a stage be wired into
        a parent with the same checking as any other edge.
        """
        self._require_routable(node, "output source")
        port = node.get_output(from_port)
        if name in self._outputs:
            raise CompositionError(f"pipeline output {name!r} is already exposed")
        # The source and the port, not a rendered string. How Conductor spells a
        # reference is the backend's business, and building it here got a group
        # member's address wrong — `member.output.field` is a name the engine
        # never binds, so the output rendered empty and the guard was always
        # false. `output_path` already knows the rule; the graph should not.
        self._outputs[name] = ExposedOutput(source=node, port=port, default=default)

    # -- guards ----------------------------------------------------------

    def _require_routable(self, end: RouteEnd, role: str) -> None:
        """A routing endpoint must be a node or a group of this pipeline."""
        if isinstance(end, ParallelGroup):
            if self._groups.get(end.group_id) is not end:
                raise CompositionError(
                    f"{role} {end.group_id!r} is not a parallel group of pipeline "
                    f"{self.pipeline_id!r}"
                )
            return
        if isinstance(end, MapGroup):
            if self._maps.get(end.group_id) is not end:
                raise CompositionError(
                    f"{role} {end.group_id!r} is not a map group of pipeline {self.pipeline_id!r}"
                )
            return
        self._require_member(end, role)

    def _reject_grouped_source(self, source: Node) -> None:
        """A member routes as part of its group, never on its own."""
        group = self.group_of(source)
        if group is not None:
            raise CompositionError(
                f"{source.node_id!r} runs inside parallel group {group.group_id!r} and "
                "cannot have its own outgoing edge; route from the group instead"
            )
        mapped = self.map_of(source)
        if mapped is not None:
            raise CompositionError(
                f"{source.node_id!r} is the body of map group {mapped.group_id!r} and "
                "cannot have its own outgoing edge; a for_each item cannot decide where "
                "the run goes next — route from the group, once every item has finished"
            )

    def _require_member(self, node: Node, role: str) -> None:
        if self._by_id.get(node.node_id) is not node:
            raise CompositionError(
                f"{role} {node.node_id!r} is not part of pipeline {self.pipeline_id!r}; "
                "add() it first (a node from another pipeline is never implicitly shared)"
            )

    @staticmethod
    def _reject_gate_source(source: Node) -> None:
        if source.routes_via_options:
            raise CompositionError(
                f"gate {source.node_id!r} routes through its options; use branch() so every "
                "choice gets a target"
            )

    @staticmethod
    def _reject_terminal_source(source: Node) -> None:
        if not source.accepts_routes:
            raise CompositionError(
                f"{source.node_id!r} is a terminate node and cannot have outgoing edges"
            )

    def _reject_second_default_route(self, source: RouteEnd, target: EdgeTarget) -> None:
        for edge in self._edges:
            if edge.source is source and edge.when is None and edge.case is None:
                tid = "END" if isinstance(target, _End) else target.node_id
                raise CompositionError(
                    f"{source.node_id!r} already has an unconditional route to "
                    f"{edge.describe_target!r}; adding one to {tid!r} would silently drop it "
                    "because Conductor takes the first matching route. Give one a "
                    "`when` condition, or use a gate."
                )

    # -- graph queries ---------------------------------------------------

    @property
    def nodes(self) -> tuple[Node, ...]:
        """Every registered node, in insertion order."""
        return tuple(self._nodes)

    @property
    def edges(self) -> tuple[Edge, ...]:
        """Every edge, in insertion order."""
        return tuple(self._edges)

    @property
    def workflow_inputs(self) -> tuple[WorkflowInput, ...]:
        """Every declared pipeline parameter."""
        return tuple(self._inputs.values())

    @property
    def input_bindings(self) -> tuple[tuple[WorkflowInput, Node, InputPort], ...]:
        """Every parameter-to-port binding."""
        return tuple(self._input_edges)

    @property
    def exposed_outputs(self) -> dict[str, ExposedOutput]:
        """The pipeline's final output templates."""
        return dict(self._outputs)

    @property
    def exposed_output_ports(self) -> tuple[OutputPort, ...]:
        """The pipeline's result as typed ports, for use by a parent pipeline."""
        return tuple(
            OutputPort(name, e.port.port_type, e.port.description, e.port.element)
            for name, e in self._outputs.items()
        )

    @property
    def output_contract_names(self) -> frozenset[str]:
        """Every key a parent can rely on reading back from this workflow.

        A terminal's ``result`` is emitted as ``output_template``, which
        *replaces* the workflow-level ``output:`` on the path that reaches it —
        so the two are alternatives, not layers. A key only counts if every way
        the run can end produces it: one resultless terminal is enough to make
        an ``output:`` key the fallback again, and one terminal that omits a key
        the others carry makes that key absent on a branch nobody tested.
        """
        exposed = frozenset(self._outputs)
        terminals = [n for n in self._nodes if isinstance(n, TerminateNode)]
        settled = [frozenset(n.result) for n in terminals if n.result]
        if not settled or len(settled) != len(terminals):
            return exposed
        return exposed | frozenset.intersection(*settled)

    @property
    def declared_input_ports(self) -> tuple[InputPort, ...]:
        """The pipeline's parameters as typed ports, for use by a parent pipeline."""
        return tuple(
            InputPort(p.name, p.port_type, p.description, optional=not p.required)
            for p in self._inputs.values()
        )

    @property
    def data_deps(self) -> tuple[DataDep, ...]:
        """Every data dependency, in declaration order."""
        return tuple(self._deps)

    def deps_into(self, node: Node) -> list[DataDep]:
        """Data dependencies that ``node`` reads."""
        return [d for d in self._deps if d.target is node]

    def outgoing(self, node: RouteEnd) -> list[Edge]:
        """Edges leaving ``node``, in insertion order."""
        return [e for e in self._edges if e.source is node]

    def inbound(self, node: Node) -> list[Edge]:
        """Edges arriving at ``node``, in insertion order."""
        return [e for e in self._edges if e.target is node]

    def entry(self) -> RouteEnd:
        """Resolve the entry point.

        An explicit ``set_entry`` wins. Otherwise the unique node with no
        inbound edge is used; if that is ambiguous the caller is told to pin it
        rather than having insertion order decide silently.
        """
        if self._entry is not None:
            return self._entry
        if not self._nodes:
            raise CompositionError(f"pipeline {self.pipeline_id!r} has no nodes")
        targeted = {e.target.node_id for e in self._edges if not isinstance(e.target, _End)}
        grouped = {m.node_id for g in self._groups.values() for m in g.members}
        bound = {n.node_id for _, n, _ in self._input_edges}
        roots: list[RouteEnd] = [
            n for n in self._nodes if n.node_id not in targeted and n.node_id not in grouped
        ]
        roots += [g for g in self._groups.values() if g.group_id not in targeted]
        if len(roots) == 1:
            return roots[0]
        if not roots:
            raise CompositionError(
                f"pipeline {self.pipeline_id!r} has no node without an inbound edge; "
                "call set_entry() to pin the entry point"
            )
        preferred = [n for n in roots if n.node_id in bound]
        if len(preferred) == 1:
            return preferred[0]
        names = ", ".join(sorted(n.node_id for n in roots))
        raise CompositionError(
            f"pipeline {self.pipeline_id!r} has {len(roots)} nodes with no inbound edge "
            f"({names}); call set_entry() to pin the entry point"
        )

    def _successors(self, end: RouteEnd) -> Iterable[RouteEnd]:
        """What runs after ``end``.

        Entering a group reaches its members: they have no inbound edge of their
        own, so without this they would look unreachable.
        """
        if isinstance(end, ParallelGroup):
            yield from end.members
        if isinstance(end, MapGroup):
            yield end.body
        for edge in self._edges:
            if edge.source is end and not isinstance(edge.target, _End):
                yield edge.target
        if isinstance(end, Node):
            enclosing: RouteEnd | None = self.group_of(end) or self.map_of(end)
            if enclosing is not None:
                # A member's continuation is whatever the group routes to.
                for edge in self._edges:
                    if edge.source is enclosing and not isinstance(edge.target, _End):
                        yield edge.target

    def reaches(self, start: RouteEnd, goal: RouteEnd) -> bool:
        """Whether ``goal`` is reachable from ``start`` by following edges."""
        seen: set[str] = set()
        stack: list[RouteEnd] = [start]
        while stack:
            current = stack.pop()
            if current.node_id in seen:
                continue
            seen.add(current.node_id)
            for nxt in self._successors(current):
                if nxt is goal:
                    return True
                stack.append(nxt)
        return False

    def reachable_from_entry(self) -> set[str]:
        """Node ids reachable from the entry point."""
        start = self.entry()
        seen: set[str] = {start.node_id}
        stack: list[RouteEnd] = [start]
        while stack:
            for nxt in self._successors(stack.pop()):
                if nxt.node_id not in seen:
                    seen.add(nxt.node_id)
                    stack.append(nxt)
        return seen

    def back_edges(self) -> list[Edge]:
        """Control edges that close a cycle, by depth-first search.

        Detected as edges into a node still on the DFS stack. The cheaper test
        "target can reach source" is wrong: in a two-node loop both edges pass
        it, so the forward edge gets reported as a back edge too.
        """
        state: dict[str, int] = {}
        found: list[Edge] = []

        def visit(node: RouteEnd) -> None:
            state[node.node_id] = 1
            for edge in self.outgoing(node):
                # A group is a routing endpoint like a node: an edge back into
                # one closes a cycle exactly as an edge back into a node does.
                if isinstance(edge.target, _End):
                    continue
                mark = state.get(edge.target.node_id, 0)
                if mark == 1:
                    found.append(edge)
                elif mark == 0:
                    visit(edge.target)
            state[node.node_id] = 2

        roots: list[RouteEnd] = [self.entry(), *self._nodes] if self._nodes else []
        for node in roots:
            if state.get(node.node_id, 0) == 0:
                visit(node)
        return found

    def may_be_unresolved(self, source: RouteEnd, target: RouteEnd) -> bool:
        """Whether ``target`` can run before ``source`` has produced anything.

        True when ``target`` is reachable from the entry point without passing
        through ``source`` — which is exactly when a reference to a
        ``source`` output has to be emitted optional.
        """
        start = self.entry()
        if start is source:
            return False
        if start is target:
            return True

        # Passing a group means every one of its members has run: the group does
        # not route onward until they finish. So a member is a barrier, and so is
        # the group that contains it — without the second, the group's outward
        # edge looks like a way to reach the target with the member skipped, and
        # a perfectly available value gets emitted as optional.
        barriers = {source.node_id}
        if isinstance(source, Node):
            group = self.group_of(source)
            if group is not None:
                barriers.add(group.group_id)

        # The entry can itself be the barrier — a pipeline whose first step is
        # the group. Expanding it would walk straight past the very thing that
        # has to run first.
        if start.node_id in barriers:
            return False

        seen: set[str] = {start.node_id}
        stack: list[RouteEnd] = [start]
        while stack:
            for nxt in self._successors(stack.pop()):
                if nxt.node_id in barriers or nxt.node_id in seen:
                    continue
                if nxt is target:
                    return True
                seen.add(nxt.node_id)
                stack.append(nxt)
        return False

    def has_cycle(self) -> bool:
        """Whether the graph loops."""
        return bool(self.back_edges())

    def has_gate(self) -> bool:
        """Whether any node pauses for a human."""
        return any(isinstance(n, GateNode) for n in self._nodes)

    def require_loop_bound(self) -> None:
        """Refuse a cyclic graph that never says how many times it may go round.

        Generic, not engine-specific: every executor needs a bound, and an
        unbounded loop is an authoring omission rather than a target's problem.
        How the bound is spent is the backend's arithmetic.
        """
        if self.has_cycle() and self.loop_passes is None and self.max_iterations is None:
            raise CompositionError(
                f"pipeline {self.pipeline_id!r} contains a loop but sets no loop_passes; "
                "an unbounded loop has no safe default. Pass loop_passes=N (or an "
                "explicit max_iterations) so the bound is a decision, not an accident."
            )

    def total_cost(self) -> int:
        """Every step in this graph, run once.

        Executions, not nodes: a parallel group costs one per member and a map
        group up to one per item, because the engine charges a group's whole
        fan-out against the same budget as a single step. ``len(nodes)`` is the
        number that reads like a step count and is not one — it prices a
        four-node fan-out over ten items at four.
        """
        grouped = {m.node_id for g in self._groups.values() for m in g.members}
        grouped |= {m.body.node_id for m in self._maps.values()}
        loose = sum(1 for n in self._nodes if n.node_id not in grouped)
        collections: tuple[RouteEnd, ...] = (*self.groups, *self.maps)
        return loose + sum(self.step_cost(g) for g in collections)

    def budget_cost(self) -> int:
        """Step executions this graph can reach, loops included.

        What a run has to be allowed to spend, as opposed to what it usually
        will. Derived here rather than in a backend so the number a person is
        shown before starting and the number compiled into the workflow's own
        limit come from one place and cannot drift apart.

        Callers that need the bound to be a decision rather than a default
        should call ``require_loop_bound`` first; this treats an unbounded loop
        as a single pass.
        """
        if not self.has_cycle():
            return max(1, self.total_cost())
        return max(1, self.total_cost() + self.loop_cost(self.loop_passes or 1))

    def loop_cost(self, passes: int) -> int:
        """Extra step executions the graph's loops buy beyond one pass each.

        Every cycle is priced, not just the costliest: two independent loops each
        run their own passes, and budgeting for one of them force-stops the run
        partway through the other. Nesting multiplies rather than adds — an inner
        loop runs its full count on *every* outer pass — so a cycle contained
        inside ``d`` others is priced at ``passes ** (d + 1)``.

        This can over-price a graph whose loops cannot actually both run to their
        bound. That is the safe direction: an over-estimate above the engine's
        ceiling is refused loudly by ``_within_ceiling``, while an under-estimate
        kills a run mid-pass with nothing to point at.
        """
        loops = self._loops()
        extra = 0
        for header, (cost, _span) in loops.items():
            # Nesting is about headers, not spans. Two back edges reaching the
            # same header are alternative ways round *one* loop — you take one
            # per pass, so their costs do not add. A loop is inside another only
            # when its header sits within the other's body, and only then does
            # it run its full count on every one of the outer's passes.
            depth = sum(1 for h, (_, span) in loops.items() if h != header and header in span)
            extra += cost * (passes ** (depth + 1) - 1)
        return extra

    def _loops(self) -> dict[str, tuple[int, frozenset[str]]]:
        """One entry per loop header: its costliest pass, and every step on it.

        Back edges are grouped by target because that target is the loop header —
        several latches into one header are one loop with several routes round
        it, which is the shape a retry loop with an escalation branch produces.
        """
        loops: dict[str, tuple[int, frozenset[str]]] = {}
        for edge in self.back_edges():
            cost, span = self._cycle_span(edge)
            header = edge.describe_target
            known = loops.get(header)
            if known is None:
                loops[header] = (cost, span)
            else:
                loops[header] = (max(known[0], cost), known[1] | span)
        return loops

    def longest_cycle_length(self) -> int:
        """Step executions on the costliest cycle, or 0 when the graph is acyclic.

        Executions, not nodes: entering a parallel group costs one per member
        and a map group up to one per item, because Conductor charges a group's
        whole fan-out against the same budget as a single step. Counting hops
        instead under-prices every loop that contains a group, and the run dies
        mid-pass on a number nobody chose.

        Exposed so a backend can price a loop in whatever unit it counts.
        """
        return max((self._cycle_span(e)[0] for e in self.back_edges()), default=0)

    def step_cost(self, end: RouteEnd) -> int:
        """How many step executions reaching ``end`` costs."""
        if isinstance(end, ParallelGroup):
            return len(end.members)
        if isinstance(end, MapGroup):
            return end.expect_items
        return 1

    # Enough to exhaust any real graph; past it the estimate falls back to the
    # whole graph's cost, which over-prices rather than silently under-pricing.
    _PATH_BUDGET = 20_000

    def _cycle_span(self, back_edge: Edge) -> tuple[int, frozenset[str]]:
        """The costliest simple cycle closed by ``back_edge``: its cost and its steps.

        The costliest rather than the shortest: a budget derived from the cheap
        way round a branching loop is a budget the expensive way round breaks.
        The step set comes back too, because whether one loop sits inside another
        is what decides if their costs add or multiply.
        """
        if isinstance(back_edge.target, _End):
            return self.step_cost(back_edge.source), frozenset({back_edge.source.node_id})
        start, goal = back_edge.target, back_edge.source
        if start is goal:
            return self.step_cost(start), frozenset({start.node_id})

        best = 0
        span: frozenset[str] = frozenset({start.node_id, goal.node_id})
        visits = 0
        stack: list[tuple[RouteEnd, int, frozenset[str]]] = [
            (start, self.step_cost(start), frozenset({start.node_id}))
        ]
        while stack:
            current, cost, seen = stack.pop()
            visits += 1
            if visits > self._PATH_BUDGET:
                return self.total_cost(), frozenset(n.node_id for n in self._nodes)
            if current is goal:
                if cost > best:
                    best, span = cost, seen
                continue
            for nxt in self._route_successors(current):
                if nxt.node_id in seen:
                    continue
                stack.append((nxt, cost + self.step_cost(nxt), seen | {nxt.node_id}))
        return (best or self.step_cost(goal)), span

    def _route_successors(self, end: RouteEnd) -> Iterable[RouteEnd]:
        """What runs after ``end``, following routes only.

        Unlike ``_successors`` this does not descend into a group's members. A
        member has no routes of its own, so it is never a step *on* a cycle —
        the group is, and its cost already covers everyone inside it.
        """
        source = self.group_of(end) or self.map_of(end) if isinstance(end, Node) else None
        for edge in self._edges:
            if edge.source is (source or end) and not isinstance(edge.target, _End):
                yield edge.target
