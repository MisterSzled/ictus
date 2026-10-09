"""Typed references to values produced elsewhere in the graph.

A ``Ref`` is checked where it is written: the port must exist, and its type
comes with it. Spelling and guarding are the backend's problem.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Union

from ictus.errors import CompositionError
from ictus.graph.ports import PortType

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from ictus.graph.composition import WorkflowInput
    from ictus.graph.node import Node

__all__ = [
    "AtLeast",
    "Comparison",
    "Every",
    "OptionalBlock",
    "Origin",
    "Ref",
    "Template",
    "TemplatePart",
    "as_template",
    "at_least",
    "equals",
    "every",
    "not_equals",
    "not_every",
    "optional",
    "ref_to",
    "tpl",
]


class Origin(StrEnum):
    """Where a referenced value comes from, which decides how it is addressed."""

    NODE = "node"
    WORKFLOW_INPUT = "workflow_input"
    LOOP_ITEM = "loop_item"


@dataclass(frozen=True, slots=True)
class Ref:
    """A reference to one output port of one node, a workflow input, or a loop item.

    ``source`` is kept so a lint can check the node is in the pipeline.
    """

    source_id: str
    port: str
    port_type: PortType
    origin: Origin = Origin.NODE
    source: Node | WorkflowInput | None = None
    element: Mapping[str, PortType] | None = None
    """An array's item shape, carried from the port so a fan-out can check it."""
    fallback: str | None = None
    """Value to use when this reference resolves to nothing."""

    @property
    def from_input(self) -> bool:
        """Whether this names a workflow parameter."""
        return self.origin is Origin.WORKFLOW_INPUT

    def or_else(self, fallback: str) -> Ref:
        """The same reference, rendered as ``fallback`` when it has no value."""
        return Ref(
            source_id=self.source_id,
            port=self.port,
            port_type=self.port_type,
            origin=self.origin,
            source=self.source,
            element=self.element,
            fallback=fallback,
        )


@dataclass(frozen=True, slots=True)
class OptionalBlock:
    """A run of template parts that only renders once its references resolve.

    For prose that makes no sense without the value it introduces.
    """

    parts: tuple[TemplatePart, ...]

    def refs(self) -> Iterator[Ref]:
        """Every reference inside this block, nesting included."""
        for part in self.parts:
            if isinstance(part, Ref):
                yield part
            elif isinstance(part, _COMPOSITE):
                yield from part.refs()


@dataclass(frozen=True, slots=True)
class Comparison:
    """A reference tested against a value, for use as a route condition.

    Each value type has its own spelling: a string is quoted, an ``int`` goes
    through ``| int``, a ``bool`` renders Jinja's bare literal. The value's
    Python type must match the port's declared type.
    """

    ref: Ref
    value: str | int | bool
    negated: bool = False

    def refs(self) -> Iterator[Ref]:
        """The reference under test."""
        yield self.ref


@dataclass(frozen=True, slots=True)
class Every:
    """A condition holding when every one of several references is true.

    Built from the references, so a derived set stays in step with the condition.
    """

    refs_: tuple[Ref, ...]
    negated: bool = False

    def refs(self) -> Iterator[Ref]:
        """Every reference under test."""
        yield from self.refs_


@dataclass(frozen=True, slots=True)
class AtLeast:
    """A condition holding once a numeric reference reaches a threshold.

    Rendered with ``| int``: a rendered value arrives as whatever
    ``_maybe_parse_json`` made of it, and a string-to-number compare raises.
    """

    ref: Ref
    threshold: int

    def refs(self) -> Iterator[Ref]:
        """The reference under test."""
        yield self.ref


TemplatePart = Union[str, Ref, Comparison, Every, AtLeast, "OptionalBlock", "Template"]


@dataclass(frozen=True, slots=True)
class Template:
    """Literal text interleaved with typed references."""

    parts: tuple[TemplatePart, ...]

    def refs(self) -> Iterator[Ref]:
        """Every reference in the template, nesting included."""
        for part in self.parts:
            if isinstance(part, Ref):
                yield part
            elif isinstance(part, _COMPOSITE):
                yield from part.refs()

    def literal_text(self) -> str:
        """Just the prose, for error messages and previews."""
        return "".join(p for p in self.parts if isinstance(p, str))


# Every part that can hold references inside it, named once for the `refs()`
# walkers.
_COMPOSITE = (Template, OptionalBlock, Comparison, Every, AtLeast)


def ref_to(node_id: str, port: str, port_type: PortType) -> Ref:
    """A reference to a node that does not exist yet, for a loop's back-edge.

    The lint resolves it against the finished graph and checks the type.
    """
    return Ref(source_id=node_id, port=port, port_type=port_type)


def tpl(*parts: TemplatePart) -> Template:
    """Build a template from literal text and references."""
    return Template(tuple(parts))


def as_template(value: str | Template | Ref) -> str | Template:
    """A bare reference, wrapped so every reader downstream sees one shape."""
    return tpl(value) if isinstance(value, Ref) else value


def equals(ref: Ref, value: str | int | bool) -> Template:
    """A condition that holds when ``ref`` equals ``value``.

    The value's Python type must match the port's declared type.
    """
    _check_comparable(ref, value)
    return Template((Comparison(ref=ref, value=value),))


def not_equals(ref: Ref, value: str | int | bool) -> Template:
    """A condition that holds when ``ref`` does not equal ``value``."""
    _check_comparable(ref, value)
    return Template((Comparison(ref=ref, value=value, negated=True),))


#: What a value has to be for each comparable port type, and how to say so.
_COMPARABLE: dict[PortType, tuple[type, str]] = {
    PortType.STRING: (str, "a quoted string"),
    PortType.NUMBER: (int, "an int"),
    PortType.BOOLEAN: (bool, "True or False"),
}


def _check_comparable(ref: Ref, value: str | int | bool) -> None:
    """Refuse a comparison whose two sides do not render as the same Jinja type.

    A route condition is evaluated against the stored value, not its rendered
    text, so a mismatched literal is well-formed and never true.
    """
    expected = _COMPARABLE.get(ref.port_type)
    if expected is None:
        raise CompositionError(
            f"{ref.source_id}.{ref.port} is {ref.port_type.value}, which equals() cannot "
            "compare: a container never equals a scalar literal, so the test would be false "
            "on every run. Route on a scalar the step also declares."
        )
    wanted, hint = expected
    if _kind(value) is not wanted:
        raise CompositionError(
            f"{ref.source_id}.{ref.port} is {ref.port_type.value}, so comparing it to "
            f"{value!r} renders `{_rendered(value)}` and is never true; pass {hint}"
            + (
                ". A boolean route is usually spelled tpl(ref) for true and "
                "not_every(ref) for false"
                if ref.port_type is PortType.BOOLEAN
                else ""
            )
        )


def _kind(value: str | int | bool) -> type:
    """``bool`` is a subclass of ``int``, so the order of these tests is the rule."""
    if isinstance(value, bool):
        return bool
    if isinstance(value, int):
        return int
    return str


def _rendered(value: str | int | bool) -> str:
    """What the comparison would have rendered as."""
    if isinstance(value, bool):
        return f"== {str(value).lower()}"
    if isinstance(value, int):
        return f"| int == {value}"
    return f"== '{value}'"


def every(*refs: Ref) -> Template:
    """A condition that holds when all of ``refs`` are true."""
    if not refs:
        raise CompositionError("every() needs at least one reference to test")
    return Template((Every(refs_=tuple(refs)),))


def not_every(*refs: Ref) -> Template:
    """A condition that holds while any of ``refs`` is still false."""
    if not refs:
        raise CompositionError("not_every() needs at least one reference to test")
    return Template((Every(refs_=tuple(refs), negated=True),))


def at_least(ref: Ref, threshold: int) -> Template:
    """A condition that holds once ``ref`` reaches ``threshold``."""
    if ref.port_type is not PortType.NUMBER:
        raise CompositionError(
            f"at_least() compares numbers, but {ref.source_id}.{ref.port} is {ref.port_type.value}"
        )
    return Template((AtLeast(ref=ref, threshold=threshold),))


def optional(*parts: TemplatePart) -> OptionalBlock:
    """A block that renders only when the references inside it have resolved."""
    return OptionalBlock(tuple(parts))
