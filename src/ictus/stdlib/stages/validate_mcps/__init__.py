"""Check every MCP server a pipeline needs, at once, before the real work starts.

The checks run in parallel, one verdict step reads their results, and a gate
stands between a failed check and the rest of the run — so somebody can go and
connect the thing, choose *Retry*, and not lose the run.

A parallel group may only hold model calls and computations, so the verdict
step and the gate sit after the group rather than inside it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.graph.composition import FailureMode
from ictus.graph.node import AgentNode
from ictus.graph.ports import InputPort, OutputPort, PortType
from ictus.graph.ref import optional, ref_to, tpl
from ictus.graph.stage import Stage
from ictus.prompting import prompt
from ictus.stdlib.exits.succeed import succeed
from ictus.stdlib.gates.choice import choice_gate
from ictus.stdlib.llm.remediate import remediate
from ictus.stdlib.llm.validate_mcp import validate_mcp

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ictus.graph.requirements import McpServer

__all__ = ["validate_mcps"]

BOOL, STR = PortType.BOOLEAN, PortType.STRING


def validate_mcps(
    *,
    stage_id: str = "validate-mcps",
    servers: Sequence[McpServer],
    description: str = "",
    retries: int = 2,
) -> Stage:
    """A stage that proves every declared MCP server is reachable from the model.

    ``retries`` is how many times a human may fix something and re-run the
    checks, and becomes the stage's loop bound.

    The gate offers help as well as retry: choosing it opens a conversation
    with an agent that diagnoses the failure. A claimed fix re-runs the checks;
    a reported failure returns to the gate carrying what it found.

    Contract: no inputs; exposes ``report`` (string) describing what was found.
    """
    if not servers:
        raise ValueError("validate_mcps needs at least one server to check")

    stage = Stage(
        stage_id=stage_id,
        description=description or f"Check {len(servers)} MCP server(s) respond",
        loop_passes=retries + 1,
    )
    body = stage.body

    checks = [body.add(validate_mcp(server)) for server in servers]

    # Conductor requires at least two members in a parallel group.
    start = (
        body.parallel(
            "checks",
            checks,
            description="Probe every declared server at once",
            # Every failure, not just the first.
            failure_mode=FailureMode.CONTINUE_ON_ERROR,
        )
        if len(checks) > 1
        else checks[0]
    )

    verdict = body.add(
        AgentNode(
            node_id="verdict",
            description="Decide whether every server is usable",
            inputs=tuple(
                port
                for check in checks
                for port in (
                    InputPort(f"{check.node_id}_available", BOOL),
                    InputPort(f"{check.node_id}_detail", STR),
                )
            ),
            prompt=tpl(
                prompt(__name__, "report") + "\n\n",
                *[
                    part
                    for check in checks
                    for part in (
                        f"--- {check.node_id} ---\navailable: ",
                        check.ref("available"),
                        "\ndetail: ",
                        check.ref("detail"),
                        "\n\n",
                    )
                ],
            ),
            declared_outputs=(
                OutputPort("all_ok", BOOL, "Whether every server answered"),
                OutputPort("report", STR, "What was found, per server"),
            ),
        )
    )
    for check in checks:
        body.feed(check, "available", verdict, f"{check.node_id}_available")
        body.feed(check, "detail", verdict, f"{check.node_id}_detail")

    gate = body.add(
        choice_gate(
            node_id="unblock",
            description="A server is unreachable",
            # Declared, though a gate currently renders undeclared references
            # anyway: that behaviour is observed, not promised.
            inputs=(
                InputPort("report", STR),
                InputPort("prior_attempt", STR, "What an earlier fix reported", optional=True),
            ),
            prompt=tpl(
                prompt(__name__, "blocked") + "\n\n",
                verdict.ref("report"),
                # A forward reference into the helper. On the first visit
                # there is nothing to show and the compiler omits the block.
                optional(
                    "\n\nAn earlier attempt to resolve this reported:\n",
                    ref_to("assist", "summary", STR),
                ),
                "\n\n" + prompt(__name__, "choose"),
            ),
            choices=(
                ("assist", "Help me fix it"),
                ("retry", "I have fixed it — check again"),
                ("abort", "Abort the run"),
            ),
        )
    )
    helper = body.add(
        remediate(
            node_id="assist",
            subject="an MCP server the run depends on",
            problem=tpl(verdict.ref("report")),
            inputs=(InputPort("report", STR),),
        )
    )
    body.feed(verdict, "report", helper, "report")
    body.feed(verdict, "report", gate, "report")
    body.feed(helper, "summary", gate, "prior_attempt")

    ready = body.add(succeed(node_id="ready", reason="Every declared MCP server responded."))
    aborted = body.add(
        succeed(
            node_id="aborted",
            reason="Aborted by the operator: a required MCP server was unreachable.",
        )
    )

    body.set_entry(start)
    body.route(start, verdict)
    body.route(verdict, ready, when=tpl(verdict.ref("all_ok")))
    body.route(verdict, gate)
    body.branch(gate, {"assist": helper, "retry": start, "abort": aborted})
    # A claimed fix is verified; a reported failure goes back to the gate.
    body.route(helper, start, when=tpl(helper.ref("resolved")))
    body.route(helper, gate)
    body.expose_output("report", verdict, "report")
    return stage
