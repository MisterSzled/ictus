"""Explicit failed exit."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.graph.node import TerminateNode
from ictus.graph.ref import as_template

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ictus.graph.ports import InputPort
    from ictus.graph.ref import Ref, Template

__all__ = ["fail"]


def fail(
    *,
    node_id: str = "failed",
    reason: str | Template,
    description: str = "",
    inputs: Sequence[InputPort] = (),
    result: Mapping[str, str | Template | Ref] | None = None,
) -> TerminateNode:
    """End the run as failed, with a non-zero exit and a stated reason.

    Distinct from ``succeed`` at the process boundary: a rejected pipeline and a
    completed one should not look the same to whatever invoked Conductor.
    """
    return TerminateNode(
        node_id=node_id,
        description=description,
        inputs=tuple(inputs),
        status="failed",
        reason=reason,
        result={name: as_template(v) for name, v in result.items()} if result else None,
    )
