"""Render structured data as prose for a human."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.graph.node import AgentNode
from ictus.graph.ports import OutputPort, PortType
from ictus.graph.ref import Ref, tpl

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ictus.graph.ports import InputPort

__all__ = ["briefing"]


def briefing(
    *,
    node_id: str = "briefing",
    subject: str,
    source: Ref,
    description: str = "",
    inputs: Sequence[InputPort] = (),
    output_name: str = "summary",
) -> AgentNode:
    """Summarise upstream output so a person can decide on it.

    Put one before a gate. The gate's prompt is the whole of what the reviewer
    sees, and a raw object dump is not a decision aid — the reviewer ends up
    approving something they did not read.

    ``source`` is a typed reference to what should be summarised, e.g.
    ``breakdown.ref("steps")``.
    """
    return AgentNode(
        node_id=node_id,
        description=description or f"Summarise {subject}",
        inputs=tuple(inputs),
        prompt=tpl(
            f"Summarise the following {subject} for a human reviewer who must decide "
            f"whether to approve it. Lead with the decision-relevant facts, name any "
            f"risk or omission plainly, and keep it short.\n\n",
            source,
        ),
        declared_outputs=(OutputPort(output_name, PortType.STRING, f"Prose summary of {subject}"),),
    )
