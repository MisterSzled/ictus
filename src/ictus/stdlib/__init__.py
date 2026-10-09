"""Ready-made nodes, stages and scopes for the shapes that recur in pipelines.

Organised by what each thing is, in ``graph.NodeKind``'s vocabulary rather
than any engine's:

* ``gates``   — a run stops and waits for a person (``HUMAN_DECISION``, ``ASK``)
* ``llm``     — a model is asked something (``LLM_CALL``)
* ``steps``   — no model is called (``COMPUTATION``, ``SUBPROCESS``, ``DELAY``)
* ``exits``   — the run ends, distinguishably (``EXIT``)
* ``stages``  — a reusable sub-graph (``SUB_GRAPH``)
* ``scopes``  — a sub-graph whose every ending is a value the caller routes on

One primitive per module. ``llm`` is not re-exported here: those exist for the
stages, and reaching for one directly names ``ictus.stdlib.llm``.
"""

from __future__ import annotations

from ictus.stdlib.exits import fail, succeed
from ictus.stdlib.gates import approval_gate, ask_human, ask_human_for, choice_gate
from ictus.stdlib.scopes import (
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
    Attempt,
    Speaker,
    Voice,
    converge,
    council,
    investigate,
    read_ticket,
    roundtable,
    try_shell,
)
from ictus.stdlib.stages import (
    APPROVE_OR_REJECT,
    ReviewOption,
    ScriptStep,
    briefing_gate,
    resolve_unknowns,
    script_sequence,
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
    "resolve_unknowns",
    "roundtable",
    "save_text",
    "script_sequence",
    "shell",
    "succeed",
    "try_shell",
    "validate_mcps",
    "wait",
]
