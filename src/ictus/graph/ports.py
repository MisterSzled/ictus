"""Ports — the typed boundary of a node.

``PortType`` is exactly the set Conductor accepts on the wire
(``OutputField.type`` / ``InputDef.type``). A refinement it cannot represent
would mean something at composition that is erased at emission.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from ictus.errors import CompositionError

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "InputPort",
    "OutputPort",
    "PortConnection",
    "PortType",
]


class PortType(StrEnum):
    """A value type on a node boundary.

    These are Conductor's five wire types; the enum value is what is emitted.
    """

    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"


@dataclass(frozen=True, slots=True)
class OutputPort:
    """A named, typed value a node produces.

    ``element`` is an array's item shape, emitted as the ``items:`` schema,
    which is what tells the model which keys to put in each entry.
    """

    name: str
    port_type: PortType
    description: str = ""
    element: Mapping[str, PortType] | None = None

    def __post_init__(self) -> None:
        if self.element is not None and self.port_type is not PortType.ARRAY:
            raise CompositionError(
                f"port {self.name!r} is {self.port_type.value}, so it has no element shape"
            )

    def accepts(self, other: InputPort) -> bool:
        """Whether this output can drive ``other``."""
        return self.port_type is other.port_type


@dataclass(frozen=True, slots=True)
class InputPort:
    """A named, typed value a node consumes.

    ``optional`` becomes the ``?`` suffix on the emitted ``input:`` reference,
    which is what lets a loop back-edge resolve on the first pass.
    """

    name: str
    port_type: PortType
    description: str = ""
    optional: bool = False


@dataclass(frozen=True, slots=True)
class PortConnection:
    """A validated output-to-input pairing."""

    source: OutputPort
    target: InputPort
