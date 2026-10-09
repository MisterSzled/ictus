"""Scopes — stages whose failures are values rather than exceptions.

A sub-workflow ending in a failed terminal does not hand control back: the
child engine raises ``SubworkflowTerminatedError`` before any parent route is
evaluated (``engine/workflow.py``, in ``_run_child_engine``).

Every exit of a scope is therefore a success terminal carrying a
closed-vocabulary ``outcome``. The vocabulary is checked on both sides:
``Scope.exit`` refuses an undeclared outcome, ``Pipeline.branch_on_outcome``
refuses to leave one unrouted.

Two payload rules:

* Every exit carries every declared value; omitted keys get a
  type-appropriate empty literal.
* An outcome name that survives ``_maybe_parse_json`` as a non-string is
  refused, since every comparison against it would silently fail.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from ictus.errors import CompositionError
from ictus.graph.node import OUTCOME_PORT, ScopeNode, TerminateNode, coerced_outcome
from ictus.graph.pipeline import Pipeline
from ictus.graph.ports import InputPort, OutputPort, PortType
from ictus.graph.ref import Ref, Template, tpl
from ictus.graph.traversal import back_edges

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ictus.graph.composition import WorkflowInput
    from ictus.graph.node import Node

__all__ = ["OUTCOME_PORT", "Scope", "ScopeNode", "outcome_scope"]

# What an omitted key becomes: an empty value of the right type, since a
# missing one is a template error on whichever path omitted it.
_EMPTY: dict[PortType, str] = {
    PortType.STRING: "",
    PortType.OBJECT: "{}",
    PortType.ARRAY: "[]",
    PortType.NUMBER: "0",
    PortType.BOOLEAN: "false",
}


class Scope:
    """A stage whose every exit reports an outcome instead of raising."""

    def __init__(
        self,
        *,
        stage_id: str,
        outcomes: Sequence[str],
        carry: Mapping[str, PortType | OutputPort] = MappingProxyType({}),
        description: str = "",
        loop_passes: int | None = None,
        max_iterations: int | None = None,
    ) -> None:
        if len(set(outcomes)) < 2:
            raise CompositionError(
                f"scope {stage_id!r} needs at least two distinct outcomes; with one there is "
                "nothing for the caller to branch on and it should be a plain Stage"
            )
        for name in outcomes:
            # Kept here as well as on ScopeNode: a Scope builds its node lazily,
            # so relying on the node's own check would move this failure from
            # the line that writes the vocabulary to the line that seals it.
            if coerced_outcome(name):
                raise CompositionError(
                    f"scope {stage_id!r} cannot use the outcome {name!r}: Conductor parses a "
                    "rendered output with json.loads, so it would arrive as a non-string and "
                    "every comparison against it would silently fail"
                )
            if not name or name != name.strip():
                raise CompositionError(f"scope {stage_id!r} has a malformed outcome {name!r}")
        self.outcomes = tuple(outcomes)
        # An OutputPort rather than a bare type where the shape matters: an
        # array's element schema has to survive the scope boundary.
        self.carry: dict[str, OutputPort] = {
            name: value if isinstance(value, OutputPort) else OutputPort(name, value)
            for name, value in carry.items()
        }
        if OUTCOME_PORT in self.carry:
            raise CompositionError(f"{OUTCOME_PORT!r} is reserved; it is the outcome itself")
        self.body = Pipeline(
            pipeline_id=stage_id,
            description=description,
            loop_passes=loop_passes,
            max_iterations=max_iterations,
        )
        self._exits: list[str] = []

    @property
    def stage_id(self) -> str:
        """The scope's identifier, and the basename of its emitted file."""
        return self.body.pipeline_id

    @property
    def output_ports(self) -> tuple[OutputPort, ...]:
        """What a parent reads: the outcome, plus every carried value."""
        return (
            OutputPort(OUTCOME_PORT, PortType.STRING, "Which exit was taken"),
            *(
                OutputPort(name, port.port_type, port.description, port.element)
                for name, port in self.carry.items()
            ),
        )

    @property
    def input_ports(self) -> tuple[InputPort, ...]:
        """The scope's parameters, as ports a parent can wire into."""
        return self.body.declared_input_ports

    def exit(
        self,
        *,
        node_id: str,
        outcome: str,
        reason: str | Template,
        **carry: Ref | Template | str,
    ) -> TerminateNode:
        """Add an exit reporting ``outcome``.

        Every reference handed in is wired as well as rendered. Carried values
        left unnamed are filled with an empty literal of their declared type.
        """
        if outcome not in self.outcomes:
            raise CompositionError(
                f"scope {self.stage_id!r} has no outcome {outcome!r}; "
                f"declared: {', '.join(self.outcomes)}"
            )
        unknown = sorted(set(carry) - set(self.carry))
        if unknown:
            known = ", ".join(sorted(self.carry)) or "(none)"
            raise CompositionError(
                f"exit {node_id!r} carries {unknown}, which scope {self.stage_id!r} does not "
                f"declare; declared: {known}"
            )
        payload: dict[str, str | Template] = {OUTCOME_PORT: outcome}
        sources: list[Ref] = []
        for name, declared in self.carry.items():
            supplied = carry.get(name)
            if supplied is None:
                payload[name] = _EMPTY[declared.port_type]
                continue
            if isinstance(supplied, Ref):
                _check_carry_type(node_id, name, declared.port_type, supplied)
                payload[name] = tpl(supplied)
                sources.append(supplied)
            else:
                payload[name] = supplied
                if isinstance(supplied, Template):
                    sources.extend(supplied.refs())
        if isinstance(reason, Template):
            sources.extend(reason.refs())

        ports, wiring = self._wiring(node_id, sources)
        node = self.body.add(
            TerminateNode(
                node_id=node_id,
                inputs=ports,
                status="success",
                reason=reason,
                result=payload,
            )
        )
        for ref, port in wiring:
            if ref.from_input:
                self.body.connect_input(self._input_named(ref.source_id), node, port.name)
            else:
                self.body.feed(_node_of(ref), ref.port, node, port.name)
        self._exits.append(node_id)
        return node

    def _wiring(
        self, node_id: str, refs: Sequence[Ref]
    ) -> tuple[tuple[InputPort, ...], list[tuple[Ref, InputPort]]]:
        """One input port per distinct reference an exit reads."""
        ports: list[InputPort] = []
        wiring: list[tuple[Ref, InputPort]] = []
        seen: set[tuple[str, str]] = set()
        for ref in refs:
            key = (ref.source_id, ref.port)
            if key in seen:
                continue
            seen.add(key)
            if ref.source is None and not ref.from_input:
                raise CompositionError(
                    f"exit {node_id!r} reads {ref.source_id}.{ref.port}, a forward reference "
                    "that cannot be wired; give the exit a settled value instead"
                )
            port = InputPort(f"{ref.source_id}__{ref.port}", ref.port_type)
            ports.append(port)
            wiring.append((ref, port))
        return tuple(ports), wiring

    def _input_named(self, name: str) -> WorkflowInput:
        for param in self.body.workflow_inputs:
            if param.name == name:
                return param
        raise CompositionError(f"scope {self.stage_id!r} has no input {name!r}")

    def instantiate(
        self,
        parent: Pipeline,
        *,
        node_id: str | None = None,
        description: str = "",
        max_depth: int | None = None,
    ) -> ScopeNode:
        """Place this scope into ``parent`` and return the node standing for it."""
        self._check_sealed()
        node = ScopeNode(
            node_id=node_id or self.stage_id.replace("-", "_"),
            description=description or self.body.description,
            inputs=self.input_ports,
            declared_outputs=self.output_ports,
            target=f"./{self.stage_id}.yaml",
            max_depth=max_depth,
            outcomes=self.outcomes,
        )
        parent.add_subgraph(node, self.body)
        return node

    def _check_sealed(self) -> None:
        """Every way out must be an exit, and nothing may leak past them."""
        # An edge to END reports nothing, so the parent's outcome routes would
        # read an undefined variable.
        loose = sorted({e.source.node_id for e in self.body.edges if e.is_end})
        if loose:
            raise CompositionError(
                f"scope {self.stage_id!r} routes {loose} to END. A scope reports its result "
                "by exiting, and END reports nothing — the caller's outcome routes would "
                "read an undefined value. Route to a Scope.exit() instead."
            )
        terminals = [n for n in self.body.nodes if isinstance(n, TerminateNode)]
        stray = [n.node_id for n in terminals if n.node_id not in self._exits]
        if stray:
            raise CompositionError(
                f"scope {self.stage_id!r} has terminal(s) {stray} that are not exits. "
                "Use Scope.exit() so every way out reports an outcome."
            )
        if not self._exits:
            raise CompositionError(f"scope {self.stage_id!r} has no exits")
        reached = set(self.outcomes) - {
            str(n.result[OUTCOME_PORT]) for n in terminals if n.result and OUTCOME_PORT in n.result
        }
        if reached:
            raise CompositionError(
                f"scope {self.stage_id!r} declares outcome(s) {sorted(reached)} that no exit "
                "reports; either add an exit or drop them from the vocabulary"
            )
        if back_edges(self.body) and self.body.loop_passes is None:
            # `_run_child_engine` catches WorkflowTerminated and nothing else,
            # so a child's MaxIterationsError escapes past every outcome route.
            raise CompositionError(
                f"scope {self.stage_id!r} loops but sets no loop_passes. Exhaustion is not an "
                "outcome — a child that runs out of iterations raises past the parent's routes "
                "— so the bound has to be explicit."
            )
        if self.body.exposed_outputs:
            raise CompositionError(
                f"scope {self.stage_id!r} uses expose_output. A terminal's output_template "
                "replaces the workflow-level output map, so that would emit a block nothing "
                "reads. Carry the value through Scope.exit(**carry) instead."
            )


def outcome_scope(
    *,
    stage_id: str,
    outcomes: Sequence[str],
    carry: Mapping[str, PortType | OutputPort] = MappingProxyType({}),
    description: str = "",
    loop_passes: int | None = None,
    max_iterations: int | None = None,
) -> Scope:
    """A stage whose every exit is a value the caller routes on."""
    return Scope(
        stage_id=stage_id,
        outcomes=outcomes,
        carry=carry,
        description=description,
        loop_passes=loop_passes,
        max_iterations=max_iterations,
    )


def _node_of(ref: Ref) -> Node:
    """The node a settled reference points at. Checked by ``_wiring`` first."""
    from ictus.graph.node import Node as _Node

    assert isinstance(ref.source, _Node)
    return ref.source


def _check_carry_type(node_id: str, name: str, declared: PortType, ref: Ref) -> None:
    if ref.port_type is not declared:
        raise CompositionError(
            f"exit {node_id!r} carries {name!r} as {ref.port_type.value} but the scope "
            f"declares it as {declared.value}"
        )
