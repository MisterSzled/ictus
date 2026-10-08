"""Ask a source for one named thing, as a step in the graph. No model call.

The sibling of ``query``, and separate from it because the two ask differently.
A query sends a statement and gets rows; this sends an *identifier* — a ticket
key, a link somebody pasted — and gets the one thing it names. Folding them
together would mean a parameter called ``sql`` holding a URL.

Nothing here knows Jira, or any other service. A ``Datasource`` carries the
program that does the reading and this builds the step that runs it, so a
second place to read from is a new module under ``ictus.sources`` and no change
at all to the graph, the stdlib, or any pipeline already written.

Refuses a source that does not promise ``read_only``, for the same reason
``query`` does: a step that may change what it touched is a different step, and
spelling the difference as an argument makes the dangerous one a typo away.

A read that fails never fails the run. The step always succeeds; one that could
not read says ``got: "false"`` and puts why in ``why`` and on stderr. What reads
it next decides — which is the point of it being a node.
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

#: What came back, as JSON text. A string rather than an object because what
#: reads it is usually a prompt, and a model is handed the thing to look at
#: rather than a structure to index.
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

    ``what`` is usually a reference: the link in the message that started the
    run. Whether that is a bare identifier or the URL it arrived in is the
    source's business — and so is refusing a URL pointing somewhere the
    credential was not meant to go.
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
            # Through argv rather than baked into the program: this is a
            # rendered value, and interpolating one into source is how a quote
            # in somebody's data becomes a syntax error at run time.
            as_template(what),
            # The program gives up at `timeout`; the engine kills the step five
            # seconds later. The gap is what keeps a slow answer from being a
            # failed step.
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
