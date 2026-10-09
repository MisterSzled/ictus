"""Add a remark to a named item, as a step in the graph. No model call.

Nothing here knows any tracker: an ``Integration`` carries the program that
posts one remark, and this builds the step that runs it.

Distinct from ``announce``, which goes to a configured place. A comment goes
to an item the graph is carrying. The programs take different arguments, and
``Integration`` declares which it is.

A comment that does not land never fails the run: the step says
``posted: "false"`` and leaves its reason in ``why`` and on stderr.
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

#: Which item was commented on, as the service resolved it — a key, not a URL.
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

    ``on`` identifies the item, usually by reference. A bare key or a URL;
    resolving and vetting one is the integration's job.

    ``body`` is usually a reference to whatever wrote it, so what gets posted
    is visible in the dashboard and in ``ictus trace``.
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
            # Through argv, not interpolated into the program: a quote in
            # somebody's data would otherwise be a syntax error.
            as_template(on),
            # The program gives up at `timeout`, the engine five seconds later,
            # so a slow tracker is not a failed step.
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
        # The engine's check runs before routes are evaluated and would fail
        # the run on unparseable stdout. A missing reference falls back.
        enforce_outputs=False,
    )
