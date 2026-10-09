"""The outcome names scopes exit by, each defined once.

A scope's vocabulary is closed and ``Pipeline.branch_on_outcome`` refuses to
leave a member unrouted, so these are what an author writes to route an ending.

Here rather than beside each scope, because several scopes share an ending and
``ictus.stdlib`` re-exports them flat.
"""

from __future__ import annotations

__all__ = [
    "AGREED",
    "ANSWERED",
    "CONVERGED",
    "EXHAUSTED",
    "FAILED",
    "HALTED",
    "MISSING",
    "OK",
    "READ",
    "UNRESOLVED",
]

#: Everybody is satisfied. ``council`` and ``roundtable``.
AGREED = "agreed"

#: The rounds ran out with somebody still dissenting; the work is carried
#: anyway. ``council`` and ``roundtable``.
UNRESOLVED = "unresolved"

#: A person interrupted and stopped it. Only with ``interject``.
HALTED = "halted"

#: The judge accepted an attempt. ``converge``.
CONVERGED = "converged"

#: The passes ran out; whatever was held at that moment is carried anyway.
#: ``converge`` and ``investigate``.
EXHAUSTED = "exhausted"

#: It decided it could answer. ``investigate``.
ANSWERED = "answered"

#: The item came back. ``read_ticket``.
READ = "read"

#: It did not, and ``why`` says what happened. ``read_ticket``.
MISSING = "missing"

#: The command exited zero. ``try_shell``.
OK = "ok"

#: It did not, and ``exit_code`` and ``stderr`` say how. ``try_shell``.
FAILED = "failed"
