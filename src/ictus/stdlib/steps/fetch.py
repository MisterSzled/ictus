"""Ask a source for one named thing, as a step in the graph. No model call.

The sibling of ``query``: a query sends a statement and gets rows, this sends
an identifier and gets the one thing it names.

Nothing here knows any service; a ``Datasource`` carries the program that does
the reading.

Refuses a source that does not promise ``read_only``, with no override.

A read that fails never fails the run: the step says ``got: "false"`` and puts
why in ``why`` and on stderr.
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

__all__ = ["FOUND_PORT", "GOT_PORT", "WHY_PORT", "fetch"]

#: What came back, as JSON text. A string, because what reads it is a prompt.
FOUND_PORT = "found"

#: "true" when it was read, "false" when it could not be.
GOT_PORT = "got"

#: Why it could not be read. Empty when it was.
WHY_PORT = "why"


def fetch(
    *,
    node_id: str,
    what: str | Ref | Template,
    against: Datasource,
    description: str = "",
    inputs: Sequence[InputPort] = (),
    timeout: int = 20,
) -> ScriptNode:
    """Read whatever ``what`` names, out of ``against``.

    ``what`` is usually a reference. A bare identifier or a URL; resolving and
    vetting one is the source's business.
    """
    if not against.read_only:
        raise CompositionError(
            f"fetch node {node_id!r} reads from {against.name!r}, which does not promise "
            "read_only. Build it with a constructor in ictus.sources that does"
        )
    if timeout < 1:
        raise CompositionError(f"fetch node {node_id!r} needs a timeout of at least 1s")

    return ScriptNode(
        node_id=node_id,
        description=description or f"Read from {against.name}",
        inputs=tuple(inputs),
        command=against.command,
        args=(
            "-c",
            against.program,
            # Through argv, not interpolated into the program: a quote in
            # somebody's data would otherwise be a syntax error.
            as_template(what),
            # The program gives up at `timeout`, the engine five seconds later,
            # so a slow answer is not a failed step.
            str(timeout),
        ),
        timeout=timeout + 5,
        uses=(against.name,),
        declared_outputs=(
            OutputPort(FOUND_PORT, PortType.STRING, "What was read, as JSON text"),
            OutputPort(GOT_PORT, PortType.STRING, "'false' when it could not be read"),
            OutputPort(WHY_PORT, PortType.STRING, "Why it could not be read"),
        ),
        # Not a contract the engine enforces. Its check runs before routes are
        # evaluated and fails the run on stdout it cannot parse; the program
        # always prints every field, and a reference that finds none falls back
        # rather than raising.
        enforce_outputs=False,
    )
