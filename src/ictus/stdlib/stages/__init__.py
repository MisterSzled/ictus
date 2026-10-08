"""Reusable stages — collections of nodes worth naming as a unit.

Each compiles to its own Conductor workflow file plus a single ``type: workflow``
agent in the parent, so a stage costs the caller one iteration however many
steps it contains, and the dashboard renders it as a nested group.
"""

from __future__ import annotations

from ictus.stdlib.stages.briefing_gate import APPROVE_OR_REJECT, ReviewOption, briefing_gate
from ictus.stdlib.stages.converge import CONVERGED, EXHAUSTED, Attempt, converge
from ictus.stdlib.stages.council import AGREED, HALTED, UNRESOLVED, Voice, council
from ictus.stdlib.stages.investigate import ANSWERED, investigate
from ictus.stdlib.stages.read_ticket import MISSING, READ, read_ticket
from ictus.stdlib.stages.resolve_unknowns import resolve_unknowns
from ictus.stdlib.stages.roundtable import Speaker, roundtable
from ictus.stdlib.stages.script_sequence import ScriptStep, script_sequence
from ictus.stdlib.stages.try_shell import FAILED, OK, try_shell
from ictus.stdlib.stages.validate_mcps import validate_mcps

__all__ = [
    "AGREED",
    "ANSWERED",
    "APPROVE_OR_REJECT",
    "CONVERGED",
    "EXHAUSTED",
    "FAILED",
    "HALTED",
    "MISSING",
    "OK",
    "READ",
    "UNRESOLVED",
    "Attempt",
    "ReviewOption",
    "ScriptStep",
    "Speaker",
    "Voice",
    "briefing_gate",
    "converge",
    "council",
    "investigate",
    "read_ticket",
    "resolve_unknowns",
    "roundtable",
    "script_sequence",
    "try_shell",
    "validate_mcps",
]
