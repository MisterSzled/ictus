"""A roundtable — several people taking turns until they agree.

Sequential turns, so the second speaker has heard the first this round. These
tests are about that ordering and what it costs: study once rather than every
round, minutes once rather than every round, and turn order visible in the
graph.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ictus import Pipeline, PortType
from ictus.errors import CompositionError
from ictus.interfaces.conductor import conductor
from ictus.lint import lint_pipeline
from ictus.stdlib import AGREED, HALTED, UNRESOLVED, Speaker, roundtable, succeed

if TYPE_CHECKING:
    from collections.abc import Callable

    from ictus.graph.scope import Scope
    from ictus.graph.values import YamlDict

STR = PortType.STRING

TABLE = (
    Speaker(node_id="alice", persona="Counts the money.", focus="what it costs"),
    Speaker(node_id="bob", persona="Has been paged.", focus="what breaks"),
    Speaker(node_id="carol", persona="Maintains it.", focus="whether it belongs"),
)


def _table(**kwargs: object) -> Scope:
    settings: dict[str, object] = {
        "stage_id": "table",
        "speakers": TABLE,
        "rounds": 3,
        "study": "Read it and form a view.",
    }
    settings.update(kwargs)
    scope = roundtable(**settings)  # type: ignore[arg-type]
    scope.body.provider = "claude-agent-sdk"
    return scope


def _agent(scope: Scope, name: str) -> YamlDict:
    agents = conductor.document(scope.body)["agents"]
    assert isinstance(agents, list)
    for candidate in agents:
        if isinstance(candidate, dict) and candidate.get("name") == name:
            return candidate
    raise AssertionError(f"no agent named {name!r} was emitted")


def _inputs(scope: Scope, name: str) -> list[str]:
    declared = _agent(scope, name)["input"]
    assert isinstance(declared, list)
    return [str(x) for x in declared]


def _host(scope: Scope, *, interject: bool = False) -> Pipeline:
    parent = Pipeline(pipeline_id="host", provider="claude-agent-sdk")
    material = parent.declare_input("material", STR)
    seat = scope.instantiate(parent, node_id="table")
    parent.set_entry(seat)
    parent.connect_input(material, seat, "subject")
    done = parent.add(succeed(node_id="done", reason="talked"))
    routes = {AGREED: done, UNRESOLVED: done, **({HALTED: done} if interject else {})}
    parent.branch_on_outcome(seat, routes)
    return parent


class TestTurnsAreSequential:
    """The whole reason this is not a council."""

    def test_each_speaker_hands_over_to_the_next(self) -> None:
        body = _table().body
        edges = {(e.source.node_id, getattr(e.target, "node_id", "$end")) for e in body.edges}
        assert ("alice", "bob") in edges
        assert ("bob", "carol") in edges

    def test_a_later_speaker_hears_the_earlier_ones_this_round(self) -> None:
        """No lag: alice has already spoken when carol reads her."""
        heard = _inputs(_table(), "carol")
        assert "alice.output.remark?" in heard
        assert "bob.output.remark?" in heard

    def test_the_first_speaker_hears_the_others_from_the_round_before(self) -> None:
        """Guarded, because on round one they have not spoken at all."""
        alice = _agent(_table(), "alice")
        assert "bob.output.remark?" in _inputs(_table(), "alice")
        assert "{% if bob is defined %}" in str(alice["prompt"])

    def test_nobody_reads_themselves(self) -> None:
        assert not [x for x in _inputs(_table(), "bob") if x.startswith("bob.")]

    def test_a_speaker_knows_which_round_it_is(self) -> None:
        """How long the table has left changes what is worth raising."""
        assert "round_number.output" in _inputs(_table(), "alice")
        assert "{{ round_number.output }}" in str(_agent(_table(), "alice")["prompt"])


class TestIndependenceBeforeInfluence:
    """Turn-taking anchors: only the first speaker is ever uninfluenced.

    Without a position recorded before anybody is heard, four agreeing
    speakers are one speaker and three confirmations.
    """

    def test_study_states_a_position_and_not_only_notes(self) -> None:
        study = _agent(_table(), "alice_study")
        assert isinstance(study["output"], dict)
        assert "opening" in study["output"]
        assert "nobody else has influenced" in str(study["prompt"])

    def test_every_speaker_sees_every_independent_opening(self) -> None:
        heard = _inputs(_table(), "alice")
        for who in ("alice", "bob", "carol"):
            assert f"study.outputs.{who}_study.opening?" in heard

    def test_a_speaker_is_asked_who_moved_it(self) -> None:
        prompt = str(_agent(_table(), "carol")["prompt"])
        assert "before anybody spoke" in prompt
        assert "the first person to speak framed it and nobody went back" in prompt

    def test_the_minutes_get_both_ends_of_the_conversation(self) -> None:
        """Convergence and capitulation look identical in the final positions."""
        minutes = str(_agent(_table(), "minutes")["prompt"])
        assert "who moved, and on whose argument" in minutes
        assert "three people who followed" in minutes
        assert "study.outputs.alice_study.opening" in minutes

    def test_without_study_there_are_no_openings_to_anchor_against(self) -> None:
        """The docstring says so; this is the shape that makes it true."""
        assert not [x for x in _inputs(_table(study=""), "alice") if "opening" in x]


class TestReadingHappensOnceAndAtOnce:
    """Study is independent, so it parallelises; it is also not per round."""

    def test_everyone_reads_at_the_same_time(self) -> None:
        group = next(g for g in _table().body.groups if g.node_id == "study")
        assert {m.node_id for m in group.members} == {f"{s.node_id}_study" for s in TABLE}

    def test_reading_is_outside_the_loop(self) -> None:
        """The loop goes back to the counter; re-reading every round is paid for."""
        body = _table().body
        back = {(e.source.node_id, getattr(e.target, "node_id", "$end")) for e in body.edges}
        assert ("carol", "round_number") in back
        assert not [t for s, t in back if t.endswith("_study") and s != "$entry"]

    def test_a_speaker_talks_from_the_session_it_read_in(self) -> None:
        """Otherwise it arrives with a summary of its own reading."""
        table = _table()
        assert _agent(table, "alice")["session_key"] == "table-alice"
        assert _agent(table, "alice_study")["session_key"] == "table-alice"

    def test_it_can_be_skipped(self) -> None:
        names = {n.node_id for n in _table(study="").body.nodes}
        assert not [n for n in names if n.endswith("_study")]


class TestTheTableStopsWhenEveryoneWouldStop:
    def test_consensus_is_every_speaker_agreeing(self) -> None:
        routes = _agent(_table(), "carol")["routes"]
        assert isinstance(routes, list)
        first = routes[0]
        assert isinstance(first, dict)
        assert first["when"] == (
            "{{ alice.output.agree and bob.output.agree and carol.output.agree }}"
        )

    def test_running_out_of_rounds_is_an_outcome_not_a_crash(self) -> None:
        routes = _agent(_table(), "carol")["routes"]
        assert isinstance(routes, list)
        second = routes[1]
        assert isinstance(second, dict)
        assert second["when"] == "{{ round_number.output | int >= 3 }}"

    def test_both_ways_out_are_written_up(self) -> None:
        routes = _agent(_table(), "carol")["routes"]
        assert isinstance(routes, list)
        assert [r["to"] for r in routes if isinstance(r, dict) and "when" in r] == [
            "minutes",
            "minutes",
        ]

    def test_the_minutes_run_once_rather_than_every_round(self) -> None:
        """A council pays a synthesis per round because its voices cannot hear
        each other. A table has already said everything to itself."""
        body = _table().body
        into = [
            e.source.node_id for e in body.edges if getattr(e.target, "node_id", None) == "minutes"
        ]
        # Two edges, one per way out, both from the last speaker — never a
        # separate write-up step inside the loop.
        assert set(into) == {"carol"}

    def test_a_failed_lookup_is_said_out_loud_and_kept(self) -> None:
        """The one signal this whole family of stages exists to protect."""
        assert "what you could not check" in str(_agent(_table(), "alice")["prompt"])
        minutes = str(_agent(_table(), "minutes")["prompt"])
        assert "Do not tidy it away" in minutes

    def test_every_outcome_carries_the_minutes(self) -> None:
        for name in ("agreed", "unresolved"):
            template = _agent(_table(), name)["output_template"]
            assert isinstance(template, dict)
            assert set(template) == {"outcome", "minutes", "dissent", "unverified", "rounds"}


class TestTheTableRefusesNonsense:
    def test_one_speaker_is_not_a_table(self) -> None:
        with pytest.raises(CompositionError, match="at least two speakers"):
            _table(speakers=(TABLE[0],))

    def test_the_same_person_cannot_sit_twice(self) -> None:
        with pytest.raises(CompositionError, match="more than one"):
            _table(speakers=(TABLE[0], TABLE[0]))

    def test_tools_without_a_ceiling_are_refused(self) -> None:
        """At a table the kill lands after every earlier turn is paid for."""
        with pytest.raises(CompositionError, match="tools but no max_turns"):
            _table(
                speakers=(
                    Speaker(node_id="a", persona="p", focus="f", tools=None),
                    TABLE[1],
                )
            )

    def test_zero_rounds_is_refused(self) -> None:
        with pytest.raises(CompositionError, match="rounds >= 1"):
            _table(rounds=0)

    def test_one_round_is_allowed_unlike_a_council(self) -> None:
        """Everyone after the first speaker has already heard somebody."""
        assert _table(rounds=1).body.nodes


class TestItLoadsAndLints:
    def test_clean_and_valid(self, validates: Callable[[Pipeline], None]) -> None:
        parent = _host(_table())
        assert lint_pipeline(parent, backend=conductor) == []
        validates(parent)

    def test_clean_and_valid_with_a_person_watching(
        self, validates: Callable[[Pipeline], None]
    ) -> None:
        parent = _host(_table(interject=True), interject=True)
        assert lint_pipeline(parent, backend=conductor) == []
        validates(parent)
