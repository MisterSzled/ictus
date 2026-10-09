"""Councils and the voices on them.

Two things: that each voice is given something different to watch for, and
that "they never agreed" arrives as an outcome rather than a dead run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ictus import END, Pipeline, PortType
from ictus.errors import CompositionError
from ictus.graph.node import AgentNode
from ictus.graph.ports import InputPort, OutputPort
from ictus.graph.ref import tpl
from ictus.interfaces.conductor import conductor
from ictus.lint import lint_pipeline
from ictus.stdlib import AGREED, HALTED, UNRESOLVED, Voice, council, succeed
from ictus.stdlib.llm import voice

if TYPE_CHECKING:
    from collections.abc import Callable

    from ictus.graph.scope import Scope
    from ictus.graph.values import YamlDict

STR, NUM, BOOL = PortType.STRING, PortType.NUMBER, PortType.BOOLEAN

SPEAKERS = (
    Voice(node_id="perf", persona="Paged at 3am once too often.", focus="allocation and IO"),
    Voice(node_id="shape", persona="Maintains this code.", focus="fitting its neighbours"),
    Voice(
        node_id="risk",
        persona="Has watched a two-line change take a service down.",
        focus="what else depends on this",
    ),
)


def _council(**kwargs: object) -> Scope:
    settings: dict[str, object] = {"stage_id": "panel", "voices": SPEAKERS, "rounds": 3}
    settings.update(kwargs)
    return council(**settings)  # type: ignore[arg-type]


def _agent(pipeline: Pipeline, name: str) -> YamlDict:
    agents = conductor.document(pipeline)["agents"]
    assert isinstance(agents, list)
    for candidate in agents:
        if isinstance(candidate, dict) and candidate.get("name") == name:
            return candidate
    raise AssertionError(f"no agent named {name!r} was emitted")


def _host(scope: Scope, *, interject: bool = False) -> Pipeline:
    parent = Pipeline(pipeline_id="host", provider="claude-agent-sdk")
    material = parent.declare_input("material", STR)
    seat = scope.instantiate(parent, node_id="council")
    parent.set_entry(seat)
    parent.connect_input(material, seat, "subject")
    ok = parent.add(succeed(node_id="ok", reason="agreed"))
    split = parent.add(succeed(node_id="split", reason="contested"))
    routes = {AGREED: ok, UNRESOLVED: split, **({HALTED: split} if interject else {})}
    parent.branch_on_outcome(seat, routes)
    return parent


# --- voices -----------------------------------------------------------------


class TestVoice:
    def test_each_voice_is_told_something_different_to_watch_for(self) -> None:
        """Identical prompts make a council that agrees with itself."""
        prompts = {
            name: _agent(_council().body, name)["prompt"] for name in ("perf", "shape", "risk")
        }
        assert len(set(prompts.values())) == 3
        assert "allocation and IO" in str(prompts["perf"])
        assert "Paged at 3am once too often." in str(prompts["perf"])

    def test_the_verdict_is_a_boolean_a_route_can_test(self) -> None:
        outputs = _agent(_council().body, "perf")["output"]
        assert isinstance(outputs, dict)
        verdict = outputs["satisfied"]
        assert isinstance(verdict, dict)
        assert verdict["type"] == "boolean"

    def test_satisfaction_is_about_the_record_not_the_material(self) -> None:
        """Otherwise `agreed` is unreachable and every council exhausts its rounds.

        A voice assessing material with a real defect would withhold
        satisfaction forever.
        """
        prompt = str(_agent(_council().body, "perf")["prompt"])
        assert "It is about the record, not the material" in prompt
        assert "any disagreement you still hold included" in prompt

    def test_the_first_round_has_no_report_to_accept(self) -> None:
        assert "No report shown below means you have not seen one" in str(
            _agent(_council().body, "perf")["prompt"]
        )

    def test_a_voice_with_no_standpoint_is_refused(self) -> None:
        p = Pipeline(pipeline_id="v")
        src = p.add(AgentNode(node_id="s", prompt="x", declared_outputs=(OutputPort("t", STR),)))
        with pytest.raises(CompositionError, match="has no persona"):
            voice(node_id="v", persona="  ", focus="things", subject=src.ref("t"))

    def test_it_can_be_used_on_its_own(self) -> None:
        """Not council-only: one standpoint assessing one thing is a valid step."""
        p = Pipeline(pipeline_id="v")
        src = p.add(AgentNode(node_id="s", prompt="x", declared_outputs=(OutputPort("t", STR),)))
        solo = p.add(
            voice(
                node_id="v",
                persona="You care about naming.",
                focus="names",
                subject=src.ref("t"),
                inputs=(InputPort("t", STR),),
            )
        )
        done = p.add(succeed(node_id="d", reason="d"))
        p.set_entry(src)
        p.connect(src, "t", solo, "t")
        p.route(solo, done)
        assert lint_pipeline(p) == []


# --- councils ---------------------------------------------------------------


class TestCouncil:
    def test_the_voices_run_at_once(self) -> None:
        doc = conductor.document(_council().body)
        groups = doc["parallel"]
        assert isinstance(groups, list)
        first = groups[0]
        assert isinstance(first, dict)
        assert first["agents"] == ["perf", "shape", "risk"]

    def test_agreement_is_tested_over_the_actual_membership(self) -> None:
        """Hand-written, the conjunction stops matching the council that was convened."""
        routes = _agent(_council().body, "report")["routes"]
        assert isinstance(routes, list)
        agreed = routes[0]
        assert isinstance(agreed, dict)
        assert agreed["when"] == (
            "{{ voices.outputs.perf.satisfied"
            " and voices.outputs.shape.satisfied"
            " and voices.outputs.risk.satisfied }}"
        )

    def test_adding_a_voice_changes_the_agreement_test(self) -> None:
        extra = (*SPEAKERS, Voice(node_id="docs", persona="You read this next year.", focus="docs"))
        routes = _agent(_council(voices=extra).body, "report")["routes"]
        assert isinstance(routes, list)
        first = routes[0]
        assert isinstance(first, dict)
        assert "voices.outputs.docs.satisfied" in str(first["when"])

    def test_each_round_is_a_deliberation_not_a_re_poll(self) -> None:
        """Without the report fed back, round two is round one run again."""
        deps = [(d.source.node_id, d.target.node_id) for d in _council().body.data_deps]
        assert ("report", "perf") in deps
        assert ("report", "shape") in deps

    def test_the_prior_round_is_guarded_on_the_first_pass(self) -> None:
        """A voice told "the last round concluded:" with nothing after it invents one."""
        assert "{% if report is defined %}" in str(_agent(_council().body, "perf")["prompt"])

    def test_never_agreeing_is_an_outcome_rather_than_a_dead_run(self) -> None:
        scope = _council()
        assert scope.outcomes == (AGREED, UNRESOLVED)
        routes = _agent(scope.body, "report")["routes"]
        assert isinstance(routes, list)
        second = routes[1]
        assert isinstance(second, dict)
        assert second["when"] == "{{ round_number.output | int >= 3 }}"
        assert second["to"] == "unresolved"

    def test_both_outcomes_carry_the_report_and_the_disagreement(self) -> None:
        """Salvage: "four people could not agree, here is why" is a useful result."""
        for name in ("agreed", "unresolved"):
            template = _agent(_council().body, name)["output_template"]
            assert isinstance(template, dict)
            assert set(template) == {"outcome", "report", "dissent", "unverified", "rounds"}

    def test_the_synthesis_records_disagreement_rather_than_averaging_it(self) -> None:
        prompt = str(_agent(_council().body, "report")["prompt"])
        assert "Do not average them" in prompt
        assert "perf" in prompt and "shape" in prompt and "risk" in prompt

    def test_a_single_round_council_is_refused(self) -> None:
        """It can only come back unresolved: there is no report to be satisfied with."""
        with pytest.raises(CompositionError, match="needs rounds >= 2"):
            _council(rounds=1)

    def test_a_council_of_one_is_refused(self) -> None:
        with pytest.raises(CompositionError, match="at least two voices"):
            _council(voices=SPEAKERS[:1])

    def test_two_voices_with_the_same_name_are_refused(self) -> None:
        with pytest.raises(CompositionError, match="more than one voice named"):
            _council(voices=(SPEAKERS[0], SPEAKERS[0]))

    def test_it_is_lint_clean_and_loads_in_conductor(
        self, validates: Callable[[Pipeline], None]
    ) -> None:
        parent = _host(_council())
        assert lint_pipeline(parent) == []
        validates(parent)


class TestInterjection:
    def test_a_person_can_steer_stop_or_stand_back(self) -> None:
        options = _agent(_council(interject=True).body, "interject")["options"]
        assert isinstance(options, list)
        assert [o["value"] for o in options if isinstance(o, dict)] == [
            "continue",
            "steer",
            "stop",
        ]

    def test_stopping_is_its_own_outcome(self) -> None:
        """Taking the report early is not the same as the council having agreed."""
        assert _council(interject=True).outcomes == (AGREED, UNRESOLVED, HALTED)

    def test_direction_reaches_the_next_round(self) -> None:
        deps = [
            (d.source.node_id, d.target.node_id) for d in _council(interject=True).body.data_deps
        ]
        assert ("interject", "perf") in deps

    def test_direction_is_a_constraint_not_another_vote(self) -> None:
        prompt = str(_agent(_council(interject=True).body, "perf")["prompt"])
        assert "not a vote you can outweigh" in prompt

    def test_an_absent_steer_does_not_kill_the_next_round(self) -> None:
        """ "Let them carry on" leaves no notes, and reading them is a hard error.

        Guarding only the gate name renders "'dict object' has no attribute
        'notes'" on every round after a plain continue.
        """
        prompt = str(_agent(_council(interject=True).body, "perf")["prompt"])
        assert "interject.output.additional_input is defined" in prompt
        assert "interject.output.additional_input.notes is defined" in prompt

    def test_the_tally_declares_everything_its_routes_read(self) -> None:
        """A gate cannot test a counter, so the decision moves one zero-cost step on."""
        tally = _agent(_council(interject=True).body, "tallied")
        assert tally["type"] == "set"
        assert tally["input"] == [
            "interject.output.selected",
            "round_number.output",
            "voices.outputs.perf.satisfied",
            "voices.outputs.shape.satisfied",
            "voices.outputs.risk.satisfied",
        ]

    def test_it_is_lint_clean_and_loads_in_conductor(
        self, validates: Callable[[Pipeline], None]
    ) -> None:
        parent = _host(_council(interject=True), interject=True)
        assert lint_pipeline(parent) == []
        validates(parent)


def test_the_round_budget_covers_a_full_deliberation() -> None:
    """A group costs one execution per member, every round it runs."""
    doc = conductor.document(_council(interject=True).body)
    workflow = doc["workflow"]
    assert isinstance(workflow, dict)
    limits = workflow["limits"]
    assert isinstance(limits, dict)
    # round + 3 voices + report + gate + tally = 7 per round, 3 rounds, then an exit.
    budget = limits["max_iterations"]
    assert isinstance(budget, int)
    assert budget >= 7 * 3 + 1


def test_a_council_reads_what_the_step_before_it_worked_out(
    validates: Callable[[Pipeline], None],
) -> None:
    """The demo's shape: pull -> intent -> council, judged against the intent."""
    parent = Pipeline(pipeline_id="chained", provider="claude-agent-sdk")
    target = parent.declare_input("target", STR)
    purpose = parent.add(
        AgentNode(
            node_id="purpose",
            inputs=(InputPort("target", STR),),
            prompt=tpl("What is this for? ", target.ref()),
            declared_outputs=(OutputPort("intent", STR),),
        )
    )
    seat = _council().instantiate(parent, node_id="council")
    ok = parent.add(succeed(node_id="ok", reason="a"))
    parent.set_entry(purpose)
    parent.connect_input(target, purpose, "target")
    parent.connect_input(target, seat, "subject")
    parent.route(purpose, seat)
    parent.feed(purpose, "intent", seat, "intent")
    parent.branch_on_outcome(seat, {AGREED: ok, UNRESOLVED: ok})
    assert lint_pipeline(parent) == []
    validates(parent)


class TestVoiceCost:
    def test_a_voice_is_denied_tools_by_default(self) -> None:
        """Four voices with tools is four agents hunting the same file.

        `tools: []` and an omitted key are different to Conductor — none
        versus all — so this needs the empty list to be expressible.
        """
        assert _agent(_council().body, "perf")["tools"] == []

    def test_a_voice_can_be_given_tools_when_it_must_go_and_look(self) -> None:
        looks = (
            Voice(
                node_id="deps",
                persona="You check what depends on things.",
                focus="callers",
                tools=("read_file", "grep"),
            ),
            SPEAKERS[0],
        )
        assert _agent(_council(voices=looks).body, "deps")["tools"] == ["read_file", "grep"]


class TestCharge:
    """One instruction every voice receives, settable per run."""

    def test_it_reaches_every_voice(self) -> None:
        for name in ("perf", "shape", "risk"):
            prompt = str(_agent(_council().body, name)["prompt"])
            assert "--- what this council has been asked to do ---" in prompt
            assert "{{ workflow.input.charge }}" in prompt

    def test_it_is_an_input_so_it_can_change_per_run(self) -> None:
        declared = {p.name: p for p in _council().input_ports}
        assert set(declared) == {"subject", "charge", "intent"}
        assert declared["charge"].optional
        assert not declared["subject"].optional

    def test_an_unset_optional_input_renders_nothing_not_the_none_literal(self) -> None:
        """The engine binds an absent optional input to None, so `is defined` is true.

        Guarding on definedness puts the literal "None" under a heading.
        """
        prompt = str(_agent(_council().body, "perf")["prompt"])
        assert "{% if workflow.input.charge %}" in prompt
        assert "{% if workflow.input.intent %}" in prompt
        assert "is defined" not in prompt.split("--- the material ---")[0]


class TestVerification:
    """Agreement measures convergence between voices. It is not evidence.

    Four models given the same wrong material agree sooner, not later.
    """

    @staticmethod
    def _checked() -> Scope:
        return _council(verify="Check every claim against the source.")

    def test_agreement_alone_does_not_end_it(self) -> None:
        routes = _agent(self._checked().body, "verify")["routes"]
        assert isinstance(routes, list)
        agreed = routes[0]
        assert isinstance(agreed, dict)
        assert "verify.output.sound" in str(agreed["when"])
        assert str(agreed["to"]) == "agreed"

    def test_without_verification_agreement_is_the_only_test(self) -> None:
        routes = _agent(_council().body, "report")["routes"]
        assert isinstance(routes, list)
        first = routes[0]
        assert isinstance(first, dict)
        assert "sound" not in str(first["when"])

    def test_the_checker_can_go_and_look(self) -> None:
        """A verifier that cannot read the source is another voice with an opinion."""
        assert "tools" not in _agent(self._checked().body, "verify")

    def test_it_is_told_to_refute_rather_than_improve(self) -> None:
        prompt = str(_agent(self._checked().body, "verify")["prompt"])
        assert "Refute it; do not improve it" in prompt
        assert "Agreement between them is no evidence" in prompt

    def test_corrections_reach_the_next_round(self) -> None:
        """Otherwise the same refuted claim is argued again, with more confidence."""
        deps = [(d.source.node_id, d.target.node_id) for d in self._checked().body.data_deps]
        assert ("verify", "perf") in deps
        prompt = str(_agent(self._checked().body, "perf")["prompt"])
        assert "{% if verify is defined %}" in prompt
        assert "did not survive being checked" in prompt

    def test_what_was_struck_out_leaves_with_the_report(self) -> None:
        template = _agent(self._checked().body, "agreed")["output_template"]
        assert isinstance(template, dict)
        assert set(template) == {
            "outcome",
            "report",
            "dissent",
            "unverified",
            "rounds",
            "corrections",
        }

    def test_voices_must_ground_their_claims(self) -> None:
        prompt = str(_agent(_council().body, "perf")["prompt"])
        assert "A claim you have not checked is not a finding" in prompt

    def test_the_checker_is_handed_what_nobody_could_verify(self) -> None:
        """It is the shortest list of falsifiable claims in the round."""
        body = self._checked().body
        prompt = str(_agent(body, "verify")["prompt"])
        assert "--- what they could not check ---" in prompt
        assert "{{ report.output.unverified }}" in prompt
        deps = [
            (d.source.node_id, d.target.node_id, d.connection.target.name) for d in body.data_deps
        ]
        assert ("report", "verify", "unverified") in deps

    def test_it_is_lint_clean_and_loads(self, validates: Callable[[Pipeline], None]) -> None:
        parent = _host(self._checked())
        assert lint_pipeline(parent) == []
        validates(parent)


class TestUnverifiedClaims:
    """A lookup a voice could not perform must not leave as a finding.

    Without `unchecked`, a blocked voice has nowhere to say so but `concerns`,
    which the report reads as a change request.
    """

    def test_a_voice_declares_somewhere_to_put_a_failed_lookup(self) -> None:
        seat = _agent(_council().body, "perf")
        assert isinstance(seat["output"], dict)
        assert "unchecked" in seat["output"]

    def test_a_voice_is_told_not_to_launder_it_into_concerns(self) -> None:
        prompt = str(_agent(_council().body, "perf")["prompt"])
        assert "goes in `unchecked`" in prompt
        assert "never into `concerns`" in prompt
        assert "fact about this environment, not about" in prompt

    def test_every_voice_reaches_the_report_with_it(self) -> None:
        body = _council().body
        deps = {(d.source.node_id, d.connection.target.name) for d in body.data_deps}
        for spec in SPEAKERS:
            assert (spec.node_id, f"{spec.node_id}__unchecked") in deps

    def test_the_report_must_not_tidy_it_away(self) -> None:
        report = _agent(_council().body, "report")
        assert isinstance(report["output"], dict)
        assert "unverified" in report["output"]
        prompt = str(report["prompt"])
        assert "must not tidy away" in prompt
        assert "Voices agreeing is not evidence" in prompt

    def test_it_leaves_with_every_outcome(self) -> None:
        for name in ("agreed", "unresolved"):
            template = _agent(_council().body, name)["output_template"]
            assert isinstance(template, dict)
            assert template["unverified"] == "{{ report.output.unverified }}"


class TestVoiceTurnBudget:
    """A voice with tools and the default ceiling dies rather than throttling."""

    def test_a_voice_carries_its_own_ceiling_to_the_engine(self) -> None:
        speakers = (
            Voice(node_id="perf", persona="Paged once.", focus="io", tools=None, max_turns=250),
            Voice(node_id="shape", persona="Maintains it.", focus="fit", tools=None, max_turns=90),
        )
        body = _council(voices=speakers).body
        assert _agent(body, "perf")["max_agent_iterations"] == 250
        assert _agent(body, "shape")["max_agent_iterations"] == 90

    def test_a_voice_without_tools_needs_no_ceiling(self) -> None:
        """It cannot spend turns it has no way to spend."""
        assert "max_agent_iterations" not in _agent(_council().body, "perf")

    def test_tools_without_a_ceiling_is_refused_at_composition(self) -> None:
        """The failure it prevents costs a whole council to discover at run time."""
        with pytest.raises(CompositionError, match="tools but no max_turns"):
            _council(
                voices=(
                    Voice(node_id="perf", persona="Paged once.", focus="io", tools=None),
                    Voice(node_id="shape", persona="Maintains it.", focus="fit"),
                )
            )


class TestPerVoiceChecking:
    """`verify_each` puts a checker behind every voice, before the round is written.

    Removes triage: one checker facing thirty claims spends about a lookup on
    each, where four checkers have the same budget for a quarter each.
    """

    def _scope(self, **kwargs: object) -> Scope:
        return _council(verify_each="Check what this voice claimed.", **kwargs)

    def test_it_is_off_unless_asked_for(self) -> None:
        """It doubles the model calls in a round, so it is never the default."""
        names = {n.node_id for n in _council().body.nodes}
        assert not [n for n in names if n.endswith("_check")]

    def test_one_checker_per_voice(self) -> None:
        names = {n.node_id for n in self._scope().body.nodes}
        for spec in SPEAKERS:
            assert f"{spec.node_id}_check" in names

    def test_they_run_at_once_rather_than_in_sequence(self) -> None:
        """Independent checks in a chain spend four steps to learn four things."""
        body = self._scope().body
        group = next(g for g in body.groups if g.node_id == "checks")
        assert {m.node_id for m in group.members} == {f"{s.node_id}_check" for s in SPEAKERS}

    def test_each_reads_only_its_own_voice(self) -> None:
        deps = {
            (d.source.node_id, d.target.node_id, d.connection.target.name)
            for d in self._scope().body.data_deps
        }
        for spec in SPEAKERS:
            guard = f"{spec.node_id}_check"
            for port in ("position", "concerns", "unchecked"):
                assert (spec.node_id, guard, port) in deps
            others = [s.node_id for s in SPEAKERS if s.node_id != spec.node_id]
            assert not [o for o in others if (o, guard, "position") in deps]

    def test_what_they_strike_reaches_the_report(self) -> None:
        deps = {(d.source.node_id, d.connection.target.name) for d in self._scope().body.data_deps}
        for spec in SPEAKERS:
            assert (f"{spec.node_id}_check", f"{spec.node_id}__checked") in deps

    def test_a_voice_reads_its_own_checker_next_round(self) -> None:
        """Not the panel's: a voice can act on a claim of its own being struck."""
        prompt = str(_agent(self._scope().body, "perf")["prompt"])
        assert "{{ checks.outputs.perf_check.corrections }}" in prompt
        assert "shape_check" not in prompt

    def test_the_group_check_stops_feeding_voices_when_each_has_its_own(self) -> None:
        """One source per port, or the two corrections would collide on it."""
        scope = self._scope(verify="Check the report.")
        deps = [(d.source.node_id, d.target.node_id) for d in scope.body.data_deps]
        assert ("verify", "perf") not in deps
        assert ("perf_check", "perf") in deps

    def test_the_group_check_still_feeds_voices_when_it_is_the_only_one(self) -> None:
        """The existing shape has to keep working."""
        deps = [
            (d.source.node_id, d.target.node_id)
            for d in _council(verify="Check the report.").body.data_deps
        ]
        assert ("verify", "perf") in deps

    def test_the_report_is_told_not_to_carry_a_struck_claim(self) -> None:
        prompt = str(_agent(self._scope().body, "report")["prompt"])
        assert "did not survive checking" in prompt
        assert "do not quietly restate it" in prompt

    def test_a_checker_is_told_that_a_citation_is_not_a_claim(self) -> None:
        """Every citation in a report can be right and every inference wrong."""
        prompt = str(_agent(self._scope().body, "perf_check")["prompt"])
        assert "A correct citation is not a correct claim" in prompt
        assert "what nearby disqualifies the conclusion" in prompt

    def test_a_checker_can_go_and_look(self) -> None:
        """One that cannot is another voice with an opinion."""
        emitted = _agent(self._scope().body, "perf_check")
        assert "tools" not in emitted, "unset means the engine's full set"
        assert emitted["max_agent_iterations"] == 200

    def test_it_is_lint_clean_and_loads(self, validates: Callable[[Pipeline], None]) -> None:
        parent = _host(self._scope(verify="Check the report."))
        assert lint_pipeline(parent) == []
        validates(parent)


class TestVoicesAnswerEachOther:
    """Without this a voice reads only the synthesis, never its neighbours.

    A compression of what everybody said lets a voice restate its position but
    not disagree with anyone in particular.
    """

    def test_it_is_on_by_default(self) -> None:
        """It costs prompt tokens and no extra model calls."""
        prompt = str(_agent(_council().body, "perf")["prompt"])
        assert "in their own words" in prompt

    def test_a_voice_reads_every_other_voice(self) -> None:
        deps = {
            (d.source.node_id, d.target.node_id, d.connection.target.name)
            for d in _council().body.data_deps
        }
        for reader in SPEAKERS:
            for other in SPEAKERS:
                if other.node_id == reader.node_id:
                    continue
                assert (other.node_id, reader.node_id, f"{other.node_id}__said") in deps
                assert (other.node_id, reader.node_id, f"{other.node_id}__wants") in deps

    def test_a_voice_does_not_read_itself(self) -> None:
        deps = {(d.source.node_id, d.target.node_id) for d in _council().body.data_deps}
        for spec in SPEAKERS:
            assert (spec.node_id, spec.node_id) not in deps

    def test_the_edges_say_they_are_a_round_behind(self) -> None:
        """Seats run at once; only the previous pass is addressable."""
        peer_edges = [
            d
            for d in _council().body.data_deps
            if d.connection.target.name.endswith(("__said", "__wants"))
        ]
        assert peer_edges
        assert all(d.previous_pass for d in peer_edges)

    def test_it_asks_them_to_answer_by_name(self) -> None:
        prompt = str(_agent(_council().body, "perf")["prompt"])
        assert "Answer the ones you disagree with by name" in prompt
        assert "four assessments filed together, not a council" in prompt

    def test_the_neighbour_is_addressed_through_the_group(self) -> None:
        """A member's output is only nameable via its group, and only an edge knows."""
        emitted = _agent(_council().body, "perf")
        assert "{{ voices.outputs.shape.position }}" in str(emitted["prompt"])
        declared = emitted["input"]
        assert isinstance(declared, list)
        assert "voices.outputs.shape.position?" in declared
        assert "voices.outputs.shape.concerns?" in declared

    def test_turning_it_off_leaves_the_voices_isolated(self) -> None:
        body = _council(deliberate=False).body
        assert "in their own words" not in str(_agent(body, "perf")["prompt"])
        assert not [d for d in body.data_deps if d.connection.target.name.endswith("__said")]

    def test_it_is_lint_clean_and_loads(self, validates: Callable[[Pipeline], None]) -> None:
        parent = _host(_council(verify="Check it."))
        assert lint_pipeline(parent) == []
        validates(parent)


class TestReadingAPassThatNeverHappens:
    """`previous_pass` on a graph with no loop renders empty, every time."""

    def _flat(self) -> Pipeline:
        p = Pipeline(pipeline_id="t", provider="claude-agent-sdk")
        a = p.add(
            AgentNode(
                node_id="a",
                prompt="x",
                inputs=(InputPort("b", STR, optional=True),),
                declared_outputs=(OutputPort("position", STR),),
            )
        )
        b = p.add(
            AgentNode(node_id="b", prompt="y", declared_outputs=(OutputPort("position", STR),))
        )
        group = p.parallel("panel", [a, b])
        p.set_entry(group)
        p.route(group, END)
        return p

    def test_a_sibling_read_is_refused_without_the_flag(self) -> None:
        p = self._flat()
        by_id = {n.node_id: n for n in p.nodes}
        with pytest.raises(CompositionError, match="at the same time"):
            p.feed(by_id["b"], "position", by_id["a"], "b")

    def test_and_linted_with_it_when_there_is_no_loop(self) -> None:
        p = self._flat()
        by_id = {n.node_id: n for n in p.nodes}
        p.feed(by_id["b"], "position", by_id["a"], "b", previous_pass=True)
        assert [x for x in lint_pipeline(p) if "no loop" in x]
