"""Work out what is missing, then ask a person only for what is actually missing.

One step reads the work and reports what it could not determine. If it
determined everything, the run continues untouched; otherwise the person is
asked precisely those questions and nothing else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.graph.node import AgentNode
from ictus.graph.ports import InputPort, OutputPort, PortType
from ictus.graph.ref import tpl
from ictus.graph.stage import Stage
from ictus.prompting import prompt
from ictus.stdlib.exits.succeed import succeed
from ictus.stdlib.gates.ask import ask_human_for

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["resolve_unknowns"]

STR, OBJ, ARR = PortType.STRING, PortType.OBJECT, PortType.ARRAY


def resolve_unknowns(
    *,
    stage_id: str = "resolve-unknowns",
    subject: str,
    needs: Sequence[str],
    description: str = "",
) -> Stage:
    """Determine what is unknown, ask a person for it, and carry the answers out.

    ``needs`` names what the work requires. The identifying step answers what
    it can and writes questions for the rest, so the number of questions
    follows the ticket.

    Contract: input ``brief`` (string) in; ``known`` (object) and ``answers``
    (object) out.
    """
    if not needs:
        raise ValueError("resolve_unknowns needs at least one thing to look for")

    stage = Stage(stage_id=stage_id, description=description or f"Resolve unknowns for {subject}")
    body = stage.body
    brief = body.declare_input("brief", STR, description=f"The {subject} to examine")

    wanted = "\n".join(f"- {item}" for item in needs)
    identify = body.add(
        AgentNode(
            node_id="identify",
            description=f"Work out what is missing from the {subject}",
            inputs=(InputPort("brief", STR),),
            prompt=tpl(
                f"Examine the {subject} below. The work needs each of these:\n{wanted}\n\n",
                brief.ref(),
                "\n\n" + prompt(__name__, "decide"),
            ),
            declared_outputs=(
                OutputPort("known", OBJ, "What could be determined without asking"),
                OutputPort("missing", ARR, "Questions for the person, one per unknown"),
                OutputPort("all_known", PortType.BOOLEAN, "Whether nothing needs asking"),
            ),
        )
    )

    ask = body.add(
        ask_human_for(
            node_id="ask",
            description=f"Ask for what the {subject} did not say",
            source=identify.ref("missing"),
        )
    )
    ready = body.add(succeed(node_id="ready", reason="Everything needed is known."))
    abandoned = body.add(
        succeed(
            node_id="abandoned",
            reason="Abandoned: the run needs values nobody supplied.",
        )
    )

    body.connect_input(brief, identify, "brief")
    # Ask only about what is actually unknown.
    body.route(identify, ready, when=tpl(identify.ref("all_known")))
    body.route(identify, ask)
    body.route(ask, ready)
    body.abort_route(ask, abandoned)

    body.expose_output("known", identify, "known")
    # Defaulted: on the path where nothing was missing, the questions never ran.
    body.expose_output("answers", ask, "answers", default="{}")
    return stage
