"""A subprocess step. Conductor ``type: script`` — deterministic, no model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.graph.node import ScriptNode

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ictus.graph.ports import InputPort, OutputPort
    from ictus.graph.ref import Template

__all__ = ["shell"]


def shell(
    *,
    node_id: str,
    command: str,
    args: Sequence[str | Template] = (),
    outputs: Sequence[OutputPort] = (),
    description: str = "",
    inputs: Sequence[InputPort] = (),
    stdin: str | Template | None = None,
    timeout: int | None = None,
    working_dir: str | None = None,
    enforce_outputs: bool = True,
) -> ScriptNode:
    """Run a command, with no model in the loop.

    Three things bite:

    * A relative ``command`` resolves against ``working_dir``, which defaults
      to the process's current directory — not the repo root, and not where
      the workflow file lives.
    * ``args`` goes on the command line and hits the OS length cap; pass large
      payloads through ``stdin``.
    * Declaring ``outputs`` makes stdout a contract: the command must print a
      JSON object or Conductor raises, after it has already run. Leave
      ``outputs`` empty for a command that prints prose.

    A stdout object is merged over ``{stdout, stderr, exit_code}``, so declared
    ``outputs`` can name its fields directly.

    ``enforce_outputs=False`` keeps the ports and drops the contract, which is
    the only way to route on a command that failed: the raise lands before
    routes are evaluated. ``stdlib.try_shell`` is that shape, already wired.
    """
    return ScriptNode(
        node_id=node_id,
        description=description,
        inputs=tuple(inputs),
        command=command,
        args=tuple(args),
        stdin=stdin,
        timeout=timeout,
        working_dir=working_dir,
        declared_outputs=tuple(outputs),
        enforce_outputs=enforce_outputs,
    )
