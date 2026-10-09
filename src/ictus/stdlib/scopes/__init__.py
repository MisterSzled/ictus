"""Scopes — stages whose every ending is a value the caller routes on.

A sub-workflow ending in a failed terminal raises before any parent route is
evaluated. A scope closes that: every exit is a success carrying a member of a
closed ``outcome`` vocabulary, and ``Pipeline.branch_on_outcome`` refuses to
leave one unrouted.

The vocabulary is in ``outcomes``, defined once and shared.
"""

from __future__ import annotations

from ictus.stdlib.scopes.converge import Attempt, converge
from ictus.stdlib.scopes.council import Voice, council
from ictus.stdlib.scopes.investigate import investigate
from ictus.stdlib.scopes.outcomes import (
    AGREED,
    ANSWERED,
    CONVERGED,
    EXHAUSTED,
    FAILED,
    HALTED,
    MISSING,
    OK,
    READ,
    UNRESOLVED,
)
from ictus.stdlib.scopes.read_ticket import read_ticket
from ictus.stdlib.scopes.roundtable import Speaker, roundtable
from ictus.stdlib.scopes.try_shell import try_shell

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
    "Attempt",
    "Speaker",
    "Voice",
    "converge",
    "council",
    "investigate",
    "read_ticket",
    "roundtable",
    "try_shell",
]
