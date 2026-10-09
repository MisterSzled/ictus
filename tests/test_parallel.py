"""Parallel groups, and the validate_mcps stage built on them.

A group may contain only model calls and computations, members carry no routes
of their own, and a member's output is addressed through the group — the
direct form validates and renders empty.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ictus import (
    END,
    AgentNode,
    CompositionError,
    InputPort,
    McpServer,
    McpTransport,
    OutputPort,
    Pipeline,
    PortType,
    tpl,
)
from ictus.graph.composition import FailureMode
from ictus.graph.traversal import (
    back_edges,
    has_cycle,
    longest_cycle_length,
    may_be_unresolved,
    reachable_from_entry,
    require_loop_bound,
)
from ictus.interfaces.conductor import conductor
from ictus.lint import lint_pipeline
from ictus.stdlib import succeed, validate_mcps
from ictus.stdlib.llm import validate_mcp

if TYPE_CHECKING:
    from collections.abc import Callable

BOOL = PortType.BOOLEAN
STR = PortType.STRING


def _checker(node_id: str) -> AgentNode:
    return AgentNode(node_id=node_id, prompt="check", declared_outputs=(OutputPort("ok", BOOL),))


def _grouped() -> tuple[Pipeline, AgentNode, AgentNode]:
    p = Pipeline(pipeline_id="t")
    a, b = p.add(_checker("a")), p.add(_checker("b"))
    group = p.parallel("both", [a, b])
    done = p.add(succeed(node_id="done", reason="d"))
    p.set_entry(group)
    p.route(group, done)
    return p, a, b


class TestComposition:
    def test_a_group_needs_two_members(self) -> None:
        """Conductor rejects a one-member group; one member is just a node."""
        p = Pipeline(pipeline_id="t")
        a = p.add(_checker("a"))
        with pytest.raises(CompositionError, match="at least two members"):
            p.parallel("solo", [a])

    def test_members_must_belong_to_the_pipeline(self) -> None:
        p, other = Pipeline(pipeline_id="one"), Pipeline(pipeline_id="two")
        a = p.add(_checker("a"))
        stranger = other.add(_checker("b"))
        with pytest.raises(CompositionError, match="is not part of pipeline"):
            p.parallel("g", [a, stranger])

    def test_a_group_and_a_node_cannot_share_a_name(self) -> None:
        p = Pipeline(pipeline_id="t")
        a, b = p.add(_checker("a")), p.add(_checker("b"))
        with pytest.raises(CompositionError, match="one routing keyspace"):
            p.parallel("a", [a, b])

    def test_a_member_cannot_route_on_its_own(self) -> None:
        """Conductor rejects a member carrying routes; refuse it where it is written."""
        p, a, _ = _grouped()
        sink = p.add(succeed(node_id="sink", reason="s"))
        with pytest.raises(CompositionError, match="cannot have its own outgoing edge"):
            p.route(a, sink)

    def test_members_are_reachable_through_their_group(self) -> None:
        """Without this they look orphaned: a member has no inbound edge."""
        p, a, b = _grouped()
        assert {a.node_id, b.node_id} <= reachable_from_entry(p)
        assert lint_pipeline(p) == []


class TestEmission:
    def test_a_member_output_is_addressed_through_the_group(self) -> None:
        """The direct form passes validation and renders empty — the worst kind of wrong."""
        p, a, _ = _grouped()
        reader = p.add(AgentNode(node_id="reader", prompt=tpl("saw ", a.ref("ok"))))
        p.route(reader, END)
        agents = conductor.document(p)["agents"]
        assert isinstance(agents, list)
        emitted = next(x for x in agents if isinstance(x, dict) and x["name"] == "reader")
        assert emitted["prompt"] == "saw {{ both.outputs.a.ok }}"

    def test_members_emit_no_routes_and_the_group_does(self) -> None:
        p, _, _ = _grouped()
        doc = conductor.document(p)
        agents = doc["agents"]
        assert isinstance(agents, list)
        for name in ("a", "b"):
            entry = next(x for x in agents if isinstance(x, dict) and x["name"] == name)
            assert "routes" not in entry
        groups = doc["parallel"]
        assert isinstance(groups, list)
        assert groups[0] == {
            "name": "both",
            "agents": ["a", "b"],
            "failure_mode": "fail_fast",
            "routes": [{"to": "done"}],
        }

    def test_a_group_may_be_the_entry_point(self) -> None:
        p, _, _ = _grouped()
        workflow = conductor.document(p)["workflow"]
        assert isinstance(workflow, dict)
        assert workflow["entry_point"] == "both"

    def test_it_loads_in_conductor(self, validates: Callable[[Pipeline], None]) -> None:
        p, _, _ = _grouped()
        validates(p)


class TestValidateMcp:
    @staticmethod
    def _server(name: str = "github") -> McpServer:
        return McpServer(
            name=name,
            purpose="Read the repository",
            transport=McpTransport.HTTP,
            url="https://example.invalid/mcp",
        )

    def test_it_declares_a_branchable_verdict(self) -> None:
        node = validate_mcp(self._server())
        assert {p.name for p in node.outputs} == {"available", "detail"}
        assert node.get_output("available").port_type is BOOL

    def test_the_prompt_names_the_server_and_its_purpose(self) -> None:
        node = validate_mcp(self._server())
        assert isinstance(node.prompt, str)
        assert "github" in node.prompt
        assert "Read the repository" in node.prompt

    def test_it_forbids_reporting_success_without_a_call(self) -> None:
        """An agent asked 'is it there' will say yes; it must be asked to prove it."""
        node = validate_mcp(self._server())
        assert isinstance(node.prompt, str)
        assert "actually returned a result" in node.prompt


class TestValidateMcpsStage:
    @staticmethod
    def _servers(count: int) -> list[McpServer]:
        return [
            McpServer(name=f"svc{i}", purpose=f"do thing {i}", command="x") for i in range(count)
        ]

    def test_several_servers_are_checked_in_one_group(self) -> None:
        stage = validate_mcps(servers=self._servers(3))
        assert len(stage.body.groups) == 1
        assert len(stage.body.groups[0].members) == 3

    def test_one_server_needs_no_group(self) -> None:
        """Conductor rejects a one-member group, so a single check is just a node."""
        stage = validate_mcps(servers=self._servers(1))
        assert stage.body.groups == ()
        assert lint_pipeline(stage.body) == []

    def test_every_failure_is_reported_not_just_the_first(self) -> None:
        stage = validate_mcps(servers=self._servers(2))
        assert stage.body.groups[0].failure_mode is FailureMode.CONTINUE_ON_ERROR

    def test_the_gate_loops_back_so_a_fix_can_be_retried(self) -> None:
        """The point of a gate here: connect the thing, retry, keep the run."""
        stage = validate_mcps(servers=self._servers(2))
        gate = next(n for n in stage.body.nodes if n.node_id == "unblock")
        retry = next(e for e in stage.body.outgoing(gate) if e.case == "retry")
        assert retry.describe_target == "checks"
        assert has_cycle(stage.body)

    def test_an_empty_list_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one server"):
            validate_mcps(servers=[])

    def test_it_is_clean_and_loads(self, validates: Callable[[Pipeline], None]) -> None:
        stage = validate_mcps(servers=self._servers(2))
        assert lint_pipeline(stage.body) == []
        validates(stage.body)


class TestCyclesThroughGroups:
    """A group is a routing endpoint, so a loop can close on one.

    Skipping a non-``Node`` target reports the graph acyclic and prices the
    iteration budget for a straight line.
    """

    @staticmethod
    def _looping() -> Pipeline:
        p = Pipeline(pipeline_id="t", loop_passes=3)
        a, b = p.add(_checker("a")), p.add(_checker("b"))
        group = p.parallel("both", [a, b])
        decide = p.add(
            AgentNode(node_id="decide", prompt="again?", declared_outputs=(OutputPort("ok", BOOL),))
        )
        done = p.add(succeed(node_id="done", reason="d"))
        p.set_entry(group)
        p.route(group, decide)
        p.route(decide, done, when=tpl(decide.ref("ok")))
        p.route(decide, group)
        return p

    def test_a_loop_back_into_a_group_is_a_cycle(self) -> None:
        p = self._looping()
        assert has_cycle(p)
        assert [e.describe_target for e in back_edges(p)] == ["both"]

    def test_the_loop_is_priced_in_executions_not_hops(self) -> None:
        """The budget is in step executions, and a two-member group costs two.

        `both -> decide -> both` is two hops and three executions.
        """
        assert longest_cycle_length(self._looping()) == 3

    def test_an_unbounded_loop_through_a_group_is_still_refused(self) -> None:
        p = Pipeline(pipeline_id="t")
        a, b = p.add(_checker("a")), p.add(_checker("b"))
        group = p.parallel("both", [a, b])
        again = p.add(AgentNode(node_id="again", prompt="x"))
        p.set_entry(group)
        p.route(group, again)
        p.route(again, group)
        with pytest.raises(CompositionError, match="loop_passes"):
            require_loop_bound(p)


class TestRemediation:
    """The gate's third option: an agent that works the problem with the person.

    Conductor forbids ``dialog`` on a gate, so the helper is a separate node
    the gate routes to.
    """

    @staticmethod
    def _stage() -> Pipeline:
        return validate_mcps(
            servers=[McpServer(name=f"svc{i}", purpose=f"thing {i}", command="x") for i in range(2)]
        ).body

    def test_the_gate_offers_help_as_well_as_retry(self) -> None:
        body = self._stage()
        gate = next(n for n in body.nodes if n.node_id == "unblock")
        routes = {e.case: e.describe_target for e in body.outgoing(gate)}
        assert routes == {"assist": "assist", "retry": "checks", "abort": "aborted"}

    def test_the_helper_converses_rather_than_acting_silently(self) -> None:
        helper = next(n for n in self._stage().nodes if n.node_id == "assist")
        assert isinstance(helper, AgentNode)
        assert helper.dialog_trigger is not None

    def test_a_claimed_fix_is_re_checked_not_believed(self) -> None:
        body = self._stage()
        helper = next(n for n in body.nodes if n.node_id == "assist")
        verified = next(e for e in body.outgoing(helper) if e.when is not None)
        assert verified.describe_target == "checks"

    def test_a_helper_that_could_not_fix_it_does_not_re_run_the_checks(self) -> None:
        """Reconfirming a failure it just diagnosed wastes a round and shows
        the person the same screen twice."""
        body = self._stage()
        helper = next(n for n in body.nodes if n.node_id == "assist")
        fallback = next(e for e in body.outgoing(helper) if e.when is None)
        assert fallback.describe_target == "unblock"

    def test_the_gate_carries_forward_what_the_helper_found(self) -> None:
        """Without this, a second visit is indistinguishable from the first."""
        agents = conductor.document(self._stage())["agents"]
        assert isinstance(agents, list)
        gate = next(a for a in agents if isinstance(a, dict) and a["name"] == "unblock")
        prompt = gate["prompt"]
        assert isinstance(prompt, str)
        assert "{{ assist.output.summary }}" in prompt
        # Guarded, because on the first visit the helper has never run.
        assert "{% if assist is defined %}" in prompt

    def test_the_helper_is_told_never_to_take_a_credential(self) -> None:
        """A security boundary, not a nicety: no secret is pasted to a model."""
        helper = next(n for n in self._stage().nodes if n.node_id == "assist")
        assert isinstance(helper, AgentNode)
        rendered = conductor.document(self._stage())
        agents = rendered["agents"]
        assert isinstance(agents, list)
        prompt = next(a for a in agents if isinstance(a, dict) and a["name"] == "assist")["prompt"]
        assert isinstance(prompt, str)
        assert "Never ask for a token" in prompt
        assert "Never print the value of an environment variable" in prompt

    def test_dialog_is_emitted_in_conductors_shape(self) -> None:
        agents = conductor.document(self._stage())["agents"]
        assert isinstance(agents, list)
        entry = next(a for a in agents if isinstance(a, dict) and a["name"] == "assist")
        dialog = entry["dialog"]
        assert isinstance(dialog, dict)
        assert set(dialog) == {"trigger_prompt"}

    def test_the_longer_loop_is_priced_in(self) -> None:
        """checks -> verdict -> gate -> assist -> checks, where `checks` is a group.

        Four hops, five executions: the group runs both its members every pass.
        """
        assert longest_cycle_length(self._stage()) == 5

    def test_it_still_loads(self, validates: Callable[[Pipeline], None]) -> None:
        validates(self._stage())


class TestGroupMemberAvailability:
    """A group runs every member before it routes on, so member output is available.

    A guard testing a variable the context does not bind renders empty rather
    than failing, swallowing the value it was protecting.
    """

    @staticmethod
    def _downstream() -> tuple[Pipeline, AgentNode]:
        p = Pipeline(pipeline_id="t")
        a, b = p.add(_checker("a")), p.add(_checker("b"))
        group = p.parallel("both", [a, b])
        reader = p.add(
            AgentNode(
                node_id="reader",
                prompt=tpl("a=", a.ref("ok"), " b=", b.ref("ok")),
                declared_outputs=(OutputPort("seen", BOOL),),
            )
        )
        p.set_entry(group)
        p.route(group, reader)
        p.route(reader, END)
        return p, reader

    def test_member_output_is_not_treated_as_deferred(self) -> None:
        p, reader = self._downstream()
        a = next(n for n in p.nodes if n.node_id == "a")
        assert not may_be_unresolved(p, a, reader)

    def test_no_guard_is_emitted_around_it(self) -> None:
        p, _ = self._downstream()
        agents = conductor.document(p)["agents"]
        assert isinstance(agents, list)
        prompt = next(x for x in agents if isinstance(x, dict) and x["name"] == "reader")["prompt"]
        assert prompt == "a={{ both.outputs.a.ok }} b={{ both.outputs.b.ok }}"

    def test_a_guard_that_is_needed_names_the_group_not_the_member(self) -> None:
        """The member's own name is never bound in the context."""
        p = Pipeline(pipeline_id="t", loop_passes=2)
        a, b = p.add(_checker("a")), p.add(_checker("b"))
        group = p.parallel("both", [a, b])
        head = p.add(
            AgentNode(
                node_id="head",
                prompt=tpl("earlier=", a.ref("ok")),
                declared_outputs=(OutputPort("go", BOOL),),
            )
        )
        p.set_entry(head)
        p.route(head, group)
        p.route(group, head)  # loop: `head` runs before the group ever has
        agents = conductor.document(p)["agents"]
        assert isinstance(agents, list)
        prompt = next(x for x in agents if isinstance(x, dict) and x["name"] == "head")["prompt"]
        assert isinstance(prompt, str)
        assert "{% if both is defined %}" in prompt
        assert "{% if a is defined %}" not in prompt


class TestExposingGroupOutputs:
    """A member's output is addressed through its group, in the final map too.

    `member.output.field` is a name Conductor never binds: it validates,
    renders empty, and its guard is always false.
    """

    @staticmethod
    def _pipeline() -> Pipeline:
        p = Pipeline(pipeline_id="expose")
        a, b = p.add(_checker("a")), p.add(_checker("b"))
        group = p.parallel("both", [a, b])
        done = p.add(succeed(node_id="done", reason="d"))
        p.set_entry(group)
        p.route(group, done)
        p.expose_output("a_ok", a, "ok")
        p.expose_output("with_default", b, "ok", default="false")
        return p

    def test_a_member_is_exposed_through_its_group(self) -> None:
        out = conductor.document(self._pipeline())["output"]
        assert isinstance(out, dict)
        assert out["a_ok"] == "{{ both.outputs.a.ok }}"

    def test_the_guard_on_a_default_names_the_group_not_the_member(self) -> None:
        """`{% if a is defined %}` can never be true: a member is not bound by name."""
        out = conductor.document(self._pipeline())["output"]
        assert isinstance(out, dict)
        assert out["with_default"] == (
            "{% if both is defined %}{{ both.outputs.b.ok }}{% else %}false{% endif %}"
        )

    def test_it_still_loads(self, validates: Callable[[Pipeline], None]) -> None:
        validates(self._pipeline())


def test_a_member_cannot_read_a_sibling() -> None:
    """They run at the same time, and `group.outputs` exists only once all have finished.

    `both.outputs.a.v` is a well-formed group reference that resolves to
    nothing at run time.
    """
    p = Pipeline(pipeline_id="sib")
    a = p.add(_checker("a"))
    b = p.add(
        AgentNode(
            node_id="b",
            inputs=(InputPort("from_a", STR),),
            prompt="y",
            declared_outputs=(OutputPort("ok", BOOL),),
        )
    )
    p.parallel("both", [a, b])
    with pytest.raises(CompositionError, match="both run inside parallel group"):
        p.feed(a, "detail", b, "from_a")
