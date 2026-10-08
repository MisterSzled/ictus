"""Read the ticket a run was asked about, and nothing else.

A stage rather than a bare step, because a stage is where a permission can be
drawn. It compiles to its own workflow file with its own ``runtime:``, so what
is declared inside it is declared *only* inside it: the credential that reads a
tracker is required by this body and by nothing else in the pipeline, and a
step elsewhere that tried to use it is refused while the pipeline is written.

Nothing here knows Jira. It takes a ``Datasource`` that promises ``read_only``
and asks it for one identifier, so a second tracker is a new module under
``ictus.sources``.

Outcomes are ``read`` and ``missing``. Missing is a value and not a failure: a
link to a ticket somebody deleted, or a token that expired overnight, is
something the rest of the run should decide about rather than something that
throws away the work in front of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.graph.ports import PortType
from ictus.graph.ref import equals
from ictus.graph.scope import Scope, outcome_scope
from ictus.stdlib.steps.fetch import fetch

if TYPE_CHECKING:
    from ictus.graph.requirements import Datasource

__all__ = ["MISSING", "READ", "read_ticket"]

#: The ticket came back.
READ = "read"

#: It did not, and ``why`` says what happened.
MISSING = "missing"

TICKET = "ticket"
WHY = "why"
STR = PortType.STRING


def read_ticket(
    *,
    stage_id: str = "read_ticket",
    against: Datasource,
    timeout: int = 20,
    description: str = "",
    subject: str = "the ticket",
) -> Scope:
    """Fetch whatever the ``link`` input names, through ``against``.

    ``link`` takes the identifier or the URL it arrived in; which of those a
    source accepts, and whether a URL points somewhere its credential was meant
    to go, is the source's business and not this stage's.

    Both outcomes carry ``ticket`` and ``why``, so a caller can route on
    ``missing`` and still read the reason without a second edge.
    """
    scope = outcome_scope(
        stage_id=stage_id,
        outcomes=(READ, MISSING),
        carry={TICKET: STR, WHY: STR},
        description=description or f"Read {subject}",
    )
    body = scope.body
    # Declared here and nowhere above: the whole point of putting this in a
    # stage is that its credential is required by this file alone.
    body.require_datasource(against)
    link = body.declare_input("link", STR, description=f"The identifier or URL of {subject}")

    got = body.add(
        fetch(
            node_id="ask",
            what=link.ref(),
            against=against,
            description=f"Read {subject} from {against.name}",
            timeout=timeout,
        )
    )
    body.set_entry(got)

    carried = {TICKET: got.ref("found"), WHY: got.ref("why")}
    here = scope.exit(node_id=READ, outcome=READ, reason=f"Read {subject}", **carried)
    gone = scope.exit(
        node_id=MISSING, outcome=MISSING, reason=f"Could not read {subject}", **carried
    )
    body.route(got, here, when=equals(got.ref("got"), "true"))
    body.route(got, gone)
    return scope
