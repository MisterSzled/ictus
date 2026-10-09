"""Stages — reusable sub-graphs worth naming as a unit.

Each compiles to its own workflow file plus one ``SUB_GRAPH`` node in the
parent, so a stage costs the caller one iteration however many steps it holds.

A stage that fails takes its caller down with it: the child engine turns a
failed terminal into an exception before any parent route is evaluated. For a
failure the caller should route on, use a ``Scope``.
"""

from __future__ import annotations

from ictus.stdlib.stages.briefing_gate import APPROVE_OR_REJECT, ReviewOption, briefing_gate
from ictus.stdlib.stages.resolve_unknowns import resolve_unknowns
from ictus.stdlib.stages.script_sequence import ScriptStep, script_sequence
from ictus.stdlib.stages.validate_mcps import validate_mcps

__all__ = [
    "APPROVE_OR_REJECT",
    "ReviewOption",
    "ScriptStep",
    "briefing_gate",
    "resolve_unknowns",
    "script_sequence",
    "validate_mcps",
]
