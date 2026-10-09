"""Scopes — the guarantee is that a failure arrives as a value, not an exception."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ictus import Pipeline, PortType, Scope, ScopeNode, outcome_scope
from ictus.errors import CompositionError
from ictus.graph.node import AgentNode
from ictus.graph.ports import OutputPort
from ictus.graph.ref import equals, tpl
from ictus.interfaces.conductor import conductor
from ictus.lint import lint_pipeline
from ictus.stdlib.exits import succeed

if TYPE_CHECKING:
    from collections.abc import Callable

    from ictus.graph.values import YamlDict


def _agent(pipeline: Pipeline, name: str) -> YamlDict:
    agents = conductor.document(pipeline)["agents"]
    assert isinstance(agents, list)
    for candidate in agents:
        if isinstance(candidate, dict) and candidate.get("name") == name:
            return candidate
    raise AssertionError(f"no agent named {name!r} was emitted")


def _terminals(pipeline: Pipeline) -> list[YamlDict]:
    agents = conductor.document(pipeline)["agents"]
    assert isinstance(agents, list)
    return [a for a in agents if isinstance(a, dict) and a.get("type") == "terminate"]


def _searcher() -> AgentNode:
    return AgentNode(
        node_id="search",
        description="Search",
        prompt="find it",
        declared_outputs=(
            OutputPort("path", PortType.STRING),
            OutputPort("hits", PortType.ARRAY),
            OutputPort("verdict", PortType.STRING),
        ),
    )


def _scope(**kwargs: object) -> Scope:
    scope = outcome_scope(
        stage_id="locate",
        outcomes=("found", "ambiguous", "missing"),
        carry={"path": PortType.STRING, "candidates": PortType.ARRAY},
        **kwargs,  # type: ignore[arg-type]
    )
    search = scope.body.add(_searcher())
    scope.body.set_entry(search)
    found = scope.exit(node_id="hit", outcome="found", reason="ok", path=search.ref("path"))
    amb = scope.exit(
        node_id="many", outcome="ambiguous", reason="many", candidates=search.ref("hits")
    )
    none = scope.exit(node_id="none", outcome="missing", reason="none")
    scope.body.route(search, amb, when=equals(search.ref("verdict"), "ambiguous"))
    scope.body.route(search, none, when=equals(search.ref("verdict"), "missing"))
    scope.body.route(search, found)
    return scope


def _parent(scope: Scope) -> tuple[Pipeline, ScopeNode]:
    parent = Pipeline(pipeline_id="p", provider="claude")
    node = scope.instantiate(parent)
    parent.set_entry(node)
    ok = parent.add(succeed(node_id="ok", reason="done"))
    ask = parent.add(succeed(node_id="ask", reason="human"))
    parent.branch_on_outcome(node, {"found": ok, "ambiguous": ask, "missing": ask})
    return parent, node


def test_every_exit_terminates_successfully() -> None:
    """A failed terminal in a child raises past the parent's routes, so there are none."""
    terminals = _terminals(_scope().body)
    assert len(terminals) == 3
    assert {t["status"] for t in terminals} == {"success"}


def test_every_exit_carries_every_declared_key() -> None:
    """A key on one branch and not another is a template error on the untested path."""
    for terminal in _terminals(_scope().body):
        template = terminal["output_template"]
        assert isinstance(template, dict)
        assert set(template) == {"outcome", "path", "candidates"}


def test_omitted_keys_get_a_literal_of_the_right_type() -> None:
    assert _agent(_scope().body, "none")["output_template"] == {
        "outcome": "missing",
        "path": "",
        "candidates": "[]",
    }


def test_a_carried_structure_round_trips_as_json() -> None:
    """Conductor json.loads the rendered value; a bare interpolation is a Python repr."""
    template = _agent(_scope().body, "many")["output_template"]
    assert isinstance(template, dict)
    assert template["candidates"] == "{{ search.output.hits | tojson }}"


def test_a_referenced_value_is_wired_as_well_as_rendered() -> None:
    """Under explicit context a template referring to an undeclared node is a hard error."""
    assert _agent(_scope().body, "hit")["input"] == ["search.output.path"]


def test_parent_branches_on_the_outcome() -> None:
    parent, _ = _parent(_scope())
    assert _agent(parent, "locate")["routes"] == [
        {"to": "ok", "when": "{{ locate.output.outcome == 'found' }}"},
        {"to": "ask", "when": "{{ locate.output.outcome == 'ambiguous' }}"},
        {"to": "ask", "when": "{{ locate.output.outcome == 'missing' }}"},
    ]


def test_an_exhaustive_scope_branch_is_not_flagged_as_a_dead_end() -> None:
    parent, _ = _parent(_scope())
    assert not [p for p in lint_pipeline(parent) if "only conditional routes" in p]


def test_the_parent_can_read_a_carried_value() -> None:
    parent, node = _parent(_scope())
    parent.expose_output("repo", node, "path")
    assert not lint_pipeline(parent)


def test_an_unrouted_outcome_is_refused() -> None:
    scope = _scope()
    parent = Pipeline(pipeline_id="p")
    node = scope.instantiate(parent)
    parent.set_entry(node)
    ok = parent.add(succeed(node_id="ok", reason="done"))
    with pytest.raises(CompositionError, match=r"unrouted outcome.*missing"):
        parent.branch_on_outcome(node, {"found": ok, "ambiguous": ok})


def test_an_outcome_the_scope_cannot_produce_is_refused() -> None:
    scope = _scope()
    parent = Pipeline(pipeline_id="p")
    node = scope.instantiate(parent)
    parent.set_entry(node)
    ok = parent.add(succeed(node_id="ok", reason="done"))
    with pytest.raises(CompositionError, match="cannot produce outcome"):
        parent.branch_on_outcome(
            node, {"found": ok, "ambiguous": ok, "missing": ok, "exploded": ok}
        )


@pytest.mark.parametrize("name", ["true", "False", "None", "null", "1", "2.5", "[x", "{y"])
def test_an_outcome_json_would_coerce_is_refused(name: str) -> None:
    """`_maybe_parse_json` turns these into non-strings, so every == test fails silently."""
    with pytest.raises(CompositionError, match=r"json\.loads"):
        outcome_scope(stage_id="s", outcomes=("ok", name))


@pytest.mark.parametrize("name", ["true", "False", "None", "null", "1", "2.5", "[x", "{y"])
def test_the_node_refuses_it_too_without_going_through_a_scope(name: str) -> None:
    """``ScopeNode`` is exported from ``ictus`` and can be built directly.

    The vocabulary was checked only by the builder, so a node constructed by
    hand carried outcomes the builder refuses by name — and
    ``branch_on_outcome`` then checked the routing was complete against a
    vocabulary that could never match at run time.
    """
    with pytest.raises(CompositionError, match=r"json\.loads"):
        ScopeNode(node_id="s", target="./s.yaml", outcomes=("ok", name))


@pytest.mark.parametrize("name", ["yes", "no", "off", "n1", "found"])
def test_an_outcome_json_leaves_alone_is_allowed(name: str) -> None:
    outcome_scope(stage_id="s", outcomes=("ok", name))


def test_an_undeclared_outcome_is_refused_at_the_exit() -> None:
    scope = outcome_scope(stage_id="s", outcomes=("a", "b"))
    with pytest.raises(CompositionError, match=r"has no outcome 'c'"):
        scope.exit(node_id="x", outcome="c", reason="r")


def test_an_undeclared_carry_key_is_refused() -> None:
    scope = outcome_scope(stage_id="s", outcomes=("a", "b"), carry={"x": PortType.STRING})
    with pytest.raises(CompositionError, match=r"carries \['y'\]"):
        scope.exit(node_id="e", outcome="a", reason="r", y="1")


def test_a_carried_value_of_the_wrong_type_is_refused() -> None:
    scope = outcome_scope(stage_id="s", outcomes=("a", "b"), carry={"x": PortType.ARRAY})
    node = scope.body.add(
        AgentNode(node_id="n", prompt="p", declared_outputs=(OutputPort("s", PortType.STRING),))
    )
    with pytest.raises(CompositionError, match="carries 'x' as string"):
        scope.exit(node_id="e", outcome="a", reason="r", x=node.ref("s"))


def test_a_stray_terminal_is_refused() -> None:
    """Anything not built by exit() can end the run without reporting an outcome."""
    scope = _scope()
    scope.body.add(succeed(node_id="leak", reason="oops"))
    parent = Pipeline(pipeline_id="p")
    with pytest.raises(CompositionError, match=r"terminal\(s\) \['leak'\]"):
        scope.instantiate(parent)


def test_an_unreported_outcome_is_refused() -> None:
    scope = outcome_scope(stage_id="s", outcomes=("a", "b", "c"))
    step = scope.body.add(AgentNode(node_id="n", prompt="p"))
    scope.body.set_entry(step)
    scope.body.route(step, scope.exit(node_id="ea", outcome="a", reason="r"), when=tpl("{{ x }}"))
    scope.body.route(step, scope.exit(node_id="eb", outcome="b", reason="r"))
    with pytest.raises(CompositionError, match=r"outcome\(s\) \['c'\] that no exit reports"):
        scope.instantiate(Pipeline(pipeline_id="p"))


def test_expose_output_on_a_scope_body_is_refused() -> None:
    """output_template replaces the workflow-level output:, so that block reads to nobody."""
    scope = _scope()
    scope.body.expose_output("x", scope.body.nodes[0], "path")
    with pytest.raises(CompositionError, match="uses expose_output"):
        scope.instantiate(Pipeline(pipeline_id="p"))


def test_a_single_outcome_is_refused() -> None:
    with pytest.raises(CompositionError, match="at least two distinct outcomes"):
        outcome_scope(stage_id="s", outcomes=("only",))


def test_a_looping_scope_must_bound_itself() -> None:
    """MaxIterationsError is not caught by _run_child_engine, so it escapes the routes."""
    scope = outcome_scope(stage_id="s", outcomes=("a", "b"))
    loop = scope.body.add(AgentNode(node_id="n", prompt="p"))
    scope.body.set_entry(loop)
    ea = scope.exit(node_id="ea", outcome="a", reason="r")
    eb = scope.exit(node_id="eb", outcome="b", reason="r")
    again = scope.body.add(AgentNode(node_id="again", prompt="p"))
    scope.body.route(loop, ea, when=tpl("{{ x }}"))
    scope.body.route(loop, again, when=tpl("{{ y }}"))
    scope.body.route(again, loop)  # the cycle
    scope.body.route(loop, eb)
    with pytest.raises(CompositionError, match="loops but sets no loop_passes"):
        scope.instantiate(Pipeline(pipeline_id="p"))


def test_a_nested_workflow_inherits_the_provider() -> None:
    """A child names no provider of its own, so Conductor would default it to copilot."""
    parent, _ = _parent(_scope())
    docs = {d.filename: d.content for d in conductor.compile(parent)}
    assert "name: claude" in docs["locate.yaml"]


def test_a_scope_and_its_parent_pass_the_backend_validator(
    validates: Callable[[Pipeline], None],
) -> None:
    parent, node = _parent(_scope())
    parent.expose_output("repo", node, "path")
    validates(parent)
