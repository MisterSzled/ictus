"""The composition graph.

``Pipeline`` owns every edge; nodes never point at each other. Invalid state is
rejected at the call that introduces it, not at emission.

The records it holds are in ``composition.py`` and the analysis that walks a
finished graph is in ``traversal.py``. What is left is the stores, and the
refusal guarding each, in ten bands:

    the registry             what is in this graph, and what runs beside what
    what the run needs       MCP servers, commands, data sources
    who hears about it       services a run reports to, messages that start one
    start policy             where the run begins, what goes before the gate
    parameters and results   what it takes in, what a parent reads back
    wiring                   connect · connect_input · feed · route
    branching                a complete fan-out from one source
    splicing                 rewriting a graph its author already finished
    refusals                 every guard the bands above call
    reading the graph back   accessors, each returning a copy

Nothing above ``refusals`` is read-only and nothing below it mutates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.errors import CompositionError, PortTypeError
from ictus.graph.composition import (
    ABORT_CASE,
    END,
    DataDep,
    Edge,
    ExposedOutput,
    FailureMode,
    Listener,
    ParallelGroup,
    WorkflowInput,
    _End,
)
from ictus.graph.mapping import Item, MapGroup
from ictus.graph.node import (
    OUTCOME_PORT,
    Node,
    NodeKind,
    QuestionsNode,
    ScopeNode,
    SubGraphNode,
    TerminateNode,
)
from ictus.graph.ports import InputPort, OutputPort, PortConnection, PortType
from ictus.graph.ref import Ref, Template, equals

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from ictus.graph.composition import (
        BudgetMode,
        ContextMode,
        EdgeTarget,
        NativeTools,
        RouteEnd,
        TrimStrategy,
    )
    from ictus.graph.node import GateNode
    from ictus.graph.requirements import Datasource, Executable, Integration, McpServer
    from ictus.graph.signals import RunSignal
    from ictus.graph.values import YamlScalar

__all__ = ["Pipeline"]

# Conductor permits only these inside a parallel group
# (config/validator.py, in `_validate_parallel_groups`). Kept beside
# `parallel()`, its only reader, the way `mapping.py` keeps `_ITERABLE`.
_GROUPABLE = frozenset({NodeKind.LLM_CALL, NodeKind.COMPUTATION})


class Pipeline:
    """A workflow graph plus the run-level settings Conductor needs.

    ``loop_passes`` is mandatory once the graph contains a cycle.
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
        # None means "whatever the backend defaults to".
        self.provider = provider
        self.default_model = default_model
        self.native_tools: NativeTools | None = None
        """Whether a step naming no tools gets the engine's built-in set. Off unless set."""
        self.context_mode: ContextMode = context_mode
        self.context_max_tokens = context_max_tokens
        """A soft ceiling on accumulated context, above which the engine trims.

        Per workflow file, so per stage. There is no per-node equivalent.
        """

        self.context_trim = context_trim
        """How the engine makes room once the ceiling is reached.

        Unset, the engine uses ``drop_oldest``.
        """
        self.loop_passes = loop_passes
        self.budget_usd = budget_usd
        self.budget_mode: BudgetMode = budget_mode
        self.max_iterations = max_iterations
        # Wall-clock ceiling on the whole run, as against a step's own
        # `timeout_seconds`, which bounds one model call.
        self.timeout_seconds = timeout_seconds
        self.metadata: dict[str, str] = dict(metadata or {})
        # Prepended to every step's prompt. The engine loads no CLAUDE.md,
        # skills or hooks, so this is a step's only project context.
        self.instructions: list[str] = list(instructions)
        # Applied to every model call that does not set its own. Unset, the
        # engine sends an empty system prompt, not a default one.
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

        Set from ``config.yaml``; travels in the listen manifest. On by default.
        """
        self._before_start: Node | None = None
        self._entry: RouteEnd | None = None

    # -- the registry --------------------------------------------------------

    def add[N: Node](self, node: N) -> N:
        """Register a node. Returns it unchanged, keeping the concrete type."""
        existing = self._by_id.get(node.node_id)
        if existing is not None:
            # Said before `_claim`, which knows the name is taken but not by
            # what: only here is the other holder the very object passed in.
            what = "the same node twice" if existing is node else "two nodes"
            raise CompositionError(
                f"pipeline {self.pipeline_id!r} would register {what} under node_id "
                f"{node.node_id!r}; ids are the routing keyspace and must be unique"
            )
        self._claim(node.node_id, f"node {node.node_id!r}")
        self._nodes.append(node)
        self._by_id[node.node_id] = node
        return node

    def add_subgraph(self, node: SubGraphNode, body: Pipeline) -> SubGraphNode:
        # `subgraph`, not `subworkflow`: the graph's own noun for this is
        # SUB_GRAPH, and "workflow" is what Conductor calls the file it compiles
        # to. The engine's spelling on a public method of the composition model
        # is the defect AGENTS.md names; test_boundaries.py now refuses it.
        """Register a nested workflow and the pipeline it compiles from.

        The parent spends exactly one iteration on it.
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

        Members must already be registered here. The group costs its member
        count against the iteration budget.
        """
        self._claim(group_id, f"parallel group {group_id!r}")
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
        """Run ``body`` once per element of ``source``, whose width is known only at run time.

        ``body`` gets no routes of its own and is not emitted in ``agents:``; it
        becomes the group's inline template.
        """
        self._claim(group_id, f"map group {group_id!r}")
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
        return self._maps.get(group_id)

    @property
    def groups(self) -> tuple[ParallelGroup, ...]:
        """Every parallel group, in declaration order."""
        return tuple(self._groups.values())

    def group_of(self, node: Node) -> ParallelGroup | None:
        """The group ``node`` runs inside, if any.

        A member's output is read through the group; the direct form renders empty.
        """
        for group in self._groups.values():
            if any(m is node for m in group.members):
                return group
        return None

    # -- what the run needs --------------------------------------------------

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

    def all_mcp_servers(self) -> tuple[McpServer, ...]:
        """This pipeline's servers and those of every stage it contains."""
        seen: dict[str, McpServer] = {s.name: s for s in self._mcp.values()}
        for child in self._children.values():
            for server in child.all_mcp_servers():
                seen.setdefault(server.name, server)
        return tuple(seen.values())

    def require_executable(self, tool: Executable) -> Executable:
        """Declare a command that must be reachable before this pipeline runs.

        Checked by preflight, so a missing tool refuses the launch.
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
        """This pipeline's commands and those of every stage it contains."""
        seen: dict[str, Executable] = dict(self._executables)
        for child in self._children.values():
            for tool in child.all_executables():
                seen.setdefault(tool.name, tool)
        return tuple(seen.values())

    def require_datasource(self, source: Datasource) -> Datasource:
        """Declare somewhere this pipeline reads data from.

        The source's own ``needs`` are registered as executables alongside it.
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

    # -- who hears about it --------------------------------------------------

    def integrate(
        self, service: Integration, *, thread: WorkflowInput | None = None
    ) -> Integration:
        """Declare a third-party service this pipeline talks to.

        ``thread`` names the input an already-open conversation arrives in;
        without one a run opens its own. Declaring is not attaching — nodes are
        inserted when the pipeline is loaded, from the signals the service names.
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

    def listen_on(
        self,
        service: Integration | None = None,
        *,
        prefix: str,
        into: WorkflowInput,
    ) -> Listener:
        """Declare that a message starts this pipeline.

        ``prefix`` marks a message as a request; whatever follows it becomes
        ``into``.

        ``service`` is where the run reports back, and must be integrated
        first; the conversation then comes from that same
        ``integrate(thread=...)``, never from a second argument here, so the
        two can never disagree about where a run answers.

        **Omit it when the pipeline reports nowhere.** Being startable is not a
        reason to hold a credential: the listener reads the channel with its
        own, and a run that says nothing there needs none of its own. Naming a
        service in order to be started was how a pipeline came to declare a
        token it never used, which `ictus preflight` and the listener then both
        refused to proceed without.
        """
        if service is not None and service.name not in self._integrations:
            raise CompositionError(
                f"pipeline {self.pipeline_id!r} listens on {service.name!r} without "
                "integrating it; integrate() it first so one declaration says both "
                "what starts a run and where it reports — or drop the argument, if "
                "it reports nowhere and only needs starting"
            )
        if service is not None and not service.listens:
            raise CompositionError(
                f"integration {service.name!r} cannot be listened on — it can only be "
                "written to. Use a constructor that holds a credential for waiting, "
                "or start this pipeline some other way"
            )
        # "" keys the serviceless one, of which there is likewise only ever one:
        # a pipeline answers to one way of being asked for.
        key = service.name if service is not None else ""
        where = f"on {service.name!r}" if service is not None else "for a message"
        if not prefix.strip():
            raise CompositionError(
                f"pipeline {self.pipeline_id!r} listens {where} with a blank prefix, "
                "which every message matches"
            )
        existing = self._listeners.get(key)
        if existing is not None:
            raise CompositionError(
                f"pipeline {self.pipeline_id!r} already listens {where}; one prefix starts it"
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
        thread = self._threads.get(service.name) if service is not None else None
        if thread is not None and thread.name == into.name:
            raise CompositionError(
                f"pipeline {self.pipeline_id!r} would put the question and the "
                f"conversation both in {into.name!r}; they are two values"
            )
        listener = Listener(service=service, prefix=prefix, into=into, thread=thread)
        self._listeners[key] = listener
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
        """This pipeline's services and those of every stage it contains."""
        seen: dict[str, Integration] = dict(self._integrations)
        for child in self._children.values():
            for service in child.all_integrations():
                seen.setdefault(service.name, service)
        return tuple(seen.values())

    def subscribed_signals(self) -> frozenset[RunSignal]:
        """Every signal any integration here or in a nested stage asked for."""
        return frozenset(s for service in self.all_integrations() for s in service.reports)

    # -- start policy --------------------------------------------------------

    def set_entry(self, start: RouteEnd) -> None:
        """Pin the entry point rather than letting it fall out of insertion order.

        A parallel group is a legal entry point.
        """
        self._require_routable(start, "entry point")
        self._entry = start

    def before_start_gate[N: Node](self, node: N) -> N:
        """Run ``node`` first, ahead of the confirmation gate.

        Wire nothing: placement is done by whoever applies the start policy, so
        it holds whether the gate is on or off. One node per pipeline.
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

    # -- parameters and results ----------------------------------------------

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

        ``prose`` marks the one input an input file's body feeds; at most one
        per pipeline, and it must be a string.
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

    @property
    def workflow_inputs(self) -> tuple[WorkflowInput, ...]:
        """Every declared pipeline parameter."""
        return tuple(self._inputs.values())

    @property
    def declared_input_ports(self) -> tuple[InputPort, ...]:
        """The pipeline's parameters as typed ports, for use by a parent pipeline."""
        return tuple(
            InputPort(p.name, p.port_type, p.description, optional=not p.required)
            for p in self._inputs.values()
        )

    def expose_output(
        self, name: str, node: Node | MapGroup, from_port: str, *, default: str | None = None
    ) -> None:
        """Publish a node output as part of the pipeline's final result.

        The port type is kept so a stage can be wired into a parent and checked
        like any other edge, though Conductor's ``output:`` is ``dict[str, str]``.
        """
        self._require_routable(node, "output source")
        port = node.get_output(from_port)
        if name in self._outputs:
            raise CompositionError(f"pipeline output {name!r} is already exposed")
        # Source and port, not a rendered string: how a reference is spelled is
        # the backend's business, and `output_path` already knows the rule.
        self._outputs[name] = ExposedOutput(source=node, port=port, default=default)

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

        A terminal's ``result`` replaces the workflow ``output:`` on its path,
        so a key counts only if every way the run can end produces it.
        """
        exposed = frozenset(self._outputs)
        terminals = [n for n in self._nodes if isinstance(n, TerminateNode)]
        settled = [frozenset(n.result) for n in terminals if n.result]
        if not settled or len(settled) != len(terminals):
            return exposed
        return exposed | frozenset.intersection(*settled)

    # -- wiring --------------------------------------------------------------

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

        For wherever data and control diverge, most often across a gate.

        ``previous_pass`` lets one member of a parallel group read another, and
        is only valid inside a loop: the value is a round behind.
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
        """Add a control-only edge carrying no data."""
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

    # -- branching -----------------------------------------------------------

    def branch(self, gate: GateNode, routes: Mapping[str, EdgeTarget]) -> None:
        """Route each of a gate's choices to a target.

        Every declared choice must be routed, and no unknown value may appear.
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

        The outcome vocabulary is closed, so the mapping must be complete.
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
            # `case` is not emitted; `when` is what Conductor evaluates. It is
            # how the lint sees that the fan-out is complete.
            self._edges.append(
                Edge(
                    source=node,
                    target=target,
                    case=outcome,
                    when=equals(node.ref(OUTCOME_PORT), outcome),
                )
            )

    def abort_route(self, node: QuestionsNode, target: EdgeTarget) -> Edge:
        """Where a person goes if they abandon a set of questions."""
        self._require_member(node, "abort source")
        if not isinstance(target, _End):
            self._require_routable(target, "abort target")
        if any(e.source is node and e.case == ABORT_CASE for e in self._edges):
            raise CompositionError(f"{node.node_id!r} already has an abort route")
        edge = Edge(source=node, target=target, case=ABORT_CASE)
        self._edges.append(edge)
        return edge

    # -- splicing ------------------------------------------------------------

    def insert_before[N: Node](self, existing: Node, inserted: N) -> N:
        """Put ``inserted`` in front of ``existing``, taking over its inbound edges.

        The entry point moves too when ``existing`` was it.
        """
        self._require_member(existing, "insertion point")
        self.add(inserted)
        self._splice_before(lambda edge: edge.target_node is existing, inserted, existing)
        if self._entry is existing:
            self._entry = inserted
        return inserted

    def insert_before_end[N: Node](self, inserted: N) -> N:
        """Put ``inserted`` on every way this graph reaches the end of the run.

        Each edge keeps its case, so a gate branch still means what it meant.
        """
        finishing = [edge for edge in self._edges if edge.is_end]
        if not finishing:
            raise CompositionError(
                f"pipeline {self.pipeline_id!r} has no route to END to put "
                f"{inserted.node_id!r} in front of"
            )
        self.add(inserted)
        self._splice_before(lambda edge: edge.is_end, inserted, END)
        return inserted

    def _splice_before(
        self, matching: Callable[[Edge], bool], inserted: Node, onward: EdgeTarget
    ) -> None:
        """Redirect every edge ``matching`` at ``inserted``, then route it onward.

        Rebind the whole list before appending, never after: the comprehension
        rewrites every edge it is given, so an edge appended first is rewritten
        onto itself and ``inserted`` ends up routing to itself instead of to
        whatever it was put in front of. Both callers had their own copy of
        this and neither said so.
        """
        self._edges = [
            Edge(source=edge.source, target=inserted, case=edge.case, when=edge.when)
            if matching(edge)
            else edge
            for edge in self._edges
        ]
        self._edges.append(Edge(source=inserted, target=onward))

    def prepend[N: Node](self, node: N) -> N:
        """Run ``node`` before whatever this graph currently starts with.

        The old entry keeps its inbound edges, so ``node`` runs once.
        """
        # Resolved before the add: `entry()` finds the unique node with no
        # inbound edge, and `node` is about to be a second one. Add first and
        # this raises "has 2 nodes with no inbound edge" on every graph that
        # had not pinned its entry.
        first = self.entry()
        self.add(node)
        self.route(node, first)
        self.set_entry(node)
        return node

    def widen_subgraph(self, host: SubGraphNode, port: InputPort) -> None:
        """Give a stage already placed here one more parameter.

        For attachment after placement, not for authoring. Mutates the frozen
        node in place, since edges and refs hold the node itself. Optional
        ports only, so other placements of the same stage stay valid.
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

    # -- refusals ------------------------------------------------------------

    def _claim(self, name: str, what: str) -> None:
        """Take a name in the one keyspace nodes and groups share.

        Each of the three registrars said so in its own error message and none
        of them checked all three stores, so the same collision was refused in
        one order and accepted in the other: ``parallel("p", ...)`` then
        ``add(node("p"))`` left the graph holding both, and every route to
        ``"p"`` then resolved to whichever the backend looked up first.
        """
        holder = (
            "a node"
            if name in self._by_id
            else "a parallel group"
            if name in self._groups
            else "a map group"
            if name in self._maps
            else None
        )
        if holder is not None:
            raise CompositionError(
                f"pipeline {self.pipeline_id!r} already has something named {name!r} "
                f"({holder}), so {what} cannot have it too; "
                "groups and nodes share one routing keyspace"
            )

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

    def _require_member(self, node: Node, role: str) -> None:
        if self._by_id.get(node.node_id) is not node:
            raise CompositionError(
                f"{role} {node.node_id!r} is not part of pipeline {self.pipeline_id!r}; "
                "add() it first (a node from another pipeline is never implicitly shared)"
            )

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

    # -- reading the graph back ----------------------------------------------

    @property
    def nodes(self) -> tuple[Node, ...]:
        """Every registered node, in insertion order."""
        return tuple(self._nodes)

    @property
    def edges(self) -> tuple[Edge, ...]:
        """Every edge, in insertion order."""
        return tuple(self._edges)

    @property
    def input_bindings(self) -> tuple[tuple[WorkflowInput, Node, InputPort], ...]:
        """Every parameter-to-port binding."""
        return tuple(self._input_edges)

    @property
    def data_deps(self) -> tuple[DataDep, ...]:
        """Every data dependency, in declaration order."""
        return tuple(self._deps)

    def deps_into(self, node: Node) -> list[DataDep]:
        return [d for d in self._deps if d.target is node]

    def outgoing(self, node: RouteEnd) -> list[Edge]:
        """Edges leaving ``node``, in insertion order."""
        return [e for e in self._edges if e.source is node]

    def entry(self) -> RouteEnd:
        """Resolve the entry point.

        ``set_entry`` wins; otherwise the unique node with no inbound edge.
        Ambiguity is an error rather than a silent choice.
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
