"""Two-way approve/reject gate."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.graph.node import GateChoice, GateNode

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ictus.graph.ports import InputPort
    from ictus.graph.ref import Template

__all__ = ["approval_gate"]


def approval_gate(
    *,
    node_id: str = "approval_gate",
    prompt: str | Template,
    description: str = "",
    inputs: Sequence[InputPort] = (),
    approve_label: str = "Approve",
    reject_label: str = "Reject",
    notes_field: str | None = "notes",
) -> GateNode:
    """A gate offering exactly approve and reject.

    Emits ``type: human_gate``, which is what the fleet TUI reads to show a run
    as waiting; a pause modelled as a plain agent reports ``running`` forever.

    ``notes_field`` adds a free-text prompt on rejection and exposes it as an
    output port. A template reading it must guard with
    ``{% if <gate> is defined %}``.
    """
    return GateNode(
        node_id=node_id,
        description=description,
        inputs=tuple(inputs),
        prompt=prompt,
        choices=(
            GateChoice(value="approved", label=approve_label),
            GateChoice(
                value="rejected",
                label=reject_label,
                prompt_for=notes_field,
                multiline=notes_field is not None,
            ),
        ),
    )
