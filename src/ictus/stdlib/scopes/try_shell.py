"""One command, whose failure the caller routes on instead of inheriting.

Two facts about script steps:

* **A non-zero exit is not a failure to Conductor.** ``executor/script.py``
  returns ``exit_code`` alongside stdout and stderr, and nothing in
  ``engine/workflow.py`` branches on it; the next step runs.
* **Declaring ``output:`` raises instead.** The engine parses stdout as JSON
  and raises when it is not an object, before ``_evaluate_routes``, so the
  route written for the failure never fires.

``try_shell`` gives the contract up to get the branch: the step declares its
ports for composition but emits no ``output:``, so nothing is parsed, nothing
raises, and ``exit_code`` decides which exit is taken.

A command that never started is still ``ExecutionError`` and not routable;
that is ``require_executable``'s. This scope handles one that ran and failed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.errors import CompositionError
from ictus.graph.ports import InputPort, OutputPort, PortType
from ictus.graph.ref import equals, tpl
from ictus.graph.scope import outcome_scope
from ictus.stdlib.scopes.outcomes import FAILED, OK
from ictus.stdlib.steps.shell import shell

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ictus.graph.composition import WorkflowInput
    from ictus.graph.ref import Ref, Template
    from ictus.graph.scope import Scope

__all__ = ["FAILED", "OK", "try_shell"]

STDOUT = "stdout"
STDERR = "stderr"
EXIT_CODE = "exit_code"

#: What the engine stores for every script step whether or not it printed
#: anything. Carried by both exits.
BASELINE: dict[str, PortType] = {
    STDOUT: PortType.STRING,
    STDERR: PortType.STRING,
    EXIT_CODE: PortType.NUMBER,
}


def try_shell(
    *,
    stage_id: str,
    command: str,
    args: Sequence[str | Template] = (),
    node_id: str = "run",
    parameter: str | None = None,
    outputs: Sequence[OutputPort] = (),
    stdin: str | Template | None = None,
    timeout: int | None = None,
    working_dir: str | None = None,
    description: str = "",
) -> Scope:
    """Run one command; report ``ok`` or ``failed`` rather than raising.

    Both outcomes carry ``stdout``, ``stderr`` and ``exit_code``.

    ``parameter`` declares an input the caller wires, appended to ``args`` as
    the command's last argument.

    ``outputs`` names JSON fields the command prints, addressable on the ``ok``
    branch. **Print every one of them whenever the command exits 0** —
    Conductor renders with ``StrictUndefined``, so an omitted field raises at
    the reference. On the ``failed`` exit they arrive as empty values of their
    declared type.

    Contract: optional input named by ``parameter``; outcomes ``ok`` and
    ``failed``.
    """
    reserved = sorted({port.name for port in outputs} & set(BASELINE))
    if reserved:
        raise CompositionError(
            f"try_shell {stage_id!r} declares {reserved} in outputs, which the engine already "
            "supplies for every script step. A JSON field of that name shadows the baseline "
            "silently — rename the field the command prints."
        )
    if parameter is not None and not parameter.strip():
        raise CompositionError(f"try_shell {stage_id!r} has a malformed parameter {parameter!r}")

    carry: dict[str, PortType | OutputPort] = {**BASELINE}
    carry.update({port.name: port for port in outputs})

    scope = outcome_scope(
        stage_id=stage_id,
        outcomes=(OK, FAILED),
        carry=carry,
        description=description or f"Run {command}, reporting whether it worked",
    )
    body = scope.body

    param: WorkflowInput | None = None
    threaded: tuple[str | Template, ...] = ()
    inputs: tuple[InputPort, ...] = ()
    if parameter is not None:
        param = body.declare_input(
            parameter, PortType.STRING, description=f"Passed to {command} as its last argument"
        )
        threaded = (tpl(param.ref()),)
        inputs = (InputPort(parameter, PortType.STRING),)

    run = body.add(
        shell(
            node_id=node_id,
            description=f"Run {command}",
            command=command,
            args=(*args, *threaded),
            inputs=inputs,
            stdin=stdin,
            timeout=timeout,
            working_dir=working_dir,
            outputs=(
                OutputPort(STDOUT, PortType.STRING, "What the command printed"),
                OutputPort(STDERR, PortType.STRING, "What it printed to stderr"),
                OutputPort(EXIT_CODE, PortType.NUMBER, "The status it exited with"),
                *outputs,
            ),
            # The whole construct: with a schema emitted, a command that dies
            # raises before either route below is looked at.
            enforce_outputs=False,
        )
    )
    body.set_entry(run)
    if param is not None:
        body.connect_input(param, run, param.name)

    baseline: dict[str, Ref | Template | str] = {name: run.ref(name) for name in BASELINE}
    broke = scope.exit(
        node_id=FAILED,
        outcome=FAILED,
        reason=tpl(f"{command} exited with status ", run.ref(EXIT_CODE)),
        **baseline,
    )
    worked = scope.exit(
        node_id=OK,
        outcome=OK,
        reason=f"{command} succeeded",
        **baseline,
        **{port.name: run.ref(port.name) for port in outputs},
    )

    # Success is the tested branch and failure the catch-all: a child killed
    # by a signal exits `-N`, which `exit_code >= 1` would read as clean.
    body.route(run, worked, when=equals(run.ref(EXIT_CODE), 0))
    body.route(run, broke)
    return scope
