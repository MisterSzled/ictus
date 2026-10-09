"""N-way human choice gate."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.errors import CompositionError
from ictus.graph.node import GateChoice, GateNode

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ictus.graph.ports import InputPort
    from ictus.graph.ref import Template

__all__ = ["choice_gate"]


def choice_gate(
    *,
    node_id: str,
    prompt: str | Template,
    choices: Sequence[tuple[str, str]],
    description: str = "",
    inputs: Sequence[InputPort] = (),
) -> GateNode:
    """A gate offering arbitrary options, as ``(value, label)`` pairs.

    Each option becomes its own outgoing edge, supplied by ``Pipeline.branch``.
    ``--skip-gates`` takes the first option, so put the safe choice first.
    """
    if not choices:
        raise CompositionError(f"choice_gate {node_id!r} needs at least one choice")
    return GateNode(
        node_id=node_id,
        description=description,
        inputs=tuple(inputs),
        prompt=prompt,
        choices=tuple(GateChoice(value=value, label=label) for value, label in choices),
    )
