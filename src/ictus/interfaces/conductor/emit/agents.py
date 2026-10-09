"""Lowering one node to one entry in Conductor's ``agents:`` list.

One decision here is load-bearing and is made for the author rather than left
to them.

A reference that may not have resolved yet is emitted optional. On the first
  pass through a loop the upstream node has not run, and under
  ``context.mode: explicit`` an unresolvable reference fails at the step
  boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.graph.composition import DataDep, FailureMode
from ictus.graph.node import (
    GateNode,
    Node,
    SubGraphNode,
)
from ictus.graph.ports import PortType
from ictus.graph.traversal import may_be_unresolved
from ictus.interfaces.conductor.emit.fields import (
    CONDUCTOR_TYPE,
    kind_fields,
    render_output_schema,
)
from ictus.interfaces.conductor.emit.routes import gate_options, route_entries
from ictus.interfaces.conductor.emit.templates import (
    guard_test,
    output_path,
)

if TYPE_CHECKING:
    from ictus.graph.pipeline import Pipeline
    from ictus.graph.values import YamlDict

__all__ = ["agent_entry"]


def agent_entry(pipeline: Pipeline, node: Node, system_prompt: str | None = None) -> YamlDict:
    agent: YamlDict = {"name": node.node_id}
    if node.description:
        agent["description"] = node.description
    agent["type"] = CONDUCTOR_TYPE[node.kind]
    agent.update(kind_fields(pipeline, node, system_prompt))

    refs = input_refs(pipeline, node)
    if refs:
        agent["input"] = list(refs)
    if node.emits_output_schema and node.outputs:
        agent["output"] = render_output_schema(node.outputs)

    if isinstance(node, SubGraphNode):
        mapping = input_mapping(pipeline, node)
        if mapping:
            agent["input_mapping"] = mapping

    if isinstance(node, GateNode):
        agent["options"] = gate_options(pipeline, node)
    elif node.accepts_routes:
        edges = route_entries(pipeline, node)
        if edges:
            agent["routes"] = edges
    return agent


def input_refs(pipeline: Pipeline, node: Node) -> list[str]:
    refs: list[str] = []
    for param, target, port in pipeline.input_bindings:
        if target is node:
            optional = port.optional or not param.required
            refs.append(f"workflow.input.{param.name}" + ("?" if optional else ""))
    for dep in pipeline.deps_into(node):
        ref = output_path(pipeline, dep.source, dep.connection.source.name)
        if _may_be_absent(pipeline, node, dep):
            ref += "?"
        refs.append(ref)
    seen: set[str] = set()
    unique: list[str] = []
    for ref in refs:
        if ref not in seen:
            seen.add(ref)
            unique.append(ref)
    return unique


def _may_be_absent(pipeline: Pipeline, node: Node, dep: DataDep) -> bool:
    """Whether this dependency can be missing when the target's context is built.

    ``_add_agent_input`` raises ``KeyError`` for a required entry whose path is
    not there (engine/context.py), which happens *before* a template guard can
    run. Three ways a value can be legitimately missing, and only the first two
    were asked about:

    * the author declared the port optional, or the source may not have run;
    * the source is a group member and the group is allowed to finish with a
      member that failed — ``continue_on_error`` is unusable otherwise;
    * the source's own spelling says the field is conditional, as a gate's
      free-text answer is on every branch that did not ask for one.
    """
    if dep.connection.target.optional or may_be_unresolved(pipeline, dep.source, node):
        return True
    if isinstance(dep.source, Node):
        if dep.source.guard_depth(dep.connection.source.name) > 0:
            return True
        group = pipeline.group_of(dep.source)
        if group is not None and group.failure_mode is not FailureMode.FAIL_FAST:
            return True
    return False


def input_mapping(pipeline: Pipeline, node: SubGraphNode) -> YamlDict:
    """Bind the parent's values to the child's declared parameters.

    Derived from the same edges that produced ``input:``, so a stage is wired
    with ordinary ``connect``/``feed`` calls and the parameter binding cannot
    drift from the data dependency it was meant to express.
    """
    mapping: YamlDict = {}
    for param, target, port in pipeline.input_bindings:
        if target is node:
            # Rendered to text and parsed back like every other boundary, so every
            # type goes through `| tojson`. A structured parameter otherwise
            # arrives as a Python repr json.loads cannot read; a boolean as
            # "True", which it cannot read either; and a string that happens to
            # look like a number — a Slack timestamp, an id with leading zeros —
            # is parsed into one, and "1700000000.000100" reaches the child as
            # 1700000000.0001.
            mapping[port.name] = "{{ workflow.input." + param.name + " | tojson }}"
    for dep in pipeline.deps_into(node):
        expression = output_path(pipeline, dep.source, dep.connection.source.name)
        rendered = "{{ " + expression + " | tojson }}"
        if _may_be_absent(pipeline, node, dep):
            # The mapping is rendered against the same explicit context as the
            # step's own templates, so a source that may not have run is an
            # undefined variable here too — and the engine turns that into an
            # ExecutionError rather than an empty string.
            empty = _JSON_EMPTY[dep.connection.source.port_type]
            rendered = (
                "{% if "
                + guard_test(pipeline, dep.source.ref(dep.connection.source.name))
                + " %}"
                + rendered
                + "{% else %}"
                + empty
                + "{% endif %}"
            )
        mapping[dep.connection.target.name] = rendered
    return mapping


# What an absent value becomes on the way into a child, as JSON like every other
# value crossing that boundary. A missing key is not the same as an empty one,
# but the child declared the parameter, so something of the right type has to
# arrive.


_JSON_EMPTY = {
    PortType.STRING: '""',
    PortType.OBJECT: "{}",
    PortType.ARRAY: "[]",
    PortType.NUMBER: "0",
    PortType.BOOLEAN: "false",
}
