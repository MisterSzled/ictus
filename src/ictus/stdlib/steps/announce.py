"""Tell somebody something, as a step in the graph. No model call.

Nothing here knows where a report goes: an ``Integration`` carries the program
that sends one, and this builds the step that runs it.

Being a node means it is costed against ``max_iterations``, routed like
anything else, and visible in the dashboard and in ``ictus trace``. What no
step can see — a budget tripping, the engine being killed — is ``ictus watch``.

A report never fails the run: the step always succeeds, and one that could not
deliver says ``posted: "false"`` with its reason on stderr. Whether a
destination's variables are set is preflight's.

Most pipelines want ``pipeline.integrate(...)`` instead, which attaches a
destination to every gate and exit at once. Reach for this when one particular
point deserves one particular sentence.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ictus.errors import CompositionError
from ictus.graph.node import GateNode, ScriptNode
from ictus.graph.ports import InputPort, OutputPort, PortType
from ictus.graph.ref import as_template

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ictus.graph.ref import Ref, Template
    from ictus.graph.requirements import Integration

__all__ = ["POSTED_PORT", "THREAD_PORT", "announce"]

#: What an announcement publishes so a later one can hang under it. Named for
#: what it is; translating to a service's spelling is the integration's job.
THREAD_PORT = "thread"

#: "true" when the report landed, "false" when it could not be sent. The step
#: succeeds either way; the reason for a "false" is on its stderr.
POSTED_PORT = "posted"


def announce(
    *,
    node_id: str,
    text: str | Template,
    to: Integration,
    thread: Ref | None = None,
    answers: GateNode | None = None,
    description: str = "",
    inputs: Sequence[InputPort] = (),
    timeout: int = 20,
) -> ScriptNode:
    """Send ``text`` through ``to``, as a step in the graph.

    ``thread`` is the port an earlier announcement published. The input is
    declared for you; the data edge is yours to wire with ``feed``.

    ``answers=<gate>`` puts that gate's choices in the message as buttons, read
    off the gate so they cannot drift from it.
    """
    if not to.announces:
        raise CompositionError(
            f"announce node {node_id!r} reports to {to.name!r}, which comments on items "
            "rather than reporting to a place. Use comment for that"
        )
    if timeout < 1:
        raise CompositionError(f"announce node {node_id!r} needs a timeout of at least 1s")
    if thread is not None and not to.threads:
        raise CompositionError(
            f"announce node {node_id!r} replies under an earlier message, but "
            f"{to.name!r} has no threads — a report there is a flat sequence, and no "
            "parent to reply under is ever published"
        )

    asks = _asked(node_id, answers, to)

    declared = list(inputs)
    if thread is not None and not any(port.name == thread.source_id for port in declared):
        declared.append(InputPort(thread.source_id, PortType.STRING, optional=True))

    return ScriptNode(
        node_id=node_id,
        description=description or f"Report to {to.name}",
        inputs=tuple(declared),
        command=to.command,
        args=(
            "-c",
            to.program,
            # Through argv, not interpolated into the program: a quote in
            # somebody's data would otherwise be a syntax error. The fallback
            # covers a parent that printed no thread.
            as_template(thread.or_else("")) if thread is not None else "",
            asks,
            # The program gives up at `timeout`, the engine five seconds
            # later, so a slow report is not a failed step.
            str(timeout),
        ),
        uses=(to.name,),
        stdin=text,
        timeout=timeout + 5,
        declared_outputs=(
            OutputPort(THREAD_PORT, PortType.STRING, "What a later report hangs under"),
            OutputPort(POSTED_PORT, PortType.STRING, "'false' when the report was not sent"),
        ),
        # The engine's check runs before routes are evaluated and would fail
        # the run on unparseable stdout. A missing reference falls back.
        enforce_outputs=False,
    )


def _asked(node_id: str, answers: GateNode | None, to: Integration) -> str:
    """The buttons, as the JSON the sending program reads from argv.

    Built from the gate's own choices, so the two cannot drift. Each button
    names the step that posted it, which is how a press finds its run and how
    an older message is told from the current question, and says whether the
    choice asks for text.
    """
    if answers is None:
        return ""
    if not to.threads:
        raise CompositionError(
            f"announce node {node_id!r} offers buttons, which {to.name!r} cannot carry "
            "an answer back from"
        )
    buttons = [
        [choice.value, choice.label or choice.value, choice.prompt_for or "", choice.multiline]
        for choice in answers.choices
    ]
    return json.dumps({"gate": answers.node_id, "step": node_id, "buttons": buttons})
