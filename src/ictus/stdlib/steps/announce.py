"""Tell somebody something, as a step in the graph. No model call.

Nothing here knows where a report goes. An ``Integration`` carries the program
that sends one, and this builds the step that runs it — so a second destination
is a new module under ``ictus.notify`` and no change at all to the graph, the
stdlib, or any pipeline already written.

Being a node is the advantage over watching from outside. It is costed against
``max_iterations``, routed like anything else, and visible in the dashboard and
in ``ictus trace``. What it cannot report is what no step can see: a budget
tripping, the engine being killed. That is ``ictus watch``.

A report never fails the run. The step always succeeds; one that could not
deliver says ``posted: "false"`` and leaves its reason on stderr, which the
dashboard and ``ictus trace`` both show. The alternative was tried first and it
is backwards: a channel being down, or a token rotated mid-run, ended a run
whose work had succeeded — at the gate, or just before the finish, after
everything had been paid for. Whether the variables a destination needs are set
is checked before the run starts, by preflight, which is where a misconfigured
destination belongs.

Most pipelines should not call this directly. ``pipeline.integrate(...)`` attaches
a destination to every gate and exit at once and wires the thread between them,
which is the mechanical part nobody should be writing by hand. Reach for this
when one particular point deserves one particular sentence.
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

#: What an announcement publishes so a later one can hang under it.
#:
#: Named for what it is rather than for what any service calls it. Slack's own
#: spelling is ``thread_ts``, and translating it is the integration's job.
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

    ``thread`` is the port an earlier announcement published, so this one hangs
    under it. The input that needs is declared for you; the *data edge* is yours
    to wire with ``feed``, and the lint refuses the graph without it.

    ``answers=<gate>`` puts that gate's choices in the message as buttons. They
    are read off the gate and cannot be spelled out by hand: a renamed option
    would leave a button that answers nothing, and a value the gate does not
    offer is worse than that — the engine accepts the answer and then fails the
    run on it.
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
            # Through argv rather than baked into the program: these are rendered
            # values, and interpolating one into source is how a quote in
            # somebody's data becomes a syntax error at run time. The fallback
            # covers a parent that printed no thread; an undefined reference is
            # a template error, and that would fail the step this exists to keep
            # from failing.
            as_template(thread.or_else("")) if thread is not None else "",
            asks,
            # The program gives up at `timeout`; the engine kills the step five
            # seconds later. The gap is what keeps a slow report from being a
            # failed step.
            str(timeout),
        ),
        uses=(to.name,),
        stdin=text,
        timeout=timeout + 5,
        declared_outputs=(
            OutputPort(THREAD_PORT, PortType.STRING, "What a later report hangs under"),
            OutputPort(POSTED_PORT, PortType.STRING, "'false' when the report was not sent"),
        ),
        # Not a contract the engine enforces. Its check runs before routes are
        # evaluated and fails the run on stdout it cannot parse; the program
        # always prints both fields, and a reference that finds neither falls
        # back rather than raising.
        enforce_outputs=False,
    )


def _asked(node_id: str, answers: GateNode | None, to: Integration) -> str:
    """The buttons, as the JSON the sending program reads from argv.

    Taking the gate itself is the point: its choices *are* the buttons, so the
    two cannot drift and a renamed option cannot leave a dead one.

    Each button also says which step posted it, which is how a press finds its
    run and how a press on an older message is told from one on the current
    question: the run's own history records what every step posted. And a
    choice that asks for text says so, so whatever answers it can ask too
    rather than sending the choice without the text it exists to collect.
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
