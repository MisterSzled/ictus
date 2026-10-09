"""A conversation — several people, taking turns, until they agree.

Each person reads the material alone, then they speak in order: the second has
heard the first this round, the last has heard everyone. As against `council`,
whose voices run at once and so can only answer a synthesis of the last round.

The cost is wall-clock: a round takes the sum of its turns, not the longest.
"""

from __future__ import annotations

import itertools
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ictus.errors import CompositionError
from ictus.graph.node import AgentNode, ComputeNode, GateChoice, GateNode
from ictus.graph.ports import InputPort, OutputPort, PortType
from ictus.graph.ref import at_least, equals, every, optional, ref_to, tpl
from ictus.graph.scope import Scope, outcome_scope
from ictus.prompting import prompt
from ictus.stdlib.scopes.outcomes import AGREED, HALTED, UNRESOLVED
from ictus.stdlib.steps.counter import counter as counter_step

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ictus.graph.composition import ParallelGroup
    from ictus.graph.node import Node
    from ictus.graph.ref import Ref, Template, TemplatePart

__all__ = ["AGREED", "HALTED", "UNRESOLVED", "Speaker", "roundtable"]

ROUND = "round_number"
STUDY = "study"
STUDY_SUFFIX = "_study"
MINUTES = "minutes"
INTERJECT = "interject"
TALLY = "tallied"
DIRECTION = "direction"

REMARK = "remark"
AGREE = "agree"
NOTES = "notes"
OPENING = "opening"

STR, NUM, BOOL = PortType.STRING, PortType.NUMBER, PortType.BOOLEAN


@dataclass(frozen=True, slots=True)
class Speaker:
    """One seat at the table: who is talking and what they are watching for."""

    node_id: str
    persona: str
    focus: str
    description: str = ""
    tools: tuple[str, ...] | None = ()
    """What this speaker may call. ``()`` denies tools, ``None`` gives the
    engine's default set. A speaker with tools needs ``max_turns``."""

    max_turns: int | None = None
    """Ceiling on tool-use rounds. Required when ``tools`` is ``None``; the
    engine's default of fifty is a kill, not a throttle."""


def roundtable(
    *,
    stage_id: str,
    speakers: Sequence[Speaker],
    subject: str = "What the table is discussing",
    charge: str = "What this discussion is for",
    rounds: int = 3,
    study: str = "",
    interject: bool = False,
    remember: bool = True,
    closing: str = "",
    description: str = "",
) -> Scope:
    """Sit ``speakers`` at a table and let them talk until they agree.

    Outcomes are ``agreed``, ``unresolved`` and — with ``interject`` on —
    ``halted``. All three carry the minutes, what was contested, and the round
    count.

    ``study`` is what each person does before anybody speaks. Those steps run
    at once, once, outside the loop, and each produces a public ``opening`` —
    a position formed with nobody else heard. Without them the first speaker
    frames the table.

    ``rounds`` is turns each, not turns total. ``closing`` is the extra
    instruction to whoever writes the minutes, which run once at the end.

    Order matters: later speakers hear the earlier ones from this round, the
    earlier ones hear them from the last.
    """
    if len(speakers) < 2:
        raise CompositionError(
            f"roundtable {stage_id!r} needs at least two speakers; one person taking "
            "turns with themselves is a monologue with extra steps"
        )
    repeated = sorted(n for n, c in Counter(s.node_id for s in speakers).items() if c > 1)
    if repeated:
        raise CompositionError(f"roundtable {stage_id!r} seats more than one {repeated}")
    if rounds < 1:
        raise CompositionError(
            f"roundtable {stage_id!r} needs rounds >= 1, got {rounds}. Unlike a council "
            "the first round is real conversation — everyone after the first speaker has "
            "already heard somebody — so one round is a short meeting rather than a "
            "pointless one."
        )
    for spec in speakers:
        if spec.tools is None and spec.max_turns is None:
            raise CompositionError(
                f"speaker {spec.node_id!r} has tools but no max_turns. The engine's "
                "default of fifty tool-use rounds is a kill rather than a throttle, and "
                "at a table it lands after every earlier turn has been paid for."
            )

    outcomes = (AGREED, UNRESOLVED, *((HALTED,) if interject else ()))
    scope = outcome_scope(
        stage_id=stage_id,
        outcomes=outcomes,
        carry={
            MINUTES: OutputPort(MINUTES, STR, "What the table concluded"),
            "dissent": OutputPort("dissent", STR, "What was still contested"),
            "unverified": OutputPort("unverified", STR, "What nobody could confirm"),
            "rounds": OutputPort("rounds", NUM, "How many rounds it took"),
        },
        description=description or f"Roundtable of {len(speakers)}: {subject}",
        loop_passes=rounds,
    )
    body = scope.body
    material = body.declare_input("subject", STR, description=subject)
    charge_in = body.declare_input("charge", STR, required=False, description=charge)
    # A second document alongside the material: what it is meant to do.
    intent_in = body.declare_input(
        "intent", STR, required=False, description="What the material is meant to do"
    )

    steer = ref_to(INTERJECT, "notes", STR) if interject else None

    # --- everyone reads on their own, once ---------------------------------
    desks: dict[str, AgentNode] = {}
    reading: ParallelGroup | None = None
    if study:
        for spec in speakers:
            desks[spec.node_id] = body.add(
                AgentNode(
                    node_id=f"{spec.node_id}{STUDY_SUFFIX}",
                    description=f"What {spec.node_id!r} makes of it alone",
                    inputs=(
                        InputPort("subject", STR),
                        InputPort("charge", STR, optional=True),
                        InputPort("intent", STR, optional=True),
                    ),
                    tools=spec.tools,
                    max_turns=spec.max_turns,
                    # The same session the speaker later talks from. Safe to
                    # share: a desk never runs concurrently with its speaker.
                    session_key=f"{stage_id}-{spec.node_id}" if remember else None,
                    prompt=tpl(
                        f"{spec.persona.strip()}\n\nYour focus: {spec.focus.strip()}\n\n",
                        study.strip(),
                        "\n\n" + prompt(__name__, "charge") + "\n\n",
                        optional("--- what this is for ---\n", charge_in.ref(), "\n\n"),
                        optional("--- what this is meant to do ---\n", intent_in.ref(), "\n\n"),
                        "--- the material ---\n",
                        material.ref(),
                    ),
                    declared_outputs=(
                        OutputPort(NOTES, STR, "What this person found alone"),
                        OutputPort(OPENING, STR, "Where they stood before anybody spoke"),
                    ),
                )
            )
        for desk in desks.values():
            body.connect_input(material, desk, "subject")
            body.connect_input(charge_in, desk, "charge")
            body.connect_input(intent_in, desk, "intent")
        reading = body.parallel(
            STUDY, list(desks.values()), description=f"{len(desks)} reading, at once"
        )
        body.set_entry(reading)

    counter = body.add(counter_step(node_id=ROUND, description="Which round this is"))
    if reading is not None:
        # Reading happens once; the loop goes back to the counter.
        body.route(reading, counter)
    else:
        body.set_entry(counter)
    body.feed(counter, "value", counter, ROUND)

    # --- the turns ----------------------------------------------------------
    seats: list[AgentNode] = []
    for index, spec in enumerate(speakers):
        # What everybody committed to before hearing anybody.
        opened: list[TemplatePart] = []
        if study:
            opened.append("--- what each person came in with, before anybody spoke ---\n")
            opened += [
                optional(
                    f"\n**{other.node_id}** opened with:\n",
                    ref_to(f"{other.node_id}{STUDY_SUFFIX}", OPENING, STR),
                    "\n",
                )
                for other in speakers
            ]
            opened.append("\n" + prompt(__name__, "notes") + "\n\n")

        heard: list[TemplatePart] = []
        for other in speakers:
            if other.node_id == spec.node_id:
                continue
            said: Ref = ref_to(other.node_id, REMARK, STR)
            # Everyone before you spoke this round, everyone after you last
            # round, and on the first round not at all.
            heard.append(optional(f"\n**{other.node_id}** said:\n", said, "\n"))
        seats.append(
            body.add(
                AgentNode(
                    node_id=spec.node_id,
                    description=spec.description or f"{spec.focus.strip()}",
                    inputs=(
                        InputPort("subject", STR),
                        InputPort("charge", STR, optional=True),
                        InputPort("intent", STR, optional=True),
                        InputPort(ROUND, NUM, "Which round this is"),
                        *((InputPort(NOTES, STR, "Your own reading"),) if study else ()),
                        *(
                            InputPort(f"{other.node_id}__opened", STR, optional=True)
                            for other in speakers
                            if study
                        ),
                        *(
                            InputPort(f"{other.node_id}__said", STR, optional=True)
                            for other in speakers
                            if other.node_id != spec.node_id
                        ),
                        *(
                            (InputPort(DIRECTION, STR, "Human direction", optional=True),)
                            if interject
                            else ()
                        ),
                    ),
                    tools=spec.tools,
                    max_turns=spec.max_turns,
                    session_key=f"{stage_id}-{spec.node_id}" if remember else None,
                    prompt=tpl(
                        f"{spec.persona.strip()}\n\nYour focus: {spec.focus.strip()}\n\n",
                        "Round ",
                        counter.ref("value"),
                        f" of {rounds}. ",
                        "You are one of several people working this out together, "
                        f"speaking {_position(index, len(speakers))}. This is a "
                        "conversation, not a survey: the others' words are below, and "
                        "the point of your turn is to move the discussion rather than "
                        "to restate where you stand.\n\n"
                        "So: answer the people you disagree with by name and say what "
                        "would change your mind. Say plainly when somebody has changed "
                        "yours, and which of them did. Where you have nothing to add to "
                        "a point somebody else has already made properly, say that "
                        "instead of making it again — agreement stated once is worth "
                        "more than the same position restated by four people.\n\n"
                        "Put your contribution in `remark`. It is the only thing the "
                        "others see of this turn, so it has to stand on its own.\n\n"
                        "Say plainly what you could not check. A lookup you failed to "
                        "make is not evidence about the thing you were looking for, and "
                        "at a table it is worth saying out loud: somebody else may be "
                        "able to make it, and the write-up needs to know which claims "
                        "rest on something nobody confirmed.\n\n"
                        "Set `agree` true when you would be content for the table to "
                        "stop here — meaning the discussion has covered what you came "
                        "with and your remaining disagreements, if any, are recorded "
                        "rather than resolved. Setting it true because the conversation "
                        "has become tiring is how a table agrees on something nobody "
                        "checked.\n\n",
                        optional("--- what this is for ---\n", charge_in.ref(), "\n\n"),
                        optional("--- what this is meant to do ---\n", intent_in.ref(), "\n\n"),
                        *(
                            (
                                "--- what you made of it on your own ---\n",
                                desks[spec.node_id].ref(NOTES),
                                "\n\n",
                            )
                            if study
                            else ()
                        ),
                        *opened,
                        "--- what has been said ---\n",
                        *heard,
                        *(
                            (
                                optional(
                                    "\n--- direction from the person overseeing this ---\n",
                                    steer,
                                    "\n\nThis is not a vote you can outweigh. Take it as "
                                    "a constraint.\n",
                                ),
                            )
                            if steer is not None
                            else ()
                        ),
                        "\n--- the material ---\n",
                        material.ref(),
                    ),
                    declared_outputs=(
                        OutputPort(REMARK, STR, "What this person says this turn"),
                        OutputPort(AGREE, BOOL, "Whether they would stop here"),
                    ),
                )
            )
        )

    for seat in seats:
        body.connect_input(material, seat, "subject")
        body.connect_input(charge_in, seat, "charge")
        body.connect_input(intent_in, seat, "intent")
        body.feed(counter, "value", seat, ROUND)
        if study:
            body.feed(desks[seat.node_id], NOTES, seat, NOTES)
            # Everyone's opening, this speaker's own included.
            for other in speakers:
                body.feed(desks[other.node_id], OPENING, seat, f"{other.node_id}__opened")

    # Turn order, and the edges that make each turn hear the ones before it.
    body.route(counter, seats[0])
    for earlier, later in itertools.pairwise(seats):
        body.route(earlier, later)
    for speaking in seats:
        for neighbour in seats:
            if neighbour is speaking:
                continue
            body.feed(neighbour, REMARK, speaking, f"{neighbour.node_id}__said")

    return _close(
        scope,
        seats=seats,
        counter=counter,
        rounds=rounds,
        interject=interject,
        closing=closing,
        desks=desks,
    )


def _position(index: int, total: int) -> str:
    if index == 0:
        return "first"
    if index == total - 1:
        return "last"
    return f"{index + 1} of {total}"


def _close(
    scope: Scope,
    *,
    seats: Sequence[AgentNode],
    counter: Node,
    rounds: int,
    interject: bool,
    closing: str,
    desks: Mapping[str, AgentNode],
) -> Scope:
    """The end of a round: write it up if it is over, otherwise go round again.

    The minutes sit outside the loop and run once, when the talking stops.
    """
    body = scope.body
    settled = every(*(seat.ref(AGREE) for seat in seats))
    spent = at_least(counter.ref("value"), rounds)

    minutes = body.add(
        AgentNode(
            node_id=MINUTES,
            description="Write up what the table concluded",
            inputs=(
                InputPort(ROUND, NUM, "Which round"),
                *(InputPort(f"{seat.node_id}__said", STR) for seat in seats),
                *(InputPort(f"{seat.node_id}__ok", BOOL) for seat in seats),
                *(
                    InputPort(f"{seat.node_id}__opened", STR, optional=True)
                    for seat in seats
                    if desks
                ),
                # Not read by the prompt: the route out tests it, and a
                # condition needs its references in scope like a template does.
                *((InputPort("choice", STR, optional=True),) if interject else ()),
            ),
            prompt=tpl(
                prompt(__name__, "minutes") + "\n",
                *((f"\n{closing.strip()}\n",) if closing else ()),
                *(
                    (
                        "\n--- what each person came in with, before anybody spoke ---\n",
                        *(
                            part
                            for seat in seats
                            for part in (
                                f"\n**{seat.node_id}** opened with:\n",
                                ref_to(f"{seat.node_id}{STUDY_SUFFIX}", OPENING, STR),
                                "\n",
                            )
                        ),
                        "\n" + prompt(__name__, "opening") + "\n",
                    )
                    if desks
                    else ()
                ),
                "\n--- the last thing each person said ---\n",
                *(
                    part
                    for seat in seats
                    for part in (f"\n**{seat.node_id}**:\n", seat.ref(REMARK), "\n")
                ),
            ),
            declared_outputs=(
                OutputPort("text", STR, "What the table concluded"),
                OutputPort("dissent", STR, "What is still contested, and by whom"),
                OutputPort("unverified", STR, "Claims nobody at the table could confirm"),
            ),
        )
    )
    for seat in seats:
        body.feed(seat, REMARK, minutes, f"{seat.node_id}__said")
        body.feed(seat, AGREE, minutes, f"{seat.node_id}__ok")
        if desks:
            body.feed(desks[seat.node_id], OPENING, minutes, f"{seat.node_id}__opened")
    body.feed(counter, "value", minutes, ROUND)

    agreed = scope.exit(
        node_id=AGREED,
        outcome=AGREED,
        reason="The table agreed",
        minutes=minutes.ref("text"),
        dissent=minutes.ref("dissent"),
        unverified=minutes.ref("unverified"),
        rounds=counter.ref("value"),
    )
    unresolved = scope.exit(
        node_id=UNRESOLVED,
        outcome=UNRESOLVED,
        reason=f"Still contested after {rounds} round(s)",
        minutes=minutes.ref("text"),
        dissent=minutes.ref("dissent"),
        unverified=minutes.ref("unverified"),
        rounds=counter.ref("value"),
    )

    # The last speaker hands over to whatever decides whether to go round again.
    decides: Node = seats[-1]
    if interject:
        gate = body.add(
            GateNode(
                node_id=INTERJECT,
                description="Read the round and steer it",
                inputs=(
                    InputPort(ROUND, NUM, "Which round"),
                    *(InputPort(f"{seat.node_id}__said", STR) for seat in seats),
                    *(InputPort(f"{seat.node_id}__ok", BOOL) for seat in seats),
                ),
                prompt=tpl(
                    "Round ",
                    counter.ref("value"),
                    f" of {rounds}.\n\n**Where it stands**\n\n",
                    *(
                        part
                        for seat in seats
                        for part in (
                            f"- `{seat.node_id}` would stop here: ",
                            tpl(seat.ref(AGREE)),
                            "\n",
                        )
                    ),
                    "\nCarrying on hands it back to the table, which finishes when "
                    f"everyone would stop, or runs another round — up to {rounds}.\n\n"
                    "--- the last thing each person said ---\n",
                    *(
                        part
                        for seat in seats
                        for part in (f"\n**{seat.node_id}**:\n", seat.ref(REMARK), "\n")
                    ),
                ),
                choices=(
                    GateChoice("continue", "Hand it back to the table"),
                    GateChoice(
                        "steer",
                        "Hand it back, with direction they must follow",
                        prompt_for="notes",
                        multiline=True,
                    ),
                    GateChoice("stop", "Stop here and write it up"),
                ),
            )
        )
        body.route(seats[-1], gate)
        for seat in seats:
            body.feed(seat, REMARK, gate, f"{seat.node_id}__said")
            body.feed(seat, AGREE, gate, f"{seat.node_id}__ok")
        body.feed(counter, "value", gate, ROUND)
        for seat in seats:
            body.feed(gate, "notes", seat, DIRECTION)
        # A gate's branches are the person's buttons, so it cannot also test
        # whether the table agreed. Both "carry on" branches meet here.
        tallied = body.add(
            ComputeNode(
                node_id=TALLY,
                description="Where the round left it",
                value="continue",
                value_type=STR,
                inputs=(
                    InputPort("choice", STR),
                    InputPort(ROUND, NUM),
                    *(InputPort(f"{seat.node_id}__ok", BOOL) for seat in seats),
                ),
                declared_outputs=(OutputPort("value", STR, "Marker"),),
            )
        )
        body.feed(gate, "selected", tallied, "choice")
        body.feed(gate, "selected", minutes, "choice")
        body.feed(counter, "value", tallied, ROUND)
        for seat in seats:
            body.feed(seat, AGREE, tallied, f"{seat.node_id}__ok")
        halted = scope.exit(
            node_id=HALTED,
            outcome=HALTED,
            reason="Stopped by the person overseeing the table",
            minutes=minutes.ref("text"),
            dissent=minutes.ref("dissent"),
            rounds=counter.ref("value"),
        )
        body.branch(gate, {"continue": tallied, "steer": tallied, "stop": minutes})
        body.route(minutes, halted, when=_stopped(gate))
        decides = tallied

    # Written up once, on the way out, whichever way that is.
    body.route(decides, minutes, when=settled)
    body.route(decides, minutes, when=spent)
    body.route(decides, counter)
    body.route(minutes, agreed, when=settled)
    body.route(minutes, unresolved)
    return scope


def _stopped(gate: Node) -> Template:
    """The person pressed stop, so the write-up is the end of it."""
    return equals(gate.ref("selected"), "stop")
