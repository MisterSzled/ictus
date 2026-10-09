"""Prompt text ictus ships, as against prompt text a pipeline writes.

One thing so far: the system prompt every model call gets unless its pipeline
says otherwise. It is here rather than with the config that selects it, because
selecting is not the same as being — ``config.yaml`` chooses this, a file of
your own, or none at all, and a sixty-line prompt sitting next to the YAML
parser read like a parser setting. Here rather than in ``graph`` for the
matching reason: this is content, and that package models structure.

Not a node, despite carrying a prompt. No step sends it; the emitter writes it
onto every agent that does not override one. A pipeline swaps it with
``system_prompt:`` in ``config.yaml``, which is the whole interface — so it is
deliberately not re-exported from ``ictus.stdlib``, because nothing composes
with it.

Why the text says what it does:

Conductor forwards ``AgentDef.system_prompt`` to the SDK, which turns ``None``
into ``--system-prompt ""`` — an empty one, not a default
(``claude_agent_sdk/_internal/transport/subprocess_cli.py``). Claude Code's own
preset cannot be asked for through Conductor: its field is ``str | None`` and
the preset is a mapping.

``Grep`` and ``Glob`` are not registered in an SDK session built with the flags
Conductor sends — the effective set appears only in the ``system``/``init``
event — so a step searches through ``Bash``, and turn economy is what is worth
saying about it.

Keep it short: it competes for attention with the step's own prompt.
"""

from __future__ import annotations

from ictus.prompting import prompt

__all__ = ["AGENT_BASELINE"]

AGENT_BASELINE = prompt(__name__, "text")
"""The default, read from ``text.md``."""
