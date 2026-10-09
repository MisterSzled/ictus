"""Explicit successful exit."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.graph.node import TerminateNode
from ictus.graph.ref import as_template

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ictus.graph.ports import InputPort
    from ictus.graph.ref import Ref, Template

__all__ = ["succeed"]


def succeed(
    *,
    node_id: str = "done",
    reason: str | Template,
    description: str = "",
    inputs: Sequence[InputPort] = (),
    result: Mapping[str, str | Template | Ref] | None = None,
) -> TerminateNode:
    """End the run successfully. Emits ``type: terminate``.

    A node with no outgoing route implicitly ends the run, which makes a
    forgotten edge indistinguishable from an intended finish. Terminating
    explicitly gives the run a distinguishable exit status and marks the stop as
    deliberate in the event log.

    ``result`` overrides the workflow's final ``output:`` map for this path,
    which is how two exits can return differently shaped results.
    """
    return TerminateNode(
        node_id=node_id,
        description=description,
        inputs=tuple(inputs),
        status="success",
        reason=reason,
        result={name: as_template(v) for name, v in result.items()} if result else None,
    )
