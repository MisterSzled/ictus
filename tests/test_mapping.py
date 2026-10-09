"""Fan-out over a run-time list.

That ictus can express Conductor's ``for_each``, and that what would
otherwise only be discoverable at run time is refused at composition.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ictus import Pipeline, PortType
from ictus.errors import CompositionError
from ictus.graph.mapping import Item
from ictus.graph.node import AgentNode, GateChoice, GateNode
from ictus.graph.ports import InputPort, OutputPort
from ictus.graph.ref import tpl
from ictus.interfaces.conductor import conductor
from ictus.lint import lint_pipeline
from ictus.stdlib.exits import succeed

if TYPE_CHECKING:
    from collections.abc import Callable

    from ictus.graph.values import YamlDict

STR, ARR, NUM, OBJ = PortType.STRING, PortType.ARRAY, PortType.NUMBER, PortType.OBJECT
SHAPE = {"repo": STR, "task": STR}


def _built() -> tuple[Pipeline, object]:
    p = Pipeline(pipeline_id="fan", provider="claude-agent-sdk")
    brief = p.declare_input("brief", STR)
    split = p.add(
        AgentNode(
            node_id="split",
            prompt=tpl("Split ", brief.ref()),
            inputs=(InputPort("brief", STR),),
            declared_outputs=(OutputPort("pieces", ARR, "One per repo", element=SHAPE),),
        )
    )
    piece = Item(name="piece", fields=SHAPE)
    work = p.add(
        AgentNode(
            node_id="work",
            prompt=tpl("In ", piece.ref("repo"), " do ", piece.ref("task")),
            declared_outputs=(OutputPort("summary", STR),),
        )
    )
    fanout = p.map_over(
        "workers", source=split.ref("pieces"), item=piece, body=work, expect_items=6, key_by="repo"
    )
    done = p.add(
        succeed(
            node_id="done",
            reason="all done",
            inputs=(InputPort("results", OBJ),),
            result={"results": tpl(fanout.ref("outputs"))},
        )
    )
    p.set_entry(split)
    p.connect_input(brief, split, "brief")
    p.route(split, fanout)
    p.route(fanout, done)
    p.feed(fanout, "outputs", done, "results")
    return p, fanout


def _group(pipeline: Pipeline) -> YamlDict:
    groups = conductor.document(pipeline)["for_each"]
    assert isinstance(groups, list)
    first = groups[0]
    assert isinstance(first, dict)
    return first


def test_it_emits_conductors_for_each_shape() -> None:
    group = _group(_built()[0])
    assert group["type"] == "for_each"
    assert group["source"] == "split.output.pieces"
    assert group["as"] == "piece"
    assert group["max_concurrent"] == 10
    assert group["key_by"] == "piece.repo"
    assert group["routes"] == [{"to": "done"}]


def test_the_loop_variable_is_not_addressed_as_a_step_output() -> None:
    """It is injected as a context root; `piece.output.repo` reads nothing."""
    template = _group(_built()[0])["agent"]
    assert isinstance(template, dict)
    assert template["prompt"] == "In {{ piece.repo }} do {{ piece.task }}"


def test_the_body_is_inline_and_not_also_a_step() -> None:
    """Emitted in both places, Conductor would schedule it once on its own."""
    doc = conductor.document(_built()[0])
    agents = doc["agents"]
    assert isinstance(agents, list)
    assert "work" not in [a["name"] for a in agents if isinstance(a, dict)]
    template = _group(_built()[0])["agent"]
    assert isinstance(template, dict)
    assert template["name"] == "work", "Conductor requires a name on the template"
    assert "routes" not in template, "an item cannot decide where the run goes next"


def test_the_aggregate_is_read_off_the_group_not_a_step() -> None:
    doc = conductor.document(_built()[0])
    agents = doc["agents"]
    assert isinstance(agents, list)
    done = next(a for a in agents if isinstance(a, dict) and a["name"] == "done")
    assert done["input"] == ["workers.outputs"]
    template = done["output_template"]
    assert isinstance(template, dict)
    assert template["results"] == "{{ workers.outputs | tojson }}"


def test_the_iteration_budget_accounts_for_the_fan_out() -> None:
    """Conductor charges one iteration per item, and the array's length is dynamic."""
    doc = conductor.document(_built()[0])
    workflow = doc["workflow"]
    assert isinstance(workflow, dict)
    limits = workflow["limits"]
    assert isinstance(limits, dict)
    assert limits["max_iterations"] == 3 + (6 - 1)


def test_it_is_lint_clean_and_loads_in_conductor(
    validates: Callable[[Pipeline], None],
) -> None:
    pipeline, _ = _built()
    assert lint_pipeline(pipeline) == []
    validates(pipeline)


def test_an_item_field_the_source_never_produces_is_refused() -> None:
    """Otherwise every iteration dies on "'dict object' has no attribute" — after paying."""
    p = Pipeline(pipeline_id="f")
    split = p.add(
        AgentNode(
            node_id="s",
            prompt="x",
            declared_outputs=(OutputPort("pieces", ARR, element={"repo": STR}),),
        )
    )
    body = p.add(AgentNode(node_id="b", prompt="y"))
    item = Item(name="piece", fields={"repo": STR, "task": STR})
    with pytest.raises(CompositionError, match=r"reads \['task'\]"):
        p.map_over("g", source=split.ref("pieces"), item=item, body=body, expect_items=3)


def test_an_undeclared_element_shape_is_refused() -> None:
    p = Pipeline(pipeline_id="f")
    split = p.add(AgentNode(node_id="s", prompt="x", declared_outputs=(OutputPort("pieces", ARR),)))
    body = p.add(AgentNode(node_id="b", prompt="y"))
    with pytest.raises(CompositionError, match="declares no element shape"):
        p.map_over(
            "g", source=split.ref("pieces"), item=Item("piece", SHAPE), body=body, expect_items=3
        )


def test_an_item_field_read_but_not_declared_is_refused() -> None:
    with pytest.raises(CompositionError, match="has no field 'nope'"):
        Item(name="piece", fields=SHAPE).ref("nope")


def test_mapping_over_something_that_is_not_a_list_is_refused() -> None:
    p = Pipeline(pipeline_id="f")
    src = p.add(AgentNode(node_id="s", prompt="x", declared_outputs=(OutputPort("v", STR),)))
    body = p.add(AgentNode(node_id="b", prompt="y"))
    with pytest.raises(CompositionError, match="which is string, not array"):
        p.map_over("g", source=src.ref("v"), item=Item("piece"), body=body, expect_items=3)


@pytest.mark.parametrize("name", ["workflow", "context", "output", "_index", "_key"])
def test_a_reserved_loop_variable_is_refused(name: str) -> None:
    p = Pipeline(pipeline_id="f")
    src = p.add(AgentNode(node_id="s", prompt="x", declared_outputs=(OutputPort("v", ARR),)))
    body = p.add(AgentNode(node_id="b", prompt="y"))
    with pytest.raises(CompositionError, match="reserved by Conductor"):
        p.map_over("g", source=src.ref("v"), item=Item(name), body=body, expect_items=3)


def test_a_body_conductor_cannot_run_inline_is_refused() -> None:
    """A gate wants the one dashboard prompt slot that N items cannot share."""
    p = Pipeline(pipeline_id="f")
    src = p.add(AgentNode(node_id="s", prompt="x", declared_outputs=(OutputPort("v", ARR),)))
    gate = p.add(
        GateNode(
            node_id="g2",
            prompt="ok?",
            choices=(GateChoice("yes", "Yes"), GateChoice("no", "No")),
        )
    )
    with pytest.raises(CompositionError, match="cannot iterate a human_decision"):
        p.map_over("g", source=src.ref("v"), item=Item("piece"), body=gate, expect_items=3)


def test_a_body_with_its_own_route_is_refused() -> None:
    p = Pipeline(pipeline_id="f")
    src = p.add(AgentNode(node_id="s", prompt="x", declared_outputs=(OutputPort("v", ARR),)))
    body = p.add(AgentNode(node_id="b", prompt="y"))
    after = p.add(succeed(node_id="done", reason="d"))
    p.route(body, after)
    with pytest.raises(CompositionError, match="has routes of its own"):
        p.map_over("g", source=src.ref("v"), item=Item("piece"), body=body, expect_items=3)


def test_an_undeclared_fan_out_width_is_refused() -> None:
    p = Pipeline(pipeline_id="f")
    src = p.add(AgentNode(node_id="s", prompt="x", declared_outputs=(OutputPort("v", ARR),)))
    body = p.add(AgentNode(node_id="b", prompt="y"))
    with pytest.raises(CompositionError, match="expect_items >= 1"):
        p.map_over("g", source=src.ref("v"), item=Item("piece"), body=body, expect_items=0)


def test_an_element_shape_on_a_non_array_port_is_refused() -> None:
    with pytest.raises(CompositionError, match="has no element shape"):
        OutputPort("x", STR, element={"a": STR})


def test_an_unknown_aggregate_port_is_refused() -> None:
    _, fanout = _built()
    with pytest.raises(CompositionError, match="has no output 'summary'"):
        fanout.ref("summary")  # type: ignore[attr-defined]


def test_mapping_over_a_stage_is_refused_rather_than_silently_wrong() -> None:
    """Conductor would accept it; ictus cannot yet bind an item to a child's ports.

    `input_mapping` is built from graph edges, and a loop item is not a node
    an edge can start from.
    """
    from ictus.graph.stage import Stage

    p = Pipeline(pipeline_id="f")
    src = p.add(AgentNode(node_id="s", prompt="x", declared_outputs=(OutputPort("v", ARR),)))
    stage = Stage(stage_id="child")
    stage.body.declare_input("thing", STR)
    inner = stage.body.add(
        AgentNode(node_id="inner", prompt="y", declared_outputs=(OutputPort("r", STR),))
    )
    stage.body.set_entry(inner)
    stage.body.expose_output("r", inner, "r")
    host = stage.instantiate(p, node_id="child")
    with pytest.raises(CompositionError, match="cannot iterate a stage yet"):
        p.map_over("g", source=src.ref("v"), item=Item("piece"), body=host, expect_items=3)


def test_a_map_body_cannot_be_read_as_an_ordinary_step() -> None:
    """There are N of it; a per-item output has no name of its own."""
    p, _ = _built()
    body = next(n for n in p.nodes if n.node_id == "work")
    reader = p.add(AgentNode(node_id="after", inputs=(InputPort("s", STR),), prompt="z"))
    with pytest.raises(CompositionError, match="only ever addressable as the group"):
        p.feed(body, "summary", reader, "s")
