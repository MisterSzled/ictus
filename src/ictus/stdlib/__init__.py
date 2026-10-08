"""Ready-made nodes and stages for the shapes that recur in every pipeline.

Organised by what Conductor charges for them:

* ``gates``     — human decision points (``human_gate``)
* ``agents``    — model calls (``agent``)
* ``steps``     — zero-model steps (``set``, ``wait``, ``script``)
* ``terminals`` — explicit, distinguishable exits (``terminate``)
* ``stages``    — reusable sub-graphs (``workflow``)

One primitive per module, so the docstring next to a thing is about that thing.
"""

from __future__ import annotations

from ictus.stdlib.agents import briefing, remediate, validate_mcp, verdict, voice
from ictus.stdlib.gates import approval_gate, ask_human, ask_human_for, choice_gate
from ictus.stdlib.stages import (
    AGREED,
    ANSWERED,
    APPROVE_OR_REJECT,
    CONVERGED,
    EXHAUSTED,
    FAILED,
    HALTED,
    MISSING,
    OK,
    READ,
    UNRESOLVED,
    Attempt,
    ReviewOption,
    ScriptStep,
    Speaker,
    Voice,
    briefing_gate,
    converge,
    council,
    investigate,
    read_ticket,
    resolve_unknowns,
    roundtable,
    script_sequence,
    try_shell,
    validate_mcps,
)
from ictus.stdlib.steps import (
    announce,
    bindings,
    comment,
    constant,
    counter,
    fetch,
    query,
    save_text,
    shell,
    wait,
)
from ictus.stdlib.terminals import fail, succeed

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
    "announce",
    "approval_gate",
    "ask_human",
    "ask_human_for",
    "bindings",
    "briefing",
    "briefing_gate",
    "choice_gate",
    "comment",
    "constant",
    "converge",
    "council",
    "counter",
    "fail",
    "fetch",
    "investigate",
    "query",
    "read_ticket",
    "remediate",
    "resolve_unknowns",
    "roundtable",
    "save_text",
    "script_sequence",
    "shell",
    "succeed",
    "try_shell",
    "validate_mcp",
    "validate_mcps",
    "verdict",
    "voice",
    "wait",
]
