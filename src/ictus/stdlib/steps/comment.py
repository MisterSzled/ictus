"""Add a remark to a named item, as a step in the graph. No model call.

Nothing here knows Jira, or any other tracker. An ``Integration`` carries the
program that posts one remark and this builds the step that runs it — so a
second tracker is a new module under ``ictus.notify`` and no change at all to
the graph, the stdlib, or any pipeline already written.

Distinct from ``announce`` because the two are handed different things.
An announcement goes to a *place* that was configured — a channel — and
optionally hangs under an earlier message. A comment goes to an *item* the
graph is carrying: a ticket named in the message that started the run. The
programs take their arguments in different positions, and ``Integration``
declares which it is, so one driven by the step meant for the other is refused
while the pipeline is being written rather than silently posting nowhere.

A comment that does not land never fails the run. The step always succeeds; one
that could not post says ``posted: "false"`` and leaves its reason in ``why``
and on stderr. A tracker being down, or a token rotated mid-run, is not a
reason to throw away work that already succeeded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.errors import CompositionError
from ictus.graph.node import ScriptNode
from ictus.graph.ports import InputPort, OutputPort, PortType
from ictus.graph.ref import as_template

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ictus.graph.ref import Ref, Template
    from ictus.graph.requirements import Integration

__all__ = ["ITEM_PORT", "POSTED_PORT", "WHY_PORT", "comment"]

#: Which item was commented on, as the service resolved it — a ticket key, not
#: the URL it was found in. Named for what it is rather than for what any
#: tracker calls it.
ITEM_PORT = "issue"

#: "true" when the remark landed, "false" when it could not be posted.
POSTED_PORT = "posted"

#: Why it did not post. Empty when it did.
WHY_PORT = "why"


def comment(
    *,
    node_id: str,
    on: str | Ref | Template,
    body: str | Ref | Template,
    to: Integration,
    description: str = "",
    inputs: Sequence[InputPort] = (),
    timeout: int = 20,
) -> ScriptNode:
    """Post ``body`` onto ``on`` through ``to``, as a step in the graph.

    ``on`` identifies the item and is usually a reference: the ticket named in
    whatever started the run. It may be a bare key or the URL it arrived in —
    resolving one from the other is the integration's job, and so is refusing a
    URL that points somewhere the credential was not meant to go.

    ``body`` is usually a reference to whatever wrote it, so what gets posted is
    visible in the dashboard and in ``ictus trace`` as the text the step was
    actually given.
    """
    if not to.comments:
        raise CompositionError(
            f"comment node {node_id!r} posts through {to.name!r}, which does not comment "
            "on items — it reports to a place. Use announce for that, or build the "
            "integration with a constructor that comments"
        )
    if timeout < 1:
        raise CompositionError(f"comment node {node_id!r} needs a timeout of at least 1s")

    return ScriptNode(
        node_id=node_id,
        description=description or f"Comment on the item in {to.name}",
        inputs=tuple(inputs),
        command=to.command,
        args=(
            "-c",
            to.program,
            # Through argv rather than baked into the program: this is a
            # rendered value, and interpolating one into source is how a quote
            # in somebody's data becomes a syntax error at run time.
            as_template(on),
            # The program gives up at `timeout`; the engine kills the step five
            # seconds later. The gap is what keeps a slow tracker from being a
            # failed step.
            str(timeout),
        ),
        uses=(to.name,),
        stdin=as_template(body),
        timeout=timeout + 5,
        declared_outputs=(
            OutputPort(ITEM_PORT, PortType.STRING, "The item it commented on"),
            OutputPort(POSTED_PORT, PortType.STRING, "'false' when it was not posted"),
            OutputPort(WHY_PORT, PortType.STRING, "Why it was not posted"),
        ),
        # Not a contract the engine enforces. Its check runs before routes are
        # evaluated and fails the run on stdout it cannot parse; the program
        # always prints every field, and a reference that finds none falls back
        # rather than raising.
        enforce_outputs=False,
    )
