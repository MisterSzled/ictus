"""Everything ictus raises, and the one base they share.

``IctusError`` is the root, and it covers more than composition: ``ConfigError``
when a folder's ``config.yaml`` will not parse, ``RunSpecError`` for its
``input.md``, ``HandshakeError`` from the websocket client, ``SlackError`` from
the bridge. A caller that wants to catch *anything ictus did* catches that one.

The subclasses below are the composition and emission half, and they are here
rather than beside the code that raises them because every package raises them:
``graph`` while a pipeline is being written, ``lint`` while it is being checked,
``interfaces`` while it is being lowered.

Every error names what was being attempted and where, so a failure is
actionable without reading the traceback.
"""

from __future__ import annotations

__all__ = [
    "CompositionError",
    "EmitError",
    "IctusError",
    "LintError",
    "PortTypeError",
    "UnknownPortError",
]


class IctusError(Exception):
    """Base class for every error ictus raises."""


class CompositionError(IctusError):
    """Raised when a graph is assembled in a way Conductor cannot express.

    Raised at the point the invalid state enters the graph — ``connect``,
    ``branch``, ``add_node`` — never deferred to emission.
    """


class PortTypeError(CompositionError):
    """Raised when an output port is wired to an incompatible input port."""


class UnknownPortError(CompositionError):
    """Raised when a port name does not exist on the referenced node."""


class EmitError(IctusError):
    """Raised when a graph cannot be lowered to a Conductor workflow."""


class LintError(IctusError):
    """Raised when the composition lints reject a graph."""

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        body = "\n".join(f"  - {v}" for v in violations)
        super().__init__(f"{len(violations)} lint violation(s):\n{body}")
