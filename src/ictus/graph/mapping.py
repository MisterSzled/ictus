"""Fan out over a list whose length is only known at run time.

As against ``Pipeline.parallel``, whose members are written down.

A group produces one value under its own name: ``outputs`` (a list, or a
mapping when ``key_by`` is set), ``errors`` keyed the same way, and ``count``.
Routes are evaluated once, after every item has finished; an item cannot route.

The loop variable is addressed as ``{{ item.field }}`` — no ``.output.`` — and
its name is checked against Conductor's reserved set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ictus.errors import CompositionError
from ictus.graph.node import Node, NodeKind
from ictus.graph.ports import OutputPort, PortType
from ictus.graph.ref import Origin, Ref

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ictus.graph.composition import FailureMode

__all__ = ["COUNT_PORT", "ERRORS_PORT", "OUTPUTS_PORT", "Item", "MapGroup"]

OUTPUTS_PORT = "outputs"
ERRORS_PORT = "errors"
COUNT_PORT = "count"

# Conductor's own reserved set, plus the two it injects per iteration.
_RESERVED = frozenset({"workflow", "context", "output", "_index", "_key"})

# What a for_each iteration may be. Conductor rejects a terminal, a wait, a
# script and a questions step. A sub-workflow it would accept, but a child's
# parameters are bound by `input_mapping`, built from graph edges, and there is
# no way to wire a loop item into a child's port.
_ITERABLE = frozenset({NodeKind.LLM_CALL, NodeKind.COMPUTATION})


@dataclass(frozen=True, slots=True)
class Item:
    """The loop variable, as something references can be taken from.

    Fields are declared: an array's port type does not carry its element shape.
    """

    name: str
    fields: Mapping[str, PortType] = field(default_factory=dict)

    def ref(self, field_name: str) -> Ref:
        """A reference to one field of the current item."""
        if field_name not in self.fields:
            known = ", ".join(sorted(self.fields)) or "(none)"
            raise CompositionError(
                f"loop item {self.name!r} has no field {field_name!r}; declared: {known}"
            )
        return Ref(
            source_id=self.name,
            port=field_name,
            port_type=self.fields[field_name],
            origin=Origin.LOOP_ITEM,
        )

    def whole(self) -> Ref:
        """A reference to the item itself, for a list of plain values."""
        return Ref(source_id=self.name, port="", port_type=PortType.OBJECT, origin=Origin.LOOP_ITEM)


@dataclass(frozen=True, eq=False)
class MapGroup:
    """One body, run once per element of an array resolved at run time."""

    group_id: str
    source: Ref
    item: Item
    body: Node
    """The step run per item.

    ``expect_items`` buys iteration budget; it caps nothing. ``for_each`` has
    no length limit and ``max_concurrent`` batches rather than truncating, so a
    longer array runs every item and the step after the group dies on the
    budget. Cap the length in the step that produces the array.
    """
    expect_items: int
    description: str = ""
    max_concurrent: int = 10
    failure_mode: FailureMode | None = None
    key_by: str | None = None

    def __post_init__(self) -> None:
        if self.source.port_type is not PortType.ARRAY:
            raise CompositionError(
                f"map group {self.group_id!r} maps over "
                f"{self.source.source_id}.{self.source.port}, which is "
                f"{self.source.port_type.value}, not array"
            )
        if self.item.name in _RESERVED:
            raise CompositionError(
                f"loop variable {self.item.name!r} is reserved by Conductor "
                f"({', '.join(sorted(_RESERVED))})"
            )
        if not self.item.name.isidentifier():
            raise CompositionError(f"loop variable {self.item.name!r} is not an identifier")
        element = self.source.element
        if self.item.fields and element is None:
            raise CompositionError(
                f"map group {self.group_id!r} reads fields off each item, but "
                f"{self.source.source_id}.{self.source.port} declares no element shape. "
                "Give the producing port an `element=` mapping: it becomes the array's "
                "`items:` schema, which is what tells the model which keys to emit."
            )
        if element is not None:
            missing = sorted(set(self.item.fields) - set(element))
            if missing:
                known = ", ".join(sorted(element)) or "(none)"
                raise CompositionError(
                    f"map group {self.group_id!r} reads {missing} off each item, which "
                    f"{self.source.source_id}.{self.source.port} does not produce; "
                    f"its items have: {known}"
                )
            wrong = sorted(
                name
                for name, port_type in self.item.fields.items()
                if element[name] is not port_type
            )
            if wrong:
                raise CompositionError(
                    f"map group {self.group_id!r} reads {wrong} at a different type than "
                    f"{self.source.source_id}.{self.source.port} produces"
                )
        if self.body.kind is NodeKind.SUB_GRAPH:
            raise CompositionError(
                f"map group {self.group_id!r} cannot iterate a stage yet. A child's "
                "parameters are bound by input_mapping, which is built from graph edges, "
                "and a loop item is not a node an edge can start from — so every iteration "
                "would silently receive the parent's own inputs instead of its item. "
                "Inline the work as a single step, or fan out over stages by mapping to a "
                "step that calls one."
            )
        if self.body.kind not in _ITERABLE:
            raise CompositionError(
                f"map group {self.group_id!r} cannot iterate a {self.body.kind.value}; "
                "an item has no way to end the run, pause, or hold the dashboard's "
                "prompt slot. Only model calls and computations may run inside one."
            )
        if self.expect_items < 1:
            raise CompositionError(
                f"map group {self.group_id!r} needs expect_items >= 1, got {self.expect_items}"
            )
        if not 1 <= self.max_concurrent <= 100:
            raise CompositionError(
                f"map group {self.group_id!r} needs max_concurrent between 1 and 100, "
                f"got {self.max_concurrent}"
            )

    @property
    def node_id(self) -> str:
        return self.group_id

    @property
    def outputs(self) -> tuple[OutputPort, ...]:
        """What a later step can read off the finished group."""
        collected = PortType.OBJECT if self.key_by else PortType.ARRAY
        return (
            OutputPort(OUTPUTS_PORT, collected, "Every item's result"),
            OutputPort(ERRORS_PORT, PortType.OBJECT, "Items that failed, keyed"),
            OutputPort(COUNT_PORT, PortType.NUMBER, "How many items ran"),
        )

    def get_output(self, port: str) -> OutputPort:
        for candidate in self.outputs:
            if candidate.name == port:
                return candidate
        known = ", ".join(p.name for p in self.outputs)
        raise CompositionError(
            f"map group {self.group_id!r} has no output {port!r}; it produces {known}"
        )

    def output_ref(self, port_name: str) -> str:
        """The path under the group's own name that reads this port.

        A for-each group stores its aggregate directly under its name, so the
        interface layer drops the ``.output.``.
        """
        return port_name

    def ref(self, port: str) -> Ref:
        found = self.get_output(port)
        return Ref(
            source_id=self.group_id, port=port, port_type=found.port_type, origin=Origin.NODE
        )
