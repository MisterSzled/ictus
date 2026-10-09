"""Attaching an integration: where announcements land, and what they can read."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from ictus import EnvVar, Integration, RunSignal
from ictus.assemble.announcements import OPENER_ID, apply_integrations
from ictus.assemble.start_gate import GATE_ID, add_start_gate
from ictus.errors import CompositionError
from ictus.graph.composition import END
from ictus.graph.node import (
    ComputeNode,
    GateChoice,
    GateNode,
    Node,
    Question,
    ScriptNode,
    TerminateNode,
)
from ictus.graph.pipeline import Pipeline
from ictus.graph.ports import InputPort, OutputPort, PortType
from ictus.graph.stage import Stage
from ictus.graph.traversal import budget_cost
from ictus.interfaces.conductor import ConductorBackend
from ictus.interfaces.environment import integration_issues
from ictus.lint import lint_pipeline
from ictus.notify.slack import slack_channel, slack_webhook
from ictus.stdlib import ask_human

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

S = PortType.STRING
EVERYTHING = (RunSignal.DECISION_NEEDED, RunSignal.RUN_FINISHED, RunSignal.RUN_FAILED)


def _channel(name: str = "slack", reports: tuple[RunSignal, ...] = EVERYTHING) -> Integration:
    return slack_channel(
        token=EnvVar("TEST_TOKEN", "a bot token"),
        channel=EnvVar("TEST_CHANNEL", "a channel id"),
        name=name,
        reports=reports,
    )


def _hook(reports: tuple[RunSignal, ...] = EVERYTHING) -> Integration:
    return slack_webhook(url=EnvVar("HOOK_URL", "where to post"), reports=reports)


def _step(node_id: str = "work") -> ComputeNode:
    return ComputeNode(node_id=node_id, value="x", declared_outputs=(OutputPort("value", S),))


def _gate(node_id: str = "approve", prompt: str = "Ship it?", **kwargs: object) -> GateNode:
    return GateNode(
        node_id=node_id,
        prompt=prompt,
        choices=(GateChoice("yes", "Yes"), GateChoice("no", "No")),
        **kwargs,  # type: ignore[arg-type]
    )


def _node(pipeline: Pipeline, node_id: str) -> Node:
    return next(n for n in pipeline.nodes if n.node_id == node_id)


def _next(pipeline: Pipeline, node: Node) -> list[str]:
    return [edge.describe_target for edge in pipeline.outgoing(node)]


def _ids(pipeline: Pipeline) -> set[str]:
    return {node.node_id for node in pipeline.nodes}


def _agents(pipeline: Pipeline) -> dict[str, Mapping[str, object]]:
    agents = ConductorBackend().document(pipeline)["agents"]
    assert isinstance(agents, list)
    return {str(a["name"]): a for a in agents if isinstance(a, dict)}


def _asks(node: Node) -> dict[str, object]:
    """The buttons an announcement offers, as its program reads them."""
    assert isinstance(node, ScriptNode)
    loaded = json.loads(str(node.args[3]))
    assert isinstance(loaded, dict)
    return loaded


def _gated(*targets: Integration) -> Pipeline:
    """A gate in front of one step, the way most pipelines look."""
    p = Pipeline(pipeline_id="demo")
    for target in targets:
        p.integrate(target)
    gate = p.add(_gate())
    work = p.add(_step())
    p.set_entry(gate)
    p.branch(gate, {"yes": work, "no": END})
    p.route(work, END)
    return p


# --- the start gate ----------------------------------------------------------


def test_the_start_gate_is_announced_with_its_own_choices_as_buttons() -> None:
    """The first gate every run stops at, and it used to go unannounced."""
    p = _gated(_channel())
    add_start_gate(p)
    apply_integrations(p)
    assert p.entry().node_id == OPENER_ID
    assert _next(p, _node(p, OPENER_ID)) == [f"report_{GATE_ID}"]
    assert _next(p, _node(p, f"report_{GATE_ID}")) == [GATE_ID]
    asks = _asks(_node(p, f"report_{GATE_ID}"))
    assert asks["gate"] == GATE_ID
    buttons = asks["buttons"]
    assert isinstance(buttons, list)
    assert [button[0] for button in buttons] == ["start", "cancel"]


def test_the_start_gate_counts_the_work_rather_than_the_reporting() -> None:
    """The person is deciding whether to spend the work; reporting on it is not that."""
    p = _gated(_channel())
    add_start_gate(p)
    apply_integrations(p)
    assert "2 step(s), beginning with `approve`" in str(_agents(p)[GATE_ID]["prompt"])


def test_declining_at_the_start_gate_is_reported_as_finishing() -> None:
    p = _gated(_channel())
    add_start_gate(p)
    apply_integrations(p)
    assert "report_not_started" in _ids(p)


# --- gates and questions, wherever they are --------------------------------


def _stage_with_a_gate(stage_id: str = "inner") -> Stage:
    stage = Stage(stage_id=stage_id)
    gate = stage.body.add(_gate(f"{stage_id}_gate"))
    done = stage.body.add(_step(f"{stage_id}_done"))
    stage.body.set_entry(gate)
    stage.body.branch(gate, {"yes": done, "no": done})
    stage.body.route(done, END)
    stage.body.expose_output("value", done, "value")
    return stage


def _two_deep() -> tuple[Pipeline, Stage, Stage]:
    inner = _stage_with_a_gate("inner")
    middle = Stage(stage_id="middle")
    hosted = inner.instantiate(middle.body)
    middle.body.set_entry(hosted)
    middle.body.route(hosted, END)
    middle.body.expose_output("value", hosted, "value")
    p = Pipeline(pipeline_id="outer")
    p.integrate(_channel())
    top = middle.instantiate(p)
    p.set_entry(top)
    p.route(top, END)
    return p, middle, inner


def test_a_gate_two_stages_down_reports_into_the_run_s_thread(
    validates: Callable[[Pipeline], None],
) -> None:
    """A child workflow sees its caller only through what it is passed."""
    p, middle, inner = _two_deep()
    apply_integrations(p)

    assert "report_inner_gate" in _ids(inner.body)
    for body in (middle.body, inner.body):
        (param,) = [w for w in body.workflow_inputs if w.name == OPENER_ID]
        assert not param.required, "every other caller of the stage stays valid"

    into_middle = _agents(p)["middle"]["input_mapping"]
    assert isinstance(into_middle, dict)
    assert "report_opened.output.thread | tojson" in str(into_middle[OPENER_ID])
    into_inner = _agents(middle.body)["inner"]["input_mapping"]
    assert isinstance(into_inner, dict)
    assert into_inner[OPENER_ID] == "{{ workflow.input.report_opened | tojson }}"
    assert "workflow.input.report_opened" in str(_agents(inner.body)["report_inner_gate"]["args"])
    validates(p)


def test_a_stage_placed_twice_is_announced_once_and_threaded_twice() -> None:
    stage = _stage_with_a_gate()
    p = Pipeline(pipeline_id="outer")
    p.integrate(_channel())
    first = stage.instantiate(p, node_id="first")
    second = stage.instantiate(p, node_id="second")
    p.set_entry(first)
    p.route(first, second)
    p.route(second, END)
    apply_integrations(p)
    assert sorted(n for n in _ids(stage.body) if n.startswith("report_")) == ["report_inner_gate"]
    for host in ("first", "second"):
        assert OPENER_ID in [port.name for port in _node(p, host).inputs]


def test_an_integration_declared_on_a_stage_is_attached() -> None:
    """A stage reports into its caller's run, so its declaration is applied from the top."""
    stage = _stage_with_a_gate()
    stage.body.integrate(_hook())
    p = Pipeline(pipeline_id="outer")
    host = stage.instantiate(p)
    p.set_entry(host)
    p.route(host, END)
    apply_integrations(p)
    assert "report_inner_gate" in _ids(stage.body)


def test_a_question_set_is_announced_without_buttons() -> None:
    """It presents one question at a time under one name; a button could not say which."""
    p = Pipeline(pipeline_id="demo")
    p.integrate(_channel())
    ask = p.add(ask_human(node_id="details", questions=(Question(text="Which branch?", id="b"),)))
    p.set_entry(ask)
    p.route(ask, END)
    apply_integrations(p)
    said = _node(p, "report_details")
    assert isinstance(said, ScriptNode)
    assert said.args[3] == ""


def test_an_announcement_reads_what_its_gate_reads(validates: Callable[[Pipeline], None]) -> None:
    """A reference written by hand into the prompt used to be blamed on the report."""
    p = Pipeline(pipeline_id="demo")
    p.integrate(_channel())
    work = p.add(_step())
    gate = p.add(_gate(prompt="Ship {{ work.output.value }}?", inputs=(InputPort("value", S),)))
    p.set_entry(work)
    p.connect(work, "value", gate, "value")
    p.branch(gate, {"yes": END, "no": END})
    apply_integrations(p)
    said = _node(p, "report_approve")
    assert [port.name for port in said.inputs] == ["value", OPENER_ID]
    assert lint_pipeline(p, backend=ConductorBackend()) == []
    validates(p)


# --- endings -----------------------------------------------------------------


def test_every_route_to_the_end_is_announced_and_a_branch_keeps_its_case() -> None:
    p = _gated(_channel())
    apply_integrations(p)
    finished = _node(p, "report_finished")
    assert not [e for e in p.edges if e.is_end and e.source is not finished]
    assert {e.case for e in p.edges if e.target is finished} == {"no", None}
    assert _next(p, finished) == ["END"]


def test_a_failed_exit_and_a_finished_one_say_which() -> None:
    p = Pipeline(pipeline_id="demo")
    p.integrate(_channel())
    gate = p.add(_gate())
    good = p.add(TerminateNode(node_id="good", status="success", reason="shipped"))
    bad = p.add(TerminateNode(node_id="bad", status="failed", reason="rejected"))
    p.set_entry(gate)
    p.branch(gate, {"yes": good, "no": bad})
    apply_integrations(p)
    agents = _agents(p)
    assert "finished" in str(agents["report_good"]["stdin"])
    assert "shipped" in str(agents["report_good"]["stdin"])
    assert "failed" in str(agents["report_bad"]["stdin"])
    assert "rejected" in str(agents["report_bad"]["stdin"])


def test_only_the_endings_asked_about_are_announced() -> None:
    p = _gated(_channel(reports=(RunSignal.DECISION_NEEDED,)))
    apply_integrations(p)
    assert "report_finished" not in _ids(p)


# --- more than one, and graphs that never pinned their entry ----------------


def test_two_threaded_integrations_each_keep_their_own_thread() -> None:
    """They used to collide over the single slot in front of the start gate."""
    p = _gated(_channel("ops"), _channel("team"))
    add_start_gate(p)
    attached = apply_integrations(p)
    assert [a.opener for a in attached] == [OPENER_ID, f"{OPENER_ID}_2"]
    readers = sorted(
        [port.name for port in node.inputs]
        for node in p.nodes
        if node.node_id.startswith("report_approve")
    )
    assert readers == [[OPENER_ID], [f"{OPENER_ID}_2"]]


def test_a_pipeline_that_never_pinned_its_entry_still_loads() -> None:
    p = Pipeline(pipeline_id="demo")
    p.integrate(_channel())
    gate = p.add(_gate())
    work = p.add(_step())
    p.branch(gate, {"yes": work, "no": END})
    p.route(work, END)
    apply_integrations(p)
    assert p.entry().node_id == OPENER_ID
    assert lint_pipeline(p, backend=ConductorBackend()) == []


def test_a_webhook_announces_with_neither_thread_nor_buttons() -> None:
    p = _gated(_hook())
    apply_integrations(p)
    said = _node(p, "report_approve")
    assert isinstance(said, ScriptNode)
    assert (said.args[2], said.args[3]) == ("", "")
    assert OPENER_ID not in _ids(p)


# --- what it costs -----------------------------------------------------------


def _looping(*, loop_passes: int | None) -> Pipeline:
    p = Pipeline(pipeline_id="demo", loop_passes=loop_passes)
    p.integrate(_channel())
    work = p.add(_step())
    gate = p.add(_gate())
    p.set_entry(work)
    p.route(work, gate)
    p.branch(gate, {"yes": END, "no": work})
    return p


def test_an_explicit_limit_grows_by_exactly_the_reporting_steps() -> None:
    """The limit budgets the work; reporting on it must not stop a run mid-pass."""
    p = _looping(loop_passes=3)
    p.max_iterations = budget_cost(p)
    apply_integrations(p)
    assert p.max_iterations == budget_cost(p)


def test_reporting_inside_a_loop_with_nothing_to_price_it_by_is_refused() -> None:
    p = _looping(loop_passes=None)
    p.max_iterations = 20
    with pytest.raises(CompositionError, match="cannot be priced"):
        apply_integrations(p)


def test_a_derived_limit_counts_the_reporting_steps() -> None:
    p = _looping(loop_passes=3)
    apply_integrations(p)
    workflow = ConductorBackend().document(p)["workflow"]
    assert isinstance(workflow, dict)
    limits = workflow["limits"]
    assert isinstance(limits, dict)
    assert limits["max_iterations"] == budget_cost(p)


# --- what preflight says -----------------------------------------------------


def test_a_moment_only_the_watcher_can_see_is_said_at_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured, passing preflight, and never arriving is the trap."""
    monkeypatch.setenv("TEST_TOKEN", "xoxb-pretend")
    monkeypatch.setenv("TEST_CHANNEL", "C0TEST")
    p = _gated(_channel(reports=(RunSignal.BUDGET_EXCEEDED, RunSignal.DECISION_NEEDED)))
    (issue,) = integration_issues(p)
    assert not issue.blocking
    assert "budget_exceeded" in issue.problem
    assert "ictus watch" in issue.problem
