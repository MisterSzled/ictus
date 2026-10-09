"""Several named values at once. Conductor ``type: set``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.errors import CompositionError
from ictus.graph.node import ComputeNode

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ictus.graph.ports import InputPort, OutputPort

__all__ = ["bindings"]


def bindings(
    *,
    node_id: str,
    values: Mapping[str, str],
    outputs: Sequence[OutputPort] = (),
    description: str = "",
    inputs: Sequence[InputPort] = (),
) -> ComputeNode:
    """Compute several named values in one step.

    Bindings inside one block cannot reference each other — they are evaluated
    against the surrounding context, not against each other — so chain two nodes
    when one value depends on another.

    Declare ``outputs`` for the keys anything downstream reads: without them the
    values exist but no typed reference can name them, and the step is
    write-only. The types are ictus's, not the engine's — Conductor decides each
    binding's type by YAML-loading its rendered text, so a binding that renders
    as ``3`` or ``2024-01-01`` arrives as an integer or a
    date whatever this says. Where the type has to hold, use one ``constant``
    per value and set its ``output_type``.
    """
    if not values:
        raise CompositionError(f"bindings {node_id!r} needs at least one value")
    return ComputeNode(
        node_id=node_id,
        description=description,
        inputs=tuple(inputs),
        values=dict(values),
        declared_outputs=tuple(outputs),
    )
