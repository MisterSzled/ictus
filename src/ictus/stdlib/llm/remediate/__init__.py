"""An in-flow helper that diagnoses a blocked run and talks the person through it.

Placed behind a gate. Conductor's ``dialog`` opens a multi-turn conversation,
in the dashboard when one is served and in the terminal otherwise.

**The helper never handles credentials.** It repairs what needs no secret; the
moment a fix needs a token or a permission change it hands over the exact
command for the person to run themselves. No secret is pasted in or printed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.graph.node import AgentNode
from ictus.graph.ports import OutputPort, PortType
from ictus.graph.ref import Template, tpl
from ictus.prompting import prompt

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ictus.graph.ports import InputPort

__all__ = ["remediate"]

_CREDENTIAL_BOUNDARY = prompt(__name__, "credential_boundary")

_METHOD = prompt(__name__, "method")


def remediate(
    *,
    node_id: str = "remediate",
    problem: Template,
    subject: str = "the blocked step",
    description: str = "",
    inputs: Sequence[InputPort] = (),
) -> AgentNode:
    """A helper that investigates a failure and works through it with the person.

    ``problem`` is a template carrying the failure detail — typically the report
    from whatever check failed, so the helper starts from evidence rather than
    guessing.
    """
    return AgentNode(
        node_id=node_id,
        description=description or f"Help resolve {subject}",
        inputs=tuple(inputs),
        prompt=tpl(
            f"A workflow is paused because {subject} failed. Here is what was observed:\n\n",
            problem,
            "\n\n" + prompt(__name__, "charge") + "\n\n",
            _METHOD,
            "\n",
            _CREDENTIAL_BOUNDARY,
            "\n" + prompt(__name__, "closing"),
        ),
        # Always converse: the person is already waiting at a gate.
        dialog_trigger=(
            "Always enter dialog. The workflow is paused on a failure that needs "
            "a person, and this agent exists to work through it with them."
        ),
        declared_outputs=(
            OutputPort("resolved", PortType.BOOLEAN, "Whether the helper believes it is fixed"),
            OutputPort("summary", PortType.STRING, "What was done, and what is still needed"),
        ),
    )
