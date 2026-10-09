"""Rendering typed references into Conductor's Jinja dialect.

Where a ``Ref`` becomes ``{{ plan.output.plan }}``. Decides the spelling and
emits the guard, since Conductor renders with strict undefined and
``Pipeline.may_be_unresolved`` already knows which references are deferred.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ictus.graph.mapping import MapGroup
from ictus.graph.node import Node
from ictus.graph.ports import PortType
from ictus.graph.ref import (
    AtLeast,
    Comparison,
    Every,
    Origin,
    Ref,
    Template,
    TemplatePart,
)
from ictus.graph.traversal import may_be_unresolved

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ictus.graph.composition import RouteEnd
    from ictus.graph.pipeline import Pipeline
    from ictus.graph.values import YamlDict

__all__ = [
    "guard_test",
    "output_block",
    "output_path",
    "reference_path",
    "render",
    "render_settled",
]


def resolve(pipeline: Pipeline, ref: Ref) -> Node | None:
    """The node a reference names, resolved against the finished graph.

    A forward reference carries no node, having been written before its target.
    """
    if isinstance(ref.source, Node):
        return ref.source
    if ref.origin is not Origin.NODE:
        # A workflow input and a loop item are context roots, not steps.
        return None
    return next((n for n in pipeline.nodes if n.node_id == ref.source_id), None)


_STRUCTURED = frozenset({PortType.OBJECT, PortType.ARRAY})


def reference_path(pipeline: Pipeline, ref: Ref) -> str:
    """How Conductor addresses the value this reference names."""
    if ref.origin is Origin.WORKFLOW_INPUT:
        return f"workflow.input.{ref.source_id}"
    if ref.origin is Origin.LOOP_ITEM:
        # A loop variable is a context root, so there is no `.output.` in it.
        return f"{ref.source_id}.{ref.port}" if ref.port else ref.source_id
    mapped = pipeline.map_named(ref.source_id)
    if mapped is not None:
        # A for-each group stores one aggregate under its own name.
        return f"{ref.source_id}.{ref.port}"
    source = resolve(pipeline, ref)
    if source is None:
        # A forward reference: the node's own spelling rule is unavailable.
        return f"{ref.source_id}.output.{ref.port}"
    # A member's output is addressed through its group; the direct form
    # validates and renders empty.
    return output_path(pipeline, source, ref.port)


def output_path(pipeline: Pipeline, source: Node | MapGroup, port_name: str) -> str:
    """How Conductor addresses one output port of one node.

    An empty ``output_ref`` is the node's whole output, so the trailing dot goes.
    """
    path = source.output_ref(port_name)
    if isinstance(source, MapGroup):
        return f"{source.group_id}.{path}"
    group = pipeline.group_of(source)
    root = (
        f"{group.group_id}.outputs.{source.node_id}"
        if group is not None
        else f"{source.node_id}.output"
    )
    return f"{root}.{path}" if path else root


# Types whose rendered text `_maybe_parse_json` can read back as something
# else. STRING included: "0700", "true" and "[a]" all retype without `| tojson`.
_RETYPED = frozenset({PortType.STRING, PortType.OBJECT, PortType.ARRAY})


def output_block(pipeline: Pipeline) -> YamlDict:
    """The workflow's final ``output:`` map."""
    out: YamlDict = {}
    for name, exposed in pipeline.exposed_outputs.items():
        path = output_path(pipeline, exposed.source, exposed.port.name)
        retyped = exposed.port.port_type in _RETYPED
        if retyped:
            path += " | tojson"
        expression = "{{ " + path + " }}"
        if exposed.default is not None:
            # The attribute chain raises before any filter runs, so the guard
            # rescues this, not `| default()`. Both branches must render the
            # same JSON or the value's type depends on which one ran.
            root = _root_of(pipeline, exposed.source)
            fallback = (
                json.dumps(exposed.default)
                if exposed.port.port_type is PortType.STRING
                else exposed.default
            )
            expression = _guarded(f"{root} is defined", expression, fallback)
        out[name] = expression
    return out


def _root_of(pipeline: Pipeline, source: Node | MapGroup) -> str:
    """The context name a guard must test for this source."""
    if isinstance(source, Node):
        group = pipeline.group_of(source)
        if group is not None:
            return group.group_id
    return source.node_id


def render(pipeline: Pipeline, node: RouteEnd, value: str | Template) -> str:
    """Render a prompt for ``node``. A plain string is emitted unchanged."""
    if isinstance(value, str):
        return value
    return "".join(_part(pipeline, node, part, frozenset()) for part in value.parts)


def render_settled(pipeline: Pipeline, node: RouteEnd, value: str | Template) -> str:
    """Render a value whose rendered text Conductor parses back with ``json.loads``.

    ``output_template``, ``input_mapping`` and the workflow ``output:`` map all
    round-trip through JSON, so a lone structured reference gets ``| tojson``.
    A fallback renders JSON too, so both branches arrive as the same type.
    """
    if isinstance(value, Template) and len(value.parts) == 1:
        part = value.parts[0]
        if isinstance(part, Ref) and part.port_type in _STRUCTURED:
            live = "{{ " + reference_path(pipeline, part) + " | tojson }}"
            # Always defined, so there is no branch for a fallback to sit in.
            if not _needs_guard(pipeline, node, part):
                return live
            # Raw, not quoted: a structured fallback is already a JSON literal.
            return _guarded(guard_test(pipeline, part), live, part.fallback)
    return render(pipeline, node, value)


def _part(
    pipeline: Pipeline,
    node: RouteEnd,
    part: TemplatePart,
    guarded: frozenset[str],
) -> str:
    """Render one part. ``guarded`` names sources an enclosing block already covers."""
    if isinstance(part, str):
        return part
    if isinstance(part, Template):
        return "".join(_part(pipeline, node, inner, guarded) for inner in part.parts)
    if isinstance(part, Comparison):
        operator = "!=" if part.negated else "=="
        path = reference_path(pipeline, part.ref)
        # One spelling per value type; bool first, being a subclass of int.
        # `| int` because an unfiltered `0 == 0` against rendered text is false
        # rather than an error. A bool renders Jinja's bare literal.
        if isinstance(part.value, bool):
            test = f"{path} {operator} {str(part.value).lower()}"
        elif isinstance(part.value, int):
            test = f"{path} | int {operator} {part.value}"
        else:
            test = f"{path} {operator} '{part.value}'"
        return _condition(pipeline, node, test, (part.ref,))
    if isinstance(part, Every):
        # `not (a and b)`, which stays correct for any number of references.
        joined = " and ".join(reference_path(pipeline, r) for r in part.refs_)
        body = f"not ({joined})" if part.negated else joined
        return _condition(pipeline, node, body, part.refs_)
    if isinstance(part, AtLeast):
        path = reference_path(pipeline, part.ref)
        return _condition(pipeline, node, f"{path} | int >= {part.threshold}", (part.ref,))
    if isinstance(part, Ref):
        expression = _expression(reference_path(pipeline, part), part.fallback)
        test = guard_test(pipeline, part)
        if test in guarded or not _needs_guard(pipeline, node, part):
            return expression
        # A bare deferred reference renders empty rather than aborting the step.
        return _guarded(test, expression)
    guards = sorted(
        {guard_test(pipeline, r) for r in part.refs() if _needs_guard(pipeline, node, r)}
    )
    inner_guarded = guarded | frozenset(guards)
    body = "".join(_part(pipeline, node, inner, inner_guarded) for inner in part.parts)
    if not guards:
        return body
    return _guarded(" and ".join(guards), body)


def _condition(pipeline: Pipeline, node: RouteEnd, test: str, refs: Sequence[Ref]) -> str:
    """A route condition, false rather than fatal when it cannot be evaluated.

    Definedness tests go inline rather than in an ``{% if %}``, since Jinja
    short-circuits ``and``.
    """
    guards = [guard_test(pipeline, r) for r in refs if _needs_guard(pipeline, node, r)]
    seen = list(dict.fromkeys(guards))
    if not seen:
        return "{{ " + test + " }}"
    return "{{ " + " and ".join([*seen, f"({test})"]) + " }}"


def _expression(path: str, fallback: str | None = None) -> str:
    """Conductor interpolates with Jinja's ``{{ }}``."""
    if fallback is not None:
        return "{{ " + path + " | default('" + fallback.replace("'", "\\'") + "') }}"
    return "{{ " + path + " }}"


def _guarded(condition: str, body: str, otherwise: str | None = None) -> str:
    """Wrap ``body`` so it renders only when ``condition`` holds.

    ``otherwise`` is what the other branch emits.
    """
    tail = "" if otherwise is None else "{% else %}" + otherwise
    return "{% if " + condition + " %}" + body + tail + "{% endif %}"


def guard_test(pipeline: Pipeline, ref: Ref) -> str:
    """The condition under which this reference is safe to read.

    Usually the node's name. Where ``guard_depth`` says otherwise, the trailing
    segments are tested one at a time, left to right.
    """
    if ref.origin is Origin.WORKFLOW_INPUT:
        # An optional workflow input is always defined — the engine binds it to
        # None (engine/context.py) — so the test is truthiness, not definedness.
        return reference_path(pipeline, ref)
    root = guard_variable(pipeline, ref)
    source = resolve(pipeline, ref)
    depth = source.guard_depth(ref.port) if isinstance(source, Node) else 0
    if depth < 1:
        return f"{root} is defined"
    path = reference_path(pipeline, ref).split(".")
    tests = [f"{root} is defined"]
    tests += [".".join(path[: len(path) - i]) + " is defined" for i in range(depth - 1, -1, -1)]
    return " and ".join(dict.fromkeys(tests))


def guard_variable(pipeline: Pipeline, ref: Ref) -> str:
    """The context variable a guard must test for this reference.

    For a group member that is the group; the member's own name is not bound.
    """
    source = resolve(pipeline, ref)
    if isinstance(source, Node):
        group = pipeline.group_of(source)
        if group is not None:
            return group.group_id
    return ref.source_id


def _needs_guard(pipeline: Pipeline, node: RouteEnd, ref: Ref) -> bool:
    """Whether reading this reference has to be guarded.

    Two reasons: the step may not have run, or the field is conditional on a
    branch within a step that did.
    """
    if _deferred(pipeline, node, ref):
        return True
    if ref.origin is Origin.WORKFLOW_INPUT:
        declared = next((p for p in pipeline.workflow_inputs if p.name == ref.source_id), None)
        return declared is not None and not declared.required
    source = resolve(pipeline, ref)
    return isinstance(source, Node) and source.guard_depth(ref.port) > 0


def _deferred(pipeline: Pipeline, node: RouteEnd, ref: Ref) -> bool:
    """Whether this reference can be read before its source has produced anything."""
    if ref.origin is not Origin.NODE:
        # Both are bound before the step runs.
        return False
    source = resolve(pipeline, ref)
    return source is not None and may_be_unresolved(pipeline, source, node)
