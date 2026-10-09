"""A chain of shell steps with no model in the loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ictus.errors import CompositionError
from ictus.graph.composition import END
from ictus.graph.ports import InputPort, OutputPort, PortType
from ictus.graph.stage import Stage
from ictus.stdlib.steps.shell import shell

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ictus.graph.node import ScriptNode

__all__ = ["ScriptStep", "script_sequence"]


@dataclass(frozen=True, slots=True)
class ScriptStep:
    """One command in a sequence.

    ``output`` names the JSON field the command prints on stdout; Conductor
    merges parsed stdout JSON over ``{stdout, stderr, exit_code}``, so a command
    that emits ``{"revision": "0001"}`` can declare ``output="revision"``.
    """

    node_id: str
    command: str
    args: Sequence[str] = ()
    output: str = "stdout"
    description: str = ""
    receives_previous: bool = False
    """Whether the previous step's output is appended to this step's arguments.

    Off by default: most sequences are ordered rather than piped.
    """


def script_sequence(
    *,
    stage_id: str,
    steps: Sequence[ScriptStep],
    parameter: str = "environment",
    description: str = "",
    working_dir: str | None = None,
) -> Stage:
    """Run commands in order, threading each one's output into the next.

    A stage, so the whole sequence costs the caller one iteration rather than
    one per command.

    Every command receives ``parameter`` as its final argument; a step with
    ``receives_previous`` also receives the previous step's output after it.

    A relative command resolves against ``working_dir``, which defaults to the
    directory the run was launched from.

    Contract: input named by ``parameter`` (string) in, output ``result``
    (string) out, taken from the last step.
    """
    if not steps:
        raise CompositionError(f"script_sequence {stage_id!r} needs at least one step")

    stage = Stage(stage_id=stage_id, description=description or f"Run {len(steps)} commands")
    param = stage.body.declare_input(
        parameter, PortType.STRING, description=f"Value passed to every command as {parameter}"
    )

    previous: ScriptNode | None = None
    previous_port = ""
    for step in steps:
        threaded = (
            (f"{{{{ {previous.node_id}.output.{previous_port} }}}}",)
            if step.receives_previous and previous is not None
            else ()
        )
        inputs = (
            (InputPort(parameter, PortType.STRING),)
            if previous is None
            else (InputPort("previous", PortType.STRING),)
        )
        node = stage.body.add(
            shell(
                node_id=step.node_id,
                description=step.description,
                command=step.command,
                args=(*step.args, f"{{{{ workflow.input.{parameter} }}}}", *threaded),
                inputs=inputs,
                working_dir=working_dir,
                outputs=(OutputPort(step.output, PortType.STRING, f"Output of {step.node_id}"),),
            )
        )
        if previous is None:
            stage.body.connect_input(param, node, parameter)
        else:
            stage.body.connect(previous, previous_port, node, "previous")
        previous, previous_port = node, step.output

    if previous is None:  # unreachable: `steps` is non-empty, checked above
        raise CompositionError(f"script_sequence {stage_id!r} produced no nodes")
    stage.body.route(previous, END)
    stage.body.expose_output("result", previous, previous_port)
    return stage
