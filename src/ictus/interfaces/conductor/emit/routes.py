"""Lowering a route: ``$end``, a case, a gate's options.

One decision here is load-bearing and is made for the author rather than left
to them: **conditional routes are emitted before unconditional ones.**
Conductor takes the first matching route, so a catch-all written first would
shadow every condition after it. Sorting by conditionality means authoring
order cannot create that bug.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.errors import EmitError
from ictus.graph.composition import ABORT_CASE
from ictus.interfaces.conductor.emit.templates import render

if TYPE_CHECKING:
    from ictus.graph.composition import Edge, RouteEnd
    from ictus.graph.node import GateNode
    from ictus.graph.pipeline import Pipeline
    from ictus.graph.values import YamlDict, YamlValue

__all__ = ["END_MARKER", "gate_options", "route_entries", "route_target"]

# Conductor's terminal route marker. The graph says an edge ends the run; this
# is the only place that says how that is written down.
END_MARKER = "$end"


def route_target(edge: Edge) -> str:
    """Spell an edge's target the way Conductor names route destinations."""
    target = edge.target_node
    return END_MARKER if target is None else target.node_id


def gate_options(pipeline: Pipeline, gate: GateNode) -> list[YamlValue]:
    by_case = {edge.case: edge for edge in pipeline.outgoing(gate)}
    options: list[YamlValue] = []
    for choice in gate.choices:
        edge = by_case.get(choice.value)
        if edge is None:
            raise EmitError(
                f"gate {gate.node_id!r} was never branched: choice {choice.value!r} has "
                "no route. Call pipeline.branch(gate, {...}) before emitting."
            )
        option: YamlDict = {
            "label": choice.label,
            "value": choice.value,
            "route": route_target(edge),
        }
        if choice.prompt_for is not None:
            option["prompt_for"] = choice.prompt_for
        if choice.multiline:
            option["multiline"] = True
        options.append(option)
    return options


def route_entries(pipeline: Pipeline, node: RouteEnd) -> list[YamlValue]:
    # An abort is carried by `abort_route`, not by the route list. Emitting it in
    # both places leaves a second unconditional route that can never be taken.
    edges = [e for e in pipeline.outgoing(node) if e.case != ABORT_CASE]
    conditional = [e for e in edges if e.when is not None]
    unconditional = [e for e in edges if e.when is None]
    routes: list[YamlValue] = []
    for edge in (*conditional, *unconditional):
        route: YamlDict = {"to": route_target(edge)}
        if edge.when is not None:
            route["when"] = render(pipeline, node, edge.when)
        routes.append(route)
    return routes
