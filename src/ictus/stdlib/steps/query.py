"""Ask a database something, as a step in the graph. No model call.

Nothing here knows any engine: a ``Datasource`` carries the program that runs
one statement, and this builds the step that runs it.

Its own step rather than a ``shell``, because it refuses a source that does
not promise ``read_only``.

A failed query never fails the run: the step always succeeds, says
``ran: "false"``, and puts why in ``why`` and on stderr.
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
    from ictus.graph.requirements import Datasource

__all__ = ["COUNT_PORT", "RAN_PORT", "ROWS_PORT", "WHY_PORT", "query"]

#: The result, as JSON text. A string, because what reads it is usually a prompt.
ROWS_PORT = "rows"

#: How many rows the statement actually returned, before any clipping.
COUNT_PORT = "count"

#: "true" when the statement ran, "false" when it was refused or failed.
RAN_PORT = "ran"

#: Why it did not run, or what was clipped when it did.
WHY_PORT = "why"

#: Rows past this are dropped, and ``why`` says so.
DEFAULT_LIMIT = 200


def query(
    *,
    node_id: str,
    sql: str | Ref | Template,
    against: Datasource,
    environment: str | Ref | Template = "",
    description: str = "",
    inputs: Sequence[InputPort] = (),
    limit: int = DEFAULT_LIMIT,
    timeout: int = 30,
) -> ScriptNode:
    """Run ``sql`` against ``against``, and publish what came back.

    ``sql`` is usually a reference to whatever wrote it, so the statement flows
    along an edge and is visible in the dashboard and in ``ictus trace``.

    ``environment`` picks which of a fleet source's environments to read. A
    source holding one connection ignores it; one holding several refuses a
    name it was not built with.

    Refuses a source that does not promise ``read_only``, with no override.
    """
    if not against.read_only:
        raise CompositionError(
            f"query node {node_id!r} reads from {against.name!r}, which does not promise "
            "read_only. Build it with a constructor in ictus.sources that does — a "
            "connection that could write is a different kind of step"
        )
    if limit < 1:
        raise CompositionError(f"query node {node_id!r} needs a limit of at least one row")
    if timeout < 1:
        raise CompositionError(f"query node {node_id!r} needs a timeout of at least 1s")

    return ScriptNode(
        node_id=node_id,
        description=description or f"Ask {against.name}",
        inputs=tuple(inputs),
        command=against.command,
        args=(
            "-c",
            against.program,
            # Through argv, not interpolated into the program: a quote in
            # somebody's data would otherwise be a syntax error.
            str(limit),
            # The program gives up at `timeout`, the engine five seconds later,
            # so a slow query is not a failed step.
            str(timeout),
            # Which environment to read. Empty for a source holding one.
            as_template(environment),
        ),
        uses=(against.name,),
        stdin=as_template(sql),
        timeout=timeout + 5,
        declared_outputs=(
            OutputPort(ROWS_PORT, PortType.STRING, "What came back, as JSON text"),
            OutputPort(COUNT_PORT, PortType.STRING, "How many rows, before clipping"),
            OutputPort(RAN_PORT, PortType.STRING, "'false' when it was refused or failed"),
            OutputPort(WHY_PORT, PortType.STRING, "Why it did not run, or what was clipped"),
        ),
        # The engine's check runs before routes are evaluated and would fail
        # the run on unparseable stdout. A missing reference falls back.
        enforce_outputs=False,
    )
