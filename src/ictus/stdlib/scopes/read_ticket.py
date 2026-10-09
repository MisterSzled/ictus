"""Read the ticket a run was asked about, and nothing else.

A stage, not a bare step, because a stage is where a permission can be drawn:
it compiles to its own workflow file, so the tracker credential is required by
this body alone.

Nothing here knows any tracker; it takes a ``Datasource`` that promises
``read_only``.

Outcomes are ``read`` and ``missing``. Missing is a value, not a failure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.graph.ports import PortType
from ictus.graph.ref import equals
from ictus.graph.scope import Scope, outcome_scope
from ictus.stdlib.scopes.outcomes import MISSING, READ
from ictus.stdlib.steps.fetch import fetch

if TYPE_CHECKING:
    from ictus.graph.requirements import Datasource

__all__ = ["MISSING", "READ", "read_ticket"]

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

    ``link`` takes the identifier or the URL it arrived in; resolving and
    vetting one is the source's business.

    Both outcomes carry ``ticket`` and ``why``.
    """
    scope = outcome_scope(
        stage_id=stage_id,
        outcomes=(READ, MISSING),
        carry={TICKET: STR, WHY: STR},
        description=description or f"Read {subject}",
    )
    body = scope.body
    # Here and nowhere above: the credential belongs to this file alone.
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
