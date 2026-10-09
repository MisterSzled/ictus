"""The start gate — a person confirms before a pipeline does anything.

Conductor's dashboard has no start: its API offers ``stop``, ``kill`` and
``resume`` only (``web/server.py``). A human gate at the entry point is the
equivalent, and costs no provider call.

Added at emit time, so what is committed in ``build/`` is what runs. On by
default; turn it off with ``start_gate: false`` in ``config.yaml``.

The prompt reads the workflow's own inputs. Optional ones are guarded on
truthiness, since the engine binds an absent one to ``None``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.errors import CompositionError
from ictus.graph.node import GateChoice, GateNode
from ictus.graph.ports import InputPort
from ictus.graph.ref import TemplatePart, optional, tpl
from ictus.graph.traversal import budget_cost, total_cost
from ictus.stdlib.exits.succeed import succeed

if TYPE_CHECKING:
    from ictus.graph.pipeline import Pipeline

__all__ = ["CANCELLED_ID", "GATE_ID", "add_start_gate", "attach_start_herald"]

GATE_ID = "confirm_start"
CANCELLED_ID = "not_started"


def add_start_gate(pipeline: Pipeline) -> Pipeline:
    """Put a confirmation gate in front of ``pipeline``'s entry point.

    Choosing not to start ends the run successfully; nothing was attempted.
    """
    for taken in (GATE_ID, CANCELLED_ID):
        if any(node.node_id == taken for node in pipeline.nodes):
            raise CompositionError(
                f"pipeline {pipeline.pipeline_id!r} already has a node called {taken!r}, "
                "which the start gate needs. Rename it, or set start_gate: false."
            )
    # Read before the gate exists, so the prompt describes the work.
    entry = pipeline.entry()
    herald = pipeline.start_herald
    if herald is not None and herald is entry:
        raise CompositionError(
            f"pipeline {pipeline.pipeline_id!r} both names {herald.node_id!r} as its entry "
            "point and asks to run it before the start gate; leave the entry on the first "
            "real step and the placement is worked out from there"
        )
    parts: list[TemplatePart] = [
        f"Start **{pipeline.pipeline_id}**?\n\n",
    ]
    if pipeline.description:
        parts.append(f"{pipeline.description}\n\n")
    # Counted before the gate's own two nodes are added.
    once, budget = total_cost(pipeline), budget_cost(pipeline)
    if budget > once:
        parts.append(
            f"{once} step(s) on one pass, up to {budget} with loops, "
            f"beginning with `{entry.node_id}`.\n"
        )
    else:
        parts.append(f"{once} step(s), beginning with `{entry.node_id}`.\n")

    declared = pipeline.workflow_inputs
    if declared:
        parts.append("\n---\n")
        for param in declared:
            body: list[TemplatePart] = [
                f"\n**{param.name}**\n\n```\n",
                param.ref(),
                "\n```\n",
            ]
            # An optional input renders nothing rather than the word None.
            parts.append(tpl(*body) if param.required else optional(*body))

    gate = pipeline.add(
        GateNode(
            node_id=GATE_ID,
            description="Confirm before anything runs",
            prompt=tpl(*parts),
            inputs=tuple(
                InputPort(param.name, param.port_type, optional=not param.required)
                for param in declared
            ),
            choices=(
                # Start first: `--skip-gates` takes the first option.
                GateChoice("start", "Start the run"),
                GateChoice("cancel", "Stop — do not run"),
            ),
        )
    )
    stopped = pipeline.add(
        succeed(
            node_id=CANCELLED_ID,
            reason="Stopped at the start gate; nothing was run.",
            result={"started": "false"},
        )
    )
    for param in declared:
        pipeline.connect_input(param, gate, param.name)
    pipeline.branch(gate, {"start": entry, "cancel": stopped})
    if herald is None:
        pipeline.set_entry(gate)
    else:
        # Something gets to speak before the run parks.
        pipeline.route(herald, gate)
        pipeline.set_entry(herald)
    return pipeline


def attach_start_herald(pipeline: Pipeline) -> Pipeline:
    """Put whatever ``before_start_gate`` named in front, with no gate behind it.

    The other half of the policy: without it a herald would be unreachable
    whenever the gate is off.
    """
    herald = pipeline.start_herald
    if herald is None:
        return pipeline
    entry = pipeline.entry()
    if herald is entry:
        return pipeline
    pipeline.route(herald, entry)
    pipeline.set_entry(herald)
    return pipeline
