"""A voice — one standpoint, assessing something on its own terms.

Two prompts, not one: a *persona* (who is speaking) and a *focus* (what they
are watching for).

The output contract is fixed, because a council routes on it. ``satisfied``
asks about the *record*, not the material: "the report captures where I
stand". A voice that withholds it until the material is good withholds it
forever, and the loop can only run out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.errors import CompositionError
from ictus.graph.node import AgentNode
from ictus.graph.ports import InputPort, OutputPort, PortType
from ictus.graph.ref import optional, tpl
from ictus.prompting import prompt

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ictus.graph.ref import Ref, TemplatePart

__all__ = ["SATISFIED", "UNCHECKED", "voice"]

SATISFIED = "satisfied"
UNCHECKED = "unchecked"

_STANCE = prompt(__name__, "stance")


def voice(
    *,
    node_id: str,
    persona: str,
    focus: str,
    subject: Ref,
    charge: Ref | None = None,
    intent: Ref | None = None,
    prior: Ref | None = None,
    peers: Sequence[tuple[str, Ref, Ref]] = (),
    checked: Ref | None = None,
    direction: Ref | None = None,
    description: str = "",
    inputs: Sequence[InputPort] = (),
    tools: Sequence[str] | None = (),
    max_turns: int | None = None,
    remember: str | None = None,
) -> AgentNode:
    """One standpoint's assessment of ``subject``.

    ``charge`` is the framing every voice on a council shares, as against
    ``focus``, which is this one voice's. ``checked`` is what verification
    struck out of the last round.

    ``peers`` is ``(name, position, concerns)`` for each other seat, rendered
    verbatim and attributed so a voice answers the others rather than a
    summary. The references are a round behind, since the seats run at once.

    ``prior`` is the last round's synthesis and ``direction`` is a human
    interjection. Both are usually forward references into a loop, so they are
    wrapped in blocks that render to nothing until they resolve.

    ``tools`` has three states:

    * ``()`` — no tools, the default: a voice already has the material.
    * ``None`` — the engine's default set. What a voice assessing a repository
      wants, since the repository does not fit in a prompt.
    * a list — emitted as written; the conductor lint refuses it on a provider
      that cannot translate the names.

    ``max_turns`` matters only with ``tools=None``. The engine's default of
    fifty is a kill, not a throttle; a voice reading a repository needs a few
    hundred.

    ``remember`` names a session this voice resumes rather than starting cold.
    Keys must differ between voices, which run at once.
    """
    if not persona.strip():
        raise CompositionError(
            f"voice {node_id!r} has no persona. Two voices with the same standpoint are "
            "one voice run twice, and a council of them agrees with itself."
        )
    if not focus.strip():
        raise CompositionError(f"voice {node_id!r} has no focus")
    if tools is None and max_turns is None:
        raise CompositionError(
            f"voice {node_id!r} has tools but no max_turns. A voice that can go and look "
            "will, and the engine's default of fifty tool-use rounds is a kill rather "
            "than a throttle: the provider raises instead of returning what the step had, "
            "no scope can turn that into an outcome, and it takes the whole council down "
            "with it — after every earlier round has been paid for. Set max_turns "
            "explicitly (200 suits a voice reading a repository), or tools=() if this "
            "voice should assess only what it is handed."
        )

    parts: list[TemplatePart] = [
        f"{persona.strip()}\n\nYour focus: {focus.strip()}\n\n{_STANCE}\n\n",
    ]
    if charge is not None:
        parts += [optional("--- what this council has been asked to do ---\n", charge, "\n\n")]
    if intent is not None:
        parts += [optional("What this is meant to do:\n", intent, "\n\n")]
    parts += ["--- the material ---\n", subject, "\n"]
    if prior is not None:
        parts += [
            "\n",
            optional(
                "--- where the last round got to ---\n",
                prior,
                "\n\nRespond to it. Say plainly if it changed your mind, and say plainly "
                "if it did not — agreeing to close a discussion you still disagree with "
                "is the one thing that makes this whole exercise worthless.\n",
            ),
        ]
    if peers:
        block: list[TemplatePart] = []
        for name, position, concerns in peers:
            block += [
                optional(
                    f"\n### {name}\nsaid: ",
                    position,
                    "",
                ),
                optional("\nwants changed:\n", concerns, "\n"),
            ]
        parts += [
            "\n",
            "--- what the others said last round, in their own words ---\n",
            *block,
            "\nThese are their words, not a summary of them. Answer the ones you "
            "disagree with by name and say what would change your mind; where one of "
            "them has changed yours, say so and say which. A round where nobody "
            "addresses anybody is four assessments filed together, not a council.\n",
        ]
    if checked is not None:
        parts += [
            "\n",
            optional(
                "--- what verification struck out of the last round ---\n",
                checked,
                "\n\nThese claims did not survive being checked against the thing "
                "itself. Do not repeat them, and do not rebuild the same argument on "
                "a different one you have not checked either.\n",
            ),
        ]
    if direction is not None:
        parts += [
            "\n",
            optional(
                "--- direction from the person overseeing this ---\n",
                direction,
                "\n\nThis is not a vote you can outweigh. Take it as a constraint.\n",
            ),
        ]

    return AgentNode(
        node_id=node_id,
        description=description or f"{focus.strip()}",
        inputs=tuple(inputs),
        tools=None if tools is None else tuple(tools),
        max_turns=max_turns,
        session_key=remember,
        prompt=tpl(*parts),
        declared_outputs=(
            OutputPort(SATISFIED, PortType.BOOLEAN, f"Nothing left to change re: {focus}"),
            OutputPort("position", PortType.STRING, "Where this voice stands, in a sentence"),
            OutputPort("concerns", PortType.STRING, "What to change, one per line"),
            OutputPort(
                UNCHECKED,
                PortType.STRING,
                "What this voice could not verify, and what blocked it",
            ),
        ),
    )
