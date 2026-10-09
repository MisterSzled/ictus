"""A bounded look/ask loop against a database that cannot be written to.

One step thinks, one step runs what it asked for, and the result comes back to
the same session. The thinking step has no tools: it can only ask for a
statement, which a node built from a read-only source runs. ``query`` refuses
a source that does not promise ``read_only``.

Bounded by ``looks``, after which the run ends with the best answer there is —
so the thinking step is told to keep one current.

``remember`` keeps the thinking step's session across passes and across
placements, so a second caller starts from what the first learned. Two
sequential callers is the case; two at once resuming one session is refused.

A failed statement comes back with ``ran: false`` and the engine's complaint
in ``why``, which the next pass reads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.errors import CompositionError
from ictus.graph.node import AgentNode
from ictus.graph.ports import InputPort, OutputPort, PortType
from ictus.graph.ref import at_least, equals, ref_to, tpl
from ictus.graph.scope import Scope, outcome_scope
from ictus.prompting import prompt
from ictus.stdlib.scopes.outcomes import ANSWERED, EXHAUSTED
from ictus.stdlib.steps.counter import counter as counter_step
from ictus.stdlib.steps.query import query

if TYPE_CHECKING:
    from ictus.graph.node import ReasoningEffort
    from ictus.graph.ref import Ref, Template
    from ictus.graph.requirements import Datasource

__all__ = ["ANSWERED", "EXHAUSTED", "investigate"]

ANSWER = "answer"
LOOKS = "looks"
LAST_SQL = "last_sql"

THINK = "think"
RUN = "run"
COUNTER = "look_number"

STR = PortType.STRING


def investigate(
    *,
    stage_id: str = "investigate",
    against: Datasource,
    looks: int = 3,
    reasoning: ReasoningEffort | None = None,
    model: str | None = None,
    max_turns: int | None = 6,
    limit: int = 50,
    timeout: int = 30,
    remember: bool = True,
    may_read_files: bool = False,
    environment: str | Ref | Template = "",
    subject: str = "a database",
    description: str = "",
    brief: str = "What to find out",
) -> Scope:
    """Answer a question by querying ``against``, at most ``looks`` times.

    Takes a ``question`` and an optional ``known`` — whatever the caller
    already worked out. Outcomes are ``answered`` and ``exhausted``; both carry
    ``answer``, the ``looks`` spent and the ``last_sql``.

    ``environment`` picks which of a fleet source's environments every
    statement goes to, once, for the whole investigation.

    ``may_read_files`` gives the thinking step the provider's built-in tools
    inside this stage only, with permissions auto-approved. A step with a shell
    can reach the connection around the query node, so read-only then rests on
    the credential rather than on the graph.

    ``reasoning`` and ``model`` set what the thinking costs, per stage.
    """
    if not against.read_only:
        raise CompositionError(
            f"investigate {stage_id!r} reads from {against.name!r}, which does not promise "
            "read_only. Build it with a constructor in ictus.sources that does — looking "
            "freely is only safe when looking is all it can do"
        )
    if looks < 1:
        raise CompositionError(f"investigate {stage_id!r} needs at least one look, got {looks}")

    scope = outcome_scope(
        stage_id=stage_id,
        outcomes=(ANSWERED, EXHAUSTED),
        carry={ANSWER: STR, LOOKS: PortType.NUMBER, LAST_SQL: STR},
        description=description or f"Investigate {subject} by querying it",
        # think, run, tally on every pass, and the pass that answers.
        loop_passes=looks + 1,
    )
    body = scope.body
    # On this body, so the credential belongs to this file and not its caller.
    body.require_datasource(against)
    if may_read_files:
        # A stage emits its own runtime block, which is the narrowest place a
        # tool grant can be drawn.
        body.native_tools = "claude_code"
    question = body.declare_input("question", STR, description=brief)
    known = body.declare_input(
        "known", STR, required=False, description="What the caller already worked out"
    )

    think = body.add(
        AgentNode(
            node_id=THINK,
            description=f"Decides what to look at in {subject}, then answers",
            inputs=(
                InputPort("question", STR),
                InputPort("known", STR, optional=True),
                InputPort("rows", STR, optional=True),
                InputPort("why", STR, optional=True),
                InputPort("tally", PortType.NUMBER, optional=True),
            ),
            # `None` takes whatever the stage grants. An empty tuple would be
            # a denial the provider refuses once anything is attached.
            tools=None,
            session_key=f"{stage_id}-{THINK}" if remember else None,
            reasoning=reasoning,
            model=model,
            max_turns=max_turns,
            prompt=tpl(
                f"You are investigating {subject}.\n"
                + (
                    "You may read files and run commands to understand the code, and "
                    "you reach the data by asking for a statement.\n\n"
                    if may_read_files
                    else "You can see it only through the statements you ask for.\n\n"
                ),
                "Set `need` to `query` and put one read in `sql` to look. Set `need` "
                "to `answer` when you can answer.\n"
                "Keep `answer` filled with the best answer you have at every pass — "
                f"after {looks} look(s) the stage ends and whatever is in it is what "
                "the caller gets.\n"
                "If you do not know the shape of what you are querying, ask for that "
                "first; you are not expected to guess it.\n"
                "Where the question can be read two ways, say in `answer` which "
                "reading you took.\n\n"
                "Question:\n\n",
                question.ref(),
                # Each is empty until there is something in it, and guarded.
                "\n\nAlready known:\n\n",
                known.ref(),
                "\n\nLast result:\n\n",
                ref_to(RUN, "rows", STR),
                "\n\n" + prompt(__name__, "last_sql") + "\n\n",
                ref_to(RUN, "why", STR),
            ),
            declared_outputs=(
                OutputPort("need", STR, "'query' to look again, 'answer' to stop"),
                OutputPort("sql", STR, "The statement to run, when looking"),
                OutputPort(ANSWER, STR, "The best answer so far"),
            ),
        )
    )
    run = body.add(
        query(
            node_id=RUN,
            description=f"Runs what {THINK} asked for, against a connection that cannot write",
            against=against,
            environment=environment,
            inputs=(InputPort(THINK, STR),),
            sql=think.ref("sql"),
            limit=limit,
            timeout=timeout,
        )
    )
    tally = body.add(counter_step(node_id=COUNTER, description="How many looks have been spent"))

    body.set_entry(think)
    body.connect_input(question, think, "question")
    body.connect_input(known, think, "known")

    carried = {ANSWER: think.ref(ANSWER), LOOKS: tally.ref("value"), LAST_SQL: think.ref("sql")}
    spent = scope.exit(
        node_id=EXHAUSTED,
        outcome=EXHAUSTED,
        reason=f"Out of looks after {looks}",
        **carried,
    )
    done = scope.exit(node_id=ANSWERED, outcome=ANSWERED, reason="Answered", **carried)

    # Conductor takes the first matching route, so the bound is tested before
    # the request: asking on the last pass ends the stage.
    body.route(think, spent, when=at_least(tally.ref("value"), looks))
    body.connect(think, "sql", run, THINK, when=equals(think.ref("need"), "query"))
    body.route(think, done)
    body.route(run, tally)
    body.route(tally, think)

    # Data travelling against the control flow is a feed, never an edge. One
    # entry carries one port, so each needs its own.
    body.feed(run, "rows", think, "rows")
    body.feed(run, "why", think, "why")
    body.feed(tally, "value", think, "tally")
    body.feed(tally, "value", tally, COUNTER)
    return scope
