"""The look/ask loop: bounded, read-only by construction, and resumable."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ictus import Datasource, EnvVar, Pipeline, PortType, ReasoningEffort
from ictus.errors import CompositionError
from ictus.graph.node import AgentNode, ScriptNode
from ictus.interfaces.conductor import conductor
from ictus.lint import lint_pipeline
from ictus.runspec.config import PipelineConfig
from ictus.sources.sqlite import readonly_sqlite
from ictus.stdlib import ANSWERED, investigate, succeed
from ictus.stdlib.scopes.investigate import EXHAUSTED

if TYPE_CHECKING:
    from ictus.graph.scope import Scope

STR = PortType.STRING


def _think(stage: Scope) -> AgentNode:
    """The stage's thinking step, typed."""
    node = next(n for n in stage.body.nodes if n.node_id == "think")
    assert isinstance(node, AgentNode)
    return node


def _source() -> Datasource:
    return readonly_sqlite(path=EnvVar("DB", "the file", secret=False))


def _staged(**over: object) -> Pipeline:
    """A pipeline with the stage placed once, wired and lintable."""
    #  resumes a session, which only claude-agent-sdk can do — the
    # lint says so, loudly, on any other provider.
    pipeline = Pipeline(pipeline_id="asks", provider="claude-agent-sdk")
    question = pipeline.declare_input("question", STR)
    pipeline.require_datasource(_source())
    stage = investigate(stage_id="looking", against=_source(), **over)  # type: ignore[arg-type]
    placed = stage.instantiate(pipeline, node_id="look")
    done = pipeline.add(succeed(node_id="done", reason="done"))
    # `remember` resumes a session, which only claude-agent-sdk can do. The
    # config is what carries that into a stage body: `apply` recurses, so a
    # body linted in isolation is not judged against the engine default.
    PipelineConfig(provider="claude-agent-sdk").apply(pipeline, where="test")
    pipeline.set_entry(placed)
    pipeline.connect_input(question, placed, "question")
    pipeline.route(placed, done)
    return pipeline


# --- read-only by construction ------------------------------------------------


def test_a_writable_source_is_refused() -> None:
    """Looking freely is only safe when looking is all it can do."""
    writable = Datasource(name="prod", purpose="everything", program="pass", read_only=False)
    with pytest.raises(CompositionError, match="does not promise read_only"):
        investigate(against=writable)


def test_the_thinking_step_is_given_no_tools() -> None:
    """Its only way out is a request for a statement, which is a node that
    cannot write. The guarantee is the absence of a path, not a prompt."""
    think = _think(investigate(against=_source()))
    assert think.tools is None


def test_the_statement_runs_through_the_declared_source() -> None:
    source = _source()
    body = investigate(against=source).body
    runner = next(n for n in body.nodes if isinstance(n, ScriptNode) and n.node_id == "run")
    assert runner.uses == (source.name,), "so the pipeline must declare what it reaches"


# --- bounded ------------------------------------------------------------------


def test_fewer_than_one_look_is_refused() -> None:
    with pytest.raises(CompositionError, match="at least one look"):
        investigate(against=_source(), looks=0)


@pytest.mark.parametrize("looks", [1, 3, 7])
def test_the_bound_is_what_the_caller_asked_for(looks: int) -> None:
    """Tested before the request, so asking on the last pass ends the stage
    rather than buying one more look."""
    body = investigate(against=_source(), looks=looks).body
    think = next(n for n in body.nodes if n.node_id == "think")
    first = body.outgoing(think)[0]
    assert first.describe_target == EXHAUSTED
    assert str(looks) in str(first.when)


def test_both_outcomes_carry_an_answer() -> None:
    """Running out is a value: `exhausted` brings back the best answer there
    was, not an apology."""
    stage = investigate(against=_source())
    carried = {port.name for port in stage.output_ports}
    assert {"answer", "looks", "last_sql"} <= carried
    assert {ANSWERED, EXHAUSTED} == set(stage.outcomes)


# --- resumable ----------------------------------------------------------------


def test_remembering_keeps_one_session_across_every_pass() -> None:
    """Placed twice, the second placement resumes the first — which is what
    makes a follow-up a continuation rather than a second stranger."""
    think = _think(investigate(stage_id="looking", against=_source()))
    assert think.session_key == "looking-think"


def test_forgetting_is_available_for_a_caller_that_wants_a_fresh_pair_of_eyes() -> None:
    think = _think(investigate(against=_source(), remember=False))
    assert think.session_key is None


def test_a_caller_can_hand_over_what_it_already_knows() -> None:
    """The explicit channel beside the session's implicit one."""
    accepted = {port.name for port in investigate(against=_source()).input_ports}
    assert {"question", "known"} <= accepted


# --- effort -------------------------------------------------------------------


def test_effort_and_model_are_set_per_stage() -> None:
    """This is usually the step that needs more than its neighbours."""
    think = _think(
        investigate(against=_source(), reasoning=ReasoningEffort.XHIGH, model="opus", max_turns=12)
    )
    assert think.reasoning is ReasoningEffort.XHIGH
    assert think.model == "opus"
    assert think.max_turns == 12


# --- it compiles --------------------------------------------------------------


def test_the_stage_lints_and_compiles_clean() -> None:
    assert lint_pipeline(_staged(), backend=conductor) == []


def test_it_emits_its_own_workflow_file() -> None:
    """A stage is a sibling `type: workflow`, not an inlined graph."""
    names = {document.filename for document in conductor.compile(_staged())}
    assert names == {"asks.yaml", "looking.yaml"}


# --- a stage is where a permission can be drawn -------------------------------


def test_the_connection_is_declared_on_the_stage_not_its_caller() -> None:
    """So a step outside is refused unless the pipeline announces it there too,
    which is what makes "only this may reach the database" a composition error
    rather than a note in a docstring."""
    stage = investigate(against=_source())
    assert [s.name for s in stage.body.datasources] == ["sqlite"]


def test_by_default_the_stage_grants_no_tools() -> None:
    assert investigate(against=_source()).body.native_tools is None


def test_reading_files_is_granted_inside_the_stage_and_stops_at_its_edge() -> None:
    """A stage emits its own runtime block, which is the narrowest scope a tool
    grant can be drawn in today."""
    stage = investigate(stage_id="looking", against=_source(), may_read_files=True)
    assert stage.body.native_tools == "claude_code"

    pipeline = Pipeline(pipeline_id="outer")
    question = pipeline.declare_input("question", STR)
    placed = stage.instantiate(pipeline, node_id="look")
    done = pipeline.add(succeed(node_id="done", reason="done"))
    pipeline.set_entry(placed)
    pipeline.connect_input(question, placed, "question")
    pipeline.route(placed, done)
    PipelineConfig(provider="claude-agent-sdk").apply(pipeline, where="test")
    assert pipeline.native_tools == "none", "the caller keeps the secure default"

    rendered = {d.filename: d.content for d in conductor.compile(pipeline)}
    assert "claude_code" in rendered["looking.yaml"]
    assert "claude_code" not in rendered["outer.yaml"]
