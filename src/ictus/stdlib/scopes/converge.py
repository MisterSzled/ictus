"""Try, judge, try again — bounded, with the exhaustion routable.

Passes are counted in a ``type: set`` step (no provider call, one iteration)
and routed on, so running out is an ordinary exit with a payload rather than
``MaxIterationsError``, which no caller can catch. A :class:`Scope`, so the
caller branches on ``converged`` vs ``exhausted``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ictus.errors import CompositionError
from ictus.graph.node import AgentNode, ComputeNode, GateNode, Node
from ictus.graph.ports import InputPort, OutputPort, PortType
from ictus.graph.ref import Ref, Template, TemplatePart, at_least, optional, ref_to, tpl
from ictus.graph.scope import Scope, outcome_scope
from ictus.stdlib.gates.approval import approval_gate
from ictus.stdlib.scopes.outcomes import CONVERGED, EXHAUSTED
from ictus.stdlib.steps.counter import counter as counter_step
from ictus.stdlib.steps.wait import wait

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["CONVERGED", "EXHAUSTED", "Attempt", "converge"]

JudgeMode = Literal["model", "human", "self"]

FEEDBACK = "feedback"
PASSES = "passes"
COUNTER = "pass_number"


@dataclass(frozen=True, slots=True)
class Attempt:
    """One step of the work a pass performs.

    A sequence becomes a chain retried as a unit. Only the last one's outputs
    are what the loop converges on.
    """

    node_id: str
    prompt: str
    produces: tuple[OutputPort, ...] = ()
    description: str = ""


def converge(
    *,
    stage_id: str,
    attempt: Attempt | Sequence[Attempt],
    judge: JudgeMode = "model",
    judge_prompt: str = "",
    verdict_port: str = "approved",
    passes: int = 3,
    pause_between: float | None = None,
    remember: bool = False,
    description: str = "",
    brief: str = "What to work from",
) -> Scope:
    """A bounded try/judge loop whose give-up is a value, not a crash.

    ``judge`` picks who decides:

    * ``model`` — an agent emits ``approved`` plus ``notes``, fed into the next
      pass.
    * ``human`` — an approval gate whose rejection branch collects free text.
    * ``self`` — no judge node; the last attempt declares the verdict port.

    ``remember`` keeps each attempt's session across passes.

    Outcomes are ``converged`` and ``exhausted``, both carrying every output of
    the final attempt, the last ``feedback`` and the ``passes`` spent.

    Contract: input ``brief`` (string) in; outcomes out.
    """
    steps = [attempt] if isinstance(attempt, Attempt) else list(attempt)
    if not steps:
        raise CompositionError(f"converge {stage_id!r} needs at least one attempt")
    if passes < 1:
        raise CompositionError(f"converge {stage_id!r} needs passes >= 1, got {passes}")
    last = steps[-1]
    if judge == "self":
        if judge_prompt:
            raise CompositionError(
                f"converge {stage_id!r} uses judge='self', so the verdict comes from "
                f"{last.node_id!r} and there is no judge to prompt"
            )
        if not any(p.name == verdict_port for p in last.produces):
            declared = ", ".join(p.name for p in last.produces) or "(none)"
            raise CompositionError(
                f"converge {stage_id!r} uses judge='self' but {last.node_id!r} declares no "
                f"{verdict_port!r} output; declared: {declared}"
            )
    elif not judge_prompt:
        raise CompositionError(f"converge {stage_id!r} needs a judge_prompt for judge={judge!r}")

    carry = {p.name: p.port_type for p in last.produces}
    carry[FEEDBACK] = PortType.STRING
    carry[PASSES] = PortType.NUMBER

    scope = outcome_scope(
        stage_id=stage_id,
        outcomes=(CONVERGED, EXHAUSTED),
        carry=carry,
        description=description or f"Converge on {last.node_id}",
        loop_passes=passes,
    )
    body = scope.body
    work = body.declare_input("brief", PortType.STRING, description=brief)

    # Counting first: the bound has to be readable by the time the judge routes.
    counter = body.add(counter_step(node_id=COUNTER, description="Which pass this is"))
    body.set_entry(counter)
    body.feed(counter, "value", counter, COUNTER)

    notes_source = "judge" if judge == "model" else "review"
    feedback = ref_to(notes_source, "notes", PortType.STRING)

    previous: Node = counter
    made: list[AgentNode] = []
    for index, step in enumerate(steps):
        node = body.add(
            AgentNode(
                node_id=step.node_id,
                description=step.description or step.node_id,
                inputs=(
                    InputPort("brief", PortType.STRING),
                    *(
                        InputPort(f"{steps[index - 1].node_id}__{p.name}", p.port_type)
                        for p in (steps[index - 1].produces if index else ())
                    ),
                    *(
                        ()
                        if judge == "self" or index > 0
                        else (InputPort("notes", PortType.STRING, "Last verdict", optional=True),)
                    ),
                ),
                session_key=f"{stage_id}-{step.node_id}" if remember else None,
                prompt=_prompt(
                    step,
                    work.ref(),
                    feedback,
                    first=index == 0,
                    judged=judge != "self",
                    upstream=made[-1] if index else None,
                    upstream_ports=steps[index - 1].produces if index else (),
                ),
                declared_outputs=step.produces,
            )
        )
        body.connect_input(work, node, "brief")
        body.route(counter if index == 0 else previous, node)
        if index:
            # A control edge carries no data, so a chained step needs feeds to
            # see what the one before it produced.
            for port in steps[index - 1].produces:
                body.feed(made[-1], port.name, node, f"{steps[index - 1].node_id}__{port.name}")
        made.append(node)
        previous = node

    produced = made[-1]

    # Before the exits: the exhausted exit carries the last verdict, and a
    # carried value is wired as well as rendered.
    assessor: Node | None = None
    if judge == "model":
        assessor = body.add(
            AgentNode(
                node_id="judge",
                description="Judge the attempt",
                inputs=tuple(InputPort(p.name, p.port_type) for p in last.produces),
                prompt=tpl(judge_prompt, "\n\n", *_readback(produced, last.produces)),
                declared_outputs=(
                    OutputPort(verdict_port, PortType.BOOLEAN, "Whether this is acceptable"),
                    OutputPort("notes", PortType.STRING, "What to fix on the next pass"),
                ),
            )
        )
    elif judge == "human":
        assessor = body.add(
            approval_gate(
                node_id="review",
                description="Review the attempt",
                inputs=tuple(InputPort(p.name, p.port_type) for p in last.produces),
                prompt=tpl(judge_prompt, "\n\n", *_readback(produced, last.produces)),
                reject_label="Reject and revise",
            )
        )
    if assessor is not None:
        for port in last.produces:
            body.connect(produced, port.name, assessor, port.name)
        # What makes this a revise loop rather than a retry loop.
        body.feed(assessor, "notes", made[0], "notes")

    exhausted_when = at_least(counter.ref("value"), passes)
    carried: dict[str, Ref | Template | str] = {p.name: produced.ref(p.name) for p in last.produces}
    hit = scope.exit(
        node_id="converged",
        outcome=CONVERGED,
        reason="Converged",
        **carried,
        **{PASSES: counter.ref("value")},
    )
    gave_up = scope.exit(
        node_id="exhausted",
        outcome=EXHAUSTED,
        reason=f"Still not accepted after {passes} pass(es)",
        **carried,
        **{
            PASSES: counter.ref("value"),
            **({FEEDBACK: assessor.ref("notes")} if assessor is not None else {}),
        },
    )

    # Back to the counter: re-entering below it leaves the count stuck at 1
    # and the exhausted exit unreachable.
    retry: Node = counter
    if pause_between is not None:
        retry = body.add(
            wait(node_id="pause", seconds=pause_between, reason="Before the next pass")
        )
        body.route(retry, counter)

    if assessor is None:
        body.route(produced, hit, when=tpl(produced.ref(verdict_port)))
        body.route(produced, gave_up, when=exhausted_when)
        body.route(produced, retry)
    elif judge == "model":
        body.route(assessor, hit, when=tpl(assessor.ref(verdict_port)))
        body.route(assessor, gave_up, when=exhausted_when)
        body.route(assessor, retry)
    else:
        # A gate's branches are the human's buttons, so it cannot test the
        # counter; a zero-cost set node routes the rejection instead.
        rejected = body.add(
            ComputeNode(
                node_id="rejected",
                description="Rejected; decide whether another pass is left",
                value="rejected",
                value_type=PortType.STRING,
                inputs=(InputPort(COUNTER, PortType.NUMBER),),
                declared_outputs=(OutputPort("value", PortType.STRING, "Rejection marker"),),
            )
        )
        body.feed(counter, "value", rejected, COUNTER)
        assert isinstance(assessor, GateNode)
        body.branch(assessor, {"approved": hit, "rejected": rejected})
        body.route(rejected, gave_up, when=exhausted_when)
        body.route(rejected, retry)
    return scope


def _prompt(
    step: Attempt,
    brief: Ref,
    feedback: Ref,
    *,
    first: bool,
    judged: bool,
    upstream: AgentNode | None = None,
    upstream_ports: Sequence[OutputPort] = (),
) -> Template:
    """The attempt's instruction, with last pass's verdict folded in.

    The feedback block renders only once there is feedback.
    """
    parts: list[TemplatePart] = [step.prompt, "\n\n", brief]
    if upstream is not None:
        for port in upstream_ports:
            parts += [f"\n\n--- {upstream.node_id}.{port.name} ---\n", upstream.ref(port.name)]
    if first and judged:
        parts += [
            "\n\n",
            optional(
                "A previous attempt was rejected with these notes — address each one:\n",
                feedback,
            ),
        ]
    return tpl(*parts)


def _readback(node: AgentNode, ports: Sequence[OutputPort]) -> list[TemplatePart]:
    """The attempt's outputs, labelled, for whoever is judging them."""
    out: list[TemplatePart] = []
    for port in ports:
        out += [f"{port.name}:\n", node.ref(port.name), "\n\n"]
    return out
