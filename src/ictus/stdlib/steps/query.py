"""Ask a database something, as a step in the graph. No model call.

Nothing here knows Postgres, or SQLite, or any engine. A ``Datasource`` carries
the program that runs one statement and this builds the step that runs it — so
a second engine is a new module under ``ictus.sources`` and no change at all to
the graph, the stdlib, or any pipeline already written.

The read-only check is the reason this is its own step rather than a ``shell``
with a command in it. ``Datasource.read_only`` is a claim the constructor makes
and this refuses to build without, so a pipeline pointed at a writable
connection fails while somebody is writing it, with a message, rather than at
the moment generated SQL reaches a real table.

A failed query never fails the run. The step always succeeds; one that could
not run says ``ran: "false"`` and puts why in ``why`` and on stderr. The step
after it reads both and decides — which is the point of it being a node, and
the difference between a query that could not run and a run that could not
finish.
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

#: The result, as JSON text. A string rather than a list because what reads it
#: is usually a prompt, and a model is handed the rows to look at rather than a
#: structure to index.
ROWS_PORT = "rows"

#: How many rows the statement actually returned, before any clipping.
COUNT_PORT = "count"

#: "true" when the statement ran, "false" when it was refused or failed.
RAN_PORT = "ran"

#: Why it did not run, or what was clipped when it did.
WHY_PORT = "why"

#: Rows past this are dropped, and ``why`` says so. A database answers with as
#: much as it is asked for, and the usual next step is a prompt with a context
#: window; a step that silently filled one is harder to notice than a step that
#: says it showed the first two hundred.
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

    ``sql`` is usually a reference to whatever wrote it — a model, an input, an
    earlier step — so the statement is data flowing along an edge like anything
    else, and is visible in the dashboard and in ``ictus trace`` as the text the
    step was actually given.

    ``environment`` picks which of a fleet source's environments to read, and
    is usually a reference: a ticket says where a change is going. A source
    holding one connection ignores it, and one holding several refuses a name
    it was never built with.

    Refuses a source that does not promise ``read_only``. There is no flag to
    override that: a step that may write is a different step, and spelling the
    difference as an argument would mean the dangerous one is a typo away.
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
            # Through argv rather than baked into the program: these are
            # rendered values, and interpolating one into source is how a quote
            # in somebody's data becomes a syntax error at run time.
            str(limit),
            # The program gives up at `timeout`; the engine kills the step five
            # seconds later. The gap is what keeps a slow query from being a
            # failed step.
            str(timeout),
            # Which environment to read, when the source holds several. Empty
            # for a source that holds one, which ignores it.
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
        # Not a contract the engine enforces. Its check runs before routes are
        # evaluated and fails the run on stdout it cannot parse; the program
        # always prints every field, and a reference that finds none falls back
        # rather than raising.
        enforce_outputs=False,
    )
