"""A bounded look/ask loop against a database that cannot be written to.

One step thinks, one step runs what it asked for, and the result comes back to
the same session. The thinking step has no tools of its own: it cannot open a
file, run a command or reach the connection. It can only *ask* for a statement,
and what runs one is a node built from a read-only source. "It must not write"
is therefore not an instruction in a prompt — there is no path from that step
to a write, and ``query`` refuses to be built against a source that does not
promise ``read_only``.

Bounded, because a loop that can always ask one more question will. After
``looks`` statements the run ends with the best answer there is, which is why
the thinking step is told to keep one current rather than saving it for the
end — ``exhausted`` carries an answer, not an apology.

Restartable, which is the point of ``remember``. The thinking step keeps one
session across every pass *and* across every placement of the stage, so a
second caller asking a harder question starts from what the first one learned
rather than from the schema again. Two callers in sequence is the case this is
for; two at once resuming one session is refused — at ``conductor validate``
where it can be seen, and by the provider's in-flight guard where it cannot,
which is what a fanned-out sub-workflow hits.

The failure this handles better than a single step is the wrong guess. A
statement against a table that does not exist comes back with ``ran: false``
and the engine's own complaint in ``why``, and that is what the next pass
reads — so the loop corrects its own assumptions instead of reporting them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.errors import CompositionError
from ictus.graph.node import AgentNode
from ictus.graph.ports import InputPort, OutputPort, PortType
from ictus.graph.ref import at_least, equals, ref_to, tpl
from ictus.graph.scope import Scope, outcome_scope
from ictus.stdlib.steps.counter import counter as counter_step
from ictus.stdlib.steps.query import query

if TYPE_CHECKING:
    from ictus.graph.node import ReasoningEffort
    from ictus.graph.ref import Ref, Template
    from ictus.graph.requirements import Datasource

__all__ = ["ANSWERED", "EXHAUSTED", "investigate"]

#: It decided it could answer.
ANSWERED = "answered"

#: It ran out of looks. The answer it had at that moment is carried anyway.
EXHAUSTED = "exhausted"

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

    The stage takes a ``question`` and an optional ``known`` — whatever a
    caller already worked out. ``known`` is the explicit channel and
    ``remember`` is the implicit one; a caller that re-enters the stage can use
    either, and using both is how a second pass starts from facts rather than
    from a summary of facts.

    Outcomes are ``answered`` and ``exhausted``. Both carry ``answer``, the
    number of ``looks`` spent and the ``last_sql``, so a caller can act on a
    partial result instead of only learning that there wasn't a whole one.

    ``environment`` picks which of a fleet source's environments every
    statement goes to — one choice for the whole investigation, made by whoever
    read the ticket, rather than one the thinking step can change mid-loop.

    ``may_read_files`` gives the thinking step the provider's built-in tools —
    filesystem, shell, web — *inside this stage only*. A stage carries its own
    ``runtime:``, so the grant stops at its edge and every step outside it keeps
    the secure default. Two things follow and neither is small. The preset is
    granted with permissions auto-approved, so nothing prompts. And a step with
    a shell can reach the connection directly, around the query node — so with
    this on, "read-only" rests on the credential (a user with no write grants,
    or a file nobody can write) rather than on the shape of the graph.

    ``reasoning`` and ``model`` set what the thinking costs. They are per-stage
    rather than per-pipeline because this is usually the step that needs more
    than its neighbours — and on a provider that declares no reasoning levels,
    ``conductor validate`` refuses the field rather than dropping it.
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
    # Declared on this body, so the credential is required by this file and not
    # by its caller. A step outside the stage built against the same source is
    # refused unless the pipeline announces it there too — which is what makes
    # "only this may reach the database" a composition error rather than a note.
    body.require_datasource(against)
    if may_read_files:
        # Scoped to this file. A stage emits its own runtime block, so this is
        # the narrowest place a tool grant can be drawn today.
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
            # `None` takes whatever the stage grants: nothing by default, and
            # the provider's preset when `may_read_files` is on. An empty tuple
            # would be a denial the provider refuses once anything is attached.
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
                # Each of these is empty until there is something to put in it,
                # and ictus guards every one of them.
                "\n\nAlready known:\n\n",
                known.ref(),
                "\n\nLast result:\n\n",
                ref_to(RUN, "rows", STR),
                "\n\nIf that is empty, this says why — a statement that failed is "
                "something to correct, not something to report:\n\n",
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

    # Route order is the conjunction. Conductor takes the first that matches, so
    # the bound is tested before the request: asking on the last pass ends the
    # stage rather than buying one more look.
    body.route(think, spent, when=at_least(tally.ref("value"), looks))
    body.connect(think, "sql", run, THINK, when=equals(think.ref("need"), "query"))
    body.route(think, done)
    body.route(run, tally)
    body.route(tally, think)

    # Data that travels backwards against the control flow, or across a node
    # that only counts: a feed, never an edge. One entry carries one port, so
    # the reason a statement failed needs its own.
    body.feed(run, "rows", think, "rows")
    body.feed(run, "why", think, "why")
    body.feed(tally, "value", think, "tally")
    body.feed(tally, "value", tally, COUNTER)
    return scope
