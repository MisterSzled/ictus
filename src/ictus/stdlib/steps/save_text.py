"""Write a value another step produced to a file.

Conductor has no declarative file write, so this is a ``script`` step. Two
details:

* **The text goes in on stdin, never into the command line**, and the path
  travels as an argv element. Interpolating either into a ``sh -c`` string
  would make a model's output executable.
* **Parent directories are created**, so a missing ``reports/`` does not lose
  the file.

The file lands relative to the run's working directory, since a script step's
``working_dir`` is the process cwd.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.errors import CompositionError
from ictus.graph.node import ScriptNode
from ictus.graph.ports import OutputPort, PortType
from ictus.graph.ref import Ref, Template, tpl

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ictus.graph.ports import InputPort

__all__ = ["save_text"]

# `$1` is the path, passed as an argument; `sh` is $0 so it lands there.
#
# stdout is a JSON object because a script declaring `output:` must print one,
# and the engine raises otherwise — after the file has been written. The path
# is escaped through sed so one containing a quote still produces valid JSON.
_ESCAPED = r"""$(printf %s "$1" | sed 's/\\/\\\\/g;s/"/\\"/g')"""
_REPORT = f'printf \'{{"path":"%s"}}\' "{_ESCAPED}"'
_WRITE = f'mkdir -p "$(dirname "$1")" && cat > "$1" && {_REPORT}'
_APPEND = f'mkdir -p "$(dirname "$1")" && cat >> "$1" && {_REPORT}'


def save_text(
    *,
    node_id: str,
    text: Ref | Template | str,
    to: str | Template,
    append: bool = False,
    description: str = "",
    inputs: Sequence[InputPort] = (),
    working_dir: str | None = None,
) -> ScriptNode:
    """Write ``text`` to the file at ``to``.

    ``to`` may be a template, so a path can carry a value from the run. Costs
    one iteration and no provider call; the written path comes back as ``path``.

    Wire ``text``'s source with ``feed`` or ``connect`` and declare it in
    ``inputs``, or under ``context.mode: explicit`` the file is written empty.
    """
    if isinstance(to, str) and not to.strip():
        raise CompositionError(f"save_text {node_id!r} needs somewhere to write")
    payload = tpl(text) if isinstance(text, Ref) else text
    return ScriptNode(
        node_id=node_id,
        description=description or f"Write {to if isinstance(to, str) else 'a file'}",
        inputs=tuple(inputs),
        command="sh",
        args=("-c", _APPEND if append else _WRITE, "sh", to),
        stdin=payload,
        working_dir=working_dir,
        declared_outputs=(OutputPort("path", PortType.STRING, "The file that was written"),),
    )
