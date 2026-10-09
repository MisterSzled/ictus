"""Generic composition rules — true of any graph, whatever executes it.

Rules that depend on one engine's runtime live in
``ictus.interfaces.<engine>.lints``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.errors import CompositionError
from ictus.graph.composition import ABORT_CASE
from ictus.graph.node import GateNode, NodeKind, ScopeNode, SubGraphNode
from ictus.graph.ref import Origin
from ictus.graph.traversal import has_cycle

if TYPE_CHECKING:
    from ictus.graph.composition import RouteEnd
    from ictus.graph.mapping import MapGroup
    from ictus.graph.node import Node
    from ictus.graph.pipeline import Pipeline
    from ictus.graph.ref import Ref
    from ictus.interfaces import Capabilities

# What to call a node in a violation: the word the author typed, rather than
# `NodeKind`'s engine-facing value.
_KIND_NAMES = {
    NodeKind.LLM_CALL: "agent",
    NodeKind.HUMAN_DECISION: "gate",
    NodeKind.SUBPROCESS: "script",
    NodeKind.COMPUTATION: "compute node",
    NodeKind.DELAY: "wait",
    NodeKind.EXIT: "terminal",
    NodeKind.SUB_GRAPH: "stage",
    NodeKind.ASK: "questions",
}


def describe(node: Node) -> str:
    """How a violation names one node: what it is, then which one."""
    if isinstance(node, ScopeNode):
        # A scope is a stage with a closed outcome vocabulary, and the rules
        # treat the two differently.
        return f"scope {node.node_id!r}"
    return f"{_KIND_NAMES.get(node.kind, node.kind.value)} {node.node_id!r}"


PLACEHOLDER = "CHANGE-ME"
"""What `ictus init` writes where a decision has to be made.

A lint rather than a scaffold check: a folder is meant to be unfinished right
after `init`.
"""

__all__ = [
    "capability_problems",
    "describe",
    "group_routing_problems",
    "node_problems",
    "placeholder_problems",
    "previous_pass_problems",
    "reference_problems",
    "stage_contract_problems",
    "undeclared_use_problems",
]


def reference_problems(pipeline: Pipeline, node: Node, where: str) -> list[str]:
    """Resolve every typed reference against the finished graph.

    The node must exist, declare that port, and declare it with the claimed type.
    """
    by_id = {n.node_id: n for n in pipeline.nodes}
    declared_inputs = {p.name: p for p in pipeline.workflow_inputs}
    problems: list[str] = []
    refs = [
        *node.prompt_refs(),
        *node.settled_refs(),
        *(r for edge in pipeline.outgoing(node) for r in edge.condition_refs()),
    ]
    maps = {m.group_id: m for m in pipeline.maps}
    for ref in refs:
        if ref.origin is Origin.LOOP_ITEM:
            # `Item.ref` checked the fields where the reference was written.
            continue
        if ref.source_id in maps:
            problems.extend(_map_reference_problems(maps[ref.source_id], node, ref, where))
            continue
        if ref.from_input:
            param = declared_inputs.get(ref.source_id)
            if param is None:
                known = ", ".join(sorted(declared_inputs)) or "(none)"
                problems.append(
                    f"{where}: {describe(node)} references pipeline input "
                    f"{ref.source_id!r}, which is not declared; declared: {known}"
                )
            elif param.port_type is not ref.port_type:
                problems.append(
                    f"{where}: {describe(node)} reads input {ref.source_id!r} as "
                    f"{ref.port_type.value} but it is declared {param.port_type.value}"
                )
            continue
        target = by_id.get(ref.source_id)
        if target is None:
            known = ", ".join(sorted(by_id)) or "(none)"
            problems.append(
                f"{where}: {describe(node)} references unknown node "
                f"{ref.source_id!r}; nodes in this pipeline: {known}"
            )
            continue
        declared = {p.name: p for p in target.outputs}
        port = declared.get(ref.port)
        if port is None:
            known = ", ".join(sorted(declared)) or "(none declared)"
            problems.append(
                f"{where}: {describe(node)} references {ref.source_id}.{ref.port}, "
                f"which {ref.source_id!r} does not declare; declared outputs: {known}"
            )
        elif port.port_type is not ref.port_type:
            problems.append(
                f"{where}: {describe(node)} reads {ref.source_id}.{ref.port} as "
                f"{ref.port_type.value} but it is declared {port.port_type.value}"
            )
    return problems


def _map_reference_problems(group: MapGroup, node: Node, ref: Ref, where: str) -> list[str]:
    """A reference to a map group's aggregate — outputs, errors or count."""
    try:
        port = group.get_output(ref.port)
    except CompositionError as exc:
        return [f"{where}: {describe(node)} {exc}"]
    if port.port_type is not ref.port_type:
        return [
            f"{where}: {describe(node)} reads {group.group_id}.{ref.port} as "
            f"{ref.port_type.value} but a map group produces {port.port_type.value}"
        ]
    return []


def group_routing_problems(pipeline: Pipeline, group: RouteEnd, where: str) -> list[str]:
    """A group routes like a step, so it can dead-end like one."""
    edges = pipeline.outgoing(group)
    if not edges:
        return [
            f"{where}: group {group.node_id!r} has no outgoing route, so the run stops "
            "there — indistinguishable from a forgotten edge. Route it to a step or END."
        ]
    if all(e.when is not None for e in edges):
        return [
            f"{where}: group {group.node_id!r} has only conditional routes; if none match, "
            "execution has nowhere to go. Add an unconditional route (or route to END)."
        ]
    return []


def placeholder_problems(pipeline: Pipeline, where: str) -> list[str]:
    """Scaffold text left where a decision was supposed to go."""
    problems: list[str] = []
    if PLACEHOLDER in pipeline.pipeline_id:
        problems.append(
            f"{where}: pipeline_id is still {pipeline.pipeline_id!r}. Name the pipeline "
            "before running it — every file it emits is named after this."
        )
    for node in pipeline.nodes:
        if PLACEHOLDER in node.node_id:
            problems.append(f"{where}: {describe(node)} still carries {PLACEHOLDER} in its id")
        if any(PLACEHOLDER in text for text in node.template_strings()):
            problems.append(
                f"{where}: {describe(node)} has {PLACEHOLDER} in its prompt, so the model "
                "would be paid to act on the placeholder"
            )
    return problems


def undeclared_use_problems(
    pipeline: Pipeline, where: str, inherited: frozenset[str] = frozenset()
) -> list[str]:
    """A step reaching something the pipeline never said it reaches.

    What a run touches outside the machine is announced at the top, where
    preflight and the start gate can see it. Checked by name, which is all a
    node is allowed to remember.

    ``inherited`` is what the pipelines above this one declare: a stage is part
    of its caller's run.
    """
    declared = set(inherited)
    declared |= {service.name for service in pipeline.integrations}
    declared |= {source.name for source in pipeline.datasources}
    return [
        f"{where}: {describe(node)} uses {name!r}, which this pipeline does not declare. "
        f"Announce it at the top with integrate() or require_datasource() — otherwise "
        f"preflight checks none of its credentials and the start gate lists none of them"
        for node in pipeline.nodes
        for name in getattr(node, "uses", ())
        if name not in declared
    ]


def previous_pass_problems(pipeline: Pipeline, where: str) -> list[str]:
    """A read of the last pass, on a graph that never takes a second one.

    With no loop there is no previous pass, so the reference renders empty.
    """
    if has_cycle(pipeline):
        return []
    return [
        f"{where}: {dep.target.node_id!r} reads {dep.source.node_id!r} with "
        "previous_pass=True, but this graph has no loop, so there is never a previous "
        "pass and the reference renders empty every time. Drop the flag and put the "
        "reader after the group, or give the graph the loop it was written for."
        for dep in pipeline.data_deps
        if dep.previous_pass
    ]


def node_problems(pipeline: Pipeline, node: Node, where: str) -> list[str]:
    """Problems with one node's place in the graph."""
    problems: list[str] = []
    edges = pipeline.outgoing(node)
    # A map body has no routes of its own, so no edge rule applies to it.
    if pipeline.map_of(node) is not None:
        return reference_problems(pipeline, node, where)
    in_group = pipeline.group_of(node) is not None

    # A scope is the one node whose conditional routes are provably
    # exhaustive: its outcome vocabulary is closed and fully routed.
    exhaustive = isinstance(node, ScopeNode) and {e.case for e in edges} >= set(node.outcomes)
    routed = [e for e in edges if e.case != ABORT_CASE]
    if (
        edges
        and routed
        and not isinstance(node, GateNode)
        and not exhaustive
        and all(e.when is not None for e in routed)
    ):
        problems.append(
            f"{where}: {describe(node)} has only conditional routes once its abort "
            "edge is set aside. An abort is emitted as `abort_route`, not in `routes:`, so "
            "it is not the fallback the answered path needs."
        )
    elif not edges and node.accepts_routes and not in_group:
        # A group member routes as part of its group and has no edge of its own.
        problems.append(
            f"{where}: {describe(node)} has no outgoing route, so it implicitly ends the "
            "run — indistinguishable from a forgotten edge. Use a TerminateNode or route to END."
        )

    problems.extend(reference_problems(pipeline, node, where))

    fed = {d.connection.target.name for d in pipeline.deps_into(node)}
    fed |= {port.name for _, target, port in pipeline.input_bindings if target is node}
    problems.extend(
        f"{where}: {describe(node)} declares required input {port.name!r} "
        "but nothing is wired to it"
        for port in node.inputs
        if not port.optional and port.name not in fed
    )
    return problems


def stage_contract_problems(where: str, host: SubGraphNode, child: Pipeline) -> list[str]:
    """Cross-check a stage's boundary against the workflow it hosts."""
    problems: list[str] = []
    declared = {p.name: p for p in child.declared_input_ports}
    supplied = {p.name: p for p in host.inputs}

    for name in sorted(set(supplied) - set(declared)):
        known = ", ".join(sorted(declared)) or "(none)"
        problems.append(
            f"{where}: stage {host.node_id!r} maps input {name!r}, which workflow "
            f"{child.pipeline_id!r} does not declare; declared inputs: {known}"
        )
    problems.extend(
        f"{where}: stage {host.node_id!r} leaves required input {name!r} of workflow "
        f"{child.pipeline_id!r} unmapped"
        for name in sorted(set(declared) - set(supplied))
        if not declared[name].optional
    )
    problems.extend(
        f"{where}: stage {host.node_id!r} binds {name!r} as {supplied[name].port_type.value} "
        f"but workflow {child.pipeline_id!r} declares {declared[name].port_type.value}"
        for name in sorted(set(declared) & set(supplied))
        if declared[name].port_type is not supplied[name].port_type
    )

    exposed = set(child.output_contract_names)
    problems.extend(
        f"{where}: stage {host.node_id!r} reads output {port.name!r}, which workflow "
        f"{child.pipeline_id!r} does not expose; exposed outputs: "
        f"{', '.join(sorted(exposed)) or '(none)'}"
        for port in host.outputs
        if port.name not in exposed
    )
    return problems


def capability_problems(pipeline: Pipeline, can: Capabilities, where: str) -> list[str]:
    """What this pipeline asks for that the chosen backend cannot supply."""
    unreportable = sorted(signal.value for signal in pipeline.subscribed_signals() - can.signals)
    if not unreportable:
        return []
    reportable = ", ".join(sorted(s.value for s in can.signals)) or "(none)"
    return [
        f"{where}: integration(s) subscribe to {unreportable}, which {can.name} cannot "
        f"report, so they would be configured and never fire; it reports: {reportable}"
    ]
